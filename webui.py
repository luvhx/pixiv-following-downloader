"""WebUI 路由。

页面结构：
- /            仪表盘
- /browse      作品浏览：关注画师列表
- /browse/<id> 某位画师的全部作品（在线浏览，可单张下载）
- /downloaded  已下载画廊（筛选 / 收藏 / 批量删除）
- /rules       下载规则（含 tag 包含/排除可视化管理）
- /config      系统配置（.env 项）
- /logs        内存日志
"""
import hmac
import logging
import os
import queue
import secrets
import threading
import time
from collections import deque
from math import ceil
from urllib.parse import urlparse

import requests
from flask import (
    Flask, Response, abort, flash, jsonify, redirect,
    render_template, request, send_file, send_from_directory, session, url_for,
)
from werkzeug.exceptions import HTTPException

import config
import rules as rules_module
from config import ConfigFile
from gallery import delete_illust, image_mime, resolve_files, webdav_public_url
from pixiv import (
    api_call, apply_proxy, download_illust, get_following_users,
    illust_tag_names, is_authenticated, reauthenticate, select_thumb_url,
)
from storage import LocalStorage, WebDAVStorage, get_storage

logger = logging.getLogger(__name__)

# Pixiv CDN 会校验 Referer；直接用 <img> 引用（含 no-referrer）会 403，
# 因此由服务端带 Referer 转发中图。这里只做透传，不生成/缩放任何图片。
_PIXIV_REFERER = "https://www.pixiv.net/"
_THUMB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
_thumb_session = requests.Session()
_thumb_session.headers.update({
    "Referer": _PIXIV_REFERER,
    "User-Agent": _THUMB_UA,
    "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
})
_thumb_adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=24)
_thumb_session.mount("https://", _thumb_adapter)
_thumb_session.mount("http://", _thumb_adapter)


def _pixiv_image_url(raw: str) -> str:
    """只允许转发 Pixiv 图片 CDN 的 https 地址，避免变成通用代理。"""
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "pximg.net" or host.endswith(".pximg.net")):
        return ""
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        return ""
    if not parsed.path.startswith("/") or parsed.path.startswith("//"):
        return ""
    return raw


def _thumb_response(url: str):
    """带 Referer 流式转发 Pixiv 中图。"""
    proxies = None
    if config.HTTPS_PROXY:
        proxies = {"http": config.HTTPS_PROXY, "https": config.HTTPS_PROXY}
    try:
        upstream = _thumb_session.get(
            url, stream=True, timeout=(5, 20), proxies=proxies,
        )
    except requests.RequestException as e:
        logger.warning(f"缩略图转发失败 {url}: {e}")
        abort(502)
    if upstream.status_code != 200:
        upstream.close()
        logger.debug(f"缩略图上游返回 {upstream.status_code}: {url}")
        abort(404)

    resp = Response(
        _iter_upstream(upstream),
        content_type=upstream.headers.get("Content-Type") or "image/jpeg",
    )
    for header in ("Content-Length", "ETag", "Last-Modified"):
        value = upstream.headers.get(header)
        if value:
            resp.headers[header] = value
    # 图片 URL 稳定，允许浏览器长期缓存，减少重复转发
    resp.headers["Cache-Control"] = "private, max-age=604800"
    return resp


# ---------- 小工具 ----------
def _iter_upstream(upstream, chunk_size: int = 64 * 1024):
    try:
        for chunk in upstream.iter_content(chunk_size=chunk_size):
            if chunk:
                yield chunk
    finally:
        upstream.close()


def _safe_next(value, default: str) -> str:
    value = (value or "").strip()
    if value.startswith("/") and not value.startswith("//"):
        return value
    return default


def _page_numbers(page: int, total_pages: int, window: int = 2) -> list:
    """生成分页序列，None 表示省略号。"""
    if total_pages <= 1:
        return []
    page = max(1, min(page, total_pages))
    start = max(1, page - window)
    end = min(total_pages, page + window)
    nums = []
    if start > 1:
        nums.append(1)
        if start > 2:
            nums.append(None)
    nums.extend(range(start, end + 1))
    if end < total_pages:
        if end < total_pages - 1:
            nums.append(None)
        nums.append(total_pages)
    return nums


def _load_secret_key() -> bytes:
    if config.DB_PATH:
        base = os.path.dirname(os.path.abspath(config.DB_PATH)) or "."
    else:
        base = str(config.ENV_FILE.parent / "data")
    path = os.path.join(base, ".webui_secret")
    try:
        os.makedirs(base, exist_ok=True)
        if os.path.exists(path):
            with open(path, "rb") as f:
                data = f.read()
            if len(data) >= 32:
                return data
        data = secrets.token_bytes(32)
        with open(path, "wb") as f:
            f.write(data)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return data
    except OSError as e:
        logger.warning(f"无法持久化 WebUI session 密钥，重启后需重新登录: {e}")
        return secrets.token_bytes(32)


# ---------- 下载队列（浏览页单张下载）----------
class DownloadQueue:
    def __init__(self, api, db, rules):
        self.api = api
        self.db = db
        self.rules = rules
        self._queue = queue.Queue()
        self._pending = set()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._worker, daemon=True, name="download-queue")
        self._thread.start()

    def submit(self, illust_id: int) -> bool:
        with self._lock:
            if illust_id in self._pending:
                return False
            self._pending.add(illust_id)
        self._queue.put(illust_id)
        return True

    def pending_ids(self) -> set:
        with self._lock:
            return set(self._pending)

    def _worker(self):
        while True:
            illust_id = self._queue.get()
            try:
                detail = api_call(
                    self.api, self.api.illust_detail, illust_id,
                    delay=0.5, jitter=0.3,
                )
                illust = (detail or {}).get("illust")
                if not illust:
                    logger.warning(f"下载队列：作品 {illust_id} 详情获取失败")
                else:
                    snapshot = self.rules.snapshot(refresh=True)
                    download_illust(self.api, illust, self.db, snapshot)
            except Exception as e:
                logger.error(f"下载队列处理 {illust_id} 失败: {e}")
            finally:
                with self._lock:
                    self._pending.discard(illust_id)
                self._queue.task_done()


