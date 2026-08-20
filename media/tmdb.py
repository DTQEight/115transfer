"""TMDB API封装"""
import requests
import os
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed


TMDB_BASE_URL = 'https://api.themoviedb.org/3'

# cloud115_config.json 由 cloud115.py 统一管理（含读写锁），tmdb 模块复用同一文件
import cloud115
CONFIG_FILE = cloud115.CONFIG_FILE
_load_config = cloud115.load_config

# 全局Session复用TCP连接 + 内存缓存
_SESSION = requests.Session()
_SESSION.trust_env = True  # 允许使用环境变量中的代理设置（国内访问TMDB通常需要代理）
_CACHE_LOCK = threading.Lock()
_SEARCH_CACHE = {}  # (query, year) -> results
_DETAIL_CACHE = {}  # (media_type, tmdb_id) -> detail
_IDENTIFY_CACHE = {}  # (name, year) -> result


def get_tmdb_api_key():
    config = _load_config()
    return config.get('tmdb_api_key', '')


def set_tmdb_api_key(api_key):
    def _update(cfg):
        cfg['tmdb_api_key'] = api_key
    cloud115.update_config(_update)
    # 清除所有缓存，避免旧的错误结果（如"未配置API Key"）被重复返回
    with _CACHE_LOCK:
        _SEARCH_CACHE.clear()
        _DETAIL_CACHE.clear()
        _IDENTIFY_CACHE.clear()


def search_multi(query, year=None, language='zh-CN'):
    """搜索电影/电视剧（多类型搜索），可选年份筛选"""
    api_key = get_tmdb_api_key()
    if not api_key:
        return None, '未配置TMDB API Key'

    cache_key = (query, year, language)
    with _CACHE_LOCK:
        if cache_key in _SEARCH_CACHE:
            return _SEARCH_CACHE[cache_key], None

    try:
        all_results = []
        # 通用搜索
        url = f'{TMDB_BASE_URL}/search/multi'
        params = {
            'api_key': api_key,
            'query': query,
            'language': language,
            'page': 1,
        }
        resp = _SESSION.get(url, params=params, timeout=15)
        data = resp.json()
        if resp.status_code == 200:
            for r in data.get('results', []):
                if r.get('media_type') in ('movie', 'tv'):
                    all_results.append(r)
        else:
            tmdb_err = data.get('status_message') or data.get('errors') or f'HTTP {resp.status_code}'
            return None, f'TMDB API错误: {tmdb_err}'

        # 如果有年份，额外用年份精确搜索
        if year and str(year).isdigit():
            for endpoint, year_key in [('movie', 'primary_release_year'), ('tv', 'first_air_date_year')]:
                try:
                    url = f'{TMDB_BASE_URL}/search/{endpoint}'
                    params = {
                        'api_key': api_key,
                        'query': query,
                        'language': language,
                        year_key: int(year),
                        'page': 1,
                    }
                    resp = _SESSION.get(url, params=params, timeout=15)
                    if resp.status_code == 200:
                        for r in resp.json().get('results', []):
                            r['media_type'] = endpoint
                            if r.get('id') not in [x.get('id') for x in all_results]:
                                all_results.append(r)
                except Exception:
                    pass

        # 去重并按年份排序（有年份的优先匹配）
        if year and str(year).isdigit():
            def year_score(r):
                r_year = (r.get('release_date') or r.get('first_air_date') or '')[:4]
                return 0 if r_year == str(year) else 1
            all_results.sort(key=year_score)

        with _CACHE_LOCK:
            _SEARCH_CACHE[cache_key] = all_results
        return all_results, None
    except Exception as e:
        return None, f'搜索失败: {str(e)}'


