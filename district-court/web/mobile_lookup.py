"""
JurisBound change: case lookup through the eCourts app (mobile API 4.0) for the
district web scraper.

The web route finds orders by date, but each new order then needs the case ID
(CNR), the order number and the case details. On the website that lookup is a
CAPTCHA + case-number search + case page: ~3-4 requests to the host that blocks
us. The app answers the same question with 2 requests and no CAPTCHA, on a
different host (verified live 2026-09-29: OS/328/2009, OS/1470/2013 and
OS/116/2014 in Medchal all gave the CNRs our web lookup had stored; the app's
order numbers 3 and 4 on 18-08-2026 equal the website's).

`lookup()` returns the case details in exactly the shape
Downloader.fetch_case_details() returns them, so file naming, the register and
the database row are unchanged -- or None whenever it is not sure, and the
caller then does the website lookup exactly as before:
  - app unreachable / no session / empty reply
  - case number not in the app, or more than one case left after the party
    check (never guess)
  - not exactly one order in the app's order list on this order's date (two
    orders of one case on one day cannot be told apart without the website's
    PDF code)

Switch, read from the environment (DC_LOOKUP):
  web      (default) the website lookup only -- behaviour as before this file
  mobile   the app first, the website whenever the app returns None
  compare  both; the WEBSITE result is used and saved, the app result is only
           compared and written to DC_COMPARE_LOG (JSON lines)

Nothing in this file runs, and the mobile code is not even imported, in the
default "web" mode.
"""

import json
import logging
import os
import random
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from src.utils.court_utils import match_case_type

logger = logging.getLogger(__name__)

LOOKUP_MODE = os.environ.get("DC_LOOKUP", "web").strip().lower()
# Seconds before each request to the app (plus up to 50% random extra).
APP_GAP_SECONDS = float(os.environ.get("DC_APP_GAP", "1"))
# After this many app failures in a row (no reply, no session), stop using the
# app for the rest of the run; every order then uses the website lookup.
APP_MAX_FAILURES = int(os.environ.get("DC_APP_MAX_FAILURES", "5"))
COMPARE_LOG = Path(os.environ.get("DC_COMPARE_LOG", "dc_lookup_compare.jsonl"))

_MOBILE_DIR = Path(__file__).resolve().parent.parent / "mobile"
_local = threading.local()
_lock = threading.Lock()
_case_types: Dict[str, Dict[str, tuple]] = {}   # complex_code -> {"OS": (17, "OS - ORIGINAL SUIT")}
_failures = 0
_disabled = False
_stats = {"app_ok": 0, "fallback": 0}


class _AppUnavailable(Exception):
    """The app did not answer (as opposed to answering "not found")."""


def _client_class():
    """Import the mobile client only when first needed. Its folder is put on
    sys.path just for this import, so none of its module names (common,
    crypto, scraper, ...) can shadow the web scraper's own."""
    added = str(_MOBILE_DIR) not in sys.path
    if added:
        sys.path.insert(0, str(_MOBILE_DIR))
    try:
        from api_client import MobileAPIClient
    finally:
        if added:
            sys.path.remove(str(_MOBILE_DIR))
    return MobileAPIClient


def _gap():
    if APP_GAP_SECONDS > 0:
        time.sleep(random.uniform(APP_GAP_SECONDS, APP_GAP_SECONDS * 1.5))


def _client():
    """One app session per worker thread."""
    c = getattr(_local, "client", None)
    if c is None:
        c = _client_class()()
        _gap()
        if not c.initialize_session():
            raise _AppUnavailable("app session could not be started")
        _local.client = c
    return c


def _request(endpoint: str, params: dict):
    _gap()
    reply = _client()._make_request(endpoint, params)
    if reply is None:
        raise _AppUnavailable(f"no reply from {endpoint}")
    return reply


def _types_for(task) -> Dict[str, tuple]:
    with _lock:
        cached = _case_types.get(task.complex_code)
    if cached is not None:
        return cached
    _gap()
    types = _client().get_case_types(
        int(task.state_code), int(task.district_code), task.court_numbers
    )
    if not types:
        raise _AppUnavailable("no case types from the app")
    table = {}
    for t in types:
        short = t.name.split(" - ")[0].strip()
        if short:
            table[short] = (t.code, t.name.strip())
    with _lock:
        _case_types[task.complex_code] = table
    return table


