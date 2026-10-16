"""The VALR pages (GET, server-rendered). Every one needs the verified Cloudflare Access
login (app/valr_dashboard.viewer, the same check as /valr/api/state); the actions stay
POST-only, owner-only and CSRF-checked in app/valr_dashboard.py.

    /valr                          Home: the leads globe and "right now"
    /valr/interviews               upcoming, by day
    /valr/interviews/<slot ref>    one upcoming interview
    /valr/interviews/past          past interviews
    /valr/interviews/past/<id>     one past interview: recording, impression, feedback
    /valr/leads                    globe, filters (?stage=&q=) and list (?lead=<id> opens one)
    /valr/performance              the coach first, then the numbers
    /valr/settings                 connections, calls, limits
    /valr/system                   health, costs, recent research (tucked away)

`?theme=light|dark` pins the theme for one view (screenshots); otherwise the toggle's choice
or the system's.
"""
import importlib
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import research, valr_dashboard, valr_page
from app.valr_ui import (attention, contracts, home, interviews, leads, performance, settings, shell,
                         system)

logger = logging.getLogger("uvicorn.error")
router = APIRouter()
# The leads branch's loader, "module:function" -> async fn(pool) -> its /valr/api/leads JSON.
LEADS_SOURCE = "app.leads_web:collect"
TITLES = {"home": "Home", "interviews": "Interviews", "interview": "Interview", "past": "Past interviews",
          "past_one": "Past interview", "leads": "Leads", "performance": "Performance",
          "settings": "Settings", "system": "System"}
NAV_OF = {"interview": "interviews", "past": "interviews", "past_one": "interviews", "system": "settings"}


async def load_leads() -> dict[str, Any]:
    """The leads, normalised; "unavailable" (never an error page) when the tracker isn't there."""
    module_name, _, function = LEADS_SOURCE.partition(":")
    try:
        loader = getattr(importlib.import_module(module_name), function)
    except (ImportError, AttributeError):
        return contracts.leads(None)
    try:
        return contracts.leads(await loader(research.pool))
    except Exception as error:  # noqa: BLE001 - the rest of the page still renders.
        logger.warning("VALR leads unavailable: %s", type(error).__name__)
        return contracts.leads(None)


async def load_recording(interview: dict[str, Any]) -> dict[str, Any] | None:
    """A past interview's stored recording (valr-notetakers: webhooks and syncs store share and
    embed links), by its performance.json id "<provider>:<id>"; None when there is none."""
    provider, _, external_id = str(interview.get("id") or "").partition(":")
    if not external_id or research.pool is None:
        return None
    try:
        from app.notetakers import base, store
        async with research.pool.connection() as connection:
            await store.setup(connection)
            row = await store.recording(connection, provider, external_id)
    except Exception as error:  # noqa: BLE001 - the page still renders, with a link if it has one.
        logger.warning("VALR recording lookup failed: %s", type(error).__name__)
        return None
    if not row:
        return None
    return {"provider": provider, "share_url": base.safe_link(row.get("share_url") or ""),
            "embed_url": base.safe_embed(row.get("embed_url") or ""), "title": row.get("title") or ""}


def not_found(what: str, back: str, label: str) -> str:
    return (shell.page_head("Not found", e_(what), crumb=f"{back}|{label}")
            + '<div class="empty"><p class="muted">It may have been moved or cancelled on your calendar.</p></div>')


def e_(text: str) -> str:
    return valr_page.e(text)


async def render(request: Request, name: str, **params: str) -> HTMLResponse:
    # The Leads page shows recruiters' addresses: owner only, like GET /valr/api/leads.
    email = await (valr_dashboard.owner if name == "leads" else valr_dashboard.viewer)(request)
    try:
        view = await valr_dashboard.state_view()
    except Exception as error:  # noqa: BLE001 - a page that says so, not a stack trace or JSON.
        logger.warning("VALR page unavailable: %s", type(error).__name__)
        return HTMLResponse(valr_page.error_page("The database didn't answer."), status_code=503,
                            headers=valr_dashboard.NO_CACHE)
    clock = valr_page.Clock(view.get("zone") or "UTC", datetime.fromisoformat(view["generated_at"]))
    items = attention.items(view)
    status = 200
    title = TITLES[name]
    query = request.query_params
    if name == "home":
        body = home.page(view, clock, items, await load_leads())
    elif name == "interviews":
        body = interviews.upcoming_page(view, clock)
    elif name == "interview":
        found = interviews.find_slot(view, params["ref"])
        if found:
            body = interviews.detail_page(view, *found, clock)
            title = interviews.wording.interview_label(found[1])["headline"]
        else:
            body, status = not_found("That interview isn't coming up.", "/valr/interviews", "Interviews"), 404
    elif name == "past":
        body = interviews.past_page(view, clock)
    elif name == "past_one":
        found = interviews.find_past(view, params["slug"])
        if found:
            stored = contracts.recording(await load_recording(found), found)
            if stored:
                found = {**found, "recording": stored}
            body = interviews.past_detail_page(view, found, clock)
            title = found["company"] or title
        else:
            body, status = not_found("That interview isn't in your history.", "/valr/interviews/past", "Past interviews"), 404
    elif name == "leads":
        body = leads.page(view, clock, await load_leads(), query.get("stage", "")[:20],
                          query.get("q", "")[:80], query.get("lead", "")[:64], query.get("status", "")[:10])
    elif name == "performance":
        body = performance.page(view, clock)
    elif name == "settings":
        body = settings.page(view, clock, items)
    else:
        body = system.page(view, clock, items)
    html = shell.layout(view, email, valr_dashboard.csrf_token(email), NAV_OF.get(name, name), title, body,
                        items, theme=query.get("theme"))
    return HTMLResponse(html, status_code=status, headers=valr_dashboard.NO_CACHE)


@router.get("/valr/interviews", response_class=HTMLResponse)
async def interviews_page(request: Request) -> HTMLResponse:
    return await render(request, "interviews")


@router.get("/valr/interviews/past", response_class=HTMLResponse)
async def past_page(request: Request) -> HTMLResponse:
    return await render(request, "past")


@router.get("/valr/interviews/past/{slug}", response_class=HTMLResponse)
async def past_one(request: Request, slug: str) -> HTMLResponse:
    return await render(request, "past_one", slug=slug[:80])


@router.get("/valr/interviews/{ref}", response_class=HTMLResponse)
async def interview_page(request: Request, ref: str) -> HTMLResponse:
    return await render(request, "interview", ref=ref[:64])


@router.get("/valr/leads", response_class=HTMLResponse)
async def leads_page(request: Request) -> HTMLResponse:
    return await render(request, "leads")


@router.get("/valr/performance", response_class=HTMLResponse)
async def performance_page(request: Request) -> HTMLResponse:
    return await render(request, "performance")


@router.get("/valr/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    return await render(request, "settings")


@router.get("/valr/system", response_class=HTMLResponse)
async def system_page(request: Request) -> HTMLResponse:
    return await render(request, "system")
