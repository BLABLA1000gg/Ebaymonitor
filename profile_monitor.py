from __future__ import annotations
import argparse
import logging
import os
import time
from pathlib import Path

import concurrent.futures
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

        # --- New flow: platform checkboxes + DeepSeek spec extraction ---
        if profile.buyback_platforms:
            try:
                _fetch_buyback_dynamic(
                    profile, active, settings,
                    ct_prices, zoxs_prices, wirkaufens_prices,
                )
            except Exception as err:
                LOGGER.warning("%s: Dynamic buyback fetch failed: %s", profile.name, err)

        # --- Legacy flow: manual URLs ---
        elif profile.clevertronic_url or profile.zoxs_url or profile.wirkaufens_url:
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


def _fetch_buyback_dynamic(profile, active, settings, ct_prices, zoxs_prices, wirkaufens_prices):
    """
    Dynamic buyback price fetch using platform checkboxes + DeepSeek spec extraction.

    Strategy:
    1. Extract base keyword from the eBay URL (e.g. 'iPhone 12')
    2. For each active listing use DeepSeek (or heuristic) to extract storage GB
    3. Find the most common storage variant across listings
    4. Search each selected platform with <keyword> + <GB>
    5. Pick the best matching product and scrape prices
    """
    from buyback_search import search_zoxs, search_wirkaufens, search_clevertronic
    from deepseek_extract import extract_specs_batch, build_search_query

    base_keyword = profile.ebay_search_keyword or profile.include_keywords
    if not base_keyword:
        LOGGER.warning("%s: No keyword found for dynamic buyback search", profile.name)
        return

    provider = settings.ai_provider if settings else "none"
    if provider == "nvidia":
        api_key = settings.nvidia_api_key if settings else ""
    elif provider == "deepseek":
        api_key = settings.deepseek_api_key if settings else ""
    else:
        api_key = ""

    # Extract specs from active listings in one batch call (max 30 listings)
    # Heuristic runs first; LLM only called for titles without a GB match
    sample_titles = [item.title for item in active[:30]]
    all_specs = extract_specs_batch(sample_titles, api_key=api_key, provider=provider)

    gb_votes: dict[int, int] = {}
    for specs in all_specs:
        if specs and specs.get("storage_gb"):
            gb = specs["storage_gb"]
            gb_votes[gb] = gb_votes.get(gb, 0) + 1

    dominant_gb = max(gb_votes, key=gb_votes.get) if gb_votes else None
    refined_query = build_search_query(base_keyword, {"storage_gb": dominant_gb})

    LOGGER.info(
        "%s: Dynamic buyback search — keyword=%r dominant_gb=%s query=%r platforms=%s",
        profile.name, base_keyword, dominant_gb, refined_query, profile.buyback_platforms,
    )

    # Search all selected platforms in parallel
    platforms = profile.buyback_platforms
    search_fns = {
        "zoxs": search_zoxs,
        "wirkaufens": search_wirkaufens,
        "clevertronic": search_clevertronic,
    }
    platform_results: dict[str, list[dict]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futures = {
            platform: ex.submit(search_fns[platform], refined_query)
            for platform in platforms if platform in search_fns
        }
        for platform, future in futures.items():
            try:
                platform_results[platform] = future.result(timeout=30)
            except Exception as err:
                LOGGER.warning("%s: %s search failed: %s", profile.name, platform, err)
                platform_results[platform] = []

    # For each platform pick best matching product and scrape prices
    with BuybackScraper() as bs:
        if "clevertronic" in platform_results and platform_results["clevertronic"]:
            best = _best_match(platform_results["clevertronic"], dominant_gb)
            if best:
                try:
                    prices = bs.clevertronic(best["url"])
                    ct_prices.update({k: str(v) for k, v in prices.items()})
                    LOGGER.info("%s: Clevertronic [%s]: %s", profile.name, best["name"], ct_prices)
                except Exception as err:
                    LOGGER.warning("%s: Clevertronic scrape failed: %s", profile.name, err)

        if "zoxs" in platform_results and platform_results["zoxs"]:
            best = _best_match(platform_results["zoxs"], dominant_gb)
            if best:
                try:
                    prices = bs.zoxs(best["url"])
                    zoxs_prices.update({k: str(v) for k, v in prices.items()})
                    LOGGER.info("%s: ZOXS [%s]: %s", profile.name, best["name"], zoxs_prices)
                except Exception as err:
                    LOGGER.warning("%s: ZOXS scrape failed: %s", profile.name, err)

        if "wirkaufens" in platform_results and platform_results["wirkaufens"]:
            best = _best_match(platform_results["wirkaufens"], dominant_gb)
            if best:
                try:
                    prices = bs.wirkaufens(best["url"])
                    wirkaufens_prices.update({k: str(v) for k, v in prices.items()})
                    LOGGER.info("%s: WirKaufens [%s]: %s", profile.name, best["name"], wirkaufens_prices)
                except Exception as err:
                    LOGGER.warning("%s: WirKaufens scrape failed: %s", profile.name, err)


def _best_match(products: list[dict], target_gb: int | None) -> dict | None:
    """Pick the product whose name best matches the target storage size."""
    if not products:
        return None
    if not target_gb:
        return products[0]
    gb_str = str(target_gb)
    # Prefer exact GB match in name
    for p in products:
        if gb_str in p.get("name", ""):
            return p
    return products[0]


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
