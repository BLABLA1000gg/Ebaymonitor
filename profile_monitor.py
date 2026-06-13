from __future__ import annotations
import argparse
import logging
import os
import time
from pathlib import Path

import concurrent.futures
import json
import re
import threading
from decimal import Decimal

import requests

from analytics import market_metrics
from browser_fetch import BrowserFetcher
from buyback import BuybackScraper
from marketplaces import EBAY, marketplace_for_url
from condition_detect import (
    detect_condition, is_worth_it, COND_LABELS,
    ai_assess_listing_batch, COND_BROKEN, COND_ACCEPTABLE,
    ai_assess_listing_full, _fetch_ka_details, _fetch_vinted_details,
    trim_caches,
)
from monitor import fetch_listings, sold_search_url
from proxy import ProfileProxyStore, redact_proxy_url, request_proxies
from settings import SettingsStore
from storage import MonitorStore

LOGGER = logging.getLogger(__name__)

# Realistic storage tiers across supported categories (phones, iPads, consoles)
# — anything else is a misparse (model number, battery %, etc.). PS5 = 825GB,
# Xbox/PS = 1/2TB, iPad up to 2TB.
_VALID_GB = {16, 32, 64, 128, 256, 512, 825, 1000, 1024, 2048}


def _gb_candidates(text: str) -> list[int]:
    t = text.lower()
    found: list[int] = []
    for m in re.finditer(r'\b(\d{1,2})\s*tb\b', t):
        gb = int(m.group(1)) * 1024
        if gb in _VALID_GB:
            found.append(gb)
    for m in re.finditer(r'\b(\d{2,4})\s*(?:gb|go)\b', t):
        gb = int(m.group(1))
        if gb in _VALID_GB:
            found.append(gb)
    return found


