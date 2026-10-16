"""Local preview of the VALR app with fictional data: the call page (/call) and every
app page (/valr, /valr/interviews, /valr/leads, /valr/progress, /valr/settings, ...).

    VALR_DEV_PREVIEW=1 python scripts/preview_ui.py        # http://127.0.0.1:8090/valr
    .../valr?scenario=empty                                 # first run: nothing connected, no data
    .../valr?scenario=error                                 # the calendar store is down
    VALR_DEV_PREVIEW=1 python scripts/preview_ui.py --performance data/performance.json
                                                            # your own scores in the Performance section
    .../valr?scenario=down                                  # the database is down (any page)
    .../valr?mode=telegram                                  # heads-up mode, Telegram not linked
    .../valr?extras=0                                       # without the other branches' data
    .../valr?theme=light                                    # pin light (or dark) for screenshots
    VALR_DEV_PREVIEW=1 python scripts/preview_ui.py --media /path/to/any.mp4
                                                            # past interviews play that local file
    .../valr/interviews/past/<id>?media=processing          # the player's "Preparing the video" state
                                                            # (?media=ready switches back)
    .../valr/leads?leads=small                              # the small lead sample (default: ~115, 70 live)

No Postgres, Temporal, Deepgram or LLM: the cockpit shows tests/valr_sample.py's made-up
interviews, its buttons change in-memory state only, and a call talks to the stub
pipeline of scripts/webrtc_latency.py (it answers "Sure, ..." with a tone).

The Cloudflare Access check is skipped only here: it needs VALR_DEV_PREVIEW=1, this
script binds to 127.0.0.1 (anything else is refused), and each request must come from
and be addressed to 127.0.0.1 (app/valr_dashboard.py). The app's images contain neither
this script nor tests/, and their startup never enables the preview.
"""
import argparse
import contextvars
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

