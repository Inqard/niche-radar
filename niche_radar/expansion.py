"""Growing the channel graph beyond search (docs/RADAR-SPEC.md, v2).

- Manual imports: links pasted from "related videos" / the home page (import-links command).
- Related channels: channels a creator recommends on the channel page + public subscriptions.
Scraping YouTube HTML is deliberately not used (Terms of Service).
"""
from __future__ import annotations

from datetime import datetime

from . import metrics as m
from .api import ApiError, QuotaExceeded
from .db import Store


def manual_query(direction: str) -> str:
    return f"manual:{direction}"


def _fetch_uploads(client, store: Store, channel_ids: list[str], ts: str, notes: list[str]) -> list[str]:
    """Latest uploads of the given channels -> stored with details; returns their video ids."""
    rows = {r["channel_id"]: r["uploads_playlist"] for r in store.all_rows("channels")}
    video_ids: list[str] = []
    for cid in channel_ids:
        if not rows.get(cid):
            continue
        try:
            ids = client.playlist_video_ids(rows[cid])
        except QuotaExceeded:
            raise
        except ApiError as e:
            notes.append(f"uploads {cid}: {e}")
            continue
        store.add_uploads(cid, ids, ts)
        video_ids += ids
    for item in client.videos(sorted(set(video_ids))):
        store.upsert_video(item, ts)
    return video_ids


def process_imports(client, store: Store, ts: str, notes: list[str]) -> tuple[set[str], list[str]]:
    """Fetch pending manual links. Videos and uploads of imported channels join the sub-niche
    `manual:<direction>`. Returns (video ids fetched, imported channel ids); channels also seed
    related-channel expansion."""
    pending = store.pending_imports()
    if not pending:
        return set(), []
    fetched: set[str] = set()
    by_direction_videos: dict[str, list[str]] = {}
    channel_direction: dict[str, str] = {}
    for row in pending:
        if row["kind"] == "video":
            by_direction_videos.setdefault(row["direction"], []).append(row["value"])
        elif row["kind"] == "channel":
            channel_direction[row["value"]] = row["direction"]
        elif row["kind"] == "handle":
            try:
                item = client.channel_by_handle(row["value"])
            except QuotaExceeded:
                raise
            except ApiError as e:  # stays pending, retried next run
                notes.append(f"импорт @{row['value']}: {e}")
                continue
            if item is None:
                store.finish_import("handle", row["value"], ts, "not found")
                notes.append(f"импорт @{row['value']}: канал не найден")
                continue
            store.upsert_channel(item, ts)
            channel_direction[item["id"]] = row["direction"]
            store.finish_import("handle", row["value"], ts, item["id"])

    for direction, ids in by_direction_videos.items():
        query = manual_query(direction)
        store.ensure_query(query, direction, "manual", ts, status="manual")
        items = {it["id"]: it for it in client.videos(ids)}
        for vid in ids:
            if vid in items:
                store.upsert_video(items[vid], ts)
                store.add_hit(vid, query, "manual", ts)
                store.finish_import("video", vid, ts)
                fetched.add(vid)
            else:
                store.finish_import("video", vid, ts, "not found")
                notes.append(f"импорт видео {vid}: не найдено (удалено или приватное)")

    imported: list[str] = []
    if channel_direction:
        for item in client.channels(sorted(channel_direction)):
            store.upsert_channel(item, ts)
        known = {r["channel_id"] for r in store.all_rows("channels")}
        for cid, direction in sorted(channel_direction.items()):
            if cid not in known:
                store.finish_import("channel", cid, ts, "not found")
                notes.append(f"импорт канала {cid}: не найден")
                continue
            query = manual_query(direction)
            store.ensure_query(query, direction, "manual", ts, status="manual")
            for vid in _fetch_uploads(client, store, [cid], ts, notes):
                store.add_hit(vid, query, "manual", ts)
                fetched.add(vid)
            store.finish_import("channel", cid, ts)
            imported.append(cid)
    store.conn.commit()
    return fetched, imported


def expand_related(client, store: Store, cfg: dict, now: datetime, ts: str,
                   seeds: list[str], notes: list[str]) -> list[str]:
    """Recommended channels + public subscriptions of seed channels; small new ones get uploads.
    Returns ids of related channels enriched in this run."""
    rc, requery = cfg["related"], cfg["schedule"]["requery_days"]
    found: list[str] = []
    expanded = with_featured = with_subs = 0
    for cid in seeds:
        if expanded >= rc["max_seed_channels"]:
            break
        last = store.last_expansion(cid)
        if last and m.days_between(now, last) < requery:
            continue
        try:
            featured = client.featured_channel_ids(cid)
        except QuotaExceeded:
            raise
        except ApiError as e:
            notes.append(f"рекомендованные каналы {cid}: {e}")
            featured = []
        subscribed = client.subscription_channel_ids(cid) if rc["use_subscriptions"] else []
        for dst in featured:
            store.add_related(cid, dst, "featured", ts)
        for dst in subscribed:
            store.add_related(cid, dst, "subscription", ts)
        store.mark_expansion(cid, ts)
        expanded += 1
        with_featured += bool(featured)
        with_subs += bool(subscribed)
        found += [d for d in featured + subscribed if d != cid]

    if expanded:  # visible summary: a silent zero once hid a real bug (lowercase section types)
        notes.append(f"похожие: проверено каналов {expanded}, с рекомендациями {with_featured}, "
                     f"с открытыми подписками {with_subs}, связей {len(found)}")
        if expanded >= 10 and not found:
            notes.append("⚠ похожие: ни одной связи у 10+ каналов — проверить ответы channelSections/subscriptions")

    known_uploads = {r["channel_id"] for r in store.all_rows("channels") if r["uploads_fetched_at"]}
    new = [c for c in dict.fromkeys(found) if c not in known_uploads][: rc["max_new_channels"]]
    if not new:
        store.conn.commit()
        return []
    for item in client.channels(new):
        store.upsert_channel(item, ts)
    stats = {r["channel_id"]: r["subs"] for r in store.conn.execute(
        "SELECT channel_id, subs FROM channel_stats WHERE ts=?", (ts,))}
    small = [c for c in new if c in stats and (stats[c] is None or stats[c] <= cfg["enrich"]["max_subs"])]
    _fetch_uploads(client, store, small, ts, notes)
    store.conn.commit()
    return small
