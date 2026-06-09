"""
Buyback / refurbished-price scrapers for arbitrage profit estimation.

Supported sites
---------------
clevertronic  – sells refurbished phones; shows starting price per condition.
                URL pattern: https://www.clevertronic.de/kaufen/handy-kaufen/BRAND/MODEL

zoxs          – buyback site; shows Ankaufpreis (what they pay you) per condition.
                URL pattern: https://www.zoxs.de/verkaufen/MODEL-ankauf/ASIN.html
                e.g.        https://www.zoxs.de/verkaufen/iphone-12-ankauf/B08L5TNKZC.html
                (navigate to the ZOXS product page for your specific model to get the URL)

Usage
-----
    from buyback import BuybackScraper
    with BuybackScraper() as scraper:
        # Clevertronic sell prices (what they charge customers)
        ct = scraper.clevertronic("https://www.clevertronic.de/kaufen/handy-kaufen/apple/iphone-12")
        # {"Sehr gut": Decimal("294.99"), "Gut": Decimal("259.99"), ...}

        # ZOXS buy prices (what they pay you — instant sell option)
        zoxs = scraper.zoxs("https://www.zoxs.de/verkaufen/iphone-12-ankauf/B08L5TNKZC.html")
        # {"Wie neu": Decimal("146.72"), "Hervorragend": Decimal("121.51"), ...}
"""
from __future__ import annotations

import re
import time
from decimal import Decimal, InvalidOperation

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Condition name as shown on Clevertronic
# ---------------------------------------------------------------------------
CLEVERTRONIC_CONDITIONS = ("Neu", "Wie neu", "Sehr gut", "Gut", "Akzeptabel", "Gebraucht")

# ---------------------------------------------------------------------------
# ZOXS condition IDs and their German display names
# ---------------------------------------------------------------------------
ZOXS_SKIP_CONDITIONS = {"Neu", "Schlecht"}   # extremes, usually 0 € anyway


class BuybackScraper:
    """
    Context manager that keeps one Playwright browser instance alive across
    multiple scrape calls for efficiency.
    """

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

    def __exit__(self, *args) -> None:
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    # ------------------------------------------------------------------
    # Clevertronic – sell prices (what Clevertronic charges customers)
    # ------------------------------------------------------------------

    def clevertronic(self, url: str) -> dict[str, Decimal]:
        """
        Return {condition_name: lowest_price} for the given Clevertronic
        category URL.  Only conditions marked data-available="1" are included.

        Example URL:
            https://www.clevertronic.de/kaufen/handy-kaufen/apple/iphone-12
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
            # Seed homepage so Clevertronic recognises the session
            parsed_base = url.split("/kaufen/")[0]
            page.goto(parsed_base, wait_until="domcontentloaded", timeout=20000)
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
                if not name_span:
                    continue
                name = name_span.get_text(strip=True)
                price_span = btn.find("span", class_="right")
                if not price_span:
                    continue
                price = _parse_eur(price_span.get_text(" ", strip=True))
                if name and price is not None:
                    result[name] = price
        finally:
            ctx.close()
        return result

    # ------------------------------------------------------------------
    # ZOXS – buy prices (Ankaufpreise – what ZOXS pays you)
    # ------------------------------------------------------------------

    def zoxs(self, url: str) -> dict[str, Decimal]:
        """
        Return {condition_name: ankaufpreis} for the given ZOXS product URL.

        The scraper:
        1. Opens the ZOXS product page (ASIN-based URL).
        2. Dismisses the GDPR cookie modal.
        3. Clicks each selectable condition label.
        4. Reads the #articlePrice element after each click.

        Example URL:
            https://www.zoxs.de/verkaufen/iphone-12-ankauf/B08L5TNKZC.html
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
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)

            # Accept GDPR modal ("Alles akzeptieren")
            try:
                page.locator(".js-gdpr-accept-all").first.click(timeout=6000)
                page.wait_for_timeout(1000)
            except Exception:
                pass

            # Get condition label data from the DOM
            soup = BeautifulSoup(page.content(), "html.parser")
            seen_ids: set[str] = set()
            conditions: list[tuple[str, str]] = []   # (condition_id, display_name)
            for el in soup.find_all("div", class_="conditionLabel"):
                cid = el.get("data-conditionid", "")
                name = el.get("data-frontendname", "")
                if cid and cid not in seen_ids and name and name not in ZOXS_SKIP_CONDITIONS:
                    seen_ids.add(cid)
                    conditions.append((cid, name))

            for cid, name in conditions:
                try:
                    # IDs like "1ConditionLabel" start with a digit → use attribute selector
                    page.locator(f'[id="{cid}ConditionLabel"]:visible').first.click(timeout=6000)
                    page.wait_for_timeout(1800)

                    price_text = page.locator("#articlePrice").text_content(timeout=3000)
                    price = _parse_eur(price_text or "")
                    if price and price > 0:
                        result[name] = price
                except Exception:
                    pass   # condition not clickable or no price

        finally:
            ctx.close()
        return result


def _parse_eur(text: str) -> Decimal | None:
    """Parse a German-format EUR price string like '183,47 €' → Decimal('183.47')."""
    text = text.replace("\xa0", " ").replace(" ", " ")
    m = re.search(r"(\d[\d.]*[.,]\d{2})", text)
    if not m:
        return None
    raw = m.group(1).replace(".", "").replace(",", ".")
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None
