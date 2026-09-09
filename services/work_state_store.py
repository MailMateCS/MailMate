import os
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import requests

from services.secure_storage import decrypt_json, encrypt_json, encryption_ready, opaque_key


TERMINAL_STATUSES = {
    'sent', 'approved_sent', 'resolved_external', 'ignored_outbound',
    'completed', 'cancelled',
}


class WorkStateStore:
    """RAM-first active Work store with minimized Supabase durability."""

    def __init__(self):
        self._lock = threading.RLock()
        self._jobs = {}
        self._last_hydrated_at = {}
        self._reachable = None

    def _enabled(self):
        return str(os.getenv('SUPABASE_CONTEXT_ENABLED', '0')).lower() in {'1', 'true', 'yes', 'on'}

    def _config(self):
        return (
            os.getenv('SUPABASE_URL', '').rstrip('/'),
            os.getenv('SUPABASE_SECRET_KEY', ''),
            os.getenv('MAILMATE_USER_NAMESPACE_UUID', ''),
        )

    def _user_uuid(self, user_id):
        return str(uuid.uuid5(uuid.UUID(self._config()[2]), str(user_id or 'default').lower()))

    def _request(self, method, table, **kwargs):
        url, secret, _ = self._config()
        headers = {
            'apikey': secret,
            'Authorization': f'Bearer {secret}',
            'Content-Type': 'application/json',
        }
        prefer = kwargs.pop('prefer', None)
        if prefer:
            headers['Prefer'] = prefer
        response = requests.request(method, f'{url}/rest/v1/{table}', headers=headers, timeout=8, **kwargs)
        response.raise_for_status()
        self._reachable = True
        return response

    @staticmethod
    def _artifact_metadata(job):
        return [
            {
                'filename': str(item.get('filename') or item.get('name') or '')[:240],
                'type': str(item.get('type') or '')[:80],
                'path': str(item.get('path') or item.get('id') or '')[:500],
            }
            for item in (job.get('artifacts') or [])
            if item.get('filename') or item.get('name') or item.get('path') or item.get('id')
        ]

    def _payload(self, job):
        source = job.get('source') or {}
        output = job.get('output') or {}
        now = datetime.now(timezone.utc)
        terminal = str(job.get('status') or '') in TERMINAL_STATUSES
        return {
            'job_id': str(job.get('id') or ''),
            'source_message_id': str(source.get('message_id') or ''),
            'source_thread_id': str(source.get('thread_id') or '') or None,
            'clean_title': str(job.get('clean_title') or job.get('title') or 'Work task')[:240],
            'status': str(job.get('status') or 'queued'),
            'current_step': str(job.get('current_step') or '')[:500] or None,
            'task_summary': str(output.get('summary') or source.get('subject') or '')[:1000] or None,
            'deadline': source.get('deadline') or None,
            'generated_reply': str(output.get('suggested_reply') or '')[:12000] or None,
            'approval_state': str((job.get('policy_verdict') or {}).get('decision') or job.get('status') or '')[:80],
            'gmail_draft_id': output.get('gmail_draft_id') or None,
            'artifact_metadata': self._artifact_metadata(job),
            'updated_at': str(job.get('updated_at') or now.isoformat()),
            'expires_at': (now + timedelta(days=2 if terminal else 14)).isoformat(),
        }

    @staticmethod
    def _record_key(user_uuid, job_id):
        return opaque_key('work_state', f'{user_uuid}:{job_id}')

    @staticmethod
    def _context_key(user_uuid, message_id):
        return opaque_key('mail_context', f'{user_uuid}:{message_id}')

    @staticmethod
    def _newer(left, right):
        return str(left or '') > str(right or '')

    @staticmethod
    def _job_from_row(row, user_id):
        artifacts = [
            {'filename': item.get('filename'), 'type': item.get('type'), 'path': item.get('path')}
            for item in (row.get('artifact_metadata') or [])
        ]
        return {
            'id': str(row.get('job_id') or ''),
            'user_id': str(user_id or '').lower(),
            'title': row.get('clean_title'),
            'clean_title': row.get('clean_title'),
            'status': row.get('status'),
            'current_step': row.get('current_step'),
            'source': {
                'type': 'gmail',
                'message_id': row.get('source_message_id'),
                'thread_id': row.get('source_thread_id'),
                'deadline': row.get('deadline'),
            },
            'output': {
                'summary': row.get('task_summary'),
                'suggested_reply': row.get('generated_reply') or '',
                'gmail_draft_id': row.get('gmail_draft_id'),
                'checklist': [],
                'notes': [],
            },
            'artifacts': artifacts,
            'steps': [],
            'created_at': row.get('created_at'),
            'updated_at': row.get('updated_at'),
        }

    def hydrate(self, user_id, force=False):
        normalized = str(user_id or '').lower()
        refresh_seconds = max(1, int(os.getenv('WORK_STATE_REFRESH_SECONDS', '4')))
        now_monotonic = time.monotonic()
        if not normalized or (not force and now_monotonic - self._last_hydrated_at.get(normalized, 0) < refresh_seconds):
            return
        if not self._enabled() or not all(self._config()) or not encryption_ready():
            return
        try:
            user_uuid = self._user_uuid(normalized)
            response = self._request('GET', 'encrypted_state', params={
                'user_id': f'eq.{user_uuid}',
                'record_type': 'eq.work_state',
                'expires_at': f'gt.{datetime.now(timezone.utc).isoformat()}',
                'select': 'record_key,payload_ciphertext,payload_nonce,encryption_version,updated_at',
            })
            with self._lock:
                for encrypted in response.json() or []:
                    row = decrypt_json(
                        encrypted['payload_ciphertext'], encrypted['payload_nonce'],
                        'work_state', encrypted['record_key'], encrypted.get('encryption_version'),
                    )
                    job_id = str(row.get('job_id') or '')
                    local = self._jobs.get(job_id)
                    if not job_id or (local and not self._newer(row.get('updated_at'), local.get('updated_at'))):
                        continue
                    self._jobs[job_id] = self._job_from_row(row, normalized)
            self._last_hydrated_at[normalized] = now_monotonic
        except Exception:
            self._reachable = False

    def read_all(self):
        with self._lock:
            return deepcopy(self._jobs)

    def write_all(self, jobs):
        with self._lock:
            self._jobs = deepcopy(jobs if isinstance(jobs, dict) else {})
        if not self._enabled() or not all(self._config()) or not encryption_ready():
            return
        try:
            for job in self._jobs.values():
                job_id = str(job.get('id') or '')
                if not job_id:
                    continue
                user_uuid = self._user_uuid(job.get('user_id'))
                if job.get('status') in TERMINAL_STATUSES:
                    source_id = str((job.get('source') or {}).get('message_id') or '')
                    if source_id:
                        self._request('DELETE', 'encrypted_state', params={
                            'user_id': f'eq.{user_uuid}',
                            'record_key': f'eq.{self._context_key(user_uuid, source_id)}',
                        })
                payload = self._payload(job)
                if not payload['source_message_id']:
                    continue
                record_key = self._record_key(user_uuid, job_id)
                remote_rows = self._request('GET', 'encrypted_state', params={
                    'record_key': f'eq.{record_key}',
                    'select': 'record_key,payload_ciphertext,payload_nonce,encryption_version,updated_at',
                    'limit': '1',
                }).json() or []
                remote = None
                if remote_rows:
                    encrypted = remote_rows[0]
                    remote = decrypt_json(
                        encrypted['payload_ciphertext'], encrypted['payload_nonce'],
                        'work_state', record_key, encrypted.get('encryption_version'),
                    )
                if remote and self._newer(remote.get('updated_at'), payload.get('updated_at')):
                    with self._lock:
                        self._jobs[job_id] = self._job_from_row(
                            remote, job.get('user_id')
                        )
                    continue
                record = {
                    'record_key': record_key,
                    'user_id': user_uuid,
                    'record_type': 'work_state',
                    **encrypt_json(payload, 'work_state', record_key),
                    'updated_at': payload['updated_at'],
                    'expires_at': payload['expires_at'],
                }
                self._request(
                    'POST', 'encrypted_state',
                    params={'on_conflict': 'record_key'}, json=[record],
                    prefer='resolution=merge-duplicates,return=minimal',
                )
        except Exception:
            self._reachable = False


work_state_store = WorkStateStore()
