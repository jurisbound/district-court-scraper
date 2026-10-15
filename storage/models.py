"""SQLAlchemy ORM models."""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON, Boolean, Column, Date, DateTime, Float, ForeignKey,
    Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class ScrapedDocument(Base):
    __tablename__ = "scraped_documents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    court_name = Column(String(200), nullable=False, index=True)
    court_type = Column(String(50), index=True)  # supreme, high, district, tribunal
    state = Column(String(100), index=True)
    district = Column(String(200), index=True)   # district courts only (e.g. "Ranga Reddy")

    case_number = Column(String(200), index=True)
    # National case identifier. One case (one CNR) has many orders, so document
    # identity is (cnr, order_no, judgment_date) — all three are encoded in the
    # HC filename, e.g. HBHC010000552025_1_2025-01-02.pdf. NULL for the Supreme
    # Court, which has no CNR, and for district rows scraped before this.
    cnr = Column(String(32), index=True)
    # The court's own order number where it is published (HC). The district
    # portal's date search does not return one, so district rows keep the
    # default 1 and are separated by judgment_date instead.
    order_no = Column(Integer, nullable=False, default=1)
    case_type = Column(String(100))
    year = Column(Integer, index=True)
    parties = Column(Text)                      # full "X vs Y" title
    petitioner = Column(Text)                   # LLM-split petitioner name
    respondent = Column(Text)                   # LLM-split respondent name
    judgment_date = Column(Date)
    coram = Column(Text)                        # raw bench / judges string
    judges = Column(JSON, default=list)         # ["Justice K. Lakshman", ...]
    subject = Column(Text)
    raw_text = Column(Text)                     # extracted full text
    summary = Column(Text)                      # 2-3 sentence AI-generated summary

    pdf_url = Column(String(500))
    pdf_path = Column(String(500))  # local MinIO path
    source_url = Column(String(500))

    # Structured extraction fields (populated by LLM analyzer)
    citations = Column(JSON, default=list)      # ["(2024) 1 SCC 123", "AIR 2024 SC 456"]
    acts_cited = Column(JSON, default=list)     # flat: ["Indian Penal Code, 1860", ...]
    statutes = Column(JSON, default=list)       # [{"act": "...", "sections": ["302"]}]
    legal_issues = Column(JSON, default=list)   # ["Whether Article 14 violated...", ...]
    outcome = Column(Text)                      # "allowed" / "dismissed" / "partly allowed"
    # Court's own record, filled by the district scraper's app lookup (2026-09-30).
    # Optional: High Court / Supreme Court rows leave them empty.
    petitioner_advocate = Column(Text)
    respondent_advocate = Column(Text)
    disposal_nature = Column(Text)              # e.g. "DISMISSED WITH COSTS"
    relief_granted = Column(Text)               # e.g. "Compensation of Rs 5 lakhs..."
    headnotes = Column(Text)                    # key legal propositions
    category = Column(String(100))              # LLM-classified: Civil / Criminal / Constitutional / ...
    keywords = Column(JSON, default=list)       # ["writ jurisdiction", "land acquisition", ...]

    is_indexed = Column(Boolean, default=False)  # vector indexed?
    ocr_done = Column(Boolean, default=False)    # OCR extraction done?
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    extra_metadata = Column(JSON, default=dict)

    __table_args__ = (
        UniqueConstraint("source_url", name="uq_source_url"),
        # Replaces the old uq_court_case (court_name, case_number), which allowed
        # only one row per case and so silently rejected every order after the
        # first. Declared as a unique Index because NULL cnr values (Supreme
        # Court, un-rescraped district rows) must not collide with each other.
        #
        # judgment_date is part of the rule because the district portal's date
        # search returns no order number -- every district row carries the
        # default order_no = 1, so without the date the second order of a case
        # would be rejected as a duplicate. order_no stays in the rule so the
        # High Court can still hold two orders passed on the same date.
        Index("uq_cnr_order_date", "cnr", "order_no", "judgment_date", unique=True),
    )

    @property
    def has_pdf_source(self) -> bool:
        src = self.source_url or ""
        pdf = self.pdf_url or ""
        return (
            bool(self.pdf_path)
            or src.lower().endswith(".pdf") or "view-pdf" in src or "/pdf" in src
            or pdf.lower().endswith(".pdf")
        )

    def __repr__(self):
        return f"<Doc {self.court_name} | {self.case_number}>"


