-- 01_court_tables.sql
-- The court-data tables, generated from storage/models.py (SQLAlchemy),
-- including the (cnr, order_no, judgment_date) document-identity index.
-- Do not hand-edit: regenerate with  python database/generate_court_tables.py
-- Equivalent to Base.metadata.create_all() against an empty database.

CREATE TABLE court_cases (
	id UUID NOT NULL, 
	court_type VARCHAR(50), 
	court_name VARCHAR(200), 
	state VARCHAR(100), 
	district VARCHAR(200), 
	court_code VARCHAR(20), 
	cnr VARCHAR(32), 
	case_number VARCHAR(200), 
	case_type VARCHAR(200), 
	filing_number VARCHAR(100), 
	filing_date DATE, 
	registration_number VARCHAR(100), 
	registration_date DATE, 
	petitioner TEXT, 
	respondent TEXT, 
	petitioner_advocate TEXT, 
	respondent_advocate TEXT, 
	judge TEXT, 
	status VARCHAR(20), 
	case_stage TEXT, 
	first_hearing_date DATE, 
	next_hearing_date DATE, 
	decision_date DATE, 
	disposal_nature TEXT, 
	acts JSON, 
	hearing_history JSON, 
	orders JSON, 
	source VARCHAR(50), 
	last_checked_at TIMESTAMP WITHOUT TIME ZONE, 
	created_at TIMESTAMP WITHOUT TIME ZONE, 
	updated_at TIMESTAMP WITHOUT TIME ZONE, 
	extra_metadata JSON, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_court_cases_case_number ON court_cases (case_number);
CREATE INDEX ix_court_cases_court_name ON court_cases (court_name);
CREATE INDEX ix_court_cases_court_type ON court_cases (court_type);
CREATE INDEX ix_court_cases_district ON court_cases (district);
CREATE INDEX ix_court_cases_next_hearing_date ON court_cases (next_hearing_date);
CREATE INDEX ix_court_cases_state ON court_cases (state);
CREATE INDEX ix_court_cases_status ON court_cases (status);
CREATE UNIQUE INDEX uq_court_cases_cnr ON court_cases (cnr);

CREATE TABLE laws (
	id UUID NOT NULL, 
	name TEXT NOT NULL, 
	short_name VARCHAR(30), 
	year INTEGER, 
	category VARCHAR(100), 
	description TEXT, 
	total_citations INTEGER, 
	sections_cited JSON, 
	court_breakdown JSON, 
	outcome_breakdown JSON, 
	year_breakdown JSON, 
	top_judges JSON, 
	top_courts JSON, 
	top_parties JSON, 
	created_at TIMESTAMP WITHOUT TIME ZONE, 
	updated_at TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_laws_category ON laws (category);
CREATE UNIQUE INDEX ix_laws_name ON laws (name);
CREATE INDEX ix_laws_total_citations ON laws (total_citations);
CREATE INDEX ix_laws_year ON laws (year);

CREATE TABLE scrape_jobs (
	id UUID NOT NULL, 
	court_key VARCHAR(100), 
	court_name VARCHAR(200), 
	status VARCHAR(50), 
	start_year INTEGER, 
	end_year INTEGER, 
	docs_scraped INTEGER, 
	docs_failed INTEGER, 
	error_log TEXT, 
	created_at TIMESTAMP WITHOUT TIME ZONE, 
	started_at TIMESTAMP WITHOUT TIME ZONE, 
	finished_at TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_scrape_jobs_court_key ON scrape_jobs (court_key);

CREATE TABLE scraped_documents (
	id UUID NOT NULL, 
	court_name VARCHAR(200) NOT NULL, 
	court_type VARCHAR(50), 
	state VARCHAR(100), 
	district VARCHAR(200), 
	case_number VARCHAR(200), 
	cnr VARCHAR(32), 
	order_no INTEGER NOT NULL, 
	case_type VARCHAR(100), 
	year INTEGER, 
	parties TEXT, 
	petitioner TEXT, 
	respondent TEXT, 
	judgment_date DATE, 
	coram TEXT, 
	judges JSON, 
	subject TEXT, 
	raw_text TEXT, 
	summary TEXT, 
	pdf_url VARCHAR(500), 
	pdf_path VARCHAR(500), 
	source_url VARCHAR(500), 
	citations JSON, 
	acts_cited JSON, 
	statutes JSON, 
	legal_issues JSON, 
	outcome TEXT, 
	petitioner_advocate TEXT, 
	respondent_advocate TEXT, 
	disposal_nature TEXT, 
	relief_granted TEXT, 
	headnotes TEXT, 
	category VARCHAR(100), 
	keywords JSON, 
	document_type VARCHAR(30), 
	sensitive_case BOOLEAN NOT NULL, 
	names_hidden BOOLEAN NOT NULL, 
	reportable BOOLEAN, 
	doc_number VARCHAR(40), 
	layout JSONB, 
	is_indexed BOOLEAN, 
	ocr_done BOOLEAN, 
	created_at TIMESTAMP WITHOUT TIME ZONE, 
	updated_at TIMESTAMP WITHOUT TIME ZONE, 
	extra_metadata JSON, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_source_url UNIQUE (source_url), 
	UNIQUE (doc_number)
);

CREATE INDEX ix_scraped_documents_case_number ON scraped_documents (case_number);
CREATE INDEX ix_scraped_documents_cnr ON scraped_documents (cnr);
CREATE INDEX ix_scraped_documents_court_name ON scraped_documents (court_name);
CREATE INDEX ix_scraped_documents_court_type ON scraped_documents (court_type);
CREATE INDEX ix_scraped_documents_district ON scraped_documents (district);
CREATE INDEX ix_scraped_documents_document_type ON scraped_documents (document_type);
CREATE INDEX ix_scraped_documents_state ON scraped_documents (state);
CREATE INDEX ix_scraped_documents_year ON scraped_documents (year);
CREATE UNIQUE INDEX uq_cnr_order_date ON scraped_documents (cnr, order_no, judgment_date);

CREATE TABLE search_queries (
	id UUID NOT NULL, 
	query_text TEXT, 
	filters JSON, 
	result_count INTEGER, 
	latency_ms FLOAT, 
	created_at TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id)
);


CREATE TABLE users (
	id UUID NOT NULL, 
	email VARCHAR(255) NOT NULL, 
	hashed_password VARCHAR(255), 
	full_name VARCHAR(255), 
	is_admin BOOLEAN, 
	is_active BOOLEAN, 
	created_at TIMESTAMP WITHOUT TIME ZONE, 
	api_key VARCHAR(64), 
	PRIMARY KEY (id)
);

CREATE UNIQUE INDEX ix_users_api_key ON users (api_key);
CREATE UNIQUE INDEX ix_users_email ON users (email);

