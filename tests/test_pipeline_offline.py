"""End-to-end run on a fake YouTube world with known answers (no network, temp DB)."""
from __future__ import annotations

import copy
import csv
import json
from datetime import datetime, timedelta, timezone

import pytest

from niche_radar.claude_io import import_from_claude
from niche_radar.config import load_config
from niche_radar.db import Store, iso, quota_day
from niche_radar.links import parse_many
from niche_radar.pipeline import QuotaMeter, rescore, run
from niche_radar.report import write_outputs
from niche_radar.scoring import window_videos

DAY1 = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)


def video(vid, ch, title, published, views, tags=(), cat="27", kids=False, minutes=12, synthetic=False):
    return {
        "id": vid,
        "snippet": {"channelId": ch, "title": title, "description": "", "publishedAt": iso(published),
                    "tags": list(tags), "categoryId": cat, "defaultAudioLanguage": "en"},
        "statistics": {"viewCount": str(views), "likeCount": "1", "commentCount": "1"},
        "contentDetails": {"duration": f"PT{minutes}M"},
        "status": {"madeForKids": kids, "containsSyntheticMedia": synthetic},
        "topicDetails": {"topicCategories": ["https://en.wikipedia.org/wiki/Knowledge"]},
        "paidProductPlacementDetails": {"hasPaidProductPlacement": False},
    }


def channel(cid, title, created, subs, video_count):
    return {"id": cid, "snippet": {"title": title, "publishedAt": iso(created)},
            "statistics": {"subscriberCount": str(subs), "viewCount": "1", "videoCount": str(video_count),
                           "hiddenSubscriberCount": False},
            "contentDetails": {"relatedPlaylists": {"uploads": "UU" + cid}}}


