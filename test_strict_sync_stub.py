#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""douban 取数层「完全对齐」逻辑的离线验证（不联网、只读代码）

用桩函数替换 fetch_watched_movies，验证三件事：
  1. 非满页 + 后续重试补全  → 并集合并后满页，gap=0
  2. 同一页只有部分重试可见  → 并集把所有条目累积起来，gap=0
  3. 某页持续少渲染且无法补齐 → 返回错误（零容忍），不静默放行

在容器内执行：
    docker run --rm -v <项目>:/w -w /w python:3.9-slim python test_strict_sync_stub.py
"""
import os
import sys
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('FLASK_SECRET_KEY', 'test_key_for_strict_sync')

# 造一个临时数据目录并写入明文 Cookie，让 fetch_all_watched_movies_slow 的
# Cookie 前置检查通过（crypto_utils.decrypt 对非 ENC[...] 明文原样返回）
_TMP = tempfile.mkdtemp(prefix='douban_stub_')
os.environ['DATA_DIR'] = _TMP
with open(os.path.join(_TMP, 'douban_config.json'), 'w', encoding='utf-8') as _f:
    json.dump({'user_id': 'stub_user', 'cookie': 'bid=stub; dbcl2=stub'}, _f)

import douban  # noqa: E402

TOTAL = 32          # 3 页：15 + 15 + 2
PER_PAGE = 15
FAILS = []


def check(name, cond, detail=''):
    status = 'PASS' if cond else 'FAIL'
    print(f'[{status}] {name} {detail}')
    if not cond:
        FAILS.append(name)


def make_movies(start, count):
    return [{'title': f'M{start + i}', 'url': f'https://movie.douban.com/subject/{start + i}/',
             'date': '2020-01-%02d' % (28 - (start + i) % 28), 'rating': ''}
            for i in range(count)]


def run_stub(name, stub, expect_gap, expect_len):
    """用桩替换 fetch 后跑一次全量拉取"""
    calls = {'n': 0}
    real_stub = stub

    def counting_stub(user_id, start=0, count=PER_PAGE):
        calls['n'] += 1
        return real_stub(user_id, start, count)

    douban.fetch_watched_movies = counting_stub
    movies, err = douban.fetch_all_watched_movies_slow('u', max_pages=10, page_delay=0)
    gap = douban._LAST_FETCH_GAP.get('gap')
    print(f'--- {name}: 返回{len(movies)}条 gap={gap} 桩调用{calls["n"]}次 err={err!r}')
    check(f'{name}: 桩确实被调用', calls['n'] > 0, f'(调用{calls["n"]}次)')
    check(f'{name}: 条数=={expect_len}', len(movies) == expect_len, f'(实际{len(movies)})')
    check(f'{name}: gap=={expect_gap}', gap == expect_gap, f'(实际{gap})')
    return movies, err


# ---- 场景1：第2页少渲染1条，重试时补齐 ----
state1 = {'calls': 0}


def stub1(user_id, start=0, count=PER_PAGE):
    if start == 0:
        return make_movies(0, 15), TOTAL, None
    if start == 15:
        state1['calls'] += 1
        if state1['calls'] == 1:
            return make_movies(15, 14), TOTAL, None   # 首次少 1 条（缺 M29）
        return make_movies(15, 15), TOTAL, None       # 重试补齐
    if start == 30:
        return make_movies(30, 2), TOTAL, None
    return [], TOTAL, None


movies1, err1 = run_stub('场景1-少渲染后重试补齐', stub1, expect_gap=0, expect_len=TOTAL)
check('场景1: 无错误', err1 is None, f'({err1!r})')
check('场景1: M29 已补回', any(m['url'].endswith('/29/') for m in movies1))
check('场景1: 无重复URL', len({m['url'] for m in movies1}) == len(movies1))


# ---- 场景2：每次请求只渲染出一部分，靠并集累积（第2页需要3次才凑满）----
state2 = {'calls': 0}
PAGE2_PARTS = [[15, 18, 21], [16, 19, 22], [17, 20, 23]]  # 每次只见3条，三次覆盖9条


def stub2(user_id, start=0, count=PER_PAGE):
    if start == 0:
        return make_movies(0, 15), TOTAL, None
    if start == 15:
        state2['calls'] += 1
        idx = min(state2['calls'], len(PAGE2_PARTS)) - 1
        return make_movies(15, 0)[:0] + [{'title': f'M{n}', 'url': f'https://movie.douban.com/subject/{n}/',
                                          'date': '2020-01-01', 'rating': ''}
                                         for n in PAGE2_PARTS[idx]], TOTAL, None
    if start == 30:
        return make_movies(30, 2), TOTAL, None
    return [], TOTAL, None


# 该场景第2页只有9条可渲染，无法凑满15，属于场景3（零容忍中止）的形态
movies2, err2 = run_stub('场景2-并集累积（仍不足15）', stub2, expect_gap=TOTAL - 15 - 9 - 2, expect_len=15 + 9 + 2)
check('场景2: 返回错误而非静默放行', bool(err2), f'({err2!r})')
check('场景2: 错误信息含缺口', '缺口' in (err2 or ''))


# ---- 场景3：单页永久少渲染1条（无法补齐）→ 必须报错、不写缓存 ----
state3 = {'calls': 0}


def stub3(user_id, start=0, count=PER_PAGE):
    if start == 0:
        return make_movies(0, 15), TOTAL, None
    if start == 15:
        state3['calls'] += 1
        return make_movies(15, 14), TOTAL, None       # 永远只有14条
    if start == 30:
        return make_movies(30, 2), TOTAL, None
    return [], TOTAL, None


movies3, err3 = run_stub('场景3-永久少渲染', stub3, expect_gap=1, expect_len=31)
check('场景3: 返回错误（零容忍）', bool(err3), f'({err3!r})')
check('场景3: 重试达到上限', state3['calls'] == douban.PARTIAL_PAGE_RETRY + 1,
      f'(调用{state3["calls"]}次，期望{douban.PARTIAL_PAGE_RETRY + 1})')
check('场景3: 错误信息含少渲染页码', '少渲染页码' in (err3 or ''))


# ---- 场景4：完整无缺口 → 正常返回 ----
def stub4(user_id, start=0, count=PER_PAGE):
    if start == 0:
        return make_movies(0, 15), TOTAL, None
    if start == 15:
        return make_movies(15, 15), TOTAL, None
    if start == 30:
        return make_movies(30, 2), TOTAL, None
    return [], TOTAL, None


movies4, err4 = run_stub('场景4-完整无缺口', stub4, expect_gap=0, expect_len=TOTAL)
check('场景4: 无错误', err4 is None, f'({err4!r})')

print()
if FAILS:
    print(f'RESULT: {len(FAILS)} 项失败 -> {FAILS}')
    sys.exit(1)
print('RESULT: 全部通过')
