"""全论坛监控爬取模块

功能：
- 全量拉取：首次手动触发，遍历所有板块所有页面
- 增量监控：定时任务，只爬新帖（板块按最新回帖排序，整页都已爬过则停止）
- 保存帖子标题 + 种子文件（不转磁力链接）
- 限流：page_delay / thread_delay / max_pages_per_run / concurrent_threads
- 存储：SQLite 存帖子元数据 + 断点续传进度 + 监控日志；种子文件存磁盘
"""
import os
import re
import json
import time
import uuid
import sqlite3
import threading
import logging
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

import baidu_forum

logger = logging.getLogger('115transfer')

# ==================== 路径与常量 ====================
_DATA_DIR: str = os.environ.get('DATA_DIR', os.path.dirname(os.path.abspath(__file__)))
_DB_FILE: str = os.path.join(_DATA_DIR, 'forum_monitor.db')
_SEED_DIR: str = os.path.join(_DATA_DIR, 'forum_seeds')
_CONFIG_KEY: str = 'monitor'
_BJ_TZ = ZoneInfo("Asia/Shanghai")

# 监控锁：防止全量/增量/手动任务并发执行
_monitor_lock: threading.Lock = threading.Lock()
# 监控运行状态（内存中，UI 查询用）
_monitor_status: Dict[str, Any] = {
    'running': False,
    'mode': '',            # 'full' / 'incremental'
    'started_at': '',
    'current_forum': '',
    'current_page': 0,
    'total_pages': 0,      # 当前板块总页数（用于计算预计用时）
    'forum_started_at': '',  # 当前板块开始爬取时间（ETA 用，多板块任务隔离计时）
    'planned_pages': 0,    # 本次计划爬取页数（增量模式用 max_pages，全量用 total_pages）
    'threads_found': 0,
    'threads_new': 0,
    'seeds_downloaded': 0,
    'message': '',
}

# ==================== 正则：板块发现 + 帖子列表解析 ====================
# Discuz X3.4 板块链接：forum.php?mod=forumdisplay&fid=12
_FORUM_LINK_RE = re.compile(
    r'forum\.php\?mod=forumdisplay&(?:amp;)?fid=(\d+)[^"]*"[^>]*>([^<]+)'
)
# 帖子标题链接：本论坛用自定义模板，标题链接是伪静态格式 {tid}_11.html
# <a href="361832_11.html" title="电影标题..."  style="...">电影标题...</a>
_THREAD_TITLE_RE = re.compile(
    r'<a\s+href="(\d+)_\d+\.html"\s+title="([^"]+)"'
)
# 兼容标准 Discuz 模板：class="s xst"
_THREAD_TITLE_STD_RE = re.compile(
    r'href="forum\.php\?mod=viewthread&(?:amp;)?tid=(\d+)[^"]*"[^>]*class="s xst"[^>]*>([^<]+)</a>'
)
# 帖子作者：<a class="user-name">作者名</a> 或 <cite><a>作者名</a></cite>
_THREAD_AUTHOR_RE = re.compile(r'<a[^>]*class="user-name"[^>]*>([^<]+)</a>')
# 帖子日期：<abbr class="timeago"><span title="2026-7-23 00:17">...</span></abbr> 或 <em>日期</em>
_THREAD_DATE_RE = re.compile(r'<abbr[^>]*class="timeago"[^>]*><span\s+title="([^"]+)"')
# 分页：尾页页码（Discuz 标准分页）
_TOTAL_PAGES_RE = re.compile(r'class="pg"[^>]*>.*?>(\d+)\s*</a>\s*<a[^>]*class="nxt"', re.DOTALL)
# 分页：class="last" 链接中的页码（自定义模板：<a ... class="last">... 9099</a>）
_LAST_PAGE_RE = re.compile(r'class="last"[^>]*>\s*(?:\.\.\.\s*)?(\d+)\s*</a>')
# 分页：伪静态分页链接 forum-44-2.html
_TOTAL_PAGES_RE2 = re.compile(r'href="forum-\d+-(\d+)\.html"')
# 分页：带 filter 的分页链接 ?mod=forumdisplay&fid=44&...&page=N（兼容 &amp; HTML 转义）
_PAGE_NUM_RE = re.compile(r'(?:&|&amp;)page=(\d+)')


def _now_iso() -> str:
    """当前北京时间 ISO 格式字符串"""
    return datetime.now(_BJ_TZ).isoformat()


# ==================== SQLite 初始化 ====================

