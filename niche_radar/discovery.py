"""Discovery of adjacent sub-niches: phrases shared by successful videos of several channels.

A phrase becomes a candidate query only if >= `min_channels` different channels use it in tags
(one more for title-only n-grams); otherwise it is one author's brand, not a sub-niche.
Phrases made only of stop/generic words ('never knew') are dropped.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

_TOKEN_RE = re.compile(r"[a-z0-9']+")


@dataclass
class SourceVideo:
    video_id: str
    channel_id: str
    title: str
    tags: list[str]
    views: int
    query: str             # sub-niche through which the video was found
    direction: str
    parent_score: float    # score of that sub-niche (0 if not scored yet)


@dataclass
class Candidate:
    phrase: str
    direction: str
    parent: str
    channels: int
    views: int
    priority: float
    evidence: list[str] = field(default_factory=list)


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _ok_phrase(words: list[str], stop: set[str], min_words: int, max_words: int) -> bool:
    if not (min_words <= len(words) <= max_words):
        return False
    if words[0] in stop or words[-1] in stop:
        return False
    return not any(w.isdigit() or len(w) < 2 for w in words)


def phrases_of(video: SourceVideo, stop: set[str], generic: set[str],
               min_words: int, max_words: int) -> tuple[set[str], set[str]]:
    """(phrases from tags, phrases from title n-grams); all-generic phrases ('never knew') dropped."""
    def keep(words: list[str]) -> bool:
        return _ok_phrase(words, stop, min_words, max_words) and any(w not in stop | generic for w in words)

    from_tags = {" ".join(w) for w in map(_tokens, video.tags) if keep(w)}
    words = _tokens(video.title)
    from_title = {" ".join(words[i:i + n]) for n in range(min_words, max_words + 1)
                  for i in range(len(words) - n + 1) if keep(words[i:i + n])}
    return from_tags, from_title


def _nested(short: str, long: str) -> bool:
    """Whole-word containment: 'game lore' is nested in 'game lore explained'."""
    return f" {short} " in f" {long} "


def queryable(phrase: str, head_blocklist: set[str]) -> bool:
    """A search query must not start with a hype modifier: 'complete game', 'actually works'.
    Genre words are allowed heads ('weird animals', 'secret societies'); a generic tail is fine ('space facts')."""
    return phrase.split()[0] not in head_blocklist


def cluster_candidates(clusters: list[tuple[str, str, float, dict[str, tuple[int, int]]]],
                       existing: set[str], cfg: dict) -> list[Candidate]:
    """Top terms of scored sub-niche clusters -> candidates.

    `clusters`: (key, direction, score, {term: (channels, views)}) — the same channel and
    nesting rules as for phrases apply; priority uses the cluster score as the parent score.
    """
    existing_low = {q.lower() for q in existing}
    heads = set(cfg["query_head_blocklist"])
    out = []
    for key, direction, score, stats in clusters:
        for term, (n_channels, views) in stats.items():
            if (n_channels < cfg["min_channels"] or len(term.split()) < cfg["min_words"]
                    or not queryable(term, heads) or any(_nested(term, q) for q in existing_low)):
                continue
            priority = math.log10(1 + views) * math.sqrt(n_channels) * (1 + score / 10)
            out.append(Candidate(term, direction, f"cluster:{key}", n_channels, views, round(priority, 3)))
    return out


def find_candidates(videos: list[SourceVideo], existing: set[str], cfg: dict,
                    extra: list[Candidate] | tuple = ()) -> list[Candidate]:
    stop, generic = set(cfg["stopwords"]), set(cfg["generic_words"])
    heads = set(cfg["query_head_blocklist"])
    min_words, max_words = cfg["min_words"], cfg["max_words"]
    channels: dict[str, set[str]] = {}
    tagged: set[str] = set()
    views: Counter[str] = Counter()
    best: dict[str, SourceVideo] = {}
    parent_weight: dict[str, float] = {}
    evidence: dict[str, list[str]] = {}

    for v in videos:
        if v.views < cfg["min_views"]:
            continue
        from_tags, from_title = phrases_of(v, stop, generic, min_words, max_words)
        tagged |= from_tags
        for p in from_tags | from_title:
            channels.setdefault(p, set()).add(v.channel_id)
            views[p] += v.views
            evidence.setdefault(p, []).append(v.video_id)
            parent_weight[p] = max(parent_weight.get(p, 0.0), v.parent_score)
            if p not in best or v.views > best[p].views:
                best[p] = v

    existing_low = {q.lower() for q in existing}
    scored: list[Candidate] = []
    for p, chans in channels.items():
        # title-only n-grams are noisier than creator-chosen tags -> one more channel required
        need = cfg["min_channels"] + (0 if p in tagged else 1)
        # a fragment of an existing query adds nothing; a narrower phrase ('english etymology') does
        if len(chans) < need or not queryable(p, heads) or any(_nested(p, q) for q in existing_low):
            continue
        priority = math.log10(1 + views[p]) * math.sqrt(len(chans)) * (1 + parent_weight[p] / 10)
        src = best[p]
        scored.append(Candidate(p, src.direction, src.query, len(chans), views[p], round(priority, 3),
                                sorted(set(evidence[p]))[:5]))

    scored += list(extra)
    # equal priority -> the longer (narrower) phrase first; alphabetical keeps runs deterministic
    scored.sort(key=lambda c: (-c.priority, -len(c.phrase.split()), c.phrase))
    chosen: list[Candidate] = []
    per_direction: Counter[str] = Counter()
    for c in scored:
        # skip near-duplicates: one phrase nested inside an already chosen (higher-priority) one
        if any(_nested(c.phrase, k.phrase) or _nested(k.phrase, c.phrase) for k in chosen):
            continue
        # diversity: one booming theme must not take the whole exploration budget
        if per_direction[c.direction] >= cfg["max_new_per_direction"]:
            continue
        chosen.append(c)
        per_direction[c.direction] += 1
        if len(chosen) >= cfg["max_new_per_run"]:
            break
    return chosen
