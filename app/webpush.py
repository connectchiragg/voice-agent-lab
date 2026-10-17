"""Phone push: standard Web Push (VAPID) to the VALR app installed on the iPhone's Home Screen.

How it fits together:

- The page (static/valr-pwa.js) asks the browser for a push subscription with our VAPID
  public key and POSTs it to /valr/api/push/subscribe (app/webpush_web.py). It is stored in
  `push_subscriptions`: the endpoint (Apple's, Google's or Mozilla's push service), the two
  encryption keys, the device's user agent, and its delivery record.
- To notify, the server encrypts a small JSON payload for each subscription and POSTs it to
  the endpoint, signed with the VAPID private key (pywebpush does both). The push service
  wakes static/sw.js on the phone, which shows the notification.
- A 404 or 410 from the push service means the subscription is gone (the app was deleted or
  notifications turned off): the row is deleted. Other failures count up in `failures`.

Settings (in .env, read by the app and by the research worker, which sends most pushes):
    VALR_VAPID_PUBLIC   the public key (base64url, 65 bytes); the page subscribes with it
    VALR_VAPID_PRIVATE  the private key (base64url, 32 bytes); never in code or git
    VALR_VAPID_SUBJECT  mailto:<owner address>; defaults to the first VALR_OWNER_EMAILS
Generate them once with `python -m app.webpush keys`. Changing the keys invalidates every
subscription: each phone must tap "Enable notifications" again.

Payloads are scrubbed (app/notifications.scrub): no pay figures, phone numbers or email
addresses, and short. Nothing here logs a title, a company or an endpoint.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("valr")

TTL_S = 12 * 60 * 60  # A push older than 12 h is not worth showing; Apple requires a TTL.
TIMEOUT_S = 10
GONE = (404, 410)
TITLE_MAX, BODY_MAX = 60, 140
# Push services we accept endpoints from (a subscription is a URL we will POST to later).
PUSH_HOSTS = (".push.apple.com", ".googleapis.com", ".mozilla.com", ".windows.com", ".mozaws.net")

SCHEMA = """
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id          bigserial PRIMARY KEY,
    endpoint    text UNIQUE NOT NULL,
    p256dh      text NOT NULL,
    auth        text NOT NULL,
    user_agent  text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now(),
    last_ok_at  timestamptz,
    failures    int NOT NULL DEFAULT 0
);
"""
FIELDS = ("id", "endpoint", "p256dh", "auth", "user_agent", "created_at", "last_ok_at", "failures")


# Keys.

def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def new_keys() -> tuple[str, str]:
    """(public, private): an EC P-256 pair as base64url, the formats browsers and pywebpush take."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_numbers().private_value.to_bytes(32, "big")
    public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return b64url(public), b64url(private)


@dataclass(frozen=True)
class Vapid:
    public: str
    private: str
    subject: str

    @classmethod
    def from_env(cls) -> "Vapid | None":
        public = os.getenv("VALR_VAPID_PUBLIC", "").strip()
        private = os.getenv("VALR_VAPID_PRIVATE", "").strip()
        subject = os.getenv("VALR_VAPID_SUBJECT", "").strip() or owner_subject()
        if not (public and private and subject.startswith(("mailto:", "https://"))):
            return None
        return cls(public, private, subject)


def owner_subject() -> str:
    first = next((e.strip() for e in os.getenv("VALR_OWNER_EMAILS", "").split(",") if e.strip()), "")
    return f"mailto:{first}" if first else ""


def public_key() -> str:
    """The key the page subscribes with, or "" when push is not configured."""
    vapid = Vapid.from_env()
    return vapid.public if vapid else ""


# Subscriptions (pure checks).

def valid_endpoint(endpoint: str) -> bool:
    parsed = urlparse(endpoint or "")
    host = (parsed.hostname or "").lower()
    return (parsed.scheme == "https" and len(endpoint) <= 1024
            and any(host == h.lstrip(".") or host.endswith(h) for h in PUSH_HOSTS))


def endpoint_key(endpoint: str) -> str:
    """A short hash of a subscription's endpoint: the page compares it with its own to mark
    "this device"; the endpoint itself never leaves the server."""
    return hashlib.sha256((endpoint or "").encode()).hexdigest()[:16]


def device_view(row: dict[str, Any]) -> dict[str, Any]:
    """One subscribed device for Settings: no endpoint, no keys."""
    iso = lambda moment: moment.astimezone(timezone.utc).isoformat() if moment else None  # noqa: E731
    return {"id": int(row["id"]), "label": device_label(row.get("user_agent") or ""),
            "key": endpoint_key(row.get("endpoint") or ""), "added_at": iso(row.get("created_at")),
            "last_ok_at": iso(row.get("last_ok_at")), "failures": int(row.get("failures") or 0)}


def device_label(user_agent: str) -> str:
    ua = user_agent or ""
    if "iPhone" in ua:
        return "iPhone"
    if "iPad" in ua:
        return "iPad"
    if "Android" in ua:
        return "Android"
    if "Macintosh" in ua:
        return "Mac"
    return "Browser"


# The store (Postgres). Tests use an in-memory stand-in with the same methods.

