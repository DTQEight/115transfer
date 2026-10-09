#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""jellyfin.build_in_library_set 诊断名单的回归测试（不联网）

背景 bug：诊断名单把"未对应"写成了 IMDb 与 TMDB 的 AND 判定——
    if (有imdb and imdb不在本地) and (有tmdb and tmdb不在本地)
要求两个 ID 都"存在且都不在本地"才记一条，于是只带单个 ID 的条目
（尤其是只有 TMDB 的条目）永远不会出现在诊断名单里。

本测试固定住以下行为：
  A. 仅带 TMDB、且 TMDB 不在本地  → 必须计入未对应
  B. 仅带 IMDb、且 IMDb 不在本地  → 必须计入未对应
  C. IMDb 命中、TMDB 未命中       → 不算未对应（核心匹配已命中，不得过计数）
  D. 两个 ID 都不在本地           → 计入未对应，且只计一次
  E. 无任何 ID                    → 计入未对应（并计入无ID统计）
  F. 两者都命中                   → 不算未对应
  G. 返回值（已入库集合）不受诊断逻辑影响

在容器内执行：
    docker run --rm -v <项目>:/w -w /w python:3.9-slim python test_jellyfin_diag.py
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import jellyfin  # noqa: E402

FAILS = []
LOGS = []


class Capture(logging.Handler):
    def emit(self, record):
        LOGS.append(record.getMessage())


logging.getLogger('jellyfin').addHandler(Capture())
logging.getLogger('jellyfin').setLevel(logging.INFO)


def check(name, cond, detail=''):
    print(f'[{"PASS" if cond else "FAIL"}] {name} {detail}')
    if not cond:
        FAILS.append(name)


def log_line(prefix):
    for line in LOGS:
        if prefix in line:
            return line
    return ''


# ---- 本地库：2 部，分别有 IMDb / TMDB ----
movies = [
    {'title': '本地片A', 'url': 'https://movie.douban.com/subject/1/',
     'imdb_id': 'tt1000001', 'tmdb_id': '111', 'tmdb_media_type': 'movie'},
    {'title': '本地片B', 'url': 'https://movie.douban.com/subject/2/',
     'imdb_id': '', 'tmdb_id': '222', 'tmdb_media_type': 'tv'},
]

# ---- Jellyfin 侧：覆盖上面 A~G 七种形态 ----
# 注意 media_type 用 jellyfin._parse_item 归一化后的 'movie'/'tv'
# 待检条目放在列表最前，确保落在"明细只打印前20条"的窗口内
items = [
    # A. 仅 TMDB，且不在本地 → 旧的 AND 判定必然漏掉
    {'title': '仅TMDB未对应', 'media_type': 'movie', 'imdb_id': '', 'tmdb_id': '333'},
    # B. 仅 IMDb，且不在本地 → 旧判定也漏掉
    {'title': '仅IMDb未对应', 'media_type': 'movie', 'imdb_id': 'tt3000003', 'tmdb_id': ''},
    # C. IMDb 命中、TMDB 未命中 → 不算未对应
    {'title': 'IMDb命中TMDB不同', 'media_type': 'movie', 'imdb_id': 'tt1000001', 'tmdb_id': '444'},
    # D. 两个都不在本地 → 计一次
    {'title': '两个都未对应', 'media_type': 'movie', 'imdb_id': 'tt4000004', 'tmdb_id': '555'},
    # E. 无任何 ID
    {'title': '无ID条目', 'media_type': 'movie', 'imdb_id': '', 'tmdb_id': ''},
    # F. 两者都命中
    {'title': '两者都命中', 'media_type': 'movie', 'imdb_id': 'tt1000001', 'tmdb_id': '222'},
    # G. 命中本地片B的 TMDB（tv 类型）
    {'title': 'TMDB命中本地B', 'media_type': 'tv', 'imdb_id': '', 'tmdb_id': '222'},
]
# 无关条目 700 条，凑成 707 条（与线上数量一致）
items += [{'title': f'无关{i}', 'media_type': 'movie',
           'imdb_id': f'tt9{i:06d}', 'tmdb_id': f'9{i:05d}'} for i in range(700)]

print(f'[装置] 本地 {len(movies)} 部，Jellyfin {len(items)} 条')

in_lib = jellyfin.build_in_library_set(movies, items)

summary = log_line('匹配完成')
print(f'[摘要] {summary}')

# 未对应名单逐条（日志里的明细行）
listed = [l for l in LOGS if 'Jellyfin侧未对应本地' in l]
mand = [l for l in LOGS if '仅带TMDB' in l]
manm = [l for l in LOGS if '仅带IMDb' in l]


def listed_has(title):
    return any(title in l for l in listed)


