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


# ── Kleinanzeigen gateway JSON API ───────────────────────────────────────────
# The Kleinanzeigen mobile app talks to a REST gateway that returns clean JSON
# ads (the public website has no anonymous API). Access is read-only and gated
# by HTTP Basic auth with a STATIC credential embedded in the app (a public app
# token, not user data) plus the app's okhttp User-Agent. We reuse one session.
#
# JSON shape note: the gateway serialises its XML schema into JSON, so most
# values are wrapped, e.g. {"value": ...}, and dict keys are namespaced like
# "{http://...}ads". Helpers below strip namespaces and unwrap {"value": ...}.

KA_API_BASE = "https://api.kleinanzeigen.de/api"
KA_API_BASIC_TOKEN = "YW5kcm9pZDpUYVI2MHBFdHRZ"  # base64("android:TaR60pEttY") — public app credential
KA_API_USER_AGENT = "okhttp/4.10.0"

_KA_API_HEADERS = {
    "Authorization": f"Basic {KA_API_BASIC_TOKEN}",
    "User-Agent": KA_API_USER_AGENT,
    "Accept": "application/json",
    "Accept-Language": "de-DE,de;q=0.9",
}

_KA_API_SESSION: requests.Session | None = None
_KA_API_SESSION_LOCK = threading.Lock()

_KA_AD_ID_RE = re.compile(r"/(\d{6,})(?:[-/?#]|$)")


def _ka_api_session() -> requests.Session:
    """Shared keep-alive requests session for the Kleinanzeigen gateway."""
    global _KA_API_SESSION
    with _KA_API_SESSION_LOCK:
        if _KA_API_SESSION is None:
            session = requests.Session()
            session.headers.update(_KA_API_HEADERS)
            _KA_API_SESSION = session
    return _KA_API_SESSION


def _ka_strip_ns(key: str) -> str:
    """'{http://...}ads' -> 'ads'."""
    return key.rsplit("}", 1)[-1]


def _ka_get(node, *keys):
    """Walk a namespaced gateway dict by local key names, tolerating missing
    nodes. Returns the final value or None."""
    cur = node
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = {_ka_strip_ns(k): v for k, v in cur.items()}.get(key)
        if cur is None:
            return None
    return cur


def _ka_value(node):
    """Unwrap a {'value': X} wrapper (one level), else return node as-is."""
    if isinstance(node, dict) and "value" in node:
        return node["value"]
    return node


def ka_ad_id_from_url(url: str) -> str | None:
    """Extract the numeric ad id from a Kleinanzeigen web URL, e.g.
    https://www.kleinanzeigen.de/s-anzeige/iphone-13-pro/3432483015-173-8855."""
    match = _KA_AD_ID_RE.search(url)
    return match.group(1) if match else None


def _ka_ad_to_listing(ad: dict) -> Listing | None:
    """Map one gateway ad dict onto a Listing (same shape as the HTML parser)."""
    ad = {_ka_strip_ns(k): v for k, v in ad.items()}
    title = _ka_value(ad.get("title"))
    if not title:
        return None

    # Canonical public web URL (rel='self-public-website'); fall back to id.
    link = None
    for entry in ad.get("link", []) or []:
        if entry.get("rel") == "self-public-website" and entry.get("href"):
            link = entry["href"]
            break
    if not link:
        ad_id = ad.get("id") or _ka_value(_ka_get(ad, "id"))
        if not ad_id:
            return None
        link = f"https://www.kleinanzeigen.de/s-anzeige/{ad_id}"

    amount = _ka_value(_ka_get(ad, "price", "amount"))
    currency = _ka_value(_ka_get(ad, "price", "currency-iso-code")) or "EUR"
    if isinstance(currency, dict):
        currency = currency.get("value") or "EUR"
    price = None
    if amount is not None:
        try:
            price = Decimal(str(amount))
        except (InvalidOperation, ValueError):
            price = None
    symbol = {"EUR": "€", "USD": "$", "GBP": "£"}.get(currency, currency or "")
    price_text = f"{amount} {symbol}".strip() if amount is not None else "VB"

    # First picture (prefer a large variant) for the thumbnail.
    image_url = None
    pictures = _ka_get(ad, "pictures", "picture") or []
    if pictures:
        links = pictures[0].get("link", []) if isinstance(pictures[0], dict) else []
        by_rel = {l.get("rel"): l.get("href") for l in links if isinstance(l, dict)}
        image_url = (
            by_rel.get("large") or by_rel.get("teaser")
            or by_rel.get("thumbnail") or (links[0].get("href") if links else None)
        )

    location = _ka_value(_ka_get(ad, "ad-address", "state"))
    zip_code = _ka_value(_ka_get(ad, "ad-address", "zip-code"))
    if location and zip_code:
        location = f"{zip_code} {location}"
    elif zip_code:
        location = zip_code

    return Listing(
        title=title,
        link=link,
        price_text=price_text,
        price=price,
        currency=currency,
        image_url=image_url,
        location=location,
    )


