# Pixiv Following Downloader

<img src="static/logo.svg" width="88" alt="Pixiv Following Downloader logo">

自动下载 Pixiv 画师的新作品，支持本地磁盘 / WebDAV，自带中文 WebUI。

## Docker Compose

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

打开 `http://<服务器地址>:8971`。

> 没有 token 也可以先 `touch .env` 启动，在网页向导里填写。
> 更新：`docker compose pull && docker compose up -d`。

## 直接运行

```bash
git clone https://github.com/luvhx/pixiv-following-downloader.git
cd pixiv-following-downloader
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # 编辑 token
python main.py
```

默认监听 `0.0.0.0:8971`。

## 首次使用

1. 打开 WebUI，按向导填写 Pixiv token（可选下载专用账号）和存储位置
2. 进入「画师」页同步关注列表，把需要下载的画师加入「抓取计划」
3. 在「下载规则」设置画质、tag 过滤和运行调度

## 配置

| 变量 | 说明 |
| --- | --- |
| `PIXIV_REFRESH_TOKEN` | 主账号 refresh_token（必填） |
| `PIXIV_DOWNLOAD_REFRESH_TOKEN` | 可选，下载专用账号 |
| `STORAGE_BACKEND` | `local` 或 `webdav` |
| `WEBDAV_URL` / `WEBDAV_USERNAME` / `WEBDAV_PASSWORD` / `WEBDAV_ROOT` | WebDAV 连接信息 |
| `WEBUI_HOST` / `WEBUI_PORT` | 监听地址与端口，默认 `8971` |
| `HTTPS_PROXY` | 访问 Pixiv 的代理 |

完整列表见 [.env.example](.env.example)，其余选项可在 WebUI 中修改。

## 许可

[MIT License](LICENSE)
