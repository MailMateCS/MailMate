"""One-time migration from MailMate's legacy plaintext stores to encrypted storage."""

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from services.secure_storage import (  # noqa: E402
    encrypt_json,
    encryption_ready,
    opaque_key,
    read_encrypted_json,
)


def _client():
    load_dotenv(BASE_DIR / 'api.env')
    url = str(os.getenv('SUPABASE_URL') or '').rstrip('/')
    secret = str(os.getenv('SUPABASE_SECRET_KEY') or '')
    if not url or not secret or not encryption_ready():
        raise RuntimeError('Supabase and encrypted storage must be configured before migration')
    return url, {
        'apikey': secret,
        'Authorization': f'Bearer {secret}',
        'Content-Type': 'application/json',
    }


def _request(method, table, *, params=None, payload=None, prefer=None):
    url, headers = _client()
    if prefer:
        headers['Prefer'] = prefer
    response = requests.request(
        method, f'{url}/rest/v1/{table}', headers=headers, params=params, json=payload, timeout=20
    )
    response.raise_for_status()
    return response


def _record(user_id, record_type, identity, payload, updated_at=None, expires_at=None):
    record_key = opaque_key(record_type, f'{user_id}:{identity}' if record_type != 'sync_state' else user_id)
    return {
        'record_key': record_key,
        'user_id': user_id,
        'record_type': record_type,
        **encrypt_json(payload, record_type, record_key),
        'updated_at': updated_at or datetime.now(timezone.utc).isoformat(),
        'expires_at': expires_at,
    }


def _supabase_records():
    records = []
    contexts = _request('GET', 'active_ui_context', params={'select': '*'}).json() or []
    for row in contexts:
        payload = {
            'gmail_message_id': row.get('source_message_id'),
            'gmail_thread_id': row.get('source_thread_id'),
            'source_fingerprint': row.get('source_fingerprint'),
            'context_type': row.get('context_type'),
            'status': row.get('status'),
            'display_title': row.get('display_title'),
            'sender_display': row.get('sender_display'),
            'summary': row.get('summary'),
            'importance_score': row.get('importance_score') or 0,
            'urgency_score': row.get('urgency_score') or 0,
            'attention_allowed': bool(row.get('needs_attention')),
            'requires_reply': bool(row.get('requires_reply')),
            'work_allowed': bool(row.get('work_required')),
            'calendar_allowed': bool(row.get('calendar_required')),
            'deadline_at': row.get('deadline_at'),
            'work_job_id': row.get('work_job_id'),
            'classifier_version': row.get('classifier_version') or 1,
        }
        records.append(_record(
            row['user_id'], 'mail_context', row['source_message_id'], payload,
            row.get('updated_at'), row.get('expires_at'),
        ))

    work = _request('GET', 'active_work_state', params={'select': '*'}).json() or []
    for row in work:
        payload = {key: value for key, value in row.items() if key not in {'user_id'}}
        records.append(_record(
            row['user_id'], 'work_state', row['job_id'], payload,
            row.get('updated_at'), row.get('expires_at'),
        ))

    accounts = _request('GET', 'mail_accounts', params={
        'select': 'user_id,last_history_id,last_sync_at,last_full_scan_at,rules_version',
    }).json() or []
    for row in accounts:
        payload = {key: value for key, value in row.items() if key != 'user_id'}
        records.append(_record(
            row['user_id'], 'sync_state', row['user_id'], payload, row.get('last_sync_at'), None,
        ))
    return records, len(contexts), len(work), len(accounts)


def _migrate_local(execute):
    migrated = []
    for filename, purpose, default in (
        ('google_credentials.json', 'google_credentials', None),
        ('automations.json', 'automations', []),
        ('work_jobs.json', 'legacy_work_jobs', {}),
        ('work_settings.json', 'work_settings', {}),
        ('calendar_dismissals.json', 'calendar_dismissals', {}),
    ):
        path = BASE_DIR / 'data' / filename
        if path.exists():
            if execute:
                read_encrypted_json(path, purpose, default=default, migrate_plaintext=True)
            migrated.append(filename)
    audio = BASE_DIR / 'data' / 'temp_audio.webm'
    if audio.exists():
        if execute:
            audio.unlink()
        migrated.append('temp_audio.webm (removed)')
    return migrated


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true', help='commit the migration and remove plaintext')
    args = parser.parse_args()

    records, contexts, work, accounts = _supabase_records()
    local = _migrate_local(args.execute)
    print(f'Prepared {len(records)} encrypted rows ({contexts} context, {work} work, {accounts} sync).')
    print(f'Local plaintext stores found: {len(local)}.')
    if not args.execute:
        print('Dry run only. Re-run with --execute after reviewing these counts.')
        return

    if records:
        _request(
            'POST', 'encrypted_state', params={'on_conflict': 'record_key'}, payload=records,
            prefer='resolution=merge-duplicates,return=minimal',
        )
    keys = [row['record_key'] for row in records]
    stored = _request('GET', 'encrypted_state', params={
        'record_key': f'in.({",".join(keys)})' if keys else 'eq.none',
        'select': 'record_key',
    }).json() or []
    if len(stored) != len(set(keys)):
        raise RuntimeError('Encrypted-row verification failed; legacy rows were not removed')

    for table in ('active_work_state', 'active_ui_context', 'mail_context', 'mail_accounts'):
        _request('DELETE', table, params={'user_id': 'not.is.null'})
    print('Migration verified. Legacy Supabase rows were removed and local files were protected.')


if __name__ == '__main__':
    main()
