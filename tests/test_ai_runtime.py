import importlib
from types import SimpleNamespace
import os

import pytest

from services.agent.models.lmstudio import LMStudioModel, ModelContextOverflow
from services.agent.session import AgentSession
from services.work_agent_service import WorkAgentService
from services.agent.token_budget import approximate_tokens, bound_messages
from services import ai_service


class _GeminiResponse:
    def __init__(self, status_code=200, text='OK'):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f'HTTP {self.status_code}')

    def json(self):
        return {'candidates': [{'content': {'parts': [{'text': self.text}]}}]}


def test_gemini_retries_one_transient_timeout(monkeypatch):
    calls = []
    monkeypatch.setenv('GEMINI_API_KEY', 'test-key')
    monkeypatch.setenv('GEMINI_MODEL', 'gemini-3.5-flash-lite')
    monkeypatch.setattr(ai_service._time, 'sleep', lambda *_: None)

    def post(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise ai_service.requests.ReadTimeout('slow response')
        return _GeminiResponse(text='Recovered')

    monkeypatch.setattr(ai_service.requests, 'post', post)
    assert ai_service._gemini_completion('hello') == 'Recovered'
    assert len(calls) == 2
    assert calls[0]['timeout'] == (5, 30)


def test_gemini_uses_stable_alias_when_configured_model_is_missing(monkeypatch):
    urls = []
    monkeypatch.setenv('GEMINI_API_KEY', 'test-key')
    monkeypatch.setenv('GEMINI_MODEL', 'retired-model')
    monkeypatch.setenv('GEMINI_FALLBACK_MODELS', 'gemini-flash-lite-latest')

    def post(url, **kwargs):
        urls.append(url)
        return _GeminiResponse(404) if 'retired-model' in url else _GeminiResponse(text='Fallback')

    monkeypatch.setattr(ai_service.requests, 'post', post)
    assert ai_service._gemini_completion('hello') == 'Fallback'
    assert len(urls) == 2


def test_gemini_quota_failure_does_not_spend_a_retry(monkeypatch):
    calls = []
    monkeypatch.setenv('GEMINI_API_KEY', 'test-key')
    monkeypatch.setattr(ai_service, '_quota_blocked_until', 0.0)
    monkeypatch.setattr(
        ai_service.requests,
        'post',
        lambda *args, **kwargs: calls.append(1) or _GeminiResponse(429),
    )

    with pytest.raises(RuntimeError, match='quota exceeded'):
        ai_service._gemini_completion('hello')
    assert calls == [1]
    monkeypatch.setattr(ai_service, '_quota_blocked_until', 0.0)


def test_prompt_budget_truncates_before_generation():
    messages = [
        {'role': 'system', 'content': 'rules ' * 1000},
        {'role': 'user', 'content': 'mail ' * 5000},
    ]
    bounded = bound_messages(messages, max_input_tokens=600)
    assert sum(approximate_tokens(item['content']) for item in bounded) <= 600
    assert 'truncated' in bounded[1]['content']


def test_host_never_routes_back_to_tailscale(monkeypatch):
    monkeypatch.setenv('MAILMATE_WORKER_HOST', '0.0.0.0')
    monkeypatch.setenv('MAILMATE_REMOTE_WORKER_URL', 'http://100.114.2.88:5000/api/compute')
    routes = LMStudioModel()._routes()
    assert [route[0] for route in routes] == ['local_lm_studio']


def test_client_uses_remote_work_compute_only(monkeypatch):
    monkeypatch.delenv('MAILMATE_WORKER_HOST', raising=False)
    monkeypatch.delenv('MAILMATE_CLIENT_LOCAL_LM', raising=False)
    monkeypatch.setenv('MAILMATE_REMOTE_WORKER_URL', 'http://100.114.2.88:5000/api/compute')
    routes = LMStudioModel()._routes()
    assert [route[0] for route in routes] == ['remote_local_worker']


def test_context_overflow_is_not_retried(monkeypatch):
    monkeypatch.setenv('MAILMATE_WORKER_HOST', '1')
    calls = []

    class Response:
        ok = False
        status_code = 400
        text = 'Context size has been exceeded'

    monkeypatch.setattr('services.agent.models.lmstudio.requests.post', lambda *args, **kwargs: calls.append(kwargs) or Response())
    with pytest.raises(ModelContextOverflow):
        LMStudioModel()._request_completion({
            'messages': [{'role': 'user', 'content': 'hello'}],
        })
    assert len(calls) == 1


def test_work_request_disables_thinking_and_is_bounded(monkeypatch):
    monkeypatch.setenv('MAILMATE_WORKER_HOST', '1')
    captured = {}

    class Response:
        ok = True
        status_code = 200

        @staticmethod
        def json():
            return {'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}]}

    def post(*args, **kwargs):
        captured.update(kwargs['json'])
        return Response()

    monkeypatch.setattr('services.agent.models.lmstudio.requests.post', post)
    LMStudioModel()._request_completion({
        'messages': [{'role': 'user', 'content': 'oversized ' * 6000}],
    }, request_kind='work', max_input_tokens=600)
    assert captured['chat_template_kwargs']['enable_thinking'] is False
    assert sum(approximate_tokens(item['content']) for item in captured['messages']) <= 600


def test_overview_is_deterministic_and_single_flight_cached(monkeypatch):
    ai_service._overview_cache.clear()
    ai_service._overview_inflight.clear()
    monkeypatch.setattr(ai_service.PrivacyGate, 'filter_threads_for_ai', lambda threads: threads)
    prompts = []

    def gemini(prompt, **kwargs):
        prompts.append(prompt)
        return '{"metrics":{},"needs_attention":[],"waiting_on_others":[],"ai_insight":"Clear"}'

    monkeypatch.setattr(ai_service, '_gemini_completion', gemini)
    threads = [{
        'id': 'thread-1',
        'messages': [{
            'id': 'message-1',
            'subject': 'Action required tomorrow',
            'sender': 'Teammate <team@example.com>',
            'snippet': 'Please review the portal. ' + ('private body ' * 1000),
            'direction': 'inbound',
            'timestamp': '2026-09-07T10:00:00Z',
        }],
    }]
    first = ai_service.get_dashboard_overview(threads)
    second = ai_service.get_dashboard_overview(threads)
    assert first == second
    assert prompts == []
    assert first['ai_insight'] == '1 message needs your attention.'
    assert first['needs_attention'][0]['description'] == 'Review this request.'


def test_overview_preserves_exact_day_first_deadline_from_context(monkeypatch):
    ai_service._overview_cache.clear()
    monkeypatch.setattr(ai_service.PrivacyGate, 'filter_threads_for_ai', lambda threads: threads)
    threads = [{'id': 'thread-deadline', 'messages': [{
        'id': 'message-deadline', 'subject': 'Confirm participation',
        'snippet': 'Please confirm attendance before 9 September 2026 at 3:00 PM.',
        'direction': 'inbound',
        'context_scores': {'attention_allowed': True, 'deadline_at': '2026-09-09T15:00:00+05:30'},
    }]}]
    result = ai_service.get_dashboard_overview(threads)
    assert result['needs_attention'][0]['deadline'] == '2026-09-09T15:00:00+05:30'


def test_calendar_deadline_parser_preserves_times_and_uses_end_of_day():
    timed, has_time = mailmate_app._deadline_target({'deadline': '2026-09-09T15:00:00+05:30'})
    date_only, date_has_time = mailmate_app._deadline_target({'deadline': '2026-09-10'})
    assert has_time is True
    assert (timed.hour, timed.minute) == (15, 0)
    assert date_has_time is False
    assert (date_only.hour, date_only.minute) == (23, 59)


def test_email_calendar_preview_without_time_is_all_day():
    source = {'id': 'mail-1', 'subject': 'Project due', 'snippet': 'Submit by September 10.'}
    attention = {'source_message_id': 'mail-1', 'deadline': '2026-09-10'}
    actions = mailmate_app._infer_agent_actions(
        'add this to my calendar',
        [{'type': 'email', 'id': 'mail-1', 'label': 'Project due'}],
        {'emails': [source], 'needs_attention': [attention]},
    )
    payload = next(action['args']['payload'] for action in actions if action['tool'] == 'calendar.preview_create')
    assert payload['all_day'] is True
    assert payload['start'] == '2026-09-10'
    assert payload['end'] == '2026-09-11'


def test_common_work_uses_one_structured_plan(monkeypatch):
    calls = []
    monkeypatch.setattr(LMStudioModel, 'work_plan', lambda self, source: calls.append(source) or {
        'summary': 'Reply prepared.',
        'checklist': ['Confirm the deadline', 'Reply to the sender'],
        'reply': {'subject': 'Re: Deadline', 'body': 'Hi,\n\nI will complete this tomorrow.\n\nBest,\nPriyam'},
        'artifacts': [],
    })
    service = WorkAgentService.__new__(WorkAgentService)
    service.timeout = 20
    monkeypatch.setattr(service, '_set_job_activity', lambda *args: None)
    session = AgentSession(
        job_id='work_test',
        user_id='user@example.com',
        goal='Prepare reply',
        source_email={},
        routing='LOCAL_ONLY',
    )
    result = service._run_fast_work_path(session, {
        'subject': 'Deadline tomorrow',
        'sender': 'Teacher <teacher@example.com>',
        'snippet': 'Please submit tomorrow.',
    })
    assert len(calls) == 1
    assert result.status == 'finished'
    assert result.reply_draft['body']
    assert result.checklist == ['Confirm the deadline', 'Reply to the sender']


os.environ.setdefault('MAILMATE_DISABLE_WHISPER_INIT', '1')
mailmate_app = importlib.import_module('app')


def test_user_avatar_proxies_authenticated_google_photo(monkeypatch):
    upstream = SimpleNamespace(
        content=b'jpeg-bytes',
        headers={'Content-Type': 'image/jpeg'},
        raise_for_status=lambda: None,
    )
    monkeypatch.setattr(mailmate_app, 'get_user_profile', lambda: {
        'email': 'user@example.com',
        'picture': 'https://lh3.googleusercontent.com/example',
    })
    monkeypatch.setattr(mailmate_app.requests, 'get', lambda url, timeout: upstream)

    response = mailmate_app.app.test_client().get('/api/user/avatar')

    assert response.status_code == 200
    assert response.data == b'jpeg-bytes'
    assert response.content_type == 'image/jpeg'
    assert response.headers['Cache-Control'] == 'private, max-age=3600'


def test_user_profile_caches_google_photo_for_avatar_proxy(monkeypatch):
    monkeypatch.setattr(mailmate_app, 'get_user_profile', lambda: {
        'email': 'user@example.com',
        'picture': 'https://lh3.googleusercontent.com/example',
    })
    client = mailmate_app.app.test_client()

    response = client.get('/api/user/profile')

    assert response.status_code == 200
    with client.session_transaction() as browser_session:
        assert browser_session['user_picture'] == 'https://lh3.googleusercontent.com/example'


def test_explicit_email_address_is_preserved(monkeypatch):
    monkeypatch.setattr(mailmate_app, 'chat_with_kyle', lambda prompt: '{"subject":"Portal deadline","body":"Hi,\\n\\nThe deadline is tomorrow.\\n\\nBest,\\nPriyam"}')
    result = mailmate_app._handle_mail_intent(
        'send an email to sreyankosinha@gmail.com about the portal deadline tomorrow',
        None,
        None,
        [],
        {'name': 'Priyam'},
    )
    assert result['actions'][0]['args']['to'] == 'sreyankosinha@gmail.com'


def test_compose_replaces_signoff_only_model_output(monkeypatch):
    monkeypatch.setattr(mailmate_app, 'chat_with_kyle', lambda _prompt: '{"subject":"Confirmation","body":"Best Regards,\\nPriyam"}')
    result = mailmate_app._handle_mail_intent(
        'send an email to attendee@example.com saying I confirm my attendance',
        None, None, [], {'name': 'Priyam'},
    )
    body = result['actions'][0]['args']['body']
    assert 'confirm my attendance' in body.lower()
    assert len(body) > len('Best Regards, Priyam')


def test_mail_send_operation_is_idempotent(monkeypatch):
    calls = []
    monkeypatch.setattr(mailmate_app, 'get_user_profile', lambda: {'email': 'user@example.com'})
    monkeypatch.setattr(mailmate_app, 'send_gmail_direct', lambda **kwargs: calls.append(kwargs) or {'id': 'sent-1'})
    mailmate_app._mail_send_operations.clear()
    client = mailmate_app.app.test_client()
    payload = {
        'operation_id': 'operation_12345',
        'to': 'person@example.com',
        'subject': 'Hello',
        'body': 'One message only.',
    }
    first = client.post('/api/mail/send', json=payload)
    second = client.post('/api/mail/send', json=payload)
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.get_json()['idempotent_replay'] is True
    assert len(calls) == 1


def test_mail_send_rejects_invalid_recipient(monkeypatch):
    monkeypatch.setattr(mailmate_app, 'get_user_profile', lambda: {'email': 'user@example.com'})
    client = mailmate_app.app.test_client()
    response = client.post('/api/mail/send', json={
        'operation_id': 'operation_67890',
        'to': 'Sreyanko',
        'subject': 'Hello',
        'body': 'Message',
    })
    assert response.status_code == 400
    assert response.get_json()['code'] == 'invalid_recipient'


def test_mailbox_snapshot_reuses_unchanged_history(monkeypatch):
    mailmate_app._mailbox_snapshots.clear()
    calls = {'threads': 0}
    monkeypatch.setattr(mailmate_app, 'get_gmail_history_id', lambda: 'history-7')

    def fetch_threads():
        calls['threads'] += 1
        email = {'id': 'm-1', 'thread_id': 't-1'}
        return [{'thread_id': 't-1', 'messages': [email]}], [email]

    monkeypatch.setattr(mailmate_app, 'get_gmail_threads', fetch_threads)
    profile = {'id': 'google-user-1'}
    first = mailmate_app._mailbox_snapshot(profile)
    second = mailmate_app._mailbox_snapshot(profile)
    assert calls['threads'] == 1
    assert first[2:4] == (False, True)
    assert second[2:4] == (True, False)


def test_manual_dashboard_refresh_reconciles_without_enqueuing_work(monkeypatch):
    calls = {'reconcile': 0, 'enqueue': 0}
    monkeypatch.setattr(mailmate_app, 'get_user_profile', lambda: {'email': 'user@example.com'})
    monkeypatch.setattr(mailmate_app, '_build_live_dashboard', lambda profile, force_ai=False: {
        'emails': [], 'needs_attention': [], 'metrics': {}, 'user': profile,
    })
    monkeypatch.setattr(mailmate_app.work_agent_service, 'reconcile_jobs_with_gmail', lambda user_id: calls.__setitem__('reconcile', calls['reconcile'] + 1))
    monkeypatch.setattr(mailmate_app.work_agent_service, 'sync_and_enqueue', lambda *args: calls.__setitem__('enqueue', calls['enqueue'] + 1))
    monkeypatch.setattr(mailmate_app.work_agent_service, 'resolved_source_message_ids', lambda user_id: set())
    response = mailmate_app.app.test_client().get('/api/dashboard/overview?refresh=true')
    assert response.status_code == 200
    assert calls == {'reconcile': 1, 'enqueue': 0}


def test_resolved_work_is_removed_from_attention_but_email_remains():
    payload = {
        'emails': [
            {'id': 'done', 'context_scores': {'attention_allowed': True, 'requires_reply': True, 'work_allowed': True}},
            {'id': 'open', 'context_scores': {'attention_allowed': True}},
        ],
        'needs_attention': [
            {'source_message_id': 'done'},
            {'source_message_id': 'open'},
        ],
        'waiting_on_others': [{'source_message_id': 'done'}],
        'metrics': {'important': 2, 'actions': 2},
    }
    result = mailmate_app._prune_resolved_attention(payload, {'done'})
    assert [item['source_message_id'] for item in result['needs_attention']] == ['open']
    assert result['waiting_on_others'] == []
    assert [email['id'] for email in result['emails']] == ['done', 'open']
    assert result['emails'][0]['context_scores']['attention_allowed'] is False
    assert result['metrics']['actions'] == 1


def test_later_outbound_thread_reply_resolves_only_earlier_inbound_messages():
    threads = [{'messages': [
        {'id': 'request-1', 'direction': 'inbound'},
        {'id': 'reply-1', 'direction': 'outbound', 'labels': ['SENT']},
        {'id': 'follow-up', 'direction': 'inbound'},
    ]}]
    assert mailmate_app._resolved_message_ids_from_threads(threads) == {'request-1'}


def test_kyle_agent_plain_chat_always_returns_json(monkeypatch):
    monkeypatch.setattr(mailmate_app, 'get_user_profile', lambda: {'email': 'user@example.com'})
    monkeypatch.setattr(mailmate_app.system_context_service, 'build', lambda **kwargs: {
        'context_version': 3,
        'emails': [],
        'work': {'all': []},
        'calendar': {'events': []},
    })
    monkeypatch.setattr(mailmate_app, 'plan_kyle_turn', lambda *args, **kwargs: {
        'reply': 'I am here and ready.',
        'voice': 'I am here and ready.',
        'actions': [],
    })

    response = mailmate_app.app.test_client().post('/api/kyle/agent', json={
        'message': 'Hello, are you working?',
        'conversation': [],
        'uiContext': {},
    })

    assert response.status_code == 200
    assert response.get_json()['reply'] == 'I am here and ready.'
    assert response.get_json()['mode'] == 'semantic-agent'


def test_dashboard_requires_google_authentication(monkeypatch):
    monkeypatch.setattr(mailmate_app, 'get_user_profile', lambda: None)
    response = mailmate_app.app.test_client().get('/dashboard.html')
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/')


@pytest.mark.parametrize(('prompt', 'key', 'value'), [
    ('turn on dark mode', 'theme', 'dark'),
    ('switch to light mode', 'theme', 'light'),
    ('mute your voice', 'voice', 'muted'),
    ('enable developer mode', 'developer_mode', 'enabled'),
])
def test_kyle_changes_safe_local_preferences_without_an_llm(prompt, key, value):
    result = mailmate_app._kyle_fast_path(prompt, {})
    assert result['handled'] is True
    assert result['command'] == {'type': 'set_preference', 'key': key, 'value': value}
