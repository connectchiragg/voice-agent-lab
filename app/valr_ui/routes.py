"""The VALR pages (GET, server-rendered). Every one needs the verified Cloudflare Access
login (app/valr_dashboard.viewer, the same check as /valr/api/state); the actions stay
POST-only, owner-only and CSRF-checked in app/valr_dashboard.py.

    /valr                          Home: what's new, "right now" and the leads globe
    /valr/interviews               upcoming, by day
    /valr/interviews/<slot ref>    one upcoming interview
    /valr/interviews/past          past interviews
    /valr/interviews/past/<id>     one past interview: recording, impression, feedback
    /valr/leads                    tabs, filters (?tab=&stage=&q=) and list (?lead=<id> opens one and
                                   reads its notifications)
    /valr/progress                 trends, then what they mean (/valr/performance redirects here)
    /valr/settings                 set-up, profile summary, connections, calls and reminders,
                                   notifications, call recordings, data and account
                                   (/valr/system redirects here)
    /valr/profile                  photo, names, pronunciation, time zone, phone, account

Every page carries the notification bell; its unread count, Home's "New" strip and the
Leads dots all come from one load_notes() call, so they always agree.

`?theme=light|dark` pins the theme for one view (screenshots); otherwise the toggle's choice
or the system's.
"""
import importlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import owner, research, valr_dashboard, valr_page
from app.valr_ui import attention, contracts, home, interviews, leads, past, performance, profile, settings, shell

logger = logging.getLogger("uvicorn.error")
router = APIRouter()
# The leads branch's loader, "module:function" -> async fn(pool) -> its /valr/api/leads JSON.
LEADS_SOURCE = "app.leads_web:collect"
TITLES = {"home": "Home", "interviews": "Interviews", "interview": "Interview", "past": "Past interviews",
          "past_one": "Past interview", "leads": "Leads", "progress": "Progress", "settings": "Settings",
          "profile": "Profile"}
NAV_OF = {"interview": "interviews", "past": "interviews", "past_one": "interviews", "profile": "settings"}
NO_NOTES = {"items": [], "unread": 0, "leads": {}}


async def load_notes() -> dict[str, Any]:
    """The unread notifications (app/notifications.py): {"items" (newest first, the API's
    shape), "unread", "leads": {lead id: unread emails}}. Empty when Postgres can't say."""
    if research.pool is None:
        return NO_NOTES
    try:
        from app import notifications
        now = datetime.now(timezone.utc)
        since = now - notifications.KEEP_FOR
        async with research.pool.connection() as connection:
            store = notifications.Store(connection)
            await store.setup()
            rows = await store.listing(True, since, limit=40)
            count = await store.unread_count(since)
            by_lead = await store.unread_by_lead(since) if hasattr(store, "unread_by_lead") else {}
        return {"items": [notifications.view(r, now) for r in rows], "unread": count, "leads": by_lead}
    except Exception as error:  # noqa: BLE001 - the page renders without the dots.
        logger.warning("VALR notifications unavailable: %s", type(error).__name__)
        return NO_NOTES


async def mark_lead_read(lead_id: str) -> None:
    """Opening a lead's card reads its notifications (its email dots clear)."""
    if research.pool is None or not lead_id:
        return
    try:
        from app import notifications
        async with research.pool.connection() as connection:
            store = notifications.Store(connection)
            await store.setup()
            await store.mark_lead_read(lead_id, datetime.now(timezone.utc))
    except Exception as error:  # noqa: BLE001
        logger.warning("VALR notifications not marked read: %s", type(error).__name__)


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


