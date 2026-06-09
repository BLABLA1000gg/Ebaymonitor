"""
Buyback / refurbished-price scrapers for arbitrage profit estimation.

Supported sites
---------------
clevertronic  – sells refurbished phones; shows lowest price per condition.
                URL: https://www.clevertronic.de/kaufen/handy-kaufen/BRAND/MODEL

zoxs          – buyback site (Ankauf); shows what ZOXS pays you per condition.
                URL: https://www.zoxs.de/verkaufen/MODEL-ankauf/ASIN.html
                Find the URL at zoxs.de → Handys → Apple → your model.

wirkaufens    – buyback site; prices are 0 € when their stock is full (common
                for popular models). Will be implemented when their API is stable.

Usage
-----
    from buyback import BuybackScraper
    with BuybackScraper() as scraper:
        ct = scraper.clevertronic("https://www.clevertronic.de/kaufen/handy-kaufen/apple/iphone-12")
        # {"Sehr gut": Decimal("294.99"), "Gut": Decimal("259.99"), ...}

        zoxs = scraper.zoxs("https://www.zoxs.de/verkaufen/iphone-12-ankauf/B08L5TNKZC.html")
        # {"Wie neu": Decimal("146.72"), "Sehr gut": Decimal("105.04"), ...}
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

CLEVERTRONIC_CONDITIONS = ("Neu", "Wie neu", "Sehr gut", "Gut", "Akzeptabel", "Gebraucht")
ZOXS_SKIP_CONDITIONS = {"Neu", "Schlecht"}

# ZOXS condition IDs → German display names
ZOXS_CONDITIONS = {
    "1": "Wie neu",
    "2": "Hervorragend",
    "3": "Sehr gut",
    "4": "Gut",
    "65": "Stark gebraucht",
}


class BuybackScraper:
    """Context manager that shares one Playwright browser across all scrape calls."""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None

    def __enter__(self) -> "BuybackScraper":
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=False,
            args=[
                "--headless=new",
                "--lang=de-DE",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        return self

    def __exit__(self, *args: Any) -> None:
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    # ------------------------------------------------------------------ #
    # Clevertronic – sell prices (what they charge customers)             #
    # ------------------------------------------------------------------ #

    def clevertronic(self, url: str) -> dict[str, Decimal]:
        """
        Scrape condition-based sell prices from a Clevertronic category page.

        Returns {condition_name: lowest_price} for available conditions only.
        """
        ctx = self._browser.new_context(
            locale="de-DE",
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()
        result: dict[str, Decimal] = {}
        try:
            base = url.split("/kaufen/")[0]
            page.goto(base, wait_until="domcontentloaded", timeout=20000)
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_selector("div.button_modellfilter_zustand", timeout=12000)
            except Exception:
                page.wait_for_timeout(4000)

            soup = BeautifulSoup(page.content(), "html.parser")
            for btn in soup.find_all("div", class_="button_modellfilter_zustand"):
                if btn.get("data-available") != "1":
                    continue
                name_span = btn.find("span", recursive=False)
                price_span = btn.find("span", class_="right")
                if not name_span or not price_span:
                    continue
                name = name_span.get_text(strip=True)
                price = _parse_eur(price_span.get_text(" ", strip=True))
                if name and price is not None:
                    result[name] = price
        finally:
            ctx.close()
        return result

    # ------------------------------------------------------------------ #
    # ZOXS – Ankaufpreise (what ZOXS pays you, per condition)            #
    # ------------------------------------------------------------------ #

    def zoxs(self, url: str) -> dict[str, Decimal]:
        """
        Scrape ZOXS Ankaufpreise for all conditions of a product.

        Strategy
        --------
        1. Open the product page (ASIN URL) to obtain Cloudflare clearance cookies.
        2. Accept the GDPR modal.
        3. Click the first available condition to trigger a ``sys_article_price.php``
           XHR request; intercept it to capture ``articleId`` and ``questions``.
        4. Use the browser's own ``fetch()`` (which carries CF cookies) to query
           all remaining conditions without further UI interaction.

        Returns {condition_name: ankaufpreis} for conditions with price > 0.
        """
        ctx = self._browser.new_context(
            locale="de-DE",
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()
        result: dict[str, Decimal] = {}

        # Intercept one API request to learn articleId + questions
        captured: dict[str, Any] = {}

        def on_request(req: Any) -> None:
            if "sys_article_price.php" in req.url and req.post_data and not captured:
                try:
                    captured.update(json.loads(req.post_data))
                except Exception:
                    pass

        page.on("request", on_request)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)

            # Accept GDPR modal
            try:
                page.locator(".js-gdpr-accept-all").first.click(timeout=6000)
                page.wait_for_timeout(800)
            except Exception:
                pass

            # Get condition labels from the page
            soup = BeautifulSoup(page.content(), "html.parser")
            seen_ids: set[str] = set()
            conditions: list[tuple[str, str]] = []  # (cond_id, display_name)
            for el in soup.find_all("div", class_="conditionLabel"):
                cid = el.get("data-conditionid", "")
                name = el.get("data-frontendname", "")
                if cid and cid not in seen_ids and name and name not in ZOXS_SKIP_CONDITIONS:
                    seen_ids.add(cid)
                    conditions.append((cid, name))

            if not conditions:
                return result

            # Click the first condition to trigger one API call → captures articleId + questions
            first_cid, _ = conditions[0]
            try:
                page.locator(f'[id="{first_cid}ConditionLabel"]:visible').first.click(timeout=6000)
                page.wait_for_timeout(1800)
            except Exception:
                pass

            if not captured.get("articleId") or not captured.get("questions"):
                return result  # page probably shows "we don't buy this" message

            article_id = captured["articleId"]
            questions = captured["questions"]

            # Now call sys_article_price.php for every condition via the browser's fetch()
            # (browser carries Cloudflare CF cookies; direct requests would be blocked)
            for cid, name in conditions:
                body = json.dumps({
                    "articleId": article_id,
                    "condition": int(cid),
                    "buyback": False,
                    "questions": questions,
                    "zoxsCheck": False,
                })
                api_result = page.evaluate(
                    """
                    async ([body]) => {
                        try {
                            const r = await fetch('/sys_article_price.php', {
                                method: 'POST',
                                headers: {
                                    'Content-Type': 'application/json',
                                    'X-Requested-With': 'XMLHttpRequest',
                                    'Accept': 'application/json, text/javascript, */*; q=0.01'
                                },
                                body: body,
                                credentials: 'include'
                            });
                            if (!r.ok) return null;
                            return await r.json();
                        } catch(e) {
                            return null;
                        }
                    }
                    """,
                    [body],
                )
                if api_result and isinstance(api_result, dict):
                    raw = api_result.get("priceRaw", 0)
                    if raw and raw > 0:
                        result[name] = Decimal(str(raw))

        finally:
            ctx.close()
        return result


def _parse_eur(text: str) -> Decimal | None:
    """Parse a German-format EUR string like '259,99 €' → Decimal('259.99')."""
    text = text.replace("\xa0", " ")
    m = re.search(r"(\d[\d.]*[.,]\d{2})", text)
    if not m:
        return None
    raw = m.group(1).replace(".", "").replace(",", ".")
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None
