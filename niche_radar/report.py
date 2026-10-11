"""Markdown report + CSV exports + JSON for Claude-side sub-niche naming.

Ranking rule (operator, 11.10.2026): views first — the money comes from views. RPM prior, money
signals outside ads and risk flags are shown beside every row as information, never as a verdict.
"""
from __future__ import annotations

import csv
from collections import Counter
from datetime import timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from .claude_io import export_for_claude
from .pipeline import CATEGORY_PREFIX, RunResult
from .scoring import Channel, Niche, Video, is_established, window_videos

try:  # В1 34:18: publish 2–3 h before competitors -> their hours in US Eastern time
    from zoneinfo import ZoneInfo
    US_EASTERN = ZoneInfo("America/New_York")
except Exception:  # no tz database (Windows without tzdata): EST, DST ignored
    US_EASTERN = timezone(timedelta(hours=-5))

CATEGORY_NAMES = {
    "1": "Film & Animation", "2": "Autos & Vehicles", "10": "Music", "15": "Pets & Animals", "17": "Sports",
    "19": "Travel & Events", "20": "Gaming", "22": "People & Blogs", "23": "Comedy", "24": "Entertainment",
    "25": "News & Politics", "26": "Howto & Style", "27": "Education", "28": "Science & Technology",
    "29": "Nonprofits & Activism",
}
CARDS_BY_VIEWS, CARDS_BY_HYPE, CARD_COMPETITORS = 12, 8, 15


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


def _label(n: Niche) -> str:
    if n.query.startswith(CATEGORY_PREFIX):
        cat = n.query[len(CATEGORY_PREFIX):]
        return f"{CATEGORY_NAMES.get(cat, cat)} (категория {cat})"
    return n.name or n.query


def _title(n: Niche) -> str:
    if n.kind == "cluster":
        head = f"**{_esc(n.name, 38)}** ({_esc(n.query, 28)})" if n.name else f"**{_esc(n.query, 40)}**"
        return head
    origin = "" if n.origin in ("seed", "category") else f" _({n.origin})_"
    return f"**{_esc(_label(n), 40)}**{origin}"


HEADER = ("| # | Подниша | Откуда | Просм. 30 дн | Просм. 14 дн | Импульс | Вечнозел. | Просм./сутки | Видео | "
          "Каналов | Новых (индикат.) | Закрепл. | Замолчали | Серийность | Мин. | Доля топ-видео | Окно (score) | "
          "RPM-ориентир $ | Деньги вне рекламы | Риски (доля видео) | Пример |")
SEP = "|" + "---|" * 21


def _rows(niches: list[Niche]) -> list[str]:
    rows = [HEADER, SEP]
    for i, n in enumerate(niches, 1):
        source = _esc(", ".join(n.sources), 40) if n.kind == "cluster" else n.direction
        speed = _num(n.views_per_day) + ("" if n.velocity_measured_share >= 0.5 else " (средн.)")
        momentum = "—" if n.momentum is None else f"{n.momentum:.1f}×"
        example = _vlink(n.top_videos[0]) if n.top_videos else "—"
        rows.append(
            f"| {i} | {_title(n)} | {source or 'похожие/импорт'} | {_num(n.window_views)} | {_num(n.fresh_views)} | "
            f"{momentum} | {_pct(n.evergreen_share)} | {speed} | {n.n_videos} | {n.channels} | {n.newcomers} | "
            f"{n.established} | {n.demonetized_channels or '—'} | {_pct(n.median_seriality)} | "
            f"{_num(n.median_minutes)} | {_pct(n.top_video_share)} | {n.score:.1f} | {n.rpm_prior:.1f} | "
            f"{_pct(n.money_share)} | {_risks(n)} | {example} |")
    if not niches:
        rows.append("| — |" + " |" * 20)
    return rows


def trends_url(term: str) -> str:
    """Google Trends, US, last 12 months (В1 18:00) — opened by a human, never fetched by the tool."""
    return f"https://trends.google.com/trends/explore?date=today%2012-m&geo=US&q={quote(term)}"


