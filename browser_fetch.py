from __future__ import annotations
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
from playwright.sync_api import Browser, Playwright, sync_playwright

try:
    from playwright_stealth import Stealth
    _STEALTH_AVAILABLE = True
except ImportError:
    _STEALTH_AVAILABLE = False


@dataclass(frozen=True)
class BrowserPage:
    text: str
    status_code: int


class BrowserFetcher:
    def __init__(self, proxy_url: str | None = None):
        self.proxy_url = proxy_url
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    def __enter__(self):
        self._playwright = sync_playwright().start()
        # Non-headless Chromium bypasses eBay's Akamai JS bot-detection challenge.
        # headless=True (old or new mode) is detectable; a visible browser is not.
        launch_options: dict = {
            "headless": False,
            "args": [
                "--lang=de-DE",
                "--disable-blink-features=AutomationControlled",
            ],
        }
        if self.proxy_url:
            launch_options["proxy"] = {"server": self.proxy_url}
        self._browser = self._playwright.chromium.launch(**launch_options)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()

    def get(self, url: str, timeout: int) -> BrowserPage:
        if not self._browser:
            raise RuntimeError("BrowserFetcher must be used as a context manager")
        context = self._browser.new_context(
            locale="de-DE",
            timezone_id="Europe/Berlin",
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        if _STEALTH_AVAILABLE:
            Stealth(
                navigator_languages_override=("de-DE", "de"),
                navigator_platform_override="MacIntel",
            ).apply_stealth_sync(context)
        page = context.new_page()
        try:
            # Seed the session with the site homepage first so that eBay's bot
            # detection sees a natural navigation flow and grants session cookies.
            parsed = urlparse(url)
            homepage = f"{parsed.scheme}://{parsed.netloc}/"
            if homepage != url and homepage != url.rstrip("/") + "/":
                page.goto(homepage, wait_until="domcontentloaded", timeout=timeout * 1000)

            response = page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=timeout * 1000,
            )
            if response is None:
                raise requests.RequestException(
                    f"Browser navigation returned no response for {url}"
                )
            # Wait for listing cards to appear in the DOM (eBay renders them via JS).
            # Try eBay's card selector first; fall back to a fixed timeout so the
            # fetcher also works on Kleinanzeigen / Vinted pages.
            try:
                page.wait_for_selector("li.s-card, article.aditem, .new-item-box__container", timeout=12000)
            except Exception:
                page.wait_for_timeout(5000)
            return BrowserPage(page.content(), response.status)
        except requests.RequestException:
            raise
        except Exception as error:
            raise requests.RequestException(
                f"Browser request failed for {url}: {error}"
            ) from error
        finally:
            context.close()
