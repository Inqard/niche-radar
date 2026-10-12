"""One radar run: plan searches within quota, refresh stats, enrich channels, score, discover."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import metrics as m
from .api import ApiError, QuotaExceeded
from .db import Store, iso, parse_ts, quota_day
from .discovery import Candidate, SourceVideo, cluster_candidates, find_candidates
from .expansion import expand_related, process_imports
from .scoring import (Channel, Niche, Video, build_channels, build_cluster_niches, build_niches, build_videos,
                      update_query_lifecycle, window_videos)

log = logging.getLogger("niche_radar")


@dataclass
class RunResult:
    run_id: int
    now: datetime
    videos: dict[str, Video]
    channels: dict[str, Channel]
    niches: list[Niche]
    clusters: list[Niche] = field(default_factory=list)
    related: list[dict] = field(default_factory=list)   # src, dst, kind for channels known in the store
    new_candidates: list[Candidate] = field(default_factory=list)
    dormant: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    calls: dict[str, int] = field(default_factory=dict)
    search_budget: int = 0
    imported_channels: list[str] = field(default_factory=list)
    related_new: list[str] = field(default_factory=list)
    solo_clusters: list[Niche] = field(default_factory=list)  # one-channel sub-niches (no competitor in sight)


class QuotaMeter:
    """Passed to the API client as on_call: persists every call and counts this run's calls."""

    def __init__(self, store: Store, now: datetime):
        self.store, self.day = store, quota_day(now)
        self.calls: dict[str, int] = {"search": 0, "default": 0}

    def __call__(self, bucket: str) -> None:
        self.store.count_call(self.day, bucket)
        self.calls[bucket] = self.calls.get(bucket, 0) + 1


def search_budget(store: Store, cfg: dict, now: datetime) -> int:
    sc = cfg["search"]
    used = store.quota_used(quota_day(now), "search")
    return max(0, min(sc["max_calls_per_run"], sc["daily_search_limit"] - used))


def direction_settings(cfg: dict, direction: str) -> tuple[list[str], int]:
    """(durations, requery_days): the broad niche map is searched less often and medium-length only."""
    sc, sch = cfg["search"], cfg["schedule"]
    if direction in cfg.get("explore_directions", {}):
        return sch.get("explore_durations", sc["durations"]), sch.get("explore_requery_days", sch["requery_days"])
    return sc["durations"], sch["requery_days"]


CATEGORY_PREFIX = "category:"  # registry key of a keyword-free category search, e.g. "category:27"


def category_id_of(query: str) -> str | None:
    return query[len(CATEGORY_PREFIX):] if query.startswith(CATEGORY_PREFIX) else None


def query_settings(cfg: dict, query: str, direction: str) -> tuple[list[str], int]:
    """(durations, requery_days) of a registry entry: category tops have their own cadence."""
    if category_id_of(query) is not None:
        cs = cfg["category_search"]
        return cs["durations"], cs["requery_days"]
    return direction_settings(cfg, direction)


def plan_searches(store: Store, cfg: dict, now: datetime, budget: int) -> list[tuple[str, str]]:
    """(query, duration) tasks: new candidates first (explore share), then the stalest seeds/actives."""
    pages = cfg["search"]["pages_per_query"]
    explore_cap = int(budget * cfg["schedule"]["explore_share"])
    tasks: list[tuple[str, str]] = []

    def fits(cap: int) -> bool:
        return (len(tasks) + 1) * pages <= cap

    # round-robin over directions: a booming theme must not take the whole exploration budget
    by_direction: dict[str, list] = {}
    for q in sorted(store.queries(("candidate",)), key=lambda q: (-(q["priority"] or 0), q["query"])):
        by_direction.setdefault(q["direction"], []).append(q)
    candidates = []
    while any(by_direction.values()):  # each round: the best candidate of every direction, best first
        heads = [lst.pop(0) for _, lst in sorted(by_direction.items()) if lst]
        candidates += sorted(heads, key=lambda q: (-(q["priority"] or 0), q["query"]))
    for q in candidates:
        for d in direction_settings(cfg, q["direction"])[0]:
            if fits(explore_cap):
                tasks.append((q["query"], d))

    due = []
    for q in store.queries(("seed", "active", "category")):
        durations, requery = query_settings(cfg, q["query"], q["direction"])
        for d in durations:
            last = store.last_query_run(q["query"], d)
            if last is None or m.days_between(now, last) >= requery:
                age = float("inf") if last is None else m.days_between(now, last)
                # staleness is measured in cycles, so a 10-day map query and a 4-day interest query compete fairly
                due.append((age / requery, q["last_score"] or 0.0, q["query"], d))
    due.sort(key=lambda t: (t[0], t[1]), reverse=True)
    # В2 category tops are few and cheap (~2 calls/day): they get their own slot, otherwise never-searched map
    # seeds (staleness ∞ as well) keep them waiting for days — live 12.10: 0 of 30 category tasks in run #5
    cat_cap = cfg.get("category_search", {}).get("max_per_run", 0)
    for _, _, query, d in [t for t in due if category_id_of(t[2]) is not None][:cat_cap]:
        if fits(budget):
            tasks.append((query, d))
    for _, _, query, d in due:
        if (query, d) not in tasks and fits(budget):
            tasks.append((query, d))

    for q in candidates:  # unused budget goes back to exploration
        for d in direction_settings(cfg, q["direction"])[0]:
            if (q["query"], d) not in tasks and fits(budget):
                tasks.append((q["query"], d))
    return tasks


