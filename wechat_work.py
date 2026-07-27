import hashlib
import base64
import struct
import time
import random
import string
import json
import os
import threading
import requests
from Crypto.Cipher import AES

# 加密工具统一入口
from crypto_utils import encrypt, decrypt

CONFIG_FILE = os.path.join(os.environ.get('DATA_DIR', os.path.dirname(os.path.abspath(__file__))), 'wechat_work_config.json')

# 配置文件读写锁：保护 load→modify→save 事务原子性
_config_lock = threading.Lock()

# access_token 刷新锁：防止并发刷新token
_token_lock = threading.Lock()


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
    """获取access_token，带双重检查锁避免并发刷新"""
    config = load_config()
    corpid = config.get('corpid', '')
    corpsecret = decrypt(config.get('corpsecret', ''))
    if not corpid or not corpsecret:
        return None, '未配置企业微信'

    cached_token = config.get('access_token', {})
    if cached_token.get('token') and cached_token.get('expires', 0) > time.time():
        return cached_token['token'], 'ok'

    # 加锁防止并发刷新token
    with _token_lock:
        # 双重检查：拿到锁后再次检查缓存，避免重复请求
        config = load_config()
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

    # agentid 必须为整数
    try:
        agentid_int = int(agentid)
    except (ValueError, TypeError):
        return False, 'AgentId 配置无效，必须为整数'

    try:
        url = f'https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}'
        data = {
            'touser': to_user,
            'msgtype': 'text',
            'agentid': agentid_int,
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
        # encoding_aes_key 必须为43位 Base64 字符串，解码后32字节
        if not encoding_aes_key or len(encoding_aes_key) != 43:
            raise ValueError('encoding_aes_key 长度必须为43位')
        self.key = base64.b64decode(encoding_aes_key + '=')

    def _decrypt(self, encrypted):
        iv = self.key[:16]
        cipher = AES.new(self.key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(base64.b64decode(encrypted))
        # PKCS7 去填充：校验 pad_len 范围，防止越界
        if not decrypted:
            raise ValueError('解密结果为空')
        pad_len = decrypted[-1]
        if pad_len < 1 or pad_len > 32 or pad_len > len(decrypted):
            raise ValueError(f'PKCS7 填充长度非法: {pad_len}')
        # 校验所有填充字节一致
        if decrypted[-pad_len:] != bytes([pad_len] * pad_len):
            raise ValueError('PKCS7 填充字节不一致')
        decrypted = decrypted[:-pad_len]
        if len(decrypted) < 20:
            raise ValueError('解密内容过短')
        content_len = struct.unpack('>I', decrypted[16:20])[0]
        if 20 + content_len > len(decrypted):
            raise ValueError('内容长度字段非法')
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
    """解析微信消息 XML，处理 child.text 为 None 的情况"""
    import xml.etree.ElementTree as ET
    try:
        if isinstance(xml_content, bytes):
            xml_content = xml_content.decode('utf-8', errors='replace')
        root = ET.fromstring(xml_content)
        msg = {}
        for child in root:
            # child.text 可能为 None（空标签如 <Content></Content>）
            msg[child.tag] = child.text or ''
        return msg
    except Exception:
        return None


def _xml_escape_cdata(text):
    """转义 CDATA 内容中的 ]]> 防止 CDATA 注入"""
    if not text:
        return ''
    return str(text).replace(']]>', ']]]]><![CDATA[>')


def build_reply_xml(to_user, from_user, content, crypto=None):
    timestamp = str(int(time.time()))
    nonce = ''.join(random.choices(string.ascii_letters + string.digits, k=16))

    # 转义 CDATA 中的内容，防止注入
    to_user_esc = _xml_escape_cdata(to_user)
    from_user_esc = _xml_escape_cdata(from_user)
    content_esc = _xml_escape_cdata(content)

    reply_msg = f"""<xml>
<ToUserName><![CDATA[{to_user_esc}]]></ToUserName>
<FromUserName><![CDATA[{from_user_esc}]]></FromUserName>
<CreateTime>{timestamp}</CreateTime>
<MsgType><![CDATA[text]]></MsgType>
<Content><![CDATA[{content_esc}]]></Content>
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
                "name": "论坛进度",
                "key": "forum_progress"
            },
            {
                "type": "click",
                "name": "增量拉取",
                "key": "forum_incremental"
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

    # 磁力链接匹配：排除空白和常见标点，但不排除点号（磁力链接中可能包含点号）
    magnet_match = re.search(r'(magnet:\?[^\s,，。！？!?]+)', content, re.IGNORECASE)
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
                '论坛进度: 发送"论坛进度"或"进度"\n'
                '示例:\n'
                '1 电影名 magnet:?xt=...\n'
                '1\\n电影名')
    else:
        return '格式错误，请按以下格式发送:\n页码 电影名 磁力链接\n搜索: 搜索 电影名\n\n发送"帮助"查看详细说明'