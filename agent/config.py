"""Load config.yaml + profile.md."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str | os.PathLike | None = None) -> dict:
    p = Path(path) if path else ROOT / "config.yaml"
    with open(p, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    cfg.setdefault("keywords", [])
    cfg.setdefault("locations", ["Remote"])
    cfg.setdefault("sources", {}).setdefault("enabled", ["dice", "gmail", "serpapi"])
    cfg.setdefault("http", {})
    cfg.setdefault("analysis", {})
    cfg.setdefault("sheet", {})
    return cfg


def load_profile(path: str | os.PathLike | None = None) -> str:
    p = Path(path) if path else ROOT / "profile.md"
    return p.read_text(encoding="utf-8")
