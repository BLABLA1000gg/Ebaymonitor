import unittest

from monitor import Listing, find_new_listings, parse_listings, validate_url


class ParseListingsTests(unittest.TestCase):
    def test_parses_complete_listing_and_skips_incomplete_entries(self):
        html = """
        <ul>
          <li class="s-item">
            <a class="s-item__link" href="https://www.ebay.de/itm/123"></a>
            <div class="s-item__title">Example laptop</div>
            <span class="s-item__price">EUR 99.00</span>
            <img class="s-item__image-img" data-src="https://example.com/image.jpg">
          </li>
          <li class="s-item"><div class="s-item__title">Missing fields</div></li>
        </ul>
        """

        self.assertEqual(
            parse_listings(html),
            [
                Listing(
                    title="Example laptop",
                    link="https://www.ebay.de/itm/123",
                    price="EUR 99.00",
                    image_url="https://example.com/image.jpg",
                )
            ],
        )

    def test_skips_ebay_placeholder_listing(self):
        html = """
        <li class="s-item">
          <a class="s-item__link" href="https://www.ebay.de/"></a>
          <div class="s-item__title">Shop on eBay</div>
          <span class="s-item__price">EUR 0.00</span>
        </li>
        """

        self.assertEqual(parse_listings(html), [])


class ChangeDetectionTests(unittest.TestCase):
    def test_returns_only_unseen_links(self):
        old = Listing("Old", "https://www.ebay.de/itm/1", "EUR 1")
        new = Listing("New", "https://www.ebay.de/itm/2", "EUR 2")

        self.assertEqual(find_new_listings([old, new], {old.link}), [new])


class UrlValidationTests(unittest.TestCase):
    def test_accepts_supported_subdomain(self):
        url = "https://www.ebay.de/sch/i.html?_nkw=laptop"
        self.assertEqual(validate_url("EBAY_URL", url, ("ebay.de",)), url)

    def test_rejects_insecure_or_unrelated_url(self):
        with self.assertRaises(ValueError):
            validate_url("EBAY_URL", "http://example.com", ("ebay.de",))


if __name__ == "__main__":
    unittest.main()