def _get_db() -> sqlite3.Connection:
    """获取 SQLite 连接（WAL 模式提升并发读写）

    注意：调用方必须负责关闭连接。推荐使用 `with _db_ctx() as conn:` 模式。
    """
    os.makedirs(os.path.dirname(_DB_FILE), exist_ok=True)
    conn = sqlite3.connect(_DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    return conn


@contextmanager
def _db_ctx() -> Any:
    """SQLite 连接上下文管理器：退出时自动 commit/rollback 并关闭连接

    解决 sqlite3.Connection 的 with 语法只 commit 不 close 的泄漏问题。
    """
    conn = _get_db()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _init_db() -> None:
    """初始化表结构"""
    with _db_ctx() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS threads (
                tid TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                fid TEXT,
                forum_name TEXT,
                author TEXT,
                post_date TEXT,
                fetched_at TEXT NOT NULL,
                seed_count INTEGER DEFAULT 0,
                seed_paths TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_threads_fid ON threads(fid);
            CREATE INDEX IF NOT EXISTS idx_threads_fetched ON threads(fetched_at);
            CREATE INDEX IF NOT EXISTS idx_threads_title ON threads(title);

            CREATE TABLE IF NOT EXISTS crawl_progress (
                fid TEXT PRIMARY KEY,
                forum_name TEXT,
                last_page INTEGER DEFAULT 0,
                last_tid TEXT,
                last_crawl_at TEXT,
                total_pages INTEGER,
                mode TEXT
            );

            CREATE TABLE IF NOT EXISTS monitor_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT,
                started_at TEXT,
                finished_at TEXT,
                mode TEXT,
                fid TEXT,
                forum_name TEXT,
                pages_crawled INTEGER,
                threads_found INTEGER,
                threads_new INTEGER,
                seeds_downloaded INTEGER,
                status TEXT,
                message TEXT
            );
        ''')


# ==================== 配置读写（复用 baidu_forum 配置文件） ====================

def get_monitor_config() -> Dict[str, Any]:
    """读取监控配置（从 baidu_forum_config.json 的 monitor 节点）

    默认值为保守限流参数。
    """
    cfg = baidu_forum.load_config()
    m = cfg.get(_CONFIG_KEY, {}) or {}
    return {
        'enabled': bool(m.get('enabled', False)),
        'cron': m.get('cron', '0 4 * * *'),
        'page_delay': float(m.get('page_delay', 5)),
        'thread_delay': float(m.get('thread_delay', 3)),
        'max_pages_per_run': int(m.get('max_pages_per_run', 200)),
        'concurrent_threads': int(m.get('concurrent_threads', 1)),
        'last_full_crawl_at': m.get('last_full_crawl_at', ''),
        'last_incremental_at': m.get('last_incremental_at', ''),
    }


def save_monitor_config(monitor_cfg: Dict[str, Any]) -> None:
    """事务性保存监控配置"""
    def _mutator(cfg: Dict[str, Any]) -> None:
        cfg[_CONFIG_KEY] = monitor_cfg
    baidu_forum.update_config(_mutator)


# ==================== 板块发现 ====================

def discover_forums() -> List[Dict[str, str]]:
    """发现论坛所有板块

    Returns:
        [{'fid': '12', 'name': '电影区'}, ...]
    """
    s = baidu_forum._get_session()
    r = s.get(baidu_forum.BASE + 'forum.php', timeout=(10, 30))
    r.encoding = 'gbk'
    logger.info(f'[论坛监控] 首页响应: HTTP {r.status_code}, 长度={len(r.text)}')

    forums: List[Dict[str, str]] = []
    seen: set = set()
    for m in _FORUM_LINK_RE.finditer(r.text):
        fid = m.group(1)
        name = baidu_forum._strip_html(m.group(2)).strip()
        if fid in seen or not name:
            continue
        seen.add(fid)
        forums.append({'fid': fid, 'name': name})

    # 如果标准正则没匹配到，尝试伪静态格式 forum-XX-1.html
    if not forums:
        logger.info('[论坛监控] 标准板块链接未匹配，尝试伪静态格式')
        for m in re.finditer(r'href="forum-(\d+)-\d+\.html"[^>]*>([^<]+)', r.text):
            fid = m.group(1)
            name = baidu_forum._strip_html(m.group(2)).strip()
            if fid in seen or not name:
                continue
            seen.add(fid)
            forums.append({'fid': fid, 'name': name})

    if not forums:
        # 记录 HTML 片段帮助诊断
        snippet = r.text[:3000] if len(r.text) > 3000 else r.text
        logger.warning(f'[论坛监控] 首页未匹配到板块链接，HTML片段:\n{snippet}')
        # 回退到已知的电影板块 fid=44（filter=sortid&sortid=1 查看全部帖子）
        logger.info('[论坛监控] 回退到默认板块 fid=44')
        forums = [{'fid': '44', 'name': '电影区'}]
    else:
        logger.info(f'[论坛监控] 发现 {len(forums)} 个板块: {forums}')

    return forums


# ==================== 板块帖子列表解析 ====================

def parse_forum_page(html: str, fid: str, forum_name: str) -> Tuple[List[Dict[str, str]], int]:
    """解析板块单页 HTML

    支持两种模板：
    1. 本论坛自定义模板：标题链接是伪静态 {tid}_11.html + title 属性
    2. 标准 Discuz 模板：class="s xst"

    Returns:
        (threads, total_pages)
        threads: [{'tid', 'title', 'fid', 'forum_name', 'author', 'post_date'}, ...]
    """
    threads: List[Dict[str, str]] = []
    seen: set = set()

    # 先尝试自定义模板（伪静态 {tid}_11.html），再尝试标准模板
    matches = list(_THREAD_TITLE_RE.finditer(html))
    if not matches:
        # 兼容标准 Discuz：class="s xst" 格式
        for m in _THREAD_TITLE_STD_RE.finditer(html):
            tid = m.group(1)
            if tid in seen:
                continue
            title = baidu_forum._strip_html(m.group(2)).strip()
            if not title:
                continue
            start = max(0, m.start() - 500)
            end = min(len(html), m.end() + 1500)
            ctx = html[start:end]
            author = ''
            author_m = _THREAD_AUTHOR_RE.search(ctx)
            if author_m:
                author = baidu_forum._strip_html(author_m.group(1)).strip()
            post_date = ''
            date_m = _THREAD_DATE_RE.search(ctx)
            if date_m:
                post_date = baidu_forum._strip_html(date_m.group(1)).strip()
            seen.add(tid)
            threads.append({
                'tid': tid, 'title': title, 'fid': fid,
                'forum_name': forum_name, 'author': author, 'post_date': post_date,
            })
    else:
        # 自定义模板：标题链接伪静态格式，作者和日期在后续兄弟节点
        for m in matches:
            tid = m.group(1)
            if tid in seen:
                continue
            title = m.group(2).strip()
            if not title:
                continue
            # 在标题位置之后取一段上下文，提取作者和日期
            start = max(0, m.start() - 200)
            end = min(len(html), m.end() + 1500)
            ctx = html[start:end]
            author = ''
            author_m = _THREAD_AUTHOR_RE.search(ctx)
            if author_m:
                author = baidu_forum._strip_html(author_m.group(1)).strip()
            post_date = ''
            date_m = _THREAD_DATE_RE.search(ctx)
            if date_m:
                post_date = baidu_forum._strip_html(date_m.group(1)).strip()
            seen.add(tid)
            threads.append({
                'tid': tid, 'title': title, 'fid': fid,
                'forum_name': forum_name, 'author': author, 'post_date': post_date,
            })

    # 总页数：优先从 class="last" 链接提取（最准确）
    total_pages = 1
    lp_m = _LAST_PAGE_RE.search(html)
    if lp_m:
        try:
            total_pages = int(lp_m.group(1))
        except ValueError:
            pass
    if total_pages == 1:
        tp_m = _TOTAL_PAGES_RE.search(html)
        if tp_m:
            try:
                total_pages = int(tp_m.group(1))
            except ValueError:
                pass
    if total_pages == 1:
        # 回退：从所有分页链接中取最大页码
        page_nums = []
        for pm in _TOTAL_PAGES_RE2.finditer(html):
            try:
                page_nums.append(int(pm.group(1)))
            except ValueError:
                pass
        for pm in _PAGE_NUM_RE.finditer(html):
            try:
                page_nums.append(int(pm.group(1)))
            except ValueError:
                pass
        if page_nums:
            total_pages = max(page_nums)
    return threads, total_pages


# ==================== 种子文件下载与保存 ====================

def _save_seed_file(content: bytes, fid: str, tid: str, aid: str) -> str:
    """保存种子文件到磁盘

    Returns:
        相对路径（相对于 _SEED_DIR），如 '12/12345_67890.torrent'
    """
    # 安全校验：fid/tid/aid 只允许字母数字下划线短横，防止路径遍历
    safe_fid = re.sub(r'[^a-zA-Z0-9_-]', '', fid)
    safe_tid = re.sub(r'[^a-zA-Z0-9_-]', '', tid)
    safe_aid = re.sub(r'[^a-zA-Z0-9_-]', '', aid)
    if not safe_fid or not safe_tid or not safe_aid:
        raise ValueError('无效的文件标识符')
    dir_path = os.path.join(_SEED_DIR, safe_fid)
    os.makedirs(dir_path, exist_ok=True)
    filename = f'{safe_tid}_{safe_aid}.torrent'
    filepath = os.path.join(dir_path, filename)
    # 二次校验：确保最终路径未逃出 _SEED_DIR
    if not os.path.abspath(filepath).startswith(os.path.abspath(_SEED_DIR) + os.sep):
        raise ValueError('路径遍历攻击')
    with open(filepath, 'wb') as f:
        f.write(content)
    return os.path.join(safe_fid, filename)


def download_thread_seeds(tid: str, fid: str) -> List[str]:
    """下载帖子所有种子文件

    复用 baidu_forum 的附件发现 + 下载逻辑，但不转磁力链接。
    单个附件失败跳过，不影响其他附件。

    Returns:
        保存的相对路径列表，如 ['12/12345_67890.torrent']
    """
    s = baidu_forum._get_session()
    attachments = baidu_forum._get_thread_attachments_with_session(s, tid)
    if not attachments:
        return []
    saved: List[str] = []
    for att in attachments:
        try:
            content, _ = baidu_forum._download_torrent_with_session(s, att['url'])
            rel_path = _save_seed_file(content, fid, tid, att['aid'])
            saved.append(rel_path)
        except Exception:
            continue  # 非种子附件或下载失败，跳过
    return saved


def recheck_thread_seeds(tid: str) -> Dict[str, Any]:
    """二次拉取单个帖子的种子

    用于对无种子帖子重新确认是否有种子。重新访问帖子页面发现附件并下载。
    若发现种子则更新数据库记录（seed_count、seed_paths、fetched_at）。

    Returns:
        {'tid': str, 'title': str, 'has_seeds': bool, 'seed_count': int, 'message': str}
    """
    # 查询当前帖子记录
    with _db_ctx() as conn:
        cur = conn.execute('SELECT tid, title, fid, forum_name FROM threads WHERE tid=?', (tid,))
        row = cur.fetchone()
    if not row:
        return {'tid': tid, 'title': '', 'has_seeds': False, 'seed_count': 0, 'message': '帖子不存在'}
    thread = dict(row)

    # 重新下载种子
    try:
        seed_paths = download_thread_seeds(tid, thread['fid'] or '')
    except Exception as e:
        logger.error(f'[论坛监控] 二次拉取 {tid} 失败: {e}')
        return {'tid': tid, 'title': thread['title'], 'has_seeds': False, 'seed_count': 0,
                'message': f'下载失败: {e}'}

    # 更新数据库（无论有无种子都更新 fetched_at，记录已二次确认过）
    with _db_ctx() as conn:
        conn.execute('''
            UPDATE threads SET seed_count=?, seed_paths=?, fetched_at=?
            WHERE tid=?
        ''', (len(seed_paths), json.dumps(seed_paths, ensure_ascii=False), _now_iso(), tid))

    has_seeds = len(seed_paths) > 0
    msg = f'发现{len(seed_paths)}个种子' if has_seeds else '仍无种子'
    logger.info(f'[论坛监控] 二次拉取 {tid}({thread["title"][:30]}): {msg}')
    return {
        'tid': tid, 'title': thread['title'],
        'has_seeds': has_seeds, 'seed_count': len(seed_paths), 'message': msg,
    }


# ==================== 存储操作 ====================

def _upsert_thread(thread: Dict[str, str], seed_paths: List[str]) -> bool:
    """插入或更新帖子记录

    Returns:
        True 表示新帖（首次插入），False 表示已存在
    """
    with _db_ctx() as conn:
        cur = conn.execute('SELECT tid FROM threads WHERE tid=?', (thread['tid'],))
        exists = cur.fetchone() is not None
        conn.execute('''
            INSERT INTO threads (tid, title, fid, forum_name, author, post_date, fetched_at, seed_count, seed_paths)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tid) DO UPDATE SET
                title=excluded.title,
                fid=excluded.fid,
                forum_name=excluded.forum_name,
                author=excluded.author,
                post_date=excluded.post_date,
                fetched_at=excluded.fetched_at,
                seed_count=excluded.seed_count,
                seed_paths=excluded.seed_paths
        ''', (
            thread['tid'], thread['title'], thread['fid'], thread['forum_name'],
            thread['author'], thread['post_date'], _now_iso(),
            len(seed_paths), json.dumps(seed_paths, ensure_ascii=False)
        ))
    return not exists


def _get_known_tids(tids: List[str]) -> set:
    """批量查询已存在的 tid 集合"""
    if not tids:
        return set()
    with _db_ctx() as conn:
        placeholders = ','.join('?' * len(tids))
        cur = conn.execute(f'SELECT tid FROM threads WHERE tid IN ({placeholders})', tids)
        return {row['tid'] for row in cur.fetchall()}


def _update_progress(fid: str, forum_name: str, last_page: int, last_tid: str,
                     total_pages: int, mode: str) -> None:
    """更新板块爬取进度"""
    with _db_ctx() as conn:
        conn.execute('''
            INSERT INTO crawl_progress (fid, forum_name, last_page, last_tid, last_crawl_at, total_pages, mode)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fid) DO UPDATE SET
                forum_name=excluded.forum_name,
                last_page=excluded.last_page,
                last_tid=excluded.last_tid,
                last_crawl_at=excluded.last_crawl_at,
                total_pages=excluded.total_pages,
                mode=excluded.mode
        ''', (fid, forum_name, last_page, last_tid, _now_iso(), total_pages, mode))


def _get_progress(fid: str) -> Optional[Dict[str, Any]]:
    """获取板块爬取进度"""
    with _db_ctx() as conn:
        cur = conn.execute('SELECT * FROM crawl_progress WHERE fid=?', (fid,))
        row = cur.fetchone()
        return dict(row) if row else None


def _insert_log(run_id: str, started_at: str, finished_at: str, mode: str,
                fid: str, forum_name: str, pages_crawled: int,
                threads_found: int, threads_new: int,
                seeds_downloaded: int, status: str, message: str) -> None:
    """插入监控日志"""
    with _db_ctx() as conn:
        conn.execute('''
            INSERT INTO monitor_log
                (run_id, started_at, finished_at, mode, fid, forum_name,
                 pages_crawled, threads_found, threads_new, seeds_downloaded, status, message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (run_id, started_at, finished_at, mode, fid, forum_name,
              pages_crawled, threads_found, threads_new, seeds_downloaded, status, message))


