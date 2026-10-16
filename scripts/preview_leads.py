"""Local preview of the leads page with made-up data (tests/leads_sample.py).

    VALR_DEV_PREVIEW=1 python scripts/preview_leads.py      # http://127.0.0.1:8091/valr/leads
    .../valr/api/leads                                      # the JSON the globe takes

No Postgres, Temporal or Gmail. The Access check is skipped only as in scripts/preview_ui.py:
VALR_DEV_PREVIEW=1, bound to 127.0.0.1, and requests from and to 127.0.0.1.
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT)]
HOST = "127.0.0.1"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=8091)
    args = parser.parse_args()

    import uvicorn

    from app import leads, leads_web, main as app_main, research, valr_dashboard
    from tests.leads_sample import sample_touches

    valr_dashboard.enable_dev_preview(HOST)
    os.environ["VALR_ADMIN_EMAILS"] = valr_dashboard.PREVIEW_EMAIL
    os.environ.setdefault("VALR_LEADS", "1")

    async def collect(pool, now=None):
        now = datetime.now(timezone.utc)
        touches = sample_touches(now)
        view = leads.graph(leads.build(touches, now), now)
        view["gmail"] = leads_web.gmail_view({"watch_expires": now + timedelta(days=6), "last_sync_at": now},
                                             {"keep:llm": 14, "drop:heuristic": 212, "drop:llm": 9}, now)
        return view

    async def start_sync(reason, history_id=0):
        return True

    leads_web.collect = collect
    leads_web.start_sync = start_sync
    research.pool = object()
    print(f"Leads preview: http://{HOST}:{args.port}/valr/leads")
    uvicorn.run(app_main.app, host=HOST, port=args.port, lifespan="off", log_level="warning")


if __name__ == "__main__":
    main()
