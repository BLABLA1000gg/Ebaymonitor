from dataclasses import dataclass
from decimal import Decimal
from statistics import median

from models import Listing


@dataclass(frozen=True)
class MarketMetrics:
    raw_count: int
    accepted_count: int
    average: Decimal | None
    median: Decimal | None
    minimum: Decimal | None
    maximum: Decimal | None
    sold_per_month: Decimal
    active_count: int
    sell_through_rate: Decimal | None
    estimated_days_to_sell: Decimal | None
    demand: str


def _percentile(values: list[Decimal], fraction: Decimal) -> Decimal:
    if len(values) == 1:
        return values[0]
    position = fraction * Decimal(len(values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = position - lower
    return values[lower] + (values[upper] - values[lower]) * weight


def robust_prices(listings: list[Listing]) -> list[Decimal]:
    prices = sorted(listing.price for listing in listings if listing.price is not None and listing.price > 0)
    if len(prices) < 4:
        return prices
    q1 = _percentile(prices, Decimal("0.25"))
    q3 = _percentile(prices, Decimal("0.75"))
    iqr = q3 - q1
    iqr_low, iqr_high = q1 - Decimal("1.5") * iqr, q3 + Decimal("1.5") * iqr
    center = Decimal(str(median(prices)))
    deviations = sorted(abs(price - center) for price in prices)
    mad = Decimal(str(median(deviations)))
    if mad == 0:
        return [price for price in prices if iqr_low <= price <= iqr_high]
    mad_limit = Decimal("3.5") * Decimal("1.4826") * mad
    return [
        price for price in prices
        if iqr_low <= price <= iqr_high and abs(price - center) <= mad_limit
    ]


def market_metrics(sold: list[Listing], active_count: int, sold_window_days: int = 90) -> MarketMetrics:
    prices = robust_prices(sold)
    sold_per_month = Decimal(len(prices)) * Decimal(30) / Decimal(max(sold_window_days, 1))
    sell_through = None
    estimated_days = None
    if active_count > 0:
        sell_through = min(Decimal("1"), sold_per_month / Decimal(active_count))
    if sold_per_month > 0:
        estimated_days = Decimal(active_count) / sold_per_month * Decimal(30)
    if sell_through is None or sell_through < Decimal("0.25"):
        demand = "low"
    elif sell_through < Decimal("0.75"):
        demand = "medium"
    else:
        demand = "high"
    return MarketMetrics(
        raw_count=sum(1 for item in sold if item.price is not None),
        accepted_count=len(prices),
        average=sum(prices, Decimal("0")) / len(prices) if prices else None,
        median=Decimal(str(median(prices))) if prices else None,
        minimum=min(prices) if prices else None,
        maximum=max(prices) if prices else None,
        sold_per_month=sold_per_month,
        active_count=active_count,
        sell_through_rate=sell_through,
        estimated_days_to_sell=estimated_days,
        demand=demand,
    )


def deal_score(active_price: Decimal | None, sold_median: Decimal | None) -> Decimal | None:
    if active_price is None or sold_median in {None, Decimal("0")}:
        return None
    discount = (sold_median - active_price) / sold_median
    return max(Decimal("0"), min(Decimal("100"), Decimal("50") + discount * Decimal("100")))