class FakeWorld:
    """Search answers are computed from video data, honoring publishedAfter like the real API."""

    def __init__(self, now: datetime):
        self.videos: dict[str, dict] = {}
        self.channels: dict[str, dict] = {}
        self.uploads: dict[str, list[str]] = {}
        self.search: dict[str, list[str]] = {}
        self.charts: dict[str, list[str]] = {}
        self.category_search: dict[str, list[str]] = {}
        d = lambda days: now - timedelta(days=days)

        def add(ch, title, created, subs, vids, query=None, search_last=None):
            self.channels[ch] = channel(ch, title, created, subs, len(vids))
            self.uploads["UU" + ch] = []
            for v in vids:
                self.videos[v["id"]] = v
                self.uploads["UU" + ch].append(v["id"])
            if query:
                ids = [v["id"] for v in vids][-search_last:] if search_last else [v["id"] for v in vids]
                self.search.setdefault(query, []).extend(ids)

        # Indicator: 40-day-old channel, steep growth (В2), tag shared with LINGO -> discovery
        growth = [3000, 4000, 5000, 6000, 20000, 50000, 90000, 150000, 250000, 400000, 300000, 350000]
        words = ["Salary", "Nice", "Quarantine", "Clue", "Muscle", "Robot", "Sandwich", "Panic", "Avocado",
                 "Disaster", "Ketchup", "Jeans"]
        patterns = ["Why We Say {}", "The Strange Story Behind {}", "{} Has a Weird Origin"]
        add("NEW", "Word Nerd", d(40), 20_000, [
            video(f"new{i}", "NEW", patterns[i % 3].format(w), d(38 - 3 * i), v, tags=["word origins"])
            for i, (w, v) in enumerate(zip(words, growth))], query="etymology", search_last=6)
        # Mature mid-size channel with an outlier video using the same tag
        add("LINGO", "Lingo Lab", d(300), 60_000, [
            video("lingo1", "LINGO", "Word Origins You Never Knew", d(10), 400_000, tags=["word origins"]),
            video("lingo2", "LINGO", "English Is Weird", d(20), 40_000)], query="etymology")
        # One-hit: young and small, but everything rides on a single video (В4 «однодневка»)
        add("ONE", "Etym Blast", d(50), 30_000, [
            video(f"one{i}", "ONE", f"Etymology Story {chr(65 + i)}", d(28 - 4 * i), v)
            for i, v in enumerate([900_000, 2000, 2500, 3000, 1800, 2200])], query="etymology")
        # Regular uploader that went silent 20 days ago (В1: possible demonetization), established by age
        add("DEMON", "Lore Daily", d(400), 150_000, [
            video(f"dem{i}", "DEMON", f"Lore of {chr(65 + i)} Explained", d(60 - 2 * i), 30_000, cat="20")
            for i in range(21)], query="game lore explained", search_last=5)
        # Seven big established channels -> overcrowded sub-niche (В1: > 5–6 закрепившихся)
        for k in range(7):
            add(f"BIG{k}", f"Lore Giant {k}", d(1800), 1_000_000, [
                video(f"big{k}", f"BIG{k}", f"Complete Game Lore {k}", d(5 + k), 500_000, cat="20")],
                query="game lore explained")
        # Made-for-kids video in the same sub-niche -> risk share
        add("KIDS", "Toon Tots", d(200), 10_000, [
            video("kid1", "KIDS", "Cartoon Lore for Kids", d(9), 25_000, cat="20", kids=True)],
            query="game lore explained")
        # Trending (outside our interests): two outlier videos sharing a tag -> discovery 'trending:27'
        add("SPACE1", "Cosmos Now", d(900), 200_000, [
            video("sp1", "SPACE1", "Black Holes Are Stranger Than You Think", d(3), 1_000_000, tags=["space facts"])])
        add("SPACE2", "Star Notes", d(500), 100_000, [
            video("sp2", "SPACE2", "The Edge of the Universe", d(4), 3_000_000, tags=["space facts"],
                  synthetic=True)])                       # the author disclosed realistic AI footage
        self.charts["27"] = ["sp1", "sp2"]
        # 11.10: a trending Short on an indicator channel must not seed discovery as direction 'unknown'
        self.videos["new_short"] = video("new_short", "NEW", "Why We Say Pizza", d(2), 2_000_000,
                                         tags=["pizza etymology"], minutes=1)
        self.uploads["UUNEW"].append("new_short")
        self.charts["24"] = ["new_short"]
        # What the discovered queries will return on day 2
        self.search["word origins"] = ["lingo1", "new11"]
        self.search["space facts"] = ["sp1", "sp2"]

        # v2 — related channels: SMITH is never returned by search; NEW recommends it, LINGO subscribes to it
        add("SMITH", "Word Smith", d(30), 15_000, [
            video(f"smith{i}", "SMITH", f"The Origin of the Word {w}", d(25 - 7 * i), v, tags=["word origins"])
            for i, (w, v) in enumerate(zip(["Boss", "Cool", "Nerd", "Dude"], [8000, 60000, 150000, 220000]))])
        self.featured = {"NEW": ["SMITH", "BIG0"]}          # BIG0 is too big to enrich
        self.subscriptions = {"LINGO": ["SMITH"]}           # ONE etc.: private/empty -> []
        # v2 — manual import: a video link and a channel @handle
        self.videos["manualvid01"] = video("manualvid01", "LINGO", "The Origin of OK", d(6), 90_000,
                                           tags=["word origins"])
        add("ARCH", "Ancient Engineering Hub", d(90), 40_000, [
            video(f"arch{i}", "ARCH", t, d(20 - 6 * i), 70_000 + 10_000 * i, tags=["ancient engineering"])
            for i, t in enumerate(["How Romans Built Aqueducts", "Roman Concrete Explained",
                                   "How the Pyramids Were Built"])])
        self.handles = {"archaeo": "ARCH"}
        # live run 11.10.2026: other-market videos mislabeled as English by their authors.
        # Without the market filter this channel is a perfect "indicator" (20 days, 5K subs, 300K views).
        add("INDX", "Exam Guru", d(20), 5_000, [
            video("ind0", "INDX", "12th Physics Quarterly Question Paper 2026", d(15), 300_000),
            video("ind1", "INDX", "11th Maths Question Paper 2026 With Answers", d(12), 250_000),
            video("ind2", "INDX", "സഹാറയല്ല ലോകത്തിലെ ഏറ്റവും വലിയ മരുഭൂമി Physics", d(8), 200_000)],
            query="etymology")
        # live run: YouTube's auto-generated music channels looked like indicators
        add("TOPIC", "Release - Topic", d(27), 57, [
            video(f"top{i}", "TOPIC", f"Golden Hour Song {chr(65 + i)}", d(20 - 4 * i), 150_000 * (i + 1))
            for i in range(4)], query="etymology")
        # 11.10 audit: a narrow-niche indicator — 40 days, 2K subs, best 45K (< 100K absolute bar)
        add("NARROW", "Compiler Nerd", d(40), 2_000, [
            video(f"nar{i}", "NARROW", f"How {w} Compilers Work", d(30 - 7 * i), v)
            for i, (w, v) in enumerate(zip(["C", "Rust", "Go", "Zig"], [4000, 12000, 30000, 45000]))],
            query="game lore explained")
        # В2 category tops: reachable ONLY through the keyword-free category search (no query, no chart).
        # FARM uploads a long video every ~7 hours (В1: «по 2–3 ролика в день — контент-ферма»).
        self.channels["FARM"] = channel("FARM", "Bridge Facts Daily", d(60), 50_000, 8)
        self.uploads["UUFARM"] = []
        for i in range(8):
            v = video(f"farm{i}", "FARM", f"How This Bridge Was Built {i}", d(4 + 0.3 * i), 120_000 - 5_000 * i,
                      cat="28", tags=["bridge engineering"])
            self.videos[v["id"]] = v
            self.uploads["UUFARM"].append(v["id"])
        self.category_search["28"] = [f"farm{i}" for i in range(8)]
        # live 11.10: a 5M-sub official channel seen only through 5 old chart hits looked "silent";
        # it is too big for its uploads to be fetched, so we do not know its real cadence
        add("OFFICIAL", "Official Cartoon Channel", d(3000), 5_000_000, [
            video(f"off{i}", "OFFICIAL", f"Official Clip Number {i}", d(24 - 2 * i), 400_000, cat="24")
            for i in range(5)])
        self.charts["24"].extend(f"off{i}" for i in range(5))
        # live run: risky outliers (adult-adjacent) must not seed new queries even with a shared tag
        for k in range(2):
            add(f"RISK{k}", f"Attraction Code {k}", d(25), 8_000, [
                video(f"risk{k}", f"RISK{k}", f"Seduction Secrets Women Never Tell {k}", d(5 + k), 300_000,
                      tags=["dating psychology"])], query="etymology")

    def grow(self, factor: float) -> None:
        for v in self.videos.values():
            v["statistics"]["viewCount"] = str(int(int(v["statistics"]["viewCount"]) * factor))


