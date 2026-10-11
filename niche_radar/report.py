"""Markdown report + CSV exports + JSON for Claude-side sub-niche naming.

Ranking rule (operator, 11.10.2026): views first — the money comes from views. RPM prior, money
signals outside ads and risk flags are shown beside every row as information, never as a verdict.
"""
from __future__ import annotations

import csv
from pathlib import Path

from .claude_io import export_for_claude
from .pipeline import RunResult
from .scoring import Channel, Niche, Video


def _esc(text: str, limit: int = 70) -> str:
    text = " ".join(text.split()).replace("|", "/")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _num(value: float | int | None, digits: int = 0) -> str:
    if value is None:
        return "—"
    if isinstance(value, float) and digits:
        return f"{value:.{digits}f}"
    value = round(value)
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.0f}K"
    return str(value)


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def _vlink(v: Video) -> str:
    return f"[{_esc(v.title, 50)}]({v.url})"


def _risks(n: Niche) -> str:
    parts = [f"{flag} {count / max(n.n_videos, 1):.0%}" for flag, count in n.flag_counts.items()
             if count / max(n.n_videos, 1) >= 0.1]
    return _esc(", ".join(parts), 40) or "—"


def _title(n: Niche) -> str:
    if n.kind == "cluster":
        head = f"**{_esc(n.name, 38)}** ({_esc(n.query, 28)})" if n.name else f"**{_esc(n.query, 40)}**"
        return head
    origin = "" if n.origin == "seed" else f" _({n.origin})_"
    return f"**{_esc(n.query, 40)}**{origin}"


HEADER = ("| # | Подниша | Откуда | Просм. 30 дн | Просм. 14 дн | Импульс | Просм./сутки | Видео | Каналов | "
          "Новых (индикат.) | Закрепл. | Замолчали | Серийность | Доля топ-видео | Окно (score) | "
          "RPM-ориентир $ | Деньги вне рекламы | Риски (доля видео) | Пример |")
SEP = "|" + "---|" * 19


def _rows(niches: list[Niche]) -> list[str]:
    rows = [HEADER, SEP]
    for i, n in enumerate(niches, 1):
        source = _esc(", ".join(n.sources), 40) if n.kind == "cluster" else n.direction
        speed = _num(n.views_per_day) + ("" if n.velocity_measured_share >= 0.5 else " (средн.)")
        momentum = "—" if n.momentum is None else f"{n.momentum:.1f}×"
        example = _vlink(n.top_videos[0]) if n.top_videos else "—"
        rows.append(
            f"| {i} | {_title(n)} | {source or 'похожие/импорт'} | {_num(n.window_views)} | {_num(n.fresh_views)} | "
            f"{momentum} | {speed} | {n.n_videos} | {n.channels} | {n.newcomers} | {n.established} | "
            f"{n.demonetized_channels or '—'} | {_pct(n.median_seriality)} | {_pct(n.top_video_share)} | "
            f"{n.score:.1f} | {n.rpm_prior:.1f} | {_pct(n.money_share)} | {_risks(n)} | {example} |")
    if not niches:
        rows.append("| — |" + " |" * 18)
    return rows


def _channel_flags(ch: Channel) -> str:
    flags = []
    if ch.possible_demonetized:
        flags.append("⚠ возможна демонетизация")
    if ch.template_score is not None and ch.template_score >= 0.5:
        flags.append(f"шаблон {ch.template_score:.0%}")
    if ch.numbered_share and ch.numbered_share >= 0.5:
        flags.append("нумерованная серия")
    if ch.risk_share and ch.risk_share >= 0.3:
        flags.append(f"рисковые флаги {ch.risk_share:.0%}")
    if ch.kids_share:
        flags.append(f"для детей {ch.kids_share:.0%}")
    if ch.money_share:
        flags.append(f"деньги вне рекламы {ch.money_share:.0%}")
    return ", ".join(flags) or "—"


