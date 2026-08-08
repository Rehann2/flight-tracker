# Flight Tracker

## What & Why
Personal tool that tracks **all flights matching a filter** (not one fixed itinerary like Google Flights tracking) and emails a daily digest of how the price landscape moved, so the user can time the booking. First use case: a 40-day round trip within a Dec 2026 – Jan 2027 window (destination India), but the tool is route-agnostic by design.

## Success criteria
- Daily email with: cheapest qualifying itinerary, average of per-date-pair minimums, day-over-day change, top movers.
- Prices match what the user sees manually on Google Flights (spot-checked before trusting — ground truth rule).
- Runs unattended every day.

## Core model
- Filter = origin + destination (each expandable to alternate airports within ~100 km), departure window, **exact trip length in days** (e.g. 40), budget cap, max stops, max total duration, cabin, passengers.
- Date matrix = every departure date where depart ≥ window start and depart + trip_length ≤ window end. One price query per (date pair × airport combo) per day.
- Daily snapshot appended to local store (SQLite) → history, diffs, trends.

## Tech stack & data source decisions (verified 2026-08-08)
- **Amadeus Self-Service API is DEAD** — portal decommissioned 2026-07-17, keys deactivated, no new signups (confirmed via PhocusWire + Amadeus). Do not suggest it.
- Kiwi Tequila: invite-only. Skyscanner: partner-only. SerpAPI free tier (~100/mo) too small for a daily matrix (~660+ calls/mo).
- **Primary source: Google Flights rendered in headless Chromium (Playwright)**, with the `faster-flights` fork (pip `faster-flights`, imports as `fast_flights`) used only to BUILD the query URL (`create_query(...).url()`).
  - Why: direct RPC/HTML fetching via fast-flights returns EMPTY for round trips **longer than 30 days** (verified: 30d ok, 31d+ empty, any route; Google's site itself works fine). Our trips are 40 days.
  - Sum-of-cheapest-one-ways was tested as a proxy and REJECTED: bias vs true round-trip price ranged -4% to +49% on 3 validation pairs.
  - Playwright flow: goto URL → click through EU consent wall if present ("Reject all") → wait for € prices → parse `ul li` innerText rows (airline, duration, stops, price). Flaky ~1/3 of loads; 3 retries handles it. ~15s/pair.
  - Original `fast-flights` (3.0.2) is broken (parser + missing `typing_extensions` dep) — use the `faster-flights` fork.
- Backup/cross-check: Travelpayouts Data API (free signup, cached Aviasales aggregates). SerpAPI paid fallback.
- Notifications: **email** (Gmail SMTP app password — credential needed at deploy step, not before).
- Scheduling: GitHub Actions daily cron preferred; fallback = cron on user's machine if Google blocks Actions IPs.
- Python 3, SQLite, config-driven (one YAML/JSON config per tracked trip).

## Conventions & working agreements
- Start small: validate 1–2 date pairs against manual Google Flights before scaling to the full matrix.
- Config-driven, no hardcoded routes. One trip first; generalize (multiple trackers) only after the single-trip version works.
- No speculative features. Surgical changes. Verifiable success criteria per step.

## First full scan results (2026-08-08, local run)
- 10/12 pairs succeeded (Dec 2 + Dec 8 departures failed 3 attempts — Google throttling after a day of heavy testing; once-daily cadence should be fine).
- BEST: **€583** Dec 9 → Jan 18 (Gulf Air, 1 stop). Average of per-date minimums: **€656**. Gulf Air is cheapest on every single date pair. Range €583–733.
- Repo: https://github.com/Rehann2/flight-tracker (private), gh CLI authed as Rehann2 on this box (token in plain text ~/.config/gh/hosts.yml).

## Deployment (2026-08-08, evening)
- Repo PUBLIC (user approved via full-setup choice). **Live dashboard: https://rehann2.github.io/flight-tracker/** (GitHub Pages, branch main, root; index.html; dashboard.html removed).
- index.html fetches data/latest.json + data/history.csv + config.yaml live → auto-updates after each daily scan. History chart (best + avg lines) activates at ≥2 scans; Δ-day column from history.csv.
- In-UI filter editor: settings drawer → edits config.yaml via GitHub contents API + optional workflow_dispatch rescan. Requires user's fine-grained PAT (repo-scoped, Contents+Actions RW) pasted once, stored in browser localStorage only. NOT YET DONE by user.
- claude.ai artifact (c7583385…) is a static snapshot from the first scan — superseded by Pages.

## Status / open items (2026-08-08)
- Route confirmed: Paris (CDG, +ORY later) ⇄ Bangalore (BLR). Window: depart ~1st week of Dec 2026, return by ~3rd week of Jan 2027, exactly 40 days. User has GitHub.
- MVP fetcher WORKS (`mvp_fetch.py`): Dec 3→Jan 12 cheapest €638 (Gulf Air, 12h55, 1 stop), Dec 7→Jan 16 cheapest €646. Awaiting user's manual Google Flights cross-check (ground truth) before scaling.
- v1 interface = config.yaml in the repo (edit via GitHub web UI); clickable UI is a v2 candidate.
- Known cleanup items: DOM rows duplicated (dedupe properly), reuse one browser context to reduce consent flakiness, exact window semantics to confirm ("Dec 1st week" = depart Dec 1–7?).
- Next: user validation → full date-matrix collector + SQLite + email digest → test from GitHub Actions IPs (may be blocked; fallback local cron).
