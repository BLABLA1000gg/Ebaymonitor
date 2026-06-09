"""
Buyback / refurbished-price scrapers for arbitrage profit estimation.

Supported sites
---------------
clevertronic  – sells refurbished phones; shows starting price per condition
                URL pattern: https://www.clevertronic.de/kaufen/handy-kaufen/BRAND/MODEL

Usage
-----
    from buyback import BuybackScraper
    with BuybackScraper() as scraper:
        prices = scraper.clevertronic("https://www.clevertronic.de/kaufen/handy-kaufen/apple/iphone-12")
        # {"Sehr gut": Decimal("294.99"), "Gut": Decimal("259.99"), "Gebraucht": Decimal("207.99")}
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from bs4 import BeautifulSoup

from browser_fetch import BrowserFetcher

# Condition name as shown on Clevertronic
CLEVERTRONIC_CONDITIONS = ("Neu", "Wie neu", "Sehr gut", "Gut", "Akzeptabel", "Gebraucht")


class BuybackScraper:
    """Context manager that keeps one browser instance alive for all scrape calls."""

    def __init__(self) -> None:
        self._fetcher = BrowserFetcher()

    def __enter__(self) -> "BuybackScraper":
        self._fetcher.__enter__()
        return self

    def __exit__(self, *args) -> None:
        self._fetcher.__exit__(*args)

    # ------------------------------------------------------------------
    # Clevertronic
    # ------------------------------------------------------------------

    def clevertronic(self, url: str) -> dict[str, Decimal]:
        """
        Return a mapping of {condition_name: lowest_price} for the given
        Clevertronic category URL, e.g.
        https://www.clevertronic.de/kaufen/handy-kaufen/apple/iphone-12

        Only conditions with data-available="1" are included.
        Prices are the starting price shown on the condition selector button.
        """
        page = self._fetcher.get(url, timeout=30)
        soup = BeautifulSoup(page.text, "html.parser")
        result: dict[str, Decimal] = {}

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
            price_text = price_span.get_text(" ", strip=True)
            price = _parse_eur(price_text)
            if name and price is not None:
                result[name] = price

        return result


def _parse_eur(text: str) -> Decimal | None:
    """Parse a German-format EUR price string like '259,99 €' → Decimal('259.99')."""
    m = re.search(r"(\d[\d.]*[.,]\d{2})", text.replace("\xa0", " "))
    if not m:
        return None
    raw = m.group(1).replace(".", "").replace(",", ".")
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None
