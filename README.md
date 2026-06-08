# Advanced eBay Monitor

A persistent eBay search and price monitor with SQLite history, CSV exports, filters, market statistics and Discord alerts.

> The monitor reads eBay's public HTML. eBay can change the page structure or block automated/cloud traffic. Use a reasonable interval and follow eBay's terms and policies.

## Features

- Monitor one or multiple eBay search URLs
- Store every listing in SQLite
- Track first seen, last seen, active status and price history
- Detect new listings, price drops and price increases
- Calculate average asking price, median, minimum and maximum per search URL and keyword filter
- Export current listings, price history and market statistics to CSV
- Include and exclude keywords
- Minimum/maximum price and currency filters
- Capture condition, shipping, location and image when available
- Rich Discord alerts with old price and percentage change
- Optional Discord market-summary messages
- Safe first scan without notification spam
- One-shot mode for cron, Task Scheduler and debugging
- Python 3.10 and 3.12 tests

## Important price-statistics note

The normal active-listing search page shows asking prices. Therefore the calculated value is the **average asking price**, not a confirmed completed-sale price. For a real average sale price, the configured eBay URL must itself point to sold/completed listings where that filter is available.

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
| `NOTIFY_STATISTICS` | `false` | Send average/median/range summary after every scan |
| `LOG_LEVEL` | `INFO` | Python log level |

Keyword matching is case-insensitive and checks title, condition and location. Statistics are calculated after all configured filters, so they represent the selected URL and keywords. Shipping is stored separately and is not included in prices.

## Commands

```bash
python monitor.py           # continuous monitoring
python monitor.py --once    # exactly one scan
python monitor.py --export  # export the existing database without fetching eBay
```

The export creates:

- `listings.csv`: latest state of each known listing
- `price_history.csv`: every observed initial price and price change
- `search_statistics.csv`: average, median, minimum, maximum and count per search scan

## Database

- `listings` stores the current state and whether an item was present in the latest scan.
- `price_history` receives a row when an item is discovered or its parsed price changes.
- `search_statistics` stores one market snapshot per URL and scan, identified by its keyword filter.

Prices are stored as exact decimal strings to avoid floating-point rounding errors.

## Operational notes

- Prefer eBay search URLs with category, condition, location, buying-format and, when desired, sold-item filters already applied.
- Keep the interval at five minutes or longer for normal use.
- Back up the SQLite file if long-term price history matters.
- A cloud host may receive HTTP 403, 429, 500 or 503 even when the same URL works from a home connection.
- HTML selectors are covered by fixture-style tests but may need updates after an eBay redesign.
