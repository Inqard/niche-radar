"""Builds video / channel / sub-niche tables from the store and ranks sub-niches.

Formula and thresholds: docs/RADAR-SPEC.md, values: radar.toml.
"""
from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median

from . import metrics as m
from .clusters import Doc, cluster_docs, video_terms
from .db import Store, parse_ts

MIN_SNAPSHOT_GAP_HOURS = 12


@dataclass
class Video:
    video_id: str
    channel_id: str
    title: str
    published: datetime | None
    duration_s: int | None
    category_id: str | None
    tags: list[str]
    topics: list[str]
    english: bool
    views: int
    views_per_day: float
    velocity_measured: bool
    subs: int | None
    views_per_sub: float | None
    flags: set[str]
    money_signals: set[str]
    rpm_prior: float
    queries: set[str] = field(default_factory=set)
    outlier: bool = False
    market_ok: bool = True        # English and aimed at the US market (radar.toml [market])

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


@dataclass
class Channel:
    channel_id: str
    title: str
    age_days: float | None
    subs: int | None
    video_count: int | None
    n_long: int = 0
    best_views: int = 0
    seriality: float | None = None
    top2_share: float | None = None
    one_hit: bool = False
    growth_ratio: float | None = None
    median_interval: float | None = None
    days_since_last: float | None = None
    possible_demonetized: bool = False
    template_score: float | None = None
    numbered_share: float | None = None
    median_minutes: float | None = None
    kids_share: float | None = None
    money_share: float | None = None
    indicator: bool = False
    market_share: float | None = None   # share of its long videos aimed at the US market
    risk_share: float | None = None     # share of its long videos with scored risk flags
    content_farm: bool = False          # В1: long videos at ≥ 2 a day
    # vidIQ-like growth from our daily channel snapshots (В1 27:19): views and subscribers per day
    views_per_day: float | None = None
    subs_per_day: float | None = None
    uploads_known: bool = False         # latest uploads fetched -> all its recent videos are tracked
    recent_views_per_day: float = 0.0   # views/day of its videos younger than evergreen_min_age_days

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/channel/{self.channel_id}"

    @property
    def evergreen_pair(self) -> tuple[float, float] | None:
        """(channel views/day, views/day of its recent videos) — only when both sides are known."""
        if not self.uploads_known or self.views_per_day is None:
            return None
        return self.views_per_day, self.recent_views_per_day

    @property
    def us_market(self) -> bool:
        return self.market_share is None or self.market_share >= 0.5


@dataclass
class Niche:
    query: str
    direction: str
    origin: str
    status: str
    parent: str | None
    n_videos: int
    window_views: int
    views_per_day: float
    channels: int
    established: int
    newcomers: int
    median_seriality: float | None
    rpm_prior: float
    risk_share: float
    money_share: float
    outliers: int
    components: dict[str, float]
    score: float
    top_videos: list[Video]
    indicator_channels: list[str]
    kind: str = "query"                                  # "query" | "cluster"
    terms: list[str] = field(default_factory=list)       # cluster: characteristic terms
    sources: list[str] = field(default_factory=list)     # cluster: queries its videos came from
    name: str | None = None                              # cluster: name given by Claude
    verdict: str | None = None
    term_stats: dict[str, tuple[int, int]] = field(default_factory=dict)  # cluster: term -> (channels, views)
    video_ids: list[str] = field(default_factory=list)                   # all member videos, views desc
    # informational only — shown next to the ranking, never multiplied into it (operator decides)
    median_views: float = 0.0
    kids_share: float = 0.0
    flag_counts: dict[str, int] = field(default_factory=dict)
    velocity_measured_share: float = 0.0
    # hype-now signals (В1: «последние 2 недели — сок»; В4: «ниша-однодневка»; В1: канал замолчал)
    fresh_views: int = 0            # views of videos published in the last `fresh_days`
    momentum: float = 0.0           # (fresh views / fresh days) / max(older views / older days, floor)
    top_video_share: float = 0.0    # share of window views taken by the single best video
    demonetized_channels: int = 0   # channels in the sub-niche with the "went silent" signal
    median_minutes: float | None = None   # В1/В4/В5: longer videos = more ads and an edge over competitors
    evergreen_share: float | None = None  # share of its channels' daily views from videos older than the window