HOST = "127.0.0.1"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--host", default=HOST, help=argparse.SUPPRESS)
    parser.add_argument("--media", type=Path,
                        help="a local MP4 standing in for Fathom's signed video (local only)")
    parser.add_argument("--performance", type=Path,
                        help="a real performance.json to render (local only; never served beyond 127.0.0.1)")
    args = parser.parse_args()

    import uvicorn

    from app import interview_store, interviews, main as app_main, research, valr_dashboard, valr_view, webrtc
    from app.valr_ui import contracts, routes
    from tests.valr_sample import notetakers_view, sample_recording, sample_view
    from tests.valr_sample_extras import sample_leads
    import webrtc_latency

    valr_dashboard.enable_dev_preview(args.host)  # Raises unless VALR_DEV_PREVIEW=1 and loopback.
    os.environ["VALR_ADMIN_EMAILS"] = valr_dashboard.PREVIEW_EMAIL
    state = {"media": "ready", "paused": False, "auto_dial": None, "prep": {}, "notetakers": {"fathom": "connected", "fireflies": "not_connected",
                                             "granola": "not_connected"}}
    scenario: contextvars.ContextVar[str] = contextvars.ContextVar("scenario", default="full")
    options: contextvars.ContextVar[dict] = contextvars.ContextVar("options", default={})

    @app_main.app.middleware("http")
    async def pick_scenario(request, call_next):
        wanted = request.query_params.get("scenario", "full")
        if wanted == "down" and request.url.path.startswith("/valr") and "/api/" not in request.url.path:
            from fastapi.responses import HTMLResponse
            from app import valr_page
            return HTMLResponse(valr_page.error_page("The database didn't answer."), status_code=503)
        scenario.set(wanted if wanted in ("full", "empty", "error") else "full")
        path = request.url.path
        if request.query_params.get("media") in ("ready", "processing", "none"):
            state["media"] = request.query_params["media"]
        if path == "/preview-media.mp4" and args.media and valr_dashboard.is_preview_request(request):
            from fastapi.responses import FileResponse
            return FileResponse(args.media, media_type="video/mp4")
        if path.startswith(("/valr/api/notifications", "/valr/api/phone/", "/valr/api/push")):
            return await preview_notify(request, path)
        if path.startswith(("/valr/api/notetakers/", "/valr/api/leads/")) or path in (
                "/valr/api/recording", "/valr/api/recording/media", "/valr/api/telegram/code", "/valr/api/reach", "/valr/api/prep"):
            return await preview_api(request, path)
        options.set({"leads": "small" if request.query_params.get("leads") == "small" else "live",
                     "telegram": request.query_params.get("mode") == "telegram",
                     "extras": request.query_params.get("extras") != "0"})
        return await call_next(request)

    async def preview_api(request, path):
        """Fake notetaker / recording / Telegram answers; a pasted key is read and dropped."""
        from fastapi.responses import JSONResponse
        if not valr_dashboard.is_preview_request(request):
            return JSONResponse({"detail": "Preview only."}, status_code=403)
        if path == "/valr/api/recording/media":
            if state["media"] == "processing" or not args.media:
                return JSONResponse({"ok": True, "status": "processing",
                                     "message": "Preparing the video… (first play can take a minute or two)"})
            if state["media"] == "none":
                return JSONResponse({"ok": True, "status": "unavailable",
                                     "message": "Fathom says this recording isn't downloadable for your account."})
            return JSONResponse({"ok": True, "status": "completed", "url": "/preview-media.mp4",
                                 "content_type": "video/mp4", "expires_at": "2099-01-01T00:00:00+00:00"})
        if path == "/valr/api/recording":
            return JSONResponse(sample_recording(found=request.query_params.get("ref") != "none"))
        if path == "/valr/api/reach":
            import json
            mode = json.loads(await request.body() or b"{}").get("mode")
            state["auto_dial"] = mode == "call"
            return JSONResponse({"ok": True, "mode": mode, "message": f"Preview: {mode} saved."})
        if path == "/valr/api/prep":
            import json
            from app.interview_store import prep_item
            body = json.loads(await request.body() or b"{}")
            ticks = state["prep"].setdefault(str(body.get("ref")), set())
            (ticks.add if body.get("done") else ticks.discard)(prep_item(str(body.get("item") or "")))
            return JSONResponse({"ok": True, "message": "Ticked." if body.get("done") else "Unticked."})
        if path.startswith("/valr/api/leads/"):
            await request.body()
            return JSONResponse({"ok": True, "message": "Preview: nothing is stored."})
        if path == "/valr/api/telegram/code":
            return JSONResponse({"ok": True, "command": "/start 123456", "expires_minutes": 15,
                                 "link": "https://t.me/example_valr_bot?start=123456",
                                 "message": "Send /start 123456 to the VALR bot within 15 minutes."})
        provider, action = path.split("/")[4:6]
        label = provider.capitalize()
        if action == "connect":
            await request.body()  # Never stored or echoed.
            state["notetakers"][provider] = "connected"
            return JSONResponse({"ok": True, "message": f"{label} connected."})
        if action == "disconnect":
            state["notetakers"][provider] = "not_connected"
            return JSONResponse({"ok": True, "message": f"{label} disconnected."})
        if action == "sync":
            return JSONResponse({"ok": True, "found": 3, "new": 1, "message": f"{label}: 3 recordings in the last 7 days, 1 new."})
        return JSONResponse({"ok": True, "message": f"{label}: 2 notes read, 2 new."})

    notes = {"read": set(), "switches": {"immediate": True, "digest": True, "quiet": True, "delivery": "both"},
             "devices": set()}

    def preview_items():
        """Fictional notifications, tied to the sample leads by company (so the Leads dots match)."""
        from datetime import datetime, timedelta, timezone
        from app import notifications
        now = datetime.now(timezone.utc)
        leads_by_name = {r["company"]: r["id"] for r in contracts.leads(sample_leads(gmail=True, shape="live"))["leads"]}
        raw = [(1, "reply_needed", "immediate", "Fableworks asked you to share your availability", "Fableworks", 0.2, True),
               (2, "offer", "immediate", "Arcworks sent an offer", "Arcworks", 2, True),
               (3, "stage", "digest", "Glintworks moved to Interviewing", "Glintworks", 5, False),
               (4, "update", "digest", "Larkworks sent an update", "Larkworks", 6, True),
               (5, "reply", "digest", "Pikeworks replied", "Pikeworks", 7, True),
               (6, "rejection", "digest", "Tessel: not moving forward", "Tessel", 30, True),
               (7, "new_lead", "digest", "New: Quillworks reached out", "Quillworks", 60, True)]
        return [{"id": i, "kind": k, "label": notifications.LABELS.get(k, "Update"), "importance": imp, "title": t,
                 "body": "", "company": c, "lead_id": leads_by_name.get(c, ""), "email": mail,
                 "link": f"/valr/leads?lead={leads_by_name[c]}#lead" if c in leads_by_name else "/valr/leads",
                 "gmail_url": "https://mail.google.com/mail/#all/x" if mail else "",
                 "created_at": (now - timedelta(hours=h)).isoformat(),
                 "day": "Today" if h < 12 else "Yesterday" if h < 40 else "Earlier", "read": i in notes["read"]}
                for i, k, imp, t, c, h, mail in raw]

    async def load_notes():
        if scenario.get() == "empty" or not options.get().get("extras", True):
            return {"items": [], "unread": 0, "leads": {}}
        items = [i for i in preview_items() if not i["read"]]
        by_lead = {}
        for i in items:
            if i["email"] and i["lead_id"]:
                by_lead[i["lead_id"]] = by_lead.get(i["lead_id"], 0) + 1
        return {"items": items, "unread": len(items), "leads": by_lead}

    async def mark_lead_read(lead_id):
        notes["read"] |= {i["id"] for i in preview_items() if i["lead_id"] == lead_id}

    routes.load_notes = load_notes
    routes.mark_lead_read = mark_lead_read

    async def preview_notify(request, path):
        """Fictional notifications, alert switches and phone-recording status (nothing stored)."""
        import json
        from datetime import datetime, timedelta, timezone
        from fastapi.responses import JSONResponse
        if not valr_dashboard.is_preview_request(request):
            return JSONResponse({"detail": "Preview only."}, status_code=403)
        now = datetime.now(timezone.utc)
        items = preview_items()
        if request.method == "POST":
            body = json.loads(await request.body() or b"{}")
            if path.endswith("/read"):
                if body.get("lead"):
                    await mark_lead_read(body["lead"])
                else:
                    notes["read"] |= {i["id"] for i in items} if body.get("all") else set(body.get("ids") or [])
                items = preview_items()
            elif path.endswith("/settings"):
                notes["switches"].update({k: v for k, v in body.items() if k in notes["switches"]})
            elif path == "/valr/api/phone/token":
                return JSONResponse({"ok": True, "token": "preview-token-not-real", "message": "Copy it now."})
        if path.startswith("/valr/api/push"):  # Phone push: a made-up key, devices counted in memory.
            if request.method == "POST":
                body = json.loads(await request.body() or b"{}")
                if path.endswith("/subscribe"):
                    notes["devices"].add(str(body.get("endpoint")))
                elif path.endswith("/unsubscribe"):
                    notes["devices"].discard(str(body.get("endpoint")))
            out = {"ok": True, "configured": True, "public_key": PREVIEW_VAPID, "devices": len(notes["devices"]),
                   "delivery": notes["switches"]["delivery"]}
            if path.endswith("/test"):
                out |= {"delivered": 0, "message": "Preview: nothing is sent."}
            return JSONResponse(out)
        if path == "/valr/api/phone/status":
            return JSONResponse({"ok": True, "token": True, "last_upload_at": (now - timedelta(hours=3)).isoformat(),
                                 "last_status": "done", "waiting": 0, "audio_days": 7})
        unread = sum(1 for i in items if i["id"] not in notes["read"])
        if request.query_params.get("unread") == "1":
            items = [i for i in items if not i["read"]]
        return JSONResponse({"ok": True, "unread": unread, "items": items, "switches": notes["switches"],
                             "marked": 1})

    from app import webpush
    PREVIEW_VAPID = webpush.new_keys()[0]  # A throwaway public key, so a local browser can subscribe.

    real = None
    if args.performance:
        import json
        real = valr_view.performance(json.loads(args.performance.read_text(encoding="utf-8")))

    async def collect(pool, now=None):
        view = sample_view(paused=state["paused"], scenario=scenario.get(), extras=options.get().get("extras", True))
        if scenario.get() == "full":
            view["connections"]["notetakers"] = notetakers_view(state["notetakers"])
            if real is not None:
                view["performance"] = real
        for block in view["blocks"]:
            for slot in block["slots"]:
                slot["prep_done"] = sorted(state["prep"].get(slot["ref"], ()))
        if state["auto_dial"] is not None:
            view["mode"] = "call" if state["auto_dial"] else "telegram"
            view["reach"] = {"auto_dial": state["auto_dial"], "source": "settings", "default_auto_dial": False}
            return view
        view["reach"] = {"auto_dial": False, "source": "default", "default_auto_dial": False}
        view["mode"] = "telegram"  # The default: a Telegram heads-up, no automatic call.
        if options.get().get("telegram"):  # ?mode=telegram: the bot is set up, not linked yet.
            view["connections"]["telegram"] = {"configured": True, "linked": False, "linked_at": None, "username": None}
        return view

    async def load_leads():
        if scenario.get() == "empty" or not options.get().get("extras", True):
            return contracts.leads(None)
        return contracts.leads(sample_leads(gmail=True, shape=options.get().get("leads", "live")))

    async def load_recording(interview):
        from tests.valr_sample_extras import sample_recording_row
        ids = [iv["id"] for iv in sample_view()["performance"]["interviews"]]
        if not options.get().get("extras", True) or interview.get("id") == ids[0]:
            return None  # The oldest has no stored recording: the page asks the notetaker.
        return sample_recording_row(interview["id"])

    async def load_rows(interviews_):
        """Stored recordings with Fathom's invitee details (made-up people and domains)."""
        from tests.valr_sample_extras import sample_recording_row
        if not options.get().get("extras", True):
            return {}
        people = {"Juniper": ("", "juniper.example", ("Maya Lin", "Dev Kapoor")),
                  "Nimbus": ("", "nimbus.example", ("Jo Park",)),
                  "Kite": ("", "", ("Mia Torres", "Nina Shah"))}
        return {iv["id"]: sample_recording_row(iv["id"], *people[iv.get("company")])
                for iv in interviews_ if iv.get("id") and iv.get("company") in people}

    routes.load_leads = load_leads
    routes.load_recording = load_recording
    routes.load_rows = load_rows

    async def load_overrides(entity_ids):
        return {}  # No "Set company" names are stored in the preview.
    routes.load_overrides = load_overrides

    class Pool:
        def connection(self):
            class Context:
                async def __aenter__(self):
                    return object()

                async def __aexit__(self, *exc):
                    return False
            return Context()

    async def setup(connection):
        return None

    async def set_flag(connection, name, enabled):
        state["paused"] = enabled

    async def sync_now(reason):
        return True

    valr_view.collect = collect
    research.pool = Pool()
    interview_store.setup = setup
    interview_store.set_flag = set_flag
    interviews.sync_now = sync_now
    app_main.app.state.engines = webrtc_latency.stub_engines()
    webrtc.peers_of(app_main.app)
    print(f"VALR preview: http://{HOST}:{args.port}/valr (app) and http://{HOST}:{args.port}/call (talk)")
    uvicorn.run(app_main.app, host=args.host, port=args.port, lifespan="off", log_level="warning")


if __name__ == "__main__":
    main()
