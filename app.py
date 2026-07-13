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
import base64
from typing import List, Dict, Any, Optional, Tuple, Callable, Union
from logging.handlers import RotatingFileHandler
from Crypto.Cipher import AES
from Crypto.Protocol.KDF import scrypt
from Crypto.Random import get_random_bytes

_KEY_SIZE: int = 32
_NONCE_SIZE: int = 12
_SALT_SIZE: int = 16
_ENCRYPTION_KEY: Optional[str] = None

def _get_encryption_key() -> str:
    global _ENCRYPTION_KEY
    if _ENCRYPTION_KEY is None:
        env_key: Optional[str] = os.environ.get('ENCRYPTION_KEY')
        if not env_key:
            _ENCRYPTION_KEY = os.environ.get('FLASK_SECRET_KEY', '')[:_KEY_SIZE]
            if len(_ENCRYPTION_KEY) < _KEY_SIZE:
                _ENCRYPTION_KEY = _ENCRYPTION_KEY.ljust(_KEY_SIZE, '0')
        else:
            _ENCRYPTION_KEY = env_key[:_KEY_SIZE].ljust(_KEY_SIZE, '0')
    return _ENCRYPTION_KEY

def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ''
    key: str = _get_encryption_key()
    salt: bytes = get_random_bytes(_SALT_SIZE)
    nonce: bytes = get_random_bytes(_NONCE_SIZE)
    derived_key: bytes = scrypt(key, salt, _KEY_SIZE, N=2**14, r=8, p=1)
    cipher = AES.new(derived_key, AES.MODE_GCM, nonce=nonce)
    ciphertext: bytes
    tag: bytes
    ciphertext, tag = cipher.encrypt_and_digest(plaintext.encode('utf-8'))
    encoded: str = base64.b64encode(salt + nonce + tag + ciphertext).decode('ascii')
    return f'ENC[{encoded}]'

def decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return ''
    if ciphertext.startswith('ENC[') and ciphertext.endswith(']'):
        encoded: str = ciphertext[4:-1]
    else:
        return ciphertext
    try:
        decoded: bytes = base64.b64decode(encoded)
        salt: bytes = decoded[:_SALT_SIZE]
        nonce: bytes = decoded[_SALT_SIZE:_SALT_SIZE + _NONCE_SIZE]
        tag: bytes = decoded[_SALT_SIZE + _NONCE_SIZE:_SALT_SIZE + _NONCE_SIZE + 16]
        data: bytes = decoded[_SALT_SIZE + _NONCE_SIZE + 16:]
        key: str = _get_encryption_key()
        derived_key: bytes = scrypt(key, salt, _KEY_SIZE, N=2**14, r=8, p=1)
        cipher = AES.new(derived_key, AES.MODE_GCM, nonce=nonce)
        plaintext: bytes = cipher.decrypt_and_verify(data, tag)
        return plaintext.decode('utf-8')
    except Exception:
        return ciphertext

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

def get_beijing_time() -> datetime:
    """获取北京时间"""
    return datetime.now(zoneinfo.ZoneInfo("Asia/Shanghai"))

app = Flask(__name__)

# Flask secret_key：从环境变量读取，生产环境必须设置强随机值
# 开发环境下若未设置，会生成临时随机值（每次重启会话失效）
app.secret_key = os.environ.get('FLASK_SECRET_KEY') or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(days=7)

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

# 登录密码：优先从环境变量读取；未设置则使用默认密码（首次启动会在日志中提示修改）
APP_PASSWORD: str = os.environ.get('APP_PASSWORD') or 'admin123'
# 是否强制要求设置密码（生产环境建议设为 True）
STRICT_PASSWORD: bool = bool(os.environ.get('APP_PASSWORD'))

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
    if request.path in ('/login', '/logout'):
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


# ==================== 登录路由 ====================

@app.route('/login', methods=['GET', 'POST'])
def login() -> Union[str, Response]:
    error: Optional[str] = None
    if request.method == 'POST':
        password: str = request.form.get('password', '')
        if password == APP_PASSWORD:
            session.clear()  # 清除旧 session 防止固定会话攻击
            session['logged_in'] = True
            session.permanent = True
            _get_csrf_token()  # 立即生成 CSRF token
            logger.info('[登录] 用户登录成功')
            next_url: str = request.args.get('next') or url_for('index')
            # 防止开放重定向
            if not next_url.startswith('/'):
                next_url = url_for('index')
            return redirect(next_url)
        error = '密码错误'
        logger.warning('[登录] 密码错误')
    return render_template('login.html', error=error, version=VERSION,
                          strict_password=STRICT_PASSWORD)


