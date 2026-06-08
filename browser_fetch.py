from dataclasses import dataclass

import requests
from playwright.sync_api import Browser, Playwright, sync_playwright


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
        launch_options = {"headless": True}
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
            viewport={"width": 1365, "height": 768},
        )
        page = context.new_page()
        try:
            response = page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=timeout * 1000,
            )
            if response is None:
                raise requests.RequestException(
                    f"Browser navigation returned no response for {url}"
                )
            return BrowserPage(page.content(), response.status)
        except requests.RequestException:
            raise
        except Exception as error:
            raise requests.RequestException(
                f"Browser request failed for {url}: {error}"
            ) from error
        finally:
            context.close()
