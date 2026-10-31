"""下载规则：从 SQLite 读取，与 .env 解耦。

规则分两类：
- settings 表：画质 / 类型 / 过滤阈值 / 单画师上限 / 调度方式；
- tag_rules 表：tag 的 include（匹配才下载）与 exclude（命中即跳过）两种模式。

请求节流参数属于部署配置，放在 .env / 配置页；调度方式放在这里。
模块只负责读写与校验，不依赖 Flask；WebUI 与调度线程共用同一个实例。
"""
import logging
import os
import re
import threading

from croniter import croniter

from database import normalize_tag

logger = logging.getLogger(__name__)

# 默认值；类型由 _INT_KEYS / _FLOAT_KEYS / _ENUM_KEYS / _BOOL_KEYS 决定
DEFAULTS = {
    "download_kind": "original",
    "illust_type": "illust",
    "first_page_only": False,
    "min_bookmark_count": 0,
    "max_per_artist": 0,
    "run_cron": "",
    "run_cron_immediate": True,
    "poll_interval": 0,
    "ugoira_format": "webm",
    "folder_template": "",
    "filename_template": "",
}

_BOOL_KEYS = {"first_page_only", "run_cron_immediate"}
_INT_KEYS = {"min_bookmark_count", "max_per_artist", "poll_interval"}
_FLOAT_KEYS = set()
_ENUM_KEYS = {
    "download_kind": {"original", "large", "medium"},
    "illust_type": {"illust", "manga", "all"},
    "ugoira_format": {"webm", "gif", "zip"},
}
TAG_MODES = ("include", "exclude")

# 文件/目录模板支持的占位符
TEMPLATE_KEYS = {
    "id", "illust_id", "title", "artist", "artist_id",
    "page", "pages", "type", "date", "ext",
}
_TEXT_KEYS = {"folder_template", "filename_template"}
_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")

# 表单里的取值范围（闭区间）
_LIMITS = {
    "min_bookmark_count": (0, 1_000_000),
    "max_per_artist": (0, 100_000),
    "poll_interval": (0, 2_592_000),
}


def _template_error(key: str, value: str) -> str:
    if len(value) > 200:
        return "最多 200 个字符"
    unknown = {m for m in _PLACEHOLDER_RE.findall(value)} - TEMPLATE_KEYS
    if unknown:
        return "不支持的占位符：" + ", ".join(sorted(unknown))
    if key == "folder_template":
        if value.startswith("/") or "\\" in value:
            return "目录模板不能以 / 开头，也不能包含反斜杠"
        if ".." in value.split("/"):
            return "目录模板不能包含 .."
    else:
        if "/" in value or "\\" in value or ".." in value:
            return "文件名模板不能包含路径分隔符或 .."
    return ""


def _as_bool(raw) -> bool:
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def coerce(key: str, raw):
    """把任意输入转成该规则的合法值；非法输入回落到默认值。"""
    if key in ("run_cron", "folder_template", "filename_template"):
        return str(raw or "").strip()
    if key in _BOOL_KEYS:
        return _as_bool(raw)
    if key in _INT_KEYS or key in _FLOAT_KEYS:
        try:
            value = int(str(raw).strip()) if key in _INT_KEYS else float(str(raw).strip())
        except (TypeError, ValueError):
            value = DEFAULTS[key]
        low, high = _LIMITS.get(key, (0, None))
        value = max(low, value)
        if high is not None:
            value = min(high, value)
        return int(value) if key in _INT_KEYS else round(float(value), 3)
    if key in _ENUM_KEYS:
        value = str(raw or DEFAULTS[key]).strip().lower()
        return value if value in _ENUM_KEYS[key] else DEFAULTS[key]
    return raw


