"""SQLite store: entities, daily stat snapshots, the query registry and quota accounting."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .metrics import parse_iso_duration

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY, channel_id TEXT, title TEXT, description TEXT,
    published_at TEXT, duration_s INTEGER, category_id TEXT, tags TEXT, topics TEXT,
    audio_lang TEXT, made_for_kids INTEGER, paid_placement INTEGER, updated_at TEXT, synthetic INTEGER
);
CREATE TABLE IF NOT EXISTS video_stats (
    video_id TEXT, ts TEXT, views INTEGER, likes INTEGER, comments INTEGER,
    PRIMARY KEY (video_id, ts)
);
CREATE TABLE IF NOT EXISTS channels (
    channel_id TEXT PRIMARY KEY, title TEXT, published_at TEXT, uploads_playlist TEXT,
    country TEXT, updated_at TEXT, uploads_fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS channel_stats (
    channel_id TEXT, ts TEXT, subs INTEGER, views INTEGER, video_count INTEGER,
    PRIMARY KEY (channel_id, ts)
);
CREATE TABLE IF NOT EXISTS uploads (
    channel_id TEXT, video_id TEXT, PRIMARY KEY (channel_id, video_id)
);
CREATE TABLE IF NOT EXISTS queries (
    query TEXT PRIMARY KEY, direction TEXT, origin TEXT, parent TEXT, evidence TEXT,
    status TEXT, priority REAL DEFAULT 0, created_at TEXT,
    last_score REAL, low_score_runs INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS query_runs (
    query TEXT, duration TEXT, last_run_at TEXT, last_order TEXT, PRIMARY KEY (query, duration)
);
CREATE TABLE IF NOT EXISTS hits (
    video_id TEXT, query TEXT, source TEXT, first_seen TEXT, last_seen TEXT,
    PRIMARY KEY (video_id, query)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT,
    search_calls INTEGER DEFAULT 0, other_calls INTEGER DEFAULT 0, notes TEXT
);
CREATE TABLE IF NOT EXISTS quota (
    day TEXT, bucket TEXT, calls INTEGER, PRIMARY KEY (day, bucket)
);
CREATE TABLE IF NOT EXISTS related (
    src TEXT, dst TEXT, kind TEXT, seen_at TEXT, PRIMARY KEY (src, dst, kind)
);
CREATE TABLE IF NOT EXISTS expansions (
    channel_id TEXT PRIMARY KEY, expanded_at TEXT
);
CREATE TABLE IF NOT EXISTS imports (
    kind TEXT, value TEXT, direction TEXT, added_at TEXT, done_at TEXT, note TEXT,
    PRIMARY KEY (kind, value)
);
CREATE TABLE IF NOT EXISTS cluster_names (
    label TEXT PRIMARY KEY, name TEXT, verdict TEXT, note TEXT, updated_at TEXT
);
"""

PACIFIC = timezone(timedelta(hours=-8))  # quota resets at Pacific midnight; DST ignored (±1 h)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def quota_day(now: datetime) -> str:
    return now.astimezone(PACIFIC).date().isoformat()


