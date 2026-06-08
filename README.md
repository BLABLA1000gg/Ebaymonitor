# Advanced eBay Market Monitor

A persistent eBay monitor with a local web dashboard, database-backed search profiles, robust sold-price analytics, deal scoring, demand estimates and per-profile proxy support.

## Highlights

- Browser-based configuration and multiple independent profiles
- HTTP, HTTPS, SOCKS5 and SOCKS5h proxy support per profile
- Separate active and sold/completed searches
- IQR plus MAD outlier filtering
- Deal scores relative to the robust sold median
- Sales/month, sell-through, demand and estimated days to sell
- SQLite history, CSV exports and trend charts

## Install

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
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

## Proxy support

Accepted proxy URL formats:

```text
http://host:8080
https://host:8443
http://username:password@host:8080
socks5://host:1080
socks5h://username:password@host:1080
```

Use `socks5h` when DNS lookups should also go through the proxy. The configured proxy handles both HTTP and HTTPS eBay requests for that profile. Leave the field empty for a direct connection.

Credentials are stored locally in SQLite. Restrict access to the database and never commit it. Logs mask credentials, for example `socks5h://***:***@host:1080`.

## Profile options

Each profile has its own search URL, required/excluded keywords, price range, currency, sold-history window, optional proxy and enabled state.

## Analytics

The scanner adds `LH_Sold=1` and `LH_Complete=1` for sold-price analysis. IQR fences and median absolute deviation remove extreme accessory, broken-item and price outliers. Samples below four prices remain untouched.

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
- eBay may block datacenter or heavily reused proxy IPs.
- Demand estimates depend on the visible sold-result window.
