"""One-off diagnostic: dump raw Oura data + aggregated output for manual review."""
import json
from datetime import date, timedelta

from dotenv import load_dotenv

from weekly_report import aggregate, env, fetch_all

load_dotenv()

end = date.today()
start = end - timedelta(days=6)
days = [start + timedelta(days=i) for i in range(7)]
keys = [d.isoformat() for d in days]

pat = env("OURA_PAT")
print(f"Fetching {start} → {end}...\n")
raw = fetch_all(pat, start, end)

# --- 1. Raw dumps (one representative doc per endpoint) ---
for ep, items in raw.items():
    print(f"{'=' * 70}")
    print(f"{ep}  —  {len(items)} documents")
    print("=" * 70)
    if items:
        # Show day field for each doc so we can see coverage
        print("Days present:", sorted({it.get("day") for it in items if "day" in it}))
        print("\nFull sample document (first one):")
        print(json.dumps(items[0], indent=2, default=str))
    print()

# --- 2. Aggregated output ---
agg = aggregate(raw, days)
print(f"{'=' * 70}")
print("AGGREGATED")
print("=" * 70)
for k, v in agg.items():
    print(f"{k:22s} {v}")

# --- 3. Spot checks ---
print(f"\n{'=' * 70}")
print("SPOT CHECKS vs raw")
print("=" * 70)

# Sleep spot check — compare deep/rem/light from longest session per day
longest = {}
for s in raw["sleep"]:
    d = s.get("day")
    if not d:
        continue
    cur = longest.get(d)
    if cur is None or (s.get("total_sleep_duration") or 0) > (cur.get("total_sleep_duration") or 0):
        longest[d] = s

print("\nSleep durations — raw (seconds) vs aggregated (hours):")
for d in keys:
    s = longest.get(d, {})
    raw_deep = s.get("deep_sleep_duration")
    raw_rem = s.get("rem_sleep_duration")
    raw_light = s.get("light_sleep_duration")
    raw_total = s.get("total_sleep_duration")
    agg_i = keys.index(d)
    def fh(v):  # format hours (or None)
        return "None" if v is None else f"{v:.2f}h"
    print(f"  {d}: raw deep={raw_deep}s rem={raw_rem}s light={raw_light}s total={raw_total}s")
    print(f"            agg deep={fh(agg['deep'][agg_i])} rem={fh(agg['rem'][agg_i])} light={fh(agg['light'][agg_i])} total={fh(agg['total_sleep_hr'][agg_i])}")
    # Does agg_total match raw_total/3600?
    if raw_total:
        expected = raw_total / 3600
        agg_total = agg["total_sleep_hr"][agg_i]
        # Note: agg_total = deep+rem+light, raw_total is from API. These differ if awake time is excluded.
        if agg_total is not None:
            print(f"            raw_total/3600 = {expected:.2f}h  (diff from agg_total: {agg_total - expected:+.2f}h)")

# Sleep session counts per day — verify dedup logic
print("\nSleep session counts per day (to verify 'longest session' dedup):")
counts = {}
for s in raw["sleep"]:
    d = s.get("day")
    if d:
        counts[d] = counts.get(d, 0) + 1
for d in keys:
    print(f"  {d}: {counts.get(d, 0)} session(s)")

# Activity spot check
print("\nActivity times — raw (seconds) vs aggregated (minutes):")
for a in raw["daily_activity"]:
    d = a.get("day")
    if d in keys:
        print(f"  {d}: steps={a.get('steps')} cal={a.get('active_calories')} "
              f"high={a.get('high_activity_time')}s med={a.get('medium_activity_time')}s")
        i = keys.index(d)
        print(f"       agg high_min={agg['high_activity_min'][i]}  med_min={agg['medium_activity_min'][i]}")

# Stress spot check
print("\nStress — raw (seconds) vs aggregated (minutes):")
for s in raw["daily_stress"]:
    d = s.get("day")
    if d in keys:
        print(f"  {d}: stress_high={s.get('stress_high')}s recovery_high={s.get('recovery_high')}s day_summary={s.get('day_summary')}")
        i = keys.index(d)
        print(f"       agg stress_min={agg['stress_min'][i]}  recovery_min={agg['recovery_min'][i]}")

# Coverage summary
print(f"\n{'=' * 70}")
print("COVERAGE")
print("=" * 70)
def cov(field):
    return sum(1 for v in agg[field] if v is not None)
for f in ["sleep_efficiency", "total_sleep_hr", "resting_hr", "steps",
         "calories", "high_activity_min", "stress_min", "recovery_min"]:
    print(f"  {f:22s} {cov(f)}/7 days")
