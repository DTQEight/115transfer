from flask import Flask, render_template, request, redirect, url_for, jsonify
import pandas as pd
import os
import shutil
import glob as glob_mod
from datetime import datetime
import zoneinfo
import threading
import cloud115

def get_beijing_time():
    """获取北京时间"""
    return datetime.now(zoneinfo.ZoneInfo("Asia/Shanghai"))

app = Flask(__name__)
DATA_DIR = os.environ.get('DATA_DIR', os.path.dirname(os.path.abspath(__file__)))
os.makedirs(DATA_DIR, exist_ok=True)
EXCEL_FILE = os.path.join(DATA_DIR, 'movies_data.xlsx')
BACKUP_DIR = os.path.join(DATA_DIR, 'backups')
MAX_BACKUPS = 10
data_lock = threading.Lock()

# 版本号
VERSION = "1.0.0"
try:
    with open('VERSION', 'r') as f:
        VERSION = f.read().strip()
except:
    pass

def load_movies():
    if os.path.exists(EXCEL_FILE):
        return pd.read_excel(EXCEL_FILE)
    else:
        df = pd.DataFrame(columns=['序号', '页码', '电影名', '磁力链接', '保存时间'])
        df.to_excel(EXCEL_FILE, index=False)
        return df

def backup_movies():
    if not os.path.exists(EXCEL_FILE):
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = get_beijing_time().strftime('%Y%m%d_%H%M%S')
    backup_file = os.path.join(BACKUP_DIR, f'movies_data_{timestamp}.xlsx')
    shutil.copy2(EXCEL_FILE, backup_file)
    backups = sorted(glob_mod.glob(os.path.join(BACKUP_DIR, 'movies_data_*.xlsx')))
    while len(backups) > MAX_BACKUPS:
        os.remove(backups.pop(0))

def save_movies(df):
    backup_movies()
    df.to_excel(EXCEL_FILE, index=False)

def build_movie_list(df):
    movies = []
    for _, row in df.iterrows():
        magnet = row['磁力链接']
        if pd.isna(magnet) or str(magnet).strip() == '':
            magnet_display = '(空)'
            magnet = ''
            is_empty = True
        else:
            magnet = str(magnet)
            magnet_display = magnet[:50] + '...' if len(magnet) > 50 else magnet
            is_empty = False
        
        movies.append({
            'id': row['序号'],
            'page': row['页码'],
            'name': str(row['电影名']) if not pd.isna(row['电影名']) else '',
            'magnet': magnet,
            'magnet_display': magnet_display,
            'is_empty': is_empty,
            'save_time': row['保存时间']
        })
    return movies

@app.route('/')
def index():
    page_num = request.args.get('page', 1)
    try:
        page_num = int(page_num)
    except ValueError:
        page_num = 1
    
    try:
        with data_lock:
            df = load_movies()
        
        if df.empty:
            return render_template('index.html', movies=[], current_page=0, all_page_nums=[], version=VERSION)
        
        all_page_nums = sorted(df['页码'].unique(), reverse=True)
        
        if page_num not in all_page_nums:
            if all_page_nums:
                page_num = all_page_nums[0]
            else:
                page_num = 0
        
        page_df = df[df['页码'] == page_num]
        movies = build_movie_list(page_df)
        
        return render_template('index.html', 
                              movies=movies, 
                              current_page=page_num, 
                              all_page_nums=all_page_nums,
                              version=VERSION)
    except Exception as e:
        return render_template('index.html', movies=[], current_page=0, all_page_nums=[], version=VERSION,
                              error=f'加载数据失败: {str(e)}')

@app.route('/search')
def search():
    keyword = request.args.get('keyword', '')
    
    try:
        with data_lock:
            df = load_movies()
        
        if not keyword or df.empty:
            return redirect(url_for('index'))
        
        mask = df['电影名'].str.lower().str.contains(keyword.lower(), na=False)
        result_df = df[mask]
        movies = build_movie_list(result_df)
        
        return render_template('search.html', movies=movies, keyword=keyword, version=VERSION)
    except Exception as e:
        return redirect(url_for('index'))