@app.route('/logout', methods=['GET', 'POST'])
def logout() -> Response:
    session.clear()
    return redirect(url_for('login'))


@app.route('/health')
def health() -> Response:
    return jsonify({'status': 'ok', 'version': VERSION})

# 版本号
VERSION: str = "1.0.0"
try:
    with open('VERSION', 'r') as f:
        VERSION = f.read().strip()
except Exception:
    pass

def load_movies() -> pd.DataFrame:
    if not os.path.exists(EXCEL_FILE):
        df = pd.DataFrame(columns=['序号', '页码', '电影名', '磁力链接', '保存时间'])
        df.to_excel(EXCEL_FILE, index=False)
        return df

    with open(EXCEL_FILE, 'rb') as f:
        current_hash = hashlib.md5(f.read()).hexdigest()
    if _movie_cache['hash'] == current_hash and _movie_cache['data'] is not None:
        return _movie_cache['data'].copy()

    df = pd.read_excel(EXCEL_FILE)
    # 过滤掉重复的表头行（序号列为非数字的行）
    if not df.empty:
        try:
            pd.to_numeric(df['序号'], errors='raise')
        except (ValueError, TypeError):
            # 序号列有非数字行，过滤掉
            df = df[pd.to_numeric(df['序号'], errors='coerce').notna()].reset_index(drop=True)
    _movie_cache['hash'] = current_hash
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
            return render_template('index.html', movies=[], current_page=0, all_page_nums=[], version=VERSION)
        
        all_page_nums = sorted(df['页码'].unique())
        
        if page_num not in all_page_nums:
            if all_page_nums:
                page_num = all_page_nums[0]
            else:
                page_num = 0
        
        page_df = df[df['页码'] == page_num]
        movies = build_movie_list(page_df)
        
        return render_template('index.html', 
                              movies=movies, 
                              current_page=page_num, 
                              all_page_nums=all_page_nums,
                              version=VERSION)
    except Exception as e:
        return render_template('index.html', movies=[], current_page=0, all_page_nums=[], version=VERSION,
                              error=f'加载数据失败: {str(e)}')

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
        return redirect(url_for('index'))

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
    config = cloud115.load_config()
    cookie = config.get('cookie', '')
    masked = cookie[:20] + '...' + cookie[-10:] if len(cookie) > 30 else cookie
    return jsonify({'success': True, 'cookie_masked': masked, 'has_cookie': bool(cookie)})


@app.route('/cloud115/config', methods=['POST'])
def cloud115_set_config():
    cookie = request.form.get('cookie', '').strip()
    if not cookie:
        return jsonify({'success': False, 'message': 'Cookie不能为空'})

    def _update(cfg):
        cfg['cookie'] = encrypt(cookie)
    cloud115.update_config(_update)
    return jsonify({'success': True, 'message': 'Cookie保存成功'})


@app.route('/cloud115/verify', methods=['POST'])
def cloud115_verify():
    success, msg = cloud115.verify_cookie()
    return jsonify({'success': success, 'message': msg})


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
        for _, row in page_df.iterrows():
            magnet = row['磁力链接']
            if not pd.isna(magnet) and str(magnet).strip() != '':
                magnets.append(str(magnet))

        if not magnets:
            return jsonify({'success': False, 'message': '当前页没有有效的磁力链接'})

        results = cloud115.batch_add_offline_tasks(magnets, save_path_id=save_path)
        success_count = sum(1 for r in results if r['success'])
        fail_count = len(results) - success_count
        dir_info = f'（保存到: 第{page_num}页）' if save_path else ''
        logger.info(f'[批量转存] 页码:{page_num}, 成功:{success_count}, 失败:{fail_count}')
        return jsonify({
            'success': True,
            'message': f'批量转存完成: 成功 {success_count}, 失败 {fail_count}{dir_info}',
            'results': results
        })
    except Exception as e:
        return jsonify({'success': False, 'message': f'批量转存失败: {str(e)}'})


