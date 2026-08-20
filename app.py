from flask import Flask, render_template, request, redirect, url_for, jsonify, Response, stream_with_context, session
from flask_cors import CORS
import pandas as pd
import os
import json
import secrets
import shutil
import glob as glob_mod
from datetime import datetime, timedelta
import zoneinfo
import threading
import logging
import time
import queue
from typing import List, Dict, Any, Optional, Tuple, Callable, Union
from logging.handlers import RotatingFileHandler

# 加密工具统一入口：避免在多个模块重复实现加密逻辑
from crypto_utils import encrypt, decrypt

import cloud115
import wechat_work
import douban
import transfer_history
import jellyfin
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# 日志配置
LOG_DIR: str = os.environ.get('DATA_DIR', os.path.dirname(os.path.abspath(__file__)))
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE: str = os.path.join(LOG_DIR, 'app.log')

logger = logging.getLogger('115transfer')
logger.setLevel(logging.INFO)

# 实时日志订阅器：维护一个订阅者队列列表，新日志会推送到所有队列
_log_subscribers: List[queue.Queue[str]] = []
_log_subscribers_lock: threading.Lock = threading.Lock()

class _SubscriberLogHandler(logging.Handler):
    """自定义日志处理器：将新日志推送给所有订阅者"""
    def emit(self, record: logging.LogRecord) -> None:
        msg: str = self.format(record)
        with _log_subscribers_lock:
            for q in _log_subscribers:
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass  # 队列满则丢弃，避免阻塞

# 文件日志（轮转：单个文件最大10MB，保留5个备份，JSON格式）
fh: RotatingFileHandler = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
fh.setLevel(logging.INFO)
from pythonjsonlogger import jsonlogger
# pythonjsonlogger 3.x 参数名为 json_ensure_ascii；旧版为 ensure_ascii。
# 这里同时兼容：通过自定义 json_serializer 强制中文不转义，所有版本通用。
import json as _stdjson
def _zh_json_serializer(obj, default=None, **kwargs):
    kwargs.pop('ensure_ascii', None)
    return _stdjson.dumps(obj, default=default, ensure_ascii=False, **kwargs)
fh.setFormatter(jsonlogger.JsonFormatter(
    '%(asctime)s %(levelname)s %(message)s',
    json_serializer=_zh_json_serializer,
))
logger.addHandler(fh)

# 控制台日志
ch: logging.StreamHandler = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S'))
logger.addHandler(ch)

# 实时推送日志处理器
sh: _SubscriberLogHandler = _SubscriberLogHandler()
sh.setLevel(logging.INFO)
sh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
logger.addHandler(sh)

# 模块logger接入同一管道：douban等模块的INFO/WARNING此前传播到root
# logger（无handler）被丢弃，诊断日志从未出现在日志文件/页面
for _mod_name in ('douban', 'crypto_utils', 'baidu_forum', 'forum_monitor', 'jellyfin'):
    _mod_logger = logging.getLogger(_mod_name)
    _mod_logger.setLevel(logging.INFO)
    _mod_logger.addHandler(fh)
    _mod_logger.addHandler(ch)
    _mod_logger.addHandler(sh)
    _mod_logger.propagate = False

user_states: Dict[str, Dict[str, Any]] = {}
user_states_lock: threading.Lock = threading.Lock()

def get_beijing_time() -> datetime:
    """获取北京时间"""
    return datetime.now(zoneinfo.ZoneInfo("Asia/Shanghai"))


# ---------- TMDB ID（带类型前缀）工具 ----------
# 存储格式："tv:85436" / "movie:1732766" / "85436"（旧纯数字，无类型）
# 这样不需要新增 Excel 列，向后兼容；同时彻底解决 movie/tv 同 ID 的命名空间冲突。
def parse_tmdb_id(raw) -> Tuple[str, Optional[str]]:
    """解析 TMDB_ID 单元格值 → (纯数字ID, media_type or None)

    例：
        "tv:85436"        → ("85436", "tv")
        "movie:1732766"   → ("1732766", "movie")
        "1732766.0"       → ("1732766", None)   旧浮点格式
        "" / NaN / "N/A"  → ("", None)
    """
    if raw is None:
        return "", None
    try:
        if pd.isna(raw):
            return "", None
    except (TypeError, ValueError):
        pass
    s = str(raw).strip()
    if not s or s == 'N/A':
        return "", None
    if s.lower().startswith('tv:'):
        tid = s[3:].strip()
        if tid.endswith('.0'):
            try:
                tid = str(int(float(tid)))
            except ValueError:
                pass
        return tid, 'tv'
    if s.lower().startswith('movie:'):
        tid = s[6:].strip()
        if tid.endswith('.0'):
            try:
                tid = str(int(float(tid)))
            except ValueError:
                pass
        return tid, 'movie'
    # 旧纯数字/浮点 → 规范化为整数串，类型未知
    try:
        f = float(s)
        if f.is_integer():
            return str(int(f)), None
    except (ValueError, TypeError):
        pass
    return s, None


def compose_tmdb_id(tmdb_id_raw, media_type: Optional[str]) -> str:
    """按 id + 类型拼接 Excel 单元格要存的字符串。"""
    if not tmdb_id_raw:
        return ""
    tid = str(tmdb_id_raw).strip()
    # 去尾 .0
    try:
        f = float(tid)
        if f.is_integer():
            tid = str(int(f))
    except (ValueError, TypeError):
        pass
    if media_type in ('movie', 'tv'):
        return f"{media_type}:{tid}"
    return tid


def tmdb_display(raw) -> str:
    """给前端展示用：返回带类型说明的纯ID或"tv:xxxx"去掉前缀后的数字。
    这里只返回纯ID数字，类型另用 media_type 判断。"""
    tid, _mt = parse_tmdb_id(raw)
    return tid

app = Flask(__name__)

# Flask secret_key：从环境变量读取，生产环境必须设置强随机值
# 开发环境下若未设置，会生成临时随机值（每次重启会话失效）
app.secret_key = os.environ.get('FLASK_SECRET_KEY') or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(days=7)

# Session cookie 安全标志：HTTPS 环境下启用 Secure，Always HttpOnly + SameSite=Lax
_is_https: bool = os.environ.get('HTTPS_ENABLED', '').lower() == 'true' or os.environ.get('FORCE_HTTPS', '').lower() == 'true'
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=_is_https,
)

# ==================== CORS 配置 ====================
allowed_origins: str = os.environ.get('ALLOWED_ORIGINS', '').strip()
if allowed_origins:
    origins: List[str] = [o.strip() for o in allowed_origins.split(',') if o.strip()]
else:
    origins = ['http://localhost:3698', 'http://127.0.0.1:3698']

CORS(app, origins=origins, supports_credentials=True)

DATA_DIR: str = os.environ.get('DATA_DIR', os.path.dirname(os.path.abspath(__file__)))
os.makedirs(DATA_DIR, exist_ok=True)
EXCEL_FILE: str = os.path.join(DATA_DIR, 'movies_data.xlsx')
BACKUP_DIR: str = os.path.join(DATA_DIR, 'backups')
MAX_BACKUPS: int = 10
data_lock: threading.Lock = threading.Lock()
_movie_cache: Dict[str, Any] = {'hash': None, 'data': None}

# ==================== 登录 & CSRF 配置 ====================

# 登录密码：必须从环境变量设置，未设置时使用默认值并记录警告
APP_PASSWORD: str = os.environ.get('APP_PASSWORD') or 'admin123'
# 是否强制要求设置密码（生产环境建议设为 True）
STRICT_PASSWORD: bool = bool(os.environ.get('APP_PASSWORD'))
if not STRICT_PASSWORD:
    logger.warning('[安全] 未设置 APP_PASSWORD 环境变量，使用默认密码 admin123，生产环境请务必修改！')

# 加密密钥安全检查由 crypto_utils._get_key() 负责，首次调用时记录警告

# 公开接口白名单（无需登录、无需 CSRF 验证）
PUBLIC_PATHS: List[str] = [
    '/login',
    '/logout',
    '/health',
    '/static/',
    '/wechat/callback',  # 企业微信回调
    '/wechat/proxy',     # 代理转发
]

# 不需要 CSRF 验证的接口（如企业微信回调）
CSRF_EXEMPT_PATHS: List[str] = [
    '/wechat/callback',
    '/wechat/proxy',
    '/health',
]


def _get_csrf_token() -> str:
    """获取当前会话的 CSRF token，不存在则生成"""
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_urlsafe(32)
    return session['csrf_token']


def _check_csrf() -> Optional[Tuple[Response, int]]:
    """验证 CSRF token，仅对受保护接口生效"""
    if request.method != 'POST':
        return None
    if any(request.path.startswith(p) for p in CSRF_EXEMPT_PATHS):
        return None
    # 登录接口放行（用户还未登录，没有 csrf_token）
    if request.path == '/login':
        return None

    token: Optional[str] = request.form.get('csrf_token') or request.headers.get('X-CSRFToken')
    if not token:
        return jsonify({'success': False, 'message': '缺少CSRF令牌'}), 403
    if token != session.get('csrf_token'):
        return jsonify({'success': False, 'message': 'CSRF令牌无效'}), 403
    return None


def _require_login() -> Optional[Union[Response, Tuple[Response, int]]]:
    """检查登录状态，未登录返回重定向或 401"""
    if any(request.path.startswith(p) for p in PUBLIC_PATHS):
        return None
    if not session.get('logged_in'):
        # POST 请求一律返回 JSON（前端 apiFetch 都是 POST）
        if request.method == 'POST' or \
           request.path.startswith('/api/') or request.is_json or \
           request.headers.get('X-Requested-With') == 'XMLHttpRequest' or \
           request.headers.get('Accept') == 'application/json':
            return jsonify({'success': False, 'message': '请先登录'}), 401
        return redirect(url_for('login', next=request.path))
    return None


@app.before_request
def _security_check() -> Optional[Union[Response, Tuple[Response, int]]]:
    """请求前的安全检查：登录验证 + CSRF 验证"""
    login_resp = _require_login()
    if login_resp is not None:
        return login_resp
    csrf_resp = _check_csrf()
    if csrf_resp is not None:
        return csrf_resp


@app.context_processor
def _inject_csrf_token() -> Dict[str, Callable[[], str]]:
    """向所有模板注入 csrf_token 函数"""
    return {'csrf_token': _get_csrf_token}


# ==================== 登录限速 ====================
# 简单的内存限速：每个IP在5分钟窗口内最多5次失败，成功登录不计数
_login_attempts: Dict[str, List[float]] = {}
_login_attempts_lock: threading.Lock = threading.Lock()
_LOGIN_WINDOW: int = 300  # 5分钟
_LOGIN_MAX_FAILS: int = 5


def _check_login_rate_limit(client_ip: str) -> Tuple[bool, int]:
    """检查登录限速，返回 (是否允许, 剩余尝试次数)"""
    now: float = time.time()
    with _login_attempts_lock:
        attempts = _login_attempts.get(client_ip, [])
        # 清理窗口外的记录
        attempts = [t for t in attempts if now - t < _LOGIN_WINDOW]
        if len(attempts) >= _LOGIN_MAX_FAILS:
            remaining_lock: int = int(_LOGIN_WINDOW - (now - attempts[0]))
            _login_attempts[client_ip] = attempts
            return False, max(remaining_lock, 0)
        _login_attempts[client_ip] = attempts
        return True, _LOGIN_MAX_FAILS - len(attempts)


def _record_login_failure(client_ip: str) -> None:
    """记录一次登录失败"""
    now: float = time.time()
    with _login_attempts_lock:
        attempts = _login_attempts.get(client_ip, [])
        attempts = [t for t in attempts if now - t < _LOGIN_WINDOW]
        attempts.append(now)
        _login_attempts[client_ip] = attempts


def _clear_login_attempts(client_ip: str) -> None:
    """登录成功后清除该IP的失败记录"""
    with _login_attempts_lock:
        _login_attempts.pop(client_ip, None)


def _get_client_ip() -> str:
    """获取客户端真实IP（支持反向代理）"""
    xff = request.headers.get('X-Forwarded-For', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.remote_addr or 'unknown'


# 版本号
VERSION: str = "1.0.0"
try:
    _version_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'VERSION')
    with open(_version_path, 'r') as f:
        VERSION = f.read().strip()
except Exception:
    pass


# ==================== 登录路由 ====================

@app.route('/login', methods=['GET', 'POST'])
def login() -> Union[str, Response]:
    error: Optional[str] = None
    if request.method == 'POST':
        client_ip: str = _get_client_ip()
        allowed, remaining = _check_login_rate_limit(client_ip)
        if not allowed:
            error = f'登录尝试过多，请 {remaining} 秒后重试'
            logger.warning(f'[登录] IP {client_ip} 触发限速')
            return render_template('login.html', error=error, version=VERSION,
                                  strict_password=STRICT_PASSWORD), 429
        password: str = request.form.get('password', '')
        if secrets.compare_digest(password, APP_PASSWORD):
            _clear_login_attempts(client_ip)
            session.clear()  # 清除旧 session 防止固定会话攻击
            session['logged_in'] = True
            session.permanent = True
            _get_csrf_token()  # 立即生成 CSRF token
            logger.info(f'[登录] 用户登录成功 IP={client_ip}')
            next_url: str = request.args.get('next') or url_for('index')
            # 防止开放重定向：只允许相对路径，阻止 //evil.com
            if not next_url.startswith('/') or next_url.startswith('//'):
                next_url = url_for('index')
            return redirect(next_url)
        _record_login_failure(client_ip)
        error = '密码错误'
        logger.warning(f'[登录] 密码错误 IP={client_ip} 剩余尝试={remaining - 1}')
    return render_template('login.html', error=error, version=VERSION,
                          strict_password=STRICT_PASSWORD)


@app.route('/logout', methods=['POST'])
def logout() -> Response:
    session.clear()
    return redirect(url_for('login'))


@app.route('/health')
def health() -> Response:
    return jsonify({'status': 'ok', 'version': VERSION})


# ==================== 待办记事本 ====================
# 服务端存储：跟随账号而非浏览器，换设备待办也在
NOTES_FILE: str = os.path.join(DATA_DIR, 'notes.json')
_notes_lock: threading.Lock = threading.Lock()


def _load_notes() -> List[Dict[str, Any]]:
    """读取待办列表（损坏时重置为空，不影响主流程）"""
    with _notes_lock:
        if os.path.exists(NOTES_FILE):
            try:
                with open(NOTES_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return data
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f'[待办] 文件读取失败，重置为空: {e}')
    return []


def _save_notes(notes: List[Dict[str, Any]]) -> None:
    with _notes_lock:
        try:
            with open(NOTES_FILE, 'w', encoding='utf-8') as f:
                json.dump(notes, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.error(f'[待办] 保存失败: {e}')
            raise


def _normalize_note(note: Any) -> Optional[Dict[str, Any]]:
    """校验单条待办：text 必须是非空字符串且长度合理，done 必须是布尔"""
    if not isinstance(note, dict):
        return None
    text = note.get('text')
    if not isinstance(text, str) or not text.strip() or len(text) > 500:
        return None
    done = note.get('done', False)
    if not isinstance(done, bool):
        done = bool(done)
    return {'text': text.strip(), 'done': done}


@app.route('/api/notes', methods=['GET'])
def notes_get() -> Response:
    return jsonify({'success': True, 'notes': _load_notes()})


@app.route('/api/notes', methods=['POST'])
def notes_save() -> Response:
    """整体保存待办列表（前端本地编辑，失焦/变更时整体提交）"""
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get('notes'), list):
        return jsonify({'success': False, 'message': '数据格式错误'}), 400
    if len(data['notes']) > 200:
        return jsonify({'success': False, 'message': '待办数量超过上限200条'}), 400
    notes: List[Dict[str, Any]] = []
    for note in data['notes']:
        normalized = _normalize_note(note)
        if normalized is not None:
            notes.append(normalized)
    try:
        _save_notes(notes)
        return jsonify({'success': True, 'count': len(notes)})
    except OSError as e:
        return jsonify({'success': False, 'message': f'保存失败: {e}'}), 500


# ==================== Jellyfin 路由 ====================

@app.route('/jellyfin/config', methods=['GET'])
def jellyfin_get_config() -> Response:
    config = jellyfin.load_config()
    # 掩码显示解密后的真 key 末4位（存的是加密串，直接取末位无意义）
    decrypted_key = decrypt(config.get('api_key', ''))
    return jsonify({
        'success': True,
        'base_url': config.get('base_url', ''),
        'api_key_masked': ('****' + decrypted_key[-4:]) if decrypted_key else '',
        'has_api_key': bool(decrypted_key),
        'library_ids': config.get('library_ids', []),
        'configured': bool(config.get('base_url') and decrypted_key),
    })


@app.route('/jellyfin/config', methods=['POST'])
def jellyfin_set_config() -> Response:
    base_url = request.form.get('base_url', '').strip().rstrip('/')
    api_key = request.form.get('api_key', '').strip()
    library_ids = [x.strip() for x in request.form.get('library_ids', '').split(',') if x.strip()]

    if not base_url or not api_key:
        return jsonify({'success': False, 'message': '地址和API Key不能为空'})

    # 掩码哨兵(用户未重新输入)时用已保存的 key 测试且不覆盖；新 key 正常测试并覆盖
    key_unchanged = api_key == '****' or api_key.startswith('****')
    if key_unchanged:
        test_key = decrypt(jellyfin.load_config().get('api_key', ''))
        if not test_key:
            return jsonify({'success': False, 'message': '请重新输入API Key'})
    else:
        test_key = api_key

    # 先测试连接再保存，避免存入错误配置
    ok, msg = jellyfin.test_connection(base_url, test_key)
    if not ok:
        return jsonify({'success': False, 'message': f'连接测试失败: {msg}'})

    def _update(cfg):
        cfg['base_url'] = base_url
        if not key_unchanged:  # 掩码回传时不覆盖
            cfg['api_key'] = encrypt(api_key)
        cfg['library_ids'] = library_ids
    jellyfin.update_config(_update)
    # 保存成功后立即后台刷新入库状态
    import threading as _threading
    _threading.Thread(target=_refresh_jellyfin_status, daemon=True).start()
    return jsonify({'success': True, 'message': f'配置保存成功。{msg}，正在后台比对入库状态'})


