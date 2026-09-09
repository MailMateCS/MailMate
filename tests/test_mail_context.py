import pytest

from services.mail_context_service import MailContextService, classify_message


def message(**overrides):
    value = {
        'id': 'm-1',
        'thread_id': 't-1',
        'subject': 'Please submit the project report tomorrow',
        'sender': 'Teacher <teacher@example.edu>',
        'snippet': 'Please send the PDF before the deadline tomorrow.',
        'timestamp': '2026-09-07T10:00:00Z',
        'direction': 'inbound',
        'labels': ['INBOX', 'IMPORTANT'],
        'privacy_gate': {'routing': 'CLOUD_ALLOWED'},
    }
    value.update(overrides)
    return value


def test_actionable_work_is_allowed():
    row = classify_message(message(), {'routing': 'CLOUD_ALLOWED'})
    assert row['attention_allowed'] is True
    assert row['work_allowed'] is True
    assert row['category'] == 'actionable_work'


def test_prize_phishing_never_becomes_work():
    row = classify_message(message(
        subject='URGENT: claim your casino prize',
        snippet='Verify your account password immediately at http://1.2.3.4/login',
    ), {'routing': 'CLOUD_ALLOWED'})
    assert row['phishing_score'] >= 0.55
    assert row['attention_allowed'] is False
    assert row['work_allowed'] is False
    assert row['calendar_allowed'] is False


def test_informational_exam_is_attention_and_calendar_not_work(monkeypatch):
    monkeypatch.setenv('MAILMATE_SEMANTIC_CLASSIFIER_ENABLED', '0')
    row = classify_message(message(
        subject='Lab exam announcement',
        snippet='The lab exam will be held on October 7.',
    ), {'routing': 'CLOUD_ALLOWED'})
    assert row['attention_allowed'] is True
    assert row['calendar_allowed'] is True
    assert row['work_allowed'] is False


def test_assignment_submission_is_work(monkeypatch):
    monkeypatch.setenv('MAILMATE_SEMANTIC_CLASSIFIER_ENABLED', '0')
    row = classify_message(message(
        subject='Operating Systems assignment',
        snippet='Please prepare the lab report and submit it before October 7.',
    ), {'routing': 'CLOUD_ALLOWED'})
    assert row['attention_allowed'] is True
    assert row['calendar_allowed'] is True
    assert row['work_allowed'] is True


def test_concrete_email_request_is_work(monkeypatch):
    monkeypatch.setenv('MAILMATE_SEMANTIC_CLASSIFIER_ENABLED', '0')
    row = classify_message(message(
        subject='Quick favor',
        snippet='Could you write and send an email response to my professor?',
    ), {'routing': 'CLOUD_ALLOWED'})
    assert row['attention_allowed'] is True
    assert row['work_allowed'] is True


@pytest.mark.parametrize(('subject', 'snippet', 'expected_deadline'), [
    ('Participation confirmation required', 'Please confirm your attendance before 9 September 2026 at 3:00 PM.', '2026-09-09T15:00:00+05:30'),
    ('IMPORTANT: Project Review Deadline - 10 September', 'Project documentation must be submitted for review by 10 September 2026.', '2026-09-10'),
    ('URGENT FINAL REMINDER: Deadline in 24 Hours - 10 September', 'The deadline is 10 September 2026 at 11:59 PM. Please complete the required action.', '2026-09-10T23:59:00+05:30'),
])
def test_real_deadline_messages_become_work_with_exact_time(monkeypatch, subject, snippet, expected_deadline):
    monkeypatch.setenv('MAILMATE_SEMANTIC_CLASSIFIER_ENABLED', '0')
    monkeypatch.setenv('APP_TIMEZONE', 'Asia/Kolkata')
    row = classify_message(message(subject=subject, snippet=snippet), {'routing': 'CLOUD_ALLOWED'})
    assert row['attention_allowed'] is True
    assert row['work_allowed'] is True
    assert row['calendar_allowed'] is True
    assert row['deadline_at'] == expected_deadline


