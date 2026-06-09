from __future__ import annotations
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}


def validate_proxy_url(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    value = value.strip()
    parsed = urlsplit(value)
    if parsed.scheme.casefold() not in SUPPORTED_PROXY_SCHEMES:
        raise ValueError("Proxy scheme must be http, https, socks5 or socks5h")
    if not parsed.hostname or parsed.port is None:
        raise ValueError("Proxy URL must contain a host and port")
    return value


def request_proxies(proxy_url: str | None) -> dict[str, str] | None:
    return {"http": proxy_url, "https": proxy_url} if proxy_url else None


def redact_proxy_url(proxy_url: str | None) -> str | None:
    if not proxy_url:
        return None
    parsed = urlsplit(proxy_url)
    hostname = parsed.hostname or ""
    if ":" in hostname:
        hostname = f"[{hostname}]"
    credentials = "***:***@" if parsed.username is not None else ""
    port = f":{parsed.port}" if parsed.port is not None else ""
    return urlunsplit((parsed.scheme, f"{credentials}{hostname}{port}", parsed.path, parsed.query, parsed.fragment))


class ProfileProxyStore:
    def __init__(self, database_path: str | Path):
        self.connection = sqlite3.connect(str(database_path))
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS profile_proxies (profile_id INTEGER PRIMARY KEY, proxy_url TEXT)"
        )

    def get(self, profile_id: int) -> str | None:
        row = self.connection.execute(
            "SELECT proxy_url FROM profile_proxies WHERE profile_id=?", (profile_id,)
        ).fetchone()
        return row[0] if row else None

    def set(self, profile_id: int, proxy_url: str | None) -> None:
        proxy_url = validate_proxy_url(proxy_url)
        with self.connection:
            if proxy_url:
                self.connection.execute(
                    "INSERT INTO profile_proxies(profile_id,proxy_url) VALUES(?,?) "
                    "ON CONFLICT(profile_id) DO UPDATE SET proxy_url=excluded.proxy_url",
                    (profile_id, proxy_url),
                )
            else:
                self.connection.execute("DELETE FROM profile_proxies WHERE profile_id=?", (profile_id,))

    def delete(self, profile_id: int) -> None:
        self.set(profile_id, None)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
