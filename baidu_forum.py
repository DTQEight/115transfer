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
import threading
import ssl
import socket
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib3.exceptions import MaxRetryError
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CONFIG_FILE = os.path.join(
    os.environ.get('DATA_DIR', os.path.dirname(os.path.abspath(__file__))),
    'baidu_forum_config.json'
)

BASE = 'https://10001.baidubaidu.win/'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

# 全局Session复用，避免每次操作都重新创建Session和验证登录
_global_session = None
_global_session_ts = 0
_session_lock = threading.Lock()

# 配置文件读写锁：保护 load→modify→save 事务原子性
_config_lock = threading.Lock()

# searchid + 分页元数据 内存缓存：按关键词缓存，避免写入共享配置文件造成并发覆盖
_searchid_cache = {}
_searchid_cache_lock = threading.Lock()
_SEARCHID_CACHE_MAX = 50


def _get_cached_searchid(keyword):
    with _searchid_cache_lock:
        entry = _searchid_cache.get(keyword.lower())
        return entry['searchid'] if entry else None


def _set_cached_searchid(keyword, searchid, total_pages=None, total_count=None):
    with _searchid_cache_lock:
        if len(_searchid_cache) >= _SEARCHID_CACHE_MAX:
            _searchid_cache.pop(next(iter(_searchid_cache)))
        entry = _searchid_cache.get(keyword.lower(), {})
        entry['searchid'] = searchid
        # 只在有效值时更新（不覆盖已有更准确的值）
        if total_pages is not None and total_pages > 0:
            entry['total_pages'] = total_pages
        if total_count is not None and total_count > 0:
            entry['total_count'] = total_count
        _searchid_cache[keyword.lower()] = entry


def _get_cached_meta(keyword):
    """返回缓存的 (total_pages, total_count)，无缓存返回 (None, None)"""
    with _searchid_cache_lock:
        entry = _searchid_cache.get(keyword.lower())
        if not entry:
            return None, None
        return entry.get('total_pages'), entry.get('total_count')


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


def _build_session():
    """构建带连接池的session（不含登录态）

    针对论坛不稳定TLS（SSLZeroReturnError）做了三层兜底：
      1) 请求级 Retry：HTTP 5xx + 连接/SSL 错误（SSLZeroReturn/EOF/ConnectionReset）
         都会自动重试 2 次，指数退避
      2) SSL 上下文：强制 TLSv1.2，避免服务器在版本协商阶段直接 EOF
         + 禁用 verify（论坛证书不标准）+ 禁用系统代理
      3) 业务调用侧还有"SSLError 时放弃当前连接池、重建全新 Session 再试"的二次兜底
    """
    s = requests.Session()
    s.headers.update({'User-Agent': UA})
    s.trust_env = False  # 忽略系统代理环境变量
    s.verify = False  # 论坛SSL证书域名不匹配，禁用证书验证

    # 定制 SSLContext：强制 TLSv1.2 + 常用密码套件，降低服务端 TLS 协商阶段 EOF 概率
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    except AttributeError:
        ctx.options |= 0x80000  # OP_NO_SSLv3
        ctx.options |= 0x4000000  # OP_NO_TLSv1
        ctx.options |= 0x10000000  # OP_NO_TLSv1_1
    try:
        ctx.set_ciphers('DEFAULT@SECLEVEL=1')
    except ssl.SSLError:
        pass

    # mount 时把自定义 ssl_context 赋给 urllib3 poolmanager
    # requests.Session.mount 的 HTTPAdapter 默认使用 self.init_poolmanager，
    # 我们通过子类来注入 ssl_context
    class _SSLAdapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            kwargs['ssl_context'] = ctx
            return super().init_poolmanager(*args, **kwargs)

    # 配置连接池 + 重试：新增连接错误（含TLS握手异常）的自动重试
    # urllib3 >=1.26 的 Retry 支持 connect/read 错误重试：通过 backoff_factor 控制间隔
    retry = Retry(
        total=4,  # 总尝试次数上限（包含重定向）
        backoff_factor=0.6,  # 0.6s, 1.2s, 2.4s 指数退避
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        connect=3,  # 连接/握手错误重试3次
        read=2,     # 读取中断错误重试2次
        other=1,    # 其它未分类错误重试1次
    )
    adapter = _SSLAdapter(pool_connections=16, pool_maxsize=16, max_retries=retry)
    s.mount('http://', adapter)
    s.mount('https://', adapter)
    return s


