-- One-time numbering of documents saved before the rule existed.
-- Oldest judgment first within each series and year; ties by save time.
-- Safe to re-run: only rows without a number are touched, and counters
-- continue from the highest number already given.

BEGIN;

LOCK TABLE doc_number_counters IN EXCLUSIVE MODE;

WITH todo AS (
    SELECT id,
           jb_doc_series(court_type, court_name, extra_metadata) AS series,
           COALESCE(extract(year FROM judgment_date)::int,
                    extract(year FROM created_at)::int) AS year,
           judgment_date, created_at
    FROM scraped_documents
    WHERE doc_number IS NULL
),
numbered AS (
    SELECT t.id, t.series, t.year,
           COALESCE(c.last_value, 0)
             + row_number() OVER (PARTITION BY t.series, t.year
                                  ORDER BY t.judgment_date NULLS LAST, t.created_at, t.id) AS n
    FROM todo t
    LEFT JOIN doc_number_counters c ON c.series = t.series AND c.year = t.year
)
UPDATE scraped_documents d
SET doc_number = n.series || '-' || n.year || '-' || lpad(n.n::text, 7, '0')
FROM numbered n
WHERE d.id = n.id;

-- Counters = highest number now used in each series and year
INSERT INTO doc_number_counters (series, year, last_value)
SELECT substring(doc_number FROM '^(.*)-\d{4}-\d{7}$'),
       substring(doc_number FROM '-(\d{4})-\d{7}$')::int,
       max(substring(doc_number FROM '-(\d{7})$')::int)
FROM scraped_documents
WHERE doc_number IS NOT NULL
GROUP BY 1, 2
ON CONFLICT (series, year) DO UPDATE SET last_value = GREATEST(doc_number_counters.last_value, EXCLUDED.last_value);

COMMIT;
