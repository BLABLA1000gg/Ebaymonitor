from __future__ import annotations
from dataclasses import dataclass
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