# ---------- 内存日志 ----------
class MemoryLogHandler(logging.Handler):
    def __init__(self, capacity: int = 3000):
        super().__init__()
        self._lines = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record):
        try:
            with self._lock:
                self._lines.append(self.format(record))
        except Exception:
            pass

    def lines(self, n: int = 500) -> list:
        with self._lock:
            items = list(self._lines)
        return items[-n:]

    def clear(self):
        with self._lock:
            self._lines.clear()


MEMORY_LOG = MemoryLogHandler()


def install_log_capture():
    root = logging.getLogger()
    if not any(isinstance(h, MemoryLogHandler) for h in root.handlers):
        MEMORY_LOG.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        root.addHandler(MEMORY_LOG)


# ---------- 系统配置字段（.env）----------
CONFIG_FIELDS = [
    ("PIXIV_REFRESH_TOKEN", "Refresh Token", "password", "Pixiv",
     "必填。修改保存后会立即使用新 token 重新认证。", None),
    ("TARGET_USER_ID", "目标用户 ID", "text", "Pixiv",
     "抓取其关注列表的 Pixiv 用户 ID（纯数字）。", None),
    ("TOKEN_REFRESH_INTERVAL", "Token 刷新间隔（分钟）", "int", "Pixiv",
     "后台刷新 access_token 的间隔，1-59。", None),

    ("STORAGE_BACKEND", "存储后端", "select", "存储",
     "local=本地磁盘；webdav=WebDAV（Nextcloud、坚果云、Alist 等）。",
     [("local", "local（本地磁盘）"), ("webdav", "webdav（WebDAV）")]),
    ("DOWNLOAD_PATH", "本地下载目录", "text", "存储",
     "STORAGE_BACKEND=local 时的保存目录。", None),
    ("DB_PATH", "数据库路径", "text", "存储",
     "修改后需重启程序。", None),
    ("WEBDAV_URL", "WebDAV 地址", "text", "存储",
     "例如 https://dav.jianguoyun.com/dav。", None),
    ("WEBDAV_USERNAME", "WebDAV 用户名", "text", "存储", "", None),
    ("WEBDAV_PASSWORD", "WebDAV 密码", "password", "存储", "", None),
    ("WEBDAV_ROOT", "WebDAV 根目录", "text", "存储",
     "上传到该子目录，留空表示根目录。", None),
    ("WEBDAV_VERIFY_SSL", "校验 WebDAV 证书", "select", "存储",
     "使用 http 或自签名证书时可关闭。", [("true", "true"), ("false", "false")]),
    ("WEBDAV_VERIFY_TIMEOUT", "WebDAV 写入确认超时（秒）", "int", "存储",
     "上传后等待文件可见的最长时间；0 表示不校验。", None),

    ("IMAGE_DELIVERY", "原图获取方式", "select", "图片",
     "proxy=由 WebUI 转发；redirect=302 到 WebDAV 直链（浏览器需能直接访问）。",
     [("proxy", "proxy（转发）"), ("redirect", "redirect（302）")]),
    ("WEBDAV_PUBLIC_URL", "WebDAV 浏览器直链", "text", "图片",
     "redirect 模式使用；留空则用 WebDAV 地址。", None),

    ("REQUEST_DELAY", "API 请求间隔（秒）", "float", "请求节流",
     "每次 Pixiv API 请求的基础间隔。", None),
    ("REQUEST_JITTER", "请求随机抖动（秒）", "float", "请求节流",
     "在基础间隔上追加 [0, 该值) 的随机等待。", None),
    ("DOWNLOAD_DELAY", "图片下载间隔（秒）", "float", "请求节流",
     "同一作品多页之间的等待。", None),
    ("MAX_RETRIES", "最大重试次数", "int", "请求节流",
     "单个请求失败后的最多尝试次数（1-20）。", None),
    ("RATE_LIMIT_WAIT", "429 等待基数（秒）", "int", "请求节流",
     "触发限速后按 基数 × 次数 退避。", None),
    ("ARTIST_COOLDOWN_EVERY", "每 N 位画师冷却", "int", "请求节流",
     "每处理 N 位画师强制冷却一次；0 表示关闭。", None),
    ("ARTIST_COOLDOWN_SECONDS", "冷却时长（秒）", "int", "请求节流", "", None),

    ("HTTPS_PROXY", "HTTPS 代理", "text", "网络",
     "例如 http://127.0.0.1:7890。修改后立即生效。", None),

    ("WEBUI_HOST", "监听地址", "text", "WebUI", "修改后需重启。", None),
    ("WEBUI_PORT", "端口", "int", "WebUI", "修改后需重启。", None),
    ("WEBUI_USERNAME", "登录用户名", "text", "WebUI",
     "留空则无需登录；建议公网部署时设置。", None),
    ("WEBUI_PASSWORD", "登录密码", "password", "WebUI",
     "与用户名配合使用，修改后立即生效。", None),
]

RESTART_REQUIRED = {"DB_PATH", "WEBUI_HOST", "WEBUI_PORT"}

SETUP_FIELDS = [
    ("PIXIV_REFRESH_TOKEN", "Refresh Token", "password",
     "必填 · Pixiv refresh_token", None),
    ("TARGET_USER_ID", "目标用户 ID", "text",
     "必填 · 抓取其关注列表的用户 ID", None),
    ("STORAGE_BACKEND", "存储后端", "select",
     "local=本地磁盘 / webdav=WebDAV",
     [("local", "local（本地磁盘）"), ("webdav", "webdav（WebDAV）")]),
    ("DOWNLOAD_PATH", "本地下载目录", "text", "local 后端时的保存目录", None),
    ("WEBDAV_URL", "WebDAV 地址", "text", "webdav 后端时必填", None),
    ("WEBDAV_USERNAME", "WebDAV 用户名", "text", "", None),
    ("WEBDAV_PASSWORD", "WebDAV 密码", "password", "", None),
    ("WEBDAV_ROOT", "WebDAV 根目录", "text", "留空表示根目录", None),
]
SETUP_KEYS = [f[0] for f in SETUP_FIELDS]

