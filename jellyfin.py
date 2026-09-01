"""Jellyfin 影视库对接：查询电影是否已入库

流程：
1. 用 API Key 认证 Jellyfin（免登录，Key 在 Jellyfin 后台 控制台-API密钥 生成）
2. 拉取指定媒体库（或全部）的电影/剧集条目
3. 按标题（+年份可选）与本地电影匹配，生成"已入库"状态
"""
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


def _auth_headers(api_key):
    """构造认证头

    Jellyfin 12.0 起默认禁用遗留认证（X-Emby-Token 等头被忽略，返回401），
    必须使用 Authorization: MediaBrowser Token="..."。
    该格式自 Jellyfin 10.x 起即为官方推荐写法，老版本同样支持。
    """
    return {'Authorization': f'MediaBrowser Token="{api_key}"'}


# 管理员用户ID缓存 {base_url -> user_id}
_admin_user_cache = {}


def _get_admin_user_id(base_url, api_key):
    """获取管理员用户ID（用于 /Users/{uid}/Items 查询）

    Jellyfin 12.0 的无用户上下文 /Items 接口会把合集(BoxSet)成员电影折叠掉：
    只返回合集本身，不返回其中的电影，导致系列片（如寒战2、画皮2）全部漏匹配。
    /Users/{uid}/Items（网页端使用的接口）返回全部条目，与界面显示一致。
    """
    if base_url in _admin_user_cache:
        return _admin_user_cache[base_url]
    user_id = None
    try:
        resp = _SESSION.get(base_url.rstrip('/') + '/Users',
                            headers=_auth_headers(api_key), timeout=10)
        if resp.status_code == 200:
            for u in resp.json():
                if u.get('Policy', {}).get('IsAdministrator'):
                    user_id = u.get('Id')
                    break
    except Exception as e:
        logger.warning(f'[Jellyfin] 获取用户列表失败，回退 /Items 接口: {e}')
    _admin_user_cache[base_url] = user_id
    return user_id


def get_library_items(base_url, api_key, library_ids=None):
    """拉取 Jellyfin 影视库全部影片条目

    Args:
        base_url: 如 http://192.168.1.100:8096
        api_key: Jellyfin API Key
        library_ids: 指定媒体库ID列表；None/空 表示全部媒体库

    Returns:
        (items, error_msg)
        items: [{'title', 'year', 'imdb_id'}, ...]
    """
    if not base_url or not api_key:
        return [], '未配置Jellyfin'

    def _parse_item(it):
        provider_ids = it.get('ProviderIds') or {}
        imdb_id = provider_ids.get('Imdb') or ''
        tmdb_id = provider_ids.get('Tmdb') or ''
        # 'Movie' / 'Series' 来自 IncludeItemTypes 请求，用于同类型精准确认，避免跨类型假阳性
        jf_type = it.get('Type') or ''
        # 归一化为 'movie' / 'tv' / ''
        norm_type = ''
        if jf_type == 'Movie':
            norm_type = 'movie'
        elif jf_type == 'Series':
            norm_type = 'tv'
        return {
            'title': it.get('Name', ''),
            'year': str(it.get('ProductionYear') or ''),
            'imdb_id': str(imdb_id) if imdb_id else '',
            'tmdb_id': str(tmdb_id) if tmdb_id else '',
            'media_type': norm_type,
        }

    # 优先走用户视角接口（含合集成员电影）；拿不到用户ID时回退 /Items
    admin_uid = _get_admin_user_id(base_url, api_key)
    url = (base_url.rstrip('/') + f'/Users/{admin_uid}/Items') if admin_uid else (base_url.rstrip('/') + '/Items')
    if admin_uid:
        logger.info(f'[Jellyfin] 使用用户视角接口拉取（含合集成员电影）')
    params = {
        'IncludeItemTypes': 'Movie,Series',
        'Fields': 'ProductionYear,ProviderIds',
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
                                     headers=_auth_headers(api_key), timeout=15)
                if resp.status_code != 200:
                    return [], f'请求失败，状态码: {resp.status_code}'
                data = resp.json()
                for it in data.get('Items', []):
                    all_items.append(_parse_item(it))
            except requests.Timeout:
                return [], '请求Jellyfin超时'
            except Exception as e:
                return [], f'连接失败: {e}'
        return all_items, None

    try:
        resp = _SESSION.get(url, params=params,
                            headers=_auth_headers(api_key), timeout=15)
        if resp.status_code == 401:
            return [], 'API Key无效'
        if resp.status_code != 200:
            return [], f'请求失败，状态码: {resp.status_code}'
        data = resp.json()
        items = [_parse_item(it) for it in data.get('Items', [])]
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
        resp = _SESSION.get(url, headers=_auth_headers(api_key), timeout=10)
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


