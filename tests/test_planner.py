from datetime import datetime, timedelta, timezone

from niche_radar.config import load_config
from niche_radar.db import Store, iso
from niche_radar.pipeline import plan_searches
from niche_radar.scoring import Niche, update_query_lifecycle

NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


def test_old_database_gets_last_order_column(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite"
    con = sqlite3.connect(path)  # schema as it was before 11.10 (no last_order)
    con.execute("CREATE TABLE query_runs (query TEXT, duration TEXT, last_run_at TEXT, PRIMARY KEY (query, duration))")
    con.execute("INSERT INTO query_runs VALUES ('etymology', 'medium', '2026-10-10T06:34:00Z')")
    con.commit()
    con.close()
    store = Store(path)
    assert store.last_query_order("etymology", "medium") is None
    store.mark_query_run("etymology", "medium", iso(NOW), "relevance")
    assert store.last_query_order("etymology", "medium") == "relevance"
    store.close()


def test_broad_map_queries_use_their_own_cadence_and_duration():
    cfg = load_config()
    cfg["directions"] = {"languages": ["etymology"]}
    cfg["explore_directions"] = {"history": ["ancient rome"]}
    cfg["search"]["durations"] = ["medium", "long"]
    cfg["schedule"].update(requery_days=4, explore_requery_days=10, explore_durations=["medium"])
    store = Store(":memory:")
    store.ensure_query("etymology", "languages", "seed", iso(NOW), "seed")
    store.ensure_query("ancient rome", "history", "seed", iso(NOW), "seed")
    assert sorted(plan_searches(store, cfg, NOW, budget=10)) == [
        ("ancient rome", "medium"), ("etymology", "long"), ("etymology", "medium")]
    for q, d in [("etymology", "medium"), ("etymology", "long"), ("ancient rome", "medium")]:
        store.mark_query_run(q, d, iso(NOW))
    later = NOW.replace(day=NOW.day + 5)                       # 5 days: interest due again, map query not yet
    assert sorted(plan_searches(store, cfg, later, budget=10)) == [("etymology", "long"), ("etymology", "medium")]


def test_category_tops_get_their_own_slot_among_never_searched_seeds():
    # live 12.10: 110 never-searched map seeds (staleness ∞) kept all 30 category tasks out of run #5
    cfg = load_config()
    cfg["directions"] = {}
    cfg["explore_directions"] = {"history": [f"topic {i}" for i in range(20)]}
    cfg["category_search"].update(category_ids=["27", "28", "2"], durations=["medium"], max_per_run=2)
    store = Store(":memory:")
    for i in range(20):
        store.ensure_query(f"topic {i}", "history", "seed", iso(NOW), "seed")
    for c in ("2", "27", "28"):          # registered after the seeds, like in run()
        store.ensure_query(f"category:{c}", f"trending:{c}", "category", iso(NOW), "category")
    tasks = plan_searches(store, cfg, NOW, budget=5)
    assert len(tasks) == 5 and sum(q.startswith("category:") for q, _ in tasks) == 2


def _niche(query: str, views: int, score: float) -> Niche:
    return Niche(query=query, direction="history", origin="cluster", status="active", parent=None, n_videos=10,
                 window_views=views, views_per_day=0.0, channels=5, established=9, newcomers=0,
                 median_seriality=None, rpm_prior=2.4, risk_share=0.0, money_share=0.0, outliers=0,
                 components={}, score=score, top_videos=[], indicator_channels=[])


def test_dormancy_is_decided_by_views_and_age_not_by_the_opportunity_score():
    # live 12.10: crowded but huge niches (true crime: score 0.20) went dormant after 3 runs within a day
    cfg = load_config()
    store = Store(":memory:")
    old, young = iso(NOW - timedelta(days=10)), iso(NOW - timedelta(days=1))
    for q, created in [("true crime", old), ("no demand", old), ("young weak", young), ("never found", old)]:
        store.ensure_query(q, "history", "cluster", created, "active")
    niches = [_niche("true crime", 30_000_000, 0.20), _niche("no demand", 50_000, 9.0),
              _niche("young weak", 50_000, 9.0)]                  # "never found": nothing in the window
    gone = []
    for k in range(3):                                            # three runs within hours
        gone += update_query_lifecycle(store, niches, cfg, NOW + timedelta(hours=k))
    assert sorted(gone) == ["never found", "no demand"]
    status = {q["query"]: q["status"] for q in store.queries()}
    assert status["true crime"] == "active" and status["young weak"] == "active"


def test_exploration_rotates_directions_instead_of_draining_one():
    cfg = load_config()
    cfg["directions"] = {}
    cfg["search"]["durations"] = ["medium"]
    store = Store(":memory:")
    for i in range(10):  # a booming theme with high priorities
        store.ensure_query(f"english topic {i}", "education", "discovered", iso(NOW), "candidate", priority=60 - i)
    store.ensure_query("deep sea creatures", "animals", "discovered", iso(NOW), "candidate", priority=20)
    store.ensure_query("ancient engineering", "tech_explainers", "discovered", iso(NOW), "candidate", priority=10)
    tasks = [q for q, _ in plan_searches(store, cfg, NOW, budget=10)]  # explore cap = 4 with share 0.4
    assert tasks[:3] == ["english topic 0", "deep sea creatures", "ancient engineering"]
    assert len(tasks) == 10 and tasks.count("deep sea creatures") == 1
