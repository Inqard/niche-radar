from datetime import datetime, timedelta, timezone

import pytest

from niche_radar import metrics as m

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize("value,expected", [
    ("PT12M", 720), ("PT1H2M3S", 3723), ("P1DT2H", 93600), ("PT45S", 45), (None, None), ("bogus", None),
])
def test_parse_iso_duration(value, expected):
    assert m.parse_iso_duration(value) == expected


def test_seriality_counts_videos_near_the_best():
    assert m.seriality([100, 30, 25, 10], 0.25) == pytest.approx(0.75)
    assert m.seriality([100, 50], 0.25) is None  # too few videos to judge


def test_top2_share_spots_one_hit_channels():
    assert m.top2_share([900_000, 2000, 2500, 3000, 1800]) > 0.99
    assert m.top2_share([10, 10, 10, 10]) == pytest.approx(0.5)


def test_growth_ratio_compares_latest_to_first_videos():
    views = [3000, 4000, 5000, 6000, 20000, 50000, 90000, 150000, 250000, 400000]
    assert m.growth_ratio(views) == pytest.approx(150000 / 5000)
    assert m.growth_ratio([1, 2, 3]) is None


def test_cadence_and_demonetization_signal():
    published = [NOW - timedelta(days=60 - 2 * i) for i in range(21)]  # every 2 days, last 20 days ago
    interval, since_last = m.cadence(published, NOW)
    assert interval == pytest.approx(2)
    assert since_last == pytest.approx(20)
    assert m.possible_demonetized(21, interval, since_last, 7, 14, 3)
    # a slow uploader with the same silence is normal
    assert not m.possible_demonetized(21, 10, since_last, 7, 14, 3)
    # a regular uploader that posted 5 days ago is fine
    assert not m.possible_demonetized(21, 2, 5, 7, 14, 3)


def test_template_score_and_numbered_series():
    titles = [f"This Rabbit Was Covered In Ticks {i}" for i in range(4)] + ["Something else entirely"]
    assert m.template_score(titles, 5) == pytest.approx(0.8)
    assert m.template_score(titles[:3], 5) is None
    assert m.numbered_share(["Crash #56", "Crash #57", "Part 2 of the story", "Plain title"]) == pytest.approx(0.75)


def test_keyword_flags_match_whole_words_only():
    kw = {"medical": ["cure"], "kids": ["for kids"], "rescue": ["rescue"]}
    assert m.keyword_flags("A natural CURE nobody tells you", kw) == {"medical"}
    assert m.keyword_flags("How to secure your server", kw) == set()  # 'cure' inside 'secure'
    assert m.keyword_flags("Songs for kids and rescue dogs", kw) == {"kids", "rescue"}


def test_substring_flags_and_rpm_prior():
    pats = {"patreon": ["patreon.com"], "sponsor": ["use code"]}
    assert m.substring_flags("Support me: https://www.Patreon.com/x", pats) == {"patreon"}
    mapping = {"27": 10.22, "default": 2.3}
    assert m.rpm_prior("27", mapping) == 10.22
    assert m.rpm_prior(None, mapping) == 2.3


def test_non_latin_letters_detect_scripts_but_not_emoji_or_accents():
    assert m.has_non_latin_letters("Class 5 EVS – Energy | ऊर्जा")             # Devanagari
    assert m.has_non_latin_letters("Real Desert Physics സഹാറ")                # Malayalam
    assert m.has_non_latin_letters("Как это работает")                         # Cyrillic
    assert not m.has_non_latin_letters("Pokémon Lore Explained 🔥 — Part 2")   # accents, emoji, dash
    # 11.10 audit: single symbols and CJK brackets are not a foreign-language title
    assert not m.has_non_latin_letters("Why π Is Everywhere | ΔT Explained")   # lone Greek letters
    assert not m.has_non_latin_letters("Delta Force Commando 💥 Сlassic movie")  # one Cyrillic look-alike
    assert not m.has_non_latin_letters("【gogh】 Focus Session | Course। Speak")  # CJK brackets, danda


def test_is_english():
    assert m.is_english("en-US") and m.is_english(None)
    assert not m.is_english("es")
