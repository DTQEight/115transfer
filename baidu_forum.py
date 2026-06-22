"""baidubaidu论坛搜索 + 种子转磁力链接

流程：
1. 登录论坛（Discuz X3.4，GBK编码）
2. 搜索电影帖子
3. 访问帖子详情，提取附件aid
4. 下载附件（.torrent文件）
5. 解析torrent的info字典，计算SHA1得到info_hash
6. 拼成 magnet:?xt=urn:btih:<hash>
"""
import os
import re
import json
import time
import hashlib
import urllib.parse
import requests

CONFIG_FILE = os.path.join(
    os.environ.get('DATA_DIR', os.path.dirname(os.path.abspath(__file__))),
    'baidu_forum_config.json'
)

BASE = 'https://10001.baidubaidu.win/'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_config(config):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def _get_session():
    """构建带登录态的session"""
    config = load_config()
    username = config.get('username', '').strip()
    password = config.get('password', '').strip()
    if not username or not password:
        raise ValueError('未配置论坛账号密码')

    s = requests.Session()
    s.headers.update({'User-Agent': UA})
    s.trust_env = False  # 忽略系统代理环境变量

    # 若有缓存的cookies且未过期，直接复用
    cached = config.get('cookies')
    cached_ts = config.get('cookies_ts', 0)
    if cached and (time.time() - cached_ts < 86400):
        for k, v in cached.items():
            s.cookies.set(k, v, domain='10001.baidubaidu.win')
        # 验证一下
        if _is_logged_in(s):
            return s
        # 失效则重新登录

    _login(s, username, password)
    # 缓存cookies
    new_config = load_config()
    new_config['cookies'] = dict(s.cookies)
    new_config['cookies_ts'] = time.time()
    save_config(new_config)
    return s


def _is_logged_in(s):
    """检查是否已登录"""
    try:
        r = s.get(BASE + 'forum.php', timeout=10)
        r.encoding = 'gbk'
        # 已登录页面会显示用户名或退出链接，未登录会跳转登录页
        return 'action=logout' in r.text or 'mod=logging' not in r.url
    except Exception:
        return False


def _login(s, username, password):
    """登录论坛"""
    r = s.get(BASE + 'member.php?mod=logging&action=login', timeout=15)
    r.encoding = 'gbk'
    m_formhash = re.search(r'name="formhash"\s+value="([^"]+)"', r.text)
    m_loginhash = re.search(r'loginhash=([A-Za-z0-9]+)', r.text)
    if not m_formhash or not m_loginhash:
        raise RuntimeError('登录页解析失败')
    formhash = m_formhash.group(1)
    loginhash = m_loginhash.group(1)

    login_url = BASE + f'member.php?mod=logging&action=login&loginsubmit=yes&loginhash={loginhash}'
    data = {
        'formhash': formhash,
        'referer': BASE,
        'username': username,
        'password': password,
        'answer': '',
        'cookietime': '2592000',
    }
    r2 = s.post(login_url, data=data, timeout=15, allow_redirects=True)
    r2.encoding = 'gbk'
    if 'succeedhandle' not in r2.text and '欢迎您回来' not in r2.text and 'succeedhandle_login' not in r2.text:
        # 再检查一下是否已经登录
        if not _is_logged_in(s):
            err = re.search(r'class="alert_info"[^>]*>([\s\S]*?)</div>', r2.text)
            raise RuntimeError('登录失败: ' + (err.group(1).strip() if err else r2.url))
    return s


