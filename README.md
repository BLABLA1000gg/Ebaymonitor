# Advanced eBay Monitor

A persistent eBay search and price monitor with SQLite history, CSV exports, filters and Discord alerts.

> The monitor reads eBay's public HTML. eBay can change the page structure or block automated/cloud traffic. Use a reasonable interval and follow eBay's terms and policies.

## Features

- Monitor one or multiple eBay search URLs
- Store every listing in SQLite
- Track first seen, last seen, active status and price history
- Detect new listings, price drops and price increases
- Export current listings and complete price history to CSV
- Include and exclude keywords
- Minimum/maximum price and currency filters
- Capture condition, shipping, location and image when available
- Rich Discord alerts with old price and percentage change
- Safe first scan without notification spam
- One-shot mode for cron, Task Scheduler and debugging
- Python 3.10 and 3.12 tests

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
python monitor.py
```

PowerShell uses `$env:NAME = 'value'` for the same variables.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `EBAY_URL` | required | One eBay search URL |
| `EBAY_URLS` | empty | Multiple URLs separated by `|`; overrides `EBAY_URL` |
| `DISCORD_WEBHOOK_URL` | empty | Optional Discord webhook |
| `DATABASE_PATH` | `ebay_monitor.db` | SQLite database location |
| `CSV_DIRECTORY` | empty | Export CSV files after every successful scan |
| `CHECK_INTERVAL_SECONDS` | `300` | Poll interval, minimum 30 seconds |
| `INCLUDE_KEYWORDS` | empty | Comma-separated words; every word must match |
| `EXCLUDE_KEYWORDS` | empty | Comma-separated words; any match rejects the listing |
| `MIN_PRICE` | empty | Minimum parsed item price |
| `MAX_PRICE` | empty | Maximum parsed item price |
| `CURRENCY` | empty | `EUR`, `USD` or `GBP` |
| `NOTIFY_EXISTING` | `false` | Notify for matching items on the first scan |
| `NOTIFY_PRICE_INCREASES` | `false` | Notify when a known item's price rises |
| `LOG_LEVEL` | `INFO` | Python log level |

Keyword matching is case-insensitive and checks title, condition and location. Shipping is stored separately and is not included in `MIN_PRICE`/`MAX_PRICE`.

## Commands

Run continuously:

```bash
python monitor.py
```

Run exactly one scan:

```bash
python monitor.py --once
```

Export an existing database without fetching eBay:

```bash
python monitor.py --export
```

The export creates:

- `listings.csv`: latest state of each known listing
- `price_history.csv`: every observed initial price and price change

## Database

`listings` stores the current state and whether an item was present in the latest scan. `price_history` receives a row when an item is first discovered or its parsed price changes. Prices are stored as exact decimal strings to avoid floating-point rounding errors.

## Operational notes

- Prefer eBay search URLs with the desired category, condition, location and buying-format filters already applied.
- Keep the interval at five minutes or longer for normal use.
- Back up the SQLite file if long-term price history matters.
- A cloud host may receive HTTP 403, 429, 500 or 503 even when the same URL works from a home connection.
- HTML selectors are covered by fixture-style tests but may need updates after an eBay redesign.