def _extract_listing_gb(text: str, title: str = "") -> int | None:
    """Extract the storage size in GB from a listing.

    Handles "128GB", "128 GB", "128Go" (FR), "1TB". The title is authoritative
    when it names a size; description text may mention other variants (trade
    offers, seller boilerplate), so there the SMALLEST size wins — pricing too
    low is a missed deal, pricing too high is money lost.
    """
    if title:
        title_sizes = _gb_candidates(title)
        if title_sizes:
            return min(title_sizes)
    sizes = _gb_candidates(text)
    return min(sizes) if sizes else None


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
    trim_caches()  # bound in-memory AI caches in long-running monitor loops

    # Vinted description fetch state — shared across ALL profiles to avoid
    # fetching the same URL multiple times (same listing can appear in several
    # profiles, e.g. "iphone 13" and "iphone 13 pro").
    #
    # Vinted rate-limits detail-page fetches to a burst of ~26 requests per IP,
    # then returns empty pages. Measured behaviour: throttling to ~2.5s per
    # request sustains a high success rate, and a single retry after a longer
    # backoff recovers most transient blocks. We therefore serialize all Vinted
    # fetches, enforce a minimum gap, and retry once before giving up.
    _vinted_sem = threading.Lock()       # serialize all Vinted detail fetches
    _vinted_cache: dict = {}             # url → (imgs, desc); avoids re-fetch
    _vinted_count = [0]                  # total fetch attempts this scan
    _vinted_last = [0.0]                 # monotonic time of last fetch
    _VINTED_PAGE_CAP = 120               # hard cap to bound total scan time
    _VINTED_MIN_GAP = 2.5                # seconds between sequential fetches
    _VINTED_RETRY_BACKOFF = 6.0          # extra wait before a single retry

    def _vinted_fetch_throttled(url: str):
        """Fetch a Vinted detail page with serialization, throttling, retry
        and caching. Returns (imgs, desc) — possibly ([], "") on failure.

        Must be called WITHOUT holding _vinted_sem; it acquires the lock itself.
        """
        with _vinted_sem:
            cached = _vinted_cache.get(url)
            if cached is not None:
                return cached
            if _vinted_count[0] >= _VINTED_PAGE_CAP:
                return [], ""

            # Enforce a minimum gap since the previous fetch
            gap = time.monotonic() - _vinted_last[0]
            if gap < _VINTED_MIN_GAP:
                time.sleep(_VINTED_MIN_GAP - gap)

            imgs, desc = [], ""
            try:
                imgs, desc = _fetch_vinted_details(url)
            except Exception as e:
                LOGGER.debug("Vinted fetch error for %s: %s", url[-50:], e)
            # Retry once after a longer backoff if blocked or errored
            if not imgs and not desc:
                time.sleep(_VINTED_RETRY_BACKOFF)
                try:
                    imgs, desc = _fetch_vinted_details(url)
                except Exception as e:
                    LOGGER.debug("Vinted retry error for %s: %s", url[-50:], e)

            _vinted_last[0] = time.monotonic()
            _vinted_count[0] += 1
            if imgs or desc:
                _vinted_cache[url] = (imgs, desc)
            return imgs, desc

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
        rebuy_prices: dict = {}

        # --- New flow: platform checkboxes + DeepSeek spec extraction ---
        # buyback_by_gb: {storage_gb: {platform: condition→price}}. Each
        # listing is priced against its OWN storage variant — a 256GB phone
        # must never be evaluated with 128GB buyback prices.
        buyback_by_gb: dict[int, dict[str, dict]] = {}
        listing_gb_map: dict[str, int] = {}  # link → AI-extracted GB (fallback for Guard 5b)
        if profile.buyback_platforms:
            try:
                buyback_by_gb, listing_gb_map = _fetch_buyback_dynamic(
                    profile, active, settings,
                    ct_prices, zoxs_prices, wirkaufens_prices, rebuy_prices,
                )
            except Exception as err:
                LOGGER.warning("%s: Dynamic buyback fetch failed: %s", profile.name, err)

        # --- Legacy flow: manual URLs ---
        elif profile.clevertronic_url or profile.zoxs_url or profile.wirkaufens_url:
            LOGGER.warning(
                "%s: Legacy-Ankauf-URLs aktiv — KEINE Speichergrößen-Prüfung "
                "pro Listing. Für Blindkäufe Plattform-Checkboxen verwenden.",
                profile.name)
            def _legacy_scrape(label, fn, url):
                with BuybackScraper() as bs:
                    return label, {k: str(v) for k, v in fn(bs, url).items()}

            legacy_tasks = []
            if profile.clevertronic_url:
                legacy_tasks.append(("clevertronic",
                    lambda bs, u: bs.clevertronic(u), profile.clevertronic_url))
            if profile.zoxs_url:
                legacy_tasks.append(("zoxs",
                    lambda bs, u: bs.zoxs(u), profile.zoxs_url))
            if profile.wirkaufens_url:
                legacy_tasks.append(("wirkaufens",
                    lambda bs, u: bs.wirkaufens(u), profile.wirkaufens_url))
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=len(legacy_tasks) or 1) as ex:
                    futs = [ex.submit(_legacy_scrape, lbl, fn, url)
                            for lbl, fn, url in legacy_tasks]
                    for f in concurrent.futures.as_completed(futs):
                        try:
                            label, prices = f.result(timeout=45)
                            if label == "clevertronic":
                                ct_prices = prices
                                LOGGER.info("%s: Clevertronic prices: %s", profile.name, prices)
                            elif label == "zoxs":
                                zoxs_prices = prices
                                LOGGER.info("%s: ZOXS Ankaufpreise: %s", profile.name, prices)
                            elif label == "wirkaufens":
                                wirkaufens_prices = prices
                                LOGGER.info("%s: WirKaufens Ankaufpreise: %s", profile.name, prices)
                        except Exception as err:
                            LOGGER.warning("%s: Legacy buyback fetch failed: %s", profile.name, err)
            except Exception as err:
                LOGGER.warning("%s: Buyback fetch failed: %s", profile.name, err)

        successful_profiles += 1
        metrics = market_metrics(sold, len(active), profile.sold_window_days)

        # Per-listing condition detection + worth-it scoring
        listing_extras = {}
        has_buyback = ct_prices or zoxs_prices or wirkaufens_prices or rebuy_prices
        if has_buyback:
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

            # Semaphore: serialize Vinted page requests (bot-detection).
            # Also tracks how many Vinted pages were fetched this scan —
            # after ~20 fetches Vinted starts blocking; cap and use vision-only after.
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
                    # ── Listing entry log ────────────────────────────────────
                    _platform = (
                        "vinted" if item.link and "vinted." in item.link else
                        "ka" if item.link and "kleinanzeigen.de" in item.link else
                        "ebay" if item.link and "ebay." in item.link else "other"
                    )
                    LOGGER.debug(
                        "%s [%s] START — price=€%s cond=%s img=%s desc_len=%d — %s",
                        profile.name, _platform,
                        item.price, item.condition or "–",
                        "ja" if item.image_url else "nein",
                        len(getattr(item, "description", "") or ""),
                        item.title[:70],
                    )

                    # ── Guard 0a: unrealistic price ──────────────────────────
                    if not item.price or item.price < Decimal("20"):
                        LOGGER.info("%s: SKIP (Preis zu niedrig €%s) — %s",
                                    profile.name, item.price, item.title[:50])
                        return item.link, _skip

                    # ── Guard 0b: model mismatch ─────────────────────────────
                    if not _title_matches_profile(item.title):
                        LOGGER.info("%s: SKIP (falsches Modell) — %s",
                                    profile.name, item.title[:60])
                        return item.link, _skip

                    item_desc = getattr(item, "description", "") or ""

                    # ── Guard 1: fetch real description for KA/Vinted ────────
                    is_vinted = item.link and "vinted." in item.link
                    is_ka = item.link and "kleinanzeigen.de" in item.link

                    # ── Guard 1b: title-only regex (no network needed) ───────
                    title_cond = detect_condition(item.title, description="")
                    if title_cond == COND_BROKEN:
                        LOGGER.info("%s: SKIP (defekt-keywords Titel) — %s",
                                    profile.name, item.title[:60])
                        return item.link, _skip

                    # ── Guard 1c: fetch description (KA only; Vinted deferred) ──
                    fetch_ok = False
                    if item.link and is_ka:
                        _t0 = time.monotonic()
                        try:
                            fetched_imgs, fetched_desc = _fetch_ka_details(item.link)
                            _elapsed = time.monotonic() - _t0
                            LOGGER.debug(
                                "%s [ka] fetch — imgs=%d desc_len=%d elapsed=%.2fs — %s",
                                profile.name, len(fetched_imgs), len(fetched_desc),
                                _elapsed, item.title[:50],
                            )
                            if fetched_imgs or fetched_desc:
                                if fetched_desc:
                                    item_desc = fetched_desc
                                fetch_ok = True
                                LOGGER.debug(
                                    "%s [ka] desc snippet: %s",
                                    profile.name, fetched_desc[:120].replace("\n", " "),
                                )
                            else:
                                LOGGER.info("%s [ka] fetch leer (%.2fs) — %s",
                                            profile.name, _elapsed, item.title[:50])
                        except Exception as e:
                            LOGGER.info("%s [ka] fetch Fehler (%.2fs) %s — %s",
                                        profile.name, time.monotonic() - _t0,
                                        type(e).__name__, item.title[:50])

                    if is_ka and not fetch_ok:
                        LOGGER.info("%s: SKIP (KA Beschreibung nicht abrufbar) — %s",
                                    profile.name, item.title[:60])
                        return item.link, _skip

                    # eBay descriptions live in an iframe and are never fetched,
                    # so the only text-level verification is eBay's own condition
                    # field. Without it the listing cannot be verified — skip.
                    is_ebay = item.link and "ebay." in item.link
                    if is_ebay and not item.condition:
                        LOGGER.info("%s: SKIP (eBay ohne Zustandsangabe) — %s",
                                    profile.name, item.title[:60])
                        return item.link, _skip

                    # ── Guard 2: regex hard-block (title + description so far) ─
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
                    vision_ok = False
                    if api_key and provider == "nvidia":
                        LOGGER.debug(
                            "%s: Vision-Start — img_url=%s desc_len=%d link=%s",
                            profile.name,
                            (item.image_url or "–")[:80],
                            len(item_desc),
                            item.link[-60:],
                        )
                        _v0 = time.monotonic()
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
                            _v_elapsed = time.monotonic() - _v0
                            if assess is not None:
                                vision_ok = True
                                LOGGER.info(
                                    "%s: Vision OK (%.2fs) — cond=%s func=%s bat=%s risk=%s reason=%s — %s",
                                    profile.name, _v_elapsed,
                                    assess.get("condition"), assess.get("functional"),
                                    assess.get("battery_ok"), assess.get("risk"),
                                    (assess.get("reason") or "")[:60],
                                    item.title[:50],
                                )
                            else:
                                LOGGER.info("%s: Vision None (%.2fs) — img=%s — %s",
                                            profile.name, _v_elapsed,
                                            (item.image_url or "–")[:60],
                                            item.title[:60])
                        except Exception as ve:
                            LOGGER.warning("%s: Vision Exception (%.2fs) %s — %s: %s",
                                           profile.name, time.monotonic() - _v0,
                                           item.title[:40], type(ve).__name__, ve)

                        # Fallback: text-only when vision returned None
                        if assess is None:
                            LOGGER.info("%s: Text-Fallback — title=%s desc_len=%d",
                                        profile.name, item.title[:50], len(item_desc))
                            _tb0 = time.monotonic()
                            try:
                                batch = ai_assess_listing_batch(
                                    [(item.title, item_desc)],
                                    api_key=api_key, provider=provider,
                                )
                                _tb_elapsed = time.monotonic() - _tb0
                                if batch and batch[0] is not None:
                                    assess = batch[0]
                                    LOGGER.info(
                                        "%s: Text-Fallback OK (%.2fs) — cond=%s func=%s — %s",
                                        profile.name, _tb_elapsed,
                                        assess.get("condition"), assess.get("functional"),
                                        item.title[:50],
                                    )
                                else:
                                    LOGGER.warning("%s: Text-Fallback None (%.2fs) — %s",
                                                   profile.name, _tb_elapsed, item.title[:60])
                            except Exception as e:
                                LOGGER.warning("%s: Text-Fallback Fehler (%.2fs) %s — %s: %s",
                                               profile.name, time.monotonic() - _tb0,
                                               item.title[:40], type(e).__name__, e)

                    # If BOTH vision AND text-only failed → skip listing
                    if api_key and provider == "nvidia" and assess is None:
                        LOGGER.info("%s: SKIP (Vision + Text-Fallback beide fehlgeschlagen) — %s",
                                    profile.name, item.title[:60])
                        return item.link, _skip

                    # Fall back to text_assess (DeepSeek batch result or default)
                    if assess is None:
                        assess = text_assess

                    # ── Guard 3b: Vinted deferred description fetch ───────────
                    # Only for Vinted listings where vision says functional=True.
                    # If vision already confirmed the listing (vision_ok=True),
                    # a missing description is acceptable — vision saw the photo.
                    if is_vinted and assess and assess.get("functional"):
                        fetched_imgs, fetched_desc = _vinted_fetch_throttled(item.link)
                        if fetched_desc:
                            item_desc = fetched_desc
                            LOGGER.debug("%s: Vinted Beschreibung OK (%d Zeichen) — %s",
                                         profile.name, len(fetched_desc), item.title[:50])
                        elif vision_ok:
                            # Vision saw the photo and approved — proceed without text desc
                            LOGGER.info("%s: Vinted kein Text aber Vision OK — weiter — %s",
                                        profile.name, item.title[:60])
                        else:
                            # No vision, no description — not safe to buy blind
                            LOGGER.info("%s: SKIP (Vinted: kein Text, kein Bild verifiziert) — %s",
                                        profile.name, item.title[:60])
                            return item.link, _skip

                        # Re-run regex with the freshly fetched description
                        if detect_condition(item.title, description=item_desc) == COND_BROKEN:
                            LOGGER.info("%s: SKIP (defekt-keywords Beschreibung) — %s",
                                        profile.name, item.title[:60])
                            return item.link, _skip

                    # ── Guard 4: condition 0 → always non-functional ─────────
                    if assess.get("condition") == COND_BROKEN:
                        assess = dict(assess, functional=False)

                    # ── Guard 5: non-functional → skip ───────────────────────
                    if not assess.get("functional", True):
                        LOGGER.info("%s: SKIP (nicht funktional) — %s",
                                    profile.name, item.title[:60])
                        return item.link, _skip

                    # ── Guard 5a: AI blind-buy risk gate ─────────────────────
                    # The vision model rates how safe a SIGHT-UNSEEN purchase is.
                    # "hoch" = red flags (possible hidden damage, contradictions,
                    # suspiciously cheap). For real-money blind buying we skip it.
                    if assess.get("risk") == "hoch":
                        LOGGER.info("%s: SKIP (KI-Risiko hoch: %s) — %s",
                                    profile.name, assess.get("reason", "")[:50],
                                    item.title[:50])
                        return item.link, _skip

                    # ── Guard 5b: price against the listing's OWN storage ────
                    # Buyback tables were fetched per storage variant. Pick the
                    # table matching this listing's size; no table for its size
                    # (or size unknown) → skip. Pricing a 128GB phone with
                    # 256GB buyback prices would inflate profit by €20-80.
                    item_ct, item_zoxs, item_wkfs, item_rebuy = (
                        ct_prices, zoxs_prices, wirkaufens_prices, rebuy_prices)
                    if buyback_by_gb:
                        # 1. Regex from title + description (fastest, most reliable)
                        listing_gb = _extract_listing_gb(item_desc, title=item.title)
                        # 2. Fallback: AI-extracted GB from spec-batch in _fetch_buyback_dynamic
                        if not listing_gb:
                            listing_gb = listing_gb_map.get(item.link)
                        # 3. Fallback: try dominant GB if only one variant was fetched
                        if not listing_gb and len(buyback_by_gb) == 1:
                            listing_gb = next(iter(buyback_by_gb))
                        gb_prices = buyback_by_gb.get(listing_gb) if listing_gb else None
                        if not gb_prices:
                            LOGGER.info(
                                "%s: SKIP (kein Ankaufspreis für Speicher %s) — %s",
                                profile.name,
                                f"{listing_gb}GB" if listing_gb else "unbekannt",
                                item.title[:60])
                            return item.link, _skip
                        item_ct = gb_prices.get("clevertronic", {})
                        item_zoxs = gb_prices.get("zoxs", {})
                        item_wkfs = gb_prices.get("wirkaufens", {})
                        item_rebuy = gb_prices.get("rebuy", {})

                    # ── Displayed condition: AI is the source of truth ───────
                    # Pricing uses fixed conservative tiers regardless of this
                    # score (see matched_buyback_price), so the displayed Zustand
                    # is the AI's HONEST read of what the buyer will receive.
                    # AI > platform field (sellers inflate) > regex.
                    ai_cond = assess.get("condition")
                    if ai_cond is not None:
                        cond_score = ai_cond
                        # If the platform field is even more pessimistic, trust it
                        if item.condition:
                            plat = detect_condition(item.title, platform_condition=item.condition)
                            cond_score = min(cond_score, plat)
                    elif item.condition:
                        cond_score = detect_condition(item.title, platform_condition=item.condition)
                    else:
                        # No AI, no platform field — regex only. Grade
                        # conservatively (cap at "Gebraucht").
                        cond_score = min(raw_cond, 1)

                    worth, profit, roi = is_worth_it(
                        listing_price=item.price or Decimal("0"),
                        condition_score=cond_score,
                        zoxs_prices=item_zoxs or None,
                        wkfs_prices=item_wkfs or None,
                        clevertronic_prices=item_ct or None,
                        rebuy_prices=item_rebuy or None,
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
                        "ai_risk": assess.get("risk"),
                        "ai_reason": assess.get("reason"),
                    }
                except Exception as e:
                    LOGGER.warning("%s: _assess_item error for %s: %s",
                                   profile.name, item.title[:40], e)
                    # On unexpected crash: never mark as worth-it
                    return item.link, {"detected_condition": "Gut", "worth_it": False,
                                       "condition_profit": None, "condition_roi": None}

            # 3 workers: safe for NVIDIA free-tier rate limits (avoids 429 errors)
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
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

        worth_it_count = sum(1 for ex in listing_extras.values() if ex.get("worth_it"))
        skipped_count = sum(1 for ex in listing_extras.values() if not ex.get("worth_it"))
        LOGGER.info(
            "%s: active=%s worth-it=%s skipped=%s "
            "sold=%s/%s sold_per_month=%.1f demand=%s proxy=%s",
            profile.name, len(active), worth_it_count, skipped_count,
            metrics.accepted_count, metrics.raw_count,
            metrics.sold_per_month, metrics.demand, redact_proxy_url(proxy_url) or "direct",
        )
        if worth_it_count:
            for link, ex in listing_extras.items():
                if ex.get("worth_it"):
                    LOGGER.info(
                        "%s: WORTH IT — profit=+%.0f€ roi=%.0f%% cond=%s risk=%s — %s",
                        profile.name,
                        ex.get("condition_profit") or 0,
                        (ex.get("condition_roi") or 0) * 100,
                        ex.get("detected_condition", "?"),
                        ex.get("ai_risk", "?"),
                        next((i.title for i in active if i.link == link), link)[:60],
                    )
    # Only record when at least one profile produced listings — recording an
    # empty scan would mark EVERY stored listing inactive and wipe the
    # dashboard after a transient network failure.
    events = (store.record_scan(list(all_active.values()))
              if successful_profiles and all_active else [])

    # Telegram alerts for new worth-it deals + auctions ending soon
    if settings and (settings.telegram_bot_token and settings.telegram_chat_id):
        _send_telegram_alerts(store, settings, all_active)

    return len(profiles), len(events)


