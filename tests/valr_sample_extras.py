"""Fictional data for what the other branches plug into the pages, in their real shapes:
the leads (valr-leads: tests/leads_sample.py's touchpoints through app/leads.build and
graph, the JSON /valr/api/leads returns), recordings as valr-notetakers stores them, and
notetaker status (app/notetakers.status_view). The coach and impression cues are already in
tests/valr_sample.performance_file (performance.json schema 3, valr-eval). Every company,
person and address is fictional (.example domains)."""
from datetime import datetime, timezone


def sample_leads(now: datetime | None = None, gmail: bool = True, shape: str = "small") -> dict:
    """GET /valr/api/leads for the sample touchpoints, as app/leads_web.collect builds it.
    shape "live": about 107 leads shaped like the real mailbox (tests/leads_sample.live_shaped_touches)."""
    from app import leads
    from tests.leads_sample import live_shaped_touches, sample_touches
    now = now or datetime.now(timezone.utc)
    touches = live_shaped_touches(now) if shape == "live" else sample_touches(now)
    view = leads.graph(leads.build(touches, now), now)
    view["gmail"] = {"enabled": gmail, "watch": "active" if gmail else "off", "watch_expires": None,
                     "last_push_at": None, "last_sync_at": now.isoformat(), "backfill_done_at": now.isoformat(),
                     "last_error": "", "decisions_30d": {"kept": 31, "dropped": 212}}
    return view


def sample_recording_row(interview_id: str, company: str = "", domain: str = "",
                         interviewers: tuple[str, ...] = ()) -> dict:
    """A stored recording (app/notetakers/store.py's fields the page uses), made-up links and people."""
    import json
    provider, _, external_id = interview_id.partition(":")
    return {"provider": provider, "external_id": external_id, "title": "Interview recording",
            "share_url": f"https://fathom.video/calls/{external_id}",
            "company": company, "company_domain": domain,
            "interviewers": json.dumps(list(interviewers)) if interviewers else "", "invitee_hashes": "",
            "event_company": ""}


def with_extras(perf: dict) -> dict:
    """performance.json as the pages see it once notetakers are linked: each interview names
    its source (the coach and impressions are already in the sample)."""
    return {**perf, "interviews": [{**iv, "source": str(iv.get("id") or "").partition(":")[0] or "fathom"}
                                   for iv in perf["interviews"]]}


NOTETAKERS = None  # The full scenario uses tests/valr_sample.notetakers_view (status_view's shape).
