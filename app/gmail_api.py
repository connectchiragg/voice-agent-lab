"""The few Gmail API calls the lead tracker makes (gmail.readonly is enough for all of them).

- `profile`: the account and its current historyId.
- `watch`: push notifications for the mailbox to a Cloud Pub/Sub topic (users.watch). Google
  stops a watch after 7 days and recommends calling it again once a day; the response's
  historyId and expiration are stored.
- `history`: message ids added since a historyId (history.list, paged and capped), and,
  when asked (`unread`), which messages gained or lost the UNREAD label in that time (the
  labelAdded / labelRemoved history types): reading a mail in Gmail reads its bell item. A
  404 means that historyId is too old: `HistoryGone`, and the caller falls back to a short
  search.
- `search`: message ids for a query (messages.list), paged and capped; the backfill.
- `unread_ids`: the ids of unread mail of the last 30 days (one messages.list,
  "is:unread newer_than:30d"): which bell items are still unread.
- `message`: one message (format "full"), parsed into a lead_classify.Email. Its text stays
  in memory for the classifier; only metadata, the decision and a derived summary are kept.

Every call goes through `send`: a per-process pace (`Pacer`, VALR_GMAIL_RPS messages.get a
second, default 4: 20 quota units/s, 1200/min, far under Gmail's per-user limit; list,
history and profile calls are charged their own units) and, on a rate limit (429, or 403
with reason rateLimitExceeded / userRateLimitExceeded), exponential backoff with jitter,
honouring Retry-After, at most MAX_TRIES tries within RETRY_BUDGET_S, then `GmailRateLimited`.
Any other 403 (insufficientPermissions...) is raised at once. Only Google's reason is logged.
"""
from __future__ import annotations

import asyncio
import base64
import html as html_lib
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any, Awaitable, Callable

import httpx

from app.lead_classify import Email

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
PAGE = 100
HEADERS = ("from", "to", "cc", "subject", "date", "list-unsubscribe", "list-id", "precedence",
           "auto-submitted")


logger = logging.getLogger("valr")

# Gmail quota units per call (developers.google.com/gmail/api/reference/quota).
UNITS = {"profile": 1, "watch": 100, "stop": 50, "history": 2, "list": 5, "get": 5}
RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}
MAX_TRIES = 5
RETRY_BUDGET_S = 120.0
BACKOFF_BASE_S = 4.0
BACKOFF_CAP_S = 60.0


class HistoryGone(Exception):
    """history.list answered 404: the stored historyId is too old."""


