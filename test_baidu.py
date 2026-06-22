"""下载附件，解析种子文件转磁力链接"""
import requests
import re
import base64
import hashlib
import urllib.parse

BASE = 'https://10001.baidubaidu.win/'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

s = requests.Session()
s.headers.update({'User-Agent': UA})

# 登录
r = s.get(BASE + 'member.php?mod=logging&action=login', timeout=15)
r.encoding = 'gbk'
formhash = re.search(r'name="formhash"\s+value="([^"]+)"', r.text).group(1)
loginhash = re.search(r'loginhash=([A-Za-z0-9]+)', r.text).group(1)
s.post(BASE + f'member.php?mod=logging&action=login&loginsubmit=yes&loginhash={loginhash}', data={
    'formhash': formhash, 'referer': BASE,
    'username': 'mcdull', 'password': 'Huangyuzhi8',
    'answer': '', 'cookietime': '2592000',
}, timeout=15)
print('登录完成')

# 下载附件
aid_raw = 'MTM5NDY2MnxlMjcwZDk2NXwxNzgyMTEzNjA2fDU2NDM1fDM1Njg3MA=='
attach_url = BASE + f'forum.php?mod=attachment&aid={urllib.parse.quote(aid_raw)}&nothumb=yes'
print('\n=== 下载附件 ===', attach_url)
r5 = s.get(attach_url, timeout=30, allow_redirects=True)
print('状态:', r5.status_code)
print('Content-Type:', r5.headers.get('Content-Type'))
print('Content-Disposition:', r5.headers.get('Content-Disposition'))
print('Content-Length:', r5.headers.get('Content-Length'))
print('最终URL:', r5.url)
print('前100字节:', r5.content[:100])

# 保存附件
with open('test_attachment.torrent', 'wb') as f:
    f.write(r5.content)
print('已保存附件，大小:', len(r5.content))

# 解析种子文件
def parse_torrent_info_hash(content):
    """从torrent文件内容解析info_hash"""
    try:
        # 尝试用bencode
        import bencodepy
        data = bencodepy.decode(content)
        info = data[b'info']
        info_bencoded = bencodepy.encode(info)
        info_hash = hashlib.sha1(info_bencoded).hexdigest()
        return info_hash
    except ImportError:
        # 手动解析
        return parse_torrent_manual(content)

def parse_torrent_manual(content):
    """手动解析torrent找info字典的hash"""
    # 简单方法：找 4:infod 开始，到 e 结束的位置
    idx = content.find(b'4:infod')
    if idx < 0:
        return None
    start = idx + 6  # d 的位置
    # 从后往前找匹配的 e
    # 简单方法：从start开始，找最后一个e（不完美但通常可行）
    # 更好的方法：用栈
    depth = 1
    i = start + 1
    while i < len(content) and depth > 0:
        if content[i:i+1] == b'd':
            depth += 1
        elif content[i:i+1] == b'e':
            depth -= 1
        i += 1
    info_dict = content[start:i]
    return hashlib.sha1(info_dict).hexdigest()

try:
    import bencodepy
    print('使用bencodepy')
except ImportError:
    print('bencodepy未安装，使用手动解析')

info_hash = parse_torrent_info_hash(r5.content)
print('\n=== Info Hash ===', info_hash)
if info_hash:
    magnet = f'magnet:?xt=urn:btih:{info_hash}'
    print('磁力链接:', magnet)

# 尝试提取种子里的name
try:
    import bencodepy
    data = bencodepy.decode(r5.content)
    info = data[b'info']
    name = info.get(b'name', b'').decode('utf-8', errors='ignore')
    print('种子name:', name)
    print('种子announce:', [a.decode('utf-8', errors='ignore') for a in data.get(b'announce-list', [[data.get(b'announce', b'')]])][:3])
except Exception as e:
    print('解析name失败:', e)
