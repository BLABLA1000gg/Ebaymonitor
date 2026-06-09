from __future__ import annotations
import argparse
import logging
import os
import time
from pathlib import Path

import concurrent.futures
import json
import re
from decimal import Decimal

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
            proxy_url = proxy_store.get(profile.id)
            # Collect all URLs to scan: primary + extra
            all_urls = [_no_auctions(u) for u in [profile.ebay_url] + (profile.extra_urls or [])]
            active_seen: dict[str, object] = {}  # dedup by link
            sold = []
            sold_url = profile.ebay_url

            for scan_url in all_urls:
                marketplace = marketplace_for_url(scan_url)
                with requests.Session() as session:
                    if proxy_url:
                        session.proxies.update(request_proxies(proxy_url) or {})
                    use_browser = marketplace is EBAY or settings.browser_fetch
                    browser_context = BrowserFetcher(proxy_url) if use_browser else None
                    if browser_context:
                        browser_context.__enter__()
                    try:
                        for item in fetch_listings(session, scan_url, browser_fetcher=browser_context):
                            if profile.listing_filter.matches(item):
                                active_seen[item.link] = item
                        if marketplace.supports_sold_search and scan_url == profile.ebay_url:
                            sold_url = sold_search_url(scan_url)
                            sold = [
                                item for item in fetch_listings(
                                    session, sold_url, browser_fetcher=browser_context,
                                )
                                if profile.listing_filter.matches(item)
                            ]
                    finally:
                        if browser_context:
                            browser_context.__exit__(None, None, None)

            active = list(active_seen.values())
            LOGGER.info("%s: fetched %d listings across %d URLs", profile.name, len(active), len(all_urls))
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

        # Per-listing condition detection + worth-it scoring
        listing_extras = {}
        has_buyback = ct_prices or zoxs_prices or wirkaufens_prices
        if has_buyback:
            from condition_detect import (
                detect_condition, is_worth_it, COND_LABELS,
                ai_assess_listing_batch, COND_BROKEN,
                ai_assess_listing_full, _fetch_ka_details, _fetch_vinted_details,
            )
            provider = settings.ai_provider if settings else "none"
            api_key = (settings.nvidia_api_key if provider == "nvidia"
                       else settings.deepseek_api_key if provider == "deepseek"
                       else "") if settings else ""
            ship = Decimal(str(settings.shipping_cost_eur)) if settings else Decimal("5")
            fee  = Decimal(str(settings.ebay_fee_rate))     if settings else Decimal("0.1235")

            # DeepSeek: text batch first (no vision available)
            default_assessment = {"condition": None, "functional": True,
                                   "battery_ok": True, "has_box": False, "has_cable": False}
            assessments: list[dict] = []
            if api_key and provider == "deepseek" and active:
                try:
                    batch_in = [(item.title, getattr(item, "description", "") or "")
                                for item in active]
                    assessments = ai_assess_listing_batch(batch_in, api_key=api_key, provider=provider)
                except Exception as err:
                    LOGGER.warning("%s: AI text-batch failed: %s", profile.name, err)
            while len(assessments) < len(active):
                assessments.append(default_assessment.copy())

            # ── Model-match guard ────────────────────────────────────────────
            # Extract required model tokens from the profile name so we can
            # reject listings that are a completely different device.
            # E.g. profile "iphone 13 pro" → tokens ["13","pro"]
            # An "iPhone XS Max" title misses both → rejected.
            _profile_tokens = re.findall(
                r'\b(\d+|pro|max|mini|plus|ultra|se|xs|xr)\b',
                profile.name.lower(),
            )

            # Modifiers that indicate a CHEAPER variant — if in title but not
            # in profile, the listing is a different (lower-value) device.
            _DOWNGRADE_MODS = {"mini", "se"}
            _profile_name_l = profile.name.lower()

            def _title_matches_profile(title: str) -> bool:
                if not _profile_tokens:
                    return True
                tl = title.lower()
                # All profile model tokens must appear in the title
                if not all(re.search(r'\b' + re.escape(t) + r'\b', tl)
                           for t in _profile_tokens):
                    return False
                # Title must not introduce a cheaper variant modifier
                # e.g. "mini" in title but profile is "iphone 13" (not mini)
                for mod in _DOWNGRADE_MODS:
                    if (re.search(r'\b' + mod + r'\b', tl)
                            and mod not in _profile_name_l):
                        return False
                return True

            # ── Parallel per-listing assessment ─────────────────────────────
            # Each listing is processed in its own thread:
            #   1. Model-match guard  (reject wrong device)
            #   2. Fetch real description (KA/Vinted page fetch)
            #   3. Regex hard-block   (multilingual broken keywords)
            #   4. AI vision assessment (title + description + 1 image)
            #   5. Text-only AI fallback when vision unavailable
            #   6. functional=False or condition=0 → skip
            #   7. ROI calculation
            # 3 workers — NVIDIA free tier allows ~3 concurrent requests safely.

            def _assess_item(item, text_assess):
                """Process one listing. Returns (link, extras_dict)."""
                _skip = {"detected_condition": "Defekt", "worth_it": False,
                         "condition_profit": None, "condition_roi": None}
                try:
                    # ── Guard 0: model mismatch ──────────────────────────────
                    if not _title_matches_profile(item.title):
                        LOGGER.info("%s: SKIP (falsches Modell) — %s",
                                    profile.name, item.title[:60])
                        return item.link, _skip

                    item_desc = getattr(item, "description", "") or ""

                    # ── Guard 1: fetch real description for KA/Vinted ────────
                    if item.link:
                        try:
                            if "kleinanzeigen.de" in item.link:
                                _, fetched = _fetch_ka_details(item.link)
                                if fetched:
                                    item_desc = fetched
                            elif "vinted." in item.link:
                                _, fetched = _fetch_vinted_details(item.link)
                                if fetched:
                                    item_desc = fetched
                        except Exception:
                            pass

                    # ── Guard 2: regex hard-block (title + description) ──────
                    raw_cond = detect_condition(item.title, description=item_desc)
                    if raw_cond == COND_BROKEN:
                        LOGGER.info("%s: SKIP (defekt-keywords) — %s",
                                    profile.name, item.title[:60])
                        return item.link, _skip

                    # ── Guard 3: AI vision assessment ────────────────────────
                    # Primary: vision model (title + description + 1 photo)
                    # Fallback: text-only batch assessment
                    # If BOTH fail: skip listing (safe default — don't buy blind)
                    assess = None
                    if api_key and provider == "nvidia":
                        try:
                            assess = ai_assess_listing_full(
                                title=item.title,
                                description=item_desc,
                                image_url=item.image_url,
                                listing_url=item.link,
                                api_key=api_key,
                                provider=provider,
                                max_images=1,
                            )
                        except Exception as ve:
                            LOGGER.warning("%s: vision failed for %s: %s",
                                           profile.name, item.title[:40], ve)

                        # Fallback: text-only when vision returned None
                        # (happens for KA listings whose images need auth)
                        if assess is None:
                            try:
                                batch = ai_assess_listing_batch(
                                    [(item.title, item_desc)],
                                    api_key=api_key, provider=provider,
                                )
                                if batch:
                                    assess = batch[0]
                            except Exception:
                                pass

                    # If BOTH vision AND text-only failed → skip listing
                    if api_key and provider == "nvidia" and assess is None:
                        LOGGER.info("%s: SKIP (AI nicht verfügbar) — %s",
                                    profile.name, item.title[:60])
                        return item.link, _skip

                    # Fall back to text_assess (DeepSeek batch result or default)
                    if assess is None:
                        assess = text_assess

                    # ── Guard 4: condition 0 → always non-functional ─────────
                    if assess.get("condition") == COND_BROKEN:
                        assess = dict(assess, functional=False)

                    # ── Guard 5: non-functional → skip ───────────────────────
                    if not assess.get("functional", True):
                        LOGGER.info("%s: SKIP (nicht funktional) — %s",
                                    profile.name, item.title[:60])
                        return item.link, _skip

                    # ── Condition score priority ─────────────────────────────
                    # Platform field > AI (if more pessimistic) > regex
                    if item.condition:
                        cond_score = detect_condition(item.title, platform_condition=item.condition)
                        ai_cond = assess.get("condition")
                        if ai_cond is not None and ai_cond < cond_score:
                            LOGGER.info("%s: AI overrides platform %s→%s — %s",
                                        profile.name, COND_LABELS.get(cond_score),
                                        COND_LABELS.get(ai_cond), item.title[:50])
                            cond_score = ai_cond
                    elif assess.get("condition") is not None:
                        cond_score = assess["condition"]
                    else:
                        cond_score = raw_cond

                    worth, profit, roi = is_worth_it(
                        listing_price=item.price or Decimal("0"),
                        condition_score=cond_score,
                        zoxs_prices=zoxs_prices or None,
                        wkfs_prices=wirkaufens_prices or None,
                        clevertronic_prices=ct_prices or None,
                        shipping_cost=ship,
                        fee_rate=fee,
                        title=item.title,
                        description=item_desc,
                        api_key=api_key,
                        provider=provider,
                    )
                    if worth:
                        LOGGER.info(
                            "%s: WORTH IT — %s | Zustand=%s functional=%s "
                            "battery=%s box=%s Profit=%.0f€ ROI=%.0f%%",
                            profile.name, item.title[:50],
                            COND_LABELS.get(cond_score),
                            assess.get("functional"), assess.get("battery_ok"),
                            assess.get("has_box"),
                            profit or 0, (roi or 0) * 100,
                        )
                    return item.link, {
                        "detected_condition": COND_LABELS.get(cond_score, "Gut"),
                        "worth_it": worth,
                        "condition_profit": float(profit) if profit is not None else None,
                        "condition_roi": roi,
                    }
                except Exception as e:
                    LOGGER.warning("%s: _assess_item error for %s: %s",
                                   profile.name, item.title[:40], e)
                    # On unexpected crash: never mark as worth-it
                    return item.link, {"detected_condition": "Gut", "worth_it": False,
                                       "condition_profit": None, "condition_roi": None}

            # 3 workers: safe for NVIDIA free-tier rate limits (avoids 429 errors)
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
                futures = {
                    ex.submit(_assess_item, item, assessments[i]): item
                    for i, item in enumerate(active)
                }
                for future in concurrent.futures.as_completed(futures):
                    try:
                        link, extras = future.result(timeout=120)
                        listing_extras[link] = extras
                    except Exception as e:
                        item = futures[future]
                        LOGGER.warning("%s: assessment timeout/error for %s: %s",
                                       profile.name, item.title[:40], e)

        store.record_profile_analysis(profile, sold_url, active, metrics,
                                      ct_prices or None, zoxs_prices or None,
                                      wirkaufens_prices or None, listing_extras or None)
        for item in active:
            all_active[item.link] = item
        LOGGER.info(
            "%s: active=%s sold=%s/%s sold_per_month=%.1f demand=%s proxy=%s",
            profile.name, len(active), metrics.accepted_count, metrics.raw_count,
            metrics.sold_per_month, metrics.demand, redact_proxy_url(proxy_url) or "direct",
        )
    events = store.record_scan(list(all_active.values())) if successful_profiles else []
    return len(profiles), len(events)