def search(keyword, max_results=20):
    """搜索电影帖子

    Returns:
        [{'tid': ..., 'title': ..., 'forum': ..., 'replies': ..., 'views': ..., 'author': ..., 'date': ...}, ...]
    """
    s = _get_session()
    # GBK编码关键词
    kw_encoded = urllib.parse.quote(keyword, encoding='gbk')
    url = BASE + f'search.php?mod=forum&searchsubmit=yes&srchtxt={kw_encoded}'
    r = s.get(url, timeout=15, allow_redirects=True)
    r.encoding = 'gbk'

    # 无结果
    if '没有找到' in r.text or '没有匹配' in r.text:
        return []

    results = []
    # 搜索结果在 <li class="pbw" id="tid"> 中
    items = re.findall(
        r'<li class="pbw" id="(\d+)">(.*?)</li>',
        r.text, re.DOTALL
    )
    for tid, body in items:
        # 标题（去除HTML标签）
        title_m = re.search(r'<a[^>]*href="forum\.php\?mod=viewthread&amp;tid=\d+[^"]*"[^>]*>(.*?)</a>', body, re.DOTALL)
        title = _strip_html(title_m.group(1)) if title_m else ''
        # 版块
        forum_m = re.search(r'class="xi1">([^<]+)</a>', body)
        forum = forum_m.group(1).strip() if forum_m else ''
        # 回复/查看
        stats_m = re.search(r'(\d+)\s*个回复\s*-\s*(\d+)\s*次查看', body)
        replies = stats_m.group(1) if stats_m else '0'
        views = stats_m.group(2) if stats_m else '0'
        # 作者
        author_m = re.search(r'home\.php\?mod=space&amp;uid=\d+"[^>]*>([^<]+)</a>', body)
        author = author_m.group(1).strip() if author_m else ''
        # 日期
        date_m = re.search(r'<span>(\d{4}-\d{1,2}-\d{1,2}[^<]*)</span>', body)
        date = date_m.group(1).strip() if date_m else ''

        results.append({
            'tid': tid,
            'title': title,
            'forum': forum,
            'replies': replies,
            'views': views,
            'author': author,
            'date': date,
        })
        if len(results) >= max_results:
            break
    return results


def get_thread_attachments(tid):
    """获取帖子中的附件下载链接

    Returns:
        [{'aid': ..., 'filename': ..., 'url': ...}, ...]
    """
    s = _get_session()
    url = BASE + f'forum.php?mod=viewthread&tid={tid}'
    r = s.get(url, timeout=15)
    r.encoding = 'gbk'

    attachments = []
    # 附件链接格式: forum.php?mod=attachment&aid=xxx&nothumb=yes (或带其他参数)
    # aid 是 base64 编码的字符串
    aids = re.findall(
        r'href="forum\.php\?mod=attachment&amp;aid=([^"&]+)[^"]*"',
        r.text
    )
    seen = set()
    for aid in aids:
        if aid in seen:
            continue
        seen.add(aid)
        # 文件名从附件附近的文本提取
        attach_url = BASE + f'forum.php?mod=attachment&aid={urllib.parse.quote(aid)}&nothumb=yes'
        attachments.append({
            'aid': aid,
            'url': attach_url,
            'filename': '',
        })
    return attachments


def download_torrent(attach_url):
    """下载种子文件，返回二进制内容"""
    s = _get_session()
    r = s.get(attach_url, timeout=30, allow_redirects=True)
    if r.status_code != 200:
        raise RuntimeError(f'下载附件失败: HTTP {r.status_code}')
    # 检查是否真的是torrent文件（以 d8:announce 或 d4:info 开头）
    if not r.content[:1] == b'd':
        raise RuntimeError('附件不是有效的种子文件')
    return r.content, r.headers.get('Content-Disposition', '')


def torrent_to_magnet(torrent_content):
    """解析torrent文件，返回磁力链接

    手动解析bencode，提取info字典计算SHA1。
    """
    info_start, info_end = _find_info_dict_range(torrent_content)
    if info_start is None:
        raise RuntimeError('种子文件解析失败: 未找到info字典')
    info_bytes = torrent_content[info_start:info_end]
    info_hash = hashlib.sha1(info_bytes).hexdigest()

    # 提取name用于展示
    name = _extract_torrent_name(torrent_content)

    # 提取tracker
    trackers = _extract_trackers(torrent_content)

    # 磁力链接最小格式：只需要info_hash（约60字符）
    magnet = f'magnet:?xt=urn:btih:{info_hash}'
    return {
        'info_hash': info_hash,
        'name': name,
        'magnet': magnet,
        'trackers': trackers,
    }


