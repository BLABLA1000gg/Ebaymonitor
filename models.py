from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


@dataclass(frozen=True)
class Listing:
    title: str
    link: str
    price_text: str
    price: Decimal | None
    currency: str | None
    image_url: str | None = None
    condition: str | None = None
    shipping: str | None = None
    location: str | None = None
    # Auction metadata (eBay). Populated only for auction listings; BIN
    # listings keep the defaults below so existing constructors stay valid.
    is_auction: bool = False
    bid_count: int | None = None
    time_left: str | None = None
    end_time: str | None = None


class EventType(str, Enum):
    NEW = "new"
    PRICE_DROP = "price_drop"
    PRICE_INCREASE = "price_increase"
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class ListingEvent:
    type: EventType
    listing: Listing
    previous_price: Decimal | None = None

    @property
    def price_change(self) -> Decimal | None:
        if self.listing.price is None or self.previous_price is None:
            return None
        return self.listing.price - self.previous_price

    @property
    def price_change_percent(self) -> Decimal | None:
        if self.previous_price in {None, Decimal("0")} or self.listing.price is None:
            return None
        return ((self.listing.price - self.previous_price) / self.previous_price) * 100
