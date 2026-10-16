"""Paths and settings for the candidate-profile importer, all overridable by env.

Everything this package writes is private: it lives under data/ (gitignored),
files are created 0600 and the raw-download cache directory 0700.
"""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO = Path(__file__).resolve().parents[2]


def load_env() -> None:
    """Load VALR_ENV_FILE if set, else the repo's .env. Never echoes values."""
    load_dotenv(os.getenv("VALR_ENV_FILE") or REPO / ".env")


def _path(name: str, default: str) -> Path:
    value = Path(os.getenv(name) or default).expanduser()
    return value if value.is_absolute() else REPO / value


@dataclass(frozen=True)
class Settings:
    profile_path: Path
    evaluation_path: Path
    scores_path: Path
    performance_path: Path
    performance_csv: Path
    compensation_path: Path
    cache_dir: Path
    candidate_name: str
    classifier_model: str
    evaluator_model: str


def settings() -> Settings:
    return Settings(
        profile_path=_path("VALR_PROFILE_PATH", "data/profile.json"),
        evaluation_path=_path("VALR_EVALUATION_PATH", "data/evaluation.json"),
        # Dated score table for a dashboard chart; a .json twin is written next to it.
        scores_path=_path("VALR_SCORES_PATH", "data/interview_scores.csv"),
        # Schema v2 (granular rubric, chart-ready trends): see app/profile/performance.py.
        performance_path=_path("VALR_PERFORMANCE_PATH", "data/performance.json"),
        performance_csv=_path("VALR_PERFORMANCE_CSV", "data/performance_scores.csv"),
        compensation_path=_path("VALR_COMPENSATION_PATH", "data/compensation.json"),
        cache_dir=_path("VALR_RECORDINGS_CACHE", "data/raw_recordings"),
        candidate_name=os.getenv("VALR_CANDIDATE_NAME", "Chirag"),
        classifier_model=os.getenv("VALR_CLASSIFIER_MODEL", "claude-haiku-5-5"),
        evaluator_model=os.getenv("VALR_EVALUATOR_MODEL", "claude-sonnet-5-5"),
    )


def write_private(path: Path, text: str) -> None:
    """Write a file only the owner can read (0600), creating its parent 0700."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o600)
