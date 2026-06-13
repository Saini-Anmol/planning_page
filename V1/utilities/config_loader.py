"""YAML config loader.

Two responsibilities:
  1. Read the YAML structure (plan, paths, demand, schedule, upload).
  2. Pull DB credentials from environment variables (so secrets never need
     to live in the YAML committed to git or baked into the Docker image).

Env-var precedence:
  - JKT_DB_HOST / JKT_DB_PORT / JKT_DB_USER / JKT_DB_PASSWORD / JKT_DB_DATABASE
    override whatever is in config.yaml when set.
  - If a project-root .env file is present, its KEY=VAL lines are loaded into
    os.environ before override resolution (convenience for local dev).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
DOTENV_PATH = PROJECT_ROOT / ".env"

_DB_ENV_MAP = {
    "host":     "JKT_DB_HOST",
    "port":     "JKT_DB_PORT",
    "user":     "JKT_DB_USER",
    "password": "JKT_DB_PASSWORD",
    "database": "JKT_DB_DATABASE",
}


def _load_dotenv_if_present() -> None:
    """Minimal KEY=VAL parser — no python-dotenv dependency. Skipped silently
    if no .env file exists (e.g., inside the Docker container)."""
    if not DOTENV_PATH.exists():
        return
    with open(DOTENV_PATH) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)


def _apply_db_env_overrides(cfg: dict) -> None:
    cfg.setdefault("db", {})
    for cfg_key, env_name in _DB_ENV_MAP.items():
        val = os.environ.get(env_name)
        if val is None:
            continue
        cfg["db"][cfg_key] = int(val) if cfg_key == "port" else val


DEFAULT_MODE = "planning"


def apply_mode(cfg: dict, mode: str = DEFAULT_MODE) -> dict:
    """Resolve logical table names into physical ones for the given mode.

    Sets cfg["mode"] and cfg["tbl"] = {logical_name -> physical_table_name}.
    Modules read cfg["tbl"]["demand"] etc. instead of hardcoding "jkt_demand",
    so the same pipeline can run against jkt_* (planning) or jkt_sim_* (simulation).

    The mode token is inserted right after a leading "jkt_":
        planning   -> jkt_demand        simulation -> jkt_sim_demand
    """
    from V1.utilities.db import safe_table

    tables = cfg.get("tables", {})
    tokens = tables.get("mode_token", {})
    if mode not in tokens:
        raise ValueError(
            f"unknown mode {mode!r}; expected one of {sorted(tokens)}"
        )
    token = tokens[mode] or ""

    resolved: dict[str, str] = {}
    for logical, base in tables.items():
        if logical == "mode_token":
            continue
        if token and base.startswith("jkt_"):
            physical = "jkt_" + token + base[len("jkt_"):]
        else:
            physical = base
        resolved[logical] = safe_table(physical)

    cfg["mode"] = mode
    cfg["tbl"] = resolved
    return cfg


def load(path: Path | str | None = None, mode: str = DEFAULT_MODE) -> dict:
    """Load YAML config from disk + apply env-var overrides for DB creds.

    Also resolves table names for `mode` (default "planning") into cfg["tbl"].
    """
    _load_dotenv_if_present()
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(p) as f:
        cfg = yaml.safe_load(f) or {}
    _apply_db_env_overrides(cfg)
    apply_mode(cfg, mode)
    return cfg


def mode_file_tag(cfg: dict) -> str:
    """Filename infix that keeps planning and simulation output Excels distinct.

    Reuses the SAME mode_token used for table-name resolution: "" for planning,
    "sim_" for simulation. Planning filenames are therefore unchanged, while a
    /simulation run writes "sim_"-prefixed Excel files — so it can never
    overwrite the /plan run's output (or vice-versa) for the same plan_id.
    """
    mode = cfg.get("mode", DEFAULT_MODE)
    tokens = cfg.get("tables", {}).get("mode_token", {})
    return tokens.get(mode, "") or ""


def resolve_paths(cfg: dict) -> dict:
    """Replace {plan_id} / {mode_tag} placeholders in known path-template fields."""
    plan_id  = cfg["plan"]["plan_id"]
    mode_tag = mode_file_tag(cfg)
    if "demand" in cfg and "output_excel" in cfg["demand"]:
        cfg["demand"]["output_excel"] = cfg["demand"]["output_excel"].format(
            plan_id=plan_id, mode_tag=mode_tag
        )
    return cfg


def input_dir(cfg: dict) -> Path:
    return PROJECT_ROOT / cfg["paths"]["input_dir"]


def output_dir(cfg: dict) -> Path:
    p = PROJECT_ROOT / cfg["paths"]["output_dir"]
    p.mkdir(parents=True, exist_ok=True)
    return p
