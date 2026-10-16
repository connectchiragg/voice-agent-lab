"""The few Gmail API calls the lead tracker makes (gmail.readonly is enough for all of them).

- `profile`: the account and its current historyId.
- `watch`: push notifications for the mailbox to a Cloud Pub/Sub topic (users.watch). Google
  stops a watch after 7 days and recommends calling it again once a day; the response's
  historyId and expiration are stored.
- `history`: message ids added since a historyId (history.list, messageAdded only, paged
  and capped). A 404 means that historyId is too old: `HistoryGone`, and the caller falls
  back to a short search.
- `search`: message ids for a query (messages.list), paged and capped; the backfill.
- `message`: one message (format "full"), parsed into a lead_classify.Email. Its text stays
  in memory for the classifier; only metadata, the decision and a derived summary are kept.
"""
from __future__ import annotations

import base64
import re
from datetime import datetime, timezone
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

import httpx

from app.lead_classify import Email

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
PAGE = 100
HEADERS = ("from", "to", "cc", "subject", "date", "list-unsubscribe", "list-id", "precedence",
           "auto-submitted")


class HistoryGone(Exception):
    """history.list answered 404: the stored historyId is too old."""


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def profile(http: httpx.AsyncClient, token: str) -> dict[str, Any]:
    response = await http.get(f"{GMAIL}/profile", headers=auth(token))
    response.raise_for_status()
    return response.json()


async def watch(http: httpx.AsyncClient, token: str, topic: str) -> dict[str, Any]:
    """-> {historyId, expiration (ms since the epoch)}. All mail, no label filter: sent
    replies are touchpoints too; spam, drafts and chats are dropped by the classifier."""
    response = await http.post(f"{GMAIL}/watch", headers=auth(token), json={"topicName": topic})
    response.raise_for_status()
    return response.json()


async def stop(http: httpx.AsyncClient, token: str) -> None:
    response = await http.post(f"{GMAIL}/stop", headers=auth(token))
    if response.status_code not in (200, 204, 404):
        response.raise_for_status()


async def history(http: httpx.AsyncClient, token: str, start: int,
                  max_pages: int = 10) -> tuple[list[dict[str, Any]], int]:
    """-> (message refs {id, threadId, labelIds}, the newest historyId seen)."""
    params: dict[str, Any] = {"startHistoryId": str(start), "historyTypes": "messageAdded",
                              "maxResults": 500}
    refs: dict[str, dict[str, Any]] = {}
    latest = start
    for _ in range(max_pages):
        response = await http.get(f"{GMAIL}/history", params=params, headers=auth(token))
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
        if not body.get("nextPageToken"):
            break
        params["pageToken"] = body["nextPageToken"]
    return list(refs.values()), latest


async def search(http: httpx.AsyncClient, token: str, query: str, cap: int) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"q": query, "maxResults": min(PAGE, cap)}
    refs: list[dict[str, Any]] = []
    while len(refs) < cap:
        response = await http.get(f"{GMAIL}/messages", params=params, headers=auth(token))
        response.raise_for_status()
        body = response.json()
        refs += body.get("messages") or []
        if not body.get("nextPageToken"):
            break
        params["pageToken"] = body["nextPageToken"]
    return refs[:cap]


async def message(http: httpx.AsyncClient, token: str, message_id: str) -> dict[str, Any] | None:
    response = await http.get(f"{GMAIL}/messages/{message_id}", params={"format": "full"},
                              headers=auth(token))
    if response.status_code == 404:
        return None  # Deleted since the notification.
    response.raise_for_status()
    return response.json()


def decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


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