def get_movie_detail(movie_id, language='zh-CN'):
    """获取电影详情"""
    api_key = get_tmdb_api_key()
    if not api_key:
        return None, '未配置TMDB API Key'

    cache_key = ('movie', movie_id, language)
    with _CACHE_LOCK:
        if cache_key in _DETAIL_CACHE:
            return _DETAIL_CACHE[cache_key], None

    try:
        url = f'{TMDB_BASE_URL}/movie/{movie_id}'
        params = {'api_key': api_key, 'language': language}
        resp = _SESSION.get(url, params=params, timeout=15)
        if resp.status_code != 200:
            try:
                err_data = resp.json()
                tmdb_err = err_data.get('status_message') or f'HTTP {resp.status_code}'
            except Exception:
                tmdb_err = f'HTTP {resp.status_code}'
            return None, f'获取详情失败: {tmdb_err}'
        data = resp.json()
        with _CACHE_LOCK:
            _DETAIL_CACHE[cache_key] = data
        return data, None
    except Exception as e:
        return None, f'获取详情失败: {str(e)}'


def get_tv_detail(tv_id, language='zh-CN'):
    """获取电视剧详情"""
    api_key = get_tmdb_api_key()
    if not api_key:
        return None, '未配置TMDB API Key'

    cache_key = ('tv', tv_id, language)
    with _CACHE_LOCK:
        if cache_key in _DETAIL_CACHE:
            return _DETAIL_CACHE[cache_key], None

    try:
        url = f'{TMDB_BASE_URL}/tv/{tv_id}'
        params = {'api_key': api_key, 'language': language}
        resp = _SESSION.get(url, params=params, timeout=15)
        if resp.status_code != 200:
            try:
                err_data = resp.json()
                tmdb_err = err_data.get('status_message') or f'HTTP {resp.status_code}'
            except Exception:
                tmdb_err = f'HTTP {resp.status_code}'
            return None, f'获取详情失败: {tmdb_err}'
        data = resp.json()
        with _CACHE_LOCK:
            _DETAIL_CACHE[cache_key] = data
        return data, None
    except Exception as e:
        return None, f'获取详情失败: {str(e)}'


def get_media_by_id(tmdb_id, language='zh-CN'):
    """通过TMDB ID直接获取电影或电视剧详情"""
    api_key = get_tmdb_api_key()
    if not api_key:
        return None, '未配置TMDB API Key'

    # 先尝试电影
    detail, err = get_movie_detail(tmdb_id, language)
    if detail:
        return _format_movie_result(detail, 'movie'), None

    # 再尝试电视剧
    detail, err = get_tv_detail(tmdb_id, language)
    if detail:
        return _format_tv_result(detail), None

    return None, f'TMDB ID {tmdb_id} 未找到'


def _format_movie_result(detail, media_type='movie'):
    """格式化电影结果"""
    return {
        'tmdb_id': detail.get('id'),
        'media_type': media_type,
        'title': detail.get('title') or detail.get('name', ''),
        'original_title': detail.get('original_title') or detail.get('original_name', ''),
        'year': (detail.get('release_date') or detail.get('first_air_date') or '')[:4],
        'genres': [g.get('name', '') for g in detail.get('genres', [])],
        'genre_ids': [g.get('id') for g in detail.get('genres', [])],
        'original_language': detail.get('original_language', ''),
        'production_countries': [c.get('iso_3166_1', '') for c in detail.get('production_countries', [])],
        'overview': detail.get('overview', ''),
        'poster_path': detail.get('poster_path', ''),
        'vote_average': detail.get('vote_average', 0),
    }


def _format_tv_result(detail):
    """格式化电视剧结果"""
    return {
        'tmdb_id': detail.get('id'),
        'media_type': 'tv',
        'title': detail.get('name') or detail.get('title', ''),
        'original_title': detail.get('original_name') or detail.get('original_title', ''),
        'year': (detail.get('first_air_date') or detail.get('release_date') or '')[:4],
        'genres': [g.get('name', '') for g in detail.get('genres', [])],
        'genre_ids': [g.get('id') for g in detail.get('genres', [])],
        'original_language': detail.get('original_language', ''),
        'production_countries': list(detail.get('origin_country', [])),
        'overview': detail.get('overview', ''),
        'poster_path': detail.get('poster_path', ''),
        'vote_average': detail.get('vote_average', 0),
    }