@app.route('/add', methods=['POST'])
def add_movie():
    page = request.form.get('page')
    name = request.form.get('name')
    magnet = request.form.get('magnet', '')
    
    if not page or not name:
        return jsonify({'success': False, 'message': '页码和电影名不能为空'})
    
    try:
        page = int(page)
    except (ValueError, TypeError):
        return jsonify({'success': False, 'message': '页码必须是数字'})
    
    try:
        with data_lock:
            df = load_movies()
            
            page_df = df[df['页码'] == page]
            new_id = int(page_df['序号'].max()) + 1 if not page_df.empty else 1
            new_movie = {
                '序号': new_id,
                '页码': page,
                '电影名': name,
                '磁力链接': magnet,
                '保存时间': get_beijing_time().strftime('%Y-%m-%d %H:%M:%S')
            }
            
            df = pd.concat([df, pd.DataFrame([new_movie])], ignore_index=True)
            save_movies(df)
        
        return jsonify({'success': True, 'message': '添加成功'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'添加失败: {str(e)}'})

@app.route('/delete/<int:movie_id>', methods=['POST'])
def delete_movie(movie_id):
    try:
        with data_lock:
            df = load_movies()
            
            if movie_id not in df['序号'].values:
                return jsonify({'success': False, 'message': '电影记录不存在'})
            
            df = df[df['序号'] != movie_id]
            for pg in df['页码'].unique():
                mask = df['页码'] == pg
                df.loc[mask, '序号'] = range(1, mask.sum() + 1)
            save_movies(df)
        
        return jsonify({'success': True, 'message': '删除成功'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'删除失败: {str(e)}'})

@app.route('/update/<int:movie_id>', methods=['POST'])
def update_movie(movie_id):
    page = request.form.get('page', '').strip()
    name = request.form.get('name', '').strip()
    magnet = request.form.get('magnet', '').strip()
    
    try:
        with data_lock:
            df = load_movies()
            
            if movie_id not in df['序号'].values:
                return jsonify({'success': False, 'message': '电影记录不存在'})
            
            if page:
                try:
                    df.loc[df['序号'] == movie_id, '页码'] = int(page)
                except (ValueError, TypeError):
                    return jsonify({'success': False, 'message': '页码必须是数字'})
            if name:
                df.loc[df['序号'] == movie_id, '电影名'] = name
            if magnet:
                df.loc[df['序号'] == movie_id, '磁力链接'] = magnet
            df.loc[df['序号'] == movie_id, '保存时间'] = get_beijing_time().strftime('%Y-%m-%d %H:%M:%S')
            
            save_movies(df)
        
        return jsonify({'success': True, 'message': '更新成功'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'更新失败: {str(e)}'})

@app.route('/reorder', methods=['POST'])
def reorder_movies():
    order = request.form.get('order', '')
    page = request.form.get('page', '')
    
    if not order or not page:
        return jsonify({'success': False, 'message': '排序数据不完整'})
    
    try:
        id_list = [int(x) for x in order.split(',')]
    except (ValueError, TypeError):
        return jsonify({'success': False, 'message': '排序数据格式错误'})
    
    try:
        page_num = int(page)
        with data_lock:
            df = load_movies()
            
            if df.empty:
                return jsonify({'success': False, 'message': '没有电影数据'})
            
            page_df = df[df['页码'] == page_num]
            other_df = df[df['页码'] != page_num]
            
            id_to_data = {}
            for _, row in page_df.iterrows():
                id_to_data[row['序号']] = row.to_dict()
            
            if not all(mid in id_to_data for mid in id_list):
                return jsonify({'success': False, 'message': '排序数据包含无效记录'})
            
            reordered_rows = [id_to_data[mid] for mid in id_list]
            for idx, row_data in enumerate(reordered_rows, 1):
                row_data['序号'] = idx
            
            reordered = pd.DataFrame(reordered_rows)
            df = pd.concat([other_df, reordered], ignore_index=True)
            save_movies(df)
        
        return jsonify({'success': True, 'message': '排序已保存'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'排序失败: {str(e)}'})

