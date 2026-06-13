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
_assess_cache: dict[str, dict] = {}  # cache key → full assessment dict


def trim_caches(max_entries: int = 5000) -> None:
    """Drop in-memory AI caches once they grow past max_entries.

    The dashboard monitor loop runs for days; without a bound these caches
    grow with every unique listing ever seen. A full clear is fine — entries
    are cheap to recompute and stale assessments are worthless anyway.
    """
    for cache in (_device_cache, _cond_cache, _assess_cache,
                  _IMAGE_CACHE, _LISTING_IMG_CACHE):
        if len(cache) > max_entries:
            cache.clear()


def _assess_key(title: str, description: str = "", extra: str = "") -> str:
    """Cache key for AI assessments.

    Must include the description (not just the title): two listings with the
    same title ("Apple iPhone 13 Pro") but different descriptions are
    different phones — reusing the first assessment for the second could mark
    a broken phone as functional.
    """
    return (title.strip().lower()[:100]
            + "|" + str(hash(description.strip().lower()[:600]))
            + ("|" + extra if extra else ""))


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

    cache_key = _assess_key(title, description)
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
    for i, (title, desc) in enumerate(listings):
        key = _assess_key(title, desc)
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
                    _cond_cache[_assess_key(listings[i][0], listings[i][1])] = s
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

    for i, (title, desc) in enumerate(listings):
        key = _assess_key(title, desc)
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
            + (f" | Desc: {desc[:400]}" if desc.strip() else " | Desc: (keine Beschreibung – bitte nur anhand Titel bewerten)")
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
        "not turning on, needs repair, touch defect, camera broken, "
        "occasional crashes ('Abstürze'), 'für Bastler', 'Ersatzteile'.\n"
        "  battery_ok: true if battery >=81% or not mentioned, false if explicitly <81%\n"
        "  has_box: true if original box/OVP included\n"
        "  has_cable: true if original cable/charger included\n"
        "CRITICAL: When in doubt grade ONE level lower. "
        "Any mention of crack/Riss/Haarriss/Sprung → condition=0 regardless of how 'klein'. "
        "'Gelegentliche Abstürze'/'startet manchmal nicht' → functional=false. "
        "Output ONLY a JSON array of objects, one per listing, in order. No text."
    )

    import logging as _logging
    import time as _time
    _log = _logging.getLogger(__name__)
    user_msg = "\n".join(lines)
    _log.debug(
        "Text-batch START — provider=%s total=%d uncached=%d",
        provider, len(listings), len(uncached),
    )
    _log.debug("Text-batch PROMPT:\n%s", user_msg[:1000])

    _tb0 = _time.monotonic()
    try:
        if provider == "nvidia":
            from openai import OpenAI
            client = OpenAI(base_url="https://integrate.api.nvidia.com/v1", api_key=api_key, timeout=40, max_retries=0)
            resp = client.chat.completions.create(
                model="meta/llama-4-maverick-17b-128e-instruct",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0,
                max_tokens=60 * len(uncached) + 20,
                stream=False,
            )
            text = resp.choices[0].message.content.strip()
            _log.debug("Text-batch NVIDIA OK (%.2fs) %d chars — RAW: %s",
                       _time.monotonic() - _tb0, len(text), text[:400])
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
            if r.status_code != 200:
                _log.warning("Text-batch DeepSeek HTTP %s (%.2fs): %s",
                             r.status_code, _time.monotonic() - _tb0, r.text[:300])
            text = r.json()["choices"][0]["message"]["content"].strip()
            _log.debug("Text-batch DeepSeek OK (%.2fs) %d chars — RAW: %s",
                       _time.monotonic() - _tb0, len(text), text[:400])
        else:
            text = ""

        # Parse JSON array from response
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if not m:
            _log.warning("Text-batch: no JSON array in response (%.2fs): %s",
                         _time.monotonic() - _tb0, text[:300])
        parsed = _json.loads(m.group()) if m else []

        for j, raw in enumerate(parsed):
            if j >= len(uncached):
                break
            i = uncached[j]
            title = listings[i][0]
            # Model sometimes returns a bare integer instead of a dict
            if isinstance(raw, (int, float)):
                score = max(0, min(5, int(raw)))
                d = {
                    "condition":  score,
                    "functional": score > 0,
                    "battery_ok": True,
                    "has_box":    False,
                    "has_cable":  False,
                }
            elif isinstance(raw, dict):
                d = {
                    "condition":   max(0, min(5, int(raw.get("condition", COND_GOOD)))),
                    "functional":  bool(raw.get("functional", True)),
                    "battery_ok":  bool(raw.get("battery_ok", True)),
                    "has_box":     bool(raw.get("has_box", False)),
                    "has_cable":   bool(raw.get("has_cable", False)),
                }
            else:
                continue
            _log.debug("Text-batch result[%d] %r: cond=%s func=%s",
                       i, title[:30], d["condition"], d["functional"])
            results[i] = d
            k = _assess_key(title, listings[i][1])
            _assess_cache[k] = d
            _cond_cache[k] = d["condition"]

    except Exception as e:
        _log.warning("Text-batch FAIL (%.2fs provider=%s %d listings): %s: %s",
                     _time.monotonic() - _tb0, provider, len(uncached),
                     type(e).__name__, str(e)[:300])

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
        # Console defects — Nintendo Switch
        "joy-con drift", "stick driftet", "stick drift", "joy-con defekt",
        "lässt sich nicht laden",
        # Console defects — PlayStation
        "liest keine discs", "laufwerk defekt", "laufwerkfehler", "kein bild",
        "überhitzt", "ce-34878",
        # Console defects — Xbox
        "disc lesefehler", "rrod", "e68",
        # iPad / tablet defects
        "displaybruch", "touch funktioniert nicht", "face id defekt",
        "touch id defekt", "home button defekt", "akku defekt", "akku tauschen",
        # Common eBay/Vinted defect phrases (often buried in description)
        "zum ausschlachten", "für bastler", "für ersatzteile", "ersatzteile oder defekt",
        "startet nicht", "bootet nicht", "lässt sich nicht einschalten",
        "geht nicht an", "schaltet sich nicht ein", "schaltet nicht ein",
        "hängt sich auf", "friert ein", "abstürze", "immer wieder aus",
        "kamera defekt", "kamera kaputt", "kamera funktioniert nicht",
        "lautsprecher defekt", "mikrofon defekt", "wlan defekt", "bluetooth defekt",
        "sim karte wird nicht erkannt", "sim wird nicht erkannt", "kein empfang",
        "icloud gesperrt", "mdm gesperrt",
        "wasserschaden", "wasser schaden", "liquid damage", "water damage",
        "geruchsschaden", "brandschaden",
        "touchscreen reagiert nicht", "display reagiert nicht",
        "schwarzer bildschirm", "schwarzes display", "kein display",
        "streifen auf dem display", "pixel fehler", "toter pixel",
        "akku hält nicht", "akku schwach", "akku kaum", "akku 0",
        "schrauben fehlen", "ersatzglas", "displayglas gebrochen",
        # Spanish (ES) — Vinted
        "no funciona", "pantalla rota", "cristal roto",
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
        # Substring fallback: eBay.de uses longer labels like
        # "Für Ersatzteile oder defekt" that exact lookup misses. A parts
        # listing slipping through as unlabeled would be a guaranteed loss,
        # so broken indicators are checked as substrings.
        if any(s in key for s in ("ersatzteile", "defekt", "for parts", "not working")):
            return COND_BROKEN
        for label, score in _EBAY_PLATFORM_COND.items():
            if label in key:
                return score

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
    rebuy_prices: dict | None = None,
) -> dict[str, Decimal | None]:
    """
    Given a condition score and all-conditions price dicts, return the
    single condition-matched price per platform.
    Returns {'zoxs': ..., 'wirkaufens': ..., 'clevertronic': ..., 'rebuy': ...}
    (each Decimal | None).
    """
    result: dict[str, Decimal | None] = {
        "zoxs": None,
        "wirkaufens": None,
        "clevertronic": None,
        "rebuy": None,
    }

    # Broken phones are rejected by buyback portals — never map to a price.
    # The functional guard in profile_monitor should catch this, but we enforce
    # it here as a hard safety net.
    if condition_score == COND_BROKEN:
        return result

    # FIXED CONSERVATIVE TIERS — independent of the detected condition.
    # Portals routinely downgrade on arrival; pricing every deal at these
    # worst-case-realistic tiers guarantees the calculated profit is a FLOOR,
    # never an estimate that can disappoint:
    #   ZOXS         → "Gut"
    #   WirKaufens   → "In Ordnung"
    #   Clevertronic → "Gebraucht"
    #   rebuy        → "Gut" (A3)
    # The AI condition is still used for filtering (broken/non-functional →
    # skip), just never for picking a higher payout tier.
    if zoxs_prices:
        val = zoxs_prices.get("Gut") or zoxs_prices.get("Stark gebraucht")
        if val:
            try:
                result["zoxs"] = Decimal(str(val))
            except Exception:
                pass

    if wkfs_prices:
        val = wkfs_prices.get("In Ordnung") or wkfs_prices.get("Schlecht")
        if val:
            try:
                result["wirkaufens"] = Decimal(str(val))
            except Exception:
                pass

    if clevertronic_prices:
        for prefix in ("Gebraucht", "Akze"):  # fall back one tier DOWN only
            for k, v in clevertronic_prices.items():
                if k.startswith(prefix):
                    try:
                        result["clevertronic"] = Decimal(str(v))
                    except Exception:
                        pass
                    break
            if result["clevertronic"] is not None:
                break

    if rebuy_prices:
        # rebuy grades: Wie neu (A1), Sehr gut (A2), Gut (A3), Stark genutzt (A4)
        val = rebuy_prices.get("Gut") or rebuy_prices.get("Stark genutzt")
        if val:
            try:
                result["rebuy"] = Decimal(str(val))
            except Exception:
                pass

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
_VISION_MODEL = "meta/llama-4-maverick-17b-128e-instruct"


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


