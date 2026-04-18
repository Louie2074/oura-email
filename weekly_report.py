#!/usr/bin/env python3
"""Weekly Oura health report.

Fetches the last 7 days of Oura Ring data, renders one chart per metric,
and emails them in a styled HTML layout via Gmail SMTP.

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

# Color palette — keep in sync with HTML accents below
INDIGO = "#6366F1"
INDIGO_DEEP = "#3730A3"
INDIGO_MID = "#818CF8"
INDIGO_LIGHT = "#C7D2FE"
ORANGE = "#F97316"
AMBER = "#F59E0B"
PINK = "#EC4899"
RED = "#EF4444"
SLATE_TEXT = "#0F172A"
SLATE_MUTED = "#64748B"
SLATE_GRID = "#E2E8F0"


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
    return {
        "daily_sleep": oura_get(token, "/daily_sleep", date_params),
        "sleep": oura_get(token, "/sleep", date_params),
        "daily_activity": oura_get(token, "/daily_activity", date_params),
        "daily_stress": oura_get(token, "/daily_stress", date_params),
    }


def aggregate(raw: dict, days: list[date]) -> dict:
    keys = [d.isoformat() for d in days]

    def by_day(items: list[dict], field: str) -> list:
        m = {it["day"]: it.get(field) for it in items if "day" in it}
        return [m.get(k) for k in keys]

    sleep_score = by_day(raw["daily_sleep"], "score")

    # Pick the longest sleep session per day (filter out naps)
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
    resting_hr = [longest.get(k, {}).get("lowest_heart_rate") for k in keys]

    activity_score = by_day(raw["daily_activity"], "score")
    steps          = by_day(raw["daily_activity"], "steps")
    calories       = by_day(raw["daily_activity"], "active_calories")

    stress_min = [
        (v / 60) if v is not None else None
        for v in by_day(raw["daily_stress"], "stress_high")
    ]

    return {
        "labels": [d.strftime("%a") for d in days],
        "sub_labels": [d.strftime("%-m/%-d") for d in days],
        "sleep_score": sleep_score,
        "deep": deep,
        "rem": rem,
        "light": light,
        "total_sleep_hr": total_sleep_hr,
        "resting_hr": resting_hr,
        "activity_score": activity_score,
        "steps": steps,
        "calories": calories,
        "stress_min": stress_min,
    }


# ---------------------------------------------------------------------------
# Chart rendering
# ---------------------------------------------------------------------------

def _setup_style() -> None:
    plt.rcParams.update({
        "font.family": ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
        "font.size": 12,
        "axes.titlesize": 16,
        "axes.titleweight": "bold",
        "axes.titlepad": 14,
        "axes.titlelocation": "left",
        "axes.titlecolor": SLATE_TEXT,
        "axes.labelcolor": SLATE_MUTED,
        "axes.labelsize": 11,
        "axes.edgecolor": SLATE_GRID,
        "axes.linewidth": 1,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.spines.left": False,
        "axes.grid": True,
        "axes.axisbelow": True,
        "grid.color": SLATE_GRID,
        "grid.linewidth": 1,
        "grid.linestyle": "-",
        "xtick.color": SLATE_MUTED,
        "ytick.color": SLATE_MUTED,
        "xtick.major.size": 0,
        "ytick.major.size": 0,
        "xtick.labelsize": 11,
        "ytick.labelsize": 10,
        "figure.facecolor": "white",
        "axes.facecolor": "white",
    })


def _none_to_nan(xs):
    return [float("nan") if x is None else x for x in xs]


def _x_labels(agg):
    return [f"{d}\n{s}" for d, s in zip(agg["labels"], agg["sub_labels"])]


def _to_png(fig) -> bytes:
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=200, bbox_inches="tight",
                facecolor="white", pad_inches=0.3)
    plt.close(fig)
    return buf.getvalue()


def _annotate_points(ax, x, ys, color, fmt="{:.0f}", dy=14):
    for xi, y in zip(x, ys):
        if y is None:
            continue
        ax.annotate(fmt.format(y), (xi, y), textcoords="offset points",
                    xytext=(0, dy), ha="center", fontsize=11,
                    fontweight="bold", color=color)


def _line_chart(agg, key, title, color, fmt="{:.0f}", ymax=None, suffix=""):
    fig, ax = plt.subplots(figsize=(10, 3.8))
    x = list(range(len(agg["labels"])))
    ys = _none_to_nan(agg[key])
    ax.plot(x, ys, color=color, linewidth=2.5, zorder=3)
    ax.scatter(x, ys, s=110, color="white", edgecolors=color, linewidths=2.5, zorder=4)
    _annotate_points(ax, x, agg[key], color, fmt=fmt + suffix)
    ax.set_title(title)
    ax.set_xticks(x)
    ax.set_xticklabels(_x_labels(agg))
    if ymax is not None:
        # Headroom for labels
        cur_top = max([v for v in agg[key] if v is not None], default=0)
        ax.set_ylim(0, max(ymax, cur_top * 1.18))
    else:
        cur_top = max([v for v in agg[key] if v is not None], default=0)
        ax.set_ylim(0, cur_top * 1.25 if cur_top else 1)
    return _to_png(fig)


def _bar_chart(agg, key, title, color, fmt="{:.0f}"):
    fig, ax = plt.subplots(figsize=(10, 3.8))
    x = list(range(len(agg["labels"])))
    ys = _none_to_nan(agg[key])
    ax.bar(x, ys, color=color, width=0.55, zorder=3)
    for xi, y in zip(x, agg[key]):
        if y is None:
            continue
        ax.annotate(fmt.format(y), (xi, y), textcoords="offset points",
                    xytext=(0, 6), ha="center", fontsize=11,
                    fontweight="bold", color=SLATE_TEXT)
    ax.set_title(title)
    ax.set_xticks(x)
    ax.set_xticklabels(_x_labels(agg))
    cur_top = max([v for v in agg[key] if v is not None], default=0)
    ax.set_ylim(0, cur_top * 1.20 if cur_top else 1)
    return _to_png(fig)


def render_sleep_score(agg) -> bytes:
    return _line_chart(agg, "sleep_score", "Sleep Score", INDIGO, ymax=100)


def render_sleep_stages(agg) -> bytes:
    fig, ax = plt.subplots(figsize=(10, 3.8))
    x = list(range(len(agg["labels"])))
    deep = agg["deep"]; rem = agg["rem"]; light = agg["light"]
    ax.bar(x, deep,  color=INDIGO_DEEP,  width=0.55, label="Deep", zorder=3)
    ax.bar(x, rem,   bottom=deep, color=INDIGO_MID,  width=0.55, label="REM",  zorder=3)
    ax.bar(x, light, bottom=[a+b for a, b in zip(deep, rem)],
           color=INDIGO_LIGHT, width=0.55, label="Light", zorder=3)
    totals = [d+r+l for d, r, l in zip(deep, rem, light)]
    for xi, t in zip(x, totals):
        if t > 0:
            ax.annotate(f"{t:.1f}h", (xi, t), textcoords="offset points",
                        xytext=(0, 6), ha="center", fontsize=11,
                        fontweight="bold", color=SLATE_TEXT)
    ax.set_title("Sleep Stages (hours)")
    ax.set_xticks(x); ax.set_xticklabels(_x_labels(agg))
    ax.legend(loc="upper right", frameon=False, fontsize=10,
              labelcolor=SLATE_MUTED, ncols=3)
    cur_top = max(totals, default=0)
    ax.set_ylim(0, cur_top * 1.25 if cur_top else 1)
    return _to_png(fig)


def render_activity_score(agg) -> bytes:
    return _line_chart(agg, "activity_score", "Activity Score", ORANGE, ymax=100)


def render_steps(agg) -> bytes:
    return _bar_chart(agg, "steps", "Steps", ORANGE, fmt="{:,.0f}")


def render_calories(agg) -> bytes:
    return _bar_chart(agg, "calories", "Active Calories", AMBER, fmt="{:,.0f}")


def render_stress(agg) -> bytes:
    return _line_chart(agg, "stress_min", "High-Stress Time (minutes)", PINK, suffix="m")


def render_resting_hr(agg) -> bytes:
    return _line_chart(agg, "resting_hr", "Resting Heart Rate (bpm)", RED)


# ---------------------------------------------------------------------------
# HTML email
# ---------------------------------------------------------------------------

def _safe_avg(xs: list) -> float | None:
    vals = [x for x in xs if x is not None]
    return round(mean(vals), 1) if vals else None


def _safe_sum(xs: list) -> int | None:
    vals = [x for x in xs if x is not None]
    return int(sum(vals)) if vals else None


def _stat_card(label: str, value: str, sublabel: str, accent: str) -> str:
    return f"""
    <td valign="top" align="center" width="50%" style="padding:6px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#ffffff;border:1px solid {SLATE_GRID};border-radius:14px;">
        <tr><td align="center" style="padding:18px 14px 16px 14px;">
          <div style="font:600 11px/1 -apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;letter-spacing:1.2px;text-transform:uppercase;color:{SLATE_MUTED};margin-bottom:8px;">{label}</div>
          <div style="font:700 30px/1.1 -apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:{accent};margin-bottom:6px;">{value}</div>
          <div style="font:500 12px/1.2 -apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:{SLATE_MUTED};">{sublabel}</div>
        </td></tr>
      </table>
    </td>
    """


def _chart_card(cid: str) -> str:
    return f"""
    <tr><td style="padding:0 6px 12px 6px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#ffffff;border:1px solid {SLATE_GRID};border-radius:14px;">
        <tr><td align="center" style="padding:18px 14px;">
          <img src="cid:{cid}" alt="" style="display:block;width:100%;max-width:640px;height:auto;border:0;outline:none;">
        </td></tr>
      </table>
    </td></tr>
    """


def _section_header(title: str, subtitle: str) -> str:
    return f"""
    <tr><td style="padding:24px 6px 8px 6px;">
      <div style="font:700 20px/1.2 -apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:{SLATE_TEXT};margin-bottom:2px;">{title}</div>
      <div style="font:400 13px/1.4 -apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:{SLATE_MUTED};">{subtitle}</div>
    </td></tr>
    """


def build_html(agg: dict, start: date, end: date) -> str:
    avg_sleep = _safe_avg(agg["sleep_score"])
    avg_sleep_dur = _safe_avg(agg["total_sleep_hr"])
    avg_act = _safe_avg(agg["activity_score"])
    total_steps = _safe_sum(agg["steps"])
    total_cal = _safe_sum(agg["calories"])
    avg_rhr = _safe_avg(agg["resting_hr"])

    def fmt(v, suffix=""):
        if v is None:
            return "—"
        if isinstance(v, float):
            return f"{v:g}{suffix}"
        return f"{v:,}{suffix}"

    cards_html = f"""
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:6px;">
      <tr>
        {_stat_card("Sleep Score", fmt(avg_sleep), "weekly avg", INDIGO)}
        {_stat_card("Sleep Duration", fmt(avg_sleep_dur, " h"), "weekly avg", INDIGO)}
      </tr>
      <tr>
        {_stat_card("Activity Score", fmt(avg_act), "weekly avg", ORANGE)}
        {_stat_card("Total Steps", fmt(total_steps), "this week", ORANGE)}
      </tr>
      <tr>
        {_stat_card("Active Calories", fmt(total_cal), "this week", AMBER)}
        {_stat_card("Resting HR", fmt(avg_rhr, " bpm"), "weekly avg", RED)}
      </tr>
    </table>
    """

    date_range = f"{start.strftime('%B %-d')} – {end.strftime('%B %-d, %Y')}"

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#F8FAFC;-webkit-font-smoothing:antialiased;">
  <center style="width:100%;background:#F8FAFC;padding:24px 12px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:680px;margin:0 auto;">

      <!-- Hero -->
      <tr><td style="padding:8px 6px 20px 6px;">
        <div style="font:600 12px/1 -apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;letter-spacing:1.4px;text-transform:uppercase;color:{INDIGO};margin-bottom:8px;">Weekly Health Report</div>
        <div style="font:700 30px/1.2 -apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:{SLATE_TEXT};margin-bottom:4px;">Your Oura Recap</div>
        <div style="font:400 15px/1.4 -apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:{SLATE_MUTED};">{date_range}</div>
      </td></tr>

      <!-- Stat cards -->
      <tr><td>{cards_html}</td></tr>

      <!-- Sleep section -->
      {_section_header("Sleep", "How well and how long you rested")}
      <tr><td>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
          {_chart_card("sleep_score")}
          {_chart_card("sleep_stages")}
        </table>
      </td></tr>

      <!-- Activity section -->
      {_section_header("Activity", "Movement and energy expenditure")}
      <tr><td>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
          {_chart_card("activity_score")}
          {_chart_card("steps")}
          {_chart_card("calories")}
        </table>
      </td></tr>

      <!-- Wellness section -->
      {_section_header("Wellness", "Stress load and cardiovascular recovery")}
      <tr><td>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
          {_chart_card("stress")}
          {_chart_card("resting_hr")}
        </table>
      </td></tr>

      <!-- Footer -->
      <tr><td align="center" style="padding:28px 6px 12px 6px;">
        <div style="font:400 12px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:{SLATE_MUTED};">
          Generated automatically from your Oura Ring data.
        </div>
      </td></tr>

    </table>
  </center>
</body></html>
"""


