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
import douban

user_states = {}

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
    # 过滤掉重复的表头行（序号列为非数字的行）
    if not df.empty:
        try:
            pd.to_numeric(df['序号'], errors='raise')
        except (ValueError, TypeError):
            # 序号列有非数字行，过滤掉
            df = df[pd.to_numeric(df['序号'], errors='coerce').notna()].reset_index(drop=True)
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
        
        all_page_nums = sorted(df['页码'].unique())
        
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
    page = request.args.get('page', type=int)
    try:
        with data_lock:
            df = load_movies()
            
            if page is not None:
                mask = (df['序号'] == movie_id) & (df['页码'] == page)
            else:
                mask = df['序号'] == movie_id
            
            if not mask.any():
                return jsonify({'success': False, 'message': '电影记录不存在'})
            
            df = df[~mask]
            for pg in df['页码'].unique():
                m = df['页码'] == pg
                df.loc[m, '序号'] = range(1, m.sum() + 1)
            save_movies(df)
        
        return jsonify({'success': True, 'message': '删除成功'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'删除失败: {str(e)}'})

@app.route('/update/<int:movie_id>', methods=['POST'])
def update_movie(movie_id):
    page = request.form.get('page', '').strip()
    name = request.form.get('name')
    magnet = request.form.get('magnet')
    
    try:
        with data_lock:
            df = load_movies()
            
            if not page:
                return jsonify({'success': False, 'message': '页码不能为空'})
            
            try:
                page_int = int(page)
            except (ValueError, TypeError):
                return jsonify({'success': False, 'message': '页码必须是数字'})
            
            mask = (df['序号'] == movie_id) & (df['页码'] == page_int)
            if not mask.any():
                return jsonify({'success': False, 'message': '电影记录不存在'})
            
            if name is not None:
                df.loc[mask, '电影名'] = name
            if magnet is not None:
                df.loc[mask, '磁力链接'] = magnet
            df.loc[mask, '保存时间'] = get_beijing_time().strftime('%Y-%m-%d %H:%M:%S')
            
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
    path_id, path_name = cloud115.get_default_save_path()
    return jsonify({'success': True, 'path_id': path_id, 'path_name': path_name})


