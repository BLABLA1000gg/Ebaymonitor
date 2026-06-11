"""
Buyback / refurbished-price scrapers for arbitrage profit estimation.

Supported sites
---------------
clevertronic  – Ankauf site (what Clevertronic pays YOU per condition).
                URL: https://www.clevertronic.de/handy_verkaufen/PRODUCT_ID/SLUG

zoxs          – buyback site (Ankauf); shows what ZOXS pays you per condition.
                URL: https://www.zoxs.de/verkaufen/MODEL-ankauf/ASIN.html
                Find the URL at zoxs.de → Handys → Apple → your model.

wirkaufens    – buyback site (Ankauf); shows what WirKaufens pays you per condition.
                URL: https://wirkaufens.de/produkte/PRODUCT-SLUG
                e.g. https://wirkaufens.de/produkte/apple-iphone-12-128-gb

Usage
-----
    from buyback import BuybackScraper
    with BuybackScraper() as scraper:
        ct   = scraper.clevertronic("https://www.clevertronic.de/handy_verkaufen/15097/apple-iphone-12-pro-128gb-graphit-verkaufen")
        zoxs = scraper.zoxs("https://www.zoxs.de/verkaufen/iphone-12-ankauf/B08L5TNKZC.html")
        wkfs = scraper.wirkaufens("https://wirkaufens.de/produkte/apple-iphone-12-128-gb")
        # Each returns {condition_name: Decimal(price)}
"""
from __future__ import annotations

import json
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

LOGGER = logging.getLogger(__name__)

CLEVERTRONIC_CONDITIONS = ("Neu", "Wie neu", "Sehr gut", "Gut", "Akzeptabel", "Gebraucht")
ZOXS_SKIP_CONDITIONS = {"Neu", "Schlecht"}

# ZOXS condition IDs → German display names
ZOXS_CONDITIONS = {
    "1": "Wie neu",
    "2": "Hervorragend",
    "3": "Sehr gut",
    "4": "Gut",
    "65": "Stark gebraucht",
}

# rebuy grade labels (best→worst). A0/Neu is not offered for used Ankauf.
REBUY_CONDITIONS = {
    "A1": "Wie neu",
    "A2": "Sehr gut",
    "A3": "Gut",
    "A4": "Stark genutzt",
}

# WirKaufens condition IDs (best→worst)
WKFS_CONDITIONS = {
    5: "Neu",
    4: "Wie Neu",
    3: "Gut",
    2: "In Ordnung",
    1: "Schlecht",
}


