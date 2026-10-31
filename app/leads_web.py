"""The lead tracker on the web: the Gmail push webhook, the leads page and its JSON.

- `POST /hooks/gmail` (Cloudflare Access bypass, exact path: Pub/Sub cannot log in). The
  request must carry Pub/Sub's OIDC token for our audience (VALR_GMAIL_PUSH_AUDIENCE, default
  https://valr.haciensus.com/hooks/gmail) and push service account (VALR_GMAIL_PUSH_SA),
  verified with Google's keys (app/google_oidc.py); with VALR_GMAIL_SUBSCRIPTION set, the
  body must name that subscription. Every workspace's Gmail watch publishes to the same topic, so
  the push's emailAddress picks the workspace (`push_workspace`: the owner's VALR_GMAIL_ACCOUNT,
  or the workspace whose connected Google account it is); an unknown address is acknowledged and
  ignored. Then that workspace's Gmail sync starts (signal-with-start, id `valr-gmail-sync`, a
  guest's `valr-gmail-sync-<uid>`). 204 on success; 403 on a bad token; 503 when Temporal is
  down, so Pub/Sub retries (exponential backoff, 1 day).
- `GET /valr/leads` is the app's Leads page (app/valr_ui/leads.py, with static/leads-globe.js);
  it reads `collect` below and posts the corrections and "Check Gmail now" to the routes here.
- `GET /valr/api/leads`: {leads, contacts, links, stages, generated_at, gmail}: every lead
  rebuilt from the stored touchpoints at request time (temperatures are "now").
- `POST /valr/api/leads/sync`: a sync now (owner, CSRF, same-origin JSON like the cockpit).
- `POST /valr/api/leads/<id>/override` {action: dismiss | stage | merge | rename, stage?, into?,
  company?}: the
  owner's corrections, stored in `lead_overrides` and applied by every rebuild. Dismiss hides
  the lead and its touchpoints and Gmail threads for good (a new thread or event may start it
  again); a stage stays until set back to automatic (""); merge folds it into another lead;
  rename ("Set company") names the company ("" goes back to the resolver's name).

The page and its JSON need the owner's Access login (app/valr_dashboard.owner).
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.service import RPCError

from app import callbacks, google_oidc, leads, leads_store, research, tenant, valr_dashboard, workspaces
from app.lead_workflow import SYNC_WORKFLOW_ID, TASK_QUEUE, GmailSyncInput, GmailSyncWorkflow

logger = logging.getLogger("uvicorn.error")
router = APIRouter()
MAX_PUSH_BYTES = 16384
DEFAULT_AUDIENCE = "https://valr.haciensus.com/hooks/gmail"
verifier: google_oidc.PushVerifier | None = None


def push_verifier() -> google_oidc.PushVerifier:
    global verifier
    audience = os.getenv("VALR_GMAIL_PUSH_AUDIENCE", DEFAULT_AUDIENCE).strip()
    email = os.getenv("VALR_GMAIL_PUSH_SA", "").strip()
    if verifier is None or (verifier.audience, verifier.email) != (audience, email):
        verifier = google_oidc.PushVerifier(audience, email)
    return verifier


def push_data(body: bytes) -> dict[str, Any]:
    """The Pub/Sub envelope -> {subscription, emailAddress, historyId}; ValueError if malformed."""
    envelope = json.loads(body)
    message = envelope.get("message") if isinstance(envelope, dict) else None
    if not isinstance(message, dict) or not isinstance(message.get("data"), str):
        raise ValueError("no message data")
    try:
        data = json.loads(base64.b64decode(message["data"], validate=False))
    except (binascii.Error, ValueError) as error:
        raise ValueError("data is not base64 JSON") from error
    if not isinstance(data, dict):
        raise ValueError("data is not an object")
    return {"subscription": str(envelope.get("subscription", "")),
            "email": str(data.get("emailAddress", "")).lower(),
            "history_id": int(data.get("historyId") or 0)}


async def start_sync(reason: str, history_id: int = 0) -> bool:
    """Start (or poke) the current workspace's Gmail sync; False when Temporal is unavailable."""
    client = await callbacks.temporal()
    if client is None:
        return False
    user = tenant.get() or tenant.OWNER
    try:
        await client.start_workflow(
            GmailSyncWorkflow.run, GmailSyncInput(reason, history_id, user=user),
            id=workspaces.scoped_id(SYNC_WORKFLOW_ID, user),
            task_queue=TASK_QUEUE, start_signal="poke", rpc_timeout=callbacks.RPC_TIMEOUT,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING)
    except RPCError as error:
        logger.warning("VALR Gmail sync not started: %s", error.status.name)
        return False
    return True


