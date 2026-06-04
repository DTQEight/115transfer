"""115文件移动（分类执行）"""
import sys
import os
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

    同一父目录下的多个视频文件会作为一个整体（整个文件夹）移动。

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

    # 按 parent_id 分组
    folder_groups = {}  # parent_id -> {category_key, files}
    for file_info in file_list:
        parent_id = str(file_info.get('parent_id', ''))
        category_key = f'{file_info["primary"]}/{file_info["secondary"]}'
        if parent_id not in folder_groups:
            folder_groups[parent_id] = {'category_key': category_key, 'files': []}
        folder_groups[parent_id]['files'].append(file_info)

    moved_dirs = set()  # 已移动的目录cid，避免重复

    for parent_id, group in folder_groups.items():
        category_key = group['category_key']
        files = group['files']
        target_cid = dir_map.get(category_key)

        if not target_cid:
            for f in files:
                results['failed'].append({
                    'fid': f['fid'],
                    'name': f.get('name', ''),
                    'reason': f'分类目录不存在: {category_key}',
                })
            continue

        # 如果 parent_id 不是源目录且不为0，说明在子文件夹中，移动整个文件夹
        if parent_id and parent_id != str(source_cid) and parent_id != '0' and parent_id not in moved_dirs:
            success, msg = cloud115.move_files([parent_id], target_cid)
            if success:
                moved_dirs.add(parent_id)
                for f in files:
                    results['success'].append({
                        'fid': f['fid'],
                        'name': f.get('name', ''),
                        'category': category_key,
                        'mode': 'folder',
                        'folder_id': parent_id,
                    })
            else:
                # 文件夹移动失败，尝试逐个移动文件
                for f in files:
                    success2, msg2 = cloud115.move_files([f['fid']], target_cid)
                    if success2:
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
                            'reason': msg2,
                        })
        else:
            # 直接在源目录下的文件，逐个移动
            for f in files:
                if f['fid'] in moved_dirs:
                    continue
                success, msg = cloud115.move_files([f['fid']], target_cid)
                if success:
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

    return results