@app.route('/cloud115/save_path', methods=['POST'])
def cloud115_set_save_path():
    path_id = request.form.get('path_id', '0')
    path_name = request.form.get('path_name', '根目录')
    cloud115.set_default_save_path(path_id, path_name)
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

    print(f'[WeChat Callback] Token: {token[:10]}..., Signature: {msg_signature}, Timestamp: {timestamp}, Nonce: {nonce}, Echostr: {echostr[:20] if echostr else "None"}', flush=True)

    if not token:
        return '未配置企业微信', 500

    crypto = wechat_work.WeChatCrypto(token, encoding_aes_key, corpid) if encoding_aes_key else None

    if request.method == 'GET':
        if not msg_signature or not timestamp or not nonce:
            return 'success'
        if crypto:
            is_valid = crypto.verify_signature(msg_signature, timestamp, nonce, echostr)
            print(f'[WeChat Callback] Signature valid: {is_valid}', flush=True)
            if is_valid:
                try:
                    decrypted, _ = crypto.decrypt_message(echostr)
                    print(f'[WeChat Callback] Decrypted echostr: {decrypted}', flush=True)
                    return decrypted
                except Exception as e:
                    print(f'[WeChat Callback] Decrypt error: {e}', flush=True)
                    return echostr
        return '签名验证失败', 403

    try:
        if crypto:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(request.data)
            encrypt_elem = root.find('Encrypt')
            encrypt_content = encrypt_elem.text if encrypt_elem is not None else ''
            print(f'[WeChat Callback] Encrypt content: {encrypt_content[:30]}...', flush=True)
            if not crypto.verify_signature(msg_signature, timestamp, nonce, encrypt_content):
                print(f'[WeChat Callback] POST signature verification failed', flush=True)
                return '签名验证失败', 403
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
            content = msg.get('Content', '').strip()
            state = user_states.get(from_user)

            if content.lower().startswith('magnet:'):
                cookie = cloud115.get_cookie_string()
                if not cookie:
                    reply = '未配置115 Cookie，请先在网页端配置'
                    wechat_work.send_wechat_message('[115Transfer] 转存失败: 未配置115 Cookie，请及时处理')
                else:
                    success, msg_text = cloud115.add_offline_task(content)
                    reply = f'转存结果: {msg_text}'
                    if not success:
                        wechat_work.send_wechat_message(f'[115Transfer] 转存失败: {msg_text}\n磁力链接: {content[:50]}...')
            elif content.lower() in ['帮助', 'help', '?']:
                if state:
                    if state['action'] == 'batch_transfer':
                        reply = '当前状态: 批量转存\n\n回复页码 - 将该页所有磁力链接转存到115\n回复"取消" - 退出批量转存'
                    elif state['action'] == 'browse_dir':
                        reply = '当前状态: 目录浏览\n\n回复序号 - 进入子目录\n回复"确认" - 设置为转存目录\n回复"新建" - 创建新目录\n回复"返回" - 回到上级目录\n回复"取消" - 退出目录浏览'
                    elif state['action'] == 'create_dir_name':
                        reply = '当前状态: 创建目录\n\n输入新目录名 - 创建目录\n回复"取消" - 取消创建'
                    else:
                        reply = '回复"帮助"查看使用说明'
                else:
                    reply = ('使用方法:\n'
                             '页码 电影名 磁力链接 - 添加电影\n'
                             '页码 - 查看该页电影\n'
                             '搜索 电影名 - 搜索电影\n'
                             '磁力链接 - 转存到115网盘\n\n'
                             '菜单功能:\n'
                             '查看电影 - 浏览电影列表\n'
                             '批量转存 - 批量转存到115\n'
                             '目录 - 管理115网盘目录')
            elif content == '取消':
                if state:
                    del user_states[from_user]
                    reply = '已取消操作'
                else:
                    reply = '没有正在进行的操作'
            elif state and state['action'] == 'batch_transfer':
                if content.isdigit():
                    page_num = int(content)
                    with data_lock:
                        df = load_movies()
                    page_df = df[df['页码'] == page_num]
                    if page_df.empty:
                        reply = f'第 {page_num} 页没有电影'
                    else:
                        magnets = []
                        for _, row in page_df.iterrows():
                            magnet = row.get('磁力链接', '')
                            if not pd.isna(magnet) and str(magnet).strip():
                                magnets.append(str(magnet))
                        if not magnets:
                            reply = f'第 {page_num} 页没有有效的磁力链接'
                        else:
                            results = cloud115.batch_add_offline_tasks(magnets)
                            success_count = sum(1 for r in results if r['success'])
                            fail_count = len(results) - success_count
                            reply = f'批量转存完成\n页码: {page_num}\n成功: {success_count}\n失败: {fail_count}'
                            if fail_count > 0:
                                wechat_work.send_wechat_message(f'[115Transfer] 批量转存部分失败\n页码: {page_num}\n成功: {success_count}\n失败: {fail_count}')
                    del user_states[from_user]
                else:
                    reply = '请输入页码数字'
            elif state and state['action'] == 'browse_dir':
                if content == '确认':
                    cloud115.set_default_save_path(state['cid'], state['path'])
                    reply = f'已设置转存目录: {state["path"]}'
                    del user_states[from_user]
                elif content == '新建':
                    state['action'] = 'create_dir_name'
                    reply = f'在 {state["path"]} 下创建目录\n请输入新目录名:'
                elif content == '返回':
                    if len(state.get('stack', [])) > 1:
                        state['stack'].pop()
                        parent = state['stack'][-1]
                        state['cid'] = parent['cid']
                        state['path'] = parent['path']
                        success, msg_text, dirs = cloud115.get_dir_list(state['cid'])
                        if success and dirs:
                            reply = f'目录: {state["path"]}\n\n'
                            for i, d in enumerate(dirs, 1):
                                reply += f'{i}. {d["name"]}\n'
                            reply += f'\n回复序号进入子目录\n回复"确认"设置为转存目录\n回复"新建"创建新目录\n回复"返回"回到上级目录'
                        else:
                            reply = f'目录: {state["path"]}\n\n此目录为空\n回复"确认"设置为转存目录\n回复"新建"创建新目录'
                    else:
                        reply = '已经在根目录，无法返回'
                elif content.isdigit():
                    idx = int(content) - 1
                    success, msg_text, dirs = cloud115.get_dir_list(state['cid'])
                    if success and 0 <= idx < len(dirs):
                        d = dirs[idx]
                        state['cid'] = d['cid']
                        state['path'] = state['path'] + ' / ' + d['name']
                        state['stack'].append({'cid': d['cid'], 'path': state['path']})
                        success2, msg2, subdirs = cloud115.get_dir_list(d['cid'])
                        if success2 and subdirs:
                            reply = f'目录: {state["path"]}\n\n'
                            for i, sd in enumerate(subdirs, 1):
                                reply += f'{i}. {sd["name"]}\n'
                            reply += f'\n回复序号进入子目录\n回复"确认"设置为转存目录\n回复"新建"创建新目录\n回复"返回"回到上级目录'
                        else:
                            reply = f'目录: {state["path"]}\n\n此目录为空\n回复"确认"设置为转存目录\n回复"新建"创建新目录\n回复"返回"回到上级目录'
                    else:
                        reply = '序号无效，请重新输入'
                else:
                    reply = '请输入序号、"确认"、"新建"或"返回"'
            elif state and state['action'] == 'create_dir_name':
                dir_name = content
                success, msg_text = cloud115.create_dir(state['cid'], dir_name)
                if success:
                    reply = f'目录创建成功: {state["path"]} / {dir_name}'
                else:
                    reply = f'创建失败: {msg_text}'
                del user_states[from_user]
            elif content.startswith('搜索') or content.startswith('search'):
                keyword = content[2:].strip() if content.startswith('搜索') else content[6:].strip()
                if not keyword:
                    reply = '请输入搜索关键词\n格式: 搜索 电影名'
                else:
                    with data_lock:
                        df = load_movies()
                    mask = df['电影名'].str.contains(keyword, case=False, na=False)
                    results = df[mask]
                    if results.empty:
                        reply = f'未找到包含"{keyword}"的电影'
                    else:
                        reply = f'搜索"{keyword}"找到 {len(results)} 部电影:\n\n'
                        for _, row in results.head(20).iterrows():
                            page = row.get('页码', '?')
                            name = row.get('电影名', '未知')
                            magnet = row.get('磁力链接', '无')
                            reply += f'[{page}页] {name}\n{magnet}\n\n'
                        if len(results) > 20:
                            reply += f'... 还有 {len(results) - 20} 部电影，请访问网页查看'
            elif content.isdigit():
                page_num = int(content)
                with data_lock:
                    df = load_movies()
                page_df = df[df['页码'] == page_num]
                if page_df.empty:
                    reply = f'第 {page_num} 页没有电影'
                else:
                    reply = f'第 {page_num} 页 ({len(page_df)} 部电影):\n\n'
                    for _, row in page_df.iterrows():
                        name = row.get('电影名', '未知')
                        magnet = row.get('磁力链接', '无')
                        reply += f'{name}\n{magnet}\n\n'
            else:
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

            reply = wechat_work.truncate_reply(reply)
            print(f'[WeChat Reply] To: {from_user}, From: {to_user}, Content: {reply[:50]}', flush=True)
            if crypto:
                reply_xml = wechat_work.build_reply_xml(from_user, to_user, reply, crypto)
                print(f'[WeChat Reply] XML: {reply_xml[:200]}', flush=True)
                return reply_xml, 200, {'Content-Type': 'application/xml'}
            else:
                reply_xml = wechat_work.build_reply_xml(from_user, to_user, reply)
                return reply_xml, 200, {'Content-Type': 'application/xml'}

        elif msg_type == 'event':
            event = msg.get('Event', '')
            event_key = msg.get('EventKey', '')
            print(f'[WeChat Event] Type: {event}, Key: {event_key}', flush=True)

            if event == 'click':
                user_states.pop(from_user, None)
                if event_key == 'view_movies':
                    with data_lock:
                        df = load_movies()
                    if df.empty:
                        reply = '暂无电影数据'
                    else:
                        total = len(df)
                        page_list = sorted(df['页码'].unique().tolist())
                        reply = f'共 {total} 部电影，请回复页码查看:\n\n'
                        reply += ' | '.join([str(p) for p in page_list])
                        reply += '\n\n直接回复页码即可查看该页所有电影'
                elif event_key == 'batch_transfer':
                    with data_lock:
                        df = load_movies()
                    if df.empty:
                        reply = '暂无电影数据'
                    else:
                        page_list = sorted(df['页码'].unique().tolist())
                        reply = '批量转存 - 请选择页码:\n\n'
                        reply += ' | '.join([str(p) for p in page_list])
                        reply += '\n\n回复页码，该页所有磁力链接将转存到115网盘'
                        user_states[from_user] = {'action': 'batch_transfer'}
                elif event_key == '115_dir':
                    user_states[from_user] = {'action': 'browse_dir', 'cid': '0', 'path': '根目录', 'stack': [{'cid': '0', 'path': '根目录'}]}
                    success, msg_text, dirs = cloud115.get_dir_list('0')
                    if success and dirs:
                        reply = '115网盘目录:\n\n'
                        for i, d in enumerate(dirs, 1):
                            reply += f'{i}. {d["name"]}\n'
                        reply += f'\n回复序号进入子目录\n回复"确认"设置为转存目录\n回复"新建"创建新目录'
                    else:
                        reply = f'获取目录失败: {msg_text}'
                else:
                    reply = '未知操作'
            elif event == 'subscribe':
                reply = '欢迎使用115Transfer！\n发送"帮助"查看使用方法'
            else:
                return 'success'

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


