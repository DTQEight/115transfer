# 🎬 电影磁力链接管理程序

一个基于 Flask 的电影磁力链接管理 Web 应用，可以部署在 NAS 上通过浏览器访问。

## ✨ 功能特点

- 📝 添加电影记录（页码、电影名、磁力链接）
- 📄 按页码分页浏览
- 🔍 搜索电影名
- ✏️ 编辑和删除电影
- 📋 点击复制磁力链接
- 🔴 空磁力链接标红显示
- 💾 数据自动保存到 Excel

## 🐳 Docker 部署

### 构建镜像
```bash
docker build -t movie-manager .
```

### 运行容器
```bash
docker run -d \
  -p 5000:5000 \
  -v /path/to/your/data:/app/data \
  --name movie-manager \
  movie-manager
```

### 访问
打开浏览器访问：`http://your-nas-ip:5000`

## 📁 项目结构

```
├── app.py                 # Flask 应用主程序
├── Dockerfile             # Docker 镜像构建文件
├── requirements.txt       # Python 依赖
├── README.md              # 项目说明
├── .gitignore             # Git 忽略文件
└── templates/             # HTML 模板
    ├── index.html         # 主页面
    └── search.html        # 搜索结果页面
```

## 🔧 技术栈

- Python 3.9
- Flask 2.3.3
- pandas 2.1.4
- Bootstrap 5

## 📝 使用说明

1. 在 NAS 上安装 Docker
2. 克隆此仓库
3. 构建并运行 Docker 容器
4. 通过浏览器访问应用

## 📄 数据文件

数据保存在 `/app/data/movies_data.xlsx`（容器内路径），挂载外部目录后可以持久化保存。
