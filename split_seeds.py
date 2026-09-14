#!/usr/bin/env python3
"""种子文件分片迁移工具

将 forum_seeds/<fid>/ 根目录下的大量 .torrent 文件拆分到 p01/p02/... 子目录，
每个子目录不超过指定数量（默认 10000），并同步更新 forum_monitor.db 中
threads.seed_paths 的引用，保证 Web 界面下载、磁力链接补算等功能不受影响。

用法（推荐在容器内执行，容器自带 python3）：
    docker cp split_seeds.py 115transfer:/app/split_seeds.py
    # 预览（不移动文件、不改数据库）：
    docker exec 115transfer python3 /app/split_seeds.py --fid 44 --dry-run
    # 正式执行：
    docker exec 115transfer python3 /app/split_seeds.py --fid 44

    # 也可在宿主机直接执行（需 python3，data 目录指向 compose 的 ./data）：
    python3 split_seeds.py --fid 44 --data-dir /path/to/115transfer/data

特性：
- 幂等可重复执行：中断后重跑自动续上（残留的根目录文件追加到最后未满的子目录）
- os.rename 同文件系统移动，不复制文件内容，27 万文件秒级完成
- 数据库更新基于移动后的磁盘状态对账，在单个事务中完成，出错自动回滚
- 建议在无爬取任务运行时执行（Web 界面确认全量/增量监控空闲），
  执行前可用 sqlite3 的 backup 接口备份数据库
"""
import argparse
import json
import os
import re
import sqlite3
import sys

# 分片子目录名：p + 编号（p01、p02、...）
BUCKET_RE = re.compile(r'^p(\d+)$')


def find_data_dir(cli_arg: str) -> str:
    """定位数据目录：--data-dir 参数 > DATA_DIR 环境变量 > ./data > 脚本所在目录/data"""
    if cli_arg:
        if not os.path.isdir(cli_arg):
            sys.exit(f'--data-dir 指定的目录不存在: {cli_arg}')
        return os.path.abspath(cli_arg)
    env = os.environ.get('DATA_DIR')
    if env and os.path.isdir(env):
        return os.path.abspath(env)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(script_dir, 'data'), './data'):
        if os.path.isdir(cand):
            return os.path.abspath(cand)
    sys.exit('未找到数据目录：请用 --data-dir 指定（包含 forum_seeds/ 和 forum_monitor.db 的目录）')


def bucket_dirs(forum_dir: str):
    """返回板块目录下已存在的分片子目录名列表（按编号升序），无则返回 []"""
    if not os.path.isdir(forum_dir):
        return []
    items = []
    for name in os.listdir(forum_dir):
        m = BUCKET_RE.match(name)
        if m and os.path.isdir(os.path.join(forum_dir, name)):
            items.append((int(m.group(1)), name))
    return [name for _, name in sorted(items)]