class FakeClient:
    def __init__(self, world: FakeWorld, on_call):
        self.world, self.on_call = world, on_call
        self.calls = {"search": 0, "default": 0}

    def _tick(self, bucket):
        self.calls[bucket] += 1
        self.on_call(bucket)

    def search_videos(self, query, published_after, duration, region, language, page_token=None,
                      order="viewCount", category_id=None):
        self._tick("search")
        self.orders = getattr(self, "orders", [])
        self.orders.append((query, order))
        after = datetime.fromisoformat(published_after.replace("Z", "+00:00"))
        source = self.world.category_search.get(category_id, []) if category_id else self.world.search.get(query, [])
        ids = [i for i in source
               if datetime.fromisoformat(self.world.videos[i]["snippet"]["publishedAt"].replace("Z", "+00:00")) >= after]
        return {"items": [{"id": {"videoId": i}} for i in ids]}

    def most_popular(self, region, category_id):
        self._tick("default")
        return [copy.deepcopy(self.world.videos[i]) for i in self.world.charts.get(category_id, [])]

    def _batched(self, ids, source):
        ids = list(ids)
        for _ in range(0, len(ids), 50):
            self._tick("default")
        return [copy.deepcopy(source[i]) for i in ids if i in source]

    def videos(self, ids):
        return self._batched(ids, self.world.videos)

    def channels(self, ids):
        return self._batched(ids, self.world.channels)

    def playlist_video_ids(self, playlist_id):
        self._tick("default")
        return list(self.world.uploads.get(playlist_id, []))

    def featured_channel_ids(self, channel_id):
        self._tick("default")
        return list(self.world.featured.get(channel_id, []))

    def subscription_channel_ids(self, channel_id):
        self._tick("default")  # the real client turns 403 subscriptionForbidden into [] (tests/test_api.py)
        return list(self.world.subscriptions.get(channel_id, []))

    def channel_by_handle(self, handle):
        self._tick("default")
        cid = self.world.handles.get(handle.lstrip("@").lower())
        return copy.deepcopy(self.world.channels[cid]) if cid else None


