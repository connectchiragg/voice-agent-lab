"""The VALR dashboard: read-only HTML at /valr (valr.haciensus.com redirects / there).

Shows today's and this month's research spend against the caps, the promise in
minutes, and recent jobs: topic, anonymised caller hash, status, duration, cost,
cap hits, the spoken answer and the notes with their source links. No phone numbers
are stored, so none can be shown.

Cloudflare Access guards the hostname; every request must also carry a valid
`Cf-Access-Jwt-Assertion` for the dashboard's Access application
(VALR_ACCESS_AUD, else CF_ACCESS_AUD), verified with app/access.py. Without one,
or with neither variable set, the answer is 403.
"""
import html
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
import httpx
from fastapi.responses import HTMLResponse, RedirectResponse

from app import access, google_oauth, research, research_jobs

logger = logging.getLogger("uvicorn.error")
router = APIRouter()
verifier: access.AccessVerifier | None = None


def dashboard_host() -> str:
    return os.getenv("VALR_HOST", "valr.haciensus.com").strip().lower()


def is_dashboard_host(host: str | None) -> bool:
    return bool(host) and host.split(":")[0].strip().lower() == dashboard_host()  # type: ignore[union-attr]


@asynccontextmanager
async def lifespan() -> AsyncIterator[None]:
    global verifier
    audience = (os.getenv("VALR_ACCESS_AUD") or os.getenv("CF_ACCESS_AUD") or "").strip()
    if not audience:
        logger.info("VALR dashboard is closed: VALR_ACCESS_AUD is not set.")
        yield
        return
    team = os.getenv("CF_ACCESS_TEAM_DOMAIN", access.DEFAULT_TEAM_DOMAIN).strip().rstrip("/")
    verifier = access.AccessVerifier(team, audience)
    verifier.refresh_in_background()
    try:
        yield
    finally:
        await verifier.close()
        verifier = None


async def viewer(request: Request) -> str:
    """The verified Access email, or 403."""
    if verifier is None:
        raise HTTPException(403, "The dashboard is not configured.")
    try:
        email = await verifier.email(request.headers.get(access.HEADER))
    except Exception as error:  # noqa: BLE001
        logger.warning("VALR dashboard: Access check failed: %s", type(error).__name__)
        email = None
    if not email:
        raise HTTPException(403, "A valid Cloudflare Access login is required.")
    return email


def owner_emails() -> set[str]:
    return {email.strip().lower() for email in os.getenv("VALR_OWNER_EMAILS", "").split(",")
            if email.strip()}


async def owner(request: Request) -> str:
    """The verified Access email, and (with VALR_OWNER_EMAILS set) the owner's; else 403."""
    email = await viewer(request)
    allowed = owner_emails()
    if allowed and email.lower() not in allowed:
        raise HTTPException(403, "Only the owner can do this.")
    return email


def e(value: object) -> str:
    return html.escape("" if value is None else str(value))


def link(url: str) -> str:
    if not url.startswith(("https://", "http://")):
        return e(url)
    return f'<a href="{e(url)}" rel="noopener noreferrer nofollow" target="_blank">{e(url[:90])}</a>'


def money(usd: float) -> str:
    return f"${usd:,.4f}" if usd < 1 else f"${usd:,.2f}"


STYLE = """
:root { color-scheme: light dark; --bg: #fbfbfa; --fg: #1d1d1b; --muted: #6b6b66; --line: #dcdcd6;
        --card: #ffffff; --accent: #2f6f4f; --warn: #a3471b; }
@media (prefers-color-scheme: dark) { :root { --bg: #161615; --fg: #ececea; --muted: #a2a29c;
        --line: #33332f; --card: #1f1f1d; --accent: #7cc49b; --warn: #f0a070; } }
body { margin: 0; background: var(--bg); color: var(--fg); font: 15px/1.5 system-ui, sans-serif; }
main { max-width: 980px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 22px; margin: 0 0 4px; } .muted { color: var(--muted); font-size: 13px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin: 20px 0; }
.tile { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px; }
.tile b { display: block; font-size: 22px; font-variant-numeric: tabular-nums; }
.bar { height: 6px; background: var(--line); border-radius: 3px; margin-top: 8px; overflow: hidden; }
.bar i { display: block; height: 100%; background: var(--accent); }
details { background: var(--card); border: 1px solid var(--line); border-radius: 8px; margin: 8px 0; }
summary { cursor: pointer; padding: 10px 14px; display: flex; flex-wrap: wrap; gap: 6px 14px; align-items: baseline; }
summary .topic { font-weight: 600; flex: 1 1 240px; }
.job { padding: 0 14px 14px; } .spoken { white-space: pre-wrap; border-left: 3px solid var(--accent); padding-left: 10px; }
.status-apologized, .status-failed, .status-cancelled { color: var(--warn); }
ul { padding-left: 20px; } li { margin: 3px 0; overflow-wrap: anywhere; } code { font-size: 12px; }
"""


