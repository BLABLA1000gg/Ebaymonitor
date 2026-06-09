"""
Condition detection for marketplace listings.

Flow:
  1. Regex scan of title + description → normalized condition score (0-5)
  2. Map score → correct key in buyback price dicts
  3. Optional: image check via NVIDIA vision API (only when ROI is promising)
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

# ------------------------------------------------------------------ #
# Accessory / non-device filter                                        #
# ------------------------------------------------------------------ #

# Keywords that indicate it's NOT the device itself
_ACCESSORY_KEYWORDS = [
    # Cases & covers
    "hülle", "huelle", "case", "cover", "schutzhülle", "schutzhuelle",
    "bumper", "handyhülle", "handyhuelle", "wallet case", "flip case",
    "book case", "lederhülle", "silikonhülle", "tpu hülle",
    # Screen protection
    "schutzglas", "panzerglas", "folie", "screen protector", "displayschutz",
    "schutzfolie", "glasfolie",
    # Cables & chargers
    "kabel", "ladekabel", "ladegerät", "charger", "netzteil", "usb",
    "lightning kabel", "magsafe", "ladestation", "wireless charger",
    "powerbank", "power bank",
    # Accessories
    "halter", "halterung", "ständer", "stand", "mount", "autohalter",
    "kopfhörer", "kopfhoerer", "airpods", "earpods", "earbuds",
    "adapter", "dongle", "hub",
    # Spare parts / repairs
    "ersatzteil", "display ersatz", "akku ersatz", "reparatur",
    "gehäuse", "gehaeuse", "backcover", "back cover", "rückseite",
    "flex kabel", "lautsprecher ersatz",
    # Bundles with ambiguous pricing
    "zubehör", "zubehoer", "bundle",
    # Smartwatch / tablet confusion
    "apple watch", "ipad", "airpods", "homepod",
]

# Must contain at least one of these to be a real phone listing
# (empty = no positive requirement, just the blocklist above)
_DEVICE_REQUIRED_ANY: list[str] = []


def is_actual_device(title: str, description: str = "") -> bool:
    """
    Fast regex pre-filter: False if obvious accessory keyword found.
    Use ai_is_device() for accurate LLM-based classification.
    """
    t = (title + " " + description[:300]).lower()
    for kw in _ACCESSORY_KEYWORDS:
        if kw in t:
            return False
    return True


_device_cache: dict[str, bool] = {}
_cond_cache: dict[str, int] = {}


def ai_is_device(
    title: str,
    description: str = "",
    api_key: str = "",
    provider: str = "none",
) -> bool:
    """
    Ask the LLM whether this listing is an actual smartphone/tablet,
    not an accessory, case, cable, spare part, etc.

    Falls back to regex filter when no API key is set.
    Results are cached per title prefix.
    """
    # Fast path: regex catches obvious cases without wasting tokens
    if not is_actual_device(title, description):
        return False

    if not api_key or provider == "none":
        return True  # regex said OK, no LLM available → trust it

    cache_key = (title.strip().lower())[:80]
    if cache_key in _device_cache:
        return _device_cache[cache_key]

    prompt = (
        "Is this marketplace listing selling an actual smartphone or tablet device "
        "(not a case, cover, charger, cable, screen protector, spare part, accessory, or bundle)?\n"
        f"Title: {title}\n"
        + (f"Description: {description[:300]}" if description else "")
        + "\nReply with exactly one word: YES or NO."
    )

    answer = None
    try:
        if provider == "nvidia":
            from openai import OpenAI
            client = OpenAI(base_url="https://integrate.api.nvidia.com/v1", api_key=api_key)
            resp = client.chat.completions.create(
                model="meta/llama-3.1-8b-instruct",
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=5,
                stream=False,
            )
            answer = resp.choices[0].message.content.strip().upper()
        elif provider == "deepseek":
            import requests as _req
            r = _req.post(
                "https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": "deepseek-chat",
                      "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 5, "temperature": 0},
                timeout=8,
            )
            answer = r.json()["choices"][0]["message"]["content"].strip().upper()
    except Exception:
        pass

    result = answer.startswith("YES") if answer else True
    _device_cache[cache_key] = result
    return result


# ------------------------------------------------------------------ #
# AI-based condition detection                                         #
# ------------------------------------------------------------------ #

_COND_SYSTEM = (
    "You are a condition grader for second-hand phones. "
    "Rate the condition based on title and description. "
    "Use this scale:\n"
    "0 = Defekt (broken, cracked display, water damage, not working)\n"
    "1 = Akzeptabel (heavy scratches, dents, strong wear)\n"
    "2 = Gut (normal wear, visible scratches, used)\n"
    "3 = Sehr gut (light scratches, well maintained, little use)\n"
    "4 = Wie neu (barely used, almost flawless, no visible marks)\n"
    "5 = Neu (brand new, sealed, original packaging, OVP)\n"
    "Reply with ONLY a single digit 0-5. Nothing else."
)


def ai_detect_condition(
    title: str,
    description: str = "",
    api_key: str = "",
    provider: str = "none",
) -> int | None:
    """
    Ask the LLM to rate the listing condition (0-5).
    Returns None when no API key is set or on error.
    Result is cached per title.
    """
    if not api_key or provider == "none":
        return None

    cache_key = title.strip().lower()[:100]
    if cache_key in _cond_cache:
        return _cond_cache[cache_key]

    user_msg = (
        f"Title: {title}\n"
        + (f"Description: {description[:600]}" if description else "")
        + "\nCondition (0-5):"
    )

    score = _call_cond_llm(user_msg, api_key, provider)
    if score is not None:
        _cond_cache[cache_key] = score
    return score


def ai_detect_condition_batch(
    listings: list[tuple[str, str]],  # [(title, description), ...]
    api_key: str = "",
    provider: str = "none",
) -> list[int | None]:
    """
    Rate condition for multiple listings in ONE API call to save tokens.
    Returns a list of scores (0-5) or None per listing.
    Cached results are reused without hitting the API.
    """
    if not api_key or provider == "none":
        return [None] * len(listings)

    results: list[int | None] = [None] * len(listings)
    uncached_indices: list[int] = []

    # Fill from cache first
    for i, (title, _) in enumerate(listings):
        key = title.strip().lower()[:100]
        if key in _cond_cache:
            results[i] = _cond_cache[key]
        else:
            uncached_indices.append(i)

    if not uncached_indices:
        return results

    # Build a numbered batch prompt
    lines = []
    for idx, i in enumerate(uncached_indices):
        title, desc = listings[i]
        lines.append(
            f"[{idx + 1}] Title: {title}"
            + (f" | Desc: {desc[:300]}" if desc else "")
        )

    user_msg = (
        "Rate each listing's condition (0-5). "
        "Reply with ONLY a JSON array of integers matching the listing numbers, "
        "e.g. [3,2,4,1]. No text.\n\n"
        + "\n".join(lines)
    )

    import json as _json
    raw = _call_cond_llm(user_msg, api_key, provider, expect_json=True)

    if isinstance(raw, list):
        for j, score in enumerate(raw):
            if j >= len(uncached_indices):
                break
            try:
                s = int(score)
                if 0 <= s <= 5:
                    i = uncached_indices[j]
                    results[i] = s
                    _cond_cache[listings[i][0].strip().lower()[:100]] = s
            except (TypeError, ValueError):
                pass

    return results


def ai_assess_listing_batch(
    listings: list[tuple[str, str]],  # [(title, description), ...]
    api_key: str = "",
    provider: str = "none",
) -> list[dict]:
    """
    Full listing assessment in ONE API call per batch.

    Returns per listing:
        {
            "condition": int,        # 0-5 normalized condition score
            "functional": bool,      # Device is fully working (no defects, SIM-unlocked)
            "battery_ok": bool,      # Battery >= 81% health
            "has_box": bool,         # Original packaging included
            "has_cable": bool,       # Original cable included
        }
    Cached per title. Falls back to defaults on error.
    """
    import json as _json

    default = {"condition": COND_GOOD, "functional": True, "battery_ok": True,
               "has_box": False, "has_cable": False}

    if not api_key or provider == "none":
        return [default.copy() for _ in listings]

    results: list[dict | None] = [None] * len(listings)
    uncached: list[int] = []

    for i, (title, _) in enumerate(listings):
        key = title.strip().lower()[:100]
        if key in _cond_cache:
            # Rebuild full result from cached condition; other fields not cached → use defaults
            d = default.copy()
            d["condition"] = _cond_cache[key]
            results[i] = d
        else:
            uncached.append(i)

    if not uncached:
        return [r or default.copy() for r in results]

    # Build numbered batch prompt
    lines = []
    for j, i in enumerate(uncached):
        title, desc = listings[i]
        lines.append(
            f"[{j+1}] Title: {title}"
            + (f" | Desc: {desc[:400]}" if desc else "")
        )

    system_prompt = (
        "You are a second-hand phone listing analyzer. "
        "For each numbered listing, output a JSON object with these keys:\n"
        "  condition: integer 0-5 "
        "(0=defekt/broken, 1=stark gebraucht/heavy wear, 2=gut/normal use, "
        "3=sehr gut/light scratches, 4=wie neu/barely used, 5=neu/sealed)\n"
        "  functional: true if device works fully, false if broken/defective/needs repair\n"
        "  battery_ok: true if battery >=81% or not mentioned, false if explicitly <81%\n"
        "  has_box: true if original box/OVP included\n"
        "  has_cable: true if original cable/charger included\n"
        "Output ONLY a JSON array of objects, one per listing, in order. No text."
    )

    user_msg = "\n".join(lines)

    try:
        if provider == "nvidia":
            from openai import OpenAI
            client = OpenAI(base_url="https://integrate.api.nvidia.com/v1", api_key=api_key)
            resp = client.chat.completions.create(
                model="meta/llama-3.1-8b-instruct",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0,
                max_tokens=60 * len(uncached) + 20,
                stream=False,
            )
            text = resp.choices[0].message.content.strip()
        elif provider == "deepseek":
            import requests as _req
            r = _req.post(
                "https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": "deepseek-chat",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_msg},
                    ],
                    "max_tokens": 60 * len(uncached) + 20,
                    "temperature": 0,
                },
                timeout=15,
            )
            text = r.json()["choices"][0]["message"]["content"].strip()
        else:
            text = ""

        # Parse JSON array from response
        m = re.search(r"\[.*\]", text, re.DOTALL)
        parsed = _json.loads(m.group()) if m else []

        for j, raw in enumerate(parsed):
            if j >= len(uncached):
                break
            i = uncached[j]
            title = listings[i][0]
            d = {
                "condition":   max(0, min(5, int(raw.get("condition", COND_GOOD)))),
                "functional":  bool(raw.get("functional", True)),
                "battery_ok":  bool(raw.get("battery_ok", True)),
                "has_box":     bool(raw.get("has_box", False)),
                "has_cable":   bool(raw.get("has_cable", False)),
            }
            results[i] = d
            _cond_cache[title.strip().lower()[:100]] = d["condition"]

    except Exception:
        pass

    return [r if r is not None else default.copy() for r in results]


def _call_cond_llm(
    user_msg: str,
    api_key: str,
    provider: str,
    expect_json: bool = False,
):
    """Internal: call the configured LLM and return parsed score or list."""
    import json as _json

    try:
        if provider == "nvidia":
            from openai import OpenAI
            client = OpenAI(base_url="https://integrate.api.nvidia.com/v1", api_key=api_key)
            resp = client.chat.completions.create(
                model="meta/llama-3.1-8b-instruct",
                messages=[
                    {"role": "system", "content": _COND_SYSTEM},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0,
                max_tokens=40 if expect_json else 5,
                stream=False,
            )
            text = resp.choices[0].message.content.strip()

        elif provider == "deepseek":
            import requests as _req
            r = _req.post(
                "https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": "deepseek-chat",
                    "messages": [
                        {"role": "system", "content": _COND_SYSTEM},
                        {"role": "user", "content": user_msg},
                    ],
                    "max_tokens": 40 if expect_json else 5,
                    "temperature": 0,
                },
                timeout=10,
            )
            text = r.json()["choices"][0]["message"]["content"].strip()
        else:
            return None

        if expect_json:
            # Extract JSON array from response
            m = re.search(r"\[[\d,\s]+\]", text)
            if m:
                return _json.loads(m.group())
            return None
        else:
            m = re.search(r"[0-5]", text)
            return int(m.group()) if m else None

    except Exception:
        return None


# ------------------------------------------------------------------ #
# Normalized condition scale                                           #
# ------------------------------------------------------------------ #
COND_BROKEN     = 0
COND_ACCEPTABLE = 1
COND_GOOD       = 2
COND_VERY_GOOD  = 3
COND_LIKE_NEW   = 4
COND_NEW        = 5

COND_LABELS = {
    COND_BROKEN:     "Defekt",
    COND_ACCEPTABLE: "Akzeptabel",
    COND_GOOD:       "Gut",
    COND_VERY_GOOD:  "Sehr gut",
    COND_LIKE_NEW:   "Wie neu",
    COND_NEW:        "Neu",
}

# ------------------------------------------------------------------ #
# Keyword patterns per condition (longest/most-specific first)        #
# ------------------------------------------------------------------ #
_PATTERNS: list[tuple[int, list[str]]] = [
    (COND_BROKEN, [
        "defekt", "kaputt", "gebrochen", "bruch", "riss", "display kaputt",
        "display gebrochen", "broken", "fault", "repair",
    ]),
    (COND_NEW, [
        "originalverpackt", "versiegelt", "sealed", "brand new", "fabrikneu",
        "ungeöffnet", "ovp", "neu & ovp",
    ]),
    (COND_LIKE_NEW, [
        "wie neu", "neuwertig", "top zustand", "mint condition", "1a zustand",
        "unbenutzt", "kaum benutzt", "so gut wie neu", "makellos",
    ]),
    (COND_VERY_GOOD, [
        "sehr gut", "very good", "gepflegt", "kaum genutzt", "sehr guter zustand",
        "wenig benutzt", "hervorragend",
    ]),
    (COND_GOOD, [
        "guter zustand", "gut erhalten", "normale gebrauchsspuren", "leichte kratzer",
        "good condition", "gut",
    ]),
    (COND_ACCEPTABLE, [
        "akzeptabel", "acceptable", "sichtbare gebrauchsspuren", "stark gebraucht",
        "kratzer", "dellen", "used", "gebraucht",
    ]),
]

# eBay platform condition labels → normalized score
_EBAY_PLATFORM_COND: dict[str, int] = {
    "neu":                         COND_NEW,
    "gebraucht – wie neu":         COND_LIKE_NEW,
    "gebraucht – sehr gut":        COND_VERY_GOOD,
    "gebraucht – gut":             COND_GOOD,
    "gebraucht – akzeptabel":      COND_ACCEPTABLE,
    "for parts or not working":    COND_BROKEN,
    "defekt":                      COND_BROKEN,
    # English
    "new":                         COND_NEW,
    "used – like new":             COND_LIKE_NEW,
    "used – very good":            COND_VERY_GOOD,
    "used – good":                 COND_GOOD,
    "used – acceptable":           COND_ACCEPTABLE,
}

# ------------------------------------------------------------------ #
# Buyback portal → condition key mapping                              #
# ------------------------------------------------------------------ #
# ZOXS: {condition_key: "Label"} from buyback.py ZOXS_CONDITIONS
# normalized score → ZOXS key (string)
_ZOXS_MAP: dict[int, str] = {
    COND_NEW:        "1",   # Wie neu
    COND_LIKE_NEW:   "1",   # Wie neu
    COND_VERY_GOOD:  "3",   # Sehr gut
    COND_GOOD:       "4",   # Gut
    COND_ACCEPTABLE: "65",  # Stark gebraucht
    COND_BROKEN:     "65",  # best we can do
}

# WirKaufens: {condition_key: "Label"} from buyback.py WKFS_CONDITIONS
# normalized score → WKFS key (string)
_WKFS_MAP: dict[int, str] = {
    COND_NEW:        "4",   # Wie Neu (5=Neu rarely offered)
    COND_LIKE_NEW:   "4",   # Wie Neu
    COND_VERY_GOOD:  "3",   # Gut
    COND_GOOD:       "2",   # In Ordnung
    COND_ACCEPTABLE: "1",   # Schlecht
    COND_BROKEN:     "1",
}

# Clevertronic condition labels in their price dict (first 4 chars match)
# We'll do prefix matching on keys
_CLEVERTRONIC_PREF: dict[int, str] = {
    COND_NEW:        "Neu",
    COND_LIKE_NEW:   "Wie",   # "Wie Neu"
    COND_VERY_GOOD:  "Sehr",  # "Sehr gut"
    COND_GOOD:       "Gut",
    COND_ACCEPTABLE: "Akze",  # "Akzeptabel"
    COND_BROKEN:     "Akze",
}


# ------------------------------------------------------------------ #
# Public API                                                           #
# ------------------------------------------------------------------ #

def detect_condition(
    title: str,
    description: str = "",
    platform_condition: str | None = None,
    api_key: str = "",
    provider: str = "none",
) -> int:
    """
    Return normalized condition score (0-5).

    Priority:
      1. eBay platform condition field (standardized, most reliable)
      2. AI analysis of title + description (when api_key is set)
      3. Regex keyword scan (fast fallback)
    """
    # 1) eBay/platform native condition field
    if platform_condition:
        key = platform_condition.strip().lower()
        if key in _EBAY_PLATFORM_COND:
            return _EBAY_PLATFORM_COND[key]

    # 2) AI detection — reads the actual listing text
    if api_key and provider != "none":
        ai_score = ai_detect_condition(title, description, api_key=api_key, provider=provider)
        if ai_score is not None:
            return ai_score

    # 3) Regex fallback
    text = f"{title} {title} {description[:400]}".lower()
    best_score: int | None = None

    for score, keywords in _PATTERNS:
        for kw in keywords:
            if kw in text:
                if best_score is None:
                    best_score = score
                break

    return best_score if best_score is not None else COND_GOOD


def matched_buyback_price(
    condition_score: int,
    zoxs_prices: dict | None,
    wkfs_prices: dict | None,
    clevertronic_prices: dict | None,
) -> dict[str, Decimal | None]:
    """
    Given a condition score and all-conditions price dicts, return the
    single condition-matched price per platform.
    Returns {'zoxs': Decimal|None, 'wirkaufens': Decimal|None, 'clevertronic': Decimal|None}
    """
    result: dict[str, Decimal | None] = {
        "zoxs": None,
        "wirkaufens": None,
        "clevertronic": None,
    }

    if zoxs_prices:
        key = _ZOXS_MAP.get(condition_score, "4")
        # Keys in stored dict are the condition LABELS (e.g. "Sehr gut": "150.00")
        # Try by label that matches the key
        zoxs_label_map = {
            "1": "Wie neu", "2": "Hervorragend", "3": "Sehr gut",
            "4": "Gut", "65": "Stark gebraucht",
        }
        label = zoxs_label_map.get(key, "Gut")
        val = zoxs_prices.get(label)
        if val:
            try:
                result["zoxs"] = Decimal(str(val))
            except Exception:
                pass

    if wkfs_prices:
        key = _WKFS_MAP.get(condition_score, "2")
        wkfs_label_map = {
            "5": "Neu", "4": "Wie Neu", "3": "Gut", "2": "In Ordnung", "1": "Schlecht",
        }
        label = wkfs_label_map.get(key, "In Ordnung")
        val = wkfs_prices.get(label)
        if val:
            try:
                result["wirkaufens"] = Decimal(str(val))
            except Exception:
                pass

    if clevertronic_prices:
        prefix = _CLEVERTRONIC_PREF.get(condition_score, "Gut")
        for k, v in clevertronic_prices.items():
            if k.startswith(prefix):
                try:
                    result["clevertronic"] = Decimal(str(v))
                except Exception:
                    pass
                break

    return result


def best_buyback_price(matched: dict[str, Decimal | None]) -> Decimal | None:
    """Return the highest price across all matched platforms."""
    values = [v for v in matched.values() if v is not None]
    return max(values) if values else None


# ------------------------------------------------------------------ #
# Image condition check via AI vision                                  #
# ------------------------------------------------------------------ #

_IMAGE_CACHE: dict[str, int] = {}
_LISTING_IMG_CACHE: dict[str, list[str]] = {}

# Vision model (NVIDIA NIM — free tier)
_VISION_MODEL = "meta/llama-3.2-11b-vision-instruct"


def _to_hires(url: str) -> str:
    """Convert eBay/Vinted thumbnail URLs to highest available resolution."""
    if not url:
        return url
    # eBay: s-l140, s-l300, s-l500 → s-l1600 JPEG
    url = re.sub(r"s-l\d+\.\w+$", "s-l1600.jpg", url)
    # Vinted: _1.jpg?... → _1.jpg
    url = re.sub(r"\?.*$", "", url)
    return url


def _fetch_ebay_images(url: str) -> list[str]:
    """Use Playwright to fetch eBay listing images (bypasses Akamai)."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=False,
                args=["--headless=new", "--no-sandbox", "--disable-dev-shm-usage"],
            )
            ctx = browser.new_context(
                locale="de-DE",
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(1500)

            # Extract all image URLs from the gallery
            img_urls = page.evaluate("""() => {
                const urls = new Set();
                // Gallery thumbnails data-src / src
                document.querySelectorAll('img[src*="ebayimg"], img[data-src*="ebayimg"]')
                    .forEach(img => {
                        const src = img.getAttribute('src') || img.getAttribute('data-src') || '';
                        if (src.includes('s-l')) urls.add(src);
                    });
                // JSON-LD data on page
                document.querySelectorAll('script[type="application/ld+json"]').forEach(sc => {
                    try {
                        const d = JSON.parse(sc.textContent);
                        const imgs = Array.isArray(d.image) ? d.image : [d.image];
                        imgs.forEach(i => { if (i && typeof i === 'string') urls.add(i); });
                    } catch(e) {}
                });
                return [...urls];
            }""")
            ctx.close()
            browser.close()

        return [_to_hires(u) for u in (img_urls or []) if u]
    except Exception:
        return []


def _fetch_ka_images(url: str) -> list[str]:
    """Fetch Kleinanzeigen listing images via curl_cffi."""
    try:
        from curl_cffi.requests import Session as CurlSession
        from bs4 import BeautifulSoup
        with CurlSession(impersonate="chrome120") as s:
            r = s.get(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "de-DE"}, timeout=12)
        soup = BeautifulSoup(r.text, "html.parser")
        imgs = []
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src", "")
            if "img.kleinanzeigen" in src or "ebayimg" in src:
                imgs.append(_to_hires(src))
        # Also check og:image
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            imgs.insert(0, _to_hires(og["content"]))
        return imgs
    except Exception:
        return []