def _related_section(result: RunResult) -> list[str]:
    by_dst: dict[str, dict] = {}
    for r in result.related:
        entry = by_dst.setdefault(r["dst"], {"kinds": set(), "src": set()})
        entry["kinds"].add("рекомендует" if r["kind"] == "featured" else "подписан")
        entry["src"].add(r["src"])
    chans = result.channels
    ranked = sorted((cid for cid in by_dst if cid in chans),
                    key=lambda cid: (not chans[cid].indicator, -chans[cid].best_views, cid))
    rows = ["| Канал | Индикатор | Подписчики | Возраст, дн. | Лучшее видео | Серийность | Связь | От кого |",
            "|---|---|---|---|---|---|---|---|"]
    for cid in ranked[:30]:
        ch, entry = chans[cid], by_dst[cid]
        sources = ", ".join(_esc(chans[s].title, 25) if s in chans else s for s in sorted(entry["src"]))
        rows.append(f"| [{_esc(ch.title, 35)}]({ch.url}) | {'✅' if ch.indicator else '—'} | {_num(ch.subs)} | "
                    f"{_num(ch.age_days)} | {_num(ch.best_views)} | {_pct(ch.seriality)} | "
                    f"{', '.join(sorted(entry['kinds']))} | {_esc(sources, 60)} |")
    if not ranked:
        rows.append("| — | | | | | | | |")
    return rows