class GmailRateLimited(Exception):
    """Gmail kept answering "rate limited" for MAX_TRIES tries (or RETRY_BUDGET_S)."""

    def __init__(self, reason: str, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.reason, self.retry_after = reason, retry_after


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class Pacer:
    """Spaces calls so they spend at most `units_per_s` quota units a second (no burst)."""

    def __init__(self, units_per_s: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep) -> None:
        self.units_per_s = max(0.1, units_per_s)
        self.clock, self.sleep = clock, sleep
        self.next_at = 0.0

    async def take(self, units: float) -> float:
        """Waits for this call's turn; -> the seconds waited."""
        now = self.clock()
        start = max(now, self.next_at)
        self.next_at = start + units / self.units_per_s
        if start > now:
            await self.sleep(start - now)
        return start - now


def units_per_s() -> float:
    """VALR_GMAIL_RPS messages.get a second (default 4), in quota units."""
    try:
        rps = float(os.getenv("VALR_GMAIL_RPS", "4"))
    except ValueError:
        rps = 4.0
    return max(0.1, rps) * UNITS["get"]


_pacer: Pacer | None = None


def pacer() -> Pacer:
    global _pacer
    if _pacer is None:
        _pacer = Pacer(units_per_s())
    return _pacer


def error_reason(response: httpx.Response) -> str:
    """Google's error reason (e.g. rateLimitExceeded), or the HTTP status."""
    try:
        error = response.json().get("error") or {}
    except ValueError:
        return str(response.status_code)
    if not isinstance(error, dict):
        return str(response.status_code)
    for item in [*(error.get("errors") or []), *(error.get("details") or [])]:
        if isinstance(item, dict) and item.get("reason"):
            return str(item["reason"])
    return str(error.get("status") or response.status_code)


def rate_limited(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    return response.status_code == 403 and error_reason(response) in RATE_REASONS


def retry_after(response: httpx.Response) -> float | None:
    try:
        return max(0.0, float(response.headers.get("retry-after", "")))
    except ValueError:
        return None  # Absent, or an HTTP date: use the backoff.


def backoff(attempt: int) -> float:
    """attempt 1, 2, 3...: 4, 8, 16, 32 s (capped at 60), plus up to 25% jitter."""
    delay = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** (attempt - 1))
    return delay + random.uniform(0, delay / 4)


async def send(http: httpx.AsyncClient, method: str, path: str, token: str, kind: str,
               **kwargs: Any) -> httpx.Response:
    """One Gmail call, paced; rate limits retried (see the module doc). Other statuses are
    returned as they are, for the caller to handle."""
    waited = 0.0
    for attempt in range(1, MAX_TRIES + 1):
        await pacer().take(UNITS.get(kind, 5))
        response = await http.request(method, f"{GMAIL}{path}", headers=auth(token), **kwargs)
        if not rate_limited(response):
            if response.status_code >= 400 and response.status_code != 404:
                logger.warning("VALR gmail: %s answered %d (%s)", kind, response.status_code,
                               error_reason(response))
            return response
        reason, after = error_reason(response), retry_after(response)
        delay = min(BACKOFF_CAP_S, after if after is not None else backoff(attempt))
        if attempt == MAX_TRIES or waited + delay > RETRY_BUDGET_S:
            logger.warning("VALR gmail: %s rate limited (%s) after %d tries; pausing", kind, reason, attempt)
            raise GmailRateLimited(reason, after)
        logger.info("VALR gmail: %s rate limited (%s); retry %d in %.1fs", kind, reason, attempt, delay)
        await pacer().sleep(delay)
        waited += delay
    raise AssertionError("unreachable")


async def profile(http: httpx.AsyncClient, token: str) -> dict[str, Any]:
    response = await send(http, "GET", "/profile", token, "profile")
    response.raise_for_status()
    return response.json()


async def watch(http: httpx.AsyncClient, token: str, topic: str) -> dict[str, Any]:
    """-> {historyId, expiration (ms since the epoch)}. All mail, no label filter: sent
    replies are touchpoints too; spam, drafts and chats are dropped by the classifier."""
    response = await send(http, "POST", "/watch", token, "watch", json={"topicName": topic})
    response.raise_for_status()
    return response.json()


async def stop(http: httpx.AsyncClient, token: str) -> None:
    response = await send(http, "POST", "/stop", token, "stop")
    if response.status_code not in (200, 204, 404):
        response.raise_for_status()


HISTORY_TYPES = ["messageAdded", "labelAdded", "labelRemoved"]
UNREAD = "UNREAD"


async def history(http: httpx.AsyncClient, token: str, start: int, max_pages: int = 10,
                  unread: dict[str, bool] | None = None) -> tuple[list[dict[str, Any]], int]:
    """-> (message refs {id, threadId, labelIds}, the newest historyId seen). `unread`, when
    given, is filled with {message id: True (marked unread) | False (read)} for every UNREAD
    label change, the last one winning."""
    params: dict[str, Any] = {"startHistoryId": str(start), "historyTypes": HISTORY_TYPES,
                              "maxResults": 500}
    refs: dict[str, dict[str, Any]] = {}
    latest = start
    for _ in range(max_pages):
        response = await send(http, "GET", "/history", token, "history", params=dict(params))
        if response.status_code == 404:
            raise HistoryGone()
        response.raise_for_status()
        body = response.json()
        latest = max(latest, int(body.get("historyId") or latest))
        for item in body.get("history") or []:
            for added in item.get("messagesAdded") or []:
                message = added.get("message") or {}
                if message.get("id"):
                    refs[message["id"]] = message
            if unread is not None:
                for key, value in (("labelsAdded", True), ("labelsRemoved", False)):
                    for change in item.get(key) or []:
                        message_id = (change.get("message") or {}).get("id")
                        if message_id and UNREAD in (change.get("labelIds") or []):
                            unread[message_id] = value
        if not body.get("nextPageToken"):
            break
        params["pageToken"] = body["nextPageToken"]
    return list(refs.values()), latest


async def search(http: httpx.AsyncClient, token: str, query: str, cap: int) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"q": query, "maxResults": min(PAGE, cap)}
    refs: list[dict[str, Any]] = []
    while len(refs) < cap:
        response = await send(http, "GET", "/messages", token, "list", params=dict(params))
        response.raise_for_status()
        body = response.json()
        refs += body.get("messages") or []
        if not body.get("nextPageToken"):
            break
        params["pageToken"] = body["nextPageToken"]
    return refs[:cap]


