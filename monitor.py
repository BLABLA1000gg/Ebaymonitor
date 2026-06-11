from __future__ import annotations
import argparse
import logging
import os
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from browser_fetch import BrowserFetcher
from filters import ListingFilter, parse_csv_words, parse_price
from marketplaces import fetch_marketplace_listings, marketplace_for_url, parse_ebay_auction_fields
from models import EventType, Listing, ListingEvent
from storage import MonitorStore

LOGGER = logging.getLogger(__name__)
DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_TIMEOUT_SECONDS = 20
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}


@dataclass(frozen=True)
class Config:
    ebay_urls: tuple[str, ...]
    webhook_url: str | None
    database_path: Path
    csv_directory: Path | None
    interval_seconds: int
    notify_existing: bool
    notify_price_increases: bool
    notify_statistics: bool
    listing_filter: ListingFilter


def text_or_none(element, selector: str) -> str | None:
    selected = element.select_one(selector)
    return selected.get_text(" ", strip=True) if selected else None


def parse_listings(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    listings: list[Listing] = []
    seen_ids: set[str] = set()

    for li in soup.find_all("li", class_="s-card"):
        # Each physical listing appears in 3 <li class="s-card"> elements;
        # deduplicate by the data-listingid attribute.
        listing_id = li.get("data-listingid", "")
        if listing_id and listing_id in seen_ids:
            continue

        # The title link (not the image link)
        link_el = li.find("a", class_="s-card__link", href=lambda h: h and "/itm/" in h)
        if not link_el:
            continue
        link = link_el.get("href", "")

        # Skip US sponsored listings served on ebay.de pages
        if "ebay.com/itm/" in link and "ebay.de/itm/" not in link:
            continue

        # Fallback dedup by the /itm/<id> in the link — eBay renders each
        # physical listing in ~3 s-card elements and data-listingid is not
        # always present, so the attribute check alone leaks duplicates.
        m_itm = re.search(r"/itm/(\d+)", link)
        item_key = listing_id or (m_itm.group(1) if m_itm else "")
        if item_key and item_key in seen_ids:
            continue

        # Title: first primary-default styled span, or fall back to image alt
        title_span = li.select_one("span.su-styled-text.primary.default")
        img_el = li.find("img", class_="s-card__image")
        if title_span:
            title = title_span.get_text(" ", strip=True)
        elif img_el:
            # Remove trailing " Bild X von Y" from alt text
            alt = img_el.get("alt", "")
            title = re.sub(r"\s+Bild \d+ von \d+$", "", alt).strip()
        else:
            title = None

        # Price: active listings use "primary bold", sold/completed listings use
        # "positive strikethrough s-card__price". Try both selectors.
        price_span = (
            li.select_one("span.su-styled-text.primary.bold")
            or li.select_one("span.s-card__price")
        )
        price_text = price_span.get_text(" ", strip=True) if price_span else None

        if not link or not title or not price_text:
            continue
        if title.casefold() in ("shop on ebay", ""):
            continue

        if item_key:
            seen_ids.add(item_key)

        # Image
        image_url = None
        if img_el:
            image_url = img_el.get("src") or img_el.get("data-src")

        # Condition: secondary-default span containing a pipe separator
        condition = None
        for sp in li.select("span.su-styled-text.secondary.default"):
            txt = sp.get_text(" ", strip=True)
            if "|" in txt:
                condition = txt.rstrip(" |").strip()
                break

        # Shipping: positive-bold or secondary-large span mentioning delivery/shipping
        shipping = None
        for sp in li.select("span.su-styled-text.positive.bold, span.su-styled-text.secondary.large"):
            txt = sp.get_text(" ", strip=True)
            if any(kw in txt for kw in ("Lieferung", "Versand", "Gratis", "kostenlos", "Abholung")):
                shipping = txt
                break

        price, currency = parse_price(price_text)
        auction = parse_ebay_auction_fields(li)
        listings.append(
            Listing(
                title=title,
                link=link,
                price_text=price_text,
                price=price,
                currency=currency,
                image_url=image_url,
                condition=condition,
                shipping=shipping,
                **auction,
            )
        )
    return listings


def sold_search_url(search_url: str) -> str:
    parts = urlsplit(search_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["LH_Sold"] = "1"
    query["LH_Complete"] = "1"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def fetch_listings(
    session: requests.Session,
    url: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    browser_fetcher=None,
) -> list[Listing]:
    return fetch_marketplace_listings(
        session, url, HEADERS, timeout, parse_listings, browser_fetcher
    )


def event_should_notify(event: ListingEvent, config: Config, initial_scan: bool) -> bool:
    if event.type is EventType.NEW:
        return config.notify_existing or not initial_scan
    if event.type is EventType.PRICE_DROP:
        return True
    if event.type is EventType.PRICE_INCREASE:
        return config.notify_price_increases
    return False


def send_to_discord(session: requests.Session, webhook_url: str, event: ListingEvent, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> None:
    colors = {EventType.NEW: 0x3498DB, EventType.PRICE_DROP: 0x2ECC71, EventType.PRICE_INCREASE: 0xE67E22}
    labels = {EventType.NEW: "New listing", EventType.PRICE_DROP: "Price dropped", EventType.PRICE_INCREASE: "Price increased"}
    fields = [{"name": "Price", "value": event.listing.price_text, "inline": True}]
    if event.previous_price is not None:
        fields.append({"name": "Previous price", "value": str(event.previous_price), "inline": True})
    if event.price_change_percent is not None:
        fields.append({"name": "Change", "value": f"{event.price_change_percent:+.1f}%", "inline": True})
    for label, value in (("Condition", event.listing.condition), ("Shipping", event.listing.shipping), ("Location", event.listing.location)):
        if value:
            fields.append({"name": label, "value": value, "inline": True})
    embed = {
        "title": event.listing.title[:256],
        "description": labels[event.type],
        "url": event.listing.link,
        "color": colors[event.type],
        "fields": fields[:25],
    }
    if event.listing.image_url:
        embed["thumbnail"] = {"url": event.listing.image_url}
    response = session.post(webhook_url, json={"embeds": [embed]}, timeout=timeout)
    response.raise_for_status()


def send_statistics_to_discord(session: requests.Session, webhook_url: str, stats: dict, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> None:
    currency = stats["currency"] or ""

    def money(value):
        return f"{value:.2f} {currency}" if value is not None else "n/a"

    embed = {
        "title": "eBay sold-price summary",
        "url": stats["search_url"],
        "color": 0x9B59B6,
        "description": f"Sold/completed results for keywords: {stats['keyword_filter'] or 'none'}",
        "fields": [
            {"name": "Average sold price", "value": money(stats["average_price"]), "inline": True},
            {"name": "Median sold price", "value": money(stats["median_price"]), "inline": True},
            {"name": "Sold results", "value": str(stats["listing_count"]), "inline": True},
            {"name": "Minimum", "value": money(stats["minimum_price"]), "inline": True},
            {"name": "Maximum", "value": money(stats["maximum_price"]), "inline": True},
        ],
    }
    response = session.post(webhook_url, json={"embeds": [embed]}, timeout=timeout)
    response.raise_for_status()


def scan_once(session: requests.Session, store: MonitorStore, config: Config, initial_scan: bool) -> tuple[int, int]:
    browser_context = BrowserFetcher() if bool_env("BROWSER_FETCH") else None
    if browser_context:
        browser_context.__enter__()
    by_link: dict[str, Listing] = {}
    try:
        keyword_signature = ",".join(config.listing_filter.include_keywords)
        for ebay_url in config.ebay_urls:
            marketplace = marketplace_for_url(ebay_url)
            active_listings = [
                listing for listing in fetch_listings(
                    session, ebay_url, browser_fetcher=browser_context
                )
                if config.listing_filter.matches(listing)
            ]
            for listing in active_listings:
                by_link[listing.link] = listing

            if not marketplace.supports_sold_search:
                continue
            sold_url = sold_search_url(ebay_url)
            sold_listings = [
                listing for listing in fetch_listings(
                    session, sold_url, browser_fetcher=browser_context
                )
                if config.listing_filter.matches(listing)
            ]
            # Legacy CLI path — the dashboard uses controller.py/profile_monitor.py.
            # MonitorStore no longer implements record_search_statistics; skip
            # statistics instead of crashing if this path is ever invoked.
            if not hasattr(store, "record_search_statistics"):
                LOGGER.warning("Sold statistics skipped: store has no record_search_statistics (legacy path)")
                continue
            stats = store.record_search_statistics(sold_url, keyword_signature, sold_listings)
            LOGGER.info(
                "Sold statistics: %s results, average=%s, median=%s, min=%s, max=%s",
                stats["listing_count"], stats["average_price"], stats["median_price"],
                stats["minimum_price"], stats["maximum_price"],
            )
            if config.notify_statistics and config.webhook_url:
                send_statistics_to_discord(session, config.webhook_url, stats)
    finally:
        if browser_context:
            browser_context.__exit__(None, None, None)

    events = store.record_scan(list(by_link.values()))
    notified = 0
    for event in events:
        if event_should_notify(event, config, initial_scan) and config.webhook_url:
            try:
                send_to_discord(session, config.webhook_url, event)
                notified += 1
            except requests.RequestException as error:
                LOGGER.error("Could not notify for %s: %s", event.listing.link, error)
    if config.csv_directory:
        store.export_csv(config.csv_directory)
    LOGGER.info("Scan complete: %s matching listings, %s notifications", len(events), notified)
    return len(events), notified


def validate_url(name: str, value: str, allowed_hosts: tuple[str, ...]) -> str:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not any(host == item or host.endswith(f".{item}") for item in allowed_hosts):
        raise ValueError(f"{name} must be an HTTPS URL for {', '.join(allowed_hosts)}")
    return value


def decimal_env(name: str) -> Decimal | None:
    value = os.getenv(name)
    return Decimal(value) if value else None


def bool_env(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).casefold() in {"1", "true", "yes"}


def load_config() -> Config:
    raw_urls = os.environ.get("EBAY_URLS") or os.environ.get("EBAY_URL")
    if not raw_urls:
        raise ValueError("Set EBAY_URL or EBAY_URLS")
    supported_hosts = ("ebay.com", "ebay.de", "kleinanzeigen.de", "vinted.de")
    ebay_urls = tuple(validate_url("EBAY_URL", value.strip(), supported_hosts) for value in raw_urls.split("|") if value.strip())
    webhook = os.getenv("DISCORD_WEBHOOK_URL")
    if webhook:
        webhook = validate_url("DISCORD_WEBHOOK_URL", webhook, ("discord.com", "discordapp.com"))
    interval = int(os.getenv("CHECK_INTERVAL_SECONDS", str(DEFAULT_INTERVAL_SECONDS)))
    if interval < 30:
        raise ValueError("CHECK_INTERVAL_SECONDS must be at least 30")
    currency = os.getenv("CURRENCY")
    return Config(
        ebay_urls=ebay_urls,
        webhook_url=webhook,
        database_path=Path(os.getenv("DATABASE_PATH", "ebay_monitor.db")),
        csv_directory=Path(os.environ["CSV_DIRECTORY"]) if os.getenv("CSV_DIRECTORY") else None,
        interval_seconds=interval,
        notify_existing=bool_env("NOTIFY_EXISTING"),
        notify_price_increases=bool_env("NOTIFY_PRICE_INCREASES"),
        notify_statistics=bool_env("NOTIFY_STATISTICS"),
        listing_filter=ListingFilter(
            include_keywords=parse_csv_words(os.getenv("INCLUDE_KEYWORDS")),
            exclude_keywords=parse_csv_words(os.getenv("EXCLUDE_KEYWORDS")),
            min_price=decimal_env("MIN_PRICE"),
            max_price=decimal_env("MAX_PRICE"),
            currency=currency.upper() if currency else None,
        ),
    )


def run_monitor(config: Config, once: bool = False) -> None:
    initial_scan = True
    with requests.Session() as session, MonitorStore(config.database_path) as store:
        while True:
            try:
                scan_once(session, store, config, initial_scan)
                initial_scan = False
            except requests.RequestException as error:
                LOGGER.error("Could not fetch eBay listings: %s", error)
            except Exception:
                LOGGER.exception("Unexpected monitor error")
            if once:
                return
            time.sleep(config.interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Advanced eBay listing and sold-price monitor")
    parser.add_argument("--once", action="store_true", help="Run one scan and exit")
    parser.add_argument("--export", action="store_true", help="Export the database to CSV and exit")
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    config = load_config()
    if args.export:
        with MonitorStore(config.database_path) as store:
            paths = store.export_csv(config.csv_directory or Path("exports"))
            LOGGER.info("Exported %s", ", ".join(str(path) for path in paths))
        return
    run_monitor(config, once=args.once)


if __name__ == "__main__":
    main()