@app.route('/wechat/test', methods=['POST'])
def wechat_test():
    content = request.form.get('content', '').strip()
    if not content:
        return jsonify({'success': False, 'message': '请输入测试消息'})
    
    print(f'[WeChat Test] Received: {content}', flush=True)
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
            return jsonify({'success': True, 'message': f'添加成功\n页码: {result["page"]}\n电影名: {result["name"]}'})
        except Exception as e:
            return jsonify({'success': False, 'message': f'添加失败: {str(e)}'})
    else:
        return jsonify({'success': True, 'message': result})


@app.route('/wechat/menu', methods=['POST'])
def wechat_menu():
    config = wechat_work.load_config()
    agentid = config.get('agentid', '')
    if not agentid:
        return jsonify({'success': False, 'message': '未配置AgentId'})
    success, msg = wechat_work.create_menu(agentid)
    return jsonify({'success': success, 'message': msg})


# ==================== 115网盘整理路由 ====================

from media.scanner import scan_115_directory, get_directory_tree
from media.tmdb import identify_media, get_tmdb_api_key, set_tmdb_api_key
from media.classifier import classify, get_all_categories
from media.organizer import organize_files


@app.route('/media')
def media_page():
    return render_template('media.html', version=VERSION)


@app.route('/media/browse', methods=['GET'])
def media_browse():
    cid = request.args.get('cid', '0')
    success, msg, items = cloud115.list_files(cid, show_dir=1)
    if success:
        return jsonify({'success': True, 'items': items})
    return jsonify({'success': False, 'message': msg})


