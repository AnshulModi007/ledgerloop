"""Loads config.yaml once and exposes a stable hash for the audit log."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.yaml"


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def config_hash(config: dict[str, Any]) -> str:
    """Stable sha256 over the config's canonical JSON form, for the audit log."""
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
