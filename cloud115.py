import requests
import json
import os
import time

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
        url = 'https://my.115.com/?ct=ajax&ac=nav'
        headers = {
            'User-Agent': USER_AGENT,
            'Cookie': cookie,
        }
        resp = requests.get(url, headers=headers, timeout=10)
        
        if resp.status_code != 200:
            return False, f'请求失败，状态码: {resp.status_code}'
        
        try:
            data = resp.json()
            if data.get('state') is True and data.get('data'):
                user_name = data['data'].get('user_name', '未知')
                return True, f'验证成功 (用户: {user_name})'
            return False, f'Cookie已失效，请重新获取'
        except json.JSONDecodeError:
            return False, f'响应格式错误'
    except requests.exceptions.Timeout:
        return False, '请求超时，请检查网络连接'
    except requests.exceptions.ConnectionError:
        return False, '连接失败，请检查网络'
    except Exception as e:
        return False, f'验证失败: {str(e)}'


def add_offline_task(magnet_url, save_path_id=None):
    cookie = get_cookie_string()
    if not cookie:
        return False, '未配置115 Cookie'

    if save_path_id is None:
        save_path_id = get_default_save_path()

    try:
        url = 'https://115.com/web/lixian/?ct=lixian&ac=add_task_url'
        data = {
            'url': magnet_url,
            'wp_path_id': save_path_id,
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
        url = f'https://115.com/web/lixian/?ct=lixian&ac=task_list&page={page}'
        resp = requests.get(url, headers=_get_headers(), timeout=15)
        
        if resp.status_code != 200:
            return False, f'请求失败，状态码: {resp.status_code}', []
        
        try:
            result = resp.json()
        except json.JSONDecodeError:
            return False, '响应格式错误', []

        if result.get('state') is True or result.get('state') == 1:
            tasks = result.get('result', result.get('data', []))
            if isinstance(tasks, dict):
                tasks = tasks.get('tasks', tasks.get('list', []))
            return True, '获取成功', tasks if isinstance(tasks, list) else []
        return False, result.get('error_msg', result.get('error', '获取任务列表失败')), []
    except Exception as e:
        return False, f'获取失败: {str(e)}', []


def batch_add_offline_tasks(magnet_urls, save_path_id=None):
    if save_path_id is None:
        save_path_id = get_default_save_path()
    results = []
    for magnet in magnet_urls:
        success, msg = add_offline_task(magnet, save_path_id)
        results.append({'magnet': magnet[:50] + '...', 'success': success, 'message': msg})
        if success:
            time.sleep(1)
    return results


def get_dir_list(cid='0'):
    cookie = get_cookie_string()
    if not cookie:
        return False, '未配置115 Cookie', []

    try:
        url = f'https://webapi.115.com/files?aid=1&cid={cid}&o=user_utime&asc=0&offset=0&show_dir=1&snap=0&natsort=1&format=json'
        resp = requests.get(url, headers=_get_headers(), timeout=15)
        
        if resp.status_code != 200:
            return False, f'请求失败，状态码: {resp.status_code}', []
        
        data = resp.json()
        if data.get('errno') == 0 or data.get('state') is True:
            dirs = []
            items = data.get('data', [])
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and item.get('cid') and item.get('n'):
                        dirs.append({
                            'cid': str(item['cid']),
                            'name': item['n'],
                            'parent_id': str(item.get('pid', '0'))
                        })
            return True, '获取成功', dirs
        return False, f'获取目录列表失败: {data.get("error", "未知错误")}', []
    except Exception as e:
        return False, f'获取失败: {str(e)}', []


def get_default_save_path():
    config = load_config()
    return config.get('save_path_id', '0'), config.get('save_path_name', '根目录')


def set_default_save_path(path_id, path_name='根目录'):
    config = load_config()
    config['save_path_id'] = path_id
    config['save_path_name'] = path_name
    save_config(config)


def create_dir(parent_cid, dir_name):
    cookie = get_cookie_string()
    if not cookie:
        return False, '未配置115 Cookie'

    try:
        url = 'https://webapi.115.com/files/add'
        data = {
            'pid': parent_cid,
            'cname': dir_name,
        }
        resp = requests.post(url, headers=_get_headers(), data=data, timeout=15)
        result = resp.json()
        if result.get('errno') == 0 or result.get('state') is True:
            cid = result.get('cid', result.get('file_id', ''))
            return True, f'目录创建成功'
        return False, f'创建失败: {result.get("error", "未知错误")}'
    except Exception as e:
        return False, f'创建失败: {str(e)}'


VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.ts', '.m2ts', '.mpg', '.mpeg', '.webm', '.m4v', '.rmvb', '.iso'}


def is_video_file(filename):
    """判断是否为视频文件"""
    name = filename.lower()
    return any(name.endswith(ext) for ext in VIDEO_EXTENSIONS)


def list_files(cid='0', offset=0, limit=100, show_dir=1):
    """列出目录下的文件和子目录"""
    cookie = get_cookie_string()
    if not cookie:
        return False, '未配置115 Cookie', []

    try:
        url = f'https://webapi.115.com/files?aid=1&cid={cid}&o=user_utime&asc=0&offset={offset}&show_dir={show_dir}&snap=0&natsort=1&format=json&limit={limit}'
        resp = requests.get(url, headers=_get_headers(), timeout=15)

        if resp.status_code != 200:
            return False, f'请求失败，状态码: {resp.status_code}', []

        data = resp.json()
        if data.get('errno') == 0 or data.get('state') is True:
            items = []
            raw = data.get('data', [])
            if isinstance(raw, list):
                for item in raw:
                    if not isinstance(item, dict):
                        continue
                    is_dir = item.get('cid') and not item.get('fid')
                    if is_dir:
                        items.append({
                            'type': 'dir',
                            'cid': str(item['cid']),
                            'name': item.get('n', ''),
                            'parent_id': str(item.get('pid', cid)),
                        })
                    elif item.get('fid'):
                        items.append({
                            'type': 'file',
                            'fid': str(item['fid']),
                            'name': item.get('n', ''),
                            'size': int(item.get('s', 0)),
                            'pickcode': item.get('pc', ''),
                            'parent_id': str(item.get('pid', cid)),
                        })
            return True, '获取成功', items
        return False, f'获取列表失败: {data.get("error", "未知错误")}', []
    except Exception as e:
        return False, f'获取失败: {str(e)}', []


def move_files(file_ids, target_cid):
    """批量移动文件到目标目录"""
    cookie = get_cookie_string()
    if not cookie:
        return False, '未配置115 Cookie'

    try:
        url = 'https://webapi.115.com/files/move'
        data = {'pid': target_cid}
        for i, fid in enumerate(file_ids):
            data[f'fid[{i}]'] = fid
        resp = requests.post(url, headers=_get_headers(), data=data, timeout=30)
        if not resp:
            return False, '请求失败'
        result = resp.json()
        if result.get('errno') == 0 or result.get('state') is True:
            return True, '移动成功'
        return False, f'移动失败: {result.get("error", "未知错误")}'
    except Exception as e:
        return False, f'移动失败: {str(e)}'


def get_video_files_recursive(cid, max_depth=5, current_depth=0):
    """递归获取目录下所有视频文件"""
    if current_depth >= max_depth:
        return []

    video_files = []
    success, msg, items = list_files(cid, show_dir=1)
    if not success:
        return video_files

    for item in items:
        if item['type'] == 'dir':
            video_files.extend(get_video_files_recursive(item['cid'], max_depth, current_depth + 1))
        elif item['type'] == 'file' and is_video_file(item['name']):
            video_files.append(item)

    return video_files
