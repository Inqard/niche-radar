"""Pure metric functions. Rules and their sources: docs/RADAR-SPEC.md."""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from statistics import median

_DURATION_RE = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")
_NUMBERED_RE = re.compile(r"#\s?\d+|\b(?:part|pt|episode|ep)\.?\s?\d+\b", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z0-9']+")


def parse_iso_duration(value: str | None) -> int | None:
    """ISO 8601 duration ('PT1H2M3S', 'P1DT2H') -> seconds."""
    if not value:
        return None
    m = _DURATION_RE.match(value)
    if not m:
        return None
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


def days_between(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds() / 86400


def seriality(views: list[int], fraction: float) -> float | None:
    """Share of videos reaching `fraction` of the channel's best video (В4: «серийные просмотры»)."""
    if len(views) < 3 or max(views) <= 0:
        return None
    best = max(views)
    return sum(v >= fraction * best for v in views) / len(views)


def top2_share(views: list[int]) -> float | None:
    """Share of total views taken by the two best videos (В4: «ниша-однодневка»)."""
    total = sum(views)
    if len(views) < 2 or total <= 0:
        return None
    return sum(sorted(views, reverse=True)[:2]) / total


def growth_ratio(views_oldest_first: list[int]) -> float | None:
    """Median of the latest k videos / median of the first k, k = min(5, n // 2) (В2: аномальный рост)."""
    n = len(views_oldest_first)
    k = min(5, n // 2)
    if k < 2:
        return None
    first = median(views_oldest_first[:k])
    last = median(views_oldest_first[-k:])
    return last / max(first, 1)


def cadence(published: list[datetime], now: datetime) -> tuple[float | None, float | None]:
    """(median days between uploads, days since the last upload)."""
    if not published:
        return None, None
    ordered = sorted(published)
    gaps = [days_between(b, a) for a, b in zip(ordered, ordered[1:])]
    return (median(gaps) if gaps else None), days_between(now, ordered[-1])


def possible_demonetized(n_videos: int, median_interval: float | None, days_since_last: float | None,
                         max_median_interval: float, min_gap: float, multiplier: float) -> bool:
    """A regular uploader that suddenly went quiet (В1 23:19)."""
    if n_videos < 5 or median_interval is None or days_since_last is None:
        return False
    if median_interval > max_median_interval:
        return False
    return days_since_last >= max(min_gap, multiplier * median_interval)


def title_skeleton(title: str) -> str:
    words = _WORD_RE.findall(re.sub(r"\d+", "#", title.lower()))
    return " ".join(words[:3])


def template_score(titles: list[str], min_titles: int) -> float | None:
    """Share of titles sharing the most common 3-word skeleton (inauthentic-content risk)."""
    if len(titles) < min_titles:
        return None
    counts = Counter(title_skeleton(t) for t in titles)
    return counts.most_common(1)[0][1] / len(titles)


def numbered_share(titles: list[str]) -> float | None:
    if not titles:
        return None
    return sum(bool(_NUMBERED_RE.search(t)) for t in titles) / len(titles)


def _phrase_pattern(phrase: str) -> re.Pattern:
    return re.compile(r"(?<![a-z0-9])" + re.escape(phrase.lower()) + r"(?![a-z0-9])")


def keyword_flags(text: str, keywords: dict[str, list[str]]) -> set[str]:
    """Flag names whose phrases occur in text as whole words, case-insensitive."""
    low = text.lower()
    return {name for name, phrases in keywords.items()
            if any(_phrase_pattern(p).search(low) for p in phrases)}


def substring_flags(text: str, patterns: dict[str, list[str]]) -> set[str]:
    """Flag names whose patterns occur anywhere in text (URLs, promo phrases)."""
    low = text.lower()
    return {name for name, pats in patterns.items() if any(p.lower() in low for p in pats)}


def rpm_prior(category_id: str | None, mapping: dict[str, float]) -> float:
    return float(mapping.get(str(category_id), mapping["default"]))


# Greek…Indic…CJK, Hangul — letters only; CJK brackets 【】 (U+3000–303F) and the Devanagari danda
# (U+0964/0965) are punctuation and do not count
_NON_LATIN_RE = re.compile(r"(?:(?![　-〿।॥])[Ͱ-῿⺀-鿿가-힯]){3,}")


def has_non_latin_letters(text: str) -> bool:
    """A run of 3+ letters from a non-Latin script (Devanagari, Tamil, Arabic, Cyrillic, CJK…).
    A lone π/Ω in a physics title, a Cyrillic look-alike letter, emoji and accents do not count."""
    return bool(_NON_LATIN_RE.search(text))


def is_auto_channel(title: str | None) -> bool:
    """YouTube's auto-generated channels ('Artist - Topic'): not creators, never indicators/outliers."""
    return bool(title) and title.endswith(" - Topic")


def is_english(audio_lang: str | None) -> bool:
    """Unknown language counts as English: relevanceLanguage=en already biased the search."""
    return audio_lang is None or audio_lang.lower().startswith("en")
