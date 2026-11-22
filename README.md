# web-scraper-toolkit

Two small, dependency-light tools I actually run in production:

- **`watchd/`** — a self-hosted page watcher. Polls a URL on an interval, extracts one
  CSS selector, stores the value history in SQLite, and fires a webhook when it changes.
  Single file, stdlib + BeautifulSoup, `/health` endpoint, ~1.25 vCPU is plenty.
- **`scraper/`** — a Playwright CLI scraper that handles both static HTML and
  JavaScript-rendered, paginated listings. Outputs CSV or JSON.

Both are deliberately boring: no framework, no database server, no cloud dependency.

## watchd

```bash
pip install beautifulsoup4
python3 watchd/app.py                 # listens on 0.0.0.0:8790

curl -X POST localhost:8790/api/monitors   -d '{"name":"book price","url":"https://books.toscrape.com/","selector":".price_color","interval_s":300}'
curl localhost:8790/api/events?limit=20
```

Routes: `GET /health`, `GET /` (dashboard, 30 s refresh), `GET|POST /api/monitors`,
`DELETE /api/monitors/<id>`, `GET /api/events`. Set `config.json` →
`{"webhook_url": "..."}` to POST change events anywhere (Slack, Telegram bridge, your API).

Verified on a 1.25 vCPU / 4 GiB box: two monitors, real values captured, change events
written, process self-restarts under `tmux` + a keepalive loop.

## scraper

```bash
cd scraper && npm i playwright && npx playwright install chromium

node scrape.js --url "https://quotes.toscrape.com/js/page/{n}/" --pages 2   --item ".quote" --fields "text:.text,author:.author" --out quotes.csv
```

Real run: 20 rows from a JS-rendered, paginated page in ~2.5 s; 40 rows from a static
listing into JSON.

## Licence

MIT.