_FIND_CACHE = {}  # imdb_id -> result
_FIND_CACHE_LOCK = threading.Lock()


def find_by_imdb_id(imdb_id, language='zh-CN'):
    """通过 IMDb ID 精确查找 TMDB 条目（零歧义，同名电影首选）

    使用 TMDB /find/{imdb_id}?external_source=imdb_id
    返回格式同 identify_media：{tmdb_id, poster_path, year, vote_average, overview}
    """
    if not imdb_id:
        return None, '缺少 IMDb 编号'
    api_key = get_tmdb_api_key()
    if not api_key:
        return None, '未配置TMDB API Key'

    cache_key = (imdb_id, language)
    with _FIND_CACHE_LOCK:
        if cache_key in _FIND_CACHE:
            cached = _FIND_CACHE[cache_key]
            return cached

    try:
        resp = _SESSION.get(
            f'{TMDB_BASE_URL}/find/{imdb_id}',
            params={'api_key': api_key, 'external_source': 'imdb_id', 'language': language},
            timeout=15,
        )
        data = resp.json()
    except Exception as e:
        return None, f'请求失败: {e}'

    # 优先电影结果，其次剧集
    candidates = (data.get('movie_results') or []) + (data.get('tv_results') or [])
    if not candidates:
        with _FIND_CACHE_LOCK:
            _FIND_CACHE[cache_key] = (None, '未找到匹配结果')
        return None, '未找到匹配结果'

    c = candidates[0]
    tmdb_id = c.get('id')
    mt = 'movie' if c in data.get('movie_results', []) else 'tv'

    # 拿一次详情补 overview / vote_average / release_date 等
    if tmdb_id:
        detail, err = get_media_by_id(tmdb_id, language=language)
        if detail:
            result = {
                'tmdb_id': detail.get('tmdb_id') or tmdb_id,
                'media_type': detail.get('media_type') or mt,
                'poster_path': detail.get('poster_path') or c.get('poster_path'),
                'name': detail.get('name') or c.get('title') or c.get('name'),
                'year': detail.get('year'),
                'vote_average': detail.get('vote_average', 0),
                'overview': detail.get('overview', ''),
            }
            with _FIND_CACHE_LOCK:
                _FIND_CACHE[cache_key] = (result, None)
            return result, None

    # 没拿到详情也返回 find 结果里的基本信息
    result = {
        'tmdb_id': tmdb_id,
        'media_type': mt,
        'poster_path': c.get('poster_path'),
        'name': c.get('title') or c.get('name'),
        'year': str(c.get('release_date') or c.get('first_air_date') or '')[:4] or None,
        'vote_average': c.get('vote_average', 0),
        'overview': c.get('overview', ''),
    }
    with _FIND_CACHE_LOCK:
        _FIND_CACHE[cache_key] = (result, None)
    return result, None


