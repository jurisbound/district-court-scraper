"""JurisBound: write one court case into the `court_cases` table (2026-10-01).

`jb_db.py` saves order documents -- one row per PDF in `scraped_documents`.
This module saves the case itself -- one row per case, with or without any
PDF -- for the case status and advocate features. The two tables join on `cnr`.

Insert-or-update by CNR: checking a case again later updates its row (new
orders, new hearings, next date, status) instead of adding a second one.

Used by the district mobile scraper only.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

import jb_db  # noqa: F401  -- puts the repo root on sys.path and loads .env (repo root)

logger = logging.getLogger(__name__)


def _iso_date(value) -> Optional[date]:
    """App dates: '2009-04-30' (case fields) or '18-08-2026' (orders).
    '0000-00-00' and blanks mean "none"."""
    text = str(value or "").strip()[:10]
    if not text or text.startswith("0000"):
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _text(value) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def order_list(history: dict) -> list:
    """Every order the court lists for the case, with or without a scan.
    Final orders carry their date as order_date1f, interim ones as order_date1."""
    out = []
    for key, is_final in (("finalOrder", True), ("interimOrder", False)):
        orders = history.get(key)
        if isinstance(orders, dict):
            orders = [orders]
        for o in orders if isinstance(orders, list) else []:
            if not isinstance(o, dict):
                continue
            when = o.get("order_date1f") or o.get("order_date1") or o.get("order_date")
            out.append({
                "order_no": int(o["order_id"]) if str(o.get("order_id") or "").isdigit() else None,
                "order_date": _text(when),
                "type": _text(o.get("order_details")),
                "is_final": is_final,
                "has_pdf": bool(o.get("filename")),
            })
    return out


def build_case_row(history: dict, details: dict, *, court_type: str, court_name: str,
                   state: str, district: str, court_code: str, case_number: str,
                   source: str) -> Optional[dict]:
    """Map the app's case record (+ the details dict mobile_lookup builds from
    it) onto `court_cases` columns. None when the record has no CNR."""
    cnr = _text(history.get("cino"))
    if not cnr:
        return None
    decided = _iso_date(history.get("date_of_decision"))
    fil_no, fil_year = history.get("fil_no"), history.get("fil_year")
    reg_no, reg_year = history.get("reg_no"), history.get("reg_year")
    return {
        "court_type": court_type,
        "court_name": court_name,
        "state": state,
        "district": district,
        "court_code": _text(court_code),
        "cnr": cnr,
        "case_number": case_number,
        "case_type": details.get("case_type_full"),
        "filing_number": f"{fil_no}/{fil_year}" if fil_no and fil_year else None,
        "filing_date": _iso_date(history.get("date_of_filing")),
        "registration_number": f"{reg_no}/{reg_year}" if reg_no and reg_year else None,
        "registration_date": _iso_date(history.get("dt_regis")),
        "petitioner": _text(history.get("pet_name")),
        "respondent": _text(history.get("res_name")),
        "petitioner_advocate": _text(history.get("pet_adv")),
        "respondent_advocate": _text(history.get("res_adv")),
        "judge": details.get("court_number_and_judge"),
        "status": "disposed" if decided else "pending",
        "case_stage": None if decided else _text(history.get("purpose_name")),
        "first_hearing_date": _iso_date(history.get("date_first_list")),
        "next_hearing_date": None if decided else _iso_date(history.get("date_next_list")),
        "decision_date": decided,
        "disposal_nature": _text(history.get("disp_name")) if decided else None,
        "acts": details.get("acts") or [],
        "hearing_history": details.get("case_history") or [],
        "orders": order_list(history),
        "source": source,
        "last_checked_at": datetime.utcnow(),
    }


def upsert_case(row: dict) -> bool:
    """Insert the case, or update its existing row (same CNR). Never raises."""
    try:
        from sqlalchemy.dialects.postgresql import insert

        from storage.database import SyncSessionLocal
        from storage.models import CourtCase

        now = datetime.utcnow()
        stmt = insert(CourtCase.__table__).values(
            id=__import__("uuid").uuid4(), created_at=now, updated_at=now,
            extra_metadata={}, **row,
        )
        # On a second visit everything is refreshed except id / created_at.
        stmt = stmt.on_conflict_do_update(
            index_elements=["cnr"],
            set_={**{k: stmt.excluded[k] for k in row if k != "cnr"}, "updated_at": now},
        )
        with SyncSessionLocal() as session:
            session.execute(stmt)
            session.commit()
        return True
    except Exception as e:  # never break the scrape
        logger.warning("cases_db: could not save case %s (%s)", row.get("cnr"), e)
        return False
