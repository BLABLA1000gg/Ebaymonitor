import argparse
import logging
import os
import time
from pathlib import Path

import requests

from analytics import market_metrics
from monitor import fetch_listings, sold_search_url
from storage import MonitorStore

LOGGER = logging.getLogger(__name__)


def scan_profiles(session: requests.Session, store: MonitorStore) -> tuple[int, int]:
    profiles = store.profiles(enabled_only=True)
    all_active = {}
    for profile in profiles:
        active = [
            item for item in fetch_listings(session, profile.ebay_url)
            if profile.listing_filter.matches(item)
        ]
        sold_url = sold_search_url(profile.ebay_url)
        sold = [
            item for item in fetch_listings(session, sold_url)
            if profile.listing_filter.matches(item)
        ]
        metrics = market_metrics(sold, len(active), profile.sold_window_days)
        store.record_profile_analysis(profile, sold_url, active, metrics)
        for item in active:
            all_active[item.link] = item
        LOGGER.info(
            "%s: active=%s sold=%s/%s sold_per_month=%.1f demand=%s",
            profile.name, len(active), metrics.accepted_count, metrics.raw_count,
            metrics.sold_per_month, metrics.demand,
        )
    events = store.record_scan(list(all_active.values()))
    return len(profiles), len(events)


def run(database_path: Path, once: bool, interval: int) -> None:
    with requests.Session() as session, MonitorStore(database_path) as store:
        while True:
            profiles, listings = scan_profiles(session, store)
            LOGGER.info("Profile scan complete: %s profiles, %s active listings", profiles, listings)
            if once:
                return
            time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run all enabled dashboard search profiles")
    parser.add_argument("--once", action="store_true", help="Run one profile scan and exit")
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    interval = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
    if interval < 30:
        raise ValueError("CHECK_INTERVAL_SECONDS must be at least 30")
    run(Path(os.getenv("DATABASE_PATH", "ebay_monitor.db")), args.once, interval)


if __name__ == "__main__":
    main()
