# Flight Tracker

**Live dashboard: https://rehann2.github.io/flight-tracker/**

Tracks **every** round-trip itinerary matching a filter — date window, exact trip
length, budget, stops, duration — and records the daily price landscape, so you can
time your booking. Built because Google Flights only tracks fixed dates.

## How it works

```
trips/*.yaml ──> collector.py ──> Google Flights (headless Chromium)
                     │
                     ├──> data/tracker.sqlite   full snapshot history
                     ├──> data/latest.json      today's landscape (dashboard/email source)
                     ├──> data/history.csv      min price per date pair per day
                     └──> stdout digest         best option, averages, day-over-day movers
```

A GitHub Actions workflow (`.github/workflows/daily.yml`) runs the scan every
morning and commits the refreshed `data/` back to the repo.

## Configure

Every file in `trips/` is one tracked trip owned by a user (see `trips/rehan--blr-winter.yaml`
for the schema). Add, edit, or delete trips from the dashboard's **⚙ Manage trips** drawer —
or edit the files directly. Each user's daily email covers only their own trips; recipient
addresses live in the private `DIGEST_TO` repo variable (a JSON map of user → addresses),
never in the repo.

## Run locally

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium --only-shell
.venv/bin/python collector.py
```

## Notes

- Prices are indicative: Google serves session-cached quotes that can differ a few
  percent from what a logged-in browser sees. Each digest line links to the live page.
- The `fast_flights` library alone can't fetch round trips longer than 30 days
  (returns empty); that's why rendering with a real browser is required here.