def _velocity(history: list, now: datetime, published: datetime | None) -> tuple[float, bool]:
    latest = history[-1]
    latest_ts = parse_ts(latest["ts"])
    for prev in reversed(history[:-1]):
        hours = (latest_ts - parse_ts(prev["ts"])).total_seconds() / 3600
        if hours >= MIN_SNAPSHOT_GAP_HOURS and prev["views"] is not None and latest["views"] is not None:
            return max(latest["views"] - prev["views"], 0) / hours * 24, True
    age = m.days_between(now, published) if published else None
    return (latest["views"] or 0) / max(age or 1, 1), False


def build_videos(store: Store, cfg: dict, now: datetime) -> dict[str, Video]:
    vstats = store.stats_history("video_stats", "video_id")
    cstats = store.stats_history("channel_stats", "channel_id")
    hits: dict[str, set[str]] = {}
    for row in store.all_rows("hits"):
        hits.setdefault(row["video_id"], set()).add(row["query"])

    window_start = now - timedelta(days=cfg["search"]["window_days"])
    mk = cfg["market"]
    auto_channels = {r["channel_id"] for r in store.all_rows("channels") if m.is_auto_channel(r["title"])}
    videos: dict[str, Video] = {}
    for row in store.all_rows("videos"):
        history = vstats.get(row["video_id"])
        if not history:
            continue
        published = parse_ts(row["published_at"])
        vpd, measured = _velocity(history, now, published)
        ch_hist = cstats.get(row["channel_id"])
        subs = ch_hist[-1]["subs"] if ch_hist else None
        views = history[-1]["views"] or 0
        tags = json.loads(row["tags"] or "[]")
        flags = m.keyword_flags(" ".join([row["title"], *tags]), cfg["risk_keywords"])
        if row["made_for_kids"]:
            flags.add("made_for_kids")
        if row["synthetic"]:
            flags.add("ai_label")  # the author disclosed realistic altered/synthetic content (В4/В5: нейроаватары)
        money = m.substring_flags(row["description"] or "", cfg["monetization_patterns"])
        if row["paid_placement"]:
            money.add("paid_placement")
        v = Video(
            row["video_id"], row["channel_id"], row["title"], published, row["duration_s"],
            row["category_id"], tags, json.loads(row["topics"] or "[]"), m.is_english(row["audio_lang"]),
            views, vpd, measured, subs, (views / subs) if subs else None, flags, money,
            m.rpm_prior(row["category_id"], cfg["rpm_by_category"]), hits.get(row["video_id"], set()))
        text = " ".join([row["title"], *tags])
        v.market_ok = v.english and not (
            (mk["exclude_non_latin_titles"] and m.has_non_latin_letters(row["title"]))
            or m.keyword_flags(text, {"other_market": mk["exclude_keywords"]}))
        # outlier = long-form only (Shorts have their own economics) and never from auto-generated channels
        v.outlier = bool(
            v.market_ok and published and published >= window_start
            and (row["duration_s"] or 0) >= cfg["metrics"]["long_min_seconds"]
            and row["channel_id"] not in auto_channels
            and views >= cfg["outlier"]["min_views"]
            and v.views_per_sub is not None and v.views_per_sub >= cfg["outlier"]["min_views_per_sub"])
        videos[v.video_id] = v
    return videos


