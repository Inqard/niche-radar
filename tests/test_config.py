"""Invariants of radar.toml that would otherwise fail silently."""
from collections import Counter

from niche_radar.config import load_config

CFG = load_config()


ALL_SEEDS = {**CFG["directions"], **CFG.get("explore_directions", {})}


def test_no_query_in_two_directions():
    # a duplicate would silently belong to whichever direction is registered last
    queries = Counter(q.strip().lower() for qs in ALL_SEEDS.values() for q in qs)
    assert [q for q, n in queries.items() if n > 1] == []
    assert not set(CFG["directions"]) & set(CFG.get("explore_directions", {}))  # direction names unique too


def test_scored_risk_flags_have_keywords():
    known = set(CFG["risk_keywords"]) | {"made_for_kids"}
    assert set(CFG["niche"]["scored_risk_flags"]) <= known


def test_seed_cadence_fits_search_quota():
    # every seed × duration must be re-searched once per its requery_days with the non-exploration budget
    sc, sch = CFG["search"], CFG["schedule"]
    interests = sum(len(qs) for qs in CFG["directions"].values()) * len(sc["durations"]) / sch["requery_days"]
    explore = (sum(len(qs) for qs in CFG.get("explore_directions", {}).values())
               * len(sch["explore_durations"]) / sch["explore_requery_days"])
    cs = CFG["category_search"]  # В2 category tops share the same search bucket
    categories = len(cs["category_ids"]) * len(cs["durations"]) / cs["requery_days"]
    per_day = (interests + explore + categories) * sc["pages_per_query"]
    seed_budget = sc["max_calls_per_run"] * (1 - sch["explore_share"])
    assert per_day <= seed_budget, f"{per_day:.0f} search calls/day needed, {seed_budget:.0f} available"
    assert sc["max_calls_per_run"] <= sc["daily_search_limit"]
