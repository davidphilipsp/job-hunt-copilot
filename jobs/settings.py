"""Personal settings for the job finder, sonar page and voice coach.

Everything that describes *you* (name, city, pay range, proofs, gaps, allowed scope numbers) lives in
`config.yaml` at the repository root, never in code. Copy `config.example.yaml` to `config.yaml` and edit it.
If `config.yaml` is missing, the example file is loaded so the tools still run with placeholder values.
`JOBHUNT_CONFIG` can point to another file.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _path() -> Path:
    env = os.environ.get("JOBHUNT_CONFIG")
    if env:
        return Path(env).expanduser()
    own = ROOT / "config.yaml"
    return own if own.exists() else ROOT / "config.example.yaml"


CONFIG_PATH = _path()
CFG: dict = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}


def get(dotted: str, default=None):
    node = CFG
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def regex(dotted: str, default: str = r"(?!x)x") -> re.Pattern:
    """Compile a case-insensitive regex from config; the default never matches."""
    return re.compile(get(dotted) or default, re.I)


def read_optional(rel: str | None) -> str:
    """Read a text file referenced by config (relative to the repo root); empty string if unset/missing."""
    if not rel:
        return ""
    p = (ROOT / rel).expanduser()
    return p.read_text(encoding="utf-8") if p.exists() else ""
