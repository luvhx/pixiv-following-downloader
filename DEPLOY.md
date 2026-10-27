# Docker 镜像构建、发布与拉取

本文覆盖三种用法：

1. 本地构建并运行；
2. 推送到 GHCR / Docker Hub，之后直接 `docker pull`；
3. 离线 `docker save` / `docker load`。

镜像内容：Python 3.11、运行依赖、应用代码、`templates/`、`static/`、健康检查。
不包含 `.env`、`data/`、`tests/`、`.git/`。

## 1. 本地构建

```bash
docker build -t pixiv-following-downloader:latest .
```

指定版本标签：

```bash
docker build --build-arg VERSION=1.0.0 \
  -t pixiv-following-downloader:1.0.0 \
  -t pixiv-following-downloader:latest .
```

启动：

```bash
docker run -d \
  --name pixiv-following-downloader \
  --restart unless-stopped \
  -p 8971:8971 \
  -v "$(pwd)/data:/app/data" \
  -v "$(pwd)/.env:/app/.env" \
  pixiv-following-downloader:latest
```

Windows PowerShell 把 `$(pwd)` 换成 `${PWD}`。

也可以继续用 Compose：

```bash
docker compose up -d --build
```

`docker-compose.yaml` 同时带有 `image` 和 `build`，本地构建会打上：

```text
pixiv-following-downloader:latest
```

## 2. 推送到 GHCR（推荐）

### 2.1 自动构建

仓库已包含 [.github/workflows/docker-publish.yml](.github/workflows/docker-publish.yml)：

- push 到 `main` / `master`：发布 `latest` 和分支标签；
- push `v*` 标签：发布 `v1.2.3`、`1.2`、`1.2.3` 等标签；
- Pull Request：只构建，不推送；
- 自动构建 `linux/amd64` 和 `linux/arm64`；
- 构建前自动运行单元测试。

使用步骤：

```bash
git remote add origin https://github.com/<owner>/<repo>.git
git push -u origin main
```

之后在 GitHub Actions 中查看构建结果。镜像地址：

```text
ghcr.io/<owner>/<repo>:latest
```

### 2.2 把包设为公开

GHCR 包默认是私有的。首次推送后：

1. 打开 GitHub 个人/组织主页；
2. `Packages` → 选择该镜像；
3. `Package settings` → `Danger Zone`；
4. `Change visibility` → `Public`。

否则匿名 `docker pull` 会返回 `unauthorized`。

### 2.3 直接拉取

```bash
docker pull ghcr.io/<owner>/<repo>:latest
```

Compose 拉取：

```bash
IMAGE_REPO=ghcr.io/<owner>/<repo> docker compose pull
IMAGE_REPO=ghcr.io/<owner>/<repo> docker compose up -d --no-build
```

PowerShell：

```powershell
$env:IMAGE_REPO = "ghcr.io/<owner>/<repo>"
docker compose pull
docker compose up -d --no-build
```

### 2.4 手动推送

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u <github-user> --password-stdin
docker tag pixiv-following-downloader:latest ghcr.io/<owner>/<repo>:latest
docker push ghcr.io/<owner>/<repo>:latest
```

Token 需要 `write:packages` 权限；只拉取公开包不需要登录。

## 3. 推送到 Docker Hub

```bash
docker login
docker tag pixiv-following-downloader:latest <dockerhub-user>/pixiv-following-downloader:latest
docker push <dockerhub-user>/pixiv-following-downloader:latest
```

拉取：

```bash
docker pull <dockerhub-user>/pixiv-following-downloader:latest
```

Docker Hub 仓库需要先在网页端创建；私有仓库拉取前需要 `docker login`。

## 4. 多架构镜像

CI 已默认构建 `amd64 + arm64`。手动构建：

```bash
docker buildx create --use --name pixiv-builder
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t ghcr.io/<owner>/<repo>:latest \
  --push .
```

检查：

```bash
docker buildx imagetools inspect ghcr.io/<owner>/<repo>:latest
```

## 5. 离线导入

没有镜像仓库时，可以把镜像导出为 tar：

```bash
docker save ghcr.io/<owner>/<repo>:latest -o pixiv-following-downloader.tar
docker load -i pixiv-following-downloader.tar
```

`docker load` 不是 `docker pull`；镜像名和标签会保留。

## 6. 运行参数

| 参数 | 说明 |
| --- | --- |
| `/app/data` | 数据库与下载文件，必须持久化 |
| `/app/.env` | 配置与凭据；也可以完全用环境变量 |
| `WEBUI_PORT` | 默认 `8971`；改端口时 compose 的 `ports` 会通过同名变量自动跟随 |
| `WEBUI_HOST` | 默认 `0.0.0.0` |
| `TZ` | 建议 `Asia/Shanghai` |

Compose 默认使用端口映射（`${WEBUI_PORT:-8971}:${WEBUI_PORT:-8971}`），Windows / macOS / Linux 行为一致。
如果容器内需要访问宿主机上的代理，可把 `HTTPS_PROXY` 指向 `host.docker.internal:端口`
（Docker Desktop），或在 Linux 上把 compose 改成 `network_mode: host` 并删掉 `ports`。

## 7. 常见问题

**`unauthorized` / `denied`**
检查是否已 `docker login`，以及 GHCR 包是否公开、token 是否有 `read:packages` / `write:packages`。

**`manifest unknown`**
标签不存在。用 `docker buildx imagetools inspect` 查看可用标签，或改用 `latest`。

**`exec format error`**
镜像架构与主机不一致；确认发布时包含 `linux/amd64` 或 `linux/arm64`。

**容器启动后没有配置**
确认挂载了 `.env`，或通过 `--env-file .env` / Compose `env_file` 注入。
下载规则不在 `.env` 里，首次启动后到 WebUI「下载规则」页设置。

**修改了代码但拉取后没变化**
重新构建并推送新标签；不要重复使用已存在的 tag 期望自动更新本地缓存。

```bash
docker buildx build --no-cache --platform linux/amd64,linux/arm64 \
  -t ghcr.io/<owner>/<repo>:latest --push .
```
