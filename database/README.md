# Database setup SQL

These files create the JurisBound **court-data** tables in an empty PostgreSQL database. They are run once, when a new database is built (for example a move to another server, or a separate test database).

**Nothing in this repository runs them.** The scrapers, the pipeline and the backend only read and write rows in a database that already exists. If you are connecting to the existing JurisBound database, you do not need these files.

The same folder is kept in every JurisBound court repository. **The copies must stay identical**: when one changes, copy it to the others in the same piece of work.

## Files, in the order to run them

| # | File | Creates |
|---|---|---|
| 1 | `01_court_tables.sql` | The tables `scraped_documents`, `court_cases`, `laws`, `scrape_jobs`, `search_queries`, `users` and their indexes. Generated from `storage/models.py`; do not edit by hand |
| 2 | `02_court_search.sql` | The vector extension; the columns the database fills by itself (`judgment_year`, `judgment_month`, `text_search`); the keyword index; the `chunks` table with its vector index |
| 3 | `03_pipeline_columns.sql` | Database-side defaults for `sensitive_case` and `names_hidden`, the index over sensitive rows, and the access setting on `court_cases` |
| 4 | `04_doc_numbers.sql` | JurisBound document numbers (`JB-HC-TS-2026-0000001`): two small tables, the `doc_number` index, and the rule that gives every new row its number when it is inserted |
| 5 | `05_doc_number_codes.sql` | The short codes used in the numbers: 25 High Courts, 36 states, every district. Generated; do not edit by hand |
| 6 | `06_doc_numbers_backfill.sql` | Numbers for rows saved before the rule existed. **Skip on an empty database** |

Run each file as one transaction and stop at the first error.

## Before running

- The files expect **Supabase**: `02_court_search.sql` installs the vector extension into Supabase's `extensions` schema and types the embedding column as `extensions.vector(768)`. On another PostgreSQL service (for example Azure), change those two places to where that service installs the extension.
- `768` is the size of the current embedding model. A different model means a different size (see the pipeline's README, "The embedding model").
- The website's own tables (accounts, matters, payments and so on) are **not** here. Their SQL lives with the website code.

## Keeping the files correct

| When this changes | Do this |
|---|---|
| A column or table in `storage/models.py` | In `court-pipeline`: `python database/generate_court_tables.py`, which rewrites `01_court_tables.sql`. Then copy the file to the other repositories |
| A column is added directly in the database | Add the same change to `storage/models.py` and regenerate, in the same piece of work. A default value or a special index goes into `03_pipeline_columns.sql` |
| The list of courts or districts | In `court-scrapers`: `python database/build_doc_number_codes.py`, which rewrites `05_doc_number_codes.sql`. Then copy the file to the other repositories and run it on the database |

Both scripts are in every repository, for reference. Each one works in the repository that holds what it reads: `generate_court_tables.py` in `court-pipeline` (the complete table list) and `build_doc_number_codes.py` in `court-scrapers` (the court lists). Run anywhere else, a script stops with a message naming the right repository and writes nothing.

## Checked against the live database

On 12 October 2026 the files were compared with the live JurisBound database: every table has the same columns (for example 48 of 48 on `scraped_documents`), every index created by these files exists in the live database, and the two defaults, the insert rule and the `court_cases` access setting are present.

They have **not** been run against an empty database since they were regenerated. Do that on a scratch database before relying on them for a real move.

One known difference: `01_court_tables.sql` and `04_doc_numbers.sql` both make `doc_number` unique, so a database built from these files has one extra index on that column compared with the live one. It is harmless.