async def load_rows(interviews_: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The stored recordings of these past interviews (company, people, links), by "<provider>:<id>"."""
    keys = [tuple(str(iv.get("id") or "").partition(":")[::2]) for iv in interviews_]
    keys = [k for k in keys if k[0] and k[1]]
    if not keys or research.pool is None:
        return {}
    try:
        from app.notetakers import store
        async with research.pool.connection() as connection:
            await store.setup(connection)
            rows = await store.many(connection, keys)
    except Exception as error:  # noqa: BLE001 - the page still renders, without the extras.
        logger.warning("VALR recordings lookup failed: %s", type(error).__name__)
        return {}
    return {f"{p}:{i}": row for (p, i), row in rows.items()}


async def load_overrides(entity_ids: list[str]) -> dict[str, str]:
    """The owner's "Set company" names (app/company_resolver.company_overrides)."""
    if not entity_ids or research.pool is None:
        return {}
    try:
        from app import company_resolver
        async with research.pool.connection() as connection:
            await company_resolver.setup(connection)
            return await company_resolver.overrides(connection, entity_ids)
    except Exception as error:  # noqa: BLE001 - the page still renders with the resolver's names.
        logger.warning("VALR company overrides unavailable: %s", type(error).__name__)
        return {}


async def with_slot_names(view: dict[str, Any]) -> dict[str, Any]:
    """Upcoming slots with the owner's "Set company" applied."""
    slots = [slot for block in view.get("blocks") or [] for slot in block.get("slots") or []]
    manual = await load_overrides([f"slot:{slot.get('ref')}" for slot in slots if slot.get("ref")])
    for slot in slots:
        name = manual.get(f"slot:{slot.get('ref')}")
        if name:
            slot["company"], slot["company_how"] = name, "set by you"
    return view


async def with_details(view: dict[str, Any]) -> dict[str, Any]:
    """The past interviews with company, interviewers and outcome filled in (app/valr_ui/past.py)."""
    perf = view.get("performance") or {}
    ivs = perf.get("interviews") or []
    if not ivs:
        return view
    rows = await load_rows(ivs)
    found = past.all_leads(await load_leads())
    owner = past.base.owner_words()
    manual = await load_overrides([f"interview:{iv.get('id')}" for iv in ivs])
    filled = [past.enrich(iv, rows.get(iv.get("id") or ""), found, owner, manual.get(f"interview:{iv.get('id')}", ""))
              for iv in ivs]
    return {**view, "performance": {**perf, "interviews": filled}}


RECORDINGS_DAYS = 365


def review_state(eval_status: str, report: str | None) -> str:
    """pending (queued, being scored, or its hand-off is retried on the worker's next start) |
    unavailable (no interview or transcript to score) | sent. A history item scored quietly
    ("history") shows its scorecard instead, so it never reaches here once scored."""
    if report == "sent":
        return "sent"
    if report in ("not_interview", "no_transcript", "failed"):
        return "unavailable"
    return "pending"


def recording_entry(row: dict[str, Any], report: str | None, company: str) -> dict[str, Any]:
    """A stored recording without a scorecard, in the shape the Past pages read."""
    from app.notetakers import store
    from app.valr_view import scrub
    start = row.get("event_start") or row.get("started_at") or row.get("received_at")
    return {"id": f"{row['provider']}:{row['external_id']}", "recording_only": True,
            "date": start.astimezone(timezone.utc).isoformat() if start else "",
            "title": scrub(row.get("event_title") or row.get("title") or ""), "company": company, "round": "",
            "interviewers": [scrub(n) for n in store.names(row)][:6], "provider": row["provider"],
            "review": review_state(row.get("eval_status") or "", report), "overall": None}


async def with_recordings(view: dict[str, Any]) -> dict[str, Any]:
    """Past interviews = the scorecards plus every stored recording (phone, Fathom, Fireflies,
    Granola) that has none yet, minus what the owner hid or deleted. Newest last, like
    performance.json (the list reverses it)."""
    if research.pool is None:
        return view
    try:
        from app import post_call
        from app.notetakers import routes as notetaker_routes
        from app.notetakers import store
        now = datetime.now(timezone.utc)
        async with research.pool.connection() as connection:
            await store.setup(connection)
            rows = await store.between(connection, now - timedelta(days=RECORDINGS_DAYS), now + timedelta(days=1))
            gone = await store.hidden(connection)
            try:  # Past interviews with no recording (python -m app.notetakers history).
                from app.notetakers import history
                no_recording = await history.calendar_entries(
                    connection, now - timedelta(days=RECORDINGS_DAYS), now + timedelta(days=1))
            except Exception as error:  # noqa: BLE001 - the recordings still show.
                logger.warning("VALR calendar history unavailable: %s", type(error).__name__)
                no_recording = []
            try:
                reports = await post_call.statuses(connection)
            except Exception as error:  # noqa: BLE001 - every review shows as pending.
                logger.warning("VALR post-call statuses unavailable: %s", type(error).__name__)
                reports = {}
    except Exception as error:  # noqa: BLE001 - the scorecards still show.
        logger.warning("VALR stored recordings unavailable: %s", type(error).__name__)
        return view
    perf = view.get("performance") or {}
    scored = [iv for iv in perf.get("interviews") or []
              if tuple(str(iv.get("id") or "").partition(":")[::2]) not in gone]
    known = {str(iv.get("id") or "") for iv in scored}
    fresh = [r for r in rows if f"{r['provider']}:{r['external_id']}" not in known
             and (r["provider"], r["external_id"]) not in gone]
    manual = await load_overrides([f"interview:{r['provider']}:{r['external_id']}" for r in fresh])
    extra = [recording_entry(r, reports.get((r["provider"], r["external_id"])),
                             manual.get(f"interview:{r['provider']}:{r['external_id']}")
                             or notetaker_routes.company_of(r.get("company") or "", r.get("event_company") or ""))
             for r in fresh]
    extra += [iv for iv in no_recording if tuple(iv["id"].partition(":")[::2]) not in gone]
    merged = sorted(scored + extra, key=lambda iv: str(iv.get("date") or "")[:10])
    return {**view, "performance": {**perf, "interviews": merged}}


async def load_recording(interview: dict[str, Any]) -> dict[str, Any] | None:
    """A past interview's stored recording (valr-notetakers: webhooks and syncs store its share
    link), by its performance.json id "<provider>:<id>"; None when there is none."""
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
            "title": row.get("title") or ""}