def _wrap_ssl_retry(fn, max_ssl_retries=2):
    """对可能发生 SSLZeroReturnError/EOF 的请求再包一层 Session 重建重试。

    原因：_build_session 里 urllib3.Retry 会在**同一个连接池**里重试，
    如果是服务端按"源IP/四元组"触发了 EOF，同一个连接池继续尝试仍会失败。
    这里在捕获到 SSL/连接层错误时，调用 _reset_session() 销毁全局连接池，
    再新建 Session 继续尝试，相当于"换一条TCP通道再试"。
    """
    last_err = None
    for attempt in range(max_ssl_retries + 1):
        try:
            return fn()
        except (requests.exceptions.SSLError,
                ssl.SSLZeroReturnError if hasattr(ssl, 'SSLZeroReturnError') else ssl.SSLEOFError if hasattr(ssl, 'SSLEOFError') else ssl.SSLError,
                requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError,
                MaxRetryError,
                ConnectionResetError,
                BrokenPipeError,
                socket.error) as e:
            last_err = e
            if attempt < max_ssl_retries:
                time.sleep(0.8 * (attempt + 1))
                _reset_session()
                continue
            # 最后一次失败：把技术性异常重新封装成带原因的友好 RuntimeError
            msg = str(e) or e.__class__.__name__
            friendly = '论坛服务器TLS握手异常（连接被远端直接断开），通常稍后重试或网络波动后恢复'
            if 'timed out' in msg.lower() or 'timeout' in msg.lower():
                friendly = '论坛连接超时，请检查网络或稍后重试'
            elif 'Name or service not known' in msg or 'getaddrinfo' in msg:
                friendly = '论坛域名解析失败，请确认 baidubaidu.win 是否可访问'
            elif 'refused' in msg.lower():
                friendly = '论坛连接被拒绝，可能服务暂时不可用'
            raise RuntimeError(f'{friendly}（{e.__class__.__name__}: {msg[:200]}）') from e


def _get_session():
    """获取带登录态的全局session（复用，避免每次创建+验证）

    策略：
    - 全局session存在且未过期(6小时)则直接复用，不再每次发请求验证登录态
    - 失效或不存在则重新登录
    - 这样单次获取磁力链接从原来的4次HTTP请求(2次验证+1帖子页+1下载)
      降为2次(1帖子页+1下载)，且复用TCP连接
    """
    global _global_session, _global_session_ts
    config = load_config()
    username = config.get('username', '').strip()
    password = config.get('password', '').strip()
    if not username or not password:
        raise ValueError('未配置论坛账号密码')

    with _session_lock:
        # 复用全局session（6小时内有效）
        if _global_session is not None and (time.time() - _global_session_ts < 21600):
            return _global_session

        s = _build_session()

        # 尝试复用缓存的cookies
        cached = config.get('cookies')
        cached_ts = config.get('cookies_ts', 0)
        if cached and (time.time() - cached_ts < 86400):
            for k, v in cached.items():
                s.cookies.set(k, v, domain='10001.baidubaidu.win')
            # 轻量验证：只在首次加载时验证一次，之后信任全局session
            if _is_logged_in(s):
                _global_session = s
                _global_session_ts = time.time()
                return s

        # 重新登录（不用缓存cookies兜底，直接走登录流程）
        _login(s, username, password)
        # 缓存cookies（事务性更新，避免覆盖并发修改的 username/password）
        def _update(cfg):
            cfg['cookies'] = dict(s.cookies)
            cfg['cookies_ts'] = time.time()
        update_config(_update)
        _global_session = s
        _global_session_ts = time.time()
        return s


def _reset_session():
    """重置全局session（登录失效时调用）"""
    global _global_session, _global_session_ts
    with _session_lock:
        _global_session = None
        _global_session_ts = 0