def _et_hours(videos: list[Video], top: int = 2) -> str:
    hours = Counter(v.published.astimezone(US_EASTERN).hour for v in videos if v.published)
    if not hours:
        return "—"
    total = sum(hours.values())
    return ", ".join(f"{h:02d}:00–{h:02d}:59 ({c} из {total})" for h, c in
                     sorted(hours.items(), key=lambda kv: (-kv[1], kv[0]))[:top])


def _role(ch: Channel, cfg: dict) -> str:
    if ch.indicator:
        return "новичок-индикатор"
    return "закрепившийся" if is_established(ch, cfg["niche"]) else "—"


def _competitors(n: Niche, result: RunResult) -> list[tuple[Channel, int, int]]:
    """(channel, its views inside the sub-niche, its videos inside the sub-niche), biggest first."""
    views, count = Counter(), Counter()
    for vid in n.video_ids:
        v = result.videos.get(vid)
        if v:
            views[v.channel_id] += v.views
            count[v.channel_id] += 1
    return [(result.channels[c], views[c], count[c]) for c, _ in
            sorted(views.items(), key=lambda kv: (-kv[1], kv[0])) if c in result.channels]


def _card(n: Niche, result: RunResult, cfg: dict) -> list[str]:
    """В1: «собрать всех конкурентов в таблицу», «улучшить 5 стреляющих идей», теги и время у конкурентов."""
    members = [result.videos[i] for i in n.video_ids if i in result.videos]
    comps = _competitors(n, result)
    top_share = comps[0][1] / n.window_views if comps and n.window_views else None
    farms = sum(ch.content_farm for ch, _, _ in comps)
    out = [f"### {_esc(_label(n), 60)} — {_num(n.window_views)} просмотров за 30 дн", "",
           f"- Видео {n.n_videos}, каналов {n.channels} (закрепившихся {n.established}, новичков-индикаторов "
           f"{n.newcomers}), просмотров на канал {_num(n.window_views / max(n.channels, 1))}, доля топ-канала "
           f"{_pct(top_share)}, медиана {_num(n.median_minutes)} мин., импульс {n.momentum:.1f}×, "
           f"вечнозелёность {_pct(n.evergreen_share)}, каналов-конвейеров {farms or '—'}.",
           f"- Хайп за год: [Google Trends, США]({trends_url(_label(n) if n.name else n.query)}) — проверка вручную.",
           f"- Когда публикуют конкуренты (восточное время США): {_et_hours(members[:20])}.", "",
           "| Конкурент | Роль | Подписчики | Возраст, дн. | Видео на канале | Просм. в поднише | Видео в поднише | "
           "Просм. канала/сутки | Подписч./сутки | Мин./видео | Выпуск раз в, дн. | Последнее, дн. назад | Пометки |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for ch, views, cnt in comps[:CARD_COMPETITORS]:
        out.append(
            f"| [{_esc(ch.title, 32)}]({ch.url}) | {_role(ch, cfg)} | {_num(ch.subs)} | {_num(ch.age_days)} | "
            f"{_num(ch.video_count)} | {_num(views)} | {cnt} | {_num(ch.views_per_day)} | {_num(ch.subs_per_day)} | "
            f"{_num(ch.median_minutes)} | {_num(ch.median_interval, 1)} | {_num(ch.days_since_last)} | "
            f"{_channel_flags(ch)} |")
    if len(comps) > CARD_COMPETITORS:
        out.append(f"| … ещё {len(comps) - CARD_COMPETITORS} — в `competitors-*.csv` | | | | | | | | | | | | |")
    out += ["", "Топ-5 видео — идеи, которые улучшаем (В1: «улучшить 5 стреляющих идей»):", "",
            "| Видео | Канал | Просмотры | Возраст, дн. | Мин. | Флаги |", "|---|---|---|---|---|---|"]
    for v in members[:5]:
        ch = result.channels.get(v.channel_id)
        age = (result.now - v.published).total_seconds() / 86400 if v.published else None
        out.append(f"| {_vlink(v)} | {_esc(ch.title if ch else v.channel_id, 30)} | {_num(v.views)} | "
                   f"{_num(age)} | {_num((v.duration_s or 0) / 60)} | {', '.join(sorted(v.flags)) or '—'} |")
    tag_channels: dict[str, set[str]] = {}
    for v in members:
        for t in {t.strip().lower() for t in v.tags if t.strip()}:
            tag_channels.setdefault(t, set()).add(v.channel_id)
    tags = sorted(tag_channels.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:10]
    out += ["", "Частые теги (у скольких каналов): "
            + ("; ".join(f"`{_esc(t, 40)}` ({len(c)})" for t, c in tags) if tags else "—"), ""]
    return out


def _channel_flags(ch: Channel) -> str:
    flags = []
    if ch.possible_demonetized:
        flags.append("⚠ возможна демонетизация")
    if ch.content_farm:
        flags.append("конвейер ≥ 2 видео/день")
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


def _solo_section(result: RunResult, top_n: int) -> list[str]:
    """One-channel sub-niches: a theme with ≥ 3 videos and no competitor in our sample (В4 33:47: «он
    единственный закрепился — создал поднишу»; В1 34:03: «один конкурент, ниша хайпует — залетит»)."""
    rows = ["| # | Тема | Канал | Подписчики | Возраст, дн. | Просм. 30 дн | Просм. 14 дн | Видео | "
            "Просм./подписч. | Индикатор | Мин. | Пометки | Пример |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    ranked = sorted(result.solo_clusters, key=lambda n: (-n.window_views, n.query))
    for i, n in enumerate(ranked[:top_n], 1):
        ch = result.channels.get(result.videos[n.video_ids[0]].channel_id) if n.video_ids else None
        per_sub = n.window_views / ch.subs if ch and ch.subs else None
        example = _vlink(n.top_videos[0]) if n.top_videos else "—"
        rows.append(
            f"| {i} | **{_esc(n.query.removeprefix('solo:'), 34)}** | "
            f"{f'[{_esc(ch.title, 28)}]({ch.url})' if ch else '—'} | {_num(ch.subs if ch else None)} | "
            f"{_num(ch.age_days if ch else None)} | {_num(n.window_views)} | {_num(n.fresh_views)} | {n.n_videos} | "
            f"{_num(per_sub, 1)} | {'✅' if ch and ch.indicator else '—'} | {_num(n.median_minutes)} | "
            f"{_channel_flags(ch) if ch else '—'} | {example} |")
    if not ranked:
        rows.append("| — |" + " |" * 12)
    return rows


def unclustered(result: RunResult, cfg: dict) -> list[Video]:
    """Window videos in no sub-niche and no one-channel topic — one-offs; listed so nothing in the sample is lost."""
    covered = {i for n in result.clusters + result.solo_clusters for i in n.video_ids}
    return sorted((v for v in window_videos(result.videos, cfg, result.now) if v.video_id not in covered),
                  key=lambda v: (-v.views, v.video_id))


def _unclustered_section(result: RunResult, cfg: dict, top_n: int) -> list[str]:
    rows = ["| Видео | Канал | Подписчики | Просмотры | Просм./подписч. | Мин. | Возраст, дн. | Флаги |",
            "|---|---|---|---|---|---|---|---|"]
    for v in unclustered(result, cfg)[:top_n]:
        ch = result.channels.get(v.channel_id)
        age = (result.now - v.published).total_seconds() / 86400 if v.published else None
        rows.append(f"| {_vlink(v)} | {_esc(ch.title if ch else v.channel_id, 28)} | {_num(v.subs)} | "
                    f"{_num(v.views)} | {_num(v.views_per_sub, 1)} | {_num((v.duration_s or 0) / 60)} | {_num(age)} | "
                    f"{', '.join(sorted(v.flags)) or '—'} |")
    return rows


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
    niches = [n for n in result.niches if n.origin not in ("chart", "manual", "category")]
    trending = [n for n in result.niches if n.origin == "chart"]
    category_tops = [n for n in result.niches if n.origin == "category"]
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
        f"из оценки: {other_market}), каналов: {len(result.channels)}, подниш по смыслу: {len(clusters)} "
        f"(+ тем одного канала: {len(result.solo_clusters)}), "
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
        "- «Вечнозел.» = доля суточных просмотров каналов подниши, которую дают их видео старше 30 дней "
        "(по снимкам канала; только каналы с известными загрузками): высокая — старые ролики продолжают кормить "
        "(вечнозелёная тема, В1/В5), около 0 — тема живёт свежими роликами (хайп или событие, В4). "
        "«Мин.» — медиана длительности видео подниши (В1: длиннее конкурента — преимущество; В4/В5: больше рекламы).",
    ]
    if result.now.month in (10, 11, 12):
        out.append("- **IV квартал:** рекламодатели доигрывают годовые бюджеты — RPM сезонно выше, пик в конце "
                   "ноября и в декабре (В4 12:22; AIR 2026: бизнес +60%, кулинария +39%). На ранжирование не влияет.")
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
    out += ["", f"## 2c. Темы одного канала — топ-{top_n} по просмотрам (конкурентов в выборке нет)", "",
            "≥ 3 видео одного канала на общую тему, которую больше никто в нашей выборке не снимает. По В4 так "
            "выглядит канал, который «создал поднишу» и держит её один; по В1 одиночный простреливающий новичок — "
            "сигнал заходить. Проверить вручную: правда ли конкурентов нет (поиск YouTube по теме) или мы их "
            "пока не нашли.", ""] + _solo_section(result, top_n)
    out += ["", f"## 2d. Ролики вне тем — топ-{top_n} по просмотрам", "",
            "Видео окна, которые не сложились ни в поднишу, ни в тему одного канала: разовые хиты (часто крупных "
            "авторов) и редкие темы. Нишей это ещё не является, но показывает спрос на тему. Полный список — "
            "`unclustered-<дата>.csv`; вместе с поднишами и темами одного канала он покрывает всю выборку.", ""]
    out += _unclustered_section(result, cfg, top_n)
    out += ["", f"## 2a. Подниши по смыслу — топ-{top_n} по скорости (просмотров в сутки)", ""] + _rows(by_speed[:top_n])
    out += ["", f"## 3. Подниши по смыслу — топ-{top_n} по окну возможностей (мало закрепившихся, есть новые)", ""]
    out += _rows(by_window[:top_n])

    carded = list({n.query: n for n in by_views[:CARDS_BY_VIEWS] + by_hype[:CARDS_BY_HYPE]}.values())
    out += ["", f"## 3a. Карточки подниш: топ-{CARDS_BY_VIEWS} по просмотрам и топ-{CARDS_BY_HYPE} по импульсу", "",
            "Все конкуренты подниши в одной таблице (В1: «выписать всех конкурентов в таблицу»), их рост в сутки "
            "по нашим снимкам (как у vidIQ за 7 дней), топ-5 роликов для улучшения, теги и часы публикации. "
            "Полный список конкурентов всех подниш — `competitors-<дата>.csv`.", ""]
    for n in carded:
        out += _card(n, result, cfg)

    out += ["", f"## 4. Подниши-запросы — топ-{top_n} по просмотрам", ""] + _rows(niches[:top_n])

    out += ["", "## 5. По направлениям: лучшие запросы по просмотрам", ""]
    for direction in sorted({n.direction for n in niches}):
        best = [n for n in niches if n.direction == direction][:5]
        line = "; ".join(f"**{_esc(n.query, 40)}** ({_num(n.window_views)}, новых {n.newcomers}, "
                         f"закрепл. {n.established})" for n in best)
        out.append(f"- **{direction}:** {line}")

    out += ["", "## 6. Каналы-индикаторы (новые каналы с аномальным ростом)", "",
            "| Канал | Возраст, дн. | Видео | Подписчики | Лучшее видео | Рост | Просм./сутки | Подписч./сутки | "
            "Серийность | Мин./видео | Пометки | Подниши |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for ch in indicators[:50]:
        via = " _(через похожие)_" if ch.channel_id in related_dst else ""
        out.append(
            f"| [{_esc(ch.title, 35)}]({ch.url}){via} | {_num(ch.age_days)} | {_num(ch.video_count)} | {_num(ch.subs)} | "
            f"{_num(ch.best_views)} | {f'{ch.growth_ratio:.1f}×' if ch.growth_ratio else '—'} | "
            f"{_num(ch.views_per_day)} | {_num(ch.subs_per_day)} | "
            f"{_pct(ch.seriality)} | {_num(ch.median_minutes, 0)} | {_channel_flags(ch)} | "
            f"{_esc(', '.join(dict.fromkeys(channel_niches.get(ch.channel_id, []))), 60) or '—'} |")
    if not indicators:
        out.append("| — | | | | | | | | | | | |")

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
    out += ["", "## 10a. Топ месяца по категориям без ключевых слов (В2: Viewstats «Top Videos → Long → категория»)",
            "", "Лучшие длинные видео категории за 30 дней в США — без наших ключей, поэтому видно то, о чём мы не "
            "догадались спросить. Их видео уже участвуют в поднишах по смыслу (раздел 1) и в автопоиске.", ""]
    out += _rows(sorted(category_tops, key=lambda n: (-n.window_views, n.query)))

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
            "money_share", "risk_share", "kids_share", "median_minutes", "evergreen_share"]
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
                 "money_share", "risk_share", "market_share", "indicator", "content_farm", "views_per_day",
                 "subs_per_day", "uploads_known"]
        w.writerow(ccols)
        for ch in result.channels.values():
            if ch.n_long:
                w.writerow([getattr(ch, c) for c in ccols])

    with open(out_dir / f"clusters-{stamp}.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["key", "name", "direction", "window_views", "fresh_views", "momentum", "views_per_day",
                    "median_views", "top_video_share", "n_videos", "channels", "newcomers", "established",
                    "demonetized_channels", "median_seriality", "score", "rpm_prior", "money_share",
                    "risk_share", "kids_share", "flags", "terms", "sources", "median_minutes", "evergreen_share"])
        for n in result.clusters:
            w.writerow([n.query, n.name, n.direction, n.window_views, n.fresh_views, n.momentum,
                        round(n.views_per_day), round(n.median_views), round(n.top_video_share, 3), n.n_videos,
                        n.channels, n.newcomers, n.established, n.demonetized_channels, n.median_seriality,
                        n.score, n.rpm_prior, round(n.money_share, 3), round(n.risk_share, 3),
                        round(n.kids_share, 3), "; ".join(f"{k}:{v}" for k, v in n.flag_counts.items()),
                        "; ".join(n.terms), "; ".join(n.sources), n.median_minutes, n.evergreen_share])

    with open(out_dir / f"solo-{stamp}.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["key", "channel_id", "channel", "subs", "age_days", "window_views", "fresh_views", "n_videos",
                    "indicator", "median_minutes", "terms", "top_video"])
        for n in sorted(result.solo_clusters, key=lambda n: (-n.window_views, n.query)):
            v = result.videos.get(n.video_ids[0]) if n.video_ids else None
            ch = result.channels.get(v.channel_id) if v else None
            w.writerow([n.query, v.channel_id if v else "", ch.title if ch else "", ch.subs if ch else "",
                        ch.age_days if ch else "", n.window_views, n.fresh_views, n.n_videos,
                        ch.indicator if ch else "", n.median_minutes, "; ".join(n.terms), v.url if v else ""])

    with open(out_dir / f"unclustered-{stamp}.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["video_id", "title", "channel_id", "channel", "subs", "views", "views_per_sub", "minutes",
                    "published_at", "flags", "url"])
        for v in unclustered(result, cfg):
            ch = result.channels.get(v.channel_id)
            w.writerow([v.video_id, v.title, v.channel_id, ch.title if ch else "", v.subs, v.views, v.views_per_sub,
                        round((v.duration_s or 0) / 60, 1), v.published.isoformat() if v.published else "",
                        "; ".join(sorted(v.flags)), v.url])

    # В1: «собрать всех конкурентов, выписать в таблицу» — every sub-niche by meaning, every channel in it
    with open(out_dir / f"competitors-{stamp}.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["niche_key", "niche_name", "channel_id", "title", "role", "subs", "age_days", "video_count",
                    "views_in_niche", "videos_in_niche", "channel_views_per_day", "subs_per_day", "median_minutes",
                    "median_interval_days", "days_since_last", "possible_demonetized", "content_farm", "url"])
        for n in result.clusters:
            for ch, views, cnt in _competitors(n, result):
                w.writerow([n.query, n.name, ch.channel_id, ch.title, _role(ch, cfg), ch.subs, ch.age_days,
                            ch.video_count, views, cnt, ch.views_per_day, ch.subs_per_day, ch.median_minutes,
                            ch.median_interval, ch.days_since_last, ch.possible_demonetized, ch.content_farm,
                            ch.url])

    export_for_claude(result.clusters, cfg, out_dir, stamp)
    return report