@app.route('/media/scan', methods=['POST'])
def media_scan():
    cid = request.form.get('cid', '0').strip()
    recursive = request.form.get('recursive', 'true').lower() == 'true'
    files = scan_115_directory(cid, recursive)
    return jsonify({'success': True, 'files': files, 'count': len(files)})


@app.route('/media/identify', methods=['POST'])
def media_identify():
    name = request.form.get('name', '').strip()
    year = request.form.get('year', '').strip()
    if not name:
        return jsonify({'success': False, 'message': '请输入名称'})
    year_int = int(year) if year.isdigit() else None
    result, err = identify_media(name, year_int)
    if err:
        return jsonify({'success': False, 'message': err})
    primary, secondary = classify(result)
    return jsonify({
        'success': True,
        'tmdb': result,
        'primary': primary,
        'secondary': secondary,
    })


@app.route('/media/search', methods=['POST'])
def media_search():
    """手动搜索TMDB，返回多个结果供选择"""
    query = request.form.get('query', '').strip()
    year = request.form.get('year', '').strip()
    if not query:
        return jsonify({'success': False, 'message': '请输入搜索名称'})
    year_int = int(year) if year.isdigit() else None
    from media.tmdb import search_multi
    results, err = search_multi(query, year=year_int)
    if err:
        return jsonify({'success': False, 'message': err})
    # 取前10个结果，补充详情
    output = []
    for r in results[:10]:
        media_type = r.get('media_type', 'movie')
        item = {
            'tmdb_id': r.get('id'),
            'media_type': media_type,
            'title': r.get('title') or r.get('name', ''),
            'original_title': r.get('original_title') or r.get('original_name', ''),
            'year': (r.get('release_date') or r.get('first_air_date') or '')[:4],
            'genres': [],
            'genre_ids': r.get('genre_ids', []),
            'original_language': r.get('original_language', ''),
            'production_countries': [],
            'poster_path': r.get('poster_path', ''),
            'vote_average': r.get('vote_average', 0),
        }
        primary, secondary = classify(item)
        item['primary'] = primary
        item['secondary'] = secondary
        output.append(item)
    return jsonify({'success': True, 'results': output, 'count': len(output)})