# ==================== 状态管理 ====================

def get_status() -> Dict[str, Any]:
    """获取监控运行状态"""
    return dict(_monitor_status)


def _update_status(**kwargs: Any) -> None:
    _monitor_status.update(kwargs)


def cancel() -> bool:
    """请求取消正在运行的监控任务（非阻塞，实际停止由爬取循环检测 running 标志）"""
    if _monitor_status.get('running', False):
        _monitor_status['running'] = False
        _monitor_status['message'] = '正在取消...'
        return True
    return False


# ==================== 核心爬取逻辑 ====================

def _download_seeds_for_thread(thread: Dict[str, str]) -> List[str]:
    """下载单个帖子的种子（独立函数，便于线程池调用）"""
    try:
        return download_thread_seeds(thread['tid'], thread['fid'])
    except Exception:
        return []


def crawl_forum(fid: str, forum_name: str, mode: str,
                page_delay: float, thread_delay: float,
                max_pages: int, concurrent_threads: int,
                run_id: str, started_at: str) -> Dict[str, Any]:
    """爬取单个板块

    Args:
        mode: 'full' 全量 / 'incremental' 增量
        max_pages: 本次最多爬多少页
        concurrent_threads: 每页内新帖种子下载并发数（1=串行）

    Returns:
        {'pages_crawled', 'threads_found', 'threads_new', 'seeds_downloaded', 'message', 'status'}
    """
    s = baidu_forum._get_session()
    pages_crawled = 0
    threads_found = 0
    threads_new = 0
    seeds_downloaded = 0
    message = ''
    status = 'success'

    # 记录当前板块开始时间（ETA 计算，隔离多板块任务的耗时）
    _update_status(forum_started_at=_now_iso())
    # 增量模式：计划爬 max_pages 页；全量模式：计划爬 total_pages 页（解析首页后更新）
    if mode == 'incremental':
        _update_status(planned_pages=max_pages)

    # 断点续传：全量模式下从上次中断的页码继续，而不是每次都从第1页开始
    start_page = 1
    if mode == 'full':
        prog = _get_progress(fid)
        if prog and prog.get('last_page') and prog.get('total_pages'):
            # 仅当上次未完成时才断点续传（last_page < total_pages）
            # 上次已完成（last_page >= total_pages）则从第1页重新爬取新帖
            if prog['last_page'] < prog['total_pages']:
                start_page = prog['last_page']
                logger.info(f'[论坛监控] {forum_name} 全量断点续传：从第{start_page}页开始（总{prog["total_pages"]}页）')
            else:
                logger.info(f'[论坛监控] {forum_name} 上次全量已完成，从第1页重新爬取新帖')
    page = start_page
    # 全量模式：max_pages 设大值（99999），实际靠 total_pages 自然停止
    # 增量模式：max_pages 解释为"最多爬多少页"（相对数量），从第1页开始
    end_page = max_pages
    actual_end = end_page  # 会在解析首页后用 total_pages 更新
    stop = False
    empty_pages_count = 0  # 连续空页计数，达到阈值才停止
    while page <= end_page and page <= actual_end and not stop:
        # 检查取消标志
        if not _monitor_status.get('running', False):
            message = '已被取消'
            status = 'cancelled'
            break

        url = baidu_forum.BASE + f'forum.php?mod=forumdisplay&fid={fid}&filter=sortid&sortid=1&page={page}'
        # 请求+解析合并重试：最多6次尝试（1次原始 + 5次重试）
        # 请求异常或解析到0帖都算失败，重试。5次重试后仍0帖才跳过本页。
        threads: List[Dict[str, str]] = []
        total_pages = 1
        max_attempts = 6
        for attempt in range(max_attempts):
            # 每次尝试前检查取消标志
            if not _monitor_status.get('running', False):
                message = '已被取消'
                status = 'cancelled'
                stop = True
                break
            r = None
            try:
                r = s.get(url, timeout=(10, 30))
                r.encoding = 'gbk'
            except Exception as e:
                logger.warning(f'[论坛监控] {forum_name} 第{page}页第{attempt+1}/{max_attempts}次请求失败: {e}')
                if attempt < max_attempts - 1:
                    time.sleep(5)
                continue
            threads, total_pages = parse_forum_page(r.text, fid, forum_name)
            if total_pages > 0:
                actual_end = min(end_page, total_pages) if mode == 'full' else end_page
                _update_status(total_pages=total_pages)
                if mode == 'full':
                    _update_status(planned_pages=total_pages)
            if threads:
                logger.info(f'[论坛监控] {forum_name} 第{page}页: HTTP {r.status_code}, '
                            f'解析到{len(threads)}帖, 总页数={total_pages}, 爬到第{actual_end}页止')
                break
            # 0帖：记录 HTML 片段帮助诊断，5秒后重试
            snippet = r.text[:1500] if len(r.text) > 1500 else r.text
            logger.warning(f'[论坛监控] {forum_name}(fid={fid}) 第{page}页第{attempt+1}/{max_attempts}次解析到0帖，HTML片段:\n{snippet}')
            if attempt < max_attempts - 1:
                logger.info(f'[论坛监控] {forum_name} 第{page}页5秒后第{attempt+2}次重试')
                time.sleep(5)
        # 被取消则直接退出主循环
        if stop:
            break
        # 6次尝试后仍0帖：跳过本页（不中断任务），连续3页都失败才结束
        if not threads:
            empty_pages_count = empty_pages_count + 1
            logger.error(f'[论坛监控] {forum_name} 第{page}页{max_attempts}次尝试均无帖子，跳过（连续{empty_pages_count}页失败）')
            if empty_pages_count >= 3:
                message = f'连续{empty_pages_count}页{max_attempts}次重试均无帖子，结束'
                break
            page += 1
            time.sleep(page_delay)
            continue
        else:
            empty_pages_count = 0  # 重置连续空页计数

        threads_found += len(threads)

        # 查询本页哪些帖子已存在
        tids_this_page = [t['tid'] for t in threads]
        known = _get_known_tids(tids_this_page)
        new_threads = [t for t in threads if t['tid'] not in known]

        # 增量模式：如果整页都已爬过，说明后面都是更老的旧帖，停止
        if mode == 'incremental' and len(new_threads) == 0 and len(known) > 0:
            message = f'第{page}页全部已爬过，增量结束'
            stop = True
            break

        # 处理新帖：下载种子
        if new_threads:
            if concurrent_threads <= 1:
                # 串行：每个帖子后 sleep thread_delay
                for t in new_threads:
                    if not _monitor_status.get('running', False):
                        message = '已被取消'
                        status = 'cancelled'
                        stop = True
                        break
                    seed_paths = _download_seeds_for_thread(t)
                    is_new = _upsert_thread(t, seed_paths)
                    if is_new:
                        threads_new += 1
                    seeds_downloaded += len(seed_paths)
                    # 每帖实时更新状态，供前端轮询检测变化
                    _update_status(
                        threads_new=threads_new,
                        seeds_downloaded=seeds_downloaded,
                    )
                    time.sleep(thread_delay)
            else:
                # 并发：用线程池下载本页新帖种子
                with ThreadPoolExecutor(max_workers=concurrent_threads) as pool:
                    future_to_thread = {
                        pool.submit(_download_seeds_for_thread, t): t
                        for t in new_threads
                    }
                    for fut in as_completed(future_to_thread):
                        if not _monitor_status.get('running', False):
                            message = '已被取消'
                            status = 'cancelled'
                            stop = True
                            break
                        t = future_to_thread[fut]
                        try:
                            seed_paths = fut.result()
                        except Exception:
                            seed_paths = []
                        is_new = _upsert_thread(t, seed_paths)
                        if is_new:
                            threads_new += 1
                        seeds_downloaded += len(seed_paths)
                        # 每帖实时更新状态
                        _update_status(
                            threads_new=threads_new,
                            seeds_downloaded=seeds_downloaded,
                        )
                # 并发模式批次间隔
                if not stop and page < actual_end:
                    time.sleep(thread_delay)

        # 更新进度（记录本页第一个帖子 tid，即本页最新的帖子）
        _update_progress(fid, forum_name, page, threads[0]['tid'], total_pages, mode)
        _update_status(
            current_forum=forum_name,
            current_page=page,
            threads_found=threads_found,
            threads_new=threads_new,
            seeds_downloaded=seeds_downloaded,
        )

        pages_crawled += 1
        page += 1
        # 页间延迟
        if page <= actual_end and not stop:
            time.sleep(page_delay)

    if not message:
        message = f'完成：爬取{pages_crawled}页，发现{threads_found}帖，新增{threads_new}帖'

    # 写日志
    _insert_log(run_id, started_at, _now_iso(), mode, fid, forum_name,
                pages_crawled, threads_found, threads_new, seeds_downloaded,
                status, message)

    return {
        'pages_crawled': pages_crawled,
        'threads_found': threads_found,
        'threads_new': threads_new,
        'seeds_downloaded': seeds_downloaded,
        'message': message,
        'status': status,
    }


