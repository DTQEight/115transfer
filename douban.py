"""豆瓣看过的电影同步模块"""
import requests
import re
import json
import os
import time
import threading
import html as html_module
import logging

# 加密工具统一入口
from crypto_utils import encrypt, decrypt

CONFIG_FILE = os.path.join(os.environ.get('DATA_DIR', os.path.dirname(os.path.abspath(__file__))), 'douban_config.json')
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

# 配置文件读写锁：保护 load→modify→save 事务原子性
_config_lock = threading.Lock()


def _load_unlocked():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logging.getLogger('douban').warning(f'[豆瓣] 配置文件读取失败: {e}，使用空配置')
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


def _get_headers():
    config = load_config()
    cookie = decrypt(config.get('cookie', ''))
    return {
        'User-Agent': USER_AGENT,
        'Cookie': cookie,
        'Referer': 'https://movie.douban.com/',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    }


def fetch_watched_movies(user_id, start=0, count=15):
    """获取用户看过的电影列表（单页）
    返回: (movie_list, total_count, error_msg)
    movie_list: [{'title': '电影名', 'year': '2019', 'rating': '', 'url': '...'}, ...]
    """
    config = load_config()
    cookie = decrypt(config.get('cookie', ''))
    if not cookie:
        return [], 0, '未配置豆瓣Cookie'

    url = f'https://movie.douban.com/people/{user_id}/collect'
    # 注意：不要使用 mode=list，默认 grid 模式才能拿到电影名
    params = {
        'start': start,
        'sort': 'time',
        'tags_sort': 'rec',
        'count': count,
    }

    try:
        resp = requests.get(url, params=params, headers=_get_headers(), timeout=15)
        if resp.status_code == 403:
            return [], 0, '豆瓣Cookie已过期，请重新配置'
        if resp.status_code != 200:
            return [], 0, f'请求失败，状态码: {resp.status_code}'

        html = resp.text

        # 检查是否需要登录
        if '登录' in html and '异常请求' in html:
            return [], 0, '豆瓣Cookie无效或已过期'

        # 解析电影列表
        movies = []
        seen = set()

        # 从每个 item 中提取: URL从 nbg 标签，简体名从 <em> 标签
        # <em>简体名 / 繁体名 / 英文名</em> 取第一个 / 前的部分
        items = re.findall(
            r'<a\s+title="[^"]*"\s+href="(https://movie\.douban\.com/subject/\d+/)"\s+class="nbg">.*?<em>([^<]+)</em>',
            html, re.DOTALL
        )
        for movie_url, em_text in items:
            movie_url = movie_url.strip()
            # <em> 内容格式: "简体名 / 繁体名 / 英文名" 或 "简体名"
            # 取第一个 / 前的部分作为简体中文名
            title = html_module.unescape(em_text.strip().split(' / ')[0].strip())
            if not title or title in seen:
                continue
            seen.add(title)
            movies.append({'title': title, 'url': movie_url, 'year': '', 'rating': ''})

        # 备选: 如果 <em> 解析失败，用 <a title="..." class="nbg"> 的 title
        if not movies:
            for m in re.finditer(
                r'<a\s+title="([^"]+)"\s+href="(https://movie\.douban\.com/subject/\d+/)"\s+class="nbg"',
                html
            ):
                title = html_module.unescape(m.group(1).strip())
                movie_url = m.group(2).strip()
                if title in seen:
                    continue
                seen.add(title)
                movies.append({'title': title, 'url': movie_url, 'year': '', 'rating': ''})
        # 从 <li class="intro"> 补充年份信息
        # 每个 item 的结构: <div class="item comment-item">...<li class="intro">日期 / 演员 / ...</li>...</div>
        # 注意：intros 和 movies 的数量可能不一致，仅按 movies 的索引安全补充
        intros = re.findall(r'<li class="intro">([^<]+)</li>', html)
        for i, intro in enumerate(intros):
            if i < len(movies):
                year_m = re.search(r'(\d{4})-\d{2}-\d{2}', intro)
                if year_m:
                    movies[i]['year'] = year_m.group(1)

        # 获取总数: <h1>我看过的影视(1192)</h1>
        total_match = re.search(r'<h1>[^<]*[\(（](\d+)[\)）]</h1>', html)
        total = int(total_match.group(1)) if total_match else len(movies)

        return movies, total, None

    except requests.Timeout:
        return [], 0, '请求超时'
    except Exception as e:
        return [], 0, f'获取失败: {str(e)}'


