from flask import Flask, render_template, request, redirect, url_for, jsonify
import pandas as pd
import os
import shutil
import glob as glob_mod
from datetime import datetime
import zoneinfo
import threading
import hashlib
import cloud115
import wechat_work

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
_movie_cache = {'hash': None, 'data': None}

# 版本号
VERSION = "1.0.0"
try:
    with open('VERSION', 'r') as f:
        VERSION = f.read().strip()
except:
    pass

def load_movies():
    if not os.path.exists(EXCEL_FILE):
        df = pd.DataFrame(columns=['序号', '页码', '电影名', '磁力链接', '保存时间'])
        df.to_excel(EXCEL_FILE, index=False)
        return df

    current_hash = hashlib.md5(open(EXCEL_FILE, 'rb').read()).hexdigest()
    if _movie_cache['hash'] == current_hash and _movie_cache['data'] is not None:
        return _movie_cache['data'].copy()

    df = pd.read_excel(EXCEL_FILE)
    _movie_cache['hash'] = current_hash
    _movie_cache['data'] = df
    return df.copy()

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
    _movie_cache['hash'] = None
    _movie_cache['data'] = None

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
        
        mask = df['电影名'].str.lower().str.contains(keyword.lower(), na=False, regex=False)
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


@app.route('/cloud115/dirs', methods=['GET'])
def cloud115_dirs():
    cid = request.args.get('cid', '0')
    success, msg, dirs = cloud115.get_dir_list(cid)
    return jsonify({'success': success, 'message': msg, 'dirs': dirs})


@app.route('/cloud115/save_path', methods=['GET'])
def cloud115_get_save_path():
    path_id = cloud115.get_default_save_path()
    return jsonify({'success': True, 'path_id': path_id})


@app.route('/cloud115/save_path', methods=['POST'])
def cloud115_set_save_path():
    path_id = request.form.get('path_id', '0')
    cloud115.set_default_save_path(path_id)
    return jsonify({'success': True, 'message': '默认保存目录已更新'})


@app.route('/wechat/config', methods=['GET'])
def wechat_get_config():
    config = wechat_work.load_config()
    return jsonify({
        'success': True,
        'corpid': config.get('corpid', ''),
        'agentid': config.get('agentid', ''),
        'token': config.get('token', ''),
        'encoding_aes_key': config.get('encoding_aes_key', ''),
        'callback_url': config.get('callback_url', ''),
        'proxy_url': config.get('proxy_url', ''),
        'configured': bool(config.get('corpid') and config.get('corpsecret'))
    })


@app.route('/wechat/config', methods=['POST'])
def wechat_set_config():
    corpid = request.form.get('corpid', '').strip()
    corpsecret = request.form.get('corpsecret', '').strip()
    agentid = request.form.get('agentid', '').strip()
    token = request.form.get('token', '').strip()
    encoding_aes_key = request.form.get('encoding_aes_key', '').strip()
    callback_url = request.form.get('callback_url', '').strip()
    proxy_url = request.form.get('proxy_url', '').strip()
    if not corpid or not corpsecret:
        return jsonify({'success': False, 'message': '企业ID和应用Secret不能为空'})
    config = wechat_work.load_config()
    config['corpid'] = corpid
    config['corpsecret'] = corpsecret
    if agentid:
        config['agentid'] = agentid
    if token:
        config['token'] = token
    if encoding_aes_key:
        config['encoding_aes_key'] = encoding_aes_key
    if callback_url:
        config['callback_url'] = callback_url
    if proxy_url:
        config['proxy_url'] = proxy_url
    config.pop('access_token', None)
    wechat_work.save_config(config)
    return jsonify({'success': True, 'message': '企业微信配置保存成功'})


