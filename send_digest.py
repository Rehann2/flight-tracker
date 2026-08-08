"""Send per-user digest emails from data/latest.json + data/history.csv.

Env: SMTP_USER (gmail address, also the sender), SMTP_PASSWORD (app password),
DIGEST_TO — a JSON map of user -> comma-separated addresses, e.g.
{"rehan": "a@gmail.com", "khush": "b@gmail.com"} (a bare address string is
treated as belonging to every user, for backward compatibility).
DRY_RUN=1 writes data/digest_preview_<user>.html instead of sending.
Users without an address, or with no trips, are skipped silently.
"""

import csv
import json
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

ROOT = Path(__file__).parent
DASHBOARD = "https://rehann2.github.io/flight-tracker/"


def recipients_map():
    raw = os.environ.get("DIGEST_TO", "").strip()
    if not raw:
        return {}
    try:
        m = json.loads(raw)
        if isinstance(m, dict):
            return {str(k).lower(): v for k, v in m.items()}
    except ValueError:
        pass
    return {"*": raw}  # legacy plain address: send everything there


def config_sig(c):
    avoid = ";".join(sorted(str(a) for a in (c.get("avoid_layovers") or [])))
    return (f"{c['origin']}-{c['destination']}|{c['trip_days']}|{c['max_stops']}"
            f"|{c['max_duration_hours']}|{avoid}|{c['cabin']}|{c['adults']}")


def deltas(trip, hist):
    sig = config_sig(trip["config"])
    rows = [h for h in hist if h["trip_id"] == trip["id"]
            and (not h.get("config_sig") or h["config_sig"] == sig)]
    runs = sorted({h["run_date"] for h in rows})
    prev_run = runs[-2] if len(runs) >= 2 else None
    prev = {h["depart"]: int(h["min_price"]) for h in rows
            if h["run_date"] == prev_run and h["min_price"]}
    return {p["depart"]: p["min_price"] - prev[p["depart"]]
            for p in trip["pairs"] if p["min_price"] and p["depart"] in prev}


def trip_section(trip, hist):
    cfg = trip["config"]
    pairs = [p for p in trip["pairs"] if p["min_price"]]
    if not pairs:
        return None, None
    best = min(pairs, key=lambda p: p["min_price"])
    avg = sum(p["min_price"] for p in pairs) // len(pairs)
    d = deltas(trip, hist)
    thr = cfg.get("alert_threshold_pct") or 3
    route = f"{cfg['origin']}⇄{cfg['destination']}"

    movers = [(p, d[p["depart"]]) for p in pairs
              if p["depart"] in d and d[p["depart"]] != 0
              and abs(d[p["depart"]]) / (p["min_price"] - d[p["depart"]]) * 100 >= thr]
    movers.sort(key=lambda x: x[1])
    mover_html = ""
    if movers:
        items = "".join(
            f"<li>{p['depart'][5:]}: {'−' if dv < 0 else '+'}€{abs(dv)} → €{p['min_price']}</li>"
            for p, dv in movers)
        mover_html = f"<p style='margin:8px 0 2px'><b>Moved ≥ {thr}%:</b></p><ul style='margin:4px 0'>{items}</ul>"

    rows = ""
    for p in pairs:
        dv = d.get(p["depart"])
        dtxt = "—" if dv is None else ("0" if dv == 0 else f"{'−' if dv < 0 else '+'}€{abs(dv)}")
        dcol = "#5C6B7A" if not dv else ("#2E7D4F" if dv < 0 else "#A8542F")
        t0 = p["top"][0] if p["top"] else {}
        via = (" via " + "/".join(t0["via"])) if t0.get("via") else ""
        over = p["min_price"] > (cfg.get("budget_eur") or 10 ** 9)
        rows += f"""<tr style="background:{'#E7EEF5' if p is best else 'transparent'}">
          <td style="padding:5px 10px">{p['depart'][5:]} → {p['return'][5:]}</td>
          <td style="padding:5px 10px;font-weight:bold;color:{'#A8542F' if over else '#182430'}">€{p['min_price']}{' 🏆' if p is best else ''}</td>
          <td style="padding:5px 10px">{t0.get('airline', '—')}{via}</td>
          <td style="padding:5px 10px;color:{dcol}">{dtxt}</td>
          <td style="padding:5px 10px"><a href="{p['url']}">book</a></td></tr>"""

    html = f"""<div style="margin:0 0 26px">
      <h3 style="margin:0 0 2px">{cfg['name']} — {route}, {cfg['trip_days']}-day trip</h3>
      <p style="margin:0 0 8px;color:#5C6B7A;font-size:13px">
        best <b style="color:#182430">€{best['min_price']}</b> ({best['depart']} → {best['return']})
        · avg of date minimums <b style="color:#182430">€{avg}</b>
        · budget €{cfg.get('budget_eur', '—')}</p>
      {mover_html}
      <table style="border-collapse:collapse;font-size:13.5px">
        <tr style="color:#5C6B7A;text-align:left">
          <th style="padding:5px 10px">Dates</th><th style="padding:5px 10px">Price</th>
          <th style="padding:5px 10px">Carrier</th><th style="padding:5px 10px">Δ</th>
          <th style="padding:5px 10px"></th></tr>
        {rows}
      </table></div>"""
    subj_bit = f"{route} €{best['min_price']}"
    dv = d.get(best["depart"])
    if dv:
        subj_bit += f" ({'▼' if dv < 0 else '▲'}€{abs(dv)})"
    return html, subj_bit