# ---------- 下载规则字段（数据库）----------
RULE_FIELDS = [
    ("download_kind", "下载画质", "select",
     "original=原图 / large=大图 / medium=中图。",
     [("original", "original（原图）"), ("large", "large（大图）"),
      ("medium", "medium（中图）")]),
    ("illust_type", "抓取类型", "select",
     "自动抓取时处理哪些作品类型。",
     [("illust", "illust（插画）"), ("manga", "manga（漫画）"),
      ("all", "all（全部）")]),
    ("first_page_only", "多页作品仅首页", "select",
     "开启后多页作品只保存第一页。",
     [("false", "false（全部页面）"), ("true", "true（仅首页）")]),
    ("max_per_artist", "单画师每轮最大下载", "int",
     "每位画师每轮最多下载多少个作品；0 表示不限制。", None),
    ("min_bookmark_count", "最小收藏数", "int",
     "收藏数低于该值的作品跳过；0 表示不过滤。", None),

    ("run_cron", "Cron 表达式", "text",
     "5 段 cron（分 时 日 月 周），例如 0 */6 * * *。留空则使用固定间隔。", None),
    ("run_cron_immediate", "启动时立即执行", "select",
     "程序启动后是否立刻跑一轮。", [("true", "true"), ("false", "false")]),
    ("poll_interval", "固定间隔（秒）", "int",
     "仅在未配置 cron 时生效；0 表示只手动触发。", None),
]
RULE_KEYS = [f[0] for f in RULE_FIELDS]


def _rule_form_values(snapshot: dict) -> dict:
    values = {}
    for key, _label, kind, _help, _options in RULE_FIELDS:
        raw = snapshot.get(key, rules_module.DEFAULTS.get(key))
        if kind == "select" and isinstance(raw, bool):
            raw = "true" if raw else "false"
        values[key] = str(raw)
    return values


# ---------- 表单 ----------
def _grouped_config_fields(values: dict = None, errors: dict = None) -> list:
    groups = []
    index = {}
    for key, label, kind, group, help_text, options in CONFIG_FIELDS:
        if group not in index:
            index[group] = {"name": group, "fields": []}
            groups.append(index[group])
        value = values.get(key) if values is not None else _current_str(key)
        index[group]["fields"].append({
            "key": key, "label": label, "kind": kind,
            "help": help_text, "options": options,
            "value": value if value is not None else "",
            "error": (errors or {}).get(key, ""),
            "restart": key in RESTART_REQUIRED,
        })
    return groups


def _current_str(key: str) -> str:
    value = getattr(config, key)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _parse_config_form(form) -> tuple:
    values, errors = {}, {}
    for key, label, kind, _group, _help, options in CONFIG_FIELDS:
        raw = str(form.get(key, "") or "").strip()
        values[key] = raw
        if kind == "int":
            try:
                if int(raw) < 0:
                    raise ValueError
            except ValueError:
                errors[key] = f"{label} 必须是非负整数"
        elif kind == "float":
            try:
                if float(raw) < 0:
                    raise ValueError
            except ValueError:
                errors[key] = f"{label} 必须是非负数字"
        elif kind == "select":
            allowed = {o[0] for o in (options or [])}
            if raw not in allowed:
                errors[key] = f"{label} 取值无效"

    if not values["PIXIV_REFRESH_TOKEN"]:
        errors["PIXIV_REFRESH_TOKEN"] = "Pixiv refresh_token 不能为空"
    if values["TARGET_USER_ID"] and not values["TARGET_USER_ID"].isdigit():
        errors["TARGET_USER_ID"] = "目标用户 ID 应为纯数字"
    if values["STORAGE_BACKEND"] == "webdav" and not values["WEBDAV_URL"]:
        errors["WEBDAV_URL"] = "存储后端为 webdav 时必须填写 WebDAV 地址"
    if "WEBUI_PORT" not in errors and not (1 <= int(values["WEBUI_PORT"]) <= 65535):
        errors["WEBUI_PORT"] = "端口必须在 1-65535 之间"
    if values["WEBUI_USERNAME"] and not values["WEBUI_PASSWORD"]:
        errors["WEBUI_PASSWORD"] = "设置了用户名则必须同时设置密码"
    if "TOKEN_REFRESH_INTERVAL" not in errors and not (
        1 <= int(values["TOKEN_REFRESH_INTERVAL"]) <= 59
    ):
        errors["TOKEN_REFRESH_INTERVAL"] = "刷新间隔应在 1-59 分钟之间"
    if "MAX_RETRIES" not in errors and not (1 <= int(values["MAX_RETRIES"]) <= 20):
        errors["MAX_RETRIES"] = "最大重试次数应在 1-20 之间"
    return values, errors


def _setup_complete() -> bool:
    if not config.PIXIV_REFRESH_TOKEN or not config.TARGET_USER_ID:
        return False
    if config.STORAGE_BACKEND == "webdav" and not config.WEBDAV_URL:
        return False
    return True


def _apply_runtime_changes(api, old_token: str, old_proxy: str):
    if api is None:
        return
    if config.PIXIV_REFRESH_TOKEN and config.PIXIV_REFRESH_TOKEN != old_token:
        try:
            reauthenticate(api)
            flash("已使用新 refresh_token 重新认证成功", "ok")
        except Exception as e:
            flash(f"配置已保存，但使用新 refresh_token 认证失败: {e}", "error")
    if config.HTTPS_PROXY != old_proxy:
        apply_proxy(api)


# ---------- 登录限速 ----------
_LOGIN_LOCK = threading.Lock()
_LOGIN_FAILS = {}          # ip -> deque[timestamp]
_LOGIN_MAX_FAILS = 5
_LOGIN_WINDOW = 600        # 10 分钟
_LOGIN_BLOCK = 600         # 封禁 10 分钟


def _login_retry_after(ip: str) -> int:
    now = time.time()
    with _LOGIN_LOCK:
        fails = _LOGIN_FAILS.get(ip)
        if not fails:
            return 0
        while fails and now - fails[0] > _LOGIN_WINDOW:
            fails.popleft()
        if len(fails) < _LOGIN_MAX_FAILS:
            return 0
        return max(1, int(fails[-1] + _LOGIN_BLOCK - now))