@app.route('/jellyfin/libraries', methods=['GET'])
def jellyfin_libraries() -> Response:
    """获取媒体库列表（配置时选择用，传入临时参数实时测试）"""
    base_url = request.args.get('base_url', '').strip().rstrip('/')
    api_key = request.args.get('api_key', '').strip()
    # 未传参或传入掩码(未改)时用已保存配置
    if not base_url or not api_key or api_key == '****' or api_key.startswith('****'):
        config = jellyfin.load_config()
        base_url = base_url or config.get('base_url', '')
        api_key = decrypt(config.get('api_key', ''))
    libs, err = jellyfin.get_libraries(base_url, api_key)
    if err:
        return jsonify({'success': False, 'message': err})
    return jsonify({'success': True, 'libraries': libs})


@app.route('/jellyfin/refresh', methods=['POST'])
def jellyfin_refresh() -> Response:
    """手动刷新入库状态（纯本地IMDb编号比对，同步返回）。

    IMDB_ID 由豆瓣全量同步负责回填，本接口只做本地↔Jellyfin 比对。
    """
    ret = _refresh_jellyfin_status()
    if ret < 0:
        return jsonify({'success': False, 'message': '刷新失败或未配置Jellyfin，请检查配置和日志'})
    return jsonify({'success': True, 'message': f'刷新完成: {ret}部已入库', 'count': ret})


# ==================== Jellyfin 入库状态 ====================

_jellyfin_refresh_lock: threading.Lock = threading.Lock()


def _refresh_jellyfin_status() -> int:
    """拉取Jellyfin库与本地电影比对，写回"已入库"列。返回已入库数量。

    纯本地比对：用 Excel 里已回填的 IMDB_ID 与 Jellyfin 条目 ProviderIds.Imdb 精确匹配。
    不在此抓豆瓣详情页——IMDB_ID 的回填由豆瓣全量同步负责（职责分离）。
    未配置Jellyfin时静默返回-1（不报错，不修改数据）。
    """
    config = jellyfin.load_config()
    if not config.get('base_url', '').strip() or not decrypt(config.get('api_key', '')):
        return -1
    if not _jellyfin_refresh_lock.acquire(blocking=False):
        logger.info('[Jellyfin] 刷新已在进行中，跳过')
        return -1
    try:
        return _jellyfin_match_and_write()
    finally:
        _jellyfin_refresh_lock.release()


def _has_missing_ids(df: pd.DataFrame, col: str) -> bool:
    if col not in df.columns:
        return False
    return any(pd.isna(v) or str(v).strip() == '' for v in df[col])


def _jellyfin_match_and_write() -> int:
    """用 IMDB_ID + TMDB_ID 两个权威编号比对 Jellyfin 并写回"已入库"列。

    优先级：IMDb（豆瓣自动回填）→ TMDB（用户手动识别，国产片无IMDb时兜底）。
    两者都是 ID 精确匹配，零歧义，不做标题模糊。
    """
    with data_lock:
        df = load_movies()
    if df.empty:
        return 0
    movies = []
    for _, r in df.iterrows():
        tmdb_tid, tmdb_mt = parse_tmdb_id(r['TMDB_ID']) if ('TMDB_ID' in r.index and pd.notna(r['TMDB_ID'])) else ('', None)
        imdb_raw = str(r['IMDB_ID']) if 'IMDB_ID' in r.index and pd.notna(r['IMDB_ID']) else ''
        movies.append({
            'title': str(r['电影名']) if pd.notna(r['电影名']) else '',
            'url': str(r['豆瓣链接']) if pd.notna(r['豆瓣链接']) else '',
            'year': '',
            'imdb_id': imdb_raw.strip() if imdb_raw and imdb_raw != 'N/A' else '',
            # TMDB ID 纯数字 + 独立的类型字段 → jellyfin 可做同类型精准确认
            'tmdb_id': tmdb_tid,
            'tmdb_media_type': tmdb_mt or '',
        })
    in_lib = jellyfin.refresh_in_library_status(movies)
    count = 0
    with data_lock:
        df = load_movies()
        for idx, row in df.iterrows():
            url = str(row['豆瓣链接']) if pd.notna(row['豆瓣链接']) else ''
            val = '是' if (url and url in in_lib) else '否'
            if val == '是':
                count += 1
            df.at[idx, '已入库'] = val
        save_movies(df)
    logger.info(f'[Jellyfin] 入库状态刷新完成: {count}/{len(df)}部已入库')
    return count


def _apply_ids_by_url(url_to_values: Dict[str, str], col: str) -> int:
    """按豆瓣链接把某个ID列合并回写到最新 Excel（不覆盖并发写入的其他列）。

    col='IMDB_ID' 或 'TMDB_ID'。Returns: 实际写入的行数。
    """
    if not url_to_values or not col:
        return 0
    with data_lock:
        df = load_movies()
        if df.empty or col not in df.columns or '豆瓣链接' not in df.columns:
            return 0
        n = 0
        for idx, row in df.iterrows():
            url = str(row['豆瓣链接']) if pd.notna(row['豆瓣链接']) else ''
            if url in url_to_values:
                df.at[idx, col] = url_to_values[url]
                n += 1
        if n:
            save_movies(df)
    return n


def _apply_imdb_ids(url_to_imdb: Dict[str, str]) -> int:
    return _apply_ids_by_url(url_to_imdb, 'IMDB_ID')


def _backfill_imdb_ids(df: pd.DataFrame) -> None:
    """逐条访问豆瓣详情页解析 IMDb 编号并回写 IMDB_ID 列。

    由豆瓣全量同步调用（IMDB_ID 回填归属豆瓣同步，与 Jellyfin 入库比对职责分离）。
    串行 + 0.8s 延迟防反爬。已解析的（含 'N/A' 占位）跳过，失败留空下次再试。
    每50条持久化一次防中途丢失。按豆瓣链接合并回写，不覆盖 Excel 其他列。
    """
    try:
        import douban as douban_mod
    except Exception as e:
        logger.warning(f'[豆瓣] 导入豆瓣模块失败，跳过IMDb解析: {e}')
        return
    # 候选：IMDB_ID 为空（不含 'N/A' 占位）且有豆瓣链接的，按URL去重
    seen: set = set()
    urls = []
    for _, r in df.iterrows():
        imdb = r['IMDB_ID'] if 'IMDB_ID' in r.index and pd.notna(r['IMDB_ID']) else ''
        url = str(r['豆瓣链接']) if '豆瓣链接' in r.index and pd.notna(r['豆瓣链接']) else ''
        if url and str(imdb).strip() == '' and url not in seen:
            seen.add(url)
            urls.append(url)
    if not urls:
        return
    logger.info(f'[豆瓣] 开始解析IMDb编号: {len(urls)}个详情页待抓取')
    import time as _time
    url_to_imdb: Dict[str, str] = {}
    resolved = failed = 0
    for n, url in enumerate(urls, 1):
        meta, err = douban_mod.fetch_movie_meta(url)
        if err:
            failed += 1
            if 'Cookie' in err:
                logger.warning(f'[豆瓣] Cookie失效，中止IMDb解析: {err}')
                break
        else:
            imdb_id = meta.get('imdb_id', '')
            # 无IMDb编号置 'N/A' 占位，下次同步跳过，避免反复抓取国产片
            url_to_imdb[url] = imdb_id if imdb_id else 'N/A'
            if imdb_id:
                resolved += 1
        if n % 50 == 0:
            logger.info(f'[豆瓣] IMDb解析进度: {n}/{len(urls)}（成功{resolved}）')
            _apply_imdb_ids(url_to_imdb)
            url_to_imdb = {}
        _time.sleep(0.8)  # 防反爬
    logger.info(f'[豆瓣] IMDb解析完成: 成功{resolved}/{len(urls)}，失败{failed}')
    _apply_imdb_ids(url_to_imdb)  # 写入剩余


def load_movies() -> pd.DataFrame:
    if not os.path.exists(EXCEL_FILE):
        df = pd.DataFrame(columns=['序号', '页码', '电影名', '磁力链接', '保存时间', '豆瓣链接', '已入库', 'IMDB_ID', 'TMDB_ID'])
        df.to_excel(EXCEL_FILE, index=False)
        return df

    # 使用文件 mtime+size 作为缓存键，避免读取整个文件计算哈希
    stat = os.stat(EXCEL_FILE)
    current_sig = f'{stat.st_mtime}:{stat.st_size}'
    if _movie_cache['hash'] == current_sig and _movie_cache['data'] is not None:
        return _movie_cache['data'].copy()

    df = pd.read_excel(EXCEL_FILE)
    # 兼容旧数据文件：无"豆瓣链接"列时自动补空列（同名电影独立入库的关键）
    if '豆瓣链接' not in df.columns:
        df['豆瓣链接'] = ''
    # 兼容旧数据文件：无"已入库"列时自动补空列（Jellyfin入库状态）
    if '已入库' not in df.columns:
        df['已入库'] = '否'
    # 兼容旧数据文件：无"IMDB_ID"列时自动补空列（Jellyfin入库精确匹配主键）
    if 'IMDB_ID' not in df.columns:
        df['IMDB_ID'] = ''
    # 兼容旧数据文件：无"TMDB_ID"列时自动补空列（手动识别的备用匹配主键，国产片常见无IMDb）
    if 'TMDB_ID' not in df.columns:
        df['TMDB_ID'] = ''
    # 规范化 TMDB_ID：支持 "tv:85436" / "movie:xxx" 前缀格式；旧浮点 1732766.0 → 1732766
    def _norm_tmdb(v):
        if pd.isna(v):
            return ''
        s = str(v).strip()
        if not s or s == 'N/A':
            return s if s == 'N/A' else ''
        # 走 parse → 再 compose，保证前缀统一、尾部 .0 被去掉
        tid, mt = parse_tmdb_id(s)
        if not tid:
            return '' if s != 'N/A' else 'N/A'
        return compose_tmdb_id(tid, mt)
    df['TMDB_ID'] = df['TMDB_ID'].apply(_norm_tmdb)
    # 过滤掉重复的表头行（序号列为非数字的行）
    if not df.empty:
        try:
            pd.to_numeric(df['序号'], errors='raise')
        except (ValueError, TypeError):
            # 序号列有非数字行，过滤掉
            df = df[pd.to_numeric(df['序号'], errors='coerce').notna()].reset_index(drop=True)
    _movie_cache['hash'] = current_sig
    _movie_cache['data'] = df
    return df.copy()

def backup_movies() -> None:
    if not os.path.exists(EXCEL_FILE):
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp: str = get_beijing_time().strftime('%Y%m%d_%H%M%S')
    backup_file: str = os.path.join(BACKUP_DIR, f'movies_data_{timestamp}.xlsx')
    shutil.copy2(EXCEL_FILE, backup_file)
    backups: List[str] = sorted(glob_mod.glob(os.path.join(BACKUP_DIR, 'movies_data_*.xlsx')))
    while len(backups) > MAX_BACKUPS:
        os.remove(backups.pop(0))

def save_movies(df: pd.DataFrame) -> None:
    # 写盘前强制把 TMDB_ID / IMDB_ID 转为文本，防止 pandas 存为数值导致下次读回变成 xxx.0
    for col in ('TMDB_ID', 'IMDB_ID'):
        if col in df.columns:
            def _norm_col(v):
                if pd.isna(v):
                    return ''
                s = str(v).strip()
                if not s:
                    return ''
                if col == 'TMDB_ID' and s != 'N/A':
                    tid, mt = parse_tmdb_id(s)
                    if not tid:
                        return ''
                    return compose_tmdb_id(tid, mt)
                if col == 'IMDB_ID' and s != 'N/A':
                    try:
                        f = float(s)
                        if f.is_integer():
                            return str(int(f))
                    except (ValueError, TypeError):
                        pass
                return s
            df[col] = df[col].apply(_norm_col)
    backup_movies()
    df.to_excel(EXCEL_FILE, index=False)
    _movie_cache['hash'] = None
    _movie_cache['data'] = None

def build_movie_list(df: pd.DataFrame) -> List[Dict[str, Any]]:
    movies: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        magnet = row['磁力链接']
        if pd.isna(magnet) or str(magnet).strip() == '':
            magnet_display: str = '(空)'
            magnet = ''
            is_empty: bool = True
        else:
            magnet = str(magnet)
            magnet_display = magnet[:50] + '...' if len(magnet) > 50 else magnet
            is_empty = False
        
        movies.append({
            'id': row['序号'],
            'page': row['页码'],
            'name': str(row['电影名']) if not pd.isna(row['电影名']) else '',
            'magnet': magnet,
            'magnet_display': magnet_display,
            'is_empty': is_empty,
            'save_time': row['保存时间'],
            'douban_url': str(row['豆瓣链接']) if '豆瓣链接' in row.index and not pd.isna(row['豆瓣链接']) else '',
            'in_library': ('是' == str(row['已入库'])) if '已入库' in row.index and not pd.isna(row['已入库']) else False,
        })
    return movies

@app.route('/')
def index() -> str:
    page_num = request.args.get('page', 1)
    try:
        page_num = int(page_num)
    except ValueError:
        page_num = 1

    try:
        with data_lock:
            df = load_movies()

        if df.empty:
            return render_template('index.html', movies=[], current_page=0, all_page_nums=[], version=VERSION,
                                  stats={'total': 0, 'pages': 0, 'filled': 0, 'empty': 0, 'complete_rate': 0})

        all_page_nums = sorted(df['页码'].unique())

        if page_num not in all_page_nums:
            if all_page_nums:
                page_num = all_page_nums[0]
            else:
                page_num = 0

        page_df = df[df['页码'] == page_num]
        movies = build_movie_list(page_df)

        # 计算统计数据
        total = len(df)
        page_count = len(all_page_nums)
        filled = 0
        empty = 0
        for _, row in df.iterrows():
            m = row['磁力链接']
            if pd.isna(m) or str(m).strip() == '':
                empty += 1
            else:
                filled += 1
        complete_rate = round(filled / total * 100, 1) if total > 0 else 0
        stats = {
            'total': total,
            'pages': page_count,
            'filled': filled,
            'empty': empty,
            'complete_rate': complete_rate,
        }

        return render_template('index.html',
                              movies=movies,
                              current_page=page_num,
                              all_page_nums=all_page_nums,
                              version=VERSION,
                              stats=stats)
    except Exception as e:
        logger.error(f'[首页] 加载数据失败: {e}', exc_info=True)
        # 返回500状态码，便于前端识别错误，而不是返回200的"成功"页面
        return render_template('index.html', movies=[], current_page=0, all_page_nums=[], version=VERSION,
                              stats={'total': 0, 'pages': 0, 'filled': 0, 'empty': 0, 'complete_rate': 0},
                              error='加载数据失败，请检查日志'), 500

@app.route('/search')
def search():
    keyword = request.args.get('keyword', '')

    try:
        with data_lock:
            df = load_movies()

        if not keyword or df.empty:
            return redirect(url_for('index'))

        mask = df['电影名'].astype(str).str.lower().str.contains(keyword.lower(), na=False, regex=False)
        result_df = df[mask]
        movies = build_movie_list(result_df)

        return render_template('search.html', movies=movies, keyword=keyword, version=VERSION)
    except Exception as e:
        logger.error(f'[搜索] 失败 keyword={keyword}: {e}', exc_info=True)
        # 显示错误信息而非静默重定向，便于用户排查
        return render_template('search.html', movies=[], keyword=keyword, version=VERSION,
                              error=f'搜索失败，请重试'), 500

# 注：原 /add 手动添加电影接口已移除，电影列表严格同步豆瓣"看过"列表

@app.route('/delete/<int:movie_id>', methods=['POST'])
def delete_movie(movie_id):
    page = request.args.get('page', type=int)
    # 豆瓣URL精确定位（同名电影独立入库后，(序号,页码)可能撞车）
    url_param = (request.args.get('url', '') or request.form.get('url', '')).strip()
    try:
        with data_lock:
            df = load_movies()

            if url_param and '豆瓣链接' in df.columns:
                mask = df['豆瓣链接'].astype(str) == url_param
            elif page is not None:
                mask = (df['序号'] == movie_id) & (df['页码'] == page)
            else:
                mask = df['序号'] == movie_id

            if not mask.any():
                return jsonify({'success': False, 'message': '电影记录不存在'})
            
            df = df[~mask]
            for pg in df['页码'].unique():
                m = df['页码'] == pg
                df.loc[m, '序号'] = range(1, m.sum() + 1)
            save_movies(df)
        
        return jsonify({'success': True, 'message': '删除成功'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'删除失败: {str(e)}'})

@app.route('/update/<int:movie_id>', methods=['POST'])
def update_movie(movie_id):
    page = request.form.get('page', '').strip()
    name = request.form.get('name')
    magnet = request.form.get('magnet')
    # 豆瓣URL精确定位（同名电影独立入库后，(序号,页码)可能撞车）
    url_param = request.form.get('url', '').strip()

    try:
        with data_lock:
            df = load_movies()

            if not page and not url_param:
                return jsonify({'success': False, 'message': '页码不能为空'})

            try:
                page_int = int(page) if page else None
            except (ValueError, TypeError):
                return jsonify({'success': False, 'message': '页码必须是数字'})

            if url_param and '豆瓣链接' in df.columns:
                mask = df['豆瓣链接'].astype(str) == url_param
            elif page_int is not None:
                mask = (df['序号'] == movie_id) & (df['页码'] == page_int)
            else:
                mask = df['序号'] == movie_id
            if not mask.any():
                return jsonify({'success': False, 'message': '电影记录不存在'})
            
            if name is not None:
                df.loc[mask, '电影名'] = name
            if magnet is not None:
                df.loc[mask, '磁力链接'] = magnet
            df.loc[mask, '保存时间'] = get_beijing_time().strftime('%Y-%m-%d %H:%M:%S')
            
            save_movies(df)
        
        return jsonify({'success': True, 'message': '更新成功'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'更新失败: {str(e)}'})

@app.route('/reorder', methods=['POST'])
def reorder_movies():
    order = request.form.get('order', '')
    page = request.form.get('page', '')
    
    if not order or not page:
        return jsonify({'success': False, 'message': '排序数据不完整'})
    
    try:
        id_list = [int(x) for x in order.split(',')]
    except (ValueError, TypeError):
        return jsonify({'success': False, 'message': '排序数据格式错误'})
    
    try:
        page_num = int(page)
        with data_lock:
            df = load_movies()
            
            if df.empty:
                return jsonify({'success': False, 'message': '没有电影数据'})
            
            page_df = df[df['页码'] == page_num]
            other_df = df[df['页码'] != page_num]
            
            id_to_data = {}
            for _, row in page_df.iterrows():
                id_to_data[row['序号']] = row.to_dict()
            
            if not all(mid in id_to_data for mid in id_list):
                return jsonify({'success': False, 'message': '排序数据包含无效记录'})
            
            reordered_rows = [id_to_data[mid] for mid in id_list]
            for idx, row_data in enumerate(reordered_rows, 1):
                row_data['序号'] = idx
            
            reordered = pd.DataFrame(reordered_rows)
            df = pd.concat([other_df, reordered], ignore_index=True)
            save_movies(df)
        
        return jsonify({'success': True, 'message': '排序已保存'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'排序失败: {str(e)}'})

