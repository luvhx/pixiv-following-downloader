"""下载规则：从 SQLite 读取，与 .env 解耦。

规则分两类：
- settings 表：画质 / 类型 / 过滤阈值 / 单画师上限 / 调度方式；
- tag_rules 表：tag 的 include（匹配才下载）与 exclude（命中即跳过）两种模式。

请求节流参数属于部署配置，放在 .env / 配置页；调度方式放在这里。
模块只负责读写与校验，不依赖 Flask；WebUI 与调度线程共用同一个实例。
"""
import logging
import os
import threading

from croniter import croniter

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
}

_BOOL_KEYS = {"first_page_only", "run_cron_immediate"}
_INT_KEYS = {"min_bookmark_count", "max_per_artist", "poll_interval"}
_FLOAT_KEYS = set()
_ENUM_KEYS = {
    "download_kind": {"original", "large", "medium"},
    "illust_type": {"illust", "manga", "all"},
}
TAG_MODES = ("include", "exclude")

# 表单里的取值范围（闭区间）
_LIMITS = {
    "min_bookmark_count": (0, 1_000_000),
    "max_per_artist": (0, 100_000),
    "poll_interval": (0, 2_592_000),
}


def _as_bool(raw) -> bool:
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def coerce(key: str, raw):
    """把任意输入转成该规则的合法值；非法输入回落到默认值。"""
    if key == "run_cron":
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

    def reset_watermarks(self) -> int:
        count = self.db.reset_watermarks()
        logger.info(f"已重置 {count} 位画师的抓取水位，下轮将全量扫描")
        return count
