from dataclasses import dataclass
from decimal import Decimal

from filters import ListingFilter, parse_csv_words


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