@app.route('/cloud115/tasks', methods=['GET'])
def cloud115_tasks():
    page = request.args.get('page', 1)
    try:
        page = int(page)
    except (ValueError, TypeError):
        page = 1
    success, msg, tasks = cloud115.get_task_list(page)
    return jsonify({'success': success, 'message': msg, 'tasks': tasks})


@app.route('/cloud115/dirs', methods=['GET'])
def cloud115_dirs():
    cid = request.args.get('cid', '0')
    success, msg, dirs = cloud115.get_dir_list(cid)
    return jsonify({'success': success, 'message': msg, 'dirs': dirs})


@app.route('/cloud115/save_path', methods=['GET'])
def cloud115_get_save_path():
    path_id, path_name = cloud115.get_default_save_path()
    return jsonify({'success': True, 'path_id': path_id, 'path_name': path_name})


@app.route('/cloud115/save_path', methods=['POST'])
def cloud115_set_save_path():
    path_id = request.form.get('path_id', '').strip() or '0'
    path_name = request.form.get('path_name', '').strip() or None
    cloud115.set_default_save_path(path_id, path_name)
    return jsonify({'success': True, 'message': '默认保存目录已更新'})


@app.route('/wechat/config', methods=['GET'])
def wechat_get_config():
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


@app.route('/wechat/config', methods=['POST'])
def wechat_set_config():
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
            state = user_states.get(from_user)

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
                    del user_states[from_user]
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
                    del user_states[from_user]
                else:
                    reply = '请输入页码数字'
            elif state and state['action'] == 'browse_dir':
                if content == '确认':
                    cloud115.set_default_save_path(state['cid'], state['path'])
                    reply = f'已设置转存目录: {state["path"]}'
                    del user_states[from_user]
                elif content == '新建':
                    state['action'] = 'create_dir_name'
                    reply = f'在 {state["path"]} 下创建目录\n请输入新目录名:'
                elif content == '返回':
                    if len(state.get('stack', [])) > 1:
                        state['stack'].pop()
                        parent = state['stack'][-1]
                        state['cid'] = parent['cid']
                        state['path'] = parent['path']
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
                        state['cid'] = d['cid']
                        state['path'] = state['path'] + ' / ' + d['name']
                        state['stack'].append({'cid': d['cid'], 'path': state['path']})
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
                del user_states[from_user]
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
                        user_states[from_user] = {'action': 'batch_transfer'}
                elif event_key == '115_dir':
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


@app.route('/wechat/menu', methods=['POST'])
def wechat_menu():
    config = wechat_work.load_config()
    agentid = config.get('agentid', '')
    if not agentid:
        return jsonify({'success': False, 'message': '未配置AgentId'})
    success, msg = wechat_work.create_menu(agentid)
    return jsonify({'success': success, 'message': msg})


# ==================== 115网盘整理路由 ====================

from media.scanner import scan_115_directory, get_directory_tree
from media.tmdb import identify_media, identify_batch, get_tmdb_api_key, set_tmdb_api_key
from media.tmdb import _load_config as _tmdb_load_config
from media.classifier import classify, get_all_categories
from media.organizer import organize_files


@app.route('/media/organize_root', methods=['GET'])
def media_get_organize_root():
    cfg = _tmdb_load_config()
    cid = cfg.get('organize_root_cid', '')
    name = cfg.get('organize_root_name', '')
    return jsonify({'success': True, 'cid': cid, 'name': name})


@app.route('/media/organize_root', methods=['POST'])
def media_set_organize_root():
    cid = request.form.get('cid', '').strip()
    name = request.form.get('name', '').strip()

    def _update(cfg):
        cfg['organize_root_cid'] = cid
        cfg['organize_root_name'] = name
    cloud115.update_config(_update)
    return jsonify({'success': True})


@app.route('/media')
def media_page():
    return render_template('media.html', version=VERSION)


@app.route('/media/browse', methods=['GET'])
def media_browse():
    cid = request.args.get('cid', '0')
    success, msg, items = cloud115.list_files(cid, show_dir=1)
    if success:
        return jsonify({'success': True, 'items': items})
    return jsonify({'success': False, 'message': msg})


