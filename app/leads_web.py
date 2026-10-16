"""The lead tracker on the web: the Gmail push webhook, the leads page and its JSON.

- `POST /hooks/gmail` (Cloudflare Access bypass, exact path: Pub/Sub cannot log in). The
  request must carry Pub/Sub's OIDC token for our audience (VALR_GMAIL_PUSH_AUDIENCE, default
  https://valr.haciensus.com/hooks/gmail) and push service account (VALR_GMAIL_PUSH_SA),
  verified with Google's keys (app/google_oidc.py); with VALR_GMAIL_SUBSCRIPTION set, the
  body must name that subscription; the decoded data must be for VALR_GMAIL_ACCOUNT. Then a
  Gmail sync starts (signal-with-start, id `valr-gmail-sync`). 204 on success; 403 on a bad
  token; 503 when Temporal is down, so Pub/Sub retries (exponential backoff, 1 day).
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

import base64
import binascii
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.service import RPCError

from app import callbacks, google_oidc, leads, leads_store, research, valr_dashboard
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
    """Start (or poke) the Gmail sync; False when Temporal is unavailable."""
    client = await callbacks.temporal()
    if client is None:
        return False
    try:
        await client.start_workflow(
            GmailSyncWorkflow.run, GmailSyncInput(reason, history_id), id=SYNC_WORKFLOW_ID,
            task_queue=TASK_QUEUE, start_signal="poke", rpc_timeout=callbacks.RPC_TIMEOUT,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING)
    except RPCError as error:
        logger.warning("VALR Gmail sync not started: %s", error.status.name)
        return False
    return True


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
    account = os.getenv("VALR_GMAIL_ACCOUNT", "").strip().lower()
    if (subscription and data["subscription"] != subscription) or (account and data["email"] != account):
        logger.warning("Gmail push ignored: another subscription or account")  # Acked: no retry, no alert.
        return Response(status_code=204)
    if os.getenv("VALR_LEADS", "0") != "1":
        return Response(status_code=204)
    if not await start_sync("push", data["history_id"]):
        raise HTTPException(503, "Temporal is unavailable.")
    return Response(status_code=204)


# The page and its data.

async def current(connection: Any, now: datetime) -> list[leads.Lead]:
    await leads_store.setup(connection)
    return leads.build(await leads_store.touches(connection), now, await leads_store.overrides(connection))


async def collect(pool: Any, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    async with pool.connection() as connection:
        built = await current(connection, now)
        state = await leads_store.load_state(connection)
        counts = await leads_store.decision_counts(connection)
    view = leads.graph(built, now)
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
        raise HTTPException(503, "Postgres is unavailable.")
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
        raise HTTPException(503, "Postgres is unavailable.")
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
        raise HTTPException(503, "Temporal is unavailable: the sync was not started.")
    return JSONResponse({"ok": True, "message": "Gmail sync started."}, headers=valr_dashboard.NO_CACHE)