_ALERTED_LINKS: set[str] = set()  # in-memory dedup: don't re-alert same deal each scan


def _send_telegram_alerts(store, settings, all_active: dict) -> None:
    """Send Telegram messages for new worth-it deals and auctions ending within 2h."""
    try:
        data = store.dashboard()
        deals = data.get("deals") or []
        worth_it = [d for d in deals if d.get("worth_it")]
        if not worth_it:
            return

        token = settings.telegram_bot_token.strip()
        chat_id = settings.telegram_chat_id.strip()
        api_url = f"https://api.telegram.org/bot{token}/sendMessage"

        import requests as _req
        session = _req.Session()

        for deal in worth_it:
            link = deal.get("link", "")
            profit = deal.get("condition_profit")
            roi = deal.get("condition_roi")
            title = deal.get("title", "")[:60]
            price = deal.get("current_price", "?")
            risk = deal.get("ai_risk") or "–"
            end_time = deal.get("auction_end_time")
            time_left = deal.get("auction_time_left")
            bid_count = deal.get("auction_bid_count")

            is_auction = bool(end_time or time_left)
            alert_key = f"{link}:{end_time or 'bin'}"

            # Always alert auctions ending within 2h (time-sensitive)
            # For BIN deals: only alert once per link
            if alert_key in _ALERTED_LINKS:
                continue
            if is_auction and not _auction_ends_within_2h(time_left):
                continue

            profit_str = f"+{profit:.0f}€" if profit else "?"
            roi_str = f"{roi*100:.0f}%" if roi else "?"

            if is_auction:
                auction_info = ""
                if time_left:
                    auction_info += f"\n⏰ *Endet in:* {time_left}"
                if bid_count is not None:
                    auction_info += f"\n🔨 *Gebote:* {bid_count}"
                # Max-bid recommendation: buyback price - shipping - fee (conservative)
                max_bid_hint = ""
                if profit and deal.get("current_price"):
                    try:
                        current = float(deal["current_price"])
                        max_safe = current + float(profit) * 0.7  # 70% profit buffer
                        max_bid_hint = f"\n💡 *Max-Gebot empfohlen:* ~{max_safe:.0f}€"
                    except Exception:
                        pass
                text = (
                    f"🔨 *AUKTION ENDET BALD — WORTH IT*\n"
                    f"*{title}*\n"
                    f"💶 Aktuell: {price}€ | Profit: {profit_str} | ROI: {roi_str}\n"
                    f"⚠️ Risiko: {risk}"
                    f"{auction_info}{max_bid_hint}\n"
                    f"[→ Zur Auktion]({link})"
                )
            else:
                text = (
                    f"✅ *NEUER DEAL — WORTH IT*\n"
                    f"*{title}*\n"
                    f"💶 Preis: {price}€ | Profit: {profit_str} | ROI: {roi_str}\n"
                    f"⚠️ Risiko: {risk}\n"
                    f"[→ Zum Listing]({link})"
                )

            try:
                resp = session.post(api_url, json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": False,
                }, timeout=10)
                if resp.status_code == 200:
                    _ALERTED_LINKS.add(alert_key)
                    LOGGER.info("Telegram alert sent: %s", title)
                else:
                    LOGGER.warning("Telegram alert failed (%s): %s", resp.status_code, resp.text[:200])
            except Exception as e:
                LOGGER.warning("Telegram send error: %s", e)
    except Exception as e:
        LOGGER.warning("_send_telegram_alerts failed: %s", e)


