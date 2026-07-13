import os
import base64
import logging
from typing import Optional, Union
from Crypto.Cipher import AES
from Crypto.Protocol.KDF import scrypt
from Crypto.Random import get_random_bytes

KEY_SIZE: int = 32
NONCE_SIZE: int = 12
SALT_SIZE: int = 16
TAG_SIZE: int = 16

_ENCRYPTION_KEY: Optional[str] = None
_KEY_WARNED: bool = False

_logger = logging.getLogger('crypto_utils')


def _get_key() -> str:
    """获取加密密钥，从环境变量读取，未配置时使用 FLASK_SECRET_KEY 并告警"""
    global _ENCRYPTION_KEY, _KEY_WARNED
    if _ENCRYPTION_KEY is None:
        env_key: Optional[str] = os.environ.get('ENCRYPTION_KEY')
        if not env_key:
            fallback = os.environ.get('FLASK_SECRET_KEY', '')
            if not fallback:
                if not _KEY_WARNED:
                    _logger.warning('[安全] 未设置 ENCRYPTION_KEY 或 FLASK_SECRET_KEY，敏感配置加密将使用空密钥，请配置环境变量！')
                    _KEY_WARNED = True
                _ENCRYPTION_KEY = ''
            else:
                _ENCRYPTION_KEY = fallback[:KEY_SIZE]
                if len(_ENCRYPTION_KEY) < KEY_SIZE:
                    _ENCRYPTION_KEY = _ENCRYPTION_KEY.ljust(KEY_SIZE, '0')
        else:
            _ENCRYPTION_KEY = env_key[:KEY_SIZE].ljust(KEY_SIZE, '0')
            # 检测弱密钥：全相同字符或全零
            if len(set(env_key)) == 1 or env_key == '0' * len(env_key):
                if not _KEY_WARNED:
                    _logger.warning('[安全] ENCRYPTION_KEY 为弱密钥（全相同字符），请使用强随机值！')
                    _KEY_WARNED = True
    return _ENCRYPTION_KEY


def encrypt(plaintext: str) -> str:
    """AES-GCM 加密，返回 ENC[base64] 格式"""
    if not plaintext:
        return ''
    key: str = _get_key()
    salt: bytes = get_random_bytes(SALT_SIZE)
    nonce: bytes = get_random_bytes(NONCE_SIZE)
    derived_key: bytes = scrypt(key, salt, KEY_SIZE, N=2**14, r=8, p=1)
    cipher = AES.new(derived_key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext.encode('utf-8'))
    encoded: str = base64.b64encode(salt + nonce + tag + ciphertext).decode('ascii')
    return f'ENC[{encoded}]'


def decrypt(ciphertext: str) -> str:
    """解密 ENC[base64] 格式的密文，失败时记录错误并返回空字符串（不返回密文以防泄露）"""
    if not ciphertext:
        return ''
    if not is_encrypted(ciphertext):
        # 非加密格式（明文），直接返回（兼容旧数据）
        return ciphertext
    try:
        encoded: str = ciphertext[4:-1]
        decoded: bytes = base64.b64decode(encoded)
        if len(decoded) < SALT_SIZE + NONCE_SIZE + TAG_SIZE:
            raise ValueError('密文长度不足')
        salt: bytes = decoded[:SALT_SIZE]
        nonce: bytes = decoded[SALT_SIZE:SALT_SIZE + NONCE_SIZE]
        tag: bytes = decoded[SALT_SIZE + NONCE_SIZE:SALT_SIZE + NONCE_SIZE + TAG_SIZE]
        data: bytes = decoded[SALT_SIZE + NONCE_SIZE + TAG_SIZE:]
        key: str = _get_key()
        derived_key: bytes = scrypt(key, salt, KEY_SIZE, N=2**14, r=8, p=1)
        cipher = AES.new(derived_key, AES.MODE_GCM, nonce=nonce)
        plaintext: bytes = cipher.decrypt_and_verify(data, tag)
        return plaintext.decode('utf-8')
    except Exception as e:
        # 解密失败：记录错误日志（不输出密文本身），返回空字符串
        _logger.error(f'[安全] 解密失败: {type(e).__name__}，可能因密钥变更或数据损坏')
        return ''


def is_encrypted(value: Union[str, None, Any]) -> bool:
    """判断值是否为 ENC[...] 格式的加密字符串"""
    if not isinstance(value, str):
        return False
    return value.startswith('ENC[') and value.endswith(']')
