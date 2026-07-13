import hashlib
import base64
import struct
import socket
import time
import random
import string
import json
import os
import threading
import requests
from Crypto.Cipher import AES
from Crypto.Protocol.KDF import scrypt
from Crypto.Random import get_random_bytes

_KEY_SIZE = 32
_NONCE_SIZE = 12
_SALT_SIZE = 16
_ENCRYPTION_KEY = None

def _get_encryption_key():
    global _ENCRYPTION_KEY
    if _ENCRYPTION_KEY is None:
        env_key = os.environ.get('ENCRYPTION_KEY')
        if not env_key:
            _ENCRYPTION_KEY = os.environ.get('FLASK_SECRET_KEY', '')[:_KEY_SIZE]
            if len(_ENCRYPTION_KEY) < _KEY_SIZE:
                _ENCRYPTION_KEY = _ENCRYPTION_KEY.ljust(_KEY_SIZE, '0')
        else:
            _ENCRYPTION_KEY = env_key[:_KEY_SIZE].ljust(_KEY_SIZE, '0')
    return _ENCRYPTION_KEY

def encrypt(plaintext):
    if not plaintext:
        return ''
    key = _get_encryption_key()
    salt = get_random_bytes(_SALT_SIZE)
    nonce = get_random_bytes(_NONCE_SIZE)
    derived_key = scrypt(key, salt, _KEY_SIZE, N=2**14, r=8, p=1)
    cipher = AES.new(derived_key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext.encode('utf-8'))
    encoded = base64.b64encode(salt + nonce + tag + ciphertext).decode('ascii')
    return f'ENC[{encoded}]'

def decrypt(ciphertext):
    if not ciphertext:
        return ''
    if ciphertext.startswith('ENC[') and ciphertext.endswith(']'):
        encoded = ciphertext[4:-1]
    else:
        return ciphertext
    try:
        decoded = base64.b64decode(encoded)
        salt = decoded[:_SALT_SIZE]
        nonce = decoded[_SALT_SIZE:_SALT_SIZE + _NONCE_SIZE]
        tag = decoded[_SALT_SIZE + _NONCE_SIZE:_SALT_SIZE + _NONCE_SIZE + 16]
        data = decoded[_SALT_SIZE + _NONCE_SIZE + 16:]
        key = _get_encryption_key()
        derived_key = scrypt(key, salt, _KEY_SIZE, N=2**14, r=8, p=1)
        cipher = AES.new(derived_key, AES.MODE_GCM, nonce=nonce)
        plaintext = cipher.decrypt_and_verify(data, tag)
        return plaintext.decode('utf-8')
    except Exception:
        return ciphertext

CONFIG_FILE = os.path.join(os.environ.get('DATA_DIR', os.path.dirname(os.path.abspath(__file__))), 'wechat_work_config.json')

# 配置文件读写锁：保护 load→modify→save 事务原子性
_config_lock = threading.Lock()


def _load_unlocked():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def _save_unlocked(config):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def load_config():
    with _config_lock:
        return _load_unlocked()


def save_config(config):
    with _config_lock:
        _save_unlocked(config)


def update_config(mutator):
    """事务性更新配置：load → mutator(config) → save，整个过程持有锁"""
    with _config_lock:
        config = _load_unlocked()
        mutator(config)
        _save_unlocked(config)


def get_access_token():
    config = load_config()
    corpid = config.get('corpid', '')
    corpsecret = decrypt(config.get('corpsecret', ''))
    if not corpid or not corpsecret:
        return None, '未配置企业微信'

    cached_token = config.get('access_token', {})
    if cached_token.get('token') and cached_token.get('expires', 0) > time.time():
        return cached_token['token'], 'ok'

    try:
        url = f'https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={corpid}&corpsecret={corpsecret}'
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if data.get('errcode') == 0:
            token = data['access_token']
            expires = time.time() + data.get('expires_in', 7200) - 300

            def _update(cfg):
                cfg['access_token'] = {'token': token, 'expires': expires}
            update_config(_update)
            return token, 'ok'
        return None, data.get('errmsg', '获取token失败')
    except Exception as e:
        return None, str(e)


def send_wechat_message(content, to_user='@all'):
    token, err = get_access_token()
    if not token:
        return False, err

    config = load_config()
    agentid = config.get('agentid', '')
    if not agentid:
        return False, '未配置AgentId'

    try:
        url = f'https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}'
        data = {
            'touser': to_user,
            'msgtype': 'text',
            'agentid': int(agentid),
            'text': {'content': content}
        }
        resp = requests.post(url, json=data, timeout=10)
        result = resp.json()
        if result.get('errcode') == 0:
            return True, '发送成功'
        return False, result.get('errmsg', '发送失败')
    except Exception as e:
        return False, str(e)