@pytest.fixture
def cfg():
    c = load_config()
    c["directions"] = {"languages": ["etymology"], "games_adult": ["game lore explained"]}
    c["explore_directions"] = {}            # the broad map is tested in test_planner; here only 2 seeds
    c["charts"]["category_ids"] = ["27", "24"]
    c["charts"]["regions"] = ["US"]
    c["category_search"]["category_ids"] = []  # category tops are tested on their own (test_category_top_*)
    c["search"]["durations"] = ["medium"]
    # tiny world: the 'word origins' theme covers ~35% of all window videos; with thousands of real
    # videos a sub-niche is a few percent, so the production cut-off (0.2) stays in radar.toml
    c["clusters"]["max_df_share"] = 0.5
    return c


@pytest.fixture
def day1(cfg, tmp_path):
    world = FakeWorld(DAY1)
    store = Store(tmp_path / "radar.sqlite")
    for kind, value in parse_many(["https://youtu.be/manualvid01"])[0]:
        store.add_import(kind, value, "languages", iso(DAY1))
    for kind, value in parse_many(["https://www.youtube.com/@archaeo/videos"])[0]:
        store.add_import(kind, value, "tech_explainers", iso(DAY1))
    meter = QuotaMeter(store, DAY1)
    client = FakeClient(world, meter)
    result = run(client, store, cfg, DAY1, meter)
    yield world, store, client, result, tmp_path
    store.close()


def niche(result, query):
    return next(n for n in result.niches if n.query == query)


def test_indicator_one_hit_and_demonetization_are_detected(day1):
    _, _, _, result, _ = day1
    ch = result.channels
    assert ch["NEW"].indicator
    assert ch["NEW"].growth_ratio > 5
    assert ch["ONE"].one_hit and not ch["ONE"].indicator
    assert ch["DEMON"].possible_demonetized
    assert not ch["LINGO"].indicator  # 300 days old
    assert not ch["BIG0"].indicator


def test_open_subniche_outranks_overcrowded_one(day1):
    _, _, _, result, _ = day1
    ety, lore = niche(result, "etymology"), niche(result, "game lore explained")
    assert lore.established == 8 and lore.components["crowding"] == pytest.approx(1 / 3, abs=1e-3)
    assert ety.newcomers == 1 and ety.established == 0  # LINGO: 300 days and 60K subs is not "established"
    assert lore.risk_share > 0  # made-for-kids video
    assert ety.score > lore.score                      # opportunity score, not the views ranking


def test_discovery_finds_shared_phrases_inside_and_outside_interests(day1):
    _, store, _, result, _ = day1
    found = {c.phrase: c for c in result.new_candidates}
    assert "unknown" not in {c.direction for c in result.new_candidates}
    assert "pizza etymology" not in found           # a Short: outside the long-form population
    # found both as a shared tag (parent = sub-niche query) and as a cluster term; either wins on priority
    assert found["word origins"].direction == "languages"
    assert found["word origins"].parent == "etymology" or found["word origins"].parent.startswith("cluster:")
    assert found["space facts"].direction == "trending:27"
    statuses = {q["query"]: q["status"] for q in store.queries()}
    assert statuses["word origins"] == statuses["space facts"] == "candidate"


def test_quota_accounting_matches_calls(day1):
    _, store, client, result, _ = day1
    day = quota_day(DAY1)
    assert store.quota_used(day, "search") == client.calls["search"] == 2  # 2 seeds × 1 duration
    assert store.quota_used(day, "default") == client.calls["default"]
    assert result.calls == client.calls


class RetryingClient(FakeClient):
    """The first search request is charged twice, as the real client's retry after a 5xx is."""

    def search_videos(self, *args, **kwargs):
        if not self.calls["search"]:
            self._tick("search")
        return super().search_videos(*args, **kwargs)


def test_search_stops_at_budget_when_a_retry_is_charged(cfg, tmp_path):
    cfg["search"]["daily_search_limit"] = cfg["search"]["max_calls_per_run"] = 2  # = 2 seeds x 1 duration
    store = Store(tmp_path / "radar.sqlite")
    meter = QuotaMeter(store, DAY1)
    client = RetryingClient(FakeWorld(DAY1), meter)
    result = run(client, store, cfg, DAY1, meter)
    assert store.quota_used(quota_day(DAY1), "search") == client.calls["search"] == 2
    searched = [q for q in ("etymology", "game lore explained") if store.last_query_run(q, "medium")]
    assert len(searched) == 1                   # the second task stays due for the next run
    assert any("бюджет 2" in n for n in result.notes)
    store.close()


