"""What the broadcast calls itself (Sprint 17 #27, S16 Track Y T17).

Every broadcast this stream has created was titled "Market World — live". The
title now states the premise and the model's ALL-TIME record, read from the
`signals` table — not `/world/state`, whose record is a 500-event window
(139–126 on 2026-09-26 against 6,357–6,170 all-time), and whose
`history.first_seen` is the window's start, not the stream's.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from broadcast import service
from broadcast.metadata import (
    CATEGORY_ID,
    DESCRIPTION_MAX,
    TAGS,
    TITLE_MAX,
    build_description,
    build_snippet,
    build_title,
)
from broadcast.youtube_client import YouTubeLiveClient

RECORD = {"wins": 6357, "losses": 6170, "since": date(2026, 7, 19), "horizon_bars": 15}
BASE = datetime(2026, 9, 26, 22, 0, tzinfo=timezone.utc)


# --- the strings -----------------------------------------------------------


def test_the_title_leads_with_the_premise_not_the_brand():
    title = build_title(RECORD)
    assert "Market World" not in title
    assert title.startswith("An AI")


def test_the_title_carries_the_all_time_record():
    title = build_title(RECORD)
    assert "6,357 right" in title and "6,170 wrong" in title


def test_a_losing_record_is_titled_the_same_way_not_hidden():
    # Truth over vanity: the losing case is not a different, kinder template.
    title = build_title({**RECORD, "wins": 10, "losses": 90})
    assert "10 right" in title and "90 wrong" in title


def test_the_title_fits_youtubes_limit_at_any_size():
    huge = {**RECORD, "wins": 10**12, "losses": 10**12}
    assert len(build_title(huge)) <= TITLE_MAX


def test_no_record_yet_does_not_invent_one():
    title = build_title({**RECORD, "wins": 0, "losses": 0})
    assert "0 right" not in title and "wrong" not in title
    assert title.startswith("An AI")


def test_the_description_states_the_record_rate_date_and_horizon():
    body = build_description(RECORD)
    assert "6,357 right" in body and "6,170 wrong" in body
    assert "50.7%" in body  # 6357 / 12527, one decimal, not rounded up to flatter
    assert "12,527 calls" in body
    assert "19 Jul 2026" in body
    assert "15 minutes" in body
    assert len(body) <= DESCRIPTION_MAX


def test_the_rate_is_truncated_never_rounded_up():
    # 2/3 = 66.67%: rounding would print 66.7, a flattering tenth.
    assert "(66.6%)" in build_description({**RECORD, "wins": 2, "losses": 1})


def test_the_description_says_calls_are_not_profit():
    # A hit rate is not P&L; the description may not let one pass for the other.
    assert "not" in build_description(RECORD).lower()
    assert "profit" in build_description(RECORD).lower()


def test_youtube_rejects_angle_brackets_so_none_are_emitted():
    for text in (build_title(RECORD), build_description(RECORD)):
        assert "<" not in text and ">" not in text


def test_a_missing_key_raises_rather_than_defaults():
    # A confident wrong figure in a public title is worse than a skipped
    # refresh, which the runner retries.
    with pytest.raises(KeyError):
        build_title({"wins": 1})


def test_the_snippet_keeps_fields_it_does_not_own_and_drops_read_only_ones():
    current = {
        "title": "Market World — live",
        "description": "",
        "categoryId": "20",
        "defaultLanguage": "en",
        "channelId": "UC-read-only",
        "thumbnails": {"default": {}},
        "liveBroadcastContent": "live",
    }
    snippet = build_snippet(RECORD, current)
    assert snippet["title"] == build_title(RECORD)
    assert snippet["description"] == build_description(RECORD)
    assert snippet["tags"] == TAGS
    assert snippet["categoryId"] == CATEGORY_ID
    assert snippet["defaultLanguage"] == "en"
    for read_only in ("channelId", "thumbnails", "liveBroadcastContent"):
        assert read_only not in snippet


def test_tags_fit_youtubes_500_character_budget():
    assert sum(len(t) + (2 if " " in t else 0) for t in TAGS) + len(TAGS) - 1 <= 500


# --- the client seam -------------------------------------------------------


class _Req:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _Videos:
    """Mirrors the real `videos()` resource's contract: `update(part="snippet")`
    REPLACES the snippet, and is refused without `title` and `categoryId`."""

    def __init__(self, snippet):
        self.snippet = snippet
        self.updates = []

    def list(self, part, id):
        assert part == "snippet"
        return _Req({"items": [{"id": id, "snippet": dict(self.snippet)}]})

    def update(self, part, body):
        assert part == "snippet"
        assert body["snippet"]["title"] and body["snippet"]["categoryId"], (
            "videos.update(part=snippet) requires title and categoryId"
        )
        self.updates.append(body)
        self.snippet = dict(body["snippet"])
        return _Req(body)


class _YT:
    def __init__(self, snippet):
        self._videos = _Videos(snippet)

    def videos(self):
        return self._videos


def _client(snippet):
    client = YouTubeLiveClient.__new__(YouTubeLiveClient)
    client._yt = _YT(snippet)
    return client


def test_the_client_reads_the_videos_snippet():
    client = _client({"title": "t", "categoryId": "20"})
    assert client.get_video_snippet("b1") == {"title": "t", "categoryId": "20"}


def test_the_client_writes_the_whole_snippet_in_one_update():
    client = _client({"title": "t", "categoryId": "20"})
    snippet = build_snippet(RECORD, client.get_video_snippet("b1"))
    client.update_video_snippet("b1", snippet)
    (body,) = client._yt._videos.updates
    assert body == {"id": "b1", "snippet": snippet}


def test_a_missing_video_reads_as_none():
    client = _client({})
    client._yt._videos.list = lambda part, id: _Req({"items": []})
    assert client.get_video_snippet("gone") is None


# --- the runner ------------------------------------------------------------


class FakeClient:
    def __init__(self, snippet=None):
        self.snippet = snippet if snippet is not None else {"title": "Market World — live", "categoryId": "20"}
        self.updates = []

    def get_video_snippet(self, video_id):
        return None if self.snippet is None else dict(self.snippet)

    def update_video_snippet(self, video_id, snippet):
        self.updates.append((video_id, snippet))
        self.snippet = dict(snippet)


def _refresher(client, record=RECORD, interval=21600):
    return service.MetadataRefresher(client, lambda: dict(record), interval)


def test_a_new_broadcast_is_titled_on_the_first_tick():
    client = FakeClient()
    _refresher(client).maybe_refresh("b1", BASE)
    assert client.updates == [("b1", build_snippet(RECORD, {"categoryId": "20"}))]


def test_no_broadcast_means_no_write():
    client = FakeClient()
    _refresher(client).maybe_refresh(None, BASE)
    assert client.updates == []


def test_the_record_is_not_rewritten_before_the_interval():
    # videos.update costs 50 quota units against the 10k/day the manager's
    # transitions also need. A title rewritten every tick would starve them.
    client = FakeClient()
    records = iter([RECORD, {**RECORD, "wins": RECORD["wins"] + 1}])
    r = service.MetadataRefresher(client, lambda: next(records), 21600)
    r.maybe_refresh("b1", BASE)
    r.maybe_refresh("b1", BASE + timedelta(hours=5, minutes=59))
    assert len(client.updates) == 1


def test_the_record_is_rewritten_after_the_interval():
    client = FakeClient()
    records = iter([RECORD, {**RECORD, "wins": RECORD["wins"] + 1}])
    r = service.MetadataRefresher(client, lambda: next(records), 21600)
    r.maybe_refresh("b1", BASE)
    r.maybe_refresh("b1", BASE + timedelta(hours=6))
    assert len(client.updates) == 2
    assert "6,358 right" in client.updates[-1][1]["title"]


def test_an_unchanged_snippet_is_not_rewritten():
    client = FakeClient()
    r = _refresher(client)
    r.maybe_refresh("b1", BASE)
    r.maybe_refresh("b1", BASE + timedelta(hours=7))
    assert len(client.updates) == 1


def test_a_new_broadcast_id_is_titled_immediately():
    client = FakeClient()
    r = _refresher(client)
    r.maybe_refresh("b1", BASE)
    r.maybe_refresh("b2", BASE + timedelta(minutes=1))
    assert [u[0] for u in client.updates] == ["b1", "b2"]


def test_a_failure_never_raises_and_is_retried_next_tick():
    class Flaky(FakeClient):
        fail = True

        def update_video_snippet(self, video_id, snippet):
            if self.fail:
                self.fail = False
                raise RuntimeError("quotaExceeded")
            super().update_video_snippet(video_id, snippet)

    client = Flaky()
    r = _refresher(client)
    r.maybe_refresh("b1", BASE)  # must not raise
    r.maybe_refresh("b1", BASE + timedelta(seconds=30))
    assert len(client.updates) == 1


def test_a_failing_record_read_writes_nothing():
    def broken():
        raise RuntimeError("postgres down")

    client = FakeClient()
    service.MetadataRefresher(client, broken, 21600).maybe_refresh("b1", BASE)
    assert client.updates == []


def test_metadata_is_off_unless_enabled(monkeypatch):
    # Outward-facing: the public title changes only when a human opted in.
    monkeypatch.delenv("BROADCAST_METADATA_ENABLED", raising=False)
    assert service.metadata_enabled() is False
    monkeypatch.setenv("BROADCAST_METADATA_ENABLED", "1")
    assert service.metadata_enabled() is True