async def push_workspace(email: str) -> str | None:
    """The workspace a Gmail push is for: the owner's when it is VALR_GMAIL_ACCOUNT; else the
    workspace (the owner's or a guest's, `tenant.user_id` of the address) whose connected Google
    account it is. None for an address no workspace has connected."""
    from app.auth import core
    email = email.strip().lower()
    account = os.getenv("VALR_GMAIL_ACCOUNT", "").strip().lower()
    if account and email == account:
        return tenant.OWNER
    if not email or research.pool is None:
        return None
    candidates = [tenant.user_id(email, core.owner_emails())]
    if not account:  # The owner's connected account may be any address (none configured).
        candidates.append(tenant.OWNER)
    for user in dict.fromkeys(candidates):
        with tenant.as_user(user):
            try:
                async with research.pool.connection() as connection:
                    cursor = await connection.execute(
                        "SELECT account FROM oauth_tokens WHERE provider = %s AND status = 'connected'",
                        (workspaces.GOOGLE,))
                    row = await cursor.fetchone()
            except Exception as error:  # noqa: BLE001 - e.g. no oauth_tokens table yet
                logger.warning("Gmail push: workspace lookup failed (%s)", type(error).__name__)
                return None
        if row and str(row[0]).lower() == email:
            return user
    return None


@router.post("/hooks/gmail")
async def gmail_push(request: Request) -> Response:
    claims = await push_verifier().verify(request.headers.get("authorization"))
    if claims is None:
        logger.warning("Gmail push refused: no valid Pub/Sub OIDC token")
        raise HTTPException(403, "Invalid token.")
    body = await request.body()
    if len(body) > MAX_PUSH_BYTES:
        raise HTTPException(413, "Too large.")
    try:
        data = push_data(body)
    except (ValueError, TypeError) as error:
        logger.warning("Gmail push ignored: %s", error)
        return Response(status_code=204)  # Malformed: acknowledge, a retry would not help.
    subscription = os.getenv("VALR_GMAIL_SUBSCRIPTION", "").strip()
    if subscription and data["subscription"] != subscription:
        logger.warning("Gmail push ignored: another subscription")  # Acked: no retry, no alert.
        return Response(status_code=204)
    if os.getenv("VALR_LEADS", "0") != "1":
        return Response(status_code=204)
    user = await push_workspace(data["email"])
    if user is None:
        logger.warning("Gmail push ignored: an account no workspace has connected")  # Acked.
        return Response(status_code=204)
    with tenant.as_user(user):
        started = await start_sync("push", data["history_id"])
    if not started:
        raise HTTPException(503, "VALR's background service isn't answering. Try again in a minute.")
    return Response(status_code=204)


# The page and its data.

BUILD_TTL = timedelta(minutes=2)
_built: dict[str, Any] = {}  # workspace -> (fingerprint, built at, leads): one per workspace and process.


async def fingerprint(connection: Any) -> tuple[Any, ...] | None:
    """What leads.build reads, in one cheap query: the touchpoints and the owner's corrections."""
    try:
        cursor = await connection.execute(
            "SELECT (SELECT count(*) FROM lead_touchpoints), (SELECT max(updated_at) FROM lead_touchpoints), "
            "(SELECT count(*) FROM lead_overrides), (SELECT max(id) FROM lead_overrides)")
        row = await cursor.fetchone()
    except Exception:  # noqa: BLE001 - then build afresh.
        return None
    return tuple(row) if row else None


async def current(connection: Any, now: datetime) -> list[leads.Lead]:
    """Every lead, built from the touchpoints. leads.build is CPU-heavy (over a second for a
    couple of thousand touches): it runs in a thread, so the event loop isn't blocked (QA B6),
    and its result is reused while nothing changed, for up to BUILD_TTL (temperatures and
    expiry move with the clock). Callers only read the leads."""
    await leads_store.setup(connection)
    key = await fingerprint(connection)
    entry = tenant.get() or tenant.OWNER  # Never another workspace's leads, even with equal counts.
    hit = _built.get(entry)
    if key is not None and hit and hit[0] == key and timedelta(0) <= now - hit[1] < BUILD_TTL:
        return hit[2]
    touches = await leads_store.touches(connection)
    overrides = await leads_store.overrides(connection)
    built = await asyncio.to_thread(leads.build, touches, now, overrides)
    if key is not None:
        _built[entry] = (key, now, built)
    return built


_schema_ready = False


def build_graph(touches: list[leads.Touch], overrides: leads.Overrides, now: datetime) -> dict[str, Any]:
    return leads.graph(leads.build(touches, now, overrides), now)