@app.route('/copy_magnet/<int:movie_id>/<int:page>')
def copy_magnet(movie_id, page):
    try:
        with data_lock:
            df = load_movies()
        
        row = df[(df['序号'] == movie_id) & (df['页码'] == page)]
        
        if not row.empty:
            magnet = row.iloc[0]['磁力链接']
            if not pd.isna(magnet) and str(magnet).strip() != '':
                return jsonify({'success': True, 'magnet': str(magnet)})
        
        return jsonify({'success': False, 'message': '磁力链接为空'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'获取失败: {str(e)}'})


@app.route('/cloud115/config', methods=['GET'])
def cloud115_get_config():
    try:
        config = cloud115.load_config()
        cookie = config.get('cookie', '')
        # Cookie 掩码：始终只显示前后各4位，防止短Cookie泄露明文
        if cookie:
            if len(cookie) > 16:
                masked = cookie[:4] + '****' + cookie[-4:]
            else:
                masked = '****'
        else:
            masked = ''
        return jsonify({'success': True, 'cookie_masked': masked, 'has_cookie': bool(cookie)})
    except Exception as e:
        logger.error(f'[115] 加载配置失败: {e}')
        return jsonify({'success': False, 'message': '加载配置失败'}), 500


@app.route('/cloud115/config', methods=['POST'])
def cloud115_set_config():
    try:
        cookie = request.form.get('cookie', '').strip()
        if not cookie:
            return jsonify({'success': False, 'message': 'Cookie不能为空'})

        def _update(cfg):
            cfg['cookie'] = encrypt(cookie)
        cloud115.update_config(_update)
        cloud115.invalidate_cookie_cache()
        return jsonify({'success': True, 'message': 'Cookie保存成功'})
    except Exception as e:
        logger.error(f'[115] 保存配置失败: {e}')
        return jsonify({'success': False, 'message': '保存配置失败'}), 500


@app.route('/cloud115/verify', methods=['POST'])
def cloud115_verify():
    try:
        success, msg = cloud115.verify_cookie()
        return jsonify({'success': success, 'message': msg})
    except Exception as e:
        logger.error(f'[115] 验证Cookie失败: {e}')
        return jsonify({'success': False, 'message': f'操作失败: {str(e)}'}), 500


@app.route('/cloud115/transfer/<int:movie_id>/<int:page>', methods=['POST'])
def cloud115_transfer(movie_id, page):
    try:
        with data_lock:
            df = load_movies()

        row = df[(df['序号'] == movie_id) & (df['页码'] == page)]
        if row.empty:
            return jsonify({'success': False, 'message': '电影记录不存在'})

        movie_name = row.iloc[0]['电影名']
        magnet = row.iloc[0]['磁力链接']
        if pd.isna(magnet) or str(magnet).strip() == '':
            return jsonify({'success': False, 'message': '磁力链接为空'})

        # 获取或创建页码子目录
        save_path = get_or_create_page_dir(page)
        success, msg = cloud115.add_offline_task(str(magnet), save_path_id=save_path)
        dir_info = f'（保存到: 第{page}页）' if save_path else ''
        if success:
            logger.info(f'[转存] 成功: {movie_name} (ID:{movie_id}, 页:{page})')
        else:
            logger.warning(f'[转存] 失败: {movie_name} - {msg}')
        # 记录转存历史
        try:
            transfer_history.add_record(
                movie_id=movie_id, page=page, movie_name=str(movie_name),
                magnet=str(magnet), success=success,
                message=msg + dir_info, source='single'
            )
        except Exception as hist_err:
            logger.error(f'[转存历史] 记录失败: {hist_err}')
        return jsonify({'success': success, 'message': msg + dir_info})
    except Exception as e:
        logger.error(f'[转存] 异常: {str(e)}')
        return jsonify({'success': False, 'message': f'转存失败: {str(e)}'})


def get_or_create_page_dir(page_num):
    """获取或创建页码子目录，返回目录cid"""
    root_cid, root_name = cloud115.get_default_save_path()
    if not root_cid or root_cid == '0':
        return None  # 使用根目录

    dir_name = f'第{page_num}页'
    # 先查找是否已存在
    success, msg, items = cloud115.list_files(root_cid, show_dir=1)
    if success:
        for item in items:
            if item.get('type') == 'dir' and item.get('name') == dir_name:
                return item['cid']

    # 不存在则创建
    success, msg = cloud115.create_dir(root_cid, dir_name)
    if success:
        success2, msg2, items = cloud115.list_files(root_cid, show_dir=1)
        if success2:
            for item in items:
                if item.get('type') == 'dir' and item.get('name') == dir_name:
                    return item['cid']
    return None


@app.route('/cloud115/batch_transfer', methods=['POST'])
def cloud115_batch_transfer():
    page = request.form.get('page', '')

    if not page:
        return jsonify({'success': False, 'message': '未指定页码'})

    try:
        page_num = int(page)
    except (ValueError, TypeError):
        return jsonify({'success': False, 'message': '页码格式错误'})

    # 获取或创建页码子目录
    save_path = get_or_create_page_dir(page_num)

    try:
        with data_lock:
            df = load_movies()

        if df.empty:
            return jsonify({'success': False, 'message': '没有电影数据'})

        page_df = df[df['页码'] == page_num]
        magnets = []
        movie_map = {}  # magnet -> {movie_id, movie_name}
        for _, row in page_df.iterrows():
            magnet = row['磁力链接']
            if not pd.isna(magnet) and str(magnet).strip() != '':
                m = str(magnet)
                magnets.append(m)
                movie_map[m] = {
                    'movie_id': row['序号'],
                    'movie_name': str(row['电影名']) if not pd.isna(row['电影名']) else '',
                }

        if not magnets:
            return jsonify({'success': False, 'message': '当前页没有有效的磁力链接'})

        results = cloud115.batch_add_offline_tasks(magnets, save_path_id=save_path)
        success_count = sum(1 for r in results if r['success'])
        fail_count = len(results) - success_count
        dir_info = f'（保存到: 第{page_num}页）' if save_path else ''
        logger.info(f'[批量转存] 页码:{page_num}, 成功:{success_count}, 失败:{fail_count}')
        # 记录转存历史
        try:
            transfer_history.add_batch_records(results, page=page_num, movie_map=movie_map, source='batch')
        except Exception as hist_err:
            logger.error(f'[转存历史] 批量记录失败: {hist_err}')
        return jsonify({
            'success': True,
            'message': f'批量转存完成: 成功 {success_count}, 失败 {fail_count}{dir_info}',
            'results': results
        })
    except Exception as e:
        return jsonify({'success': False, 'message': f'批量转存失败: {str(e)}'})


@app.route('/cloud115/tasks', methods=['GET'])
def cloud115_tasks():
    try:
        page = request.args.get('page', 1)
        try:
            page = int(page)
        except (ValueError, TypeError):
            page = 1
        success, msg, tasks = cloud115.get_task_list(page)
        return jsonify({'success': success, 'message': msg, 'tasks': tasks})
    except Exception as e:
        logger.error(f'[115] 获取任务列表失败: {e}')
        return jsonify({'success': False, 'message': f'操作失败: {str(e)}'}), 500


@app.route('/cloud115/dirs', methods=['GET'])
def cloud115_dirs():
    try:
        cid = request.args.get('cid', '0')
        success, msg, dirs = cloud115.get_dir_list(cid)
        return jsonify({'success': success, 'message': msg, 'dirs': dirs})
    except Exception as e:
        logger.error(f'[115] 获取目录列表失败: {e}')
        return jsonify({'success': False, 'message': f'操作失败: {str(e)}'}), 500


@app.route('/cloud115/save_path', methods=['GET'])
def cloud115_get_save_path():
    try:
        path_id, path_name = cloud115.get_default_save_path()
        return jsonify({'success': True, 'path_id': path_id, 'path_name': path_name})
    except Exception as e:
        logger.error(f'[115] 获取保存路径失败: {e}')
        return jsonify({'success': False, 'message': f'操作失败: {str(e)}'}), 500


@app.route('/cloud115/save_path', methods=['POST'])
def cloud115_set_save_path():
    try:
        path_id = request.form.get('path_id', '').strip() or '0'
        path_name = request.form.get('path_name', '').strip() or None
        cloud115.set_default_save_path(path_id, path_name)
        return jsonify({'success': True, 'message': '默认保存目录已更新'})
    except Exception as e:
        logger.error(f'[115] 设置保存路径失败: {e}')
        return jsonify({'success': False, 'message': f'操作失败: {str(e)}'}), 500


@app.route('/wechat/config', methods=['GET'])
def wechat_get_config():
    try:
        config = wechat_work.load_config()
        return jsonify({
            'success': True,
            'corpid': config.get('corpid', ''),
            'agentid': config.get('agentid', ''),
            'token': config.get('token', ''),
            'encoding_aes_key': config.get('encoding_aes_key', ''),
            'callback_url': config.get('callback_url', ''),
            'proxy_url': config.get('proxy_url', ''),
            'configured': bool(config.get('corpid') and config.get('corpsecret'))
        })
    except Exception as e:
        logger.error(f'[微信] 加载配置失败: {e}')
        return jsonify({'success': False, 'message': f'操作失败: {str(e)}'}), 500


@app.route('/wechat/config', methods=['POST'])
def wechat_set_config():
    try:
        corpid = request.form.get('corpid', '').strip()
        corpsecret = request.form.get('corpsecret', '').strip()
        agentid = request.form.get('agentid', '').strip()
        token = request.form.get('token', '').strip()
        encoding_aes_key = request.form.get('encoding_aes_key', '').strip()
        callback_url = request.form.get('callback_url', '').strip()
        proxy_url = request.form.get('proxy_url', '').strip()
        if not corpid or not corpsecret:
            return jsonify({'success': False, 'message': '企业ID和应用Secret不能为空'})

        def _update(cfg):
            cfg['corpid'] = corpid
            cfg['corpsecret'] = encrypt(corpsecret)
            if agentid:
                cfg['agentid'] = agentid
            if token:
                cfg['token'] = token
            if encoding_aes_key:
                cfg['encoding_aes_key'] = encoding_aes_key
            if callback_url:
                cfg['callback_url'] = callback_url
            if proxy_url:
                cfg['proxy_url'] = proxy_url
            cfg.pop('access_token', None)
        wechat_work.update_config(_update)
        return jsonify({'success': True, 'message': '企业微信配置保存成功'})
    except Exception as e:
        logger.error(f'[微信] 保存配置失败: {e}')
        return jsonify({'success': False, 'message': f'操作失败: {str(e)}'}), 500