class WeChatCrypto:
    def __init__(self, token, encoding_aes_key, corp_id):
        self.token = token
        self.corp_id = corp_id
        self.key = base64.b64decode(encoding_aes_key + '=')

    def _decrypt(self, encrypted):
        iv = self.key[:16]
        cipher = AES.new(self.key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(base64.b64decode(encrypted))
        decrypted = decrypted[:-decrypted[-1]]
        content_len = struct.unpack('>I', decrypted[16:20])[0]
        content = decrypted[20:20 + content_len].decode('utf-8')
        from_id = decrypted[20 + content_len:].decode('utf-8')
        return content, from_id

    def _encrypt(self, reply_msg, from_user):
        msg = reply_msg.encode('utf-8')
        from_user = from_user.encode('utf-8')
        random_str = ''.join(random.choices(string.ascii_letters + string.digits, k=16)).encode('utf-8')
        msg_len = struct.pack('>I', len(msg))
        text = random_str + msg_len + msg + from_user
        block_size = 32
        pad_len = block_size - (len(text) % block_size)
        text += bytes([pad_len] * pad_len)
        iv = self.key[:16]
        cipher = AES.new(self.key, AES.MODE_CBC, iv)
        encrypted = base64.b64encode(cipher.encrypt(text)).decode('utf-8')
        return encrypted

    def verify_signature(self, signature, timestamp, nonce, echostr=None):
        params = [self.token, timestamp, nonce]
        if echostr:
            params.append(echostr)
        params.sort()
        hash_str = hashlib.sha1(''.join(params).encode('utf-8')).hexdigest()
        return hash_str == signature

    def decrypt_message(self, encrypted_msg):
        return self._decrypt(encrypted_msg)

    def encrypt_message(self, reply_msg, from_user):
        return self._encrypt(reply_msg, from_user)


def parse_message(xml_content):
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_content)
        msg = {}
        for child in root:
            msg[child.tag] = child.text
        return msg
    except Exception:
        return None


def build_reply_xml(to_user, from_user, content, crypto=None):
    timestamp = str(int(time.time()))
    nonce = ''.join(random.choices(string.ascii_letters + string.digits, k=16))

    reply_msg = f"""<xml>
<ToUserName><![CDATA[{to_user}]]></ToUserName>
<FromUserName><![CDATA[{from_user}]]></FromUserName>
<CreateTime>{timestamp}</CreateTime>
<MsgType><![CDATA[text]]></MsgType>
<Content><![CDATA[{content}]]></Content>
</xml>"""

    if crypto:
        encrypted = crypto.encrypt_message(reply_msg, crypto.corp_id)
        msg_signature = hashlib.sha1(
            ''.join(sorted([crypto.token, timestamp, nonce, encrypted])).encode('utf-8')
        ).hexdigest()
        return f"""<xml>
<Encrypt><![CDATA[{encrypted}]]></Encrypt>
<MsgSignature><![CDATA[{msg_signature}]]></MsgSignature>
<TimeStamp>{timestamp}</TimeStamp>
<Nonce><![CDATA[{nonce}]]></Nonce>
</xml>"""
    else:
        return reply_msg


def truncate_reply(text, max_bytes=2000):
    encoded = text.encode('utf-8')
    if len(encoded) <= max_bytes:
        return text
    truncated = encoded[:max_bytes - 3]
    while truncated and (truncated[-1] & 0xC0) == 0x80:
        truncated = truncated[:-1]
    return truncated.decode('utf-8', errors='ignore') + '...'


def create_menu(agentid):
    token, err = get_access_token()
    if not token:
        return False, err

    menu = {
        "button": [
            {
                "type": "click",
                "name": "查看电影",
                "key": "view_movies"
            },
            {
                "type": "click",
                "name": "批量转存",
                "key": "batch_transfer"
            },
            {
                "type": "click",
                "name": "目录",
                "key": "115_dir"
            }
        ]
    }

    try:
        url = f'https://qyapi.weixin.qq.com/cgi-bin/menu/create?access_token={token}&agentid={agentid}'
        resp = requests.post(url, json=menu, timeout=10)
        data = resp.json()
        if data.get('errcode') == 0:
            return True, '菜单创建成功'
        return False, data.get('errmsg', '创建菜单失败')
    except Exception as e:
        return False, str(e)


def handle_text_message(content):
    import re
    content = content.strip()

    magnet_match = re.search(r'(magnet:\?[^\s,，。！？.!?]+)', content, re.IGNORECASE)
    if magnet_match:
        magnet = magnet_match.group(1)
        before_magnet = content[:magnet_match.start()].strip()
        page_match = re.match(r'^(\d+)\s*(.+)$', before_magnet)
        if page_match:
            page = int(page_match.group(1))
            name = page_match.group(2).strip()
            return {'page': page, 'name': name, 'magnet': magnet}

    # 单行格式：页码 电影名（无磁力链接）
    single_match = re.match(r'^(\d+)\s+(.+)$', content)
    if single_match:
        page = int(single_match.group(1))
        name = single_match.group(2).strip()
        if name:
            return {'page': page, 'name': name, 'magnet': ''}

    lines = content.split('\n')
    if len(lines) >= 2:
        page = lines[0].strip()
        name = lines[1].strip()
        magnet = lines[2].strip() if len(lines) >= 3 else ''
        try:
            page = int(page)
        except (ValueError, TypeError):
            return f'页码必须是数字，收到: {page}'
        if not name:
            return '电影名不能为空'
        return {'page': page, 'name': name, 'magnet': magnet}
    elif content.lower() in ['帮助', 'help', '?']:
        return ('使用方法:\n'
                '格式1: 页码 电影名 磁力链接\n'
                '格式2: 页码\\n电影名\\n磁力链接(可留空)\n'
                '搜索: 搜索 电影名\n'
                '示例:\n'
                '1 电影名 magnet:?xt=...\n'
                '1\\n电影名')
    else:
        return '格式错误，请按以下格式发送:\n页码 电影名 磁力链接\n搜索: 搜索 电影名\n\n发送"帮助"查看详细说明'