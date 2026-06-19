"""豆瓣看过的电影同步模块"""
import requests
import re
import json
import os
import time
import html as html_module

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
    # 注意：不要使用 mode=list，默认 grid 模式才能拿到电影名
    params = {
        'start': start,
        'sort': 'time',
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

        # 默认 grid 模式: <a title="电影名" href="https://movie.douban.com/subject/xxx/" class="nbg">
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

        # 获取总数: <h1>我看过的影视(1192)</h1>
        total_match = re.search(r'<h1>[^<]*[\(（](\d+)[\)）]</h1>', html)
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
