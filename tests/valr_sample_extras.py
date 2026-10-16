"""Fictional data for what the other branches plug into the pages, in their real shapes:
the leads (valr-leads: tests/leads_sample.py's touchpoints through app/leads.build and
graph, the JSON /valr/api/leads returns), recordings as valr-notetakers stores them, and
notetaker status (app/notetakers.status_view). The coach and impression cues are already in
tests/valr_sample.performance_file (performance.json schema 3, valr-eval). Every company,
person and address is fictional (.example domains)."""
from datetime import datetime, timezone


def sample_leads(now: datetime | None = None, gmail: bool = True) -> dict:
    """GET /valr/api/leads for the sample touchpoints, as app/leads_web.collect builds it."""
    from app import leads
    from tests.leads_sample import sample_touches
    now = now or datetime.now(timezone.utc)
    view = leads.graph(leads.build(sample_touches(now), now), now)
    view["gmail"] = {"enabled": gmail, "watch": "active" if gmail else "off", "watch_expires": None,
                     "last_push_at": None, "last_sync_at": now.isoformat(), "backfill_done_at": now.isoformat(),
                     "last_error": "", "decisions_30d": {"kept": 31, "dropped": 212}}
    return view


def sample_recording_row(interview_id: str, embed: bool = True) -> dict:
    """A stored recording (app/notetakers/store.py's fields the page uses), made-up links."""
    provider, _, external_id = interview_id.partition(":")
    return {"provider": provider, "title": "Interview recording",
            "share_url": f"https://fathom.video/share/sample-{external_id}",
            "embed_url": f"https://fathom.video/embed/sample-{external_id}" if embed else ""}


def with_extras(perf: dict) -> dict:
    """performance.json as the pages see it once notetakers are linked: each interview names
    its source (the coach and impressions are already in the sample)."""
    return {**perf, "interviews": [{**iv, "source": str(iv.get("id") or "").partition(":")[0] or "fathom"}
                                   for iv in perf["interviews"]]}


NOTETAKERS = None  # The full scenario uses tests/valr_sample.notetakers_view (status_view's shape).
