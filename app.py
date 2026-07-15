from flask import Flask, render_template, request, redirect, url_for, jsonify, Response, stream_with_context, session
from flask_cors import CORS
import pandas as pd
import os
import secrets
import shutil
import glob as glob_mod
from datetime import datetime, timedelta
import zoneinfo
import threading
import hashlib
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
fh.setFormatter(jsonlogger.JsonFormatter('%(asctime)s %(levelname)s %(message)s'))
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

user_states: Dict[str, Dict[str, Any]] = {}
user_states_lock: threading.Lock = threading.Lock()

def get_beijing_time() -> datetime:
    """获取北京时间"""
    return datetime.now(zoneinfo.ZoneInfo("Asia/Shanghai"))

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
        if password == APP_PASSWORD:
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

def load_movies() -> pd.DataFrame:
    if not os.path.exists(EXCEL_FILE):
        df = pd.DataFrame(columns=['序号', '页码', '电影名', '磁力链接', '保存时间'])
        df.to_excel(EXCEL_FILE, index=False)
        return df

    # 使用文件 mtime+size 作为缓存键，避免读取整个文件计算哈希
    stat = os.stat(EXCEL_FILE)
    current_sig = f'{stat.st_mtime}:{stat.st_size}'
    if _movie_cache['hash'] == current_sig and _movie_cache['data'] is not None:
        return _movie_cache['data'].copy()

    df = pd.read_excel(EXCEL_FILE)
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
            'save_time': row['保存时间']
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

        mask = df['电影名'].str.lower().str.contains(keyword.lower(), na=False, regex=False)
        result_df = df[mask]
        movies = build_movie_list(result_df)

        return render_template('search.html', movies=movies, keyword=keyword, version=VERSION)
    except Exception as e:
        logger.error(f'[搜索] 失败 keyword={keyword}: {e}', exc_info=True)
        # 显示错误信息而非静默重定向，便于用户排查
        return render_template('search.html', movies=[], keyword=keyword, version=VERSION,
                              error=f'搜索失败，请重试'), 500

@app.route('/add', methods=['POST'])
def add_movie():
    page = request.form.get('page')
    name = request.form.get('name')
    magnet = request.form.get('magnet', '')
    
    if not page or not name:
        return jsonify({'success': False, 'message': '页码和电影名不能为空'})
    
    try:
        page = int(page)
    except (ValueError, TypeError):
        return jsonify({'success': False, 'message': '页码必须是数字'})
    
    try:
        with data_lock:
            df = load_movies()
            
            page_df = df[df['页码'] == page]
            new_id = int(page_df['序号'].max()) + 1 if not page_df.empty else 1
            new_movie = {
                '序号': new_id,
                '页码': page,
                '电影名': name,
                '磁力链接': magnet,
                '保存时间': get_beijing_time().strftime('%Y-%m-%d %H:%M:%S')
            }
            
            df = pd.concat([df, pd.DataFrame([new_movie])], ignore_index=True)
            save_movies(df)
        
        return jsonify({'success': True, 'message': '添加成功'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'添加失败: {str(e)}'})

