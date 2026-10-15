-- 03_pipeline_columns.sql
-- Run after 02_court_search.sql.
--
-- Pieces of the court tables that the live database has and that
-- 01_court_tables.sql cannot express, because it is generated from the Python
-- models: database-side defaults, one partial index and one access setting.
-- Written from the live database on 2026-10-12. Safe to run again.

-- The pipeline's columns (OCR and analysis steps). 01_court_tables.sql already
-- creates them on a new database; these lines add them to a database built
-- from an older copy of that file.
ALTER TABLE scraped_documents
  ADD COLUMN IF NOT EXISTS document_type  varchar(30),
  ADD COLUMN IF NOT EXISTS sensitive_case boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS reportable     boolean,
  ADD COLUMN IF NOT EXISTS layout         jsonb,
  ADD COLUMN IF NOT EXISTS names_hidden   boolean NOT NULL DEFAULT false;

-- Database-side defaults. The scrapers insert rows without these two columns,
-- so without a default every scraper insert would fail.
ALTER TABLE scraped_documents
  ALTER COLUMN sensitive_case SET DEFAULT false,
  ALTER COLUMN names_hidden   SET DEFAULT false;

CREATE INDEX IF NOT EXISTS ix_scraped_documents_document_type
  ON scraped_documents (document_type);
-- Small index over the sensitive rows only.
CREATE INDEX IF NOT EXISTS ix_scraped_documents_sensitive_case
  ON scraped_documents (sensitive_case) WHERE sensitive_case;

-- court_cases is not reachable through the public Supabase API; the scrapers
-- and the backend use their own database login.
ALTER TABLE court_cases ENABLE ROW LEVEL SECURITY;