def test_report_files_are_written(day1, cfg):
    _, _, _, result, tmp_path = day1
    report = write_outputs(result, cfg, tmp_path / "reports")
    text = report.read_text(encoding="utf-8")
    assert "Word Nerd" in text and "etymology" in text and "word origins" in text
    assert (tmp_path / "reports" / "niches-2026-10-10.csv").exists()
    assert (tmp_path / "reports" / "claude_input-2026-10-10.json").exists()


def test_day2_searches_only_candidates_and_measures_velocity(day1, cfg):
    world, store, _, _, _ = day1
    day2 = DAY1 + timedelta(days=1)
    world.grow(1.1)
    meter = QuotaMeter(store, day2)
    client = FakeClient(world, meter)
    result = run(client, store, cfg, day2, meter)
    statuses = {q["query"]: q["status"] for q in store.queries()}
    assert statuses["word origins"] == "active" and statuses["space facts"] == "active"
    # seeds were searched yesterday (requery_days=3) -> today the budget goes to candidates only
    for seed in ("etymology", "game lore explained"):
        assert store.last_query_run(seed, "medium") == DAY1
    searched_today = {r["query"] for r in store.conn.execute(
        "SELECT query FROM query_runs WHERE last_run_at = ?", (iso(day2),))}
    assert {"word origins", "space facts"} <= searched_today
    assert all(statuses[q] == "active" for q in searched_today)
    assert not searched_today & {"etymology", "game lore explained"}
    assert client.calls["search"] == len(searched_today)  # 1 duration × 1 page per query
    assert result.videos["lingo1"].velocity_measured
    assert result.videos["lingo1"].views_per_day == pytest.approx(40_000, rel=0.01)
    assert niche(result, "word origins").channels == 2


# --- v2: related channels, manual import, clusters, Claude loop -------------------------------------

def cluster_with(result, term):
    return next(c for c in result.clusters if term in c.terms)


def test_related_channel_known_only_from_recommendations_is_enriched(day1):
    _, store, _, result, _ = day1
    smith = result.channels["SMITH"]
    assert smith.n_long == 4 and smith.indicator          # found by no search, yet a full indicator
    assert "SMITH" in result.related_new
    edges = {(r["src"], r["dst"], r["kind"]) for r in result.related}
    assert ("NEW", "SMITH", "featured") in edges and ("LINGO", "SMITH", "subscription") in edges
    assert result.channels["BIG0"].n_long == 1           # recommended but too big -> uploads not fetched
    assert store.last_expansion("NEW") == DAY1


def test_manual_import_of_video_link_and_handle(day1):
    _, store, _, result, _ = day1
    assert store.pending_imports() == []
    assert "manual:languages" in result.videos["manualvid01"].queries
    assert result.imported_channels == ["ARCH"]
    assert all("manual:tech_explainers" in result.videos[f"arch{i}"].queries for i in range(3))
    assert {n.query for n in result.niches} >= {"manual:languages", "manual:tech_explainers"}


def test_clusters_merge_sources_and_keep_topics_apart(day1):
    _, _, _, result, _ = day1
    words = cluster_with(result, "word origins")
    members = {result.videos[v].channel_id for v in words.video_ids}
    assert {"NEW", "SMITH", "LINGO"} <= members            # search + related + import in one sub-niche
    assert members.isdisjoint({"DEMON", "KIDS", "ONE", "ARCH"}) and not any(c.startswith("BIG") for c in members)
    assert {"NEW", "SMITH"} <= set(words.indicator_channels)
    assert "etymology" in words.sources and "manual:languages" in words.sources
    lore = cluster_with(result, "lore")
    assert lore.query != words.query and lore.established >= 6
    assert set(lore.video_ids).isdisjoint(words.video_ids)


def test_report_shows_v2_sections(day1, cfg):
    _, _, _, result, tmp_path = day1
    text = write_outputs(result, cfg, tmp_path / "reports").read_text(encoding="utf-8")
    assert "Подниши по смыслу" in text and "Похожие каналы" in text
    assert "по просмотрам" in text and "по скорости" in text and "RPM-ориентир" in text
    assert "Word Smith" in text and "(через похожие)" in text
    assert (tmp_path / "reports" / "claude_prompt.md").exists()
    assert (tmp_path / "reports" / "clusters-2026-10-10.csv").exists()