@app.route('/wechat/callback', methods=['GET', 'POST'])
def wechat_callback():
    config = wechat_work.load_config()
    token = config.get('token', '')
    encoding_aes_key = config.get('encoding_aes_key', '')
    corpid = config.get('corpid', '')

    msg_signature = request.args.get('msg_signature', request.args.get('signature', ''))
    timestamp = request.args.get('timestamp', '')
    nonce = request.args.get('nonce', '')
    echostr = request.args.get('echostr', '')

    print(f'[WeChat Callback] Token: {token[:10]}..., Signature: {msg_signature}, Timestamp: {timestamp}, Nonce: {nonce}, Echostr: {echostr[:20] if echostr else "None"}')

    if not token:
        return '未配置企业微信', 500

    crypto = wechat_work.WeChatCrypto(token, encoding_aes_key, corpid) if encoding_aes_key else None

    if request.method == 'GET':
        if not msg_signature or not timestamp or not nonce:
            return 'success'
        if crypto:
            is_valid = crypto.verify_signature(msg_signature, timestamp, nonce, echostr)
            print(f'[WeChat Callback] Signature valid: {is_valid}')
            if is_valid:
                try:
                    decrypted, _ = crypto.decrypt_message(echostr)
                    print(f'[WeChat Callback] Decrypted echostr: {decrypted}')
                    return decrypted
                except Exception as e:
                    print(f'[WeChat Callback] Decrypt error: {e}')
                    return echostr
        return '签名验证失败', 403

    try:
        if crypto:
            if not crypto.verify_signature(msg_signature, timestamp, nonce):
                return '签名验证失败', 403
            import xml.etree.ElementTree as ET
            root = ET.fromstring(request.data)
            encrypt_elem = root.find('Encrypt')
            if encrypt_elem is not None:
                decrypted, from_user = crypto.decrypt_message(encrypt_elem.text)
                msg = wechat_work.parse_message(decrypted)
            else:
                msg = wechat_work.parse_message(request.data)
        else:
            msg = wechat_work.parse_message(request.data)

        if not msg:
            return 'success'

        msg_type = msg.get('MsgType', '')
        from_user = msg.get('FromUserName', '')
        to_user = msg.get('ToUserName', '')

        if msg_type == 'text':
            content = msg.get('Content', '')
            result = wechat_work.handle_text_message(content)

            if isinstance(result, dict):
                try:
                    with data_lock:
                        df = load_movies()
                        page_df = df[df['页码'] == result['page']]
                        new_id = int(page_df['序号'].max()) + 1 if not page_df.empty else 1
                        new_movie = {
                            '序号': new_id,
                            '页码': result['page'],
                            '电影名': result['name'],
                            '磁力链接': result['magnet'],
                            '保存时间': get_beijing_time().strftime('%Y-%m-%d %H:%M:%S')
                        }
                        df = pd.concat([df, pd.DataFrame([new_movie])], ignore_index=True)
                        save_movies(df)
                    reply = f'添加成功\n页码: {result["page"]}\n电影名: {result["name"]}'
                except Exception as e:
                    reply = f'添加失败: {str(e)}'
            else:
                reply = result

            if crypto:
                reply_xml = wechat_work.build_reply_xml(from_user, to_user, reply, crypto)
                return reply_xml, 200, {'Content-Type': 'application/xml'}
            else:
                reply_xml = wechat_work.build_reply_xml(from_user, to_user, reply)
                return reply_xml, 200, {'Content-Type': 'application/xml'}

        elif msg_type == 'event':
            event = msg.get('Event', '')
            if event == 'subscribe':
                reply = '欢迎使用115Transfer！\n发送"帮助"查看使用方法'
                if crypto:
                    reply_xml = wechat_work.build_reply_xml(from_user, to_user, reply, crypto)
                    return reply_xml, 200, {'Content-Type': 'application/xml'}
                else:
                    reply_xml = wechat_work.build_reply_xml(from_user, to_user, reply)
                    return reply_xml, 200, {'Content-Type': 'application/xml'}

        return 'success'
    except Exception as e:
        return 'success'


@app.route('/wechat/proxy', methods=['POST'])
def wechat_proxy():
    try:
        content_type = request.content_type or ''

        if 'json' in content_type:
            data = request.get_json(force=True, silent=True) or {}
            content = data.get('content', data.get('text', data.get('msg', '')))
            from_user = data.get('from_user', data.get('user', data.get('from', '')))
        else:
            content = request.form.get('content', request.form.get('text', request.form.get('msg', '')))
            from_user = request.form.get('from_user', request.form.get('user', request.form.get('from', '')))

        if not content:
            content = request.args.get('content', request.args.get('text', ''))
            from_user = from_user or request.args.get('from_user', request.args.get('user', ''))

        if not content:
            return jsonify({'success': False, 'message': '未收到消息内容'}), 400

        result = wechat_work.handle_text_message(content)

        if isinstance(result, dict):
            try:
                with data_lock:
                    df = load_movies()
                    page_df = df[df['页码'] == result['page']]
                    new_id = int(page_df['序号'].max()) + 1 if not page_df.empty else 1
                    new_movie = {
                        '序号': new_id,
                        '页码': result['page'],
                        '电影名': result['name'],
                        '磁力链接': result['magnet'],
                        '保存时间': get_beijing_time().strftime('%Y-%m-%d %H:%M:%S')
                    }
                    df = pd.concat([df, pd.DataFrame([new_movie])], ignore_index=True)
                    save_movies(df)
                reply = f'添加成功\n页码: {result["page"]}\n电影名: {result["name"]}'
            except Exception as e:
                reply = f'添加失败: {str(e)}'
        else:
            reply = result

        return jsonify({'success': True, 'message': reply})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


if __name__ == '__main__':
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(host='0.0.0.0', port=3698, debug=debug)
