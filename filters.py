import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from models import Listing


PRICE_RE = re.compile(r"(?P<amount>\d[\d.,\s]*\d|\d)")


def parse_price(value: str) -> tuple[Decimal | None, str | None]:
    match = PRICE_RE.search(value.replace("\xa0", " "))
    if not match:
        return None, None

    raw = match.group("amount").replace(" ", "")
    if "," in raw and "." in raw:
        decimal_separator = "," if raw.rfind(",") > raw.rfind(".") else "."
        thousands_separator = "." if decimal_separator == "," else ","
        raw = raw.replace(thousands_separator, "").replace(decimal_separator, ".")
    elif "," in raw:
        tail = raw.rsplit(",", 1)[1]
        raw = raw.replace(",", ".") if len(tail) <= 2 else raw.replace(",", "")
    elif raw.count(".") > 1:
        raw = raw.replace(".", "")

    try:
        amount = Decimal(raw)
    except InvalidOperation:
        return None, None

    upper = value.upper()
    currency = None
    if "EUR" in upper or "€" in value:
        currency = "EUR"
    elif "USD" in upper or "$" in value:
        currency = "USD"
    elif "GBP" in upper or "£" in value:
        currency = "GBP"
    return amount, currency


def parse_csv_words(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(word.strip().casefold() for word in value.split(",") if word.strip())


@dataclass(frozen=True)
class ListingFilter:
    include_keywords: tuple[str, ...] = ()
    exclude_keywords: tuple[str, ...] = ()
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    currency: str | None = None

    def matches(self, listing: Listing) -> bool:
        haystack = " ".join(
            part for part in (listing.title, listing.condition, listing.location) if part
        ).casefold()
        if self.include_keywords and not all(word in haystack for word in self.include_keywords):
            return False
        if any(word in haystack for word in self.exclude_keywords):
            return False
        if self.currency and listing.currency != self.currency.upper():
            return False
        if self.min_price is not None and (listing.price is None or listing.price < self.min_price):
            return False
        if self.max_price is not None and (listing.price is None or listing.price > self.max_price):
            return False
        return True
