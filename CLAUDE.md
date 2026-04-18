# oura-email

Single-purpose script that pulls 7 days of Oura Ring data, renders a 6-panel matplotlib dashboard, and emails it via Gmail SMTP. Runs weekly in GitHub Actions; can also be invoked locally.

## Files

- `weekly_report.py` — the entire program. Keep it single-file; resist splitting into modules unless it grows past ~500 lines.
- `requirements.txt` — `requests`, `matplotlib`, `python-dotenv`. No other deps.
- `.github/workflows/weekly-report.yml` — Mondays 13:00 UTC + manual `workflow_dispatch`.
- `.env` — local secrets (gitignored). In CI, the same vars come from repo secrets.
- `openapi-1.29.json` — Oura's OpenAPI spec, kept for reference. Grep this before guessing field names.

## Required env vars

| Var | Where it comes from |
|---|---|
| `OURA_PAT` | https://cloud.ouraring.com/personal-access-tokens (NOT OAuth — see "Auth" below) |
| `GMAIL_USER` | Sender Gmail address |
| `GMAIL_APP_PASSWORD` | https://myaccount.google.com/apppasswords (requires 2FA) |
| `EMAIL_TO` | Optional, defaults to `GMAIL_USER` |

## Auth

Use the **Personal Access Token** flow (`Authorization: Bearer $OURA_PAT`). The OAuth `OURA_CLIENT` / `OURA_CLIENT_SECRET` in `.env` are leftover from initial setup and are not used by the script — leave them or remove them, doesn't matter.

## Oura API gotchas

- Endpoints are under `https://api.ouraring.com/v2/usercollection`.
- All daily endpoints accept `start_date` / `end_date` (YYYY-MM-DD).
- `/daily_stress` does **not** have a `score` field. The numeric stress metric is `stress_high` (seconds in high-stress zone). The script charts this in minutes/day.
- `/sleep` can return multiple sessions per day (e.g. naps); the script picks the longest by `total_sleep_duration`.
- Resting HR comes from `/sleep.lowest_heart_rate` (matches what Oura's app shows). The `/heartrate` time-series endpoint is NOT used — averaging 24h BPM produces a misleading ~85–90 bpm figure for a healthy user because it includes workouts and stress spikes.
- Pagination: responses include `next_token` when there's more data — `oura_get()` follows it automatically.

## Charting / email conventions

- Always render exactly 7 days. Missing days stay as `None` in aggregation and render as `nan` (matplotlib leaves a gap) so the x-axis stays consistent.
- One PNG **per chart** (7 total), each full-width at figsize=(10, 3.8), dpi=200. Do NOT go back to a single multi-subplot dashboard — it's unreadable on mobile.
- Each chart is attached inline via a distinct `Content-ID` (`sleep_score`, `sleep_stages`, `activity_score`, `steps`, `calories`, `stress`, `resting_hr`).
- HTML layout uses tables (not flex/grid) with inline styles only — Gmail strips `<style>` blocks and doesn't honor flex/grid reliably.
- Accent color palette lives in constants at the top of `weekly_report.py`. Keep matplotlib chart colors and HTML accent colors in sync.
- Use `matplotlib.use("Agg")` at import time so the script works headless (CI has no display).

## Local preview

`python weekly_report.py --dry-run` writes `preview.html` + all chart PNGs to the project root (without sending email). Open `preview.html` in a browser to iterate on layout/styling before burning through your inbox.

## Local dev

The repo's `.venv` was created with `pyenv` Python 3.11.1, which prints noisy `unsupported hash type blake2b` warnings on every command. They are cosmetic — the script still works. If setting up fresh, prefer Python 3.12 (matches CI):

```
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python weekly_report.py
```

## Things to avoid

- Don't add a database, web UI, or historical-trend logic — out of scope for this project.
- Don't add Readiness charts (the user explicitly excluded that metric).
- Don't switch to OAuth — PAT is the chosen auth path.
- Don't commit `.env` (it's gitignored; preserve that).
