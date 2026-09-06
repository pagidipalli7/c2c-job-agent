# JobAgent — done-for-you job applications (10-client MVP)

Applies to jobs on behalf of consenting clients, **directly on company career sites** (Greenhouse, Lever,
Ashby, SmartRecruiters, Workday). LinkedIn, Indeed, Glassdoor and ZipRecruiter are never automated — the
registry refuses their URLs.

```
discovery (ATS JSON APIs, every 2h) ─▶ job store (fingerprint dedup) ─▶ matching (hard filters → Haiku score)
   ─▶ Application(queued) ─▶ worker (pacing, tailoring → PDF, adapter) ─▶ proof-of-work ─▶ digests
                                        │                                       ▲
                                        ├─ custom question ─▶ Answer Engine ─▶ escalation page (you answer)
                                        └─ OTP screen ──▶ session registry ◀── /webhooks/inbound-mail (client{id}@apply.DOMAIN)
```

Three product rules are enforced in code and covered by tests at 100% branch coverage. Do not weaken them:

| Rule | Where |
|---|---|
| Resumes may rephrase/reorder/emphasise but never add or change employers, titles, dates, degrees, certs or skills | `app/tailoring/validator.py` (reject → retry once → fall back to base resume) |
| Salary / relocation / criminal / clearance / licence / references / start date / legal / work-auth questions are never auto-answered | `app/escalation/forbidden.py` → escalation queue |
| No LinkedIn / Indeed / Glassdoor / ZipRecruiter | `app/discovery/detect.py` (`ForbiddenSource`) |

## Layout

```
jobagent/
  app/main.py            FastAPI: /clients, /escalations, /webhooks/inbound-mail, /admin, /health
  app/config.py          all settings from one .env
  app/db/                SQLAlchemy models (SQLite now; DATABASE_URL=postgresql+psycopg://... later)
  app/clients/           intake schema + validation, answer bank keys
  app/accounts/          Fernet credential vault (get_or_create_account)
  app/discovery/         greenhouse/lever/ashby/smartrecruiters crawlers, workday tenant search, registry, store, simulator
  app/matching/          hard filters, Haiku scorer, dedup rules, pipeline, dry run
  app/tailoring/         Sonnet tailoring, anti-fabrication validator, WeasyPrint rendering
  app/submission/        adapters (greenhouse, lever, ashby, smartrecruiters, workday), worker, pacing, health, simulators
  app/escalation/        answer engine, forbidden classes, escalation queue + HTML page
  app/otp/               inbound-mail webhook, extractor, classifier, OTP session registry
  app/reporting/         mail provider, digests, proof-of-work, admin pages, backups
  app/scheduler.py       APScheduler jobs
  templates/resume/      classic.html, modern.html (single column, no tables/images)
  companies.yaml         company registry seed
  seed/client_*.yaml     example intake documents (2 fake clients)
  clients/*.yaml         real client intake files (clients/tarun.yaml is the first one)
  scripts/               seed, reset_db, add_client, show_resume, detect_ats, run_discovery, dry_run, run_worker, run_one, run_api, send_digests, backup, gen_key
  tests/                 187 tests (pytest); tests/fixtures/fake_workday is a local Workday-style wizard
```

## Setup

```bash
cd jobagent
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
playwright install chromium            # or set CHROMIUM_EXECUTABLE to an existing Chrome/Chromium
cp .env.example .env
python scripts/gen_key.py              # paste into ENCRYPTION_KEY
# fill ANTHROPIC_API_KEY, MAIL_WEBHOOK_SECRET, APPLY_DOMAIN, OPERATOR_EMAIL, ADMIN_PASSWORD
python scripts/seed.py                 # companies.yaml + clients/*.yaml  (--demo adds the 2 fake clients)
python -m pytest -q                    # ~4 min (Workday browser tests); add --ignore=tests/test_phase5d_workday.py for ~20s
```

WeasyPrint needs system libs (`libpango`, `libcairo`, `libgdk-pixbuf`) — see https://doc.courtbouillon.org/weasyprint/stable/first_steps.html.

### Environment variables (`.env`)