@app.route('/wechat/callback', methods=['GET', 'POST'])
def wechat_callback():
    config = wechat_work.load_config()
    token = config.get('token', '')
    encoding_aes_key = config.get('encoding_aes_key', '')
    corpid = config.get('corpid', '')

    msg_signature = request.args.get('msg_signature', request.args.get('signature', ''))
    timestamp = request.args.get('timestamp', '')
    nonce = request.args.get('nonce', '')
    echostr = request.args.get('echostr', '')

    logger.info(f'[WeChat Callback] token_configured={bool(token)}, has_signature={bool(msg_signature)}, method={request.method}')

    if not token:
        return '未配置企业微信', 500

    crypto = wechat_work.WeChatCrypto(token, encoding_aes_key, corpid) if encoding_aes_key else None

    if request.method == 'GET':
        if not msg_signature or not timestamp or not nonce:
            return 'success'
        if crypto:
            is_valid = crypto.verify_signature(msg_signature, timestamp, nonce, echostr)
            logger.info(f'[WeChat Callback] Signature valid: {is_valid}')
            if is_valid:
                try:
                    decrypted, _ = crypto.decrypt_message(echostr)
                    logger.info(f'[WeChat Callback] Decrypted echostr')
                    return decrypted
                except Exception as e:
                    logger.error(f'[WeChat Callback] Decrypt error: {e}')
                    return echostr
        return '签名验证失败', 403

    try:
        if crypto:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(request.data)
            encrypt_elem = root.find('Encrypt')
            encrypt_content = encrypt_elem.text if encrypt_elem is not None else ''
            logger.info(f'[WeChat Callback] Encrypt content received')
            if not crypto.verify_signature(msg_signature, timestamp, nonce, encrypt_content):
                logger.warning('[WeChat Callback] POST signature verification failed')
                return '签名验证失败', 403
            if encrypt_elem is not None:
                decrypted, from_user = crypto.decrypt_message(encrypt_elem.text)
                msg = wechat_work.parse_message(decrypted)
            else:
                msg = wechat_work.parse_message(request.data)
        else:
            msg = wechat_work.parse_message(request.data)

        if not msg:
            return 'success'

        msg_type = msg.get('MsgType', '')
        from_user = msg.get('FromUserName', '')
        to_user = msg.get('ToUserName', '')

        if msg_type == 'text':
            content = msg.get('Content', '').strip()
            state = user_states.get(from_user)  # 读取操作，Dict 读取在 CPython 中线程安全（GIL）

            if content.lower().startswith('magnet:'):
                cookie = cloud115.get_cookie_string()
                if not cookie:
                    reply = '未配置115 Cookie，请先在网页端配置'
                    wechat_work.send_wechat_message('[115Transfer] 转存失败: 未配置115 Cookie，请及时处理')
                else:
                    success, msg_text = cloud115.add_offline_task(content)
                    reply = f'转存结果: {msg_text}'
                    if not success:
                        wechat_work.send_wechat_message(f'[115Transfer] 转存失败: {msg_text}\n磁力链接: {content[:50]}...')
                    # 记录转存历史
                    try:
                        transfer_history.add_record(
                            movie_id='', page='', movie_name='', magnet=content,
                            success=success, message=msg_text, source='wechat'
                        )
                    except Exception:
                        pass
            elif content.lower() in ['帮助', 'help', '?']:
                if state:
                    if state['action'] == 'batch_transfer':
                        reply = '当前状态: 批量转存\n\n回复页码 - 将该页所有磁力链接转存到115\n回复"取消" - 退出批量转存'
                    elif state['action'] == 'browse_dir':
                        reply = '当前状态: 目录浏览\n\n回复序号 - 进入子目录\n回复"确认" - 设置为转存目录\n回复"新建" - 创建新目录\n回复"返回" - 回到上级目录\n回复"取消" - 退出目录浏览'
                    elif state['action'] == 'create_dir_name':
                        reply = '当前状态: 创建目录\n\n输入新目录名 - 创建目录\n回复"取消" - 取消创建'
                    else:
                        reply = '回复"帮助"查看使用说明'
                else:
                    reply = ('使用方法:\n'
                             '页码 - 查看该页电影\n'
                             '搜索 电影名 - 搜索本地电影记录\n'
                             '磁力链接 - 转存到115网盘\n'
                             '取消 - 退出当前操作\n\n'
                             '论坛监控:\n'
                             '增量 - 启动增量监控（爬新帖）\n'
                             '全量 - 启动全量拉取（耗时较长）\n'
                             '进度 - 查看监控进度\n'
                             '取消增量 / 取消全量 - 停止对应任务\n\n'
                             '菜单功能:\n'
                             '查看电影 - 浏览电影列表\n'
                             '批量转存 - 批量转存到115\n'
                             '论坛进度 - 查看监控状态\n'
                             '增量拉取 - 一键启动增量监控\n'
                             '目录 - 管理115网盘目录\n\n'
                             '电影列表由豆瓣同步管理，\n'
                             '请到网页端"豆瓣"页面同步观影记录')
            elif content == '取消':
                if state:
                    with user_states_lock:
                        user_states.pop(from_user, None)
                    reply = '已取消操作'
                else:
                    reply = '没有正在进行的操作'
            elif state and state['action'] == 'batch_transfer':
                if content.isdigit():
                    page_num = int(content)
                    with data_lock:
                        df = load_movies()
                    page_df = df[df['页码'] == page_num]
                    if page_df.empty:
                        reply = f'第 {page_num} 页没有电影'
                    else:
                        magnets = []
                        for _, row in page_df.iterrows():
                            magnet = row.get('磁力链接', '')
                            if not pd.isna(magnet) and str(magnet).strip():
                                magnets.append(str(magnet))
                        if not magnets:
                            reply = f'第 {page_num} 页没有有效的磁力链接'
                        else:
                            save_path = get_or_create_page_dir(page_num)
                            results = cloud115.batch_add_offline_tasks(magnets, save_path_id=save_path)
                            success_count = sum(1 for r in results if r['success'])
                            fail_count = len(results) - success_count
                            dir_info = f'（保存到: 第{page_num}页）' if save_path else ''
                            reply = f'批量转存完成{dir_info}\n页码: {page_num}\n成功: {success_count}\n失败: {fail_count}'
                            if fail_count > 0:
                                wechat_work.send_wechat_message(f'[115Transfer] 批量转存部分失败\n页码: {page_num}\n成功: {success_count}\n失败: {fail_count}')
                            # 记录转存历史
                            try:
                                transfer_history.add_batch_records(results, page=page_num, movie_map=None, source='wechat')
                            except Exception:
                                pass
                    with user_states_lock:
                        user_states.pop(from_user, None)
                else:
                    reply = '请输入页码数字'
            elif state and state['action'] == 'browse_dir':
                if content == '确认':
                    cloud115.set_default_save_path(state['cid'], state['path'])
                    reply = f'已设置转存目录: {state["path"]}'
                    with user_states_lock:
                        user_states.pop(from_user, None)
                elif content == '新建':
                    with user_states_lock:
                        if from_user in user_states:
                            user_states[from_user]['action'] = 'create_dir_name'
                    reply = f'在 {state["path"]} 下创建目录\n请输入新目录名:'
                elif content == '返回':
                    if len(state.get('stack', [])) > 1:
                        with user_states_lock:
                            cur = user_states.get(from_user)
                            if cur and cur.get('action') == 'browse_dir':
                                cur['stack'].pop()
                                parent = cur['stack'][-1]
                                cur['cid'] = parent['cid']
                                cur['path'] = parent['path']
                                state = cur
                        success, msg_text, dirs = cloud115.get_dir_list(state['cid'])
                        if success and dirs:
                            reply = f'目录: {state["path"]}\n\n'
                            for i, d in enumerate(dirs, 1):
                                reply += f'{i}. {d["name"]}\n'
                            reply += f'\n回复序号进入子目录\n回复"确认"设置为转存目录\n回复"新建"创建新目录\n回复"返回"回到上级目录'
                        else:
                            reply = f'目录: {state["path"]}\n\n此目录为空\n回复"确认"设置为转存目录\n回复"新建"创建新目录'
                    else:
                        reply = '已经在根目录，无法返回'
                elif content.isdigit():
                    idx = int(content) - 1
                    success, msg_text, dirs = cloud115.get_dir_list(state['cid'])
                    if success and 0 <= idx < len(dirs):
                        d = dirs[idx]
                        with user_states_lock:
                            cur = user_states.get(from_user)
                            if cur and cur.get('action') == 'browse_dir':
                                cur['cid'] = d['cid']
                                cur['path'] = cur['path'] + ' / ' + d['name']
                                cur['stack'].append({'cid': d['cid'], 'path': cur['path']})
                                state = cur
                        success2, msg2, subdirs = cloud115.get_dir_list(d['cid'])
                        if success2 and subdirs:
                            reply = f'目录: {state["path"]}\n\n'
                            for i, sd in enumerate(subdirs, 1):
                                reply += f'{i}. {sd["name"]}\n'
                            reply += f'\n回复序号进入子目录\n回复"确认"设置为转存目录\n回复"新建"创建新目录\n回复"返回"回到上级目录'
                        else:
                            reply = f'目录: {state["path"]}\n\n此目录为空\n回复"确认"设置为转存目录\n回复"新建"创建新目录\n回复"返回"回到上级目录'
                    else:
                        reply = '序号无效，请重新输入'
                else:
                    reply = '请输入序号、"确认"、"新建"或"返回"'
            elif state and state['action'] == 'create_dir_name':
                dir_name = content
                success, msg_text = cloud115.create_dir(state['cid'], dir_name)
                if success:
                    reply = f'目录创建成功: {state["path"]} / {dir_name}'
                else:
                    reply = f'创建失败: {msg_text}'
                with user_states_lock:
                    user_states.pop(from_user, None)
            elif content in ('论坛进度', '监控进度', '进度', 'forum', 'monitor'):
                # 主动查询论坛监控进度
                try:
                    text = forum_monitor.build_progress_report()
                    if text:
                        reply = text
                    else:
                        # 没有任务运行时，返回最近的统计快照
                        dash = forum_monitor.get_dashboard()
                        total = dash.get('total_threads', 0)
                        with_seeds = dash.get('threads_with_seeds', 0)
                        without_seeds = dash.get('threads_without_seeds', 0)
                        total_seeds = dash.get('total_seeds', 0)
                        seed_rate = dash.get('seed_rate', 0)
                        reply = (f'【论坛监控·当前状态】\n'
                                 f'状态: 空闲\n'
                                 f'总帖子: {total} | 有种: {with_seeds} | 无种: {without_seeds}\n'
                                 f'种子文件: {total_seeds} | 覆盖率: {seed_rate}%')
                except Exception as e:
                    reply = f'查询进度失败: {e}'
            elif content in ('增量', '增量拉取', '增量监控', '开始增量', 'incremental'):
                # 触发增量监控（后台异步执行，与主任务独立）
                try:
                    if forum_monitor.get_status().get('incremental', {}).get('running'):
                        reply = '增量监控已在运行，回复"进度"查看详情'
                    else:
                        import threading as _threading
                        t = _threading.Thread(target=forum_monitor.run_incremental, daemon=True)
                        t.start()
                        reply = '增量监控已启动，将爬取各板块最新帖子\n回复"进度"查看实时进度\n回复"取消增量"可停止'
                except Exception as e:
                    reply = f'启动增量监控失败: {e}'
            elif content in ('全量', '全量拉取', '开始全量', 'full'):
                # 触发全量拉取（后台异步执行，与二次拉取互斥）
                try:
                    if forum_monitor.get_status()['running']:
                        reply = '主任务已在运行（全量/二次拉取），回复"进度"查看详情'
                    else:
                        import threading as _threading
                        t = _threading.Thread(target=forum_monitor.run_full_crawl, daemon=True)
                        t.start()
                        reply = '全量拉取已启动，将遍历所有板块所有页面\n耗时较长，回复"进度"查看实时进度\n回复"取消全量"可停止'
                except Exception as e:
                    reply = f'启动全量拉取失败: {e}'
            elif content in ('取消增量', '停止增量'):
                # 仅取消增量监控任务
                try:
                    cancelled = forum_monitor.cancel('incremental')
                    reply = '已请求取消增量监控，任务将在下一次循环检查时停止' if cancelled else '增量监控未在运行'
                except Exception as e:
                    reply = f'取消增量监控失败: {e}'
            elif content in ('取消全量', '停止全量'):
                # 仅取消主任务（全量/二次拉取）
                try:
                    cancelled = forum_monitor.cancel('main')
                    reply = '已请求取消主任务，任务将在下一次循环检查时停止' if cancelled else '当前没有运行中的主任务'
                except Exception as e:
                    reply = f'取消主任务失败: {e}'
            elif content.startswith('搜索') or content.startswith('search'):
                keyword = content[2:].strip() if content.startswith('搜索') else content[6:].strip()
                if not keyword:
                    reply = '请输入搜索关键词\n格式: 搜索 电影名'
                else:
                    with data_lock:
                        df = load_movies()
                    mask = df['电影名'].str.contains(keyword, case=False, na=False, regex=False)
                    results = df[mask]
                    if results.empty:
                        reply = f'未找到包含"{keyword}"的电影'
                    else:
                        reply = f'搜索"{keyword}"找到 {len(results)} 部电影:\n\n'
                        for _, row in results.head(20).iterrows():
                            page = row.get('页码', '?')
                            name = row.get('电影名', '未知')
                            magnet = row.get('磁力链接', '无')
                            reply += f'[{page}页] {name}\n{magnet}\n\n'
                        if len(results) > 20:
                            reply += f'... 还有 {len(results) - 20} 部电影，请访问网页查看'
            elif content.isdigit():
                page_num = int(content)
                with data_lock:
                    df = load_movies()
                page_df = df[df['页码'] == page_num]
                if page_df.empty:
                    reply = f'第 {page_num} 页没有电影'
                else:
                    reply = f'第 {page_num} 页 ({len(page_df)} 部电影):\n\n'
                    for _, row in page_df.iterrows():
                        name = row.get('电影名', '未知')
                        magnet = row.get('磁力链接', '无')
                        reply += f'{name}\n{magnet}\n\n'
            else:
                reply = wechat_work.handle_text_message(content)

            reply = wechat_work.truncate_reply(reply)
            logger.info(f'[WeChat Reply] To: {from_user}, Content: {reply[:50]}')
            if crypto:
                reply_xml = wechat_work.build_reply_xml(from_user, to_user, reply, crypto)
                logger.debug(f'[WeChat Reply] XML generated')
                return reply_xml, 200, {'Content-Type': 'application/xml'}
            else:
                reply_xml = wechat_work.build_reply_xml(from_user, to_user, reply)
                return reply_xml, 200, {'Content-Type': 'application/xml'}

        elif msg_type == 'event':
            event = msg.get('Event', '')
            event_key = msg.get('EventKey', '')
            logger.info(f'[WeChat Event] Type: {event}, Key: {event_key}')

            if event == 'click':
                with user_states_lock:
                    user_states.pop(from_user, None)
                if event_key == 'view_movies':
                    with data_lock:
                        df = load_movies()
                    if df.empty:
                        reply = '暂无电影数据'
                    else:
                        total = len(df)
                        page_list = sorted(df['页码'].unique().tolist())
                        reply = f'共 {total} 部电影，请回复页码查看:\n\n'
                        reply += ' | '.join([str(p) for p in page_list])
                        reply += '\n\n直接回复页码即可查看该页所有电影'
                elif event_key == 'batch_transfer':
                    with data_lock:
                        df = load_movies()
                    if df.empty:
                        reply = '暂无电影数据'
                    else:
                        page_list = sorted(df['页码'].unique().tolist())
                        reply = '批量转存 - 请选择页码:\n\n'
                        reply += ' | '.join([str(p) for p in page_list])
                        reply += '\n\n回复页码，该页所有磁力链接将转存到115网盘'
                        with user_states_lock:
                            user_states[from_user] = {'action': 'batch_transfer'}
                elif event_key == '115_dir':
                    with user_states_lock:
                        user_states[from_user] = {'action': 'browse_dir', 'cid': '0', 'path': '根目录', 'stack': [{'cid': '0', 'path': '根目录'}]}
                    success, msg_text, dirs = cloud115.get_dir_list('0')
                    if success and dirs:
                        reply = '115网盘目录:\n\n'
                        for i, d in enumerate(dirs, 1):
                            reply += f'{i}. {d["name"]}\n'
                        reply += f'\n回复序号进入子目录\n回复"确认"设置为转存目录\n回复"新建"创建新目录'
                    else:
                        reply = f'获取目录失败: {msg_text}'
                elif event_key == 'forum_progress':
                    # 主动查询论坛监控进度
                    try:
                        text = forum_monitor.build_progress_report()
                        if text:
                            reply = text
                        else:
                            dash = forum_monitor.get_dashboard()
                            total = dash.get('total_threads', 0)
                            with_seeds = dash.get('threads_with_seeds', 0)
                            without_seeds = dash.get('threads_without_seeds', 0)
                            total_seeds = dash.get('total_seeds', 0)
                            seed_rate = dash.get('seed_rate', 0)
                            reply = (f'【论坛监控·当前状态】\n'
                                     f'状态: 空闲\n'
                                     f'总帖子: {total} | 有种: {with_seeds} | 无种: {without_seeds}\n'
                                     f'种子文件: {total_seeds} | 覆盖率: {seed_rate}%')
                    except Exception as e:
                        reply = f'查询进度失败: {e}'
                elif event_key == 'forum_incremental':
                    # 菜单按钮：触发增量监控
                    try:
                        if forum_monitor.get_status().get('incremental', {}).get('running'):
                            reply = '增量监控已在运行，回复"进度"查看详情'
                        else:
                            import threading as _threading
                            t = _threading.Thread(target=forum_monitor.run_incremental, daemon=True)
                            t.start()
                            reply = '增量监控已启动，将爬取各板块最新帖子\n回复"进度"查看实时进度\n回复"取消增量"可停止'
                    except Exception as e:
                        reply = f'启动增量监控失败: {e}'
                else:
                    reply = '未知操作'
            elif event == 'subscribe':
                reply = '欢迎使用115Transfer！\n发送"帮助"查看使用方法'
            else:
                return 'success'

            if crypto:
                reply_xml = wechat_work.build_reply_xml(from_user, to_user, reply, crypto)
                return reply_xml, 200, {'Content-Type': 'application/xml'}
            else:
                reply_xml = wechat_work.build_reply_xml(from_user, to_user, reply)
                return reply_xml, 200, {'Content-Type': 'application/xml'}

        return 'success'
    except Exception as e:
        logger.error(f'[WeChat Callback] 处理异常: {type(e).__name__}: {e}', exc_info=True)
        return 'success'


@app.route('/wechat/proxy', methods=['POST'])
def wechat_proxy():
    try:
        # 安全检查：要求共享密钥验证，防止未授权调用消息处理
        proxy_token = os.environ.get('WECHAT_PROXY_TOKEN', '')
        if proxy_token:
            provided = request.headers.get('X-Proxy-Token') or request.form.get('proxy_token') or request.args.get('proxy_token', '')
            if not secrets.compare_digest(provided, proxy_token):
                return jsonify({'success': False, 'message': '未授权访问'}), 403

        content_type = request.content_type or ''

        if 'json' in content_type:
            data = request.get_json(force=True, silent=True) or {}
            content = data.get('content', data.get('text', data.get('msg', '')))
            from_user = data.get('from_user', data.get('user', data.get('from', '')))
        else:
            content = request.form.get('content', request.form.get('text', request.form.get('msg', '')))
            from_user = request.form.get('from_user', request.form.get('user', request.form.get('from', '')))

        if not content:
            content = request.args.get('content', request.args.get('text', ''))
            from_user = from_user or request.args.get('from_user', request.args.get('user', ''))

        if not content:
            return jsonify({'success': False, 'message': '未收到消息内容'}), 400

        result = wechat_work.handle_text_message(content)
        return jsonify({'success': True, 'message': result})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/wechat/test', methods=['POST'])
def wechat_test():
    content = request.form.get('content', '').strip()
    if not content:
        return jsonify({'success': False, 'message': '请输入测试消息'})
    try:
        logger.info(f'[WeChat Test] Received: {content}')
        result = wechat_work.handle_text_message(content)
        return jsonify({'success': True, 'message': result})
    except Exception as e:
        logger.error(f'[微信] 测试消息处理失败: {e}')
        return jsonify({'success': False, 'message': f'操作失败: {str(e)}'}), 500


@app.route('/wechat/menu', methods=['POST'])
def wechat_menu():
    try:
        config = wechat_work.load_config()
        agentid = config.get('agentid', '')
        if not agentid:
            return jsonify({'success': False, 'message': '未配置AgentId'})
        success, msg = wechat_work.create_menu(agentid)
        return jsonify({'success': success, 'message': msg})
    except Exception as e:
        logger.error(f'[微信] 创建菜单失败: {e}')
        return jsonify({'success': False, 'message': f'操作失败: {str(e)}'}), 500


# ==================== 115网盘整理路由 ====================

from media.scanner import scan_115_directory, get_directory_tree
from media.tmdb import identify_media, identify_batch, get_tmdb_api_key, set_tmdb_api_key
from media.tmdb import _load_config as _tmdb_load_config
from media.classifier import classify, get_all_categories
from media.organizer import organize_files


@app.route('/media/organize_root', methods=['GET'])
def media_get_organize_root():
    try:
        cfg = _tmdb_load_config()
        cid = cfg.get('organize_root_cid', '')
        name = cfg.get('organize_root_name', '')
        return jsonify({'success': True, 'cid': cid, 'name': name})
    except Exception as e:
        logger.error(f'[媒体] 获取整理根目录失败: {e}')
        return jsonify({'success': False, 'message': f'操作失败: {str(e)}'}), 500


@app.route('/media/organize_root', methods=['POST'])
def media_set_organize_root():
    try:
        cid = request.form.get('cid', '').strip()
        name = request.form.get('name', '').strip()

        def _update(cfg):
            cfg['organize_root_cid'] = cid
            cfg['organize_root_name'] = name
        cloud115.update_config(_update)
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f'[媒体] 设置整理根目录失败: {e}')
        return jsonify({'success': False, 'message': f'操作失败: {str(e)}'}), 500


@app.route('/media')
def media_page():
    return render_template('media.html', version=VERSION)


@app.route('/media/browse', methods=['GET'])
def media_browse():
    try:
        cid = request.args.get('cid', '0')
        success, msg, items = cloud115.list_files(cid, show_dir=1)
        if success:
            return jsonify({'success': True, 'items': items})
        return jsonify({'success': False, 'message': msg})
    except Exception as e:
        logger.error(f'[媒体] 浏览目录失败: {e}')
        return jsonify({'success': False, 'message': f'操作失败: {str(e)}'}), 500


@app.route('/media/scan', methods=['POST'])
def media_scan():
    try:
        cid = request.form.get('cid', '0').strip()
        recursive = request.form.get('recursive', 'true').lower() == 'true'
        files = scan_115_directory(cid, recursive)
        return jsonify({'success': True, 'files': files, 'count': len(files)})
    except Exception as e:
        logger.error(f'[媒体] 扫描目录失败: {e}')
        return jsonify({'success': False, 'message': f'操作失败: {str(e)}'}), 500


@app.route('/media/identify', methods=['POST'])
def media_identify():
    try:
        name = request.form.get('name', '').strip()
        year = request.form.get('year', '').strip()
        if not name:
            return jsonify({'success': False, 'message': '请输入名称'})
        year_int = int(year) if year.isdigit() else None
        result, err = identify_media(name, year_int)
        if err:
            logger.warning(f'[TMDB] 识别失败: name={name}, year={year_int}, err={err}')
            return jsonify({'success': False, 'message': err})
        primary, secondary = classify(result)
        logger.info(f'[TMDB] 识别成功: name={name} -> {result.get("title")} ({primary}/{secondary})')
        return jsonify({
            'success': True,
            'tmdb': result,
            'primary': primary,
            'secondary': secondary,
        })
    except Exception as e:
        logger.error(f'[媒体] 识别失败: {e}')
        return jsonify({'success': False, 'message': f'操作失败: {str(e)}'}), 500


@app.route('/movies/posters', methods=['POST'])
def movies_posters():
    """批量获取电影海报 URL（用于封面墙视图）

    请求参数:
        names: JSON 字符串，电影名列表 ['电影名1', '电影名2', ...]

    返回:
        {
            'success': True,
            'posters': {'电影名1': {'url': '...', 'year': '2019'}, ...}
        }
    """
    import json as _json
    from media.tmdb import identify_media, get_tmdb_api_key

    # 未配置 TMDB API Key 时直接返回空，避免无效查询
    if not get_tmdb_api_key():
        return jsonify({'success': False, 'message': '未配置TMDB API Key', 'posters': {}})

    names_json = request.form.get('names', '[]')
    try:
        names: List[str] = _json.loads(names_json)
    except (ValueError, TypeError):
        return jsonify({'success': False, 'message': 'names 参数格式错误'}), 400

    if not isinstance(names, list) or len(names) == 0:
        return jsonify({'success': True, 'posters': {}})

    # 限制单次查询数量，避免请求过多
    if len(names) > 50:
        names = names[:50]

    posters: Dict[str, Dict[str, str]] = {}
    try:
        for name in names:
            if not name or not isinstance(name, str):
                continue
            result, err = identify_media(name)
            if result and result.get('poster_path'):
                posters[name] = {
                    'url': f"https://image.tmdb.org/t/p/w300{result['poster_path']}",
                    'year': result.get('year', ''),
                    'rating': str(result.get('vote_average', '')) if result.get('vote_average') else '',
                }
        return jsonify({'success': True, 'posters': posters})
    except Exception as e:
        logger.error(f'[电影海报] 批量获取失败: {e}')
        return jsonify({'success': False, 'message': f'操作失败: {str(e)}'}), 500


