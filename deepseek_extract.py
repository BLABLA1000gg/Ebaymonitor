"""
Extract product specs (model, storage GB, color) from listing titles.

Cost-optimisation strategy (in order):
  1. In-memory cache  — same title never hits the API twice per process lifetime
  2. Heuristic first  — regex finds GB/model in ~95 % of German phone listings
  3. Batch DeepSeek   — only titles where heuristic found nothing are bundled
                        into ONE API call (up to 50 titles per request)
  4. Prefix cache key — 64-char title prefix is enough; avoids long-tail misses
"""
from __future__ import annotations

import json
import re
from typing import Sequence

import requests

# DeepSeek
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL   = "deepseek-chat"

# NVIDIA NIM (OpenAI-compatible)
NVIDIA_BASE_URL  = "https://integrate.api.nvidia.com/v1"
NVIDIA_MODEL     = "meta/llama-3.1-8b-instruct"

# ------------------------------------------------------------------ #
# In-memory cache  (title → specs dict)                              #
# ------------------------------------------------------------------ #
_cache: dict[str, dict] = {}


def _cache_key(title: str) -> str:
    return title.strip().lower()[:64]


# ------------------------------------------------------------------ #
# Model name lists (longest match wins)                               #
# ------------------------------------------------------------------ #
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

_ALL_MODELS = _IPHONE_MODELS + _SAMSUNG_MODELS + _PIXEL_MODELS

_COLORS = [
    "schwarz", "weiß", "weiss", "silber", "gold", "blau", "rot",
    "black", "white", "silver", "blue", "red", "green", "purple",
    "midnight", "starlight", "space gray", "graphite",
]


# ------------------------------------------------------------------ #
# Public API                                                          #
# ------------------------------------------------------------------ #

def extract_specs(
    title: str,
    description: str = "",
    api_key: str = "",
    provider: str = "none",
) -> dict:
    """
    Return {'model': str|None, 'storage_gb': int|None, 'color': str|None}.

    Heuristic runs first; LLM is only called if storage_gb is still
    unknown AND an api_key + provider are configured. Results are cached.
    provider: "none" | "deepseek" | "nvidia"
    """
    key = _cache_key(title)
    if key in _cache:
        return _cache[key]

    result = _heuristic_extract(title)

    if api_key and provider != "none" and result["storage_gb"] is None:
        llm = _llm_single(title, description, api_key, provider)
        if llm:
            result = {**result, **{k: v for k, v in llm.items() if v is not None}}

    _cache[key] = result
    return result


def extract_specs_batch(
    titles: Sequence[str],
    api_key: str = "",
    provider: str = "none",
) -> list[dict]:
    """
    Process many titles efficiently:
    - Return cached results immediately
    - Run heuristic on all uncached titles
    - Bundle only the *still-ambiguous* titles into ONE LLM call
    Returns a list in the same order as `titles`.
    provider: "none" | "deepseek" | "nvidia"
    """
    results: list[dict | None] = [None] * len(titles)
    need_llm: list[int] = []

    for i, title in enumerate(titles):
        key = _cache_key(title)
        if key in _cache:
            results[i] = _cache[key]
            continue
        h = _heuristic_extract(title)
        _cache[key] = h
        results[i] = h
        if h["storage_gb"] is None and api_key and provider != "none":
            need_llm.append(i)

    # Batch call — one request for all ambiguous titles
    if need_llm and api_key and provider != "none":
        batch_titles = [titles[i] for i in need_llm]
        batch_results = _llm_batch(batch_titles, api_key, provider)
        for idx, llm in zip(need_llm, batch_results):
            if llm:
                merged = {**results[idx], **{k: v for k, v in llm.items() if v is not None}}
                results[idx] = merged
                _cache[_cache_key(titles[idx])] = merged

    return results  # type: ignore[return-value]


def build_search_query(base_keyword: str, specs: dict) -> str:
    """
    Build refined search query: 'iPhone 12' + {'storage_gb': 128} → 'iPhone 12 128GB'
    """
    parts = [base_keyword.strip()]
    if specs.get("storage_gb"):
        parts.append(f"{specs['storage_gb']}GB")
    return " ".join(parts)


