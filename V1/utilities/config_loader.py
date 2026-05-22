"""YAML config loader. Resolves {plan_id} placeholders in path strings."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


def load(path: Path | str | None = None) -> dict:
    """Load YAML config from disk. Returns the parsed dict as-is."""
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(p) as f:
        return yaml.safe_load(f) or {}


def resolve_paths(cfg: dict) -> dict:
    """Replace {plan_id} placeholders in known path-template fields."""
    plan_id = cfg["plan"]["plan_id"]
    if "demand" in cfg and "output_excel" in cfg["demand"]:
        cfg["demand"]["output_excel"] = cfg["demand"]["output_excel"].format(plan_id=plan_id)
    return cfg


def input_dir(cfg: dict) -> Path:
    return PROJECT_ROOT / cfg["paths"]["input_dir"]


def output_dir(cfg: dict) -> Path:
    p = PROJECT_ROOT / cfg["paths"]["output_dir"]
    p.mkdir(parents=True, exist_ok=True)
    return p
