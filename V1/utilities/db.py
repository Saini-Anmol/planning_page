"""DB connection helpers — one place to vary credentials."""
from __future__ import annotations

import mysql.connector

try:
    from sqlalchemy import create_engine as _create_engine
except ImportError:
    _create_engine = None


def connect(db_cfg: dict):
    """Plain mysql.connector connection (used by ad-hoc writers)."""
    return mysql.connector.connect(
        host=db_cfg["host"],
        port=db_cfg["port"],
        user=db_cfg["user"],
        password=db_cfg["password"],
        database=db_cfg["database"],
    )


def engine(db_cfg: dict):
    """SQLAlchemy engine (required by the LP scheduler's pandas.read_sql calls)."""
    if _create_engine is None:
        raise ImportError("sqlalchemy not installed — required for engine()")
    return _create_engine(
        f"mysql+pymysql://{db_cfg['user']}:{db_cfg['password']}"
        f"@{db_cfg['host']}:{db_cfg['port']}/{db_cfg['database']}"
    )
