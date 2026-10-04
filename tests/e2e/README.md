# Dashboard browser tests

These drive the real dashboard UI (`supermarket_bot/web/`) in headless Chromium against the
simulated market, so they need no API key and never touch the network.

| File | What it does |
| --- | --- |
| `serve_demo.py` | Starts the demo dashboard on a free `127.0.0.1` port with a 2 s snapshot interval. Prints the URL as its first line of stdout; stops on stdin EOF, SIGTERM or Ctrl-C. |
| `ui_smoke.cjs` | Playwright script (CommonJS). Opens every view, sorts, filters and searches the markets table, opens and closes the detail drawer (button, Esc, backdrop) and checks focus returns, uses the chart range buttons, crosshair and table view, re-analyzes a surge, toggles the theme (and checks it persists), simulates an outage and a revoked-key error, waits for poll cycles and checks the 390 px phone layout. It fails on any console error, page error, failed request, HTTP error response or unhandled promise rejection. |
| `../test_e2e.py` | Pytest wrapper (`@pytest.mark.e2e`). Skips when node or Playwright is missing. |

## Run

```bash
python3 -m pytest -m e2e -q              # through pytest (screenshots go to a temp folder)
node tests/e2e/ui_smoke.cjs              # directly; refreshes docs/screenshots/
node tests/e2e/ui_smoke.cjs http://127.0.0.1:8765   # against a dashboard you started yourself
python3 -m pytest -m "not e2e" -q        # everything except the browser test
```

`node tests/e2e/ui_smoke.cjs` writes `docs/screenshots/<view>-<light|dark>.png` (views:
overview, markets, surges, high, strategy, drawer) at 1440x900, `mobile-390.png`, and
`offline-light.png` / `fatal-light.png` for the error banners.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `PLAYWRIGHT_MODULE` | `/opt/node-tools/node_modules/playwright` | Where to `require` Playwright from |
| `PLAYWRIGHT_BROWSERS_PATH` | `/opt/pw-browsers` when it exists | Installed browsers (never run `playwright install` here) |
| `CHROME_PATH` | unset | An explicit Chromium binary, e.g. `/opt/pw-browsers/chromium-1194/chrome-linux/chrome` |
| `E2E_SCREENSHOT_DIR` | `docs/screenshots` | Where screenshots go |
| `E2E_EXTRA_SHOTS` | unset | Also save full-page review shots (and phone views of every page) here |
| `E2E_HEADED` | unset | `1` shows the browser window |
| `PYTHON` | `python3` | Interpreter used to start `serve_demo.py` |

If the default headless shell cannot start, the script retries with the full Chromium binary it
finds under `/opt/pw-browsers/chromium-*/chrome-linux/chrome`.
