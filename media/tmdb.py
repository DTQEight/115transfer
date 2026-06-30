"""TMDB API封装"""
import requests
import os
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache

TMDB_BASE_URL = 'https://api.themoviedb.org/3'

# cloud115_config.json 由 cloud115.py 统一管理（含读写锁），tmdb 模块复用同一文件
import cloud115
CONFIG_FILE = cloud115.CONFIG_FILE
_load_config = cloud115.load_config

# 全局Session复用TCP连接 + 内存缓存
_SESSION = requests.Session()
_SESSION.trust_env = False
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
            return None, '获取详情失败'
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
            return None, '获取详情失败'
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
        'production_countries': [c.get('iso_3166_1', '') for c in detail.get('origin_country', [])],
        'overview': detail.get('overview', ''),
        'poster_path': detail.get('poster_path', ''),
        'vote_average': detail.get('vote_average', 0),
    }


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
