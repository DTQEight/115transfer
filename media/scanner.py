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

    # 提取年份（支持末尾无分隔符的情况）
    year = None
    # 先尝试带分隔符的年份
    year_match = re.search(r'[\.\(\[\s_\-](\d{4})[\.\)\]\s_\-]', name)
    if year_match:
        y = int(year_match.group(1))
        if 1900 < y < 2050:
            year = y
    # 再尝试末尾年份 (如 Good.Will.Hunting.1997)
    if not year:
        year_match = re.search(r'[\.\(\[\s_\-](\d{4})$', name)
        if year_match:
            y = int(year_match.group(1))
            if 1900 < y < 2050:
                year = y

    # 清理常见标签
    clean_patterns = [
        r'\[.*?\]',
        r'\(.*?\)',
        r'S\d{1,2}E\d{1,3}',
        r'S\d{1,2}\b',
        r'EP?\d{1,4}\b',
        r'\b4K\b|\b2160p\b|\b1080p\b|\b720p\b|\b480p\b',
        r'BluRay|WEB-DL|WEBRip|HDRip|DVDRip|BDRip|REMUX',
        r'x264|x265|H\.?264|H\.?265|HEVC|AVC|10bit|8bit',
        r'AAC|DTS[-\s]?(?:HD|X)?|FLAC|AC3|Atmos|TrueHD|DD[P+]?\s*\d\.\d',
        r'\d+[Aa]udio',
        r'AMZN|NF|Netflix|iQIYI|Bilibili',
        r'国粤|国语|中字|中英|简繁|双语|多音轨|多国语|特效字幕',
        r'未删减|加长版|导演剪辑|剧场版',
        r'\bCC\b|\bSUBBED\b|\bEXTENDED\b|\bREPACK\b',
        r'\bIMAX\b|\bIMDb\b',
    ]

    cleaned = name
    for pattern in clean_patterns:
        cleaned = re.sub(pattern, '', cleaned, flags=re.IGNORECASE)

    # 去掉末尾发行组名（最后一个 - 后面的纯字母大写串）
    cleaned = re.sub(r'[-\.][A-Z]{2,}$', '', cleaned)

    # 清理多余符号和年份
    cleaned = re.sub(r'[._\-]+', ' ', cleaned).strip()
    cleaned = re.sub(r'\b(19|20)\d{2}\b', '', cleaned)
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
        folder = f.get('folder_name', '')
        # 如果有文件夹名，也解析一下
        folder_parsed = parse_media_name(folder) if folder else None
        # 优先用文件夹名的中文作为搜索名（文件夹名通常是电影中文名）
        search_name = parsed['cleaned_name']
        if folder_parsed and folder_parsed['cleaned_name']:
            # 如果文件夹名包含中文，优先用文件夹名
            if re.search(r'[\u4e00-\u9fff]', folder_parsed['cleaned_name']):
                search_name = folder_parsed['cleaned_name']
        result.append({
            'fid': f['fid'],
            'name': f['name'],
            'size': f.get('size', 0),
            'pickcode': f.get('pickcode', ''),
            'parent_id': f.get('parent_id', cid),
            'folder_name': folder,
            'cleaned_name': search_name,
            'year': parsed['year'] or (folder_parsed['year'] if folder_parsed else None),
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
