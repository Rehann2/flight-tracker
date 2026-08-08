"""Daily collector: scan every valid date pair from config.yaml, store a snapshot
in SQLite, and print the daily digest (with day-over-day diff when history exists).

Pipeline per date pair: fast_flights builds the Google Flights URL -> headless
Chromium renders it -> DOM result rows parsed. Prices are indicative (±few %
session noise vs a logged-in browser); the digest links to live pages.
"""

import datetime as dt
import json
import re
import sqlite3
import sys
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright

from fast_flights import FlightQuery, Passengers, create_query

ROOT = Path(__file__).parent
DB = ROOT / "data" / "tracker.sqlite"
PRICE_RE = re.compile(r"€\s?[\d,]+")
LAYOVER_RE = re.compile(r"(?:\d+\s*hr(?:\s*\d+\s*min)?|\d+\s*min)\s+([A-Z]{3})\b")

# Connection airports covered by the "middle-east" preset in avoid_layovers.
# Gulf, Levant, Iraq, Iran, Yemen. Turkey (IST/SAW) and Egypt (CAI) are NOT
# included — add their codes to avoid_layovers explicitly if wanted.
MIDDLE_EAST_HUBS = {
    "DXB", "DWC", "SHJ", "AUH", "DOH", "BAH", "KWI", "MCT", "SLL",
    "RUH", "JED", "DMM", "MED", "AHB",
    "AMM", "AQJ", "BEY", "TLV", "DAM", "ALP",
    "BGW", "BSR", "NJF", "EBL", "ISU",
    "IKA", "MHD", "SYZ", "SAH", "ADE",
}


def expand_avoid(cfg):
    out = set()
    for item in cfg["filters"].get("avoid_layovers") or []:
        if str(item).lower().replace("_", "-") in ("middle-east", "middleeast", "me"):
            out |= MIDDLE_EAST_HUBS
        else:
            out.add(str(item).upper())
    return out


def load_config():
    return yaml.safe_load((ROOT / "config.yaml").read_text())


def date_pairs(cfg):
    start = cfg["trip"]["window_start"]
    end = cfg["trip"]["window_end"]
    days = cfg["trip"]["trip_days"]
    dep = start
    while dep + dt.timedelta(days=days) <= end:
        yield dep.isoformat(), (dep + dt.timedelta(days=days)).isoformat()
        dep += dt.timedelta(days=1)


def build_url(cfg, depart, ret):
    q = create_query(
        flights=[
            FlightQuery(date=depart, from_airport=cfg["route"]["origin"],
                        to_airport=cfg["route"]["destination"]),
            FlightQuery(date=ret, from_airport=cfg["route"]["destination"],
                        to_airport=cfg["route"]["origin"]),
        ],
        trip="round-trip",
        seat=cfg["filters"]["cabin"],
        passengers=Passengers(adults=cfg["filters"]["adults"]),
        currency="EUR",
        max_stops=cfg["filters"]["max_stops"],
    )
    return q.url() + "&hl=en"


def parse_row(text):
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    price = next((int(m.group().replace("€", "").replace(",", "").strip())
                  for l in lines if (m := PRICE_RE.search(l))), None)
    airline = duration_min = stops = None
    for i, l in enumerate(lines):
        m = re.fullmatch(r"(\d+) hr(?: (\d+) min)?", l)
        if m:
            duration_min = int(m.group(1)) * 60 + int(m.group(2) or 0)
            airline = lines[i - 1] if i else None
        if re.fullmatch(r"Nonstop|\d+ stops?", l):
            stops = 0 if l == "Nonstop" else int(l.split()[0])
    via = [c for l in lines for c in LAYOVER_RE.findall(l)]
    return {"price": price, "airline": airline, "duration_min": duration_min,
            "stops": stops, "via": via, "raw": text}


def accept_consent(page):
    if "consent" in page.url:
        for label in ["Reject all", "Tout refuser", "Accept all"]:
            btn = page.locator(f'button:has-text("{label}")')
            if btn.count():
                btn.first.click()
                break
        page.wait_for_url(lambda u: "consent" not in u, timeout=30000)


def fetch_pair(context, url, attempts=3):
    for i in range(attempts):
        page = context.new_page()
        try:
            page.goto(url, timeout=60000, wait_until="domcontentloaded")
            accept_consent(page)
            page.wait_for_function(
                "() => /€\\s?\\d{3}/.test(document.body.innerText)", timeout=45000)
            raw = page.eval_on_selector_all("ul li", "els => els.map(e => e.innerText)")
            rows = [parse_row(r) for r in raw if r and "€" in r and "round trip" in r]
            # DOM renders each itinerary twice (one stripped duplicate) — keep parsed ones
            rows = [r for r in rows if r["price"] and r["airline"]]
            if rows:
                return rows
        except Exception as e:
            print(f"    attempt {i + 1} failed: {type(e).__name__}", file=sys.stderr)
        finally:
            page.close()
    return []


def apply_filters(rows, cfg):
    f = cfg["filters"]
    avoid = expand_avoid(cfg)
    return [r for r in rows
            if (r["duration_min"] or 0) <= f["max_duration_hours"] * 60
            and (r["stops"] is None or r["stops"] <= f["max_stops"])
            and not (set(r.get("via") or []) & avoid)]