def _norm(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _parties_match(order_data: dict, case: dict) -> bool:
    """Same containment test the website lookup uses on its candidates."""
    op, orr = _norm(order_data.get("petitioner")), _norm(order_data.get("respondent"))
    cp, cr = _norm(case.get("pet_name")), _norm(case.get("res_name"))
    if not (op and cp) or not (op in cp or cp in op):
        return False
    if orr and cr:
        return orr in cr or cr in orr
    return not orr


def _dmy(value) -> Optional[str]:
    """'2009-04-30' -> '30-04-2009' (the website's format for these fields)."""
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").strftime("%d-%m-%Y")
    except (ValueError, TypeError):
        return None


def _long(value) -> Optional[str]:
    """'2021-02-24' -> '24th February 2021' (the website's hearing-date format)."""
    try:
        d = datetime.strptime(str(value)[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    day = d.day
    suffix = "th" if 11 <= day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day:02d}{suffix} {d.strftime('%B %Y')}"


def _same_day(a, b) -> bool:
    try:
        return datetime.strptime(str(a).strip(), "%d-%m-%Y") == datetime.strptime(str(b).strip(), "%d-%m-%Y")
    except (ValueError, TypeError):
        return False


def _order_no(hist: dict, order_date: str) -> Optional[int]:
    """The order number of the ONE order the app lists on this date, else None.
    Final orders carry the date as order_date1f, interim ones as order_date1."""
    hits = set()
    for key in ("finalOrder", "interimOrder"):
        orders = hist.get(key)
        if not isinstance(orders, list):
            continue
        for o in orders:
            if not isinstance(o, dict):
                continue
            when = o.get("order_date1f") or o.get("order_date1") or o.get("order_date")
            if _same_day(when, order_date) and str(o.get("order_id") or "").isdigit():
                hits.add(int(o["order_id"]))
    return hits.pop() if len(hits) == 1 else None


def _details(hist: dict, full_type_name: Optional[str], order_no: int, pdf_code: str) -> dict:
    """The website lookup's details dict, filled from the app's case history."""
    decided = str(hist.get("date_of_decision") or "")
    disposed = bool(decided) and not decided.startswith("0000")

    def with_adv(name, adv):
        name, adv = str(name or "").strip(), str(adv or "").strip()
        if not name:
            return None
        return [f"{name} -- Advocate: {adv}" if adv else name]

    acts = [
        {"act": str(a.get("actCodeName") or "").strip(), "section": str(a.get("actSectionName") or "").strip()}
        for a in (hist.get("act") or []) if isinstance(a, dict) and a.get("actCodeName")
    ]
    history = []
    for h in hist.get("historyOfCaseHearing") or []:
        if not isinstance(h, dict):
            continue
        nxt = str(h.get("nextdate") or "")
        history.append({
            "judge": h.get("judge_name"),
            "business_date": h.get("todays_date"),
            "hearing_date": f"{nxt[6:8]}-{nxt[4:6]}-{nxt[:4]}" if len(nxt) == 8 else None,
            "purpose": h.get("purpose"),
        })
    fil_no, fil_year = hist.get("fil_no"), hist.get("fil_year")
    reg_no, reg_year = hist.get("reg_no"), hist.get("reg_year")
    court_no, judge = hist.get("court_no"), str(hist.get("desgname") or "").strip()

    details = {
        "cnr": hist.get("cino"),
        "case_type_full": full_type_name or hist.get("type_name"),
        "filing_number": f"{fil_no}/{fil_year}" if fil_no and fil_year else None,
        "filing_date": _dmy(hist.get("date_of_filing")),
        "registration_number": f"{reg_no}/{reg_year}" if reg_no and reg_year else None,
        "registration_date": _dmy(hist.get("dt_regis")),
        "first_hearing_date": _long(hist.get("date_first_list")),
        "next_hearing_date": None if disposed else _long(hist.get("date_next_list")),
        "case_status": "Case disposed" if disposed else None,
        "case_stage": None if disposed else (str(hist.get("purpose_name") or "").strip() or None),
        "court_number_and_judge": f"{court_no}-{judge}" if court_no and judge else (judge or None),
        "petitioners_with_advocates": with_adv(hist.get("pet_name"), hist.get("pet_adv")),
        "respondents_with_advocates": with_adv(hist.get("res_name"), hist.get("res_adv")),
        # Advocate names on their own -> own database columns (jb_db).
        "petitioner_advocate": str(hist.get("pet_adv") or "").strip() or None,
        "respondent_advocate": str(hist.get("res_adv") or "").strip() or None,
        "acts": acts or None,
        "case_history": history or None,
        "disposal_nature": (str(hist.get("disp_name") or "").strip() or None) if disposed else None,
        "lookup_source": "mobile_app",
    }
    details = {k: v for k, v in details.items() if v is not None}
    details["_order_no_by_code"] = {pdf_code: order_no}
    return details


def _lookup(task, order_data: dict, pdf_code: Optional[str]) -> Optional[dict]:
    parts = str(order_data.get("case_number") or "").split("/")
    if len(parts) < 3 or not pdf_code:
        return None
    ctype, number, year = parts[0].strip(), parts[1].strip(), parts[2].strip()
    if not (number.isdigit() and len(number) <= 7 and year.isdigit()):
        return None

    # JurisBound change: "STC.NI" in the order list is "STC-NI" in the court's
    # list; see match_case_type. Not found -> the website lookup is tried next
    # and logs it.
    types = _types_for(task)
    matched = match_case_type(ctype, types)
    code = types.get(matched) if matched else None
    if not code:
        return None
    reply = _request("caseNumberSearch.php", {
        "case_type": str(code[0]), "case_number": number, "year": year,
        "bilingual_flag": "0", "state_code": str(task.state_code),
        "dist_code": str(task.district_code), "court_code_arr": task.court_numbers,
    })
    if not isinstance(reply, dict):
        raise _AppUnavailable("unexpected case search reply")
    candidates = [
        (str(est.get("court_code")), case)
        for est in reply.values() if isinstance(est, dict)
        for case in (est.get("caseNos") or []) if isinstance(case, dict) and case.get("cino")
    ]
    if len(candidates) > 1:
        candidates = [c for c in candidates if _parties_match(order_data, c[1])]
    if len(candidates) != 1:
        return None
    court_code, case = candidates[0]

    reply = _request("caseHistoryWebService.php", {
        "state_code": str(task.state_code), "dist_code": str(task.district_code),
        "court_code": court_code, "court_code_arr": task.court_numbers,
        "cino": case["cino"], "language_flag": "english", "bilingual_flag": "0",
    })
    hist = reply.get("history") if isinstance(reply, dict) else None
    if not isinstance(hist, dict):
        raise _AppUnavailable("no case history in the app's reply")
    if hist.get("cino") != case["cino"]:
        return None
    order_no = _order_no(hist, order_data.get("order_date"))
    if order_no is None:
        return None
    return _details(hist, code[1], order_no, pdf_code)


def lookup(task, order_data: dict, pdf_code: Optional[str]) -> Optional[dict]:
    """Case details from the app, or None -- the caller then uses the website.
    Never raises."""
    global _failures, _disabled
    if _disabled:
        return None
    try:
        details = _lookup(task, order_data, pdf_code)
        with _lock:
            _failures = 0
            _stats["app_ok" if details else "fallback"] += 1
        if not details:
            logger.debug(f"App lookup not sure for {order_data.get('case_number')} -- using the website")
        return details
    except Exception as e:  # _AppUnavailable or anything unexpected
        with _lock:
            _failures += 1
            _stats["fallback"] += 1
            if _failures >= APP_MAX_FAILURES and not _disabled:
                _disabled = True
                logger.warning(
                    f"App lookup failed {_failures} times in a row ({e}) -- "
                    f"using the website lookup for the rest of this run"
                )
        logger.debug(f"App lookup failed for {order_data.get('case_number')}: {e}")
        _local.client = None   # start a fresh app session next time
        return None


def stats() -> dict:
    with _lock:
        return dict(_stats, disabled=_disabled)


def log_compare(task, order_data: dict, pdf_code: Optional[str], web: dict, app: Optional[dict]) -> None:
    """compare mode: one JSON line per order -- does the app agree with the
    website on the fields we store?"""
    web = web or {}
    fields = ("cnr", "case_type_full", "filing_number", "filing_date",
              "registration_number", "registration_date", "first_hearing_date",
              "next_hearing_date", "case_status", "court_number_and_judge")
    row = {
        "complex": task.complex_code,
        "case_number": order_data.get("case_number"),
        "order_date": order_data.get("order_date"),
        "web_order_no": (web.get("_order_no_by_code") or {}).get(pdf_code),
        "app_order_no": ((app or {}).get("_order_no_by_code") or {}).get(pdf_code),
        "app_answered": bool(app),
        "differences": {
            f: [web.get(f), app.get(f)] for f in fields
            if app and web.get(f) != app.get(f)
        },
        "web_acts": web.get("acts"),
        "app_acts": (app or {}).get("acts"),
    }
    row["same_identity"] = bool(app) and web.get("cnr") == app.get("cnr") and row["web_order_no"] == row["app_order_no"]
    with _lock:
        with open(COMPARE_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
