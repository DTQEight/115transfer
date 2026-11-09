"""2bt0 资源库（局域网磁力库）对接

115transfer 本地数据库搜不到磁力链接时，回退到局域网内的 2bt0 资源库服务做补充搜索。

接口（服务端 FastAPI）：
    GET /api/items?source=local&q=<关键词>&page=<页码>
    Header: X-API-Key: <密钥>
返回体：
    {'items': [{'id', 'title', 'magnet', 'size', 'published_at',
                'category', 'detail_url', 'extra': {...}}],
     'page', 'total_pages', 'total_items', 'source'}

单条结果自带 magnet，无需再请求详情，因此可以零成本展示并直接添加到115。
"""
import json
import os
import threading

import requests

_DATA_DIR = (
    os.environ.get('DATA_DIR')
    or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
)
CONFIG_FILE = os.path.join(_DATA_DIR, 'bt0_library_config.json')

# 服务默认地址（密钥不入库，需在网页「企业微信设置」旁的论坛设置里填写）
DEFAULT_BASE_URL = 'http://192.168.31.52:8000'

_config_lock = threading.Lock()


def _load_unlocked():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
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
    """事务性更新配置：load → mutator(config) → save"""
    with _config_lock:
        config = _load_unlocked()
        mutator(config)
        _save_unlocked(config)


def get_settings():
    """返回 (base_url, api_key, enabled)"""
    cfg = load_config()
    base = (cfg.get('base_url') or DEFAULT_BASE_URL).strip().rstrip('/')
    key = (cfg.get('api_key') or '').strip()
    enabled = bool(cfg.get('enabled', True))
    return base, key, enabled


def is_configured():
    """是否已填密钥且启用（未配置时调用方直接跳过回退）"""
    base, key, enabled = get_settings()
    return bool(enabled and key and base)


def _synthetic_tid(item):
    """把 2bt0 的 hash id 映射成稳定的数字 tid

    前端沿用论坛结果的渲染逻辑（DOM id 用数值 tid），这里给出稳定的
    数字标识；不同条目的 hash 不同，9 位十六进制碰撞概率可忽略。
    """
    raw = (item.get('id') or item.get('magnet') or item.get('title') or '').strip()
    h = 0
    for ch in raw:
        h = (h * 131 + ord(ch)) & 0xFFFFFFFFFFFFFFFF
    return 900000000 + h % 90000000


def search(keyword, page=1, page_size=20):
    """搜索 2bt0 资源库

    Returns:
        {'results': [{'tid', 'title', 'forum', 'author', 'date', 'replies',
                      'views', 'magnet', 'magnets', 'size', 'detail_url',
                      'source': 'bt0'}, ...],
         'page', 'total_pages', 'total_count', 'source': 'bt0'}
    """
    base, key, enabled = get_settings()
    if not enabled:
        raise RuntimeError('2bt0 资源库未启用')
    if not key:
        raise RuntimeError('未配置 2bt0 资源库 API 密钥')
    if not keyword:
        return {'results': [], 'page': page, 'total_pages': 0,
                'total_count': 0, 'source': 'bt0'}

    params = {'source': 'local', 'q': keyword, 'page': max(1, int(page))}
    resp = requests.get(
        f'{base}/api/items',
        params=params,
        headers={'X-API-Key': key, 'Accept': 'application/json'},
        timeout=(5, 20),
    )
    if resp.status_code == 401:
        raise RuntimeError('2bt0 资源库鉴权失败（API 密钥无效）')
    if resp.status_code >= 400:
        raise RuntimeError(f'2bt0 资源库返回 HTTP {resp.status_code}')
    data = resp.json() or {}

    items = data.get('items') or []
    results = []
    for it in items:
        magnet = (it.get('magnet') or '').strip()
        if not magnet:
            continue
        title = (it.get('title') or '').strip()
        extra = it.get('extra') or {}
        results.append({
            'tid': _synthetic_tid(it),
            'title': title,
            'forum': '2bt0资源库',
            'author': extra.get('movie_title') or '',
            'date': (it.get('published_at') or '')[:10],
            'replies': '',
            'views': '',
            'magnet': magnet,
            # 单条结果即一个版本，无多磁力；保持与论坛结果同结构
            'magnets': [{'magnet': magnet, 'name': title}],
            'size': it.get('size') or '',
            'detail_url': it.get('detail_url') or '',
            'source': 'bt0',
        })

    total_count = data.get('total_items') or len(results)
    total_pages = data.get('total_pages') or 0
    return {
        'results': results,
        'page': data.get('page') or page,
        'total_pages': total_pages,
        'total_count': total_count,
        'source': 'bt0',
    }


def test_connection():
    """连通性测试：用一个常见词探测接口是否可用"""
    base, key, enabled = get_settings()
    if not enabled:
        return False, '2bt0 资源库未启用'
    if not key:
        return False, '未配置 API 密钥'
    try:
        resp = requests.get(
            f'{base}/api/items',
            params={'source': 'local', 'q': '测试', 'page': 1},
            headers={'X-API-Key': key, 'Accept': 'application/json'},
            timeout=(5, 15),
        )
    except Exception as e:
        return False, f'连接失败: {e}'
    if resp.status_code == 401:
        return False, 'API 密钥无效（HTTP 401）'
    if resp.status_code >= 400:
        return False, f'服务返回 HTTP {resp.status_code}'
    try:
        data = resp.json() or {}
    except Exception:
        return False, '服务返回内容不是 JSON'
    return True, f'连接成功（库内可检索条目 {data.get("total_items", 0)} 个）'
