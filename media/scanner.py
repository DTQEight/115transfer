"""扫描115目录，解析文件名提取标题和年份"""
import re
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cloud115


def parse_media_name(filename):
    """从文件名解析出媒体名称和年份"""
    name = filename
    # 去掉扩展名
    if '.' in name:
        parts = name.rsplit('.', 1)
        if len(parts[-1]) <= 5:
            name = parts[0]

    # 提取年份
    year = None
    year_match = re.search(r'[\.\(\[\s_\-](\d{4})[\.\)\]\s_\-]', name)
    if year_match:
        y = int(year_match.group(1))
        if 1900 < y < 2050:
            year = y

    # 清理常见标签
    clean_patterns = [
        r'\[.*?\]',
        r'S\d{1,2}E\d{1,3}',
        r'S\d{1,2}',
        r'EP?\d{1,4}',
        r'4K|2160p|1080p|720p|480p',
        r'BluRay|WEB-DL|WEBRip|HDRip|DVDRip|BDRip|REMUX',
        r'x264|x265|H\.?264|H\.?265|HEVC|AVC|10bit',
        r'AAC|DTS|FLAC|AC3|Atmos|TrueHD|DD[P+]?\s*\d\.\d',
        r'AMZN|NF|Netflix|iQIYI|Bilibili',
        r'国粤|国语|中字|中英|简繁|双语',
        r'未删减|加长版|导演剪辑|剧场版',
    ]

    cleaned = name
    for pattern in clean_patterns:
        cleaned = re.sub(pattern, '', cleaned, flags=re.IGNORECASE)

    # 清理多余符号
    cleaned = re.sub(r'[._\-]+', ' ', cleaned).strip()
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()

    # 如果年份没提取到，再从原名尝试
    if not year:
        year_match = re.search(r'(\d{4})', filename)
        if year_match:
            y = int(year_match.group(1))
            if 1900 < y < 2050:
                year = y

    return {
        'original_name': filename,
        'cleaned_name': cleaned,
        'year': year,
    }


def scan_115_directory(cid, recursive=True):
    """扫描115目录，返回视频文件列表（带解析信息）"""
    if recursive:
        video_files = cloud115.get_video_files_recursive(cid)
    else:
        success, msg, items = cloud115.list_files(cid, show_dir=0)
        video_files = [item for item in items if item['type'] == 'file' and cloud115.is_video_file(item['name'])]

    result = []
    for f in video_files:
        parsed = parse_media_name(f['name'])
        result.append({
            'fid': f['fid'],
            'name': f['name'],
            'size': f.get('size', 0),
            'pickcode': f.get('pickcode', ''),
            'parent_id': f.get('parent_id', cid),
            'cleaned_name': parsed['cleaned_name'],
            'year': parsed['year'],
        })

    return result


def get_directory_tree(cid='0', max_depth=3, current_depth=0):
    """获取目录树结构"""
    if current_depth >= max_depth:
        return []

    tree = []
    success, msg, items = cloud115.list_files(cid, show_dir=1)
    if not success:
        return tree

    dirs = [item for item in items if item['type'] == 'dir']
    for d in dirs:
        node = {
            'cid': d['cid'],
            'name': d['name'],
            'children': get_directory_tree(d['cid'], max_depth, current_depth + 1),
        }
        tree.append(node)

    return tree
