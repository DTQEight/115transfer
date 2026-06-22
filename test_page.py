"""测试搜索分页"""
import requests
import re
import urllib.parse

BASE = 'https://10001.baidubaidu.win/'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

s = requests.Session()
s.headers.update({'User-Agent': UA})
s.trust_env = False

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

# 搜索第1页
kw_encoded = urllib.parse.quote('阿凡达', encoding='gbk')
url = BASE + f'search.php?mod=forum&searchsubmit=yes&srchtxt={kw_encoded}'
r = s.get(url, timeout=15, allow_redirects=True)
r.encoding = 'gbk'
print('搜索URL:', r.url)

# 提取searchid
searchid_m = re.search(r'searchid=(\d+)', r.url)
searchid = searchid_m.group(1) if searchid_m else ''
print('searchid:', searchid)

# 统计结果数
items1 = re.findall(r'<li class="pbw" id="(\d+)">', r.text)
print('第1页结果数:', len(items1))

# 提取总结果数
cnt_m = re.search(r'相关内容\s*(\d+)\s*个', r.text)
total = cnt_m.group(1) if cnt_m else '?'
print('总结果数:', total)

# 提取分页信息
page_links = re.findall(r'page=(\d+)', r.text)
print('分页链接:', sorted(set(page_links), key=int))

# 测试第2页
if searchid:
    url2 = BASE + f'search.php?mod=forum&searchid={searchid}&orderby=lastpost&ascdesc=desc&searchsubmit=yes&kw={kw_encoded}&page=2'
    r2 = s.get(url2, timeout=15, allow_redirects=True)
    r2.encoding = 'gbk'
    items2 = re.findall(r'<li class="pbw" id="(\d+)">', r2.text)
    print('\n第2页结果数:', len(items2))
    if items2:
        print('第2页第一个tid:', items2[0])
