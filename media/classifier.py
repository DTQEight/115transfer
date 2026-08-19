"""分类规则引擎 - 根据TMDB元数据确定二级分类"""

CATEGORY_CONFIG = {
    'movie': {
        'rules': [
            {
                'name': '动画电影',
                'condition': lambda m: 16 in (m.get('genre_ids') or []),
            },
            {
                'name': '华语电影',
                'condition': lambda m: (m.get('original_language') or '') in ('zh', 'cn', 'bo', 'za')
                    or any(c in ('CN', 'HK', 'TW') for c in (m.get('production_countries') or [])),
            },
        ],
        'default': '外语电影',
    },
    'tv': {
        'rules': [
            {
                'name': '纪录片',
                'condition': lambda m: 99 in (m.get('genre_ids') or []),
            },
            {
                'name': '儿童',
                'condition': lambda m: 10762 in (m.get('genre_ids') or []),
            },
            {
                'name': '综艺',
                'condition': lambda m: any(gid in (10764, 10767) for gid in (m.get('genre_ids') or [])),
            },
            {
                'name': '日番',
                'condition': lambda m: 16 in (m.get('genre_ids') or [])
                    and ((m.get('original_language') or '') == 'ja'
                         or any(c == 'JP' for c in (m.get('production_countries') or []))),
            },
            {
                'name': '国漫',
                'condition': lambda m: 16 in (m.get('genre_ids') or [])
                    and ((m.get('original_language') or '') in ('zh', 'cn')
                         or any(c in ('CN', 'HK', 'TW') for c in (m.get('production_countries') or []))),
            },
            {
                'name': '国产剧',
                'condition': lambda m: (m.get('original_language') or '') in ('zh', 'cn')
                    or any(c in ('CN', 'HK', 'TW') for c in (m.get('production_countries') or [])),
            },
            {
                'name': '日韩剧',
                'condition': lambda m: (m.get('original_language') or '') in ('ja', 'ko')
                    or any(c in ('JP', 'KR') for c in (m.get('production_countries') or [])),
            },
            {
                'name': '欧美剧',
                'condition': lambda m: any(c in ('US', 'GB', 'FR', 'DE', 'CA', 'AU', 'IT', 'ES') for c in (m.get('production_countries') or [])),
            },
        ],
        'default': '欧美剧',
    },
}


def classify(media_info):
    """根据TMDB元数据进行分类

    Returns:
        (一级分类, 二级分类)
    """
    media_type = media_info.get('media_type', 'movie')
    primary = '电视剧' if media_type == 'tv' else '电影'

    config = CATEGORY_CONFIG.get(media_type, CATEGORY_CONFIG['movie'])
    for rule in config['rules']:
        try:
            if rule['condition'](media_info):
                return primary, rule['name']
        except Exception:
            continue

    return primary, config['default']


def get_all_categories():
    """获取所有分类配置"""
    return {
        '电影': ['华语电影', '外语电影', '动画电影'],
        '电视剧': ['日番', '国漫', '国产剧', '日韩剧', '欧美剧', '综艺', '儿童', '纪录片'],
    }
