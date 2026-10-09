"""豆瓣看过的电影同步模块"""
import requests
import re
import json
import os
import time
import threading
import html as html_module
import logging
from typing import Any, Dict, List

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
    # 模拟真实浏览器请求头：豆瓣对请求特征不完整的会话会"少渲染"
    # collect 列表（浏览器 15 条，服务端只返回 13~14 条且不报错），
    # 补全 Accept-Language / Sec-Fetch-* 等字段降低被降级的概率。
    # 不显式设置 Accept-Encoding，交由 requests 处理（避免 br 解码问题）。
    return {
        'User-Agent': USER_AGENT,
        'Cookie': cookie,
        'Referer': 'https://movie.douban.com/',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Cache-Control': 'max-age=0',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'same-origin',
        'Sec-Fetch-User': '?1',
    }


# 豆瓣请求专用会话：直连不走代理。
# 豆瓣是国内站点，容器配置 HTTP_PROXY 时（如 Clash）豆瓣请求会从代理出口发出，
# 共享出口IP易触发豆瓣反爬（只开放collect列表前~14页就返回空页）。
# 与 baidu_forum 的 trust_env=False 策略一致。
_SESSION = requests.Session()
_SESSION.trust_env = False

# 非满页重试次数：豆瓣对部分会话/页码会"少渲染"（末页除外，浏览器 15 条
# 而服务端只给 13~14 条）。非满页时反复重试，并把多次结果按 URL 取并集，
# 把少渲染隐藏的条目捞回来——这是"完全对齐"（零缺口）的关键手段。
PARTIAL_PAGE_RETRY = 6

# 全量拉取时的缺口计数器：记录"页面报告总数 - 实际拉到条数"以及少渲染的页码。
# 由 fetch_all_watched_movies_slow 填写，_full_fetch_with_cache 读取后写入缓存，
# 用于判断缓存是否完整（缺口会被写进缓存并据此拒绝增量命中，避免缺口固化）。
_LAST_FETCH_GAP = {'gap': 0, 'claimed': None, 'fetched': 0, 'short_pages': []}

# 缓存缺口容忍阈值：默认 0 = 完全对齐，任何缺口都视为缓存不完整并强制全量重拉。
# 这是刻意选择的严格模式：宁可多花一次全量拉取的代价，也不接受"看起来同步成功、
# 实际少了几部"。仅在豆瓣确实永久隐藏条目、无法通过并集重试补齐时，才考虑在
# douban_config.json 里用 cache_gap_tolerance 放宽（例如设为 2）。
DEFAULT_GAP_TOLERANCE = 0


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
        resp = _SESSION.get(url, params=params, headers=_get_headers(), timeout=15)
        if resp.status_code == 403:
            return [], 0, '豆瓣Cookie已过期，请重新配置'
        if resp.status_code != 200:
            return [], 0, f'请求失败，状态码: {resp.status_code}'

        html = resp.text

        # 检查是否需要登录
        if '登录' in html and '异常请求' in html:
            return [], 0, '豆瓣Cookie无效或已过期'

        # 解析电影列表：按 item 块切分后逐块提取。
        # 旧版单一正则要求锚点属性顺序严格为 title→href→class，属性顺序不同
        # 的条目整条丢失；且 DOTALL 跨条目匹配可能把解析失败的条目"吞进"下一条。
        # 逐块解析彻底规避这两个问题。
        movies = []
        seen = set()
        no_url_blocks = []
        dup_in_page = 0
        blocks = re.split(r'<div\s+class="item', html)[1:]
        for block in blocks:
            url_m = re.search(r'href="(https://movie\.douban\.com/subject/(\d+)/?)"', block)
            if not url_m:
                # 无条目链接的块：通常是豆瓣已删除条目的占位（计数含它但无链接）
                no_url_blocks.append(re.sub(r'<[^>]+>', ' ', block)[:100].strip())
                continue
            movie_url = url_m.group(1)
            if not movie_url.endswith('/'):
                movie_url += '/'
            if movie_url in seen:
                dup_in_page += 1
                continue
            # 标题提取链：<em>简体名</em> → 锚点 title 属性 → title li 链接文本
            title = ''
            em_m = re.search(r'<em>([^<]+)</em>', block)
            if em_m:
                title = html_module.unescape(em_m.group(1).strip().split(' / ')[0].strip())
            if not title:
                t_m = re.search(r'\stitle="([^"]+)"', block)
                if t_m:
                    title = html_module.unescape(t_m.group(1).strip().split(' / ')[0].strip())
            if not title:
                a_m = re.search(r'class="title"[^>]*>\s*<a[^>]*>([^<]+)</a>', block)
                if a_m:
                    title = html_module.unescape(a_m.group(1).strip())
            if not title:
                title = 'subject_' + url_m.group(2)  # 兜底：subject id 占位
            # 年份：块内 intro li 的观看日期
            year = ''
            intro_m = re.search(r'<li class="intro">([^<]+)</li>', block)
            if intro_m:
                y_m = re.search(r'(\d{4})-\d{2}-\d{2}', intro_m.group(1))
                if y_m:
                    year = y_m.group(1)
            seen.add(movie_url)
            movies.append({'title': title, 'url': movie_url, 'year': year, 'rating': ''})

        # 诊断日志：块数与解析数的差值暴露丢条目原因
        log = logging.getLogger('douban')
        if no_url_blocks:
            log.warning(f'[豆瓣] 本页{len(blocks)}块中有{len(no_url_blocks)}块无条目链接（疑似豆瓣已删除条目占位），'
                        f'内容片段: {no_url_blocks[:3]}')
        if dup_in_page:
            log.warning(f'[豆瓣] 本页有{dup_in_page}个页内重复URL条目被去重')

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
        resp = _SESSION.get(subject_url, headers=_get_headers(), timeout=15)
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


