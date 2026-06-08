import argparse
import logging
import os
import time
from pathlib import Path

import requests

from analytics import market_metrics
from monitor import fetch_listings, sold_search_url
from proxy import ProfileProxyStore, redact_proxy_url, request_proxies
from storage import MonitorStore

LOGGER = logging.getLogger(__name__)


def scan_profiles(store: MonitorStore, proxy_store: ProfileProxyStore) -> tuple[int, int]:
    profiles = store.profiles(enabled_only=True)
    all_active = {}
    for profile in profiles:
        proxy_url = proxy_store.get(profile.id)
        with requests.Session() as session:
            if proxy_url:
                session.proxies.update(request_proxies(proxy_url) or {})
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
            "%s: active=%s sold=%s/%s sold_per_month=%.1f demand=%s proxy=%s",
            profile.name, len(active), metrics.accepted_count, metrics.raw_count,
            metrics.sold_per_month, metrics.demand, redact_proxy_url(proxy_url) or "direct",
        )
    events = store.record_scan(list(all_active.values()))
    return len(profiles), len(events)


def run(database_path: Path, once: bool, interval: int) -> None:
    with MonitorStore(database_path) as store, ProfileProxyStore(database_path) as proxy_store:
        while True:
            profiles, listings = scan_profiles(store, proxy_store)
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
