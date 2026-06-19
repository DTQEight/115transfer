"""豆瓣看过的电影同步模块"""
import requests
import re
import json
import os
import time

CONFIG_FILE = os.path.join(os.environ.get('DATA_DIR', os.path.dirname(os.path.abspath(__file__))), 'douban_config.json')
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass
    return {}


def save_config(config):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def _get_headers():
    config = load_config()
    cookie = config.get('cookie', '')
    return {
        'User-Agent': USER_AGENT,
        'Cookie': cookie,
        'Referer': 'https://movie.douban.com/',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    }


def fetch_watched_movies(user_id, start=0, count=15):
    """获取用户看过的电影列表
    返回: (movie_list, total_count, error_msg)
    movie_list: [{'title': '电影名', 'year': '2019', 'rating': '8.5', 'url': '...'}, ...]
    """
    config = load_config()
    cookie = config.get('cookie', '')
    if not cookie:
        return [], 0, '未配置豆瓣Cookie'

    url = f'https://movie.douban.com/people/{user_id}/collect'
    params = {
        'start': start,
        'sort': 'time',
        'mode': 'list',
        'tags_sort': 'rec',
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

        # 模式1: <a ... class="nbg" ... title="电影名 英文名 (年份)">
        for m in re.finditer(
            r'class="?nbg"?[^>]*?title="([^"]*)"',
            html, re.DOTALL
        ):
            title_str = m.group(1)
            if title_str in seen:
                continue
            seen.add(title_str)
            year_match = re.search(r'\((\d{4})\)\s*$', title_str)
            year = year_match.group(1) if year_match else ''
            title_clean = re.split(r'\s+[A-Za-z]', title_str)[0].strip()
            if not title_clean:
                title_clean = title_str.split('(')[0].strip()
            title_clean = re.sub(r'\s*\(\d{4}\)\s*$', '', title_clean).strip()
            if title_clean:
                movies.append({'title': title_clean, 'url': '', 'year': year, 'rating': ''})

        # 模式2: <li class="title"><a ... >电影名</a></li>
        if not movies:
            for m in re.finditer(
                r'<li class="title">\s*<a[^>]*>([^<]+)</a>',
                html, re.DOTALL
            ):
                title = m.group(1).strip()
                if title and title not in seen:
                    seen.add(title)
                    movies.append({'title': title, 'url': '', 'year': '', 'rating': ''})

        # 模式3: <a ... title="中文名 ..." ... class="nbg">
        if not movies:
            for m in re.finditer(
                r'title="([^"]*)"[^>]*?class="?nbg"?',
                html, re.DOTALL
            ):
                title_str = m.group(1)
                if title_str in seen:
                    continue
                seen.add(title_str)
                year_match = re.search(r'\((\d{4})\)\s*$', title_str)
                year = year_match.group(1) if year_match else ''
                title_clean = re.split(r'\s+[A-Za-z]', title_str)[0].strip()
                if not title_clean:
                    title_clean = title_str.split('(')[0].strip()
                title_clean = re.sub(r'\s*\(\d{4}\)\s*$', '', title_clean).strip()
                if title_clean:
                    movies.append({'title': title_clean, 'url': '', 'year': year, 'rating': ''})

        # 获取总数
        total_match = re.search(r'<span class="count">\s*[\（(](\d+)[\)）]\s*</span>', html)
        total = int(total_match.group(1)) if total_match else len(movies)

        return movies, total, None

    except requests.Timeout:
        return [], 0, '请求超时'
    except Exception as e:
        return [], 0, f'获取失败: {str(e)}'


def fetch_all_watched_movies(user_id):
    """获取用户所有看过的电影"""
    config = load_config()
    cookie = config.get('cookie', '')
    if not cookie:
        return [], '未配置豆瓣Cookie'

    all_movies = []
    start = 0
    per_page = 15
    total = None

    while True:
        movies, count, err = fetch_watched_movies(user_id, start, per_page)
        if err:
            if all_movies:
                break
            return [], err

        if total is None:
            total = count

        all_movies.extend(movies)

        if len(all_movies) >= total or len(movies) < per_page:
            break

        start += per_page
        time.sleep(0.5)  # 避免请求过快

    return all_movies, None


def check_cookie(user_id):
    """检查豆瓣Cookie是否有效"""
    config = load_config()
    cookie = config.get('cookie', '')
    if not cookie:
        return False, '未配置豆瓣Cookie'

    movies, total, err = fetch_watched_movies(user_id, 0, 15)
    if err:
        return False, err
    return True, f'Cookie有效，本页获取到{len(movies)}部电影，页面报告共{total}部'
