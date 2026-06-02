"""Time helpers — all DB `createdAt` columns use Indian Standard Time (IST, UTC+5:30).

We use a fixed offset (India does not observe DST) so the value matches the
wall-clock time in India regardless of where the server is running (Mac in Mumbai,
Docker container in AWS Mumbai/Singapore, gunicorn in any timezone).

The datetime returned is **naive** (tzinfo stripped) because MySQL `DATETIME`
columns don't store timezone — they store whatever you hand them. By stripping
the tz after computing in IST, MySQL receives the wall-clock IST time directly.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> datetime:
    """Return the current Indian Standard Time as a naive datetime suitable for
    MySQL DATETIME columns (no tzinfo attached)."""
    return datetime.now(IST).replace(tzinfo=None)
