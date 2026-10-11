from datetime import datetime, timezone

from niche_radar.config import load_config
from niche_radar.db import Store, iso
from niche_radar.pipeline import plan_searches

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
