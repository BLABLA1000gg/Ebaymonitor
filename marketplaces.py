from __future__ import annotations
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    from curl_cffi.requests import Session as CurlSession
    _CURL_CFFI_AVAILABLE = True
except ImportError:
    _CURL_CFFI_AVAILABLE = False

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
        # Strip query params (e.g. ?referrer=catalog) — keeps URLs canonical
        # so DB deduplication works correctly across scans.
        href = href.split("?")[0]
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


_EBAY_CURL_SESSION: "CurlSession | None" = None  # type: ignore[type-arg]

_EBAY_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def _fetch_ebay(url: str, headers: dict, timeout: int):
    """
    Fetch an eBay URL using curl_cffi which mimics Chrome's TLS fingerprint,
    bypassing eBay's bot detection (JA3/TLS fingerprinting + header analysis).

    eBay requires session cookies obtained from the homepage before accepting
    search requests. The curl_cffi session is reused across calls.

    Falls back to plain requests if curl_cffi is not installed.
    """
    global _EBAY_CURL_SESSION

    if not _CURL_CFFI_AVAILABLE:
        # Fallback — likely to get 403 on server/datacenter IPs
        return requests.get(url, headers=headers, timeout=timeout)

    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}/"

    if _EBAY_CURL_SESSION is None:
        _EBAY_CURL_SESSION = CurlSession(impersonate="chrome120")
        # Seed the session with homepage cookies so eBay accepts search requests
        _EBAY_CURL_SESSION.get(base_url, headers=_EBAY_HEADERS, timeout=timeout)

    search_headers = _EBAY_HEADERS.copy()
    search_headers["Sec-Fetch-Site"] = "same-origin"
    search_headers["Referer"] = base_url
    return _EBAY_CURL_SESSION.get(url, headers=search_headers, timeout=timeout)


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
    elif marketplace is EBAY:
        # eBay uses an Akamai JS challenge that blocks headless scrapers.
        # curl_cffi alone is no longer sufficient; fall back to curl_cffi and
        # detect the challenge page, or use the caller-supplied BrowserFetcher.
        response = _fetch_ebay(url, headers, timeout)
        response_text = response.text
        # If we received the JS challenge page, propagate a clear error so the
        # caller can retry with a BrowserFetcher.
        if response.status_code == 200 and "challenge" in response_text[:4000]:
            raise requests.HTTPError(
                "eBay returned a bot-challenge page. Enable browser fetch in Settings "
                "or pass a BrowserFetcher to bypass Akamai detection.",
                response=response,
            )
    else:
        response = session.get(url, headers=headers, timeout=timeout)
        response_text = response.text

    if response.status_code in {403, 429, 500, 503}:
        guidance = (
            " curl_cffi is installed but the IP may still be blocked."
            " Try setting BROWSER_FETCH=true or use a residential proxy."
            if marketplace is EBAY and _CURL_CFFI_AVAILABLE
            else " Install curl_cffi (pip install curl_cffi) for Chrome TLS impersonation."
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