def test_claude_answer_names_clusters_and_adds_queries(day1, cfg):
    _, store, _, result, tmp_path = day1
    words = cluster_with(result, "word origins")
    label = words.query.split(" #")[0]
    answer = tmp_path / "claude_answer.json"
    answer.write_text(json.dumps({
        "subniches": [{"label": label, "name": "Происхождение слов", "verdict": "go", "note": "вечнозелёное"},
                      {"label": "", "name": "без label"}],
        "new_queries": [{"query": "  Slang   History ", "direction": "languages", "why": "соседняя подниша"}],
    }, ensure_ascii=False), encoding="utf-8")
    names, added, problems = import_from_claude(store, answer, cfg, iso(DAY1))
    assert (names, added, len(problems)) == (1, 1, 1)
    assert "вердикт" not in write_outputs(rescore(store, cfg, DAY1), cfg, tmp_path / "r").read_text(encoding="utf-8").lower()
    q = next(q for q in store.queries() if q["query"] == "slang history")
    assert (q["origin"], q["status"], q["priority"]) == ("claude", "candidate", cfg["claude"]["import_priority"])
    renamed = cluster_with(rescore(store, cfg, DAY1), "word origins")
    assert (renamed.name, renamed.verdict) == ("Происхождение слов", "go")


def test_other_market_videos_are_out_of_every_signal(day1):
    _, _, _, result, _ = day1
    assert not any(result.videos[f"ind{i}"].market_ok for i in range(3))
    assert not result.channels["INDX"].indicator and result.channels["INDX"].market_share == 0
    assert not any(result.videos[f"ind{i}"].outlier for i in range(3))
    ety = niche(result, "etymology")
    assert not any(v.startswith("ind") for v in ety.video_ids) and ety.newcomers == 1
    assert all(not v.startswith("ind") for c in result.clusters for v in c.video_ids)
    assert not any("question paper" in c.phrase for c in result.new_candidates)


def test_search_order_alternates_between_repeats(day1, cfg):
    world, store, client1, _, _ = day1
    assert all(o == "viewCount" for _, o in client1.orders)          # first time: by views
    day4 = DAY1 + timedelta(days=4)                                   # past requery_days -> seeds due again
    meter = QuotaMeter(store, day4)
    client = FakeClient(world, meter)
    run(client, store, cfg, day4, meter)
    orders = dict(client.orders)
    assert orders["etymology"] == "relevance" and orders["game lore explained"] == "relevance"
    assert store.last_query_order("etymology", "medium") == "relevance"


def test_ranking_is_by_views_and_money_risk_are_informational(day1, cfg):
    _, _, _, result, _ = day1
    for n in result.niches + result.clusters:
        assert set(n.components) == {"demand", "newcomers", "crowding", "seriality"}
    views = [n.window_views for n in result.niches]
    assert views == sorted(views, reverse=True)                       # default order = views
    ety, lore = niche(result, "etymology"), niche(result, "game lore explained")
    assert ety.rpm_prior == cfg["rpm_by_category"]["27"] and lore.risk_share > 0  # shown, not multiplied
    assert "money" not in lore.components and "risk" not in lore.components
    assert lore.flag_counts.get("made_for_kids") == 1 and lore.kids_share > 0
    # LINGO (300 days, 60K) and KIDS (200 days, 10K) are not established; DEMON (150K subs) is
    assert ety.established == 0 and lore.established == 8


def test_hype_signals_are_computed(day1, cfg):
    _, _, _, result, _ = day1
    ety = niche(result, "etymology")
    assert 0 < ety.fresh_views < ety.window_views and ety.momentum is not None and ety.momentum > 0
    assert 0 < ety.top_video_share < 1
    lore = niche(result, "game lore explained")
    assert lore.demonetized_channels == 1                       # DEMON went silent
    assert lore.top_video_share == pytest.approx(500_000 / lore.window_views, rel=1e-3)  # one BIG video
    assert ety.fresh_views == sum(result.videos[v].views for v in ety.video_ids
                                  if result.videos[v].published >= DAY1 - timedelta(days=cfg["metrics"]["fresh_days"]))


def test_narrow_niche_indicator_passes_via_relative_rule(day1, cfg):
    _, _, _, result, _ = day1
    ch = result.channels["NARROW"]
    assert ch.best_views < cfg["indicator"]["min_best_views"] and (ch.growth_ratio or 0) < 5
    assert ch.indicator                                   # 45K views on a 2K-sub channel = 22× subs


