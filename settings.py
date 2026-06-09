"""
Global application settings stored in the SQLite database.
Values override environment variables when set via the web UI.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field, fields
from pathlib import Path

SETTINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_DEFAULTS: dict[str, str] = {
    "discord_webhook_url":    "",
    "check_interval_seconds": "300",
    "notify_existing":        "false",
    "notify_price_increases": "false",
    "notify_statistics":      "false",
    "browser_fetch":          "false",
}


@dataclass
class AppSettings:
    discord_webhook_url: str = ""
    check_interval_seconds: int = 300
    notify_existing: bool = False
    notify_price_increases: bool = False
    notify_statistics: bool = False
    browser_fetch: bool = False


class SettingsStore:
    def __init__(self, path: str | Path):
        self._conn = sqlite3.connect(str(path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SETTINGS_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def load(self) -> AppSettings:
        rows = {r[0]: r[1] for r in self._conn.execute("SELECT key, value FROM app_settings")}
        # Fall back to env vars, then hardcoded defaults
        def get(k: str) -> str:
            return rows.get(k) or os.getenv(k.upper(), _DEFAULTS[k])

        return AppSettings(
            discord_webhook_url=get("discord_webhook_url"),
            check_interval_seconds=max(30, int(get("check_interval_seconds") or 300)),
            notify_existing=_bool(get("notify_existing")),
            notify_price_increases=_bool(get("notify_price_increases")),
            notify_statistics=_bool(get("notify_statistics")),
            browser_fetch=_bool(get("browser_fetch")),
        )

    def save(self, settings: AppSettings) -> None:
        pairs = [
            ("discord_webhook_url",    settings.discord_webhook_url),
            ("check_interval_seconds", str(settings.check_interval_seconds)),
            ("notify_existing",        _str_bool(settings.notify_existing)),
            ("notify_price_increases", _str_bool(settings.notify_price_increases)),
            ("notify_statistics",      _str_bool(settings.notify_statistics)),
            ("browser_fetch",          _str_bool(settings.browser_fetch)),
        ]
        with self._conn:
            self._conn.executemany(
                "INSERT INTO app_settings(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                pairs,
            )


def _bool(v: str) -> bool:
    return v.strip().lower() in {"1", "true", "yes"}

def _str_bool(v: bool) -> str:
    return "true" if v else "false"