def not_found(what: str, back: str, label: str) -> str:
    return (shell.page_head(e_(what), crumb=f"{back}|{label}")
            + shell.empty_state("calendar", "It may have been moved or cancelled on your calendar."))


def e_(text: str) -> str:
    return valr_page.e(text)


async def render(request: Request, name: str, **params: str) -> HTMLResponse:
    # Leads (recruiters' addresses), Settings and Profile (the owner's own details): owner only.
    email = await (valr_dashboard.owner if name in ("leads", "settings", "profile") else valr_dashboard.viewer)(request)
    try:
        view = await valr_dashboard.state_view()
    except Exception as error:  # noqa: BLE001 - a page that says so, not a stack trace or JSON.
        logger.warning("VALR page unavailable: %s", type(error).__name__)
        return HTMLResponse(valr_page.error_page("The database didn't answer."), status_code=503,
                            headers=valr_dashboard.NO_CACHE)
    await owner.refresh()  # The profile (name, photo, phone) at most a minute old.
    clock = valr_page.Clock(view.get("zone") or "UTC", datetime.fromisoformat(view["generated_at"]))
    items = attention.items(view)
    query = request.query_params
    selected = query.get("lead", "")[:64] if name == "leads" else ""
    if selected:
        await mark_lead_read(selected)
    notes = await load_notes()
    status = 200
    title = TITLES[name]
    if name == "home":
        body = home.page(view, clock, items, await load_leads(), notes)
    elif name == "interviews":
        # The Past tab's count must match the Past list: recordings and calendar history included.
        body = interviews.upcoming_page(await with_slot_names(await with_recordings(await with_details(view))), clock)
    elif name == "interview":
        view = await with_slot_names(view)
        found = interviews.find_slot(view, params["ref"])
        if found:
            body = interviews.detail_page(view, *found, clock)
            title = interviews.wording.interview_label(found[1])["headline"]
        else:
            body, status = not_found("That interview isn't coming up.", "/valr/interviews", "Interviews"), 404
    elif name == "past":
        body = interviews.past_page(await with_recordings(await with_details(view)), clock)
    elif name == "past_one":
        view = await with_recordings(await with_details(view))
        found = interviews.find_past(view, params["slug"])
        if found:
            stored = contracts.recording(await load_recording(found), found)
            if stored:
                found = {**found, "recording": stored}
            if found.get("recording_only"):
                body = interviews.recording_detail_page(found, clock)
                title = interviews.past_name(found)
            else:
                body = interviews.past_detail_page(view, found, clock)
                title = found["company"] or title
        else:
            body, status = not_found("That interview isn't in your history.", "/valr/interviews/past", "Past interviews"), 404
    elif name == "leads":
        body = leads.page(view, clock, await load_leads(), query.get("stage", "")[:20],
                          query.get("q", "")[:80], selected, query.get("status", "")[:10],
                          band=query.get("band", "")[:10], waiting=query.get("waiting", "")[:10],
                          tab=query.get("tab", "")[:6], closed=query.get("closed") == "1", unread=notes["leads"])
    elif name == "progress":
        body = performance.page(view, clock, await load_leads())
    elif name == "profile":
        body = profile.page(view, clock, email)
    else:
        view = {**view, "_email": email, "_host": valr_dashboard.dashboard_host(),
                "_query": {"google_error": query.get("google_error", "")[:20], "connected": query.get("connected", "")[:20]}}
        body = settings.page(view, clock, items)
    html = shell.layout(view, email, valr_dashboard.csrf_token(email), NAV_OF.get(name, name), title, body,
                        theme=query.get("theme"), bell=shell.bell(notes, items, clock),
                        lead_dot=bool(notes["leads"]))
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


@router.get("/valr/progress", response_class=HTMLResponse)
async def progress_page(request: Request) -> HTMLResponse:
    return await render(request, "progress")


@router.get("/valr/performance")
async def performance_page(request: Request) -> RedirectResponse:
    await valr_dashboard.viewer(request)
    return RedirectResponse("/valr/progress", status_code=301)


@router.get("/valr/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    return await render(request, "settings")


@router.get("/valr/profile", response_class=HTMLResponse)
async def profile_page(request: Request) -> HTMLResponse:
    return await render(request, "profile")


@router.get("/valr/system")
async def system_page(request: Request) -> RedirectResponse:
    await valr_dashboard.viewer(request)
    return RedirectResponse("/valr/settings", status_code=301)
