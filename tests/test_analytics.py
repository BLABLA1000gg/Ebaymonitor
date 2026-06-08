import unittest
from decimal import Decimal

from analytics import deal_score, market_metrics, robust_prices
from models import Listing


def item(price):
    return Listing("item", f"/{price}", f"EUR {price}", Decimal(str(price)), "EUR")


class RobustPriceTests(unittest.TestCase):
    def test_removes_low_accessory_and_high_broken_distribution_outliers(self):
        prices = robust_prices([item(value) for value in [20, 100, 110, 120, 130, 5000]])
        self.assertEqual(prices, [Decimal("100"), Decimal("110"), Decimal("120"), Decimal("130")])

    def test_keeps_small_samples(self):
        self.assertEqual(robust_prices([item(100), item(200), item(300)]), [Decimal("100"), Decimal("200"), Decimal("300")])


class MarketMetricsTests(unittest.TestCase):
    def test_calculates_monthly_sales_sell_through_and_days(self):
        metrics = market_metrics([item(100), item(120), item(140)], active_count=2, sold_window_days=90)
        self.assertEqual(metrics.sold_per_month, Decimal("1"))
        self.assertEqual(metrics.sell_through_rate, Decimal("0.5"))
        self.assertEqual(metrics.estimated_days_to_sell, Decimal("60"))
        self.assertEqual(metrics.demand, "medium")
        self.assertEqual(metrics.median, Decimal("120"))

    def test_deal_score_rewards_prices_below_sold_median(self):
        self.assertEqual(deal_score(Decimal("75"), Decimal("100")), Decimal("75.00"))
        self.assertEqual(deal_score(Decimal("125"), Decimal("100")), Decimal("25.00"))
        self.assertIsNone(deal_score(None, Decimal("100")))


if __name__ == "__main__":
    unittest.main()