def fetch_movie_meta(subject_url):
    """访问电影subject页面，提取中文名/上映年份/IMDb编号

    用于 Jellyfin 入库精确匹配：IMDb 编号可与 Jellyfin 条目的 ProviderIds.Imdb
    直接比对，无需靠片名搜 TMDB（避免系列片/同名片歧义）。

    返回: ({'title', 'year', 'imdb_id'}, error_msg)
    """
    if not re.match(r'^https://movie\.douban\.com/subject/\d+/?', subject_url or ''):
        return {}, 'URL格式不合法，仅支持豆瓣电影页面'
    config = load_config()
    cookie = decrypt(config.get('cookie', ''))
    if not cookie:
        return {}, '未配置豆瓣Cookie'
    try:
        resp = _SESSION.get(subject_url, headers=_get_headers(), timeout=15)
        if resp.status_code != 200:
            return {}, f'请求失败，状态码: {resp.status_code}'
        html = resp.text
        meta = {'title': '', 'year': '', 'imdb_id': ''}
        # 片名：<title>X (豆瓣)</title> → og:title 兜底
        t = re.search(r'<title>\s*([^<]+?)\s*\(豆瓣\)\s*</title>', html)
        if t:
            meta['title'] = html_module.unescape(t.group(1).strip())
        else:
            og = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html)
            if og:
                meta['title'] = html_module.unescape(og.group(1).strip())
        # 上映年份：标题旁 <span class="year">(2003)</span>
        y = re.search(r'class="year">\s*\((\d{4})\)', html)
        if y:
            meta['year'] = y.group(1)
        # IMDb编号：页面信息块里的 ttXXXXXXX（兼容"IMDb:"/"IMDb编号:"等写法）
        imdb = re.search(r'(tt\d{7,9})', html)
        if imdb:
            meta['imdb_id'] = imdb.group(1)
        return meta, None
    except Exception as e:
        return {}, f'获取失败: {str(e)}'


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
    """检查豆瓣Cookie是否有效（含深度分页探测）

    仅查第1页是不够的：豆瓣对游客/可疑流量也正常返回第1页。
    增加深度分页探测（第16页起）：豆瓣对反爬限流的会话只开放
    collect列表前~14页(约208部)就返回空页，此时全量同步必然不完整。
    """
    config = load_config()
    cookie = decrypt(config.get('cookie', ''))
    if not cookie:
        return False, '未配置豆瓣Cookie'

    movies, total, err = fetch_watched_movies(user_id, 0, 15)
    if err:
        return False, err

    # 总数超过深度探测阈值时，检查第16页能否访问
    if total and total > 225:
        deep, _dt, deep_err = fetch_watched_movies(user_id, 225, 15)
        if not deep_err and not deep:
            return True, (f'Cookie有效（第1页正常，页面报告共{total}部），'
                          f'但豆瓣对当前网络只开放前~14页，深度分页返回空。'
                          f'全量同步会不完整并中止。'
                          f'可能原因：容器配置了HTTP_PROXY导致豆瓣请求走代理出口'
                          f'（新版已改为直连，请更新镜像），或当前IP被豆瓣限流')
        if deep_err:
            return True, f'Cookie有效，本页获取到{len(movies)}部电影，页面报告共{total}部；但深度分页探测失败: {deep_err}'
        return True, f'Cookie有效，本页获取到{len(movies)}部电影，页面报告共{total}部，深度分页正常（第16页可访问）'

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
    log = logging.getLogger('douban')
    incomplete_reason = None
    empty_retried = False  # 空页只重试一次
    short_pages = []       # 非满页页码（少渲染诊断，写入缓存用于缺口判断）

    while pages < max_pages:
        movies, count, err = fetch_watched_movies(user_id, start, per_page)
        if err:
            if all_movies:
                # 拉取中断：返回部分数据但必须标记不完整，
                # 否则上层会把残缺列表当完整数据写入缓存并误删本地电影
                incomplete_reason = f'第{pages+1}页出错: {err}'
                log.warning(f'[豆瓣] 全量拉取中断: 已获取{len(all_movies)}部, {incomplete_reason}')
                break
            return [], err

        if total is None:
            total = count

        # 逐页诊断：非满页记录（末页除外都值得注意，用于定位丢条目的页）
        if len(movies) != per_page:
            log.info(f'[豆瓣] 第{pages+1}页(start={start})返回{len(movies)}部（非满页{per_page}）')

        # 空页：未拉满总数时可能是豆瓣偶发抽风，重试一次当前页；
        # 重试仍空（或已拉满）则视为到达末尾
        if not movies:
            if not empty_retried and total is not None and len(all_movies) < total:
                empty_retried = True
                log.warning(f'[豆瓣] 第{pages+1}页(start={start})为空但未拉满({len(all_movies)}/{total})，重试一次')
                time.sleep(page_delay * 2)
                continue  # 不推进start，重试当前页
            break

        # 非满页重试：豆瓣对部分页会"少渲染"（浏览器 15 条、服务端仅 13~14 条
        # 且不报错），非末页出现非满页时反复重试，并把多次结果**按 URL 取并集**
        # ——只保留"条数最多的那一次"会丢掉每次尝试各自渲染出的条目；
        # 取并集才能把少渲染隐藏的条目真正捞回来（完全对齐的关键）。
        # 末页天然不满15条（start + per_page >= total），不重试。
        if 0 < len(movies) < per_page and total is not None and start + per_page < total:
            merged = {m['url']: m for m in movies if m.get('url')}
            for attempt in range(1, PARTIAL_PAGE_RETRY + 1):
                log.warning(f'[豆瓣] 第{pages+1}页(start={start})非满页({len(merged)}/{per_page})，'
                            f'重试第{attempt}次（并集合并）')
                time.sleep(page_delay * 2)
                retry_movies, _rc, retry_err = fetch_watched_movies(user_id, start, per_page)
                if retry_err:
                    log.warning(f'[豆瓣] 第{pages+1}页重试失败: {retry_err}')
                    break
                before = len(merged)
                for m in retry_movies:
                    if m.get('url') and m['url'] not in merged:
                        merged[m['url']] = m
                if len(merged) > before:
                    log.info(f'[豆瓣] 第{pages+1}页重试后并集新增{len(merged) - before}部，'
                             f'累计{len(merged)}部')
                if len(merged) >= per_page:
                    break
            if len(merged) != len(movies):
                # 按观看时间倒序重排（豆瓣 sort=time 的语义）；时间相同按 URL 稳定排序
                movies = sorted(merged.values(),
                                key=lambda m: (m.get('date') or '', m.get('url') or ''),
                                reverse=True)
                log.info(f'[豆瓣] 第{pages+1}页并集合并结果: {len(movies)}/{per_page}部')
            if 0 < len(movies) < per_page:
                short_pages.append({'page': pages + 1, 'start': start, 'got': len(movies)})

        empty_retried = False
        all_movies.extend(movies)
        pages += 1

        if total is not None and len(all_movies) >= total:
            break
        # 注意：不能因"本页不足15部"提前结束——豆瓣页内同名条目/解析
        # 差异会导致中间页不足额，提前结束会截断列表（208部事故根因）。
        # 真正的末页由"下一页为空"判定，多一次请求无妨

        start += per_page
        time.sleep(page_delay)  # 慢速间隔，防限流

    if incomplete_reason:
        return all_movies, f'拉取不完整(已获取{len(all_movies)}部): {incomplete_reason}'

    # 全局 URL 去重：并集重试或豆瓣少渲染会让同一条目跨页重复出现，
    # 不去重会让条数虚高、掩盖真实缺口（"完全对齐"必须按 URL 计数）。
    deduped: List[Dict[str, Any]] = []
    seen_urls: set = set()
    dup_across_pages = 0
    for m in all_movies:
        u = m.get('url')
        if u and u in seen_urls:
            dup_across_pages += 1
            continue
        if u:
            seen_urls.add(u)
        deduped.append(m)
    if dup_across_pages:
        log.warning(f'[豆瓣] 全局去重移除{dup_across_pages}条跨页重复URL条目'
                    f'（{len(all_movies)} → {len(deduped)}）')
    all_movies = deduped

    gap = 0
    if total is not None:
        gap = max(0, total - len(all_movies))
    # 记录本次拉取结果，供 _full_fetch_with_cache 写入缓存、供增量命中判断
    _LAST_FETCH_GAP.update({
        'gap': gap,
        'claimed': total,
        'fetched': len(all_movies),
        'short_pages': short_pages,
    })

    # 完全对齐：任何缺口都不再当作"正常"放行，必须显式上报为不完整。
    # 调用方（_full_fetch_with_cache）据此拒绝写缓存，上层 _do_douban_auto_sync
    # 收到 err 后会中止本次重建、保留原有数据，不会用残缺列表覆盖数据库。
    if gap > 0:
        short_desc = '、'.join(str(p['page']) for p in short_pages[:10]) or '无'
        return all_movies, (
            f'拉取不完整: 已获取{len(all_movies)}部，豆瓣报告共{total}部，缺口{gap}部'
            f'（少渲染页码: {short_desc}）。并集重试后仍未补齐，本次同步已中止'
            f'（未做任何修改）。请稍后重试；若持续出现，可能是豆瓣Cookie降级'
            f'（游客只能访问前~14页）或当前IP被限流')
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
# 缓存结构版本：增量策略/缓存格式变更时递增，旧版本缓存自动失效并全量刷新。
# v2: 修复拉取中断时把残缺列表写入缓存的bug（曾导致208部缓存覆盖1242部真实列表）
# v3: v2仍可能包含截断数据——豆瓣第14页不满15部时提前break绕过了旧版完整性
#     校验(pages>=max_pages)，208部被当完整结果写入v2缓存。缓存命中路径
#     新增总数校验，且v2缓存一律作废重拉
# v4: 旧解析正则要求锚点属性顺序固定，8条属性顺序不同的条目整条丢失
#     （1198只解析出1190且写入v3缓存）。解析改为按item块切分后，v3缓存作废重拉
# v5: 新解析器全量拉取仍为1190/1198，加入逐页诊断日志定位8部缺口；
#     v4缓存（1190条）作废，部署后带诊断全量重拉
# v6: 缺口根因确认——豆瓣对登录态不完整的会话静默隐藏部分条目（总数
#     照常显示但列表少渲染，不报错）。换完整Cookie后所有页面满15条。
#     v5缓存（1190条，Cookie降级期间拉取）作废，更新Cookie后全量重拉
# v7: 中间页"少渲染"缺口确认（第16/23/24/37/42/79页各少1~2条，共8条）。
#     根因为豆瓣按请求特征降级：浏览器同页满15条、服务端仅13~14条。
#     已补全浏览器请求头 + 非末页非满页自动重试；v6缓存（少渲染期间
#     的1210/1211条）作废，部署后全量重拉校验
# v8: 缺口固化根因修复，并改为"完全对齐"（零缺口）语义。
#     v7 把"缺口 ≤ max(5, 5%)"判为正常并静默写入缓存，而增量命中路径的阈值
#     同样是 max(5, 5%)，两者叠加导致一份缺 8 部的缓存（1215/1223）被永久复用：
#     既不会触发全量刷新，7天有效期前也不会自愈。实测确认豆瓣能完整返回 1223 条。
#     现在：
#       1) 非满页重试 4 次并按 URL 取并集（不再只留"最长的一次"），把少渲染
#          隐藏的条目真正捞回来；
#       2) 全局按 URL 去重，避免跨页重复让条数虚高、掩盖真实缺口；
#       3) 任何缺口都不再放行——不写缓存、同步中止、如实报错（容忍值默认 0）；
#       4) 缺口与少渲染页码写入缓存，命中前先验完整性，缺口即强制全量重拉。
#     v7 缓存（1215条）作废，部署后全量重拉补齐。
_CACHE_VERSION = 8

