#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""豆瓣「看过」列表缺口诊断工具（只读，不修改任何数据）

背景
----
系统状态栏显示豆瓣报告 1223 部，本地库只有 1215 部，差 8 部。
本项目历史上多次出现「豆瓣服务端少渲染」导致的缺口（见 douban.py 的
_CACHE_VERSION v2~v7 注释），但现有代码对缺口 ≤ max(5, 总数5%) 的情况
直接判为「正常」并写进缓存，缺口会被永久固化。

本脚本复刻 douban.fetch_watched_movies 的解析逻辑，逐页实测并回答三个问题：
  1. 客户端到底能访问到多少条？（豆瓣报告的 1223 是否真的可访问）
  2. 缺口出现在哪些页、每页少几条、少的是全局第几个下标？
  3. 缺的这几部，能否和本地 Excel 里已有的记录对上（区分「解析丢弃」和
     「豆瓣隐藏」）？

用法（在项目目录 / 容器 /app 内执行）
--------------------------------------
    # 默认：读实例配置，全量逐页诊断，写诊断报告到当前目录
    python3 diag_douban_gap.py

    # 容器内（数据在挂载卷时）
    docker exec -it 115transfer python3 /app/diag_douban_gap.py --data-dir /app/data

    # 加大重试次数，确认「少渲染」是稳定现象还是偶发限流
    python3 diag_douban_gap.py --retries 5

    # 不要覆盖真实 Cookie，改用手工提供的
    python3 diag_douban_gap.py --cookie 'bid=xxx; dbcl2=xxx; ck=xxx'

输出
----
    控制台摘要 + douban_gap_report.txt（完整逐页明细、缺失下标、片名线索）