def page(view: dict[str, Any], email: str) -> str:
    limits: research_jobs.Limits = view["limits"]
    month_share = min(view["month_usd"] / limits.month_usd, 1) if limits.month_usd else 0
    day_share = min(view["today_jobs"] / limits.jobs_per_day, 1) if limits.jobs_per_day else 0
    tiles = f"""
<div class="tiles">
 <div class="tile"><span class="muted">This month</span><b>{money(view['month_usd'])}</b>
  <span class="muted">of {money(limits.month_usd)} cap, {view['month_jobs']} jobs</span>
  <div class="bar"><i style="width:{month_share * 100:.1f}%"></i></div></div>
 <div class="tile"><span class="muted">Today (UTC)</span><b>{money(view['today_usd'])}</b>
  <span class="muted">{view['today_jobs']} of {limits.jobs_per_day} jobs</span>
  <div class="bar"><i style="width:{day_share * 100:.1f}%"></i></div></div>
 <div class="tile"><span class="muted">Per job / per caller</span><b>{money(limits.job_usd)}</b>
  <span class="muted">{limits.jobs_per_caller_day} jobs per caller per day</span></div>
 <div class="tile"><span class="muted">Promise now</span><b>{view['promise_minutes']} min</b>
  <span class="muted">p90 of the last 20 jobs, plus dialling</span></div>
 {gmail_tile(view.get("gmail") or {})}
</div>"""
    jobs = []
    for job in view["jobs"]:
        sources = {source["id"]: source["url"] for source in job["sources"] or []}
        notes = "".join(
            f"<li>{e(note.get('fact'))}{' <em>(sensitive)</em>' if note.get('sensitive') else ''} "
            + " ".join(f"[{link(sources.get(sid, sid))}]" for sid in note.get("source_ids", []))
            + "</li>" for note in job["notes"] or [])
        caps = ", ".join(f"{e(k)} {v}" for k, v in sorted((job["cap_hits"] or {}).items())) or "none"
        duration = "-" if job["duration_s"] is None else f"{job['duration_s']:.0f} s"
        jobs.append(f"""
<details><summary><span class="topic">{e(job['topic'])}</span>
 <span class="status-{e(job['status'])}">{e(job['status'])}</span>
 <span class="muted">{job['created_at']:%Y-%m-%d %H:%M} UTC · caller {e(job['caller'])} · {duration} ·
 {money(job['spend_usd'])} · promised {job['promised_minutes']} min</span></summary>
 <div class="job">
  <p class="spoken">{e(job['spoken']) or '<span class="muted">No answer yet.</span>'}</p>
  {f'<p class="muted">{e(job["detail"])}</p>' if job['detail'] else ''}
  <p class="muted">Cap hits: {caps}</p>
  {f'<ul>{notes}</ul>' if notes else ''}
 </div></details>""")
    body = "".join(jobs) or '<p class="muted">No research jobs yet.</p>'
    now = datetime.now(timezone.utc)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>VALR dashboard</title>