# 缓存缺口容忍值：默认 0 = 完全对齐（可由 douban_config.json 的
# cache_gap_tolerance 覆盖，仅用于豆瓣确实永久隐藏条目、无法补齐的例外情况）。
# 缺口 > 该值 → 缓存视为不完整，强制全量刷新；缺口 ≤ 该值 → 仍走增量，
# 但缺口会写进同步结果，避免"看起来同步成功、实际少了几部"。
_CACHE_GAP_TOLERANCE = DEFAULT_GAP_TOLERANCE


def _override_gap_tolerance():
    """启动时用配置覆盖缺口容忍值（配置非法时保留默认）"""
    global _CACHE_GAP_TOLERANCE
    try:
        raw = load_config().get('cache_gap_tolerance')
        if raw is not None and str(raw).strip() != '':
            _CACHE_GAP_TOLERANCE = max(0, int(raw))
            logging.getLogger('douban').info(
                f'[豆瓣] 缺口容忍值由配置覆盖为 {_CACHE_GAP_TOLERANCE}')
    except (TypeError, ValueError):
        pass


_override_gap_tolerance()


def _load_movies_cache():
    """读取观影列表缓存（版本不匹配视为无缓存）"""
    with _cache_lock:
        if os.path.exists(_CACHE_FILE):
            try:
                with open(_CACHE_FILE, 'r', encoding='utf-8') as f:
                    cache = json.load(f)
                if cache.get('version') != _CACHE_VERSION:
                    logging.getLogger('douban').info(
                        f'[豆瓣] 缓存版本不匹配(v{cache.get("version")})，将全量刷新')
                    return {}
                return cache
            except (json.JSONDecodeError, IOError) as e:
                logging.getLogger('douban').warning(f'[豆瓣] 缓存文件读取失败: {e}，忽略缓存')
    return {}