def run_full_crawl() -> Dict[str, Any]:
    """全量拉取所有板块（首次使用）

    遍历所有板块的所有页面（受 max_pages_per_run 限制），
    已存在的帖子跳过种子下载，只下载新帖种子。
    """
    run_id = str(uuid.uuid4())[:8]
    started_at = _now_iso()

    if not _monitor_lock.acquire(blocking=False):
        return {'success': False, 'message': '已有监控任务在运行'}

    try:
        _init_db()
        cfg = get_monitor_config()
        _monitor_status.update({
            'running': True, 'mode': 'full', 'started_at': started_at,
            'current_forum': '', 'current_page': 0, 'total_pages': 0,
            'forum_started_at': '', 'planned_pages': 0,
            'threads_found': 0, 'threads_new': 0, 'seeds_downloaded': 0,
            'message': '正在发现板块...'
        })
        logger.info('[论坛监控] 全量拉取开始')

        forums = discover_forums()
        if not forums:
            _monitor_status.update({'running': False, 'message': '未发现任何板块'})
            logger.warning('[论坛监控] 全量拉取失败：未发现任何板块')
            return {'success': False, 'message': '未发现任何板块，请检查论坛登录状态'}

        total_new = 0
        total_seeds = 0
        forum_results: List[Dict[str, Any]] = []
        for forum in forums:
            if not _monitor_status.get('running', False):
                _monitor_status['message'] = '已取消'
                break
            logger.info(f'[论坛监控] 开始爬取板块: {forum["name"]}(fid={forum["fid"]})')
            # 全量模式：max_pages 设为 99999 表示爬到论坛最后一页（受 total_pages 自然限制）
            # 不再受配置的 max_pages_per_run 限制，否则 9099 页的论坛永远爬不完
            result = crawl_forum(
                forum['fid'], forum['name'], 'full',
                cfg['page_delay'], cfg['thread_delay'],
                99999, cfg['concurrent_threads'],
                run_id, started_at
            )
            total_new += result['threads_new']
            total_seeds += result['seeds_downloaded']
            forum_results.append({'fid': forum['fid'], 'name': forum['name'], **result})

        cfg['last_full_crawl_at'] = _now_iso()
        save_monitor_config(cfg)

        _monitor_status.update({
            'running': False,
            'message': f'全量完成：新增{total_new}帖，下载{total_seeds}种子',
        })
        logger.info(f'[论坛监控] 全量拉取完成：新增{total_new}帖，下载{total_seeds}种子')
        return {
            'success': True,
            'run_id': run_id,
            'forums': forum_results,
            'total_new': total_new,
            'total_seeds': total_seeds,
        }
    except Exception as e:
        _monitor_status.update({'running': False, 'message': f'全量失败: {e}'})
        logger.error(f'[论坛监控] 全量拉取异常: {e}', exc_info=True)
        return {'success': False, 'message': str(e)}
    finally:
        _monitor_lock.release()