def fetch_movie_chinese_name(subject_url):
    """访问电影subject页面获取中文名
    返回: (chinese_name, error_msg)
    """
    # SSRF 防护：校验 URL 必须是豆瓣电影 subject 页面
    if not re.match(r'^https://movie\.douban\.com/subject/\d+/?', subject_url or ''):
        return '', 'URL格式不合法，仅支持豆瓣电影页面'

    config = load_config()
    cookie = decrypt(config.get('cookie', ''))
    if not cookie:
        return '', '未配置豆瓣Cookie'

    try:
        resp = requests.get(subject_url, headers=_get_headers(), timeout=15)
        if resp.status_code != 200:
            return '', f'请求失败，状态码: {resp.status_code}'

        html = resp.text
        # <title>挽救计划 (豆瓣)</title>
        title_m = re.search(r'<title>\s*([^<]+?)\s*\(豆瓣\)\s*</title>', html)
        if title_m:
            name = html_module.unescape(title_m.group(1).strip())
            return name, None

        # 备选: og:title
        og_m = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html)
        if og_m:
            name = html_module.unescape(og_m.group(1).strip())
            return name, None

        return '', '未找到电影名'
    except Exception as e:
        return '', f'获取失败: {str(e)}'


def fetch_all_watched_movies(user_id, max_pages=200):
    """获取用户所有看过的电影

    max_pages: 最大分页数，防止因解析异常导致无限循环
    """
    config = load_config()
    cookie = decrypt(config.get('cookie', ''))
    if not cookie:
        return [], '未配置豆瓣Cookie'

    all_movies = []
    start = 0
    per_page = 15
    total = None
    pages = 0

    while pages < max_pages:
        movies, count, err = fetch_watched_movies(user_id, start, per_page)
        if err:
            if all_movies:
                break
            return [], err

        if total is None:
            total = count

        all_movies.extend(movies)
        pages += 1

        # 终止条件：已获取达到total、本页为空、或本页不足一页
        if len(all_movies) >= total or len(movies) < per_page:
            break

        # 额外保护：如果本页没有新电影（去重后），也终止
        if not movies:
            break

        start += per_page
        time.sleep(0.5)  # 避免请求过快

    return all_movies, None


def check_cookie(user_id):
    """检查豆瓣Cookie是否有效"""
    config = load_config()
    cookie = decrypt(config.get('cookie', ''))
    if not cookie:
        return False, '未配置豆瓣Cookie'

    movies, total, err = fetch_watched_movies(user_id, 0, 15)
    if err:
        return False, err
    return True, f'Cookie有效，本页获取到{len(movies)}部电影，页面报告共{total}部'


def fetch_all_watched_movies_slow(user_id, max_pages=200, page_delay=2.0):
    """获取用户所有看过的电影（慢速版，用于自动同步）

    与 fetch_all_watched_movies 相同，但每页间隔加大到 page_delay 秒，
    避免触发豆瓣限流机制。首次全量拉取时特别重要。

    Args:
        user_id: 豆瓣用户ID
        max_pages: 最大分页数
        page_delay: 每页请求间隔（秒），默认2秒

    Returns:
        (all_movies, error_msg)
    """
    config = load_config()
    cookie = decrypt(config.get('cookie', ''))
    if not cookie:
        return [], '未配置豆瓣Cookie'

    all_movies = []
    start = 0
    per_page = 15
    total = None
    pages = 0

    while pages < max_pages:
        movies, count, err = fetch_watched_movies(user_id, start, per_page)
        if err:
            if all_movies:
                logging.getLogger('douban').warning(f'[豆瓣自动同步] 第{pages+1}页出错但已有{len(all_movies)}条，提前返回: {err}')
                break
            return [], err

        if total is None:
            total = count

        all_movies.extend(movies)
        pages += 1

        if total is not None and len(all_movies) >= total:
            break
        if len(movies) < per_page:
            break
        if not movies:
            break

        start += per_page
        time.sleep(page_delay)  # 慢速间隔，防限流

    return all_movies, None


# ==================== 观影列表缓存（增量同步防限流） ====================

_CACHE_FILE = os.path.join(
    os.environ.get('DATA_DIR', os.path.dirname(os.path.abspath(__file__))),
    'douban_movies_cache.json'
)
_cache_lock = threading.Lock()
# 缓存最长有效期：超过后强制全量刷新一次，纠正增量策略无法感知的偏差
# （如用户在豆瓣移除标记后又新增了同样数量、改标时间导致顺序漂移等）
_CACHE_FULL_REFRESH_DAYS = 7


