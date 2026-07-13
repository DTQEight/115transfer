# 🎬 115Transfer - 电影磁力链接管理与115转存工具

一个基于 Flask 的电影磁力链接管理 Web 应用，支持115网盘离线转存和企业微信集成，可以部署在 NAS 上通过浏览器访问。

## ✨ 功能特点

### Web 端
- 📝 添加电影记录（页码、电影名、磁力链接）
- 📄 按页码分页浏览
- 🔍 搜索电影名
- ✏️ 编辑和删除电影
- 📋 点击复制磁力链接
- 🔴 空磁力链接标红显示
- 💾 数据自动保存到 Excel
- ☁️ 支持115网盘离线转存（单个/批量）
- 📂 115网盘目录管理（浏览、设置默认转存目录）

### 企业微信端
- � 通过企业微信发送消息添加电影
- 📊 点击菜单查看电影列表（按页码浏览）
- 🔄 选择页码批量转存到115网盘
- 📁 浏览和设置115网盘转存目录
- 📂 在115网盘创建新目录
- 🔗 直接发送磁力链接自动转存到115网盘

## �🐳 Docker 部署

### 使用 docker-compose（推荐）
```yaml
services:
  115transfer:
    image: ghcr.io/dtqeight/115transfer:master
    container_name: 115transfer
    ports:
      - "3698:3698"
    volumes:
      - ./data:/app/data
    restart: unless-stopped
    environment:
      - FLASK_DEBUG=false
      - TZ=Asia/Shanghai
      - DATA_DIR=/app/data
      - PYTHONUNBUFFERED=1
      # 安全相关配置，请务必修改为强随机值
      - APP_PASSWORD=your_strong_password_here
      - FLASK_SECRET_KEY=your_random_32_byte_hex_here
      - ALLOWED_ORIGINS=http://your-nas-ip:3698
```

> **安全提示**：`APP_PASSWORD` 和 `FLASK_SECRET_KEY` 必须修改为强随机值。完整环境变量说明见 `.env.example`。

```bash
docker-compose up -d
```

### 访问
打开浏览器访问：`http://your-nas-ip:3698`

## 📁 项目结构

```
├── app.py                  # Flask 应用主程序
├── cloud115.py             # 115网盘API模块
├── wechat_work.py          # 企业微信API模块
├── Dockerfile              # Docker 镜像构建文件
├── docker-compose.yml      # Docker Compose 配置
├── requirements.txt        # Python 依赖
├── VERSION                 # 版本号
├── README.md               # 项目说明
└── templates/              # HTML 模板
    ├── base.html           # 基础模板（含企业微信设置）
    ├── index.html          # 主页面
    └── search.html         # 搜索结果页面
```

## 🔧 技术栈

- Python 3.9
- Flask 2.3.3
- pandas 2.1.4
- Bootstrap 5
- pycryptodome（企业微信消息加解密）

## � 企业微信配置

### 1. 创建企业微信应用
1. 登录企业微信管理后台
2. 创建自建应用
3. 记录企业ID、应用Secret、AgentId

### 2. 配置接收消息
1. 在应用设置中开启"接收消息"
2. 设置API接收：填写Token和EncodingAESKey
3. URL填写：`http://your-domain:3698/wechat/callback`

### 3. 在115Transfer中配置
1. 打开Web界面
2. 点击右上角"企业微信设置"
3. 填写企业ID、应用Secret、AgentId、Token、EncodingAESKey
4. 点击保存
5. 点击"创建菜单"创建企业微信菜单

### 4. 企业微信菜单功能
- **查看电影** - 显示页码列表，回复页码查看该页电影
- **批量转存** - 选择页码，一键将该页所有磁力链接转存到115
- **目录** - 浏览115网盘目录，设置转存目录，创建新目录

### 5. 消息格式
支持两种格式添加电影：

**单行格式：**
```
页码 电影名 磁力链接
```
示例：`1 流浪地球 magnet:?xt=urn:btih:xxx`

**多行格式：**
```
页码
电影名
磁力链接
```

## 📄 数据文件

- 电影数据：`/app/data/movies_data.xlsx`
- 115配置：`/app/data/cloud115_config.json`
- 企业微信配置：`/app/data/wechat_work_config.json`
