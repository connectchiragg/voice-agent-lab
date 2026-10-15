"""Build VALR's private candidate profile from a resume PDF (+ owner-provided compensation).

    python scripts/build_profile.py --resume "/path/to/Resume.pdf"

Compensation is read from VALR_COMPENSATION_PATH (default data/compensation.json,
gitignored; see scripts/compensation.example.json). It is written only into the
profile (VALR_PROFILE_PATH, default data/profile.json, mode 0600) and is never
printed: this script prints the brief with compensation redacted.
"""
import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.profile import profile as profiles  # noqa: E402
from app.profile.config import load_env, settings  # noqa: E402
from app.profile.resume import extract_text, parse_resume  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    load_env()
    cfg = settings()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--resume", type=Path, default=os.getenv("VALR_RESUME_PDF"),
                        required=not os.getenv("VALR_RESUME_PDF"))
    parser.add_argument("--compensation", type=Path, default=cfg.compensation_path)
    parser.add_argument("--out", type=Path, default=cfg.profile_path)
    args = parser.parse_args(argv)

    parsed = parse_resume(extract_text(Path(args.resume)), today=date.today())
    comp = None
    if args.compensation and Path(args.compensation).exists():
        comp = json.loads(Path(args.compensation).read_text(encoding="utf-8"))
    else:
        print("No compensation file; profile.compensation will be null.")
    profile = profiles.build_profile(parsed, comp)
    profiles.save_profile(profile, args.out)

    print(f"Saved profile to {args.out} (mode 0600): {len(profile['roles'])} roles, "
          f"{len(profile['achievements'])} achievements, "
          f"compensation {'present' if profile['compensation'] else 'absent'}.")
    print(f"Brief (~{profiles.estimate_tokens(profile['brief'])} tokens, compensation redacted):")
    print(profiles.redacted_summary(profile))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
