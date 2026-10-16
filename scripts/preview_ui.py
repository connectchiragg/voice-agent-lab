"""Local preview of both pages with fictional data: the call page (/) and the VALR cockpit (/valr).

    VALR_DEV_PREVIEW=1 python scripts/preview_ui.py        # http://127.0.0.1:8090/ and /valr
    .../valr?scenario=empty                                 # first run: nothing connected, no data
    .../valr?scenario=error                                 # the calendar store is down
    VALR_DEV_PREVIEW=1 python scripts/preview_ui.py --performance data/performance.json
                                                            # your own scores in the Performance section

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
    parser.add_argument("--performance", type=Path,
                        help="a real performance.json to render (local only; never served beyond 127.0.0.1)")
    args = parser.parse_args()

    import uvicorn

    from app import interview_store, interviews, main as app_main, research, valr_dashboard, valr_view, webrtc
    from tests.valr_sample import sample_view
    import webrtc_latency

    valr_dashboard.enable_dev_preview(args.host)  # Raises unless VALR_DEV_PREVIEW=1 and loopback.
    os.environ["VALR_ADMIN_EMAILS"] = valr_dashboard.PREVIEW_EMAIL
    state = {"paused": False}
    scenario: contextvars.ContextVar[str] = contextvars.ContextVar("scenario", default="full")

    @app_main.app.middleware("http")
    async def pick_scenario(request, call_next):
        wanted = request.query_params.get("scenario", "full")
        if wanted == "down" and request.url.path == "/valr":
            from fastapi.responses import HTMLResponse
            from app import valr_page
            return HTMLResponse(valr_page.error_page("The database didn't answer."), status_code=503)
        scenario.set(wanted if wanted in ("full", "empty", "error") else "full")
        return await call_next(request)

    real = None
    if args.performance:
        import json
        real = valr_view.performance(json.loads(args.performance.read_text(encoding="utf-8")))

    async def collect(pool, now=None):
        view = sample_view(paused=state["paused"], scenario=scenario.get())
        if real is not None and scenario.get() == "full":
            view["performance"] = real
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
