# 🎬 115Transfer - 电影磁力链接管理与115转存工具

一个基于 Flask 的电影磁力链接管理 Web 应用，支持115网盘离线转存、豆瓣观影同步、企业微信集成，可部署在 NAS 上通过浏览器访问。

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

### 豆瓣观影同步
- 🎯 一键拉取豆瓣"看过的电影"列表
- ⏰ 定时自动全量同步（cron 表达式，应用内调度）
- 🐢 慢速拉取防限流（每页间隔 2 秒）
- 🔀 严格保持豆瓣顺序（页码 = 豆瓣页码，序号 = 页内位置）
- 📊 同步统计仪表盘（总数、页码数、磁链完整度、转存统计、最近记录）

### 百度论坛磁链搜索
- 🔎 论坛帖子搜索与磁力链接提取
- 🧵 帖子详情解析

### 论坛全站监控
- 📥 **全量拉取**：遍历所有板块所有页面，下载所有种子文件（首次使用）
- 🔁 **增量监控**：只爬各板块最新帖（按最新回帖排序，整页已爬过则停止）
- 🔍 **二次拉取**：重新访问无种帖子，对延迟上传种子的帖子再次抓取
- ⏰ **定时任务**：cron 表达式自动增量监控（默认每天凌晨4点）
- 🗄️ **本地数据库**：SQLite 存储帖子元数据 + 种子路径 + 磁力链接，搜索优先本地命中
- 📊 **统计仪表盘**：总帖/有种/无种/覆盖率/最近7天趋势/最近运行记录
- 🧲 **磁力链接缓存**：爬取时同步计算并存储磁力链接，搜索结果可直接使用
- 🔄 **登录态自动恢复**：检测到 session 失效时自动重新登录并继续爬取，推送微信通知
- 🐢 **限流控制**：页间延迟/帖间延迟/最大页数/并发线程数可配置

### 媒体识别整理
- 🎬 TMDB 元数据识别
- 📁 网盘文件自动分类整理

### 企业微信端
- 💬 通过企业微信发送消息添加电影
- 📊 点击菜单查看电影列表（按页码浏览）
- 🔄 选择页码批量转存到115网盘
- 📁 浏览和设置115网盘转存目录
- 📂 在115网盘创建新目录
- 🔗 直接发送磁力链接自动转存到115网盘
- 🔁 发送"增量"启动增量监控，"全量"启动全量拉取
- 📊 发送"进度"查看监控实时状态
- ✋ 发送"取消增量"/"取消全量"停止对应任务

### 安全特性
- 🔐 登录密码保护 + 登录限速（5分钟5次）
- 🛡️ CSRF 令牌防护
- 🔒 敏感配置（Cookie/Token/API Key）AES-GCM 加密存储
- 🌐 CORS 白名单
- 📝 JSON 格式日志 + 实时 SSE 推送

