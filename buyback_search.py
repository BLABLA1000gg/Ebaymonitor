"""
Lightweight product search for buyback/refurbished sites.
Used by the profile form autocomplete — returns product name + URL per site.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

_cache: dict[str, tuple[float, list[dict]]] = {}
_CACHE_TTL = 1800  # 30 min


def _cached(key: str) -> list[dict] | None:
    entry = _cache.get(key)
    if entry and time.time() - entry[0] < _CACHE_TTL:
        return entry[1]
    return None


def _store(key: str, results: list[dict]) -> list[dict]:
    _cache[key] = (time.time(), results)
    return results


# ------------------------------------------------------------------ #
# WirKaufens – Elasticsearch-backed product search                    #
# ------------------------------------------------------------------ #

def search_wirkaufens(query: str) -> list[dict]:
    """Search WirKaufens product catalogue. Returns [{name, url, id}]."""
    key = f"wkfs:{query.lower()}"
    cached = _cached(key)
    if cached is not None:
        return cached

    try:
        from playwright.sync_api import sync_playwright

        import json as _json

        # Build ES source as the browser does (plain text query inside JSON)
        es_source = _json.dumps({
            "query": {
                "function_score": {
                    "query": {
                        "bool": {
                            "should": [
                                {"multi_match": {"fields": ["model^2", "searchTerms^2"],
                                                 "query": query, "type": "phrase_prefix"}},
                                {"match": {"searchTerms": {"query": query, "fuzziness": "AUTO"}}}
                            ]
                        }
                    },
                    "functions": [{"field_value_factor": {"field": "releaseYear", "factor": 1,
                                                          "missing": 0, "modifier": "none"}}],
                    "boost_mode": "sum"
                }
            }
        }, ensure_ascii=False)

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=False,
                args=["--headless=new", "--lang=de-DE", "--no-sandbox", "--disable-dev-shm-usage"],
            )
            ctx = browser.new_context(locale="de-DE", viewport={"width": 1200, "height": 800})
            page = ctx.new_page()
            page.goto("https://wirkaufens.de", wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(1000)
            page.evaluate("document.getElementById('cookiescript_accept')?.click()")
            page.wait_for_timeout(500)

            hits = page.evaluate(
                """
                async ([source, query]) => {
                    const body = {
                        search: '&source=' + source + '&source_content_type=application/json',
                        application: 'shop-de',
                        resultSize: 100
                    };
                    const r = await fetch('/trade-in/searches', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
                        body: JSON.stringify(body),
                        credentials: 'include'
                    });
                    const data = await r.json();
                    // Filter: all query words must appear as whole words in the product name
                    const qwords = query.toLowerCase().split(/\s+/).filter(w => w.length >= 2);
                    return (data?.hits?.hits || [])
                        .map(h => ({
                            id: h._source.id,
                            name: h._source.name,
                            url: 'https://wirkaufens.de/produkte/' + h._source.url
                        }))
                        .filter(x => {
                            if (!x.id) return false;
                            // Normalize: remove spaces before GB/TB so "128 GB" == "128GB"
                            const nl = x.name.toLowerCase().replace(/(\d)\s+(gb|tb)/g, '$1$2');
                            return qwords.every(w => {
                                // Normalize query word too
                                const wn = w.replace(/(\d)\s+(gb|tb)/g, '$1$2');
                                // Use word boundary for pure numbers to avoid "12" matching "12GB RAM"
                                if (/^\d+$/.test(wn)) {
                                    return new RegExp('(?<![\\d])' + wn + '(?![\\d])').test(nl);
                                }
                                // For "128gb" style tokens, match the number part with word boundary
                                const gbMatch = wn.match(/^(\d+)(gb|tb)$/);
                                if (gbMatch) {
                                    return new RegExp('(?<![\\d])' + gbMatch[1] + gbMatch[2]).test(nl);
                                }
                                return nl.includes(wn);
                            });
                        });
                }
                """,
                [es_source, query],
            )
            ctx.close()
            browser.close()

        results = hits or []
        return _store(key, results)

    except Exception:
        return []


# ------------------------------------------------------------------ #
# ZOXS – scrape product cards from their Apple category page          #
# ------------------------------------------------------------------ #

def search_zoxs(query: str) -> list[dict]:
    """
    Search ZOXS for a phone model. Returns [{name, url}].
    URL is the ASIN-based product URL needed by BuybackScraper.zoxs().

    Strategy: build a model-specific category slug (e.g. "iphone-12") and
    try that page first; fall back to the brand-level category page.
    """
    key = f"zoxs:{query.lower()}"
    cached = _cached(key)
    if cached is not None:
        return cached

    try:
        from curl_cffi.requests import Session as CurlSession
        from bs4 import BeautifulSoup

        q_lower = query.lower()

        # Build candidate category URLs, most specific first
        cat_urls = _zoxs_category_urls(q_lower)

        headers = {
            "Accept": "text/html,*/*;q=0.8",
            "Accept-Language": "de-DE,de;q=0.9",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        }

        # Split query into meaningful tokens; use word-boundary matching so
        # "12" doesn't accidentally match inside "128GB".
        q_words = [w for w in re.split(r"\s+", q_lower) if len(w) >= 2]

        def _word_matches(name_lower: str, words: list[str]) -> bool:
            for w in words:
                # Use word boundary if token is purely numeric (avoid "12" ⊂ "128")
                if w.isdigit():
                    if not re.search(r"(?<!\d)" + re.escape(w) + r"(?!\d)", name_lower):
                        return False
                else:
                    if w not in name_lower:
                        return False
            return True

        results: list[dict] = []
        with CurlSession(impersonate="chrome120") as s:
            s.get("https://www.zoxs.de/", headers=headers, timeout=10)
            for cat_url in cat_urls:
                r = s.get(cat_url, headers={**headers, "Referer": "https://www.zoxs.de/"}, timeout=10)
                soup = BeautifulSoup(r.text, "html.parser")
                for a in soup.find_all("a", class_="nav-link", href=True):
                    href = a.get("href", "")
                    if not re.search(r"/[A-Z0-9]{10}\.html$", href):
                        continue
                    title_span = a.find("span")
                    name = title_span.get_text(strip=True) if title_span else a.get_text(strip=True)
                    if not name:
                        continue
                    name_lower = name.lower()
                    if _word_matches(name_lower, q_words):
                        full_url = f"https://www.zoxs.de/{href.lstrip('/')}"
                        if not any(r["url"] == full_url for r in results):
                            results.append({"name": name, "url": full_url})
                if results:
                    break  # found results on this category page — stop

        return _store(key, results[:10])

    except Exception:
        return []


def _zoxs_category_urls(q_lower: str) -> list[str]:
    """Build ZOXS category page URLs to try, from most to least specific."""
    urls = []

    # Try to extract a model slug like "iphone-12-pro" from the query
    # Pattern: "iphone" or "galaxy" followed by model identifier
    model_match = re.search(
        r"(iphone[\s\-]?(?:se[\s\-]?(?:\d{4})?\s*|(?:\d+(?:\s*(?:pro|plus|max|mini|ultra|ultra\s*max))*)))",
        q_lower,
    )
    if model_match:
        slug = re.sub(r"\s+", "-", model_match.group(1).strip().rstrip("-"))
        # Remove storage size from slug if present
        slug = re.sub(r"-?\d{2,4}\s*gb.*", "", slug).strip("-")
        if slug:
            urls.append(f"https://www.zoxs.de/verkaufen/apple-{slug}-ankauf.html")
            urls.append(f"https://www.zoxs.de/verkaufen/{slug}-ankauf.html")

    galaxy_match = re.search(r"(galaxy[\s\-]?[a-z0-9]+(?:[\s\-][a-z0-9]+)*)", q_lower)
    if galaxy_match:
        slug = re.sub(r"\s+", "-", galaxy_match.group(1).strip())
        slug = re.sub(r"-?\d{2,4}\s*gb.*", "", slug).strip("-")
        if slug:
            urls.append(f"https://www.zoxs.de/verkaufen/{slug}-ankauf.html")

    pixel_match = re.search(r"(pixel[\s\-]?\d+[a-z]*)", q_lower)
    if pixel_match:
        slug = re.sub(r"\s+", "-", pixel_match.group(1).strip())
        urls.append(f"https://www.zoxs.de/verkaufen/{slug}-ankauf.html")

    # Fall back to brand-level pages
    if "iphone" in q_lower or "apple" in q_lower:
        urls.append("https://www.zoxs.de/verkaufen/apple-iphone-ankauf.html")
    if "samsung" in q_lower or "galaxy" in q_lower:
        urls.append("https://www.zoxs.de/verkaufen/samsung-galaxy-ankauf.html")
    if "pixel" in q_lower:
        urls.append("https://www.zoxs.de/verkaufen/google-pixel-ankauf.html")
    urls.append("https://www.zoxs.de/verkaufen/handy-smartphone-ankauf.html")
    return urls


# ------------------------------------------------------------------ #
# Clevertronic – search trade-in (Ankauf) product pages              #
# ------------------------------------------------------------------ #

def search_clevertronic(query: str) -> list[dict]:
    """
    Search Clevertronic Ankauf products via their autocomplete API.
    Returns [{name, url}] where url is a /handy_verkaufen/ product page.
    """
    key = f"ct:{query.lower()}"
    cached = _cached(key)
    if cached is not None:
        return cached

    try:
        from curl_cffi.requests import Session as CurlSession
        import json as _json

        headers = {
            "Accept": "application/json, text/javascript, */*",
            "Accept-Language": "de-DE,de;q=0.9",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Referer": "https://www.clevertronic.de/handy_verkaufen/",
        }

        import urllib.parse
        api_url = f"https://www.clevertronic.de/findTypes.php?is_ankauf=1&query={urllib.parse.quote(query)}"

        with CurlSession(impersonate="chrome120") as s:
            r = s.get(api_url, headers=headers, timeout=10)
            data = _json.loads(r.text)

        results = []
        for item in data.get("suggestions", []):
            name = item.get("article_type_name") or item.get("value", "")
            path = item.get("buying_page_url", "")
            if name and path:
                full_url = f"https://www.clevertronic.de{path}" if path.startswith("/") else path
                results.append({"name": name, "url": full_url})

        return _store(key, results[:10])

    except Exception:
        return []