def identify_media_candidates(name, year=None, limit=10):
    """手动识别：按电影名+年份返回多个候选（不走自动 best 决策），供用户点选。

    Returns (candidates, err):
        candidates: list[{
            'tmdb_id': int, 'media_type': 'movie'|'tv',
            'title': str, 'original_title': str,
            'year': 'YYYY',
            'overview': str,
            'poster_path': str (相对URL, /xxx.jpg 或空),
            'poster_url': str (TMDB w300完整URL，方便前端直接用),
            'vote_average': float,
            'name_zh': str, 'name_en': str,
        }, ...]  最多 limit 条；失败时空列表 + err 文本
    """
    if not name or not name.strip():
        return [], '搜索关键词为空'
    if not get_tmdb_api_key():
        return [], '未配置TMDB API Key'

    name = name.strip()
    # 收集多策略的搜索结果（按顺序去重），不做 best 决策
    queries = [(name, year)]
    # 括号去掉
    cleaned = name.split('（')[0].split('(')[0].strip()
    if cleaned and cleaned != name:
        queries.append((cleaned, year))
    # 中文部分
    cn_match = re.search(r'([\u4e00-\u9fff]+)', name)
    if cn_match:
        cn = cn_match.group(1)
        if len(cn) >= 2 and cn != name and (cn, year) not in queries:
            queries.append((cn, year))
    # 英文部分
    en_match = re.search(r'([A-Za-z][A-Za-z\s\.]+)', name)
    if en_match:
        en = en_match.group(1).strip().replace('.', ' ')
        if len(en) >= 3 and en != name and (en, year) not in queries:
            queries.append((en, None))

    seen_ids = set()
    merged = []
    last_err = None
    for q_name, q_year in queries:
        try:
            results, err = search_multi(q_name, year=q_year)
        except Exception as e:
            last_err = f'search失败: {e}'
            continue
        if err:
            last_err = err
            continue
        if not results:
            continue
        for r in results:
            rid = r.get('id')
            if rid is None or rid in seen_ids:
                continue
            seen_ids.add(rid)
            merged.append(r)
            if len(merged) >= limit * 3:
                break
        if len(merged) >= limit:
            break

    if not merged:
        if last_err:
            return [], last_err
        return [], '未找到匹配结果'

    # 拿详情补全字段（批量，最多 limit 条）
    candidates = []
    for r in merged[:limit]:
        tmdb_id = r.get('id')
        media_type = r.get('media_type') or 'movie'
        r_year = (r.get('release_date') or r.get('first_air_date') or '')[:4]
        try:
            if media_type == 'tv':
                detail, err = get_tv_detail(tmdb_id)
            else:
                detail, err = get_movie_detail(tmdb_id)
        except Exception as e:
            detail, err = None, str(e)
        if detail:
            title = detail.get('title') or detail.get('name') or r.get('title') or r.get('name') or ''
            otitle = detail.get('original_title') or detail.get('original_name') or r.get('original_title') or r.get('original_name') or ''
            dyear = (detail.get('release_date') or detail.get('first_air_date') or r_year or '')[:4]
            poster = detail.get('poster_path') or r.get('poster_path') or ''
            overview = detail.get('overview') or r.get('overview') or ''
            vote = detail.get('vote_average') or r.get('vote_average') or 0
        else:
            title = r.get('title') or r.get('name') or ''
            otitle = r.get('original_title') or r.get('original_name') or ''
            dyear = r_year
            poster = r.get('poster_path') or ''
            overview = r.get('overview') or ''
            vote = r.get('vote_average') or 0
        poster_url = f'https://image.tmdb.org/t/p/w300{poster}' if poster else ''
        candidates.append({
            'tmdb_id': tmdb_id,
            'media_type': media_type,
            'title': title,
            'original_title': otitle,
            'year': dyear,
            'overview': overview,
            'poster_path': poster,
            'poster_url': poster_url,
            'vote_average': vote,
        })
    return candidates, None


def identify_media(name, year=None):
    """自动识别媒体，多策略搜索

    支持两种输入：
    1. TMDB ID（纯数字）- 直接获取详情
    2. 电影名 - 多策略搜索
    """
    cache_key = (name, year)
    with _CACHE_LOCK:
        if cache_key in _IDENTIFY_CACHE:
            cached = _IDENTIFY_CACHE[cache_key]
            if cached[0] is not None:
                return cached[0], None
            return None, cached[1]

    result, err = _identify_media_impl(name, year)
    # 只缓存成功结果和"未找到匹配"，不缓存配置错误（如"未配置API Key"），
    # 避免配置修正后仍返回旧错误
    if result is not None or (err and '未配置' not in err and '搜索失败' not in err):
        with _CACHE_LOCK:
            _IDENTIFY_CACHE[cache_key] = (result, err)
    return result, err