UNREAD_QUERY = "is:unread newer_than:30d"


async def unread_ids(http: httpx.AsyncClient, token: str, cap: int = 1000) -> tuple[set[str], bool]:
    """-> (ids of unread mail of the last 30 days, complete: False when the cap cut the list)."""
    refs = await search(http, token, UNREAD_QUERY, cap + 1)
    return {ref["id"] for ref in refs[:cap] if ref.get("id")}, len(refs) <= cap


async def message(http: httpx.AsyncClient, token: str, message_id: str) -> dict[str, Any] | None:
    response = await send(http, "GET", f"/messages/{message_id}", token, "get", params={"format": "full"})
    if response.status_code == 404:
        return None  # Deleted since the notification.
    response.raise_for_status()
    return response.json()


def decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


# An HTML-only LinkedIn message notification: its "View message" link is kept as text.
LINKEDIN_HREF = re.compile(r"""(?i)<a\b[^>]*\bhref=["'](https://(?:www\.)?linkedin\.com/(?:comm/)?messaging/thread/[^"']+)["'][^>]*>""")


def body_text(payload: dict[str, Any]) -> str:
    """The first text/plain part, else the first text/html part with tags removed."""
    stack, html = [payload], ""
    while stack:
        part = stack.pop(0)
        data = (part.get("body") or {}).get("data")
        mime = part.get("mimeType", "")
        if data and mime == "text/plain":
            return decode(data)
        if data and mime == "text/html" and not html:
            raw = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", decode(data))
            raw = LINKEDIN_HREF.sub(lambda m: f"\n{html_lib.unescape(m.group(1))}\n", raw)  # Keep the conversation link.
            html = re.sub(r"[ \t]+", " ", re.sub(r"<br\s*/?>|</p>|</div>", "\n", re.sub(r"<(?!br|/p|/div)[^>]+>", " ", raw)))
        stack.extend(part.get("parts") or [])
    return html


def parse(raw: dict[str, Any]) -> Email:
    payload = raw.get("payload") or {}
    headers = {}
    for item in payload.get("headers") or []:
        name = str(item.get("name", "")).lower()
        if name in HEADERS and name not in headers:
            headers[name] = str(item.get("value", ""))
    sender = getaddresses([headers.get("from", "")])
    from_name, from_email = sender[0] if sender else ("", "")
    to = [(n, a.lower()) for n, a in getaddresses([headers.get("to", ""), headers.get("cc", "")]) if a]
    try:
        at = datetime.fromtimestamp(int(raw["internalDate"]) / 1000, timezone.utc)
    except (KeyError, ValueError, TypeError):
        try:
            at = parsedate_to_datetime(headers.get("date", "")).astimezone(timezone.utc)
        except (TypeError, ValueError):
            at = datetime.now(timezone.utc)
    return Email(id=str(raw.get("id", "")), thread_id=str(raw.get("threadId", "")), at=at,
                 labels=list(raw.get("labelIds") or []), from_name=from_name.strip(),
                 from_email=from_email.lower(), to=to, subject=headers.get("subject", "")[:300],
                 headers=headers, text=body_text(payload)[:20000], snippet=str(raw.get("snippet", ""))[:300])


def thread_url(thread_id: str) -> str:
    return f"https://mail.google.com/mail/#all/{thread_id}"
