"""MVP: fetch round-trip prices for date pairs, Paris (CDG) -> Bangalore (BLR).

Pipeline: fast_flights builds the Google Flights URL -> headless Chromium renders it
-> parse result rows from the DOM. Validate output against Google Flights manually.
"""

import re
from playwright.sync_api import sync_playwright

from fast_flights import FlightQuery, Passengers, create_query

PAIRS = [
    ("2026-12-03", "2027-01-12"),
    ("2026-12-07", "2027-01-16"),
]

ROW_RE = re.compile(r"€\s?[\d,]+")


def build_url(depart, ret, from_airport="CDG", to_airport="BLR"):
    q = create_query(
        flights=[
            FlightQuery(date=depart, from_airport=from_airport, to_airport=to_airport),
            FlightQuery(date=ret, from_airport=to_airport, to_airport=from_airport),
        ],
        trip="round-trip",
        seat="economy",
        passengers=Passengers(adults=1),
        currency="EUR",
    )
    return q.url() + "&hl=en"


def parse_row(text):
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    price = next((int(m.group().replace("€", "").replace(",", "").strip())
                  for l in lines if (m := ROW_RE.search(l))), None)
    airline = duration = stops = None
    for i, l in enumerate(lines):
        if re.fullmatch(r"\d+ hr(?: \d+ min)?", l):
            duration = l
            airline = lines[i - 1] if i else None
        if re.fullmatch(r"Nonstop|\d+ stops?", l):
            stops = l
    return {"price": price, "airline": airline, "duration": duration, "stops": stops}


def fetch_pair(browser, depart, ret, attempts=3):
    url = build_url(depart, ret)
    for i in range(attempts):
        page = browser.new_page(locale="en-US")
        try:
            page.goto(url, timeout=60000, wait_until="domcontentloaded")
            if "consent" in page.url:
                for label in ["Reject all", "Tout refuser", "Accept all"]:
                    btn = page.locator(f'button:has-text("{label}")')
                    if btn.count():
                        btn.first.click()
                        break
                page.wait_for_url(lambda u: "consent" not in u, timeout=30000)
            page.wait_for_function(
                "() => /€\\s?\\d{3}/.test(document.body.innerText)", timeout=60000)
            raw = page.eval_on_selector_all("ul li", "els => els.map(e => e.innerText)")
            rows = [parse_row(r) for r in raw if r and "€" in r and "round trip" in r]
            rows = [r for r in rows if r["price"]]
            return url, rows
        except Exception as e:
            print(f"  attempt {i + 1} failed: {type(e).__name__}")
        finally:
            page.close()
    return url, []


if __name__ == "__main__":
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for depart, ret in PAIRS:
            url, rows = fetch_pair(browser, depart, ret)
            uniq = {(r["price"], r["airline"]): r for r in rows}
            top = sorted(uniq.values(), key=lambda r: r["price"])
            prices = [r["price"] for r in top]
            print(f"\n=== {depart} -> {ret} | {len(top)} unique options ===")
            print(f"validate here: {url}")
            for r in top[:5]:
                print(f"  €{r['price']:>5} | {r['airline'] or '?':<22} | {r['duration'] or '?':<12} | {r['stops'] or '?'}")
            if prices:
                print(f"  cheapest €{min(prices)} | avg €{sum(prices) // len(prices)}")
        browser.close()
