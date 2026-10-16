"""Add interviewer impression cues and the coach's read to performance.json (schema v3).

    python scripts/refresh_insights.py                 # Fathom transcripts via FATHOM_API_KEY
    python scripts/refresh_insights.py --from-export DIR
    python scripts/refresh_insights.py --no-fetch      # no transcripts: derived data + coach only
    python scripts/refresh_insights.py --force-coach   # rewrite the coach read even if cached

Does not re-score the rubric (scripts/score_performance.py does). Transcripts are
fetched into a fresh 0700 scratch directory (VALR_SCRATCH_DIR's parent, else the system
temp dir) that is deleted when the run ends, success or not. Fireflies transcripts are
not fetched: those interviews keep their stored derived data. Prints counts, coverage
and spend only (no names, quotes or figures).
"""
import argparse
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.profile import importer, recordings  # noqa: E402
from app.profile.config import load_env, settings  # noqa: E402
from app.profile.llm import AnthropicJsonLLM  # noqa: E402


def main(argv: list[str] | None = None, llm=None, http=None) -> int:
    load_env()
    cfg = settings()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--from-export", type=Path)
    parser.add_argument("--no-fetch", action="store_true")
    parser.add_argument("--force-coach", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    scratch = Path(tempfile.mkdtemp(prefix="valr-insights-", dir=os.getenv("VALR_SCRATCH_DIR") or None))
    os.chmod(scratch, 0o700)
    try:
        status: dict = {}
        meetings: list[dict] = []
        if not args.no_fetch:
            sources = importer.Sources(fathom_key=os.getenv("FATHOM_API_KEY") or None,
                                       export_path=args.from_export,
                                       http=http or recordings.Http())
            meetings, status = importer.fetch_all(sources, recordings.window_start(args.days),
                                                  recordings.RawCache(scratch / "raw"))
        by_id = {f"{m['source']}:{m['id']}": m for m in meetings}
        llm = llm or AnthropicJsonLLM()
        perf = importer.refresh_insights(cfg, by_id, llm, importer.load_comp(cfg.profile_path),
                                         force_coach=args.force_coach)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    for name, value in status.items():
        print(f"{name}: {value}")
    coverage: dict[str, int] = {}
    for iv in perf["interviews"]:
        kind = (iv.get("impression") or {}).get("coverage", "none")
        coverage[kind] = coverage.get(kind, 0) + 1
    imp = perf.get("impressions") or {}
    print(f"interviews: {len(perf['interviews'])}; impression coverage: {coverage}")
    print(f"impression by outcome: { {k: v for k, v in (imp.get('by_outcome') or {}).items()} }; "
          f"r with rubric overall: {imp.get('r_with_rubric_overall')}")
    print(f"coach read: {'yes' if perf.get('coach') else 'no'}; errors: {len(perf.get('errors') or [])}")
    print(f"token spend: {llm.ledger.summary()}")
    print(f"saved {cfg.performance_path}, {cfg.performance_csv}; scratch deleted: {not scratch.exists()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