| Var | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | all LLM calls (Haiku: scoring/classification, Sonnet: tailoring/answers). Unset → `LLM_MODE=mock` deterministic responders |
| `MAIL_WEBHOOK_SECRET` | shared secret for `/webhooks/inbound-mail` (header `X-Webhook-Secret`) **or** your Mailgun webhook signing key |
| `ENCRYPTION_KEY` | Fernet key for the ATS credential vault |
| `PROXY_URL` | optional; routes Playwright and the HTTP crawlers/adapters through it |
| `APPLY_DOMAIN` | clients get `client{id}@APPLY_DOMAIN`; all ATS accounts and verification mail use it |
| `OPERATOR_EMAIL` | escalation notifications + daily summary |
| `DATABASE_URL`, `REDIS_URL` | SQLite by default; Redis optional (DB queue is the fallback and the default) |
| `MAIL_PROVIDER` + `MAILGUN_*` / `SMTP_*` | outbound mail (`log` prints instead of sending) |
| `ADMIN_USER` / `ADMIN_PASSWORD` | basic auth for `/admin` and `/escalations` |
| `HEADLESS`, `CHROMIUM_EXECUTABLE` | browser adapters |
| pacing knobs | `DAILY_CAP_PER_CLIENT=40`, `SUBMIT_WINDOW_START_HOUR=8`, `SUBMIT_WINDOW_END_HOUR=20`, `JITTER_MIN_MINUTES=3`, `JITTER_MAX_MINUTES=12`, `PER_ATS_CONCURRENCY=3`, `MATCH_THRESHOLD=65`, `COMPANY_COOLDOWN_DAYS=90`, `CROSS_CLIENT_STAGGER_HOURS=3`, `OTP_WAIT_SECONDS=120` |

## Running

```bash
python scripts/run_api.py              # API + webhooks + admin + scheduler (discovery every 2h, digests, backups)
python scripts/run_worker.py           # submission worker (Ctrl-C / SIGTERM finishes the current submission, requeues the rest)
python scripts/run_worker.py --ats greenhouse --ats lever   # split adapters across processes if you like
```

Offline / no network (this is how the test-suite runs):

```bash
python scripts/run_discovery.py --simulate      # deterministic fake boards for every company in companies.yaml
python scripts/dry_run.py                       # match decisions with reasons, nothing queued
python scripts/dry_run.py --commit              # queue applications
python scripts/run_worker.py --once --simulate --force   # submit to fake Greenhouse/Lever/Ashby endpoints, ignore pacing
```

### Add a client

1. Copy `seed/client_alex.yaml`, fill it in. Required or the intake is rejected: `consent: true`,
   `profile.blacklist.companies` (current employer + affiliates; the current employer is auto-added anyway),
   `profile.work_auth`, `profile.preferences.min_salary`, `profile.preferences.target_titles`.
2. Put every answer the client has pre-approved under `answers:` — especially the forbidden-class ones
   (salary, relocation, notice period, sponsorship). Those are the only way such questions get answered without you.
3. `python scripts/add_client.py path/to/client.yaml` (or `POST /clients` with the same JSON), then review the base resume:
   `python scripts/show_resume.py --client <id> --pdf review.pdf`. When it is right, `python scripts/add_client.py path/to/client.yaml --approve-resume`.
   **Nothing is submitted until the base resume is approved**; re-running with a changed `base_resume` resets approval.
4. Set `resume_template: classic|modern`, `timezone` (drives the 8am–8pm window).

### Add a company

Edit `companies.yaml` and re-run `python scripts/seed.py`, or:

```bash
python scripts/detect_ats.py https://boards.greenhouse.io/stripe --add --name Stripe
python scripts/detect_ats.py https://acme.wd5.myworkdayjobs.com/External --add --name Acme
```

Companies deactivate after 5 consecutive crawl failures (or immediately on 404); re-enable in `companies.yaml` with `active: true`.

### Handle escalations

You get an email per new escalation. Open `BASE_URL/escalations` (basic auth), type the answer, submit: it is saved to the
client's answer bank (`source=client`) and every application waiting on that question is requeued automatically.
"Dismiss" cancels the application. Applications sit in `needs_human` until then — also visible at `/admin/applications?status=needs_human`.

Other `needs_human` causes (captcha, unknown page, 3 transient failures, OTP never arrived) show the reason and a
screenshot path in the same table. Fix and re-run one application with `python scripts/run_one.py --app <id> [--headed]`.

### Weekly digest / daily summary

Scheduler sends client digests Monday 08:00 America/Chicago and the operator summary daily 07:30. Preview now:
`python scripts/send_digests.py --weekly --operator --print`.

### Backups

Nightly 03:15 Central → `data/backups/<timestamp>/` (consistent SQLite copy + pdfs + screenshots, 14 kept). `python scripts/backup.py` runs one now.

## Manual setup TODOs (external services not reachable from the build environment)

Everything below has a full code path plus a simulator; these are the steps to point it at the real world.

- [ ] **Inbound mail (Phase 7).** Option A, Cloudflare Email Routing: add a catch-all rule on `APPLY_DOMAIN` → an Email Worker that
  POSTs JSON `{recipient, sender, subject, body_text, body_html, message_id}` to `BASE_URL/webhooks/inbound-mail` with header
  `X-Webhook-Secret: $MAIL_WEBHOOK_SECRET`. Option B, Mailgun: receive route `catch_all()` → `forward("BASE_URL/webhooks/inbound-mail")`,
  set `MAIL_WEBHOOK_SECRET` to the Mailgun **HTTP webhook signing key** (the signature is verified). Test with
  `curl -X POST BASE_URL/webhooks/inbound-mail -H 'X-Webhook-Secret: ...' -H 'content-type: application/json' -d '{"recipient":"client1@apply.example.com","sender":"noreply@myworkday.com","subject":"code","body_text":"Your verification code is 123456"}'`.