def _load_movies_cache():
    """读取观影列表缓存"""
    with _cache_lock:
        if os.path.exists(_CACHE_FILE):
            try:
                with open(_CACHE_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logging.getLogger('douban').warning(f'[豆瓣] 缓存文件读取失败: {e}，忽略缓存')
    return {}


def _save_movies_cache(user_id, movies):
    """保存观影列表缓存"""
    with _cache_lock:
        try:
            os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
            with open(_CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump({
                    'user_id': user_id,
                    'total': len(movies),
                    'movies': movies,
                    'fetched_at': time.time(),
                }, f, ensure_ascii=False)
        except (OSError, TypeError) as e:
            logging.getLogger('douban').warning(f'[豆瓣] 缓存文件保存失败: {e}')


def _full_fetch_with_cache(user_id, max_pages, page_delay):
    """全量拉取（慢速防限流）并写入缓存"""
    movies, err = fetch_all_watched_movies_slow(user_id, max_pages=max_pages, page_delay=page_delay)
    if not err and movies:
        _save_movies_cache(user_id, movies)
    return movies, err


def _is_transient_err(err):
    """是否为可回退缓存的临时性错误（Cookie 失效类错误不算，必须上抛）"""
    if not err:
        return False
    return 'Cookie' not in err and 'cookie' not in err


def fetch_all_watched_movies_cached(user_id, max_pages=200, page_delay=2.0):
    """带缓存的观影列表同步（防限流）

    豆瓣"看过"列表按标记时间倒序，新标记的电影总是出现在最前面。
    基于这一特性做增量同步：

    1. 无缓存 / 用户ID变更 / 缓存超过7天      → 全量拉取（写缓存）
    2. 豆瓣总数少于缓存数（移除过标记）        → 全量拉取（写缓存）
    3. 第1页首部电影与缓存一致                → 无新增，直接返回缓存（仅1次请求）
    4. 首部电影在缓存中但位置变了（改标漂移）  → 全量拉取（写缓存）
    5. 第1页有新电影                          → 逐页拉取，遇到整页都已缓存的页
                                                 即停，新电影与缓存余量拼接
                                                 （通常仅2~3次请求）

    Returns:
        (all_movies, error_msg)
    """
    log = logging.getLogger('douban')
    cache = _load_movies_cache()
    cached_movies = cache.get('movies') or []
    cache_usable = bool(cached_movies) and cache.get('user_id') == user_id

    if not cache_usable:
        log.info('[豆瓣] 无可用缓存，执行全量拉取')
        return _full_fetch_with_cache(user_id, max_pages, page_delay)

    cache_age = time.time() - (cache.get('fetched_at') or 0)
    if cache_age > _CACHE_FULL_REFRESH_DAYS * 86400:
        log.info('[豆瓣] 缓存已超过%d天，执行全量刷新校准' % _CACHE_FULL_REFRESH_DAYS)
        return _full_fetch_with_cache(user_id, max_pages, page_delay)

    cached_urls = {m.get('url') for m in cached_movies if m.get('url')}

    # 探测第1页
    first_page, total, err = fetch_watched_movies(user_id, 0, 15)
    if err:
        if _is_transient_err(err):
            # 临时性错误（超时/限流）：回退用缓存，下次同步再校准
            log.warning(f'[豆瓣] 增量探测失败（{err}），本次回退使用缓存（{len(cached_movies)}部）')
            return list(cached_movies), None
        return [], err  # Cookie 失效等错误必须上抛，让用户重新配置

    # 豆瓣总数变少：用户移除过标记，缓存不可信
    if total < len(cached_movies):
        log.info('[豆瓣] 豆瓣总数(%d)少于缓存(%d)，执行全量刷新校准' % (total, len(cached_movies)))
        return _full_fetch_with_cache(user_id, max_pages, page_delay)

    first_url = first_page[0].get('url') if first_page else None
    cached_first_url = cached_movies[0].get('url') if cached_movies else None

    if first_url and first_url == cached_first_url:
        # 首位未变：无新增电影，缓存即最新
        log.info('[豆瓣] 第1页无变化，命中缓存（%d部，本次仅1次请求）' % len(cached_movies))
        return list(cached_movies), None

    if first_url and first_url in cached_urls:
        # 首位是旧电影但顺序变了（改标时间等），保守起见全量
        log.info('[豆瓣] 列表头部顺序变化，执行全量刷新校准')
        return _full_fetch_with_cache(user_id, max_pages, page_delay)

    # 第1页有新电影：逐页增量拉取，直到整页都已缓存
    new_movies = []
    page_movies = first_page
    start = 0
    pages = 1
    while True:
        page_all_cached = True
        for m in page_movies:
            if m.get('url') and m['url'] not in cached_urls:
                new_movies.append(m)
                page_all_cached = False
        if page_all_cached:
            break  # 整页已缓存，其后内容与缓存一致，拼接缓存即可
        if not new_movies or len(new_movies) >= total or len(page_movies) < 15:
            break  # 已到豆瓣末尾
        start += 15
        time.sleep(page_delay)
        pages += 1
        page_movies, _t, perr = fetch_watched_movies(user_id, start, 15)
        if perr:
            if not _is_transient_err(perr):
                return [], perr
            log.warning(f'[豆瓣] 增量第{pages}页拉取失败（{perr}），已拉取部分与缓存合并')
            break

    result = new_movies + list(cached_movies)
    _save_movies_cache(user_id, result)
    log.info('[豆瓣] 增量同步完成: 新增%d部，复用缓存%d部，实际请求%d页' % (
        len(new_movies), len(cached_movies), pages))
    return result, None