def test_topic_channels_and_risky_sources_are_ignored(day1):
    _, _, _, result, _ = day1
    assert not result.channels["TOPIC"].indicator
    assert not any(result.videos[f"top{i}"].outlier for i in range(4))   # 600K views on 57 subs, but auto channel
    assert not result.videos["new_short"].outlier                         # 2M views, but a Short
    assert result.channels["RISK0"].risk_share == 1.0
    assert result.videos["risk0"].outlier            # still visible as an outlier in the report...
    assert "dating psychology" not in {c.phrase for c in result.new_candidates}  # ...but seeds nothing


def test_retention_deletes_statistics_older_than_30_days(day1, cfg):
    world, store, _, _, _ = day1
    before = store.conn.execute("SELECT count(*) FROM videos").fetchone()[0]
    later = DAY1 + timedelta(days=cfg["retention"]["days"] + 1)
    meter = QuotaMeter(store, later)
    result = run(FakeClient(world, meter), store, cfg, later, meter)
    cutoff = iso(later - timedelta(days=cfg["retention"]["days"]))
    assert store.conn.execute("SELECT count(*) FROM video_stats WHERE ts < ?", (cutoff,)).fetchone()[0] == 0
    assert store.conn.execute("SELECT count(*) FROM channel_stats WHERE ts < ?", (cutoff,)).fetchone()[0] == 0
    assert store.conn.execute("SELECT count(*) FROM videos WHERE updated_at < ?", (cutoff,)).fetchone()[0] == 0
    assert store.conn.execute("SELECT count(*) FROM videos").fetchone()[0] < before
    assert any(n.startswith("retention:") for n in result.notes)
    # nothing dangling: every hit/upload/related row still points at an existing entity
    assert store.conn.execute("SELECT count(*) FROM hits WHERE video_id NOT IN (SELECT video_id FROM videos)").fetchone()[0] == 0
    assert store.conn.execute("SELECT count(*) FROM related WHERE dst NOT IN (SELECT channel_id FROM channels)").fetchone()[0] == 0


# --- docs/VIDEO-COVERAGE.md: methods added after the full-transcript audit (11.10.2026) ---------------

def test_category_top_search_reaches_what_no_keyword_found(cfg, tmp_path):
    # В2: Viewstats «Top Videos → Long → категория → месяц»; FARM is in no query and no chart
    cfg["category_search"].update(category_ids=["28"], durations=["medium"])
    store = Store(tmp_path / "radar.sqlite")
    meter = QuotaMeter(store, DAY1)
    client = FakeClient(FakeWorld(DAY1), meter)
    result = run(client, store, cfg, DAY1, meter)
    assert (None, "viewCount") in client.orders                              # searched without a keyword
    top = next(n for n in result.niches if n.query == "category:28")
    assert top.origin == "category" and set(top.video_ids) == {f"farm{i}" for i in range(8)}
    # feeds the population the niches by meaning are built from (a one-channel group is not a niche itself)
    assert "farm0" in {v.video_id for v in window_videos(result.videos, cfg, DAY1)}
    assert store.queries(("category",))[0]["query"] == "category:28"          # not retired like a stale seed
    farm = result.channels["FARM"]
    assert farm.content_farm and farm.median_interval < 0.5                  # В1: «2–3 ролика в день»
    text = write_outputs(result, cfg, tmp_path / "reports").read_text(encoding="utf-8")
    assert "## 10a." in text and "Science & Technology (категория 28)" in text
    assert "конвейер ≥ 2 видео/день" in text
    # FARM is the only channel on its theme -> a one-channel topic (В4 «создал поднишу»), listed in §2c
    solo = next(n for n in result.solo_clusters if "farm0" in n.video_ids)
    assert solo.query.startswith("solo:") and solo.channels == 1
    assert not any("farm0" in c.video_ids for c in result.clusters)
    section_2c = text.split("## 2c.", 1)[1].split("\n## ", 1)[0]
    assert "Bridge Facts Daily" in section_2c
    assert (tmp_path / "reports" / "solo-2026-10-10.csv").exists()
    store.close()