def _run_searches(client, store: Store, cfg: dict, now: datetime, ts: str,
                  tasks: list[tuple[str, str]], notes: list[str], budget: int) -> set[str]:
    sc = cfg["search"]
    published_after = iso(now - timedelta(days=sc["window_days"]))
    status = {q["query"]: q["status"] for q in store.queries()}
    orders = sc.get("orders", ["viewCount"])
    found: set[str] = set()
    # tasks are planned within the budget, but a request retried after a 5xx is charged twice (11.10: 35 calls
    # for a budget of 34) -> stop on the calls actually made, not on the number of tasks
    day = quota_day(now)
    stop_at = store.quota_used(day, "search") + budget
    for query, duration in tasks:
        # alternate result ordering between repeats: viewCount = big channels, relevance = another slice
        last = store.last_query_order(query, duration)
        order = orders[(orders.index(last) + 1) % len(orders)] if last in orders else orders[0]
        token = None
        for _ in range(sc["pages_per_query"]):
            if store.quota_used(day, "search") >= stop_at:
                notes.append(f"search: бюджет {budget} вызовов исчерпан (повторы после ошибок тоже тарифицируются)")
                store.conn.commit()
                return found
            category = category_id_of(query)
            try:
                resp = client.search_videos(None if category else query, published_after, duration,
                                            sc["region"], sc["language"], token, order=order,
                                            category_id=category)
            except QuotaExceeded as e:
                notes.append(f"search остановлен: {e}")
                store.conn.commit()
                return found
            except ApiError as e:
                notes.append(f"search '{query}' ({duration}): {e}")
                break
            for item in resp.get("items", []):
                vid = item.get("id", {}).get("videoId")
                if vid:
                    store.add_hit(vid, query, f"search:{duration}:{order}", ts)
                    found.add(vid)
            token = resp.get("nextPageToken")
            if not token:
                break
        store.mark_query_run(query, duration, ts, order)
        if status.get(query) == "candidate":
            store.set_query_status(query, "active")
    store.conn.commit()
    return found


def _run_charts(client, store: Store, cfg: dict, ts: str, notes: list[str]) -> set[str]:
    regions = cfg["charts"].get("regions", [cfg["search"]["region"]])
    fetched: set[str] = set()
    for region in regions:
        for cat in cfg["charts"]["category_ids"]:
            query = f"chart:{region}:{cat}"
            store.ensure_query(query, f"trending:{cat}", "chart", ts, status="chart")
            try:
                items = client.most_popular(region, cat)
            except QuotaExceeded as e:
                notes.append(f"charts остановлены: {e}")
                store.conn.commit()
                return fetched
            except ApiError as e:  # some categories have no chart in a region
                notes.append(f"chart {region}:{cat}: {e}")
                continue
            for item in items:
                store.upsert_video(item, ts)
                store.add_hit(item["id"], query, "chart", ts)
                fetched.add(item["id"])
    store.conn.commit()
    return fetched


