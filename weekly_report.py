#!/usr/bin/env python3
"""Weekly Oura health report.

Fetches the last 7 days of Oura Ring data, generates a dashboard chart,
and emails it via Gmail SMTP.

Required env vars (.env):
    OURA_PAT              Personal Access Token from cloud.ouraring.com/personal-access-tokens
    GMAIL_USER            Gmail address used to send (and as From)
    GMAIL_APP_PASSWORD    16-char app password from myaccount.google.com/apppasswords
    EMAIL_TO              (optional) recipient; defaults to GMAIL_USER
"""
from __future__ import annotations

import os
import smtplib
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from io import BytesIO
from statistics import mean

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import requests
from dotenv import load_dotenv

OURA_BASE = "https://api.ouraring.com/v2/usercollection"
DAYS = 7


def env(name: str, default: str | None = None) -> str:
    val = os.environ.get(name, default)
    if not val:
        sys.exit(f"Missing required env var: {name}")
    return val


def oura_get(token: str, path: str, params: dict) -> list[dict]:
    """GET an Oura endpoint, following next_token pagination, return concatenated data."""
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{OURA_BASE}{path}"
    out: list[dict] = []
    while True:
        r = requests.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        body = r.json()
        out.extend(body.get("data", []))
        nxt = body.get("next_token")
        if not nxt:
            return out
        params = {**params, "next_token": nxt}


def fetch_all(token: str, start: date, end: date) -> dict:
    date_params = {"start_date": start.isoformat(), "end_date": end.isoformat()}
    dt_params = {
        "start_datetime": datetime.combine(start, time.min).isoformat(),
        "end_datetime": datetime.combine(end, time.max).replace(microsecond=0).isoformat(),
    }
    return {
        "daily_sleep": oura_get(token, "/daily_sleep", date_params),
        "sleep": oura_get(token, "/sleep", date_params),
        "daily_activity": oura_get(token, "/daily_activity", date_params),
        "daily_stress": oura_get(token, "/daily_stress", date_params),
        "heartrate": oura_get(token, "/heartrate", dt_params),
    }


def aggregate(raw: dict, days: list[date]) -> dict:
    keys = [d.isoformat() for d in days]

    def by_day(items: list[dict], field: str) -> list:
        m = {it["day"]: it.get(field) for it in items if "day" in it}
        return [m.get(k) for k in keys]

    sleep_score = by_day(raw["daily_sleep"], "score")

    # Pick the longest sleep session per day (people sometimes have naps)
    longest: dict[str, dict] = {}
    for s in raw["sleep"]:
        d = s.get("day")
        if not d:
            continue
        cur = longest.get(d)
        if cur is None or (s.get("total_sleep_duration") or 0) > (cur.get("total_sleep_duration") or 0):
            longest[d] = s
    deep  = [(longest.get(k, {}).get("deep_sleep_duration")  or 0) / 3600 for k in keys]
    rem   = [(longest.get(k, {}).get("rem_sleep_duration")   or 0) / 3600 for k in keys]
    light = [(longest.get(k, {}).get("light_sleep_duration") or 0) / 3600 for k in keys]
    total_sleep_hr = [d + r + l for d, r, l in zip(deep, rem, light)]

    activity_score = by_day(raw["daily_activity"], "score")
    steps          = by_day(raw["daily_activity"], "steps")
    calories       = by_day(raw["daily_activity"], "active_calories")

    # daily_stress exposes stress_high (seconds) — convert to minutes
    stress_min = [
        (v / 60) if v is not None else None
        for v in by_day(raw["daily_stress"], "stress_high")
    ]

    # Average heart rate per day (across all readings, regardless of source)
    hr_buckets: dict[str, list[int]] = defaultdict(list)
    for hr in raw["heartrate"]:
        ts = hr.get("timestamp")
        bpm = hr.get("bpm")
        if ts and bpm is not None:
            hr_buckets[ts[:10]].append(bpm)
    avg_hr = [round(mean(hr_buckets[k]), 1) if hr_buckets.get(k) else None for k in keys]

    return {
        "labels": [d.strftime("%a %m/%d") for d in days],
        "sleep_score": sleep_score,
        "deep": deep,
        "rem": rem,
        "light": light,
        "total_sleep_hr": total_sleep_hr,
        "activity_score": activity_score,
        "steps": steps,
        "calories": calories,
        "stress_min": stress_min,
        "avg_hr": avg_hr,
    }


