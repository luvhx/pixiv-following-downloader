"""cron / 固定间隔 / 手动触发调度线程。

每轮开始重新读取数据库里的下载规则；按画师水位做增量抓取；
单个画师本轮下载数受 max_per_artist 限制。
"""
import logging
import threading
import time
from datetime import datetime, timedelta

from croniter import croniter

import config
from pixiv import (
    get_user_illusts, is_authenticated, sync_following,
    should_exclude, matches_include, meets_bookmark_requirement,
    download_illust, reauthenticate,
)

logger = logging.getLogger(__name__)

_EMPTY_STATS = {
    "artists": 0, "artists_empty": 0, "scanned": 0,
    "skipped_exclude": 0, "skipped_include": 0, "skipped_bookmark": 0,
    "skipped_existing": 0, "skipped_limit": 0, "skipped_ignored": 0,
    "downloaded": 0, "failed": 0, "retry_later": 0,
}


def run_once(api, db, rules: dict, download_api=None,
             should_stop=None, progress=None) -> dict:
    """执行一轮抓取与下载，返回统计。

    api 负责读取关注列表；download_api 负责抓取作品与下载，未配置时复用 api。
    """
    download_api = download_api or api

    def report(**fields):
        if progress:
            try:
                progress(**fields)
            except Exception:
                pass

    logger.info("=" * 60)
    logger.info("同步当前账号的关注列表...")
    try:
        sync_following(api, db)
    except Exception as e:
        logger.error(f"同步关注列表失败，本轮仍按现有抓取计划执行: {e}")

    plan = db.plan_artists()
    if not plan:
        logger.warning("抓取计划为空，请在「画师」页把需要下载的画师加入计划")
        return dict(_EMPTY_STATS)

    stats = dict(_EMPTY_STATS)
    stats["artists"] = len(plan)
    illust_type = rules.get("illust_type", "illust")
    max_per_artist = int(rules.get("max_per_artist", 0) or 0)
    ignored_ids = db.ignored_artist_ids() if db else set()
    report(index=0, total=len(plan))

    for idx, artist in enumerate(plan, 1):
        if should_stop and should_stop():
            logger.info("已暂停，本轮提前结束")
            break

        # 定期冷却，规避静默限流
        cooldown_every = config.ARTIST_COOLDOWN_EVERY
        if cooldown_every > 0 and idx > 1 and idx % cooldown_every == 1:
            seconds = config.ARTIST_COOLDOWN_SECONDS
            logger.info(f"已经历 {idx - 1} 位画师，强制冷却 {seconds}s...")
            if not _interruptible_sleep(seconds, should_stop):
                logger.info("冷却期间被暂停，本轮提前结束")
                break

        artist_id = artist["artist_id"]
        artist_name = artist["artist_name"]
        report(index=idx, total=len(plan), artist=artist_name, work=None)
        if artist_id in ignored_ids:
            stats["skipped_ignored"] += 1
            logger.info(f"  ↷ 已暂停同步，跳过画师: {artist_name}")
            continue
        watermark = db.artist_watermark(artist_id)
        logger.info(
            f"[{idx}/{len(plan)}] 处理画师: {artist_name} "
            f"(ID: {artist_id}, 水位: {watermark or '无'})"
        )

        try:
            illusts, complete = get_user_illusts(
                download_api, artist_id, illust_type=illust_type,
                stop_at_id=watermark,
            )
        except Exception as e:
            logger.error(f"获取画师 {artist_name} 作品失败: {e}", exc_info=True)
            continue

        if not illusts:
            stats["artists_empty"] += 1
            logger.info(f"  → 画师 {artist_name} 无新作品")
            continue

        logger.info(f"  → 画师 {artist_name} 本次拉取 {len(illusts)} 个作品")

        downloaded_here = 0
        failed_here = 0
        limit_hit = False
        for illust in illusts:
            if should_stop and should_stop():
                logger.info("已暂停，本轮提前结束")
                return stats

            stats["scanned"] += 1
            report(
                index=idx, total=len(plan), artist=artist_name,
                work=illust.get("title") or str(illust["id"]),
            )

            if should_exclude(illust, rules.get("exclude_tags")):
                stats["skipped_exclude"] += 1
                logger.info(f"  ⨯ [排除 tag] {illust.get('title')} (ID={illust['id']})")
                continue

            if not matches_include(illust, rules.get("include_tags")):
                stats["skipped_include"] += 1
                logger.info(f"  ⨯ [不含匹配 tag] {illust.get('title')} (ID={illust['id']})")
                continue

            if not meets_bookmark_requirement(illust, rules.get("min_bookmark_count", 0)):
                stats["skipped_bookmark"] += 1
                logger.info(
                    f"  ⨯ [收藏数] {illust.get('title')} "
                    f"(收藏={illust.get('total_bookmarks', 0)}, "
                    f"阈值={rules.get('min_bookmark_count', 0)})"
                )
                continue

            if db.is_downloaded(illust["id"]):
                stats["skipped_existing"] += 1
                logger.debug(f"  ✓ [已存在] {illust.get('title')} (ID={illust['id']})")
                continue

            if max_per_artist and downloaded_here >= max_per_artist:
                stats["skipped_limit"] += 1
                limit_hit = True
                continue

            if download_illust(download_api, illust, db, rules):
                downloaded_here += 1
                stats["downloaded"] += 1
            else:
                failed_here += 1
                stats["failed"] += 1

        # 水位策略：
        # - 本轮有失败：不推进，下轮从原水位重扫，失败作品会被重试；
        # - 因单画师上限跳过：不推进，否则被跳过的旧作品永远不会再被看到；
        # - 只翻到页数上限：不推进，避免漏掉更早的作品。
        if not complete:
            logger.info("  ↷ 未扫描完（页数上限），本轮不推进水位")
        elif failed_here:
            logger.info(f"  ↷ 有 {failed_here} 个作品失败，本轮不推进水位，下轮重试")
            stats["retry_later"] += failed_here
        elif limit_hit:
            logger.info(f"  ↷ 已达单画师上限 {max_per_artist}，本轮不推进水位")
        elif downloaded_here or any(i["id"] > watermark for i in illusts):
            new_watermark = max(i["id"] for i in illusts)
            if new_watermark > watermark:
                db.set_artist_watermark(artist_id, artist_name, new_watermark)
                logger.info(f"  ↻ 水位更新为 {new_watermark}")

        logger.info(f"  → 画师 {artist_name} 本轮下载 {downloaded_here} 个")

    logger.info(
        f"本轮完成 | 画师 {stats['artists']} (无新作 {stats['artists_empty']}) | "
        f"扫描 {stats['scanned']} | 排除 tag {stats['skipped_exclude']} | "
        f"不匹配 tag {stats['skipped_include']} | 收藏数不足 {stats['skipped_bookmark']} | "
        f"已存在 {stats['skipped_existing']} | 超出上限 {stats['skipped_limit']} | "
        f"已暂停 {stats['skipped_ignored']} | "
        f"成功下载 {stats['downloaded']} | 失败 {stats['failed']}"
    )
    return stats


