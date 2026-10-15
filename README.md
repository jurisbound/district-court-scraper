# JurisBound district court scraper

Collects district court orders and judgments from the eCourts services website for JurisBound. For each order it saves the PDF to Azure Blob Storage and one row to the JurisBound database, and keeps a "done up to" marker for every court complex so the next run continues from there.

## What is in this repository

| Path | What it is |
|---|---|
| `district-court/web/download.py` | The scraper: date search per court complex, case lookup, PDF download |
| `district-court/web/mobile_lookup.py` | Case details from the eCourts app service (fewer website requests) |
| `district-court/mobile/` | The eCourts app client used by the lookup |
| `district-court/jb_db.py`, `cases_db.py` | Writing order rows and case rows to the database |
| `district-court/archive_manager.py` | Storage index files, bundles and markers |
| `district-court/courts.csv` | All 3,566 court complexes (state, district, complex codes) |
| `storage/`, `configs/` | Shared database, storage and settings code (same as the other JurisBound court repositories) |
| `database/` | SQL that creates the court tables and JB numbering in an empty database. Not used by a run |
| `.github/workflows/district-courts.yml` | The GitHub Actions workflow |

## Running on GitHub

Actions → **JurisBound – District courts** → **Run workflow**.

| Box | Meaning |
|---|---|
| State code(s) | One or more state codes, comma-separated (`29` = Telangana), or `all` for every state |
| Complexes per job | About how many court complexes one job handles (default 30) |
| Start date / End date | `YYYY-MM-DD`. **Always give dates for a first run**: a complex without a marker otherwise starts from 1 January |
| Timeout hours | No new work is started after this; the index and markers are saved before GitHub's 6-hour limit |

The chosen states are cut into groups of whole districts of about "complexes per job" complexes (`.github/scripts/plan_groups.py`); each group is one job on its own GitHub machine. With the default of 30, Telangana is 4 jobs and all of India 123. At most 20 jobs run at a time; the rest wait. A job that fails or is blocked does not stop the others. Run the same states and dates again to finish a job that stopped: saved orders are skipped.

Jobs of one run never share a court complex, and everything the scraper stores (index, markers, PDFs) is kept per complex, so parallel jobs never write the same file. Two runs of the workflow never run at once; a second run waits for the first.

To see the groups before a run: `python .github/scripts/plan_groups.py 29 30`.

There is no daily schedule yet.

### Secrets (Settings → Secrets and variables → Actions)

| Secret | Value |
|---|---|
| `AZURE_KB_DATABASE_URL` | `postgresql://jb_court:…@…:5432/jurisbound_dev_db?sslmode=require` |
| `AZURE_KB_ASYNC_DATABASE_URL` | The same with `postgresql+asyncpg://` and `ssl=require` |
| `AZURE_STORAGE_CONNECTION_STRING` | The storage account's connection string |

Secrets are never shown in the run logs. The database firewall lets GitHub's machines in through its "allow Azure services" rule.

## Running on a laptop

```
python -m venv .venv
.venv\Scripts\activate            (Windows)   |   source .venv/bin/activate   (Linux/Mac)
pip install --extra-index-url https://download.pytorch.org/whl/cpu -r district-court/requirements.txt
```

Copy `.env.example` to `.env` and fill it in. The laptop's internet address must be allowed in the database firewall. On Windows run `$env:PYTHONIOENCODING="utf-8"` first. Then:

```
cd district-court/web
python download.py --sync-s3 --state_code 29 --start_date 2026-10-14 --end_date 2026-10-14
python download.py --sync-s3 --state_code 29 --district_code 23          # automatic dates
```

Without `--sync-s3` nothing is uploaded or saved to the database (a local test).

## Speed and blocking

The court website blocks an internet address that sends requests too fast. The default pace (5-second pause before each case, 2 workers) was tested clean on one address; 2 workers with a 3-second pause were blocked in 90 seconds. When blocked, the run stops sending requests, saves what it has and exits with code 2. Do not raise the pace; run more states side by side instead.

## Run logs

Run logs of a public repository can be read by anyone. The district log shows court complex codes, case numbers, CNRs and dates. It does not print party names or secrets.