def test_semantic_null_deadline_does_not_erase_deterministic_value():
    from services.mail_context_service import _apply_semantic, _fallback_classify_message
    value = message(snippet='Please submit the project before 9 September 2026 at 3:00 PM.')
    baseline = _fallback_classify_message(value, {'routing': 'CLOUD_ALLOWED'})
    row = _apply_semantic(value, baseline, {'work_required': True, 'deadline_at': None})
    assert row['deadline_at'] == '2026-09-09T15:00:00+05:30'


def test_semantic_midnight_does_not_invent_time_for_date_only_mail():
    from services.mail_context_service import _apply_semantic, _fallback_classify_message
    value = message(
        subject='Project review deadline - 10 September',
        snippet='Please submit the documentation by 10 September 2026.',
    )
    baseline = _fallback_classify_message(value, {'routing': 'CLOUD_ALLOWED'})
    row = _apply_semantic(value, baseline, {
        'work_required': True,
        'deadline_at': '2026-09-10T00:00:00Z',
    })
    assert row['deadline_at'] == '2026-09-10'


def test_context_rows_never_contain_mail_content(monkeypatch):
    monkeypatch.setenv('SUPABASE_CONTEXT_ENABLED', '0')
    service = MailContextService()
    result = service.update('user-1', [message(body='full private body')])
    row = result['rows'][0]
    assert result['changed_count'] == 1
    assert 'subject' not in row
    assert 'snippet' not in row
    assert 'body' not in row
    assert 'sender' not in row


def test_unchanged_message_reuses_classification(monkeypatch):
    monkeypatch.setenv('SUPABASE_CONTEXT_ENABLED', '0')
    service = MailContextService()
    first = service.update('user-1', [message()])
    second = service.update('user-1', [message()])
    changed = service.update('user-1', [message(snippet='Please send the updated PDF tomorrow.')])
    assert first['changed_count'] == 1
    assert second['changed_count'] == 0
    assert changed['changed_count'] == 1


def _configure_supabase(monkeypatch):
    monkeypatch.setenv('SUPABASE_CONTEXT_ENABLED', '1')
    monkeypatch.setenv('SUPABASE_URL', 'https://example.supabase.co')
    monkeypatch.setenv('SUPABASE_SECRET_KEY', 'server-secret')
    monkeypatch.setenv('MAILMATE_USER_NAMESPACE_UUID', '12345678-1234-4234-8234-123456789abc')
    monkeypatch.setenv('MAILMATE_DATA_ENCRYPTION_KEY', 'MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=')


def test_irrelevant_email_is_not_persisted(monkeypatch):
    _configure_supabase(monkeypatch)
    service = MailContextService()
    calls = []
    monkeypatch.setattr(service, '_request', lambda method, table, **kwargs: calls.append((method, table, kwargs)))
    result = service.update('user-1', [message(
        subject='Weekly newsletter', snippet='Sale discount unsubscribe', labels=['CATEGORY_PROMOTIONS']
    )])
    assert result['persistence']['stored'] == 0
    assert not any(method == 'POST' and table == 'active_ui_context' for method, table, _ in calls)


def test_supabase_payload_is_minimized(monkeypatch):
    _configure_supabase(monkeypatch)
    service = MailContextService()
    calls = []
    monkeypatch.setattr(service, '_request', lambda method, table, **kwargs: calls.append((method, table, kwargs)))
    result = service.update('user-1', [message(
        body='private full body', body_html='<b>private</b>',
        attachments=[{'filename': 'secret.pdf', 'data': 'private'}],
        raw_headers={'Authorization': 'secret'},
    )])
    context_call = next(call for call in calls if call[0] == 'POST' and call[1] == 'encrypted_state')
    payload = context_call[2]['payload'][0]
    assert result['persistence']['stored'] == 1
    assert 'private' not in str(payload)
    assert 'teacher@example.edu' not in str(payload)
    assert 'Please submit the project report tomorrow' not in str(payload)
    from services.secure_storage import decrypt_json
    restored = decrypt_json(
        payload['payload_ciphertext'], payload['payload_nonce'], 'mail_context',
        payload['record_key'], payload['encryption_version'],
    )
    assert not ({'body', 'body_html', 'snippet', 'attachments', 'raw_headers'} & set(restored))
    assert restored['gmail_message_id'] == 'm-1'
    assert restored['attention_allowed'] is True
