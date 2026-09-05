# C2C Job Agent

Automated contract-job aggregator that runs every 2 hours on GitHub Actions, pulls new **Power Platform**
postings from **Dice**, **LinkedIn (public guest search)**, **~30 company career pages** (Accenture, PwC,
Booz Allen, Kyndryl, Leidos, GDIT, Salesforce, Nvidia, Adobe, Amazon, Databricks, HSO ...), **Adzuna**, keyless remote boards
(**Remotive / RemoteOK / Jobicy**), **Gmail (recruiter/vendor emails)** and optionally **SerpAPI Google Jobs**,
scores every job against `profile.md` with **Claude Haiku** in one batched call, and appends the matches to a
**Google Sheet**. Location scope is anywhere in the USA.

```
sources/*  ──fetch()──▶  dedupe (sheet + in-run)  ──▶  Claude (1 batched JSON call)
          ──▶  filter: match_percent ≥ 80 AND visa_status ≠ Restricted  ──▶  gspread append
```

## Repo layout

| Path | Purpose |
|---|---|
| `main.py` | Orchestrator; `--dry-run`, `--sources`, `--skip-analysis`, `--limit`, `-v` |
| `config.yaml` | Keywords, locations, per-source settings, model, sheet columns |
| `profile.md` | Candidate profile the scorer reads (edit freely; it is sent verbatim to Claude) |
| `sources/dice.py` | Parses dice.com search results (server-rendered payload) + job-detail JSON-LD |
| `sources/linkedin.py` | LinkedIn guest job search (no login) + guest posting detail for descriptions |
| `sources/careers.py` | Company career pages via ATS JSON APIs: Workday (16 companies), Greenhouse (9), Lever (3), Amazon |
| `sources/adzuna.py` | Adzuna official API; skipped when `ADZUNA_APP_ID`/`ADZUNA_APP_KEY` are unset |
| `sources/remoteboards.py` | Remotive, RemoteOK, Jobicy JSON feeds with strict title matching |
| `sources/gmail_imap.py` | IMAP scan of the last N hours for vendor requirement emails |
| `sources/serpapi_jobs.py` | Google Jobs via SerpAPI; skipped when `SERPAPI_KEY` is unset |
| `agent/analyzer.py` | Claude batch scoring, structured-output schema, defensive JSON parsing |
| `agent/sheet.py` | gspread append-only writer, header bootstrap, existing `job_id` read |
| `agent/models.py` | `Job` dataclass, URL normalisation, `job_id` derivation |
| `agent/textsig.py` | Regex heuristics: C2C/W2, visa restrictions, rates, emails |
| `.github/workflows/job_agent.yml` | Cron `0 */2 * * *` + manual dispatch |

## Environment variables / GitHub secrets

| Name | Required | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | Console → API keys. Model is `claude-haiku-4-5` (see `config.yaml`). |
| `SHEET_ID` | yes (not for `--dry-run`) | The long id in the sheet URL `…/spreadsheets/d/<SHEET_ID>/edit` |
| `GOOGLE_SA_JSON` | yes (not for `--dry-run`) | Full service-account JSON, pasted as one line |
| `GMAIL_USER` | for Gmail source | Your Gmail address |
| `GMAIL_APP_PASSWORD` | for Gmail source | 16-char App Password (needs 2-Step Verification) |
| `SERPAPI_KEY` | optional | Source is skipped gracefully when missing |
| `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | optional | Free at https://developer.adzuna.com; skipped when missing |

### 1. Google Sheet + service account
1. Google Cloud Console → create/select a project → **APIs & Services → Enable** *Google Sheets API* and *Google Drive API*.
2. **IAM & Admin → Service Accounts → Create**. Then **Keys → Add key → JSON**; download it.
3. Create a blank Google Sheet. Share it (Editor) with the service account's `client_email`.
4. `SHEET_ID` = the id from the sheet URL. `GOOGLE_SA_JSON` = the JSON file contents (one line: `python -c "import json;print(json.dumps(json.load(open('sa.json'))))"`).
   The header row is created automatically on the first write.

### 2. Gmail App Password
Google Account → Security → 2-Step Verification (on) → **App passwords** → create one for "Mail".
IMAP must be enabled (Gmail → Settings → Forwarding and POP/IMAP). The agent opens `INBOX` read-only
and looks at messages from the last `gmail.lookback_hours` (default 3).

### 3. Anthropic + SerpAPI
Create an API key at https://console.anthropic.com. Optional: https://serpapi.com key for Google Jobs.

### 4. GitHub Actions
Push this folder to a repo → **Settings → Secrets and variables → Actions** → add the secrets above.
The workflow runs every 2 hours at 01:07, 03:07 ... 23:07 UTC (even hours Central Daylight Time: 12:07 AM, 2:07 AM ... 10:07 PM CDT;
odd hours during Central Standard Time) and can be triggered manually
(**Actions → C2C Job Agent → Run workflow**, optional *dry_run* checkbox).

> Scheduled workflows are disabled by GitHub after 60 days without repo activity; any commit re-enables them.

## Local run

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in values; main.py loads .env automatically
python main.py --dry-run    # prints rows instead of writing
python main.py --dry-run --sources dice          # one source
python main.py --skip-analysis --sources gmail   # see raw parsed emails, no Claude call
python -m unittest -q                            # offline tests
```