def _safe_avg(xs: list) -> float | None:
    vals = [x for x in xs if x is not None]
    return round(mean(vals), 1) if vals else None


def _label_line(ax, xs, ys, fmt="{:.0f}"):
    for x, y in zip(xs, ys):
        if y is not None:
            ax.annotate(fmt.format(y), (x, y), textcoords="offset points",
                        xytext=(0, 6), ha="center", fontsize=8)


def _none_to_nan(xs):
    return [float("nan") if x is None else x for x in xs]


def render_dashboard(agg: dict, start: date, end: date) -> bytes:
    labels = agg["labels"]
    x = list(range(len(labels)))

    fig, axes = plt.subplots(3, 2, figsize=(11, 10))
    fig.suptitle(
        f"Oura Weekly Report  •  {start.strftime('%b %-d')} – {end.strftime('%b %-d, %Y')}",
        fontsize=14, fontweight="bold",
    )

    # (0,0) Sleep score
    ax = axes[0][0]
    ys = _none_to_nan(agg["sleep_score"])
    ax.plot(x, ys, marker="o", color="#5B6CFF", linewidth=2)
    _label_line(ax, x, agg["sleep_score"])
    ax.set_title("Sleep Score")
    ax.set_ylim(0, 100)
    ax.set_xticks(x); ax.set_xticklabels(labels, rotation=30, ha="right")
    ax.grid(alpha=0.3)

    # (0,1) Sleep stages stacked
    ax = axes[0][1]
    deep, rem, light = agg["deep"], agg["rem"], agg["light"]
    ax.bar(x, deep, label="Deep", color="#1B3B8B")
    ax.bar(x, rem, bottom=deep, label="REM", color="#7A8CFF")
    ax.bar(x, light, bottom=[d + r for d, r in zip(deep, rem)], label="Light", color="#C5CCFF")
    ax.set_title("Sleep Stages (hours)")
    ax.set_xticks(x); ax.set_xticklabels(labels, rotation=30, ha="right")
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(alpha=0.3, axis="y")

    # (1,0) Activity score + steps (twin y)
    ax = axes[1][0]
    ax.bar(x, _none_to_nan(agg["steps"]), color="#FFC59B", alpha=0.7, label="Steps")
    ax.set_ylabel("Steps", color="#B8651E")
    ax.tick_params(axis="y", labelcolor="#B8651E")
    ax2 = ax.twinx()
    ax2.plot(x, _none_to_nan(agg["activity_score"]), marker="o", color="#E8590C",
             linewidth=2, label="Score")
    _label_line(ax2, x, agg["activity_score"])
    ax2.set_ylim(0, 100)
    ax2.set_ylabel("Activity Score", color="#E8590C")
    ax2.tick_params(axis="y", labelcolor="#E8590C")
    ax.set_title("Activity Score & Steps")
    ax.set_xticks(x); ax.set_xticklabels(labels, rotation=30, ha="right")
    ax.grid(alpha=0.3, axis="y")

    # (1,1) Active calories
    ax = axes[1][1]
    cals = _none_to_nan(agg["calories"])
    ax.bar(x, cals, color="#F08C4B")
    for xi, c in zip(x, agg["calories"]):
        if c is not None:
            ax.annotate(f"{int(c)}", (xi, c), textcoords="offset points",
                        xytext=(0, 4), ha="center", fontsize=8)
    ax.set_title("Active Calories")
    ax.set_ylabel("kcal")
    ax.set_xticks(x); ax.set_xticklabels(labels, rotation=30, ha="right")
    ax.grid(alpha=0.3, axis="y")

    # (2,0) Stress (minutes in high-stress zone)
    ax = axes[2][0]
    ax.plot(x, _none_to_nan(agg["stress_min"]), marker="o", color="#D6336C", linewidth=2)
    _label_line(ax, x, agg["stress_min"], fmt="{:.0f}m")
    ax.set_title("High-Stress Time (minutes)")
    ax.set_ylabel("min")
    ax.set_xticks(x); ax.set_xticklabels(labels, rotation=30, ha="right")
    ax.grid(alpha=0.3)

    # (2,1) Avg heart rate
    ax = axes[2][1]
    ax.plot(x, _none_to_nan(agg["avg_hr"]), marker="o", color="#C92A2A", linewidth=2)
    _label_line(ax, x, agg["avg_hr"], fmt="{:.0f}")
    ax.set_title("Average Heart Rate (bpm)")
    ax.set_ylabel("bpm")
    ax.set_xticks(x); ax.set_xticklabels(labels, rotation=30, ha="right")
    ax.grid(alpha=0.3)

    fig.tight_layout(rect=(0, 0, 1, 0.96))
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=140, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def build_html(agg: dict, start: date, end: date) -> str:
    rows = [
        ("Avg sleep score", _safe_avg(agg["sleep_score"])),
        ("Avg sleep duration", f"{_safe_avg(agg['total_sleep_hr'])} h" if _safe_avg(agg["total_sleep_hr"]) else "—"),
        ("Avg activity score", _safe_avg(agg["activity_score"])),
        ("Total steps", sum(s for s in agg["steps"] if s) or "—"),
        ("Total active calories", sum(c for c in agg["calories"] if c) or "—"),
        ("Avg high-stress time", f"{_safe_avg(agg['stress_min'])} min" if _safe_avg(agg["stress_min"]) else "—"),
        ("Avg heart rate", f"{_safe_avg(agg['avg_hr'])} bpm" if _safe_avg(agg["avg_hr"]) else "—"),
    ]
    table = "".join(
        f"<tr><td style='padding:4px 14px 4px 0;color:#555'>{k}</td>"
        f"<td style='padding:4px 0;font-weight:600'>{v if v is not None else '—'}</td></tr>"
        for k, v in rows
    )
    return f"""\
<!doctype html>
<html><body style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#222;max-width:760px;margin:0 auto;padding:20px">
  <h2 style="margin:0 0 4px 0">Your Oura Weekly Report</h2>
  <div style="color:#777;margin-bottom:18px">{start.strftime('%b %-d')} – {end.strftime('%b %-d, %Y')}</div>
  <table style="border-collapse:collapse;margin-bottom:20px;font-size:14px">{table}</table>
  <img src="cid:dashboard" alt="Weekly dashboard" style="width:100%;max-width:720px;display:block">
  <div style="color:#999;font-size:12px;margin-top:18px">Generated by your local oura-email script.</div>
</body></html>
"""