def _identify_media_impl(name, year=None):
    """identify_media 的实际实现"""
    # 如果输入是纯数字，当作TMDB ID直接查询
    if name.isdigit():
        result, err = get_media_by_id(int(name))
        if result:
            return result, None
        # ID查询失败，继续尝试搜索
        pass

    # 策略1: 原始名称搜索（带年份）
    results, err = search_multi(name, year=year)
    if err:
        return None, err

    # 策略2: 去掉括号内容
    if not results:
        cleaned = name.split('（')[0].split('(')[0].strip()
        if cleaned != name and cleaned:
            results, err = search_multi(cleaned, year=year)
            if err:
                return None, err

    # 策略3: 取中文部分（如果有中英混合）
    if not results:
        cn_match = re.search(r'([\u4e00-\u9fff]+)', name)
        if cn_match:
            cn_name = cn_match.group(1)
            if cn_name != name and len(cn_name) >= 2:
                results, err = search_multi(cn_name, year=year)
                if err:
                    return None, err

    # 策略4: 取英文部分（如果有中英混合）
    if not results:
        en_match = re.search(r'([A-Za-z][A-Za-z\s\.]+)', name)
        if en_match:
            en_name = en_match.group(1).strip().replace('.', ' ')
            if en_name and len(en_name) >= 3 and en_name != name:
                results, err = search_multi(en_name, year=year)
                if err:
                    return None, err

    if not results:
        return None, '未找到匹配结果'

    best = results[0]
    if year:
        for r in results:
            r_year = (r.get('release_date') or r.get('first_air_date') or '')[:4]
            if r_year == str(year):
                best = r
                break

    media_type = best.get('media_type', 'movie')
    tmdb_id = best.get('id')

    if media_type == 'tv':
        detail, err = get_tv_detail(tmdb_id)
    else:
        detail, err = get_movie_detail(tmdb_id)

    if detail:
        return {
            'tmdb_id': tmdb_id,
            'media_type': media_type,
            'title': detail.get('title') or detail.get('name', ''),
            'original_title': detail.get('original_title') or detail.get('original_name', ''),
            'year': (detail.get('release_date') or detail.get('first_air_date') or '')[:4],
            'genres': [g.get('name', '') for g in detail.get('genres', [])],
            'genre_ids': [g.get('id') for g in detail.get('genres', [])],
            'original_language': detail.get('original_language', ''),
            'production_countries': [c.get('iso_3166_1', '') for c in detail.get('production_countries', [])],
            'overview': detail.get('overview', ''),
            'poster_path': detail.get('poster_path', ''),
            'vote_average': detail.get('vote_average', 0),
        }, None

    return {
        'tmdb_id': tmdb_id,
        'media_type': media_type,
        'title': best.get('title') or best.get('name', ''),
        'original_title': best.get('original_title') or best.get('original_name', ''),
        'year': (best.get('release_date') or best.get('first_air_date') or '')[:4],
        'genres': [],
        'genre_ids': best.get('genre_ids', []),
        'original_language': best.get('original_language', ''),
        'production_countries': [],
        'overview': best.get('overview', ''),
        'poster_path': best.get('poster_path', ''),
        'vote_average': best.get('vote_average', 0),
    }, None


def identify_batch(items, max_workers=5):
    """批量识别媒体（并发）

    Args:
        items: [{'name': ..., 'year': ...}, ...]
        max_workers: 最大并发数

    Returns:
        [{'success': bool, 'result': ..., 'error': ...}, ...] 顺序与items一致
    """
    results = [None] * len(items)

    def _worker(idx, item):
        name = item.get('name', '')
        year = item.get('year')
        if not name:
            return idx, {'success': False, 'error': '名称为空'}
        try:
            result, err = identify_media(name, year)
            if result:
                return idx, {'success': True, 'result': result}
            return idx, {'success': False, 'error': err or '未识别'}
        except Exception as e:
            return idx, {'success': False, 'error': str(e)}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_worker, i, item) for i, item in enumerate(items)]
        for future in as_completed(futures):
            idx, res = future.result()
            results[idx] = res

    return results
