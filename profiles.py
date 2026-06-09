from __future__ import annotations
import re
import urllib.parse
from dataclasses import dataclass, field
from decimal import Decimal

from filters import ListingFilter, parse_csv_words
from proxy import validate_proxy_url


@dataclass(frozen=True)
class SearchProfile:
    id: int | None
    name: str
    ebay_url: str
    include_keywords: str = ""
    exclude_keywords: str = ""
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    currency: str | None = None
    sold_window_days: int = 90
    enabled: bool = True
    proxy_url: str | None = None
    # Optional eBay search URL used to pull sold-price data for arbitrage profit
    # estimation when this profile monitors a non-eBay marketplace (KA / Vinted).
    ebay_reference_url: str | None = None
    # Optional Clevertronic category URL for condition-based refurbished sell prices.
    # e.g. https://www.clevertronic.de/kaufen/handy-kaufen/apple/iphone-12
    clevertronic_url: str | None = None
    # Optional ZOXS product URL for Ankaufpreise (what ZOXS pays you) per condition.
    # e.g. https://www.zoxs.de/verkaufen/iphone-12-ankauf/B08L5TNKZC.html
    zoxs_url: str | None = None
    # Optional WirKaufens product URL for Ankaufpreise.
    # e.g. https://wirkaufens.de/produkte/apple-iphone-12-128-gb
    wirkaufens_url: str | None = None
    # Buyback platforms to search automatically (new flow).
    # List of platform keys: ["zoxs", "wirkaufens", "clevertronic"]
    # When set, overrides the individual *_url fields above.
    buyback_platforms: list[str] = field(default_factory=list)
    # Additional search URLs (Kleinanzeigen, Vinted, extra eBay) scanned alongside ebay_url
    extra_urls: list[str] = field(default_factory=list)

    def __post_init__(self):
        object.__setattr__(self, "proxy_url", validate_proxy_url(self.proxy_url))

    @property
    def listing_filter(self) -> ListingFilter:
        return ListingFilter(
            include_keywords=parse_csv_words(self.include_keywords),
            exclude_keywords=parse_csv_words(self.exclude_keywords),
            min_price=self.min_price,
            max_price=self.max_price,
            currency=self.currency,
        )

    @property
    def keyword_signature(self) -> str:
        return self.include_keywords.strip().casefold()

    @property
    def ebay_search_keyword(self) -> str:
        """Extract the search keyword from the eBay (or KA/Vinted) search URL."""
        # Check all URLs (primary + extra) and return first meaningful keyword found
        for url in [self.ebay_url] + (self.extra_urls or []):
            kw = self._keyword_from_url(url)
            if kw and kw not in ("k0", "catalog", "sch"):
                return kw
        return self.include_keywords

    @staticmethod
    def _keyword_from_url(url: str) -> str:
        try:
            parsed = urllib.parse.urlparse(url)
            qs = urllib.parse.parse_qs(parsed.query)
            # eBay: _nkw, Vinted: search_text, generic: q/query
            for param in ("_nkw", "search_text", "query", "q"):
                if param in qs:
                    return urllib.parse.unquote_plus(qs[param][0])
            # Kleinanzeigen: /s-iphone-12/k0  → take segment before /k0
            path_parts = [p for p in parsed.path.strip("/").split("/") if p and p != "k0"]
            for part in reversed(path_parts):
                if part.startswith("s-"):
                    return part[2:].replace("-", " ")
            # Generic slug fallback
            slug = path_parts[-1] if path_parts else ""
            return slug.replace("-", " ")
        except Exception:
            return ""