def _is_logged_in(s):
    """检查是否已登录：通过页面特征判断，多重条件避免误判"""
    try:
        r = s.get(BASE + 'forum.php', timeout=10)
        r.encoding = 'gbk'
        # 已登录页面包含退出链接或用户控制面板
        # 未登录页面会重定向到登录页（URL包含mod=logging）
        if 'mod=logging' in r.url:
            return False
        # 退出链接是已登录的强信号
        if 'action=logout' in r.text:
            return True
        # 用户面板链接也是已登录信号
        if 'home.php?mod=space' in r.text and '登录' not in r.text[:500]:
            return True
        return False
    except Exception:
        return False


def _login(s, username, password):
    """登录论坛（外层调用会在 session 重建后再次调用，所以 s.session 可能是新建的）"""
    def _step1():
        r = s.get(BASE + 'member.php?mod=logging&action=login', timeout=15)
        r.encoding = 'gbk'
        return r
    r = _wrap_ssl_retry(_step1)
    m_formhash = re.search(r'name="formhash"\s+value="([^"]+)"', r.text)
    m_loginhash = re.search(r'loginhash=([A-Za-z0-9]+)', r.text)
    if not m_formhash or not m_loginhash:
        raise RuntimeError('登录页解析失败（论坛页面未返回预期内容，可能登录限制或临时宕机）')
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

    def _step2():
        return s.post(login_url, data=data, timeout=15, allow_redirects=True)
    r2 = _wrap_ssl_retry(_step2)
    r2.encoding = 'gbk'

    # 成功标志
    if 'succeedhandle' in r2.text or '欢迎您回来' in r2.text or 'succeedhandle_login' in r2.text:
        return s

    # 失败：提取 Discuz 真实错误信息
    # Discuz X3.x 登录失败的错误消息在 <div class="alert_error"> 或 alert_info 中
    err_msg = ''

    # alert_error（Discuz 常见错误容器）
    err_m = re.search(r'class="alert_error"[^>]*>([\s\S]*?)</div>', r2.text)
    if err_m:
        err_msg = _strip_html(err_m.group(1)).strip()
    if not err_msg:
        # alert_info
        err_m = re.search(r'class="alert_info"[^>]*>([\s\S]*?)</div>', r2.text)
        if err_m:
            err_msg = _strip_html(err_m.group(1)).strip()
    if not err_msg:
        # messagetext
        err_m = re.search(r'class="messagetext"[^>]*>([\s\S]*?)</div>', r2.text)
        if err_m:
            err_msg = _strip_html(err_m.group(1)).strip()

    # 常见 Discuz 登录失败模式
    txt = r2.text
    if '登录次数过多' in txt or 'floodcontrol' in txt:
        err_msg = err_msg or '登录次数过多，论坛触发了登录频控，请稍等几分钟后再试（或在网页上退出后重试）'
    elif '验证码' in txt or 'seccode' in txt or 'secqaa' in txt:
        err_msg = err_msg or '论坛要求输入验证码，当前无法自动处理，请稍后在网页上正常登录一次再试'
    elif '密码错误' in txt or '登录失败' in txt:
        err_msg = err_msg or '账号或密码错误'
    elif '禁止' in txt and '登录' in txt:
        err_msg = err_msg or '账号被禁止登录'
    elif '欢迎您回来' not in txt and 'succeedhandle' not in txt:
        # 最终兜底：返回响应的前300字符纯文本，帮助诊断
        snippet = _strip_html(txt)[:300].strip()
        err_msg = err_msg or f'登录响应异常（可能会话冲突或网页端登录占用了会话）: {snippet}'

    # 最后再检查一次是否其实已登录（有些 Discuz 版本成功后不返回 succeedhandle）
    if _is_logged_in(s):
        return s

    raise RuntimeError(err_msg or '登录失败: 未知原因')