def _no_auctions(url: str) -> str:
    """Append LH_BIN=1 to eBay URLs to exclude auctions — UNLESS the user
    opted into auction-sniper mode by putting LH_Auction=1 in the profile URL
    (then we leave it untouched so auctions are scanned)."""
    if "ebay.de" in url and "LH_Auction=1" in url:
        return url  # sniper profile — keep auctions
    if "ebay.de" in url and "LH_BIN" not in url:
        sep = "&" if "?" in url else "?"
        return url + sep + "LH_BIN=1"
    return url


def _ending_soonest_auctions(url: str) -> str:
    """Build an ending-soonest eBay auction URL (sniper mode):
    LH_Auction=1 (auctions only) + _sop=1 (sort by time: ending soonest).
    Drops the LH_BIN-only filter. Non-eBay URLs pass through unchanged.
    """
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    if "ebay.de" not in url and "ebay.com" not in url:
        return url
    parts = list(urlsplit(url))
    query = dict(parse_qsl(parts[3], keep_blank_values=True))
    query.pop("LH_BIN", None)
    query["LH_Auction"] = "1"
    query["_sop"] = "1"
    parts[3] = urlencode(query)
    return urlunsplit(parts)


def _auction_ends_within_2h(time_left: str | None) -> bool:
    """True if an eBay auction's time-left string indicates < 2 hours remain.
    Examples: 'Noch 22 Min' → True, 'Noch 1 Std 5 Min' → True,
    'Noch 1 T 10 Std' → False (contains days)."""
    if not time_left:
        return False
    if re.search(r"\bT\b|\bTag", time_left):  # days remain
        return False
    h = re.search(r"(\d+)\s*Std", time_left)
    return not h or int(h.group(1)) < 2