def send_email(html: str, png: bytes, gmail_user: str, gmail_pass: str,
               recipient: str, subject: str) -> None:
    msg = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = recipient

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText("Your weekly Oura report (HTML required to view charts).", "plain"))
    alt.attach(MIMEText(html, "html"))
    msg.attach(alt)

    img = MIMEImage(png, _subtype="png")
    img.add_header("Content-ID", "<dashboard>")
    img.add_header("Content-Disposition", "inline", filename="dashboard.png")
    msg.attach(img)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_pass)
        server.send_message(msg)


def main() -> None:
    load_dotenv()
    pat = env("OURA_PAT")
    gmail_user = env("GMAIL_USER")
    gmail_pass = env("GMAIL_APP_PASSWORD")
    recipient = os.environ.get("EMAIL_TO") or gmail_user

    end = date.today()
    start = end - timedelta(days=DAYS - 1)
    days = [start + timedelta(days=i) for i in range(DAYS)]

    print(f"Fetching Oura data for {start} → {end}...")
    raw = fetch_all(pat, start, end)
    agg = aggregate(raw, days)

    print("Rendering dashboard...")
    png = render_dashboard(agg, start, end)

    subject = f"Oura Weekly Report — {start.strftime('%b %-d')} to {end.strftime('%b %-d')}"
    print(f"Sending email to {recipient}...")
    send_email(build_html(agg, start, end), png, gmail_user, gmail_pass, recipient, subject)
    print("Done.")


if __name__ == "__main__":
    main()
