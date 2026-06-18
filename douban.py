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
        # 匹配模式: <li class="item"> ... <a href="https://movie.douban.com/subject/xxx/" ... >电影名</a> ... <span class="date">2019</span>
        # 或者更简单的模式
        items = re.findall(
            r'<div class="item">\s*<a[^>]*href="(https://movie\.douban\.com/subject/\d+/)"[^>]*>\s*<img[^>]*alt="([^"]*)"[^>]*/>\s*</a>.*?<li class="title">[^<]*<a[^>]*>([^<]*)</a>.*?(?:<span class="date">([^<]*)</span>)?',
            html, re.DOTALL
        )

        if not items:
            # 备用解析模式
            items = re.findall(
                r'<a[^>]*href="(https://movie\.douban\.com/subject/\d+/)"[^>]*class="nbg"[^>]*title="([^"]*)"',
                html, re.DOTALL
            )
            for url, title in items:
                movies.append({
                    'title': title.strip(),
                    'url': url,
                    'year': '',
                    'rating': '',
                })
        else:
            for url, alt, title, date in items:
                year_match = re.search(r'(\d{4})', date if date else '')
                movies.append({
                    'title': title.strip() if title.strip() else alt.strip(),
                    'url': url,
                    'year': year_match.group(1) if year_match else '',
                    'rating': '',
                })

        # 如果两种模式都没匹配到，尝试最简单的模式
        if not movies:
            titles = re.findall(r'title="([^"]+)"[^>]*class="nbg"', html)
            if not titles:
                titles = re.findall(r'class="title">([^<]+)<', html)
            for title in titles:
                movies.append({
                    'title': title.strip(),
                    'url': '',
                    'year': '',
                    'rating': '',
                })

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

    movies, total, err = fetch_watched_movies(user_id, 0, 1)
    if err:
        return False, err
    return True, f'Cookie有效，共{total}部看过的电影'
