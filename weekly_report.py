#!/usr/bin/env python3
"""Weekly Oura health report.

Fetches the last 14 days of Oura data (last 7 + prior 7 for WoW comparison),
renders one chart per metric with target zones + best/worst callouts,
and emails a styled HTML layout via Gmail SMTP.

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
from datetime import date, datetime, timedelta
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from io import BytesIO
from statistics import mean, median

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import requests
from dotenv import load_dotenv
from matplotlib.colors import LinearSegmentedColormap

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
GREEN = "#10B981"
GREEN_DARK = "#047857"
SLATE_TEXT = "#0F172A"
SLATE_MUTED = "#64748B"
SLATE_GRID = "#E2E8F0"

# Benchmarks / targets
SLEEP_EFFICIENCY_GOOD = (85, 100)
RHR_GOOD = (50, 65)
ACTIVE_MIN_TARGET_PER_DAY = 21  # WHO: 150 min/week ≈ 21 min/day
STEPS_TARGET = 10_000

FONT_STACK = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"


# ---------------------------------------------------------------------------
# Fetch + aggregate
# ---------------------------------------------------------------------------

def env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"Missing required env var: {name}")
    return val


def oura_get(token: str, path: str, params: dict) -> list[dict]:
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
    dp = {"start_date": start.isoformat(), "end_date": end.isoformat()}
    # /heartrate uses datetime and needs an inclusive right edge — pad by a day
    hr_params = {
        "start_datetime": f"{start.isoformat()}T00:00:00+00:00",
        "end_datetime":   f"{(end + timedelta(days=1)).isoformat()}T00:00:00+00:00",
    }
    return {
        "sleep":          oura_get(token, "/sleep", dp),
        "daily_activity": oura_get(token, "/daily_activity", dp),
        "daily_stress":   oura_get(token, "/daily_stress", dp),
        "heartrate":      oura_get(token, "/heartrate", hr_params),
    }


def aggregate(raw: dict, days: list[date]) -> dict:
    keys = [d.isoformat() for d in days]

    def by_day(items, field):
        m = {it["day"]: it.get(field) for it in items if "day" in it}
        return [m.get(k) for k in keys]

    # Pick longest sleep session per day (filter out naps)
    longest: dict[str, dict] = {}
    for s in raw["sleep"]:
        d = s.get("day")
        if not d:
            continue
        cur = longest.get(d)
        if cur is None or (s.get("total_sleep_duration") or 0) > (cur.get("total_sleep_duration") or 0):
            longest[d] = s

    def _hours(k, field):
        v = longest.get(k, {}).get(field)
        return v / 3600 if v is not None else None

    deep  = [_hours(k, "deep_sleep_duration")  for k in keys]
    rem   = [_hours(k, "rem_sleep_duration")   for k in keys]
    light = [_hours(k, "light_sleep_duration") for k in keys]
    total_sleep_hr = [
        (d + r + l) if None not in (d, r, l) else None
        for d, r, l in zip(deep, rem, light)
    ]
    resting_hr       = [longest.get(k, {}).get("lowest_heart_rate") for k in keys]
    sleep_efficiency = [longest.get(k, {}).get("efficiency")        for k in keys]

    steps    = by_day(raw["daily_activity"], "steps")
    calories = by_day(raw["daily_activity"], "active_calories")

    def sec_to_min(xs):
        return [(v / 60) if v is not None else None for v in xs]

    high_activity_min   = sec_to_min(by_day(raw["daily_activity"], "high_activity_time"))
    medium_activity_min = sec_to_min(by_day(raw["daily_activity"], "medium_activity_time"))

    stress_min   = sec_to_min(by_day(raw["daily_stress"], "stress_high"))
    recovery_min = sec_to_min(by_day(raw["daily_stress"], "recovery_high"))

    # Time-of-day stress: excess BPM above baseline, bucketed by (day, hour-of-day)
    rhr_vals = [v for v in resting_hr if v is not None]
    baseline_hr = int(round(median(rhr_vals))) if rhr_vals else 65
    stress_clock, stress_peaks = _stress_clock(raw.get("heartrate", []), days, baseline_hr)

    return {
        "labels":     [d.strftime("%a") for d in days],
        "sub_labels": [d.strftime("%-m/%-d") for d in days],
        "sleep_efficiency": sleep_efficiency,
        "deep": deep, "rem": rem, "light": light,
        "total_sleep_hr": total_sleep_hr,
        "resting_hr": resting_hr,
        "high_activity_min": high_activity_min,
        "medium_activity_min": medium_activity_min,
        "steps": steps,
        "calories": calories,
        "stress_min": stress_min,
        "recovery_min": recovery_min,
        "stress_clock": stress_clock,
        "stress_peaks": stress_peaks,
        "baseline_hr": baseline_hr,
    }


MIN_SAMPLES_FOR_PEAK = 10  # Typical full-hour coverage is ~12 samples (5-min cadence)


def _stress_clock(samples: list[dict], days: list[date], baseline: int):
    """Bucket waking, non-workout HR samples into a 7×24 grid.

    Each cell = max(0, mean(bpm) − baseline) for samples that fall in that
    (day, hour) bucket. Empty cells are None. Returns (grid, top_peaks) where
    top_peaks is a list of (row_index, hour, excess) sorted desc, capped at 3.
    Peaks only consider cells with MIN_SAMPLES_FOR_PEAK+ samples so that sparse
    buckets (e.g. brief ring-wear near bedtime) can't produce false positives.
    """
    WAKING = {"awake", "rest", "live"}  # exclude sleep/workout/session
    buckets: dict[tuple[str, int], list[int]] = defaultdict(list)
    for s in samples:
        if s.get("source") not in WAKING:
            continue
        ts_raw = s.get("timestamp")
        bpm = s.get("bpm")
        if not ts_raw or bpm is None:
            continue
        try:
            ts = datetime.fromisoformat(ts_raw)
        except ValueError:
            continue
        buckets[(ts.date().isoformat(), ts.hour)].append(bpm)

    grid: list[list[float | None]] = []
    peak_candidates: list[tuple[int, int, float]] = []
    for i, d in enumerate(days):
        row: list[float | None] = []
        key = d.isoformat()
        for h in range(24):
            vals = buckets.get((key, h))
            if not vals:
                row.append(None)
                continue
            excess = max(0.0, sum(vals) / len(vals) - baseline)
            row.append(excess)
            if excess > 0 and len(vals) >= MIN_SAMPLES_FOR_PEAK:
                peak_candidates.append((i, h, excess))
        grid.append(row)
    peak_candidates.sort(key=lambda p: -p[2])
    return grid, peak_candidates[:3]


# ---------------------------------------------------------------------------
# Chart rendering
# ---------------------------------------------------------------------------

def _setup_style():
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


def _extremes(values, good_is_high=True):
    """Return (best_i, best_v, worst_i, worst_v) or None if fewer than 2 points."""
    pairs = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(pairs) < 2:
        return None
    max_i, max_v = max(pairs, key=lambda p: p[1])
    min_i, min_v = min(pairs, key=lambda p: p[1])
    if good_is_high:
        return max_i, max_v, min_i, min_v
    return min_i, min_v, max_i, max_v


def _annotate_extreme_labels(ax, labels, extremes, y_best, y_worst,
                             best_color, worst_color, dy_best=22, dy_worst=-22):
    best_i, best_v, worst_i, worst_v = extremes
    ax.annotate(f"Best · {labels[best_i]}",
                (best_i, y_best), textcoords="offset points",
                xytext=(0, dy_best), ha="center", fontsize=9, fontweight="bold",
                color=best_color)
    ax.annotate(f"Low · {labels[worst_i]}",
                (worst_i, y_worst), textcoords="offset points",
                xytext=(0, dy_worst), ha="center", va="top", fontsize=9, fontweight="bold",
                color=worst_color)


def _line_chart(agg, key, title, color, fmt="{:.0f}", suffix="", ymax=None,
                target_zone=None, target_label=None, good_is_high=True):
    fig, ax = plt.subplots(figsize=(10, 3.8))
    x = list(range(len(agg["labels"])))
    values = agg[key]
    ys = _none_to_nan(values)

    if target_zone:
        ax.axhspan(target_zone[0], target_zone[1], color=GREEN, alpha=0.08, zorder=1)
        if target_label:
            ax.text(len(x) - 0.5, target_zone[1], target_label,
                    color=GREEN_DARK, fontsize=9, fontweight="bold",
                    ha="right", va="bottom", alpha=0.85)

    ax.plot(x, ys, color=color, linewidth=2.5, zorder=3)
    ax.scatter(x, ys, s=110, color="white", edgecolors=color, linewidths=2.5, zorder=4)

    ex = _extremes(values, good_is_high=good_is_high)
    if ex:
        best_i, best_v, worst_i, worst_v = ex
        ax.scatter([best_i], [best_v], s=180, color=GREEN, zorder=6,
                   edgecolors="white", linewidths=2)
        ax.scatter([worst_i], [worst_v], s=180, color=SLATE_MUTED, zorder=6,
                   edgecolors="white", linewidths=2)

    for xi, y in zip(x, values):
        if y is None:
            continue
        ax.annotate(fmt.format(y) + suffix, (xi, y), textcoords="offset points",
                    xytext=(0, 14), ha="center", fontsize=11,
                    fontweight="bold", color=color)

    ax.set_title(title)
    ax.set_xticks(x); ax.set_xticklabels(_x_labels(agg))

    cur_top = max([v for v in values if v is not None], default=0)
    cur_bot = min([v for v in values if v is not None], default=0)
    if ymax is not None:
        top = max(ymax, cur_top * 1.18)
    else:
        top = cur_top * 1.25 if cur_top else 1
    bot = 0 if cur_bot >= 0 else cur_bot * 1.1
    # For RHR, don't start at 0 — it squashes the signal
    if key == "resting_hr" and cur_bot > 0:
        bot = max(0, cur_bot - 5)
        top = cur_top + 8
    ax.set_ylim(bot, top)
    return _to_png(fig)


def render_sleep_efficiency(agg):
    return _line_chart(agg, "sleep_efficiency", "Sleep Efficiency",
                       INDIGO, suffix="%", ymax=100,
                       target_zone=SLEEP_EFFICIENCY_GOOD,
                       target_label="target zone",
                       good_is_high=True)


def render_sleep_stages(agg):
    fig, ax = plt.subplots(figsize=(10, 4.2))
    x = list(range(len(agg["labels"])))
    deep  = _none_to_nan(agg["deep"])
    rem   = _none_to_nan(agg["rem"])
    light = _none_to_nan(agg["light"])

    ax.bar(x, deep,  color=INDIGO_DEEP,  width=0.6, label="Deep", zorder=3)
    ax.bar(x, rem,   bottom=deep, color=INDIGO_MID,  width=0.6, label="REM", zorder=3)
    ax.bar(x, light, bottom=[a + b for a, b in zip(deep, rem)],
           color=INDIGO_LIGHT, width=0.6, label="Light", zorder=3)

    # Per-segment labels, centered inside each colored region (skip tiny / missing).
    # NaN comparisons always return False, so missing days drop out naturally.
    MIN_SEG = 0.4
    for xi, d, r, l in zip(x, deep, rem, light):
        if d >= MIN_SEG:
            ax.text(xi, d / 2, f"{d:.1f}h", ha="center", va="center",
                    fontsize=10, fontweight="bold", color="white")
        if r >= MIN_SEG:
            ax.text(xi, d + r / 2, f"{r:.1f}h", ha="center", va="center",
                    fontsize=10, fontweight="bold", color="white")
        if l >= MIN_SEG:
            ax.text(xi, d + r + l / 2, f"{l:.1f}h", ha="center", va="center",
                    fontsize=10, fontweight="bold", color=INDIGO_DEEP)

    totals = agg["total_sleep_hr"]
    for xi, t in zip(x, totals):
        if t is not None and t > 0:
            ax.annotate(f"{t:.1f}h total", (xi, t),
                        textcoords="offset points", xytext=(0, 6),
                        ha="center", fontsize=10, fontweight="bold",
                        color=SLATE_TEXT)

    ax.set_title("Sleep Stages")
    ax.set_xticks(x); ax.set_xticklabels(_x_labels(agg))
    ax.legend(loc="upper right", frameon=False, fontsize=10,
              labelcolor=SLATE_MUTED, ncols=3)
    cur_top = max([t for t in totals if t is not None], default=0)
    ax.set_ylim(0, cur_top * 1.30 if cur_top else 1)
    return _to_png(fig)


def render_activity_minutes(agg):
    fig, ax = plt.subplots(figsize=(10, 4.2))
    x = list(range(len(agg["labels"])))
    high = [v or 0 for v in agg["high_activity_min"]]
    med  = [v or 0 for v in agg["medium_activity_min"]]

    ax.bar(x, high, color=ORANGE, width=0.6, label="High intensity", zorder=3)
    ax.bar(x, med,  bottom=high, color=AMBER, width=0.6,
           label="Moderate intensity", zorder=3)

    # WHO target line (21 min/day)
    ax.axhline(ACTIVE_MIN_TARGET_PER_DAY, color=GREEN, linewidth=1.5,
               linestyle="--", zorder=2, alpha=0.8)
    ax.text(len(x) - 0.5, ACTIVE_MIN_TARGET_PER_DAY + 1.5,
            f"{ACTIVE_MIN_TARGET_PER_DAY} min daily goal",
            color=GREEN_DARK, fontsize=9, fontweight="bold",
            ha="right", va="bottom", alpha=0.85)

    # Per-segment labels
    MIN_SEG = 4
    for xi, h, m in zip(x, high, med):
        if h >= MIN_SEG:
            ax.text(xi, h / 2, f"{h:.0f}", ha="center", va="center",
                    fontsize=10, fontweight="bold", color="white")
        if m >= MIN_SEG:
            ax.text(xi, h + m / 2, f"{m:.0f}", ha="center", va="center",
                    fontsize=10, fontweight="bold", color="white")

    totals = [h + m for h, m in zip(high, med)]
    for xi, t in zip(x, totals):
        if t > 0:
            ax.annotate(f"{t:.0f}m", (xi, t), textcoords="offset points",
                        xytext=(0, 6), ha="center", fontsize=10,
                        fontweight="bold", color=SLATE_TEXT)
    ax.set_title("Active Minutes")
    ax.set_xticks(x); ax.set_xticklabels(_x_labels(agg))
    ax.legend(loc="upper left", frameon=False, fontsize=10,
              labelcolor=SLATE_MUTED, ncols=2)
    cur_top = max(totals, default=0)
    ax.set_ylim(0, max(ACTIVE_MIN_TARGET_PER_DAY * 1.8, cur_top * 1.30))
    return _to_png(fig)


def _bar_with_highlight(agg, key, title, color, fmt="{:.0f}",
                        target=None, target_label=None):
    fig, ax = plt.subplots(figsize=(10, 3.8))
    x = list(range(len(agg["labels"])))
    values = agg[key]
    ys = _none_to_nan(values)

    ex = _extremes(values, good_is_high=True)
    bars = ax.bar(x, ys, color=color, width=0.6, zorder=3, alpha=0.55)
    if ex:
        best_i, _, worst_i, _ = ex
        bars[best_i].set_alpha(1.0)
        bars[worst_i].set_alpha(0.3)

    if target is not None:
        ax.axhline(target, color=GREEN, linewidth=1.5, linestyle="--",
                   zorder=2, alpha=0.8)
        if target_label:
            ax.text(len(x) - 0.5, target * 1.01, target_label,
                    color=GREEN_DARK, fontsize=9, fontweight="bold",
                    ha="right", va="bottom", alpha=0.85)

    for xi, y in zip(x, values):
        if y is None:
            continue
        prefix = "★ " if ex and xi == ex[0] else ""
        ax.annotate(prefix + fmt.format(y), (xi, y),
                    textcoords="offset points", xytext=(0, 6),
                    ha="center", fontsize=11, fontweight="bold",
                    color=SLATE_TEXT)

    ax.set_title(title)
    ax.set_xticks(x); ax.set_xticklabels(_x_labels(agg))
    cur_top = max([v for v in values if v is not None], default=0)
    ymax_val = max(cur_top, target or 0) * 1.22
    ax.set_ylim(0, ymax_val if ymax_val else 1)
    return _to_png(fig)


def render_steps(agg):
    return _bar_with_highlight(agg, "steps", "Steps", ORANGE, fmt="{:,.0f}",
                               target=STEPS_TARGET,
                               target_label=f"{STEPS_TARGET:,} step goal")


def render_calories(agg):
    return _bar_with_highlight(agg, "calories", "Active Calories", AMBER,
                               fmt="{:,.0f}")


def render_stress_recovery(agg):
    fig, ax = plt.subplots(figsize=(10, 4.2))
    x = list(range(len(agg["labels"])))
    recovery = [v or 0 for v in agg["recovery_min"]]
    stress   = [v or 0 for v in agg["stress_min"]]

    ax.bar(x, recovery, color=GREEN, width=0.6, zorder=3,
           label="Recovery time")
    ax.bar(x, [-s for s in stress], color=PINK, width=0.6, zorder=3,
           label="High-stress time")
    ax.axhline(0, color=SLATE_TEXT, linewidth=1, zorder=4)

    for xi, r in zip(x, recovery):
        if r > 0:
            ax.annotate(f"{r:.0f}m", (xi, r), textcoords="offset points",
                        xytext=(0, 4), ha="center", fontsize=10,
                        fontweight="bold", color=GREEN_DARK)
    for xi, s in zip(x, stress):
        if s > 0:
            ax.annotate(f"{s:.0f}m", (xi, -s), textcoords="offset points",
                        xytext=(0, -4), ha="center", va="top",
                        fontsize=10, fontweight="bold", color=PINK)

    ax.set_title("Stress vs. Recovery")
    ax.set_xticks(x); ax.set_xticklabels(_x_labels(agg))
    ax.legend(loc="upper right", frameon=False, fontsize=10,
              labelcolor=SLATE_MUTED, ncols=2)

    max_abs = max(max(recovery, default=0), max(stress, default=0))
    ymax_val = max_abs * 1.40 if max_abs else 1
    ax.set_ylim(-ymax_val, ymax_val)
    ax.set_yticks([])  # signed axis labels add clutter; colors speak for themselves
    return _to_png(fig)


def render_resting_hr(agg):
    return _line_chart(agg, "resting_hr", "Resting Heart Rate",
                       RED, suffix=" bpm",
                       target_zone=RHR_GOOD,
                       target_label="healthy range",
                       good_is_high=False)


STRESS_CMAP = LinearSegmentedColormap.from_list(
    "stress", ["#FFF1F2", "#FBCFE8", "#F9A8D4", "#EC4899", "#BE185D"])


def render_stress_clock(agg):
    """7×24 heatmap — day × hour-of-day, colored by excess BPM over resting."""
    grid_raw = agg["stress_clock"]
    if not grid_raw or not any(any(v is not None for v in row) for row in grid_raw):
        # No heartrate data — render empty placeholder so the layout stays consistent
        fig, ax = plt.subplots(figsize=(10, 3.5))
        ax.text(0.5, 0.5, "No heart-rate samples available",
                ha="center", va="center", color=SLATE_MUTED, fontsize=12,
                transform=ax.transAxes)
        ax.set_axis_off()
        return _to_png(fig)

    grid = np.array(
        [[float("nan") if v is None else v for v in row] for row in grid_raw],
        dtype=float,
    )
    masked = np.ma.masked_invalid(grid)
    vmax = max(10.0, float(np.ma.max(masked)) if masked.count() else 10.0)

    cmap = STRESS_CMAP.copy()
    cmap.set_bad(color="#F1F5F9")  # slate-100 for no-wear/missing

    fig, ax = plt.subplots(figsize=(10, 4.8))
    im = ax.imshow(masked, aspect="auto", cmap=cmap, vmin=0, vmax=vmax,
                   interpolation="nearest")

    # Cell borders for readability
    ax.set_xticks(np.arange(-0.5, 24, 1), minor=True)
    ax.set_yticks(np.arange(-0.5, len(agg["labels"]), 1), minor=True)
    ax.grid(which="minor", color="white", linewidth=1.2)
    ax.grid(which="major", visible=False)
    ax.tick_params(which="minor", length=0)

    ax.set_title(f"Stress Pattern by Hour  ·  baseline {agg['baseline_hr']} bpm")
    ax.set_xticks(range(0, 24, 2))
    ax.set_xticklabels([f"{h:02d}" for h in range(0, 24, 2)], fontsize=10)
    ax.set_yticks(range(len(agg["labels"])))
    ax.set_yticklabels([f"{d}  {s}" for d, s in zip(agg["labels"], agg["sub_labels"])],
                        fontsize=10)
    ax.set_xlabel("Hour of day (local)", color=SLATE_MUTED, fontsize=10)

    # Circle + annotate top 3 peak hours
    for row_i, hour, excess in agg["stress_peaks"]:
        ax.scatter([hour], [row_i], s=180, facecolors="none",
                   edgecolors=SLATE_TEXT, linewidths=1.8, zorder=5)
        ax.annotate(f"+{excess:.0f}", (hour, row_i),
                    textcoords="offset points", xytext=(0, -18),
                    ha="center", fontsize=9, fontweight="bold",
                    color=SLATE_TEXT,
                    bbox=dict(boxstyle="round,pad=0.2", fc="white",
                              ec=SLATE_GRID, lw=0.8))

    cbar = fig.colorbar(im, ax=ax, pad=0.015, fraction=0.03)
    cbar.set_label("bpm over resting", color=SLATE_MUTED, fontsize=9)
    cbar.ax.tick_params(colors=SLATE_MUTED, labelsize=9)
    cbar.outline.set_visible(False)

    return _to_png(fig)


# ---------------------------------------------------------------------------
# HTML email
# ---------------------------------------------------------------------------

def _safe_avg(xs):
    vals = [x for x in xs if x is not None]
    return round(mean(vals), 1) if vals else None


def _safe_sum(xs):
    vals = [x for x in xs if x is not None]
    return int(sum(vals)) if vals else None


def _sparkline(values, accent):
    """Tiny inline SVG trend line — sharper than a PNG, zero extra deps."""
    width, height = 92, 28
    vs = [v for v in values if v is not None]
    if len(vs) < 2:
        return ""
    vmin, vmax = min(vs), max(vs)
    rng = max(vmax - vmin, 1e-9)
    pts = []
    last = None
    for i, v in enumerate(values):
        if v is None:
            continue
        x = 3 + (i / max(len(values) - 1, 1)) * (width - 6)
        y = 3 + (1 - (v - vmin) / rng) * (height - 6)
        pts.append(f"{x:.1f},{y:.1f}")
        last = (x, y)
    poly = f'<polyline points="{" ".join(pts)}" fill="none" stroke="{accent}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>'
    dot = f'<circle cx="{last[0]:.1f}" cy="{last[1]:.1f}" r="2.2" fill="{accent}"/>' if last else ""
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg" style="display:block;margin:10px auto 0 auto;">'
        f'{poly}{dot}</svg>'
    )


def _delta_html(cur, prev, good_is_up=True, suffix="", fmt="{:+.1f}"):
    if cur is None or prev is None:
        return ""
    delta = cur - prev
    if abs(delta) < 0.05:
        return (f'<div style="font:500 11px/1 {FONT_STACK};'
                f'color:{SLATE_MUTED};margin-top:4px;">— flat vs last week</div>')
    improving = (delta > 0) == good_is_up
    color = GREEN_DARK if improving else RED
    arrow = "↑" if delta > 0 else "↓"
    return (f'<div style="font:600 11px/1 {FONT_STACK};'
            f'color:{color};margin-top:4px;">'
            f'{arrow} {fmt.format(delta)}{suffix} vs last week</div>')


def _stat_card(label, value, sublabel, accent, delta_html="", sparkline_svg=""):
    return f"""
    <td valign="top" align="center" width="50%" style="padding:6px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#ffffff;border:1px solid {SLATE_GRID};border-radius:14px;">
        <tr><td align="center" style="padding:18px 14px 16px 14px;">
          <div style="font:600 11px/1 {FONT_STACK};letter-spacing:1.2px;text-transform:uppercase;color:{SLATE_MUTED};margin-bottom:8px;">{label}</div>
          <div style="font:700 30px/1.1 {FONT_STACK};color:{accent};margin-bottom:6px;">{value}</div>
          <div style="font:500 12px/1.2 {FONT_STACK};color:{SLATE_MUTED};">{sublabel}</div>
          {delta_html}
          {sparkline_svg}
        </td></tr>
      </table>
    </td>
    """


def _chart_card(cid):
    return f"""
    <tr><td style="padding:0 6px 12px 6px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#ffffff;border:1px solid {SLATE_GRID};border-radius:14px;">
        <tr><td align="center" style="padding:18px 14px;">
          <img src="cid:{cid}" alt="" style="display:block;width:100%;max-width:640px;height:auto;border:0;outline:none;">
        </td></tr>
      </table>
    </td></tr>
    """


def _section(title, subtitle, narrative, accent, chart_cids):
    charts_html = "".join(_chart_card(c) for c in chart_cids)
    return f"""
    <tr><td style="padding:24px 6px 8px 6px;">
      <div style="font:700 20px/1.2 {FONT_STACK};color:{SLATE_TEXT};margin-bottom:2px;">{title}</div>
      <div style="font:400 13px/1.4 {FONT_STACK};color:{SLATE_MUTED};margin-bottom:10px;">{subtitle}</div>
      <div style="font:500 14px/1.5 {FONT_STACK};color:{SLATE_TEXT};background:#ffffff;border:1px solid {SLATE_GRID};border-left:3px solid {accent};border-radius:8px;padding:12px 14px;margin-bottom:12px;">{narrative}</div>
    </td></tr>
    <tr><td>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
        {charts_html}
      </table>
    </td></tr>
    """


def _best_label(values, labels, good_is_high=True):
    """Return the Day label for the best value, or '—'."""
    pairs = [(i, v) for i, v in enumerate(values) if v is not None]
    if not pairs:
        return "—"
    best_i = (max if good_is_high else min)(pairs, key=lambda p: p[1])[0]
    return labels[best_i]


def _sleep_narrative(agg):
    effs = [v for v in agg["sleep_efficiency"] if v is not None]
    durs = [v for v in agg["total_sleep_hr"] if v and v > 0]
    if not effs or not durs:
        return "No sleep data available this week."
    good = sum(1 for e in effs if e >= 85)
    avg_dur = sum(durs) / len(durs)
    best = _best_label(agg["sleep_efficiency"], agg["labels"], good_is_high=True)
    return (f"{good} of {len(effs)} nights hit ≥85% efficiency, "
            f"averaging {avg_dur:.1f}h of sleep. Best night: {best}.")


def _activity_narrative(agg):
    steps_vals = [s for s in agg["steps"] if s is not None]
    if not steps_vals:
        return "No activity data available this week."
    total_steps = sum(steps_vals)
    hi = [h or 0 for h in agg["high_activity_min"]]
    med = [m or 0 for m in agg["medium_activity_min"]]
    total_active = sum(h + m for h, m in zip(hi, med))
    days_over_goal = sum(1 for s in steps_vals if s >= STEPS_TARGET)
    best = _best_label(agg["steps"], agg["labels"], good_is_high=True)
    return (f"{total_steps:,} steps across {total_active:.0f} min of moderate+ activity. "
            f"{days_over_goal} of {len(steps_vals)} days hit {STEPS_TARGET:,} steps. "
            f"Most active: {best}.")


def _wellness_narrative(agg):
    rhr = [v for v in agg["resting_hr"] if v is not None]
    stress = [s for s in agg["stress_min"] if s is not None]
    rec = [r for r in agg["recovery_min"] if r is not None]
    if not rhr and not stress:
        return "No wellness data available this week."
    parts = []
    if rhr:
        parts.append(f"Resting HR averaged {sum(rhr)/len(rhr):.0f} bpm")
    if rec and stress:
        parts.append(f"{sum(rec)/len(rec):.0f} min recovery vs. {sum(stress)/len(stress):.0f} min high-stress per day")
    peaks = agg.get("stress_peaks") or []
    if peaks:
        peak_strs = [
            f"{agg['labels'][i]} {h:02d}:00 (+{ex:.0f} bpm)"
            for i, h, ex in peaks
        ]
        parts.append(f"Peak stress hours: {', '.join(peak_strs)}")
    return ". ".join(parts) + "."


def build_html(this_wk, last_wk, start, end) -> str:
    # This-week + last-week aggregates for delta calcs
    avg_eff_c  = _safe_avg(this_wk["sleep_efficiency"])
    avg_eff_p  = _safe_avg(last_wk["sleep_efficiency"])
    avg_dur_c  = _safe_avg(this_wk["total_sleep_hr"])
    avg_dur_p  = _safe_avg(last_wk["total_sleep_hr"])
    avg_act_c  = _safe_avg([(h or 0) + (m or 0) for h, m in
                            zip(this_wk["high_activity_min"], this_wk["medium_activity_min"])])
    avg_act_p  = _safe_avg([(h or 0) + (m or 0) for h, m in
                            zip(last_wk["high_activity_min"], last_wk["medium_activity_min"])])
    tot_stp_c  = _safe_sum(this_wk["steps"])
    tot_stp_p  = _safe_sum(last_wk["steps"])
    tot_cal_c  = _safe_sum(this_wk["calories"])
    tot_cal_p  = _safe_sum(last_wk["calories"])
    avg_rhr_c  = _safe_avg(this_wk["resting_hr"])
    avg_rhr_p  = _safe_avg(last_wk["resting_hr"])

    def f(v, suffix=""):
        if v is None: return "—"
        if isinstance(v, float): return f"{v:g}{suffix}"
        return f"{v:,}{suffix}"

    active_series = [(h or 0) + (m or 0) for h, m in
                     zip(this_wk["high_activity_min"], this_wk["medium_activity_min"])]

    cards_html = f"""
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:6px;">
      <tr>
        {_stat_card("Sleep Efficiency", f(avg_eff_c, "%"), "weekly avg", INDIGO,
                    _delta_html(avg_eff_c, avg_eff_p, good_is_up=True, suffix="%"),
                    _sparkline(this_wk["sleep_efficiency"], INDIGO))}
        {_stat_card("Sleep Duration", f(avg_dur_c, " h"), "weekly avg", INDIGO,
                    _delta_html(avg_dur_c, avg_dur_p, good_is_up=True, suffix="h"),
                    _sparkline(this_wk["total_sleep_hr"], INDIGO))}
      </tr>
      <tr>
        {_stat_card("Active Min / Day", f(avg_act_c, " min"), "weekly avg", ORANGE,
                    _delta_html(avg_act_c, avg_act_p, good_is_up=True, suffix=" min"),
                    _sparkline(active_series, ORANGE))}
        {_stat_card("Total Steps", f(tot_stp_c), "this week", ORANGE,
                    _delta_html(tot_stp_c, tot_stp_p, good_is_up=True, fmt="{:+,.0f}"),
                    _sparkline(this_wk["steps"], ORANGE))}
      </tr>
      <tr>
        {_stat_card("Active Calories", f(tot_cal_c), "this week", AMBER,
                    _delta_html(tot_cal_c, tot_cal_p, good_is_up=True, fmt="{:+,.0f}"),
                    _sparkline(this_wk["calories"], AMBER))}
        {_stat_card("Resting HR", f(avg_rhr_c, " bpm"), "weekly avg", RED,
                    _delta_html(avg_rhr_c, avg_rhr_p, good_is_up=False, suffix=" bpm"),
                    _sparkline(this_wk["resting_hr"], RED))}
      </tr>
    </table>
    """

    date_range = f"{start.strftime('%B %-d')} – {end.strftime('%B %-d, %Y')}"

    sleep_html = _section(
        "Sleep", "How well and how long you rested",
        _sleep_narrative(this_wk), INDIGO,
        ["sleep_efficiency", "sleep_stages"])
    activity_html = _section(
        "Activity", "Movement and energy expenditure",
        _activity_narrative(this_wk), ORANGE,
        ["activity_minutes", "steps", "calories"])
    wellness_html = _section(
        "Wellness", "Stress load and cardiovascular recovery",
        _wellness_narrative(this_wk), PINK,
        ["stress_recovery", "stress_clock", "resting_hr"])

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#F8FAFC;-webkit-font-smoothing:antialiased;">
  <center style="width:100%;background:#F8FAFC;padding:24px 12px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:680px;margin:0 auto;">

      <tr><td style="padding:8px 6px 20px 6px;">
        <div style="font:600 12px/1 {FONT_STACK};letter-spacing:1.4px;text-transform:uppercase;color:{INDIGO};margin-bottom:8px;">Weekly Health Report</div>
        <div style="font:700 30px/1.2 {FONT_STACK};color:{SLATE_TEXT};margin-bottom:4px;">Your Oura Recap</div>
        <div style="font:400 15px/1.4 {FONT_STACK};color:{SLATE_MUTED};">{date_range}</div>
      </td></tr>

      <tr><td>{cards_html}</td></tr>

      {sleep_html}
      {activity_html}
      {wellness_html}

      <tr><td align="center" style="padding:28px 6px 12px 6px;">
        <div style="font:400 12px/1.5 {FONT_STACK};color:{SLATE_MUTED};">
          Generated automatically from your Oura Ring data.
        </div>
      </td></tr>

    </table>
  </center>
</body></html>
"""