@app.route('/media/organize', methods=['POST'])
def media_organize():
    data = request.get_json(force=True, silent=True) or {}
    file_list = data.get('files', [])
    root_cid = data.get('root_cid', '0')
    source_cid = data.get('source_cid', '0')
    if not file_list:
        return jsonify({'success': False, 'message': '没有要整理的文件'})
    results = organize_files(file_list, root_cid, source_cid)
    return jsonify({
        'success': True,
        'message': f'整理完成: 成功 {len(results["success"])} 个, 失败 {len(results["failed"])} 个',
        'results': results,
    })


@app.route('/media/tmdb_key', methods=['GET'])
def media_get_tmdb_key():
    return jsonify({'success': True, 'key': get_tmdb_api_key()})


@app.route('/media/tmdb_key', methods=['POST'])
def media_set_tmdb_key():
    key = request.form.get('key', '').strip()
    set_tmdb_api_key(key)
    return jsonify({'success': True, 'message': 'TMDB API Key 已保存'})


@app.route('/media/categories', methods=['GET'])
def media_categories():
    categories = get_all_categories()
    return jsonify({'success': True, 'categories': categories})


@app.route('/media/tree', methods=['GET'])
def media_tree():
    cid = request.args.get('cid', '0')
    depth = int(request.args.get('depth', '3'))
    tree = get_directory_tree(cid, depth)
    return jsonify({'success': True, 'tree': tree})


# ===== 豆瓣同步 =====

@app.route('/douban')
def douban_page():
    return render_template('douban.html', version=VERSION)


@app.route('/douban/config', methods=['GET', 'POST'])
def douban_config():
    if request.method == 'GET':
        config = douban.load_config()
        return jsonify({'success': True, 'config': config})
    cookie = request.form.get('cookie', '').strip()
    user_id = request.form.get('user_id', '').strip()
    config = douban.load_config()
    if cookie:
        config['cookie'] = cookie
    if user_id:
        config['user_id'] = user_id
    douban.save_config(config)
    return jsonify({'success': True, 'message': '配置已保存'})


