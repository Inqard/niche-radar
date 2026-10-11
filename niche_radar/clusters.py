"""Sub-niches by meaning: lexical clustering of window videos (docs/RADAR-SPEC.md, v2).

Terms = tag phrases + title 1..n-grams (no stop/clickbait words), weighted by TF-IDF.
Leader clustering in a fixed order (views desc, id) + one centroid-merge pass, so the result
does not depend on hash seeds or input order.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

_TOKEN_RE = re.compile(r"[a-z0-9']+")
MAX_TAG_WORDS = 4
TOP_TERMS = 10


@dataclass(frozen=True)
class Doc:
    video_id: str
    channel_id: str
    views: int
    terms: frozenset[str]


@dataclass
class Cluster:
    key: str
    label: str
    terms: list[str]                       # most characteristic first
    video_ids: list[str]                   # views desc
    channels: int
    term_stats: dict[str, tuple[int, int]] = field(default_factory=dict)  # term -> (channels, views)


def _keep(words: list[str], stop: set[str], generic: set[str], max_words: int) -> bool:
    if not words or len(words) > max_words:
        return False
    if words[0] in stop or words[-1] in stop:
        return False
    if any(w.isdigit() or len(w) < 2 for w in words):
        return False
    return any(w not in stop and w not in generic for w in words)


def video_terms(title: str, tags: list[str], stop: set[str], generic: set[str], max_title_ngram: int) -> frozenset[str]:
    terms = {" ".join(w) for w in (_TOKEN_RE.findall(t.lower()) for t in tags)
             if _keep(w, stop, generic, MAX_TAG_WORDS)}
    words = _TOKEN_RE.findall(title.lower())
    for n in range(1, max_title_ngram + 1):
        for i in range(len(words) - n + 1):
            gram = words[i:i + n]
            if _keep(gram, stop, generic, max_title_ngram):
                terms.add(" ".join(gram))
    return frozenset(terms)


def _normalized(vec: dict[str, float]) -> dict[str, float]:
    norm = math.sqrt(sum(w * w for w in vec.values()))
    return {t: w / norm for t, w in vec.items()} if norm else {}


def _cos(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(w * b.get(t, 0.0) for t, w in a.items())


def _centroid(total: dict[str, float], keep: int) -> dict[str, float]:
    top = sorted(total.items(), key=lambda kv: (-kv[1], kv[0]))[:keep]
    return _normalized(dict(top))


def cluster_docs(docs: list[Doc], cfg: dict, generic: set[str] | frozenset = frozenset()) -> list[Cluster]:
    n = len(docs)
    if n == 0:
        return []
    df = Counter(t for d in docs for t in d.terms)
    max_df = max(3, int(cfg["max_df_share"] * n))
    idf = {t: math.log(n / c) + 1.0 for t, c in df.items() if 2 <= c <= max_df}
    vectors = {d.video_id: _normalized({t: idf[t] for t in d.terms if t in idf}) for d in docs}

    totals: list[dict[str, float]] = []
    centroids: list[dict[str, float]] = []
    members: list[list[Doc]] = []
    index: dict[str, set[int]] = {}
    for d in sorted(docs, key=lambda d: (-d.views, d.video_id)):
        vec = vectors[d.video_id]
        if not vec:
            continue
        best, best_sim = None, 0.0
        for i in sorted({i for t in vec for i in index.get(t, ())}):
            sim = _cos(vec, centroids[i])
            if sim > best_sim:
                best, best_sim = i, sim
        if best is not None and best_sim >= cfg["join_similarity"]:
            i = best
            for t, w in vec.items():
                totals[i][t] = totals[i].get(t, 0.0) + w
            members[i].append(d)
            centroids[i] = _centroid(totals[i], cfg["centroid_terms"])
        else:
            i = len(totals)
            totals.append(dict(vec))
            members.append([d])
            centroids.append(_centroid(vec, cfg["centroid_terms"]))
        for t in centroids[i]:
            index.setdefault(t, set()).add(i)

    # one merge pass over non-singleton clusters (union-find, fixed order)
    parent = list(range(len(totals)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    big = [i for i in range(len(totals)) if len(members[i]) >= 2]
    for a, i in enumerate(big):
        for j in big[a + 1:]:
            ri, rj = find(i), find(j)
            if ri != rj and _cos(centroids[i], centroids[j]) >= cfg["merge_similarity"]:
                parent[rj] = ri

    groups: dict[int, list[int]] = {}
    for i in range(len(totals)):
        groups.setdefault(find(i), []).append(i)

    clusters: list[Cluster] = []
    labels: Counter[str] = Counter()
    for root in sorted(groups):
        docs_in = [d for i in groups[root] for d in members[i]]
        channels = {d.channel_id for d in docs_in}
        if len(docs_in) < cfg["min_videos"] or len(channels) < cfg["min_channels"]:
            continue
        total: dict[str, float] = {}
        for i in groups[root]:
            for t, w in totals[i].items():
                total[t] = total.get(t, 0.0) + w
        terms = [t for t, _ in sorted(total.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_TERMS]]
        # name: a multi-word term without clickbait words ('game lore', not 'complete game')
        label = (next((t for t in terms[:5] if " " in t and not set(t.split()) & generic), None)
                 or next((t for t in terms[:5] if " " in t), terms[0]))
        labels[label] += 1
        key = label if labels[label] == 1 else f"{label} #{labels[label]}"
        stats = {}
        for t in terms:
            with_term = [d for d in docs_in if t in d.terms]
            stats[t] = (len({d.channel_id for d in with_term}), sum(d.views for d in with_term))
        ordered = sorted(docs_in, key=lambda d: (-d.views, d.video_id))
        clusters.append(Cluster(key, label, terms, [d.video_id for d in ordered], len(channels), stats))
    return clusters