def run_incremental() -> Dict[str, Any]:
    """增量监控：只爬每个板块的新帖

    从第1页开始，遇到整页都已爬过则停止该板块。
    限制最多爬 20 页（增量只需覆盖最新帖）。
    """
    run_id = str(uuid.uuid4())[:8]
    started_at = _now_iso()

    if not _monitor_lock.acquire(blocking=False):
        return {'success': False, 'message': '已有监控任务在运行'}

    try:
        _init_db()
        cfg = get_monitor_config()
        _monitor_status.update({
            'running': True, 'mode': 'incremental', 'started_at': started_at,
            'current_forum': '', 'current_page': 0, 'total_pages': 0,
            'forum_started_at': '', 'planned_pages': 0,
            'threads_found': 0, 'threads_new': 0, 'seeds_downloaded': 0,
            'message': '正在发现板块...'
        })

        forums = discover_forums()
        if not forums:
            _monitor_status.update({'running': False, 'message': '未发现任何板块'})
            return {'success': False, 'message': '未发现任何板块，请检查论坛登录状态'}

        # 增量模式最多爬20页
        inc_max_pages = min(cfg['max_pages_per_run'], 20)

        total_new = 0
        total_seeds = 0
        forum_results: List[Dict[str, Any]] = []
        for forum in forums:
            if not _monitor_status.get('running', False):
                _monitor_status['message'] = '已取消'
                break
            result = crawl_forum(
                forum['fid'], forum['name'], 'incremental',
                cfg['page_delay'], cfg['thread_delay'],
                inc_max_pages, cfg['concurrent_threads'],
                run_id, started_at
            )
            total_new += result['threads_new']
            total_seeds += result['seeds_downloaded']
            forum_results.append({'fid': forum['fid'], 'name': forum['name'], **result})

        cfg['last_incremental_at'] = _now_iso()
        save_monitor_config(cfg)

        _monitor_status.update({
            'running': False,
            'message': f'增量完成：新增{total_new}帖，下载{total_seeds}种子',
        })
        return {
            'success': True,
            'run_id': run_id,
            'forums': forum_results,
            'total_new': total_new,
            'total_seeds': total_seeds,
        }
    except Exception as e:
        _monitor_status.update({'running': False, 'message': f'增量失败: {e}'})
        return {'success': False, 'message': str(e)}
    finally:
        _monitor_lock.release()