_ka_sess = None
_ka_sess_lock = __import__("threading").Lock()


def _ka_session():
    """Shared keep-alive curl_cffi session for Kleinanzeigen (fewer handshakes)."""
    global _ka_sess
    with _ka_sess_lock:
        if _ka_sess is None:
            from curl_cffi.requests import Session as CurlSession
            _ka_sess = CurlSession(impersonate="chrome120")
        return _ka_sess


def _ka_reset_session():
    global _ka_sess
    with _ka_sess_lock:
        try:
            if _ka_sess is not None:
                _ka_sess.close()
        except Exception:
            pass
        _ka_sess = None


def _fetch_ka_details(url: str) -> tuple[list[str], str]:
    """Fetch Kleinanzeigen listing images + description. Returns (images, description).

    Primary path: the Kleinanzeigen mobile gateway JSON API (read-only, gated by
    a static public app credential) which returns the description and full-size
    image URLs directly. Falls back to the hardened HTML scrape (shared keep-alive
    session, one retry, JSON-LD ImageObject fallback) if the gateway fails.
    """
    import time as _time
    from bs4 import BeautifulSoup
    import json as _json

    _t0 = _time.monotonic()
    _LOGGER_CD.debug("KA fetch START — %s", url[-80:])
    try:
        from marketplaces import fetch_kleinanzeigen_ad_detail

        imgs, description = fetch_kleinanzeigen_ad_detail(url)
        imgs = [_to_hires(u) for u in imgs]
        _LOGGER_CD.debug(
            "KA API OK (%.2fs) — imgs=%d desc_len=%d — %s",
            _time.monotonic() - _t0, len(imgs), len(description), url[-60:],
        )
        if imgs or description:
            return imgs, description
        _LOGGER_CD.debug("KA API leer — fallback auf HTML — %s", url[-60:])
    except Exception as e:
        _LOGGER_CD.debug("KA API Fehler (%.2fs) %s — %s — HTML-Fallback",
                         _time.monotonic() - _t0, type(e).__name__, url[-60:])

    last_err: Exception | None = None
    for attempt in range(2):
        _ta = _time.monotonic()
        try:
            _LOGGER_CD.debug("KA HTML attempt=%d — %s", attempt + 1, url[-60:])
            s = _ka_session()
            r = s.get(url, headers={"Accept-Language": "de-DE,de;q=0.9"}, timeout=12)
            _LOGGER_CD.debug("KA HTML HTTP %s (%.2fs) size=%d — %s",
                             r.status_code, _time.monotonic() - _ta, len(r.content), url[-60:])
            soup = BeautifulSoup(r.text, "html.parser")
            imgs = []
            for img in soup.find_all("img"):
                src = img.get("src") or img.get("data-src", "")
                if "img.kleinanzeigen" in src or "ebayimg" in src:
                    imgs.append(_to_hires(src))
            og = soup.find("meta", property="og:image")
            if og and og.get("content"):
                imgs.insert(0, _to_hires(og["content"]))
            # Fallback: JSON-LD ImageObject blocks carry reliable contentUrls
            if not imgs:
                for blk in re.findall(r'<script type="application/ld\+json">(.*?)</script>', r.text, re.S):
                    try:
                        d = _json.loads(blk)
                    except Exception:
                        continue
                    if isinstance(d, dict) and d.get("@type") == "ImageObject" and d.get("contentUrl"):
                        imgs.append(_to_hires(d["contentUrl"]))
            desc_el = soup.find(id="viewad-description-text")
            description = desc_el.get_text(" ", strip=True) if desc_el else ""
            # Deduplicate images, keep order
            imgs = list(dict.fromkeys(imgs))
            _LOGGER_CD.debug(
                "KA HTML parse — imgs=%d desc_len=%d — first_img=%s",
                len(imgs), len(description), (imgs[0][:70] if imgs else "–"),
            )
            if imgs or description:
                _LOGGER_CD.debug("KA fetch DONE (%.2fs total) — %s",
                                 _time.monotonic() - _t0, url[-60:])
                return imgs, description
        except Exception as e:
            last_err = e
            _LOGGER_CD.debug("KA HTML attempt=%d Fehler (%.2fs) %s: %s",
                             attempt + 1, _time.monotonic() - _ta, type(e).__name__, e)
            _ka_reset_session()
    _LOGGER_CD.warning("KA fetch FAIL (%.2fs) — last_err=%r — %s",
                       _time.monotonic() - _t0, last_err, url[-60:])
    return [], ""


