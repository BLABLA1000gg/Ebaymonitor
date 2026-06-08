import csv
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from analytics import MarketMetrics, deal_score
from models import EventType, Listing, ListingEvent
from profiles import SearchProfile


SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    link TEXT PRIMARY KEY, title TEXT NOT NULL, price_text TEXT NOT NULL,
    current_price TEXT, currency TEXT, image_url TEXT, condition TEXT,
    shipping TEXT, location TEXT, first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, link TEXT NOT NULL, price TEXT,
    price_text TEXT NOT NULL, currency TEXT, observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS search_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
    ebay_url TEXT NOT NULL, include_keywords TEXT NOT NULL DEFAULT '',
    exclude_keywords TEXT NOT NULL DEFAULT '', min_price TEXT, max_price TEXT,
    currency TEXT, sold_window_days INTEGER NOT NULL DEFAULT 90,
    enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, profile_id INTEGER NOT NULL,
    sold_url TEXT NOT NULL, raw_sold_count INTEGER NOT NULL,
    accepted_sold_count INTEGER NOT NULL, average_sold_price TEXT,
    median_sold_price TEXT, minimum_sold_price TEXT, maximum_sold_price TEXT,
    sold_per_month TEXT NOT NULL, active_count INTEGER NOT NULL,
    sell_through_rate TEXT, estimated_days_to_sell TEXT,
    demand TEXT NOT NULL, observed_at TEXT NOT NULL,
    FOREIGN KEY(profile_id) REFERENCES search_profiles(id)
);
CREATE TABLE IF NOT EXISTS profile_listings (
    profile_id INTEGER NOT NULL, link TEXT NOT NULL, deal_score TEXT,
    last_seen TEXT NOT NULL, PRIMARY KEY(profile_id, link),
    FOREIGN KEY(profile_id) REFERENCES search_profiles(id)
);
CREATE INDEX IF NOT EXISTS idx_price_history_link_time ON price_history(link, observed_at);
CREATE INDEX IF NOT EXISTS idx_market_snapshots_profile_time ON market_snapshots(profile_id, observed_at);
"""


class MonitorStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(SCHEMA)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def save_profile(self, profile: SearchProfile) -> int:
        now = datetime.now(timezone.utc).isoformat()
        values = (
            profile.name, profile.ebay_url, profile.include_keywords,
            profile.exclude_keywords,
            str(profile.min_price) if profile.min_price is not None else None,
            str(profile.max_price) if profile.max_price is not None else None,
            profile.currency, profile.sold_window_days, int(profile.enabled), now,
        )
        with self.connection:
            if profile.id is None:
                cursor = self.connection.execute(
                    """
                    INSERT INTO search_profiles (
                        name, ebay_url, include_keywords, exclude_keywords,
                        min_price, max_price, currency, sold_window_days,
                        enabled, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                return int(cursor.lastrowid)
            self.connection.execute(
                """
                UPDATE search_profiles SET name=?, ebay_url=?, include_keywords=?,
                    exclude_keywords=?, min_price=?, max_price=?, currency=?,
                    sold_window_days=?, enabled=?, updated_at=? WHERE id=?
                """,
                values[:-1] + (now, profile.id),
            )
            return profile.id

    def profiles(self, enabled_only: bool = False) -> list[SearchProfile]:
        query = "SELECT * FROM search_profiles"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY name"
        return [self._profile(row) for row in self.connection.execute(query)]

    def profile(self, profile_id: int) -> SearchProfile | None:
        row = self.connection.execute(
            "SELECT * FROM search_profiles WHERE id = ?", (profile_id,)
        ).fetchone()
        return self._profile(row) if row else None

    def delete_profile(self, profile_id: int) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM profile_listings WHERE profile_id = ?", (profile_id,))
            self.connection.execute("DELETE FROM market_snapshots WHERE profile_id = ?", (profile_id,))
            self.connection.execute("DELETE FROM search_profiles WHERE id = ?", (profile_id,))

    @staticmethod
    def _profile(row) -> SearchProfile:
        return SearchProfile(
            id=row["id"], name=row["name"], ebay_url=row["ebay_url"],
            include_keywords=row["include_keywords"],
            exclude_keywords=row["exclude_keywords"],
            min_price=Decimal(row["min_price"]) if row["min_price"] else None,
            max_price=Decimal(row["max_price"]) if row["max_price"] else None,
            currency=row["currency"], sold_window_days=row["sold_window_days"],
            enabled=bool(row["enabled"]),
        )

    def record_scan(self, listings: list[Listing]) -> list[ListingEvent]:
        now = datetime.now(timezone.utc).isoformat()
        events: list[ListingEvent] = []
        seen_links = {listing.link for listing in listings}
        with self.connection:
            for listing in listings:
                previous = self.connection.execute(
                    "SELECT current_price FROM listings WHERE link = ?", (listing.link,)
                ).fetchone()
                previous_price = Decimal(previous["current_price"]) if previous and previous["current_price"] else None
                if previous is None:
                    event_type = EventType.NEW
                elif listing.price is not None and previous_price is not None and listing.price < previous_price:
                    event_type = EventType.PRICE_DROP
                elif listing.price is not None and previous_price is not None and listing.price > previous_price:
                    event_type = EventType.PRICE_INCREASE
                else:
                    event_type = EventType.UNCHANGED
                self.connection.execute(
                    """
                    INSERT INTO listings (
                        link, title, price_text, current_price, currency, image_url,
                        condition, shipping, location, first_seen, last_seen, active
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT(link) DO UPDATE SET title=excluded.title,
                        price_text=excluded.price_text, current_price=excluded.current_price,
                        currency=excluded.currency, image_url=excluded.image_url,
                        condition=excluded.condition, shipping=excluded.shipping,
                        location=excluded.location, last_seen=excluded.last_seen, active=1
                    """,
                    (
                        listing.link, listing.title, listing.price_text,
                        str(listing.price) if listing.price is not None else None,
                        listing.currency, listing.image_url, listing.condition,
                        listing.shipping, listing.location, now, now,
                    ),
                )
                if previous is None or listing.price != previous_price:
                    self.connection.execute(
                        "INSERT INTO price_history (link, price, price_text, currency, observed_at) VALUES (?, ?, ?, ?, ?)",
                        (listing.link, str(listing.price) if listing.price is not None else None, listing.price_text, listing.currency, now),
                    )
                events.append(ListingEvent(event_type, listing, previous_price))
            if seen_links:
                placeholders = ",".join("?" for _ in seen_links)
                self.connection.execute(
                    f"UPDATE listings SET active = 0 WHERE link NOT IN ({placeholders})", tuple(seen_links)
                )
            else:
                self.connection.execute("UPDATE listings SET active = 0")
        return events

    def record_profile_analysis(
        self,
        profile: SearchProfile,
        sold_url: str,
        active: list[Listing],
        metrics: MarketMetrics,
    ) -> None:
        if profile.id is None:
            raise ValueError("Profile must be saved before analysis")
        now = datetime.now(timezone.utc).isoformat()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO market_snapshots (
                    profile_id, sold_url, raw_sold_count, accepted_sold_count,
                    average_sold_price, median_sold_price, minimum_sold_price,
                    maximum_sold_price, sold_per_month, active_count,
                    sell_through_rate, estimated_days_to_sell, demand, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.id, sold_url, metrics.raw_count, metrics.accepted_count,
                    _decimal(metrics.average), _decimal(metrics.median),
                    _decimal(metrics.minimum), _decimal(metrics.maximum),
                    str(metrics.sold_per_month), metrics.active_count,
                    _decimal(metrics.sell_through_rate),
                    _decimal(metrics.estimated_days_to_sell), metrics.demand, now,
                ),
            )
            for listing in active:
                score = deal_score(listing.price, metrics.median)
                self.connection.execute(
                    """
                    INSERT INTO profile_listings (profile_id, link, deal_score, last_seen)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(profile_id, link) DO UPDATE SET
                        deal_score=excluded.deal_score, last_seen=excluded.last_seen
                    """,
                    (profile.id, listing.link, _decimal(score), now),
                )

    def dashboard(self, profile_id: int | None = None) -> dict:
        profiles = self.profiles()
        selected = profile_id or (profiles[0].id if profiles else None)
        snapshot = None
        trend = []
        deals = []
        if selected is not None:
            snapshot = self.connection.execute(
                "SELECT * FROM market_snapshots WHERE profile_id=? ORDER BY observed_at DESC LIMIT 1",
                (selected,),
            ).fetchone()
            trend = self.connection.execute(
                "SELECT * FROM market_snapshots WHERE profile_id=? ORDER BY observed_at ASC LIMIT 180",
                (selected,),
            ).fetchall()
            deals = self.connection.execute(
                """
                SELECT l.*, pl.deal_score FROM profile_listings pl
                JOIN listings l ON l.link=pl.link
                WHERE pl.profile_id=? AND l.active=1
                ORDER BY CAST(pl.deal_score AS REAL) DESC LIMIT 50
                """,
                (selected,),
            ).fetchall()
        return {
            "profiles": profiles, "selected_profile_id": selected,
            "snapshot": dict(snapshot) if snapshot else None,
            "trend": [dict(row) for row in trend],
            "deals": [dict(row) for row in deals],
        }

    def price_history(self, link: str) -> list[dict]:
        rows = self.connection.execute(
            "SELECT price, observed_at FROM price_history WHERE link=? ORDER BY observed_at", (link,)
        ).fetchall()
        return [dict(row) for row in rows]

    def export_csv(self, directory: str | Path) -> tuple[Path, Path, Path]:
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        paths = (
            target / "listings.csv", target / "price_history.csv", target / "sold_statistics.csv"
        )
        self._write_query(paths[0], "SELECT * FROM listings ORDER BY last_seen DESC")
        self._write_query(paths[1], "SELECT * FROM price_history ORDER BY observed_at DESC")
        self._write_query(paths[2], "SELECT * FROM market_snapshots ORDER BY observed_at DESC")
        return paths

    def _write_query(self, path: Path, query: str) -> None:
        rows = self.connection.execute(query).fetchall()
        with path.open("w", newline="", encoding="utf-8") as handle:
            if not rows:
                return
            writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(dict(row) for row in rows)


def _decimal(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None
