-- 02_court_search.sql
-- Run after 01_court_tables.sql.
--
-- The search pieces of the court tables: the vector extension, the columns the
-- database fills by itself, the keyword index and the chunks table. Each block
-- names what uses it. Tables are unqualified so the file can be test-run in a
-- scratch schema via search_path.

-- pgvector, in Supabase's default extensions schema (on the default search_path).
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions;

-- Browse pages and the month filter of search (backend).
-- Derived by the database from judgment_date; no code ever writes them.
ALTER TABLE scraped_documents
  ADD COLUMN judgment_year  integer GENERATED ALWAYS AS (EXTRACT(YEAR  FROM judgment_date)::integer) STORED,
  ADD COLUMN judgment_month integer GENERATED ALWAYS AS (EXTRACT(MONTH FROM judgment_date)::integer) STORED;

-- Keyword side of hybrid search (backend). Weights:
-- parties/coram (A), case number/subject (B), category (C), raw_text (D).
-- Kept current by the database on every insert/update.
ALTER TABLE scraped_documents
  ADD COLUMN text_search tsvector GENERATED ALWAYS AS (
      setweight(to_tsvector('english', coalesce(parties, '')     || ' ' || coalesce(coram, '')),   'A')
   || setweight(to_tsvector('english', coalesce(case_number, '') || ' ' || coalesce(subject, '')), 'B')
   || setweight(to_tsvector('english', coalesce(category, '')), 'C')
   || setweight(to_tsvector('english', coalesce(raw_text, '')), 'D')
  ) STORED;
CREATE INDEX ix_scraped_documents_text_search ON scraped_documents USING gin (text_search);

-- Vector side of hybrid search. Columns match the INSERT in the pipeline's indexer.py.
-- 768 = EMBED_DIM for Snowflake/snowflake-arctic-embed-m (the pipeline's configs/settings.py).
-- Changing the embedding model means changing this size and re-indexing.
CREATE TABLE chunks (
  id           uuid PRIMARY KEY,
  doc_id       uuid NOT NULL REFERENCES scraped_documents(id) ON DELETE CASCADE,
  chunk_index  integer NOT NULL,
  chunk_count  integer,
  chunk_text   text,
  snippet      text,
  embedding    extensions.vector(768)
);
CREATE INDEX chunks_doc_id ON chunks (doc_id);
CREATE INDEX chunks_embedding_hnsw ON chunks USING hnsw (embedding extensions.vector_cosine_ops);