def render_markdown(result: RunResult, cfg: dict, top_n: int = 25) -> str:
    # manual:* is a container of pasted links, not a sub-niche: shown apart, its videos still feed clusters
    niches = [n for n in result.niches if n.origin not in ("chart", "manual")]
    trending = [n for n in result.niches if n.origin == "chart"]
    manual = [n for n in result.niches if n.origin == "manual"]
    clusters = result.clusters
    channel_niches: dict[str, list[str]] = {}
    for n in niches:
        for cid in n.indicator_channels:
            channel_niches.setdefault(cid, []).append(n.query)
    for n in clusters:
        for cid in n.indicator_channels:
            channel_niches.setdefault(cid, []).append(n.name or n.query)
    related_dst = {r["dst"] for r in result.related}
    indicators = sorted((c for c in result.channels.values() if c.indicator),
                        key=lambda c: (-c.best_views, c.channel_id))
    outliers = sorted((v for v in result.videos.values() if v.outlier),
                      key=lambda v: (-(v.views_per_sub or 0), v.video_id))
    measured = sum(v.velocity_measured for v in result.videos.values())
    other_market = sum(not v.market_ok for v in result.videos.values())

    # В4: a group whose views sit in one video is a hit, not a niche — listed apart, never hidden
    one_hit_share = cfg["metrics"]["one_hit_share"]
    multi = [n for n in clusters if n.top_video_share <= one_hit_share]
    single = [n for n in clusters if n.top_video_share > one_hit_share]
    by_views = sorted(multi, key=lambda n: (-n.window_views, n.query))
    by_speed = sorted(multi, key=lambda n: (-n.views_per_day, n.query))
    by_window = sorted(multi, key=lambda n: (-n.score, n.query))
    min_fresh = cfg["metrics"]["hype_min_fresh_views"]
    by_hype = sorted((n for n in multi if n.fresh_views >= min_fresh), key=lambda n: (-n.momentum, n.query))
    by_single = sorted((n for n in single if n.fresh_views >= min_fresh), key=lambda n: (-n.fresh_views, n.query))

    out = [f"# Niche Radar — {result.now:%Y-%m-%d %H:%M} UTC", ""]
    out += [
        f"- Запуск #{result.run_id}: search.list {result.calls.get('search', 0)} из бюджета {result.search_budget}, "
        f"прочие вызовы API {result.calls.get('default', 0)}.",
        f"- Видео в базе: {len(result.videos)} (скорость по снимкам: {measured}; не для рынка США — исключены "
        f"из оценки: {other_market}), каналов: {len(result.channels)}, подниш по смыслу: {len(clusters)}, "
        f"подниш-запросов: {len(niches)}, трендовых категорий: {len(trending)}.",
        f"- Похожих каналов добавлено: {len(result.related_new)}; импортировано каналов вручную: "
        f"{len(result.imported_channels)}.",
        f"- Новых автонайденных подниш: {len(result.new_candidates)}; ушли в dormant: {len(result.dormant)}.",
        "- **Порядок = просмотры.** RPM-ориентир (медиана AIR 2026 по категории), деньги вне рекламы и риски — "
        "справочные колонки, в рейтинг не входят. «Окно (score)» = спрос × новые каналы × теснота × серийность — "
        "метод из разобранных гайдов, тоже справочно. Решение о входе — за оператором.",
        "- «Просм./сутки (средн.)» = просмотры / возраст видео; без пометки — измерено по ежедневным снимкам. "
        "«Импульс» = просмотры/сутки у видео последних 14 дней ÷ у видео 15–30 дней (>1 — ниша разгоняется). "
        "«Доля топ-видео» близко к 100% = ниша держится на одном ролике (В4: «однодневка»). «Замолчали» — "
        "каналы ниши, которые регулярно выкладывали и остановились (В1: признак демонетизации).",
    ]
    if result.notes:
        out += ["", "**Замечания запуска:**"] + [f"- {n}" for n in result.notes]

    out += ["", f"## 1. Подниши по смыслу — топ-{top_n} по просмотрам за 30 дней", "",
            "Видео сгруппированы по общим темам независимо от запроса, которым найдены. Имена — из разметки "
            "(`claude_prompt.md`), иначе — ведущий термин. Группы, где > "
            f"{one_hit_share:.0%} просмотров у одного ролика, вынесены в раздел 2b.", ""] + _rows(by_views[:top_n])
    out += ["", f"## 2. Хайп сейчас — топ-{top_n} по импульсу (≥ {_num(min_fresh)} просмотров за 14 дней)", "",
            "Что разгоняется прямо сейчас: видео последних двух недель набирают быстрее, чем видео предыдущих "
            "(В1: «последние 2 недели — сок»).", ""]
    out += _rows(by_hype[:top_n])
    out += ["", f"## 2b. Одиночные прострелы за 14 дней (один ролик даёт > {one_hit_share:.0%} просмотров группы)", "",
            "По В4 это ещё не ниша — но так начинаются новые ниши: смотреть, появятся ли у темы другие "
            "каналы и видео в следующих прогонах.", ""]
    out += _rows(by_single[:top_n])
    out += ["", f"## 2a. Подниши по смыслу — топ-{top_n} по скорости (просмотров в сутки)", ""] + _rows(by_speed[:top_n])
    out += ["", f"## 3. Подниши по смыслу — топ-{top_n} по окну возможностей (мало закрепившихся, есть новые)", ""]
    out += _rows(by_window[:top_n])

    out += ["", f"## 4. Подниши-запросы — топ-{top_n} по просмотрам", ""] + _rows(niches[:top_n])

    out += ["", "## 5. По направлениям: лучшие запросы по просмотрам", ""]
    for direction in sorted({n.direction for n in niches}):
        best = [n for n in niches if n.direction == direction][:5]
        line = "; ".join(f"**{_esc(n.query, 40)}** ({_num(n.window_views)}, новых {n.newcomers}, "
                         f"закрепл. {n.established})" for n in best)
        out.append(f"- **{direction}:** {line}")

    out += ["", "## 6. Каналы-индикаторы (новые каналы с аномальным ростом)", "",
            "| Канал | Возраст, дн. | Видео | Подписчики | Лучшее видео | Рост | Серийность | Мин./видео | Пометки | Подниши |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for ch in indicators[:50]:
        via = " _(через похожие)_" if ch.channel_id in related_dst else ""
        out.append(
            f"| [{_esc(ch.title, 35)}]({ch.url}){via} | {_num(ch.age_days)} | {_num(ch.video_count)} | {_num(ch.subs)} | "
            f"{_num(ch.best_views)} | {f'{ch.growth_ratio:.1f}×' if ch.growth_ratio else '—'} | "
            f"{_pct(ch.seriality)} | {_num(ch.median_minutes, 0)} | {_channel_flags(ch)} | "
            f"{_esc(', '.join(dict.fromkeys(channel_niches.get(ch.channel_id, []))), 60) or '—'} |")
    if not indicators:
        out.append("| — | | | | | | | | | |")

    out += ["", "## 7. Похожие каналы (рекомендованные на странице канала и публичные подписки)", ""]
    out += _related_section(result)

    out += ["", "## 8. Видео-выбросы (просмотры ≫ подписчики, длинные)", "",
            "| Видео | Просмотры | Просм./подписч. | Просм./сутки | Мин. | Флаги |", "|---|---|---|---|---|---|"]
    for v in outliers[:30]:
        speed = _num(v.views_per_day) + ("" if v.velocity_measured else " (средн.)")
        out.append(f"| {_vlink(v)} | {_num(v.views)} | {_num(v.views_per_sub, 1)} | {speed} | "
                   f"{_num((v.duration_s or 0) / 60)} | {', '.join(sorted(v.flags)) or '—'} |")

    out += ["", "## 9. Новые подниши, найденные автоматически (будут проверены в следующих запусках)", "",
            "| Фраза | Направление | Найдена через | Каналов | Просмотры источников | Приоритет |",
            "|---|---|---|---|---|---|"]
    for c in result.new_candidates:
        out.append(f"| **{c.phrase}** | {c.direction} | {_esc(c.parent, 40)} | {c.channels} | "
                   f"{_num(c.views)} | {c.priority:.2f} |")
    if not result.new_candidates:
        out.append("| — новых нет | | | | | |")

    out += ["", "## 10. За пределами интересов: тренды США по категориям (по просмотрам)", ""] + _rows(trending)

    if result.dormant:
        out += ["", "## 11. Ушли в dormant (слабый score несколько запусков подряд)", ""]
        out += [f"- {q}" for q in result.dormant]

    if manual:
        out += ["", "## 12. Импортировано вручную (ссылки из «похожих» и главной)", "",
                "Не подниши, а контейнеры ссылок; их видео и каналы уже учтены в кластерах (раздел 1) "
                "и стали источниками похожих каналов.", ""] + _rows(manual)
    return "\n".join(out) + "\n"


def write_outputs(result: RunResult, cfg: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{result.now:%Y-%m-%d}"
    report = out_dir / f"radar-{stamp}.md"
    report.write_text(render_markdown(result, cfg), encoding="utf-8")

    cols = ["query", "direction", "origin", "status", "parent", "window_views", "fresh_views", "momentum",
            "views_per_day", "median_views", "top_video_share", "n_videos", "channels", "newcomers",
            "established", "demonetized_channels", "median_seriality", "score", "rpm_prior",
            "money_share", "risk_share", "kids_share"]
    with open(out_dir / f"niches-{stamp}.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols + list(result.niches[0].components) if result.niches else cols)
        for n in result.niches:
            w.writerow([getattr(n, c) for c in cols] + list(n.components.values()))

    with open(out_dir / f"channels-{stamp}.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        ccols = ["channel_id", "title", "age_days", "subs", "video_count", "n_long", "best_views", "seriality",
                 "top2_share", "one_hit", "growth_ratio", "median_interval", "days_since_last",
                 "possible_demonetized", "template_score", "numbered_share", "median_minutes", "kids_share",
                 "money_share", "risk_share", "market_share", "indicator"]
        w.writerow(ccols)
        for ch in result.channels.values():
            if ch.n_long:
                w.writerow([getattr(ch, c) for c in ccols])

    with open(out_dir / f"clusters-{stamp}.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["key", "name", "direction", "window_views", "fresh_views", "momentum", "views_per_day",
                    "median_views", "top_video_share", "n_videos", "channels", "newcomers", "established",
                    "demonetized_channels", "median_seriality", "score", "rpm_prior", "money_share",
                    "risk_share", "kids_share", "flags", "terms", "sources"])
        for n in result.clusters:
            w.writerow([n.query, n.name, n.direction, n.window_views, n.fresh_views, n.momentum,
                        round(n.views_per_day), round(n.median_views), round(n.top_video_share, 3), n.n_videos,
                        n.channels, n.newcomers, n.established, n.demonetized_channels, n.median_seriality,
                        n.score, n.rpm_prior, round(n.money_share, 3), round(n.risk_share, 3),
                        round(n.kids_share, 3), "; ".join(f"{k}:{v}" for k, v in n.flag_counts.items()),
                        "; ".join(n.terms), "; ".join(n.sources)])

    export_for_claude(result.clusters, cfg, out_dir, stamp)
    return report