@app.route('/media/identify_batch', methods=['POST'])
def media_identify_batch():
    """批量识别媒体（并发）"""
    import json as _json
    items_json = request.form.get('items', '[]')
    try:
        items = _json.loads(items_json)
    except Exception:
        return jsonify({'success': False, 'message': 'items参数格式错误'})

    if not items:
        return jsonify({'success': False, 'message': '无待识别项'})

    try:
        results = identify_batch(items, max_workers=5)
        # 整理结果，附加分类
        out = []
        for r in results:
            if r and r.get('success') and r.get('result'):
                tmdb = r['result']
                primary, secondary = classify(tmdb)
                out.append({
                    'success': True,
                    'tmdb': tmdb,
                    'primary': primary,
                    'secondary': secondary,
                })
            else:
                out.append({
                    'success': False,
                    'error': (r or {}).get('error', '未识别'),
                })
        success_count = sum(1 for x in out if x['success'])
        return jsonify({
            'success': True,
            'results': out,
            'count': len(out),
            'success_count': success_count,
        })
    except Exception as e:
        logger.error(f'[媒体] 批量识别失败: {e}')
        return jsonify({'success': False, 'message': f'操作失败: {str(e)}'}), 500


@app.route('/media/search', methods=['POST'])
def media_search():
    """手动搜索TMDB，返回多个结果供选择。支持TMDB ID直接识别。"""
    try:
        query = request.form.get('query', '').strip()
        year = request.form.get('year', '').strip()
        if not query:
            return jsonify({'success': False, 'message': '请输入搜索名称'})
        year_int = int(year) if year.isdigit() else None

        # 如果输入是纯数字，当作TMDB ID直接查询
        if query.isdigit():
            from media.tmdb import get_media_by_id
            result, err = get_media_by_id(int(query))
            if result:
                primary, secondary = classify(result)
                result['primary'] = primary
                result['secondary'] = secondary
                return jsonify({'success': True, 'results': [result], 'count': 1})
            # ID查询失败，继续搜索
            pass

        from media.tmdb import search_multi
        results, err = search_multi(query, year=year_int)
        if err:
            return jsonify({'success': False, 'message': err})
        # 取前10个结果，补充详情
        output = []
        for r in results[:10]:
            media_type = r.get('media_type', 'movie')
            item = {
                'tmdb_id': r.get('id'),
                'media_type': media_type,
                'title': r.get('title') or r.get('name', ''),
                'original_title': r.get('original_title') or r.get('original_name', ''),
                'year': (r.get('release_date') or r.get('first_air_date') or '')[:4],
                'genres': [],
                'genre_ids': r.get('genre_ids', []),
                'original_language': r.get('original_language', ''),
                'production_countries': [],
                'poster_path': r.get('poster_path', ''),
                'vote_average': r.get('vote_average', 0),
            }
            primary, secondary = classify(item)
            item['primary'] = primary
            item['secondary'] = secondary
            output.append(item)
        return jsonify({'success': True, 'results': output, 'count': len(output)})
    except Exception as e:
        logger.error(f'[媒体] 搜索失败: {e}')
        return jsonify({'success': False, 'message': f'操作失败: {str(e)}'}), 500


@app.route('/media/organize', methods=['POST'])
def media_organize():
    try:
        data = request.get_json(force=True, silent=True) or {}
        file_list = data.get('files', [])
        root_cid = data.get('root_cid', '0')
        source_cid = data.get('source_cid', '0')
        if not file_list:
            return jsonify({'success': False, 'message': '没有要整理的文件'})
        logger.info(f'[整理] 开始整理 {len(file_list)} 个文件, 根目录:{root_cid}')
        results = organize_files(file_list, root_cid, source_cid)
        success_count = len(results["success"])
        fail_count = len(results["failed"])
        logger.info(f'[整理] 完成: 成功 {success_count}, 失败 {fail_count}')
        if fail_count > 0:
            for f in results["failed"][:5]:
                logger.warning(f'[整理] 失败: {f.get("name", "")} - {f.get("reason", "")}')
        return jsonify({
            'success': True,
            'message': f'整理完成: 成功 {success_count} 个, 失败 {fail_count} 个',
            'results': results,
        })
    except Exception as e:
        logger.error(f'[媒体] 整理失败: {e}')
        return jsonify({'success': False, 'message': f'操作失败: {str(e)}'}), 500


@app.route('/media/tmdb_key', methods=['GET'])
def media_get_tmdb_key():
    try:
        return jsonify({'success': True, 'key': get_tmdb_api_key()})
    except Exception as e:
        logger.error(f'[媒体] 获取TMDB Key失败: {e}')
        return jsonify({'success': False, 'message': f'操作失败: {str(e)}'}), 500


@app.route('/media/tmdb_key', methods=['POST'])
def media_set_tmdb_key():
    try:
        key = request.form.get('key', '').strip()
        set_tmdb_api_key(key)
        return jsonify({'success': True, 'message': 'TMDB API Key 已保存'})
    except Exception as e:
        logger.error(f'[媒体] 设置TMDB Key失败: {e}')
        return jsonify({'success': False, 'message': f'操作失败: {str(e)}'}), 500


@app.route('/media/categories', methods=['GET'])
def media_categories():
    try:
        categories = get_all_categories()
        return jsonify({'success': True, 'categories': categories})
    except Exception as e:
        logger.error(f'[媒体] 获取分类失败: {e}')
        return jsonify({'success': False, 'message': f'操作失败: {str(e)}'}), 500


@app.route('/media/tree', methods=['GET'])
def media_tree():
    try:
        cid = request.args.get('cid', '0')
        try:
            depth = int(request.args.get('depth', '3'))
        except ValueError:
            depth = 3
        tree = get_directory_tree(cid, depth)
        return jsonify({'success': True, 'tree': tree})
    except Exception as e:
        logger.error(f'[媒体] 获取目录树失败: {e}')
        return jsonify({'success': False, 'message': f'操作失败: {str(e)}'}), 500


# ===== 论坛电影搜索 =====

import baidu_forum


@app.route('/baidu')
def baidu_page():
    return render_template('baidu.html', version=VERSION)


@app.route('/baidu/config', methods=['GET', 'POST'])
def baidu_config():
    if request.method == 'GET':
        try:
            config = baidu_forum.load_config()
            # 不返回密码明文，前端只需知道是否已配置
            return jsonify({
                'success': True,
                'config': {
                    'username': config.get('username', ''),
                    'has_password': bool(config.get('password', '')),
                }
            })
        except Exception as e:
            logger.error(f'[百度] 加载配置失败: {e}')
            return jsonify({'success': False, 'message': f'操作失败: {str(e)}'}), 500
    try:
        data = request.get_json(force=True, silent=True) or {}
        username = (data.get('username') or '').strip()
        password = (data.get('password') or '').strip()
        if not username:
            return jsonify({'success': False, 'message': '请输入账号'})

        def _update(cfg):
            cfg['username'] = username
            if password:
                cfg['password'] = password
                # 账号密码变更后清除旧的cookie缓存
                cfg.pop('cookies', None)
                cfg.pop('cookies_ts', None)
        baidu_forum.update_config(_update)
        return jsonify({'success': True, 'message': '配置已保存'})
    except Exception as e:
        logger.error(f'[百度] 保存配置失败: {e}')
        return jsonify({'success': False, 'message': f'操作失败: {str(e)}'}), 500


