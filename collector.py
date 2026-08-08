"""Daily collector, multi-trip: every trips/*.yaml is one tracked trip owned by a
user. Scans all trips (deduplicating identical Google queries across trips),
stores per-trip snapshots in SQLite, exports data/latest.json + data/history.csv,
prints a digest.

Pipeline per date pair: fast_flights builds the Google Flights URL -> headless
Chromium renders it -> DOM result rows parsed. Prices are indicative (±few %
session noise vs a logged-in browser); links show live fares.
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


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")


def config_sig(t):
    """Signature of the price-affecting filters; a change restarts the trip's
    history thread (budget is display-only, so it is deliberately excluded)."""
    avoid = ";".join(sorted(str(a) for a in (t.get("avoid_layovers") or [])))  # ';' — sig lives in a CSV column
    return (f"{t['origin']}-{t['destination']}|{t['trip_days']}|{t['max_stops']}"
            f"|{t['max_duration_hours']}|{avoid}|{t['cabin']}|{t['adults']}")


def load_trips():
    trips = []
    for f in sorted((ROOT / "trips").glob("*.yaml")):
        t = yaml.safe_load(f.read_text())
        t["file"] = f"trips/{f.name}"
        t["id"] = f"{t['user']}/{slug(t['name'])}"
        t["route"] = f"{t['origin']}-{t['destination']}"
        trips.append(t)
    return trips


def expand_avoid(t):
    out = set()
    for item in t.get("avoid_layovers") or []:
        if str(item).lower().replace("_", "-") in ("middle-east", "middleeast", "me"):
            out |= MIDDLE_EAST_HUBS
        else:
            out.add(str(item).upper())
    return out


def date_pairs(t):
    start, end, days = t["window_start"], t["window_end"], t["trip_days"]
    dep = start
    while dep + dt.timedelta(days=days) <= end:
        yield dep.isoformat(), (dep + dt.timedelta(days=days)).isoformat()
        dep += dt.timedelta(days=1)


def query_key(t, depart, ret):
    return (t["origin"], t["destination"], depart, ret, t["cabin"], t["adults"], t["max_stops"])


def build_url(t, depart, ret):
    q = create_query(
        flights=[
            FlightQuery(date=depart, from_airport=t["origin"], to_airport=t["destination"]),
            FlightQuery(date=ret, from_airport=t["destination"], to_airport=t["origin"]),
        ],
        trip="round-trip",
        seat=t["cabin"],
        passengers=Passengers(adults=t["adults"]),
        currency="EUR",
        max_stops=t["max_stops"],
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
            "stops": stops, "via": via}


def accept_consent(page):
    if "consent" in page.url:
        for label in ["Reject all", "Tout refuser", "Accept all"]:
            btn = page.locator(f'button:has-text("{label}")')
            if btn.count():
                btn.first.click()
                break
        page.wait_for_url(lambda u: "consent" not in u, timeout=30000)


def fetch_url(context, url, attempts=3):
    for i in range(attempts):
        page = context.new_page()
        try:
            page.goto(url, timeout=60000, wait_until="domcontentloaded")
            accept_consent(page)
            page.wait_for_function(
                "() => /€\\s?\\d{3}/.test(document.body.innerText)", timeout=45000)
            raw = page.eval_on_selector_all("ul li", "els => els.map(e => e.innerText)")
            rows = [parse_row(r) for r in raw if r and "€" in r and "round trip" in r]
            rows = [r for r in rows if r["price"] and r["airline"]]
            if rows:
                return rows
        except Exception as e:
            print(f"    attempt {i + 1} failed: {type(e).__name__}", file=sys.stderr)
        finally:
            page.close()
    return []


def apply_filters(rows, t):
    avoid = expand_avoid(t)
    return [r for r in rows
            if (r["duration_min"] or 0) <= t["max_duration_hours"] * 60
            and (r["stops"] is None or r["stops"] <= t["max_stops"])
            and not (set(r.get("via") or []) & avoid)]


def store(run_date, trip_results):
    DB.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS snapshots (
        run_date TEXT, depart TEXT, ret TEXT, price INTEGER,
        airline TEXT, duration_min INTEGER, stops INTEGER, rank INTEGER,
        via TEXT, route TEXT, trip_days INTEGER)""")
    cols = [r[1] for r in con.execute("PRAGMA table_info(snapshots)")]
    for col, typ in (("via", "TEXT"), ("route", "TEXT"), ("trip_days", "INTEGER"),
                     ("user", "TEXT"), ("trip_id", "TEXT"), ("config_sig", "TEXT")):
        if col not in cols:
            con.execute(f"ALTER TABLE snapshots ADD COLUMN {col} {typ}")
    # legacy rows predate users/trips; they were all rehan's CDG-BLR experiments
    con.execute("UPDATE snapshots SET user='rehan', trip_id='rehan/blr-winter' WHERE trip_id IS NULL")
    for t, _ in trip_results:
        con.execute("UPDATE snapshots SET config_sig=? WHERE trip_id=? AND config_sig IS NULL",
                    (config_sig(t), t["id"]))
    con.execute("DELETE FROM snapshots WHERE run_date = ?", (run_date,))
    for t, results in trip_results:
        for depart, ret, rows in results:
            for rank, r in enumerate(sorted(rows, key=lambda x: x["price"])):
                con.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (run_date, depart, ret, r["price"], r["airline"],
                             r["duration_min"], r["stops"], rank,
                             ",".join(r.get("via") or []), t["route"], t["trip_days"],
                             t["user"], t["id"], config_sig(t)))
    con.commit()
    return con


