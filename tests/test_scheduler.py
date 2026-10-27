import os
import sys
import tempfile
import types
import unittest
from unittest import mock

_dotenv = types.ModuleType("dotenv")
_dotenv.load_dotenv = lambda *args, **kwargs: None
sys.modules.setdefault("dotenv", _dotenv)

import scheduler  # noqa: E402
from database import DownloadDB  # noqa: E402
from rules import Rules  # noqa: E402

FOLLOWING = [{"user_id": 55, "user_name": "Artist", "avatar_url": None}]


def make_rules(**overrides):
    rules = {
        "download_kind": "original",
        "illust_type": "illust",
        "first_page_only": False,
        "min_bookmark_count": 0,
        "max_per_artist": 0,
        "include_tags": [],
        "exclude_tags": [],
    }
    rules.update(overrides)
    return rules


def illust(illust_id, tags=(), bookmarks=0, title=None):
    return {
        "id": illust_id,
        "title": title or f"work-{illust_id}",
        "total_bookmarks": bookmarks,
        "tags": [{"name": t} for t in tags],
        "user": {"id": 55, "name": "Artist"},
        "page_count": 1,
    }


class SchedulerRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DownloadDB(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def run_once(self, illusts, rules=None, complete=True, ok=True):
        with mock.patch.object(scheduler, "get_following_users", return_value=FOLLOWING), \
             mock.patch.object(scheduler, "get_user_illusts",
                               return_value=(illusts, complete)), \
             mock.patch.object(scheduler, "download_illust", return_value=ok):
            return scheduler.run_once(
                object(), self.db, "123", rules or make_rules()
            )

    def test_incremental_only_processes_new_works(self):
        self.db.set_artist_watermark(55, "Artist", 100)
        self.run_once([illust(102), illust(101), illust(100), illust(99)])
        self.assertEqual(self.db.artist_watermark(55), 102)
        stats = self.run_once([illust(103)])
        self.assertEqual(stats["scanned"], 1)

    def test_max_per_artist_limits_and_keeps_watermark(self):
        rules = make_rules(max_per_artist=2)
        stats = self.run_once([illust(10), illust(9), illust(8)], rules=rules)
        self.assertEqual(stats["downloaded"], 2)
        self.assertEqual(stats["skipped_limit"], 1)
        # 达到上限时水位不推进，保证被跳过的旧作品下一轮还能看到
        self.assertEqual(self.db.artist_watermark(55), 0)

    def test_failure_blocks_watermark(self):
        stats = self.run_once([illust(10), illust(9)], ok=False)
        self.assertEqual(stats["failed"], 2)
        self.assertEqual(stats["retry_later"], 2)
        self.assertEqual(self.db.artist_watermark(55), 0)

    def test_incomplete_scan_blocks_watermark(self):
        self.run_once([illust(10), illust(9)], complete=False)
        self.assertEqual(self.db.artist_watermark(55), 0)

    def test_watermark_advances_when_scan_complete(self):
        self.run_once([illust(10), illust(9)], complete=True)
        self.assertEqual(self.db.artist_watermark(55), 10)

    def test_include_and_exclude_filters(self):
        rules = make_rules(include_tags=["风景"], exclude_tags=["r-18"])
        stats = self.run_once([
            illust(10, tags=("风景",)),
            illust(9, tags=("人物",)),
            illust(8, tags=("风景", "R-18")),
        ], rules=rules)
        self.assertEqual(stats["downloaded"], 1)
        self.assertEqual(stats["skipped_include"], 1)
        self.assertEqual(stats["skipped_exclude"], 1)

    def test_bookmark_filter(self):
        stats = self.run_once(
            [illust(10, bookmarks=5), illust(9, bookmarks=50)],
            rules=make_rules(min_bookmark_count=10),
        )
        self.assertEqual(stats["downloaded"], 1)
        self.assertEqual(stats["skipped_bookmark"], 1)

    def test_existing_works_skipped(self):
        self.db.mark_downloaded(10, "x", 55, "Artist", [], 1)
        stats = self.run_once([illust(10), illust(9)])
        self.assertEqual(stats["skipped_existing"], 1)
        self.assertEqual(stats["downloaded"], 1)

    def test_following_cache_updated(self):
        self.run_once([illust(10)])
        self.assertEqual([a["artist_id"] for a in self.db.following()], [55])

    def test_pause_stops_early(self):
        with mock.patch.object(scheduler, "get_following_users", return_value=FOLLOWING):
            stats = scheduler.run_once(
                object(), self.db, "123", make_rules(),
                should_stop=lambda: True,
            )
        self.assertEqual(stats["downloaded"], 0)

    def test_missing_target(self):
        self.assertIsNone(scheduler.run_once(object(), self.db, "", make_rules()))


class SchedulerRulesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DownloadDB(os.path.join(self.tmp.name, "t.db"))
        self.rules = Rules(self.db)
        self.sched = scheduler.Scheduler(None, self.db, self.rules)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_current_mode(self):
        self.assertIn("手动", self.sched._current_mode())

        self.rules.update({"run_cron": "0 3 * * *"})
        self.assertIn("cron 定时", self.sched._current_mode())
        self.assertIn("0 3 * * *", self.sched._current_mode())

        self.rules.update({"run_cron": "", "poll_interval": "3600"})
        self.assertIn("固定间隔", self.sched._current_mode())

        self.rules.update({"poll_interval": "0"})
        self.assertIn("手动", self.sched._current_mode())

    def test_initial_run(self):
        with mock.patch.object(self.sched, "_run_once") as run:
            self.rules.update({"run_cron": "0 3 * * *", "run_cron_immediate": "false"})
            self.sched._initial_run()
            run.assert_not_called()

            self.rules.update({"run_cron_immediate": "true"})
            self.sched._initial_run()
            self.assertEqual(run.call_count, 1)

        # 没有调度时启动先跑一次
        self.rules.update({"run_cron": "", "poll_interval": "0"})
        with mock.patch.object(self.sched, "_run_once") as run:
            self.sched._initial_run()
            run.assert_called_once()

    def test_schedule_change_wakes_waiter(self):
        self.sched.notify_schedule_changed()
        self.assertEqual(self.sched._wait(30), "reload")


if __name__ == "__main__":
    unittest.main()
