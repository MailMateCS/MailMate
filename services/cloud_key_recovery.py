import base64
import os
import uuid

import google.auth
import requests
from google.auth.transport.requests import AuthorizedSession

from services.secure_storage import data_key_for_wrapping


class CloudKeyRecovery:
    def _config(self):
        return {
            'kms_key': str(os.getenv('GOOGLE_CLOUD_KMS_KEY_NAME') or '').strip(),
            'url': str(os.getenv('SUPABASE_URL') or '').rstrip('/'),
            'secret': str(os.getenv('SUPABASE_SECRET_KEY') or ''),
            'namespace': str(os.getenv('MAILMATE_USER_NAMESPACE_UUID') or ''),
        }

    def _user_uuid(self, identity):
        return str(uuid.uuid5(uuid.UUID(self._config()['namespace']), str(identity).lower()))

    def _request(self, method, *, params=None, payload=None, prefer=None):
        config = self._config()
        headers = {'apikey': config['secret'], 'Authorization': f"Bearer {config['secret']}", 'Content-Type': 'application/json'}
        if prefer:
            headers['Prefer'] = prefer
        response = requests.request(method, f"{config['url']}/rest/v1/key_recovery", headers=headers, params=params, json=payload, timeout=10)
        response.raise_for_status()
        return response

    def status(self, identity):
        config = self._config()
        available = bool(config['kms_key'] and config['url'] and config['secret'] and config['namespace'])
        enrolled = False
        if available:
            rows = self._request('GET', params={'user_id': f'eq.{self._user_uuid(identity)}', 'select': 'user_id', 'limit': '1'}).json() or []
            enrolled = bool(rows)
        return {'available': available, 'enrolled': enrolled, 'recommended': available and not enrolled, 'provider': 'google-cloud-kms'}

    def enroll(self, identity):
        config = self._config()
        if not config['kms_key']:
            raise RuntimeError('Google Cloud KMS is not configured by the MailMate administrator')
        credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
        response = AuthorizedSession(credentials).post(
            f"https://cloudkms.googleapis.com/v1/{config['kms_key']}:encrypt",
            json={'plaintext': base64.b64encode(data_key_for_wrapping()).decode('ascii')}, timeout=15,
        )
        response.raise_for_status()
        wrapped = response.json()['ciphertext']
        self._request('POST', params={'on_conflict': 'user_id'}, payload=[{
            'user_id': self._user_uuid(identity), 'provider': 'google-cloud-kms',
            'kms_key_name': config['kms_key'], 'wrapped_data_key': wrapped,
        }], prefer='resolution=merge-duplicates,return=minimal')
        return self.status(identity)

    def disable(self, identity):
        self._request('DELETE', params={'user_id': f'eq.{self._user_uuid(identity)}'})
        return self.status(identity)


cloud_key_recovery = CloudKeyRecovery()