def test_enrichment_serves_young_small_channels_first(cfg, tmp_path):
    # live 11.10: ordered by the best hit only, 184 of 267 young small channels never got their uploads
    cfg["enrich"]["max_channels"] = 3
    store = Store(tmp_path / "radar.sqlite")
    meter = QuotaMeter(store, DAY1)
    result = run(FakeClient(FakeWorld(DAY1), meter), store, cfg, DAY1, meter)
    fetched = {r["channel_id"] for r in store.all_rows("channels") if r["uploads_fetched_at"]}
    ind = cfg["indicator"]
    # 3 from the queue (+ related channels, which have their own budget, e.g. SMITH via NEW's featured list)
    assert {"ONE", "NEW"} <= fetched
    assert all(result.channels[c].age_days <= ind["max_age_days"] and result.channels[c].subs < ind["max_subs"]
               for c in fetched)
    assert "LINGO" not in fetched                 # 400K hit, but 300 days old: waits behind the young ones
    assert "TOPIC" not in fetched                 # auto "- Topic" channel: never an indicator
    store.close()


def test_silence_is_judged_only_when_the_upload_list_is_known(day1):
    _, _, _, result, _ = day1
    official = result.channels["OFFICIAL"]
    assert official.n_long == 5 and not official.uploads_known
    assert not official.possible_demonetized and official.days_since_last is None
    assert result.channels["DEMON"].uploads_known and result.channels["DEMON"].possible_demonetized


def test_every_window_video_lands_in_exactly_one_list(day1, cfg):
    # completeness: sub-niche, one-channel topic or the "outside any topic" list — nothing in the sample is lost
    _, _, _, result, tmp_path = day1
    write_outputs(result, cfg, tmp_path / "reports")
    population = {v.video_id for v in window_videos(result.videos, cfg, DAY1)}
    multi = [i for n in result.clusters for i in n.video_ids]
    solo = [i for n in result.solo_clusters for i in n.video_ids]
    with open(tmp_path / "reports" / "unclustered-2026-10-10.csv", encoding="utf-8-sig") as f:
        rest = [r["video_id"] for r in csv.DictReader(f)]
    listed = multi + solo + rest
    assert len(listed) == len(set(listed))            # no video in two lists
    assert set(listed) == population                  # and none missing


def test_author_ai_label_becomes_a_flag(day1):
    _, _, _, result, _ = day1
    assert "ai_label" in result.videos["sp2"].flags and "ai_label" not in result.videos["sp1"].flags


def test_cards_list_all_competitors_ideas_tags_and_trends_link(day1, cfg):
    _, _, _, result, tmp_path = day1
    text = write_outputs(result, cfg, tmp_path / "reports").read_text(encoding="utf-8")
    assert "## 3a. Карточки подниш" in text and "trends.google.com/trends/explore" in text
    assert "Топ-5 видео" in text and "Частые теги" in text and "восточное время США" in text
    with open(tmp_path / "reports" / "competitors-2026-10-10.csv", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    new = [r for r in rows if r["channel_id"] == "NEW"]
    assert new and {r["role"] for r in new} == {"новичок-индикатор"}
    assert all(int(r["views_in_niche"]) > 0 for r in rows)


def test_channel_growth_per_day_and_evergreen_share_after_two_snapshots(day1, cfg):
    world, store, _, _, _ = day1
    world.grow(1.1)
    world.channels["NEW"]["statistics"]["viewCount"] = str(1 + 5_000_000)  # +5M channel views in a day
    day2 = DAY1 + timedelta(days=1)
    meter = QuotaMeter(store, day2)
    result = run(FakeClient(world, meter), store, cfg, day2, meter)
    new = result.channels["NEW"]
    assert new.views_per_day == pytest.approx(5_000_000) and new.uploads_known
    recent = sum(v.views_per_day for v in result.videos.values()
                 if v.channel_id == "NEW" and v.published > day2 - timedelta(days=30))
    assert new.evergreen_pair == (pytest.approx(5_000_000), pytest.approx(recent))
    ety = niche(result, "etymology")
    assert ety.evergreen_share is not None and 0 <= ety.evergreen_share <= 1
    assert ety.median_minutes == pytest.approx(12)


def test_claude_answer_saved_with_bom_and_broken_json(day1, cfg):
    _, store, _, _, tmp_path = day1
    bom = tmp_path / "bom.json"  # Notepad / PowerShell 5.1 `Set-Content -Encoding utf8` write a BOM
    bom.write_bytes(b"\xef\xbb\xbf" + json.dumps({"new_queries": [{"query": "roman roads"}]}).encode())
    assert import_from_claude(store, bom, cfg, iso(DAY1))[1] == 1
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(SystemExit, match="не JSON"):
        import_from_claude(store, broken, cfg, iso(DAY1))