def build_channels(store: Store, videos: dict[str, Video], cfg: dict, now: datetime) -> dict[str, Channel]:
    mc, ind = cfg["metrics"], cfg["indicator"]
    scored_flags = set(cfg["niche"]["scored_risk_flags"])
    cstats = store.stats_history("channel_stats", "channel_id")
    by_channel: dict[str, list[Video]] = {}
    for v in videos.values():
        by_channel.setdefault(v.channel_id, []).append(v)

    channels: dict[str, Channel] = {}
    for row in store.all_rows("channels"):
        cid = row["channel_id"]
        hist = cstats.get(cid)
        last = hist[-1] if hist else None
        created = parse_ts(row["published_at"])
        ch = Channel(cid, row["title"], m.days_between(now, created) if created else None,
                     last["subs"] if last else None, last["video_count"] if last else None)
        if hist:
            ch.views_per_day = m.rate_per_day(hist, "views", mc["rate_target_days"], mc["rate_min_days"])
            ch.subs_per_day = m.rate_per_day(hist, "subs", mc["rate_target_days"], mc["rate_min_days"])
        ch.uploads_known = bool(row["uploads_fetched_at"])
        ch.recent_views_per_day = sum(
            v.views_per_day for v in by_channel.get(cid, [])
            if v.published and m.days_between(now, v.published) < mc["evergreen_min_age_days"])

        mature_long = sorted(
            (v for v in by_channel.get(cid, [])
             if v.published and (v.duration_s or 0) >= mc["long_min_seconds"]
             and m.days_between(now, v.published) >= mc["min_video_age_days"]),
            key=lambda v: v.published)
        views = [v.views for v in mature_long]
        ch.n_long = len(mature_long)
        if mature_long:
            ch.best_views = max(views)
            ch.seriality = m.seriality(views, mc["seriality_fraction"])
            ch.top2_share = m.top2_share(views)
            ch.one_hit = bool(ch.n_long >= mc["one_hit_min_videos"] and ch.top2_share is not None
                              and ch.top2_share > mc["one_hit_top2_share"])
            ch.growth_ratio = m.growth_ratio(views)
            # cadence needs the channel's real upload list: from a few search hits a big channel that uploads
            # daily looks "silent" (live 11.10: 9 of 36 flags, incl. Peppa Pig Official with 44.8M subs)
            if ch.uploads_known:
                ch.median_interval, ch.days_since_last = m.cadence([v.published for v in mature_long], now)
                ch.possible_demonetized = m.possible_demonetized(
                    ch.n_long, ch.median_interval, ch.days_since_last,
                    mc["demonet_max_median_interval_days"], mc["demonet_min_gap_days"],
                    mc["demonet_gap_multiplier"])
                ch.content_farm = m.content_farm(ch.n_long, ch.median_interval, mc["farm_max_interval_days"],
                                                 mc["farm_min_videos"])
            titles = [v.title for v in mature_long]
            ch.template_score = m.template_score(titles, mc["template_min_titles"])
            ch.numbered_share = m.numbered_share(titles)
            ch.median_minutes = median(v.duration_s for v in mature_long) / 60
            ch.kids_share = sum("made_for_kids" in v.flags for v in mature_long) / ch.n_long
            ch.money_share = sum(bool(v.money_signals) for v in mature_long) / ch.n_long
            ch.market_share = sum(v.market_ok for v in mature_long) / ch.n_long
            ch.risk_share = sum(bool(v.flags & scored_flags) for v in mature_long) / ch.n_long
        elif by_channel.get(cid):
            ch.market_share = sum(v.market_ok for v in by_channel[cid]) / len(by_channel[cid])

        ch.indicator = bool(
            ch.age_days is not None and ch.age_days <= ind["max_age_days"]
            and ch.video_count is not None and ind["min_videos"] <= ch.video_count <= ind["max_videos"]
            and ch.subs is not None and ch.subs < ind["max_subs"]
            and (ch.best_views >= ind["min_best_views"]
                 or (ch.growth_ratio or 0) >= ind["min_growth_ratio"]
                 or (ch.best_views >= ind["relative_min_best_views"]
                     and ch.best_views >= ind["relative_min_views_per_sub"] * max(ch.subs, 1)))
            and not ch.one_hit
            and ch.us_market
            and not m.is_auto_channel(ch.title))
        channels[cid] = ch
    return channels


SCORED_STATUSES = ("seed", "active", "candidate", "chart", "manual", "category")  # not retired / dormant
AUTO_ORIGINS = ("discovered", "cluster", "claude")                      # may go dormant


