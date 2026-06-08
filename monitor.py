import logging
import os
import time
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

LOGGER = logging.getLogger(__name__)
DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_TIMEOUT_SECONDS = 20
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}


@dataclass(frozen=True)
class Listing:
    title: str
    link: str
    price: str
    image_url: str | None = None


def parse_listings(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    listings: list[Listing] = []

    for element in soup.select(".s-item"):
        link_element = element.select_one(".s-item__link")
        title_element = element.select_one(".s-item__title")
        price_element = element.select_one(".s-item__price")
        if not link_element or not title_element or not price_element:
            continue

        link = link_element.get("href")
        title = title_element.get_text(" ", strip=True)
        price = price_element.get_text(" ", strip=True)
        if not link or not title or title.lower() == "shop on ebay" or not price:
            continue

        image_element = element.select_one(".s-item__image-img")
        image_url = None
        if image_element:
            image_url = image_element.get("src") or image_element.get("data-src")

        listings.append(Listing(title=title, link=link, price=price, image_url=image_url))

    return listings


def send_to_discord(
    session: requests.Session,
    webhook_url: str,
    listing: Listing,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    embed = {
        "title": listing.title,
        "url": listing.link,
        "color": 16711680,
        "fields": [{"name": "Price", "value": listing.price, "inline": True}],
    }
    if listing.image_url:
        embed["thumbnail"] = {"url": listing.image_url}

    response = session.post(webhook_url, json={"embeds": [embed]}, timeout=timeout)
    response.raise_for_status()
    LOGGER.info("Sent new listing to Discord: %s", listing.title)


def fetch_listings(
    session: requests.Session,
    ebay_url: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> list[Listing]:
    response = session.get(ebay_url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return parse_listings(response.text)


def find_new_listings(listings: Iterable[Listing], seen_links: set[str]) -> list[Listing]:
    return [listing for listing in listings if listing.link not in seen_links]


def validate_url(name: str, value: str, allowed_hosts: tuple[str, ...]) -> str:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not any(host == item or host.endswith(f".{item}") for item in allowed_hosts):
        raise ValueError(f"{name} must be an HTTPS URL for {', '.join(allowed_hosts)}")
    return value


def run_monitor(
    ebay_url: str,
    webhook_url: str,
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    notify_existing: bool = False,
) -> None:
    session = requests.Session()
    seen_links: set[str] = set()
    first_scan = True

    LOGGER.info("Monitoring eBay every %s seconds", interval_seconds)
    while True:
        try:
            listings = fetch_listings(session, ebay_url)
            new_listings = find_new_listings(listings, seen_links)

            if first_scan and not notify_existing:
                LOGGER.info("Initial scan found %s listings; notifications start with new results", len(listings))
            else:
                for listing in new_listings:
                    try:
                        send_to_discord(session, webhook_url, listing)
                    except requests.RequestException as error:
                        LOGGER.error("Could not send %s to Discord: %s", listing.link, error)

            seen_links = {listing.link for listing in listings}
            first_scan = False
        except requests.RequestException as error:
            LOGGER.error("Could not fetch eBay listings: %s", error)
        except Exception:
            LOGGER.exception("Unexpected monitor error")

        time.sleep(interval_seconds)


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")

    ebay_url = validate_url("EBAY_URL", os.environ["EBAY_URL"], ("ebay.com", "ebay.de"))
    webhook_url = validate_url("DISCORD_WEBHOOK_URL", os.environ["DISCORD_WEBHOOK_URL"], ("discord.com", "discordapp.com"))
    interval_seconds = int(os.getenv("CHECK_INTERVAL_SECONDS", str(DEFAULT_INTERVAL_SECONDS)))
    if interval_seconds < 30:
        raise ValueError("CHECK_INTERVAL_SECONDS must be at least 30")

    notify_existing = os.getenv("NOTIFY_EXISTING", "false").lower() in {"1", "true", "yes"}
    run_monitor(ebay_url, webhook_url, interval_seconds, notify_existing)


if __name__ == "__main__":
    main()