def store(run_date, results, cfg):
    route = f"{cfg['route']['origin']}-{cfg['route']['destination']}"
    trip_days = cfg["trip"]["trip_days"]
    DB.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS snapshots (
        run_date TEXT, depart TEXT, ret TEXT, price INTEGER,
        airline TEXT, duration_min INTEGER, stops INTEGER, rank INTEGER)""")
    cols = [r[1] for r in con.execute("PRAGMA table_info(snapshots)")]
    if "via" not in cols:
        con.execute("ALTER TABLE snapshots ADD COLUMN via TEXT")
    if "route" not in cols:
        con.execute("ALTER TABLE snapshots ADD COLUMN route TEXT")
        con.execute("ALTER TABLE snapshots ADD COLUMN trip_days INTEGER")
        # legacy rows predate config stamping; backfill with the current config
        con.execute("UPDATE snapshots SET route = ?, trip_days = ? WHERE route IS NULL",
                    (route, trip_days))
    con.execute("DELETE FROM snapshots WHERE run_date = ?", (run_date,))
    for depart, ret, rows in results:
        for rank, r in enumerate(sorted(rows, key=lambda x: x["price"])):
            con.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (run_date, depart, ret, r["price"], r["airline"],
                         r["duration_min"], r["stops"], rank,
                         ",".join(r.get("via") or []), route, trip_days))
    con.commit()
    return con


def previous_mins(con, run_date):
    rows = con.execute("""
        SELECT depart, MIN(price) FROM snapshots
        WHERE run_date = (SELECT MAX(run_date) FROM snapshots WHERE run_date < ?)
        GROUP BY depart""", (run_date,)).fetchall()
    return dict(rows)


def digest(cfg, con, run_date, results):
    prev = previous_mins(con, run_date)
    print(f"\n{'=' * 62}")
    print(f"  FLIGHT TRACKER DIGEST — {run_date}")
    print(f"  {cfg['route']['origin']} <-> {cfg['route']['destination']}, "
          f"{cfg['trip']['trip_days']}-day trip, budget <= EUR {cfg['filters']['budget_eur']}")
    print(f"{'=' * 62}")
    mins, best = [], None
    for depart, ret, rows in results:
        kept = apply_filters(rows, cfg)
        if not kept:
            print(f"  {depart} -> {ret}: no options passed filters")
            continue
        top = min(kept, key=lambda r: r["price"])
        mins.append(top["price"])
        delta = ""
        if depart in prev:
            pct = (top["price"] - prev[depart]) / prev[depart] * 100
            mark = " <-- MOVED" if abs(pct) >= cfg["notify"]["alert_threshold_pct"] else ""
            delta = f"  ({pct:+.1f}% vs prev){mark}"
        flag = " OVER BUDGET" if top["price"] > cfg["filters"]["budget_eur"] else ""
        print(f"  {depart} -> {ret}:  EUR {top['price']:>5}  "
              f"{top['airline']:<20} {top['stops']} stop(s){delta}{flag}")
        if best is None or top["price"] < best[0]:
            best = (top["price"], depart, ret, top["airline"])
    if mins:
        print(f"{'-' * 62}")
        print(f"  BEST:    EUR {best[0]} | {best[1]} -> {best[2]} | {best[3]}")
        print(f"  AVERAGE of per-date minimums: EUR {sum(mins) // len(mins)} "
              f"across {len(mins)} date pairs")
    print(f"{'=' * 62}")


def export_data(cfg, con, run_date, results):
    """Write data/latest.json (dashboard + email source) and data/history.csv."""
    pairs_out = []
    for depart, ret, rows in results:
        kept = sorted(apply_filters(rows, cfg), key=lambda r: r["price"])
        pairs_out.append({
            "depart": depart, "return": ret,
            "min_price": kept[0]["price"] if kept else None,
            "url": build_url(cfg, depart, ret),
            "top": [{k: r[k] for k in ("price", "airline", "duration_min", "stops", "via")}
                    for r in kept[:5]],
        })
    mins = [p["min_price"] for p in pairs_out if p["min_price"]]
    (ROOT / "data" / "latest.json").write_text(json.dumps({
        "run_date": run_date,
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "route": cfg["route"], "trip": cfg["trip"], "filters": cfg["filters"],
        "best": min(mins) if mins else None,
        "avg_of_mins": sum(mins) // len(mins) if mins else None,
        "pairs": pairs_out,
    }, indent=1, default=str))
    hist = con.execute("""SELECT run_date, depart, ret, MIN(price), route, trip_days
                          FROM snapshots GROUP BY run_date, depart
                          ORDER BY run_date, depart""").fetchall()
    lines = ["run_date,depart,return,min_price,route,trip_days"] + [
        ",".join("" if v is None else str(v) for v in r) for r in hist]
    (ROOT / "data" / "history.csv").write_text("\n".join(lines) + "\n")


def main():
    cfg = load_config()
    run_date = dt.date.today().isoformat()
    pairs = list(date_pairs(cfg))
    print(f"Scanning {len(pairs)} date pairs...")
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(locale="en-US")
        for depart, ret in pairs:
            rows = fetch_pair(context, build_url(cfg, depart, ret))
            print(f"  {depart} -> {ret}: {len(rows)} options"
                  + (f", cheapest EUR {min(r['price'] for r in rows)}" if rows else ""),
                  flush=True)
            results.append((depart, ret, rows))
        browser.close()
    con = store(run_date, results, cfg)
    export_data(cfg, con, run_date, results)
    digest(cfg, con, run_date, results)
    con.close()


if __name__ == "__main__":
    main()
