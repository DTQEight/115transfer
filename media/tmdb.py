"""TMDB API封装"""
import requests
import os
import json

TMDB_BASE_URL = 'https://api.themoviedb.org/3'

CONFIG_FILE = os.path.join(os.environ.get('DATA_DIR', os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'cloud115_config.json')


def _load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def get_tmdb_api_key():
    config = _load_config()
    return config.get('tmdb_api_key', '')


def set_tmdb_api_key(api_key):
    config = _load_config()
    config['tmdb_api_key'] = api_key
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def search_multi(query, language='zh-CN'):
    """搜索电影/电视剧（多类型搜索）"""
    api_key = get_tmdb_api_key()
    if not api_key:
        return None, '未配置TMDB API Key'

    try:
        url = f'{TMDB_BASE_URL}/search/multi'
        params = {
            'api_key': api_key,
            'query': query,
            'language': language,
            'page': 1,
        }
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()

        if resp.status_code != 200:
            return None, data.get('status_message', '搜索失败')

        results = data.get('results', [])
        filtered = [r for r in results if r.get('media_type') in ('movie', 'tv')]
        return filtered, None
    except Exception as e:
        return None, f'搜索失败: {str(e)}'


def get_movie_detail(movie_id, language='zh-CN'):
    """获取电影详情"""
    api_key = get_tmdb_api_key()
    if not api_key:
        return None, '未配置TMDB API Key'

    try:
        url = f'{TMDB_BASE_URL}/movie/{movie_id}'
        params = {'api_key': api_key, 'language': language}
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code != 200:
            return None, '获取详情失败'
        return resp.json(), None
    except Exception as e:
        return None, f'获取详情失败: {str(e)}'


def get_tv_detail(tv_id, language='zh-CN'):
    """获取电视剧详情"""
    api_key = get_tmdb_api_key()
    if not api_key:
        return None, '未配置TMDB API Key'

    try:
        url = f'{TMDB_BASE_URL}/tv/{tv_id}'
        params = {'api_key': api_key, 'language': language}
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code != 200:
            return None, '获取详情失败'
        return resp.json(), None
    except Exception as e:
        return None, f'获取详情失败: {str(e)}'


def identify_media(name, year=None):
    """自动识别媒体"""
    results, err = search_multi(name)
    if err:
        return None, err
    if not results:
        cleaned = name.split('（')[0].split('(')[0].strip()
        if cleaned != name:
            results, err = search_multi(cleaned)
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
