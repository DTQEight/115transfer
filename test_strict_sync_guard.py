#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""app.py 防误删闸门的离线集成验证（不联网）

构造"Cookie 半失效"场景：豆瓣只返回本地库条数的一半（恰好等于旧的 50% 阈值，
旧代码会放行并重建、把另一半删掉），验证三道闸门能拦住、Excel 一字节不变。

在容器内执行：
    docker run --rm -v <项目>:/w -w /w python:3.9-slim \
        sh -c "pip install --quiet flask pandas openpyxl requests pycryptodome numpy flask-cors python-json-logger apscheduler; python test_strict_sync_guard.py"
"""
import hashlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TMP = tempfile.mkdtemp(prefix='guard_test_')
os.environ['DATA_DIR'] = TMP
os.environ.setdefault('FLASK_SECRET_KEY', 'guard_test_secret_key_long_enough_x')
os.environ.setdefault('APP_PASSWORD', 'test_password')

# 写入豆瓣配置（提供 user_id，否则 _do_douban_auto_sync 会在第一步就跳过）
with open(os.path.join(TMP, 'douban_config.json'), 'w', encoding='utf-8') as _f:
    json.dump({'user_id': 'guard_test_user', 'cookie': 'bid=stub'}, _f)

FAILS = []


def check(name, cond, detail=''):
    print(f'[{"PASS" if cond else "FAIL"}] {name} {detail}')
    if not cond:
        FAILS.append(name)


def sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


import pandas as pd  # noqa: E402
import app as app_mod  # noqa: E402
import douban  # noqa: E402

EXCEL = app_mod.EXCEL_FILE
print(f'[装置] DATA_DIR={TMP}')

# ---- 造本地库：20 部 ----
rows = [{'序号': i + 1, '页码': i // 15 + 1, '电影名': f'本地电影{i + 1}',
         '磁力链接': f'magnet:?xt=urn:btih:{i:040d}', '保存时间': '2026-01-01 00:00:00',
         '豆瓣链接': f'https://movie.douban.com/subject/{1000 + i}/',
         '已入库': '否', 'IMDB_ID': '', 'TMDB_ID': ''} for i in range(20)]
pd.DataFrame(rows).to_excel(EXCEL, index=False)
before_hash = sha(EXCEL)
before_rows = len(pd.read_excel(EXCEL))
print(f'[装置] 本地库 {before_rows} 部，sha256={before_hash[:16]}…')

# ---- 场景A：豆瓣只返回 10 部（= 本地库 50%，恰好躲过旧的"跌到一半以下"检查）----
half = [{'title': f'豆瓣电影{i + 1}', 'url': f'https://movie.douban.com/subject/{2000 + i}/',
         'year': '2020', 'rating': ''} for i in range(10)]
douban.fetch_all_watched_movies_cached = lambda *a, **k: (half, None)
app_mod.douban.fetch_all_watched_movies_cached = lambda *a, **k: (half, None)

app_mod._do_douban_auto_sync()

after_hash = sha(EXCEL)
after_rows = len(pd.read_excel(EXCEL))
status = app_mod._auto_sync_status.get('last_result', '')
print(f'[结果A] 状态: {status}')
print(f'[结果A] Excel: {before_rows} -> {after_rows} 部, sha 相同={after_hash == before_hash}')

check('场景A: Excel 未被修改（哈希一致）', after_hash == before_hash)
check('场景A: 行数未变', after_rows == before_rows, f'({before_rows} -> {after_rows})')
check('场景A: 状态报出缩水拦截', ('缩水' in status or '落盘前拦截' in status or '异常' in status),
      f'({status[:80]}…)')
check('场景A: 状态不是"成功"', not status.startswith('成功'), f'({status[:40]}…)')

# ---- 场景B：豆瓣返回 0 部（Cookie 完全失效）----
app_mod._auto_sync_status['last_result'] = ''
app_mod.douban.fetch_all_watched_movies_cached = lambda *a, **k: ([], None)
app_mod._do_douban_auto_sync()
after2_hash = sha(EXCEL)
status_b = app_mod._auto_sync_status.get('last_result', '')
print(f'[结果B] 状态: {status_b}')
check('场景B: Excel 未被修改', after2_hash == before_hash)
check('场景B: 行数未变', len(pd.read_excel(EXCEL)) == before_rows)
check('场景B: 状态不是"成功"', not status_b.startswith('成功'), f'({status_b[:40]}…)')

# ---- 场景C：豆瓣返回 21 部（正常增长）→ 应当重建成功 ----
app_mod._auto_sync_status['last_result'] = ''
more = half + [{'title': f'新增电影{i}', 'url': f'https://movie.douban.com/subject/{3000 + i}/',
                'year': '2021', 'rating': ''} for i in range(11)]
app_mod.douban.fetch_all_watched_movies_cached = lambda *a, **k: (more, None)
app_mod._do_douban_auto_sync()
after3_rows = len(pd.read_excel(EXCEL))
status_c = app_mod._auto_sync_status.get('last_result', '')
print(f'[结果C] 状态: {status_c}')
print(f'[结果C] Excel: {after3_rows} 部')
check('场景C: 正常增长时正常重建', after3_rows == 21, f'(实际{after3_rows})')
check('场景C: 状态为成功', status_c.startswith('成功'), f'({status_c[:40]}…)')

print()
if FAILS:
    print(f'RESULT: {len(FAILS)} 项失败 -> {FAILS}')
    sys.exit(1)
print('RESULT: 全部通过')