check('A: 仅TMDB未对应进入名单', listed_has('仅TMDB未对应'),
      '(这是原 bug 漏掉的形态)')
check('B: 仅IMDb未对应进入名单', listed_has('仅IMDb未对应'))
check('C: IMDb命中者不算未对应', not listed_has('IMDb命中TMDB不同'))
check('D: 两个都未对应进入名单', listed_has('两个都未对应'))
check('E: 无ID条目进入名单', listed_has('无ID条目'))
check('F: 两者都命中者不在名单', not listed_has('两者都命中'))
check('G: TMDB命中本地者不在名单', not listed_has('TMDB命中本地B'))

# 汇总计数：A/B/D/E 四条 + 700 条无关条目 = 704（C、F、G 不算）
# 旧 AND 逻辑会漏掉 A（仅TMDB）与 B（仅IMDb），只报 702
expected_unmatched = 4 + 700
m = summary
check(f'汇总数={expected_unmatched}', f'未对应本地电影{expected_unmatched}条' in m, f'({m})')
check('汇总含仅TMDB计数', '仅TMDB' in m, f'({m})')
check('汇总含仅IMDb计数', '仅IMDb' in m, f'({m})')
check('仅TMDB条目单独列出', any('仅TMDB未对应' in l for l in mand))
check('仅IMDb条目单独列出', any('仅IMDb未对应' in l for l in manm))

# 返回值只受核心匹配影响：F(tt1000001)、G(222) 命中，加上本地片A本身
check('已入库集合含本地片A', 'https://movie.douban.com/subject/1/' in in_lib,
      f'({sorted(in_lib)})')
check('已入库集合含本地片B(TMDB 222)', 'https://movie.douban.com/subject/2/' in in_lib)

print()

# ---- 对照：用同一判定逻辑复算"修复后"的名单，并复现旧的 AND 判定 ----
def fixed_unmatched(item_list):
    """镜像 jellyfin.build_in_library_set 里修复后的诊断判定"""
    li = {str(x.get('imdb_id') or '').strip() for x in movies if str(x.get('imdb_id') or '').strip()}
    lt = {str(x.get('tmdb_id') or '').strip() for x in movies if str(x.get('tmdb_id') or '').strip()}
    out = []
    for it in item_list:
        imdb = str(it.get('imdb_id') or '').strip()
        tmdb = str(it.get('tmdb_id') or '').strip()
        if not imdb and not tmdb:
            out.append(it)
        elif not ((imdb and imdb in li) or (tmdb and tmdb in lt)):
            out.append(it)
    return out


jf_unmatched_actual = fixed_unmatched(items)
old_local_imdb = {str(x.get('imdb_id') or '').strip() for x in movies if str(x.get('imdb_id') or '').strip()}
old_local_tmdb = {str(x.get('tmdb_id') or '').strip() for x in movies if str(x.get('tmdb_id') or '').strip()}
old_unmatched = [
    it for it in items
    if (it.get('imdb_id') and it['imdb_id'] not in old_local_imdb)
    and (it.get('tmdb_id') and it['tmdb_id'] not in old_local_tmdb)
]
print(f'[对照] 旧 AND 判定: {len(old_unmatched)} 条；修复后: {len(jf_unmatched_actual)} 条')
check('对照: 与日志汇总数一致', len(jf_unmatched_actual) == expected_unmatched,
      f'({len(jf_unmatched_actual)} vs {expected_unmatched})')
# 旧逻辑漏掉的必须恰好是"只带 ≤1 个 ID 且确实未对应"的条目
# （带 ≤1 个 ID 但命中了本地 ID 的条目，两边都不该算未对应）
old_ids = {id(it) for it in old_unmatched}
new_ids = {id(it) for it in jf_unmatched_actual}
missed = [it for it in jf_unmatched_actual if id(it) not in old_ids]
spurious = [it for it in old_unmatched if id(it) not in new_ids]
print(f'[对照] 旧漏掉{len(missed)}条: {[it["title"] for it in missed]}；旧多报{len(spurious)}条')
check('对照: 旧逻辑漏掉的都是≤1个ID的条目',
      bool(missed) and all(not it.get('imdb_id') or not it.get('tmdb_id') for it in missed),
      f'({[it["title"] for it in missed]})')
check('对照: 旧逻辑没有多报', len(spurious) == 0, f'({[it["title"] for it in spurious]})')
old_titles = {it['title'] for it in old_unmatched}
check('对照: 旧逻辑漏掉了"仅TMDB未对应"', '仅TMDB未对应' not in old_titles)
check('对照: 旧逻辑漏掉了"仅IMDb未对应"', '仅IMDb未对应' not in old_titles)

print()
if FAILS:
    print(f'RESULT: {len(FAILS)} 项失败 -> {FAILS}')
    sys.exit(1)
print('RESULT: 全部通过')
