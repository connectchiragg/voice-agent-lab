"""Import the owner's Fathom / Fireflies meetings, find his interviews, and build a self-evaluation.

    python scripts/import_recordings.py                # last 60 days, every source with a key
    python scripts/import_recordings.py --no-llm       # fetch + heuristics only (no Claude calls)
    python scripts/import_recordings.py --from-export path/to/export.json   # or a directory of *.json
    python scripts/import_recordings.py --purge        # delete the raw-download cache and exit

Keys come from .env (VALR_ENV_FILE overrides): FATHOM_API_KEY, FIREFLIES_API_KEY,
ANTHROPIC_API_KEY. A missing vendor key skips that source. Keys are never printed.

Export format (for --from-export): {"source": "...", "meetings": [{"source", "id",
"title", "date" (ISO 8601), "duration_minutes", "participants": [{"name", "role"}],
"vendor_summary", "transcript": [{"speaker", "text", "timestamp"}]}]}.

Output: VALR_EVALUATION_PATH (default data/evaluation.json, mode 0600) holding
per-interview analysis and the <=600-token evaluation brief; no transcripts.
Raw vendor pages are cached in VALR_RECORDINGS_CACHE (default data/raw_recordings,
mode 0700) until --purge.
"""
import argparse
import logging
import os
import sys
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
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--no-cache", action="store_true", help="do not keep raw downloads")
    parser.add_argument("--purge", action="store_true", help="delete cached raw downloads, exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    cache = recordings.RawCache(cfg.cache_dir)
    if args.purge:
        print("Purged raw cache." if cache.purge() else "No raw cache to purge.")
        return 0

    sources = importer.Sources(fathom_key=os.getenv("FATHOM_API_KEY") or None,
                               fireflies_key=os.getenv("FIREFLIES_API_KEY") or None,
                               export_path=args.from_export,
                               http=http or recordings.Http())
    if not (sources.fathom_key or sources.fireflies_key or sources.export_path):
        print("Nothing to import: FATHOM_API_KEY and FIREFLIES_API_KEY are not set and no "
              "--from-export was given. Ready to run once a key is added to .env.")
        return 0
    if llm is None and not args.no_llm:
        if not os.getenv("ANTHROPIC_API_KEY"):
            print("ANTHROPIC_API_KEY is not set; use --no-llm for a fetch-only run.")
            return 1
        llm = AnthropicJsonLLM()

    record = importer.run(cfg, sources, None if args.no_llm else llm, days=args.days,
                          cache=None if args.no_cache else cache,
                          comp=importer.load_comp(cfg.profile_path))
    stats = record["stats"]
    for name, status in stats["sources"].items():
        print(f"{name}: {status}")
    print(f"meetings: {stats['meetings']}, sent to classifier: {stats['classified_by_llm']}, "
          f"interviews (owner as candidate): {stats['interviews']}, analysed: {stats['analysed']}")
    for error in stats["errors"]:
        print(f"error: {error}")
    if llm is not None and not args.no_llm:
        print(f"token spend: {llm.ledger.summary()}")
        print(f"saved {cfg.evaluation_path}")
    if not args.no_cache and cfg.cache_dir.exists():
        print(f"raw downloads cached in {cfg.cache_dir} (0700); run --purge to delete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
