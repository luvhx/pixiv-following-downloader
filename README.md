# Pixiv Following Downloader

<img src="static/logo.svg" width="88" alt="Pixiv Following Downloader logo">

自动下载你关注的 Pixiv 画师的新作品，按画师归档；自带中文 WebUI，可以在线浏览关注画师、管理下载规则、查看已下载画廊和日志。

## 功能

- **自动同步**：按 cron 或固定间隔增量抓取，只处理每位画师的新作品
- **作品浏览**：关注画师列表，可开启「代表作预览」每人展示 3 个作品；进入画师页浏览全部作品、单张或批量下载
- **Tag 操作**：作品浏览页和已下载详情的 tag 支持 `☆` 收藏与 `⊘` 屏蔽
- **已下载画廊**：搜索、按画师/tag 筛选、按时间/收藏数/页数/标题排序、收藏、批量删除，多页作品可翻页查看
- **下载规则**：画质、作品类型、仅首页、最小收藏数、单画师每轮最大下载数、动图格式（webm/gif/zip）
- **命名模板**：目录/文件名支持 `{artist}`、`{date}`、`{id}`、`{title}` 等占位符
- **画师暂停**：关注列表里可暂停某位画师的自动同步，不影响手动浏览
- **Tag 规则**：匹配（白名单）与排除（黑名单）两种模式，可视化增删，可连带删除已下载作品
- **存储**：本地磁盘或 WebDAV（Nextcloud、坚果云、Alist 等）
- **WebUI**：登录保护、移动端适配、实时进度、内存日志，配置和规则都可在网页修改
- **维护工具**：扫描数据库，清理文件已丢失的记录

## 快速开始

### Docker Compose（推荐）

```bash
mkdir -p pixiv-downloader/data
cd pixiv-downloader
```

创建 `.env`（完整示例见 [.env.example](.env.example)）：

```env
PIXIV_REFRESH_TOKEN=你的 refresh_token
TARGET_USER_ID=你的 Pixiv 用户 ID
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

启动后打开 `http://<服务器地址>:8971`：

```bash
docker compose up -d
docker compose logs -f
```

更新镜像：`docker compose pull && docker compose up -d`。

> 没有 token 也可以先 `touch .env` 启动，进入网页版配置向导填写。
> 镜像地址：`ghcr.io/luvhx/pixiv-following-downloader:latest`。

### 直接运行

```bash
git clone https://github.com/luvhx/pixiv-following-downloader.git
cd pixiv-following-downloader
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # 编辑里面的 token 和用户 ID
python main.py
```

默认监听 `0.0.0.0:8971`。

## 使用

| 页面 | 作用 |
| --- | --- |
| 仪表盘 | 认证/运行状态、实时进度、下次运行、上次统计、最近下载、日志 |
| 作品浏览 | 关注画师列表；可预览代表作；进入画师页浏览全部作品 |
| 已下载 | 本地画廊：筛选、排序、收藏、批量删除；详情页可翻页查看多图 |
| 下载规则 | 下载范围、调度、tag 规则、重置抓取水位 |
| 配置 | token、存储、WebDAV、请求节流、WebUI |
| 日志 | 内存日志，可自动刷新 |

### 作品浏览

- 点右上角「预览代表作」后，每位画师展示 3 个作品（按收藏数排序），已下载的带角标
- 预览数据来自关注列表接口，升级后需要点一次「刷新关注列表」或等自动运行
- 进入画师页可以浏览其全部插画/漫画，勾选多个作品批量加入下载队列
- 画师页可「暂停同步」，暂停后自动任务会跳过该画师（仍可手动下载）
- 作品 tag 旁的 `☆` 收藏该 tag，`⊘` 加入排除规则
- 已下载作品的详情页支持多页切换（页码或键盘 ← →），并显示收藏数；动图可直接播放

### 下载规则

- **下载范围**：画质（original/large/medium）、类型（插画/漫画/全部）、多页是否只留首页、单画师每轮上限、最小收藏数
- **调度**：cron 优先（如 `0 */6 * * *`），固定间隔兜底，都留空则只手动触发；保存后立即重新计算下次运行
- **Tag 匹配**：白名单，列表非空时必须命中其中一个 tag 才下载（精确匹配）
- **Tag 排除**：黑名单，命中 tag 或标题关键词就跳过；可勾选连带删除已下载作品
- **动图**：Pixiv ugoira 可保存为 webm（推荐）、gif 或原始 zip；镜像内已带 ffmpeg
- **命名模板**：目录/文件名支持 `{artist}` `{artist_id}` `{date}` `{id}` `{title}` `{page}` `{pages}` `{type}` `{ext}`；留空使用默认命名
- **抓取水位**：只抓新作品；有失败或达到上限时本轮不推进水位，下轮自动重试。想重扫旧作品可「重置抓取水位」
- **维护**：下载规则页底部可扫描并清理文件已丢失的记录

### 配置

常用项：

| 配置 | 说明 |
| --- | --- |
| `PIXIV_REFRESH_TOKEN` | Pixiv refresh_token（必填） |
| `TARGET_USER_ID` | 抓取其关注列表的用户 ID（必填） |
| `STORAGE_BACKEND` | `local` 或 `webdav` |
| `DOWNLOAD_PATH` / `DB_PATH` | 本地下载目录 / 数据库路径 |
| `WEBDAV_URL` / `WEBDAV_USERNAME` / `WEBDAV_PASSWORD` / `WEBDAV_ROOT` | WebDAV 连接信息 |
| `HTTPS_PROXY` | 访问 Pixiv 的代理，如 `http://127.0.0.1:7890` |
| `WEBUI_HOST` / `WEBUI_PORT` | 监听地址与端口，默认 `8971` |
| `WEBUI_USERNAME` / `WEBUI_PASSWORD` | 登录账号；用户名留空则无需登录 |

其余请求节流参数（请求间隔、重试、画师冷却等）在「配置」页修改，完整列表见 [.env.example](.env.example)。

## 说明

- 数据在 `data/`（数据库、图片），备份 `.env` + `data/` 即可；旧版本数据库启动时自动迁移。
- 缩略图由服务端带着 Pixiv 的 `Referer` 转发中图（浏览器直连会被 403），只透传不生成；服务器需要能访问 `i.pximg.net`，必要时配置 `HTTPS_PROXY`。
- 动图转 webm/gif 需要 ffmpeg；Docker 镜像已内置，手动运行需自行安装（没有 ffmpeg 时会回退保存 zip）。
- 公网部署请务必设置 WebUI 用户名密码，并放在 HTTPS 反向代理之后；登录失败 5 次会临时封禁。
- 请遵守 Pixiv 服务条款与画师的转载授权要求。

## 常见问题

**启动后提示未认证**  
检查 `PIXIV_REFRESH_TOKEN`，可在仪表盘点「重新认证」；`invalid_grant` 表示 token 已失效。

**缩略图不显示**  
确认服务器能访问 `i.pximg.net`；需要代理时设置 `HTTPS_PROXY` 后重启。

**作品浏览页没有画师 / 没有代表作**  
点「刷新关注列表」同步一次；之后每轮自动运行也会更新。

**改了规则但旧作品没补下载**  
增量抓取只看新作品，到「下载规则」页重置抓取水位后重扫。

**WebDAV 上传失败**  
确认地址和账号密码；自签名证书可关闭 `WEBDAV_VERIFY_SSL`。

**忘记 WebUI 密码**  
修改 `.env` 的 `WEBUI_USERNAME` / `WEBUI_PASSWORD` 后重启。

## 许可

[MIT License](LICENSE)