@app.route('/douban/check', methods=['POST'])
def douban_check():
    user_id = request.form.get('user_id', '').strip()
    if not user_id:
        config = douban.load_config()
        user_id = config.get('user_id', '')
    if not user_id:
        return jsonify({'success': False, 'message': '请输入豆瓣用户ID'})
    ok, msg = douban.check_cookie(user_id)
    return jsonify({'success': ok, 'message': msg})


@app.route('/douban/fetch', methods=['POST'])
def douban_fetch():
    """获取豆瓣看过的电影列表（按页）"""
    user_id = request.form.get('user_id', '').strip()
    if not user_id:
        config = douban.load_config()
        user_id = config.get('user_id', '')
    if not user_id:
        return jsonify({'success': False, 'message': '请输入豆瓣用户ID'})

    # 支持按页获取，默认第1页，每页15部
    try:
        page = int(request.form.get('page', 1))
        if page < 1:
            page = 1
    except:
        page = 1
    per_page = 15
    start = (page - 1) * per_page

    movies, total, err = douban.fetch_watched_movies(user_id, start, per_page)
    if err:
        return jsonify({'success': False, 'message': err})

    # 获取已有电影名（用于对比）
    try:
        with data_lock:
            df = load_movies()
        existing_names = set()
        if not df.empty:
            for name in df['电影名'].dropna():
                existing_names.add(str(name).strip())
    except:
        existing_names = set()

    # 标记哪些是新的
    for m in movies:
        m['exists'] = m['title'] in existing_names

    import math
    total_pages = math.ceil(total / per_page) if total > 0 else 0

    return jsonify({
        'success': True,
        'movies': movies,
        'total': total,
        'page': page,
        'per_page': per_page,
        'total_pages': total_pages,
        'new_count': sum(1 for m in movies if not m['exists']),
    })


@app.route('/douban/movie_info', methods=['POST'])
def douban_movie_info():
    """获取单个电影的中文名（访问subject页面）"""
    subject_url = request.form.get('url', '').strip()
    if not subject_url:
        return jsonify({'success': False, 'message': '缺少电影URL'})

    name, err = douban.fetch_movie_chinese_name(subject_url)
    if err:
        return jsonify({'success': False, 'message': err})

    return jsonify({'success': True, 'name': name})


@app.route('/douban/sync', methods=['POST'])
def douban_sync():
    """同步选中的电影到数据库"""
    data = request.get_json()
    if not data or 'movies' not in data:
        return jsonify({'success': False, 'message': '没有要同步的电影'})

    movies = data['movies']
    douban_page = data.get('page', 1)
    if not movies:
        return jsonify({'success': False, 'message': '没有要同步的电影'})

    try:
        with data_lock:
            df = load_movies()

            # 豆瓣页码 → 系统页码: 1:1对应
            page = douban_page
            added = 0
            skipped = 0

            for m in movies:
                name = m.get('title', '').strip()
                if not name:
                    continue

                # 检查是否已存在
                if not df.empty and name in df['电影名'].values:
                    skipped += 1
                    continue

                new_id = 1
                if not df.empty:
                    page_df = df[df['页码'] == page]
                    if not page_df.empty:
                        new_id = int(page_df['序号'].max()) + 1

                new_movie = {
                    '序号': new_id,
                    '页码': page,
                    '电影名': name,
                    '磁力链接': '',
                    '保存时间': get_beijing_time().strftime('%Y-%m-%d %H:%M:%S'),
                }
                df = pd.concat([df, pd.DataFrame([new_movie])], ignore_index=True)
                added += 1

            save_movies(df)

        return jsonify({
            'success': True,
            'message': f'同步完成: 新增{added}部，跳过{skipped}部（已存在），页码{page}',
            'added': added,
            'skipped': skipped,
            'page': page,
        })
    except Exception as e:
        return jsonify({'success': False, 'message': f'同步失败: {str(e)}'})


if __name__ == '__main__':
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(host='0.0.0.0', port=3698, debug=debug)