class CourtCase(Base):
    """One row per court case -- with or without any order PDF (2026-10-01).

    `scraped_documents` holds order documents (one row per PDF); this table
    holds the case itself: parties, advocates, status, hearings. The two join
    on `cnr`. Filled by the district mobile scraper first; High Court and
    Supreme Court rows are planned for later, hence `court_type`. Re-scanning a
    case updates its row instead of adding a new one (pending cases change).
    """
    __tablename__ = "court_cases"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    court_type = Column(String(50), index=True)   # district, high, supreme
    court_name = Column(String(200), index=True)  # complex / High Court name
    state = Column(String(100), index=True)
    district = Column(String(200), index=True)
    court_code = Column(String(20))               # establishment inside the complex

    # National case identifier; one row per CNR. NULL allowed for the Supreme
    # Court (no CNR) -- NULLs never collide in the unique index.
    cnr = Column(String(32))
    case_number = Column(String(200), index=True)  # e.g. "OS/132/2025"
    case_type = Column(String(200))                # e.g. "OS - ORIGINAL SUIT"
    filing_number = Column(String(100))
    filing_date = Column(Date)
    registration_number = Column(String(100))
    registration_date = Column(Date)

    petitioner = Column(Text)
    respondent = Column(Text)
    petitioner_advocate = Column(Text)
    respondent_advocate = Column(Text)
    judge = Column(Text)                           # court number and judge

    status = Column(String(20), index=True)        # pending / disposed
    case_stage = Column(Text)                      # purpose of the next hearing
    first_hearing_date = Column(Date)
    next_hearing_date = Column(Date, index=True)
    decision_date = Column(Date)
    disposal_nature = Column(Text)                 # e.g. "DISMISSED WITH COSTS"

    acts = Column(JSON, default=list)              # [{"act": ..., "section": ...}]
    hearing_history = Column(JSON, default=list)   # [{judge, business_date, hearing_date, purpose}]
    orders = Column(JSON, default=list)            # [{order_no, order_date, is_final, has_pdf}]

    source = Column(String(50))                    # e.g. "dc_mobile"
    last_checked_at = Column(DateTime)             # last time the court was asked
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    extra_metadata = Column(JSON, default=dict)

    __table_args__ = (
        Index("uq_court_cases_cnr", "cnr", unique=True),
    )

    def __repr__(self):
        return f"<Case {self.court_name} | {self.case_number}>"


class Law(Base):
    """One row per unique Indian statute / act cited across all documents."""
    __tablename__ = "laws"

    id             = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name           = Column(Text, unique=True, nullable=False, index=True)
    short_name     = Column(String(30))
    year           = Column(Integer, index=True)
    category       = Column(String(100), index=True)
    description    = Column(Text)  # LLM-generated plain-English summary

    total_citations   = Column(Integer, default=0, index=True)
    sections_cited    = Column(JSON, default=dict)    # {"302": 45, "307": 23}
    court_breakdown   = Column(JSON, default=dict)    # {"supreme": 5, "high": 120}
    outcome_breakdown = Column(JSON, default=dict)    # {"allowed": 30, "dismissed": 50}
    year_breakdown    = Column(JSON, default=dict)    # {"2020": 10, "2021": 15}
    top_judges        = Column(JSON, default=list)    # [{"name": "…", "count": 5}]
    top_courts        = Column(JSON, default=list)    # [{"name": "…", "count": 5}]
    top_parties       = Column(JSON, default=list)    # [{"name": "…", "count": 3}]

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ScrapeJob(Base):
    __tablename__ = "scrape_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    court_key = Column(String(100), index=True)  # e.g. "delhi", "sci", "ecourts"
    court_name = Column(String(200))
    status = Column(String(50), default="pending")  # pending, running, done, failed
    start_year = Column(Integer)
    end_year = Column(Integer)
    docs_scraped = Column(Integer, default=0)
    docs_failed = Column(Integer, default=0)
    error_log = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime)
    finished_at = Column(DateTime)


class SearchQuery(Base):
    __tablename__ = "search_queries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    query_text = Column(Text)
    filters = Column(JSON, default=dict)
    result_count = Column(Integer)
    latency_ms = Column(Float)
    created_at = Column(DateTime, default=datetime.utcnow)


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255))
    full_name = Column(String(255))
    is_admin = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    api_key = Column(String(64), unique=True, index=True)