def _fetch_buyback_dynamic(profile, active, settings, ct_prices, zoxs_prices, wirkaufens_prices, rebuy_prices) -> int | None:
    """
    Dynamic buyback price fetch using platform checkboxes + DeepSeek spec extraction.

    Strategy:
    1. Extract base keyword from the eBay URL (e.g. 'iPhone 12')
    2. For each active listing use DeepSeek (or heuristic) to extract storage GB
    3. Find the most common storage variant across listings
    4. Search each selected platform with <keyword> + <GB>
    5. Pick the best matching product and scrape prices

    Returns {gb: {"clevertronic": {...}, "zoxs": {...}, "wirkaufens": {...}}}
    with full condition→price tables per storage size, so every listing can be
    priced against ITS OWN variant. Empty dict when nothing could be fetched.

    The dominant storage size's prices are additionally copied into the passed
    ct_prices/zoxs_prices/wirkaufens_prices dicts (profile-level display).
    """
    from buyback_search import (search_zoxs, search_wirkaufens, search_clevertronic,
                                search_rebuy)
    from deepseek_extract import extract_specs_batch, build_search_query

    base_keyword = profile.ebay_search_keyword or profile.include_keywords
    if not base_keyword:
        LOGGER.warning("%s: No keyword found for dynamic buyback search", profile.name)
        return {}

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

    # Build per-listing link → GB map for Guard 5b fallback
    listing_gb_map: dict[str, int] = {}
    for item, specs in zip(active[:30], all_specs):
        if specs and specs.get("storage_gb"):
            listing_gb_map[item.link] = specs["storage_gb"]

    if not gb_votes:
        LOGGER.warning("%s: No storage size detectable in any listing — "
                       "skipping buyback fetch (cannot price safely)", profile.name)
        return {}, listing_gb_map

    # Fetch prices for the most common storage variants (up to 3) so listings
    # of every relevant size get matched against their own buyback product.
    target_gbs = sorted(gb_votes, key=gb_votes.get, reverse=True)[:3]
    dominant_gb = target_gbs[0]

    # One keyword search per platform (without GB — results contain all
    # variants); per-GB product selection happens via _best_match below.
    refined_query = build_search_query(base_keyword, {"storage_gb": None})
    LOGGER.info(
        "%s: Dynamic buyback search — keyword=%r target_gbs=%s query=%r platforms=%s",
        profile.name, base_keyword, target_gbs, refined_query, profile.buyback_platforms,
    )

    platforms = profile.buyback_platforms
    search_fns = {
        "zoxs": search_zoxs,
        "wirkaufens": search_wirkaufens,
        "clevertronic": search_clevertronic,
        "rebuy": search_rebuy,
    }
    platform_results: dict[str, list[dict]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
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

    # Scrape the full condition→price table per (platform, storage size).
    # We always scrape with functional=True / battery_ok=True to get the FULL
    # table; the per-listing assessment picks the right condition tier.
    scrape_fns = {
        "clevertronic": lambda bs, url: bs.clevertronic(url, functional=True, battery_ok=True),
        "zoxs": lambda bs, url: bs.zoxs(url, functional=True, battery_ok=True,
                                        has_box=False, has_cable=False),
        "wirkaufens": lambda bs, url: bs.wirkaufens(url, functional=True, battery_ok=True),
        "rebuy": lambda bs, url: bs.rebuy(url, functional=True, battery_ok=True),
    }
    # Build all (gb, platform) scrape tasks upfront, then run them all in parallel.
    # Each call creates its own CurlSession internally so concurrent calls are safe.
    scrape_tasks: list[tuple[int, str, str, str]] = []  # (gb, platform, url, name)
    for gb in target_gbs:
        for platform in platforms:
            products = platform_results.get(platform) or []
            best = _best_match(products, gb, base_keyword)
            if best:
                scrape_tasks.append((gb, platform, best["url"], best["name"]))

    def _scrape_one(task: tuple) -> tuple:
        gb, platform, url, name = task
        with BuybackScraper() as bs:
            prices = scrape_fns[platform](bs, url)
        return gb, platform, name, {k: str(v) for k, v in prices.items()} if prices else {}

    by_gb: dict[int, dict[str, dict]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(scrape_tasks) or 1, 8)) as ex:
        futures = {ex.submit(_scrape_one, task): task for task in scrape_tasks}
        for future in concurrent.futures.as_completed(futures):
            task = futures[future]
            gb, platform = task[0], task[1]
            try:
                _, _, name, prices = future.result(timeout=45)
                if prices:
                    by_gb.setdefault(gb, {})[platform] = prices
                    LOGGER.info("%s: %s %sGB [%s]: %s", profile.name, platform, gb, name, prices)
            except Exception as err:
                LOGGER.warning("%s: %s scrape failed (%sGB): %s", profile.name, platform, gb, err)

    # Profile-level price display uses the dominant variant
    dom = by_gb.get(dominant_gb, {})
    ct_prices.update(dom.get("clevertronic", {}))
    zoxs_prices.update(dom.get("zoxs", {}))
    wirkaufens_prices.update(dom.get("wirkaufens", {}))
    rebuy_prices.update(dom.get("rebuy", {}))

    return by_gb, listing_gb_map