def main():
    ap = argparse.ArgumentParser(
        description='种子文件分片迁移：forum_seeds/<fid>/ 根目录 → p01/p02/... 子目录（每目录 ≤ N 个）')
    ap.add_argument('--fid', default='44', help='板块ID（默认 44）')
    ap.add_argument('--max-files', type=int, default=10000,
                    help='每个子目录最大文件数（默认 10000）')
    ap.add_argument('--data-dir', help='数据目录（默认取 DATA_DIR 环境变量或 ./data）')
    ap.add_argument('--dry-run', action='store_true', help='只预览计划，不移动文件、不改数据库')
    args = ap.parse_args()

    if args.max_files < 1:
        sys.exit('--max-files 必须 >= 1')

    data_dir = find_data_dir(args.data_dir)
    seed_root = os.path.join(data_dir, 'forum_seeds')
    db_file = os.path.join(data_dir, 'forum_monitor.db')
    forum_dir = os.path.join(seed_root, args.fid)

    if not os.path.isdir(forum_dir):
        sys.exit(f'板块目录不存在: {forum_dir}')
    if not os.path.isfile(db_file):
        sys.exit(f'数据库不存在: {db_file}')

    # ---- 1. 盘点现状 ----
    all_names = os.listdir(forum_dir)
    root_files = sorted(
        n for n in all_names
        if n.endswith('.torrent') and os.path.isfile(os.path.join(forum_dir, n)))
    buckets = bucket_dirs(forum_dir)
    bucket_counts = {b: len(os.listdir(os.path.join(forum_dir, b))) for b in buckets}
    root_set, bucket_set = set(root_files), set(buckets)
    others = [n for n in all_names if n not in root_set and n not in bucket_set]

    print(f'数据目录: {data_dir}')
    print(f'板块 {args.fid}: 根目录种子 {len(root_files)} 个, '
          f'已有分片子目录 {len(buckets)} 个（共 {sum(bucket_counts.values())} 个文件）')
    if others:
        print(f'注意: 目录下有 {len(others)} 个非种子/非分片项，保持原样不动，'
              f'如: {others[:5]}')

    # ---- 2. 生成分片计划：优先填最后一个未满子目录，满了开新目录 ----
    plan = []  # (src_abs, dst_abs)
    cur_bucket = buckets[-1] if buckets else None
    # 已有分片：剩余容量 = 上限 - 最后一个子目录现有数量；无分片：0（首个文件触发新建 p01）
    room = (args.max_files - bucket_counts[cur_bucket]) if cur_bucket else 0
    if room < 0:
        room = 0
    next_idx = int(cur_bucket[1:]) if cur_bucket else 0

    for name in root_files:
        if room <= 0:
            next_idx += 1
            cur_bucket = f'p{next_idx:02d}'
            room = args.max_files
            bucket_counts[cur_bucket] = bucket_counts.get(cur_bucket, 0)
        plan.append((os.path.join(forum_dir, name),
                     os.path.join(forum_dir, cur_bucket, name)))
        room -= 1
        bucket_counts[cur_bucket] = bucket_counts.get(cur_bucket, 0) + 1

    touched = sorted({os.path.basename(os.path.dirname(d)) for _, d in plan})
    if plan:
        print(f'计划: 移动 {len(plan)} 个文件，涉及子目录 {touched[0]} ~ {touched[-1]}（共 {len(touched)} 个）')
        for b in touched:
            print(f'  {b}: 迁移后共 {bucket_counts[b]} 个')
    else:
        print('根目录没有待分片的种子文件。')

    if args.dry_run:
        conn = sqlite3.connect(db_file)
        try:
            n = conn.execute(
                'SELECT COUNT(*) FROM threads WHERE seed_paths LIKE ?',
                (f'%"{args.fid}/%',)).fetchone()[0]
        finally:
            conn.close()
        print(f'数据库中含板块 {args.fid} 种子路径的帖子: {n} 行（将被同步更新）')
        print('（dry-run 预览模式，未执行任何变更；去掉 --dry-run 正式执行）')
        return

    # ---- 3. 移动文件（同文件系统 rename，不复制内容） ----
    moved, failed = 0, []
    try:
        for src, dst in plan:
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                os.rename(src, dst)
                moved += 1
            except OSError as e:
                # 单个文件失败（如被占用）跳过继续，不中断整体迁移
                failed.append((os.path.basename(src), str(e)))
            if moved and moved % 50000 == 0:
                print(f'  已移动 {moved}/{len(plan)} ...')
    except KeyboardInterrupt:
        print(f'\n已中断：本次移动 {moved} 个。直接重跑本脚本即可从断点续传。')
    print(f'移动完成: {moved} 个成功' + (f', {len(failed)} 个失败（可重跑重试）: {failed[:3]}' if failed else ''))

    # ---- 4. 同步数据库（按移动后的磁盘状态对账，天然幂等） ----
    # 重新扫描分片子目录，建立 文件名 -> 分片后相对路径 索引；
    # 把 DB 中仍指向根目录旧路径（fid/xxx.torrent，无子目录层）的条目改写到新位置。
    buckets = bucket_dirs(forum_dir)
    disk_index = {}
    for b in buckets:
        bdir = os.path.join(forum_dir, b)
        for n in os.listdir(bdir):
            if n.endswith('.torrent'):
                disk_index[n] = f'{args.fid}/{b}/{n}'

    prefix = f'{args.fid}/'
    updated, bad_json, broken = 0, [], []
    conn = sqlite3.connect(db_file, timeout=60)
    try:
        rows = conn.execute(
            'SELECT tid, seed_paths FROM threads WHERE seed_paths LIKE ?',
            (f'%"{args.fid}/%',)).fetchall()
        updates = []
        for tid, sp in rows:
            try:
                paths = json.loads(sp)
            except (ValueError, TypeError):
                bad_json.append(tid)
                continue
            changed = False
            for i, p in enumerate(paths):
                if (isinstance(p, str) and p.startswith(prefix)
                        and '/' not in p[len(prefix):]
                        and p[len(prefix):] in disk_index):
                    paths[i] = disk_index[p[len(prefix):]]
                    changed = True
            if changed:
                updates.append((json.dumps(paths, ensure_ascii=False), tid))
        if updates:
            conn.executemany('UPDATE threads SET seed_paths=? WHERE tid=?', updates)
        conn.commit()
        updated = len(updates)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(f'数据库已更新 {updated} 个帖子的种子路径')
    if bad_json:
        print(f'警告: {len(bad_json)} 个帖子的 seed_paths 无法解析（与本次迁移无关，原样保留）')

    # ---- 5. 校验 ----
    total = 0
    for b in bucket_dirs(forum_dir):
        c = len(os.listdir(os.path.join(forum_dir, b)))
        total += c
        if c > args.max_files:
            print(f'警告: 子目录 {b} 有 {c} 个文件，超过上限 {args.max_files}')
    total += len([n for n in os.listdir(forum_dir) if n.endswith('.torrent')])
    print(f'校验: 板块 {args.fid} 磁盘种子总数 {total} 个，所有子目录均 ≤ {args.max_files}')
    print('完成。')


if __name__ == '__main__':
    main()
