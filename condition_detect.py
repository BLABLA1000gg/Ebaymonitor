"""
Condition detection for marketplace listings.

Flow:
  1. Regex scan of title + description → normalized condition score (0-5)
  2. Map score → correct key in buyback price dicts
  3. Optional: image check via NVIDIA vision API (only when ROI is promising)
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any

# ------------------------------------------------------------------ #
# Accessory / non-device filter                                        #
# ------------------------------------------------------------------ #

# Keywords that indicate it's NOT the device itself
_ACCESSORY_KEYWORDS = [
    # Cases & covers — must be in TITLE only (checked separately)
    "handyhülle", "handyhuelle", "schutzhülle", "schutzhuelle",
    "bumper", "wallet case", "flip case", "book case",
    "lederhülle", "silikonhülle", "tpu hülle",
    # Screen protection
    "panzerglas", "screen protector", "displayschutz", "schutzfolie", "glasfolie",
    # Cables / chargers as standalone product
    "ladekabel", "ladegerät", "ladestation", "wireless charger",
    "powerbank", "power bank", "magsafe",
    # Accessories
    "halterung", "autohalter", "kopfhörer", "kopfhoerer",
    "earpods", "earbuds", "dongle",
    # Spare parts
    "ersatzteil", "display ersatz", "akku ersatz", "flex kabel",
    "lautsprecher ersatz",
    # Back glass as standalone spare part
    "backglas", "back glas", "rückglas", "rueckglas",
    # Wanted / buying ads — multi-word to avoid false positives
    "ankauf suche", "suche iphone", "kaufe iphone", "suche samsung",
    "kaufe samsung", "suche smartphone", "kaufe smartphone",
    "ankauf iphone", "ankauf samsung", "wir kaufen", "ich kaufe",
    # Smartwatch / tablet
    "apple watch", "ipad", "homepod",
]

# Keywords only checked in TITLE (not description) — too common in descriptions
_TITLE_ONLY_ACCESSORY = [
    "hülle", "schutzglas", "kabel", "charger", "netzteil",
    "adapter", "hub", "airpods",
    "reparatur", "reparatur service", "diagnose",
    "gesucht", "wanted", "looking for", "zubehör", "zubehoer", "bundle",
    # Display/glass/LCD parts — repair components, not phones
    "display glas", "display lcd", "lcd display", "glas lcd", "lcd glas",
    "displayglas", " lcd ", "displaytausch", "glasbruch",
    # Ankauf / bulk buy listings — match many models → not a single-phone listing
    "ankauf", "suche iphone", "kauf iphone",
]

# Multi-model pattern: "11,12,13,14" or "11/12/13/14" or "(X,11,12,13)"
# Repair shops and buy-back listings often list many models → not a single phone
_MULTI_MODEL_PATTERN = re.compile(
    r'\b1[0-9]\s*[,/]\s*1[0-9]\s*[,/]\s*1[0-9]'  # e.g. 11,12,13 or 11/12/13
    r'|\b(?:x|xs|xr)\s*[,/]\s*1[0-9]\s*[,/]\s*1[0-9]',  # e.g. X,11,12
    re.IGNORECASE,
)


def is_actual_device(title: str, description: str = "") -> bool:
    """
    Fast regex pre-filter: False if obvious accessory keyword found.
    Checks full text for _ACCESSORY_KEYWORDS, title-only for _TITLE_ONLY_ACCESSORY.
    Also blocks multi-model listings (repair shops, bulk-buy ads).
    """
    title_l = title.lower()
    full_l = (title + " " + description[:500]).lower()
    for kw in _ACCESSORY_KEYWORDS:
        if kw in full_l:
            return False
    for kw in _TITLE_ONLY_ACCESSORY:
        if kw in title_l:
            return False
    # Block repair-shop / multi-buy listings that name 3+ models
    if _MULTI_MODEL_PATTERN.search(title):
        return False
    return True


_device_cache: dict[str, bool] = {}
_cond_cache: dict[str, int] = {}
_assess_cache: dict[str, dict] = {}  # title → full assessment dict


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
            client = OpenAI(base_url="https://integrate.api.nvidia.com/v1", api_key=api_key, timeout=40, max_retries=0)
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

    _json = json
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
    _json = json

    default = {"condition": COND_GOOD, "functional": True, "battery_ok": True,
               "has_box": False, "has_cable": False}

    if not api_key or provider == "none":
        return [default.copy() for _ in listings]

    results: list[dict | None] = [None] * len(listings)
    uncached: list[int] = []

    for i, (title, _) in enumerate(listings):
        key = title.strip().lower()[:100]
        if key in _assess_cache:
            results[i] = _assess_cache[key].copy()
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
        "You are a second-hand phone listing analyzer for resale arbitrage. "
        "Grade each listing using these EXACT condition levels that map to buyback portal prices:\n"
        "  5 = Neu: brand new, sealed, OVP\n"
        "  4 = Wie neu: no visible marks, display pristine, frame/back pristine\n"
        "  3 = Sehr gut: display no/minimal marks; frame/back may have few light scratches\n"
        "  2 = Gut: display has visible wear; frame/back has visible scratches/wear\n"
        "  1 = Gebraucht: display clearly worn; frame/back many scratches, dents, paint wear\n"
        "  0 = Beschädigt: display OR back/frame has a CRACK or BREAK (portals reject these)\n\n"
        "For each numbered listing output a JSON object with:\n"
        "  condition: integer 0-5 as above\n"
        "  functional: true ONLY if fully working, no significant damage. "
        "Set false if: cracked/broken screen, cracked back glass, water damage, "
        "not turning on, needs repair, touch defect.\n"
        "  battery_ok: true if battery >=81% or not mentioned, false if explicitly <81%\n"
        "  has_box: true if original box/OVP included\n"
        "  has_cable: true if original cable/charger included\n"
        "When in doubt grade ONE level lower (be conservative). "
        "Output ONLY a JSON array of objects, one per listing, in order. No text."
    )

    user_msg = "\n".join(lines)

    try:
        if provider == "nvidia":
            from openai import OpenAI
            client = OpenAI(base_url="https://integrate.api.nvidia.com/v1", api_key=api_key, timeout=40, max_retries=0)
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
            _assess_cache[title.strip().lower()[:100]] = d
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
    _json = json

    try:
        if provider == "nvidia":
            from openai import OpenAI
            client = OpenAI(base_url="https://integrate.api.nvidia.com/v1", api_key=api_key, timeout=40, max_retries=0)
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
        # Multi-word phrases first — unambiguous, no false positives
        "display kaputt", "display gebrochen", "display defekt", "display schaden",
        "displayschaden", "schaden intern", "innerer schaden", "screen defekt",
        "display problem", "touch defekt", "touch kaputt", "funktioniert nicht",
        "rückseite gebrochen", "rückseite kaputt", "rückseite riss", "rückseite gesprungen",
        "back cover kaputt", "back glass broken", "back glass cracked",
        "hinten kaputt", "hinten gebrochen", "hinten riss",
        "gehäuse kaputt", "gehäuse gebrochen", "rahmen gebrochen",
        "hat einen riss", "hat risse", "hat einen bruch", "weist risse auf",
        "glas gesprungen", "glas gebrochen", "glas kaputt",
        # Single words — only safe ones that rarely appear negated
        "defekt", "kaputt", "broken", "fault",
        # Dutch (NL) — Vinted listings
        "gaat niet meer aan", "werkt niet meer", "scherm kapot", "scherm gebarsten",
        "scherm gebroken", "niet aan te zetten", "werkt niet",
        # French (FR) — Vinted listings
        "ne fonctionne plus", "ne s'allume plus", "écran cassé", "écran fissuré",
        "ne marche plus", "en panne", "batterie à changer", "batterie a changer",
        "batterie morte", "à réparer", "a reparer",
        # Italian (IT) — Vinted listings
        "non funziona", "schermo rotto", "vetro rotto", "rotto dietro",
        "non si accende", "display rotto",
        # "riss", "bruch", "gebrochen" removed — too many false positives with "kein Riss" etc.
        # The AI handles these ambiguous cases correctly
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
    COND_LIKE_NEW:   "2",   # Hervorragend (was "1"; "2" is ZOXS's 2nd-best tier)
    COND_VERY_GOOD:  "3",   # Sehr gut
    COND_GOOD:       "4",   # Gut
    COND_ACCEPTABLE: "65",  # Stark gebraucht
    # COND_BROKEN intentionally omitted — buyback portals reject broken phones
}

# WirKaufens: {condition_key: "Label"} from buyback.py WKFS_CONDITIONS
# normalized score → WKFS key (string)
_WKFS_MAP: dict[int, str] = {
    COND_NEW:        "4",   # Wie Neu (5=Neu rarely offered)
    COND_LIKE_NEW:   "4",   # Wie Neu
    COND_VERY_GOOD:  "3",   # Gut
    COND_GOOD:       "2",   # In Ordnung
    COND_ACCEPTABLE: "1",   # Schlecht
    # COND_BROKEN intentionally omitted — portals reject/reprice broken phones
}

# Clevertronic condition labels in their price dict (first 4 chars match)
# We'll do prefix matching on keys
_CLEVERTRONIC_PREF: dict[int, str] = {
    COND_NEW:        "Neu",
    COND_LIKE_NEW:   "Wie",   # "Wie Neu"
    COND_VERY_GOOD:  "Sehr",  # "Sehr gut"
    COND_GOOD:       "Gut",
    COND_ACCEPTABLE: "Akze",  # "Akzeptabel"
    # COND_BROKEN intentionally omitted — portals reject broken phones
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

    # Conservative fallback: when no keyword matches, default to COND_ACCEPTABLE
    # (one level below COND_GOOD). The AI assessment is the primary grading path;
    # this regex-only fallback should never optimistically assume "Gut" quality.
    return best_score if best_score is not None else COND_ACCEPTABLE


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

    # Broken phones are rejected by buyback portals — never map to a price.
    # The functional guard in profile_monitor should catch this, but we enforce
    # it here as a hard safety net.
    if condition_score == COND_BROKEN:
        return result

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


def _fetch_ka_details(url: str) -> tuple[list[str], str]:
    """Fetch Kleinanzeigen listing images + description. Returns (images, description)."""
    try:
        from curl_cffi.requests import Session as CurlSession
        from bs4 import BeautifulSoup
        with CurlSession(impersonate="chrome120") as s:
            r = s.get(url, headers={"Accept-Language": "de-DE,de;q=0.9"}, timeout=12)
        soup = BeautifulSoup(r.text, "html.parser")
        imgs = []
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src", "")
            if "img.kleinanzeigen" in src or "ebayimg" in src:
                imgs.append(_to_hires(src))
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            imgs.insert(0, _to_hires(og["content"]))
        # Extract description
        desc_el = soup.find(id="viewad-description-text")
        description = desc_el.get_text(" ", strip=True) if desc_el else ""
        return imgs, description
    except Exception:
        return [], ""


def _fetch_ka_images(url: str) -> list[str]:
    imgs, _ = _fetch_ka_details(url)
    return imgs


def _fetch_vinted_details(url: str) -> tuple[list[str], str]:
    """Fetch Vinted listing images + description. Returns (images, description).

    Vinted dropped __NEXT_DATA__ — description and photos are now embedded as
    JSON strings within the main HTML bundle. We extract them via regex.
    """
    try:
        from curl_cffi.requests import Session as CurlSession
        _json = json
        with CurlSession(impersonate="chrome120") as s:
            # Do NOT override User-Agent — curl_cffi chrome120 impersonation
            # sets the full browser fingerprint. Overriding breaks it.
            r = s.get(url, headers={"Accept-Language": "de-DE,de;q=0.9"}, timeout=15)

        text = r.text
        imgs: list[str] = []
        description = ""

        # ── Description ──────────────────────────────────────────────────
        # Embedded as "description":"..." in a JSON blob in the HTML
        m = re.search(r'"description":"((?:[^"\\]|\\.)*)"', text)
        if m:
            try:
                description = _json.loads('"' + m.group(1) + '"')  # unescape JSON string
            except Exception:
                description = m.group(1).replace("\\n", "\n").replace('\\"', '"')

        # ── Photos ───────────────────────────────────────────────────────
        # Real listing photos come from images*.vinted.net CDN
        full_urls = re.findall(r'"full_size_url":"([^"]+)"', text)
        imgs = [u for u in full_urls if re.search(r"images\d*\.vinted\.", u)]

        # Fallback: preload image links from images*.vinted.net
        if not imgs:
            preloads = re.findall(r'href="(https://images\d*\.vinted\.[^"]+)"', text)
            imgs = list(dict.fromkeys(preloads))  # deduplicate, keep order

        # Last resort: og:image (usually the main photo)
        if not imgs:
            og = re.search(r'property="og:image"[^>]*content="([^"]+images\d*\.vinted[^"]+)"', text)
            if og:
                imgs = [og.group(1)]

        return imgs, description
    except Exception:
        return [], ""


def _fetch_vinted_images(url: str) -> list[str]:
    imgs, _ = _fetch_vinted_details(url)
    return imgs


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


def _image_to_b64(image_url: str) -> str | None:
    """Download an image and return base64 data URI. Required for URLs that
    NVIDIA's servers can't fetch directly (e.g. eBay CDN images)."""
    try:
        import base64
        from curl_cffi.requests import Session as CurlSession
        with CurlSession(impersonate="chrome120") as s:
            r = s.get(image_url, timeout=10, headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "image/*",
            })
            if r.status_code != 200 or not r.content:
                return None
            mime = r.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
            b64 = base64.b64encode(r.content).decode()
            return f"data:{mime};base64,{b64}"
    except Exception:
        return None


def _check_single_image(image_url: str, api_key: str) -> int | None:
    """Call NVIDIA vision on a single image URL. Downloads and base64-encodes
    the image first so NVIDIA can access it regardless of CDN restrictions."""
    try:
        from openai import OpenAI

        data_uri = _image_to_b64(image_url)
        if not data_uri:
            return None

        client = OpenAI(
            base_url="https://integrate.api.nvidia.com/v1",
            api_key=api_key,
            timeout=30,  # hard timeout — never hang indefinitely
            max_retries=0,
        )
        resp = client.chat.completions.create(
            model=_VISION_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_uri}},
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


def quick_vision_score(
    image_url: str | None,
    listing_url: str | None,
    api_key: str,
    provider: str = "nvidia",
    max_images: int = 3,
) -> int | None:
    """Kept for compatibility. Prefer ai_assess_listing_full()."""
    result = ai_assess_listing_full(
        title="", description="",
        image_url=image_url, listing_url=listing_url,
        api_key=api_key, provider=provider, max_images=max_images,
    )
    return result.get("condition") if result else None


def ai_assess_listing_full(
    title: str,
    description: str,
    image_url: str | None,
    listing_url: str | None,
    api_key: str,
    provider: str = "nvidia",
    max_images: int = 3,
) -> dict | None:
    """
    Full listing assessment combining title, description AND photos in ONE call
    to the vision model. Returns the same dict as ai_assess_listing_batch:
        {condition, functional, battery_ok, has_box, has_cable}

    This is the highest-reliability path — the model sees everything at once
    and can catch contradictions (e.g. text says 'perfect' but photo shows cracks).

    Falls back to None if vision is unavailable (no API key, wrong provider,
    no images found). Caller should then use text-only batch assessment.
    """
    if not api_key or provider != "nvidia":
        return None

    _json = json

    # Fetch images + description from listing page
    images: list[str] = []
    fetched_desc = ""
    if listing_url:
        try:
            if "kleinanzeigen.de" in listing_url:
                images, fetched_desc = _fetch_ka_details(listing_url)
            elif "vinted." in listing_url:
                images, fetched_desc = _fetch_vinted_details(listing_url)
            else:
                images = fetch_listing_images(listing_url)
        except Exception:
            pass
    if not images and image_url:
        images = [_to_hires(image_url)]
    if not images:
        return None

    # NVIDIA vision model supports at most 1 image per prompt
    images = images[:1]

    # Use fetched description if caller didn't provide one
    if not description and fetched_desc:
        description = fetched_desc

    # Cache key: title + first image URL
    cache_key = (title.strip().lower()[:80] + "|" + (images[0] if images else ""))
    if cache_key in _assess_cache:
        return _assess_cache[cache_key].copy()

    # Build multimodal message: images first (base64-encoded), then text context
    content: list[dict] = []
    for img_url in images:
        data_uri = _image_to_b64(img_url)
        if data_uri:
            content.append({"type": "image_url", "image_url": {"url": data_uri}})

    if not content:
        return None  # couldn't download any image

    listing_text = f"Title: {title}"
    if description:
        listing_text += f"\nDescription: {description[:600]}"

    content.append({"type": "text", "text": (
        f"{listing_text}\n\n"
        "You are evaluating a second-hand phone listing for resale arbitrage. "
        "Look at the photo AND the title/description together.\n\n"
        "Use these EXACT condition grades (they map directly to buyback portal prices):\n\n"
        "5 = Neu: Brand new, sealed, original packaging (OVP/ungeöffnet)\n"
        "4 = Wie neu: No visible marks at all, display pristine, frame/back pristine\n"
        "3 = Sehr gut: Display has no or minimal traces; frame/back may have a few light scratches\n"
        "2 = Gut: Display has visible wear; frame/back has visible wear/scratches\n"
        "1 = Gebraucht: Display has clearly visible heavy wear; frame/back has many scratches, dents, paint wear\n"
        "0 = Beschädigt/Defekt: Display OR frame/back has a CRACK or BREAK — buyback portals reject these\n\n"
        "functional: false if ANY of these apply: cracked/broken screen, cracked back glass, "
        "water damage, device not turning on, needs repair, display defect, touch not working\n"
        "battery_ok: false only if description explicitly states battery <81% health\n"
        "has_box: true if original box/OVP mentioned\n"
        "has_cable: true if original cable/charger mentioned\n\n"
        "IMPORTANT: photo overrides text — if photo shows damage not mentioned in text, "
        "use the lower condition. When in doubt, grade ONE level lower (be conservative).\n\n"
        "Reply with ONLY a JSON object, no text before or after:\n"
        '{"condition": <0-5>, "functional": <true/false>, '
        '"battery_ok": <true/false>, "has_box": <true/false>, "has_cable": <true/false>}'
    )})

    try:
        from openai import OpenAI
        client = OpenAI(base_url="https://integrate.api.nvidia.com/v1", api_key=api_key, timeout=40, max_retries=0)
        resp = client.chat.completions.create(
            model=_VISION_MODEL,
            messages=[{"role": "user", "content": content}],
            temperature=0,
            max_tokens=80,
            stream=False,
        )
        text = resp.choices[0].message.content.strip()
        m = re.search(r"\{[^}]+\}", text, re.DOTALL)
        if not m:
            return None
        raw = _json.loads(m.group())
        result = {
            "condition":  max(0, min(5, int(raw.get("condition", COND_GOOD)))),
            "functional": bool(raw.get("functional", True)),
            "battery_ok": bool(raw.get("battery_ok", True)),
            "has_box":    bool(raw.get("has_box", False)),
            "has_cable":  bool(raw.get("has_cable", False)),
        }
        _assess_cache[cache_key] = result
        return result
    except Exception:
        return None


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
    title: str = "",
    description: str = "",
    api_key: str = "",
    provider: str = "none",
    # Legacy params kept for compat — vision is now handled upstream
    image_url: str | None = None,
    listing_url: str | None = None,
) -> tuple[bool, Decimal | None, float | None]:
    """
    Pure ROI calculation given a pre-determined condition score.
    Vision check is done BEFORE calling this, in profile_monitor.py.

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

    if net_profit < min_profit or (roi is not None and roi < min_roi):
        return False, net_profit, roi

    return True, net_profit, roi