class Store:
    def __init__(self, connection: Any) -> None:
        self.c = connection

    async def setup(self) -> None:
        await self.c.execute(SCHEMA)

    async def save(self, endpoint: str, p256dh: str, auth: str, user_agent: str) -> None:
        await self.c.execute(
            "INSERT INTO push_subscriptions (endpoint, p256dh, auth, user_agent) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (endpoint) DO UPDATE SET p256dh = excluded.p256dh, auth = excluded.auth, "
            "user_agent = excluded.user_agent, failures = 0", (endpoint, p256dh, auth, user_agent[:300]))

    async def remove(self, endpoint: str) -> int:
        cursor = await self.c.execute("DELETE FROM push_subscriptions WHERE endpoint = %s", (endpoint,))
        return int(getattr(cursor, "rowcount", 0) or 0)

    async def all(self) -> list[dict[str, Any]]:
        cursor = await self.c.execute(f"SELECT {', '.join(FIELDS)} FROM push_subscriptions ORDER BY created_at")
        return [dict(zip(FIELDS, row)) for row in await cursor.fetchall()]

    async def count(self) -> int:
        cursor = await self.c.execute("SELECT count(*) FROM push_subscriptions")
        return int((await cursor.fetchone())[0])

    async def ok(self, sub_id: int, now: datetime) -> None:
        await self.c.execute("UPDATE push_subscriptions SET last_ok_at = %s, failures = 0 WHERE id = %s", (now, sub_id))

    async def failed(self, sub_id: int) -> None:
        await self.c.execute("UPDATE push_subscriptions SET failures = failures + 1 WHERE id = %s", (sub_id,))

    async def delete(self, sub_id: int) -> int:
        cursor = await self.c.execute("DELETE FROM push_subscriptions WHERE id = %s", (sub_id,))
        return int(getattr(cursor, "rowcount", 0) or 0)


# Payloads (pure).

def clip(text: str, most: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= most else text[:most - 1].rstrip() + "…"


def payload(title: str, body: str, url: str = "/valr", tag: str = "valr", badge: int | None = None,
            urgent: bool = False) -> dict[str, Any]:
    """What static/sw.js shows. Scrubbed of pay, phones and emails; the url stays on this site."""
    from app.notifications import scrub
    out = {"title": clip(scrub(title), TITLE_MAX) or "VALR", "body": clip(scrub(body), BODY_MAX),
           "url": url if url.startswith("/") and not url.startswith("//") else "/valr",
           "tag": tag[:64], "urgent": urgent}
    if badge is not None:
        out["badge"] = max(0, int(badge))
    return out


def tag_for(*parts: Any) -> str:
    """The same thread (or lead) gets the same tag, so the phone replaces instead of stacking."""
    key = next((str(p) for p in parts if p), "")
    return "valr-" + hashlib.sha256(key.encode()).hexdigest()[:12] if key else "valr"


# Sending.

@dataclass
class Result:
    delivered: int = 0
    gone: int = 0
    failed: int = 0


class Sender:
    """Sends one payload to every stored subscription. `post` is pywebpush.webpush (tests mock it)."""

    def __init__(self, store: Any, vapid: Vapid, post: Any = None) -> None:
        self.store, self.vapid, self.post = store, vapid, post

    def _post(self) -> Any:
        if self.post is None:
            from pywebpush import webpush
            self.post = webpush
        return self.post

    async def subscribers(self) -> int:
        return await self.store.count()

    async def send(self, message: dict[str, Any], now: datetime | None = None, only: int | None = None) -> Result:
        """To every device, or `only` the one with that id."""
        from pywebpush import WebPushException
        now = now or datetime.now(timezone.utc)
        data = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        headers = {"Urgency": "high" if message.get("urgent") else "normal"}
        result = Result()
        for sub in await self.store.all():
            if only is not None and sub["id"] != only:
                continue
            info = {"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}}
            try:
                await asyncio.to_thread(self._post(), subscription_info=info, data=data,
                                        vapid_private_key=self.vapid.private,
                                        vapid_claims={"sub": self.vapid.subject},  # Fresh: webpush adds aud/exp.
                                        ttl=TTL_S, timeout=TIMEOUT_S, headers=headers)
            except WebPushException as error:
                status = getattr(getattr(error, "response", None), "status_code", None)
                if status in GONE:
                    await self.store.delete(sub["id"])
                    result.gone += 1
                    continue
                logger.warning("VALR push not delivered: HTTP %s (%s)", status, device_label(sub["user_agent"]))
                await self.store.failed(sub["id"])
                result.failed += 1
                continue
            except Exception as error:  # noqa: BLE001 - network trouble: count it, keep going
                logger.warning("VALR push not delivered: %s", type(error).__name__)
                await self.store.failed(sub["id"])
                result.failed += 1
                continue
            await self.store.ok(sub["id"], now)
            result.delivered += 1
        return result


async def sender(connection: Any) -> Sender | None:
    """A Sender on this connection, or None when the VAPID keys are not set."""
    vapid = Vapid.from_env()
    if vapid is None:
        return None
    store = Store(connection)
    await store.setup()
    return Sender(store, vapid)


def main(argv: list[str]) -> int:
    if argv[1:] != ["keys"]:
        print("usage: python -m app.webpush keys", file=sys.stderr)
        return 2
    public, private = new_keys()
    subject = owner_subject() or "mailto:you@example.com"
    print("# Add to .env (the app and the research worker both read it). Keep the private key secret;")
    print("# new keys mean every phone must tap Enable notifications again.")
    print(f"VALR_VAPID_PUBLIC={public}")
    print(f"VALR_VAPID_PRIVATE={private}")
    print(f"VALR_VAPID_SUBJECT={subject}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
