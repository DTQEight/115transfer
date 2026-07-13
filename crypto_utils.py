import os
import base64
from typing import Optional
from Crypto.Cipher import AES
from Crypto.Protocol.KDF import scrypt
from Crypto.Random import get_random_bytes

KEY_SIZE: int = 32
NONCE_SIZE: int = 12
SALT_SIZE: int = 16

_ENCRYPTION_KEY: Optional[str] = None


def _get_key() -> str:
    global _ENCRYPTION_KEY
    if _ENCRYPTION_KEY is None:
        env_key: Optional[str] = os.environ.get('ENCRYPTION_KEY')
        if not env_key:
            _ENCRYPTION_KEY = os.environ.get('FLASK_SECRET_KEY', '')[:KEY_SIZE]
            if len(_ENCRYPTION_KEY) < KEY_SIZE:
                _ENCRYPTION_KEY = _ENCRYPTION_KEY.ljust(KEY_SIZE, '0')
        else:
            _ENCRYPTION_KEY = env_key[:KEY_SIZE].ljust(KEY_SIZE, '0')
    return _ENCRYPTION_KEY


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ''
    key: str = _get_key()
    salt: bytes = get_random_bytes(SALT_SIZE)
    nonce: bytes = get_random_bytes(NONCE_SIZE)
    derived_key: bytes = scrypt(key, salt, KEY_SIZE, N=2**14, r=8, p=1)
    cipher = AES.new(derived_key, AES.MODE_GCM, nonce=nonce)
    ciphertext: bytes
    tag: bytes
    ciphertext, tag = cipher.encrypt_and_digest(plaintext.encode('utf-8'))
    encoded: str = base64.b64encode(salt + nonce + tag + ciphertext).decode('ascii')
    return f'ENC[{encoded}]'


def decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return ''
    if ciphertext.startswith('ENC[') and ciphertext.endswith(']'):
        encoded: str = ciphertext[4:-1]
    else:
        return ciphertext
    try:
        decoded: bytes = base64.b64decode(encoded)
        salt: bytes = decoded[:SALT_SIZE]
        nonce: bytes = decoded[SALT_SIZE:SALT_SIZE + NONCE_SIZE]
        tag: bytes = decoded[SALT_SIZE + NONCE_SIZE:SALT_SIZE + NONCE_SIZE + 16]
        data: bytes = decoded[SALT_SIZE + NONCE_SIZE + 16:]
        key: str = _get_key()
        derived_key: bytes = scrypt(key, salt, KEY_SIZE, N=2**14, r=8, p=1)
        cipher = AES.new(derived_key, AES.MODE_GCM, nonce=nonce)
        plaintext: bytes = cipher.decrypt_and_verify(data, tag)
        return plaintext.decode('utf-8')
    except Exception:
        return ciphertext


def is_encrypted(value: str) -> bool:
    return value.startswith('ENC[') and value.endswith(']')