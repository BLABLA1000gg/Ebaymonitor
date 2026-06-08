# Advanced eBay Market Monitor

A persistent eBay monitor with a local web dashboard, database-backed search profiles, robust sold-price analytics, deal scoring and demand estimates.

## Highlights

- Browser-based configuration instead of per-search environment variables
- Multiple independent profiles with URLs, keywords, exclusions and price ranges
- Separate active and sold/completed searches
- Robust sold-price median and average using IQR plus MAD outlier filtering
- Deal score comparing each active price with the filtered sold median
- Sales-per-month estimate, sell-through rate, demand level and estimated days to sell
- SQLite history and CSV exports
- Dashboard charts for sold-price and demand trends
- Active listing price history API

## Install

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Dashboard setup

Start the web interface:

```bash
python dashboard.py
```

Open `http://127.0.0.1:5000`, create one or more profiles and configure:

- active eBay search URL
- required keywords
- excluded keywords such as `defekt,zubehoer,ersatzteile`
- minimum and maximum price
- currency
- sold-history window in days

Run all enabled profiles once:

```bash
python profile_monitor.py --once
```

Run continuously using the default five-minute interval:

```bash
python profile_monitor.py
```

Keep the dashboard running in a second terminal to view updated charts and deals.

## Analytics

For every profile the scanner derives a sold search by adding `LH_Sold=1` and `LH_Complete=1`. Sold prices are filtered in two stages:

1. Interquartile range fences remove extreme distribution tails.
2. Median absolute deviation removes values far from the robust center.

Small samples below four prices remain untouched. The dashboard reports how many values were excluded.

### Deal score

A score of `50` means the active price equals the filtered sold median. Prices below the median score above 50; prices above it score below 50. Scores are capped between 0 and 100.

### Demand and sale duration

`sales/month = accepted sold results * 30 / sold-history days`

The sell-through indicator compares estimated monthly sales with active supply. Estimated sale duration divides active supply by monthly sales. These are market estimates, not guarantees; eBay can limit historical sold-result depth.

## Data

The default database is `ebay_monitor.db`. Set `DATABASE_PATH` to use another location. `CHECK_INTERVAL_SECONDS` controls the profile scanner interval and must be at least 30 seconds.

```bash
python monitor.py --export
```

Exports:

- `listings.csv`
- `price_history.csv`
- `sold_statistics.csv`

## Limitations

- eBay may hide the final negotiated Best Offer amount.
- Public HTML and query behavior can change.
- Cloud IP addresses may receive HTTP 403, 429, 500 or 503.
- Demand and sales-per-month are estimates based on visible sold results in the selected window.
