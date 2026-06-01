import requests
import json
import os
import time
import hashlib
import random
import string

CONFIG_FILE = os.path.join(os.environ.get('DATA_DIR', os.path.dirname(os.path.abspath(__file__))), 'cloud115_config.json')

USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_config(config):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def get_cookie_string():
    config = load_config()
    return config.get('cookie', '')


def _get_headers():
    return {
        'User-Agent': USER_AGENT,
        'Cookie': get_cookie_string(),
        'Content-Type': 'application/x-www-form-urlencoded',
        'Referer': 'https://115.com/',
        'Origin': 'https://115.com',
    }


def verify_cookie():
    cookie = get_cookie_string()
    if not cookie:
        return False, '未配置115 Cookie'

    try:
        url = 'https://passportapi.115.com/app/1.0/web/1.0/checkLoginInfo'
        headers = {
            'User-Agent': USER_AGENT,
            'Cookie': cookie,
        }
        resp = requests.get(url, headers=headers, timeout=10)
        
        if resp.status_code != 200:
            return False, f'请求失败，状态码: {resp.status_code}'
        
        try:
            data = resp.json()
        except json.JSONDecodeError:
            return False, f'响应不是有效JSON，内容: {resp.text[:100]}'
        
        if data.get('state') == 1 and data.get('data', {}).get('USER_ID'):
            user_id = data['data']['USER_ID']
            return True, f'验证成功 (UID: {user_id})'
        return False, f'Cookie已失效，请重新获取。响应: {data}'
    except requests.exceptions.Timeout:
        return False, '请求超时，请检查网络连接'
    except requests.exceptions.ConnectionError:
        return False, '连接失败，请检查网络'
    except Exception as e:
        return False, f'验证失败: {str(e)}'


def add_offline_task(magnet_url, save_path='/'):
    cookie = get_cookie_string()
    if not cookie:
        return False, '未配置115 Cookie'

    try:
        url = 'https://115.com/web/lixian/?ct=lixian&ac=add_task_url'
        data = {
            'url': magnet_url,
            'savepath_id': '0',
        }
        resp = requests.post(url, headers=_get_headers(), data=data, timeout=30)
        result = resp.json()

        if result.get('state') is True or result.get('state') == 1:
            task_id = result.get('result', [{}])[0].get('info_hash', '') if isinstance(result.get('result'), list) else ''
            return True, f'离线任务添加成功'
        else:
            error_msg = result.get('error_msg') or result.get('error') or result.get('msg') or '未知错误'
            return False, f'添加失败: {error_msg}'
    except requests.Timeout:
        return False, '请求超时，请稍后重试'
    except Exception as e:
        return False, f'请求失败: {str(e)}'


def get_task_list(page=1):
    cookie = get_cookie_string()
    if not cookie:
        return False, '未配置115 Cookie', []

    try:
        url = f'https://115.com/web/lixian/?ct=lixian&ac=task_progress&page={page}'
        resp = requests.get(url, headers=_get_headers(), timeout=15)
        result = resp.json()

        if result.get('state') is True or result.get('state') == 1:
            tasks = result.get('result', [])
            return True, '获取成功', tasks
        return False, '获取任务列表失败', []
    except Exception as e:
        return False, f'获取失败: {str(e)}', []


def batch_add_offline_tasks(magnet_urls):
    results = []
    for magnet in magnet_urls:
        success, msg = add_offline_task(magnet)
        results.append({'magnet': magnet[:50] + '...', 'success': success, 'message': msg})
        if success:
            time.sleep(1)
    return results


def generate_sign(uid):
    timestamp = str(int(time.time()))
    rand = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
    raw = f'{uid}{timestamp}{rand}'
    sign = hashlib.md5(raw.encode()).hexdigest()
    return sign, timestamp, rand