def _find_info_dict_range(content):
    """找到info字典的字节范围 [start, end)"""
    marker = b'4:infod'
    idx = content.find(marker)
    if idx < 0:
        return None, None
    start = idx + len(marker) - 1  # 指向 d
    end = _find_bencode_end(content, start)
    if end < 0:
        return None, None
    return start, end


def _find_bencode_end(content, start):
    """从start位置的 d 或 l 开始，找到匹配的 e 的位置（exclusive）

    正确跳过字符串内容，避免字符串中的 d/e 字符干扰。
    """
    i = start
    c = content[i:i+1]
    if c not in (b'd', b'l'):
        return -1
    i += 1
    depth = 1
    while i < len(content) and depth > 0:
        c = content[i:i+1]
        if c in (b'd', b'l'):
            depth += 1
            i += 1
        elif c == b'e':
            depth -= 1
            i += 1
        elif c == b'i':
            # 整数 i<digits>e
            end_i = content.find(b'e', i)
            if end_i < 0:
                return -1
            i = end_i + 1
        elif c.isdigit():
            # 字符串 <length>:<bytes>
            colon = content.find(b':', i)
            if colon < 0:
                return -1
            length = int(content[i:colon])
            i = colon + 1 + length
        else:
            return -1
    return i if depth == 0 else -1


def _extract_torrent_name(content):
    """从torrent中提取info.name"""
    try:
        # 找 4:name 后面的字符串
        # 在info字典内找 4:name<len>:<value>
        m = re.search(rb'4:name(\d+):', content)
        if m:
            length = int(m.group(1))
            start = m.end()
            name_bytes = content[start:start+length]
            try:
                return name_bytes.decode('utf-8')
            except UnicodeDecodeError:
                return name_bytes.decode('gbk', errors='ignore')
    except Exception:
        pass
    return ''


def _extract_trackers(content):
    """提取tracker列表"""
    trackers = []
    # announce (单个)
    m = re.search(rb'8:announce(\d+):', content)
    if m:
        length = int(m.group(1))
        start = m.end()
        trackers.append(content[start:start+length].decode('utf-8', errors='ignore'))
    # announce-list (多个)
    idx = content.find(b'13:announce-listl')
    if idx >= 0:
        # 提取所有 tracker URL
        for m in re.finditer(rb'\d+:https?://[^\s"]+', content[idx:idx+2000]):
            url = m.group(0)
            # 去掉长度前缀
            colon = url.find(b':')
            url = url[colon+1:].decode('utf-8', errors='ignore')
            if url not in trackers:
                trackers.append(url)
    return trackers


def _strip_html(html_text):
    """去除HTML标签，保留纯文本"""
    text = re.sub(r'<[^>]+>', '', html_text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&quot;', '"', text)
    return text.strip()


def get_magnet_from_thread(tid):
    """一键获取帖子第一个附件的磁力链接

    Returns:
        {'tid': ..., 'magnet': ..., 'name': ..., 'info_hash': ..., 'filename': ...}
    """
    attachments = get_thread_attachments(tid)
    if not attachments:
        raise RuntimeError('帖子中没有找到附件')

    # 尝试每个附件，找到第一个有效的torrent
    last_err = ''
    for att in attachments:
        try:
            content, disposition = download_torrent(att['url'])
            # 从Content-Disposition提取文件名
            if disposition:
                fn_m = re.search(r'filename="([^"]+)"', disposition)
                if fn_m:
                    att['filename'] = fn_m.group(1)
            result = torrent_to_magnet(content)
            result['tid'] = tid
            result['filename'] = att.get('filename', '')
            return result
        except Exception as e:
            last_err = str(e)
            continue
    raise RuntimeError(f'所有附件解析失败: {last_err}')