## How scoring and filtering work

Claude receives `profile.md` and a JSON array of all *new* candidates (deduped against the sheet's
`job_id` column and within the run) in **one call** and returns, per job:

| Field | Values |
|---|---|
| `match_percent` | 0–100 fit of required skills vs profile; non-Power-Platform roles are told to score below 80 |
| `years_required` | minimum years of experience the posting asks for (0 if not stated) |
| `missing_skills` | comma-separated required skills not in the profile |
| `employment_type` | `C2C` / `W2` / `Full-time` / `Unclear` (vendor emails mentioning C2C, corp-to-corp, 1099 ⇒ `C2C`) |
| `visa_status` | `H1B-OK` / `Restricted` / `Unclear` (`Restricted` = USC only, GC only, USC/GC, no sponsorship, W2-citizens-only, clearance…) |
| `contact_email` | recruiter/vendor email for C2C jobs (from the sender/reply-to or posting text) |

Hard filters: drop `match_percent < analysis.min_match_percent` (80), drop `visa_status == Restricted`,
and drop `years_required > analysis.max_years_required` (7).
A regex guard in `agent/textsig.py` also forces `Restricted` when the text contains explicit
citizenship-only language, regardless of the model's answer.

`job_id` = normalised URL (lower-cased host, tracking params and fragment removed) when a URL exists,
otherwise `sha1(lower(company + title + location))`.

## Sheet layout

* **One tab per day**, named with the Central-time date (`2026-09-05`), created automatically by the first run
  that has something to write. Header row is bold and frozen. Set `sheet.daily_tabs: false` for a single fixed tab.
* `date_found` is Central time, e.g. `2026-09-05 03:41 PM CDT` (`CST` in winter). Change `sheet.timezone` /
  `sheet.time_format` to taste.
* Dedupe reads the `job_id` column of **every** tab (two batched API calls), so a job never repeats across days.
* Only fresh postings are considered: Dice and LinkedIn last 24 h, Adzuna `max_days_old: 1`, Google Jobs
  `date_posted: today`, Gmail last 3 h. Combined with dedupe, each job is written once, on the day it is first seen.

Columns:
`date_found | job_id | title | company | location | rate | employment_type | contact_email | match_percent | missing_skills | visa_status | source | url`

## Logging

Every run prints per-source counters and a machine-readable summary line:

```
SUMMARY source=dice found=41 new=12 dropped_low_match=7 dropped_visa=2 dropped_experience=1 dropped_unanalyzed=0 written=2
RUN_SUMMARY {"candidates": 15, "kept": 4, "sources": {...}, "source_errors": {}}
```

A source that throws is logged as `source=<name> FAILED: …` and the run continues with the others.
Exit codes: `0` ok, `2` sheet unavailable (non-dry-run), `3` Claude analysis failed.

## Tuning

* Keywords / locations / per-source knobs: `config.yaml`.
* Gmail noise: `gmail.ignore_sender_domains`, `gmail.job_signals`, `gmail.skill_signals`.
* Dice: `posted_within` (`ONE`/`THREE`/`SEVEN`), `employment_types`, `remote_only`, `extra_locations`, `max_detail_fetches`.
* LinkedIn: `posted_within_seconds`, `job_types` (`C` contract, `T` temporary, `F` full-time), `max_pages`, `request_delay_seconds`.
* Remote boards: `title_terms` (strict title/tag match), `us_locations_only`.
* Career pages: `careers.workday` / `greenhouse` / `lever` company lists, `queries`, `title_terms`, `max_days_old`.
  To add a Workday company, copy `tenant`, `wd` and `site` from its URL `https://<tenant>.<wd>.myworkdayjobs.com/<site>/`.
  Microsoft, Google, Meta and Apple have no stable public job API; their postings arrive via LinkedIn (and Google Jobs when a SerpAPI key is set).
* Experience cap: `analysis.max_years_required` (0 disables).
* Analysis: `analysis.model`, `description_chars` (per-job truncation), `min_match_percent`.