def _enrich(client, store: Store, cfg: dict, now: datetime, ts: str,
            found: set[str], fetched: set[str], notes: list[str]) -> None:
    since = iso(now - timedelta(days=cfg["schedule"]["refresh_window_days"]))
    to_refresh = (set(store.tracked_video_ids(since)) | found) - fetched
    for item in client.videos(sorted(to_refresh)):
        store.upsert_video(item, ts)
    fetched |= to_refresh
    store.conn.commit()

    channel_ids = store.channel_ids_of(list(fetched))
    for item in client.channels(channel_ids):
        store.upsert_channel(item, ts)
    store.conn.commit()

    # Channels worth a look at their uploads: small enough, with a strong recent video.
    ec = cfg["enrich"]
    window_start = iso(now - timedelta(days=cfg["search"]["window_days"]))
    rows = store.conn.execute(
        """SELECT c.channel_id, c.title, c.uploads_playlist, c.uploads_fetched_at, c.published_at, cs.subs,
                  MAX(s.views) AS best
           FROM channels c JOIN videos v ON v.channel_id = c.channel_id
           JOIN video_stats s ON s.video_id = v.video_id AND s.ts = ?
           JOIN channel_stats cs ON cs.channel_id = c.channel_id AND cs.ts = ?
           WHERE v.published_at >= ? AND (cs.subs IS NULL OR cs.subs <= ?)
           GROUP BY c.channel_id HAVING best >= ?""",
        (ts, ts, window_start, ec["max_subs"], ec["min_hit_views"])).fetchall()
    # Indicator candidates first (young and small — В1/В2), then the rest by their best hit. Ordering by
    # the best hit alone starved them: live 11.10, 184 of 267 young small channels never got their uploads,
    # and without uploads neither growth nor cadence can be measured.
    ind = cfg["indicator"]

    def young_small(row) -> bool:
        created = parse_ts(row["published_at"])
        return (created is not None and m.days_between(now, created) <= ind["max_age_days"]
                and (row["subs"] is not None and row["subs"] < ind["max_subs"])
                and not m.is_auto_channel(row["title"]))  # "… - Topic" can never be an indicator

    rows = sorted(rows, key=lambda r: (not young_small(r), -r["best"], r["channel_id"]))
    requery = cfg["schedule"]["requery_days"]
    new_ids: list[str] = []
    enriched = 0
    for row in rows:
        if enriched >= ec["max_channels"]:
            break
        last = parse_ts(row["uploads_fetched_at"])
        if not row["uploads_playlist"] or (last and m.days_between(now, last) < requery):
            continue
        try:
            ids = client.playlist_video_ids(row["uploads_playlist"])
        except QuotaExceeded:
            raise
        except ApiError as e:
            notes.append(f"uploads {row['channel_id']}: {e}")
            continue
        store.add_uploads(row["channel_id"], ids, ts)
        new_ids += [v for v in ids if v not in fetched]
        enriched += 1
    for item in client.videos(sorted(set(new_ids))):
        store.upsert_video(item, ts)
    store.conn.commit()


def _discovery_sources(videos: dict[str, Video], channels: dict[str, Channel],
                       niches: list[Niche], cfg: dict, now: datetime) -> list[SourceVideo]:
    """Outlier videos and videos of indicator channels — long, US-market, inside the window only
    (11.10: Shorts/old videos of indicator channels found via charts had no scored sub-niche -> 'unknown')."""
    score = {n.query: n.score for n in niches}
    direction = {n.query: n.direction for n in niches}
    eligible = window_videos(videos, cfg, now)
    channel_query: dict[str, str] = {}  # upload-only videos inherit the channel's best sub-niche
    for v in eligible:
        for q in sorted(v.queries):
            if q in direction and score.get(q, 0) > score.get(channel_query.get(v.channel_id, ""), -1):
                channel_query[v.channel_id] = q
    scored_flags = set(cfg["niche"]["scored_risk_flags"])
    sources = []
    for v in eligible:
        ch = channels.get(v.channel_id)
        if v.flags & scored_flags or not (v.outlier or (ch and ch.indicator)):
            continue
        known = [q for q in sorted(v.queries) if q in direction]
        q = max(known, key=lambda x: score.get(x, 0)) if known else channel_query.get(v.channel_id)
        if not q:
            continue
        sources.append(SourceVideo(v.video_id, v.channel_id, v.title, v.tags, v.views, q,
                                   direction[q], score.get(q, 0.0)))
    return sources


def _related_seeds(channels: dict[str, Channel], videos: dict[str, Video], cfg: dict,
                   imported: list[str]) -> list[str]:
    """Indicator channels and channels with outlier videos (small enough), strongest first."""
    outlier_channels = {v.channel_id for v in videos.values() if v.outlier}
    eligible = [c for c in channels.values()
                if (c.indicator or c.channel_id in outlier_channels) and c.us_market
                and (c.subs is None or c.subs <= cfg["enrich"]["max_subs"])]
    ranked = sorted(eligible, key=lambda c: (not c.indicator, -c.best_views, c.channel_id))
    return list(dict.fromkeys(imported + [c.channel_id for c in ranked]))


def _related_rows(store: Store) -> list[dict]:
    known = {r["channel_id"] for r in store.all_rows("channels")}
    return [dict(r) for r in store.conn.execute("SELECT src, dst, kind FROM related ORDER BY src, dst, kind")
            if r["dst"] in known]


def _score_all(store: Store, cfg: dict, now: datetime):
    """(videos, channels, query niches, multi-channel clusters, one-channel 'solo' clusters)."""
    videos = build_videos(store, cfg, now)
    channels = build_channels(store, videos, cfg, now)
    groups = build_cluster_niches(store, videos, channels, cfg, now)
    return (videos, channels, build_niches(store, videos, channels, cfg, now),
            [n for n in groups if n.kind == "cluster"], [n for n in groups if n.kind == "solo"])