def _fetch_vinted_images(url: str) -> list[str]:
    """Fetch Vinted listing images via curl_cffi."""
    try:
        from curl_cffi.requests import Session as CurlSession
        from bs4 import BeautifulSoup
        import json as _json
        with CurlSession(impersonate="chrome120") as s:
            r = s.get(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "de-DE"}, timeout=12)
        soup = BeautifulSoup(r.text, "html.parser")
        imgs = []
        # Vinted embeds photos in __NEXT_DATA__ JSON
        nd = soup.find("script", id="__NEXT_DATA__")
        if nd:
            try:
                data = _json.loads(nd.string or "")
                photos = (data.get("props", {})
                              .get("pageProps", {})
                              .get("item", {})
                              .get("photos", []))
                for p in photos:
                    full = p.get("full_size_url") or p.get("url", "")
                    if full:
                        imgs.append(full)
            except Exception:
                pass
        if not imgs:
            for meta in soup.find_all("meta", property=re.compile(r"og:image")):
                content = meta.get("content", "")
                if content:
                    imgs.append(content)
        return imgs
    except Exception:
        return []


def fetch_listing_images(listing_url: str, max_images: int = 6) -> list[str]:
    """
    Scrape all product images from a listing page.
    Works for eBay, Kleinanzeigen, and Vinted.
    Returns a list of high-res image URLs (up to max_images).
    Results are cached.
    """
    if listing_url in _LISTING_IMG_CACHE:
        return _LISTING_IMG_CACHE[listing_url]

    images: list[str] = []
    try:
        from bs4 import BeautifulSoup

        if "ebay.de" in listing_url or "ebay.com" in listing_url:
            images = _fetch_ebay_images(listing_url)

        elif "kleinanzeigen.de" in listing_url or "ebay-kleinanzeigen" in listing_url:
            images = _fetch_ka_images(listing_url)

        elif "vinted.de" in listing_url or "vinted.com" in listing_url:
            images = _fetch_vinted_images(listing_url)

    except Exception:
        pass

    # Deduplicate and limit
    seen: set[str] = set()
    unique = []
    for img in images:
        if img and img not in seen:
            seen.add(img)
            unique.append(img)
        if len(unique) >= max_images:
            break

    _LISTING_IMG_CACHE[listing_url] = unique
    return unique


