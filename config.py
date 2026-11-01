"""系统配置：部署 / 凭据 / 存储 / 请求节流这些写在 .env 的项。

下载规则与调度方式在数据库里维护，见 rules.py。
"""
import os
import re
import threading
from pathlib import Path

from dotenv import load_dotenv

ENV_FILE = Path(__file__).resolve().parent / ".env"

_DEFAULTS = {
    # Pixiv
    "PIXIV_REFRESH_TOKEN": "",
    "PIXIV_DOWNLOAD_REFRESH_TOKEN": "",
    "TOKEN_REFRESH_INTERVAL": "50",
    # 存储
    "STORAGE_BACKEND": "local",
    "DOWNLOAD_PATH": "./data/downloads",
    "DB_PATH": "./data/pixiv_downloader.db",
    "WEBDAV_URL": "",
    "WEBDAV_USERNAME": "",
    "WEBDAV_PASSWORD": "",
    "WEBDAV_ROOT": "pixiv",
    "WEBDAV_VERIFY_SSL": "true",
    "WEBDAV_VERIFY_TIMEOUT": "60",
    "WEBDAV_PUBLIC_URL": "",
    "IMAGE_DELIVERY": "proxy",
    # 请求节流（调度在 WebUI 的「下载规则」页）
    "REQUEST_DELAY": "3.0",
    "REQUEST_JITTER": "1.0",
    "DOWNLOAD_DELAY": "1.5",
    "MAX_RETRIES": "3",
    "RATE_LIMIT_WAIT": "180",
    "ARTIST_COOLDOWN_EVERY": "15",
    "ARTIST_COOLDOWN_SECONDS": "120",
    # 网络
    "HTTPS_PROXY": "",
    # WebUI
    "WEBUI_HOST": "0.0.0.0",
    "WEBUI_PORT": "8971",
    "WEBUI_USERNAME": "",
    "WEBUI_PASSWORD": "",
}

_BOOL_KEYS = {"WEBDAV_VERIFY_SSL"}
_INT_KEYS = {
    "TOKEN_REFRESH_INTERVAL", "WEBUI_PORT", "WEBDAV_VERIFY_TIMEOUT",
    "MAX_RETRIES", "RATE_LIMIT_WAIT", "ARTIST_COOLDOWN_EVERY",
    "ARTIST_COOLDOWN_SECONDS",
}
_FLOAT_KEYS = {"REQUEST_DELAY", "REQUEST_JITTER", "DOWNLOAD_DELAY"}
_ENUM_KEYS = {
    "STORAGE_BACKEND": {"local", "webdav"},
    "IMAGE_DELIVERY": {"redirect", "proxy"},
}

_RELOAD_LOCK = threading.Lock()
_ENV_SIGNATURE = None


def _load_env():
    load_dotenv(dotenv_path=str(ENV_FILE), override=True)


def _coerce(key: str, raw: str):
    if key in _BOOL_KEYS:
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if key in _INT_KEYS:
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            value = int(_DEFAULTS[key])
        if key == "TOKEN_REFRESH_INTERVAL":
            value = max(1, min(59, value))
        elif key == "WEBUI_PORT" and not (1 <= value <= 65535):
            value = int(_DEFAULTS[key])
        elif key == "MAX_RETRIES":
            value = max(1, min(20, value))
        elif key == "RATE_LIMIT_WAIT":
            value = max(0, min(3600, value))
        elif key == "ARTIST_COOLDOWN_EVERY":
            value = max(0, min(1000, value))
        elif key == "ARTIST_COOLDOWN_SECONDS":
            value = max(0, min(7200, value))
        else:
            value = max(0, value)
        return value
    if key in _FLOAT_KEYS:
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            value = float(_DEFAULTS[key])
        return round(max(0.0, min(600.0, value)), 3)
    if key in _ENUM_KEYS:
        value = str(raw or _DEFAULTS[key]).strip().lower()
        return value if value in _ENUM_KEYS[key] else _DEFAULTS[key]
    if key == "WEBDAV_ROOT":
        return str(raw if raw is not None else "pixiv").strip().strip("/")
    if key == "WEBDAV_PUBLIC_URL":
        return str(raw or "").strip().rstrip("/")
    return str(raw or "").strip()


def _env_signature():
    try:
        st = os.stat(ENV_FILE)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def reload(force: bool = False):
    """重新读取 .env；文件未变化时跳过。"""
    global _ENV_SIGNATURE
    signature = _env_signature()
    with _RELOAD_LOCK:
        if not force and signature is not None and signature == _ENV_SIGNATURE:
            return
        _load_env()
        for key, default in _DEFAULTS.items():
            globals()[key] = _coerce(key, os.getenv(key, default))
        _ENV_SIGNATURE = signature


_KV_RE = re.compile(
    r"^(?P<indent>\s*)(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*)$"
)
_SAVE_LOCK = threading.Lock()


class ConfigFile:
    """按行读写 .env，保留注释与未知键。"""

    def __init__(self, path=None):
        self.path = Path(path) if path else ENV_FILE
        self._entries = self._load()

    def _load(self) -> list:
        if not self.path.exists():
            return []
        entries = []
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                entries.append({"type": "comment", "text": raw})
                continue
            m = _KV_RE.match(raw)
            if not m:
                entries.append({"type": "comment", "text": raw})
                continue
            value_part = m.group("value")
            inline_comment = ""
            # 只在 " #" 处切分，避免误伤值中的 #
            idx = value_part.find(" #")
            if idx >= 0:
                inline_comment = value_part[idx:].strip()
                value_part = value_part[:idx].rstrip()
            entries.append({
                "type": "kv",
                "indent": m.group("indent"),
                "key": m.group("key"),
                "value": value_part.strip(),
                "comment": inline_comment,
            })
        return entries

    def _render(self) -> str:
        lines = []
        for e in self._entries:
            if e["type"] == "comment":
                lines.append(e["text"])
            else:
                line = f"{e['indent']}{e['key']}={e['value']}"
                if e.get("comment"):
                    line += f" {e['comment']}"
                lines.append(line)
        return "\n".join(lines) + "\n"

    def get(self, key: str):
        for e in self._entries:
            if e["type"] == "kv" and e["key"] == key:
                return e["value"]
        return None

    def get_all(self) -> dict:
        return {
            e["key"]: e["value"]
            for e in self._entries if e["type"] == "kv"
        }

    def set_many(self, updates: dict):
        for key, value in updates.items():
            value = str(value)
            target = next(
                (e for e in self._entries
                 if e["type"] == "kv" and e["key"] == key),
                None,
            )
            if target is not None:
                target["value"] = value
            else:
                self._entries.append({
                    "type": "kv", "indent": "", "key": key,
                    "value": value, "comment": "",
                })

    def save(self):
        """原子写入；单文件挂载无法 rename 时退化为直接写入。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = self._render()
        tmp_path = self.path.with_name(self.path.name + ".tmp")
        with _SAVE_LOCK:
            try:
                try:
                    tmp_path.write_text(content, encoding="utf-8")
                except OSError:
                    self.path.write_text(content, encoding="utf-8")
                    return
                try:
                    os.replace(tmp_path, self.path)
                except OSError:
                    self.path.write_text(content, encoding="utf-8")
            finally:
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except OSError:
                    pass


reload(force=True)
