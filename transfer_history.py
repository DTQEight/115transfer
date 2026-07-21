"""转存历史记录模块

使用 JSON 文件持久化转存历史，支持：
- 记录单条/批量转存结果
- 查询最近记录
- 统计今日/累计成功失败数
- 自动清理90天前的记录
"""
import json
import os
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional

# 历史记录文件路径（与 Excel 同目录）
_HISTORY_FILE: Optional[str] = None
_history_lock: threading.Lock = threading.Lock()
_RETENTION_DAYS: int = 90  # 保留90天
_MAX_RECORDS: int = 5000   # 最多保留5000条


def _get_history_file() -> str:
    """惰性确定历史文件路径"""
    global _HISTORY_FILE
    if _HISTORY_FILE is None:
        data_dir = os.environ.get('DATA_DIR', os.path.dirname(os.path.abspath(__file__)))
        _HISTORY_FILE = os.path.join(data_dir, 'transfer_history.json')
    return _HISTORY_FILE


_BJ_TZ = ZoneInfo("Asia/Shanghai")


def _now_iso() -> str:
    """当前北京时间 ISO 格式字符串（带时区）"""
    return datetime.now(_BJ_TZ).isoformat()


def _load() -> List[Dict[str, Any]]:
    """读取历史记录（调用方需持有 _history_lock）"""
    path = _get_history_file()
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return []
    except (json.JSONDecodeError, OSError):
        return []


def _save(records: List[Dict[str, Any]]) -> None:
    """写入历史记录（调用方需持有 _history_lock）"""
    path = _get_history_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _cleanup(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """清理过期记录（90天前 或 超过最大数量）"""
    if not records:
        return records
    cutoff = datetime.now(_BJ_TZ) - timedelta(days=_RETENTION_DAYS)
    kept: List[Dict[str, Any]] = []
    for r in records:
        ts = r.get('time', '')
        try:
            dt = datetime.fromisoformat(ts)
            if dt >= cutoff:
                kept.append(r)
        except (ValueError, TypeError):
            kept.append(r)  # 无法解析的时间保留
    # 若仍超过上限，按时间倒序保留最新的 _MAX_RECORDS 条
    if len(kept) > _MAX_RECORDS:
        kept.sort(key=lambda x: x.get('time', ''), reverse=True)
        kept = kept[:_MAX_RECORDS]
    return kept


def add_record(movie_id: Any, page: Any, movie_name: str, magnet: str,
               success: bool, message: str, source: str = 'single') -> None:
    """添加一条转存历史记录

    Args:
        movie_id: 电影序号
        page: 页码
        movie_name: 电影名
        magnet: 磁力链接（会截断50字符）
        success: 是否成功
        message: 结果消息
        source: 来源 'single'单条 'batch'批量 'wechat'企业微信
    """
    record = {
        'time': _now_iso(),
        'movie_id': movie_id,
        'page': page,
        'movie_name': movie_name or '',
        'magnet': (magnet or '')[:50],
        'success': bool(success),
        'message': message or '',
        'source': source,
    }
    with _history_lock:
        records = _load()
        records.append(record)
        records = _cleanup(records)
        _save(records)


def add_batch_records(results: List[Dict[str, Any]], page: Any,
                      movie_map: Optional[Dict[str, Dict[str, Any]]] = None,
                      source: str = 'batch') -> None:
    """批量添加转存历史记录

    Args:
        results: batch_add_offline_tasks 返回的结果列表
        page: 页码
        movie_map: magnet -> {movie_id, movie_name} 映射，用于补充电影信息
        source: 来源
    """
    if not results:
        return
    if movie_map is None:
        movie_map = {}
    now = _now_iso()
    new_records: List[Dict[str, Any]] = []
    for r in results:
        magnet = r.get('magnet', '')
        info = movie_map.get(magnet, {})
        new_records.append({
            'time': now,
            'movie_id': info.get('movie_id', ''),
            'page': page,
            'movie_name': info.get('movie_name', ''),
            'magnet': magnet[:50] if magnet else '',
            'success': bool(r.get('success', False)),
            'message': r.get('message', ''),
            'source': source,
        })
    with _history_lock:
        records = _load()
        records.extend(new_records)
        records = _cleanup(records)
        _save(records)


def get_recent(limit: int = 10) -> List[Dict[str, Any]]:
    """获取最近 N 条转存记录（按时间倒序）"""
    with _history_lock:
        records = _load()
    records.sort(key=lambda x: x.get('time', ''), reverse=True)
    return records[:limit]


def get_statistics() -> Dict[str, Any]:
    """获取转存统计数据

    Returns:
        {
            'today_success': int,
            'today_fail': int,
            'total_success': int,
            'total_fail': int,
        }
    """
    with _history_lock:
        records = _load()

    today_str = datetime.now(_BJ_TZ).strftime('%Y-%m-%d')
    today_success = 0
    today_fail = 0
    total_success = 0
    total_fail = 0
    for r in records:
        is_ok = r.get('success', False)
        ts = r.get('time', '')
        if is_ok:
            total_success += 1
        else:
            total_fail += 1
        if ts.startswith(today_str):
            if is_ok:
                today_success += 1
            else:
                today_fail += 1
    return {
        'today_success': today_success,
        'today_fail': today_fail,
        'total_success': total_success,
        'total_fail': total_fail,
    }


def get_by_name(movie_name: str, limit: int = 20) -> List[Dict[str, Any]]:
    """按电影名查询转存历史（按时间倒序）

    Args:
        movie_name: 电影名（精确匹配）
        limit: 最多返回条数
    """
    if not movie_name:
        return []
    with _history_lock:
        records = _load()
    matched = [r for r in records if r.get('movie_name', '') == movie_name]
    matched.sort(key=lambda x: x.get('time', ''), reverse=True)
    return matched[:limit]