def check_listing_images(
    listing_url: str,
    thumbnail_url: str | None,
    api_key: str,
    provider: str = "nvidia",
) -> int | None:
    """
    Check ALL images from a listing and return the lowest (most conservative)
    condition score found. Fetches full-resolution images from the listing page.

    Falls back to thumbnail_url if listing scrape fails.
    Returns normalized score 0-5 or None on error.
    """
    if not api_key or provider not in ("nvidia",):
        return None  # vision only supported on NVIDIA for now

    # Fetch all listing images
    images = fetch_listing_images(listing_url) if listing_url else []
    if not images and thumbnail_url:
        images = [_to_hires(thumbnail_url)]
    if not images:
        return None

    # Check each image — use the most conservative (lowest) score
    scores: list[int] = []
    for img_url in images[:5]:  # max 5 images per listing
        cached = _IMAGE_CACHE.get(img_url)
        if cached is not None:
            scores.append(cached)
            continue

        score = _check_single_image(img_url, api_key)
        if score is not None:
            _IMAGE_CACHE[img_url] = score
            scores.append(score)

    if not scores:
        return None

    # Return median score (ignores outliers from product shot vs worn side)
    scores.sort()
    return scores[len(scores) // 2]


def _check_single_image(image_url: str, api_key: str) -> int | None:
    """Call NVIDIA vision on a single image URL. Returns 0-5 or None."""
    try:
        from openai import OpenAI
        client = OpenAI(base_url="https://integrate.api.nvidia.com/v1", api_key=api_key)
        resp = client.chat.completions.create(
            model=_VISION_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": (
                        "Look at this phone listing image. "
                        "Rate the physical condition of the device. "
                        "Reply with ONLY a single digit:\n"
                        "0=broken/cracked screen or body\n"
                        "1=heavy scratches or dents\n"
                        "2=visible wear, normal used\n"
                        "3=minor/light scratches, good shape\n"
                        "4=like new, barely any marks\n"
                        "5=brand new sealed in box\n"
                        "One digit only."
                    )},
                ],
            }],
            temperature=0,
            max_tokens=5,
            stream=False,
        )
        text = resp.choices[0].message.content.strip()
        m = re.search(r"[0-5]", text)
        return int(m.group()) if m else None
    except Exception:
        return None


