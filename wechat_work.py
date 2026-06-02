import hashlib
import base64
import struct
import socket
import time
import random
import string
import json
import os
import requests
from Crypto.Cipher import AES

CONFIG_FILE = os.path.join(os.environ.get('DATA_DIR', os.path.dirname(os.path.abspath(__file__))), 'wechat_work_config.json')


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_config(config):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def get_access_token():
    config = load_config()
    corpid = config.get('corpid', '')
    corpsecret = config.get('corpsecret', '')
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
            config['access_token'] = {'token': token, 'expires': expires}
            save_config(config)
            return token, 'ok'
        return None, data.get('errmsg', '获取token失败')
    except Exception as e:
        return None, str(e)


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
        print(f'[WeChat Verify] Params: {params}, Hash: {hash_str}, Expected: {signature}', flush=True)
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


def handle_text_message(content):
    content = content.strip()
    lines = content.split('\n')
    if len(lines) >= 3:
        page = lines[0].strip()
        name = lines[1].strip()
        magnet = lines[2].strip()
        try:
            page = int(page)
        except (ValueError, TypeError):
            return f'页码必须是数字，收到: {page}'
        if not name:
            return '电影名不能为空'
        return {'page': page, 'name': name, 'magnet': magnet}
    elif content.lower() in ['帮助', 'help', '?']:
        return ('使用方法:\n'
                '格式: 页码\\n电影名\\n磁力链接\n'
                '示例:\n'
                '1\n'
                '电影名\n'
                'magnet:?xt=...')
    else:
        return '格式错误，请按以下格式发送:\n页码\\n电影名\\n磁力链接\n\n发送"帮助"查看详细说明'