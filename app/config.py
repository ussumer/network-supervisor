"""Runtime configuration, all from environment variables."""

from __future__ import annotations

import os


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, "") or default))
    except (TypeError, ValueError):
        return default


class Config:
    def __init__(self) -> None:
        self.clash_api: str = (os.getenv("CLASH_API") or "http://host.docker.internal:9090").rstrip("/")
        self.secret: str = os.getenv("CLASH_SECRET") or ""
        self.secret_file: str = os.getenv("CLASH_SECRET_FILE") or "/flclash/shared_preferences.json"
        self.poll_interval: float = max(0.5, _f("POLL_INTERVAL", 1.0))
        self.db_path: str = os.getenv("DB_PATH") or "/data/usage.db"
        self.listen_host: str = os.getenv("LISTEN_HOST") or "0.0.0.0"
        self.port: int = _i("PORT", 8787)
        self.tz_offset: float = _f("TZ_OFFSET_HOURS", 8.0)
        self.budget_gb: float = _f("BUDGET_GB", 100.0)
        self.billing_mode: str = (os.getenv("BILLING_MODE") or "updown").lower()
        self.detail_days: int = _i("DETAIL_RETENTION_DAYS", 30)
        self.cycle_mode: str = (os.getenv("CYCLE_MODE") or "calendar").lower()
        self.cycle_start_day: int = _i("CYCLE_START_DAY", 1)
        self.cycle_days: int = _i("CYCLE_DAYS", 30)
        # multi-device support
        self.device_name: str = os.getenv("DEVICE_NAME") or "local"
        self.ingest_token: str = os.getenv("INGEST_TOKEN") or ""
        self.dashboard_token: str = os.getenv("DASHBOARD_TOKEN") or ""

    @property
    def budget_bytes(self) -> int:
        return int(self.budget_gb * 1024 ** 3)

    def defaults(self) -> dict:
        """Settings seeded on first run, then owned by the database."""
        return {
            "budget_gb": self.budget_gb,
            "billing_mode": self.billing_mode,
            "cycle_mode": self.cycle_mode,
            "cycle_start_day": self.cycle_start_day,
            "cycle_days": self.cycle_days,
        }

    def __repr__(self) -> str:
        return (
            f"Config(api={self.clash_api!r}, db={self.db_path!r}, port={self.port}, "
            f"poll={self.poll_interval}s, budget={self.budget_gb}GB, billing={self.billing_mode})"
        )
