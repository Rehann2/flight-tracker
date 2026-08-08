"""Feasibility spike v2: render a 40-day round trip (CDG->BLR) in headless Chromium,
capture prices from the DOM and log XHR endpoints that carry results."""

import re
import sys
from playwright.sync_api import sync_playwright

URL = ("https://www.google.com/travel/flights/search?tfs="
       "GhoSCjIwMjYtMTItMDNqBRIDQ0RHcgUSA0JMUhoaEgoyMDI3LTAxLTEyagUSA0JMUnIFEgNDREdCAQFIAZgBAQ=="
       "&hl=en&curr=EUR")
SCRATCH = "/tmp/claude-10001/-home-criteo/7a02781d-d714-4848-a3f7-3de0ba52aa80/scratchpad/rpc"

def attempt(page, xhr_log):
    page.on("response", lambda r: xhr_log.append((r.status, r.url))
            if ("batchexecute" in r.url or "GetShoppingResults" in r.url) else None)
    page.goto(URL, timeout=60000, wait_until="domcontentloaded")

    if "consent" in page.url:
        print("consent wall hit, clicking through...")
        for label in ["Reject all", "Tout refuser", "Alle ablehnen", "Accept all"]:
            btn = page.locator(f'button:has-text("{label}")')
            if btn.count():
                btn.first.click()
                break
        page.wait_for_url(lambda u: "consent" not in u, timeout=30000)

    page.wait_for_function("() => /€\\s?\\d{3}/.test(document.body.innerText)", timeout=60000)
    return page.inner_text("body")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    for i in (1, 2):
        page = browser.new_page(locale="en-US")
        xhr = []
        try:
            text = attempt(page, xhr)
            prices = sorted(set(re.findall(r"€\s?[\d,]{3,6}", text)))
            print(f"attempt {i}: OK | prices: {prices}")
            rows = [r for r in page.eval_on_selector_all(
                "ul li", "els => els.map(e => e.innerText)") if "€" in (r or "")]
            print(f"result rows with prices: {len(rows)}")
            for r in rows[:3]:
                print("ROW:", repr(r[:220]))
            print("result XHRs:", [u[:110] for _, u in xhr][:5])
            break
        except Exception as e:
            print(f"attempt {i}: FAILED {type(e).__name__} | url={page.url[:90]}")
            try:
                page.screenshot(path=f"{SCRATCH}/fail_{i}.png")
                snippet = page.inner_text("body")[:300].replace("\n", " | ")
                print("body snippet:", snippet)
            except Exception:
                pass
        finally:
            page.close()
    browser.close()
