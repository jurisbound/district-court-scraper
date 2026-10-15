"""JurisBound integration for the adapted district-court scraper.

The original scraper packs every PDF for a court complex into one tar archive
and knows nothing about our database. Our enrichment pipeline (`cli.py ocr` →
`analyze` → `index`) works the other way round: it reads rows from
`scraped_documents` and fetches one PDF at a time by its own address.

This module bridges the two. For each downloaded order it:

  1. uploads the PDF and its metadata to our bucket as individually addressable
     objects, using the same key layout as `high-court`, and
  2. writes one row into `scraped_documents`.

Their tar archive is left completely alone -- this is a second copy, not a
replacement.

Identity note: the district date search never returns an order number. Since
2026-09-28 the scraper reads it off the case history page (matched by the PDF
code) and passes it in as `order_no`; when that match fails, and for rows
written before then, `order_no = 1` and rows are separated by `judgment_date`.
See the unique index `uq_cnr_order_date` in storage/models.py.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# The adapted scraper runs with district-court/web as the working directory. Put the repo
# root on the path so we reuse the existing models and settings rather than
# redefining the schema here.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Load .env (repo root) at import time. download.py imports this module before
# archive_manager, so this runs before any boto3 client reads the environment.
try:
    from dotenv import load_dotenv

    load_dotenv(_REPO_ROOT / ".env")
except Exception:  # pragma: no cover
    pass

logger = logging.getLogger(__name__)

_s3_client = None


# ── storage ─────────────────────────────────────────────────────────────────

def _client():
    """Lazily build the S3 client pointed at our Supabase bucket."""
    global _s3_client
    if _s3_client is None and os.environ.get("STORAGE_BACKEND", "s3").strip().lower() == "azure":
        # JurisBound change: Azure Blob Storage (storage/blob_store.py, same S3 calls)
        from storage.blob_store import storage_client

        _s3_client = storage_client()
    if _s3_client is None:
        import boto3

        _s3_client = boto3.client(
            "s3",
            endpoint_url=os.environ["SUPABASE_S3_ENDPOINT"],
            region_name=os.environ.get("SUPABASE_S3_REGION", "us-east-1"),
            aws_access_key_id=os.environ["SUPABASE_S3_ACCESS_KEY"],
            aws_secret_access_key=os.environ["SUPABASE_S3_SECRET_KEY"],
        )
    return _s3_client


def _slug(text: str) -> str:
    """Lowercase, non-word characters to underscore — the same rule the older
    JurisBound district scraper used, so both trees look like one system."""
    return re.sub(r"_+", "_", re.sub(r"[^\w]", "_", str(text).lower())).strip("_")


def storage_key(data_type: str, year: int, state: str, district: str,
                complex_name: str, filename: str) -> str:
    """Readable, drillable key: court type → state → district → complex → year.

        courts/district/telangana/medchal_malkajgiri/
               medchal_adj_court_complex/2026/orders/<cnr>_<order no>_<date>.pdf

    `orders` holds the PDFs, `details` the JSON sidecars, and `archive` the tar
    parts and their index (written by archive_manager, same folder shape).
    """
    leaf = "details" if data_type == "metadata" else "orders"
    return (
        f"courts/district/{state}/{district}/{complex_name}/{year}/{leaf}/{filename}"
    )


def _public_url(key: str) -> Optional[str]:
    # JurisBound change: Supabase or Azure address, from STORAGE_BACKEND (storage/blob_store.py)
    from storage.blob_store import public_url

    return public_url(key)


def _put(key: str, body: bytes, content_type: str) -> bool:
    try:
        _client().put_object(
            Bucket=os.environ["SUPABASE_S3_BUCKET"],
            Key=key,
            Body=body,
            ContentType=content_type,
        )
        return True
    except Exception as e:
        logger.warning("district-court: upload failed for %s (%s)", key, e)
        return False


# ── parsing helpers ─────────────────────────────────────────────────────────

def _to_date(value):
    """District dates arrive as DD-MM-YYYY; accept the usual variants too."""
    if not value:
        return None
    if hasattr(value, "year") and not isinstance(value, str):
        return value
    text = str(value).strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def split_acts(acts) -> tuple[list, list]:
    """`[{"act": "INDIAN PENAL CODE", "section": "306 IPC"}, ...]` →
    (flat act names, grouped {act, sections})."""
    if not isinstance(acts, list):
        return [], []
    grouped: dict[str, list] = {}
    for entry in acts:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("act") or "").strip()
        if not name:
            continue
        section = (entry.get("section") or "").strip()
        bucket = grouped.setdefault(name, [])
        if section and section not in bucket:
            bucket.append(section)
    return list(grouped), [{"act": k, "sections": v} for k, v in grouped.items()]


# ── mapping ─────────────────────────────────────────────────────────────────

# Written into their own columns below; everything else in the metadata dict
# falls through to extra_metadata untouched.
_PROMOTED = {
    "cnr", "order_no", "case_number", "parties", "petitioner", "respondent", "order_date",
    "case_type_full", "case_status", "court_number_and_judge", "acts",
    "state_name", "district_name", "complex_name",
    "petitioner_advocate", "respondent_advocate", "disposal_nature",
}


def build_row(metadata: dict, pdf_key: str) -> Optional[dict]:
    """Map one scraped order onto our `scraped_documents` columns."""
    cnr = (metadata.get("cnr") or "").strip()
    judgment_date = _to_date(metadata.get("order_date"))
    if not cnr or cnr.startswith("UNKNOWN_"):
        # Without a CNR the row cannot take part in document identity at all.
        # Keep it -- uq_source_url still protects it -- but say so in the log.
        logger.info("district-court: order has no CNR, storing without one (%s)", pdf_key)
        cnr = None

    acts_cited, statutes = split_acts(metadata.get("acts"))
    source_url = _public_url(pdf_key)

    return {
        "court_name": metadata.get("complex_name") or "District Court",
        "court_type": "district",
        "state": metadata.get("state_name"),
        "district": metadata.get("district_name"),
        "case_number": metadata.get("case_number"),
        "cnr": cnr,
        # JurisBound change: the real order number when the scraper matched it on
        # the case history page; otherwise 1, separated by judgment_date --
        # see uq_cnr_order_date.
        "order_no": metadata.get("order_no") or 1,
        "case_type": metadata.get("case_type_full"),
        "year": judgment_date.year if judgment_date else None,
        "parties": metadata.get("parties"),
        "petitioner": metadata.get("petitioner"),
        "respondent": metadata.get("respondent"),
        "judgment_date": judgment_date,
        "coram": metadata.get("court_number_and_judge"),
        "outcome": metadata.get("case_status"),
        # JurisBound change: court-record fields the app lookup provides (empty
        # when the website lookup was used -- it does not return them).
        "petitioner_advocate": metadata.get("petitioner_advocate"),
        "respondent_advocate": metadata.get("respondent_advocate"),
        "disposal_nature": metadata.get("disposal_nature"),
        "acts_cited": acts_cited,
        "statutes": statutes,
        "pdf_path": pdf_key,
        "pdf_url": source_url,
        "source_url": source_url or pdf_key,
        "extra_metadata": {
            **{k: v for k, v in metadata.items() if k not in _PROMOTED},
            "source": "district-court_web",
        },
    }


# ── entry point ─────────────────────────────────────────────────────────────

def record_order(
    metadata: dict,
    pdf_content: bytes,
    year: int,
    pdf_filename: str,
    metadata_filename: str,
) -> bool:
    """Upload one order and write its database row. Returns True if a row was
    written.

    Never raises into the scraper: any failure here logs and returns False so a
    single bad order cannot abort a whole day's run.
    """
    try:
        # Names, not codes. The scraper already carries all three on every
        # order (they come from courts.csv, loaded at startup), so this is a
        # read of what we have rather than a lookup.
        state = _slug(metadata.get("state_name") or f"state_{metadata.get('state_code')}")
        district = _slug(
            metadata.get("district_name") or f"district_{metadata.get('district_code')}"
        )
        complex_name = _slug(
            metadata.get("complex_name") or f"complex_{metadata.get('complex_code')}"
        )

        pdf_key = storage_key("data", year, state, district, complex_name, pdf_filename)
        json_key = storage_key(
            "metadata", year, state, district, complex_name, metadata_filename
        )

        if not _put(pdf_key, pdf_content, "application/pdf"):
            return False
        _put(
            json_key,
            json.dumps(metadata, indent=2, ensure_ascii=False).encode("utf-8"),
            "application/json",
        )

        row = build_row(metadata, pdf_key)
        if not row:
            return False

        from sqlalchemy import select
        from sqlalchemy.exc import IntegrityError

        from storage.database import SyncSessionLocal
        from storage.models import ScrapedDocument

        with SyncSessionLocal() as session:
            existing = session.execute(
                select(ScrapedDocument.id).where(
                    ScrapedDocument.source_url == row["source_url"]
                )
            ).first()
            if existing:
                return False

            session.add(ScrapedDocument(**row))
            try:
                session.commit()
            except IntegrityError as e:
                # Expected when the same case has two orders on one date and
                # the order number could not be matched: both carry
                # order_no = 1 and the same judgment_date, so the second loses
                # to uq_cnr_order_date. Skip and count rather than abort.
                session.rollback()
                logger.warning(
                    "district-court: row rejected by a database constraint (%s) — %s",
                    e.orig.__class__.__name__ if e.orig else "IntegrityError",
                    pdf_key,
                )
                return False
        return True
    except Exception as e:  # never break the scrape
        logger.warning("district-court: record_order failed for %s (%s)", pdf_filename, e)
        return False


# ── orders whose PDF the court has not uploaded ─────────────────────────────
# JurisBound change (2026-10-15): an order the court lists but whose PDF never
# downloads (the court's site says the file is not uploaded) used to hold its
# day back on every run, so the complex was never marked done. Each run that
# fails adds one strike to the table `missing_orders`
# (database/07_missing_orders.sql); from MISSING_PDF_STRIKES strikes on the
# order is "set_aside" and no longer holds the day back. recheck_missing.py
# tries set-aside orders again once a week (4 times); when the PDF is finally
# saved the row becomes "found".

MISSING_PDF_STRIKES = int(os.environ.get("DC_MISSING_PDF_STRIKES", "3"))


def record_missing_pdf(task, pdf_code: str, order_data: dict, run_id: str) -> tuple[int, Optional[str]]:
    """Add this run's strike for an order whose PDF did not download and return
    (strikes, status). One strike per run, however often the order is seen in
    that run. Returns (0, None) if the table cannot be reached, so the caller
    treats the order as an ordinary failure (the day stays not done)."""
    try:
        from sqlalchemy import text

        from storage.database import SyncSessionLocal

        with SyncSessionLocal() as session:
            row = session.execute(
                text(
                    """
                    INSERT INTO missing_orders AS m
                        (state_code, district_code, complex_code, complex_name,
                         case_number, order_date, pdf_code, strikes, last_tried, last_run)
                    VALUES (:state, :district, :complex, :complex_name,
                            :case_number, :order_date, :pdf_code, 1, now(), :run)
                    ON CONFLICT (complex_code, pdf_code) DO UPDATE SET
                        strikes    = m.strikes + CASE WHEN m.last_run IS DISTINCT FROM :run THEN 1 ELSE 0 END,
                        last_tried = now(),
                        last_run   = :run
                    RETURNING id, strikes, status
                    """
                ),
                {
                    "state": str(task.state_code),
                    "district": str(task.district_code),
                    "complex": str(task.complex_code),
                    "complex_name": getattr(task, "complex_name", None),
                    "case_number": order_data.get("case_number"),
                    "order_date": _to_date(order_data.get("order_date")),
                    "pdf_code": pdf_code,
                    "run": run_id,
                },
            ).one()
            row_id, strikes, status = row
            if status == "retrying" and strikes >= MISSING_PDF_STRIKES:
                session.execute(
                    text("UPDATE missing_orders SET status = 'set_aside' WHERE id = :id"),
                    {"id": row_id},
                )
                status = "set_aside"
            session.commit()
            return strikes, status
    except Exception as e:  # never break the scrape
        logger.warning("district-court: could not update missing_orders for %s (%s)",
                       order_data.get("case_number"), e)
        return 0, None


def mark_missing_found(complex_code: str, pdf_code: Optional[str], pdf_filename: str) -> None:
    """The order's PDF is now saved: mark its missing_orders row (if any) found
    and link the judgment row. No row -> nothing happens."""
    if not pdf_code:
        return
    try:
        from sqlalchemy import text

        from storage.database import SyncSessionLocal

        with SyncSessionLocal() as session:
            session.execute(
                text(
                    """
                    UPDATE missing_orders SET
                        status = 'found', found_at = now(),
                        doc_id = (SELECT id FROM scraped_documents
                                  WHERE pdf_path LIKE '%/' || :fname LIMIT 1)
                    WHERE complex_code = :complex AND pdf_code = :pdf_code AND status <> 'found'
                    """
                ),
                {"complex": str(complex_code), "pdf_code": pdf_code, "fname": pdf_filename},
            )
            session.commit()
    except Exception as e:  # never break the scrape
        logger.warning("district-court: could not mark %s found in missing_orders (%s)", pdf_filename, e)