def search(keyword, page=1):
    """搜索电影帖子，支持分页

    Returns:
        {'results': [...], 'page': N, 'total_pages': N, 'total_count': N, 'searchid': '...'}
    """
    s = _get_session()
    # GBK 编码可能对某些字符（如emoji、特殊Unicode）失败，需捕获异常
    try:
        kw_encoded = urllib.parse.quote(keyword, encoding='gbk')
    except UnicodeEncodeError:
        # GBK无法编码的字符用替换字符代替
        kw_encoded = urllib.parse.quote(keyword.encode('gbk', errors='replace').decode('gbk', errors='replace'), encoding='gbk')

    # 第1页需要先获取searchid
    if page == 1:
        url = BASE + f'search.php?mod=forum&searchsubmit=yes&srchtxt={kw_encoded}'
    else:
        # 后续页需要searchid，从内存缓存取（按关键词隔离，避免并发覆盖）
        searchid = _get_cached_searchid(keyword)
        if not searchid:
            url = BASE + f'search.php?mod=forum&searchsubmit=yes&srchtxt={kw_encoded}'
            page = 1
        else:
            url = BASE + f'search.php?mod=forum&searchid={searchid}&orderby=lastpost&ascdesc=desc&searchsubmit=yes&kw={kw_encoded}&page={page}'

    r = _wrap_ssl_retry(lambda: s.get(url, timeout=(5, 15), allow_redirects=True))
    r.encoding = 'gbk'

    # 提取searchid并缓存到内存（不写文件，避免并发覆盖其他配置项）
    searchid_m = re.search(r'searchid=(\d+)', r.url)
    if searchid_m:
        searchid = searchid_m.group(1)
        _set_cached_searchid(keyword, searchid)
    else:
        searchid = ''

    # 无结果
    if '没有找到' in r.text or '没有匹配' in r.text:
        return {'results': [], 'page': page, 'total_pages': 0, 'total_count': 0, 'searchid': searchid}

    # 提取总结果数
    cnt_m = re.search(r'相关内容\s*(\d+)\s*个', r.text)
    total_count = int(cnt_m.group(1)) if cnt_m else 0

    # 提取分页数（从分页链接中提取最大页码）
    page_nums = [int(p) for p in re.findall(r'page=(\d+)', r.text)]
    total_pages = max(page_nums) if page_nums else 1

    # 第 2 页及以后，论坛 HTML 的分页区可能不含 page= 链接，导致 total_pages=1
    # 用第 1 页缓存的 total_pages / total_count 兜底
    cached_tp, cached_tc = _get_cached_meta(keyword)
    if cached_tp and (total_pages <= 1 or total_pages < page):
        total_pages = cached_tp
    if not total_count and cached_tc:
        total_count = cached_tc

    # 缓存分页元数据（后续页复用）
    _set_cached_searchid(keyword, searchid, total_pages=total_pages, total_count=total_count)

    results = []
    items = re.findall(
        r'<li class="pbw" id="(\d+)">(.*?)</li>',
        r.text, re.DOTALL
    )
    for tid, body in items:
        title_m = re.search(r'<a[^>]*href="forum\.php\?mod=viewthread&amp;tid=\d+[^"]*"[^>]*>(.*?)</a>', body, re.DOTALL)
        title = _strip_html(title_m.group(1)) if title_m else ''
        forum_m = re.search(r'class="xi1">([^<]+)</a>', body)
        forum = forum_m.group(1).strip() if forum_m else ''
        stats_m = re.search(r'(\d+)\s*个回复\s*-\s*(\d+)\s*次查看', body)
        replies = stats_m.group(1) if stats_m else '0'
        views = stats_m.group(2) if stats_m else '0'
        author_m = re.search(r'home\.php\?mod=space&amp;uid=\d+"[^>]*>([^<]+)</a>', body)
        author = author_m.group(1).strip() if author_m else ''
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

    return {
        'results': results,
        'page': page,
        'total_pages': total_pages,
        'total_count': total_count,
        'searchid': searchid,
    }


def get_thread_attachments(tid):
    """获取帖子中的附件下载链接（兼容接口，使用全局session）

    Returns:
        [{'aid': ..., 'filename': ..., 'url': ...}, ...]
    """
    s = _get_session()
    return _get_thread_attachments_with_session(s, tid)


