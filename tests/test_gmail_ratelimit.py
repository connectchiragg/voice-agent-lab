"""gmail_api pacing and rate-limit retries, against a fake Gmail (no network, no sleeping)."""
import asyncio
import logging

import httpx
import pytest

from app import gmail_api

TOKEN = "ya29.secret-test-token"


def google_error(code, reason):
    return {"error": {"code": code, "message": "Quota exceeded for quota metric 'Total Query Cost'",
                      "errors": [{"domain": "usageLimits", "reason": reason}]}}


class Clock:
    def __init__(self):
        self.now, self.slept = 0.0, []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(gmail_api, "_pacer", gmail_api.Pacer(1e9, clock=clock, sleep=clock.sleep))
    monkeypatch.setattr(gmail_api.random, "uniform", lambda a, b: b)  # The most jitter.
    return clock


def fetch(responses, kind="get"):
    """messages.get against a Gmail that answers `responses` in turn; -> (result or error, calls)."""
    calls = []

    def handler(request):
        calls.append(request)
        return responses[min(len(calls), len(responses)) - 1]
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        return asyncio.run(gmail_api.message(http, TOKEN, "m1")), calls
    except Exception as error:  # noqa: BLE001
        return error, calls


def test_403_rate_limit_exceeded_is_retried_with_exponential_backoff(clock, caplog):
    caplog.set_level(logging.INFO, "valr")
    limited = httpx.Response(403, json=google_error(403, "rateLimitExceeded"))
    result, calls = fetch([limited, limited, httpx.Response(200, json={"id": "m1"})])
    assert result == {"id": "m1"} and len(calls) == 3
    assert clock.slept == [5.0, 10.0]  # 4 s and 8 s, plus the most jitter (25%).
    assert "rateLimitExceeded" in caplog.text and TOKEN not in caplog.text


def test_429_and_user_rate_limit_are_retried_and_retry_after_is_honoured(clock):
    result, calls = fetch([httpx.Response(429, headers={"Retry-After": "7"}),
                           httpx.Response(403, json=google_error(403, "userRateLimitExceeded")),
                           httpx.Response(200, json={"id": "m1"})])
    assert result == {"id": "m1"} and len(calls) == 3 and clock.slept == [7.0, 10.0]


def test_a_lasting_rate_limit_raises_gmail_rate_limited_within_about_two_minutes(clock):
    error, calls = fetch([httpx.Response(429)])
    assert isinstance(error, gmail_api.GmailRateLimited) and error.reason == "429"
    assert len(calls) == gmail_api.MAX_TRIES == 5
    assert clock.slept == [5.0, 10.0, 20.0, 40.0] and sum(clock.slept) <= gmail_api.RETRY_BUDGET_S
    # A long Retry-After stops early instead of blowing the budget.
    clock.slept.clear()
    error, calls = fetch([httpx.Response(429, headers={"Retry-After": "50"})])
    assert isinstance(error, gmail_api.GmailRateLimited) and error.retry_after == 50
    assert [t for t in clock.slept if t > 0.001] == [50.0, 50.0] and len(calls) == 3


def test_other_403s_are_not_retried(clock, caplog):
    caplog.set_level(logging.INFO, "valr")
    error, calls = fetch([httpx.Response(403, json=google_error(403, "insufficientPermissions"))])
    assert isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 403
    assert len(calls) == 1 and clock.slept == []
    assert "insufficientPermissions" in caplog.text and TOKEN not in caplog.text
    assert not gmail_api.rate_limited(httpx.Response(403, text="not json"))
    assert gmail_api.rate_limited(httpx.Response(429, text="slow down"))


def test_pacing_math(monkeypatch):
    monkeypatch.setenv("VALR_GMAIL_RPS", "4")
    assert gmail_api.units_per_s() == 20  # 4 messages.get a second = 1200 units a minute.
    clock = Clock()
    pacer = gmail_api.Pacer(gmail_api.units_per_s(), clock=clock, sleep=clock.sleep)

    async def run():
        for _ in range(8):
            await pacer.take(gmail_api.UNITS["get"])
        await pacer.take(gmail_api.UNITS["history"])
    asyncio.run(run())
    assert clock.slept == [0.25] * 7 + [0.25]  # 8 gets in 2 s, then history.list waits its turn.
    assert pacer.next_at == pytest.approx(2.1)  # history.list (2 units) holds 0.1 s.
    # 150 messages.get (one run) take 37.5 s; 500 take 125 s.
    assert 150 * gmail_api.UNITS["get"] / gmail_api.units_per_s() == 37.5
    monkeypatch.setenv("VALR_GMAIL_RPS", "nonsense")
    assert gmail_api.units_per_s() == 20
    # Idle time is not banked: no burst after a pause.
    clock.now += 60
    asyncio.run(pacer.take(5))
    asyncio.run(pacer.take(5))
    assert clock.slept[-1] == 0.25


def test_list_and_history_calls_are_paced_too(clock):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path.endswith("/history"):
            return httpx.Response(200, json={"historyId": "9", "history": []})
        return httpx.Response(200, json={"messages": [{"id": "a"}]})
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gmail_api._pacer = gmail_api.Pacer(10, clock=clock, sleep=clock.sleep)
    asyncio.run(gmail_api.search(http, TOKEN, "q", 10))
    asyncio.run(gmail_api.history(http, TOKEN, 1))
    asyncio.run(gmail_api.search(http, TOKEN, "q", 10))
    assert clock.slept == pytest.approx([0.5, 0.2])  # list: 5 units at 10/s; then history: 2 units.


def test_history_reports_unread_label_changes_and_the_unread_list(clock):
    seen = []

    def handler(request):
        seen.append(request.url)
        if request.url.path.endswith("/history"):
            return httpx.Response(200, json={"historyId": "12", "history": [
                {"id": "10", "messagesAdded": [{"message": {"id": "new1", "labelIds": ["UNREAD", "INBOX"]}}]},
                {"id": "11", "labelsRemoved": [{"message": {"id": "a"}, "labelIds": ["UNREAD"]},
                                               {"message": {"id": "b"}, "labelIds": ["INBOX"]}]},
                {"id": "12", "labelsAdded": [{"message": {"id": "c"}, "labelIds": ["UNREAD"]}]}]})
        if request.url.params.get("pageToken"):
            return httpx.Response(200, json={"messages": [{"id": "u3"}, {"id": "u4"}]})
        return httpx.Response(200, json={"messages": [{"id": "u1"}, {"id": "u2"}], "nextPageToken": "p2"})
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gmail_api._pacer = gmail_api.Pacer(1e9, clock=clock, sleep=clock.sleep)
    changes = {}
    refs, latest = asyncio.run(gmail_api.history(http, TOKEN, 1, unread=changes))
    assert [r["id"] for r in refs] == ["new1"] and latest == 12
    assert changes == {"a": False, "c": True}
    assert seen[0].params.get_list("historyTypes") == ["messageAdded", "labelAdded", "labelRemoved"]
    ids, complete = asyncio.run(gmail_api.unread_ids(http, TOKEN, cap=10))
    assert ids == {"u1", "u2", "u3", "u4"} and complete
    assert seen[-1].params["q"] == "is:unread newer_than:30d"
    ids, complete = asyncio.run(gmail_api.unread_ids(http, TOKEN, cap=3))
    assert len(ids) == 3 and not complete  # Cut short: the caller reads nothing by absence.
