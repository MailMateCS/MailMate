import json

import pytest

from services.secure_storage import (
    DataEncryptionError,
    decrypt_json,
    encrypt_json,
    encrypted_path,
    opaque_key,
    read_encrypted_json,
    write_encrypted_json,
)


KEY_A = 'MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA='
KEY_B = 'MTExMTExMTExMTExMTExMTExMTExMTExMTExMTExMTExMTExMTE='


def test_authenticated_encryption_round_trip_and_wrong_key(monkeypatch):
    monkeypatch.setenv('MAILMATE_DATA_ENCRYPTION_KEY', KEY_A)
    record_key = opaque_key('work_state', 'private-id')
    encrypted = encrypt_json({'reply': 'Private reply'}, 'work_state', record_key)
    assert 'Private reply' not in json.dumps(encrypted)
    assert decrypt_json(
        encrypted['payload_ciphertext'], encrypted['payload_nonce'],
        'work_state', record_key, encrypted['encryption_version'],
    ) == {'reply': 'Private reply'}

    monkeypatch.setenv('MAILMATE_DATA_ENCRYPTION_KEY', KEY_B)
    with pytest.raises(DataEncryptionError):
        decrypt_json(
            encrypted['payload_ciphertext'], encrypted['payload_nonce'],
            'work_state', record_key, encrypted['encryption_version'],
        )


def test_plaintext_file_is_atomically_migrated(monkeypatch, tmp_path):
    monkeypatch.setenv('MAILMATE_DATA_ENCRYPTION_KEY', KEY_A)
    path = tmp_path / 'credentials.json'
    path.write_text(json.dumps({'refresh_token': 'very-private'}), encoding='utf-8')

    restored = read_encrypted_json(path, 'credentials', default=None)

    assert restored == {'refresh_token': 'very-private'}
    assert not path.exists()
    assert encrypted_path(path).exists()
    assert 'very-private' not in encrypted_path(path).read_text(encoding='utf-8')


def test_encrypted_file_survives_restart(monkeypatch, tmp_path):
    monkeypatch.setenv('MAILMATE_DATA_ENCRYPTION_KEY', KEY_A)
    path = tmp_path / 'automations.json'
    value = [{'goal': 'Prepare a private report'}]
    write_encrypted_json(path, value, 'automations')
    assert read_encrypted_json(path, 'automations', default=[]) == value