def _login_failed(ip: str):
    now = time.time()
    with _LOGIN_LOCK:
        fails = _LOGIN_FAILS.setdefault(ip, deque())
        fails.append(now)
        while fails and now - fails[0] > _LOGIN_WINDOW:
            fails.popleft()


def _login_succeeded(ip: str):
    with _LOGIN_LOCK:
        _LOGIN_FAILS.pop(ip, None)


# ---------- 浏览辅助 ----------
def _card(illust: dict, downloaded: set) -> dict:
    user = illust.get("user") or {}
    tags = illust_tag_names(illust)[:8]
    return {
        "illust_id": illust.get("id"),
        "title": illust.get("title") or "untitled",
        "author_id": user.get("id"),
        "author_name": user.get("name") or "",
        "thumb_url": select_thumb_url(illust),
        "page_count": illust.get("page_count") or 1,
        "bookmarks": illust.get("total_bookmarks") or 0,
        "tags": tags,
        "downloaded": illust.get("id") in downloaded,
        "type": illust.get("type") or "illust",
    }


# ---------- Flask 应用 ----------
def create_app(api, db, rules, scheduler) -> Flask:
    app = Flask(__name__)
    app.secret_key = _load_secret_key()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SEND_FILE_MAX_AGE_DEFAULT=86400,
    )
    download_queue = DownloadQueue(api, db, rules) if (api and db) else None

    @app.context_processor
    def _inject_auth_flag():
        return {"auth_enabled": bool(config.WEBUI_USERNAME)}

    def _csrf_token() -> str:
        token = session.get("_csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["_csrf_token"] = token
        return token

    app.jinja_env.globals["csrf_token"] = _csrf_token

    @app.after_request
    def _security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if request.path.startswith("/static/"):
            resp.headers.setdefault("Cache-Control", "public, max-age=86400")
        elif request.path.startswith("/api/"):
            resp.headers.setdefault("Cache-Control", "no-store")
        elif resp.mimetype == "text/html":
            # 配置页可能包含 token/密码，避免浏览器缓存
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    @app.get("/favicon.ico")
    def favicon():
        return send_from_directory(app.static_folder, "favicon.ico", max_age=86400)

    # ---------- 首次配置向导 ----------
    @app.before_request
    def _setup_guard():
        if request.path.startswith("/static/") or request.path in (
            "/setup", "/login", "/healthz", "/favicon.ico",
        ):
            return None
        if _setup_complete():
            return None
        return redirect(url_for("setup"))

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        if _setup_complete():
            return redirect(url_for("index"))
        errors, values = {}, None
        if request.method == "POST":
            values = {}
            for key, label, kind, _help, options in SETUP_FIELDS:
                raw = str(request.form.get(key, "") or "").strip()
                values[key] = raw
                if kind == "select" and raw not in {o[0] for o in (options or [])}:
                    errors[key] = f"{label} 取值无效"
            if not values.get("PIXIV_REFRESH_TOKEN"):
                errors["PIXIV_REFRESH_TOKEN"] = "请填写 Pixiv refresh_token"
            if not values.get("TARGET_USER_ID"):
                errors["TARGET_USER_ID"] = "请填写目标用户 ID"
            elif not values["TARGET_USER_ID"].isdigit():
                errors["TARGET_USER_ID"] = "目标用户 ID 应为纯数字"
            if values.get("STORAGE_BACKEND") == "webdav" and not values.get("WEBDAV_URL"):
                errors["WEBDAV_URL"] = "WebDAV 后端需要填写地址"

            if not errors:
                cfg = ConfigFile()
                cfg.set_many({k: v for k, v in values.items() if cfg.get(k) != v})
                cfg.save()
                old_token, old_proxy = config.PIXIV_REFRESH_TOKEN, config.HTTPS_PROXY
                config.reload(force=True)
                _apply_runtime_changes(api, old_token, old_proxy)
                flash("首次配置完成，可以开始使用了。", "ok")
                return redirect(url_for("index"))
            flash("请检查标红的必填项", "error")

        if values is None:
            values = {k: _current_str(k) for k in SETUP_KEYS}
        fields = []
        for key, label, kind, help_text, options in SETUP_FIELDS:
            fields.append({
                "key": key, "label": label, "kind": kind, "help": help_text,
                "options": options, "value": values.get(key, ""),
                "error": (errors or {}).get(key, ""),
                "required": key in ("PIXIV_REFRESH_TOKEN", "TARGET_USER_ID"),
            })
        return render_template("setup.html", fields=fields, active=None)

    # ---------- 登录 ----------
    ALLOWED_WITHOUT_LOGIN = {"/login", "/logout", "/favicon.ico", "/healthz"}

    def _auth_enabled() -> bool:
        return bool(config.WEBUI_USERNAME)

    def _logged_in() -> bool:
        if not _auth_enabled():
            return True
        return bool(session.get("logged_in")) and session.get("user") == config.WEBUI_USERNAME

    @app.before_request
    def _require_login():
        if request.path.startswith("/static/") or request.path in ALLOWED_WITHOUT_LOGIN:
            return None
        if _logged_in():
            return None
        if request.path.startswith("/api/"):
            return jsonify({"error": "unauthorized"}), 401
        nxt = request.full_path if request.query_string else request.path
        return redirect(url_for("login", next=nxt))

    @app.before_request
    def _verify_csrf():
        if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
            return None
        expected = session.get("_csrf_token")
        sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token")
        if not expected or not sent or not hmac.compare_digest(str(expected), str(sent)):
            logger.warning(f"CSRF 校验失败: {request.method} {request.path}")
            abort(400, description="CSRF token 校验失败，请刷新页面后重试")
        return None

    @app.route("/login", methods=["GET", "POST"])
    def login():
        next_url = request.args.get("next") or request.form.get("next") or ""
        if not _auth_enabled():
            flash("未设置 WebUI 用户名/密码，无需登录", "warn")
            return redirect(url_for("index"))
        if _logged_in():
            return redirect(url_for("index"))

        ip = request.remote_addr or "unknown"
        retry_after = _login_retry_after(ip)
        if retry_after:
            logger.warning(f"登录失败次数过多，暂时封禁 {ip}（剩余 {retry_after}s）")
            return render_template(
                "login.html", next=next_url, active=None,
                error=f"尝试次数过多，请 {retry_after // 60 + 1} 分钟后再试",
            ), 429

        if request.method == "POST":
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            if username == config.WEBUI_USERNAME and hmac.compare_digest(
                    str(password), str(config.WEBUI_PASSWORD or "")):
                session.clear()
                session["logged_in"] = True
                session["user"] = username
                _login_succeeded(ip)
                logger.info("WebUI 登录成功")
                if next_url.startswith("/") and not next_url.startswith("//"):
                    return redirect(next_url)
                return redirect(url_for("index"))
            _login_failed(ip)
            logger.warning("WebUI 登录失败：用户名或密码错误")
            flash("用户名或密码错误", "error")
        return render_template("login.html", next=next_url, error=None, active=None)

    @app.post("/logout")
    def logout():
        session.clear()
        flash("已退出登录", "ok")
        return redirect(url_for("login"))

    @app.get("/healthz")
    def healthz():
        return jsonify(status="ok")

    # ---------- 仪表盘 ----------
    @app.route("/")
    def index():
        status = scheduler.status() if scheduler else {
            "running": False, "paused": False, "mode": "未运行",
            "next_run": None, "last_run": None, "last_stats": None,
            "target_user_id": config.TARGET_USER_ID,
        }
        db_stats = db.stats() if db else {"downloaded": 0, "artists": 0}
        recent = db.list_downloaded(limit=6)["rows"] if db else []
        if recent and db:
            tag_map = db.tags_for_many([r["illust_id"] for r in recent])
            for row in recent:
                row["tags"] = tag_map.get(row["illust_id"], [])
        following = db.following() if db else []
        backend = config.STORAGE_BACKEND
        if backend == "webdav":
            backend += f"（{config.WEBDAV_URL}/{config.WEBDAV_ROOT}）"
        return render_template(
            "index.html",
            active="index", status=status, db_stats=db_stats,
            recent=recent, backend=backend, authed=_api_authed(api),
            following_count=len(following),
            rules=rules.snapshot(),
            tag_rules=rules.tags(),
            log_text="\n".join(MEMORY_LOG.lines(80)),
        )

    # ---------- 系统配置 ----------
    @app.route("/config", methods=["GET", "POST"])
    def config_page():
        errors, values = {}, None
        if request.method == "POST":
            values, errors = _parse_config_form(request.form)
            if not errors:
                cfg = ConfigFile()
                changed = {k: v for k, v in values.items() if cfg.get(k) != v}
                cfg.set_many(changed)
                cfg.save()
                old_token, old_proxy = config.PIXIV_REFRESH_TOKEN, config.HTTPS_PROXY
                config.reload(force=True)
                _apply_runtime_changes(api, old_token, old_proxy)
                flash("系统配置已保存", "ok")
                return redirect(url_for("config_page"))
            flash("配置校验未通过，请检查标红字段", "error")
        return render_template(
            "config.html", active="config",
            groups=_grouped_config_fields(values, errors),
        )

    # ---------- 下载规则 ----------
    @app.route("/rules", methods=["GET", "POST"])
    def rules_page():
        snapshot = rules.snapshot(refresh=True)
        errors, values = {}, None
        if request.method == "POST":
            values = {k: request.form.get(k, "") for k in RULE_KEYS}
            errors = rules.update(values)
            if errors:
                flash(f"规则未保存：{len(errors)} 项需要修正", "error")
            else:
                if scheduler:
                    scheduler.notify_schedule_changed()
                flash("下载规则已保存，下一轮生效", "ok")
                return redirect(url_for("rules_page"))
        if values is None:
            values = _rule_form_values(snapshot)

        fields = []
        for key, label, kind, help_text, options in RULE_FIELDS:
            fields.append({
                "key": key, "label": label, "kind": kind, "help": help_text,
                "options": options, "value": values.get(key, ""),
                "error": errors.get(key, ""),
            })
        schedule_keys = {"run_cron", "run_cron_immediate", "poll_interval"}
        tag_rules = rules.tags()
        return render_template(
            "rules.html", active="rules",
            download_fields=[f for f in fields if f["key"] not in schedule_keys],
            schedule_fields=[f for f in fields if f["key"] in schedule_keys],
            include_tags=[t for t in tag_rules if t["mode"] == "include"],
            exclude_tags=[t for t in tag_rules if t["mode"] == "exclude"],
        )

    @app.post("/rules/tag/add")
    def rules_tag_add():
        tag = (request.form.get("tag") or "").strip()
        mode = (request.form.get("mode") or "exclude").strip()
        purge = request.form.get("purge") in ("1", "true", "on", "yes")
        ok, error = rules.add_tag(tag, mode)
        if not ok:
            flash(f"添加 tag 失败：{error}", "error")
            return redirect(_safe_next(request.form.get("next"), url_for("rules_page")))

        message = f"已{'加入匹配' if mode == 'include' else '加入排除'}：{tag}"
        deleted = failed = 0
        if purge and mode != "exclude":
            flash("只有排除 tag 支持删除已下载作品", "warn")
        elif purge and not db:
            flash("数据库不可用，未删除作品", "error")
        elif purge:
            try:
                storage = get_storage()
            except Exception as e:
                flash(f"存储不可用，未删除作品：{e}", "error")
            else:
                for row in db.list_by_tag(tag):
                    try:
                        result = delete_illust(db, storage, row)
                        if result["removed"]:
                            deleted += 1
                        else:
                            failed += 1
                    except Exception as e:
                        logger.warning(f"删除含 tag 作品失败 {row.get('illust_id')}: {e}")
                        failed += 1
                message += f"，并删除 {deleted} 个已下载作品"
                if failed:
                    message += f"（{failed} 个删除失败）"
        flash(message, "ok" if not failed else "warn")
        return redirect(_safe_next(request.form.get("next"), url_for("rules_page")))

    @app.post("/rules/tag/remove")
    def rules_tag_remove():
        tag = (request.form.get("tag") or "").strip()
        if rules.remove_tag(tag):
            flash(f"已移除 tag 规则：{tag}", "ok")
        else:
            flash(f"没有找到 tag 规则：{tag}", "warn")
        return redirect(_safe_next(request.form.get("next"), url_for("rules_page")))

    @app.post("/rules/reset-watermarks")
    def rules_reset_watermarks():
        count = rules.reset_watermarks()
        flash(f"已重置 {count} 位画师的抓取水位，下一轮将重新全量扫描", "ok")
        return redirect(url_for("rules_page"))

    # ---------- 作品浏览 ----------
    @app.get("/browse")
    def browse():
        preview = request.args.get("preview") in ("1", "true", "yes")
        following = db.following() if db else []
        counts = {}
        if db:
            counts = {a["id"]: a["count"] for a in db.artist_options()}
        preview_ids = [
            work["illust_id"]
            for artist in following for work in (artist.get("previews") or [])
        ]
        downloaded = db.downloaded_ids(preview_ids) if (db and preview) else set()
        artists = []
        for artist in following:
            item = dict(artist)
            item["downloaded"] = counts.get(artist["artist_id"], 0)
            artists.append(item)
        return render_template(
            "browse.html", active="browse", artists=artists,
            updated_at=(db.following_updated_at() if db else None),
            target_user_id=config.TARGET_USER_ID,
            auth_ok=_api_authed(api),
            preview=preview,
            downloaded=downloaded,
            has_previews=any(a.get("previews") for a in following),
        )

    @app.post("/browse/refresh")
    def browse_refresh():
        if api is None or not _api_authed(api):
            flash("Pixiv 未认证，无法刷新关注列表", "error")
            return redirect(url_for("browse"))
        try:
            following = get_following_users(api, config.TARGET_USER_ID)
            if not following:
                # 请求失败或确实没有关注：保留旧缓存，避免把列表清空
                flash("没有获取到关注画师，已保留原缓存", "warn")
            else:
                if db:
                    db.replace_following(following)
                flash(f"已刷新关注列表：{len(following)} 位画师", "ok")
        except Exception as e:
            logger.error(f"刷新关注列表失败: {e}")
            flash(f"刷新失败：{e}", "error")
        return redirect(url_for("browse"))

    @app.get("/browse/<int:artist_id>")
    def browse_artist(artist_id):
        kind = request.args.get("type") or "illust"
        if kind not in ("illust", "manga"):
            kind = "illust"
        page = max(1, request.args.get("page", type=int) or 1)
        per_page = 30
        offset = (page - 1) * per_page

        artist = next(
            (a for a in (db.following() if db else []) if a["artist_id"] == artist_id),
            None,
        )
        works, has_next, error = [], False, None
        if api is None or not _api_authed(api):
            error = "Pixiv 未认证，无法在线浏览"
        else:
            try:
                resp = api_call(
                    api, api.user_illusts, artist_id, type=kind,
                    offset=offset or None,
                    delay=min(1.0, config.REQUEST_DELAY),
                    jitter=config.REQUEST_JITTER,
                ) or {}
                if resp.get("error"):
                    error = f"Pixiv 返回错误：{resp['error']}"
                downloaded = db.downloaded_ids(
                    [i["id"] for i in (resp.get("illusts") or [])]
                ) if db else set()
                works = [_card(i, downloaded) for i in (resp.get("illusts") or [])]
                has_next = bool(resp.get("next_url"))
                if artist is None and works:
                    artist = {
                        "artist_id": artist_id,
                        "artist_name": works[0]["author_name"],
                        "avatar_url": None,
                    }
            except Exception as e:
                logger.error(f"浏览画师 {artist_id} 作品失败: {e}")
                error = f"加载失败：{e}"

        pending = download_queue.pending_ids() if download_queue else set()
        downloaded_count = next(
            (a["count"] for a in (db.artist_options() if db else [])
             if a["id"] == artist_id), 0
        )
        return render_template(
            "browse_artist.html", active="browse",
            artist=artist or {"artist_id": artist_id, "artist_name": str(artist_id),
                              "avatar_url": None},
            works=works, has_next=has_next, error=error,
            kind=kind, page=page, pending=pending,
            downloaded_count=downloaded_count,
            favorite_tag_norms={
                t.strip().lower() for t in (db.favorite_tags() if db else [])
            },
            excluded_tags=[t["norm"] for t in rules.tags("exclude")],
            pages=[],
        )

    @app.post("/browse/download/<int:illust_id>")
    def browse_download(illust_id):
        if not download_queue:
            flash("下载队列不可用（Pixiv 未认证）", "error")
            return redirect(_safe_next(request.form.get("next"), url_for("browse")))
        if download_queue.submit(illust_id):
            flash(f"已加入下载队列：作品 {illust_id}", "ok")
        else:
            flash(f"作品 {illust_id} 已在下载队列中", "warn")
        return redirect(_safe_next(request.form.get("next"), url_for("browse")))

    @app.post("/browse/download-batch")
    def browse_download_batch():
        if not download_queue:
            flash("下载队列不可用（Pixiv 未认证）", "error")
            return redirect(_safe_next(request.form.get("next"), url_for("browse")))
        ids = []
        for raw in request.form.getlist("illust_id"):
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value not in ids:
                ids.append(value)
        if not ids:
            flash("没有选中任何作品", "warn")
            return redirect(_safe_next(request.form.get("next"), url_for("browse")))
        if len(ids) > 100:
            flash("一次最多加入 100 个作品", "warn")
            return redirect(_safe_next(request.form.get("next"), url_for("browse")))

        already = db.downloaded_ids(ids) if db else set()
        added = skipped = 0
        for illust_id in ids:
            if illust_id in already:
                skipped += 1
                continue
            if download_queue.submit(illust_id):
                added += 1
            else:
                skipped += 1
        message = f"已加入下载队列：{added} 个作品"
        if skipped:
            message += f"，跳过 {skipped} 个（已下载或已在队列）"
        flash(message, "ok" if added else "warn")
        return redirect(_safe_next(request.form.get("next"), url_for("browse")))

    # ---------- 已下载（原画廊）----------
    @app.get("/downloaded")
    def downloaded():
        q = (request.args.get("q") or "").strip()
        tag = (request.args.get("tag") or "").strip()
        artist_id = request.args.get("artist", type=int) or None
        favorites_only = request.args.get("fav") in ("1", "true", "yes")
        sort = request.args.get("sort") or "new"
        if sort not in ("new", "old", "bookmarks", "pages", "title"):
            sort = "new"
        page = max(1, request.args.get("page", type=int) or 1)
        per_page = 24
        data = db.list_downloaded(
            q=q, tag=tag, artist_id=artist_id, favorites_only=favorites_only,
            offset=(page - 1) * per_page, limit=per_page, sort=sort,
        ) if db else {"total": 0, "rows": []}

        total_pages = max(1, ceil(data["total"] / per_page))
        if page > total_pages:
            page = total_pages
            data = db.list_downloaded(
                q=q, tag=tag, artist_id=artist_id, favorites_only=favorites_only,
                offset=(page - 1) * per_page, limit=per_page, sort=sort,
            ) if db else {"total": 0, "rows": []}

        items = []
        tag_map = db.tags_for_many([r["illust_id"] for r in data["rows"]]) if db else {}
        for row in data["rows"]:
            item = dict(row)
            item["tags"] = tag_map.get(row["illust_id"], [])
            items.append(item)

        return render_template(
            "downloaded.html", active="downloaded",
            rows=items, total=data["total"], q=q, tag=tag,
            artist_id=artist_id, favorites_only=favorites_only,
            favorite_tags=db.favorite_tags() if db else [],
            artists=db.artist_options() if db else [],
            tags=db.tag_options() if db else [],
            sort=sort,
            page=page, total_pages=total_pages,
            pages=_page_numbers(page, total_pages),
        )

    # 兼容旧链接：/gallery、/gallery/<id>、/downloads -> 新的已下载页面
    @app.get("/gallery")
    def legacy_gallery():
        return redirect(url_for("downloaded", **request.args))

    @app.get("/gallery/<int:illust_id>")
    def legacy_gallery_detail(illust_id):
        return redirect(
            url_for("downloaded_detail", illust_id=illust_id, **request.args)
        )

    @app.get("/downloads")
    def legacy_downloads():
        return redirect(url_for("downloaded", **request.args))

    @app.get("/downloaded/<int:illust_id>")
    def downloaded_detail(illust_id):
        row = db.get(illust_id) if db else None
        if not row:
            abort(404)
        try:
            files = resolve_files(get_storage(), db, row)
        except Exception as e:
            logger.warning(f"读取作品 {illust_id} 文件失败: {e}")
            files = []
        tags = db.tags_for(illust_id) if db else []
        try:
            related = db.related(illust_id, row.get("author_id"), tags, limit=8) if db else []
        except Exception as e:
            logger.warning(f"读取相关作品失败 {illust_id}: {e}")
            related = []
        if related and db:
            related_tags = db.tags_for_many([r["illust_id"] for r in related])
            for item in related:
                item["tags"] = related_tags.get(item["illust_id"], [])

        back_args = {}
        for key in ("q", "tag", "artist", "page", "fav", "sort"):
            value = request.args.get(key)
            if value:
                back_args[key] = value

        page_count = max(1, len(files))
        page = request.args.get("p", type=int) or 1
        page = min(max(1, page), page_count)
        prev_url = (
            url_for("downloaded_detail", illust_id=illust_id, p=page - 1, **back_args)
            if page > 1 else None
        )
        page_next_url = (
            url_for("downloaded_detail", illust_id=illust_id, p=page + 1, **back_args)
            if page < page_count else None
        )
        next_url = request.full_path if request.query_string else request.path
        return render_template(
            "work_detail.html", active="downloaded",
            row=row, files=files, tags=tags, related=related,
            favorite_tags=db.favorite_tags() if db else [],
            back_url=url_for("downloaded", **back_args),
            next_url=next_url,
            page=page, page_count=page_count,
            prev_url=prev_url, page_next_url=page_next_url,
            page_args=back_args,
        )

    @app.post("/downloaded/<int:illust_id>/favorite")
    def downloaded_favorite(illust_id):
        row = db.get(illust_id) if db else None
        if not row:
            flash("作品不存在或已删除", "warn")
        else:
            favorite = not bool(row.get("favorite"))
            db.set_favorite(illust_id, favorite)
            flash("已加入收藏" if favorite else "已取消收藏", "ok")
        return redirect(_safe_next(request.form.get("next"), url_for("downloaded")))

    @app.post("/tag/favorite")
    def favorite_tag():
        tag = (request.form.get("tag") or "").strip()[:100]
        default = url_for("downloaded")
        if not tag:
            flash("tag 不能为空", "warn")
        elif not db:
            flash("数据库不可用", "error")
        else:
            favorite = not db.is_tag_favorite(tag)
            db.set_tag_favorite(tag, favorite)
            flash(
                f"已收藏 tag「{tag}」" if favorite else f"已取消收藏 tag「{tag}」",
                "ok",
            )
        return redirect(_safe_next(request.form.get("next"), default))

    @app.get("/thumb")
    def thumb_proxy():
        url = _pixiv_image_url(request.args.get("u", ""))
        if not url:
            abort(400)
        return _thumb_response(url)

    @app.get("/thumb/<int:illust_id>")
    def thumb_for_work(illust_id):
        row = db.get(illust_id) if db else None
        url = _pixiv_image_url((row or {}).get("thumb_url") or "")
        if not url:
            abort(404)
        return _thumb_response(url)

    @app.get("/image/<int:illust_id>")
    def work_image(illust_id):
        row = db.get(illust_id) if db else None
        if not row:
            abort(404)
        try:
            storage = get_storage()
            files = resolve_files(storage, db, row)
            if not files:
                abort(404)
            page = request.args.get("p", type=int) or 1
            if not (1 <= page <= len(files)):
                abort(404)
            file = files[page - 1]

            if config.IMAGE_DELIVERY == "redirect" and isinstance(storage, WebDAVStorage):
                url = webdav_public_url(file, config.WEBDAV_ROOT)
                if url:
                    resp = redirect(url, code=302)
                    resp.headers["Cache-Control"] = "private, max-age=3600"
                    return resp

            if isinstance(storage, LocalStorage):
                path = storage.abs_path(file)
                if os.path.exists(path):
                    resp = send_file(
                        path, mimetype=image_mime(file),
                        max_age=86400, conditional=True,
                    )
                    resp.headers["Cache-Control"] = "private, max-age=86400"
                    return resp

            if isinstance(storage, WebDAVStorage):
                upstream = storage.open_stream(file)
                resp = Response(
                    _iter_upstream(upstream), mimetype=image_mime(file),
                )
                for header in ("Content-Length", "ETag", "Last-Modified"):
                    value = upstream.headers.get(header)
                    if value:
                        resp.headers[header] = value
                resp.headers["Cache-Control"] = "private, max-age=86400"
                return resp

            abort(404)
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"读取图片失败 {illust_id}: {e}")
            abort(404)

    @app.post("/downloaded/delete")
    def downloaded_delete():
        illust_id = request.form.get("illust_id", type=int)
        row = db.get(illust_id) if (db and illust_id) else None
        if not row:
            flash("记录不存在或已删除", "warn")
            return redirect(_downloaded_back())
        try:
            result = delete_illust(db, get_storage(), row)
        except Exception as e:
            logger.error(f"删除作品失败 {illust_id}: {e}")
            flash(f"删除失败: {e}", "error")
            return redirect(_downloaded_back())

        if result["removed"]:
            flash(f"已删除《{row.get('title')}》（文件 {result['deleted']} 个）", "ok")
        else:
            flash(
                f"删除未完成：{result['errors']} 个文件删除失败，记录已保留，可重试",
                "error",
            )
        return redirect(_downloaded_back())

    @app.post("/downloaded/delete-batch")
    def downloaded_delete_batch():
        ids = []
        for raw in request.form.getlist("illust_id"):
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value not in ids:
                ids.append(value)
        if not ids:
            flash("没有选中任何作品", "warn")
            return redirect(_downloaded_back())
        if len(ids) > 200:
            flash("一次最多删除 200 个作品，请分页操作", "warn")
            return redirect(_downloaded_back())

        try:
            storage = get_storage()
        except Exception as e:
            logger.error(f"批量删除时存储不可用: {e}")
            flash(f"存储不可用: {e}", "error")
            return redirect(_downloaded_back())

        removed = files_deleted = failed = 0
        for illust_id in ids:
            row = db.get(illust_id) if db else None
            if not row:
                continue
            try:
                result = delete_illust(db, storage, row)
                if result["removed"]:
                    removed += 1
                    files_deleted += result["deleted"]
                else:
                    failed += 1
            except Exception as e:
                failed += 1
                logger.error(f"批量删除作品失败 {illust_id}: {e}")

        msg = f"批量删除：作品 {removed} 个，文件 {files_deleted} 个"
        if failed:
            msg += f"，{failed} 个未删除（记录已保留）"
        flash(msg, "ok" if removed and not failed else "warn")
        return redirect(_downloaded_back())

    def _downloaded_back():
        args = {}
        q = (request.form.get("q") or "").strip()
        tag = (request.form.get("tag") or "").strip()
        artist = request.form.get("artist", type=int)
        page = request.form.get("page", type=int) or 1
        if q:
            args["q"] = q
        if tag:
            args["tag"] = tag
        if artist:
            args["artist"] = artist
        if page > 1:
            args["page"] = page
        if request.form.get("fav") in ("1", "true", "yes"):
            args["fav"] = 1
        sort = (request.form.get("sort") or "").strip()
        if sort and sort != "new":
            args["sort"] = sort
        return url_for("downloaded", **args)

    # ---------- 日志 ----------
    @app.route("/logs")
    def logs():
        n = request.args.get("n", type=int) or 1000
        n = max(50, min(n, 5000))
        return render_template(
            "logs.html", active="logs", log_text="\n".join(MEMORY_LOG.lines(n)), n=n,
        )

    # ---------- 操作 ----------
    @app.post("/run")
    def run_now():
        msg = scheduler.trigger_now() if scheduler else "调度器未运行"
        ok = "已触发" in msg or "再跑一轮" in msg
        flash(msg, "ok" if ok else "warn")
        return redirect(url_for("index"))

    @app.post("/pause")
    def pause():
        if scheduler:
            scheduler.set_paused(True)
        flash("已暂停调度，当前轮次完成后不再运行", "ok")
        return redirect(url_for("index"))

    @app.post("/resume")
    def resume():
        if scheduler:
            scheduler.set_paused(False)
        flash("已恢复调度", "ok")
        return redirect(url_for("index"))

    @app.post("/logs/clear")
    def logs_clear():
        MEMORY_LOG.clear()
        flash("内存日志已清空（不影响控制台输出）", "ok")
        return redirect(url_for("logs"))

    @app.post("/reauth")
    def reauth():
        if api is None:
            flash("API 客户端不可用", "error")
            return redirect(url_for("index"))
        if not config.PIXIV_REFRESH_TOKEN:
            flash("未配置 refresh_token，请先在配置页填写", "error")
            return redirect(url_for("config_page"))
        try:
            reauthenticate(api)
            flash("重新认证成功", "ok")
        except Exception as e:
            logger.error(f"WebUI 手动重新认证失败: {e}")
            flash(f"重新认证失败：{e}。若提示 invalid_grant，说明 refresh_token 已失效。", "error")
        return redirect(url_for("index"))

    # ---------- API ----------
    @app.get("/api/status")
    def api_status():
        return jsonify(
            status=scheduler.status() if scheduler else {},
            db=db.stats() if db else {},
            authed=_api_authed(api),
            log=MEMORY_LOG.lines(200),
        )

    @app.get("/api/logs")
    def api_logs():
        n = request.args.get("n", type=int) or 1000
        n = max(50, min(n, 5000))
        return jsonify(log=MEMORY_LOG.lines(n))

    return app


def _api_authed(api) -> bool:
    try:
        return bool(api) and is_authenticated(api)
    except Exception:
        return False
