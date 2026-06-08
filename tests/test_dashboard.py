import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from dashboard import create_app
from profiles import SearchProfile
from storage import MonitorStore


class ProfileStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.db"

    def tearDown(self):
        self.temp.cleanup()

    def test_profile_round_trip(self):
        with MonitorStore(self.path) as store:
            profile_id = store.save_profile(SearchProfile(
                None, "MacBooks", "https://www.ebay.de/sch/i.html?_nkw=macbook",
                "macbook,pro", "defekt,zubehoer", Decimal("200"),
                Decimal("900"), "EUR", 90, True,
            ))
            loaded = store.profile(profile_id)
            self.assertEqual(loaded.name, "MacBooks")
            self.assertEqual(loaded.min_price, Decimal("200"))
            self.assertTrue(loaded.enabled)

    def test_dashboard_and_profile_form_routes(self):
        app = create_app(self.path)
        app.config["TESTING"] = True
        client = app.test_client()
        self.assertEqual(client.get("/").status_code, 200)
        response = client.post("/profiles/new", data={
            "name": "ThinkPads",
            "ebay_url": "https://www.ebay.de/sch/i.html?_nkw=thinkpad",
            "include_keywords": "thinkpad",
            "exclude_keywords": "defekt",
            "min_price": "100",
            "max_price": "600",
            "currency": "EUR",
            "sold_window_days": "90",
            "enabled": "on",
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn(b"ThinkPads", client.get("/").data)


if __name__ == "__main__":
    unittest.main()
