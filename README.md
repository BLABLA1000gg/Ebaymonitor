# Advanced eBay Monitor

A persistent eBay monitor with SQLite history, CSV exports, filters, sold-price statistics and Discord alerts.

> The monitor reads eBay's public HTML. eBay can change the page structure or block automated/cloud traffic. Use a reasonable interval and follow eBay's terms and policies.

## Features

- Monitor one or multiple active eBay search URLs
- Automatically run a separate sold/completed search for each configured URL
- Calculate average sold price, sold-price median, minimum, maximum and result count
- Store listings and price history in SQLite
- Detect new active listings, price drops and price increases
- Export listings, price history and sold statistics to CSV
- Include/exclude keywords, minimum/maximum price and currency filters
- Capture condition, shipping, location and image when available
- Rich Discord alerts and optional sold-market summaries
- Safe first scan, one-shot mode and export-only mode

## How sold prices work

For each configured active search URL, the monitor creates a second URL with:

- `LH_Sold=1`
- `LH_Complete=1`

Only results returned by that sold/completed search are used for average sold price, median and range. Active listings are used only for new-listing and price-change monitoring. Sold results are not inserted into the active-listing history.

The shown value is the public sold price displayed by eBay. It may not reveal a privately negotiated Best Offer amount when eBay hides that final amount.

## Installation

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Quick start

```bash
export EBAY_URL='https://www.ebay.de/sch/i.html?_nkw=macbook&LH_BIN=1'
export DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/...'
export INCLUDE_KEYWORDS='macbook,pro'
export EXCLUDE_KEYWORDS='defekt,ersatzteile'
export MIN_PRICE='200'
export MAX_PRICE='900'
export CURRENCY='EUR'
export CSV_DIRECTORY='exports'
export NOTIFY_STATISTICS='true'
python monitor.py
```

PowerShell uses `$env:NAME = 'value'` for the same variables.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `EBAY_URL` | required | One active eBay search URL |
| `EBAY_URLS` | empty | Multiple active URLs separated by `|`; overrides `EBAY_URL` |
| `DISCORD_WEBHOOK_URL` | empty | Optional Discord webhook |
| `DATABASE_PATH` | `ebay_monitor.db` | SQLite database location |
| `CSV_DIRECTORY` | empty | Export CSV files after every successful scan |
| `CHECK_INTERVAL_SECONDS` | `300` | Poll interval, minimum 30 seconds |
| `INCLUDE_KEYWORDS` | empty | Comma-separated words; every word must match |
| `EXCLUDE_KEYWORDS` | empty | Comma-separated words; any match rejects the result |
| `MIN_PRICE` | empty | Minimum active and sold result price |
| `MAX_PRICE` | empty | Maximum active and sold result price |
| `CURRENCY` | empty | `EUR`, `USD` or `GBP` |
| `NOTIFY_EXISTING` | `false` | Notify for active items on the first scan |
| `NOTIFY_PRICE_INCREASES` | `false` | Notify when a known active item's price rises |
| `NOTIFY_STATISTICS` | `false` | Send sold average/median/range after every scan |
| `LOG_LEVEL` | `INFO` | Python log level |

Keyword matching is case-insensitive and checks title, condition and location. Sold statistics are calculated after these filters. Shipping is stored separately and is not included in sold prices.

## Commands

```bash
python monitor.py           # continuous monitoring
python monitor.py --once    # one active scan plus one sold-statistics scan
python monitor.py --export  # export the existing database
```

Exports:

- `listings.csv`: latest active state of every known listing
- `price_history.csv`: observed active-listing price changes
- `sold_statistics.csv`: sold average, median, range and count per URL/keyword scan

## Database

- `listings`: current active-listing state
- `price_history`: initial and changed active prices
- `search_statistics`: snapshots calculated exclusively from sold/completed results

Prices use exact decimal strings to avoid floating-point rounding errors.

## Operational notes

- Keep the interval at five minutes or longer for normal use.
- Back up the SQLite database if long-term history matters.
- Cloud hosts may receive HTTP 403, 429, 500 or 503 while a home connection works.
- eBay HTML and sold-search behavior can change, requiring selector or query updates.
