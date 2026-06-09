# Marketplace Arbitrage Monitor

An automated monitor for eBay, Kleinanzeigen and Vinted that identifies profitable resale opportunities by comparing active listing prices against buyback portal prices (ZOXS, WirKaufens, Clevertronic). Built for phone flipping — finds underpriced listings, assesses their condition with AI, and calculates net profit after fees and shipping.

## What it does

1. **Monitors** eBay, Kleinanzeigen and Vinted listings based on your search profiles
2. **Detects condition** from listing title + description using AI (functional state, battery health, accessories)
3. **Queries buyback portals** (ZOXS, WirKaufens, Clevertronic) for current Ankaufpreise
4. **Dynamically answers** condition wizard questions on each buyback portal based on the AI assessment
5. **Calculates ROI** after eBay fees and shipping; flags listings worth buying
6. **Optionally checks listing images** with a vision model when ROI looks promising
7. **Shows everything** in a local web dashboard with price history, charts and deal scores

## Supported platforms

| Source (buy from) | Buyback portals (sell to) |
|---|---|
| eBay | ZOXS |
| Kleinanzeigen | WirKaufens |
| Vinted | Clevertronic |

## AI features

- **Batch condition assessment** — one API call per scan evaluates all listings for condition score (0–5), functional state, battery health and accessories (box, cable)
- **Portal question automation** — Clevertronic / ZOXS / WirKaufens condition wizards are answered automatically based on the per-listing assessment
- **Vision check** — if ROI is promising, up to 6 listing images are fetched and rated by a vision model; condition score is downgraded if photos look worse than the description claims
- **Spec extraction** — storage capacity is inferred from listing titles to pick the correct buyback product

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

A listing is flagged as **WORTH IT** when profit > 0 and ROI exceeds the configured threshold. The correct buyback price tier is selected based on the AI-assessed condition of each individual listing.

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

- eBay may hide final Best Offer amounts.
- Kleinanzeigen and Vinted have no public sold-results search — analytics are eBay-only.
- Demand estimates depend on the visible sold-result window.
- Proxy quality and legality are the operator's responsibility.