<meta name="robots" content="noindex"><style>{STYLE}</style></head><body><main>
<h1>VALR: Voice Agent Lab Research</h1>
<div class="muted">Read-only. {now:%Y-%m-%d %H:%M} UTC · signed in as {e(email)} · callers are shown as anonymous hashes</div>
{tiles}<h2 style="font-size:17px">Recent jobs</h2>{body}</main></body></html>"""


NO_CACHE = {"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"}


def gmail_tile(state: dict[str, Any]) -> str:
    """Gmail connection for R1: connected, reconnect needed (Testing-mode tokens last 7 days)."""
    configured = google_oauth.OAuthSettings.from_env() is not None
    connect = '<a href="/oauth/google/start">connect Gmail</a>' if configured else \
        "OAuth client not configured"
    label = {"connected": "connected", "reconnect_needed": "reconnect needed",
             "not_connected": "not connected"}.get(state.get("state", ""), "unknown")
    detail = ""
    if state.get("state") == "connected":
        detail = f"{e(state.get('account'))}, {state.get('age_days')} days old"
        if state.get("expires_soon"):
            label = "connected (expires soon: reconnect)"
    return (f'<div class="tile"><span class="muted">Gmail (R1)</span><b>{e(label)}</b>'
            f'<span class="muted">{detail} {connect if label != "connected" or state.get("expires_soon") else ""}'
            '</span></div>')


async def gmail_state() -> dict[str, Any]:
    if research.pool is None:
        return {"state": "unknown"}
    try:
        async with research.pool.connection() as connection:
            await google_oauth.setup(connection)
            return await google_oauth.status(connection)
    except Exception as error:  # noqa: BLE001
        logger.warning("Gmail status unavailable: %s", type(error).__name__)
        return {"state": "unknown"}


@router.get("/oauth/google/start")
async def google_start(request: Request) -> RedirectResponse:
    await owner(request)
    settings = google_oauth.OAuthSettings.from_env()
    if settings is None:
        raise HTTPException(503, "Google OAuth is not configured (client id, secret, VALR_TOKEN_KEY).")
    return RedirectResponse(google_oauth.consent_url(settings, google_oauth.make_state(
        settings.token_key)), status_code=303, headers=NO_CACHE)


@router.get("/oauth/google/callback", response_class=HTMLResponse)
async def google_callback(request: Request, code: str = "", state: str = "",
                          error: str = "") -> HTMLResponse:
    await owner(request)
    settings = google_oauth.OAuthSettings.from_env()
    if settings is None or research.pool is None:
        raise HTTPException(503, "Google OAuth or Postgres is not configured.")
    if error or not code:
        return HTMLResponse(f"<p>Gmail was not connected: {e(error or 'no code')}.</p>",
                            status_code=400, headers=NO_CACHE)
    if not google_oauth.check_state(settings.token_key, state):
        raise HTTPException(400, "This consent link expired; start again from /oauth/google/start.")
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            refresh, scope, account = await google_oauth.exchange_code(settings, code, http)
    except (ValueError, httpx.HTTPError) as failure:
        logger.warning("Gmail consent failed: %s", type(failure).__name__)
        return HTMLResponse(f"<p>Gmail was not connected: {e(failure)}.</p>", status_code=400,
                            headers=NO_CACHE)
    if settings.account and account != settings.account:
        return HTMLResponse(f"<p>That was {e(account) or 'another account'}; please connect "
                            f"{e(settings.account)}.</p>", status_code=400, headers=NO_CACHE)
    async with research.pool.connection() as connection:
        await google_oauth.setup(connection)
        await google_oauth.store(connection, settings, account, refresh, scope)
    logger.info("Gmail connected for VALR (read-only)")
    return HTMLResponse(f"<!doctype html><meta name=viewport content='width=device-width'>"
                        f"<p style='font:16px system-ui;margin:24px'>Gmail connected read-only for "
                        f"{e(account)}. <a href='/valr'>Back to the dashboard</a></p>",
                        headers=NO_CACHE)


@router.get("/valr", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    email = await viewer(request)
    if research.pool is None:
        raise HTTPException(503, "Postgres is unavailable.")
    async with research.pool.connection() as connection:
        view = await research_jobs.overview(connection, research_jobs.Limits.from_env())
    view["gmail"] = await gmail_state()
    return HTMLResponse(page(view, email), headers={"Cache-Control": "no-store",
                                                    "X-Frame-Options": "DENY",
                                                    "Referrer-Policy": "no-referrer"})