@app.route('/delete/<int:movie_id>', methods=['POST'])
def delete_movie(movie_id):
    page = request.args.get('page', type=int)
    try:
        with data_lock:
            df = load_movies()
            
            if page is not None:
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
    
    try:
        with data_lock:
            df = load_movies()
            
            if not page:
                return jsonify({'success': False, 'message': '页码不能为空'})
            
            try:
                page_int = int(page)
            except (ValueError, TypeError):
                return jsonify({'success': False, 'message': '页码必须是数字'})
            
            mask = (df['序号'] == movie_id) & (df['页码'] == page_int)
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
                             '页码 电影名 磁力链接 - 添加电影\n'
                             '页码 - 查看该页电影\n'
                             '搜索 电影名 - 搜索电影\n'
                             '磁力链接 - 转存到115网盘\n\n'
                             '菜单功能:\n'
                             '查看电影 - 浏览电影列表\n'
                             '批量转存 - 批量转存到115\n'
                             '目录 - 管理115网盘目录')
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
                result = wechat_work.handle_text_message(content)

                if isinstance(result, dict):
                    try:
                        with data_lock:
                            df = load_movies()
                            page_df = df[df['页码'] == result['page']]
                            new_id = int(page_df['序号'].max()) + 1 if not page_df.empty else 1
                            new_movie = {
                                '序号': new_id,
                                '页码': result['page'],
                                '电影名': result['name'],
                                '磁力链接': result['magnet'],
                                '保存时间': get_beijing_time().strftime('%Y-%m-%d %H:%M:%S')
                            }
                            df = pd.concat([df, pd.DataFrame([new_movie])], ignore_index=True)
                            save_movies(df)
                        reply = f'添加成功\n页码: {result["page"]}\n电影名: {result["name"]}'
                    except Exception as e:
                        reply = f'添加失败: {str(e)}'
                else:
                    reply = result

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
        # 安全检查：要求共享密钥验证，防止未授权添加电影
        proxy_token = os.environ.get('WECHAT_PROXY_TOKEN', '')
        if proxy_token:
            provided = request.headers.get('X-Proxy-Token') or request.form.get('proxy_token') or request.args.get('proxy_token', '')
            if provided != proxy_token:
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

        if isinstance(result, dict):
            try:
                with data_lock:
                    df = load_movies()
                    page_df = df[df['页码'] == result['page']]
                    new_id = int(page_df['序号'].max()) + 1 if not page_df.empty else 1
                    new_movie = {
                        '序号': new_id,
                        '页码': result['page'],
                        '电影名': result['name'],
                        '磁力链接': result['magnet'],
                        '保存时间': get_beijing_time().strftime('%Y-%m-%d %H:%M:%S')
                    }
                    df = pd.concat([df, pd.DataFrame([new_movie])], ignore_index=True)
                    save_movies(df)
                reply = f'添加成功\n页码: {result["page"]}\n电影名: {result["name"]}'
            except Exception as e:
                reply = f'添加失败: {str(e)}'
        else:
            reply = result

        return jsonify({'success': True, 'message': reply})
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

        if isinstance(result, dict):
            try:
                with data_lock:
                    df = load_movies()
                    page_df = df[df['页码'] == result['page']]
                    new_id = int(page_df['序号'].max()) + 1 if not page_df.empty else 1
                    new_movie = {
                        '序号': new_id,
                        '页码': result['page'],
                        '电影名': result['name'],
                        '磁力链接': result['magnet'],
                        '保存时间': get_beijing_time().strftime('%Y-%m-%d %H:%M:%S')
                    }
                    df = pd.concat([df, pd.DataFrame([new_movie])], ignore_index=True)
                    save_movies(df)
                return jsonify({'success': True, 'message': f'添加成功\n页码: {result["page"]}\n电影名: {result["name"]}'})
            except Exception as e:
                return jsonify({'success': False, 'message': f'添加失败: {str(e)}'})
        else:
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
    if not keyword:
        return jsonify({'success': False, 'message': '请输入搜索关键词'})
    try:
        result = baidu_forum.search(keyword, page=page)
        logger.info(f'[搜索] 关键词:{keyword}, 页:{page}, 结果:{result.get("total_count", 0)}个')
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

                for m in movies:
                    name = m.get('title', '').strip()
                    if not name:
                        continue

                    # 检查是否已存在
                    if not df.empty and name in df['电影名'].values:
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
                    }
                    df = pd.concat([df, pd.DataFrame([new_movie])], ignore_index=True)
                    added += 1

                save_movies(df)

            return jsonify({
                'success': True,
                'message': f'同步完成: 新增{added}部，跳过{skipped}部（已存在），页码{page}',
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


def _do_douban_auto_sync():
    """执行豆瓣全量自动同步（由调度器调用）"""
    if _auto_sync_lock.locked():
        logger.info('[豆瓣自动同步] 上一次同步仍在执行，跳过')
        return

    with _auto_sync_lock:
        _auto_sync_status['running'] = True
        try:
            config = douban.load_config()
            user_id = config.get('user_id', '').strip()
            if not user_id:
                logger.warning('[豆瓣自动同步] 未配置豆瓣用户ID，跳过')
                _auto_sync_status['last_result'] = '未配置用户ID'
                _auto_sync_status['last_time'] = get_beijing_time().strftime('%Y-%m-%d %H:%M:%S')
                return

            logger.info(f'[豆瓣自动同步] 开始全量拉取用户 {user_id} 的观影记录...')
            movies, err = douban.fetch_all_watched_movies_slow(user_id, max_pages=200, page_delay=2.0)
            if err:
                logger.error(f'[豆瓣自动同步] 拉取失败: {err}')
                _auto_sync_status['last_result'] = f'失败: {err}'
                _auto_sync_status['last_time'] = get_beijing_time().strftime('%Y-%m-%d %H:%M:%S')
                return

            logger.info(f'[豆瓣自动同步] 拉取到 {len(movies)} 部电影，开始写入数据库...')

            # 按豆瓣顺序写入：每15条为一页，保持豆瓣原始顺序
            with data_lock:
                df = load_movies()
                added = 0
                skipped = 0
                per_page = 15

                for i, m in enumerate(movies):
                    name = m.get('title', '').strip()
                    if not name:
                        continue
                    # 去重：按电影名全表查重
                    if not df.empty and name in df['电影名'].values:
                        skipped += 1
                        continue
                    # 页码分配：每15条一页，从第1页开始
                    page = (i // per_page) + 1
                    # 序号：该页码内 max+1
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
                    }
                    df = pd.concat([df, pd.DataFrame([new_movie])], ignore_index=True)
                    added += 1

                if added > 0:
                    save_movies(df)

            result_msg = f'成功: 新增{added}部，跳过{skipped}部（已存在），共{len(movies)}部'
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

        except Exception as e:
            logger.error(f'[豆瓣自动同步] 异常: {e}', exc_info=True)
            _auto_sync_status['last_result'] = f'异常: {str(e)}'
            _auto_sync_status['last_time'] = get_beijing_time().strftime('%Y-%m-%d %H:%M:%S')
        finally:
            _auto_sync_status['running'] = False


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
            logger.info('[调度器] APScheduler 已启动')
        except Exception as e:
            logger.error(f'[调度器] 启动失败: {e}')

    # threaded=True: 多线程处理请求，避免 SSE 长连接阻塞其他请求
    app.run(host='0.0.0.0', port=3698, debug=debug, threaded=True)