# Keep old name as alias for compatibility
def check_image_condition(image_url: str, api_key: str, provider: str = "nvidia") -> int | None:
    if not image_url or not api_key or provider == "none":
        return None
    cached = _IMAGE_CACHE.get(image_url)
    if cached is not None:
        return cached
    score = _check_single_image(_to_hires(image_url), api_key)
    if score is not None:
        _IMAGE_CACHE[image_url] = score
    return score


# ------------------------------------------------------------------ #
# Worth-it decision                                                    #
# ------------------------------------------------------------------ #

def is_worth_it(
    listing_price: Decimal,
    condition_score: int,
    zoxs_prices: dict | None,
    wkfs_prices: dict | None,
    clevertronic_prices: dict | None,
    shipping_cost: Decimal = Decimal("5"),
    fee_rate: Decimal = Decimal("0.1235"),
    fee_fixed: Decimal = Decimal("0.35"),
    min_profit: Decimal = Decimal("15"),
    min_roi: float = 0.15,
    image_url: str | None = None,
    listing_url: str | None = None,
    api_key: str = "",
    provider: str = "none",
    title: str = "",
    description: str = "",
) -> tuple[bool, Decimal | None, float | None]:
    """
    Determine if a listing is worth buying for arbitrage.

    Flow:
      1. Accessory filter (AI or regex)
      2. ROI check with text-based condition score
      3. If ROI positive → AI Vision checks ALL listing images
         to confirm the physical condition isn't worse than described
      4. If images show worse condition → recalculate with downgraded score

    Returns (worth_it, net_profit, roi_pct).
    """
    # Hard block: accessories, cases, chargers → never worth it
    if title and not ai_is_device(title, description, api_key=api_key, provider=provider):
        return False, None, None

    matched = matched_buyback_price(condition_score, zoxs_prices, wkfs_prices, clevertronic_prices)
    buyback = best_buyback_price(matched)

    if buyback is None or listing_price <= 0:
        return False, None, None

    ebay_fees = listing_price * fee_rate + fee_fixed
    net_profit = buyback - listing_price - shipping_cost - ebay_fees
    roi = float(net_profit / listing_price) if listing_price else None

    # ROI gate — only proceed to image check if profitable on paper
    if net_profit < min_profit or (roi is not None and roi < min_roi):
        return False, net_profit, roi

    # ── Image check (only when ROI is good) ─────────────────────────────
    # Fetches ALL images from the listing page (up to 5), checks each
    # with NVIDIA Vision, uses the median score.
    # Only NVIDIA NIM supports vision; DeepSeek text-only → skip.
    if api_key and provider == "nvidia":
        img_score = check_listing_images(
            listing_url=listing_url or "",
            thumbnail_url=image_url,
            api_key=api_key,
            provider=provider,
        )
        if img_score is not None and img_score < condition_score - 1:
            # Images reveal worse condition than described in text
            downgraded = max(COND_BROKEN, img_score)
            matched2 = matched_buyback_price(downgraded, zoxs_prices, wkfs_prices, clevertronic_prices)
            buyback2 = best_buyback_price(matched2)
            if buyback2:
                net_profit = buyback2 - listing_price - shipping_cost - ebay_fees
                roi = float(net_profit / listing_price)
                if net_profit < min_profit or roi < min_roi:
                    return False, net_profit, roi

    return True, net_profit, roi