def fetch_kleinanzeigen_listings_api(url: str, timeout: int = 10) -> list[Listing]:
    """Fetch Kleinanzeigen search results via the gateway JSON API.

    Translates a KA web search URL (e.g. https://www.kleinanzeigen.de/s-iphone-13-pro/k0)
    into a /ads.json query and maps the JSON ads onto Listing objects with the
    same shape parse_kleinanzeigen_listings() produces.
    """
    params: dict = {
        "q": _ka_query_from_url(url),
        "page": 0,
        "size": 30,
        "includeTopAds": "false",
        "limitTotalResultCount": "false",
    }
    session = _ka_api_session()
    response = session.get(f"{KA_API_BASE}/ads.json", params=params, timeout=timeout)
    if response.status_code != 200:
        raise requests.HTTPError(
            f"Kleinanzeigen gateway returned HTTP {response.status_code} for search",
            response=response,
        )
    payload = response.json()
    ads = _ka_get(payload, "ads", "value", "ad") or _ka_get(payload, "ads", "ad") or []
    listings: list[Listing] = []
    for ad in ads:
        listing = _ka_ad_to_listing(ad)
        if listing is not None:
            listings.append(listing)
    return listings


_KA_SLUG_RE = re.compile(r"/s-([^/]+?)(?:/(?:k\d+|c\d+|[\w-]*))*$")


def _ka_query_from_url(url: str) -> str:
    """Derive a free-text query from a KA web search URL.

    https://www.kleinanzeigen.de/s-iphone-13-pro/k0 -> 'iphone 13 pro'
    Also honours an explicit ?keywords= / ?q= query parameter if present.
    """
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("keywords", "q", "search_text"):
        if query.get(key):
            return query[key][0]
    # Path slug: take the first '/s-<slug>' segment and turn dashes into spaces.
    for segment in parsed.path.split("/"):
        if segment.startswith("s-"):
            slug = segment[2:]
            # Drop a trailing category-ish token (rare in the first segment).
            return slug.replace("-", " ").strip()
    return ""


def fetch_kleinanzeigen_ad_detail(url: str, timeout: int = 10) -> tuple[list[str], str]:
    """Fetch a single Kleinanzeigen ad's images + description via the gateway.

    Returns (image_urls, description). Raises on non-200 / missing id so callers
    can fall back to HTML scraping.
    """
    ad_id = ka_ad_id_from_url(url)
    if not ad_id:
        raise ValueError(f"No Kleinanzeigen ad id in URL: {url}")
    session = _ka_api_session()
    response = session.get(f"{KA_API_BASE}/ads/{ad_id}.json", timeout=timeout)
    if response.status_code != 200:
        raise requests.HTTPError(
            f"Kleinanzeigen gateway returned HTTP {response.status_code} for ad {ad_id}",
            response=response,
        )
    payload = response.json()
    ad = _ka_get(payload, "ad", "value") or _ka_get(payload, "ad") or {}
    ad = {_ka_strip_ns(k): v for k, v in ad.items()} if isinstance(ad, dict) else {}
    description = _ka_value(ad.get("description")) or ""

    images: list[str] = []
    pictures = _ka_get(ad, "pictures", "picture") or []
    for picture in pictures:
        links = picture.get("link", []) if isinstance(picture, dict) else []
        by_rel = {l.get("rel"): l.get("href") for l in links if isinstance(l, dict)}
        href = (
            by_rel.get("XXL") or by_rel.get("extraLarge") or by_rel.get("large")
            or by_rel.get("teaser") or (links[0].get("href") if links else None)
        )
        if href and "{imageId}" not in href:
            images.append(href)
    images = list(dict.fromkeys(images))
    return images, description


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

    if marketplace is KLEINANZEIGEN and not browser_fetcher:
        # Prefer the gateway JSON API — clean JSON, no markup churn. Fall back
        # to HTML scraping if it fails (token rotated, schema change, etc.).
        try:
            listings = fetch_kleinanzeigen_listings_api(url, timeout=timeout)
            if listings:
                return listings
        except Exception:
            pass

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
