from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from filters import parse_price
from models import Listing


@dataclass(frozen=True)
class Marketplace:
    name: str
    hosts: tuple[str, ...]
    supports_sold_search: bool = False


EBAY = Marketplace("eBay", ("ebay.de", "ebay.com"), supports_sold_search=True)
KLEINANZEIGEN = Marketplace("Kleinanzeigen", ("kleinanzeigen.de",))
VINTED = Marketplace("Vinted", ("vinted.de",))
MARKETPLACES = (EBAY, KLEINANZEIGEN, VINTED)


def marketplace_for_url(url: str) -> Marketplace:
    host = (urlparse(url).hostname or "").casefold()
    for marketplace in MARKETPLACES:
        if any(host == allowed or host.endswith(f".{allowed}") for allowed in marketplace.hosts):
            return marketplace
    raise ValueError(f"Unsupported marketplace URL: {url}")


def _text(element, selector: str) -> str | None:
    selected = element.select_one(selector)
    return selected.get_text(" ", strip=True) if selected else None


def parse_kleinanzeigen_listings(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    listings = []
    for element in soup.select("article.aditem"):
        link_element = element.select_one("a.ellipsis")
        title = _text(element, "a.ellipsis")
        price_text = _text(element, ".aditem-main--middle--price-shipping--price")
        href = link_element.get("href") if link_element else element.get("data-href")
        if not href or not title or not price_text:
            continue
        image = element.select_one(".aditem-image img")
        shipping = _text(element, ".aditem-main--bottom")
        price, currency = parse_price(price_text)
        listings.append(
            Listing(
                title=title,
                link=urljoin("https://www.kleinanzeigen.de", href),
                price_text=price_text,
                price=price,
                currency=currency,
                image_url=(image.get("src") or image.get("data-src")) if image else None,
                shipping=shipping,
                location=_text(element, ".aditem-main--top--left"),
            )
        )
    return listings


def parse_vinted_listings(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    listings = []
    for element in soup.select(".new-item-box__container"):
        link_element = element.select_one('a[href*="/items/"]')
        title = _text(element, '[data-testid$="--description-title"]')
        condition = _text(element, '[data-testid$="--description-subtitle"]')
        price_text = _text(element, '[data-testid$="--price-text"]')
        href = link_element.get("href") if link_element else None
        if not href or not title or not price_text:
            continue
        image = element.select_one('img[data-testid$="--image--img"]')
        price, currency = parse_price(price_text)
        listings.append(
            Listing(
                title=title,
                link=urljoin("https://www.vinted.de", href),
                price_text=price_text,
                price=price,
                currency=currency,
                image_url=image.get("src") if image else None,
                condition=condition,
            )
        )
    return listings


def fetch_marketplace_listings(
    session: requests.Session,
    url: str,
    headers: dict[str, str],
    timeout: int,
    ebay_parser,
    browser_fetcher=None,
) -> list[Listing]:
    marketplace = marketplace_for_url(url)
    if browser_fetcher:
        response = browser_fetcher.get(url, timeout)
        response_text = response.text
    else:
        response = session.get(url, headers=headers, timeout=timeout)
        response_text = response.text
    if response.status_code in {403, 429, 500, 503}:
        guidance = (
            " Use eBay's official Browse API or configure a proxy you are authorized to use."
            if marketplace is EBAY
            else ""
        )
        raise requests.HTTPError(
            f"{marketplace.name} rejected the request with HTTP {response.status_code}; "
            f"the IP may be rate-limited or blocked.{guidance}",
            response=response,
        )
    if browser_fetcher:
        if response.status_code >= 400:
            raise requests.HTTPError(
                f"{marketplace.name} returned HTTP {response.status_code}"
            )
    else:
        response.raise_for_status()
    if marketplace is KLEINANZEIGEN:
        return parse_kleinanzeigen_listings(response_text)
    if marketplace is VINTED:
        return parse_vinted_listings(response_text)
    return ebay_parser(response_text)