# ------------------------------------------------------------------ #
# Heuristic (no API cost)                                             #
# ------------------------------------------------------------------ #

def _heuristic_extract(title: str) -> dict:
    t = title.lower()

    m = re.search(r"(\d+)\s*gb", t)
    storage_gb = int(m.group(1)) if m else None

    model = None
    for candidate in _ALL_MODELS:
        if candidate in t:
            model = candidate.title()
            break

    color = None
    for col in _COLORS:
        if col in t:
            color = col
            break

    return {"model": model, "storage_gb": storage_gb, "color": color}


# ------------------------------------------------------------------ #
# LLM single + batch (supports DeepSeek and NVIDIA NIM)              #
# ------------------------------------------------------------------ #

_BATCH_SYSTEM = (
    "You extract product specs from marketplace listing titles. "
    "Return a JSON array — one object per title, same order. "
    'Each object: {"model": str|null, "storage_gb": int|null, "color": str|null}. '
    "Output ONLY the JSON array, no prose."
)


def _llm_single(title: str, description: str, api_key: str, provider: str) -> dict | None:
    prompt = (
        "Extract product specs. Return ONLY JSON: "
        '{"model":...,"storage_gb":...,"color":...}.\n'
        f"Title: {title}"
        + (f"\nDesc: {description[:200]}" if description else "")
    )
    return _call_llm([{"role": "user", "content": prompt}], api_key, provider, max_tokens=60)


def _safe_storage_gb(value) -> int | None:
    """Parse a storage size from arbitrary LLM output.

    The model sometimes returns garbage (e.g. an Apple model number like
    'A2407', a string '128GB', or None). Extract only valid GB values and
    accept only realistic phone storage sizes.
    """
    if value is None:
        return None
    if isinstance(value, int):
        gb = value
    else:
        m = re.search(r"\d+", str(value))
        if not m:
            return None
        gb = int(m.group())
    # Only accept realistic phone storage tiers
    return gb if gb in {16, 32, 64, 128, 256, 512, 1024} else None


def _llm_batch(titles: list[str], api_key: str, provider: str) -> list[dict | None]:
    if not titles:
        return []
    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(titles[:50]))
    raw = _call_llm(
        [{"role": "user", "content": f"Titles:\n{numbered}"}],
        api_key,
        provider,
        system=_BATCH_SYSTEM,
        max_tokens=60 * len(titles),
    )
    if raw is None:
        return [None] * len(titles)
    if isinstance(raw, dict):
        raw = [raw]
    results: list[dict | None] = []
    for item in raw[: len(titles)]:
        if isinstance(item, dict):
            results.append({
                "model": item.get("model") or None,
                "storage_gb": _safe_storage_gb(item.get("storage_gb")),
                "color": item.get("color") or None,
            })
        else:
            results.append(None)
    while len(results) < len(titles):
        results.append(None)
    return results


def _call_llm(
    messages: list[dict],
    api_key: str,
    provider: str,
    system: str | None = None,
    max_tokens: int = 80,
) -> dict | list | None:
    """
    Universal LLM call — routes to DeepSeek or NVIDIA NIM based on provider.
    Returns parsed JSON (dict or list) or None on error.
    """
    full_messages = []
    if system:
        full_messages.append({"role": "system", "content": system})
    full_messages.extend(messages)

    try:
        if provider == "nvidia":
            from openai import OpenAI
            client = OpenAI(base_url=NVIDIA_BASE_URL, api_key=api_key)
            resp = client.chat.completions.create(
                model=NVIDIA_MODEL,
                messages=full_messages,
                temperature=0.1,
                top_p=0.7,
                max_tokens=max_tokens,
                stream=False,
            )
            text = resp.choices[0].message.content.strip()
        else:
            # DeepSeek via requests (no extra lib needed)
            r = requests.post(
                DEEPSEEK_API_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": DEEPSEEK_MODEL, "messages": full_messages,
                      "max_tokens": max_tokens, "temperature": 0},
                timeout=15,
            )
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"].strip()

        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        return json.loads(text)
    except Exception:
        return None