def _fetch_ka_images(url: str) -> list[str]:
    imgs, _ = _fetch_ka_details(url)
    return imgs


_VINTED_ITEM_ID_RE = re.compile(r"/items/(\d+)")
_VINTED_GB_RE = re.compile(r'\b\d{2,4}\s?(?:gb|go)\b|\b\d\s?tb\b', re.IGNORECASE)


def _fetch_vinted_details(url: str) -> tuple[list[str], str]:
    """Fetch Vinted listing images + description. Returns (images, description).

    Primary path: Vinted's internal JSON API —
    /api/v2/items/<id>/plugins/sidebar for the description (and title) and
    /api/v2/items/<id>/photos for full-size photo URLs. Uses the shared
    anonymous-cookie session from marketplaces, which tolerates far more
    sequential requests than the HTML item pages (~26/IP burst limit).
    Falls back to the legacy HTML-regex scrape if the API fails.
    """
    import time as _time
    _t0 = _time.monotonic()
    _LOGGER_CD.debug("Vinted fetch START — %s", url[-80:])

    id_match = _VINTED_ITEM_ID_RE.search(url)
    if id_match:
        try:
            from marketplaces import vinted_api_get

            item_id = id_match.group(1)
            _LOGGER_CD.debug("Vinted API — item_id=%s", item_id)
            r = vinted_api_get(f"/api/v2/items/{item_id}/plugins/sidebar")
            _LOGGER_CD.debug("Vinted sidebar HTTP %s (%.2fs)", r.status_code, _time.monotonic() - _t0)
            if r.status_code != 200:
                raise RuntimeError(f"sidebar HTTP {r.status_code}")
            description = ""
            title = ""
            for plugin in r.json().get("plugins", []):
                data = plugin.get("data") or {}
                if plugin.get("name") == "description":
                    description = data.get("description") or ""
                elif plugin.get("name") == "summary":
                    for line in data.get("lines", []):
                        for el in line.get("elements", []):
                            if el.get("style") == "title":
                                title = el.get("value") or ""

            # Storage attribute guard: sellers often put the GB size only in
            # the title / platform attribute, never in the free text. Accept
            # the title's size ONLY when it names exactly one plausible value.
            if description and not _VINTED_GB_RE.search(description):
                sizes = {
                    int(m) for m in re.findall(r'\b(\d{2,4})\s?(?:gb|go)\b', title, re.IGNORECASE)
                } & {16, 32, 64, 128, 256, 512}
                sizes |= {
                    int(m) * 1024 for m in re.findall(r'\b(\d)\s?tb\b', title, re.IGNORECASE)
                } & {1024}
                if len(sizes) == 1:
                    description += f"\n[Vinted-Attribut: {sizes.pop()} GB]"

            rp = vinted_api_get(f"/api/v2/items/{item_id}/photos")
            _LOGGER_CD.debug("Vinted photos HTTP %s (%.2fs)", rp.status_code, _time.monotonic() - _t0)
            imgs: list[str] = []
            if rp.status_code == 200:
                for photo in rp.json().get("photos", []):
                    u = photo.get("full_size_url") or photo.get("url")
                    if u:
                        imgs.append(u)

            _LOGGER_CD.debug(
                "Vinted API OK (%.2fs) — imgs=%d desc_len=%d — first_img=%s",
                _time.monotonic() - _t0, len(imgs), len(description),
                (imgs[0][:70] if imgs else "–"),
            )
            if description or imgs:
                return imgs, description
            _LOGGER_CD.debug("Vinted API leer — HTML-Fallback")
        except Exception as e:
            _LOGGER_CD.debug("Vinted API Fehler (%.2fs) %s: %s — HTML-Fallback",
                             _time.monotonic() - _t0, type(e).__name__, e)

    result = _fetch_vinted_details_html(url)
    _LOGGER_CD.debug("Vinted HTML DONE (%.2fs) — imgs=%d desc_len=%d",
                     _time.monotonic() - _t0, len(result[0]), len(result[1]))
    return result


