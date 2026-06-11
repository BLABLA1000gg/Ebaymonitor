from __future__ import annotations
import re
import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin, urlparse, parse_qs

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


# Matches "3 Gebote" / "1 Gebot" (DE) and "3 bids" / "1 bid" (EN) in an
# eBay search-result card. The bid count is the strongest auction signal.
_EBAY_BID_RE = re.compile(r"(\d+)\s+(?:Gebot(?:e)?|bids?)\b", re.IGNORECASE)


def parse_ebay_auction_fields(li) -> dict:
    """
    Inspect a single eBay search-result card (``<li class="s-card">`` element,
    a BeautifulSoup tag) and extract auction metadata.

    Returns a dict with keys ``is_auction``, ``bid_count``, ``time_left`` and
    ``end_time``. For Buy-It-Now (non-auction) cards it returns
    ``{"is_auction": False, ...}`` with the other values ``None`` so callers can
    splat it straight into the ``Listing`` constructor.

    Detection (robust, two independent signals — either one marks an auction):
      * a bid-count badge ("X Gebote" / "X bids"), OR
      * a time-left element (``.s-card__time-left`` / ``.s-card__time``) which
        eBay only renders for timed auctions, not BIN listings.

    The current bid for an auction is the card's price span (eBay shows the
    leading bid as the price on auction cards), so price parsing in the caller
    is unchanged — the price already reflects the current bid.
    """
    bid_count: int | None = None
    # Bid count: scan the small/secondary text spans for an "X Gebote" badge.
    for sp in li.select("span.su-styled-text, span"):
        txt = sp.get_text(" ", strip=True)
        if not txt or len(txt) > 24:
            continue
        match = _EBAY_BID_RE.search(txt)
        if match:
            bid_count = int(match.group(1))
            break

    # Time-left ("Noch 22 Min", "Noch 1 T 10 Std") and end-time.
    time_left = None
    end_time = None
    time_left_el = li.select_one(".s-card__time-left")
    if time_left_el:
        time_left = time_left_el.get_text(" ", strip=True) or None
    time_el = li.select_one(".s-card__time")
    if time_el:
        time_full = time_el.get_text(" ", strip=True)
        # Format: "Restzeit Noch 22 Min (Heute 15:33)" — the (...) is the end time.
        paren = re.search(r"\(([^)]+)\)", time_full)
        if paren:
            end_time = paren.group(1).strip()
        if time_left is None:
            # Fall back to the "Noch ..." fragment if the dedicated element is absent.
            noch = re.search(r"(Noch\b.*?)(?:\s*\(|$)", time_full)
            if noch:
                time_left = noch.group(1).strip()

    is_auction = bid_count is not None or time_left is not None or end_time is not None
    return {
        "is_auction": is_auction,
        "bid_count": bid_count,
        "time_left": time_left,
        "end_time": end_time,
    }


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


# ── Vinted JSON API ──────────────────────────────────────────────────────────
# Vinted's web app talks to an internal JSON API which is far more reliable
# than scraping the HTML (no markup churn, much higher rate limits). The API
# only requires the anonymous session cookies handed out on the homepage
# (access_token_web etc.), so we do one homepage GET per process and reuse
# the curl_cffi session (Chrome TLS fingerprint) for every API call.

VINTED_API_BASE = "https://www.vinted.de"

_VINTED_CURL_SESSION: "CurlSession | None" = None  # type: ignore[type-arg]
_VINTED_CURL_SESSION_LOCK = threading.Lock()

_VINTED_API_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
}


def _vinted_session(timeout: int = 15) -> "CurlSession":
    """Return the shared Vinted curl_cffi session, performing the anonymous
    cookie handshake (homepage GET) on first use. Thread-safe."""
    global _VINTED_CURL_SESSION
    if not _CURL_CFFI_AVAILABLE:
        raise RuntimeError("curl_cffi is required for the Vinted API")
    with _VINTED_CURL_SESSION_LOCK:
        if _VINTED_CURL_SESSION is None:
            session = CurlSession(impersonate="chrome120")
            # Homepage GET seeds the anonymous session cookies
            # (access_token_web / refresh_token_web / anon_id).
            session.get(f"{VINTED_API_BASE}/", timeout=timeout)
            _VINTED_CURL_SESSION = session
    return _VINTED_CURL_SESSION


