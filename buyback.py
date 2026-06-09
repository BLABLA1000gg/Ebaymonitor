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
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

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

            # Click the first condition to trigger one API call → captures articleId + questions
            first_cid, _ = conditions[0]
            try:
                page.locator(f'[id="{first_cid}ConditionLabel"]:visible').first.click(timeout=6000)
                page.wait_for_timeout(1800)
            except Exception:
                pass

            if not captured.get("articleId") or not captured.get("questions"):
                return result  # page probably shows "we don't buy this" message

            article_id = captured["articleId"]
            # Start with captured questions (device-specific), then override
            # with our AI-assessed answers where we know the question IDs.
            questions = dict(captured["questions"])
            # Inject AI-assessed answers into the questions dict.
            # ZOXS question IDs (common for smartphones — may vary by product):
            # We update keys we know about; unknown keys keep their captured value.
            _Q_FUNCTIONAL = None  # discovered dynamically from page
            _Q_BATTERY    = None
            _Q_BOX        = None
            _Q_CABLE      = None

            # Read question metadata from the page to map ID → meaning
            q_meta = page.evaluate("""() => {
                return [...document.querySelectorAll('.questionItem, [data-question-id]')]
                    .map(el => ({
                        id: el.dataset.questionId || el.id || '',
                        text: el.textContent.toLowerCase().trim().slice(0, 80)
                    }));
            }""")
            for qm in (q_meta or []):
                t = qm.get("text", "")
                qid = str(qm.get("id", "")).strip()
                if not qid:
                    continue
                if any(kw in t for kw in ("funktionsfähig", "funktionstüchtig", "working", "funktion")):
                    _Q_FUNCTIONAL = qid
                elif any(kw in t for kw in ("akku", "batterie", "battery")):
                    _Q_BATTERY = qid
                elif any(kw in t for kw in ("ovp", "originalverpack", "box", "karton")):
                    _Q_BOX = qid
                elif any(kw in t for kw in ("kabel", "cable", "lightning", "usb")):
                    _Q_CABLE = qid

            # Apply AI-determined answers (1=Ja, 0=Nein)
            if _Q_FUNCTIONAL and _Q_FUNCTIONAL in questions:
                questions[_Q_FUNCTIONAL] = 1 if functional else 0
            if _Q_BATTERY and _Q_BATTERY in questions:
                questions[_Q_BATTERY] = 1 if battery_ok else 0
            if _Q_BOX and _Q_BOX in questions:
                questions[_Q_BOX] = 1 if has_box else 0
            if _Q_CABLE and _Q_CABLE in questions:
                questions[_Q_CABLE] = 1 if has_cable else 0

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