def _int(value) -> int | None:
    return int(value) if value not in (None, "") else None


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Columns added after the first live DB (11.10.2026); CREATE IF NOT EXISTS does not add them."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(query_runs)")}
        if "last_order" not in cols:
            self.conn.execute("ALTER TABLE query_runs ADD COLUMN last_order TEXT")
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(videos)")}
        if "synthetic" not in cols:  # 11.10 (вечер): status.containsSyntheticMedia
            self.conn.execute("ALTER TABLE videos ADD COLUMN synthetic INTEGER")
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    # --- runs & quota -------------------------------------------------------------
    def start_run(self, now: datetime) -> int:
        cur = self.conn.execute("INSERT INTO runs (started_at) VALUES (?)", (iso(now),))
        return cur.lastrowid

    def finish_run(self, run_id: int, now: datetime, search_calls: int, other_calls: int,
                   notes: list[str]) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at=?, search_calls=?, other_calls=?, notes=? WHERE run_id=?",
            (iso(now), search_calls, other_calls, json.dumps(notes, ensure_ascii=False), run_id))
        self.conn.commit()

    def last_run(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY run_id DESC LIMIT 1").fetchone()

    def count_call(self, day: str, bucket: str) -> None:
        self.conn.execute(
            "INSERT INTO quota (day, bucket, calls) VALUES (?, ?, 1) "
            "ON CONFLICT(day, bucket) DO UPDATE SET calls = calls + 1", (day, bucket))

    def quota_used(self, day: str, bucket: str) -> int:
        row = self.conn.execute("SELECT calls FROM quota WHERE day=? AND bucket=?", (day, bucket)).fetchone()
        return row["calls"] if row else 0

    # --- entities -----------------------------------------------------------------
    def upsert_video(self, item: dict, ts: str) -> None:
        sn, st = item.get("snippet", {}), item.get("statistics", {})
        status, cd = item.get("status", {}), item.get("contentDetails", {})
        paid = item.get("paidProductPlacementDetails", {}).get("hasPaidProductPlacement")
        synthetic = status.get("containsSyntheticMedia")  # author's own "altered or synthetic content" label
        self.conn.execute(
            """INSERT INTO videos (video_id, channel_id, title, description, published_at, duration_s,
                 category_id, tags, topics, audio_lang, made_for_kids, paid_placement, updated_at, synthetic)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(video_id) DO UPDATE SET
                 title=excluded.title, description=excluded.description, duration_s=excluded.duration_s,
                 category_id=excluded.category_id, tags=excluded.tags, topics=excluded.topics,
                 audio_lang=excluded.audio_lang, made_for_kids=excluded.made_for_kids,
                 paid_placement=excluded.paid_placement, updated_at=excluded.updated_at,
                 synthetic=excluded.synthetic""",
            (item["id"], sn.get("channelId"), sn.get("title", ""), sn.get("description", ""),
             sn.get("publishedAt"), parse_iso_duration(cd.get("duration")), sn.get("categoryId"),
             json.dumps(sn.get("tags", []), ensure_ascii=False),
             json.dumps(item.get("topicDetails", {}).get("topicCategories", [])),
             sn.get("defaultAudioLanguage") or sn.get("defaultLanguage"),
             None if "madeForKids" not in status else int(bool(status["madeForKids"])),
             None if paid is None else int(bool(paid)), ts,
             None if synthetic is None else int(bool(synthetic))))
        self.conn.execute(
            "INSERT OR REPLACE INTO video_stats VALUES (?,?,?,?,?)",
            (item["id"], ts, _int(st.get("viewCount")), _int(st.get("likeCount")),
             _int(st.get("commentCount"))))

    def upsert_channel(self, item: dict, ts: str) -> None:
        sn, st = item.get("snippet", {}), item.get("statistics", {})
        uploads = item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        self.conn.execute(
            """INSERT INTO channels (channel_id, title, published_at, uploads_playlist, country, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(channel_id) DO UPDATE SET title=excluded.title,
                 uploads_playlist=excluded.uploads_playlist, country=excluded.country,
                 updated_at=excluded.updated_at""",
            (item["id"], sn.get("title", ""), sn.get("publishedAt"), uploads, sn.get("country"), ts))
        subs = None if st.get("hiddenSubscriberCount") else _int(st.get("subscriberCount"))
        self.conn.execute(
            "INSERT OR REPLACE INTO channel_stats VALUES (?,?,?,?,?)",
            (item["id"], ts, subs, _int(st.get("viewCount")), _int(st.get("videoCount"))))

    def add_uploads(self, channel_id: str, video_ids: list[str], ts: str) -> None:
        self.conn.executemany("INSERT OR IGNORE INTO uploads VALUES (?, ?)",
                              [(channel_id, v) for v in video_ids])
        self.conn.execute("UPDATE channels SET uploads_fetched_at=? WHERE channel_id=?", (ts, channel_id))

    def add_hit(self, video_id: str, query: str, source: str, ts: str) -> None:
        self.conn.execute(
            """INSERT INTO hits VALUES (?,?,?,?,?)
               ON CONFLICT(video_id, query) DO UPDATE SET last_seen=excluded.last_seen""",
            (video_id, query, source, ts, ts))

    # --- query registry -----------------------------------------------------------
    def ensure_query(self, query: str, direction: str, origin: str, ts: str, status: str,
                     parent: str | None = None, evidence: str | None = None,
                     priority: float = 0.0) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO queries (query, direction, origin, parent, evidence, status, priority, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)", (query, direction, origin, parent, evidence, status, priority, ts))
        return cur.rowcount == 1

    def queries(self, statuses: tuple[str, ...] | None = None) -> list[sqlite3.Row]:
        if statuses:
            marks = ",".join("?" * len(statuses))
            return self.conn.execute(f"SELECT * FROM queries WHERE status IN ({marks})", statuses).fetchall()
        return self.conn.execute("SELECT * FROM queries").fetchall()

    def set_query_status(self, query: str, status: str) -> None:
        self.conn.execute("UPDATE queries SET status=? WHERE query=?", (status, query))

    def record_query_score(self, query: str, score: float, low: bool) -> None:
        self.conn.execute(
            "UPDATE queries SET last_score=?, low_score_runs = CASE WHEN ? THEN low_score_runs + 1 ELSE 0 END"
            " WHERE query=?", (score, int(low), query))

    def last_query_run(self, query: str, duration: str) -> datetime | None:
        row = self.conn.execute("SELECT last_run_at FROM query_runs WHERE query=? AND duration=?",
                                (query, duration)).fetchone()
        return parse_ts(row["last_run_at"]) if row else None

    def last_query_order(self, query: str, duration: str) -> str | None:
        row = self.conn.execute("SELECT last_order FROM query_runs WHERE query=? AND duration=?",
                                (query, duration)).fetchone()
        return row["last_order"] if row else None

    def mark_query_run(self, query: str, duration: str, ts: str, order: str = "viewCount") -> None:
        self.conn.execute("INSERT OR REPLACE INTO query_runs VALUES (?,?,?,?)", (query, duration, ts, order))

    # --- retention (YouTube API Developer Policies III.E.4: public statistics ≤ 30 days) --------------
    def prune(self, cutoff: str) -> dict[str, int]:
        """Delete statistics snapshots older than `cutoff` and entities not refreshed since then."""
        removed: dict[str, int] = {}
        removed["video_stats"] = self.conn.execute("DELETE FROM video_stats WHERE ts < ?", (cutoff,)).rowcount
        removed["channel_stats"] = self.conn.execute("DELETE FROM channel_stats WHERE ts < ?", (cutoff,)).rowcount
        stale_videos = "SELECT video_id FROM videos WHERE updated_at IS NULL OR updated_at < ?"
        removed["hits"] = self.conn.execute(f"DELETE FROM hits WHERE video_id IN ({stale_videos})", (cutoff,)).rowcount
        removed["uploads"] = self.conn.execute(f"DELETE FROM uploads WHERE video_id IN ({stale_videos})", (cutoff,)).rowcount
        removed["videos"] = self.conn.execute("DELETE FROM videos WHERE updated_at IS NULL OR updated_at < ?",
                                              (cutoff,)).rowcount
        stale_channels = "SELECT channel_id FROM channels WHERE updated_at IS NULL OR updated_at < ?"
        removed["related"] = self.conn.execute(
            f"DELETE FROM related WHERE src IN ({stale_channels}) OR dst IN ({stale_channels})",
            (cutoff, cutoff)).rowcount
        removed["expansions"] = self.conn.execute(f"DELETE FROM expansions WHERE channel_id IN ({stale_channels})",
                                                  (cutoff,)).rowcount
        removed["channels"] = self.conn.execute("DELETE FROM channels WHERE updated_at IS NULL OR updated_at < ?",
                                                (cutoff,)).rowcount
        self.conn.commit()
        return removed

    # --- related channels, manual imports, Claude names -----------------------------
    def add_related(self, src: str, dst: str, kind: str, ts: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO related VALUES (?,?,?,?)", (src, dst, kind, ts))

    def last_expansion(self, channel_id: str) -> datetime | None:
        row = self.conn.execute("SELECT expanded_at FROM expansions WHERE channel_id=?", (channel_id,)).fetchone()
        return parse_ts(row["expanded_at"]) if row else None

    def mark_expansion(self, channel_id: str, ts: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO expansions VALUES (?,?)", (channel_id, ts))

    def add_import(self, kind: str, value: str, direction: str, ts: str) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO imports (kind, value, direction, added_at) VALUES (?,?,?,?)",
            (kind, value, direction, ts))
        return cur.rowcount == 1

    def pending_imports(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM imports WHERE done_at IS NULL ORDER BY added_at").fetchall()

    def finish_import(self, kind: str, value: str, ts: str, note: str = "") -> None:
        self.conn.execute("UPDATE imports SET done_at=?, note=? WHERE kind=? AND value=?", (ts, note, kind, value))

    def set_cluster_name(self, label: str, name: str, verdict: str, note: str, ts: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO cluster_names VALUES (?,?,?,?,?)",
                          (label, name, verdict, note, ts))

    def cluster_names(self) -> dict[str, sqlite3.Row]:
        return {r["label"]: r for r in self.conn.execute("SELECT * FROM cluster_names")}

    # --- reads for scoring --------------------------------------------------------
    def tracked_video_ids(self, since: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT h.video_id FROM hits h JOIN videos v USING (video_id) WHERE v.published_at >= ?"
            " UNION SELECT u.video_id FROM uploads u JOIN videos v USING (video_id) WHERE v.published_at >= ?",
            (since, since)).fetchall()
        return [r["video_id"] for r in rows]

    def video_ids_without_details(self, ids: list[str]) -> list[str]:
        known = {r["video_id"] for r in self.conn.execute("SELECT video_id FROM videos")}
        return [v for v in ids if v not in known]

    def stats_history(self, table: str, key: str) -> dict[str, list[sqlite3.Row]]:
        history: dict[str, list[sqlite3.Row]] = {}
        for row in self.conn.execute(f"SELECT * FROM {table} ORDER BY ts"):
            history.setdefault(row[key], []).append(row)
        return history

    def all_rows(self, table: str) -> list[sqlite3.Row]:
        return self.conn.execute(f"SELECT * FROM {table}").fetchall()