class BuybackScraper:
    """Context manager that shares one Playwright browser across all scrape calls."""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None

    def __enter__(self) -> "BuybackScraper":
        self._pw = sync_playwright().start()
        # headless=False + --headless=new is intentional: Playwright's default
        # headless mode is detected by the portals' bot checks, Chrome's "new"
        # headless (passed as a raw flag) is not. Do not "simplify" this.
        self._browser = self._pw.chromium.launch(
            headless=False,
            args=[
                "--headless=new",
                "--lang=de-DE",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        return self

    def __exit__(self, *args: Any) -> None:
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    # ------------------------------------------------------------------ #
    # Clevertronic – Ankaufpreise (what Clevertronic pays YOU)           #
    # URL: /handy_verkaufen/PRODUCT_ID/SLUG                              #
    # ------------------------------------------------------------------ #

    def clevertronic(self, url: str, functional: bool = True, battery_ok: bool = True) -> dict[str, Decimal]:
        """
        Scrape Clevertronic Ankaufpreise per condition.

        functional / battery_ok control the first wizard question:
          - True  → "Ja – voll funktionsfähig und Akku mindestens bei 81%"
          - False → "Nein – Einschränkungen vorhanden oder Akku unter 81%"

        Returns {condition_label: price} for all available conditions.
        """
        ctx = self._browser.new_context(
            locale="de-DE",
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()
        result: dict[str, Decimal] = {}
        # Device is "good" only when fully functional AND battery OK
        device_good = functional and battery_ok
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2500)

            # Step 1: Dismiss cookie banner / any blocking modal via JS
            page.evaluate("""() => {
                document.querySelectorAll('dialog[open]').forEach(d => d.close());
                document.querySelectorAll('.popup.active').forEach(p => p.remove());
                document.querySelectorAll('[id*="cookie"] button').forEach(b => b.click());
            }""")
            page.wait_for_timeout(400)

            # Step 2: Click the correct functional answer
            # "Ja – voll funktionsfähig" = first button; "Nein – Einschränkungen" = second
            if device_good:
                page.evaluate("document.querySelectorAll('.js_sellbox_button')[0]?.click()")
            else:
                page.evaluate("document.querySelectorAll('.js_sellbox_button')[1]?.click()")
            page.wait_for_timeout(600)

            # Step 3: Click "Weiter" button
            page.evaluate(
                "document.querySelector('.sellbox_button_next')?.click()"
            )
            page.wait_for_timeout(800)

            # Step 4: Find ONLY the main condition buttons (Neu, Wie neu, Sehr gut, Gut, etc.)
            # These are always in the second step of the wizard after clicking "Weiter".
            # Sub-question buttons (e.g. "Ja, das Display...") appear later and are longer.
            _VALID_CONDITIONS = {"neu", "wie neu", "sehr gut", "gut", "gebraucht", "akzeptabel", "beschädigt"}

            cond_labels = page.evaluate("""() => {
                return [...document.querySelectorAll('.js_sellbox_button')]
                    .map(b => ({
                        text: b.textContent.trim().replace(/\\s+/g, ' '),
                        cls: b.className
                    }))
                    .filter(o => o.text.length > 0);
            }""")

            for item in (cond_labels or []):
                raw_label = item.get("text", "") if isinstance(item, dict) else str(item)
                # Strip badge suffixes like "HÄUFIGSTE ANTWORT"
                clean_label = re.sub(r"[A-ZÄÖÜ\s]{6,}$", "", raw_label).strip()
                # Only process known condition labels
                if clean_label.lower() not in _VALID_CONDITIONS:
                    continue

                # Click the condition button
                page.evaluate(
                    """(label) => {
                        const btn = [...document.querySelectorAll('.js_sellbox_button')]
                            .find(b => b.textContent.trim().startsWith(label.substring(0, 6)));
                        btn?.click();
                    }""",
                    clean_label,
                )
                page.wait_for_timeout(600)

                # Read price from span.sell_price
                price_text = page.evaluate("""() => {
                    const el = document.querySelector('span.sell_price');
                    return el ? el.textContent.trim() : null;
                }""")

                if price_text:
                    price = _parse_eur(price_text)
                    if price is not None and price > 0:
                        result[clean_label] = price

        finally:
            ctx.close()
        return result

    # ------------------------------------------------------------------ #
    # ZOXS – Ankaufpreise (what ZOXS pays you, per condition)            #
    # ------------------------------------------------------------------ #

    def zoxs(
        self,
        url: str,
        functional: bool = True,
        battery_ok: bool = True,
        has_box: bool = False,
        has_cable: bool = False,
    ) -> dict[str, Decimal]:
        """
        Scrape ZOXS Ankaufpreise for all conditions of a product.

        The assessment parameters control the bonus question answers that ZOXS
        asks after condition selection — these affect the final price:
          functional  → "Voll funktionstüchtig?" (Ja/Nein)
          battery_ok  → "Akku in Ordnung?"       (Ja/Nein)
          has_box     → "OVP vorhanden?"          (Ja/Nein)
          has_cable   → "Original Kabel?"         (Ja/Nein)

        Returns {condition_name: ankaufpreis} for conditions with price > 0.

        Implementation: no browser needed. ZOXS's product page is static HTML
        that embeds the articleId, all condition ids and all question inputs;
        the price endpoint ``/sys_article_price.php`` accepts a plain JSON
        POST when the request is TLS-impersonated (curl_cffi chrome120) and
        carries the cookies from a prior GET of the product page. One GET +
        one POST per condition replaces the old full-browser flow.
        """
        result: dict[str, Decimal] = {}
        try:
            result = self._zoxs_api(url, functional, battery_ok, has_box, has_cable)
        except Exception:
            LOGGER.warning("ZOXS: API scrape failed for %s — falling back to browser", url, exc_info=True)
        if result:
            return result
        try:
            return self._zoxs_browser(url, functional, battery_ok, has_box, has_cable)
        except Exception:
            LOGGER.warning("ZOXS: browser fallback failed for %s", url, exc_info=True)
            return {}

    def _zoxs_api(
        self,
        url: str,
        functional: bool,
        battery_ok: bool,
        has_box: bool,
        has_cable: bool,
    ) -> dict[str, Decimal]:
        """Direct JSON-API scrape of ZOXS Ankaufpreise (no browser)."""
        from curl_cffi.requests import Session as CurlSession

        ua = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        result: dict[str, Decimal] = {}
        with CurlSession(impersonate="chrome120") as s:
            html = ""
            for attempt in range(2):
                try:
                    r = s.get(
                        url,
                        headers={
                            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                            "Accept-Language": "de-DE,de;q=0.9",
                            "User-Agent": ua,
                        },
                        timeout=20,
                    )
                    if r.status_code == 200 and 'data-articleid="' in r.text:
                        html = r.text
                        break
                except Exception:
                    if attempt == 1:
                        raise
            if not html:
                return result

            m = re.search(r'data-articleid="(\d+)"', html)
            if not m:
                return result
            article_id = int(m.group(1))

            soup = BeautifulSoup(html, "html.parser")

            # Conditions from the static condition selector
            seen_ids: set[str] = set()
            conditions: list[tuple[str, str]] = []
            for el in soup.find_all("div", class_="conditionLabel"):
                cid = el.get("data-conditionid", "")
                name = el.get("data-frontendname", "")
                if cid and cid not in seen_ids and name and name not in ZOXS_SKIP_CONDITIONS:
                    seen_ids.add(cid)
                    conditions.append((cid, name))
            if not conditions:
                return result

            # Build the questions payload exactly like the page's own JS:
            # every radio question defaults to "Ja" (true), selects take an
            # option value. Then inject the caller's assessment answers by
            # matching each question's on-page text.
            questions = _zoxs_build_questions(soup, functional, battery_ok, has_box, has_cable)
            if not questions:
                return result

            headers = {
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "User-Agent": ua,
                "Referer": url,
                "Origin": "https://www.zoxs.de",
            }
            for cid, name in conditions:
                body = json.dumps({
                    "articleId": article_id,
                    "condition": int(cid),
                    "buyback": False,
                    "questions": questions,
                    "zoxsCheck": False,
                })
                data = None
                for attempt in range(2):
                    try:
                        pr = s.post(
                            "https://www.zoxs.de/sys_article_price.php",
                            data=body,
                            headers=headers,
                            timeout=15,
                        )
                        if pr.status_code == 200:
                            data = pr.json()
                            break
                    except Exception:
                        pass
                if isinstance(data, dict):
                    raw = data.get("priceRaw", 0)
                    if raw and raw > 0:
                        result[name] = Decimal(str(raw))
        return result

    def _zoxs_browser(
        self,
        url: str,
        functional: bool = True,
        battery_ok: bool = True,
        has_box: bool = False,
        has_cable: bool = False,
    ) -> dict[str, Decimal]:
        """Playwright fallback for ZOXS (used only if the direct API path fails)."""
        ctx = self._browser.new_context(
            locale="de-DE",
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()
        result: dict[str, Decimal] = {}

        # Intercept one API request to learn articleId + questions
        captured: dict[str, Any] = {}

        def on_request(req: Any) -> None:
            if "sys_article_price.php" in req.url and req.post_data and not captured:
                try:
                    captured.update(json.loads(req.post_data))
                except Exception:
                    pass

        page.on("request", on_request)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)

            # Accept GDPR modal
            try:
                page.locator(".js-gdpr-accept-all").first.click(timeout=6000)
                page.wait_for_timeout(800)
            except Exception:
                pass

            # Get condition labels from the page
            soup = BeautifulSoup(page.content(), "html.parser")
            seen_ids: set[str] = set()
            conditions: list[tuple[str, str]] = []  # (cond_id, display_name)
            for el in soup.find_all("div", class_="conditionLabel"):
                cid = el.get("data-conditionid", "")
                name = el.get("data-frontendname", "")
                if cid and cid not in seen_ids and name and name not in ZOXS_SKIP_CONDITIONS:
                    seen_ids.add(cid)
                    conditions.append((cid, name))

            if not conditions:
                return result

            # Click a condition to trigger one API call → captures articleId +
            # questions. Retry with the second condition if the first click
            # doesn't fire the request (timing / overlay issues).
            for cid, _name in conditions[:2]:
                try:
                    page.locator(f'[id="{cid}ConditionLabel"]:visible').first.click(timeout=6000)
                    page.wait_for_timeout(2200)
                except Exception:
                    continue
                if captured.get("articleId"):
                    break

            if not captured.get("articleId") or not captured.get("questions"):
                return result  # page probably shows "we don't buy this" message

            article_id = captured["articleId"]
            # The captured questions payload is a LIST of dicts:
            #   [{"id": 370, "userSelect": true}, ..., {"id": 378, "userSelect": "891"}]
            # Boolean userSelect entries are the Ja/Nein questions; the page
            # pre-selects the best answers ("Ja"). Non-boolean entries (e.g.
            # storage selects) must be passed through unchanged.
            questions = [dict(q) for q in captured["questions"]]

            # Map question id → meaning from the page DOM so AI-assessed
            # answers can be injected (only relevant when called with
            # functional=False etc. — full-table scrapes keep the defaults).
            q_meta = page.evaluate("""() => {
                return [...document.querySelectorAll('.questionItem, [data-question-id]')]
                    .map(el => ({
                        id: el.dataset.questionId || el.id || '',
                        text: el.textContent.toLowerCase().trim().slice(0, 80)
                    }));
            }""")
            answers: dict[str, bool] = {}
            for qm in (q_meta or []):
                t = qm.get("text", "")
                # DOM ids may be prefixed (e.g. "question-370") — keep digits only
                digits = re.sub(r"\D", "", str(qm.get("id", "")))
                if not digits:
                    continue
                if any(kw in t for kw in ("funktionsfähig", "funktionstüchtig", "working", "funktion")):
                    answers[digits] = functional
                elif any(kw in t for kw in ("akku", "batterie", "battery")):
                    answers[digits] = battery_ok
                elif any(kw in t for kw in ("ovp", "originalverpack", "box", "karton")):
                    answers[digits] = has_box
                elif any(kw in t for kw in ("kabel", "cable", "lightning", "usb")):
                    answers[digits] = has_cable
            injected = 0
            for q in questions:
                qid = str(q.get("id", ""))
                if qid in answers and isinstance(q.get("userSelect"), bool):
                    q["userSelect"] = answers[qid]
                    injected += 1
            if injected == 0 and not (has_box and has_cable):
                # Page defaults answer every bonus question with "Ja" —
                # without injection the quoted prices include box/cable
                # bonuses we don't have, overstating payouts by ~€5-15.
                LOGGER.warning("ZOXS: no assessment answers injected (%d questions) — "
                               "prices may include box/cable bonus", len(questions))

            # Now call sys_article_price.php for every condition via the browser's fetch()
            # (browser carries Cloudflare CF cookies; direct requests would be blocked)
            for cid, name in conditions:
                body = json.dumps({
                    "articleId": article_id,
                    "condition": int(cid),
                    "buyback": False,
                    "questions": questions,
                    "zoxsCheck": False,
                })
                api_result = page.evaluate(
                    """
                    async ([body]) => {
                        try {
                            const r = await fetch('/sys_article_price.php', {
                                method: 'POST',
                                headers: {
                                    'Content-Type': 'application/json',
                                    'X-Requested-With': 'XMLHttpRequest',
                                    'Accept': 'application/json, text/javascript, */*; q=0.01'
                                },
                                body: body,
                                credentials: 'include'
                            });
                            if (!r.ok) return null;
                            return await r.json();
                        } catch(e) {
                            return null;
                        }
                    }
                    """,
                    [body],
                )
                if api_result and isinstance(api_result, dict):
                    raw = api_result.get("priceRaw", 0)
                    if raw and raw > 0:
                        result[name] = Decimal(str(raw))

        finally:
            ctx.close()
        return result


    # ------------------------------------------------------------------ #
    # WirKaufens – Ankaufpreise (what WirKaufens pays you, per condition) #
    # ------------------------------------------------------------------ #

    def wirkaufens(self, url: str, functional: bool = True, battery_ok: bool = True) -> dict[str, Decimal]:
        """
        Scrape WirKaufens Ankaufpreise for all conditions of a product.

        functional / battery_ok map to WirKaufens questions:
          - Question 4 (Gerät einwandfrei benutzbar?): functional → 1 (Ja) or 0 (Nein)
          - Question 2 (Batterie in Ordnung?):          battery_ok → 1 (Ja) or 0 (Nein)

        Returns {condition_label: price} for all conditions.
        """
        ctx = self._browser.new_context(
            locale="de-DE",
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()
        result: dict[str, Decimal] = {}

        # WirKaufens question IDs (derived from UI inspection):
        # Q4 = Gerät lässt sich einwandfrei benutzen?
        # Q2 = Batterie in Ordnung?
        wkfs_questions = {
            "4": 1 if functional else 0,
            "2": 1 if battery_ok else 0,
        }

        # Intercept /trade-in/devices to capture the device ID
        captured: dict[str, Any] = {}

        def on_request(req: Any) -> None:
            if "trade-in/devices" in req.url and req.post_data and not captured:
                try:
                    captured.update(json.loads(req.post_data))
                except Exception:
                    pass

        page.on("request", on_request)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)

            # Accept cookie consent
            page.evaluate("document.getElementById('cookiescript_accept')?.click()")
            page.wait_for_timeout(800)

            # Trigger one /trade-in/devices call to capture the device ID
            # Always click Ja first to capture the device_id (we override questions manually)
            page.evaluate("""
                async () => {
                    const click = el => el?.dispatchEvent(
                        new MouseEvent('click', {bubbles: true, cancelable: true})
                    );
                    document.querySelectorAll('button[id^="answer-1"]').forEach(click);
                    await new Promise(r => setTimeout(r, 400));
                    click(document.querySelector('[data-condition="4"]'));
                }
            """)
            page.wait_for_timeout(2000)

            device_id = captured.get("device")
            # Use our AI-determined question answers, not whatever the page captured
            questions = wkfs_questions

            if not device_id:
                return result

            # Fetch all conditions via the browser's own fetch() (carries session cookies)
            conditions_js = json.dumps({str(k): v for k, v in WKFS_CONDITIONS.items()})
            api_results = page.evaluate(
                """
                async ([deviceId, questions, conditions]) => {
                    const prices = {};
                    for (const [condId, condName] of Object.entries(conditions)) {
                        try {
                            const r = await fetch('/trade-in/devices', {
                                method: 'POST',
                                headers: {
                                    'Content-Type': 'application/json',
                                    'Accept': 'application/json'
                                },
                                body: JSON.stringify({
                                    device: deviceId,
                                    condition: parseInt(condId),
                                    questions: questions,
                                    application: 'shop',
                                    landing_page: 'WKFS_NUXT_DE'
                                }),
                                credentials: 'include'
                            });
                            const data = await r.json();
                            if (data.price > 0) prices[condName] = data.price;
                        } catch (e) { /* skip */ }
                    }
                    return prices;
                }
                """,
                [device_id, questions, json.loads(conditions_js)],
            )

            for name, price_raw in (api_results or {}).items():
                if price_raw:
                    result[name] = Decimal(str(price_raw))

        finally:
            ctx.close()
        return result


    # ------------------------------------------------------------------ #
    # rebuy – Ankaufpreise (what rebuy pays you, per grade A1–A4)         #
    # URL: https://www.rebuy.de/verkaufen/<category>/<slug>_<productId>   #
    # ------------------------------------------------------------------ #

    def rebuy(
        self,
        url: str,
        functional: bool = True,
        battery_ok: bool = True,
        has_box: bool = False,
        has_cable: bool = False,
    ) -> dict[str, Decimal]:
        """
        Scrape rebuy.de Ankaufpreise for all condition grades of a product.

        rebuy's sell page is Angular SSR: the rendered HTML already embeds a
        product JSON blob with ``"variants": [{"label": "A1", "purchasePrice":
        <cents>}, ...]`` — the exact instant Ankaufpreis per grade that the
        6-step wizard would converge to. So no browser is needed; a curl_cffi
        chrome120-impersonated GET is enough (the Playwright browser from the
        context manager is unused here, which is fine).

        Grades: A1 = Wie neu, A2 = Sehr gut, A3 = Gut, A4 = Stark genutzt.

        Assessment parameters:
          functional  → False: rebuy's instant grade table only applies to
                        fully working devices ("Funktioniert ohne
                        Einschränkungen? Ja"); defective devices go through a
                        separate repair-quote flow → return {}.
          battery_ok  → False: a degraded battery disqualifies the top grades
                        in the wizard → drop "Wie neu" and "Sehr gut".
          has_box / has_cable: accepted for signature parity; rebuy pays no
                        accessory bonus, prices are unaffected.

        Returns {condition_label: price} for grades with price > 0.
        """
        result: dict[str, Decimal] = {}
        if not functional:
            return result

        m = re.search(r"_(\d+)$", url.rstrip("/"))
        product_id = m.group(1) if m else None

        try:
            from curl_cffi.requests import Session as CurlSession

            headers = {
                "Accept": "text/html,*/*;q=0.8",
                "Accept-Language": "de-DE,de;q=0.9",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                "Referer": "https://www.rebuy.de/verkaufen",
            }
            with CurlSession(impersonate="chrome120") as s:
                r = s.get(url, headers=headers, timeout=20)
            html = r.text

            # Find the variants array belonging to THIS product (the page can
            # embed cross-sell products too). Variants arrays contain only flat
            # objects, so they never include a ']' character.
            variants: list[dict] | None = None
            for vm in re.finditer(r'"variants":(\[[^\]]*\])', html):
                window = html[max(0, vm.start() - 800): vm.end() + 800]
                if product_id is None or product_id in window:
                    try:
                        candidate = json.loads(vm.group(1))
                    except Exception:
                        continue
                    if candidate and isinstance(candidate, list):
                        variants = candidate
                        break

            if not variants:
                LOGGER.warning("rebuy: no variant price data found at %s", url)
                return result

            for v in variants:
                label = REBUY_CONDITIONS.get(str(v.get("label", "")))
                if not label:
                    continue
                if not battery_ok and label in ("Wie neu", "Sehr gut"):
                    continue
                cents = v.get("purchasePrice") or 0
                if cents and cents > 0:
                    result[label] = Decimal(str(cents)) / Decimal("100")

        except Exception:
            LOGGER.warning("rebuy: scrape failed for %s", url, exc_info=True)
        return result


def _zoxs_build_questions(
    soup: BeautifulSoup,
    functional: bool,
    battery_ok: bool,
    has_box: bool,
    has_cable: bool,
) -> list[dict]:
    """
    Build the ``questions`` payload for sys_article_price.php from the static
    product-page HTML, mirroring the page's own JS: every yes/no radio
    defaults to "Ja" (true), selects take an option value. Assessment answers
    are injected by matching each question's on-page text.
    """
    questions: list[dict] = []
    seen: set[str] = set()

    for el in soup.find_all(["input", "select"]):
        name = el.get("name", "")
        m = re.fullmatch(r"questions\[(\d+)\]", name)
        if not m:
            continue
        qid = m.group(1)
        if qid in seen:
            continue
        seen.add(qid)

        label_el = el.find_previous("span", class_="article-question")
        text = label_el.get_text(" ", strip=True).lower() if label_el else ""

        if el.name == "select":
            options = [o for o in el.find_all("option") if o.get("value")]
            if not options:
                continue
            # Battery-capacity selects: best option when battery is OK,
            # worst otherwise. Other selects (e.g. storage) keep the first
            # (= best/default) option.
            if any(kw in text for kw in ("akku", "batterie", "battery")) and not battery_ok:
                choice = options[-1]
            else:
                choice = options[0]
            questions.append({"id": int(qid), "userSelect": choice.get("value")})
            continue

        # Radio (yes/no) question — default "Ja", override from assessment
        answer = True
        if any(kw in text for kw in ("funktionsfähig", "funktionstüchtig", "working", "funktion")):
            answer = functional
        elif any(kw in text for kw in ("akku", "batterie", "battery")):
            answer = battery_ok
        elif any(kw in text for kw in ("ovp", "originalverpack", "karton")) or "box" in text:
            answer = has_box
        elif any(kw in text for kw in ("kabel", "cable", "lightning", "usb")):
            answer = has_cable
        questions.append({"id": int(qid), "userSelect": answer})

    return questions


def _parse_eur(text: str) -> Decimal | None:
    """Parse a German-format EUR string like '259,99 €' → Decimal('259.99')."""
    text = text.replace("\xa0", " ")
    m = re.search(r"(\d[\d.]*[.,]\d{2})", text)
    if not m:
        return None
    raw = m.group(1).replace(".", "").replace(",", ".")
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None
