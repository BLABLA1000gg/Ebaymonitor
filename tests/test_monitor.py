import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from filters import ListingFilter, parse_price
from models import EventType, Listing
from monitor import parse_listings, validate_url
from storage import MonitorStore


def listing(link="https://www.ebay.de/itm/1", price="100", title="MacBook Pro"):
    return Listing(
        title=title,
        link=link,
        price_text=f"EUR {price}",
        price=Decimal(price),
        currency="EUR",
        condition="Used",
        location="Berlin",
    )


class PriceParsingTests(unittest.TestCase):
    def test_parses_german_and_us_price_formats(self):
        self.assertEqual(parse_price("EUR 1.299,99"), (Decimal("1299.99"), "EUR"))
        self.assertEqual(parse_price("US $1,299.99"), (Decimal("1299.99"), "USD"))

    def test_returns_none_for_unparseable_price(self):
        self.assertEqual(parse_price("Price unavailable"), (None, None))


class ParseListingsTests(unittest.TestCase):
    def test_parses_optional_listing_metadata(self):
        html = """
        <li class="s-item">
          <a class="s-item__link" href="https://www.ebay.de/itm/123"></a>
          <div class="s-item__title">MacBook Pro M1</div>
          <span class="s-item__price">EUR 799,99</span>
          <span class="SECONDARY_INFO">Gebraucht</span>
          <span class="s-item__shipping">EUR 5 Versand</span>
          <span class="s-item__location">Berlin</span>
          <img class="s-item__image-img" data-src="https://example.com/image.jpg">
        </li>
        """
        result = parse_listings(html)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].price, Decimal("799.99"))
        self.assertEqual(result[0].condition, "Gebraucht")
        self.assertEqual(result[0].shipping, "EUR 5 Versand")

    def test_skips_incomplete_and_placeholder_entries(self):
        html = """
        <li class="s-item"><div class="s-item__title">Missing fields</div></li>
        <li class="s-item">
          <a class="s-item__link" href="https://www.ebay.de/"></a>
          <div class="s-item__title">Shop on eBay</div>
          <span class="s-item__price">EUR 0.00</span>
        </li>
        """
        self.assertEqual(parse_listings(html), [])


class ListingFilterTests(unittest.TestCase):
    def test_applies_keywords_price_and_currency(self):
        configured = ListingFilter(
            include_keywords=("macbook", "pro"),
            exclude_keywords=("defekt",),
            min_price=Decimal("200"),
            max_price=Decimal("900"),
            currency="EUR",
        )
        self.assertTrue(configured.matches(listing(price="799")))
        self.assertFalse(configured.matches(listing(price="999")))
        self.assertFalse(configured.matches(listing(title="MacBook Pro defekt")))


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = MonitorStore(Path(self.directory.name) / "monitor.db")

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_detects_new_listing_and_price_changes(self):
        self.assertEqual(self.store.record_scan([listing()])[0].type, EventType.NEW)
        drop = self.store.record_scan([listing(price="80")])[0]
        self.assertEqual(drop.type, EventType.PRICE_DROP)
        self.assertEqual(drop.previous_price, Decimal("100"))
        increase = self.store.record_scan([listing(price="90")])[0]
        self.assertEqual(increase.type, EventType.PRICE_INCREASE)
        count = self.store.connection.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
        self.assertEqual(count, 3)

    def test_records_average_median_and_range_per_search(self):
        stats = self.store.record_search_statistics(
            "https://www.ebay.de/sch/i.html?_nkw=macbook",
            "macbook,pro",
            [listing(price="100"), listing(link="/2", price="200"), listing(link="/3", price="600")],
        )
        self.assertEqual(stats["average_price"], Decimal("300"))
        self.assertEqual(stats["median_price"], Decimal("200"))
        self.assertEqual(stats["minimum_price"], Decimal("100"))
        self.assertEqual(stats["maximum_price"], Decimal("600"))

    def test_exports_all_csv_files(self):
        self.store.record_scan([listing()])
        self.store.record_search_statistics("https://www.ebay.de/search", "macbook", [listing()])
        paths = self.store.export_csv(Path(self.directory.name) / "exports")
        self.assertEqual({path.name for path in paths}, {
            "listings.csv", "price_history.csv", "search_statistics.csv"
        })
        self.assertTrue(all(path.exists() for path in paths))


class UrlValidationTests(unittest.TestCase):
    def test_accepts_supported_subdomain(self):
        url = "https://www.ebay.de/sch/i.html?_nkw=laptop"
        self.assertEqual(validate_url("EBAY_URL", url, ("ebay.de",)), url)

    def test_rejects_insecure_or_unrelated_url(self):
        with self.assertRaises(ValueError):
            validate_url("EBAY_URL", "http://example.com", ("ebay.de",))


if __name__ == "__main__":
    unittest.main()