# ==================== 查询接口 ====================

def list_threads(fid: Optional[str] = None, keyword: str = '',
                 limit: int = 50, offset: int = 0,
                 seed_filter: str = '') -> Tuple[List[Dict[str, Any]], int]:
    """查询帖子列表

    Args:
        fid: 板块ID过滤，None 表示全部
        keyword: 标题关键词模糊搜索
        limit/offset: 分页
        seed_filter: 种子筛选，'no_seeds' 只看无种子帖子，'with_seeds' 只看有种子的帖子，
                     ''（默认）不筛选

    Returns:
        (rows, total)
    """
    with _db_ctx() as conn:
        where: List[str] = []
        params: List[Any] = []
        if fid:
            where.append('fid=?')
            params.append(fid)
        if keyword:
            where.append('title LIKE ?')
            params.append(f'%{keyword}%')
        if seed_filter == 'no_seeds':
            where.append('seed_count = 0')
        elif seed_filter == 'with_seeds':
            where.append('seed_count > 0')
        where_clause = (' WHERE ' + ' AND '.join(where)) if where else ''

        cur = conn.execute(f'SELECT COUNT(*) as c FROM threads{where_clause}', params)
        total = cur.fetchone()['c']

        cur = conn.execute(
            f'SELECT * FROM threads{where_clause} ORDER BY fetched_at DESC LIMIT ? OFFSET ?',
            params + [limit, offset]
        )
        rows = [dict(r) for r in cur.fetchall()]
        return rows, total


