import argparse
import logging
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from filters import ListingFilter, parse_csv_words, parse_price
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
    listing_filter: ListingFilter


def text_or_none(element, selector: str) -> str | None:
    selected = element.select_one(selector)
    return selected.get_text(" ", strip=True) if selected else None


def parse_listings(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    listings: list[Listing] = []

    for element in soup.select(".s-item"):
        link_element = element.select_one(".s-item__link")
        title = text_or_none(element, ".s-item__title")
        price_text = text_or_none(element, ".s-item__price")
        link = link_element.get("href") if link_element else None
        if not link or not title or not price_text or title.casefold() == "shop on ebay":
            continue

        image_element = element.select_one(".s-item__image-img")
        image_url = None
        if image_element:
            image_url = image_element.get("src") or image_element.get("data-src")
        price, currency = parse_price(price_text)
        listings.append(
            Listing(
                title=title,
                link=link,
                price_text=price_text,
                price=price,
                currency=currency,
                image_url=image_url,
                condition=text_or_none(element, ".SECONDARY_INFO"),
                shipping=text_or_none(element, ".s-item__shipping, .s-item__logisticsCost"),
                location=text_or_none(element, ".s-item__location"),
            )
        )
    return listings


def fetch_listings(
    session: requests.Session,
    ebay_url: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> list[Listing]:
    response = session.get(ebay_url, headers=HEADERS, timeout=timeout)
    if response.status_code in {403, 429, 500, 503}:
        raise requests.HTTPError(
            f"eBay rejected the request with HTTP {response.status_code}; "
            "the IP may be rate-limited or blocked",
            response=response,
        )
    response.raise_for_status()
    return parse_listings(response.text)


def event_should_notify(event: ListingEvent, config: Config, initial_scan: bool) -> bool:
    if event.type is EventType.NEW:
        return config.notify_existing or not initial_scan
    if event.type is EventType.PRICE_DROP:
        return True
    if event.type is EventType.PRICE_INCREASE:
        return config.notify_price_increases
    return False


def send_to_discord(
    session: requests.Session,
    webhook_url: str,
    event: ListingEvent,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    colors = {
        EventType.NEW: 0x3498DB,
        EventType.PRICE_DROP: 0x2ECC71,
        EventType.PRICE_INCREASE: 0xE67E22,
    }
    labels = {
        EventType.NEW: "New listing",
        EventType.PRICE_DROP: "Price dropped",
        EventType.PRICE_INCREASE: "Price increased",
    }
    fields = [{"name": "Price", "value": event.listing.price_text, "inline": True}]
    if event.previous_price is not None:
        fields.append(
            {"name": "Previous price", "value": str(event.previous_price), "inline": True}
        )
    if event.price_change_percent is not None:
        fields.append(
            {
                "name": "Change",
                "value": f"{event.price_change_percent:+.1f}%",
                "inline": True,
            }
        )
    for label, value in (
        ("Condition", event.listing.condition),
        ("Shipping", event.listing.shipping),
        ("Location", event.listing.location),
    ):
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


def scan_once(
    session: requests.Session,
    store: MonitorStore,
    config: Config,
    initial_scan: bool,
) -> tuple[int, int]:
    by_link: dict[str, Listing] = {}
    for ebay_url in config.ebay_urls:
        for listing in fetch_listings(session, ebay_url):
            if config.listing_filter.matches(listing):
                by_link[listing.link] = listing

    events = store.record_scan(list(by_link.values()))
    notified = 0
    for event in events:
        if not event_should_notify(event, config, initial_scan):
            continue
        if config.webhook_url:
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
    if parsed.scheme != "https" or not any(
        host == item or host.endswith(f".{item}") for item in allowed_hosts
    ):
        raise ValueError(f"{name} must be an HTTPS URL for {', '.join(allowed_hosts)}")
    return value


def decimal_env(name: str) -> Decimal | None:
    value = os.getenv(name)
    return Decimal(value) if value else None


def load_config() -> Config:
    raw_urls = os.environ.get("EBAY_URLS") or os.environ.get("EBAY_URL")
    if not raw_urls:
        raise ValueError("Set EBAY_URL or EBAY_URLS")
    ebay_urls = tuple(
        validate_url("EBAY_URL", value.strip(), ("ebay.com", "ebay.de"))
        for value in raw_urls.split("|")
        if value.strip()
    )
    webhook = os.getenv("DISCORD_WEBHOOK_URL")
    if webhook:
        webhook = validate_url(
            "DISCORD_WEBHOOK_URL", webhook, ("discord.com", "discordapp.com")
        )
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
        notify_existing=os.getenv("NOTIFY_EXISTING", "false").casefold() in {"1", "true", "yes"},
        notify_price_increases=os.getenv("NOTIFY_PRICE_INCREASES", "false").casefold()
        in {"1", "true", "yes"},
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
    parser = argparse.ArgumentParser(description="Advanced eBay listing and price monitor")
    parser.add_argument("--once", action="store_true", help="Run one scan and exit")
    parser.add_argument("--export", action="store_true", help="Export the database to CSV and exit")
    args = parser.parse_args()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = load_config()
    if args.export:
        with MonitorStore(config.database_path) as store:
            paths = store.export_csv(config.csv_directory or Path("exports"))
            LOGGER.info("Exported %s and %s", *paths)
        return
    run_monitor(config, once=args.once)


if __name__ == "__main__":
    main()
