"""Local preview of the VALR app with fictional data: the call page (/call) and every
app page (/valr, /valr/interviews, /valr/leads, /valr/performance, /valr/settings, ...).

    VALR_DEV_PREVIEW=1 python scripts/preview_ui.py        # http://127.0.0.1:8090/valr
    .../valr?scenario=empty                                 # first run: nothing connected, no data
    .../valr?scenario=error                                 # the calendar store is down
    .../valr?scenario=down                                  # the database is down (any page)
    .../valr?mode=telegram                                  # heads-up mode, Telegram not linked
    .../valr?extras=0                                       # without the other branches' data
    .../valr?theme=light                                    # pin light (or dark) for screenshots

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
    from app.valr_ui import contracts, routes
    from tests.valr_sample import sample_view
    from tests.valr_sample_extras import sample_leads
    import webrtc_latency

    valr_dashboard.enable_dev_preview(args.host)  # Raises unless VALR_DEV_PREVIEW=1 and loopback.
    os.environ["VALR_ADMIN_EMAILS"] = valr_dashboard.PREVIEW_EMAIL
    state = {"paused": False}
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
        options.set({"telegram": request.query_params.get("mode") == "telegram",
                     "extras": request.query_params.get("extras") != "0"})
        return await call_next(request)

    async def collect(pool, now=None):
        view = sample_view(paused=state["paused"], scenario=scenario.get(), extras=options.get().get("extras", True))
        if options.get().get("telegram"):
            view["mode"] = "telegram"
            view["connections"]["telegram"] = {"configured": True, "linked": False, "linked_at": None, "username": None}
        else:
            view.setdefault("mode", "telegram" if "telegram" in view["connections"] else "call")
        return view

    async def load_leads():
        if scenario.get() == "empty" or not options.get().get("extras", True):
            return contracts.leads(None)
        return contracts.leads(sample_leads())

    routes.load_leads = load_leads

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