def build_in_library_set(movies, jellyfin_items):
    """比对本地电影与 Jellyfin 条目，返回已入库的豆瓣URL集合

    匹配优先级（两者都是元数据权威ID，零歧义）：
      1. IMDb 编号：本地 IMDB_ID ∈ Jellyfin ProviderIds.Imdb
      2. TMDB 编号：本地 TMDB_ID ∈ Jellyfin ProviderIds.Tmdb（国产片无IMDb时手动识别）
    类型校验（避免跨类型假阳性）：
      - 本地有明确类型（imdb_media_type / tmdb_media_type 为 movie/tv）时，要求 Jellyfin 侧 Item.Type 一致；
        不一致 → 不命中，即使 ID 相同。
      - 本地无类型（旧纯数字 ID 未手动标注）→ 降级为"只要 ID 匹配就算命中"，保持向后兼容。
    不做标题/前缀模糊匹配——避免系列片、同名片误判（宁缺毋滥）。
    两个编号都缺即判未入库，待回填后再刷新。

    Args:
        movies: [{'title', 'url',
                  'imdb_id'(可选), 'imdb_media_type'(可选, 'movie'/'tv'),
                  'tmdb_id'(可选), 'tmdb_media_type'(可选, 'movie'/'tv')}, ...]
        jellyfin_items: get_library_items 的返回（含 media_type）
    Returns:
        set of 豆瓣URL
    """
    # 按 (id, media_type) 建桶 + 全局无类型桶（空字符串 type）
    def _build_id_sets(field):
        """field='imdb_id' 或 'tmdb_id' → (typed_map, global_set)
        typed_map: { id -> set(media_types 出现过) }，用于快速按类型精确命中
        global_set: set(id)，用于旧数据无类型时降级匹配
        """
        typed_map = {}
        global_set = set()
        for it in jellyfin_items:
            v = str(it.get(field) or '').strip()
            if not v:
                continue
            global_set.add(v)
            mt = it.get('media_type') or ''
            typed_map.setdefault(v, set()).add(mt)
        return typed_map, global_set

    jf_imdb_typed, jf_imdb_global = _build_id_sets('imdb_id')
    jf_tmdb_typed, jf_tmdb_global = _build_id_sets('tmdb_id')

    def _hit(typed_map, global_set, the_id, the_type):
        """ID + 类型判定命中
        - the_id 不存在于全局 → False
        - the_type 明确：要求 the_type 在 typed_map[the_id] 里；若 Jellyfin 侧未知该 ID 的类型，降级通配
        - the_type 未知（''/None）：仅要求 ID ∈ global_set
        """
        if not the_id or the_id not in global_set:
            return False
        if not the_type:
            return True
        jf_types = typed_map.get(the_id) or set()
        if not jf_types:
            return True  # Jellyfin 侧这个 ID 所有条目都没类型信息 → 无法排除，判命中
        return the_type in jf_types

    in_lib = set()
    imdb_n = 0
    tmdb_n = 0
    unmatched_local = []
    for m in movies:
        m_imdb = str(m.get('imdb_id') or '').strip()
        m_tmdb = str(m.get('tmdb_id') or '').strip()
        # 本地的类型提示（可选），来自 tv:/movie: 前缀解析
        m_imdb_type = m.get('imdb_media_type') or ''
        m_tmdb_type = m.get('tmdb_media_type') or ''
        hit = False
        if m_imdb and _hit(jf_imdb_typed, jf_imdb_global, m_imdb, m_imdb_type):
            in_lib.add(m.get('url', ''))
            imdb_n += 1
            hit = True
        elif m_tmdb and _hit(jf_tmdb_typed, jf_tmdb_global, m_tmdb, m_tmdb_type):
            in_lib.add(m.get('url', ''))
            tmdb_n += 1
            hit = True
        if not hit:
            unmatched_local.append((m.get('title', ''), m_imdb, m_tmdb, m_tmdb_type))

    # 诊断：Jellyfin 侧未对应任何本地电影的条目
    local_imdb_ids = {str(m.get('imdb_id') or '').strip() for m in movies if str(m.get('imdb_id') or '').strip()}
    local_tmdb_ids = {str(m.get('tmdb_id') or '').strip() for m in movies if str(m.get('tmdb_id') or '').strip()}
    jf_no_id = [it for it in jellyfin_items if not it.get('imdb_id') and not it.get('tmdb_id')]
    jf_unmatched = [
        it for it in jellyfin_items
        if (it.get('imdb_id') and it['imdb_id'] not in local_imdb_ids)
        and (it.get('tmdb_id') and it['tmdb_id'] not in local_tmdb_ids)
    ]

    logger.info(f'[Jellyfin] 匹配完成：IMDb{imdb_n} TMDB{tmdb_n} 共命中{len(in_lib)}/{len(movies)}，'
                f'Jellyfin共{len(jellyfin_items)}条（有ID{len(jellyfin_items)-len(jf_no_id)} 无ID{len(jf_no_id)}），'
                f'Jellyfin侧未对应本地电影{len(jf_unmatched)}条')
    for title, imdb, tmdb, tmdb_type in unmatched_local[:20]:
        logger.info(f'[Jellyfin] 本地未匹配: "{title}" IMDb="{imdb or "无"}" TMDB="{tmdb or "无"}" TMDB类型={tmdb_type or "未指定"}')
    for it in jf_unmatched[:20]:
        logger.info(f'[Jellyfin] Jellyfin侧未对应本地: "{it["title"]}" ({it.get("media_type") or "未知类型"}) IMDb="{it["imdb_id"] or "无"}" TMDB="{it["tmdb_id"] or "无"}"')
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
