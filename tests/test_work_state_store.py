from services.work_state_store import WorkStateStore


def _configure(monkeypatch):
    monkeypatch.setenv('SUPABASE_CONTEXT_ENABLED', '1')
    monkeypatch.setenv('SUPABASE_URL', 'https://example.supabase.co')
    monkeypatch.setenv('SUPABASE_SECRET_KEY', 'server-secret')
    monkeypatch.setenv('MAILMATE_USER_NAMESPACE_UUID', '12345678-1234-4234-8234-123456789abc')
    monkeypatch.setenv('MAILMATE_DATA_ENCRYPTION_KEY', 'MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=')


def _job(status='waiting_approval'):
    return {
        'id': 'work-1', 'user_id': 'user@example.com', 'clean_title': 'Reply to teacher',
        'status': status, 'current_step': 'Review draft',
        'source': {
            'message_id': 'm-1', 'thread_id': 't-1', 'subject': 'Private source subject',
            'snippet': 'Never persist this raw snippet', 'body': 'Never persist this body',
        },
        'output': {'summary': 'Reply is ready.', 'suggested_reply': 'Thanks, I will send it.', 'gmail_draft_id': 'draft-1'},
        'artifacts': [{'filename': 'plan.md', 'type': 'markdown', 'path': 'workspaces/work-1/plan.md', 'content': 'secret'}],
    }


def test_active_work_is_minimized_and_terminal_work_is_shared(monkeypatch):
    _configure(monkeypatch)
    store = WorkStateStore()
    calls = []
    class Response:
        def json(self): return []
    monkeypatch.setattr(store, '_request', lambda method, table, **kwargs: calls.append((method, table, kwargs)) or Response())
    store.write_all({'work-1': _job()})
    upsert = next(call for call in calls if call[0] == 'POST')
    payload = upsert[2]['json'][0]
    packed = str(payload)
    assert 'Never persist' not in packed
    assert 'Reply to teacher' not in packed
    from services.secure_storage import decrypt_json
    restored = decrypt_json(
        payload['payload_ciphertext'], payload['payload_nonce'], 'work_state',
        payload['record_key'], payload['encryption_version'],
    )
    assert 'content' not in restored['artifact_metadata'][0]
    calls.clear()
    store.write_all({'work-1': _job('completed')})
    terminal = next(call for call in calls if call[0] == 'POST' and call[1] == 'encrypted_state')
    terminal_payload = terminal[2]['json'][0]
    restored = decrypt_json(
        terminal_payload['payload_ciphertext'], terminal_payload['payload_nonce'], 'work_state',
        terminal_payload['record_key'], terminal_payload['encryption_version'],
    )
    assert restored['status'] == 'completed'
    assert any(method == 'DELETE' and table == 'encrypted_state' for method, table, _ in calls)


def test_hydrate_refreshes_when_remote_state_is_newer(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv('WORK_STATE_REFRESH_SECONDS', '1')
    store = WorkStateStore()
    plain_rows = [{
        'job_id': 'work-1', 'source_message_id': 'm-1', 'source_thread_id': 't-1',
        'clean_title': 'Reply', 'status': 'waiting_approval', 'updated_at': '2026-09-07T10:00:00+00:00',
        'expires_at': '2099-01-01T00:00:00+00:00', 'artifact_metadata': [],
    }]
    def encrypted_rows():
        from services.secure_storage import encrypt_json
        user_uuid = store._user_uuid('user@example.com')
        key = store._record_key(user_uuid, 'work-1')
        return [{'record_key': key, 'updated_at': plain_rows[0]['updated_at'], **encrypt_json(plain_rows[0], 'work_state', key)}]
    class Response:
        def json(self): return encrypted_rows()
    monkeypatch.setattr(store, '_request', lambda *args, **kwargs: Response())
    store.hydrate('user@example.com', force=True)
    plain_rows[0] = {**plain_rows[0], 'status': 'sent', 'updated_at': '2026-09-07T10:01:00+00:00'}
    store.hydrate('user@example.com', force=True)
    assert store.read_all()['work-1']['status'] == 'sent'


def test_stale_client_cannot_overwrite_or_keep_older_work_state(monkeypatch):
    _configure(monkeypatch)
    store = WorkStateStore()
    remote = {
        'job_id': 'work-1', 'source_message_id': 'm-1', 'source_thread_id': 't-1',
        'clean_title': 'Reply', 'status': 'sent', 'updated_at': '2026-09-07T10:01:00+00:00',
        'expires_at': '2099-01-01T00:00:00+00:00', 'artifact_metadata': [],
    }
    calls = []

    class Response:
        def json(self):
            from services.secure_storage import encrypt_json
            user_uuid = store._user_uuid('user@example.com')
            key = store._record_key(user_uuid, 'work-1')
            return [{'record_key': key, 'updated_at': remote['updated_at'], **encrypt_json(remote, 'work_state', key)}]

    monkeypatch.setattr(
        store, '_request',
        lambda method, table, **kwargs: calls.append((method, table, kwargs)) or Response(),
    )
    stale = _job('waiting_approval')
    stale['updated_at'] = '2026-09-07T10:00:00+00:00'
    store.write_all({'work-1': stale})

    assert store.read_all()['work-1']['status'] == 'sent'
    assert not any(method == 'POST' for method, _, _ in calls)
