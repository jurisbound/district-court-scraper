-- Orders the court lists but whose PDF cannot be downloaded (the court has not
-- uploaded the file). One row per order per court complex.
--
-- How the district scraper uses it (district-court/jb_db.py):
--   * every run that fails to download the PDF adds one strike (once per run);
--   * status 'retrying'  -> the order still holds its day back (day not done);
--   * after 3 strikes     -> 'set_aside': the day can be marked done without it;
--   * district-court/web/recheck_missing.py tries set-aside orders again once a
--     week; after 4 weekly re-checks without the PDF -> 'given_up';
--   * when the PDF is finally saved -> 'found', with found_at and doc_id.
--
-- Run as the login that owns the court tables (Azure: jb_court, default
-- schema jb_kb). Safe to run again.

CREATE TABLE IF NOT EXISTS missing_orders (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    court_type     text NOT NULL DEFAULT 'district',
    state_code     text,
    district_code  text,
    complex_code   text NOT NULL,
    complex_name   text,
    case_number    text,
    order_date     date,
    pdf_code       text NOT NULL,
    reason         text NOT NULL DEFAULT 'pdf_not_uploaded',
    strikes        integer NOT NULL DEFAULT 0,
    rechecks       integer NOT NULL DEFAULT 0,
    status         text NOT NULL DEFAULT 'retrying'
                   CHECK (status IN ('retrying', 'set_aside', 'given_up', 'found')),
    first_seen     timestamptz NOT NULL DEFAULT now(),
    last_tried     timestamptz,
    last_run       text,
    found_at       timestamptz,
    doc_id         uuid REFERENCES scraped_documents(id) ON DELETE SET NULL,
    CONSTRAINT uq_missing_orders_complex_pdf UNIQUE (complex_code, pdf_code)
);

CREATE INDEX IF NOT EXISTS ix_missing_orders_status ON missing_orders (status, last_tried);