def list_forums_with_progress() -> List[Dict[str, Any]]:
    """列出所有板块及其爬取进度"""
    with _db_ctx() as conn:
        cur = conn.execute('''
            SELECT p.fid, p.forum_name, p.last_page, p.last_tid,
                   p.last_crawl_at, p.total_pages, p.mode,
                   (SELECT COUNT(*) FROM threads t WHERE t.fid=p.fid) as thread_count
            FROM crawl_progress p
            ORDER BY p.forum_name
        ''')
        return [dict(r) for r in cur.fetchall()]


def get_statistics() -> Dict[str, Any]:
    """获取统计信息"""
    with _db_ctx() as conn:
        cur = conn.execute('SELECT COUNT(*) as c, SUM(seed_count) as s FROM threads')
        row = cur.fetchone()
        cur2 = conn.execute('SELECT COUNT(DISTINCT fid) as f FROM crawl_progress')
        forums_crawled = cur2.fetchone()['f']
        return {
            'total_threads': row['c'],
            'total_seeds': row['s'] or 0,
            'forums_crawled': forums_crawled,
        }


def list_recent_logs(limit: int = 20) -> List[Dict[str, Any]]:
    """获取最近监控日志"""
    with _db_ctx() as conn:
        cur = conn.execute(
            'SELECT * FROM monitor_log ORDER BY id DESC LIMIT ?',
            (limit,)
        )
        return [dict(r) for r in cur.fetchall()]