@app.route('/baidu/test', methods=['POST'])
def baidu_test():
    try:
        s = baidu_forum._get_session()
        if baidu_forum._is_logged_in(s):
            return jsonify({'success': True, 'message': '登录成功'})
        return jsonify({'success': False, 'message': '登录失败，请检查账号密码'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@app.route('/baidu/search', methods=['POST'])
def baidu_search():
    keyword = (request.form.get('keyword') or '').strip()
    page = request.form.get('page', '1').strip()
    page = int(page) if page.isdigit() else 1
    # source 参数：'local'（仅本地）/ 'remote'（仅论坛）/ 默认本地优先回退论坛
    source = (request.form.get('source') or '').strip()
    if not keyword:
        return jsonify({'success': False, 'message': '请输入搜索关键词'})
    try:
        if source == 'remote':
            # 强制论坛实时搜索
            result = baidu_forum.search(keyword, page=page)
            result.setdefault('source', 'remote')
            logger.info(f'[搜索] 关键词:{keyword}, 页:{page}, 强制论坛:{result.get("total_count", 0)}个')
            return jsonify({'success': True, **result})
        elif source == 'local':
            # 强制本地数据库搜索
            local_result = forum_monitor.search_local_magnets(keyword, page=page)
            logger.info(f'[搜索] 关键词:{keyword}, 页:{page}, 强制本地:{local_result.get("total_count", 0)}个')
            return jsonify({'success': True, **local_result})
        else:
            # 默认：本地优先，无结果回退论坛
            local_result = forum_monitor.search_local_magnets(keyword, page=page)
            if local_result.get('total_count', 0) > 0:
                logger.info(f'[搜索] 关键词:{keyword}, 页:{page}, 本地命中:{local_result.get("total_count", 0)}个')
                return jsonify({'success': True, **local_result})
            result = baidu_forum.search(keyword, page=page)
            result.setdefault('source', 'remote')
            logger.info(f'[搜索] 关键词:{keyword}, 页:{page}, 本地无结果，回退论坛:{result.get("total_count", 0)}个')
            return jsonify({'success': True, **result})
    except Exception as e:
        logger.error(f'[搜索] 失败: {keyword} - {str(e)}')
        return jsonify({'success': False, 'message': str(e)})


@app.route('/baidu/magnet', methods=['POST'])
def baidu_magnet():
    tid = (request.form.get('tid') or '').strip()
    if not tid:
        return jsonify({'success': False, 'message': '缺少帖子ID'})
    try:
        result = baidu_forum.get_magnet_from_thread(tid)
        logger.info(f'[磁力] 帖子:{tid}, 获取成功')
        return jsonify({'success': True, **result})
    except Exception as e:
        logger.error(f'[磁力] 帖子:{tid}, 失败: {str(e)}')
        return jsonify({'success': False, 'message': str(e)})


@app.route('/baidu/batch_magnets', methods=['POST'])
def baidu_batch_magnets():
    """并发批量获取多个帖子的磁力链接"""
    tids_raw = (request.form.get('tids') or '').strip()
    if not tids_raw:
        return jsonify({'success': False, 'message': '缺少帖子ID列表'})
    tids = [t.strip() for t in tids_raw.split(',') if t.strip()]
    if not tids:
        return jsonify({'success': False, 'message': '帖子ID列表为空'})
    if len(tids) > 30:
        return jsonify({'success': False, 'message': '单次最多30个'})
    try:
        import time as _t
        start = _t.time()
        result = baidu_forum.batch_get_magnets(tids, max_workers=6)
        cost = round(_t.time() - start, 1)
        logger.info(f'[批量磁力] 共:{len(tids)}, 成功:{len(result["success"])}, 失败:{len(result["failed"])}, 耗时:{cost}s')
        return jsonify({
            'success': True,
            'magnets': result['success'],
            'failed': result['failed'],
            'total': len(tids),
            'cost': cost,
        })
    except Exception as e:
        logger.error(f'[批量磁力] 失败: {str(e)}')
        return jsonify({'success': False, 'message': str(e)})


@app.route('/baidu/save', methods=['POST'])
def baidu_save():
    """获取磁力链接并添加到115离线下载"""
    tid = (request.form.get('tid') or '').strip()
    magnet = (request.form.get('magnet') or '').strip()
    try:
        if not magnet:
            # 没有直接传磁力链接，从帖子获取
            if not tid:
                return jsonify({'success': False, 'message': '缺少帖子ID或磁力链接'})
            result = baidu_forum.get_magnet_from_thread(tid)
            magnet = result['magnet']
        success, msg = cloud115.add_offline_task(magnet)
        # 记录转存历史
        try:
            transfer_history.add_record(
                movie_id='', page='', movie_name='', magnet=magnet,
                success=success, message=msg, source='baidu'
            )
        except Exception:
            pass
        return jsonify({'success': success, 'message': msg, 'magnet': magnet})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


# ===== 论坛全论坛监控 =====

import forum_monitor


def _reschedule_forum_monitor() -> None:
    """根据配置重新调度论坛增量监控任务"""
    global _scheduler
    if _scheduler is None:
        return
    try:
        _scheduler.remove_job('forum_monitor_auto')
    except Exception:
        pass
    try:
        cfg = forum_monitor.get_monitor_config()
        if not cfg['enabled']:
            logger.info('[论坛监控] 自动监控未启用')
            return
        trigger = _parse_cron_expr(cfg['cron'])
        if trigger is None:
            logger.warning(f'[论坛监控] cron表达式无效: {cfg["cron"]}')
            return
        _scheduler.add_job(
            _do_forum_monitor_auto,
            trigger=trigger,
            id='forum_monitor_auto',
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info(f'[论坛监控] 已启用，调度表达式: {cfg["cron"]}')
    except Exception as e:
        logger.error(f'[论坛监控] 调度失败: {e}')


def _do_forum_monitor_auto() -> None:
    """定时增量监控执行函数（由 APScheduler 调用）"""
    try:
        logger.info('[论坛监控] 定时增量任务开始')
        result = forum_monitor.run_incremental()
        if result.get('success'):
            logger.info(f'[论坛监控] 定时增量完成: 新增{result.get("total_new", 0)}帖，'
                        f'下载{result.get("total_seeds", 0)}种子')
        else:
            logger.warning(f'[论坛监控] 定时增量失败: {result.get("message", "")}')
    except Exception as e:
        logger.error(f'[论坛监控] 定时增量异常: {e}')


def _do_forum_monitor_progress_push() -> None:
    """定时推送论坛监控进度到微信（每2小时触发）

    只在有任务运行时推送，避免无意义的空消息。
    """
    try:
        text = forum_monitor.build_progress_report()
        if not text:
            # 没有任务运行，不推送
            return
        ok, msg = wechat_work.send_wechat_message(text)
        if ok:
            logger.info('[论坛监控] 微信进度推送成功')
        else:
            logger.warning(f'[论坛监控] 微信进度推送失败: {msg}')
    except Exception as e:
        logger.error(f'[论坛监控] 微信进度推送异常: {e}')


def _reschedule_forum_monitor_progress_push() -> None:
    """注册/刷新"每2小时推送监控进度到微信"的定时任务

    固定 cron: 0 */2 * * *（每2小时整点触发）
    """
    global _scheduler
    if _scheduler is None:
        return
    try:
        _scheduler.remove_job('forum_monitor_progress_push')
    except Exception:
        pass
    try:
        trigger = CronTrigger(hour='*/2', minute=0, timezone='Asia/Shanghai')
        _scheduler.add_job(
            _do_forum_monitor_progress_push,
            trigger=trigger,
            id='forum_monitor_progress_push',
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info('[论坛监控] 已启用每2小时微信进度推送')
    except Exception as e:
        logger.error(f'[论坛监控] 微信进度推送调度失败: {e}')


@app.route('/baidu/monitor_config', methods=['GET', 'POST'])
def baidu_monitor_config() -> Union[Response, Tuple[Response, int]]:
    """读取/配置论坛监控"""
    if request.method == 'GET':
        try:
            cfg = forum_monitor.get_monitor_config()
            return jsonify({
                'success': True,
                'config': {
                    'enabled': cfg['enabled'],
                    'cron': cfg['cron'],
                    'page_delay': cfg['page_delay'],
                    'thread_delay': cfg['thread_delay'],
                    'max_pages_per_run': cfg['max_pages_per_run'],
                    'concurrent_threads': cfg['concurrent_threads'],
                    'last_full_crawl_at': cfg['last_full_crawl_at'],
                    'last_incremental_at': cfg['last_incremental_at'],
                    'running': forum_monitor.get_status()['running'],
                }
            })
        except Exception as e:
            logger.error(f'[论坛监控] 读取配置失败: {e}')
            return jsonify({'success': False, 'message': str(e)}), 500

    # POST: 更新配置
    try:
        cfg = forum_monitor.get_monitor_config()
        cfg['enabled'] = request.form.get('enabled', '').lower() == 'true'
        cron_expr = (request.form.get('cron', '')).strip()
        if cfg['enabled']:
            if not cron_expr:
                return jsonify({'success': False, 'message': '请填写cron表达式'}), 400
            if _parse_cron_expr(cron_expr) is None:
                return jsonify({'success': False, 'message': 'cron表达式格式错误（应为5段：分 时 日 月 周）'}), 400
        cfg['cron'] = cron_expr
        # 限流参数（带范围校验）
        try:
            cfg['page_delay'] = max(0.5, float(request.form.get('page_delay', cfg['page_delay'])))
            cfg['thread_delay'] = max(0.0, float(request.form.get('thread_delay', cfg['thread_delay'])))
            cfg['max_pages_per_run'] = max(1, int(request.form.get('max_pages_per_run', cfg['max_pages_per_run'])))
            cfg['concurrent_threads'] = max(1, int(request.form.get('concurrent_threads', cfg['concurrent_threads'])))
        except (ValueError, TypeError) as ve:
            return jsonify({'success': False, 'message': f'限流参数错误: {ve}'}), 400

        forum_monitor.save_monitor_config(cfg)
        _reschedule_forum_monitor()
        status = '已启用' if cfg['enabled'] else '已关闭'
        logger.info(f'[论坛监控] 配置更新: {status}, cron={cron_expr}')
        return jsonify({'success': True, 'message': f'监控{status}'})
    except Exception as e:
        logger.error(f'[论坛监控] 配置失败: {e}')
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/baidu/monitor_status')
def baidu_monitor_status() -> Response:
    """查询监控运行状态"""
    try:
        status = forum_monitor.get_status()
        stats = forum_monitor.get_statistics()
        cfg = forum_monitor.get_monitor_config()
        return jsonify({
            'success': True,
            'status': status,
            'statistics': stats,
            'config': {
                'enabled': cfg['enabled'],
                'cron': cfg['cron'],
                'last_full_crawl_at': cfg['last_full_crawl_at'],
                'last_incremental_at': cfg['last_incremental_at'],
            }
        })
    except Exception as e:
        logger.error(f'[论坛监控] 状态查询失败: {e}')
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/baidu/monitor_full', methods=['POST'])
def baidu_monitor_full() -> Response:
    """手动触发全量拉取（后台异步执行）"""
    try:
        # 仅检查主任务锁（全量/二次拉取互斥），不阻止增量监控
        if forum_monitor.get_status()['running']:
            return jsonify({'success': False, 'message': '主任务已在运行（全量/二次拉取），请先取消'})
        import threading as _threading
        t = _threading.Thread(target=forum_monitor.run_full_crawl, daemon=True)
        t.start()
        return jsonify({'success': True, 'message': '全量拉取已启动，请在状态页查看进度'})
    except Exception as e:
        logger.error(f'[论坛监控] 启动全量失败: {e}')
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/baidu/monitor_incremental', methods=['POST'])
def baidu_monitor_incremental() -> Response:
    """手动触发增量监控（后台异步执行，与主任务独立）"""
    try:
        # 增量监控独立锁，不检查主任务状态：全量拉取中也可启动增量
        if forum_monitor.get_status().get('incremental', {}).get('running'):
            return jsonify({'success': False, 'message': '增量监控已在运行'})
        import threading as _threading
        t = _threading.Thread(target=forum_monitor.run_incremental, daemon=True)
        t.start()
        return jsonify({'success': True, 'message': '增量监控已启动，请在状态页查看进度'})
    except Exception as e:
        logger.error(f'[论坛监控] 启动增量失败: {e}')
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/baidu/monitor_cancel', methods=['POST'])
def baidu_monitor_cancel() -> Response:
    """取消正在运行的监控任务

    支持参数 target: 'main'(默认) / 'incremental' / 'all'
    """
    try:
        target = (request.form.get('target') or 'main').strip()
        if target not in ('main', 'incremental', 'all'):
            target = 'main'
        cancelled = forum_monitor.cancel(target)
        if cancelled:
            logger.info(f'[论坛监控] 用户请求取消任务: {target}')
            return jsonify({'success': True, 'message': '已请求取消，任务将在下一次循环检查时停止'})
        return jsonify({'success': False, 'message': '当前没有运行中的监控任务'})
    except Exception as e:
        logger.error(f'[论坛监控] 取消失败: {e}')
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/baidu/monitor_recheck_all', methods=['POST'])
def baidu_monitor_recheck_all() -> Response:
    """全量二次拉取所有无种子帖子（后台异步执行）

    请求体（JSON）: {'delay': 3}  可选帖间延迟，默认3秒
    """
    try:
        if forum_monitor.get_status()['running']:
            return jsonify({'success': False, 'message': '已有监控任务在运行，请先取消'})
        delay = 3.0
        if request.is_json:
            data = request.get_json() or {}
            try:
                delay = float(data.get('delay') or 3)
            except (ValueError, TypeError):
                delay = 3.0
        delay = max(0.5, min(delay, 30))
        import threading as _threading
        t = _threading.Thread(target=forum_monitor.run_recheck_all_no_seeds, args=(delay,), daemon=True)
        t.start()
        return jsonify({'success': True, 'message': '全量二次拉取已启动，请在状态页查看进度'})
    except Exception as e:
        logger.error(f'[论坛监控] 启动全量二次拉取失败: {e}')
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/baidu/monitor_threads')
def baidu_monitor_threads() -> Response:
    """查询已爬取的帖子/种子列表"""
    try:
        fid = request.args.get('fid') or None
        keyword = (request.args.get('keyword') or '').strip()
        seed_filter = (request.args.get('seed_filter') or '').strip()
        try:
            page = max(1, int(request.args.get('page', 1)))
            page_size = max(1, min(200, int(request.args.get('page_size', 50))))
        except (ValueError, TypeError):
            page, page_size = 1, 50
        offset = (page - 1) * page_size
        rows, total = forum_monitor.list_threads(fid=fid, keyword=keyword,
                                                  limit=page_size, offset=offset,
                                                  seed_filter=seed_filter)
        return jsonify({
            'success': True,
            'threads': rows,
            'total': total,
            'page': page,
            'page_size': page_size,
        })
    except Exception as e:
        logger.error(f'[论坛监控] 列表查询失败: {e}')
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/baidu/monitor_forums')
def baidu_monitor_forums() -> Response:
    """查询所有板块的爬取进度"""
    try:
        forums = forum_monitor.list_forums_with_progress()
        return jsonify({'success': True, 'forums': forums})
    except Exception as e:
        logger.error(f'[论坛监控] 板块进度查询失败: {e}')
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/baidu/monitor_logs')
def baidu_monitor_logs() -> Response:
    """查询监控日志"""
    try:
        try:
            limit = max(1, min(200, int(request.args.get('limit', 20))))
        except (ValueError, TypeError):
            limit = 20
        logs = forum_monitor.list_recent_logs(limit=limit)
        return jsonify({'success': True, 'logs': logs})
    except Exception as e:
        logger.error(f'[论坛监控] 日志查询失败: {e}')
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/baidu/monitor_dashboard')
def baidu_monitor_dashboard() -> Response:
    """获取监控统计仪表盘数据"""
    try:
        data = forum_monitor.get_dashboard()
        return jsonify({'success': True, 'dashboard': data})
    except Exception as e:
        logger.error(f'[论坛监控] 仪表盘查询失败: {e}')
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/baidu/monitor_recheck_seeds', methods=['POST'])
def baidu_monitor_recheck_seeds() -> Response:
    """二次拉取无种子帖子的种子

    支持单个 tid 或批量 tids（JSON 数组）。对每个帖子重新访问页面下载种子，
    发现种子则更新数据库记录。批量操作内部串行 + 限流，避免压垮论坛。

    请求体（JSON）: {'tids': ['123', '456'], 'delay': 3}
    或表单: tid=123 （单个）
    """
    try:
        # 解析 tids：优先 JSON，兼容表单单 tid
        tids: List[str] = []
        delay = 3.0
        if request.is_json:
            data = request.get_json() or {}
            tids = [str(t) for t in (data.get('tids') or []) if str(t).strip()]
            if data.get('tid'):
                tids.insert(0, str(data['tid']))
            delay = float(data.get('delay') or 3)
        else:
            tid = (request.form.get('tid') or '').strip()
            if tid:
                tids = [tid]
            tids_str = (request.form.get('tids') or '').strip()
            if tids_str:
                tids.extend([t.strip() for t in tids_str.split(',') if t.strip()])
            try:
                delay = float(request.form.get('delay') or 3)
            except (ValueError, TypeError):
                delay = 3.0

        # 去重 + 限制批量大小（防止滥用）
        seen = set()
        tids = [t for t in tids if not (t in seen or seen.add(t))]
        if not tids:
            return jsonify({'success': False, 'message': '缺少 tid 参数'}), 400
        if len(tids) > 50:
            return jsonify({'success': False, 'message': '单次最多50个帖子'}), 400
        delay = max(0.5, min(delay, 30))

        # 监控运行中时禁止二次拉取（避免并发请求压垮论坛）
        if forum_monitor.get_status().get('running'):
            return jsonify({'success': False, 'message': '监控任务运行中，请稍后再试'}), 409

        results = []
        found_count = 0
        for i, tid in enumerate(tids):
            r = forum_monitor.recheck_thread_seeds(tid)
            results.append(r)
            if r.get('has_seeds'):
                found_count += 1
            # 帖间限流（最后一个不等）
            if i < len(tids) - 1 and delay > 0:
                time.sleep(delay)

        return jsonify({
            'success': True,
            'total': len(tids),
            'found_seeds': found_count,
            'still_empty': len(tids) - found_count,
            'results': results,
        })
    except Exception as e:
        logger.error(f'[论坛监控] 二次拉取失败: {e}')
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/baidu/monitor_seed_download')
def baidu_monitor_seed_download() -> Union[Response, Tuple[Response, int]]:
    """下载已保存的种子文件"""
    try:
        rel_path = (request.args.get('path') or '').strip()
        if not rel_path:
            return jsonify({'success': False, 'message': '缺少path参数'}), 400
        # 防止路径穿越：只允许 forum_seeds 目录下的文件
        seed_root = forum_monitor._SEED_DIR
        abs_path = os.path.normpath(os.path.join(seed_root, rel_path))
        if not abs_path.startswith(os.path.normpath(seed_root) + os.sep):
            return jsonify({'success': False, 'message': '非法路径'}), 400
        if not os.path.isfile(abs_path):
            return jsonify({'success': False, 'message': '文件不存在'}), 404
        import mimetypes
        mime = mimetypes.guess_type(abs_path)[0] or 'application/octet-stream'
        with open(abs_path, 'rb') as f:
            content = f.read()
        filename = os.path.basename(abs_path)
        return Response(
            content,
            mimetype=mime,
            headers={'Content-Disposition': f'attachment; filename="{filename}"'}
        )
    except Exception as e:
        logger.error(f'[论坛监控] 种子下载失败: {e}')
        return jsonify({'success': False, 'message': str(e)}), 500


# ===== 豆瓣同步 =====

@app.route('/douban')
def douban_page():
    return render_template('douban.html', version=VERSION)


@app.route('/douban/config', methods=['GET', 'POST'])
def douban_config():
    if request.method == 'GET':
        try:
            config = douban.load_config()
            # 不返回 cookie 明文，只返回是否已配置
            return jsonify({
                'success': True,
                'config': {
                    'user_id': config.get('user_id', ''),
                    'has_cookie': bool(config.get('cookie', '')),
                }
            })
        except Exception as e:
            logger.error(f'[豆瓣] 加载配置失败: {e}')
            return jsonify({'success': False, 'message': f'加载配置失败: {str(e)}'}), 500
    try:
        cookie = request.form.get('cookie', '').strip()
        user_id = request.form.get('user_id', '').strip()

        def _update(cfg):
            if cookie:
                cfg['cookie'] = encrypt(cookie)
            if user_id:
                cfg['user_id'] = user_id
        douban.update_config(_update)
        return jsonify({'success': True, 'message': '配置已保存'})
    except Exception as e:
        logger.error(f'[豆瓣] 保存配置失败: {e}')
        return jsonify({'success': False, 'message': f'保存失败: {str(e)}'}), 500


@app.route('/douban/check', methods=['POST'])
def douban_check():
    try:
        user_id = request.form.get('user_id', '').strip()
        if not user_id:
            config = douban.load_config()
            user_id = config.get('user_id', '')
        if not user_id:
            return jsonify({'success': False, 'message': '请输入豆瓣用户ID'})
        ok, msg = douban.check_cookie(user_id)
        return jsonify({'success': ok, 'message': msg})
    except Exception as e:
        logger.error(f'[豆瓣] 检查Cookie失败: {e}')
        return jsonify({'success': False, 'message': f'操作失败: {str(e)}'}), 500


@app.route('/douban/fetch', methods=['POST'])
def douban_fetch():
    """获取豆瓣看过的电影列表（按页）"""
    try:
        user_id = request.form.get('user_id', '').strip()
        if not user_id:
            config = douban.load_config()
            user_id = config.get('user_id', '')
        if not user_id:
            return jsonify({'success': False, 'message': '请输入豆瓣用户ID'})

        # 支持按页获取，默认第1页，每页15部
        try:
            page = int(request.form.get('page', 1))
            if page < 1:
                page = 1
        except Exception:
            page = 1
        per_page = 15
        start = (page - 1) * per_page

        movies, total, err = douban.fetch_watched_movies(user_id, start, per_page)
        if err:
            return jsonify({'success': False, 'message': err})

        # 获取已有电影名（用于对比）
        try:
            with data_lock:
                df = load_movies()
            existing_names = set()
            if not df.empty:
                for name in df['电影名'].dropna():
                    existing_names.add(str(name).strip())
        except Exception:
            existing_names = set()

        # 标记哪些是新的
        for m in movies:
            m['exists'] = m['title'] in existing_names

        import math
        total_pages = math.ceil(total / per_page) if total > 0 else 0

        return jsonify({
            'success': True,
            'movies': movies,
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': total_pages,
            'new_count': sum(1 for m in movies if not m['exists']),
        })
    except Exception as e:
        logger.error(f'[豆瓣] 获取电影列表失败: {e}')
        return jsonify({'success': False, 'message': f'操作失败: {str(e)}'}), 500


@app.route('/douban/movie_info', methods=['POST'])
def douban_movie_info():
    """获取单个电影的中文名（访问subject页面）"""
    try:
        subject_url = request.form.get('url', '').strip()
        if not subject_url:
            return jsonify({'success': False, 'message': '缺少电影URL'})

        # SSRF 防护：校验 URL 必须是豆瓣电影 subject 页面
        import re as _re
        if not _re.match(r'^https://movie\.douban\.com/subject/\d+/?', subject_url):
            return jsonify({'success': False, 'message': 'URL格式不合法，仅支持豆瓣电影页面'})

        name, err = douban.fetch_movie_chinese_name(subject_url)
        if err:
            return jsonify({'success': False, 'message': err})

        return jsonify({'success': True, 'name': name})
    except Exception as e:
        logger.error(f'[豆瓣] 获取电影信息失败: {e}')
        return jsonify({'success': False, 'message': f'操作失败: {str(e)}'}), 500


@app.route('/douban/sync', methods=['POST'])
def douban_sync():
    """同步选中的电影到数据库"""
    try:
        data = request.get_json(silent=True)
        if not data or 'movies' not in data:
            return jsonify({'success': False, 'message': '没有要同步的电影'})

        movies = data['movies']
        douban_page = data.get('page', 1)
        if not movies:
            return jsonify({'success': False, 'message': '没有要同步的电影'})

        try:
            with data_lock:
                df = load_movies()

                # 豆瓣页码 → 系统页码: 1:1对应
                page = douban_page
                added = 0
                skipped = 0
                # 已存在判断：优先按豆瓣URL（唯一键，同名独立）；旧数据无URL回退按名字
                existing_urls = set()
                existing_names_set = set()
                if not df.empty:
                    if '豆瓣链接' in df.columns:
                        existing_urls = {str(u) for u in df['豆瓣链接'].dropna() if str(u).strip()}
                    existing_names_set = {str(n) for n in df['电影名'].dropna() if str(n).strip()}

                for m in movies:
                    name = m.get('title', '').strip()
                    url = (m.get('url') or '').strip()
                    if not name:
                        continue

                    # 检查是否已存在（URL优先；无URL数据按名字，同名会跳过——
                    # 后台全量同步会按URL重新独立入库）
                    if (url and url in existing_urls) or (not url and name in existing_names_set):
                        skipped += 1
                        continue

                    new_id = 1
                    if not df.empty:
                        page_df = df[df['页码'] == page]
                        if not page_df.empty:
                            new_id = int(page_df['序号'].max()) + 1

                    new_movie = {
                        '序号': new_id,
                        '页码': page,
                        '电影名': name,
                        '磁力链接': '',
                        '保存时间': get_beijing_time().strftime('%Y-%m-%d %H:%M:%S'),
                        '豆瓣链接': url,
                    }
                    df = pd.concat([df, pd.DataFrame([new_movie])], ignore_index=True)
                    if url:
                        existing_urls.add(url)
                    existing_names_set.add(name)
                    added += 1

                save_movies(df)

            # 勾选同步只把新电影追加到页尾，顺序会与豆瓣错位
            # （豆瓣新增电影后整体后移，本地已有电影不会跟着移动）。
            # 新增了电影时，后台自动触发一次全量同步，按豆瓣顺序重建列表。
            # _do_douban_auto_sync 内部有锁保护，若已在运行会自动跳过；
            # 全量同步失败也不影响本次已添加的数据。
            if added > 0 and not _auto_sync_status['running']:
                import threading as _threading
                _threading.Thread(target=_do_douban_auto_sync, daemon=True).start()
                logger.info('[豆瓣] 勾选同步新增%d部，已启动后台全量同步对齐顺序' % added)

            return jsonify({
                'success': True,
                'message': f'同步完成: 新增{added}部，跳过{skipped}部（已存在），页码{page}。'
                           f'正在后台按豆瓣顺序全量对齐，磁力链接和保存时间会保留，稍候自动完成',
                'added': added,
                'skipped': skipped,
                'page': page,
            })
        except Exception as e:
            return jsonify({'success': False, 'message': f'同步失败: {str(e)}'})
    except Exception as e:
        logger.error(f'[豆瓣] 同步异常: {e}')
        return jsonify({'success': False, 'message': f'操作失败: {str(e)}'}), 500


# ===== 豆瓣自动同步 =====

# 全局调度器实例
_scheduler: Optional[BackgroundScheduler] = None
# 同步锁：防止自动同步和手动同步同时执行
_auto_sync_lock: threading.Lock = threading.Lock()
# 同步状态
_auto_sync_status: Dict[str, Any] = {'running': False, 'last_result': '', 'last_time': ''}


# ===== TMDB ID 批量同步 =====
_tmdb_sync_lock: threading.Lock = threading.Lock()
_tmdb_sync_status: Dict[str, Any] = {'running': False, 'last_summary': '', 'last_time': ''}


def _do_tmdb_sync(overwrite: bool = False, only_imdb_missing: bool = False) -> None:
    """后台补齐/刷新 TMDB_ID（带类型前缀 tv:/movie:）。

    优先级：
      1. 有 IMDB_ID → find_by_imdb_id（零歧义），取出类型拼前缀
      2. 无 IMDB_ID，且 only_imdb_missing=False → identify_media 按名最佳匹配（有类型）
    控制：
      - 每部之间 sleep 0.4s，避免 TMDB 速率限制；搜索结果在 tmdb 模块内有缓存，重复名不重复请求
      - 写 Excel 每 commit_batch=50 部 批量落盘一次，减少 IO；完成时再最终落盘
    """
    from media.tmdb import find_by_imdb_id, identify_media, get_tmdb_api_key
    import threading as _threading
    if not _tmdb_sync_lock.acquire(blocking=False):
        logger.info('[TMDB批量同步] 上一次同步仍在执行，跳过本次请求')
        return
    try:
        _tmdb_sync_status['running'] = True
        if not get_tmdb_api_key():
            logger.warning('[TMDB批量同步] 未配置 TMDB API Key，中止')
            return

        with data_lock:
            df = load_movies()
        if df.empty:
            logger.info('[TMDB批量同步] 电影列表为空，结束')
            return

        total = len(df)
        # 判定哪些行需要处理
        rows_idx: List[int] = []
        for i, r in df.iterrows():
            tid, _mt = parse_tmdb_id(r['TMDB_ID']) if ('TMDB_ID' in r.index and pd.notna(r['TMDB_ID'])) else ('', None)
            if tid and not overwrite:
                continue
            rows_idx.append(i)

        need = len(rows_idx)
        logger.info(f'[TMDB批量同步] 开始: 共{total}部，待处理{need}部（overwrite={overwrite}, only_imdb_missing={only_imdb_missing}）')

        imdb_ok = 0
        name_ok = 0
        skipped_imdb = 0
        skipped_name = 0
        failed = 0
        updates: Dict[int, str] = {}  # df行号 -> compose后的值
        COMMIT_EVERY = 50

        for pos, i in enumerate(rows_idx, start=1):
            r = df.iloc[i]
            name = str(r['电影名']) if pd.notna(r['电影名']) else ''
            imdb_raw = str(r['IMDB_ID']) if 'IMDB_ID' in r.index and pd.notna(r['IMDB_ID']) else ''
            imdb_id = imdb_raw.strip() if imdb_raw and imdb_raw != 'N/A' else ''
            matched_tmdb_id = ''
            matched_type: Optional[str] = None
            source = ''

            # 1. IMDb 精确查找（零歧义，优先）
            if imdb_id:
                res, err = find_by_imdb_id(imdb_id)
                if res and res.get('tmdb_id'):
                    matched_tmdb_id = str(res['tmdb_id'])
                    matched_type = res.get('media_type') or None
                    source = 'imdb'
                    imdb_ok += 1
                else:
                    skipped_imdb += 1
                    logger.debug(f'[TMDB批量同步] IMDb查不到: {name!r} IMDb={imdb_id} err={err or "无结果"}')

            # 2. 无IMDb或查不到时，退回按电影名自动最佳匹配
            if not matched_tmdb_id and not only_imdb_missing:
                try:
                    res, err = identify_media(name)
                except Exception as e:
                    res, err = None, str(e)
                if res and res.get('tmdb_id'):
                    matched_tmdb_id = str(res['tmdb_id'])
                    matched_type = res.get('media_type') or None
                    source = 'name'
                    name_ok += 1
                else:
                    skipped_name += 1
                    if pos <= 10 or pos % 50 == 0:
                        logger.debug(f'[TMDB批量同步] 名称匹配失败: {name!r} err={err or "无结果"}')

            if not matched_tmdb_id:
                failed += 1
            else:
                composed = compose_tmdb_id(matched_tmdb_id, matched_type)
                updates[i] = composed

            # 进度日志（每100部或最后）
            if pos % 100 == 0 or pos == need:
                logger.info(f'[TMDB批量同步] 进度{pos}/{need} 成功{len(updates)} IMDb命中{imdb_ok} 名称命中{name_ok} 失败{failed}')

            # 批量落盘
            if len(updates) >= COMMIT_EVERY:
                with data_lock:
                    df2 = load_movies()
                    if 'TMDB_ID' not in df2.columns:
                        df2['TMDB_ID'] = ''
                    for ri, val in updates.items():
                        if ri < len(df2):
                            df2.at[ri, 'TMDB_ID'] = val
                    save_movies(df2)
                    df = df2
                logger.debug(f'[TMDB批量同步] 已批次落盘{len(updates)}条')
                updates.clear()

            # 限频：0.4s/条；find_by_imdb_id/identify_media 大部分走缓存，真实请求不到 1% 仍要限流
            time.sleep(0.4)

        # 最终落盘（不足一批的余数）
        if updates:
            with data_lock:
                df2 = load_movies()
                if 'TMDB_ID' not in df2.columns:
                    df2['TMDB_ID'] = ''
                for ri, val in updates.items():
                    if ri < len(df2):
                        df2.at[ri, 'TMDB_ID'] = val
                save_movies(df2)

        summary = (f'完成: 待处理{need}/总{total} 写入{imdb_ok + name_ok}（IMDb{imdb_ok} 名称{name_ok}）'
                   f' 失败{failed}（IMDb未找到{skipped_imdb} 名称未找到{skipped_name}）')
        logger.info(f'[TMDB批量同步] {summary}')
        _tmdb_sync_status['last_summary'] = summary
        _tmdb_sync_status['last_time'] = get_beijing_time().strftime('%Y-%m-%d %H:%M:%S')
    except Exception as e:
        logger.error(f'[TMDB批量同步] 异常: {e}', exc_info=True)
        _tmdb_sync_status['last_summary'] = '异常中止: ' + str(e)
        _tmdb_sync_status['last_time'] = get_beijing_time().strftime('%Y-%m-%d %H:%M:%S')
    finally:
        _tmdb_sync_status['running'] = False
        try:
            _tmdb_sync_lock.release()
        except RuntimeError:
            pass


def _do_douban_auto_sync():
    """执行豆瓣全量自动同步（由调度器调用）"""
    # 原子地检查并获取锁，避免 TOCTOU 竞态条件
    if not _auto_sync_lock.acquire(blocking=False):
        logger.info('[豆瓣自动同步] 上一次同步仍在执行，跳过')
        return

    try:
        _auto_sync_status['running'] = True
        try:
            config = douban.load_config()
            user_id = config.get('user_id', '').strip()
            if not user_id:
                logger.warning('[豆瓣自动同步] 未配置豆瓣用户ID，跳过')
                _auto_sync_status['last_result'] = '未配置用户ID'
                _auto_sync_status['last_time'] = get_beijing_time().strftime('%Y-%m-%d %H:%M:%S')
                return

            logger.info(f'[豆瓣自动同步] 开始同步用户 {user_id} 的观影记录（优先缓存增量拉取）...')
            movies, err = douban.fetch_all_watched_movies_cached(user_id, max_pages=200, page_delay=2.0)
            if err:
                logger.error(f'[豆瓣自动同步] 拉取失败: {err}')
                _auto_sync_status['last_result'] = f'失败: {err}'
                _auto_sync_status['last_time'] = get_beijing_time().strftime('%Y-%m-%d %H:%M:%S')
                return

            logger.info(f'[豆瓣自动同步] 拉取到 {len(movies)} 部电影，开始按豆瓣顺序重建数据库...')

            # sanity check：拉取数量相对现有库骤降时中止重建。
            # 防止豆瓣限流/解析异常返回残缺列表导致误删本地电影
            # （正常使用中豆瓣"看过"列表只会缓慢变化，不会一夜少一半）
            try:
                with data_lock:
                    _existing_df = load_movies()
                existing_count = 0 if _existing_df.empty else len(_existing_df)
            except Exception:
                existing_count = 0
            if existing_count > 100 and len(movies) < existing_count * 0.5:
                msg = (f'拉取数量异常: 豆瓣返回{len(movies)}部，本地有{existing_count}部，'
                       f'疑似豆瓣限流或数据不完整，本次同步中止（未做任何修改）。'
                       f'如确属豆瓣大幅删除标记，请手动清理 data/movies_data.xlsx 后重试')
                logger.error(f'[豆瓣自动同步] {msg}')
                _auto_sync_status['last_result'] = msg
                _auto_sync_status['last_time'] = get_beijing_time().strftime('%Y-%m-%d %H:%M:%S')
                return


            # 顺序对齐策略：每次同步都按豆瓣顺序重建列表，保证系统顺序与豆瓣完全一致。
            # - 豆瓣中的电影严格按豆瓣顺序排列（页码 = i//15+1，序号 = i%15+1）
            # - 已存在电影的磁力链接和保存时间会被保留
            # - 本地存在但豆瓣已不存在的电影（移除标记/手动添加）直接删除，列表严格等于豆瓣
            # - 唯一键为豆瓣subject URL：同名电影（翻拍/重映/同名不同片）独立入库互不覆盖
            with data_lock:
                df = load_movies()
                added = 0
                skipped = 0
                url_matched = 0
                name_matched = 0
                per_page = 15

                # 建立现有电影映射：豆瓣URL → {磁力链接, 保存时间, 电影名}；
                # 旧数据无URL的按电影名建回退映射（名字→第一条记录），本次同步后即全部有URL
                existing_by_url: Dict[str, Dict[str, str]] = {}
                existing_by_name: Dict[str, Dict[str, str]] = {}
                if not df.empty:
                    for _, row in df.iterrows():
                        name = str(row['电影名']) if not pd.isna(row['电影名']) else ''
                        if not name:
                            continue
                        magnet = str(row['磁力链接']) if not pd.isna(row['磁力链接']) else ''
                        save_time = str(row['保存时间']) if not pd.isna(row['保存时间']) else ''
                        url = str(row['豆瓣链接']) if '豆瓣链接' in row.index and not pd.isna(row['豆瓣链接']) else ''
                        in_lib = '是' if ('已入库' in row.index and not pd.isna(row['已入库']) and str(row['已入库']) == '是') else '否'
                        imdb_id = str(row['IMDB_ID']) if 'IMDB_ID' in row.index and not pd.isna(row['IMDB_ID']) else ''
                        tmdb_id = str(row['TMDB_ID']) if 'TMDB_ID' in row.index and not pd.isna(row['TMDB_ID']) else ''
                        rec = {'磁力链接': magnet, '保存时间': save_time, '电影名': name, '已入库': in_lib,
                               'imdb_id': imdb_id, 'tmdb_id': tmdb_id}
                        if url:
                            existing_by_url[url] = rec
                        elif name not in existing_by_name:
                            # 同名旧数据（无URL）：优先保留有磁链的，否则保留最新的
                            old = existing_by_name.get(name)
                            if old is None or (not old['磁力链接'] and magnet) or \
                               (not old['磁力链接'] and not magnet and save_time > old['保存时间']):
                                existing_by_name[name] = rec

                new_rows: List[Dict[str, Any]] = []
                seen_urls: set = set()

                # 严格按豆瓣顺序重建：同名独立入库（每条豆瓣标记一个条目）
                for i, m in enumerate(movies):
                    url = (m.get('url') or '').strip()
                    name = m.get('title', '').strip()
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)

                    page = (i // per_page) + 1
                    seq = (i % per_page) + 1  # 页内序号从1开始

                    # 优先按URL匹配（新数据）；回退按名字匹配（旧数据迁移，一次性）
                    rec = existing_by_url.get(url)
                    if rec is not None:
                        skipped += 1
                        url_matched += 1
                        magnet = rec['磁力链接']
                        save_time = rec['保存时间']
                        in_lib = rec['已入库']
                        imdb_id = rec.get('imdb_id', '')
                        tmdb_id = rec.get('tmdb_id', '')
                        if rec['电影名'] != name:
                            name = rec['电影名']  # 保留用户可能改过的电影名
                    elif name in existing_by_name:
                        skipped += 1
                        name_matched += 1
                        magnet = existing_by_name[name]['磁力链接']
                        save_time = existing_by_name[name]['保存时间']
                        in_lib = existing_by_name[name]['已入库']
                        imdb_id = existing_by_name[name].get('imdb_id', '')
                        tmdb_id = existing_by_name[name].get('tmdb_id', '')
                    else:
                        added += 1
                        magnet = ''
                        save_time = get_beijing_time().strftime('%Y-%m-%d %H:%M:%S')
                        in_lib = '否'
                        imdb_id = ''
                        tmdb_id = ''

                    new_rows.append({
                        '序号': seq,
                        '页码': page,
                        '电影名': name,
                        '磁力链接': magnet,
                        '保存时间': save_time,
                        '豆瓣链接': url,
                        '已入库': in_lib,
                        'IMDB_ID': imdb_id,
                        'TMDB_ID': tmdb_id,
                    })

                # 豆瓣中不存在的本地电影 → 删除（不追加）
                # 按实际匹配数分桶计算（之前用 skipped 总数扣减会出现 -1 这类错值）
                removed = (len(existing_by_url) - url_matched) + (len(existing_by_name) - name_matched)
                if removed > 0:
                    kept_urls = seen_urls
                    kept_names = {r['电影名'] for r in new_rows}
                    removed_items = [
                        f"{v['电影名']}" for k, v in existing_by_url.items() if k not in kept_urls
                    ] + [
                        f"{v['电影名']}(旧数据)" for k, v in existing_by_name.items() if k not in kept_names
                    ]
                    logger.info(f'[豆瓣自动同步] 删除{removed}部豆瓣已不存在的电影: {removed_items[:10]}')

                new_df = pd.DataFrame(new_rows, columns=['序号', '页码', '电影名', '磁力链接', '保存时间', '豆瓣链接', '已入库', 'IMDB_ID', 'TMDB_ID'])

                # 判断是否需要保存：有新增/删除 或 顺序/页码/URL发生变化
                need_save = added > 0 or removed > 0
                if not need_save and not df.empty:
                    if len(df) != len(new_df):
                        need_save = True
                    else:
                        old_order = df[['电影名', '页码', '序号']].reset_index(drop=True)
                        new_order = new_df[['电影名', '页码', '序号']].reset_index(drop=True)
                        if not old_order.equals(new_order):
                            need_save = True

                if need_save:
                    save_movies(new_df)
                    logger.info(f'[豆瓣自动同步] 数据库已重建，共{len(new_rows)}部（豆瓣{len(movies)}部，删除{removed}部）')
                else:
                    logger.info('[豆瓣自动同步] 顺序已一致，无需更新')

            # 回填缺失的 IMDB_ID（抓豆瓣详情页）——IMDB_ID 回填归属豆瓣同步
            # 首次全量较慢（每部0.8s），之后仅新增电影需回填；'N/A' 占位的不重抓
            try:
                with data_lock:
                    _df_for_imdb = load_movies()
                if not _df_for_imdb.empty and _has_missing_ids(_df_for_imdb, 'IMDB_ID'):
                    logger.info('[豆瓣自动同步] 检测到缺失 IMDB_ID，开始抓取豆瓣详情页回填')
                    _backfill_imdb_ids(_df_for_imdb)
            except Exception as imdb_err:
                logger.warning(f'[豆瓣自动同步] IMDB_ID回填失败（不影响同步）: {imdb_err}')

            result_msg = f'成功: 新增{added}部，跳过{skipped}部（已存在），删除{removed}部，共{len(movies)}部'
            logger.info(f'[豆瓣自动同步] {result_msg}')
            _auto_sync_status['last_result'] = result_msg
            _auto_sync_status['last_time'] = get_beijing_time().strftime('%Y-%m-%d %H:%M:%S')

            # 更新配置中的最后同步时间
            try:
                douban.update_config(lambda cfg: cfg.update({
                    'last_sync_time': _auto_sync_status['last_time'],
                    'last_sync_result': result_msg,
                }))
            except Exception:
                pass

            # 豆瓣同步完成后刷新 Jellyfin 入库状态（已配置时）
            # 失败不影响同步结果
            try:
                jf_count = _refresh_jellyfin_status()
                if jf_count >= 0:
                    logger.info(f'[豆瓣自动同步] Jellyfin入库状态已刷新: {jf_count}部已入库')
            except Exception as jf_err:
                logger.warning(f'[豆瓣自动同步] Jellyfin状态刷新失败（不影响同步）: {jf_err}')

        except Exception as e:
            logger.error(f'[豆瓣自动同步] 异常: {e}', exc_info=True)
            _auto_sync_status['last_result'] = f'异常: {str(e)}'
            _auto_sync_status['last_time'] = get_beijing_time().strftime('%Y-%m-%d %H:%M:%S')
        finally:
            _auto_sync_status['running'] = False
    finally:
        _auto_sync_lock.release()


def _parse_cron_expr(cron_expr: str) -> Optional[CronTrigger]:
    """解析 cron 表达式，返回 CronTrigger 或 None"""
    if not cron_expr or not cron_expr.strip():
        return None
    try:
        parts = cron_expr.strip().split()
        if len(parts) != 5:
            return None
        return CronTrigger(
            minute=parts[0], hour=parts[1],
            day=parts[2], month=parts[3], day_of_week=parts[4],
        )
    except Exception:
        return None


def _reschedule_auto_sync():
    """根据配置重新调度自动同步任务"""
    global _scheduler
    if _scheduler is None:
        return

    # 移除旧任务（如果存在）
    try:
        _scheduler.remove_job('douban_auto_sync')
    except Exception:
        pass

    config = douban.load_config()
    enabled = config.get('auto_sync_enabled', False)
    cron_expr = config.get('auto_sync_cron', '0 3 * * *')

    if not enabled:
        logger.info('[豆瓣自动同步] 自动同步未启用')
        return

    trigger = _parse_cron_expr(cron_expr)
    if trigger is None:
        logger.warning(f'[豆瓣自动同步] cron表达式无效: {cron_expr}')
        return

    _scheduler.add_job(
        _do_douban_auto_sync,
        trigger=trigger,
        id='douban_auto_sync',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info(f'[豆瓣自动同步] 已启用，调度表达式: {cron_expr}')


@app.route('/douban/auto_sync_config', methods=['GET', 'POST'])
def douban_auto_sync_config():
    """读取/配置豆瓣自动同步"""
    if request.method == 'GET':
        try:
            config = douban.load_config()
            return jsonify({
                'success': True,
                'config': {
                    'auto_sync_enabled': config.get('auto_sync_enabled', False),
                    'auto_sync_cron': config.get('auto_sync_cron', '0 3 * * *'),
                    'last_sync_time': config.get('last_sync_time', ''),
                    'last_sync_result': config.get('last_sync_result', ''),
                    'running': _auto_sync_status['running'],
                }
            })
        except Exception as e:
            return jsonify({'success': False, 'message': str(e)})

    # POST: 更新配置
    try:
        enabled = request.form.get('enabled', '').lower() == 'true'
        cron_expr = (request.form.get('cron', '')).strip()

        # 验证 cron 表达式
        if enabled:
            if not cron_expr:
                return jsonify({'success': False, 'message': '请填写cron表达式'})
            trigger = _parse_cron_expr(cron_expr)
            if trigger is None:
                return jsonify({'success': False, 'message': 'cron表达式格式错误（应为5段：分 时 日 月 周）'})

        douban.update_config(lambda cfg: cfg.update({
            'auto_sync_enabled': enabled,
            'auto_sync_cron': cron_expr,
        }))

        # 重新调度
        _reschedule_auto_sync()

        status = '已启用' if enabled else '已关闭'
        logger.info(f'[豆瓣自动同步] 配置更新: {status}, cron={cron_expr}')
        return jsonify({'success': True, 'message': f'自动同步{status}'})
    except Exception as e:
        logger.error(f'[豆瓣自动同步] 配置失败: {e}')
        return jsonify({'success': False, 'message': str(e)})


@app.route('/douban/auto_sync_now', methods=['POST'])
def douban_auto_sync_now():
    """手动触发一次自动同步"""
    if _auto_sync_status['running']:
        return jsonify({'success': False, 'message': '同步正在进行中，请稍候'})
    # 异步执行，避免请求超时
    import threading as _threading
    t = _threading.Thread(target=_do_douban_auto_sync, daemon=True)
    t.start()
    return jsonify({'success': True, 'message': '全量同步已启动，请在日志或状态页查看进度'})


@app.route('/douban/auto_sync_status')
def douban_auto_sync_status():
    """查询自动同步状态"""
    try:
        config = douban.load_config()
        return jsonify({
            'success': True,
            'status': {
                'running': _auto_sync_status['running'],
                'last_time': _auto_sync_status['last_time'] or config.get('last_sync_time', ''),
                'last_result': _auto_sync_status['last_result'] or config.get('last_sync_result', ''),
                'enabled': config.get('auto_sync_enabled', False),
                'cron': config.get('auto_sync_cron', '0 3 * * *'),
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


# ===== TMDB ID 批量同步路由 =====
@app.route('/movies/tmdb_sync', methods=['POST'])
def movies_tmdb_sync():
    """一键批量补全 / 刷新全部电影的 TMDB ID（带类型前缀）。

    Body / Form:
        mode: 'fill'    仅补缺失（已有 TMDB_ID 的跳过）—— 默认，推荐日常
              'refresh' 全量重新识别 / 覆盖已有（用于整体升级或怀疑旧值不准）
        scope: 'all'        优先 IMDb 精确匹配，未命中再按电影名识别 —— 默认
               'imdb_only'  仅对有 IMDb 的条目做 find_by_imdb_id（零歧义，不会误匹配，但没IMDb的留空）
    返回 200 即表示已启动后台线程；实际进度通过 /logs 或 /movies/tmdb_sync_status 查看。
    """
    try:
        payload = request.get_json(silent=True) or request.form
        mode = (payload.get('mode') or 'fill').strip().lower() or 'fill'
        scope = (payload.get('scope') or 'all').strip().lower() or 'all'
        if mode not in ('fill', 'refresh'):
            return jsonify({'success': False, 'message': 'mode 必须是 fill 或 refresh'}), 400
        if scope not in ('all', 'imdb_only'):
            return jsonify({'success': False, 'message': 'scope 必须是 all 或 imdb_only'}), 400

        overwrite = (mode == 'refresh')
        only_imdb = (scope == 'imdb_only')

        import threading as _threading_local
        # 立即放后台，避免请求超时
        _threading_local.Thread(target=_do_tmdb_sync, args=(overwrite, only_imdb), daemon=True).start()

        mode_label = '仅补缺失' if mode == 'fill' else '覆盖刷新'
        scope_label = 'IMDb+名称' if scope == 'all' else '仅 IMDb 精确匹配'
        message = ('已启动后台批量同步 TMDB ID，请到【实时日志】查看进度。'
                   + f'（模式：{mode_label}；范围：{scope_label}）')
        return jsonify({
            'success': True,
            'message': message,
            'mode': mode,
            'scope': scope,
        })
    except Exception as e:
        logger.error(f'[TMDB批量同步] 启动失败: {e}')
        return jsonify({'success': False, 'message': f'操作失败: {str(e)}'}), 500


@app.route('/movies/tmdb_sync_status')
def movies_tmdb_sync_status():
    try:
        return jsonify({
            'success': True,
            'running': bool(_tmdb_sync_status.get('running')),
            'last_time': _tmdb_sync_status.get('last_time') or '',
            'last_summary': _tmdb_sync_status.get('last_summary') or '',
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


# ===== 转存历史 =====

@app.route('/history/recent')
def history_recent():
    """获取最近转存记录"""
    try:
        limit = request.args.get('limit', 10, type=int)
        if limit < 1 or limit > 100:
            limit = 10
        records = transfer_history.get_recent(limit=limit)
        return jsonify({'success': True, 'records': records})
    except Exception as e:
        logger.error(f'[转存历史] 查询失败: {e}')
        return jsonify({'success': False, 'message': str(e), 'records': []})


@app.route('/history/statistics')
def history_statistics():
    """获取转存统计数据"""
    try:
        stats = transfer_history.get_statistics()
        return jsonify({'success': True, 'statistics': stats})
    except Exception as e:
        logger.error(f'[转存历史] 统计失败: {e}')
        return jsonify({'success': False, 'message': str(e), 'statistics': {}})


@app.route('/history/by_name/<path:movie_name>')
def history_by_name(movie_name: str):
    """按电影名查询转存历史"""
    try:
        records = transfer_history.get_by_name(movie_name, limit=20)
        return jsonify({'success': True, 'records': records})
    except Exception as e:
        logger.error(f'[转存历史] 按名查询失败: {e}')
        return jsonify({'success': False, 'message': str(e), 'records': []})


@app.route('/tmdb/search')
def tmdb_search():
    """TMDB候选搜索（供手动识别：返回多个候选由用户点选）。

    Query:
        q: 电影名（必填）
        year: 年份（可选，辅助搜索）
        limit: 最大返回条数（默认10，最多20）
    Returns:
        { success: bool, candidates: [...] , message: err }
        candidates 元素见 media.tmdb.identify_media_candidates。
    """
    q = (request.args.get('q') or '').strip()
    year = (request.args.get('year') or '').strip() or None
    try:
        limit = max(1, min(20, int(request.args.get('limit') or 10)))
    except ValueError:
        limit = 10
    try:
        from media.tmdb import identify_media_candidates
        candidates, err = identify_media_candidates(q, year=year, limit=limit)
        if err:
            return jsonify({'success': False, 'message': err, 'candidates': []})
        return jsonify({'success': True, 'candidates': candidates})
    except Exception as e:
        logger.error(f'[TMDB搜索] 失败 q={q!r}: {e}')
        return jsonify({'success': False, 'message': str(e), 'candidates': []}), 500


@app.route('/movie/<int:movie_id>/tmdb_id', methods=['POST'])
def save_movie_tmdb_id(movie_id: int):
    """按序号+页码+豆瓣链接定位电影行，写入 TMDB_ID 列（支持带类型前缀 tv:/movie:）。

    Body:
        page: 页码（int，因为每页序号从1重新编号，必传）
        douban_url: 豆瓣链接（首选定位键，同名电影独立区分，必传）
        tmdb_id: 新的 TMDB ID（纯数字或空字符串清除）
        tmdb_type: 可选 'movie' | 'tv'；有值时拼成 "movie:xxx" / "tv:xxx" 前缀，
                   彻底解决 TMDB 电影/剧集同 ID 命名空间冲突。
    返回:
        { success, message, row_url }
    """
    try:
        payload = request.get_json(silent=True) or request.form
        page = payload.get('page')
        douban_url = (payload.get('douban_url') or '').strip()
        tmdb_id_raw = (payload.get('tmdb_id') or '').strip()
        tmdb_type = (payload.get('tmdb_type') or '').strip() or None
        if tmdb_type not in ('movie', 'tv'):
            tmdb_type = None
        if page is None or not douban_url:
            return jsonify({'success': False, 'message': '缺少定位参数（page + douban_url）'}), 400
        try:
            page = int(page)
        except (ValueError, TypeError):
            return jsonify({'success': False, 'message': 'page 必须是数字'}), 400
        if tmdb_id_raw and not str(tmdb_id_raw).isdigit():
            return jsonify({'success': False, 'message': 'TMDB ID 必须是纯数字或留空'}), 400
        # 合成存储格式："movie:xxx" / "tv:xxx" / "xxx" / ""
        new_tmdb_id = compose_tmdb_id(tmdb_id_raw, tmdb_type) if tmdb_id_raw else ''

        with data_lock:
            df = load_movies()
            # 按 douban_url 定位（最强），兜底再按序号+页码
            idx = None
            for i, row in df.iterrows():
                row_url = str(row['豆瓣链接']) if '豆瓣链接' in row.index and not pd.isna(row['豆瓣链接']) else ''
                if row_url and row_url == douban_url:
                    idx = i
                    break
            if idx is None:
                for i, row in df.iterrows():
                    try:
                        if int(row['序号']) == int(movie_id) and int(row['页码']) == page:
                            idx = i
                            break
                    except (ValueError, TypeError):
                        pass
            if idx is None:
                return jsonify({'success': False, 'message': '未找到该电影，请刷新列表'}), 404

            if 'TMDB_ID' not in df.columns:
                df['TMDB_ID'] = ''
            df.at[idx, 'TMDB_ID'] = new_tmdb_id
            save_movies(df)
            actual_url = str(df.at[idx, '豆瓣链接']) if '豆瓣链接' in df.columns and pd.notna(df.at[idx, '豆瓣链接']) else ''
        logger.info(f'[TMDB手动识别] 已写入 TMDB_ID={new_tmdb_id or "清除"} (type={tmdb_type or "auto"}) 电影#{movie_id} 页{page} URL={douban_url}')
        return jsonify({'success': True,
                        'message': ('已写入 TMDB ID ' + tmdb_id_raw + ('（剧集）' if tmdb_type == 'tv' else '（电影）' if tmdb_type == 'movie' else '')) if tmdb_id_raw else '已清除手动 TMDB ID',
                        'row_url': actual_url})
    except Exception as e:
        logger.error(f'[TMDB手动识别] 写入失败 #{movie_id}: {e}')
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/movies/detail/<int:movie_id>')
def movies_detail(movie_id: int):
    """获取电影详情（聚合电影信息、TMDB信息、转存历史）

    返回:
        {
            'success': True,
            'movie': {id, page, name, magnet, save_time, is_empty},
            'tmdb': {url, year, rating} or None,
            'history': [...]
        }
    """
    try:
        with data_lock:
            df = load_movies()
        if df.empty:
            return jsonify({'success': False, 'message': '数据为空'}), 404

        # 定位优先级：豆瓣URL（唯一键，同名不串）> (序号,页码) > 序号
        # 序号是页内序号（每页从1开始），同名独立入库后仅靠(序号,页码)仍可能撞车，
        # 前端已传 douban_url 时优先按URL精确定位
        url_param = request.args.get('url', '').strip()
        page_param = request.args.get('page', '').strip()
        row = pd.DataFrame()
        if url_param and '豆瓣链接' in df.columns:
            row = df[df['豆瓣链接'].astype(str) == url_param]
        if row.empty and page_param:
            try:
                page_num = int(page_param)
                row = df[(df['序号'] == movie_id) & (df['页码'] == page_num)]
            except (ValueError, TypeError):
                row = df[df['序号'] == movie_id]
        if row.empty and not url_param:
            row = df[df['序号'] == movie_id]
        if row.empty:
            return jsonify({'success': False, 'message': '电影不存在'}), 404

        r = row.iloc[0]
        magnet = r['磁力链接']
        is_empty = pd.isna(magnet) or str(magnet).strip() == ''
        name = str(r['电影名']) if not pd.isna(r['电影名']) else ''
        magnet_str = '' if is_empty else str(magnet)

        movie = {
            'id': int(r['序号']),
            'page': int(r['页码']),
            'name': name,
            'magnet': magnet_str,
            'magnet_display': (magnet_str[:50] + '...') if len(magnet_str) > 50 else magnet_str,
            'is_empty': is_empty,
            'save_time': str(r['保存时间']) if not pd.isna(r['保存时间']) else '',
            'douban_url': str(r['豆瓣链接']) if '豆瓣链接' in r.index and not pd.isna(r['豆瓣链接']) else '',
        }

        # 转存历史
        history = transfer_history.get_by_name(name, limit=10)

        # TMDB 信息（可选，失败不影响主流程）
        tmdb_info = None
        ids_info = {'imdb_id': '', 'tmdb_id': ''}
        try:
            from media.tmdb import identify_media, get_tmdb_api_key, find_by_imdb_id, get_media_by_id
            # 先取本机编号（前端"手动识别TMDB"按钮显隐要用：都没有才提示手动识别）
            imdb_id = ''
            tmdb_id = ''
            tmdb_media_type: Optional[str] = None
            if 'IMDB_ID' in r.index and not pd.isna(r['IMDB_ID']):
                raw = str(r['IMDB_ID']).strip()
                if raw and raw != 'N/A':
                    imdb_id = raw
            if 'TMDB_ID' in r.index and not pd.isna(r['TMDB_ID']):
                tid, mt = parse_tmdb_id(r['TMDB_ID'])
                if tid:
                    tmdb_id = tid
                    tmdb_media_type = mt
            # ids_info.tmdb_id 给前端 meta-chip 用：展示纯 ID；另外返回 tmdb_media_type 控制类型标签
            ids_info = {'imdb_id': imdb_id, 'tmdb_id': tmdb_id, 'tmdb_media_type': tmdb_media_type}

            if get_tmdb_api_key():
                result = None
                # 1. 优先按 IMDb 编号精确查询（零歧义，解决同名电影卡片串信息）
                if imdb_id:
                    result, _ = find_by_imdb_id(imdb_id)
                # 2. 再按本地 TMDB_ID 精确查询（手动识别，国产片无IMDb兜底）——**定向类型**，避免 tv/movie 同ID冲突
                if (not result or not result.get('poster_path')) and tmdb_id:
                    try:
                        res, _err = get_media_by_id(tmdb_id, media_type=tmdb_media_type)
                        if res:
                            result = res
                    except Exception:
                        result = None
                # 3. 两个 ID 都没时：退回按电影名搜索（仍可能误匹配，但比无海报好）
                if (not result or not result.get('poster_path')) and name and not imdb_id and not tmdb_id:
                    result, _ = identify_media(name)
                if result and result.get('poster_path'):
                    tmdb_info = {
                        'url': f"https://image.tmdb.org/t/p/w300{result['poster_path']}",
                        'year': result.get('year', ''),
                        'rating': str(result.get('vote_average', '')) if result.get('vote_average') else '',
                        'overview': result.get('overview', ''),
                        'source': 'imdb' if imdb_id else ('tmdb' if tmdb_id else 'name'),
                        'tmdb_id': result.get('tmdb_id', ''),
                    }
        except Exception as te:
            logger.debug(f'[电影详情] TMDB获取失败: {te}')

        return jsonify({
            'success': True,
            'movie': movie,
            'tmdb': tmdb_info,
            'ids': ids_info,
            'history': history,
        })
    except Exception as e:
        logger.error(f'[电影详情] 获取失败: {e}')
        return jsonify({'success': False, 'message': f'操作失败: {str(e)}'}), 500


# ===== 日志查看 =====

@app.route('/logs')
def logs_page():
    return render_template('logs.html', version=VERSION)


@app.route('/logs/api')
def logs_api():
    """读取日志文件，支持按关键词/级别筛选，返回最近N条"""
    keyword = request.args.get('keyword', '').strip()
    level = request.args.get('level', '').strip().upper()
    try:
        limit = int(request.args.get('limit', 200))
        limit = min(limit, 1000)
    except Exception:
        limit = 200

    if not os.path.exists(LOG_FILE):
        return jsonify({'success': True, 'lines': [], 'total': 0})

    try:
        with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
            all_lines = f.readlines()

        # 按关键词和级别筛选
        filtered = []
        for line in all_lines:
            line_s = line.rstrip('\n\r')
            if keyword and keyword not in line_s:
                continue
            if level:
                if f'[{level}]' not in line_s:
                    continue
            filtered.append(line_s)

        total = len(filtered)
        # 返回最新的limit条
        lines = filtered[-limit:]

        return jsonify({'success': True, 'lines': lines, 'total': total})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@app.route('/logs/stream')
def logs_stream():
    """SSE 实时日志流：先推送历史日志，再持续推送新日志"""
    keyword = request.args.get('keyword', '').strip()
    level = request.args.get('level', '').strip().upper()
    try:
        limit = int(request.args.get('limit', 200))
        limit = min(limit, 1000)
    except Exception:
        limit = 200

    def generate():
        # 1. 创建订阅队列
        q = queue.Queue(maxsize=500)
        with _log_subscribers_lock:
            _log_subscribers.append(q)

        # SSE 连接最大时长 30 分钟，超时后客户端会自动重连
        max_duration = 30 * 60
        start_time = time.time()

        try:
            # 0. 立即发送一个 SSE 注释，让浏览器立刻触发 onopen
            yield ': connected\n\n'

            # 2. 推送历史日志（最新的 limit 条）
            if os.path.exists(LOG_FILE):
                with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
                    all_lines = f.readlines()
                history = []
                for line in all_lines:
                    line = line.rstrip('\n\r')
                    if keyword and keyword not in line:
                        continue
                    if level and f'[{level}]' not in line:
                        continue
                    history.append(line)
                history = history[-limit:]

                # 批量发送，每50行打包一次
                batch = []
                for i, line in enumerate(history):
                    batch.append(f'data: {line}\n\n')
                    if len(batch) >= 50 or i == len(history) - 1:
                        yield ''.join(batch)
                        batch = []

            # 3. 持续推送新日志
            while True:
                # 超过最大连接时长，主动断开让客户端重连
                if time.time() - start_time > max_duration:
                    yield ': reconnect\n\n'
                    break
                try:
                    msg = q.get(timeout=15)
                    # 应用筛选
                    if keyword and keyword not in msg:
                        continue
                    if level and f'[{level}]' not in msg:
                        continue
                    yield f'data: {msg}\n\n'
                except queue.Empty:
                    # 发送心跳保持连接
                    yield ': heartbeat\n\n'
        finally:
            with _log_subscribers_lock:
                if q in _log_subscribers:
                    _log_subscribers.remove(q)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive'
        }
    )


@app.route('/logs/clear', methods=['POST'])
def logs_clear():
    """清空日志文件"""
    try:
        if os.path.exists(LOG_FILE):
            # 先刷新并关闭 RotatingFileHandler 的文件句柄，避免句柄继续写入旧缓冲
            for h in list(logger.handlers):
                if isinstance(h, RotatingFileHandler):
                    h.flush()
                    # 轮转：将当前文件移到 .1，并创建新的空文件
                    h.doRollover()
            logger.info('[日志] 日志已清空')
        return jsonify({'success': True, 'message': '日志已清空'})
    except Exception as e:
        logger.error(f'[日志] 清空失败: {e}')
        return jsonify({'success': False, 'message': '清空日志失败'}), 500


if __name__ == '__main__':
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'

    # 初始化豆瓣自动同步调度器
    # debug 模式下 Werkzeug reloader 会启动两次进程，只在主进程初始化调度器
    if not debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        try:
            _scheduler = BackgroundScheduler(timezone='Asia/Shanghai')
            _scheduler.start()
            _reschedule_auto_sync()
            _reschedule_forum_monitor()
            _reschedule_forum_monitor_progress_push()
            logger.info('[调度器] APScheduler 已启动')
        except Exception as e:
            logger.error(f'[调度器] 启动失败: {e}')

    # threaded=True: 多线程处理请求，避免 SSE 长连接阻塞其他请求
    app.run(host='0.0.0.0', port=3698, debug=debug, threaded=True)