@app.route('/media/scan', methods=['POST'])
def media_scan():
    cid = request.form.get('cid', '0').strip()
    recursive = request.form.get('recursive', 'true').lower() == 'true'
    files = scan_115_directory(cid, recursive)
    return jsonify({'success': True, 'files': files, 'count': len(files)})


@app.route('/media/identify', methods=['POST'])
def media_identify():
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


@app.route('/media/search', methods=['POST'])
def media_search():
    """手动搜索TMDB，返回多个结果供选择。支持TMDB ID直接识别。"""
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


@app.route('/media/organize', methods=['POST'])
def media_organize():
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


@app.route('/media/tmdb_key', methods=['GET'])
def media_get_tmdb_key():
    return jsonify({'success': True, 'key': get_tmdb_api_key()})


@app.route('/media/tmdb_key', methods=['POST'])
def media_set_tmdb_key():
    key = request.form.get('key', '').strip()
    set_tmdb_api_key(key)
    return jsonify({'success': True, 'message': 'TMDB API Key 已保存'})


@app.route('/media/categories', methods=['GET'])
def media_categories():
    categories = get_all_categories()
    return jsonify({'success': True, 'categories': categories})


@app.route('/media/tree', methods=['GET'])
def media_tree():
    cid = request.args.get('cid', '0')
    depth = int(request.args.get('depth', '3'))
    tree = get_directory_tree(cid, depth)
    return jsonify({'success': True, 'tree': tree})


# ===== 论坛电影搜索 =====

import baidu_forum


@app.route('/baidu')
def baidu_page():
    return render_template('baidu.html', version=VERSION)


@app.route('/baidu/config', methods=['GET', 'POST'])
def baidu_config():
    if request.method == 'GET':
        config = baidu_forum.load_config()
        # 不返回密码明文，前端只需知道是否已配置
        return jsonify({
            'success': True,
            'config': {
                'username': config.get('username', ''),
                'has_password': bool(config.get('password', '')),
            }
        })
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


@app.route('/baidu/test')
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
        config = douban.load_config()
        # 不返回 cookie 明文，只返回是否已配置
        return jsonify({
            'success': True,
            'config': {
                'user_id': config.get('user_id', ''),
                'has_cookie': bool(config.get('cookie', '')),
            }
        })
    cookie = request.form.get('cookie', '').strip()
    user_id = request.form.get('user_id', '').strip()

    def _update(cfg):
        if cookie:
            cfg['cookie'] = encrypt(cookie)
        if user_id:
            cfg['user_id'] = user_id
    douban.update_config(_update)
    return jsonify({'success': True, 'message': '配置已保存'})


@app.route('/douban/check', methods=['POST'])
def douban_check():
    user_id = request.form.get('user_id', '').strip()
    if not user_id:
        config = douban.load_config()
        user_id = config.get('user_id', '')
    if not user_id:
        return jsonify({'success': False, 'message': '请输入豆瓣用户ID'})
    ok, msg = douban.check_cookie(user_id)
    return jsonify({'success': ok, 'message': msg})


@app.route('/douban/fetch', methods=['POST'])
def douban_fetch():
    """获取豆瓣看过的电影列表（按页）"""
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


@app.route('/douban/movie_info', methods=['POST'])
def douban_movie_info():
    """获取单个电影的中文名（访问subject页面）"""
    subject_url = request.form.get('url', '').strip()
    if not subject_url:
        return jsonify({'success': False, 'message': '缺少电影URL'})

    name, err = douban.fetch_movie_chinese_name(subject_url)
    if err:
        return jsonify({'success': False, 'message': err})

    return jsonify({'success': True, 'name': name})


@app.route('/douban/sync', methods=['POST'])
def douban_sync():
    """同步选中的电影到数据库"""
    data = request.get_json()
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
            with open(LOG_FILE, 'w', encoding='utf-8') as f:
                pass
            logger.info('[日志] 日志已清空')
        return jsonify({'success': True, 'message': '日志已清空'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


if __name__ == '__main__':
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    # threaded=True: 多线程处理请求，避免 SSE 长连接阻塞其他请求
    app.run(host='0.0.0.0', port=3698, debug=debug, threaded=True)
