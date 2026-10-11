"""Claude loop without a paid API (docs/RADAR-SPEC.md, v2).

export: clusters -> claude_input-<date>.json + claude_prompt.md (what to answer and in which format).
import: Claude's JSON answer -> names/verdicts for clusters + new candidate queries (origin=claude).
"""
from __future__ import annotations

import json
from pathlib import Path

from .db import Store

VERDICTS = {"go", "watch", "skip"}
MAX_QUERY_LEN = 80

PROMPT = """# Задача для Claude: разметить подниши радара

Вход: `{input_name}` — подниши-кластеры (термины, примеры названий, запросы-источники, метрики).
Контекст канала: длинные безликие видео на английском для США, автор живёт в Армении;
интересы и ограничения — `research/01-niche-research-from-2-videos.md` §11, риски — `research/03-niche-catalogs-30-50.md` §4.

Ответь ОДНИМ JSON-файлом такого вида и сохрани его (например, `data/reports/claude_answer.json`):

```json
{{
  "subniches": [
    {{"label": "<label из входа, без изменений>", "name": "<короткое имя подниши по-русски>",
      "note": "<факты в 1 строку: формат видео, кто аудитория, барьер входа, что из флагов реально>"}}
  ],
  "new_queries": [
    {{"query": "<англоязычный поисковый запрос соседней подниши, 2–5 слов>",
      "direction": "<направление из radar.toml или новое>", "why": "<1 строка>"}}
  ]
}}
```

Правила: `label` копировать точно (по нему имя привязывается к кластеру). **Вердиктов не ставить** — отбор
подниш идёт по просмотрам, решение о входе принимает оператор; в `note` только факты. В `new_queries` —
соседние подниши, которых во входе нет, особенно на пересечении интересов. Затем:
`python -m niche_radar import-claude data/reports/claude_answer.json`
"""


def export_for_claude(clusters: list, cfg: dict, out_dir: Path, stamp: str) -> Path:
    payload = [{
        "label": c.query.split(" #")[0],  # cluster key = label (+ " #N" for duplicate labels)
        "key": c.query,
        "direction": c.direction,
        "score": c.score,
        "components": c.components,
        "terms": c.terms,
        "sources": c.sources,
        "channels": c.channels,
        "established": c.established,
        "newcomers": c.newcomers,
        "window_views": c.window_views,
        "fresh_views_14d": c.fresh_views,
        "momentum": c.momentum,
        "top_video_share": round(c.top_video_share, 2),
        "demonetized_channels": c.demonetized_channels,
        "views_per_day": round(c.views_per_day),
        "rpm_prior": c.rpm_prior,
        "money_share": round(c.money_share, 2),
        "risk_share": round(c.risk_share, 2),
        "kids_share": round(c.kids_share, 2),
        "flags": c.flag_counts,
        "titles": [v.title for v in c.top_videos],
        "current_name": c.name,
    } for c in clusters[: cfg["claude"]["export_clusters"]]]
    path = out_dir / f"claude_input-{stamp}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "claude_prompt.md").write_text(PROMPT.format(input_name=path.name), encoding="utf-8")
    return path


def import_from_claude(store: Store, path: Path, cfg: dict, ts: str) -> tuple[int, int, list[str]]:
    """Returns (names applied, queries added, problems). Invalid entries are skipped, not fatal."""
    try:  # utf-8-sig: files saved by Notepad / PowerShell 5.1 start with a BOM
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"{path}: не JSON ({e.msg}, строка {e.lineno}, позиция {e.colno})") from None
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: ожидается объект {{\"subniches\": [...], \"new_queries\": [...]}}")
    problems: list[str] = []
    names = 0
    for item in data.get("subniches", []):
        label, name = str(item.get("label", "")).strip(), str(item.get("name", "")).strip()
        verdict = str(item.get("verdict", "")).strip().lower()  # optional; the report does not show it
        if not label or not name:
            problems.append(f"подниша без label/name: {item}")
            continue
        if verdict and verdict not in VERDICTS:
            problems.append(f"{label}: verdict '{verdict}' проигнорирован")
            verdict = ""
        store.set_cluster_name(label, name, verdict, str(item.get("note", "")).strip(), ts)
        names += 1
    added = 0
    for item in data.get("new_queries", []):
        query = " ".join(str(item.get("query", "")).lower().split())
        if not query or len(query) > MAX_QUERY_LEN:
            problems.append(f"запрос пропущен: {item}")
            continue
        direction = str(item.get("direction", "claude")).strip() or "claude"
        evidence = json.dumps({"why": item.get("why", "")}, ensure_ascii=False)
        if store.ensure_query(query, direction, "claude", ts, status="candidate", parent="claude",
                              evidence=evidence, priority=cfg["claude"]["import_priority"]):
            added += 1
    store.conn.commit()
    return names, added, problems
