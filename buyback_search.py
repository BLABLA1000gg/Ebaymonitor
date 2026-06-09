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
                    return (data?.hits?.hits || []).map(h => ({
                        id: h._source.id,
                        name: h._source.name,
                        url: 'https://wirkaufens.de/produkte/' + h._source.url
                    })).filter(x => x.id);
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
    Search ZOXS by fetching the Apple iPhone category page and filtering
    by the query keyword. Returns [{name, url}].
    The URL is the ASIN-based product URL needed by BuybackScraper.zoxs().
    """
    key = f"zoxs:{query.lower()}"
    cached = _cached(key)
    if cached is not None:
        return cached

    try:
        from curl_cffi.requests import Session as CurlSession
        from bs4 import BeautifulSoup

        # Determine category page from query keywords
        q_lower = query.lower()
        if "iphone" in q_lower or "apple" in q_lower:
            cat_url = "https://www.zoxs.de/verkaufen/apple-iphone-ankauf.html"
        elif "samsung" in q_lower or "galaxy" in q_lower:
            cat_url = "https://www.zoxs.de/verkaufen/samsung-galaxy-ankauf.html"
        elif "pixel" in q_lower:
            cat_url = "https://www.zoxs.de/verkaufen/google-pixel-ankauf.html"
        else:
            cat_url = "https://www.zoxs.de/verkaufen/handy-smartphone-ankauf.html"

        headers = {
            "Accept": "text/html,*/*;q=0.8",
            "Accept-Language": "de-DE,de;q=0.9",
        }
        with CurlSession(impersonate="chrome120") as s:
            # Seed homepage
            s.get("https://www.zoxs.de/", headers=headers, timeout=10)
            r = s.get(cat_url, headers={**headers, "Referer": "https://www.zoxs.de/"}, timeout=10)

        soup = BeautifulSoup(r.text, "html.parser")

        # Product cards: <a class="... nav-link ..."> wrapping <div class="article-card">
        results = []
        # Split query into meaningful words (min 2 chars), require ALL to match
        q_words = [w for w in re.split(r"\s+", q_lower) if len(w) >= 2]

        for a in soup.find_all("a", class_="nav-link", href=True):
            href = a.get("href", "")
            # Only ASIN-based links (e.g. iphone-12-ankauf/B08L5TNKZC.html)
            if not re.search(r"/[A-Z0-9]{10}\.html$", href):
                continue
            title_span = a.find("span")
            name = title_span.get_text(strip=True) if title_span else a.get_text(strip=True)
            if not name:
                continue
            name_lower = name.lower()
            if all(w in name_lower for w in q_words):
                full_url = f"https://www.zoxs.de/{href.lstrip('/')}"
                results.append({"name": name, "url": full_url})
            if len(results) >= 10:
                break

        return _store(key, results)

    except Exception:
        return []


# ------------------------------------------------------------------ #
# Clevertronic – scrape product links from category pages             #
# ------------------------------------------------------------------ #

def search_clevertronic(query: str) -> list[dict]:
    """
    Search Clevertronic by fetching their Apple category page and filtering.
    Returns [{name, url}] where url is the category page for BuybackScraper.clevertronic().
    """
    key = f"ct:{query.lower()}"
    cached = _cached(key)
    if cached is not None:
        return cached

    try:
        from curl_cffi.requests import Session as CurlSession
        from bs4 import BeautifulSoup

        q_lower = query.lower()
        if "iphone" in q_lower or "apple" in q_lower:
            cat_url = "https://www.clevertronic.de/kaufen/handy-kaufen/apple"
        elif "samsung" in q_lower or "galaxy" in q_lower:
            cat_url = "https://www.clevertronic.de/kaufen/handy-kaufen/samsung"
        elif "pixel" in q_lower:
            cat_url = "https://www.clevertronic.de/kaufen/handy-kaufen/google"
        else:
            cat_url = "https://www.clevertronic.de/kaufen/handy-kaufen"

        headers = {"Accept": "text/html,*/*;q=0.8", "Accept-Language": "de-DE,de;q=0.9",
                   "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        with CurlSession(impersonate="chrome120") as s:
            r = s.get(cat_url, headers=headers, timeout=10)

        soup = BeautifulSoup(r.text, "html.parser")
        q_words = [w for w in re.split(r"\s+", q_lower) if len(w) >= 2]
        results = []

        for a in soup.find_all("a", href=re.compile(r"/kaufen/handy-kaufen/\w+/[\w-]+")):
            href = a.get("href", "")
            # Skip generic category links (too short path)
            parts = href.strip("/").split("/")
            if len(parts) < 4:
                continue
            name = a.get_text(strip=True) or parts[-1].replace("-", " ").title()
            if not name or len(name) < 4:
                continue
            name_lower = name.lower()
            href_lower = href.lower()
            # Match against name OR href slug
            combined = name_lower + " " + href_lower
            if all(w in combined for w in q_words):
                full_url = f"https://www.clevertronic.de{href}" if href.startswith("/") else href
                if not any(r["url"] == full_url for r in results):
                    results.append({"name": name, "url": full_url})
            if len(results) >= 10:
                break

        return _store(key, results)

    except Exception:
        return []
