"""Provenance capture: which app build and which models produced a run.

Assumes the backend at BENCH_BACKEND_URL is the co-located checkout in
MAIN_REPO_DIR; backend_url is recorded on the run so mismatches are auditable.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

from .config import settings


def git_sha(repo_dir: str | None = None) -> tuple[str, bool]:
    """Return (sha, dirty). ("", False) when git is unavailable."""
    repo = repo_dir or settings.MAIN_REPO_DIR
    try:
        sha = subprocess.run(
            ["git", "-C", repo, "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        porcelain = subprocess.run(
            ["git", "-C", repo, "status", "--porcelain"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return sha, bool(porcelain)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "", False


def app_version(repo_dir: str | None = None) -> str:
    pyproject = Path(repo_dir or settings.MAIN_REPO_DIR) / "backend" / "pyproject.toml"
    try:
        return tomllib.loads(pyproject.read_text())["project"]["version"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return ""


def models_snapshot(app_client, overrides: dict[str, str] | None) -> dict:
    """Merge the overrides the runner set with the app's profile defaults so
    un-overridden stages remain attributable."""
    try:
        profiles = app_client.get_models()
    except Exception:
        profiles = []
    return {
        "overrides": overrides or {},
        "profiles": profiles,
    }
