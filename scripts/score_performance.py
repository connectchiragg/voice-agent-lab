"""Re-score stored interview analyses with the granular v2 rubric and rebuild the trends.

    python scripts/score_performance.py                         # transcripts via Fathom API key
    python scripts/score_performance.py --from-export DIR       # plus an MCP/JSON export
    python scripts/score_performance.py --purge                 # then delete raw downloads

Reads the v1 analyses in VALR_EVALUATION_PATH; re-fetches transcripts (Fathom API
if FATHOM_API_KEY is set, Fireflies API if FIREFLIES_API_KEY is set, and/or
--from-export) so sub-scores can quote the candidate. Interviews without a
transcript are scored from the stored analysis. Writes VALR_PERFORMANCE_PATH,
VALR_PERFORMANCE_CSV and updates the evaluation file to schema v2 (v1 keys kept).
Prints counts, badges and token spend only.
"""
import argparse
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.profile import importer, recordings  # noqa: E402
from app.profile.config import load_env, settings, write_private  # noqa: E402
from app.profile.llm import AnthropicJsonLLM  # noqa: E402
from app.profile.profile import redact_compensation  # noqa: E402


def main(argv: list[str] | None = None, llm=None, http=None) -> int:
    load_env()
    cfg = settings()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--from-export", type=Path)
    parser.add_argument("--purge", action="store_true",
                        help="delete cached raw downloads after scoring")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    record = json.loads(cfg.evaluation_path.read_text(encoding="utf-8"))
    cache = recordings.RawCache(cfg.cache_dir)
    sources = importer.Sources(fathom_key=os.getenv("FATHOM_API_KEY") or None,
                               fireflies_key=os.getenv("FIREFLIES_API_KEY") or None,
                               export_path=args.from_export, http=http or recordings.Http())
    meetings, status = importer.fetch_all(sources, recordings.window_start(args.days), cache)
    by_id = {f"{m['source']}:{m['id']}": m for m in meetings}
    comp = importer.load_comp(cfg.profile_path)
    llm = llm or AnthropicJsonLLM()
    record = importer.score_performance(cfg, record, by_id, llm, comp)
    write_private(cfg.evaluation_path, redact_compensation(
        json.dumps(record, indent=2, ensure_ascii=False), comp) + "\n")

    perf = json.loads(cfg.performance_path.read_text(encoding="utf-8"))
    used = sum(1 for iv in perf["interviews"] if iv["transcript_used"])
    for name, value in status.items():
        print(f"{name}: {value}")
    print(f"scored {len(perf['interviews'])} interviews ({used} with transcripts); "
          f"errors: {len(perf['errors'])}")
    for dim, s in perf["trends"]["dimensions"].items():
        print(f"{dim}: {s['badge']} (n={s['n']}, change {s['fitted_change']})")
    print(f"token spend: {llm.ledger.summary()}")
    print(f"saved {cfg.performance_path}, {cfg.performance_csv}, {cfg.evaluation_path}")
    if args.purge:
        print("Purged raw cache." if cache.purge() else "No raw cache to purge.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