# ---------------------------------------------------------------------------
# Send + main
# ---------------------------------------------------------------------------

def send_email(html, images, gmail_user, gmail_pass, recipient, subject):
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


def main():
    load_dotenv()
    pat = env("OURA_PAT")
    gmail_user = env("GMAIL_USER")
    gmail_pass = env("GMAIL_APP_PASSWORD")
    recipient = os.environ.get("EMAIL_TO") or gmail_user

    # Window ends today — Oura labels last night's sleep with today's date
    # (the day you woke up). Fix 1 ensures unsynced days render as gaps, not zeros.
    end = date.today()
    start = end - timedelta(days=DAYS - 1)
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=DAYS - 1)

    this_days = [start + timedelta(days=i) for i in range(DAYS)]
    prev_days = [prev_start + timedelta(days=i) for i in range(DAYS)]

    print(f"Fetching Oura data for {prev_start} → {end} (14 days)...")
    raw = fetch_all(pat, prev_start, end)
    this_wk = aggregate(raw, this_days)
    last_wk = aggregate(raw, prev_days)

    print("Rendering charts...")
    _setup_style()
    images = {
        "sleep_efficiency": render_sleep_efficiency(this_wk),
        "sleep_stages":     render_sleep_stages(this_wk),
        "activity_minutes": render_activity_minutes(this_wk),
        "steps":            render_steps(this_wk),
        "calories":         render_calories(this_wk),
        "stress_recovery":  render_stress_recovery(this_wk),
        "stress_clock":     render_stress_clock(this_wk),
        "resting_hr":       render_resting_hr(this_wk),
    }

    if "--dry-run" in sys.argv:
        html = build_html(this_wk, last_wk, start, end)
        for cid, png in images.items():
            with open(f"{cid}.png", "wb") as f:
                f.write(png)
        for cid in images:
            html = html.replace(f"cid:{cid}", f"{cid}.png")
        with open("preview.html", "w") as f:
            f.write(html)
        print(f"Wrote preview.html and {len(images)} chart PNGs.")
        return

    subject = f"Your Oura Weekly Report — {start.strftime('%b %-d')} to {end.strftime('%b %-d')}"
    print(f"Sending email to {recipient}...")
    send_email(build_html(this_wk, last_wk, start, end), images,
               gmail_user, gmail_pass, recipient, subject)
    print("Done.")


if __name__ == "__main__":
    main()
