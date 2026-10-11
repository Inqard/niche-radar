"""Thin YouTube Data API v3 client.

The key travels in the X-Goog-Api-Key header, so it never appears in URLs, exceptions or logs.
Every HTTP response is reported to `on_call(bucket)` for quota accounting: Google charges
failed requests too.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator

import requests

BASE_URL = "https://www.googleapis.com/youtube/v3/"
VIDEO_PARTS = "snippet,statistics,contentDetails,status,topicDetails,paidProductPlacementDetails"
QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"}
RETRY_STATUS = {429, 500, 502, 503, 504}


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None, reason: str = ""):
        super().__init__(message)
        self.status, self.reason = status, reason


class QuotaExceeded(ApiError):
    pass


def _chunks(items: list[str], size: int = 50) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _error_reason(resp: requests.Response) -> str:
    try:
        errors = resp.json()["error"]["errors"]
        return errors[0].get("reason", "") if errors else ""
    except (ValueError, KeyError, TypeError):
        return ""


class YouTubeClient:
    def __init__(
        self,
        api_key: str,
        on_call: Callable[[str], None] | None = None,
        session: requests.Session | None = None,
        timeout: float = 30,
        retries: int = 3,
    ):
        self._session = session or requests.Session()
        self._session.headers["X-Goog-Api-Key"] = api_key
        self._on_call = on_call or (lambda bucket: None)
        self._timeout = timeout
        self._retries = retries

    def _get(self, endpoint: str, params: dict, bucket: str = "default") -> dict:
        for attempt in range(self._retries + 1):
            try:
                resp = self._session.get(BASE_URL + endpoint, params=params, timeout=self._timeout)
            except requests.RequestException as e:
                if attempt == self._retries:
                    raise ApiError(f"{endpoint}: network error ({type(e).__name__})") from None
                time.sleep(2 ** attempt)
                continue
            self._on_call(bucket)
            if resp.status_code == 200:
                return resp.json()
            reason = _error_reason(resp)
            if resp.status_code == 403 and reason in QUOTA_REASONS:
                raise QuotaExceeded(f"{endpoint}: {reason}", resp.status_code, reason)
            if resp.status_code in RETRY_STATUS and attempt < self._retries:
                time.sleep(2 ** attempt)
                continue
            raise ApiError(f"{endpoint}: HTTP {resp.status_code} {reason}".rstrip(), resp.status_code, reason)
        raise ApiError(f"{endpoint}: retries exhausted")

    def search_videos(self, query: str | None, published_after: str, duration: str,
                      region: str, language: str, page_token: str | None = None,
                      order: str = "viewCount", category_id: str | None = None) -> dict:
        """Keyword search, or — with `category_id` and no query — the category's top videos of the
        window (В2: Viewstats «Top Videos → Long → страна + категория → за месяц»)."""
        params = {
            "part": "snippet", "type": "video", "order": order,
            "publishedAfter": published_after, "videoDuration": duration,
            "regionCode": region, "relevanceLanguage": language, "maxResults": 50,
        }
        if query:
            params["q"] = query
        if category_id:
            params["videoCategoryId"] = category_id
        if page_token:
            params["pageToken"] = page_token
        return self._get("search", params, bucket="search")

    def most_popular(self, region: str, category_id: str) -> list[dict]:
        params = {"part": VIDEO_PARTS, "chart": "mostPopular", "regionCode": region,
                  "videoCategoryId": category_id, "maxResults": 50}
        return self._get("videos", params).get("items", [])

    def videos(self, ids: Iterable[str]) -> list[dict]:
        items: list[dict] = []
        for chunk in _chunks(list(ids)):
            items += self._get("videos", {"part": VIDEO_PARTS, "id": ",".join(chunk)}).get("items", [])
        return items

    def channels(self, ids: Iterable[str]) -> list[dict]:
        items: list[dict] = []
        for chunk in _chunks(list(ids)):
            params = {"part": "snippet,statistics,contentDetails", "id": ",".join(chunk)}
            items += self._get("channels", params).get("items", [])
        return items

    def channel_by_handle(self, handle: str) -> dict | None:
        params = {"part": "snippet,statistics,contentDetails", "forHandle": handle}
        items = self._get("channels", params).get("items", [])
        return items[0] if items else None

    def featured_channel_ids(self, channel_id: str) -> list[str]:
        """Channels a creator recommends on the channel page (sections of type multipleChannels)."""
        params = {"part": "snippet,contentDetails", "channelId": channel_id}
        ids: list[str] = []
        for section in self._get("channelSections", params).get("items", []):
            # docs say 'multipleChannels', the live API returns 'multiplechannels' (seen 11.10.2026)
            if str(section.get("snippet", {}).get("type", "")).lower() == "multiplechannels":
                ids += section.get("contentDetails", {}).get("channels", [])
        return ids

    def subscription_channel_ids(self, channel_id: str) -> list[str]:
        """Public subscriptions of a creator (first 50); private ones -> [] instead of an error."""
        params = {"part": "snippet", "channelId": channel_id, "maxResults": 50}
        try:
            items = self._get("subscriptions", params).get("items", [])
        except QuotaExceeded:
            raise
        except ApiError as e:
            if e.status == 403 or e.reason in ("subscriptionForbidden", "accountClosed", "accountSuspended"):
                return []
            raise
        ids = (it.get("snippet", {}).get("resourceId", {}).get("channelId") for it in items)
        return [i for i in ids if i]

    def playlist_video_ids(self, playlist_id: str) -> list[str]:
        """Latest 50 uploads (one page is enough for the channels we enrich)."""
        params = {"part": "contentDetails", "playlistId": playlist_id, "maxResults": 50}
        items = self._get("playlistItems", params).get("items", [])
        return [it["contentDetails"]["videoId"] for it in items]
