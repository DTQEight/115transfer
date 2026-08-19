"""Jellyfin 影视库对接：查询电影是否已入库

流程：
1. 用 API Key 认证 Jellyfin（免登录，Key 在 Jellyfin 后台 控制台-API密钥 生成）
2. 拉取指定媒体库（或全部）的电影/剧集条目
3. 按标题（+年份可选）与本地电影匹配，生成"已入库"状态
"""
import re
import threading
import requests
import logging

# 加密工具统一入口
from crypto_utils import encrypt, decrypt

import os
import json

CONFIG_FILE = os.path.join(
    os.environ.get('DATA_DIR', os.path.dirname(os.path.abspath(__file__))),
    'jellyfin_config.json'
)

_config_lock = threading.Lock()

logger = logging.getLogger('jellyfin')

# Jellyfin 请求专用会话：国内NAS多为局域网部署，直连不走代理
_SESSION = requests.Session()
_SESSION.trust_env = False


def _load_unlocked():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f'[Jellyfin] 配置文件读取失败: {e}，使用空配置')
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


def _normalize_title(title):
    """标题标准化：去空格/标点/大小写，用于宽松匹配"""
    if not title:
        return ''
    t = title.lower()
    t = re.sub(r'[\s·・:：!！?？.。,，\-—_\'"()\[\]【】<>《》/\\|]+', '', t)
    return t


def get_library_items(base_url, api_key, library_ids=None):
    """拉取 Jellyfin 影视库全部影片条目

    Args:
        base_url: 如 http://192.168.1.100:8096
        api_key: Jellyfin API Key
        library_ids: 指定媒体库ID列表；None/空 表示全部媒体库

    Returns:
        (items, error_msg)
        items: [{'title': 原始标题, 'year': 年份或'', 'normalized': 标准化标题}, ...]
    """
    if not base_url or not api_key:
        return [], '未配置Jellyfin'

    url = base_url.rstrip('/') + '/Items'
    params = {
        'IncludeItemTypes': 'Movie,Series',
        'Fields': 'ProductionYear',
        'Recursive': 'true',
        'EnableImages': 'false',
        'Limit': 20000,
    }
    if library_ids:
        # 多媒体库分次拉取合并（Jellyfin 不支持一次传多个 ParentId）
        all_items = []
        for lib_id in library_ids:
            params['ParentId'] = lib_id
            try:
                resp = _SESSION.get(url, params=params,
                                     headers={'X-Emby-Token': api_key}, timeout=15)
                if resp.status_code != 200:
                    return [], f'请求失败，状态码: {resp.status_code}'
                data = resp.json()
                for it in data.get('Items', []):
                    all_items.append({
                        'title': it.get('Name', ''),
                        'year': str(it.get('ProductionYear') or ''),
                        'normalized': _normalize_title(it.get('Name', '')),
                    })
            except requests.Timeout:
                return [], '请求Jellyfin超时'
            except Exception as e:
                return [], f'连接失败: {e}'
        return all_items, None

    try:
        resp = _SESSION.get(url, params=params,
                            headers={'X-Emby-Token': api_key}, timeout=15)
        if resp.status_code == 401:
            return [], 'API Key无效'
        if resp.status_code != 200:
            return [], f'请求失败，状态码: {resp.status_code}'
        data = resp.json()
        items = []
        for it in data.get('Items', []):
            items.append({
                'title': it.get('Name', ''),
                'year': str(it.get('ProductionYear') or ''),
                'normalized': _normalize_title(it.get('Name', '')),
            })
        return items, None
    except requests.Timeout:
        return [], '请求Jellyfin超时'
    except requests.exceptions.ConnectionError:
        return [], '连接失败，请检查地址'
    except Exception as e:
        return [], f'连接失败: {e}'


def get_libraries(base_url, api_key):
    """获取媒体库列表（用于配置页选择）

    Returns:
        (libraries, error_msg)
        libraries: [{'id': ..., 'name': ...}, ...]
    """
    if not base_url or not api_key:
        return [], '未配置地址或API Key'
    url = base_url.rstrip('/') + '/Library/MediaFolders'
    try:
        resp = _SESSION.get(url, headers={'X-Emby-Token': api_key}, timeout=10)
        if resp.status_code == 401:
            return [], 'API Key无效'
        if resp.status_code != 200:
            return [], f'请求失败，状态码: {resp.status_code}'
        libs = []
        for it in resp.json().get('Items', []):
            libs.append({'id': it.get('Id', ''), 'name': it.get('Name', '')})
        return libs, None
    except requests.Timeout:
        return [], '请求Jellyfin超时'
    except requests.exceptions.ConnectionError:
        return [], '连接失败，请检查地址'
    except Exception as e:
        return [], f'连接失败: {e}'


def test_connection(base_url, api_key):
    """测试连接：拉媒体库列表，返回 (success, message)"""
    libs, err = get_libraries(base_url, api_key)
    if err:
        return False, err
    return True, f'连接成功，共{len(libs)}个媒体库: ' + '、'.join(l['name'] for l in libs)


def build_in_library_set(movies, jellyfin_items, match_year=True):
    """比对本地电影与 Jellyfin 条目，返回已入库的豆瓣URL集合

    匹配策略：
    1. 标题标准化后精确匹配；同名多条时若带年份则校验年份
    2. Jellyfin 同名多个版本（年份不同）只要有一个命中即算已入库

    Args:
        movies: [{'title': ..., 'url': ..., 'year': ...}, ...] 本地电影（豆瓣同步格式）
        jellyfin_items: get_library_items 的返回
        match_year: 是否在同名多版本时校验年份

    Returns:
        set of 豆瓣URL
    """
    # Jellyfin 条目按标准化标题分组：{norm: [year1, year2...]}
    jf_map = {}
    for it in jellyfin_items:
        if it['normalized']:
            jf_map.setdefault(it['normalized'], []).append(it['year'])

    in_lib = set()
    for m in movies:
        norm = _normalize_title(m.get('title', ''))
        if not norm or norm not in jf_map:
            continue
        years = jf_map[norm]
        if match_year and len(years) > 1 and m.get('year'):
            # Jellyfin 同名多版本：任一年份匹配即入库
            if m['year'] in years:
                in_lib.add(m.get('url', ''))
        else:
            in_lib.add(m.get('url', ''))
    return in_lib


def refresh_in_library_status(movies):
    """根据配置拉取 Jellyfin 并比对，返回入库URL集合（配置缺失返回空集不报错）"""
    config = load_config()
    base_url = config.get('base_url', '').strip()
    api_key = decrypt(config.get('api_key', ''))
    if not base_url or not api_key:
        return set()
    library_ids = config.get('library_ids') or []
    items, err = get_library_items(base_url, api_key, library_ids)
    if err:
        logger.warning(f'[Jellyfin] 拉取媒体库失败: {err}')
        return set()
    logger.info(f'[Jellyfin] 拉取到{len(items)}个条目，开始比对')
    return build_in_library_set(movies, items)
