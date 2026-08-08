"""Send the daily digest email from data/latest.json + data/history.csv.

Env: SMTP_USER (gmail address, also the sender), SMTP_PASSWORD (app password),
DIGEST_TO (comma-separated recipients). DRY_RUN=1 writes digest.html instead of sending.
Exits 0 silently when recipients or credentials are missing.
"""

import csv
import json
import os
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import yaml

ROOT = Path(__file__).parent
DASHBOARD = "https://rehann2.github.io/flight-tracker/"


def load():
    latest = json.loads((ROOT / "data" / "latest.json").read_text())
    hist = list(csv.DictReader((ROOT / "data" / "history.csv").open()))
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    return latest, hist, cfg


def deltas(latest, hist):
    runs = sorted({h["run_date"] for h in hist})
    prev_run = runs[-2] if len(runs) >= 2 else None
    prev = {h["depart"]: int(h["min_price"]) for h in hist if h["run_date"] == prev_run}
    out = {}
    for p in latest["pairs"]:
        if p["min_price"] and p["depart"] in prev:
            out[p["depart"]] = p["min_price"] - prev[p["depart"]]
    return out


def build(latest, hist, cfg):
    pairs = [p for p in latest["pairs"] if p["min_price"]]
    best = min(pairs, key=lambda p: p["min_price"])
    avg = sum(p["min_price"] for p in pairs) // len(pairs)
    d = deltas(latest, hist)
    thr = cfg["notify"]["alert_threshold_pct"]
    route = f"{cfg['route']['origin']}⇄{cfg['route']['destination']}"

    best_delta = d.get(best["depart"])
    subj_delta = ""
    if best_delta:
        subj_delta = f" ({'▼' if best_delta < 0 else '▲'}€{abs(best_delta)})"
    subject = f"✈ {route} best €{best['min_price']}{subj_delta} · avg €{avg}"

    movers = [(p, d[p["depart"]]) for p in pairs
              if p["depart"] in d and d[p["depart"]] != 0
              and abs(d[p["depart"]]) / (p["min_price"] - d[p["depart"]]) * 100 >= thr]
    movers.sort(key=lambda x: x[1])

    rows = ""
    for p in pairs:
        dv = d.get(p["depart"])
        dtxt = "—" if dv is None else ("0" if dv == 0 else f"{'−' if dv < 0 else '+'}€{abs(dv)}")
        dcol = "#5C6B7A" if not dv else ("#2E7D4F" if dv < 0 else "#A8542F")
        is_best = p is best
        t0 = p["top"][0] if p["top"] else {}
        rows += f"""<tr style="background:{'#E7EEF5' if is_best else 'transparent'}">
          <td style="padding:6px 10px">{p['depart'][5:]} → {p['return'][5:]}</td>
          <td style="padding:6px 10px;font-weight:bold">€{p['min_price']}{' 🏆' if is_best else ''}</td>
          <td style="padding:6px 10px">{t0.get('airline', '—')}</td>
          <td style="padding:6px 10px;color:{dcol}">{dtxt}</td>
          <td style="padding:6px 10px"><a href="{p['url']}">book</a></td></tr>"""

    mover_html = ""
    if movers:
        items = "".join(
            f"<li>{p['depart'][5:]}: {'−' if dv < 0 else '+'}€{abs(dv)} → €{p['min_price']}</li>"
            for p, dv in movers)
        mover_html = f"<p><b>Moved ≥ {thr}%:</b></p><ul>{items}</ul>"

    html = f"""<div style="font-family:Segoe UI,Arial,sans-serif;max-width:640px;color:#182430">
      <h2 style="margin:0 0 4px">✈ {route} — 40-day trip tracker</h2>
      <p style="margin:0 0 14px;color:#5C6B7A">Scan of {latest['run_date']}</p>
      <p style="font-size:17px"><b>Best: €{best['min_price']}</b> — depart {best['depart']},
        return {best['return']} ({(best['top'][0]['airline'] if best['top'] else '')})<br>
        Average of date minimums: <b>€{avg}</b></p>
      {mover_html}
      <table style="border-collapse:collapse;font-size:14px">
        <tr style="color:#5C6B7A;text-align:left">
          <th style="padding:6px 10px">Dates</th><th style="padding:6px 10px">Price</th>
          <th style="padding:6px 10px">Carrier</th><th style="padding:6px 10px">Δ day</th>
          <th style="padding:6px 10px"></th></tr>
        {rows}
      </table>
      <p style="margin-top:16px"><a href="{DASHBOARD}">Open the dashboard</a> ·
        prices indicative, links show live fares</p></div>"""

    text = f"{route} best EUR {best['min_price']} ({best['depart']} -> {best['return']}), avg EUR {avg}. Dashboard: {DASHBOARD}"
    return subject, html, text


def main():
    to = [a.strip() for a in os.environ.get("DIGEST_TO", "").split(",") if a.strip()]
    user = os.environ.get("SMTP_USER", "")
    pwd = os.environ.get("SMTP_PASSWORD", "")
    latest, hist, cfg = load()
    subject, html, text = build(latest, hist, cfg)

    if os.environ.get("DRY_RUN"):
        (ROOT / "data" / "digest_preview.html").write_text(html)
        print("DRY_RUN: wrote data/digest_preview.html | subject:", subject)
        return
    if not to or not user or not pwd:
        print("email skipped: recipients or SMTP credentials not configured")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, ", ".join(to)
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
        s.starttls()
        s.login(user, pwd)
        s.sendmail(user, to, msg.as_string())
    print(f"digest sent to {len(to)} recipient(s) | {subject}")


if __name__ == "__main__":
    main()