def run(client, store: Store, cfg: dict, now: datetime, meter: QuotaMeter) -> RunResult:
    ts = iso(now)
    run_id = store.start_run(now)
    notes: list[str] = []
    seed_sets = {**cfg["directions"], **cfg.get("explore_directions", {})}
    configured = {q for queries in seed_sets.values() for q in queries}
    for direction, queries in seed_sets.items():
        for q in queries:
            if not store.ensure_query(q, direction, "seed", ts, status="seed"):
                store.conn.execute("UPDATE queries SET status='seed', direction=? WHERE query=? AND "
                                   "origin='seed'", (direction, q))
    for q in store.queries(("seed",)):  # removed from the config -> stop spending quota on it
        if q["query"] not in configured:
            store.set_query_status(q["query"], "retired")
    # В2: category tops without a keyword; direction = the same family as the category's trending chart
    categories = {CATEGORY_PREFIX + c for c in cfg.get("category_search", {}).get("category_ids", [])}
    for key in sorted(categories):
        store.ensure_query(key, f"trending:{category_id_of(key)}", "category", ts, status="category")
        store.conn.execute("UPDATE queries SET status='category' WHERE query=? AND origin='category'", (key,))
    for q in store.queries(("category",)):
        if q["query"] not in categories:
            store.set_query_status(q["query"], "retired")

    budget = search_budget(store, cfg, now)
    tasks = plan_searches(store, cfg, now, budget)
    log.info("search budget %d, tasks %d", budget, len(tasks))
    found = _run_searches(client, store, cfg, now, ts, tasks, notes, budget)
    log.info("search: %d videos", len(found))
    fetched = _run_charts(client, store, cfg, ts, notes)
    imported_channels: list[str] = []
    related_new: list[str] = []
    try:
        try:
            imported_videos, imported_channels = process_imports(client, store, ts, notes)
            fetched |= imported_videos
        except QuotaExceeded:
            raise
        except ApiError as e:  # imports stay pending and are retried next run
            notes.append(f"импорт ссылок: {e}")
        _enrich(client, store, cfg, now, ts, found, fetched, notes)
        # related channels need indicators, so score once before expanding the graph
        videos = build_videos(store, cfg, now)
        channels = build_channels(store, videos, cfg, now)
        try:
            related_new = expand_related(client, store, cfg, now, ts,
                                         _related_seeds(channels, videos, cfg, imported_channels), notes)
        except QuotaExceeded:
            raise
        except ApiError as e:
            notes.append(f"похожие каналы: {e}")
        log.info("related channels enriched: %d", len(related_new))
    except QuotaExceeded as e:
        notes.append(f"обогащение остановлено: {e}")
    store.conn.commit()

    # Developer Policies III.E.4: public statistics are kept ≤ retention_days, then refreshed or deleted
    removed = store.prune(iso(now - timedelta(days=cfg["retention"]["days"])))
    if any(removed.values()):
        notes.append("retention: удалено " + ", ".join(f"{k} {v}" for k, v in removed.items() if v))

    videos, channels, niches, clusters, solo = _score_all(store, cfg, now)
    dormant = update_query_lifecycle(store, niches, cfg, now)

    existing = {q["query"] for q in store.queries()}
    safe_clusters = [c for c in clusters if c.risk_share <= cfg["discovery"]["max_source_risk_share"]]
    from_clusters = cluster_candidates(
        [(c.query, c.direction, c.score, c.term_stats) for c in safe_clusters], existing, cfg["discovery"])
    candidates = find_candidates(_discovery_sources(videos, channels, niches, cfg, now), existing,
                                 cfg["discovery"], extra=from_clusters)
    for c in candidates:
        origin = "cluster" if c.parent.startswith("cluster:") else "discovered"
        evidence = json.dumps({"channels": c.channels, "views": c.views, "videos": c.evidence})
        store.ensure_query(c.phrase, c.direction, origin, ts, status="candidate",
                           parent=c.parent, evidence=evidence, priority=c.priority)
    store.finish_run(run_id, now, meter.calls.get("search", 0), meter.calls.get("default", 0), notes)
    return RunResult(run_id, now, videos, channels, niches, clusters, _related_rows(store), candidates,
                     dormant, notes, dict(meter.calls), budget, imported_channels, related_new, solo)


def rescore(store: Store, cfg: dict, now: datetime) -> RunResult:
    """Recompute tables from the store without API calls or registry changes."""
    videos, channels, niches, clusters, solo = _score_all(store, cfg, now)
    last = store.last_run()
    return RunResult(last["run_id"] if last else 0, now, videos, channels, niches, clusters,
                     _related_rows(store), solo_clusters=solo)
