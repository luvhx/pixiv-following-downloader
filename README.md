# Pixiv Following Downloader

<img src="static/logo.svg" width="88" alt="Pixiv Following Downloader logo">

自动下载 Pixiv 画师的新作品，支持本地磁盘 / WebDAV，自带中文 WebUI。

## 功能

- 按 cron 或固定间隔增量同步，只下载每位画师的新作品
- 从账号关注列表选择画师加入「抓取计划」，也可添加未关注画师
- 主账号读取关注列表，可选下载专用账号负责作品抓取和图片下载
- 支持插画 / 漫画 / 动图，可配置画质、Tag 过滤、单画师上限和命名模板
- 本地画廊支持搜索、筛选、排序、收藏、批量删除和多页查看
- 下载文件可保存在本地磁盘或 WebDAV

## 部署

### Docker Compose

```bash
mkdir -p pixiv-downloader/data
cd pixiv-downloader
```

创建 `.env`：

```env
PIXIV_REFRESH_TOKEN=你的 refresh_token
# 可选：下载专用账号，留空则复用主账号
PIXIV_DOWNLOAD_REFRESH_TOKEN=
WEBUI_USERNAME=admin
WEBUI_PASSWORD=change-me
```

创建 `docker-compose.yml`：

```yaml
services:
  pixiv-downloader:
    image: ghcr.io/luvhx/pixiv-following-downloader:latest
    container_name: pixiv-following-downloader
    restart: unless-stopped
    env_file: .env
    environment:
      - TZ=Asia/Shanghai
    ports:
      - "${WEBUI_PORT:-8971}:${WEBUI_PORT:-8971}"
    volumes:
      - ./data:/app/data
      - ./.env:/app/.env
```

启动：

```bash
docker compose up -d
```

打开 `http://<服务器地址>:8971`，按向导填写 token 和存储位置，然后到「画师」页把需要下载的画师加入抓取计划。

### 直接运行

```bash
git clone https://github.com/luvhx/pixiv-following-downloader.git
cd pixiv-following-downloader
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # 编辑 token
python main.py
```

默认监听 `0.0.0.0:8971`，其余配置见 [.env.example](.env.example)。