def window_videos(videos: dict[str, Video], cfg: dict, now: datetime) -> list[Video]:
    """Long, US-market (English), published within the window — the population every sub-niche is scored on."""
    window_start = now - timedelta(days=cfg["search"]["window_days"])
    out = []
    for v in sorted(videos.values(), key=lambda v: v.video_id):
        if not v.published or v.published < window_start:
            continue
        if v.duration_s is not None and v.duration_s < cfg["metrics"]["long_min_seconds"]:
            continue
        if cfg["niche"]["english_only"] and not v.market_ok:
            continue
        out.append(v)
    return out


def is_established(ch: Channel, nc: dict) -> bool:
    """В1: a competitor that has already taken its place — big, or old and mid-size (11.10 audit)."""
    return ((ch.subs or 0) >= nc["established_min_subs"]
            or ((ch.age_days or 0) >= nc["established_min_age_days"]
                and (ch.subs or 0) >= nc["established_aged_min_subs"]))


def score_group(key: str, direction: str, origin: str, status: str, parent: str | None,
                vids: list[Video], channels: dict[str, Channel], cfg: dict, now: datetime) -> Niche:
    nc = cfg["niche"]
    scored_flags = set(nc["scored_risk_flags"])
    chans = {v.channel_id for v in vids}
    hit_chans = {v.channel_id for v in vids if v.views >= cfg["enrich"]["min_hit_views"]}
    established = sum(1 for c in hit_chans if c in channels and is_established(channels[c], nc))
    newcomers = sorted(c for c in chans if c in channels and channels[c].indicator)
    serial = [channels[c].seriality for c in chans if c in channels and channels[c].seriality is not None]
    window_views = sum(v.views for v in vids)
    rpm = median(v.rpm_prior for v in vids)
    risk = sum(bool(v.flags & scored_flags) for v in vids) / len(vids)
    money = sum(bool(v.money_signals) for v in vids) / len(vids)
    med_serial = median(serial) if serial else None
    # Opportunity score = demand × newcomers × crowding × seriality (В1/В2 method). RPM prior, money
    # signals and risk flags are reported beside it, not multiplied in: views pay, the operator decides.
    comp = {
        "demand": math.log10(1 + window_views),
        "newcomers": 1 + nc["newcomer_weight"] * min(len(newcomers), nc["newcomer_cap"]),
        "crowding": 1 / (1 + max(0, established - nc["max_established"])),
        "seriality": 0.5 + (med_serial if med_serial is not None else 0.5),
    }
    ordered = sorted(vids, key=lambda v: (-v.views, v.video_id))
    flag_counts: dict[str, int] = {}
    for v in vids:
        for f in v.flags:
            flag_counts[f] = flag_counts.get(f, 0) + 1
    fresh_days = cfg["metrics"]["fresh_days"]
    older_days = max(cfg["search"]["window_days"] - fresh_days, 1)
    fresh_start = now - timedelta(days=fresh_days)
    fresh = sum(v.views for v in vids if v.published and v.published >= fresh_start)
    older = window_views - fresh
    # floor on the older rate: a theme with no older videos is "new", not infinitely accelerating
    momentum = (fresh / fresh_days) / max(older / older_days, cfg["metrics"]["momentum_floor_views_per_day"])
    demonetized = sum(1 for c in chans if c in channels and channels[c].possible_demonetized)
    durations = [v.duration_s for v in vids if v.duration_s]
    evergreen = m.evergreen_share([channels[c].evergreen_pair for c in sorted(chans)
                                   if c in channels and channels[c].evergreen_pair is not None])
    return Niche(
        key, direction, origin, status, parent, len(vids), window_views,
        sum(v.views_per_day for v in vids), len(chans), established, len(newcomers), med_serial,
        rpm, risk, money, sum(v.outlier for v in vids),
        {k: round(val, 3) for k, val in comp.items()}, round(math.prod(comp.values()), 3),
        ordered[:3], newcomers, video_ids=[v.video_id for v in ordered],
        median_views=median(v.views for v in vids),
        kids_share=sum("made_for_kids" in v.flags for v in vids) / len(vids),
        flag_counts=dict(sorted(flag_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        velocity_measured_share=sum(v.velocity_measured for v in vids) / len(vids),
        fresh_views=fresh, momentum=round(momentum, 2),
        top_video_share=(ordered[0].views / window_views) if window_views else 0.0,
        demonetized_channels=demonetized,
        median_minutes=(median(durations) / 60) if durations else None,
        evergreen_share=evergreen)


def _ranked(niches: list[Niche]) -> list[Niche]:
    """Default order = views in the window (what pays); other orders are built in the report."""
    return sorted(niches, key=lambda n: (-n.window_views, n.query))


def build_niches(store: Store, videos: dict[str, Video], channels: dict[str, Channel],
                 cfg: dict, now: datetime) -> list[Niche]:
    """One sub-niche per registered query (seed, discovered, chart, manual...)."""
    members: dict[str, list[Video]] = {}
    for v in window_videos(videos, cfg, now):
        for q in v.queries:
            members.setdefault(q, []).append(v)
    niches = [score_group(q["query"], q["direction"], q["origin"], q["status"], q["parent"],
                          members[q["query"]], channels, cfg, now)
              for q in store.queries(SCORED_STATUSES) if q["query"] in members]
    return _ranked(niches)


def build_cluster_niches(store: Store, videos: dict[str, Video], channels: dict[str, Channel],
                         cfg: dict, now: datetime) -> list[Niche]:
    """Sub-niches by meaning: clusters over ALL window videos, regardless of the query that found them."""
    cc, dc = cfg["clusters"], cfg["discovery"]
    stop, generic = set(dc["stopwords"]), set(dc["generic_words"])
    eligible = {v.video_id: v for v in window_videos(videos, cfg, now)}
    docs = [Doc(v.video_id, v.channel_id, v.views, video_terms(v.title, v.tags, stop, generic, cc["max_title_ngram"]))
            for v in eligible.values()]
    direction_of = {q["query"]: q["direction"] for q in store.queries()}
    names = store.cluster_names()
    niches = []
    # one-channel groups are kept apart as kind "solo" (В4: «он единственный, кто закрепился — создал поднишу»;
    # live 11.10: 49% of the window's views sat outside multi-channel clusters, incl. 22M on a 12K-sub channel)
    for c in cluster_docs(docs, cc, generic, keep_single_channel=True):
        vids = [eligible[i] for i in c.video_ids]
        queries = Counter(q for v in vids for q in sorted(v.queries))
        directions = Counter(direction_of[q] for v in vids for q in sorted(v.queries) if q in direction_of)
        direction = min(directions.items(), key=lambda kv: (-kv[1], kv[0]))[0] if directions else "related"
        n = score_group(c.key, direction, "cluster", "cluster", None, vids, channels, cfg, now)
        n.kind, n.terms = ("cluster" if c.channels >= cc["min_channels"] else "solo"), c.terms
        n.sources = [q for q, _ in sorted(queries.items(), key=lambda kv: (-kv[1], kv[0]))[:5]]
        if c.label in names:
            n.name, n.verdict = names[c.label]["name"], names[c.label]["verdict"]
        n.term_stats = c.term_stats
        niches.append(n)
    return _ranked(niches)


def update_query_lifecycle(store: Store, niches: list[Niche], cfg: dict) -> list[str]:
    """Record scores; automatically added sub-niches that stay weak go dormant. Seeds never do."""
    dc = cfg["discovery"]
    dormant: list[str] = []
    for n in niches:
        store.record_query_score(n.query, n.score, n.score < dc["dormant_score"])
    for q in store.queries(("active",)):
        if q["origin"] in AUTO_ORIGINS and (q["low_score_runs"] or 0) >= dc["dormant_after_runs"]:
            store.set_query_status(q["query"], "dormant")
            dormant.append(q["query"])
    store.conn.commit()
    return dormant
