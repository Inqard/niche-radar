"""CLI: python -m niche_radar {run,report,quota,add-query}."""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from .api import YouTubeClient
from .claude_io import import_from_claude
from .config import DEFAULT_CONFIG, DEFAULT_DATA_DIR, load_api_key, load_config
from .db import Store, iso, quota_day
from .links import parse_many
from .pipeline import QuotaMeter, rescore, run
from .report import write_outputs


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="niche_radar", description="Радар подниш YouTube")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--db", type=Path, default=DEFAULT_DATA_DIR / "radar.sqlite")
    p.add_argument("--out", type=Path, default=DEFAULT_DATA_DIR / "reports")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="поиск + обновление + скоринг + автопоиск подниш + отчёт (тратит квоту)")
    sub.add_parser("report", help="пересчитать отчёт из базы без вызовов API")
    sub.add_parser("quota", help="расход квоты за текущие сутки (Тихоокеанское время)")
    add = sub.add_parser("add-query", help="вручную добавить подниш-запрос (например, из «похожих видео»)")
    add.add_argument("query")
    add.add_argument("--direction", default="manual")
    imp = sub.add_parser("import-links", help="ссылки на видео/каналы из «похожих» и главной; обработка при run")
    imp.add_argument("links", nargs="*", help="watch?v=…, youtu.be/…, /shorts/…, /channel/UC…, @handle")
    imp.add_argument("--file", type=Path, help="файл со ссылками (по одной или через пробел)")
    imp.add_argument("--direction", default="manual")
    cla = sub.add_parser("import-claude", help="имена подниш и новые запросы из JSON-ответа Claude")
    cla.add_argument("path", type=Path)
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stdout)
    sys.stdout.reconfigure(encoding="utf-8")
    cfg = load_config(args.config)
    now = datetime.now(timezone.utc)
    store = Store(args.db)
    try:
        if args.cmd == "run":
            meter = QuotaMeter(store, now)
            client = YouTubeClient(load_api_key(), on_call=meter)
            result = run(client, store, cfg, now, meter)
        elif args.cmd == "report":
            result = rescore(store, cfg, now)
        elif args.cmd == "quota":
            day = quota_day(now)
            print(f"{day}: search.list {store.quota_used(day, 'search')}/{cfg['search']['daily_search_limit']}, "
                  f"остальные вызовы {store.quota_used(day, 'default')}/10000")
            return 0
        elif args.cmd == "import-links":
            lines = list(args.links)
            if args.file:
                lines += args.file.read_text(encoding="utf-8-sig").splitlines()
            links, rejected = parse_many(lines)
            added = sum(store.add_import(kind, value, args.direction, iso(now)) for kind, value in links)
            store.conn.commit()
            print(f"в очереди: {added} новых из {len(links)} распознанных; обработаются при следующем run")
            if rejected:
                print("не распознано:", ", ".join(rejected[:20]))
            return 0
        elif args.cmd == "import-claude":
            names, added, problems = import_from_claude(store, args.path, cfg, iso(now))
            print(f"имён подниш: {names}, новых запросов-кандидатов: {added}")
            for problem in problems:
                print("пропущено:", problem)
            return 0
        else:
            added = store.ensure_query(args.query.strip().lower(), args.direction, "manual", iso(now), "active")
            store.conn.commit()
            print("добавлено" if added else "уже есть в реестре")
            return 0
        report = write_outputs(result, cfg, args.out)
        print(f"Отчёт: {report}")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
