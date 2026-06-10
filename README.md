# Marketplace Arbitrage Monitor

An automated monitor for eBay, Kleinanzeigen and Vinted that identifies profitable resale opportunities by comparing active listing prices against buyback portal prices (ZOXS, WirKaufens, Clevertronic). Built for phone flipping — finds underpriced listings, assesses their condition with AI, and calculates net profit after fees and shipping.

## What it does

1. **Monitors** eBay, Kleinanzeigen and Vinted listings based on your search profiles
2. **Detects condition** from listing title, description AND a photo using an AI vision model (functional state, battery health, accessories, defects)
3. **Queries buyback portals** (ZOXS, WirKaufens, Clevertronic) for current Ankaufpreise — per storage variant
4. **Rates blind-buy risk** with the AI (niedrig / mittel / hoch) and skips high-risk listings
5. **Calculates a floor profit** using fixed conservative buyback tiers, after eBay fees and shipping
6. **Flags WORTH IT** only when the listing survives every safety guard and clears the profit threshold
7. **Shows everything** in a local German web dashboard with price history, charts, deal scores and the AI's reasoning per deal

The pipeline is built for **blind buying** — i.e. purchasing flagged listings sight-unseen with real money. Every step errs on the side of skipping rather than risking a bad purchase.

## Supported platforms

| Source (buy from) | Buyback portals (sell to) |
|---|---|
| eBay | ZOXS |
| Kleinanzeigen | WirKaufens |
| Vinted | Clevertronic |

## AI features

- **Vision assessment** — each promising listing is rated from its photo + title + description in one call: condition score (0–5), functional state, battery health, box/cable, plus a blind-buy **risk level** and a short German **reason**
- **Risk gate** — listings the AI rates high-risk (`hoch` — possible hidden damage, contradictions, suspiciously cheap) are skipped automatically; the risk and reason are shown in the dashboard for every deal
- **Multilingual defect detection** — broken-device keywords in German, Dutch, French and Italian are hard-blocked from title and the fetched description
- **Portal question automation** — Clevertronic / ZOXS / WirKaufens condition wizards are answered automatically by the scraper
- **Spec extraction** — storage capacity is inferred from each listing (title authoritative, Vinted attribute fallback) so it is priced against its own variant
- **Conservative-by-design** — when the AI is uncertain or unavailable, grading falls to the lowest realistic tier rather than an optimistic guess

### AI providers

| Provider | Text model | Vision |
|---|---|---|
| NVIDIA NIM (free tier) | `meta/llama-3.1-8b-instruct` | `meta/llama-3.2-11b-vision-instruct` |
| DeepSeek | `deepseek-chat` | — |

API keys are stored only in the local SQLite database — never in code or config files.

## Install

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium --with-deps
```

## Start

```bash
python dashboard.py
```

Open `http://127.0.0.1:8080`, create profiles and configure your search URLs, buyback platforms and AI provider.

Run the scanner:

```bash
python profile_monitor.py        # continuous (uses CHECK_INTERVAL_SECONDS)
python profile_monitor.py --once # single scan and exit
```

## Docker

```bash
docker compose up -d
```

Open `http://localhost:8080`. The database is stored in a Docker volume and persists across restarts.

```bash
docker compose logs -f   # view logs
docker compose down      # stop
```

> **Note:** Hosting on a datacenter/VPS IP may get blocked by eBay. Configure a residential proxy per profile in that case.

## How the profit calculation works

```
Profit = Buyback price − Listing price − Shipping − (Listing price × eBay fee rate)
ROI    = Profit / Listing price
```

A listing is flagged as **WORTH IT** when profit ≥ €15 and ROI ≥ 15 % (defaults).

### Fixed conservative buyback tiers

The buyback price is **always** taken from a fixed conservative condition tier, regardless of the AI-detected condition — because portals routinely downgrade devices on arrival:

| Portal | Tier always used |
|---|---|
| Clevertronic | Gebraucht |
| ZOXS | Gut |
| WirKaufens | In Ordnung |

This makes the calculated profit a **floor**, not an optimistic estimate. The AI condition is still used to *filter* (broken / non-functional / high-risk listings are skipped), never to pick a higher payout tier. Broken devices map to no price at all (portals reject them).

### Per-storage-variant pricing

Buyback prices are fetched for the most common storage sizes in each profile, and every listing is priced against **its own** size. A 128 GB phone is never valued with 256 GB prices; a listing whose size can't be verified is skipped.

## Blind-buy safety guards

Listings pass through a chain of guards; failing any one skips the listing:

- Price floor (€20), exact model-token match (incl. mini/SE/Pro variants), accessory / repair-shop / multi-model blocklist
- Multilingual broken-keyword regex on title and on the **fetched** description
- Kleinanzeigen: full description is mandatory (skipped if it can't be fetched)
- eBay: requires the platform condition field (descriptions live in an iframe and aren't fetched); "Für Ersatzteile oder defekt" is treated as broken
- Vinted: description text is mandatory (photos alone don't count); fetches are throttled to respect rate limits
- AI risk `hoch` → skip; condition 0 / not functional → skip
- Storage size must match an available buyback product

## Configuration

All settings are managed in the dashboard. Key options:

| Setting | Description |
|---|---|
| Search URL | eBay / Kleinanzeigen / Vinted search URL |
| Buyback platforms | Select ZOXS, WirKaufens, Clevertronic per profile |
| AI provider | none / nvidia / deepseek |
| AI API key | Stored in DB only, never in code |
| Shipping cost | Deducted from profit calculation |
| eBay fee rate | Default 12.35 % |
| Proxy | HTTP / HTTPS / SOCKS5 per profile |

## Bot detection

eBay uses Akamai bot detection. The monitor runs Chromium in `--headless=new` mode (Chrome 112+), which passes bot checks without a visible window. Clevertronic uses the same approach. Kleinanzeigen and Vinted use `curl_cffi` with Chrome impersonation.

If scanning fails on a VPS / datacenter IP, configure a residential proxy per profile.

## Analytics (eBay profiles)

- IQR + MAD outlier filtering on sold prices
- Deal score relative to the robust sold-price median
- Sold-per-month, sell-through rate, demand level, estimated days to sell
- Price history charts and CSV exports

## Proxy format

```
http://host:8080
http://user:pass@host:8080
socks5://host:1080
socks5h://user:pass@host:1080
```

Use `socks5h` to route DNS through the proxy. Credentials are masked in logs.

## Data and exports

The default database is `ebay_monitor.db`. Override with `DATABASE_PATH` env var. `CHECK_INTERVAL_SECONDS` controls the scan interval (minimum 30 s).

```bash
python monitor.py --export
```

Exports: `listings.csv`, `price_history.csv`, `sold_statistics.csv`.

## Limitations

- eBay descriptions live in an iframe and are not fetched — eBay listings without a platform condition field are skipped.
- IMEI / iCloud-lock status cannot be determined from a listing — a residual risk for any blind purchase.
- The legacy manual-buyback-URL flow has no per-storage guard; use the platform-checkbox flow for blind buying.
- Vinted rate-limits detail-page fetches per IP (~26 before throttling); a residential proxy increases throughput.
- Kleinanzeigen and Vinted have no public sold-results search — sold analytics are eBay-only.
- Proxy quality and legality are the operator's responsibility.

> **macOS / Apple Silicon:** never wrap the scanner in the `timeout` command — the Homebrew `timeout` binary is x86-64 and forces Python under Rosetta, which breaks the `curl_cffi` native backend and makes all fetches return empty.