def get_dashboard() -> Dict[str, Any]:
    """获取监控统计仪表盘数据

    Returns:
        {
            'total_threads': 总帖子数,
            'threads_with_seeds': 有种子的帖子数,
            'threads_without_seeds': 无种子的帖子数,
            'total_seeds': 种子文件总数,
            'seed_rate': 种子覆盖率百分比,
            'progress': {'fid', 'forum_name', 'last_page', 'total_pages', 'percent'},
            'recent_runs': 最近10次运行记录,
            'daily_stats': 最近7天每日新增帖子和种子数,
        }
    """
    with _db_ctx() as conn:
        # 总帖子数和种子统计
        cur = conn.execute(
            'SELECT COUNT(*) as total, '
            'SUM(CASE WHEN seed_count > 0 THEN 1 ELSE 0 END) as with_seeds, '
            'SUM(CASE WHEN seed_count = 0 THEN 1 ELSE 0 END) as without_seeds, '
            'SUM(seed_count) as total_seeds '
            'FROM threads'
        )
        row = cur.fetchone()
        total_threads = row['total'] or 0
        threads_with_seeds = row['with_seeds'] or 0
        threads_without_seeds = row['without_seeds'] or 0
        total_seeds = row['total_seeds'] or 0
        seed_rate = round(threads_with_seeds / total_threads * 100, 1) if total_threads > 0 else 0

        # 各板块进度
        cur = conn.execute(
            'SELECT fid, forum_name, last_page, total_pages, last_crawl_at, mode '
            'FROM crawl_progress ORDER BY fid'
        )
        progress = []
        for r in cur.fetchall():
            tp = r['total_pages'] or 0
            lp = r['last_page'] or 0
            percent = round(lp / tp * 100, 1) if tp > 0 else 0
            progress.append({
                'fid': r['fid'],
                'forum_name': r['forum_name'],
                'last_page': lp,
                'total_pages': tp,
                'percent': percent,
                'last_crawl_at': r['last_crawl_at'],
                'mode': r['mode'],
            })

        # 最近10次运行记录
        cur = conn.execute(
            'SELECT started_at, finished_at, mode, forum_name, pages_crawled, '
            'threads_new, seeds_downloaded, status '
            'FROM monitor_log ORDER BY id DESC LIMIT 10'
        )
        recent_runs = [dict(r) for r in cur.fetchall()]

        # 最近7天每日统计：按 fetched_at 日期分组
        cur = conn.execute(
            "SELECT substr(fetched_at, 1, 10) as day, "
            "COUNT(*) as threads, SUM(seed_count) as seeds "
            "FROM threads "
            "WHERE fetched_at >= date('now', '-7 days', 'localtime') "
            "GROUP BY substr(fetched_at, 1, 10) ORDER BY day"
        )
        daily_stats = [dict(r) for r in cur.fetchall()]

    return {
        'total_threads': total_threads,
        'threads_with_seeds': threads_with_seeds,
        'threads_without_seeds': threads_without_seeds,
        'total_seeds': total_seeds,
        'seed_rate': seed_rate,
        'progress': progress,
        'recent_runs': recent_runs,
        'daily_stats': daily_stats,
    }


# 模块加载时初始化数据库
try:
    _init_db()
except Exception:
    pass
