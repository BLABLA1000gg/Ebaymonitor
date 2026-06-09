from __future__ import annotations
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from profile_monitor import scan_profiles
from proxy import ProfileProxyStore
from storage import MonitorStore

LOGGER = logging.getLogger(__name__)


class MonitorController:
    def __init__(self, database_path: str | Path, interval_seconds: int = 300):
        self.database_path = Path(database_path)
        self.interval_seconds = max(30, interval_seconds)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._scanning = False
        self._last_started: str | None = None
        self._last_finished: str | None = None
        self._last_result: str | None = None
        self._last_error: str | None = None

    def status(self) -> dict:
        with self._lock:
            return {
                "running": bool(self._thread and self._thread.is_alive()),
                "scanning": self._scanning,
                "interval_seconds": self.interval_seconds,
                "last_started": self._last_started,
                "last_finished": self._last_finished,
                "last_result": self._last_result,
                "last_error": self._last_error,
            }

    def start(self) -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(target=self._run_loop, name="marketplace-monitor", daemon=True)
            self._thread.start()
            return True

    def stop(self) -> bool:
        with self._lock:
            if not self._thread or not self._thread.is_alive():
                return False
            self._stop.set()
            return True

    def scan_once(self) -> bool:
        with self._lock:
            if self._scanning:
                return False
            threading.Thread(target=self._scan, name="marketplace-scan", daemon=True).start()
            return True

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            self._scan()
            self._stop.wait(self.interval_seconds)

    def _scan(self) -> None:
        with self._lock:
            if self._scanning:
                return
            self._scanning = True
            self._last_started = _now()
            self._last_error = None
        try:
            errors: list[str] = []
            with MonitorStore(self.database_path) as store, ProfileProxyStore(self.database_path) as proxies:
                profiles, listings = scan_profiles(store, proxies, errors)
            with self._lock:
                self._last_result = f"{profiles} profile(s), {listings} listing event(s)"
                self._last_error = " | ".join(errors) if errors else None
        except Exception as error:
            LOGGER.exception("Dashboard monitor scan failed")
            with self._lock:
                self._last_error = str(error)
        finally:
            with self._lock:
                self._scanning = False
                self._last_finished = _now()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
