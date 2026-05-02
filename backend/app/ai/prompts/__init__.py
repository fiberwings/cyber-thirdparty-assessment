"""Prompt loaders. Templates live as .md files alongside this module."""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).parent


def load(name: str) -> str:
    return (_HERE / f"{name}.md").read_text(encoding="utf-8")
