# eBay Product Monitor

Monitor an eBay search page and send newly appearing listings to a Discord webhook.

> This project uses eBay's public HTML, which can change without notice. Keep the request interval reasonable and follow eBay's terms and policies.

## Requirements

- Python 3.10 or newer
- A Discord webhook URL
- An eBay search URL

## Setup

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Configure the monitor with environment variables. Do not commit webhook URLs to Git.

macOS/Linux:

```bash
export EBAY_URL='https://www.ebay.de/sch/i.html?_nkw=macbook'
export DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/...'
python monitor.py
```

PowerShell:

```powershell
$env:EBAY_URL = 'https://www.ebay.de/sch/i.html?_nkw=macbook'
$env:DISCORD_WEBHOOK_URL = 'https://discord.com/api/webhooks/...'
python monitor.py
```

## Options

- `CHECK_INTERVAL_SECONDS`: Delay between scans. Default: `300`; minimum: `30`.
- `NOTIFY_EXISTING`: Set to `true` to notify for all results on the first scan. Default: `false`.
- `LOG_LEVEL`: Python logging level. Default: `INFO`.

By default, the first scan establishes the known listings without sending a large batch of notifications. Later scans notify only for newly appearing listing links.

## Notes

- Use eBay's filters and sorting options in the search URL.
- A missing title, link, price, or image no longer crashes the monitor.
- Temporary eBay or Discord network failures are logged and retried on the next scan.