# Model modifiers that change the device (and its buyback price) entirely.
# A search for "iPhone 14" also returns "14 Pro"/"14 Plus" products — matching
# the wrong variant prices every listing against the wrong device ("14 Pro"
# pays ~€140 more than "14"; the inflated payout would fake-profit every deal).
_MODEL_MODS = ("pro", "max", "mini", "plus", "ultra", "se")


def _best_match(products: list[dict], target_gb: int | None,
                base_keyword: str = "") -> dict | None:
    """Pick the product matching the target storage size AND the model.

    Returns None when no verified match exists. Guessing wrong (other storage
    tier or "Pro Max" instead of "Pro") would price every listing against the
    wrong buyback product, so no match means no prices and therefore no
    WORTH IT signal — the safe default.
    """
    if not products or not target_gb:
        return None
    if target_gb >= 1024:
        gb_pattern = rf'\b{target_gb // 1024}\s*tb\b'
    else:
        # "128" must be followed by GB/GO so target 128 can't match "1280"
        # or a model number.
        gb_pattern = rf'\b{target_gb}\s*(?:gb|go)\b'

    kw = base_keyword.lower()
    kw_tokens = re.findall(r'\b(\d+|pro|max|mini|plus|ultra|se|xs|xr)\b', kw)

    def model_ok(name: str) -> bool:
        nl = name.lower()
        # All keyword model tokens must appear in the product name
        if not all(re.search(rf'\b{re.escape(t)}\b', nl) for t in kw_tokens):
            return False
        # Product must not introduce a variant modifier the keyword lacks
        # (e.g. product "13 Pro Max" for keyword "13 pro")
        for mod in _MODEL_MODS:
            if re.search(rf'\b{mod}\b', nl) and mod not in kw_tokens:
                return False
        return True

    for p in products:
        name = p.get("name", "")
        if re.search(gb_pattern, name, re.IGNORECASE) and model_ok(name):
            return p
    return None


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

    console_level = os.getenv("LOG_LEVEL", "INFO").upper()
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # root captures everything

    # Console — INFO by default (or LOG_LEVEL override)
    ch = logging.StreamHandler()
    ch.setLevel(getattr(logging, console_level, logging.INFO))
    ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root.addHandler(ch)

    # File — always DEBUG, full detail for post-scan analysis
    from logging.handlers import RotatingFileHandler
    log_path = Path(os.getenv("DATABASE_PATH", "ebay_monitor.db")).with_suffix(".debug.log")
    fh = RotatingFileHandler(log_path, maxBytes=20 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(fh)

    LOGGER.info("Debug log → %s", log_path)
    run(Path(os.getenv("DATABASE_PATH", "ebay_monitor.db")), args.once)


if __name__ == "__main__":
    main()
