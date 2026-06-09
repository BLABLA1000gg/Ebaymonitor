"""
Extract product specs (model, storage GB, color) from listing titles.
Uses DeepSeek API when a key is configured, otherwise falls back to regex heuristics.
"""
from __future__ import annotations

import json
import re

import requests

DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

# Known iPhone / Samsung / Pixel model strings, longest first so greedier match wins
_IPHONE_MODELS = [
    "iphone 15 pro max", "iphone 15 pro", "iphone 15 plus", "iphone 15",
    "iphone 14 pro max", "iphone 14 pro", "iphone 14 plus", "iphone 14",
    "iphone 13 pro max", "iphone 13 pro", "iphone 13 mini", "iphone 13",
    "iphone 12 pro max", "iphone 12 pro", "iphone 12 mini", "iphone 12",
    "iphone 11 pro max", "iphone 11 pro", "iphone 11",
    "iphone se",
    "iphone xs max", "iphone xs", "iphone xr", "iphone x",
]
_SAMSUNG_MODELS = [
    "galaxy s24 ultra", "galaxy s24+", "galaxy s24",
    "galaxy s23 ultra", "galaxy s23+", "galaxy s23",
    "galaxy s22 ultra", "galaxy s22+", "galaxy s22",
    "galaxy a55", "galaxy a54", "galaxy a53",
    "galaxy z fold5", "galaxy z fold4", "galaxy z fold3",
    "galaxy z flip5", "galaxy z flip4", "galaxy z flip3",
]
_PIXEL_MODELS = [
    "pixel 9 pro xl", "pixel 9 pro fold", "pixel 9 pro", "pixel 9",
    "pixel 8 pro", "pixel 8a", "pixel 8",
    "pixel 7 pro", "pixel 7a", "pixel 7",
]


def extract_specs(title: str, description: str = "", api_key: str = "") -> dict:
    """
    Return {'model': str|None, 'storage_gb': int|None, 'color': str|None}.
    Tries DeepSeek when api_key is set, falls back to regex.
    """
    if api_key:
        result = _deepseek_extract(title, description, api_key)
        if result:
            return result
    return _heuristic_extract(title)


def build_search_query(base_keyword: str, specs: dict) -> str:
    """
    Build a refined search query for buyback sites.
    E.g. base='iPhone 12', specs={'storage_gb': 128} → 'iPhone 12 128GB'
    """
    parts = [base_keyword.strip()]
    if specs.get("storage_gb"):
        parts.append(f"{specs['storage_gb']}GB")
    return " ".join(parts)


# ------------------------------------------------------------------ #
# DeepSeek                                                            #
# ------------------------------------------------------------------ #

def _deepseek_extract(title: str, description: str, api_key: str) -> dict | None:
    prompt = (
        "Extract product specs from this marketplace listing title. "
        "Return ONLY a JSON object with keys: model (string or null), "
        "storage_gb (integer or null), color (string or null). No prose.\n\n"
        f"Title: {title}\n"
        + (f"Description: {description[:300]}" if description else "")
    )
    try:
        r = requests.post(
            DEEPSEEK_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": DEEPSEEK_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 80,
                "temperature": 0,
            },
            timeout=8,
        )
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"].strip()
        match = re.search(r"\{.*?\}", text, re.DOTALL)
        if match:
            data = json.loads(match.group())
            return {
                "model": data.get("model") or None,
                "storage_gb": int(data["storage_gb"]) if data.get("storage_gb") else None,
                "color": data.get("color") or None,
            }
    except Exception:
        pass
    return None


# ------------------------------------------------------------------ #
# Heuristic fallback                                                  #
# ------------------------------------------------------------------ #

def _heuristic_extract(title: str) -> dict:
    t = title.lower()

    # Storage GB  →  first match of e.g. "128 gb", "128gb"
    m = re.search(r"(\d+)\s*gb", t)
    storage_gb = int(m.group(1)) if m else None

    # Model
    model = None
    for candidate in _IPHONE_MODELS + _SAMSUNG_MODELS + _PIXEL_MODELS:
        if candidate in t:
            model = candidate.title()
            break

    # Color (very rough, German + English)
    color = None
    for col in ["schwarz", "weiß", "weiss", "silber", "gold", "blau", "rot",
                "black", "white", "silver", "blue", "red", "green", "purple",
                "midnight", "starlight", "space gray", "graphite"]:
        if col in t:
            color = col
            break

    return {"model": model, "storage_gb": storage_gb, "color": color}