def _fetch_vinted_details_html(url: str) -> tuple[list[str], str]:
    """Legacy fallback: scrape the item HTML page. Rate-limited (~26 req/IP
    burst) and brittle — only used when the JSON API path fails.

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
        # Embedded as "description":"..." in a JSON blob in the HTML.
        # The page contains MANY "description" fields (og:meta, site config,
        # related items) — verifying the wrong one would silently pass a
        # broken phone. Anchor on the item JSON ('"item":{') when present;
        # otherwise take the longest candidate (item descriptions are seller
        # free-text, meta descriptions are short marketing strings).
        raw_desc = ""
        item_anchor = text.find('"item":{')
        if item_anchor >= 0:
            m = re.search(r'"description":"((?:[^"\\]|\\.)*)"', text[item_anchor:])
            if m:
                raw_desc = m.group(1)
        if not raw_desc:
            candidates = re.findall(r'"description":"((?:[^"\\]|\\.)*)"', text)
            if candidates:
                raw_desc = max(candidates, key=len)
        if raw_desc:
            try:
                description = _json.loads('"' + raw_desc + '"')  # unescape JSON string
            except Exception:
                description = raw_desc.replace("\\n", "\n").replace('\\"', '"')

        # ── Storage attribute ────────────────────────────────────────────
        # Vinted sellers often set the storage size only as a platform
        # attribute, never in the free text. The attribute renders as e.g.
        # "256 GB" in the page. Accept it ONLY when the whole page names
        # exactly one plausible size — multiple sizes could come from
        # related-item widgets and must not be guessed from.
        if description and not re.search(r'\b\d{2,4}\s?(?:gb|go)\b|\b\d\s?tb\b',
                                         description, re.IGNORECASE):
            page_sizes = {
                int(m) for m in re.findall(r'\b(\d{2,4})\s?(?:gb|go)\b', text, re.IGNORECASE)
            } & {16, 32, 64, 128, 256, 512}
            page_sizes |= {
                int(m) * 1024 for m in re.findall(r'\b(\d)\s?tb\b', text, re.IGNORECASE)
            } & {1024}
            if len(page_sizes) == 1:
                description += f"\n[Vinted-Attribut: {page_sizes.pop()} GB]"

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


_LOGGER_CD = __import__("logging").getLogger(__name__)


def _image_to_b64(image_url: str) -> str | None:
    """Download an image and return base64 data URI."""
    if not image_url:
        return None
    try:
        import base64
        from curl_cffi.requests import Session as CurlSession
        # Referer must match the image CDN to avoid 403s
        if "vinted." in image_url:
            referer = "https://www.vinted.de/"
        elif "kleinanzeigen.de" in image_url or "ebayimg" in image_url:
            referer = "https://www.kleinanzeigen.de/"
        else:
            referer = "https://www.google.com/"

        # KA CDN (img.kleinanzeigen.de/api/v1/...) requires the same Basic auth
        # as the mobile gateway API — without it the CDN returns HTTP 400.
        extra_headers: dict = {}
        if "img.kleinanzeigen.de/api/v1" in image_url:
            from marketplaces import KA_API_BASIC_TOKEN
            extra_headers["Authorization"] = f"Basic {KA_API_BASIC_TOKEN}"

        _LOGGER_CD.debug("Image DL — url=%s referer=%s auth=%s",
                         image_url[:80], referer, "yes" if extra_headers else "no")
        import time as _time
        _t0 = _time.monotonic()
        with CurlSession(impersonate="chrome120") as s:
            r = s.get(image_url, timeout=10, headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "image/*,*/*",
                "Referer": referer,
                **extra_headers,
            })
            _elapsed = _time.monotonic() - _t0
            mime = r.headers.get("Content-Type", "?").split(";")[0].strip()
            if r.status_code != 200 or not r.content:
                _LOGGER_CD.debug("Image DL FAIL — HTTP %s mime=%s size=%d elapsed=%.2fs — %s",
                                 r.status_code, mime, len(r.content), _elapsed, image_url[:80])
                return None
            if not mime.startswith("image/"):
                _LOGGER_CD.debug("Image DL FAIL — non-image mime=%s HTTP %s — %s",
                                 mime, r.status_code, image_url[:80])
                return None
            b64 = base64.b64encode(r.content).decode()
            _LOGGER_CD.debug("Image DL OK — %d bytes mime=%s b64_len=%d elapsed=%.2fs — %s",
                             len(r.content), mime, len(b64), _elapsed, image_url[:80])
            return f"data:{mime};base64,{b64}"
    except Exception as e:
        _LOGGER_CD.debug("Image DL exception — %s — %s", image_url[:80], e)
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

    # Fetch images + description from listing page.
    # Vinted is NOT fetched here: detail-page requests are rate-limited per IP
    # and must go through the caller's throttled fetcher (profile_monitor
    # Guard 3b). The catalog thumbnail (image_url) is sufficient for vision.
    images: list[str] = []
    fetched_desc = ""
    if listing_url:
        try:
            if "kleinanzeigen.de" in listing_url:
                images, fetched_desc = _fetch_ka_details(listing_url)
            elif "vinted." not in listing_url:
                images = fetch_listing_images(listing_url)
        except Exception:
            pass
    if not images and image_url:
        images = [_to_hires(image_url)]
    if not images:
        return None

    # NVIDIA vision model supports at most 1 image per prompt
    images = images[:1]
    _LOGGER_CD.debug(
        "Vision image list — count=%d fetched_desc_len=%d img=%s",
        len(images), len(fetched_desc), (images[0][:80] if images else "–"),
    )

    # Use fetched description if caller didn't provide one
    if not description and fetched_desc:
        description = fetched_desc

    # Cache key: title + description + first image URL
    cache_key = _assess_key(title, description, extra=(images[0] if images else ""))
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
        "water damage, device not turning on, needs repair, display defect, touch not working, "
        "camera broken, SIM not recognized, 'für Bastler', 'Ersatzteile'\n"
        "battery_ok: false only if description explicitly states battery <81% health\n"
        "has_box: true if original box/OVP mentioned\n"
        "has_cable: true if original cable/charger included\n\n"
        "CRITICAL RULES — apply these before scoring:\n"
        "1. Photo overrides text — if photo shows damage not mentioned in text, use lower condition\n"
        "2. When in doubt, grade ONE level lower (conservative = you lose less money)\n"
        "3. 'Kleinere Mängel' / 'kleiner Kratzer' with photo showing heavy wear → grade what you SEE\n"
        "4. Only one photo or blurry/angled photo hiding the screen → risk = 'hoch'\n"
        "5. 'Für Bastler' / 'Ersatzteile' / 'startet manchmal nicht' → functional=false\n"
        "6. Price suspiciously low (<50% market) without explanation → risk = 'hoch'\n"
        "7. Phrases like 'hat einen kleinen Haarriss' or 'minimaler Riss' still mean condition=0\n\n"
        "EXAMPLES of tricky listings (learn these patterns):\n"
        "- 'Top Zustand' but photo shows scratched frame → condition=1, risk='mittel'\n"
        "- 'Kleinere Gebrauchsspuren' + single blurry photo → risk='hoch'\n"
        "- 'Gerät hat minimalen Haarriss am Glas' → condition=0, functional=false\n"
        "- 'Akku bei 79%' → battery_ok=false\n"
        "- 'Gelegentliche Abstürze' → functional=false\n"
        "- 'Display hat leichte Kratzer, Rest einwandfrei' + photo confirms → condition=2\n\n"
        "risk: your confidence that buying this BLIND (sight unseen, with real money) is safe:\n"
        "  'niedrig' = clearly clean working phone, multiple photos, text+photo consistent\n"
        "  'mittel'  = probably fine but some uncertainty (few photos, vague condition text)\n"
        "  'hoch'    = red flags: hidden damage possible, contradictions, too cheap, vague, few photos\n"
        "reason: ONE short German sentence (max 12 words) explaining your main concern.\n\n"
        "Reply with ONLY a JSON object, no text before or after:\n"
        '{"condition": <0-5>, "functional": <true/false>, '
        '"battery_ok": <true/false>, "has_box": <true/false>, "has_cable": <true/false>, '
        '"risk": "<niedrig/mittel/hoch>", "reason": "<kurze Begründung>"}'
    )})

    img_size_kb = len(content[0].get("image_url", {}).get("url", "")) // 1024 if content else 0
    _LOGGER_CD.debug(
        "Vision CALL — model=%s images_in_content=%d payload_kb~=%d title=%r",
        _VISION_MODEL, sum(1 for c in content if c.get("type") == "image_url"),
        img_size_kb, title[:50],
    )
    import time as _time
    _v0 = _time.monotonic()
    try:
        from openai import OpenAI
        client = OpenAI(base_url="https://integrate.api.nvidia.com/v1", api_key=api_key, timeout=40, max_retries=0)
        resp = client.chat.completions.create(
            model=_VISION_MODEL,
            messages=[{"role": "user", "content": content}],
            temperature=0,
            max_tokens=160,
            stream=False,
        )
        _v_elapsed = _time.monotonic() - _v0
        text = resp.choices[0].message.content.strip()
        _LOGGER_CD.debug("Vision RAW (%.2fs) — response=%s", _v_elapsed, text[:300])
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            _LOGGER_CD.warning("Vision no JSON (%.2fs) — title=%r response=%s",
                               _v_elapsed, title[:40], text[:200])
            return None
        raw = _json.loads(m.group())
        risk = str(raw.get("risk", "")).strip().lower()
        result = {
            "condition":  max(0, min(5, int(raw.get("condition", COND_GOOD)))),
            "functional": bool(raw.get("functional", True)),
            "battery_ok": bool(raw.get("battery_ok", True)),
            "has_box":    bool(raw.get("has_box", False)),
            "has_cable":  bool(raw.get("has_cable", False)),
            "risk":       risk if risk in ("niedrig", "mittel", "hoch") else "mittel",
            "reason":     str(raw.get("reason", "")).strip()[:120],
        }
        _LOGGER_CD.debug(
            "Vision PARSED (%.2fs) — cond=%s func=%s bat=%s risk=%s reason=%s — %r",
            _v_elapsed, result["condition"], result["functional"],
            result["battery_ok"], result["risk"], result["reason"], title[:40],
        )
        _assess_cache[cache_key] = result
        return result
    except Exception as e:
        _LOGGER_CD.warning("Vision FAIL (%.2fs) — %r — %s: %s",
                           _time.monotonic() - _v0, title[:40], type(e).__name__, str(e)[:300])
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
    rebuy_prices: dict | None = None,
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

    matched = matched_buyback_price(
        condition_score, zoxs_prices, wkfs_prices, clevertronic_prices, rebuy_prices
    )
    buyback = best_buyback_price(matched)

    if buyback is None or listing_price <= 0:
        return False, None, None

    ebay_fees = listing_price * fee_rate + fee_fixed
    net_profit = buyback - listing_price - shipping_cost - ebay_fees
    roi = float(net_profit / listing_price) if listing_price else None

    if net_profit < min_profit or (roi is not None and roi < min_roi):
        return False, net_profit, roi

    return True, net_profit, roi