def download_torrent(attach_url):
    """下载种子文件，返回二进制内容（兼容接口，使用全局session）"""
    s = _get_session()
    return _download_torrent_with_session(s, attach_url)


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
    """去除HTML标签，保留纯文本

    先移除 script/style 内容避免其中的代码被当作文本保留
    """
    # 移除 script 和 style 标签及其内容
    text = re.sub(r'<script[^>]*>[\s\S]*?</script>', '', html_text, flags=re.IGNORECASE)
    text = re.sub(r'<style[^>]*>[\s\S]*?</style>', '', text, flags=re.IGNORECASE)
    # 去除所有 HTML 标签
    text = re.sub(r'<[^>]+>', '', text)
    # HTML 实体解码
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
    # 首次尝试用全局session
    s = _get_session()
    try:
        return _get_magnet_from_thread_with_session(s, tid)
    except Exception as first_err:
        # 如果失败且可能是登录态失效，重置session重试一次
        err_msg = str(first_err).lower()
        if '登录' in err_msg or 'login' in err_msg or 'logout' in err_msg or '403' in err_msg:
            _reset_session()
            try:
                s = _get_session()
                return _get_magnet_from_thread_with_session(s, tid)
            except Exception:
                pass  # 重试也失败，抛出原始错误
        raise


def _get_magnet_from_thread_with_session(s, tid):
    """使用指定 session 获取帖子磁力链接（已登录）"""
    attachments = _get_thread_attachments_with_session(s, tid)
    if not attachments:
        raise RuntimeError('帖子中没有找到附件')

    # 尝试每个附件，找到第一个有效的torrent
    last_err = ''
    for att in attachments:
        try:
            content, disposition = _download_torrent_with_session(s, att['url'])
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


def _get_thread_attachments_with_session(s, tid):
    """使用已有session获取帖子附件（避免重复创建session）"""
    url = BASE + f'forum.php?mod=viewthread&tid={tid}'
    r = _wrap_ssl_retry(lambda: s.get(url, timeout=(5, 15)))
    r.encoding = 'gbk'

    attachments = []
    aids = re.findall(
        r'href="forum\.php\?mod=attachment&amp;aid=([^"&]+)[^"]*"',
        r.text
    )
    seen = set()
    for aid in aids:
        if aid in seen:
            continue
        seen.add(aid)
        attach_url = BASE + f'forum.php?mod=attachment&aid={urllib.parse.quote(aid)}&nothumb=yes'
        attachments.append({
            'aid': aid,
            'url': attach_url,
            'filename': '',
        })
    return attachments


def _download_torrent_with_session(s, attach_url):
    """使用已有session下载种子（避免重复创建session）"""
    r = _wrap_ssl_retry(lambda: s.get(attach_url, timeout=(5, 30), allow_redirects=True))
    if r.status_code != 200:
        raise RuntimeError(f'下载附件失败: HTTP {r.status_code}')
    if not r.content[:1] == b'd':
        raise RuntimeError('附件不是有效的种子文件')
    return r.content, r.headers.get('Content-Disposition', '')


def batch_get_magnets(tids, max_workers=6):
    """并发批量获取多个帖子的磁力链接

    Args:
        tids: 帖子ID列表
        max_workers: 最大并发数

    Returns:
        {'success': [...], 'failed': [...]}
    """
    results = {'success': [], 'failed': []}
    if not tids:
        return results

    base_session = _get_session()  # 已登录的全局 session

    # 深拷贝 cookies 和 headers，确保线程间不共享可变状态
    base_cookies = base_session.cookies.copy()
    base_headers = dict(base_session.headers)

    def _worker(tid):
        try:
            # 每个线程创建独立 session，复用连接池配置但独立 cookies
            local_session = _build_session()
            local_session.cookies = base_cookies.copy()
            local_session.headers.update(base_headers)
            r = _get_magnet_from_thread_with_session(local_session, tid)
            return ('ok', tid, r)
        except Exception as e:
            return ('err', tid, str(e))

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_worker, tid): tid for tid in tids}
        for fut in as_completed(futures):
            status, tid, data = fut.result()
            if status == 'ok':
                results['success'].append(data)
            else:
                results['failed'].append({'tid': tid, 'error': data})
    return results
