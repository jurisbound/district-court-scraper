# AGENTS.md

Developer notes for `district-court/`. Last updated 16 October 2026. **This folder is kept identical in `court-scrapers` and `district-court-scraper`** (the public repository the GitHub runs use); change both together.

## Current state (October 2026)

- **Azure since 15 Oct 2026:** bundles, index files, markers and the loose PDFs go to Azure Blob Storage (`storage/blob_store.py`, `STORAGE_BACKEND=azure`); rows go to `jb_kb.scraped_documents`. The Supabase lines below describe how it was set up before.
- **GitHub (district-court-scraper):** the chosen states are split into groups of whole districts (~30 complexes each, `.github/scripts/plan_groups.py`); each group is one job with its own court list (`--courts_csv`), at most 20 at a time. Daily 22:30 IST, all states, **yesterday's date** (never the automatic mode yet — most complexes had no marker). Weekly re-check on Sundays.
- **Case types written differently** (`STC.NI` in the order list, `STC-NI` in the court's case-type list): exact match first, then without dots / hyphens / spaces, only if exactly one fits (`web/src/utils/court_utils.match_case_type`, used by `download.py` and `mobile_lookup.py`). A case type still not found is logged as a warning.
- **PDF the court never uploaded:** one strike per run in the table `missing_orders` (`database/07_missing_orders.sql`, `jb_db.record_missing_pdf`); after 3 runs the order is `set_aside` and no longer holds its day back. `web/recheck_missing.py` (weekly) re-runs those dates for those complexes, 4 times; then `given_up`. A PDF saved later marks its row `found` (`jb_db.mark_missing_found`). The re-check sets `DC_IGNORE_COMPLETED=1` so a machine that finished the day before still searches it.
- **Tested on Azure 14–16 Oct 2026, Telangana (123 complexes), 14 Oct:** 3 runs of 4 parallel jobs, no block; 145 orders; then Karimnagar (STC.NI/76/2021 saved after the case-type fix), Jinnaram and Godavarikhani (4 orders not uploaded by the court set aside after 3 runs, both complexes marked done); 119 of 123 complexes done (the other 4: date search failures on the court site).

## The two routes, and which one we use

| | web (`web/download.py`) | mobile (`mobile/scraper.py`) |
|---|---|---|
| Site | `services.ecourts.gov.in/ecourtindia_v6/` | `app.ecourts.gov.in/services_DC_4.0/` |
| Search by | **order date** | case type + year |
| CAPTCHA | yes (6-char Securimage, solved by their ONNX model) | none |
| Order number | not returned | returned, with interim/final split |

**We currently run the web route**, because date-range scraping is what the pipeline needs. The web route also asks the eCourts **app** for case details first when `DC_LOOKUP=mobile` (set in the workflow), falling back to the website — see `web/mobile_lookup.py`. The separate mobile scraper is described under "Not done yet".

## Our changes, all marked with a `JurisBound change:` comment

| File | Change |
|---|---|
| `web/download.py` | Sends the site's rotating secret. It goes in **two** headers — `delimeter` plus a second header whose **name also rotates** — both read from `js/components.js`. Without them every ajax POST is refused with `Invalid Request...!` and the scraper dies before the CAPTCHA |
| `web/download.py` | Files named `{cnr}_{order_date}` instead of `{cnr}`. One case has many orders; under the old name the `file_exists()` checks treated every order after the first as already downloaded and skipped it silently |
| `web/download.py` | The `metadata` dict is built unconditionally rather than only when the archive lacks the file, so `jb_db` can still write a row when the archive part already exists |
| `web/download.py` | Calls `jb_db.record_order()` after each downloaded PDF |
| `web/download.py` | Imports `jb_db` ahead of `archive_manager`, so `.env (repo root)` is loaded before any boto3 client reads the environment |
| `archive_manager.py` | `os.fsync` wrapped in `try/except OSError`. On Windows it raised errno 9 *after* the PDFs were downloaded, so the run aborted before the index files were ever written |
| `jb_db.py` | **new** — uploads each order to our bucket and maps it onto `scraped_documents` |

### Mobile route — original targets the retired 3.0 API

Every one of these had to change before a single call succeeded. Smoke-tested 2026-08-22: session, 36 states, 33 Telangana districts, 4 Medchal complexes.

| File | Change |
|---|---|
| `mobile/api_client.py` | `BASE_URL` `/ecourt_mobile_DC` → `/services_DC_4.0`. The 3.0 host is gone entirely |
| `mobile/api_client.py` | Case types: `caseNumberWebService.php` → `caseTypesWebService.php`, and it needs `court_code_arr` — the full comma list of establishments, not just the first. The old single `court_code` returns `ERROR_courtcode4` |
| `mobile/api_client.py` | Case history is keyed on the CNR under `cino`, not `case_no`. The old key returns an **empty** response with no error code, so it failed silently |
| `mobile/api_client.py` | Orders come back as structured lists, not HTML — original's HTML scraper finds nothing. Shape varies (list / dict / None) so it is normalised first |
| `mobile/api_client.py` | PDFs: `display_pdf.php` answers `error_Error at Parameters`. 4.0 uses `display_pdf_new.php`, which returns `{"pdf_url": …/helpdesk_alias/<hash>.pdf}` — an unauthenticated address you then fetch. **The alias is regenerated per request, so never use it as a dedup key** |
| `mobile/scraper.py` | Passes `case.cino` into the history call, matching the key change above |
| `mobile/api_client.py` | Dead `_download_pdf_UNUSED_OLD` (the 3.0 downloader) deleted rather than adapted |

### 2026-08-24/25 — uploads turned on, readable layout, part-name fix

| File | Change |
|---|---|
| `archive_manager.py` | `_make_s3_client()` points at Supabase. Original built a plain AWS client, so every upload from this copy went to Amazon |
| `archive_manager.py` | `MAX_ARCHIVE_SIZE` 1 GB → 40 MB (`MAX_ARCHIVE_SIZE_MB` overrides). Supabase rejects objects over 50 MB; an oversized part fails to upload and the index is never written |
| `archive_manager.py` | "object missing" detected from the HTTP 404 / `Code`, not the error text — Supabase leaves the message empty, so the original string check never matched and the **first run died** |
| `archive_manager.py` | `_get_s3_dir` writes `courts/district/<state>/<district>/<complex>/<year>/archive/`, names resolved from `courts.csv` |
| `archive_manager.py` | Archive parts named `{archive_type}-part-<ts>`. **Observed corruption:** the PDF part and the details part are created back to back and were named by timestamp alone, so both got `part-20260822T122915.tar` in one folder — the PDF overwrote the details, and `metadata.index.json` went on claiming to hold a file that was gone |
| `web/download.py` | `local_only=False`. The tar parts and, more importantly, **the index** never left the local disk. The index is what a later run reads to know what it already has |
| `web/download.py` | `S3_BUCKET` read from the environment |
| `jb_db.py` | `storage_key` writes `courts/district/<state>/<district>/<complex>/<year>/{orders,details}/` with slugged names, matching the archive path |
| `mobile/generate_parquet.py` | Scan prefix and path pattern follow the new layout — it reads state/district/complex back **out of the folder names**, so it would have silently produced an empty table |
| `mobile/flush_pending.py` | Rebuilds the same path via `_court_names`, or a stranded part is retried into a folder nothing reads |

Neither `generate_parquet.py` nor `flush_pending.py` has been run since being changed.

## Storage

Their tar archives are left exactly as original wrote them. `jb_db` writes a **second, addressable copy** of each order to our storage (Azure Blob Storage since 15 Oct 2026), because a PDF inside a tar has no URL of its own and the `cli.py ocr` → `analyze` → `index` pipeline fetches one PDF at a time.

Current key layout (names come from `courts.csv`, slugged):

```
courts/district/<state>/<district>/<complex>/<year>/orders/<cnr>_<order_no>_<date>.pdf
courts/district/<state>/<district>/<complex>/<year>/details/<cnr>_<order_no>_<date>.json
courts/district/<state>/<district>/<complex>/<year>/archive/   (tar parts + data.index.json)
courts/district/<state>/<district>/<complex>/scraped_through.json   ("done up to" marker)
courts/district/<state>/<district>/<complex>/not_found.json         (orders the court says don't exist)
```

## Document identity

The order number is read from the case history page (the order table row whose PDF code matches the search line). Files are named `<cnr>_<order_no>_<date>`. When no match is found the file falls back to `<cnr>_<date>` with `order_no = 1` and a warning is logged. The database's unique key is `(cnr, order_no, judgment_date)`.

The skip check uses the PDF fingerprint `"<3rd arg>|<4th arg>"` of the search line's `displayPdf(...)` call, stored as `pdf_codes` in each court+year `data.index.json`. The 4th argument alone is **not** unique.

## Metadata we get

The web route's `fetch_case_details` is on by default and costs one extra CAPTCHA per case. It is worth it — it returns the CNR plus roughly twenty fields: case number, petitioner/respondent split, case type, filing and registration numbers and dates, first hearing date, case status, court number and judge, acts with sections, and the full hearing history.

## Running it

```bash
cd district-court/web
python download.py --sync-s3 --state_code 29 --district_code 23 --complex_code 1290218 \
                   --start_date 2026-07-29 --end_date 2026-07-29
```

`dc_completed_tasks.json` records finished tasks and is checked **before** hitting the server; clear it to force a re-run. Re-running is safe — orders already saved are skipped by PDF code, and `jb_db` skips a row whose `source_url` is already stored.

If Ghostscript is not installed, PDF compression logs a warning and is skipped. That is expected, not a failure. The GitHub workflow installs it.

## Mobile route — measured 2026-08-25, one complex, 2024

First real run. Stopped at **17 of 144 work units** by throttling; captured 29
case records and **1 PDF**.

```
   cnr                       29/29
   petitioner / respondent   29/29   properly separated
                                     (the web route mashes both into one string)
   advocates, both sides     21/29   the web route has none
   real order number, interim vs final marked
   acts                       0/29   the web route returns these
   hearing history            0/29   the web route returns this
   outcome                    a bare number (4, 20, 21, 35, 36, 46) with
                              NO lookup table anywhere in their code
```

34 history calls failed against 39 connection timeouts — the host throttles, and
the default pause between calls is 0.3 s. **Whether the empty acts/history and
the 1-PDF-in-29 are real for these case types, or fallout from those failures,
is unknown** — no run has completed cleanly.

So mobile is strong for *case records* and weak for *documents*; the web route is
the opposite.

## Do not wire mobile to write until these are settled

Both routes share **one** index per complex per year. Five problems follow —
recorded in full in the `project-district-two-route-collision` memory:

1. The index compares filenames only. The two routes name the same order
   differently (`<cnr>_<date>.pdf` vs `orders_<year>_<case_no>_<n>.pdf`), so each
   re-downloads what the other already has.
2. The filename is also the database's duplicate check, so it misses too.
3. **The routes disagree about order numbers.** Web has none, so every web row is
   `order_no = 1`; mobile has the court's real number. A case's *second* order is
   `(cnr,1,date)` to web and `(cnr,2,date)` to mobile — two rows, one PDF.
   Renaming does not fix this.
4. Renaming mobile's files costs a re-download of anything already stored.
5. One bundle, two writers: concurrent runs would have the last finisher
   overwrite the other's index. Implied by the code, not observed.

## Not done yet

- The mobile route (`mobile/scraper.py`) now writes order PDFs + `scraped_documents` rows through `jb_db`, and one `court_cases` row per case through `cases_db.py` (cases with no PDF go only to `court_cases`). It has had one small live run (AS/2018) and is **not** part of any workflow yet. Never run it on the same court at the same time as the web route.
- The CNR bridge (`caseNumberSearch.php` turns a case number into a CNR without a CAPTCHA) was tested on 29 Sep 2026 and is in use: `web/mobile_lookup.py` (`DC_LOOKUP=mobile`).
- `--sync-s3-fill` (historical gap-fill mode) has a known fix pending; use explicit `--start_date/--end_date` runs for backfills instead.