本脚本只发出 GET 请求读取 collect 列表页，不写入数据库、不改配置、不删缓存。
"""

import argparse
import glob
import html as html_module
import json
import os
import re
import sys
import time
from datetime import datetime

PER_PAGE = 15
DEFAULT_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
)


# ==================== 配置读取 ====================

def _decrypt(value):
    """解密 ENC[...] 密文，失败或非密文时原样返回

    为避免依赖 crypto_utils，这里内联实现（算法与 crypto_utils.decrypt 一致）。
    """
    if not value or not isinstance(value, str):
        return value or ''
    if not (value.startswith('ENC[') and value.endswith(']')):
        return value
    try:
        import base64
        from Crypto.Cipher import AES
        from Crypto.Protocol.KDF import scrypt
    except ImportError:
        print('[警告] 缺少 pycryptodome，无法解密配置里的 Cookie，请用 --cookie 手工提供')
        return ''

    key = os.environ.get('ENCRYPTION_KEY') or os.environ.get('FLASK_SECRET_KEY', '')
    key = key[:32].ljust(32, '0')
    try:
        decoded = base64.b64decode(value[4:-1])
        salt = decoded[:16]
        nonce = decoded[16:28]
        tag = decoded[28:44]
        data = decoded[44:]
        derived = scrypt(key, salt, 32, N=2 ** 14, r=8, p=1)
        cipher = AES.new(derived, AES.MODE_GCM, nonce=nonce)
        return cipher.decrypt_and_verify(data, tag).decode('utf-8')
    except Exception as e:  # noqa: BLE001 - 诊断脚本需容错
        print(f'[警告] Cookie 解密失败: {type(e).__name__}（若是密钥变更导致，请用 --cookie 手工提供）')
        return ''


def load_settings(args):
    """从实例配置文件读取 user_id / cookie"""
    data_dir = args.data_dir or os.environ.get(
        'DATA_DIR', os.path.dirname(os.path.abspath(__file__)))
    cfg_path = os.path.join(data_dir, 'douban_config.json')

    cfg = {}
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            print(f'[配置] 已读取 {cfg_path}')
        except Exception as e:  # noqa: BLE001
            print(f'[警告] 配置文件读取失败: {e}')
    else:
        print(f'[警告] 未找到配置文件 {cfg_path}（可用 --data-dir 指定数据目录）')

    user_id = (args.user_id or cfg.get('user_id') or '').strip()
    cookie = (args.cookie or os.environ.get('DOUBAN_COOKIE')
              or _decrypt(cfg.get('cookie', ''))).strip()

    if user_id:
        print(f'[配置] user_id = {user_id}')
    if cookie:
        print(f'[配置] cookie 长度 = {len(cookie)}（{cookie[:24]}...）')
    else:
        print('[警告] 未取得 Cookie，请求会以游客身份发起，只能拿到前 ~14 页')
    return data_dir, user_id, cookie


# ==================== 抓取与解析（复刻 douban.py 逻辑） ====================

def build_headers(cookie):
    """与 douban._get_headers 保持一致：补全浏览器请求头，降低被「少渲染」的概率"""
    return {
        'User-Agent': DEFAULT_USER_AGENT,
        'Cookie': cookie,
        'Referer': 'https://movie.douban.com/',
        'Accept': ('text/html,application/xhtml+xml,application/xml;q=0.9,'
                   'image/avif,image/webp,image/apng,*/*;q=0.8,'
                   'application/signed-exchange;v=b3;q=0.7'),
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Cache-Control': 'max-age=0',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'same-origin',
        'Sec-Fetch-User': '?1',
    }


def parse_page(html):
    """解析单页，返回 (movies, claimed_total, detail)

    detail 记录诊断所需的中间量：item 块数、无链接块、页内重复、观看日期序列。
    解析分支与 douban.fetch_watched_movies 完全一致，便于对比定位。
    """
    movies, seen = [], set()
    no_url_blocks, dup_urls, date_seq = [], [], []
    blocks = re.split(r'<div\s+class="item', html)[1:]

    for block in blocks:
        url_m = re.search(r'href="(https://movie\.douban\.com/subject/(\d+)/?)"', block)
        if not url_m:
            no_url_blocks.append(re.sub(r'<[^>]+>', ' ', block)[:160].strip())
            continue
        movie_url = url_m.group(1)
        if not movie_url.endswith('/'):
            movie_url += '/'
        if movie_url in seen:
            dup_urls.append(movie_url)
            continue

        title = ''
        em_m = re.search(r'<em>([^<]+)</em>', block)
        if em_m:
            title = html_module.unescape(em_m.group(1).strip().split(' / ')[0].strip())
        if not title:
            t_m = re.search(r'\stitle="([^"]+)"', block)
            if t_m:
                title = html_module.unescape(t_m.group(1).strip().split(' / ')[0].strip())
        if not title:
            a_m = re.search(r'class="title"[^>]*>\s*<a[^>]*>([^<]+)</a>', block)
            if a_m:
                title = html_module.unescape(a_m.group(1).strip())
        if not title:
            title = 'subject_' + url_m.group(2)

        intro_m = re.search(r'<li class="intro">([^<]+)</li>', block)
        date = ''
        if intro_m:
            d_m = re.search(r'(\d{4}-\d{2}-\d{2})', intro_m.group(1))
            if d_m:
                date = d_m.group(1)

        seen.add(movie_url)
        date_seq.append(date)
        movies.append({'title': title, 'url': movie_url, 'date': date})

    total_m = re.search(r'<h1>[^<]*[\(（](\d+)[\)）]</h1>', html)
    claimed = int(total_m.group(1)) if total_m else len(movies)

    detail = {
        'blocks': len(blocks),
        'no_url_blocks': no_url_blocks,
        'dup_urls': dup_urls,
        'date_seq': date_seq,
    }
    return movies, claimed, detail


def fetch_page(session, user_id, start, cookie, timeout=20):
    """抓取单页，返回 (status, html_or_err)"""
    url = f'https://movie.douban.com/people/{user_id}/collect'
    params = {'start': start, 'sort': 'time', 'tags_sort': 'rec', 'count': PER_PAGE}
    try:
        resp = session.get(url, params=params, headers=build_headers(cookie), timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return -1, f'请求异常: {type(e).__name__}: {e}'
    if resp.status_code != 200:
        return resp.status_code, f'HTTP {resp.status_code}'
    return 200, resp.text


def fetch_with_retry(session, user_id, start, cookie, retries, delay, log):
    """抓取单页并按「块数」取最佳结果

    关键点：正常页面的 item 块数应等于 15。若服务端少渲染，块数会小于 15。
    重试时取「块数最多」的那一次结果，把少渲染丢掉的条目尽量捞回来。
    """
    best = None
    for attempt in range(1, retries + 2):
        status, payload = fetch_page(session, user_id, start, cookie)
        if status != 200:
            log(f'    尝试{attempt}: {payload}')
            time.sleep(delay)
            continue

        movies, claimed, detail = parse_page(payload)
        score = (detail['blocks'], len(movies))
        if best is None or score > best['score']:
            best = {
                'score': score, 'movies': movies, 'claimed': claimed,
                'detail': detail, 'attempts': attempt, 'html_len': len(payload),
            }
        # 满页即最优，无需继续重试
        if detail['blocks'] >= PER_PAGE and len(movies) >= PER_PAGE:
            break
        if attempt <= retries:
            log(f'    尝试{attempt}: 仅 {detail["blocks"]} 块 / {len(movies)} 条，重试…')
            time.sleep(delay)

    if best is not None:
        log(f'    采用: {best["detail"]["blocks"]} 块 / {len(best["movies"])} 条'
            f'（第 {best["attempts"]} 次尝试）')
    return best


# ==================== 本地 Excel 对比 ====================

def load_local(data_dir, explicit=None):
    """读取本地电影库，返回 [{'name','url','page','seq'}]（失败返回 None）"""
    path = explicit
    if not path:
        candidates = [os.path.join(data_dir, 'movies_data.xlsx')]
        candidates += sorted(glob.glob(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'movies_data.xlsx')))
        path = next((p for p in candidates if os.path.exists(p)), None)
    if not path:
        print('[本地库] 未找到 movies_data.xlsx，跳过对比')
        return None, None
    try:
        import pandas as pd
    except ImportError:
        print('[本地库] 缺少 pandas，跳过对比')
        return None, None
    try:
        df = pd.read_excel(path)
    except Exception as e:  # noqa: BLE001
        print(f'[本地库] 读取失败 {path}: {e}')
        return None, None

    rows = []
    for _, r in df.iterrows():
        name = '' if pd.isna(r.get('电影名')) else str(r.get('电影名')).strip()
        if not name:
            continue
        url = '' if pd.isna(r.get('豆瓣链接')) else str(r.get('豆瓣链接')).strip()
        page = r.get('页码')
        seq = r.get('序号')
        rows.append({
            'name': name, 'url': url,
            'page': int(page) if pd.notna(page) else 0,
            'seq': int(seq) if pd.notna(seq) else 0,
        })
    print(f'[本地库] {path}：{len(rows)} 部')
    return rows, path


# ==================== 主流程 ====================

def main():
    ap = argparse.ArgumentParser(
        description='豆瓣「看过」列表缺口诊断工具（只读，不改数据）')
    ap.add_argument('--data-dir', help='数据目录（默认取 DATA_DIR 或脚本所在目录）')
    ap.add_argument('--excel', help='手工指定 movies_data.xlsx 路径')
    ap.add_argument('--user-id', help='豆瓣用户ID（默认读配置）')
    ap.add_argument('--cookie', help='手工提供 Cookie（默认读配置解密）')
    ap.add_argument('--max-pages', type=int, default=300, help='最大页数保护（默认300）')
    ap.add_argument('--retries', type=int, default=2,
                    help='非满页重试次数（默认2，与程序内 PARTIAL_PAGE_RETRY 一致；调大可验证是否偶发限流）')
    ap.add_argument('--delay', type=float, default=2.0, help='页间间隔秒数（默认2.0）')
    ap.add_argument('--out', default='douban_gap_report.txt', help='报告文件路径')
    args = ap.parse_args()

    lines = []

    def log(msg=''):
        print(msg)
        lines.append(str(msg))

    log('=' * 78)
    log('豆瓣「看过」列表缺口诊断报告')
    log(f'生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    log('=' * 78)

    data_dir, user_id, cookie = load_settings(args)
    if not user_id:
        log('\n[中止] 缺少豆瓣 user_id，请用 --user-id 提供')
        write_report(args.out, lines)
        return 2

    try:
        import requests
    except ImportError:
        log('\n[中止] 缺少 requests 库，请在项目环境内执行（pip install -r requirements.txt）')
        write_report(args.out, lines)
        return 2

    session = requests.Session()
    session.trust_env = False  # 与 douban._SESSION 一致：豆瓣直连，不走代理

    # ---- 逐页拉取 ----
    log('')
    log('-' * 78)
    log('一、逐页实测（item 块数 < 15 即为「少渲染」迹象）')
    log('-' * 78)
    log(f'页间隔 {args.delay}s，非满页重试 {args.retries} 次')

    collected = []      # 全部条目（按页序拼接）
    page_stats = []     # 每页统计
    claimed_total = None
    short_pages = []    # 非满页（块数 < 15）
    anomalies = []      # 无链接块 / 页内重复
    truncated = None    # 中止原因
    visited = set()

    for page_no in range(1, args.max_pages + 1):
        start = (page_no - 1) * PER_PAGE
        if start in visited:
            break
        visited.add(start)

        log(f'第 {page_no:>3} 页 (start={start}) …')
        best = fetch_with_retry(session, user_id, start, cookie,
                                args.retries, args.delay, log)
        if best is None:
            truncated = f'第{page_no}页(start={start})全部尝试失败'
            log(f'    [中止] {truncated}')
            break

        movies, detail = best['movies'], best['detail']
        if claimed_total is None:
            claimed_total = best['claimed']
            total_pages = (claimed_total + PER_PAGE - 1) // PER_PAGE
            log(f'    >> 页面报告总数: {claimed_total} 部，应为 {total_pages} 页')

        global_start = len(collected)
        for i, m in enumerate(movies):
            m['global_index'] = global_start + i
            m['page'] = page_no
            m['seq'] = i + 1
        collected.extend(movies)

        stat = {
            'page': page_no, 'start': start, 'blocks': detail['blocks'],
            'parsed': len(movies), 'global_start': global_start,
            'no_url': len(detail['no_url_blocks']), 'dup': len(detail['dup_urls']),
            'date_seq': detail['date_seq'],
        }
        page_stats.append(stat)

        if detail['blocks'] < PER_PAGE:
            short_pages.append(stat)
            log(f'    [少渲染] 块数 {detail["blocks"]} < {PER_PAGE}，'
                f'本页解析 {len(movies)} 条（全局下标 {global_start}~{global_start + len(movies) - 1}）')
        if detail['no_url_blocks']:
            anomalies.append((page_no, '无条目链接块', detail['no_url_blocks']))
            log(f'    [异常] {len(detail["no_url_blocks"])} 个 item 块无条目链接:')
            for frag in detail['no_url_blocks'][:3]:
                log(f'           {frag}')
        if detail['dup_urls']:
            anomalies.append((page_no, '页内重复URL', detail['dup_urls']))
            log(f'    [异常] {len(detail["dup_urls"])} 个页内重复 URL（会被去重丢弃）')

        # 终止判定：与 douban.fetch_all_watched_movies_slow 一致的「拉满即停」
        if claimed_total is not None and len(collected) >= claimed_total:
            log(f'    >> 已达页面报告总数，结束（实际 {len(collected)} 条）')
            break
        # 空页：可能是末页之后，也可能是限流，重试一次区分
        if not movies:
            log('    >> 本页为空，判定为列表末尾')
            break

        time.sleep(args.delay)

    # ---- 缺口分析 ----
    gap = (claimed_total or 0) - len(collected)
    log('')
    log('-' * 78)
    log('二、缺口分析')
    log('-' * 78)
    log(f'页面报告总数 : {claimed_total}')
    log(f'实际可访问数 : {len(collected)}')
    log(f'缺口         : {gap} 部')
    log(f'非满页数量   : {len(short_pages)} 页')
    if gap != 0 and not short_pages and not anomalies:
        log('>> 无任何少渲染/无链接块迹象，却仍有缺口：')
        log('   说明豆瓣把缺口条目排除在列表分页之外（总数包含但不渲染），')
        log('   只能通过 subject 直连或收藏计数接口补齐，无法靠翻页拿到。')
    if gap == 0:
        log('>> 本次实测无缺口：说明历史缺口是当时的临时限流/少渲染，')
        log('   当前 Cookie 与网络条件下可以完整拉取，缺口可通过强制全量刷新修复。')

    # ---- 缺失下标推断 ----
    log('')
    log('-' * 78)
    log('三、缺口位置推断（按观看日期锚定）')
    log('-' * 78)

    missing_anchor = []
    if gap > 0:
        # 每次少渲染都会让后续条目整体前移一格。用每页首条的观看日期作为
        # 锚点：把所有日期按降序排成"应有的"全局时间轴，再看哪些槽位没被占。
        all_dates = []
        for st in page_stats:
            for d in st['date_seq']:
                if d:
                    all_dates.append(d)
        all_dates.sort(reverse=True)
        # 采集到的条目按其日期在时间轴上的位置回收下标（同名日期按出现顺序分配）
        used = {}
        for m in collected:
            d = m['date']
            if not d:
                continue
            idx = used.get(d, 0)
            slots = [i for i, v in enumerate(all_dates) if v == d]
            if idx < len(slots):
                m['inferred_index'] = slots[idx]
                used[d] = idx + 1
        occupied = {m['inferred_index'] for m in collected if 'inferred_index' in m}
        missing_anchor = [{'index': i, 'date': all_dates[i]}
                          for i in range(len(all_dates)) if i not in occupied]
        if missing_anchor:
            log('以下全局下标（按观看日期降序）没有被任何条目占用，即缺口所在：')
            for item in missing_anchor:
                log(f'  全局第 {item["index"] + 1:>4} 位  观看日期 {item["date"]}'
                    f'  → 对应第 {item["index"] // PER_PAGE + 1} 页'
                    f' 序 {item["index"] % PER_PAGE + 1}')
        else:
            log('按日期锚定未发现空洞（可能因为页内同日条目较多，锚定不准）。')

        # 每页缺口量：该页实际条数 vs 15
        log('')
        log('各页实际条数明细（仅列非满页）：')
        if short_pages:
            for st in short_pages:
                log(f'  第 {st["page"]:>3} 页: {st["parsed"]} 条'
                    f'（少 {PER_PAGE - st["parsed"]} 条，全局下标 {st["global_start"]} 起）')
        else:
            log('  无非满页——缺口不在翻页环节')

    # ---- 本地库对比 ----
    log('')
    log('-' * 78)
    log('四、与本地 Excel 库对比')
    log('-' * 78)
    local_rows, local_path = load_local(data_dir, args.excel)
    missing_names = []
    local_only = []
    if local_rows is not None:
        fetched_urls = {m['url'] for m in collected}
        fetched_names = {m['title'] for m in collected}
        by_url = {r['url']: r for r in local_rows if r['url']}

        missing_names = [m for m in collected
                         if m['url'] not in {r['url'] for r in local_rows if r['url']}
                         and m['title'] not in {r['name'] for r in local_rows}]
        log(f'豆瓣可访问但本地库没有的条目: {len(missing_names)} 部')
        for m in missing_names[:30]:
            log(f'  第{m["page"]}页 序{m["seq"]}  {m["title"]}  ({m["url"]})')

        local_only = [r for r in local_rows
                      if r['url'] and r['url'] not in fetched_urls]
        log(f'本地库有但本次未拉到的条目: {len(local_only)} 部')
        for r in local_only[:30]:
            log(f'  第{r["page"]}页 序{r["seq"]}  {r["name"]}  ({r["url"]})')

        if not missing_names and not local_only:
            log('>> 两边完全一致：本地库与本次可访问列表无差异。')

    # ---- 结论与建议 ----
    log('')
    log('=' * 78)
    log('五、结论')
    log('=' * 78)
    if gap == 0 and not missing_names and not local_only:
        log('实测可完整拉取，且与本地库一致 → 历史缺口是临时限流造成。')
        log('建议：清除 douban_movies_cache.json 后强制全量刷新，缺口即可补齐。')
    elif gap == 0 and (missing_names or local_only):
        log('实测可完整拉取，但与本地库存在差异 → 缺口固化在缓存/数据库里。')
        log('建议：清除 douban_movies_cache.json 后强制全量刷新。')
    elif gap > 0:
        log(f'实测确认缺口 {gap} 部：豆瓣的列表分页本身不提供这些条目。')
        if short_pages:
            log(f'其中 {len(short_pages)} 页存在少渲染，属于可重试恢复的类型（本脚本已取最优重试结果）。')
        if missing_anchor:
            log('缺口下标见第三节，可到浏览器打开对应页确认该位置是哪个条目。')
        log('建议：调大 PARTIAL_PAGE_RETRY 并改为「多次结果取并集」，')
        log('      同时取消 max(5, 5%) 容差，让缺口不再被静默写进缓存。')
    log('')
    log(f'完整报告: {os.path.abspath(args.out)}')

    write_report(args.out, lines)

    # 附带 JSON，便于后续比对
    json_path = os.path.splitext(args.out)[0] + '.json'
    try:
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump({
                'generated_at': datetime.now().isoformat(),
                'claimed_total': claimed_total,
                'accessible_total': len(collected),
                'gap': gap,
                'truncated': truncated,
                'page_stats': page_stats,
                'short_pages': short_pages,
                'anomalies': [{'page': p, 'kind': k, 'items': v} for p, k, v in anomalies],
                'missing_index_anchors': missing_anchor,
                'movies': collected,
                'local_only': local_only,
                'fetched_not_local': missing_names,
            }, f, ensure_ascii=False, indent=2)
        print(f'JSON 明细: {os.path.abspath(json_path)}')
    except Exception as e:  # noqa: BLE001
        print(f'[警告] JSON 写入失败: {e}')

    return 0


def write_report(path, lines):
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
    except Exception as e:  # noqa: BLE001
        print(f'[警告] 报告写入失败: {e}')


if __name__ == '__main__':
    sys.exit(main())
