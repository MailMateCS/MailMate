import base64
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


ENCRYPTION_VERSION = 1


class DataEncryptionError(RuntimeError):
    pass


def _windows_protect(value):
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_byte))]

    source_buffer = ctypes.create_string_buffer(value)
    source = DataBlob(len(value), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)))
    target = DataBlob()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source), 'MailMate data key', None, None, None, 1, ctypes.byref(target)
    ):
        raise DataEncryptionError('Windows could not protect the MailMate data key')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)


def _windows_unprotect(value):
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_byte))]

    source_buffer = ctypes.create_string_buffer(value)
    source = DataBlob(len(value), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)))
    target = DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)
    ):
        raise DataEncryptionError('Windows could not unlock the MailMate data key')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)


def _windows_key():
    key_path = Path(os.getenv('MAILMATE_KEY_FILE') or Path(__file__).resolve().parent.parent / 'data' / '.mailmate-key')
    if key_path.exists():
        key = _windows_unprotect(key_path.read_bytes())
    else:
        key = os.urandom(32)
        key_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = key_path.with_suffix('.tmp')
        temporary.write_bytes(_windows_protect(key))
        temporary.replace(key_path)
    if len(key) != 32:
        raise DataEncryptionError('The protected MailMate data key is invalid')
    return key


def _master_key():
    encoded = str(os.getenv('MAILMATE_DATA_ENCRYPTION_KEY') or '').strip()
    if not encoded:
        if sys.platform == 'win32':
            return _windows_key()
        raise DataEncryptionError('MAILMATE_DATA_ENCRYPTION_KEY is required for durable storage on this platform')
    try:
        key = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4))
    except Exception as exc:
        raise DataEncryptionError('MAILMATE_DATA_ENCRYPTION_KEY is not valid base64') from exc
    if len(key) != 32:
        raise DataEncryptionError('MAILMATE_DATA_ENCRYPTION_KEY must decode to exactly 32 bytes')
    return key


def encryption_ready():
    try:
        _master_key()
        return True
    except DataEncryptionError:
        return False


def data_key_for_wrapping():
    """Return key material only to the server-side recovery provider."""
    return _master_key()


def install_recovered_data_key(key):
    if len(key) != 32:
        raise DataEncryptionError('Recovered MailMate data key is invalid')
    if os.getenv('MAILMATE_DATA_ENCRYPTION_KEY'):
        raise DataEncryptionError('Replace MAILMATE_DATA_ENCRYPTION_KEY to restore this deployment')
    if sys.platform != 'win32':
        raise DataEncryptionError('Automatic key restore is currently available on Windows only')
    key_path = Path(os.getenv('MAILMATE_KEY_FILE') or Path(__file__).resolve().parent.parent / 'data' / '.mailmate-key')
    key_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = key_path.with_suffix('.tmp')
    temporary.write_bytes(_windows_protect(key))
    temporary.replace(key_path)


def opaque_key(purpose, identity):
    message = f'{purpose}\0{identity}'.encode('utf-8')
    return hmac.new(_master_key(), message, hashlib.sha256).hexdigest()


def _aad(purpose, identity):
    return f'mailmate:v{ENCRYPTION_VERSION}:{purpose}:{identity}'.encode('utf-8')


def encrypt_json(value, purpose, identity):
    nonce = os.urandom(12)
    plaintext = json.dumps(value, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    ciphertext = AESGCM(_master_key()).encrypt(nonce, plaintext, _aad(purpose, identity))
    return {
        'payload_ciphertext': base64.urlsafe_b64encode(ciphertext).decode('ascii'),
        'payload_nonce': base64.urlsafe_b64encode(nonce).decode('ascii'),
        'encryption_version': ENCRYPTION_VERSION,
    }


def decrypt_json(ciphertext, nonce, purpose, identity, version=ENCRYPTION_VERSION):
    if int(version or 0) != ENCRYPTION_VERSION:
        raise DataEncryptionError(f'Unsupported encrypted payload version: {version}')
    try:
        raw_ciphertext = base64.urlsafe_b64decode(str(ciphertext))
        raw_nonce = base64.urlsafe_b64decode(str(nonce))
        plaintext = AESGCM(_master_key()).decrypt(
            raw_nonce, raw_ciphertext, _aad(purpose, identity)
        )
        return json.loads(plaintext.decode('utf-8'))
    except DataEncryptionError:
        raise
    except Exception as exc:
        raise DataEncryptionError('Encrypted data could not be authenticated or decrypted') from exc


def encrypted_path(path):
    path = Path(path)
    return path.with_suffix('.enc')


def write_encrypted_json(path, value, purpose):
    destination = encrypted_path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    envelope = {
        'version': ENCRYPTION_VERSION,
        **encrypt_json(value, purpose, purpose),
    }
    temporary = destination.with_suffix(destination.suffix + '.tmp')
    temporary.write_text(json.dumps(envelope, separators=(',', ':')), encoding='utf-8')
    temporary.replace(destination)
    return destination


def read_encrypted_json(path, purpose, default=None, migrate_plaintext=True):
    legacy = Path(path)
    source = encrypted_path(legacy)
    if source.exists():
        envelope = json.loads(source.read_text(encoding='utf-8'))
        return decrypt_json(
            envelope['payload_ciphertext'], envelope['payload_nonce'], purpose, purpose,
            envelope.get('encryption_version') or envelope.get('version'),
        )
    if not legacy.exists():
        return default
    value = json.loads(legacy.read_text(encoding='utf-8'))
    if migrate_plaintext:
        write_encrypted_json(legacy, value, purpose)
        legacy.unlink()
    return value


def delete_encrypted_json(path):
    for candidate in (Path(path), encrypted_path(path)):
        if candidate.exists():
            candidate.unlink()
