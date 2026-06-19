"""115文件移动（分类执行）"""
import sys
import os
import time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cloud115


def find_or_create_dir(parent_cid, dir_name):
    """查找或创建目录，返回目录cid"""
    success, msg, items = cloud115.list_files(parent_cid, show_dir=1)
    if success:
        for item in items:
            if item['type'] == 'dir' and item['name'] == dir_name:
                return item['cid']

    success, msg = cloud115.create_dir(parent_cid, dir_name)
    if success:
        success, msg, items = cloud115.list_files(parent_cid, show_dir=1)
        if success:
            for item in items:
                if item['type'] == 'dir' and item['name'] == dir_name:
                    return item['cid']
    return None


def ensure_category_dirs(root_cid, categories):
    """确保分类目录存在，返回 {分类名: cid} 映射"""
    dir_map = {}
    for primary, secondaries in categories.items():
        primary_cid = find_or_create_dir(root_cid, primary)
        if not primary_cid:
            continue
        for secondary in secondaries:
            secondary_cid = find_or_create_dir(primary_cid, secondary)
            if secondary_cid:
                dir_map[f'{primary}/{secondary}'] = secondary_cid
    return dir_map


def organize_files(file_list, root_cid='0', source_cid='0'):
    """将文件/文件夹按分类移动到对应目录

    - 同一文件夹下的文件若属于同一分类，整个文件夹移动
    - 同一文件夹下的文件若属于不同分类，逐个移动文件（避免整个文件夹只能进一个分类）
    - 带重试机制，避免单次失败需要多次点击

    Args:
        file_list: [{'fid': ..., 'parent_id': ..., 'primary': '电影', 'secondary': '国语电影', 'name': ...}, ...]
        root_cid: 分类根目录cid
        source_cid: 扫描源目录cid（用于判断是否需要移动文件夹）

    Returns:
        {'success': [...], 'failed': [...]}
    """
    from .classifier import get_all_categories

    categories = get_all_categories()
    dir_map = ensure_category_dirs(root_cid, categories)

    results = {'success': [], 'failed': []}

    # 按 parent_id -> {category_key -> [files]} 分组
    # 这样可以判断同一文件夹下的文件是否属于同一分类
    folder_groups = {}
    for file_info in file_list:
        parent_id = str(file_info.get('parent_id', ''))
        category_key = f'{file_info["primary"]}/{file_info["secondary"]}'
        if parent_id not in folder_groups:
            folder_groups[parent_id] = {}
        if category_key not in folder_groups[parent_id]:
            folder_groups[parent_id][category_key] = []
        folder_groups[parent_id][category_key].append(file_info)

    moved_dirs = set()  # 已移动的目录cid，避免重复
    moved_files = set()  # 已移动的文件fid，避免重复

    def move_with_retry(file_ids, target_cid, max_retries=3):
        """带重试的移动，应对115 API临时失败/限流"""
        last_msg = ''
        for attempt in range(max_retries):
            success, msg = cloud115.move_files(file_ids, target_cid)
            if success:
                return True, msg
            last_msg = msg
            if attempt < max_retries - 1:
                time.sleep(1 * (attempt + 1))
        return False, last_msg

    for parent_id, categories_in_folder in folder_groups.items():
        # 只有当目录下所有文件属于同一分类时，才移动整个文件夹
        # 多分类时必须逐个移动文件，否则整个文件夹只能进一个分类
        single_category = len(categories_in_folder) == 1
        can_move_folder = (
            single_category
            and parent_id
            and parent_id != str(source_cid)
            and parent_id != '0'
            and parent_id not in moved_dirs
        )

        for category_key, files in categories_in_folder.items():
            target_cid = dir_map.get(category_key)
            if not target_cid:
                for f in files:
                    results['failed'].append({
                        'fid': f['fid'],
                        'name': f.get('name', ''),
                        'reason': f'分类目录不存在: {category_key}',
                    })
                continue

            if can_move_folder:
                # 移动整个文件夹
                success, msg = move_with_retry([parent_id], target_cid)
                if success:
                    moved_dirs.add(parent_id)
                    for f in files:
                        moved_files.add(f['fid'])
                        results['success'].append({
                            'fid': f['fid'],
                            'name': f.get('name', ''),
                            'category': category_key,
                            'mode': 'folder',
                            'folder_id': parent_id,
                        })
                    continue
                # 文件夹移动失败，回退到逐个移动文件

            # 逐个移动文件
            for f in files:
                if f['fid'] in moved_files:
                    continue
                success, msg = move_with_retry([f['fid']], target_cid)
                if success:
                    moved_files.add(f['fid'])
                    results['success'].append({
                        'fid': f['fid'],
                        'name': f.get('name', ''),
                        'category': category_key,
                        'mode': 'file',
                    })
                else:
                    results['failed'].append({
                        'fid': f['fid'],
                        'name': f.get('name', ''),
                        'reason': msg,
                    })
                # 小延迟，避免请求过快被115限流
                time.sleep(0.3)

    return results
