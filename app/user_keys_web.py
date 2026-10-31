"""Settings › API keys: a workspace's own LLM and Deepgram keys (app/user_keys.py).

Per user (the /valr prefix, app/auth/gate.py): any signed-in workspace, the owner's included
(valr_dashboard.viewer, not .owner, which admits only the owner's addresses). Every POST needs
the CSRF token, same origin and JSON, as valr_dashboard.action_owner checks them. The key itself is accepted once and never sent back:
responses carry provider, model, the last four characters, status and when it was checked.

- `GET /valr/api/keys`: {owner, keys: {llm, speech}, providers}. The owner's workspace uses
  the server's keys and stores none here (owner: true, keys: null).
- `POST /valr/api/keys` {purpose, provider, key, model?}: checked with one free call
  (app/key_providers.check); a rejected key is not saved (400), an unreachable provider saves it
  as "unchecked".
- `POST /valr/api/keys/test` {purpose}: check the stored key again and record the result.
- `POST /valr/api/keys/remove` {purpose}: delete it; the features it served stop.
"""
from __future__ import annotations

import logging
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app import key_providers, tenant, user_keys, valr_dashboard
from app.settings_web import parse, pool

logger = logging.getLogger("uvicorn.error")
router = APIRouter()
NO_CACHE = valr_dashboard.NO_CACHE
OWNER_NOTE = "Your workspace uses the server's keys."
_http: httpx.AsyncClient | None = None


def http() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=10)
    return _http


class SaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    purpose: Literal["llm", "speech"]
    provider: str = Field(max_length=32)
    key: str = Field(min_length=8, max_length=400)
    model: str = Field(default="", max_length=key_providers.MODEL_CHARS, pattern=r"^[A-Za-z0-9._:/@-]*$")


class PurposeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    purpose: Literal["llm", "speech"]


async def action(request: Request) -> str:
    """valr_dashboard.action_owner's checks for any signed-in workspace: same origin, JSON, CSRF."""
    email = await valr_dashboard.viewer(request)
    site, origin = request.headers.get("sec-fetch-site"), request.headers.get("origin")
    if site and site not in ("same-origin", "none"):
        raise HTTPException(403, "Cross-site request refused.")
    if origin and origin.split("://", 1)[-1] != request.headers.get("host", ""):
        raise HTTPException(403, "Cross-origin request refused.")
    if not request.headers.get("content-type", "").startswith("application/json"):
        raise HTTPException(415, "Expected JSON.")
    if not valr_dashboard.csrf_ok(email, request.headers.get("x-csrf-token")):
        raise HTTPException(403, "The page is stale: reload it and try again.")
    return email


def providers() -> list[dict[str, str]]:
    return [{"name": p.name, "label": p.label, "purpose": p.purpose, "default_model": p.default_model,
             "console": p.console} for p in key_providers.PROVIDERS.values()]


async def view() -> dict:
    if tenant.is_owner():
        return {"ok": True, "owner": True, "keys": None, "providers": providers()}
    async with pool().connection() as connection:
        keys = await user_keys.public(connection)
    return {"ok": True, "owner": False, "keys": keys, "providers": providers()}


def guest_only() -> None:
    if tenant.is_owner():
        raise HTTPException(400, OWNER_NOTE)


def stored() -> None:
    try:
        user_keys.token_key()
    except user_keys.NotConfigured:
        raise HTTPException(503, "This server can't store keys yet. Try again later.") from None


@router.get("/valr/api/keys")
async def get_keys(request: Request) -> JSONResponse:
    await valr_dashboard.viewer(request)
    return JSONResponse(await view(), headers=NO_CACHE)


@router.post("/valr/api/keys")
async def save_key(request: Request) -> JSONResponse:
    await action(request)
    guest_only()
    body = parse(SaveRequest, await request.body(), "Expected {purpose, provider, key, model?}.")
    provider = key_providers.PROVIDERS.get(body.provider)
    if provider is None or provider.purpose != body.purpose:
        raise HTTPException(400, "Pick a provider from the list.")
    key = body.key.strip()
    if any(ch.isspace() for ch in key):
        raise HTTPException(400, "That doesn't look like an API key: paste it without spaces.")
    stored()
    status = await key_providers.check(provider, key, http())
    if status == "rejected":
        raise HTTPException(400, f"{provider.label} rejected this key. Check it and paste it again.")
    model = body.model.strip() if provider.purpose == "llm" else ""
    async with pool().connection() as connection:
        await user_keys.save(connection, body.purpose, provider.name, model, key, status)
    logger.info("VALR user key saved (%s, %s, %s)", body.purpose, provider.name, status)
    message = "Key saved and working." if status == "ok" else \
        f"Key saved. {provider.label} didn't answer the check, so it will be tried on first use."
    return JSONResponse(await view() | {"message": message}, headers=NO_CACHE)


@router.post("/valr/api/keys/test")
async def test_key(request: Request) -> JSONResponse:
    await action(request)
    guest_only()
    body = parse(PurposeRequest, await request.body(), "Expected {purpose}.")
    stored()
    async with pool().connection() as connection:
        rows = await user_keys.public(connection)
        row = rows.get(body.purpose)
        found = (await user_keys.load(connection)).get(body.purpose) if row else None
        if row is None:
            raise HTTPException(404, "No key saved yet.")
        if found is None and row["status"] != "rejected":
            raise HTTPException(409, "The saved key can't be read any more. Paste it again.")
        provider = key_providers.PROVIDERS[row["provider"]]
        status = await key_providers.check(provider, found.key, http()) if found else "rejected"
        if found is not None:
            await user_keys.set_status(connection, body.purpose, status)
    message = {"ok": "The key works.", "rejected": f"{provider.label} rejected the key.",
               "unchecked": f"{provider.label} didn't answer. Try again in a minute."}[status]
    return JSONResponse(await view() | {"message": message, "status": status}, headers=NO_CACHE)


@router.post("/valr/api/keys/remove")
async def remove_key(request: Request) -> JSONResponse:
    await action(request)
    guest_only()
    body = parse(PurposeRequest, await request.body(), "Expected {purpose}.")
    async with pool().connection() as connection:
        await user_keys.remove(connection, body.purpose)
    logger.info("VALR user key removed (%s)", body.purpose)
    return JSONResponse(await view() | {"message": "Key removed."}, headers=NO_CACHE)
