# CLAUDE.md

Guidance for AI coding assistants (and people) working in this repository. User documentation is in `README.md`; developer notes for the scraper code are in `district-court/AGENTS.md` — read both before changing anything. Last updated 16 October 2026.

## What this is

The JurisBound district court scraper and the GitHub Actions workflows that run it. It collects district court orders from the eCourts services website, saves each PDF to Azure Blob Storage and one row (with its JurisBound number) to the JurisBound database (schema `jb_kb`), and keeps a "done up to" date per court complex.

This repository is **public**: everything in it, and every run log, can be read by anyone.

| Path | What |
|---|---|
| `district-court/` | The scraper. **Identical copy in the private `court-scrapers` repository** (used on laptops); change both together |
| `storage/`, `configs/`, `database/` | Shared database / storage code and setup SQL, identical in every JurisBound court repository |
| `.github/workflows/district-courts.yml` | Scraping: states → groups of whole districts → one job each (at most 20 at a time) |
| `.github/workflows/district-recheck-missing.yml` | Weekly re-check of orders whose PDF the court had not uploaded |
| `.github/scripts/plan_groups.py` | Splits states into job groups; writes each job's court list |

## Hard rules

- **Public repository:** never commit `.env`, secrets, connection strings, internet addresses, people's names or internal notes. Secrets live only in GitHub secrets (`AZURE_KB_DATABASE_URL`, `AZURE_KB_ASYNC_DATABASE_URL`, `AZURE_STORAGE_CONNECTION_STRING`).
- **No names or links of other projects** anywhere (code, comments, README, commit messages).
- **Be gentle to the court website.** Do not raise `--max_workers` (2) or shorten `DC_CASE_PAUSE` (5 s). The website blocks an internet address that is too fast; speed comes from more jobs on more GitHub machines.
- **Jobs of one run must never share a court complex** (everything stored is kept per complex). Keep whole districts together in `plan_groups.py`; keep the workflow-level concurrency so two runs never overlap.
- **First run of a state: always with dates.** A complex without a "done up to" date starts from 1 January in the automatic mode.
- **Never delete or rewrite** index files, `scraped_through.json` or `not_found.json` in storage, or rows of `missing_orders`.
- Run logs must not print party names (today they show complex names, case numbers, CNRs and dates only). Check any new log line for this.

## How a scheduled run works

Daily 22:30 IST (GitHub starts it 4–6 hours late): all states, **yesterday's date** (India time, worked out once by the plan job). Not the automatic mode yet, because most complexes have no "done up to" date. A day is "done" for a complex only when all its orders are saved or set aside; otherwise a later dated run or the automatic mode must search it again.

Orders that cannot be saved: "Record not found" (3 runs, then listed in `not_found.json`); PDF not uploaded by the court (3 runs, then `set_aside` in `missing_orders`, weekly re-check 4 times, then `given_up`); a case type written differently (`STC.NI` / `STC-NI`) is matched without punctuation.

## Verifying a change

Run one complex for one day on a laptop (`cd district-court/web`, `python download.py --sync-s3 --courts_csv ... --start_date D --end_date D`), check the rows in `jb_kb.scraped_documents` and the files in storage, and that a second run saves nothing new. Then one manual GitHub run with one state before relying on the schedule.

## State and pending (16 October 2026)

- Telangana tested (14 Oct 2026): 119 of 123 complexes done; the first all-states scheduled night is 16 Oct.
- Pending: decide whether to switch the schedule to the automatic mode once every complex has a "done up to" date; the mobile route (`district-court/mobile`) is not in any workflow (backfill / full case details later); remove the copy in `court-scrapers` once this repository has proven itself.
