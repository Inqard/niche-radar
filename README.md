# Niche Radar

A free, open-source research tool for independent YouTube creators. It watches **publicly available**
YouTube data through the official YouTube Data API v3 and answers one question every day:
*which long-form topics are people watching right now, how crowded is each topic, and which new channels are growing in it?*

The creator reads a Markdown report and decides what to make. The tool makes no decisions, uploads nothing,
and never touches private data.

- Website & privacy policy: https://inqard.github.io/niche-radar/
- Source: https://github.com/Inqard/niche-radar
- License: MIT

## What it does

Once a day (`python -m niche_radar run`):

1. **Searches** recent long-form videos (4–20 min and >20 min, last 30 days, US region, English) for a configurable
   list of topic keywords — the creator's own directions plus a broad map of long-form YouTube areas
   (history, science, machines, true crime, nature, games, …), see `radar.toml`.
2. **Reads the "most popular" charts** by category for US, GB, CA and AU.
3. **Reads public metadata and statistics** of the videos and channels found (views, likes, comments, subscribers,
   publication dates, durations, tags, categories), and the latest uploads, public featured-channel sections and
   public subscriptions of small, fast-growing channels.
4. **Groups videos into topics** (shared tags and title phrases) and ranks topics by views in the last 30 days,
   by momentum (last 14 days vs the 16 before) and by an "opportunity" index (demand × new channels × crowding × seriality).
5. **Flags patterns worth knowing** before choosing a topic: templated or numbered series, content labelled
   "made for kids", channels that suddenly stopped uploading, medical/political/adult-bait keywords —
   so a creator can avoid producing mass-produced or unsuitable content.
6. Writes `data/reports/radar-<date>.md` plus CSV exports. Figures taken from YouTube are shown as returned by the API;
   the tool's own indices are labelled as such.

Discovery grows on its own: phrases shared by successful videos of several channels become new search keywords,
and channels recommended by (or publicly subscribed to by) growing channels are added to the graph.

## Compliance with YouTube API Services policies

- **Public data only.** No OAuth, no user accounts, no authorized data, no comments content. Only `list`
  methods and `chart=mostPopular`.
- **Official API only.** No scraping of youtube.com; the tool deliberately does not read "related videos"
  or the home page.
- **Retention ≤ 30 days.** Statistics snapshots older than 30 days and any video/channel record not refreshed
  within 30 days are deleted on every run (`[retention]` in `radar.toml`, `Store.prune()`).
- **No audiovisual content** is downloaded or stored. No data is sold, shared or published.
- **One project, one key.** The API key lives in `.env` (git-ignored). Each creator who runs the tool
  uses their own Google Cloud project and key.
- Branding and attribution requirements are met on the project website.

See [PRIVACY POLICY](https://inqard.github.io/niche-radar/privacy.html) and
[TERMS](https://inqard.github.io/niche-radar/terms.html).

## Install

Requirements: Python 3.11+, a Google Cloud project with **YouTube Data API v3** enabled and an API key
(APIs & Services → Credentials → Create credentials → API key; restrict it to YouTube Data API v3).

```bash
git clone https://github.com/Inqard/niche-radar.git
cd niche-radar
pip install -r requirements.txt
copy .env.example .env      # then put YOUTUBE_API_KEY=... inside
python -m pytest -q         # 79 offline tests, no key needed
python -u -m niche_radar run
```

Commands:

```bash
python -m niche_radar run                  # search + refresh + score + discovery + report (spends quota)
python -m niche_radar report               # rebuild the report from the local database, no API calls
python -m niche_radar quota                # quota used today (Pacific-time day, like Google's)
python -m niche_radar add-query "ancient engineering" --direction tech_explainers
python -m niche_radar import-links --direction languages https://youtu.be/... https://www.youtube.com/@handle
python -m niche_radar import-claude answer.json   # apply topic names from an assistant's JSON answer (optional)
```

`scripts/run_daily.ps1` is a Windows Task Scheduler runner (daily, after the quota reset).

## Configuration

Everything is in `radar.toml`: your own directions (`[directions]`), the broad topic map
(`[explore_directions]`), thresholds for indicator channels and sub-niche scoring, risk keyword lists,
retention and quota budgets. Every threshold is a starting hypothesis taken from creator-education videos;
the report shows all components so any ranking can be challenged.

## Quota

The default allocation (10,000 units and 100 `search.list` calls per day) is respected automatically:
the tool budgets `search.list` calls per run, rotates keywords (own directions every 4 days, the broad map
every 10 days) and refreshes statistics with cheap batched `videos.list` calls. With the default quota a full
pass over the topic map takes about ten days.

## Status

Built and used by an independent creator since October 2026. Issues and pull requests are welcome.