## 🐳 Docker 部署

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
      - ENCRYPTION_KEY=                # 可选，留空则回退到 FLASK_SECRET_KEY
      - ALLOWED_ORIGINS=http://your-nas-ip:3698
      - WECHAT_PROXY_TOKEN=            # 可选，保护 /wechat/proxy 接口
      - HTTPS_ENABLED=false            # 启用 HTTPS 时设为 true
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
├── crypto_utils.py         # AES-GCM 加密工具（统一入口）
├── cloud115.py             # 115网盘 API 模块
├── wechat_work.py          # 企业微信 API 模块
├── douban.py               # 豆瓣观影同步模块
├── baidu_forum.py          # 百度论坛搜索模块
├── forum_monitor.py        # 论坛全站监控模块（全量/增量/二次拉取/本地搜索）
├── transfer_history.py     # 转存历史记录模块
├── media/                  # 媒体识别整理
│   ├── scanner.py          #   网盘文件扫描
│   ├── tmdb.py             #   TMDB 元数据
│   ├── classifier.py       #   分类器
│   └── organizer.py        #   整理器
├── templates/              # HTML 模板
│   ├── base.html           #   基础模板
│   ├── login.html          #   登录页
│   ├── index.html          #   主页（含统计仪表盘）
│   ├── search.html         #   搜索结果
│   ├── baidu.html          #   论坛配置与搜索
│   ├── douban.html         #   豆瓣同步与自动同步配置
│   ├── media.html          #   媒体识别整理
│   └── logs.html           #   实时日志
├── Dockerfile              # Docker 镜像构建文件
├── docker-compose.yml      # Docker Compose 配置
├── requirements.txt        # Python 依赖
├── .env.example            # 环境变量示例
├── VERSION                 # 版本号
└── README.md               # 项目说明
```

## 🔧 技术栈

- Python 3.9
- Flask 2.3.3
- pandas 2.1.4
- SQLite 3（论坛监控本地数据库，标准库自带）
- APScheduler 3.10.4（定时调度）
- flask-cors 4.0.0（CORS 白名单）
- python-json-logger 2.0.7（JSON 日志）
- pycryptodome 3.20.0（企业微信消息加解密 + AES-GCM 配置加密）
- Bootstrap 5

## 🔐 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `APP_PASSWORD` | 是 | 登录密码，请修改为强密码 |
| `FLASK_SECRET_KEY` | 是 | Flask 会话密钥（32字节强随机值） |
| `ALLOWED_ORIGINS` | 是 | CORS 白名单（逗号分隔的源） |
| `ENCRYPTION_KEY` | 否 | 敏感配置加密密钥，留空则回退到 `FLASK_SECRET_KEY` |
| `WECHAT_PROXY_TOKEN` | 否 | 保护 `/wechat/proxy` 写入接口 |
| `HTTPS_ENABLED` | 否 | 启用 HTTPS 时设为 `true`（强制 Secure cookie） |
| `FLASK_DEBUG` | 否 | 调试模式，默认 `false` |
| `TZ` | 否 | 时区，默认 `Asia/Shanghai` |
| `DATA_DIR` | 否 | 数据目录，默认 `/app/data` |

## 💬 企业微信配置

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
菜单结构（3 个一级菜单 + 二级菜单）：

- **电影**
  - **查看电影** - 显示页码列表，回复页码查看该页电影
  - **批量转存** - 选择页码，一键将该页所有磁力链接转存到115
- **论坛**
  - **论坛进度** - 查看论坛监控当前状态
  - **增量拉取** - 一键启动增量监控（爬新帖）
- **网盘**
  - **目录** - 浏览115网盘目录，设置转存目录，创建新目录

### 5. 支持的命令

| 命令 | 别名 | 功能 |
|------|------|------|
| `页码 电影名 [磁力链接]` | - | 添加电影（磁力链接可留空） |
| `页码` | - | 查看该页电影 |
| `搜索 电影名` | `search` | 搜索本地电影记录 |
| `磁力链接` | - | 转存到115网盘 |
| `帮助` | `help` / `?` | 查看使用说明 |
| `取消` | - | 退出当前操作 |
| `增量` | `增量拉取` / `增量监控` / `开始增量` / `incremental` | 启动增量监控 |
| `全量` | `全量拉取` / `开始全量` / `full` | 启动全量拉取 |
| `进度` | `论坛进度` / `监控进度` / `forum` / `monitor` | 查看监控进度 |
| `取消增量` | `停止增量` | 取消增量监控任务 |
| `取消全量` | `停止全量` | 取消主任务（全量/二次拉取） |

### 6. 消息格式
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
- 转存历史：`/app/data/transfer_history.json`（90天自动清理，上限5000条）
- 115配置：`/app/data/cloud115_config.json`
- 企业微信配置：`/app/data/wechat_work_config.json`
- 豆瓣配置：`/app/data/douban_config.json`
- 论坛配置：`/app/data/baidu_forum_config.json`
- 论坛监控数据库：`/app/data/forum_monitor.db`（SQLite，存储帖子元数据/种子路径/磁力链接/板块进度/运行日志，含 magnet_links 字段，首次启动自动迁移）
- 论坛种子文件：`/app/data/forum_seeds/`（按板块分目录存放 .torrent 文件）
- 应用日志：`/app/data/app.log`（轮转：单文件10MB，保留5个备份）
- 数据备份：`/app/data/backups/`
