import requests
import json

USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

cookie = 'UID=101304078_R2_1780339447; CID=ef06439a854a3f0e58fc2d6c2d441ddb; SEID=658d84f58241b1854b311fe43df5430ebb47532a9073d951eadf26333e8466322c030a548d3fd99c3d942872eae155a9da8b0c2327d3e261c2bc5112; KID=6db6914813bc6038a1d5d21bf0f1eaea'

print("测试115 Cookie验证（新API）...")
print(f"Cookie: {cookie[:50]}...")
print()

url = 'https://my.115.com/?ct=ajax&ac=nav'
headers = {
    'User-Agent': USER_AGENT,
    'Cookie': cookie,
}

try:
    resp = requests.get(url, headers=headers, timeout=10)
    print(f"状态码: {resp.status_code}")
    
    if resp.status_code == 200:
        try:
            data = resp.json()
            print(f"JSON解析成功: {json.dumps(data, ensure_ascii=False, indent=2)[:500]}")
            if data.get('state') is True and data.get('data'):
                user_name = data['data'].get('user_name', '未知')
                print(f"")
                print(f"✓ 验证成功 (用户: {user_name})")
            else:
                print(f"✗ Cookie已失效")
        except json.JSONDecodeError:
            print(f"✗ 响应格式错误: {resp.text[:200]}")
    else:
        print(f"✗ 请求失败，状态码: {resp.status_code}")
        
except requests.exceptions.Timeout:
    print("✗ 请求超时")
except requests.exceptions.ConnectionError:
    print("✗ 连接失败")
except Exception as e:
    print(f"✗ 错误: {e}")

print()
print("测试完成！")
