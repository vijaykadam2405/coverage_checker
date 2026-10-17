"""Backend-only credentials. Secrets never belong in audit configuration or reports."""
from __future__ import annotations

import ctypes
import getpass
import json
import os
import re
from pathlib import Path

API_URL = 'https://187.127.134.12:8445/'


def api_url() -> str:
    return os.getenv('CRAWLSCOPE_CORPUS_API_URL', API_URL).rstrip('/') + '/'


def credential_path() -> Path:
    return Path(os.getenv('CRAWLSCOPE_DATA_DIR', 'data')) / 'corpus_api_credentials.json'


def _protect(value: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != 'nt':
        raise ValueError('Use CRAWLSCOPE_CORPUS_API_KEY on non-Windows systems')
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
    source, output = Blob(len(value), buffer), Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    # Current Windows user + machine; never machine-wide encryption. No UI prompts.
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ValueError('Windows could not access the locally protected Corpus API credential')
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


def save_key(key: str) -> None:
    import base64
    key = key.strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,512}', key):
        raise ValueError('Enter a valid Corpus API key')
    encrypted = base64.b64encode(_protect(key.encode())).decode('ascii')
    path = credential_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'format': 'windows-dpapi-v1', 'encrypted_key': encrypted}), encoding='utf-8')
    temporary.replace(path)


def load_key() -> str:
    key = os.getenv('CRAWLSCOPE_CORPUS_API_KEY', '').strip()
    if key:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,512}', key):
            raise ValueError('The configured Corpus API key is invalid')
        return key
    import base64
    try:
        saved = json.loads(credential_path().read_text(encoding='utf-8'))
        if saved['format'] != 'windows-dpapi-v1':
            raise ValueError()
        key = _protect(base64.b64decode(saved['encrypted_key'], validate=True), decrypt=True).decode()
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,512}', key):
            raise ValueError()
        return key
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError('Corpus API key is not configured or cannot be unlocked. Run python -m crawler_coverage_auditor.corpus.credentials to set it.') from None


def configured() -> bool:
    try:
        return bool(load_key())
    except ValueError:
        return False


def public_key_status() -> dict:
    """Only status, never a key, prefix, ciphertext or credential-file contents."""
    environment = bool(os.getenv('CRAWLSCOPE_CORPUS_API_KEY', '').strip())
    return {'configured': configured(),
            'source': 'environment' if environment else 'protected_local' if credential_path().is_file() else 'missing',
            'key_management_supported': os.name == 'nt', 'text_comparison_supported': True}


if __name__ == '__main__':
    save_key(getpass.getpass('Corpus API key (hidden): '))
    print('Corpus API key saved locally, protected for this Windows user. No secret was written to source code.')
