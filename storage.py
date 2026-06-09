from __future__ import annotations
import csv
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import json

from analytics import MarketMetrics, deal_score, flip_profit
from models import EventType, Listing, ListingEvent
from profiles import SearchProfile

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (link TEXT PRIMARY KEY,title TEXT NOT NULL,price_text TEXT NOT NULL,current_price TEXT,currency TEXT,image_url TEXT,condition TEXT,shipping TEXT,location TEXT,first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS price_history (id INTEGER PRIMARY KEY AUTOINCREMENT,link TEXT NOT NULL,price TEXT,price_text TEXT NOT NULL,currency TEXT,observed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS search_profiles (id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL UNIQUE,ebay_url TEXT NOT NULL,include_keywords TEXT NOT NULL DEFAULT '',exclude_keywords TEXT NOT NULL DEFAULT '',min_price TEXT,max_price TEXT,currency TEXT,sold_window_days INTEGER NOT NULL DEFAULT 90,enabled INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS market_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT,profile_id INTEGER NOT NULL,sold_url TEXT NOT NULL,raw_sold_count INTEGER NOT NULL,accepted_sold_count INTEGER NOT NULL,average_sold_price TEXT,median_sold_price TEXT,minimum_sold_price TEXT,maximum_sold_price TEXT,sold_per_month TEXT NOT NULL,active_count INTEGER NOT NULL,sell_through_rate TEXT,estimated_days_to_sell TEXT,demand TEXT NOT NULL,observed_at TEXT NOT NULL,FOREIGN KEY(profile_id) REFERENCES search_profiles(id));
CREATE TABLE IF NOT EXISTS profile_listings (profile_id INTEGER NOT NULL,link TEXT NOT NULL,deal_score TEXT,last_seen TEXT NOT NULL,PRIMARY KEY(profile_id,link),FOREIGN KEY(profile_id) REFERENCES search_profiles(id));
CREATE INDEX IF NOT EXISTS idx_price_history_link_time ON price_history(link,observed_at);
CREATE INDEX IF NOT EXISTS idx_market_snapshots_profile_time ON market_snapshots(profile_id,observed_at);
"""

_MIGRATIONS = [
    "ALTER TABLE search_profiles ADD COLUMN ebay_reference_url TEXT",
    "ALTER TABLE search_profiles ADD COLUMN clevertronic_url TEXT",
    "ALTER TABLE profile_listings ADD COLUMN clevertronic_prices TEXT",
    "ALTER TABLE search_profiles ADD COLUMN zoxs_url TEXT",
    "ALTER TABLE profile_listings ADD COLUMN zoxs_prices TEXT",
    "ALTER TABLE search_profiles ADD COLUMN wirkaufens_url TEXT",
    "ALTER TABLE profile_listings ADD COLUMN wirkaufens_prices TEXT",
    "ALTER TABLE search_profiles ADD COLUMN buyback_platforms TEXT",
]


class MonitorStore:
    def __init__(self, path: str | Path):
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(SCHEMA)
        self._migrate()

    def _migrate(self):
        for stmt in _MIGRATIONS:
            try:
                with self.connection:
                    self.connection.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def save_profile(self, profile: SearchProfile) -> int:
        now = datetime.now(timezone.utc).isoformat()
        platforms_json = json.dumps(profile.buyback_platforms) if profile.buyback_platforms else None
        base = (profile.name, profile.ebay_url, profile.include_keywords, profile.exclude_keywords,
                _decimal(profile.min_price), _decimal(profile.max_price), profile.currency,
                profile.sold_window_days, int(profile.enabled),
                profile.ebay_reference_url or None, profile.clevertronic_url or None,
                profile.zoxs_url or None, profile.wirkaufens_url or None, platforms_json)
        with self.connection:
            if profile.id is None:
                cursor = self.connection.execute(
                    "INSERT INTO search_profiles(name,ebay_url,include_keywords,exclude_keywords,min_price,max_price,currency,sold_window_days,enabled,ebay_reference_url,clevertronic_url,zoxs_url,wirkaufens_url,buyback_platforms,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    base + (now, now),
                )
                return int(cursor.lastrowid)
            self.connection.execute(
                "UPDATE search_profiles SET name=?,ebay_url=?,include_keywords=?,exclude_keywords=?,min_price=?,max_price=?,currency=?,sold_window_days=?,enabled=?,ebay_reference_url=?,clevertronic_url=?,zoxs_url=?,wirkaufens_url=?,buyback_platforms=?,updated_at=? WHERE id=?",
                base + (now, profile.id),
            )
            return profile.id

    def profiles(self, enabled_only=False):
        query = "SELECT * FROM search_profiles" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY name"
        return [self._profile(row) for row in self.connection.execute(query)]

    def profile(self, profile_id):
        row = self.connection.execute("SELECT * FROM search_profiles WHERE id=?", (profile_id,)).fetchone()
        return self._profile(row) if row else None

    def delete_profile(self, profile_id):
        with self.connection:
            self.connection.execute("DELETE FROM profile_listings WHERE profile_id=?", (profile_id,))
            self.connection.execute("DELETE FROM market_snapshots WHERE profile_id=?", (profile_id,))
            self.connection.execute("DELETE FROM search_profiles WHERE id=?", (profile_id,))

    @staticmethod
    def _profile(row):
        keys = row.keys()
        ref = row["ebay_reference_url"] if "ebay_reference_url" in keys else None
        ct = row["clevertronic_url"] if "clevertronic_url" in keys else None
        zoxs = row["zoxs_url"] if "zoxs_url" in keys else None
        wkfs = row["wirkaufens_url"] if "wirkaufens_url" in keys else None
        platforms_raw = row["buyback_platforms"] if "buyback_platforms" in keys else None
        platforms = json.loads(platforms_raw) if platforms_raw else []
        return SearchProfile(row["id"], row["name"], row["ebay_url"], row["include_keywords"],
                             row["exclude_keywords"], Decimal(row["min_price"]) if row["min_price"] else None,
                             Decimal(row["max_price"]) if row["max_price"] else None, row["currency"],
                             row["sold_window_days"], bool(row["enabled"]),
                             ebay_reference_url=ref, clevertronic_url=ct, zoxs_url=zoxs, wirkaufens_url=wkfs,
                             buyback_platforms=platforms)

    def record_scan(self, listings: list[Listing]):
        now = datetime.now(timezone.utc).isoformat()
        events = []
        seen = {item.link for item in listings}
        with self.connection:
            for item in listings:
                old = self.connection.execute("SELECT current_price FROM listings WHERE link=?", (item.link,)).fetchone()
                previous = Decimal(old["current_price"]) if old and old["current_price"] else None
                kind = EventType.NEW if old is None else EventType.UNCHANGED
                if item.price is not None and previous is not None:
                    kind = EventType.PRICE_DROP if item.price < previous else EventType.PRICE_INCREASE if item.price > previous else kind
                self.connection.execute(
                    """INSERT INTO listings(link,title,price_text,current_price,currency,image_url,condition,shipping,location,first_seen,last_seen,active) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)
                    ON CONFLICT(link) DO UPDATE SET title=excluded.title,price_text=excluded.price_text,current_price=excluded.current_price,currency=excluded.currency,image_url=excluded.image_url,condition=excluded.condition,shipping=excluded.shipping,location=excluded.location,last_seen=excluded.last_seen,active=1""",
                    (item.link,item.title,item.price_text,_decimal(item.price),item.currency,item.image_url,item.condition,item.shipping,item.location,now,now),
                )
                if old is None or item.price != previous:
                    self.connection.execute("INSERT INTO price_history(link,price,price_text,currency,observed_at) VALUES(?,?,?,?,?)", (item.link,_decimal(item.price),item.price_text,item.currency,now))
                events.append(ListingEvent(kind, item, previous))
            if seen:
                marks = ",".join("?" for _ in seen)
                self.connection.execute(f"UPDATE listings SET active=0 WHERE link NOT IN ({marks})", tuple(seen))
            else:
                self.connection.execute("UPDATE listings SET active=0")
        return events

    def record_profile_analysis(
        self,
        profile: SearchProfile,
        sold_url: str,
        active: list[Listing],
        metrics: MarketMetrics,
        clevertronic_prices: dict | None = None,
        zoxs_prices: dict | None = None,
        wirkaufens_prices: dict | None = None,
    ):
        if profile.id is None:
            raise ValueError("Profile must be saved before analysis")
        now = datetime.now(timezone.utc).isoformat()
        ct_json = json.dumps(clevertronic_prices) if clevertronic_prices else None
        zoxs_json = json.dumps(zoxs_prices) if zoxs_prices else None
        wkfs_json = json.dumps(wirkaufens_prices) if wirkaufens_prices else None
        with self.connection:
            self.connection.execute(
                "INSERT INTO market_snapshots(profile_id,sold_url,raw_sold_count,accepted_sold_count,average_sold_price,median_sold_price,minimum_sold_price,maximum_sold_price,sold_per_month,active_count,sell_through_rate,estimated_days_to_sell,demand,observed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (profile.id,sold_url,metrics.raw_count,metrics.accepted_count,_decimal(metrics.average),_decimal(metrics.median),_decimal(metrics.minimum),_decimal(metrics.maximum),str(metrics.sold_per_month),metrics.active_count,_decimal(metrics.sell_through_rate),_decimal(metrics.estimated_days_to_sell),metrics.demand,now),
            )
            for item in active:
                self.connection.execute(
                    "INSERT INTO profile_listings(profile_id,link,deal_score,clevertronic_prices,zoxs_prices,wirkaufens_prices,last_seen) VALUES(?,?,?,?,?,?,?) ON CONFLICT(profile_id,link) DO UPDATE SET deal_score=excluded.deal_score,clevertronic_prices=excluded.clevertronic_prices,zoxs_prices=excluded.zoxs_prices,wirkaufens_prices=excluded.wirkaufens_prices,last_seen=excluded.last_seen",
                    (profile.id,item.link,_decimal(deal_score(item.price, metrics.median)),ct_json,zoxs_json,wkfs_json,now),
                )

    def dashboard(self, profile_id=None, shipping_cost=Decimal("5.00"), fee_rate=Decimal("0.1235")):
        profiles = self.profiles()
        selected = profile_id or (profiles[0].id if profiles else None)
        snapshot = trend = deals = None
        ref_median: Decimal | None = None
        if selected is not None:
            snapshot = self.connection.execute("SELECT * FROM market_snapshots WHERE profile_id=? ORDER BY observed_at DESC LIMIT 1", (selected,)).fetchone()
            trend = self.connection.execute("SELECT * FROM market_snapshots WHERE profile_id=? ORDER BY observed_at ASC LIMIT 180", (selected,)).fetchall()
            raw_deals = self.connection.execute("SELECT l.*,pl.deal_score FROM profile_listings pl JOIN listings l ON l.link=pl.link WHERE pl.profile_id=? AND l.active=1 ORDER BY CAST(pl.deal_score AS REAL) DESC LIMIT 50", (selected,)).fetchall()

            # Arbitrage: if the profile has an ebay_reference_url, look up the
            # latest median sold price for that eBay search to compute flip profit.
            selected_profile = next((p for p in profiles if p.id == selected), None)
            if selected_profile and selected_profile.ebay_reference_url:
                ref_snap = self.connection.execute(
                    "SELECT median_sold_price FROM market_snapshots WHERE sold_url LIKE ? ORDER BY observed_at DESC LIMIT 1",
                    (selected_profile.ebay_reference_url.split("?")[0] + "%",),
                ).fetchone()
                if ref_snap and ref_snap["median_sold_price"]:
                    ref_median = Decimal(ref_snap["median_sold_price"])

            deals = []
            for row in raw_deals:
                d = dict(row)
                price = Decimal(row["current_price"]) if row["current_price"] else None
                if price and ref_median:
                    profit, roi = flip_profit(price, ref_median, shipping_cost, fee_rate)
                    d["flip_profit"] = float(profit)
                    d["flip_roi"] = float(roi) if roi is not None else None
                    d["ref_median"] = float(ref_median)
                else:
                    d["flip_profit"] = None
                    d["flip_roi"] = None
                    d["ref_median"] = None
                # Buyback prices (stored as JSON strings)
                ct_raw = row["clevertronic_prices"] if "clevertronic_prices" in row.keys() else None
                d["clevertronic_prices"] = json.loads(ct_raw) if ct_raw else None
                zoxs_raw = row["zoxs_prices"] if "zoxs_prices" in row.keys() else None
                d["zoxs_prices"] = json.loads(zoxs_raw) if zoxs_raw else None
                wkfs_raw = row["wirkaufens_prices"] if "wirkaufens_prices" in row.keys() else None
                d["wirkaufens_prices"] = json.loads(wkfs_raw) if wkfs_raw else None
                deals.append(d)

        return {"profiles":profiles,"selected_profile_id":selected,"snapshot":dict(snapshot) if snapshot else None,"trend":[dict(x) for x in trend or []],"deals":deals or []}

    def price_history(self, link):
        return [dict(row) for row in self.connection.execute("SELECT price,observed_at FROM price_history WHERE link=? ORDER BY observed_at", (link,))]

    def export_csv(self, directory):
        target = Path(directory); target.mkdir(parents=True, exist_ok=True)
        paths = target/"listings.csv", target/"price_history.csv", target/"sold_statistics.csv"
        for path, query in zip(paths, ("SELECT * FROM listings ORDER BY last_seen DESC","SELECT * FROM price_history ORDER BY observed_at DESC","SELECT * FROM market_snapshots ORDER BY observed_at DESC")):
            rows = self.connection.execute(query).fetchall()
            with path.open("w", newline="", encoding="utf-8") as handle:
                if rows:
                    writer = csv.DictWriter(handle, fieldnames=rows[0].keys()); writer.writeheader(); writer.writerows(dict(row) for row in rows)
        return paths


def _decimal(value):
    return str(value) if value is not None else None
