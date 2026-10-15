# JurisBound district court scraper

Collects district court orders and judgments from the eCourts services website (`services.ecourts.gov.in`) for JurisBound. For each order it saves the PDF to Azure Blob Storage and one row, with its JurisBound document number, to the JurisBound database. Every court complex keeps a "done up to" date, so the next run continues from there.

It runs on GitHub Actions: the chosen states are split into groups of districts, and each group runs as its own job on its own GitHub machine, side by side.

**Contents**

1. [What is in this repository](#1-what-is-in-this-repository)
2. [How a run works](#2-how-a-run-works)
3. [One-time setup on GitHub](#3-one-time-setup-on-github)
4. [Running on GitHub](#4-running-on-github)
5. [Schedules](#5-schedules)
6. [Running on a laptop](#6-running-on-a-laptop)
7. [Command-line options](#7-command-line-options)
8. [Settings (environment variables)](#8-settings-environment-variables)
9. [Where the data goes](#9-where-the-data-goes)
10. [Orders that cannot be saved](#10-orders-that-cannot-be-saved)
11. [State codes](#11-state-codes)
12. [Speed limits and blocking](#12-speed-limits-and-blocking)
13. [GitHub Actions minutes](#13-github-actions-minutes)
14. [Checking that a run worked](#14-checking-that-a-run-worked)
15. [Troubleshooting](#15-troubleshooting)
16. [Known gaps](#16-known-gaps)
17. [Related repositories](#17-related-repositories)

---

## 1. What is in this repository

| Path | What it does |
|---|---|
| `district-court/web/download.py` | The scraper: date search per court complex (CAPTCHA read automatically), case details lookup, PDF download |
| `district-court/web/sync_s3.py` | The `--sync-s3` mode: which dates each complex needs, and moving its "done up to" date |
| `district-court/web/mobile_lookup.py` | Case details from the eCourts app service first (fewer website requests), the website as fallback |
| `district-court/web/recheck_missing.py` | Weekly re-check of orders whose PDF the court had not uploaded (section 10) |
| `district-court/mobile/` | The eCourts app client used by the lookup |
| `district-court/jb_db.py` | Uploads each order's PDF and details, writes its database row, keeps the missing-PDF list |
| `district-court/archive_manager.py` | Bundles (tar files) and index files per complex and year, in storage |
| `district-court/courts.csv` | All 3,566 court complexes: state, district and complex codes and names |
| `storage/`, `configs/` | Database connection, table definitions, storage (Azure or S3), settings. The same files as in the other JurisBound court repositories |
| `database/` | SQL that creates the court tables, the JurisBound numbering and `missing_orders` in an empty database. Not used by a run |
| `.github/workflows/district-courts.yml` | The scraping workflow (section 4) |
| `.github/workflows/district-recheck-missing.yml` | The weekly re-check workflow (section 10) |
| `.github/scripts/plan_groups.py` | Splits the chosen states into job groups |

## 2. How a run works

1. **Plan.** The chosen states are split into groups of whole districts of about `complexes_per_job` court complexes each. Each group is one job. Telangana with the default 30 = 4 jobs; all of India = 123 jobs.
2. **Dates.** With dates given, every complex in the job searches those days. Without dates (automatic mode), each complex starts from its own "done up to" date minus 3 days (late uploads), up to today. A complex with no "done up to" date starts from 1 January, so **always give dates for a state's first run**.
3. **For each complex and day:**
   - search the court's orders for that date (one CAPTCHA, read automatically);
   - for each order not saved yet: look up the case details (app first, then website), download the PDF;
   - save the PDF and details to storage and one row to the database. The row gets its JurisBound number (`JB-DC-TS-KAR-2026-0000003`) from the database.
4. **"Done up to".** A day counts as done only when every order of that day was saved (or set aside, section 10). The complex's "done up to" date then moves forward. It never moves back.
5. **End.** Bundles and index files are written to storage; the job ends. A job stops starting new work at its time limit and saves what it has.

Rules that keep parallel jobs safe:
- Jobs of one run never share a court complex, and everything stored (index, bundles, "done up to", PDFs) is kept per complex.
- Two runs of the district workflows never run at the same time; a second run waits.
- Numbers are given by the database one at a time, so two jobs never get the same number.

## 3. One-time setup on GitHub

### 3.1 Secrets

Repository → Settings → Secrets and variables → Actions → New repository secret:

| Secret | Value |
|---|---|
| `AZURE_KB_DATABASE_URL` | `postgresql://jb_court:PASSWORD@HOST:5432/jurisbound_dev_db?sslmode=require` |
| `AZURE_KB_ASYNC_DATABASE_URL` | The same with `postgresql+asyncpg://` and `?ssl=require` |
| `AZURE_STORAGE_CONNECTION_STRING` | The storage account's connection string |

Secrets are never shown in run logs (they appear as `***`).

### 3.2 Database access

GitHub's machines reach the Azure database through its firewall rule that allows Azure services. Keep that rule. The database table `missing_orders` must exist (`database/07_missing_orders.sql`).

## 4. Running on GitHub

Actions tab → pick the workflow on the left → **Run workflow** → branch **main** → fill the boxes → **Run workflow**.

### JurisBound – District courts

| Box | Example | Empty / default |
|---|---|---|
| `state_codes` | `29`, `29,2` or `all` | `29` (Telangana) |
| `complexes_per_job` | `30` | 30. Smaller = more, shorter jobs |
| `start_date` / `end_date` | `2026-10-14` | Automatic mode (section 2) |
| `timeout_hours` | `5.5` | 5.5 hours (GitHub stops any job at 6) |

To see the groups before a run, on a laptop: `python .github/scripts/plan_groups.py 29 30`.

### JurisBound – District re-check of missing PDFs

| Box | Meaning | Default |
|---|---|---|
| `list_only` | `true` = only list the orders due for a re-check | `false` |
| `timeout_hours` | Stop starting new days after this many hours | 5.5 |

### Reading a run

- Each job is one row, for example `29-1 (31 complexes)`. Click a job, then the step **Run the district scraper**, to read its log. The first line shows the command: `Running: python download.py ...`.
- Green = finished. Red with **exit code 2** = the court website blocked that machine (section 12): run that state again later.
- **Cancel:** open the run → Cancel workflow. Nothing half-saved is left; the next run searches those days again.

## 5. Schedules

| Workflow | Schedule | Status |
|---|---|---|
| JurisBound – District courts | Daily 22:30 IST (`0 17 * * *`) | On (test from 16 October 2026): **all states, yesterday's date** (India time, worked out once when the run starts). Never the automatic mode, because most complexes have no "done up to" date yet |
| JurisBound – District re-check of missing PDFs | Sundays 22:30 IST (`0 17 * * 0`) | On |

- Times are UTC in the workflow file (`cron`); India = UTC + 5:30. GitHub starts scheduled runs late (4-6 hours in October 2026).
- To stop a schedule: Actions → the workflow → ⋯ → **Disable workflow** (this also hides its Run workflow button), or remove its `schedule:` lines and commit.
- Schedules run only from `main`. GitHub switches off schedules in a public repository after 60 days without commits (it sends an e-mail first).

## 6. Running on a laptop

1. Install **Python 3.11** and, optionally, **Ghostscript** (PDF compression; without it compression is skipped with a warning).
2. In the repository folder:

   ```
   python -m venv .venv
   .venv\Scripts\activate            (Windows)   |   source .venv/bin/activate   (Linux/Mac)
   pip install --extra-index-url https://download.pytorch.org/whl/cpu -r district-court/requirements.txt
   ```

   (The extra index installs the small CPU-only PyTorch used by the CAPTCHA reader.)
3. Copy `.env.example` to `.env` and fill in the values. Never commit `.env`.
4. The laptop's internet address must be allowed in the database firewall (Azure portal → the Postgres server → Networking → Add current client IP address → Save). Home addresses change often; add the new one when the connection times out.
5. Windows: `$env:PYTHONIOENCODING="utf-8"` first.
6. Run from `district-court/web`:

   ```
   cd district-court/web
   python download.py --sync-s3 --state_code 29 --start_date 2026-10-14 --end_date 2026-10-14
   python download.py --sync-s3 --state_code 29 --district_code 3 --complex_code 1290041 --start_date 2026-10-14 --end_date 2026-10-14
   python download.py --sync-s3 --state_code 29 --district_code 23                          # automatic dates
   python recheck_missing.py --list
   ```

Without `--sync-s3` nothing is uploaded and no rows are written (a local test). Do not run a laptop and a GitHub run on the same complexes at the same time.

## 7. Command-line options

`district-court/web/download.py`:

| Option | Meaning | Default |
|---|---|---|
| `--sync-s3` | Normal mode: save to storage and database, move "done up to" dates | off (local test) |
| `--start_date`, `--end_date` | Dates `YYYY-MM-DD`; without them, automatic mode | — |
| `--state_code`, `--district_code`, `--complex_code` | Limit to one state / district / complex | all |
| `--courts_csv` | A court list to use instead of `courts.csv` (the workflow gives each job its own) | `district-court/courts.csv` |
| `--max_workers` | Parallel workers. **Do not raise** (section 12) | 2 |
| `--timeout-hours` | Stop starting new work after this many hours, then save and exit | none |
| `--no-compress` | Do not compress PDFs | compression on |
| `--day_step` | Days per search chunk | 2100 |
| `--sync-s3-fill`, `--upload-local`, `--dry-run` | Older backfill and upload modes. Not used by JurisBound runs | — |

`district-court/web/recheck_missing.py`: `--list` (show what is due), `--timeout-hours` (default 5.5).

## 8. Settings (environment variables)

Database and storage (`.env` on a laptop, secrets on GitHub):

| Variable | Meaning |
|---|---|
| `DATABASE_URL`, `SYNC_DATABASE_URL` | Azure database, login `jb_court` (default schema `jb_kb`) |
| `STORAGE_BACKEND` | `azure` |
| `AZURE_STORAGE_CONNECTION_STRING` | Storage account |
| `SUPABASE_S3_BUCKET` | Container name: `court-documents` |

Scraper behaviour (defaults are the tested values):

| Variable | Meaning | Default |
|---|---|---|
| `DC_LOOKUP` | Case details: `mobile` = app first, website as fallback; `web` = website only | `web` (the workflows set `mobile`) |
| `DC_CASE_PAUSE` | Seconds to wait before each case and each search (plus up to 50% random) | 5 |
| `DC_APP_GAP` | Seconds before each app request | 1 |
| `DC_APP_MAX_FAILURES` | App failures in a row before the run uses only the website | 5 |
| `DC_LOOKBACK_DAYS` | Automatic mode starts this many days before "done up to" | 3 |
| `DC_NOT_FOUND_STRIKES` | Runs with "Record not found" before an order stops holding its day back | 3 |
| `DC_MISSING_PDF_STRIKES` | Runs with a failed PDF before the order is set aside | 3 |
| `DC_MISSING_RECHECKS` | Weekly re-checks of a set-aside order before it is given up | 4 |
| `MAX_ARCHIVE_SIZE_MB` | Size of one bundle part | 40 |

## 9. Where the data goes

### Storage (container `court-documents`)

```
courts/district/<state>/<district>/<complex>/
    scraped_through.json                 "done up to" date
    not_found.json                       orders the court's case search does not know (section 10)
    <year>/orders/<CNR>_<order no>_<date>.pdf
    <year>/details/<CNR>_<order no>_<date>.json
    <year>/archive/                      bundles and their index files
```

Names are lower case with `_`, for example `courts/district/telangana/karimnagar/karimnagar_pdj_court_complex/`. The container is public-read: a file opens by its exact address.

### Database (Azure, schema `jb_kb`)

| Table | What |
|---|---|
| `scraped_documents` | One row per order with a PDF: court, case number, CNR, order number, date, parties, judges, acts, advocates, PDF address, JurisBound number |
| `missing_orders` | Orders whose PDF the court has not uploaded (section 10) |

The year in a path and in the number is the order's own date.

## 10. Orders that cannot be saved

A complex's day counts as done only when every order is saved. Three kinds of order are handled so one bad order does not hold a complex back forever:

| Case | What happens |
|---|---|
| **PDF not uploaded by the court** (the court's own site says so) | Each failed run adds a strike to `missing_orders`. After **3** runs the order is **set aside** and the day can be done without it. Every **Sunday** the re-check tries set-aside orders again (only those last tried 6 or more days ago), **4 times**. A PDF that now downloads is saved and its row becomes `found`; after 4 re-checks it becomes `given_up` and stays in the table |
| **"Record not found"** from the court's case search | After 3 runs the order stops holding its day back; it stays listed in the complex's `not_found.json` |
| **Case type written differently** (`STC.NI` in the order list, `STC-NI` in the court's case-type list) | The lookup tries the exact name, then the name without dots, hyphens and spaces, and uses it only if exactly one case type fits. A case type still not found is written to the log as a warning |

Every other failure (court site busy, CAPTCHA, network) leaves the day not done; the next run tries it again.

## 11. State codes

District state codes are the eCourts website's own and differ from the High Court codes (Telangana = `29` here). District and complex codes are in `district-court/courts.csv`.

| Code | State | Districts | Complexes |
|---|---|---|---|
| 1 | Maharashtra | 41 | 493 |
| 2 | Andhra Pradesh | 13 | 193 |
| 3 | Karnataka | 31 | 197 |
| 4 | Kerala | 14 | 180 |
| 5 | Himachal Pradesh | 10 | 45 |
| 6 | Assam | 33 | 73 |
| 7 | Jharkhand | 24 | 24 |
| 8 | Bihar | 37 | 81 |
| 9 | Rajasthan | 44 | 335 |
| 10 | Tamil Nadu | 38 | 272 |
| 11 | Odisha | 29 | 150 |
| 12 | Jammu and Kashmir | 20 | 73 |
| 13 | Uttar Pradesh | 73 | 220 |
| 14 | Haryana | 22 | 61 |
| 15 | Uttarakhand | 13 | 92 |
| 16 | West Bengal | 23 | 91 |
| 17 | Gujarat | 32 | 329 |
| 18 | Chhattisgarh | 23 | 93 |
| 19 | Mizoram | 3 | 8 |
| 20 | Tripura | 8 | 17 |
| 21 | Meghalaya | 14 | 17 |
| 22 | Punjab | 22 | 67 |
| 23 | Madhya Pradesh | 50 | 230 |
| 24 | Sikkim | 6 | 11 |
| 25 | Manipur | 9 | 19 |
| 26 | Delhi | 11 | 12 |
| 27 | Chandigarh | 1 | 1 |
| 28 | Andaman and Nicobar | 1 | 1 |
| 29 | Telangana | 33 | 123 |
| 30 | Goa | 2 | 15 |
| 33 | Ladakh | 2 | 6 |
| 34 | Nagaland | 11 | 11 |
| 35 | Puducherry | 4 | 4 |
| 36 | Arunachal Pradesh | 13 | 16 |
| 37 | Lakshadweep | 1 | 3 |
| 38 | Dadra and Nagar Haveli and Daman and Diu | 3 | 3 |
| | **Total** | **714** | **3,566** |

## 12. Speed limits and blocking

- The court website blocks an internet address that sends requests too fast ("Security Page", HTTP 405). Tested on one address: 1 worker with a 5-second pause ran clean; 2 workers with a 3-second pause were blocked within 90 seconds.
- The default (2 workers, 5-second pause) is the tested pace. **Do not raise it.** Speed comes from more jobs on more GitHub machines, not from a faster pace on one machine.
- When blocked, the run stops sending requests, saves what it has and exits with code 2.
- Telangana, 14 October 2026, 4 jobs side by side: no block; 7 to 40 minutes per run. The court website was often slow or unavailable ("site busy", connections closed); those days are simply searched again by the next run.

## 13. GitHub Actions minutes

This repository is public: GitHub's standard machines are free for it, with no monthly minute limit. The free plan runs at most 20 jobs at a time across the organisation's repositories; the rest wait. One job may run 6 hours; one run may have up to 256 jobs.

## 14. Checking that a run worked

1. All jobs green. In each job's log: `All tasks completed`, or `N of M tasks not done; will be retried.`
2. In the database:

   ```sql
   -- orders saved per district, newest first
   select district, count(*), max(doc_number), max(created_at)
   from jb_kb.scraped_documents where court_type = 'district'
   group by district order by max(created_at) desc;

   -- orders whose PDF is not uploaded
   select complex_name, case_number, order_date, strikes, rechecks, status
   from jb_kb.missing_orders order by order_date desc;
   ```

3. In storage: each finished complex has `scraped_through.json` with the new date.

## 15. Troubleshooting

| Symptom | Cause | What to do |
|---|---|---|
| A job is red with exit code 2 | The court website blocked that GitHub machine | Run that state again later |
| `Search failed, day will be retried` | The court website did not answer the date search | Nothing; the next run searches again |
| `PDF not saved … strike 1 of 3` | The PDF did not download | Nothing; after 3 runs it is set aside (section 10) |
| `Case type 'X' is not in the case type list of …` | That court has no such case type, even ignoring punctuation | Check the court's case-type list on the website; the order stays not saved |
| `Case lookup failed for …` | App and website gave no case details this time | Nothing; the next run tries again |
| Database connection times out (laptop) | The laptop's address is not in the database firewall | Add it (section 6) |
| A complex starts from 1 January | It has no "done up to" date and the run had no dates | Run it once with dates |
| Windows: `UnicodeEncodeError` | Windows console encoding | `$env:PYTHONIOENCODING="utf-8"` |

## 16. Known gaps

- **One job per group of whole districts.** A very large district stays in one job; if a state cannot finish one day in 6 hours, lower `complexes_per_job`.
- **The "done up to" date waits for the whole day.** A day with a court-site failure stays not done until a later run succeeds.
- **The app service** is sometimes slow from GitHub's machines; after 5 failures in a row a job uses only the website lookup (slower, more CAPTCHAs).
- **Order number.** When an order cannot be matched to its line on the case history page, it is saved as `<CNR>_<date>` instead of `<CNR>_<order no>_<date>`, and the run log says so.
- **Run logs are public** (this repository is public). They show court complex names, case numbers, CNRs and dates; no party names and no secrets.

## 17. Related repositories

| Repository | What |
|---|---|
| `jurisbound/court-scrapers` (private) | High Court and Supreme Court scrapers, and a laptop copy of this district scraper. A change to the district scraper is made in both copies |
| `jurisbound/court-pipeline` (private) | Reads the saved PDFs: text (OCR), analysis, search index |