def _save_movies_cache(user_id, movies, claimed=None, gap=0, short_pages=None):
    """保存观影列表缓存

    claimed/gap/short_pages 为本次全量拉取的完整性标记：命中路径据此判断
    缓存是否完整，避免把"缺几部"的列表当作最新数据永久复用（v7 及之前的问题）。
    """
    with _cache_lock:
        try:
            os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
            with open(_CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump({
                    'version': _CACHE_VERSION,
                    'user_id': user_id,
                    'total': len(movies),
                    'claimed_total': claimed,
                    'gap': int(gap or 0),
                    'short_pages': short_pages or [],
                    'movies': movies,
                    'fetched_at': time.time(),
                }, f, ensure_ascii=False)
        except (OSError, TypeError) as e:
            logging.getLogger('douban').warning(f'[豆瓣] 缓存文件保存失败: {e}')


def _gap_tolerance():
    """读取缺口容忍值（配置可覆盖，非法值回退默认）"""
    try:
        raw = load_config().get('cache_gap_tolerance')
        if raw is None or str(raw).strip() == '':
            return _CACHE_GAP_TOLERANCE
        return max(0, int(raw))
    except (TypeError, ValueError):
        return _CACHE_GAP_TOLERANCE


def _is_cache_complete(cache):
    """缓存是否完整（缺口在容忍范围内）

    返回 (是否完整, 缓存缺口, 缓存记录的豆瓣总数)。没有完整性标记的旧缓存
    视为完整（交由版本号作废机制处理）。
    """
    gap = int(cache.get('gap') or 0)
    claimed = cache.get('claimed_total')
    try:
        claimed = int(claimed) if claimed is not None else None
    except (TypeError, ValueError):
        claimed = None
    return gap <= _gap_tolerance(), gap, claimed


def _format_gap_note(fetched, claimed, gap):
    """统一格式化的缺口说明，拼进同步结果里，让状态栏如实反映数量差"""
    if not claimed or gap <= 0:
        return ''
    pages = ''
    short_pages = _LAST_FETCH_GAP.get('short_pages') or []
    if short_pages:
        pages = '（少渲染页码: ' + '、'.join(str(p['page']) for p in short_pages[:8]) + '）'
    return f'；注意：豆瓣报告{claimed}部，本次实际{fetched}部，差{gap}部{pages}'


def _full_fetch_with_cache(user_id, max_pages, page_delay):
    """全量拉取（慢速防限流）并写入缓存

    只有零缺口（完全对齐）的结果才会写入缓存；带缺口的结果会被 fetch 层直接
    判为错误（err 非空），这里不会再写缓存，上层同步也会中止、保留原数据。
    """
    # 拉取前清零，避免上一轮/上一模块的残留影响本次判断
    _LAST_FETCH_GAP.update({'gap': 0, 'claimed': None, 'fetched': 0, 'short_pages': []})
    movies, err = fetch_all_watched_movies_slow(user_id, max_pages=max_pages, page_delay=page_delay)
    if not err and movies:
        _save_movies_cache(
            user_id, movies,
            claimed=_LAST_FETCH_GAP.get('claimed'),
            gap=_LAST_FETCH_GAP.get('gap') or 0,
            short_pages=_LAST_FETCH_GAP.get('short_pages') or [],
        )
        gap = _LAST_FETCH_GAP.get('gap') or 0
        if gap > 0:
            logging.getLogger('douban').warning(
                f'[豆瓣] 全量拉取存在缺口{gap}部，已写入缓存完整性标记；'
                f'下次同步将强制全量重拉校正')
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

    # 缓存完整性前置判断：上次全量拉取就带缺口（如 1215/1223）时，
    # 即便首位未变也不能当"无变化"复用——否则缺口会被永久固化
    # （v7 的 max(5, 5%) 容差阈值过大，8 部缺口够不着，缓存永远不会自愈）。
    if cache_usable:
        complete, cached_gap, cached_claimed = _is_cache_complete(cache)
        if not complete:
            log.warning(f'[豆瓣] 缓存完整性不足（记录{len(cached_movies)}部'
                        f'{"，豆瓣报告" + str(cached_claimed) + "部" if cached_claimed else ""}'
                        f'，缺口{cached_gap}部 > 容忍值{_gap_tolerance()}），执行全量刷新校正')
            return _full_fetch_with_cache(user_id, max_pages, page_delay)

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
        # 首位未变但豆瓣总数多于缓存数：缓存缺失中后段内容
        # （如历史上被豆瓣截断写入的208部缓存），不能当"无变化"用。
        # 容差取缺口容忍值而非 5%：豆瓣同名电影按URL去重后可能天然少 1~2 部，
        # 但差到 3 部以上就不该再复用缓存（v7 用 5% 导致 8 部缺口被永久固化）。
        tol = _gap_tolerance()
        if total and total > len(cached_movies) + tol:
            log.info('[豆瓣] 首位未变但豆瓣总数(%d)多于缓存(%d，差%d > 容忍值%d)，'
                     '缓存不完整，执行全量刷新'
                     % (total, len(cached_movies), total - len(cached_movies), tol))
            return _full_fetch_with_cache(user_id, max_pages, page_delay)
        # 首位未变：无新增电影，缓存即最新（缺口已在前置判断里拦截）
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
    # 完全对齐：增量拼接后只要与豆瓣报告总数不符，就一律全量刷新兜底；
    # 全量刷新后若仍有缺口，_full_fetch_with_cache 不会写缓存、上层会中止同步，
    # 绝不会把残缺列表写进数据库。
    tol = _gap_tolerance()
    if total and len(result) < total - tol:
        log.info('[豆瓣] 增量拼接结果(%d部)少于豆瓣总数(%d，差%d > 容忍值%d)，'
                 '缓存不完整，执行全量刷新'
                 % (len(result), total, total - len(result), tol))
        return _full_fetch_with_cache(user_id, max_pages, page_delay)
    _save_movies_cache(user_id, result, claimed=total, gap=max(0, (total or 0) - len(result)))
    log.info('[豆瓣] 增量同步完成: 新增%d部，复用缓存%d部，实际请求%d页' % (
        len(new_movies), len(cached_movies), pages))
    return result, None