def export_data(con, run_date, trip_results):
    trips_out = []
    for t, results in trip_results:
        pairs_out = []
        for depart, ret, rows in results:
            kept = sorted(apply_filters(rows, t), key=lambda r: r["price"])
            pairs_out.append({
                "depart": depart, "return": ret,
                "min_price": kept[0]["price"] if kept else None,
                "url": build_url(t, depart, ret),
                "top": [{k: r[k] for k in ("price", "airline", "duration_min", "stops", "via")}
                        for r in kept[:5]],
            })
        mins = [p["min_price"] for p in pairs_out if p["min_price"]]
        cfg_keys = ("user", "name", "origin", "destination", "window_start", "window_end",
                    "trip_days", "budget_eur", "max_stops", "max_duration_hours",
                    "avoid_layovers", "cabin", "adults", "alert_threshold_pct")
        trips_out.append({
            "id": t["id"], "file": t["file"],
            "config": {k: t.get(k) for k in cfg_keys},
            "best": min(mins) if mins else None,
            "avg_of_mins": sum(mins) // len(mins) if mins else None,
            "pairs": pairs_out,
        })
    (ROOT / "data" / "latest.json").write_text(json.dumps({
        "run_date": run_date,
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "trips": trips_out,
    }, indent=1, default=str))
    hist = con.execute("""SELECT run_date, trip_id, depart, ret, MIN(price), config_sig
                          FROM snapshots GROUP BY run_date, trip_id, depart
                          ORDER BY run_date, trip_id, depart""").fetchall()
    lines = ["run_date,trip_id,depart,return,min_price,config_sig"] + [
        ",".join("" if v is None else str(v) for v in r) for r in hist]
    (ROOT / "data" / "history.csv").write_text("\n".join(lines) + "\n")


def digest(con, run_date, trip_results):
    for t, results in trip_results:
        mins, best = [], None
        print(f"\n=== {t['id']} | {t['route']} {t['trip_days']}d ===")
        for depart, ret, rows in results:
            kept = apply_filters(rows, t)
            if not kept:
                continue
            top = min(kept, key=lambda r: r["price"])
            mins.append(top["price"])
            if best is None or top["price"] < best[0]:
                best = (top["price"], depart, ret, top["airline"])
        if mins:
            print(f"  BEST: EUR {best[0]} | {best[1]} -> {best[2]} | {best[3]}")
            print(f"  AVERAGE of per-date minimums: EUR {sum(mins) // len(mins)} across {len(mins)} date pairs")


def main():
    trips = load_trips()
    run_date = dt.date.today().isoformat()
    # dedupe identical queries across trips so overlapping trips cost one fetch
    wanted = {}
    for t in trips:
        for depart, ret in date_pairs(t):
            wanted.setdefault(query_key(t, depart, ret), (t, depart, ret))
    print(f"{len(trips)} trip(s), {len(wanted)} unique queries to scan...")
    cache = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(locale="en-US")
        for key, (t, depart, ret) in wanted.items():
            rows = fetch_url(context, build_url(t, depart, ret))
            cache[key] = rows
            print(f"  {t['origin']}->{t['destination']} {depart}/{ret}: {len(rows)} options"
                  + (f", cheapest EUR {min(r['price'] for r in rows)}" if rows else ""),
                  flush=True)
        browser.close()
    trip_results = []
    for t in trips:
        results = [(depart, ret, cache.get(query_key(t, depart, ret), []))
                   for depart, ret in date_pairs(t)]
        trip_results.append((t, results))
    con = store(run_date, trip_results)
    export_data(con, run_date, trip_results)
    digest(con, run_date, trip_results)
    con.close()


if __name__ == "__main__":
    main()
