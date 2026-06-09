# Advanced Marketplace Monitor

A persistent eBay, Kleinanzeigen and Vinted monitor with a local web dashboard, database-backed search profiles, price history, filters and per-profile proxy support. eBay profiles additionally provide robust sold-price analytics, deal scoring and demand estimates.

## Highlights

- Browser-based configuration and multiple independent profiles
- HTTP, HTTPS, SOCKS5 and SOCKS5h proxy support per profile
- Active searches for eBay, Kleinanzeigen and Vinted
- Separate sold/completed searches for eBay
- IQR plus MAD outlier filtering
- Deal scores relative to the robust sold median
- Sales/month, sell-through, demand and estimated days to sell
- SQLite history, CSV exports and trend charts

## Install

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium --with-deps
```

## Start

```bash
python dashboard.py
```

Open `http://127.0.0.1:5000`, create profiles and optionally configure a proxy. Scan once or continuously:

```bash
python profile_monitor.py --once
python profile_monitor.py
```

## eBay bot detection

eBay uses Akamai bot-detection that blocks standard HTTP clients and headless browsers. The monitor works around this by running Chromium in its new headless mode (`--headless=new`, Chrome 112+), which is indistinguishable from a real browser session. No visible window appears.

The browser first loads the eBay homepage to obtain session cookies, then fetches the search page and waits for listings to render in the DOM before reading the HTML. This approach is used automatically for all eBay profiles — no configuration required.

eBay also periodically changes its HTML structure. The parser targets stable semantic elements (`li.s-card`, `span.su-styled-text`) rather than layout-specific class names.

If scanning still fails on a datacenter IP, configure a residential proxy per profile.

## Proxy support

Accepted proxy URL formats:

```text
http://host:8080
https://host:8443
http://username:password@host:8080
socks5://host:1080
socks5h://username:password@host:1080
```

Use `socks5h` when DNS lookups should also go through the proxy. The configured proxy handles both HTTP and HTTPS marketplace requests for that profile. Leave the field empty for a direct connection.

Credentials are stored locally in SQLite. Restrict access to the database and never commit it. Logs mask credentials, for example `socks5h://***:***@host:1080`.

## Profile options

Each profile has its own search URL, required/excluded keywords, price range, currency, sold-history window, optional proxy and enabled state.

## Analytics

For eBay profiles, the scanner adds `LH_Sold=1` and `LH_Complete=1` for sold-price analysis. IQR fences and median absolute deviation remove extreme accessory, broken-item and price outliers. Samples below four prices remain untouched.

A Deal Score of `50` equals the sold median. Lower active prices score higher. Demand uses visible sold results per month compared with active supply; estimated sale duration is an estimate, not a guarantee.

## Data and exports

The default database is `ebay_monitor.db`. Change it with `DATABASE_PATH`. `CHECK_INTERVAL_SECONDS` controls scanning and must be at least 30 seconds.

```bash
python monitor.py --export
```

Exports: `listings.csv`, `price_history.csv`, and `sold_statistics.csv`.

## Limitations

- eBay may hide final Best Offer amounts.
- Public HTML and sold-query behavior can change.
- Proxy quality and legality are the operator's responsibility.
- eBay may block datacenter or heavily reused proxy IPs — use a residential proxy in that case.
- Demand estimates depend on the visible sold-result window.
- Kleinanzeigen and Vinted have no equivalent public sold-results search, so sold-price and demand analytics are eBay-only.
