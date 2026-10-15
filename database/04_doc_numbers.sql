-- JurisBound document numbers -- tables, column and the automatic rule.
-- Run order: 04_doc_numbers.sql -> 05_doc_number_codes.sql -> 06_doc_numbers_backfill.sql (backfill only
-- where documents already exist; on a fresh Azure database skip it).
--
--   JB-SC-2026-0000001
--   JB-HC-TS-2026-0000001          one series per High Court
--   JB-DC-TS-MED-2026-0000001      one series per district
--
-- The number is written by the database when a row is inserted, so every
-- scraper (GitHub or local) and any future upload gets one without code
-- changes. It is never changed afterwards.

BEGIN;

-- Short codes (filled by 02_codes.sql)
CREATE TABLE IF NOT EXISTS doc_series_codes (
    kind  text NOT NULL,          -- 'hc' | 'dc_state' | 'district'
    key   text NOT NULL,          -- hc: court_name; dc_state: state_code; district: state_code_district_code
    code  text NOT NULL,
    label text,
    PRIMARY KEY (kind, key)
);

-- Running count per series per year
CREATE TABLE IF NOT EXISTS doc_number_counters (
    series     text    NOT NULL,  -- e.g. JB-HC-TS
    year       integer NOT NULL,
    last_value integer NOT NULL,
    PRIMARY KEY (series, year)
);

-- Not reachable through the public Supabase API; the backend uses its own login.
ALTER TABLE doc_series_codes    ENABLE ROW LEVEL SECURITY;
ALTER TABLE doc_number_counters ENABLE ROW LEVEL SECURITY;

ALTER TABLE scraped_documents ADD COLUMN IF NOT EXISTS doc_number varchar(40);
CREATE UNIQUE INDEX IF NOT EXISTS uq_doc_number ON scraped_documents (doc_number);

-- Series for a row. A court missing from doc_series_codes falls back to its
-- eCourts code, so an insert never fails for want of a short code.
CREATE OR REPLACE FUNCTION jb_doc_series(
    p_court_type text, p_court_name text, p_meta json
) RETURNS text
LANGUAGE sql STABLE
SET search_path = public
AS $$
    SELECT CASE p_court_type
        WHEN 'supreme' THEN 'JB-SC'
        WHEN 'high' THEN 'JB-HC-' || COALESCE(
            (SELECT code FROM doc_series_codes WHERE kind = 'hc' AND key = p_court_name),
            'X' || replace(COALESCE(p_meta->>'court_code', 'NA'), '~', '_'))
        WHEN 'district' THEN 'JB-DC-'
            || COALESCE(
                (SELECT code FROM doc_series_codes WHERE kind = 'dc_state' AND key = p_meta->>'state_code'),
                'S' || COALESCE(p_meta->>'state_code', 'NA'))
            || '-'
            || COALESCE(
                (SELECT code FROM doc_series_codes WHERE kind = 'district'
                  AND key = (p_meta->>'state_code') || '_' || (p_meta->>'district_code')),
                'D' || COALESCE(p_meta->>'district_code', 'NA'))
        ELSE 'JB-' || upper(COALESCE(p_court_type, 'XX'))
    END
$$;

-- Before each insert: take the next number of the row's series and year.
-- The counter update runs in the same transaction as the insert, so a failed
-- insert also undoes the counter (no gaps), and two parallel scrapers can
-- never get the same number (the counter row is locked until commit).
CREATE OR REPLACE FUNCTION jb_assign_doc_number() RETURNS trigger
LANGUAGE plpgsql
SET search_path = public
AS $$
DECLARE
    v_series text;
    v_year   integer;
    v_next   integer;
BEGIN
    IF NEW.doc_number IS NOT NULL THEN
        RETURN NEW;
    END IF;
    v_series := jb_doc_series(NEW.court_type, NEW.court_name, NEW.extra_metadata);
    -- judgment year; no date -> year it was saved (India time)
    v_year := COALESCE(extract(year FROM NEW.judgment_date)::int,
                       extract(year FROM (now() AT TIME ZONE 'Asia/Kolkata'))::int);
    INSERT INTO doc_number_counters AS c (series, year, last_value)
    VALUES (v_series, v_year, 1)
    ON CONFLICT (series, year) DO UPDATE SET last_value = c.last_value + 1
    RETURNING last_value INTO v_next;
    NEW.doc_number := v_series || '-' || v_year || '-' || lpad(v_next::text, 7, '0');
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS trg_assign_doc_number ON scraped_documents;
CREATE TRIGGER trg_assign_doc_number
    BEFORE INSERT ON scraped_documents
    FOR EACH ROW EXECUTE FUNCTION jb_assign_doc_number();

COMMIT;