def send_email(html: str, images: dict[str, bytes], gmail_user: str, gmail_pass: str,
               recipient: str, subject: str) -> None:
    msg = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = recipient

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText("Your weekly Oura report (HTML required to view charts).", "plain"))
    alt.attach(MIMEText(html, "html"))
    msg.attach(alt)

    for cid, png in images.items():
        img = MIMEImage(png, _subtype="png")
        img.add_header("Content-ID", f"<{cid}>")
        img.add_header("Content-Disposition", "inline", filename=f"{cid}.png")
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

    print("Rendering charts...")
    _setup_style()
    images = {
        "sleep_score":  render_sleep_score(agg),
        "sleep_stages": render_sleep_stages(agg),
        "activity_score": render_activity_score(agg),
        "steps":        render_steps(agg),
        "calories":     render_calories(agg),
        "stress":       render_stress(agg),
        "resting_hr":   render_resting_hr(agg),
    }

    if "--dry-run" in sys.argv:
        out = "preview.html"
        with open(out, "w") as f:
            f.write(build_html(agg, start, end)
                    .replace('src="cid:', 'src="').replace('"', '"'))
        # Also save chart PNGs alongside so the preview HTML can reference them
        for cid, png in images.items():
            with open(f"{cid}.png", "wb") as f:
                f.write(png)
        # Rewrite preview.html to reference local files
        html = build_html(agg, start, end)
        for cid in images:
            html = html.replace(f"cid:{cid}", f"{cid}.png")
        with open(out, "w") as f:
            f.write(html)
        print(f"Wrote {out} and {len(images)} chart PNGs. Open {out} in a browser.")
        return

    subject = f"Your Oura Weekly Report — {start.strftime('%b %-d')} to {end.strftime('%b %-d')}"
    print(f"Sending email to {recipient}...")
    send_email(build_html(agg, start, end), images, gmail_user, gmail_pass, recipient, subject)
    print("Done.")


if __name__ == "__main__":
    main()