def _no_auctions(url: str) -> str:
    """Append LH_BIN=1 to eBay URLs to exclude auction listings."""
    if "ebay.de" in url and "LH_BIN" not in url:
        sep = "&" if "?" in url else "?"
        return url + sep + "LH_BIN=1"
    return url


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

    # For each platform pick best matching product and scrape prices.
    # We always scrape with functional=True / battery_ok=True to get the FULL
    # condition price table. The per-listing assessment then picks the right tier
    # (e.g. a non-functional listing uses COND_BROKEN which maps to the lowest price).
    with BuybackScraper() as bs:
        if "clevertronic" in platform_results and platform_results["clevertronic"]:
            best = _best_match(platform_results["clevertronic"], dominant_gb)
            if best:
                try:
                    prices = bs.clevertronic(best["url"], functional=True, battery_ok=True)
                    ct_prices.update({k: str(v) for k, v in prices.items()})
                    LOGGER.info("%s: Clevertronic [%s]: %s", profile.name, best["name"], ct_prices)
                except Exception as err:
                    LOGGER.warning("%s: Clevertronic scrape failed: %s", profile.name, err)

        if "zoxs" in platform_results and platform_results["zoxs"]:
            best = _best_match(platform_results["zoxs"], dominant_gb)
            if best:
                try:
                    prices = bs.zoxs(best["url"], functional=True, battery_ok=True,
                                     has_box=False, has_cable=False)
                    zoxs_prices.update({k: str(v) for k, v in prices.items()})
                    LOGGER.info("%s: ZOXS [%s]: %s", profile.name, best["name"], zoxs_prices)
                except Exception as err:
                    LOGGER.warning("%s: ZOXS scrape failed: %s", profile.name, err)

        if "wirkaufens" in platform_results and platform_results["wirkaufens"]:
            best = _best_match(platform_results["wirkaufens"], dominant_gb)
            if best:
                try:
                    prices = bs.wirkaufens(best["url"], functional=True, battery_ok=True)
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