class Rules:
    def __init__(self, db):
        self.db = db
        self._lock = threading.Lock()
        self._snapshot = None

    # ---------- 读取 ----------
    def _build(self) -> dict:
        stored = self.db.get_settings()
        values = {}
        for key, default in DEFAULTS.items():
            values[key] = coerce(key, stored.get(key, default))

        rules = self.db.tag_rules()
        values["include_tags"] = [r["norm"] for r in rules if r["mode"] == "include"]
        values["exclude_tags"] = [r["norm"] for r in rules if r["mode"] == "exclude"]
        return values

    def snapshot(self, refresh: bool = False) -> dict:
        with self._lock:
            if self._snapshot is None or refresh:
                self._snapshot = self._build()
            return dict(self._snapshot)

    def invalidate(self):
        with self._lock:
            self._snapshot = None

    def get(self, key, default=None):
        return self.snapshot().get(key, default)

    # ---------- 写入 ----------
    def update(self, values: dict) -> dict:
        """校验并保存规则；返回 {字段: 错误}，为空表示成功。"""
        clean, errors = self.validate(values)
        if errors:
            return errors
        self.db.set_settings(clean)
        self.invalidate()
        logger.info("下载规则已更新: %s", ", ".join(f"{k}={v}" for k, v in clean.items()))
        return {}

    def validate(self, values: dict) -> tuple:
        clean, errors = {}, {}
        for key, default in DEFAULTS.items():
            if key in _BOOL_KEYS:
                clean[key] = str(_as_bool(values.get(key, default))).lower()
                continue
            if key == "run_cron":
                value = str(values.get(key, default) or "").strip()
                if value and not croniter.is_valid(value):
                    errors[key] = "cron 表达式无效（应为 5 段：分 时 日 月 周）"
                    continue
                clean[key] = value
                continue
            if key in _TEXT_KEYS:
                value = str(values.get(key, default) or "").strip()
                error = _template_error(key, value)
                if error:
                    errors[key] = error
                    continue
                clean[key] = value
                continue
            raw = values.get(key, default)
            if raw is None or str(raw).strip() == "":
                errors[key] = "不能为空"
                continue
            try:
                if key in _INT_KEYS:
                    value = int(str(raw).strip())
                elif key in _FLOAT_KEYS:
                    value = float(str(raw).strip())
                else:
                    value = str(raw).strip().lower()
            except (TypeError, ValueError):
                errors[key] = "格式不正确"
                continue
            if key in _ENUM_KEYS and value not in _ENUM_KEYS[key]:
                errors[key] = "取值无效"
                continue
            if key in _LIMITS:
                low, high = _LIMITS[key]
                if value < low or (high is not None and value > high):
                    errors[key] = f"应在 {low} - {high} 之间"
                    continue
            clean[key] = str(int(value)) if key in _INT_KEYS else (
                str(round(value, 3)) if key in _FLOAT_KEYS else value
            )
        return clean, errors

    def migrate_legacy_env(self):
        """把旧版本放在 .env 里的调度配置导入数据库（只导一次）。"""
        stored = self.db.get_settings()
        if "run_cron" in stored or "poll_interval" in stored:
            return False
        cron = (os.getenv("RUN_CRON") or "").strip()
        poll = (os.getenv("POLL_INTERVAL") or "0").strip()
        if cron and not croniter.is_valid(cron):
            logger.warning(f"旧 .env 中的 RUN_CRON 无效，已忽略: {cron}")
            cron = ""
        if not cron and poll in ("", "0"):
            return False
        self.db.set_settings({
            "run_cron": cron,
            "poll_interval": poll,
            "run_cron_immediate": (os.getenv("RUN_CRON_IMMEDIATE") or "true").strip(),
        })
        self.invalidate()
        logger.info("已将旧版 .env 中的调度配置迁移到下载规则")
        return True

    # ---------- tag 规则 ----------
    def tags(self, mode: str = None) -> list:
        return self.db.tag_rules(mode)

    def add_tag(self, tag: str, mode: str) -> tuple:
        tag = (tag or "").strip()
        if not tag:
            return False, "tag 不能为空"
        if mode not in TAG_MODES:
            return False, "模式必须是 include 或 exclude"
        if len(tag) > 100:
            return False, "tag 过长（最多 100 字符）"
        self.db.set_tag_rule(tag, mode)
        self.invalidate()
        logger.info("tag 规则已保存: %s (%s)", tag, mode)
        return True, ""

    def remove_tag(self, tag: str) -> bool:
        removed = self.db.remove_tag_rule(tag)
        self.invalidate()
        return removed

    def export_tags(self) -> dict:
        rules = self.tags()
        return {
            "version": 1,
            "include": [r["tag"] for r in rules if r["mode"] == "include"],
            "exclude": [r["tag"] for r in rules if r["mode"] == "exclude"],
        }

    def import_tags(self, data: dict, replace: bool = False) -> dict:
        """导入 tag 规则；返回 {added, invalid, replaced}。"""
        if not isinstance(data, dict):
            raise ValueError("JSON 顶层必须是对象")
        pairs = []
        for mode in ("include", "exclude"):
            values = data.get(mode) or []
            if not isinstance(values, list):
                raise ValueError(f"{mode} 必须是数组")
            for value in values:
                tag = str(value).strip()
                if not tag or len(tag) > 100:
                    continue
                pairs.append((tag, mode))
        if len(pairs) > 2000:
            raise ValueError("一次最多导入 2000 个 tag")
        if replace:
            self.db.clear_tag_rules()
        added = 0
        seen = set()
        for tag, mode in pairs:
            key = (normalize_tag(tag), mode)
            if key in seen:
                continue
            seen.add(key)
            if self.db.set_tag_rule(tag, mode):
                added += 1
        self.invalidate()
        logger.info(f"导入 tag 规则：{added} 个（{'替换' if replace else '合并'}）")
        return {"added": added, "invalid": len(pairs) - len(seen),
                "replaced": bool(replace)}

    def reset_watermarks(self) -> int:
        count = self.db.reset_watermarks()
        logger.info(f"已重置 {count} 位画师的抓取水位，下轮将全量扫描")
        return count
