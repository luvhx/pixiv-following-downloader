"""SQLite 存储层。

设计要点：
- 每个线程独立连接（WAL + busy_timeout），写操作用 BEGIN IMMEDIATE 串行化，
  避免全库一把大锁把 WebUI 读请求也堵住。
- tag 使用关联表 illust_tags 存储，不再用逗号拼接，含逗号的 tag 也不会误判。
- 下载水位 artist_state.last_illust_id 支持增量抓取。
- 规则（download rules）存放在 settings / tag_rules 表，与 .env 解耦。
"""
import json
import logging
import math
import os
import random
import sqlite3
import threading
from contextlib import contextmanager

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 2


def _escape_like(value: str) -> str:
    """转义 LIKE 通配符，避免用户输入的 % / _ 被当作通配符。"""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def normalize_tag(tag: str) -> str:
    return str(tag or "").strip().lower()


class DownloadDB:
    def __init__(self, db_path: str):
        if not db_path:
            raise ValueError("DB_PATH 不能为空")
        self.path = db_path
        if os.path.dirname(db_path):
            os.makedirs(os.path.dirname(db_path), exist_ok=True)

        self._local = threading.local()
        self._conns = []
        self._conns_lock = threading.Lock()
        self._meta_cache = {}
        self._cache_lock = threading.Lock()
        self._init_lock = threading.Lock()
        self._initialized = False

        conn = self._conn()
        self._migrate(conn)
        self._initialized = True

    # ---------- 连接管理 ----------
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None  # 显式事务控制
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA journal_size_limit=67108864")
            conn.execute("PRAGMA wal_autocheckpoint=1000")
        except sqlite3.Error as e:
            logger.warning(f"无法启用 WAL 模式，使用默认日志模式: {e}")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        self._local.conn = conn
        with self._conns_lock:
            self._conns.append(conn)
        return conn

    @contextmanager
    def _write(self):
        """写事务：BEGIN IMMEDIATE 立即拿写锁，避免读事务升级死锁。"""
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def _invalidate_meta(self):
        with self._cache_lock:
            self._meta_cache.clear()

    @contextmanager
    def _meta(self):
        """短临界区：只保护进程内缓存，不保护数据库 IO。"""
        with self._cache_lock:
            yield self._meta_cache

    # ---------- 建表 / 迁移 ----------
    def _migrate(self, conn: sqlite3.Connection):
        with self._init_lock:
            if self._initialized:
                return
            conn.execute("PRAGMA foreign_keys=OFF")
            cols = [r[1] for r in conn.execute("PRAGMA table_info(downloaded)")]
            if cols and "tags" in cols:
                logger.info("数据库迁移：tags 拆分为 illust_tags 关联表")
                self._rebuild_downloaded(conn)
            self._create_tables(conn)
            self._migrate_favorite_tags(conn)
            self._migrate_following(conn)
            self._migrate_downloaded_columns(conn)
            conn.execute("PRAGMA foreign_keys=ON")
            self._invalidate_meta()

    @staticmethod
    def _create_tables(conn: sqlite3.Connection):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS downloaded (
                illust_id   INTEGER PRIMARY KEY,
                title       TEXT NOT NULL DEFAULT '',
                author_id   INTEGER,
                author_name TEXT NOT NULL DEFAULT '',
                page_count  INTEGER DEFAULT 1,
                thumb_url   TEXT,
                illust_type TEXT,
                bookmarks   INTEGER DEFAULT 0,
                favorite    INTEGER DEFAULT 0,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                files       TEXT NOT NULL DEFAULT '[]'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS illust_tags (
                illust_id INTEGER NOT NULL REFERENCES downloaded(illust_id) ON DELETE CASCADE,
                tag       TEXT NOT NULL,
                norm      TEXT NOT NULL,
                PRIMARY KEY (illust_id, norm)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tag_rules (
                tag  TEXT PRIMARY KEY,
                norm TEXT NOT NULL UNIQUE,
                mode TEXT NOT NULL CHECK (mode IN ('include', 'exclude'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS favorite_tags (
                tag        TEXT PRIMARY KEY,
                norm       TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS artist_state (
                artist_id      INTEGER PRIMARY KEY,
                artist_name    TEXT,
                last_illust_id INTEGER NOT NULL DEFAULT 0,
                last_run_at    TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS following (
                artist_id   INTEGER PRIMARY KEY,
                artist_name TEXT NOT NULL DEFAULT '',
                avatar_url  TEXT,
                previews    TEXT NOT NULL DEFAULT '[]',
                ignored     INTEGER NOT NULL DEFAULT 0,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tags_norm ON illust_tags(norm)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_downloaded_author ON downloaded(author_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_downloaded_created "
            "ON downloaded(created_at DESC, illust_id DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_downloaded_favorite ON downloaded(favorite)"
        )

    def _rebuild_downloaded(self, conn: sqlite3.Connection):
        old_rows = conn.execute(
            "SELECT illust_id, title, author_id, author_name, tags, page_count, "
            "created_at, files, favorite FROM downloaded"
        ).fetchall()
        conn.execute("ALTER TABLE downloaded RENAME TO downloaded_old")
        self._create_tables(conn)
        for row in old_rows:
            files = row["files"] if row["files"] else "[]"
            try:
                json.loads(files)
            except (TypeError, ValueError):
                files = "[]"
            conn.execute(
                """INSERT OR IGNORE INTO downloaded
                   (illust_id, title, author_id, author_name, page_count,
                    created_at, files, favorite)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["illust_id"], row["title"] or "", row["author_id"],
                    row["author_name"] or "", row["page_count"] or 1,
                    row["created_at"], files, row["favorite"] or 0,
                ),
            )
            for tag in str(row["tags"] or "").split(","):
                tag = tag.strip()
                if tag:
                    conn.execute(
                        "INSERT OR IGNORE INTO illust_tags (illust_id, tag, norm) "
                        "VALUES (?, ?, ?)",
                        (row["illust_id"], tag, normalize_tag(tag)),
                    )
        conn.execute("DROP TABLE downloaded_old")

    @staticmethod
    def _migrate_favorite_tags(conn: sqlite3.Connection):
        cols = [r[1] for r in conn.execute("PRAGMA table_info(favorite_tags)")]
        if cols and "norm" not in cols:
            logger.info("数据库迁移：favorite_tags 增加 norm 列")
            conn.execute("ALTER TABLE favorite_tags ADD COLUMN norm TEXT NOT NULL DEFAULT ''")
            conn.execute("UPDATE favorite_tags SET norm = LOWER(TRIM(tag))")
        # 旧数据可能只按大小写不同的同名 tag 重复，先按 norm 去重
        conn.execute(
            "DELETE FROM favorite_tags WHERE rowid NOT IN ("
            "SELECT MAX(rowid) FROM favorite_tags GROUP BY norm)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_favorite_tags_norm "
            "ON favorite_tags(norm)"
        )

    @staticmethod
    def _migrate_downloaded_columns(conn: sqlite3.Connection):
        cols = [r[1] for r in conn.execute("PRAGMA table_info(downloaded)")]
        if cols and "bookmarks" not in cols:
            logger.info("数据库迁移：downloaded 增加 bookmarks 列")
            conn.execute(
                "ALTER TABLE downloaded ADD COLUMN bookmarks INTEGER DEFAULT 0"
            )

    @staticmethod
    def _migrate_following(conn: sqlite3.Connection):
        cols = [r[1] for r in conn.execute("PRAGMA table_info(following)")]
        if cols and "previews" not in cols:
            logger.info("数据库迁移：following 增加 previews 列")
            conn.execute(
                "ALTER TABLE following ADD COLUMN previews TEXT NOT NULL DEFAULT '[]'"
            )
        if cols and "ignored" not in cols:
            logger.info("数据库迁移：following 增加 ignored 列")
            conn.execute(
                "ALTER TABLE following ADD COLUMN ignored INTEGER NOT NULL DEFAULT 0"
            )

    # ---------- 已下载 ----------
    def is_downloaded(self, illust_id: int) -> bool:
        row = self._conn().execute(
            "SELECT 1 FROM downloaded WHERE illust_id = ?", (illust_id,)
        ).fetchone()
        return row is not None

    def downloaded_ids(self, illust_ids) -> set:
        ids = [int(i) for i in illust_ids]
        if not ids:
            return set()
        marks = ",".join("?" * len(ids))
        rows = self._conn().execute(
            f"SELECT illust_id FROM downloaded WHERE illust_id IN ({marks})", ids
        ).fetchall()
        return {r["illust_id"] for r in rows}

    def mark_downloaded(self, illust_id, title, author_id, author_name, tags,
                        page_count, files=None, thumb_url=None, illust_type=None,
                        bookmarks=0):
        files_json = json.dumps(files or [], ensure_ascii=False)
        tag_list = []
        for tag in tags or []:
            tag = str(tag).strip()
            norm = normalize_tag(tag)
            if norm and norm not in {t[1] for t in tag_list}:
                tag_list.append((tag[:100], norm))
        with self._write() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO downloaded
                   (illust_id, title, author_id, author_name, page_count,
                    files, thumb_url, illust_type, bookmarks)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (illust_id, title, author_id, author_name, page_count,
                 files_json, thumb_url, illust_type, int(bookmarks or 0)),
            )
            # 旧记录补齐文件列表 / 缩略图
            conn.execute(
                """UPDATE downloaded
                   SET files = CASE WHEN files IS NULL OR files = '[]' THEN ? ELSE files END,
                       thumb_url = COALESCE(thumb_url, ?),
                       illust_type = COALESCE(illust_type, ?),
                       bookmarks = CASE WHEN IFNULL(bookmarks, 0) = 0 THEN ? ELSE bookmarks END
                   WHERE illust_id = ?""",
                (files_json, thumb_url, illust_type, int(bookmarks or 0), illust_id),
            )
            for tag, norm in tag_list:
                conn.execute(
                    "INSERT OR IGNORE INTO illust_tags (illust_id, tag, norm) "
                    "VALUES (?, ?, ?)",
                    (illust_id, tag, norm),
                )
        self._invalidate_meta()

    def get(self, illust_id: int):
        row = self._conn().execute(
            "SELECT * FROM downloaded WHERE illust_id = ?", (illust_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_many(self, illust_ids):
        ids = [int(i) for i in illust_ids]
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = self._conn().execute(
            f"SELECT * FROM downloaded WHERE illust_id IN ({marks})", ids
        ).fetchall()
        return {r["illust_id"]: dict(r) for r in rows}

    def tags_for(self, illust_id: int) -> list:
        rows = self._conn().execute(
            "SELECT tag FROM illust_tags WHERE illust_id = ? ORDER BY tag COLLATE NOCASE",
            (illust_id,),
        ).fetchall()
        return [r["tag"] for r in rows]

    def tags_for_many(self, illust_ids) -> dict:
        ids = [int(i) for i in illust_ids]
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = self._conn().execute(
            f"SELECT illust_id, tag FROM illust_tags WHERE illust_id IN ({marks}) "
            "ORDER BY tag COLLATE NOCASE",
            ids,
        ).fetchall()
        result = {}
        for r in rows:
            result.setdefault(r["illust_id"], []).append(r["tag"])
        return result

    def set_files(self, illust_id: int, files: list):
        with self._write() as conn:
            conn.execute(
                "UPDATE downloaded SET files = ? WHERE illust_id = ?",
                (json.dumps(files, ensure_ascii=False), illust_id),
            )

    def set_favorite(self, illust_id: int, favorite: bool) -> bool:
        with self._write() as conn:
            cur = conn.execute(
                "UPDATE downloaded SET favorite = ? WHERE illust_id = ?",
                (1 if favorite else 0, illust_id),
            )
            changed = cur.rowcount > 0
        return changed

    def delete(self, illust_id: int) -> bool:
        with self._write() as conn:
            cur = conn.execute("DELETE FROM downloaded WHERE illust_id = ?", (illust_id,))
            deleted = cur.rowcount > 0
        if deleted:
            self._invalidate_meta()
        return deleted

    def delete_many(self, illust_ids) -> int:
        ids = [int(i) for i in illust_ids]
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        with self._write() as conn:
            cur = conn.execute(
                f"DELETE FROM downloaded WHERE illust_id IN ({marks})", ids
            )
            count = cur.rowcount
        if count:
            self._invalidate_meta()
        return count

    # ---------- 查询 / 统计 ----------
    def stats(self) -> dict:
        with self._meta() as cache:
            cached = cache.get("stats")
            if cached is not None:
                return dict(cached)
        row = self._conn().execute(
            "SELECT COUNT(*) AS c, COUNT(DISTINCT author_id) AS a FROM downloaded"
        ).fetchone()
        result = {"downloaded": row["c"], "artists": row["a"]}
        with self._meta() as cache:
            cache["stats"] = result
        return dict(result)

    def _tag_filter(self, where: list, args: list, tag: str):
        where.append(
            "EXISTS (SELECT 1 FROM illust_tags t "
            "WHERE t.illust_id = downloaded.illust_id AND t.norm = ?)"
        )
        args.append(normalize_tag(tag))

    def _search_filter(self, where: list, args: list, q: str):
        like = f"%{_escape_like(q)}%"
        where.append(
            "(title LIKE ? ESCAPE '\\' OR author_name LIKE ? ESCAPE '\\' "
            "OR EXISTS (SELECT 1 FROM illust_tags t WHERE t.illust_id = downloaded.illust_id "
            "AND t.norm LIKE ? ESCAPE '\\'))"
        )
        args.extend([like, like, f"%{_escape_like(normalize_tag(q))}%"])

    _DOWNLOAD_ORDER = {
        "new": "created_at DESC, illust_id DESC",
        "old": "created_at ASC, illust_id ASC",
        "bookmarks": "bookmarks DESC, created_at DESC, illust_id DESC",
        "pages": "page_count DESC, created_at DESC, illust_id DESC",
        "title": "title COLLATE NOCASE ASC, illust_id DESC",
    }

    def list_downloaded(self, q=None, tag=None, artist_id=None,
                        favorites_only=False, offset=0, limit=24,
                        sort="new") -> dict:
        where, args = [], []
        if q:
            self._search_filter(where, args, q)
        if tag:
            self._tag_filter(where, args, tag)
        if artist_id:
            where.append("author_id = ?")
            args.append(artist_id)
        if favorites_only:
            where.append("favorite = 1")
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        conn = self._conn()
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM downloaded {where_sql}", args
        ).fetchone()["c"]
        order = self._DOWNLOAD_ORDER.get(sort, self._DOWNLOAD_ORDER["new"])
        rows = conn.execute(
            f"""SELECT illust_id, title, author_id, author_name, page_count,
                       created_at, files, favorite, thumb_url, illust_type,
                       bookmarks
                FROM downloaded {where_sql}
                ORDER BY {order}
                LIMIT ? OFFSET ?""",
            args + [limit, offset],
        ).fetchall()
        return {"total": total, "rows": [dict(r) for r in rows]}

    def all_files(self) -> list:
        rows = self._conn().execute("SELECT illust_id, files FROM downloaded").fetchall()
        result = []
        for r in rows:
            try:
                items = json.loads(r["files"] or "[]")
            except (TypeError, ValueError):
                items = []
            result.append((r["illust_id"], [f for f in items if isinstance(f, str)]))
        return result

    @staticmethod
    def _tags_map(conn, illust_ids) -> dict:
        """一次查出多作品的 tag 集合，避免逐条查询。"""
        result = {}
        ids = list({int(i) for i in illust_ids})
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"SELECT illust_id, tag FROM illust_tags "
                f"WHERE illust_id IN ({marks})",
                chunk,
            ).fetchall():
                result.setdefault(row["illust_id"], set()).add(normalize_tag(row["tag"]))
        return result

    def related(self, illust_id: int, author_id=None, tags=None, limit: int = 16) -> list:
        """优先匹配更小众的 tag，其次叠加同作者加分，随机打散同分作品。"""
        tags = list(dict.fromkeys(
            normalize_tag(t) for t in (tags or []) if t and t.strip()
        ))
        if not tags and not author_id:
            return []

        conn = self._conn()
        frequencies = {}
        if tags:
            marks = ",".join("?" * len(tags))
            for row in conn.execute(
                f"SELECT norm, COUNT(*) AS c FROM illust_tags "
                f"WHERE norm IN ({marks}) GROUP BY norm",
                tags,
            ).fetchall():
                frequencies[row["norm"]] = row["c"]

        raw_weights = {t: 1.0 / math.sqrt(max(1, frequencies.get(t, 1))) for t in tags}
        max_weight = max(raw_weights.values()) if raw_weights else 1.0
        weights = {t: 6.0 * raw_weights[t] / max_weight for t in tags}
        ordered_tags = sorted(tags, key=lambda t: frequencies.get(t, 1))
        top_tags = set(ordered_tags[:2])

        candidates = {}
        select_sql = """SELECT illust_id, title, author_id, author_name, page_count,
                               created_at, files, favorite, thumb_url, illust_type
                        FROM downloaded"""
        if author_id:
            for row in conn.execute(
                f"{select_sql} WHERE illust_id != ? AND author_id = ? "
                "ORDER BY created_at DESC LIMIT 400",
                (illust_id, author_id),
            ).fetchall():
                candidates[row["illust_id"]] = row

        # 先找最稀有的 tag，确保旧作品也有机会进入候选集
        for tag in ordered_tags:
            for row in conn.execute(
                f"{select_sql} WHERE illust_id != ? AND illust_id IN ("
                "SELECT illust_id FROM illust_tags WHERE norm = ?) "
                "ORDER BY created_at DESC LIMIT 250",
                (illust_id, tag),
            ).fetchall():
                candidates.setdefault(row["illust_id"], row)
            if len(candidates) >= 1200:
                break

        tags_map = self._tags_map(conn, candidates.keys()) if candidates else {}
        scored = []
        for row in candidates.values():
            row_tags = tags_map.get(row["illust_id"], set())
            score = 3.5 if author_id and row["author_id"] == author_id else 0.0
            for tag in row_tags:
                if tag in weights:
                    score += weights[tag]
                    if tag in top_tags:
                        score += 1.5
            scored.append((score, random.random(), dict(row)))

        scored.sort(key=lambda item: (-item[0], item[1]))
        return [row for _, _, row in scored[:limit]]

    def artist_options(self) -> list:
        with self._meta() as cache:
            cached = cache.get("artists")
            if cached is not None:
                return list(cached)
        rows = self._conn().execute(
            "SELECT author_id, author_name, COUNT(*) AS c FROM downloaded "
            "GROUP BY author_id ORDER BY author_name COLLATE NOCASE"
        ).fetchall()
        result = [
            {"id": r["author_id"], "name": r["author_name"] or "", "count": r["c"]}
            for r in rows
        ]
        with self._meta() as cache:
            cache["artists"] = result
        return list(result)

    def tag_options(self, limit: int = 3000) -> list:
        key = ("tags", limit)
        with self._meta() as cache:
            cached = cache.get(key)
            if cached is not None:
                return list(cached)
        rows = self._conn().execute(
            "SELECT tag, COUNT(*) AS c FROM illust_tags GROUP BY norm "
            "ORDER BY tag COLLATE NOCASE LIMIT ?",
            (limit,),
        ).fetchall()
        tags = [r["tag"] for r in rows]
        with self._meta() as cache:
            cache[key] = tags
        return list(tags)

    def artist_tag_stats(self, artist_id: int, limit: int = 12) -> list:
        rows = self._conn().execute(
            "SELECT t.norm, t.tag, COUNT(*) AS c FROM illust_tags t "
            "JOIN downloaded d ON d.illust_id = t.illust_id "
            "WHERE d.author_id = ? GROUP BY t.norm "
            "ORDER BY c DESC, t.tag COLLATE NOCASE LIMIT ?",
            (artist_id, limit),
        ).fetchall()
        return [{"tag": r["tag"], "count": r["c"]} for r in rows]

    # ---------- 收藏 tag ----------
    def set_tag_favorite(self, tag: str, favorite: bool = True) -> bool:
        tag = (tag or "").strip()[:100]
        if not tag:
            return False
        with self._write() as conn:
            if favorite:
                conn.execute(
                    "INSERT OR IGNORE INTO favorite_tags (tag, norm) VALUES (?, ?)",
                    (tag, normalize_tag(tag)),
                )
            else:
                conn.execute(
                    "DELETE FROM favorite_tags WHERE norm = ?", (normalize_tag(tag),)
                )
        return True

    def is_tag_favorite(self, tag: str) -> bool:
        row = self._conn().execute(
            "SELECT 1 FROM favorite_tags WHERE norm = ?", (normalize_tag(tag),)
        ).fetchone()
        return row is not None

    def favorite_tags(self) -> list:
        rows = self._conn().execute(
            "SELECT tag FROM favorite_tags ORDER BY created_at DESC, tag COLLATE NOCASE"
        ).fetchall()
        return [r["tag"] for r in rows]

    def list_by_tag(self, tag: str) -> list:
        rows = self._conn().execute(
            "SELECT d.* FROM downloaded d JOIN illust_tags t ON t.illust_id = d.illust_id "
            "WHERE t.norm = ?",
            (normalize_tag(tag),),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------- tag 规则 ----------
    def tag_rules(self, mode: str = None) -> list:
        sql = "SELECT tag, norm, mode FROM tag_rules"
        args = []
        if mode:
            sql += " WHERE mode = ?"
            args.append(mode)
        sql += " ORDER BY tag COLLATE NOCASE"
        rows = self._conn().execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def set_tag_rule(self, tag: str, mode: str) -> bool:
        tag = (tag or "").strip()[:100]
        norm = normalize_tag(tag)
        if not tag or mode not in ("include", "exclude"):
            return False
        with self._write() as conn:
            conn.execute(
                "INSERT INTO tag_rules (tag, norm, mode) VALUES (?, ?, ?) "
                "ON CONFLICT(norm) DO UPDATE SET tag = excluded.tag, mode = excluded.mode",
                (tag, norm, mode),
            )
        return True

    def remove_tag_rule(self, tag: str) -> bool:
        with self._write() as conn:
            cur = conn.execute(
                "DELETE FROM tag_rules WHERE norm = ?", (normalize_tag(tag),)
            )
            removed = cur.rowcount > 0
        return removed

    # ---------- 设置（下载规则）----------
    def get_settings(self) -> dict:
        rows = self._conn().execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def set_settings(self, values: dict):
        if not values:
            return
        with self._write() as conn:
            for key, value in values.items():
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, str(value)),
                )

    # ---------- 抓取水位 ----------
    def artist_watermark(self, artist_id: int) -> int:
        row = self._conn().execute(
            "SELECT last_illust_id FROM artist_state WHERE artist_id = ?", (artist_id,)
        ).fetchone()
        return int(row["last_illust_id"]) if row else 0

    def watermarks(self) -> dict:
        rows = self._conn().execute(
            "SELECT artist_id, last_illust_id FROM artist_state"
        ).fetchall()
        return {r["artist_id"]: r["last_illust_id"] for r in rows}

    def set_artist_watermark(self, artist_id: int, artist_name: str, last_illust_id: int):
        with self._write() as conn:
            conn.execute(
                """INSERT INTO artist_state (artist_id, artist_name, last_illust_id, last_run_at)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(artist_id) DO UPDATE SET
                       artist_name = excluded.artist_name,
                       last_illust_id = MAX(artist_state.last_illust_id, excluded.last_illust_id),
                       last_run_at = CURRENT_TIMESTAMP""",
                (artist_id, artist_name, int(last_illust_id or 0)),
            )

    def reset_watermarks(self) -> int:
        with self._write() as conn:
            cur = conn.execute("UPDATE artist_state SET last_illust_id = 0")
            return cur.rowcount

    # ---------- 关注列表缓存 ----------
    def replace_following(self, artists: list):
        """全量替换关注列表缓存。"""
        with self._write() as conn:
            seen = []
            for artist in artists:
                artist_id = artist.get("user_id") or artist.get("artist_id")
                if not artist_id:
                    continue
                seen.append(artist_id)
                previews = artist.get("previews") or []
                conn.execute(
                    """INSERT INTO following
                           (artist_id, artist_name, avatar_url, previews, updated_at)
                       VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                       ON CONFLICT(artist_id) DO UPDATE SET
                           artist_name = excluded.artist_name,
                           avatar_url = COALESCE(excluded.avatar_url, following.avatar_url),
                           previews = excluded.previews,
                           updated_at = CURRENT_TIMESTAMP""",
                    (
                        int(artist_id),
                        artist.get("user_name") or artist.get("artist_name") or "",
                        artist.get("avatar_url") or None,
                        json.dumps(previews, ensure_ascii=False),
                    ),
                )
            if seen:
                marks = ",".join("?" * len(seen))
                conn.execute(
                    f"DELETE FROM following WHERE artist_id NOT IN ({marks})", seen
                )

    def following(self) -> list:
        rows = self._conn().execute(
            "SELECT artist_id, artist_name, avatar_url, previews, ignored, updated_at "
            "FROM following ORDER BY artist_name COLLATE NOCASE"
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["previews"] = json.loads(item.get("previews") or "[]")
            except (TypeError, ValueError):
                item["previews"] = []
            result.append(item)
        return result

    def set_artist_ignored(self, artist_id: int, ignored: bool) -> bool:
        with self._write() as conn:
            cur = conn.execute(
                "UPDATE following SET ignored = ? WHERE artist_id = ?",
                (1 if ignored else 0, int(artist_id)),
            )
            return cur.rowcount > 0

    def ignored_artist_ids(self) -> set:
        rows = self._conn().execute(
            "SELECT artist_id FROM following WHERE ignored = 1"
        ).fetchall()
        return {r["artist_id"] for r in rows}

    def following_updated_at(self):
        row = self._conn().execute(
            "SELECT MAX(updated_at) AS ts FROM following"
        ).fetchone()
        return row["ts"] if row else None

    def close(self):
        with self._conns_lock:
            conns, self._conns = self._conns, []
        for conn in conns:
            try:
                conn.execute("PRAGMA optimize")
            except sqlite3.Error:
                pass
            try:
                conn.close()
            except sqlite3.Error:
                pass
