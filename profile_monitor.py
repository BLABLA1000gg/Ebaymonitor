from __future__ import annotations
import argparse
import logging
import os
import time
from pathlib import Path

import json

import requests

from analytics import market_metrics
from browser_fetch import BrowserFetcher
from buyback import BuybackScraper
from marketplaces import EBAY, marketplace_for_url
from monitor import fetch_listings, sold_search_url
from proxy import ProfileProxyStore, redact_proxy_url, request_proxies
from settings import SettingsStore
from storage import MonitorStore

LOGGER = logging.getLogger(__name__)


def scan_profiles(
    store: MonitorStore,
    proxy_store: ProfileProxyStore,
    errors: list[str] | None = None,
    settings=None,
) -> tuple[int, int]:
    if settings is None:
        with SettingsStore(store.connection.execute("PRAGMA database_list").fetchone()[2]) as ss:
            settings = ss.load()
    profiles = store.profiles(enabled_only=True)
    all_active = {}
    successful_profiles = 0
    for profile in profiles:
        try:
            marketplace = marketplace_for_url(profile.ebay_url)
            proxy_url = proxy_store.get(profile.id)
            with requests.Session() as session:
                if proxy_url:
                    session.proxies.update(request_proxies(proxy_url) or {})
                # eBay requires a real (non-headless) browser to bypass Akamai bot
                # detection. Always use BrowserFetcher for eBay profiles.
                use_browser = marketplace is EBAY or settings.browser_fetch
                browser_context = BrowserFetcher(proxy_url) if use_browser else None
                if browser_context:
                    browser_context.__enter__()
                try:
                    active = [
                        item for item in fetch_listings(
                            session,
                            profile.ebay_url,
                            browser_fetcher=browser_context,
                        )
                        if profile.listing_filter.matches(item)
                    ]
                    if marketplace.supports_sold_search:
                        sold_url = sold_search_url(profile.ebay_url)
                        sold = [
                            item for item in fetch_listings(
                                session,
                                sold_url,
                                browser_fetcher=browser_context,
                            )
                            if profile.listing_filter.matches(item)
                        ]
                    else:
                        sold_url = profile.ebay_url
                        sold = []
                finally:
                    if browser_context:
                        browser_context.__exit__(None, None, None)
        except requests.RequestException as error:
            message = f"{profile.name}: {error}"
            LOGGER.warning("Profile scan skipped: %s", message)
            if errors is not None:
                errors.append(message)
            continue
        # Fetch buyback prices if configured.
        # Run outside the main BrowserFetcher context (browser already closed).
        ct_prices: dict = {}
        zoxs_prices: dict = {}
        wirkaufens_prices: dict = {}
        needs_buyback = profile.clevertronic_url or profile.zoxs_url or profile.wirkaufens_url
        if needs_buyback:
            try:
                with BuybackScraper() as bs:
                    if profile.clevertronic_url:
                        ct_prices = {k: str(v) for k, v in bs.clevertronic(profile.clevertronic_url).items()}
                        LOGGER.info("%s: Clevertronic prices: %s", profile.name, ct_prices)
                    if profile.zoxs_url:
                        zoxs_prices = {k: str(v) for k, v in bs.zoxs(profile.zoxs_url).items()}
                        LOGGER.info("%s: ZOXS Ankaufpreise: %s", profile.name, zoxs_prices)
                    if profile.wirkaufens_url:
                        wirkaufens_prices = {k: str(v) for k, v in bs.wirkaufens(profile.wirkaufens_url).items()}
                        LOGGER.info("%s: WirKaufens Ankaufpreise: %s", profile.name, wirkaufens_prices)
            except Exception as err:
                LOGGER.warning("%s: Buyback fetch failed: %s", profile.name, err)

        successful_profiles += 1
        metrics = market_metrics(sold, len(active), profile.sold_window_days)
        store.record_profile_analysis(profile, sold_url, active, metrics,
                                      ct_prices or None, zoxs_prices or None, wirkaufens_prices or None)
        for item in active:
            all_active[item.link] = item
        LOGGER.info(
            "%s: active=%s sold=%s/%s sold_per_month=%.1f demand=%s proxy=%s",
            profile.name, len(active), metrics.accepted_count, metrics.raw_count,
            metrics.sold_per_month, metrics.demand, redact_proxy_url(proxy_url) or "direct",
        )
    events = store.record_scan(list(all_active.values())) if successful_profiles else []
    return len(profiles), len(events)


def run(database_path: Path, once: bool) -> None:
    with MonitorStore(database_path) as store, \
         ProfileProxyStore(database_path) as proxy_store, \
         SettingsStore(database_path) as ss:
        while True:
            settings = ss.load()
            profiles, listings = scan_profiles(store, proxy_store, settings=settings)
            LOGGER.info("Profile scan complete: %s profiles, %s active listings", profiles, listings)
            if once:
                return
            time.sleep(settings.check_interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run all enabled dashboard search profiles")
    parser.add_argument("--once", action="store_true", help="Run one profile scan and exit")
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    run(Path(os.getenv("DATABASE_PATH", "ebay_monitor.db")), args.once)


if __name__ == "__main__":
    main()