def _vinted_reset_session() -> None:
    global _VINTED_CURL_SESSION
    with _VINTED_CURL_SESSION_LOCK:
        _VINTED_CURL_SESSION = None


def vinted_api_get(path: str, params: dict | None = None, timeout: int = 15):
    """GET a Vinted /api/v2/... path with the shared anonymous session.
    Refreshes the cookie handshake once on 401."""
    session = _vinted_session(timeout)
    response = session.get(
        f"{VINTED_API_BASE}{path}", params=params,
        headers=_VINTED_API_HEADERS, timeout=timeout,
    )
    if response.status_code == 401:
        _vinted_reset_session()
        session = _vinted_session(timeout)
        response = session.get(
            f"{VINTED_API_BASE}{path}", params=params,
            headers=_VINTED_API_HEADERS, timeout=timeout,
        )
    return response


def fetch_vinted_listings_api(url: str, timeout: int = 15) -> list[Listing]:
    """Fetch Vinted catalog results via the JSON API.

    Translates a user-configured catalog URL (e.g.
    https://www.vinted.de/catalog?search_text=iphone+13+pro) into a
    /api/v2/catalog/items query and maps the JSON items onto Listing objects
    with the same shape parse_vinted_listings() produces.
    """
    query = parse_qs(urlparse(url).query)
    params: dict = {"per_page": 96, "page": 1}
    for key in ("search_text", "price_from", "price_to", "order", "currency"):
        if key in query and query[key]:
            params[key] = query[key][0]
    # Multi-value filters (catalog[], brand_ids[], status_ids[], ...) pass
    # straight through — the API uses the same parameter names as the web URL.
    for key, values in query.items():
        if key.endswith("[]") and values:
            params[key] = values

    response = vinted_api_get("/api/v2/catalog/items", params=params, timeout=timeout)
    if response.status_code != 200:
        raise requests.HTTPError(
            f"Vinted API returned HTTP {response.status_code} for catalog query"
        )
    payload = response.json()
    listings: list[Listing] = []
    for item in payload.get("items", []):
        title = item.get("title")
        item_url = item.get("url") or (
            urljoin(VINTED_API_BASE, item["path"]) if item.get("path") else None
        )
        price_obj = item.get("price") or {}
        amount = price_obj.get("amount")
        if not title or not item_url or amount is None:
            continue
        # Canonical link without query params so DB dedup stays stable.
        link = item_url.split("?")[0]
        currency = price_obj.get("currency_code")
        try:
            price = Decimal(str(amount))
        except (InvalidOperation, ValueError):
            price = None
        photo = item.get("photo") or {}
        symbol = {"EUR": "€", "USD": "$", "GBP": "£"}.get(currency, currency or "")
        price_text = f"{amount} {symbol}".strip()
        listings.append(
            Listing(
                title=title,
                link=link,
                price_text=price_text,
                price=price,
                currency=currency,
                image_url=photo.get("url"),
                condition=item.get("status"),
            )
        )
    return listings


_EBAY_CURL_SESSION: "CurlSession | None" = None  # type: ignore[type-arg]
_EBAY_CURL_SESSION_LOCK = threading.Lock()

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

    with _EBAY_CURL_SESSION_LOCK:
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

    if marketplace is VINTED and not browser_fetcher and _CURL_CFFI_AVAILABLE:
        # Prefer the JSON API — faster, no markup churn, far higher rate
        # limits than the HTML pages. Fall back to HTML scraping if it fails.
        try:
            return fetch_vinted_listings_api(url, timeout=timeout)
        except Exception:
            pass

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
