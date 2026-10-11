import pytest
import requests

from niche_radar import api
from niche_radar.api import ApiError, QuotaExceeded, YouTubeClient

KEY = "SECRET-KEY-123"


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.headers, self.responses, self.calls = {}, list(responses), []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(api.time, "sleep", lambda s: None)


def make(responses):
    buckets = []
    session = FakeSession(responses)
    return YouTubeClient(KEY, on_call=buckets.append, session=session), session, buckets


def test_key_goes_in_header_not_url_and_calls_are_metered():
    client, session, buckets = make([FakeResponse(200, {"items": []})])
    client.search_videos("etymology", "2026-09-10T00:00:00Z", "medium", "US", "en")
    url, params = session.calls[0]
    assert session.headers["X-Goog-Api-Key"] == KEY
    assert KEY not in url and KEY not in str(params)
    assert buckets == ["search"]


def test_category_top_search_has_no_keyword_and_sets_the_category():
    # В2: Viewstats «Top Videos → Long → категория» = search.list with videoCategoryId and no q
    client, session, buckets = make([FakeResponse(200, {"items": []})])
    client.search_videos(None, "2026-09-10T00:00:00Z", "long", "US", "en", category_id="27")
    _, params = session.calls[0]
    assert "q" not in params and params["videoCategoryId"] == "27" and params["type"] == "video"
    assert params["order"] == "viewCount" and buckets == ["search"]


def test_quota_exceeded_raises():
    payload = {"error": {"errors": [{"reason": "quotaExceeded"}]}}
    client, _, buckets = make([FakeResponse(403, payload)])
    with pytest.raises(QuotaExceeded):
        client.videos(["a"])
    assert buckets == ["default"]


def test_retry_on_server_error_then_success():
    client, session, buckets = make([FakeResponse(503, {}), FakeResponse(200, {"items": [{"id": "a"}]})])
    assert client.videos(["a"]) == [{"id": "a"}]
    assert len(session.calls) == 2 and buckets == ["default", "default"]


def test_network_error_message_does_not_leak_key():
    err = requests.ConnectionError(f"https://www.googleapis.com/youtube/v3/videos?key={KEY}")
    client, _, _ = make([err] * 4)
    with pytest.raises(ApiError) as exc:
        client.videos(["a"])
    assert KEY not in str(exc.value) and exc.value.__cause__ is None


def test_private_subscriptions_return_empty_list_not_error():
    payload = {"error": {"errors": [{"reason": "subscriptionForbidden"}]}}
    client, _, buckets = make([FakeResponse(403, payload)])
    assert client.subscription_channel_ids("UCx") == []
    assert buckets == ["default"]


def test_subscriptions_quota_error_still_raises():
    payload = {"error": {"errors": [{"reason": "quotaExceeded"}]}}
    client, _, _ = make([FakeResponse(403, payload)])
    with pytest.raises(QuotaExceeded):
        client.subscription_channel_ids("UCx")


def test_featured_channels_only_from_multiple_channels_sections():
    items = [{"snippet": {"type": "multipleChannels"}, "contentDetails": {"channels": ["UC1", "UC2"]}},
             {"snippet": {"type": "singlePlaylist"}, "contentDetails": {"playlists": ["PL1"]}}]
    client, session, _ = make([FakeResponse(200, {"items": items})])
    assert client.featured_channel_ids("UCx") == ["UC1", "UC2"]
    assert session.calls[0][1]["channelId"] == "UCx"


def test_featured_channels_type_is_case_insensitive():
    # live API (11.10.2026) returns lowercase types, unlike the documentation
    items = [{"snippet": {"type": "multiplechannels"}, "contentDetails": {"channels": ["UC7"]}},
             {"snippet": {"type": "channelsectiontypeundefined"}}]
    client, _, _ = make([FakeResponse(200, {"items": items})])
    assert client.featured_channel_ids("UCx") == ["UC7"]


def test_channel_by_handle():
    client, session, _ = make([FakeResponse(200, {"items": [{"id": "UC9"}]}), FakeResponse(200, {})])
    assert client.channel_by_handle("@archaeo") == {"id": "UC9"}
    assert session.calls[0][1]["forHandle"] == "@archaeo"
    assert client.channel_by_handle("nobody") is None


def test_ids_are_chunked_by_50():
    client, session, buckets = make([FakeResponse(200, {"items": []})] * 3)
    client.videos([f"v{i}" for i in range(120)])
    assert [len(p["id"].split(",")) for _, p in session.calls] == [50, 50, 20]
    assert len(buckets) == 3