def _interruptible_sleep(seconds: float, should_stop=None) -> bool:
    """可被暂停打断的睡眠。"""
    end = time.time() + seconds
    while time.time() < end:
        if should_stop and should_stop():
            return False
        time.sleep(min(0.5, max(0, end - time.time())))
    return True


class Scheduler(threading.Thread):
    def __init__(self, api, download_api, db, rules):
        super().__init__(daemon=True, name="scheduler")
        self.api = api
        self.download_api = download_api or api
        self.db = db
        self.rules = rules

        self._wake = threading.Event()
        self._schedule_dirty = threading.Event()
        self._paused = False
        self._running = False
        self._rerun = False

        self._last_run = None
        self._last_stats = None
        self._next_run = None
        self._progress = {}
        self._progress_lock = threading.Lock()

    # ---------- 状态 ----------
    def _set_progress(self, **fields):
        with self._progress_lock:
            self._progress.update(fields)

    def _clear_progress(self):
        with self._progress_lock:
            self._progress = {}

    def progress(self) -> dict:
        with self._progress_lock:
            return dict(self._progress)

    def status(self) -> dict:
        return {
            "running": self._running,
            "paused": self._paused,
            "mode": self._current_mode(),
            "next_run": self._fmt(self._next_run),
            "last_run": self._fmt(self._last_run),
            "last_stats": self._last_stats,
            "pixiv_user_id": getattr(self.api, "user_id", None),
            "progress": self.progress(),
        }

    def _current_mode(self) -> str:
        snap = self.rules.snapshot()
        if snap.get("run_cron"):
            return f"cron 定时（{snap['run_cron']}）"
        interval = int(snap.get("poll_interval") or 0)
        if interval > 0:
            return f"固定间隔（每 {interval}s）"
        return "手动触发（单次模式）"

    @staticmethod
    def _fmt(dt):
        return dt.strftime("%Y-%m-%d %H:%M:%S") if isinstance(dt, datetime) else None

    # ---------- 控制 ----------
    def trigger_now(self) -> str:
        """手动触发；返回提示信息。"""
        if self._paused:
            return "当前已暂停，请先恢复调度"
        if self._running:
            self._rerun = True
            return "当前正在运行，本轮结束后将自动再跑一轮"
        self._wake.set()
        return "已触发立即运行"

    def notify_schedule_changed(self):
        """规则页改了调度方式：让主循环重新计算下次运行时间。"""
        self._schedule_dirty.set()

    def set_paused(self, paused: bool):
        self._paused = paused
        if paused:
            logger.info("调度已暂停（WebUI）")
        else:
            logger.info("调度已恢复（WebUI）")
            self._wake.set()

    # ---------- 主循环 ----------
    def run(self):
        config.reload()
        self._initial_run()
        while True:
            try:
                config.reload()

                if self._rerun:
                    self._rerun = False
                    self._run_once()
                    continue

                # 暂停状态：等待恢复，期间不计算下次运行时间
                while self._paused:
                    self._next_run = None
                    time.sleep(1)
                self._wake.clear()

                snap = self.rules.snapshot()
                if snap.get("run_cron"):
                    now = datetime.now()
                    next_run = croniter(snap["run_cron"], now).get_next(datetime)
                    self._next_run = next_run
                    wait = max(1, (next_run - now).total_seconds())
                elif int(snap.get("poll_interval") or 0) > 0:
                    wait = int(snap["poll_interval"])
                    self._next_run = datetime.now() + timedelta(seconds=wait)
                else:
                    # 手动触发模式：阻塞等待
                    self._next_run = None
                    if self._wait(None) == "run":
                        self._run_once()
                    continue

                if self._wait(wait) in ("run", "timeout"):
                    self._run_once()
            except Exception as e:
                logger.exception(f"调度循环异常: {e}")
                time.sleep(30)

    def _wait(self, seconds):
        """等待到点 / 手动触发 / 规则变更 / 暂停；返回原因字符串。"""
        deadline = None if seconds is None else time.time() + seconds
        while True:
            if self._paused:
                return "pause"
            if self._wake.is_set():
                self._wake.clear()
                return "run"
            if self._schedule_dirty.is_set():
                self._schedule_dirty.clear()
                return "reload"
            now = time.time()
            if deadline is not None and now >= deadline:
                return "timeout"
            remaining = 0.5 if deadline is None else min(0.5, deadline - now)
            time.sleep(max(0.05, remaining))

    def _initial_run(self):
        snap = self.rules.snapshot()
        has_schedule = bool(snap.get("run_cron")) or int(snap.get("poll_interval") or 0) > 0
        if snap.get("run_cron_immediate") and has_schedule:
            logger.info("启动时立即执行一次")
            self._run_once()
        elif not has_schedule:
            logger.info("单次执行模式：启动即运行一次")
            self._run_once()

    def _run_once(self):
        self._running = True
        self._wake.clear()
        try:
            config.reload()
            if not _ensure_authenticated(self.api):
                logger.error(
                    "未通过 Pixiv 认证，本轮跳过。请在配置页确认 refresh_token，"
                    "或点击仪表盘「重新认证」按钮"
                )
                self._last_stats = None
                return
            rules = self.rules.snapshot(refresh=True)
            if not _ensure_authenticated(self.download_api):
                logger.error("下载账号未通过认证，本轮跳过")
                self._last_stats = None
                return
            self._last_stats = run_once(
                self.api, self.db, rules,
                download_api=self.download_api,
                should_stop=lambda: self._paused,
                progress=self._set_progress,
            )
        except Exception as e:
            logger.exception(f"运行异常: {e}")
            self._last_stats = None
        finally:
            self._running = False
            self._last_run = datetime.now()
            self._clear_progress()


def _ensure_authenticated(api) -> bool:
    """无 access_token 时尝试重新认证一次。"""
    if is_authenticated(api):
        return True
    logger.warning("API 客户端未认证，尝试自动重新认证...")
    try:
        reauthenticate(api)
        return is_authenticated(api)
    except Exception as e:
        logger.error(f"自动重新认证失败: {e}")
        return False
