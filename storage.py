import csv
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from models import EventType, Listing, ListingEvent


SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    link TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    price_text TEXT NOT NULL,
    current_price TEXT,
    currency TEXT,
    image_url TEXT,
    condition TEXT,
    shipping TEXT,
    location TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    link TEXT NOT NULL,
    price TEXT,
    price_text TEXT NOT NULL,
    currency TEXT,
    observed_at TEXT NOT NULL,
    FOREIGN KEY(link) REFERENCES listings(link)
);
CREATE INDEX IF NOT EXISTS idx_price_history_link_time
ON price_history(link, observed_at);
"""


class MonitorStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def record_scan(self, listings: list[Listing]) -> list[ListingEvent]:
        now = datetime.now(timezone.utc).isoformat()
        events: list[ListingEvent] = []
        seen_links = {listing.link for listing in listings}

        with self.connection:
            for listing in listings:
                previous = self.connection.execute(
                    "SELECT current_price FROM listings WHERE link = ?", (listing.link,)
                ).fetchone()
                previous_price = Decimal(previous["current_price"]) if previous and previous["current_price"] is not None else None

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
                    ON CONFLICT(link) DO UPDATE SET
                        title=excluded.title, price_text=excluded.price_text,
                        current_price=excluded.current_price, currency=excluded.currency,
                        image_url=excluded.image_url, condition=excluded.condition,
                        shipping=excluded.shipping, location=excluded.location,
                        last_seen=excluded.last_seen, active=1
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
                        """
                        INSERT INTO price_history (link, price, price_text, currency, observed_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            listing.link,
                            str(listing.price) if listing.price is not None else None,
                            listing.price_text,
                            listing.currency,
                            now,
                        ),
                    )
                events.append(ListingEvent(event_type, listing, previous_price))

            if seen_links:
                placeholders = ",".join("?" for _ in seen_links)
                self.connection.execute(
                    f"UPDATE listings SET active = 0 WHERE link NOT IN ({placeholders})",
                    tuple(seen_links),
                )
            else:
                self.connection.execute("UPDATE listings SET active = 0")
        return events

    def export_csv(self, directory: str | Path) -> tuple[Path, Path]:
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        listings_path = target / "listings.csv"
        history_path = target / "price_history.csv"

        self._write_query(
            listings_path,
            "SELECT * FROM listings ORDER BY last_seen DESC",
        )
        self._write_query(
            history_path,
            "SELECT * FROM price_history ORDER BY observed_at DESC",
        )
        return listings_path, history_path

    def _write_query(self, path: Path, query: str) -> None:
        rows = self.connection.execute(query).fetchall()
        with path.open("w", newline="", encoding="utf-8") as handle:
            if not rows:
                handle.write("")
                return
            writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(dict(row) for row in rows)