- [ ] **Outbound mail.** `MAIL_PROVIDER=mailgun` + `MAILGUN_DOMAIN`/`MAILGUN_API_KEY` (or `smtp`). Set `MAIL_FROM` on a verified domain so
  forwarded recruiter replies (Reply-To = recruiter) land in the client's inbox.
- [ ] **Greenhouse/Lever real submissions (Phase 5a/5b).** The adapters post to the hosted application forms
  (`boards.greenhouse.io/{slug}/jobs/{id}`, `jobs.lever.co/{slug}/{id}/apply`) with the field names those forms use.
  Verify once against your own test board: create a free Greenhouse job board / Lever posting, add it to `companies.yaml`,
  queue one application (`dry_run.py --commit`), run `python scripts/run_one.py --app <id>`, and check the candidate appears
  in the ATS. Boards with reCAPTCHA/hCaptcha come back as `needs_human` (see quirks).
- [ ] **Ashby.** The posting-api application endpoint is used when the board enables API applications; otherwise the generic
  browser flow runs. Verify with one Ashby company in headed mode.
- [ ] **Workday headed acceptance (Phase 5d).** Queue one Workday application, then `python scripts/run_one.py --app <id> --headed`
  on two different tenants. Fix selector drift in `app/submission/workday_fields.yaml` (never in the adapter). The account
  password is in the vault (`ats_accounts`) if you need to log in by hand.
- [ ] **Proxy.** Set `PROXY_URL` (residential proxy recommended for Workday). Per-client assignment stub: `app/submission/browser.py:proxy_for_client`.
- [ ] **Redis (optional).** `REDIS_URL=redis://...`; the worker resyncs the queue from the DB at start. Without it the DB queue is used.
- [ ] **Postgres (later).** `DATABASE_URL=postgresql+psycopg://...` and `pip install psycopg`; models use portable types only.
- [ ] **Live LLM.** Set `ANTHROPIC_API_KEY`; `LLM_MODE=auto` switches from the deterministic mocks to Haiku/Sonnet. Check `/health`.

## Known ATS quirks (log)

| ATS | Quirk | Handling |
|---|---|---|
| Greenhouse | Some boards require reCAPTCHA on the hosted form | `needs_human` with step `captcha`; apply by hand or use a proxy + browser flow |
| Greenhouse | New `job-boards.greenhouse.io` SPA boards may not accept the classic multipart POST | Falls through to `failed` with the response body; TODO: browser flow if seen in the wild |
| Greenhouse | Custom questions come back as `answers_attributes[n]` field names from `?questions=true`; select values must be option ids | handled in `parse_questions` |
| Lever | hCaptcha on some boards; EEO selects have empty placeholder options | `needs_human`; placeholders skipped |
| Lever | `comments` ("Additional information") is optional | never filled (we don't fabricate) |
| Ashby | API application only when the company enabled it (401/403/404 otherwise) | browser fallback |
| SmartRecruiters | candidate API needs a company API key | browser flow only |
| Workday | Progress bar + `data-automation-id`s are stable across tenants; "Apply Manually" vs "Autofill with Resume" modal | selector map; we always apply manually |
| Workday | Account creation triggers an email verification (code or link) from `myworkday.com` | OTP session waits 120s; both code and link are handled |
| Workday | Existing account → "already exists" error on create | adapter switches to sign-in with the vault password |
| Workday | Dropdowns are buttons opening `[role=listbox]`; long lists need typeahead | `_choose_dropdown` tries exact, contains, then types |
| Workday | Validation errors render in `[data-automation-id="errorMessage"]` without navigation | detected after every Next → `needs_human` + screenshot |
| All | Rate limits (429) on crawls | exponential backoff, Retry-After honoured, 4 attempts |

## Operations cheat-sheet

- Pacing: per client ≤ 40/day, 08:00–20:00 client-local, 3–12 min jitter between a client's submissions, 3 parallel per ATS.
- Adapter health: rolling 24h success rate < 70% (≥ 5 attempts) pauses the adapter with a WARNING; resume from `/admin/health`.
- Dedup: `(client, job)` unique constraint; one application per company per 90 days; ≥ 3 h stagger when several clients match one job.
- Logs: structlog JSON on stdout; every submission carries `trace_id`, `app_id`, `client_id`, `ats`.
- Proof of work per application: `/admin/applications/<id>` (job snapshot, resume sha256 + path, screenshots, confirmation, history).