def main():
    user_env = os.environ.get("SMTP_USER", "")
    pwd = os.environ.get("SMTP_PASSWORD", "")
    latest = json.loads((ROOT / "data" / "latest.json").read_text())
    hist = list(csv.DictReader((ROOT / "data" / "history.csv").open()))
    rec = recipients_map()

    by_user = {}
    for trip in latest["trips"]:
        by_user.setdefault(trip["config"]["user"], []).append(trip)

    sent = 0
    for user, trips in by_user.items():
        addr = rec.get(user.lower()) or rec.get("*")
        if not addr:
            print(f"{user}: no recipient address configured, skipped")
            continue
        to = list(dict.fromkeys(a.strip().lower() for a in addr.split(",") if a.strip()))
        sections, subj_bits = [], []
        for trip in trips:
            html, bit = trip_section(trip, hist)
            if html:
                sections.append(html)
                subj_bits.append(bit)
        if not sections:
            print(f"{user}: no trip data, skipped")
            continue
        subject = "✈ " + " · ".join(subj_bits)
        body = f"""<div style="font-family:Segoe UI,Arial,sans-serif;max-width:660px;color:#182430">
          <p style="margin:0 0 16px;color:#5C6B7A">Flight tracker · scan of {latest['run_date']} · for {user}</p>
          {''.join(sections)}
          <p style="margin-top:4px"><a href="{DASHBOARD}">Open the dashboard</a> ·
            prices indicative, links show live fares</p></div>"""
        if os.environ.get("DRY_RUN"):
            (ROOT / "data" / f"digest_preview_{user}.html").write_text(body)
            print(f"DRY_RUN {user}: wrote preview | {subject}")
            continue
        if not user_env or not pwd:
            print("email skipped: SMTP credentials not configured")
            return
        msg = MIMEMultipart("alternative")
        msg["Subject"], msg["From"], msg["To"] = subject, user_env, ", ".join(to)
        msg.attach(MIMEText(f"Flight tracker digest. Dashboard: {DASHBOARD}", "plain"))
        msg.attach(MIMEText(body, "html"))
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
            s.starttls()
            s.login(user_env, pwd)
            s.sendmail(user_env, to, msg.as_string())
        sent += 1
        print(f"digest sent to {user} ({len(to)} address(es)) | {subject}")
    if not by_user:
        print("no trips found, nothing to send")
    print(f"done: {sent} email(s) sent")


if __name__ == "__main__":
    main()