@app.route('/copy_magnet/<int:movie_id>/<int:page>')
def copy_magnet(movie_id, page):
    try:
        with data_lock:
            df = load_movies()
        
        row = df[(df['序号'] == movie_id) & (df['页码'] == page)]
        
        if not row.empty:
            magnet = row.iloc[0]['磁力链接']
            if not pd.isna(magnet) and str(magnet).strip() != '':
                return jsonify({'success': True, 'magnet': str(magnet)})
        
        return jsonify({'success': False, 'message': '磁力链接为空'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'获取失败: {str(e)}'})


@app.route('/cloud115/config', methods=['GET'])
def cloud115_get_config():
    config = cloud115.load_config()
    cookie = config.get('cookie', '')
    masked = cookie[:20] + '...' + cookie[-10:] if len(cookie) > 30 else cookie
    return jsonify({'success': True, 'cookie_masked': masked, 'has_cookie': bool(cookie)})


@app.route('/cloud115/config', methods=['POST'])
def cloud115_set_config():
    cookie = request.form.get('cookie', '').strip()
    if not cookie:
        return jsonify({'success': False, 'message': 'Cookie不能为空'})
    config = cloud115.load_config()
    config['cookie'] = cookie
    cloud115.save_config(config)
    return jsonify({'success': True, 'message': 'Cookie保存成功'})


@app.route('/cloud115/verify', methods=['POST'])
def cloud115_verify():
    success, msg = cloud115.verify_cookie()
    return jsonify({'success': success, 'message': msg})


@app.route('/cloud115/transfer/<int:movie_id>/<int:page>', methods=['POST'])
def cloud115_transfer(movie_id, page):
    try:
        with data_lock:
            df = load_movies()

        row = df[(df['序号'] == movie_id) & (df['页码'] == page)]
        if row.empty:
            return jsonify({'success': False, 'message': '电影记录不存在'})

        magnet = row.iloc[0]['磁力链接']
        if pd.isna(magnet) or str(magnet).strip() == '':
            return jsonify({'success': False, 'message': '磁力链接为空'})

        success, msg = cloud115.add_offline_task(str(magnet))
        return jsonify({'success': success, 'message': msg})
    except Exception as e:
        return jsonify({'success': False, 'message': f'转存失败: {str(e)}'})


@app.route('/cloud115/batch_transfer', methods=['POST'])
def cloud115_batch_transfer():
    page = request.form.get('page', '')
    if not page:
        return jsonify({'success': False, 'message': '未指定页码'})

    try:
        page_num = int(page)
    except (ValueError, TypeError):
        return jsonify({'success': False, 'message': '页码格式错误'})

    try:
        with data_lock:
            df = load_movies()

        if df.empty:
            return jsonify({'success': False, 'message': '没有电影数据'})

        page_df = df[df['页码'] == page_num]
        magnets = []
        for _, row in page_df.iterrows():
            magnet = row['磁力链接']
            if not pd.isna(magnet) and str(magnet).strip() != '':
                magnets.append(str(magnet))

        if not magnets:
            return jsonify({'success': False, 'message': '当前页没有有效的磁力链接'})

        results = cloud115.batch_add_offline_tasks(magnets)
        success_count = sum(1 for r in results if r['success'])
        fail_count = len(results) - success_count
        return jsonify({
            'success': True,
            'message': f'批量转存完成: 成功 {success_count}, 失败 {fail_count}',
            'results': results
        })
    except Exception as e:
        return jsonify({'success': False, 'message': f'批量转存失败: {str(e)}'})


@app.route('/cloud115/tasks', methods=['GET'])
def cloud115_tasks():
    page = request.args.get('page', 1)
    try:
        page = int(page)
    except (ValueError, TypeError):
        page = 1
    success, msg, tasks = cloud115.get_task_list(page)
    return jsonify({'success': success, 'message': msg, 'tasks': tasks})


if __name__ == '__main__':
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(host='0.0.0.0', port=3698, debug=debug)
