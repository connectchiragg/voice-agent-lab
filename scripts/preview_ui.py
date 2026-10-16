"""Local preview of both pages with fictional data: the call page (/) and the VALR cockpit (/valr).

    VALR_DEV_PREVIEW=1 python scripts/preview_ui.py        # http://127.0.0.1:8090/ and /valr
    .../valr?scenario=empty                                 # first run: nothing connected, no data
    .../valr?scenario=error                                 # the calendar store is down

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
    args = parser.parse_args()

    import uvicorn

    from app import interview_store, interviews, main as app_main, research, valr_dashboard, valr_view, webrtc
    from tests.valr_sample import notetakers_view, sample_recording, sample_view
    import webrtc_latency

    valr_dashboard.enable_dev_preview(args.host)  # Raises unless VALR_DEV_PREVIEW=1 and loopback.
    os.environ["VALR_ADMIN_EMAILS"] = valr_dashboard.PREVIEW_EMAIL
    state = {"paused": False, "notetakers": {"fathom": "connected", "fireflies": "not_connected",
                                             "granola": "not_connected"}}
    scenario: contextvars.ContextVar[str] = contextvars.ContextVar("scenario", default="full")

    @app_main.app.middleware("http")
    async def pick_scenario(request, call_next):
        wanted = request.query_params.get("scenario", "full")
        if wanted == "down" and request.url.path == "/valr":
            from fastapi.responses import HTMLResponse
            from app import valr_page
            return HTMLResponse(valr_page.error_page("The database didn't answer."), status_code=503)
        scenario.set(wanted if wanted in ("full", "empty", "error") else "full")
        path = request.url.path
        if path.startswith("/valr/api/notetakers/") or path == "/valr/api/recording" \
                or path == "/valr/api/telegram/code":
            return await preview_api(request, path)
        return await call_next(request)

    async def preview_api(request, path):
        """Fake notetaker / recording / Telegram answers; a pasted key is read and dropped."""
        from fastapi.responses import JSONResponse
        if not valr_dashboard.is_preview_request(request):
            return JSONResponse({"detail": "Preview only."}, status_code=403)
        if path == "/valr/api/recording":
            return JSONResponse(sample_recording(found=request.query_params.get("ref") != "none"))
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

    async def collect(pool, now=None):
        view = sample_view(paused=state["paused"], scenario=scenario.get())
        if scenario.get() == "full":
            view["connections"]["notetakers"] = notetakers_view(state["notetakers"])
            view["mode"] = "telegram"  # The default since 2026-10-16; the bot is set up, not linked.
            view["connections"]["telegram"] = {"configured": True, "linked": False, "linked_at": None}
        return view

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
    print(f"VALR preview: http://{HOST}:{args.port}/ (call) and http://{HOST}:{args.port}/valr (cockpit)")
    uvicorn.run(app_main.app, host=args.host, port=args.port, lifespan="off", log_level="warning")


if __name__ == "__main__":
    main()
