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
    Return True if the listing is an actual smartphone/device,
    False if it's a case, charger, accessory, spare part, etc.
    """
    t = (title + " " + description[:300]).lower()
    for kw in _ACCESSORY_KEYWORDS:
        if kw in t:
            return False
    return True


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
) -> int:
    """
    Return normalized condition score (0-5) from text signals.
    Priority: platform condition field > title+description regex.
    """
    # 1) eBay/platform native condition field (most reliable)
    if platform_condition:
        key = platform_condition.strip().lower()
        if key in _EBAY_PLATFORM_COND:
            return _EBAY_PLATFORM_COND[key]

    # 2) Regex over title (weighted higher) + description
    text = f"{title} {title} {description[:400]}".lower()
    best_score: int | None = None

    for score, keywords in _PATTERNS:
        for kw in keywords:
            if kw in text:
                # Take the most specific match (first encountered with highest priority)
                if best_score is None:
                    best_score = score
                break  # one keyword matched this tier, move on

    return best_score if best_score is not None else COND_GOOD  # default to "Gut"


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
# Image condition check via NVIDIA vision (optional)                  #
# ------------------------------------------------------------------ #

_IMAGE_CACHE: dict[str, int] = {}


def check_image_condition(
    image_url: str,
    api_key: str,
    provider: str = "nvidia",
) -> int | None:
    """
    Use AI vision to estimate condition from listing image.
    Returns normalized score or None if unavailable.
    Only NVIDIA NIM (meta/llama-3.2-11b-vision-instruct) supported for now.
    """
    if not image_url or not api_key or provider == "none":
        return None
    if image_url in _IMAGE_CACHE:
        return _IMAGE_CACHE[image_url]

    try:
        from openai import OpenAI
        client = OpenAI(
            base_url="https://integrate.api.nvidia.com/v1",
            api_key=api_key,
        )
        resp = client.chat.completions.create(
            model="meta/llama-3.2-11b-vision-instruct",
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": image_url},
                    },
                    {
                        "type": "text",
                        "text": (
                            "Rate the physical condition of this device in the image. "
                            "Reply with ONLY a single digit: "
                            "0=broken/cracked, 1=heavy scratches/dents, "
                            "2=visible wear, 3=minor scratches, 4=like new, 5=brand new. "
                            "One digit only."
                        ),
                    },
                ],
            }],
            temperature=0.1,
            max_tokens=5,
            stream=False,
        )
        text = resp.choices[0].message.content.strip()
        m = re.search(r"[0-5]", text)
        if m:
            score = int(m.group())
            _IMAGE_CACHE[image_url] = score
            return score
    except Exception:
        pass
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
    image_url: str | None = None,
    api_key: str = "",
    provider: str = "none",
    title: str = "",
    description: str = "",
) -> tuple[bool, Decimal | None, float | None]:
    """
    Determine if a listing is worth buying for arbitrage.

    Returns (worth_it, net_profit, roi_pct).
    If ROI looks good AND image_url+api_key provided → also checks image condition.
    """
    # Hard block: accessories, cases, chargers etc. are never worth it
    if title and not is_actual_device(title, description):
        return False, None, None

    matched = matched_buyback_price(condition_score, zoxs_prices, wkfs_prices, clevertronic_prices)
    buyback = best_buyback_price(matched)

    if buyback is None or listing_price <= 0:
        return False, None, None

    ebay_fees = listing_price * fee_rate + fee_fixed
    net_profit = buyback - listing_price - shipping_cost - ebay_fees
    roi = float(net_profit / listing_price) if listing_price else None

    # Basic ROI check
    if net_profit < min_profit or (roi is not None and roi < min_roi):
        return False, net_profit, roi

    # Image check: verify condition isn't worse than text suggests
    if image_url and api_key and provider != "none":
        img_score = check_image_condition(image_url, api_key, provider)
        if img_score is not None and img_score < condition_score - 1:
            # Image shows worse condition → recalculate with downgraded score
            downgraded = max(COND_BROKEN, condition_score - 1)
            matched2 = matched_buyback_price(downgraded, zoxs_prices, wkfs_prices, clevertronic_prices)
            buyback2 = best_buyback_price(matched2)
            if buyback2:
                net_profit = buyback2 - listing_price - shipping_cost - ebay_fees
                roi = float(net_profit / listing_price)
                if net_profit < min_profit or roi < min_roi:
                    return False, net_profit, roi

    return True, net_profit, roi
