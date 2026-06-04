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


def organize_files(file_list, root_cid='0'):
    """将文件列表按分类移动到对应目录

    Args:
        file_list: [{'fid': ..., 'primary': '电影', 'secondary': '国语电影'}, ...]
        root_cid: 分类根目录cid

    Returns:
        {'success': [...], 'failed': [...]}
    """
    from .classifier import get_all_categories

    categories = get_all_categories()
    dir_map = ensure_category_dirs(root_cid, categories)

    results = {'success': [], 'failed': []}

    for file_info in file_list:
        fid = file_info['fid']
        primary = file_info['primary']
        secondary = file_info['secondary']
        file_name = file_info.get('name', '')

        category_key = f'{primary}/{secondary}'
        target_cid = dir_map.get(category_key)

        if not target_cid:
            results['failed'].append({
                'fid': fid,
                'name': file_name,
                'reason': f'分类目录不存在: {category_key}',
            })
            continue

        success, msg = cloud115.move_files([fid], target_cid)
        if success:
            results['success'].append({
                'fid': fid,
                'name': file_name,
                'category': category_key,
            })
        else:
            results['failed'].append({
                'fid': fid,
                'name': file_name,
                'reason': msg,
            })

    return results