async def collect(pool: Any, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    global _schema_ready
    async with pool.connection() as connection:
        if not _schema_ready:  # Once per process: its ALTER TABLE locks gmail_state on every page otherwise.
            await leads_store.setup(connection)
            _schema_ready = True
        touches = await leads_store.touches(connection)
        overrides = await leads_store.overrides(connection)
        state = await leads_store.load_state(connection)
        counts = await leads_store.decision_counts(connection)
    # Every touchpoint through the company rules is pure CPU: in a thread, so the event loop
    # (the bell's polls, other pages, a live call) is not held up while it runs.
    view = await asyncio.to_thread(build_graph, touches, overrides, now)
    view["gmail"] = gmail_view(state, counts, now)
    return view


def gmail_view(state: dict[str, Any], counts: dict[str, int], now: datetime) -> dict[str, Any]:
    expires = state.get("watch_expires")
    return {"enabled": os.getenv("VALR_LEADS", "0") == "1",
            "watch": "active" if expires and expires > now else "off",
            "watch_expires": leads.iso(expires), "last_push_at": leads.iso(state.get("last_push_at")),
            "last_sync_at": leads.iso(state.get("last_sync_at")),
            "backfill_done_at": leads.iso(state.get("backfill_done_at")),
            "last_error": str(state.get("last_error") or "")[:120], "decisions_30d": counts}


async def view_or_503() -> dict[str, Any]:
    if research.pool is None:
        raise HTTPException(503, "VALR can't reach its data right now. Try again in a minute.")
    return await collect(research.pool)


@router.get("/valr/api/leads")
async def leads_json(request: Request) -> JSONResponse:
    await valr_dashboard.owner(request)
    return JSONResponse(await view_or_503(), headers=valr_dashboard.NO_CACHE)


class OverrideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["dismiss", "stage", "merge", "rename"]
    stage: str = ""  # For "stage": one of leads.STAGES, or "" for automatic again.
    into: str = ""  # For "merge": the lead to merge into.
    company: str = Field(default="", max_length=80)  # For "rename": the company name.


async def apply_override(connection: Any, lead_id: str, body: OverrideRequest, email: str,
                         now: datetime) -> str:
    """Store one correction; -> the message for the page. ValueError when it does not apply."""
    built = {lead.id: lead for lead in await current(connection, now)}
    lead = built.get(lead_id)
    if lead is None:
        raise LookupError("Unknown lead.")
    if body.action == "rename":
        name = " ".join(body.company.split())
        await leads_store.add_override(connection, lead_id, "rename", name, by=email)
        return f"Company set to {name}." if name else "Company name is automatic again."
    if body.action == "dismiss":
        await leads_store.add_override(connection, lead_id, "dismiss",
                                       touch_ids=[t.id for t in lead.touches],
                                       threads=sorted({t.thread for t in lead.touches if t.thread}), by=email)
        return f"{lead.company} dismissed."
    if body.action == "stage":
        if body.stage and body.stage not in leads.STAGES:
            raise ValueError("Unknown stage.")
        await leads_store.add_override(connection, lead_id, "stage", body.stage, by=email)
        return f"{lead.company}: stage set to {body.stage}." if body.stage else f"{lead.company}: stage is automatic again."
    if body.into == lead_id or body.into not in built:
        raise ValueError("Pick another lead to merge into.")
    await leads_store.add_override(connection, lead_id, "merge", body.into, by=email)
    return f"{lead.company} merged into {built[body.into].company}."


@router.post("/valr/api/leads/{lead_id}/override")
async def lead_override(request: Request, lead_id: str) -> JSONResponse:
    """The owner's corrections from the lead panel: dismiss, set stage, merge into another lead."""
    email = await valr_dashboard.action_owner(request)
    try:
        body = OverrideRequest.model_validate_json(await request.body())
    except ValidationError as error:
        raise HTTPException(400, "Expected {action: dismiss|stage|merge|rename, stage?, into?, company?}.") from error
    if research.pool is None:
        raise HTTPException(503, "VALR can't reach its data right now. Try again in a minute.")
    try:
        async with research.pool.connection() as connection:
            message = await apply_override(connection, lead_id[:40], body, email, datetime.now(timezone.utc))
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    logger.info("VALR lead override: %s", body.action)
    return JSONResponse({"ok": True, "message": message}, headers=valr_dashboard.NO_CACHE)


@router.post("/valr/api/leads/sync")
async def leads_sync(request: Request) -> JSONResponse:
    await valr_dashboard.action_owner(request)
    if not await start_sync("manual"):
        raise HTTPException(503, "VALR's background service isn't answering, so the sync didn't start. Try again in a minute.")
    return JSONResponse({"ok": True, "message": "Gmail sync started."}, headers=valr_dashboard.NO_CACHE)
