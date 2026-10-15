"""
JurisBound District Court scraper (eCourts website route)
Scrapes court orders from services.ecourts.gov.in

Usage:
    # Download locally for a date range
    python download.py --start_date 2025-01-01 --end_date 2025-01-03

    # Download for specific state/district/complex
    python download.py --state_code 24 --district_code 10 --complex_code 2400101 --start_date 2025-01-01 --end_date 2025-01-03

    # S3 sync mode (incremental)
    python download.py --sync-s3

    # S3 fill mode (historical backfill)
    python download.py --sync-s3-fill --timeout-hours 5.5
"""

import argparse
import concurrent.futures
import json
import logging
import os
import random
import re
import sys
import threading
import time
import traceback
import uuid
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Generator, List, Optional

import colorlog
import requests
import urllib3
from bs4 import BeautifulSoup
from PIL import Image
from requests.exceptions import ConnectionError, Timeout, ChunkedEncodingError
from tqdm import tqdm

# Add src and repo root to path for imports
sys.path.insert(0, str(Path(__file__).parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent))

# JurisBound change: imported ahead of archive_manager so .env (repo root) is loaded before
# any boto3 client is constructed from the environment.
import jb_db
import mobile_lookup
from archive_manager import S3ArchiveManager
from src.captcha_solver.main import get_text
from src.utils.court_utils import CourtComplex, load_courts_csv, match_case_type
from gs import check_ghostscript_available, compress_pdf_if_enabled

# Configure logging
root_logger = logging.getLogger()
root_logger.setLevel("INFO")

for handler in root_logger.handlers[:]:
    root_logger.removeHandler(handler)

console_handler = colorlog.StreamHandler()
console_handler.setFormatter(
    colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s - %(levelname)s - %(message)s",
        log_colors={
            "DEBUG": "cyan",
            "INFO": "green",
            "WARNING": "yellow",
            "ERROR": "red",
            "CRITICAL": "red,bg_white",
        },
    )
)
root_logger.addHandler(console_handler)

logger = logging.getLogger(__name__)

warnings.filterwarnings("ignore", message=".*pin_memory.*not supported on MPS.*")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Check if Ghostscript is available for PDF compression
COMPRESSION_AVAILABLE = check_ghostscript_available()
if not COMPRESSION_AVAILABLE:
    logger.warning("PDF compression not available (Ghostscript not found)")

# Configuration
BASE_URL = "https://services.ecourts.gov.in/ecourtindia_v6/"

# JurisBound change: seconds to wait before each case and before each court/date
# search. Measured 2026-09-24: with no pause the scraper made ~2 requests/s
# (5 per case, back to back) and the portal blocked the IP after ~20 cases,
# with zero CAPTCHA failures first -- speed alone triggered it. High Court,
# never blocked, runs at ~0.5 requests/s. 5 s brings district to roughly a
# third of the pace that was blocked. Override with DC_CASE_PAUSE=<seconds>;
# 0 turns it off.
CASE_PAUSE_SECONDS = float(os.environ.get("DC_CASE_PAUSE", "5"))


def _polite_pause() -> None:
    """Wait CASE_PAUSE_SECONDS plus up to 50% random extra, so the gaps are not
    a perfectly regular beat."""
    if CASE_PAUSE_SECONDS > 0:
        time.sleep(random.uniform(CASE_PAUSE_SECONDS, CASE_PAUSE_SECONDS * 1.5))


class PortalBlocked(Exception):
    """The portal answered 405 "Security Page" -- it has blocked this IP."""


# JurisBound change: set on the first 405 "Security Page". Original treated the block
# like any other failure and moved on to the next case, so a blocked run kept
# knocking for hours (measured 2026-09-24: 4.5+ min of requests after the block,
# every case lost). The block lasts minutes, not seconds, so once it is seen no
# further request is sent in this run: _fetch_with_retry refuses, every task
# still queued returns without being marked done, and main() exits with code 2.
_BLOCKED = threading.Event()

# JurisBound change: optional run time limit (--timeout-hours), for hosts that kill
# a job at a fixed time (GitHub Actions: 6 h). When it passes, no new day or
# order is started; the work in hand finishes and the run ends normally, so
# the register and the "done up to" markers are still saved. Days not reached
# are simply not marked done and the next run picks them up. None = no limit
# (the behaviour before this change).
#
# Kept in the environment, not a module global: in --sync-s3 mode sync_s3.py
# imports this file a second time as the module "download", and that copy is
# the one that runs the tasks -- a global set in __main__ would not reach it.
_DEADLINE_ENV = "JB_DC_DEADLINE"


def set_time_limit(hours: Optional[float]) -> None:
    if hours:
        os.environ[_DEADLINE_ENV] = str(time.time() + hours * 3600)
        logger.info(f"Time limit: {hours} h -- no new work is started after that")
    else:
        os.environ.pop(_DEADLINE_ENV, None)


def time_up() -> bool:
    raw = os.environ.get(_DEADLINE_ENV)
    try:
        return raw is not None and time.time() >= float(raw)
    except ValueError:
        return False


# JurisBound change: bucket read from the environment (.env (repo root)), not original's
# hardcoded test bucket. jb_db is imported above, so .env is already loaded.
S3_BUCKET = os.environ.get("SUPABASE_S3_BUCKET", "court-documents")
S3_PREFIX = ""
LOCAL_DIR = Path("./local_dc_judgments_data")
PACKAGES_DIR = Path("./packages")
IST = timezone(timedelta(hours=5, minutes=30))
START_DATE = "1950-01-01"
COMPLETED_TASKS_FILE = Path("./dc_completed_tasks.json")

# JurisBound change: orders the court's own case search answers "Record not found"
# for (e.g. STC.NI/150/2022 in the Medchal complex, 2026-09-29: listed on the
# date search, unknown to the case search). Such an order is not saved (no real
# CNR) and used to hold its day back forever, so the complex's scraped-through
# date never moved and every daily run re-checked a growing window. Now each
# run that gets "Record not found" for it adds a strike to a list kept next to
# the marker (<complex>/not_found.json); from NOT_FOUND_STRIKES runs on, the
# order stays on the list, still unsaved, but no longer holds the day back.
# Every other lookup failure (CAPTCHA, network, busy site, block) still does.
NOT_FOUND_STRIKES = int(os.environ.get("DC_NOT_FOUND_STRIKES", "3"))
_NOT_FOUND_NAME = "not_found.json"
_RUN_ID = datetime.now().isoformat(timespec="seconds")
_not_found_lock = threading.Lock()


def record_not_found(task, pdf_code: str, order_data: dict) -> int:
    """Add this run's strike for an order the case search could not find and
    return its strike count (one strike per run, however often it is seen).
    Returns 0 if the list cannot be read or written, so the caller falls back
    to treating the order as an ordinary failure."""
    from archive_manager import _court_names, _make_s3_client

    try:
        state, district, complex_name = _court_names(
            task.state_code, task.district_code, task.complex_code
        )
        key = f"courts/district/{state}/{district}/{complex_name}/{_NOT_FOUND_NAME}"
        with _not_found_lock:
            s3 = _make_s3_client()
            try:
                data = json.loads(
                    s3.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()
                )
            except Exception as e:
                # First strike for this complex: no list yet. Supabase's 404
                # carries no message text, so match on the response code.
                resp = getattr(e, "response", None) or {}
                code = resp.get("Error", {}).get("Code")
                status = resp.get("ResponseMetadata", {}).get("HTTPStatusCode")
                if status != 404 and code != "NoSuchKey":
                    raise
                data = {"orders": {}}
            entry = data["orders"].setdefault(
                pdf_code,
                {
                    "case_number": order_data.get("case_number"),
                    "order_date": order_data.get("order_date"),
                    "first_seen": _RUN_ID,
                    "strikes": 0,
                    "last_run": None,
                },
            )
            if entry["last_run"] != _RUN_ID:
                entry["strikes"] += 1
                entry["last_run"] = _RUN_ID
                s3.put_object(
                    Bucket=S3_BUCKET,
                    Key=key,
                    Body=json.dumps(data, indent=2).encode("utf-8"),
                    ContentType="application/json",
                )
            return entry["strikes"]
    except Exception as e:
        logger.warning(f"Could not update the not-found list: {e}")
        return 0

# Directories for captcha handling
captcha_tmp_dir = Path("./captcha-tmp")
captcha_failures_dir = Path("./captcha-failures")
captcha_tmp_dir.mkdir(parents=True, exist_ok=True)
captcha_failures_dir.mkdir(parents=True, exist_ok=True)

# Thread lock for completed tasks file
completed_tasks_lock = threading.Lock()

# Request headers
HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": "https://services.ecourts.gov.in",
    "Referer": "https://services.ecourts.gov.in/ecourtindia_v6/",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "X-Requested-With": "XMLHttpRequest",
}


def get_task_key(task) -> str:
    """Generate a unique key for a task (court + date combination)"""
    return f"{task.state_code}_{task.district_code}_{task.complex_code}_{task.from_date}_{task.to_date}"


def load_completed_tasks() -> set:
    """Load completed tasks from file"""
    if not COMPLETED_TASKS_FILE.exists():
        return set()
    try:
        with open(COMPLETED_TASKS_FILE, "r") as f:
            data = json.load(f)
            return set(data.get("completed", []))
    except (json.JSONDecodeError, IOError):
        return set()


def save_completed_task(task_key: str):
    """Save a completed task to file (thread-safe)"""
    with completed_tasks_lock:
        completed = load_completed_tasks()
        completed.add(task_key)
        with open(COMPLETED_TASKS_FILE, "w") as f:
            json.dump({"completed": list(completed)}, f)


def is_task_completed(task) -> bool:
    """Check if a task has already been completed"""
    # JurisBound change: recheck_missing.py must search a day again even if this
    # machine finished it before (the completed-tasks file is local to the
    # machine; a GitHub runner starts without it). It sets DC_IGNORE_COMPLETED=1.
    if os.environ.get("DC_IGNORE_COMPLETED") == "1":
        return False
    task_key = get_task_key(task)
    completed = load_completed_tasks()
    return task_key in completed


@dataclass
class DistrictCourtTask:
    """A task representing a date range to process for a specific court complex"""

    id: str
    state_code: str
    state_name: str
    district_code: str
    district_name: str
    complex_code: str
    complex_name: str
    court_numbers: str
    from_date: str  # DD-MM-YYYY format
    to_date: str  # DD-MM-YYYY format
    order_type: str = "both"  # "interim", "finalorder", or "both"

    def __str__(self):
        return f"Task({self.state_name}/{self.district_name}/{self.complex_name}, {self.from_date} to {self.to_date})"


def format_date_for_api(date_str: str) -> str:
    """Convert YYYY-MM-DD to DD-MM-YYYY format"""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return dt.strftime("%d-%m-%Y")


def parse_date_from_api(date_str: str) -> datetime:
    """Convert DD-MM-YYYY to datetime"""
    return datetime.strptime(date_str, "%d-%m-%Y")


def get_date_ranges(
    start_date: str, end_date: str, day_step: int = 1
) -> Generator[tuple[str, str], None, None]:
    """Generate date ranges in YYYY-MM-DD format"""
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    # Cap at today
    today = datetime.now().date()
    if end_dt.date() > today:
        end_dt = datetime.combine(today, datetime.min.time())

    current = start_dt
    while current <= end_dt:
        range_end = min(current + timedelta(days=day_step - 1), end_dt)
        yield (current.strftime("%Y-%m-%d"), range_end.strftime("%Y-%m-%d"))
        current = range_end + timedelta(days=1)


def generate_tasks(
    courts: List[CourtComplex],
    start_date: str,
    end_date: str,
    day_step: int = 1,
) -> Generator[DistrictCourtTask, None, None]:
    """Generate tasks for all courts and date ranges"""
    for from_date, to_date in get_date_ranges(start_date, end_date, day_step):
        for court in courts:
            yield DistrictCourtTask(
                id=str(uuid.uuid4()),
                state_code=court.state_code,
                state_name=court.state_name,
                district_code=court.district_code,
                district_name=court.district_name,
                complex_code=court.complex_code,
                complex_name=court.complex_name,
                court_numbers=court.court_numbers,
                from_date=format_date_for_api(from_date),
                to_date=format_date_for_api(to_date),
                order_type="both",  # "interim", "finalorder", or "both"
            )


class Downloader:
    """Downloads court orders from eCourts website"""

    def __init__(
        self,
        task: DistrictCourtTask,
        archive_manager: S3ArchiveManager,
        compress_pdfs: bool = True,
        fetch_case_details: bool = True,
    ):
        self.task = task
        self.archive_manager = archive_manager
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.app_token = None
        self.session_cookie = None
        # JurisBound change: the site now rejects every ajax POST that does not carry
        # its rotating secret. It is sent as TWO headers -- "delimeter" plus a
        # second header whose NAME also rotates. Both are read out of
        # js/components.js in init_session(); see _load_delimeter().
        self.delim_value = None
        self.delim_name = None
        # PDF compression (enabled by default if Ghostscript is available)
        self.compress_pdfs = compress_pdfs and COMPRESSION_AVAILABLE
        # Fetch detailed case information (CNR, filing date, hearing dates, etc.)
        self.fetch_case_details_enabled = fetch_case_details
        # JurisBound change: orders in this task whose PDF could not be saved. A task
        # with any is not marked done, so the next run tries the day again.
        self.failed_orders = 0
        # JurisBound change: set by search_case_status when the court answers
        # "Record not found"; reset before each order's lookup.
        self.lookup_not_found = False

    def _extract_app_token(self, html: str) -> Optional[str]:
        """Extract app_token from HTML content"""
        soup = BeautifulSoup(html, "lxml")
        token_input = soup.find("input", {"name": "app_token"})
        if token_input:
            return token_input.get("value", "")

        pattern = r"app_token['\"]?\s*[:=]\s*['\"]([^'\"]+)['\"]"
        match = re.search(pattern, html)
        if match:
            return match.group(1)

        pattern2 = r"app_token=([^&'\"]+)"
        match = re.search(pattern2, html)
        if match:
            return match.group(1)

        return None

    def _update_token(self, response_json: dict):
        """Update app_token from API response"""
        if "app_token" in response_json:
            self.app_token = response_json["app_token"]
            logger.debug(f"Updated app_token: {self.app_token[:20]}...")

    def init_session(self):
        """Initialize session and get app_token"""
        logger.debug(f"Initializing session for task: {self.task}")

        # Add small random delay to avoid rate limiting
        time.sleep(random.uniform(0.5, 1.5))

        # Get the court orders page with retry
        url = f"{BASE_URL}?p=courtorder/index"
        # JurisBound change: original waited 30 s here on a 405 and tried once more.
        # The block outlasts 30 s, and _fetch_with_retry now stops the run on
        # the first 405 anyway (see _BLOCKED).
        response = self._fetch_with_retry("GET", url, timeout=30, verify=False)

        response.raise_for_status()

        # Extract app_token
        self.app_token = self._extract_app_token(response.text)
        if not self.app_token:
            token_match = re.search(r"app_token=([^&'\"]+)", response.url)
            if token_match:
                self.app_token = token_match.group(1)

        if not self.app_token:
            self.app_token = ""
            logger.debug("app_token empty in page HTML, proceeding with session cookies only")

        # Get session cookies
        self.session_cookie = response.cookies.get("SERVICES_SESSID")
        if not self.session_cookie:
            self.session_cookie = response.cookies.get("PHPSESSID")

        logger.debug(f"Got app_token: {self.app_token[:20]}...")

        # JurisBound change: read the rotating secret from the page's own JS.
        self._load_delimeter(response.text)

    def _load_delimeter(self, page_html: str):
        """Read the rotating header pair out of js/components.js.

        The site's own ajaxCall() does:

            var delimeter = "<value>";
            headers: { "delimeter": delimeter, "<name>": delimeter }

        BOTH the value and that second header NAME change on every fetch, and the
        server rejects the request unless they arrive as a matched pair from the
        SAME file. Reading a fresh value but pairing it with a name remembered
        from an earlier read fails with a generic "Invalid Request" -- which is
        indistinguishable from rate limiting, so it is easy to misdiagnose.
        """
        try:
            m = re.search(r'([^"\']*js/components\.js\?v=\d+)', page_html)
            if not m:
                logger.warning("components.js not referenced in page; delimeter unavailable")
                return
            src = m.group(1)
            url = src if src.startswith("http") else BASE_URL.rstrip("/") + "/" + src.split("ecourtindia_v6/")[-1]
            js = self._fetch_with_retry(
                "GET", url,
                headers={**HEADERS, "Accept": "*/*", "Referer": f"{BASE_URL}?p=courtorder/index"},
                timeout=30, verify=False,
            ).text
            val = re.search(r'function ajaxCall[\s\S]{0,300}?var\s+delimeter\s*=\s*"([^"]+)"', js)
            name = re.search(
                r'headers:\s*\{\s*"delimeter"\s*:\s*delimeter\s*,\s*"([^"]+)"\s*:\s*delimeter', js
            )
            if val and name:
                self.delim_value, self.delim_name = val.group(1), name.group(1)
                logger.debug(f"delimeter pair: {self.delim_name}={self.delim_value}")
            else:
                logger.warning("could not parse delimeter pair out of components.js")
        except Exception as e:
            logger.warning(f"delimeter load failed: {e}")

    def set_court_data(self):
        """Set the court complex in the session"""
        url = f"{BASE_URL}?p=casestatus/set_data"

        # Format: complex_code@court_numbers@flag
        complex_code_full = f"{self.task.complex_code}@{self.task.court_numbers}@N"

        data = {
            "complex_code": complex_code_full,
            "selected_state_code": self.task.state_code,
            "selected_dist_code": self.task.district_code,
            "selected_est_code": "null",
            "ajax_req": "true",
            "app_token": self.app_token,
        }

        response = self._fetch_with_retry(
            "POST", url, data=data, timeout=30, verify=False
        )
        response.raise_for_status()

        result = response.json()
        self._update_token(result)

        if result.get("status") != 1:
            logger.warning(f"Failed to set court data: {result}")
            return False

        return True

    # JurisBound change: how many times a search is sent with a fresh CAPTCHA
    # before giving up on it. Original had no limit -- a site that rejects
    # every CAPTCHA kept one case going ~1,000 times (~8 h, measured
    # 2026-09-28). 5 is the High Court scraper's limit for the same situation.
    # A given-up case lookup counts as a failed order and a given-up date
    # search as a failed search, so the day is retried on the next run.
    CAPTCHA_MAX_TRIES = 5

    @staticmethod
    def _retry_backoff(attempt: int) -> None:
        """Wait before a retry, waiting longer each time.

        JurisBound change: this is the same exponential-backoff-with-jitter formula
        `_fetch_with_retry` below already uses -- 1s, 2s, 4s, 8s ... capped at
        30s, plus a random fraction so parallel workers do not retry in step.
        That helper only catches *network* errors, so CAPTCHA failures never
        reached it and retried with no pause at all: ~4 requests a second,
        which is what got the IP blocked.

        This changes only the *timing*. The same retries still happen, the same
        number of times, in the same order.
        """
        delay = min(1.0 * (2**attempt) + random.uniform(0, 1), 30.0)
        logger.debug(f"CAPTCHA retry {attempt + 1}: waiting {delay:.1f}s")
        time.sleep(delay)

    def solve_captcha(self, retries: int = 0) -> str:
        """Solve CAPTCHA using ONNX model"""
        if retries > 10:
            raise ValueError("Failed to solve CAPTCHA after 10 attempts")

        # Get captcha image with retry
        captcha_url = (
            f"{BASE_URL}vendor/securimage/securimage_show.php?{uuid.uuid4().hex}"
        )
        response = self._fetch_with_retry("GET", captcha_url, timeout=30, verify=False)

        # Save and process
        unique_id = uuid.uuid4().hex[:8]
        captcha_path = captcha_tmp_dir / f"captcha_dc_{unique_id}.png"
        with open(captcha_path, "wb") as f:
            f.write(response.content)

        try:
            img = Image.open(captcha_path)
            captcha_text = get_text(img).strip()

            # eCourts captcha is 6 characters
            if len(captcha_text) != 6:
                logger.debug(f"Invalid captcha length: {captcha_text}")
                captcha_path.unlink()
                self._retry_backoff(retries)
                return self.solve_captcha(retries + 1)

            captcha_path.unlink()
            return captcha_text

        except Exception as e:
            logger.error(f"Error solving captcha: {e}")
            # Move to failures dir for debugging (if file exists)
            if captcha_path.exists():
                new_path = (
                    captcha_failures_dir / f"{uuid.uuid4().hex[:8]}_{captcha_path.name}"
                )
                try:
                    captcha_path.rename(new_path)
                except Exception:
                    pass  # Ignore if file was already moved/deleted
            self._retry_backoff(retries)
            return self.solve_captcha(retries + 1)

    def search_orders(self, _attempt: int = 0) -> Optional[str]:
        """Search for orders by date range

        JurisBound change: `_attempt` only sizes the wait before a CAPTCHA retry.
        It defaults to 0, so every existing caller behaves exactly as before.
        """
        url = f"{BASE_URL}?p=courtorder/submitOrderDate"

        # Solve captcha
        captcha_code = self.solve_captcha()

        data = {
            "state_code": self.task.state_code,
            "dist_code": self.task.district_code,
            "court_complex": self.task.complex_code,
            "court_complex_arr": self.task.court_numbers,
            "est_code": "",
            "from_date": self.task.from_date,
            "to_date": self.task.to_date,
            "fradorderdt": self.task.order_type,  # "interim", "finalorder", or "both"
            "orderflagvaldate": self.task.order_type,  # "interim", "finalorder", or "both"
            "order_date_captcha_code": captcha_code,  # Correct field name for captcha
            "ajax_req": "true",
            "app_token": self.app_token,
        }

        try:
            response = self._fetch_with_retry(
                "POST", url, data=data, timeout=60, verify=False
            )
            response.raise_for_status()

            # Check if response is JSON with token update
            try:
                result = response.json()
                self._update_token(result)

                # Check for captcha error
                if result.get("errormsg"):
                    logger.warning(f"Search error: {result.get('errormsg')}")
                    if "captcha" in result.get("errormsg", "").lower():
                        if _attempt + 1 >= self.CAPTCHA_MAX_TRIES:
                            logger.warning(
                                f"Date search: CAPTCHA rejected {self.CAPTCHA_MAX_TRIES} "
                                f"times, giving up for now: {self.task}"
                            )
                            return None
                        # JurisBound change: this retried instantly, and each retry
                        # can fetch up to 10 more CAPTCHA images. Same retry,
                        # now spaced out.
                        self._retry_backoff(_attempt)
                        return self.search_orders(_attempt + 1)
                    return None

                # Check status
                if result.get("status") != 1:
                    logger.debug(f"Search returned non-success status: {result}")
                    return None

                # Check for HTML content in response (court_dt_data field)
                if "court_dt_data" in result:
                    return result["court_dt_data"]
                if "html" in result:
                    return result["html"]

            except json.JSONDecodeError:
                # Response is HTML
                return response.text

            return response.text

        except (
            ConnectionError,
            Timeout,
            ChunkedEncodingError,
            urllib3.exceptions.ProtocolError,
        ) as e:
            logger.error(f"Network error searching orders (after retries): {e}")
            return None
        except Exception as e:
            logger.error(f"Error searching orders: {e}")
            return None

    def parse_order_results(self, html: str) -> List[dict]:
        """Parse order search results from HTML"""
        soup = BeautifulSoup(html, "lxml")
        results = []

        # Look for the results table
        table = soup.find("table", {"id": "caseList"})
        if not table:
            tables = soup.find_all("table")
            for t in tables:
                rows = t.find_all("tr")
                if len(rows) > 1:  # Has data rows
                    table = t
                    break

        if not table:
            return results

        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all("td")
            if not cells:
                continue

            order_data = {
                "raw_html": str(row),
            }

            # Column name mapping for the order results table
            # The table structure is: Serial | Case Number | Parties | Order Date | Order Link
            column_names = [
                "serial_number",
                "case_number",
                "parties",
                "order_date",
                "document_type",
            ]

            # Extract data from cells with proper field names
            for idx, cell in enumerate(cells):
                text = cell.get_text(strip=True)
                if text:
                    # Use proper field name if available, otherwise fall back to indexed name
                    if idx < len(column_names):
                        order_data[column_names[idx]] = text
                    else:
                        order_data[f"column_{idx}"] = text

                # Look for links/buttons with PDF info
                link = cell.find("a")
                if link:
                    href = link.get("href", "")
                    onclick = link.get("onclick", "")
                    if href:
                        order_data["pdf_href"] = href
                    if onclick:
                        order_data["onclick"] = onclick

                button = cell.find("button")
                if button:
                    onclick = button.get("onclick", "")
                    if onclick:
                        order_data["onclick"] = onclick

            # Parse parties into petitioner and respondent if possible
            if order_data.get("parties"):
                parties = order_data["parties"]
                # Try different separator patterns: "Vs", "VS", "vs", "V/s", "v/s"
                for separator in ["Vs", "VS", "vs", "V/s", "v/s", " v ", " V "]:
                    if separator in parties:
                        parts = parties.split(separator, 1)
                        if len(parts) == 2:
                            order_data["petitioner"] = parts[0].strip()
                            order_data["respondent"] = parts[1].strip()
                        break

            # Try to extract CNR (16-char format)
            cnr_match = re.search(r"\b([A-Z]{4}\d{12})\b", str(row))
            if cnr_match:
                order_data["cnr"] = cnr_match.group(1)
            # If no CNR, try to use case number as unique identifier
            elif order_data.get("case_number"):
                # Sanitize case number for filename: MVOP/63/2021 -> MVOP_63_2021
                case_no = order_data["case_number"]
                case_id = re.sub(r"[^\w\d.-]", "_", case_no)
                order_data["cnr"] = case_id

            if order_data.get("onclick") or order_data.get("pdf_href"):
                results.append(order_data)

        return results

    def get_case_type_codes(self) -> Dict[str, str]:
        """
        Fetch case type code mapping from fillCaseType API.

        Returns:
            Dictionary mapping short code (e.g., "OS") to internal code (e.g., "17^43")
        """
        url = f"{BASE_URL}?p=casestatus/fillCaseType"

        data = {
            "state_code": self.task.state_code,
            "dist_code": self.task.district_code,
            "court_complex_code": self.task.complex_code,
            "est_code": "",
            "search_type": "c_no",
            "ajax_req": "true",
            "app_token": self.app_token,
        }

        try:
            response = self._fetch_with_retry(
                "POST", url, data=data, timeout=30, verify=False
            )
            response.raise_for_status()

            result = response.json()
            self._update_token(result)

            case_type_mapping = {}
            casetype_html = result.get("casetype_list", "")

            if casetype_html:
                soup = BeautifulSoup(casetype_html, "lxml")
                for option in soup.find_all("option"):
                    value = option.get("value", "").strip()
                    text = option.get_text(strip=True)

                    if value and text:
                        # Extract short code from text like "OS - ORIGINAL SUIT"
                        short_code = (
                            text.split(" - ")[0].strip() if " - " in text else text
                        )
                        case_type_mapping[short_code] = value

            return case_type_mapping

        except Exception as e:
            logger.debug(f"Error fetching case type codes: {e}")
            return {}

    def search_case_status(
        self, case_type_code: str, case_number: str, year: str, _attempt: int = 0
    ) -> List[Dict]:
        """
        Search Case Status by case number to get list of matching cases.

        Args:
            case_type_code: Internal case type code (e.g., "17^43" for OS)
            case_number: Case number (e.g., "32")
            year: Case year (e.g., "2024")
            _attempt: JurisBound change -- only sizes the wait before a CAPTCHA
                retry. Defaults to 0, so existing callers are unchanged.

        Returns:
            List of case dictionaries with viewHistory parameters
        """
        url = f"{BASE_URL}?p=casestatus/submitCaseNo"

        # Solve captcha for this request
        captcha_code = self.solve_captcha()

        # API parameters must match actual form submission
        data = {
            "state_code": self.task.state_code,
            "dist_code": self.task.district_code,
            "court_complex_code": self.task.complex_code,
            "est_code": "",
            "case_type": case_type_code,
            "search_case_no": case_number,
            "case_no": case_number,
            "rgyear": year,
            "case_captcha_code": captcha_code,
            "ajax_req": "true",
            "app_token": self.app_token,
        }

        try:
            response = self._fetch_with_retry(
                "POST", url, data=data, timeout=60, verify=False
            )
            response.raise_for_status()

            try:
                result = response.json()
                self._update_token(result)

                # Check for captcha error
                if result.get("errormsg"):
                    error_msg = result.get("errormsg", "").lower()
                    if "captcha" in error_msg:
                        if _attempt + 1 >= self.CAPTCHA_MAX_TRIES:
                            logger.warning(
                                f"Case lookup: CAPTCHA rejected {self.CAPTCHA_MAX_TRIES} "
                                f"times, giving up for now ({case_number}/{year})"
                            )
                            return []
                        logger.debug("Captcha error in case status search, retrying...")
                        # JurisBound change: this retried instantly -- seen 3 times
                        # in half a second on 2026-09-25. Same backoff as
                        # search_orders; the retries themselves are unchanged.
                        self._retry_backoff(_attempt)
                        return self.search_case_status(
                            case_type_code, case_number, year, _attempt + 1
                        )
                    logger.debug(f"Case status search error: {result.get('errormsg')}")
                    return []

                # Parse case_data HTML to extract viewHistory parameters
                case_data_html = result.get("case_data", "")
                if not case_data_html:
                    return []

                # JurisBound change: remember when the court itself answered
                # "Record not found" (the same id='nodata' block an empty date
                # search returns), as opposed to an error -- see
                # record_not_found().
                if "id='nodata'" in case_data_html or 'id="nodata"' in case_data_html:
                    self.lookup_not_found = True
                    return []

                return self._parse_case_list(case_data_html)

            except (json.JSONDecodeError, ValueError):
                return []

        except Exception as e:
            logger.debug(f"Error in case status search: {e}")
            return []

    def _parse_case_list(self, html: str) -> List[Dict]:
        """
        Parse case list HTML from submitCaseNo to extract viewHistory parameters and party names.

        Extracts from onclick like: viewHistory(201700000322025,'TSRA160001082025',24,'','CScaseNumber',29,9,1290105,'CScaseNumber')
        Also extracts party names from the table row for matching purposes.
        """
        cases = []
        soup = BeautifulSoup(html, "lxml")

        # Find all View links with viewHistory onclick
        for link in soup.find_all("a", onclick=True):
            onclick = link.get("onclick", "")
            if "viewHistory" not in onclick:
                continue

            # Parse viewHistory(case_no, cino, court_code, hideparty, search_flag, state_code, dist_code, complex_code, search_by)
            match = re.search(
                r"viewHistory\s*\(\s*(\d+)\s*,\s*'([^']+)'\s*,\s*(\d+)\s*,\s*'([^']*)'\s*,\s*'([^']+)'\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*'([^']+)'\s*\)",
                onclick,
            )
            if match:
                case_info = {
                    "internal_case_no": match.group(1),
                    "cino": match.group(2),  # CNR number
                    "court_code": match.group(3),
                    "hideparty": match.group(4),
                    "search_flag": match.group(5),
                    "state_code": match.group(6),
                    "dist_code": match.group(7),
                    "court_complex_code": match.group(8),
                    "search_by": match.group(9),
                }

                # Extract party names from the table row
                # Table structure: <tr><td>Sr</td><td>Case</td><td>Parties</td><td><a>View</a></td></tr>
                row = link.find_parent("tr")
                if row:
                    cells = row.find_all("td")
                    if len(cells) >= 3:
                        # Third cell contains "Petitioner<br>Vs</br>Respondent"
                        parties_cell = cells[2]
                        parties_text = parties_cell.get_text(separator=" ").strip()
                        # Parse "Petitioner Vs Respondent" format (Vs may or may not have surrounding spaces)
                        # Pattern handles: "Name Vs Name", "Name VsName", "NameVs Name", "NameVsName"
                        vs_match = re.search(
                            r"(.+?)\s*vs\s*(.+)", parties_text, flags=re.IGNORECASE
                        )
                        if vs_match:
                            case_info["petitioner"] = vs_match.group(1).strip()
                            case_info["respondent"] = vs_match.group(2).strip()
                        case_info["parties"] = parties_text

                cases.append(case_info)

        return cases

    def view_case_history(self, case_info: Dict) -> Optional[str]:
        """
        Get full case details by calling viewHistory API.

        Args:
            case_info: Dictionary with viewHistory parameters from _parse_case_list

        Returns:
            HTML content with full case details
        """
        url = f"{BASE_URL}?p=home/viewHistory"

        data = {
            "court_code": case_info.get("court_code"),
            "state_code": case_info.get("state_code", self.task.state_code),
            "dist_code": case_info.get("dist_code", self.task.district_code),
            "court_complex_code": case_info.get(
                "court_complex_code", self.task.complex_code
            ),
            "case_no": case_info.get("internal_case_no"),
            "cino": case_info.get("cino"),
            "hideparty": case_info.get("hideparty", ""),
            "search_flag": case_info.get("search_flag", "CScaseNumber"),
            "search_by": case_info.get("search_by", "CScaseNumber"),
            "ajax_req": "true",
            "app_token": self.app_token,
        }

        try:
            response = self._fetch_with_retry(
                "POST", url, data=data, timeout=60, verify=False
            )
            response.raise_for_status()

            result = response.json()
            self._update_token(result)

            if result.get("errormsg"):
                logger.debug(f"viewHistory error: {result.get('errormsg')}")
                return None

            return result.get("data_list", "")

        except Exception as e:
            logger.debug(f"Error in viewHistory: {e}")
            return None

    def parse_case_details(self, html: str) -> dict:
        """
        Parse detailed case information from Case Status HTML response.

        Extracts:
        - CNR number
        - Case type (full name)
        - Filing number and date
        - Registration number and date
        - First hearing date
        - Next hearing date
        - Case status
        - Case stage
        - Court number and judge
        - Petitioners with advocates
        - Respondents with advocates
        - Acts and sections
        - Case history

        Args:
            html: HTML content from case status search

        Returns:
            Dictionary with all extracted case details
        """
        soup = BeautifulSoup(html, "lxml")
        details = {}

        # Helper to extract text from table cells
        def get_cell_value(label: str) -> Optional[str]:
            """Find a label in th or td and return the adjacent td value."""
            for cell in soup.find_all(["th", "td"]):
                text = cell.get_text(strip=True)
                if label.lower() in text.lower():
                    # Look for next td sibling (works for both th→td and td→td layouts)
                    next_td = cell.find_next_sibling("td")
                    if next_td:
                        return next_td.get_text(strip=True)
            return None

        # Extract CNR Number (16 character format: XXXX########YYYY)
        cnr_pattern = r"\b([A-Z]{4}\d{12})\b"
        cnr_match = re.search(cnr_pattern, html)
        if cnr_match:
            details["cnr"] = cnr_match.group(1)

        # Extract from Case Details section
        details["case_type_full"] = get_cell_value("Case Type")
        details["filing_number"] = get_cell_value("Filing Number")
        details["filing_date"] = get_cell_value("Filing Date")
        details["registration_number"] = get_cell_value("Registration Number")
        details["registration_date"] = get_cell_value("Registration Date")

        # Extract from Case Status section
        details["first_hearing_date"] = get_cell_value("First Hearing Date")
        details["next_hearing_date"] = get_cell_value("Next Hearing Date")
        details["case_status"] = get_cell_value("Case Status")
        details["case_stage"] = get_cell_value("Stage of Case")
        details["court_number_and_judge"] = get_cell_value("Court Number and Judge")

        # Alternative field names
        if not details.get("case_stage"):
            details["case_stage"] = get_cell_value("Stage")
        if not details.get("court_number_and_judge"):
            details["court_number_and_judge"] = get_cell_value("Court No")

        # Extract Petitioner and Advocate section
        petitioners = []
        pet_section = soup.find(string=re.compile(r"Petitioner.*Advocate", re.I))
        if pet_section:
            parent = pet_section.find_parent("div") or pet_section.find_parent("table")
            if parent:
                for item in parent.find_all(["li", "tr", "p"]):
                    text = item.get_text(strip=True)
                    if text and "petitioner" not in text.lower():
                        petitioners.append(text)
        if petitioners:
            details["petitioners_with_advocates"] = petitioners

        # Extract Respondent and Advocate section
        respondents = []
        resp_section = soup.find(string=re.compile(r"Respondent.*Advocate", re.I))
        if resp_section:
            parent = resp_section.find_parent("div") or resp_section.find_parent(
                "table"
            )
            if parent:
                for item in parent.find_all(["li", "tr", "p"]):
                    text = item.get_text(strip=True)
                    if text and "respondent" not in text.lower():
                        respondents.append(text)
        if respondents:
            details["respondents_with_advocates"] = respondents

        # Extract Acts section
        acts = []
        acts_section = soup.find(string=re.compile(r"Under Act", re.I))
        if acts_section:
            parent = acts_section.find_parent("table")
            if parent:
                rows = parent.find_all("tr")
                for row in rows[1:]:  # Skip header row
                    cells = row.find_all("td")
                    if len(cells) >= 2:
                        act = cells[0].get_text(strip=True)
                        section = (
                            cells[1].get_text(strip=True) if len(cells) > 1 else ""
                        )
                        if act:
                            acts.append({"act": act, "section": section})
        if acts:
            details["acts"] = acts

        # Extract Case History
        history = []
        history_section = soup.find(string=re.compile(r"Case History", re.I))
        if history_section:
            parent = history_section.find_parent("table") or history_section.find_next(
                "table"
            )
            if parent:
                rows = parent.find_all("tr")
                headers = []
                for row in rows:
                    cells = row.find_all(["td", "th"])
                    if not headers:
                        headers = [c.get_text(strip=True) for c in cells]
                        continue
                    if len(cells) >= 2:
                        entry = {}
                        for i, cell in enumerate(cells):
                            if i < len(headers):
                                entry[headers[i]] = cell.get_text(strip=True)
                        if entry:
                            history.append(entry)
        if history:
            details["case_history"] = history

        # Clean up None values
        return {k: v for k, v in details.items() if v is not None}

    def fetch_case_details(self, order_data: dict) -> dict:
        """
        Fetch detailed case information for an order.

        Parses the case_number (e.g., "OS/32/2024") and searches Case Status API.

        Args:
            order_data: Order data dictionary with case_number field

        Returns:
            Dictionary with detailed case information
        """
        case_number_full = order_data.get("case_number", "")
        if not case_number_full:
            return {}

        # Parse case number: "OS/32/2024" -> case_type="OS", case_no="32", year="2024"
        parts = case_number_full.split("/")
        if len(parts) >= 3:
            case_type = parts[0]
            case_no = parts[1]
            year = parts[2]
        elif len(parts) == 2:
            # Could be "OS/32" with year from order_date
            case_type = parts[0]
            case_no = parts[1]
            order_date = order_data.get("order_date", "")
            if order_date:
                try:
                    year = parse_date_from_api(order_date).year
                except:
                    year = datetime.now().year
            else:
                year = datetime.now().year
            year = str(year)
        else:
            logger.debug(f"Could not parse case number: {case_number_full}")
            return {}

        # Get case type code mapping (cached after first call)
        if not hasattr(self, "_case_type_codes"):
            self._case_type_codes = self.get_case_type_codes()

        # Look up internal case type code (e.g., "OS" -> "17^43")
        # JurisBound change: exact name first, then the same name without dots /
        # hyphens / spaces ("STC.NI" -> "STC-NI"); see match_case_type. A case
        # type that is still not found is logged as a warning, so it shows in
        # the run log (it was a debug line, never shown).
        matched = match_case_type(case_type, self._case_type_codes)
        case_type_code = self._case_type_codes.get(matched) if matched else None
        if not case_type_code:
            if not self._case_type_codes:
                logger.warning(
                    f"Case type list of {self.task.complex_name} could not be read "
                    f"-- cannot look up {case_number_full}"
                )
            else:
                logger.warning(
                    f"Case type {case_type!r} is not in the case type list of "
                    f"{self.task.complex_name} -- cannot look up {case_number_full}"
                )
            return {}
        if matched != case_type:
            logger.info(f"Case type {case_type!r} matched {matched!r} for {case_number_full}")

        # Step 1: Search case status to get list of matching cases
        logger.debug(
            f"Fetching case details for {case_type}/{case_no}/{year} (code: {case_type_code})"
        )
        case_list = self.search_case_status(case_type_code, case_no, year)

        if not case_list:
            logger.debug(f"No cases found for {case_number_full}")
            return {}

        # Step 2: Find the matching case from this court complex
        # Filter by court_code that matches the task's court_numbers
        valid_court_codes = [c.strip() for c in self.task.court_numbers.split(",")]
        matching_cases = [
            c for c in case_list if c.get("court_code") in valid_court_codes
        ]

        # Use court-filtered cases if available, otherwise all cases
        candidates = matching_cases if matching_cases else case_list

        # If multiple candidates, narrow down by matching party names
        if len(candidates) > 1:
            order_petitioner = (order_data.get("petitioner") or "").lower().strip()
            order_respondent = (order_data.get("respondent") or "").lower().strip()
            order_parties = (order_data.get("parties") or "").lower().strip()

            def parties_match(case_info: dict) -> bool:
                """Check if case party names match order party names"""
                case_petitioner = (case_info.get("petitioner") or "").lower().strip()
                case_respondent = (case_info.get("respondent") or "").lower().strip()
                case_parties = (case_info.get("parties") or "").lower().strip()

                # Try exact match on petitioner/respondent
                if order_petitioner and case_petitioner:
                    if (
                        order_petitioner in case_petitioner
                        or case_petitioner in order_petitioner
                    ):
                        if order_respondent and case_respondent:
                            if (
                                order_respondent in case_respondent
                                or case_respondent in order_respondent
                            ):
                                return True
                        elif not order_respondent:
                            return True

                # Try matching on combined parties string
                if order_parties and case_parties:
                    # Normalize "vs" variations
                    order_norm = re.sub(
                        r"\s+",
                        " ",
                        order_parties.replace(" vs ", " ").replace(" v/s ", " "),
                    )
                    case_norm = re.sub(
                        r"\s+",
                        " ",
                        case_parties.replace(" vs ", " ").replace(" v/s ", " "),
                    )
                    if order_norm in case_norm or case_norm in order_norm:
                        return True

                return False

            party_matched = [c for c in candidates if parties_match(c)]
            if len(party_matched) == 1:
                candidates = party_matched
                logger.debug(
                    f"Matched case by party names: {candidates[0].get('cino')}"
                )
            elif len(party_matched) > 1:
                logger.debug(
                    f"Multiple cases ({len(party_matched)}) matched by party names, using first"
                )
                candidates = party_matched
            else:
                logger.debug(
                    f"No party name match found among {len(candidates)} candidates"
                )

        # Select the best candidate
        if not candidates:
            logger.debug(f"No matching cases found for {case_number_full}")
            return {}

        case_info = candidates[0]
        if len(candidates) > 1:
            logger.warning(
                f"Multiple cases ({len(candidates)}) for {case_number_full}, "
                f"using first: CNR={case_info.get('cino')}, court={case_info.get('court_code')}"
            )

        # Fetch case history for selected case
        html = self.view_case_history(case_info)
        if not html:
            logger.debug(
                f"No case history found for {case_number_full} (CNR: {case_info.get('cino')})"
            )
            return {}

        # Parse the HTML
        details = self.parse_case_details(html)

        # Add CNR from case_info if not parsed
        if not details.get("cnr") and case_info.get("cino"):
            details["cnr"] = case_info.get("cino")

        # JurisBound change: order number per PDF code, from the full page (the
        # stored copy below is cut at 10,000 characters and can lose the
        # order table on long cases).
        details["_order_no_by_code"] = self._order_numbers(html)

        # Store raw HTML for reference
        details["case_details_html"] = html[:10000]  # Limit size

        return details

    def lookup_case(self, order_data: dict, pdf_code: Optional[str]) -> dict:
        """JurisBound change: the case lookup, by the DC_LOOKUP switch (see
        mobile_lookup.py). "web" (default) is fetch_case_details() exactly as
        before; "mobile" asks the eCourts app first and falls back to the
        website whenever the app is not sure; "compare" does both, keeps the
        website's answer and logs the app's next to it."""
        mode = mobile_lookup.LOOKUP_MODE
        if mode == "mobile":
            details = mobile_lookup.lookup(self.task, order_data, pdf_code)
            if details:
                return details
            return self.fetch_case_details(order_data)
        if mode == "compare":
            details = self.fetch_case_details(order_data)
            if details.get("cnr"):
                app = mobile_lookup.lookup(self.task, order_data, pdf_code)
                mobile_lookup.log_compare(self.task, order_data, pdf_code, details, app)
            return details
        return self.fetch_case_details(order_data)

    # displayPdf('normal_v','case_val','court_code','filename','appFlag')
    _DISPLAY_PDF = re.compile(
        r"displayPdf\s*\(\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*\)"
    )

    @staticmethod
    def _fingerprint(groups) -> Optional[str]:
        """'<court>|<file>' from displayPdf's 3rd and 4th arguments.

        The 4th (the portal's encrypted file name) is stable over time but NOT
        unique on its own: courts inside one complex name files the same way,
        so a case moved between courts has different orders with the same 4th
        argument and a different 3rd (the court). Measured 2026-09-28: 118 such
        codes in Malkajgiri's 2025 orders. Both together identify the order.
        """
        court, file_code = groups[2], groups[3]
        return f"{court}|{file_code}" if court and file_code else None

    @classmethod
    def _pdf_code(cls, order_data: dict) -> Optional[str]:
        """JurisBound change: the order's fingerprint (see _fingerprint) from its
        search line -- stable across sessions and days."""
        m = cls._DISPLAY_PDF.search(order_data.get("onclick") or "")
        return cls._fingerprint(m.groups()) if m else None

    def _already_saved(self, order_data: dict) -> bool:
        """JurisBound change: True if this search line's PDF code is already in the
        court+year PDF index. Costs no request to the portal."""
        pdf_code = self._pdf_code(order_data)
        if not pdf_code or self.archive_manager is None:
            return False
        try:
            year = parse_date_from_api(
                order_data.get("order_date") or self.task.from_date
            ).year
        except (ValueError, TypeError):
            return False
        seen = self.archive_manager.code_exists(
            year,
            self.task.state_code,
            self.task.district_code,
            self.task.complex_code,
            pdf_code,
        )
        if seen:
            logger.debug(
                f"Already saved (PDF code seen): {order_data.get('case_number')} "
                f"{order_data.get('order_date')}"
            )
        return seen

    @classmethod
    def _order_numbers(cls, html: str) -> Dict[str, int]:
        """JurisBound change: {fingerprint: order number} from the case history
        page's order table(s). Each row is: order number | order date | link
        whose displayPdf carries the same arguments as the date-search line
        (verified 2026-09-25 and 2026-09-28; a judgment is listed in the same
        numbered table as the other orders)."""
        out: Dict[str, int] = {}
        soup = BeautifulSoup(html or "", "lxml")
        for table in soup.find_all("table", class_="order_table"):
            for tr in table.find_all("tr"):
                tds = tr.find_all("td")
                if not tds:
                    continue
                num = tds[0].get_text(strip=True).replace("\xa0", "")
                m = cls._DISPLAY_PDF.search(str(tr))
                fp = cls._fingerprint(m.groups()) if m else None
                if num.isdigit() and fp:
                    out.setdefault(fp, int(num))
        return out

    def _fetch_with_retry(
        self, method: str, url: str, max_retries: int = 3, **kwargs
    ) -> requests.Response:
        """Fetch URL with retry logic for network errors"""
        last_exception = None
        # JurisBound change: every ajax POST must carry the rotating header pair, or
        # the server answers "Invalid Request". Injected here so all callers get
        # it without each one having to remember.
        if method != "GET" and self.delim_value and self.delim_name:
            hdrs = dict(kwargs.get("headers") or {})
            hdrs.setdefault("delimeter", self.delim_value)
            hdrs.setdefault(self.delim_name, self.delim_value)
            kwargs["headers"] = hdrs
        for attempt in range(max_retries + 1):
            # JurisBound change: send nothing once the portal has blocked us.
            if _BLOCKED.is_set():
                raise PortalBlocked("portal blocked earlier in this run")
            try:
                if method == "GET":
                    response = self.session.get(url, **kwargs)
                else:
                    response = self.session.post(url, **kwargs)
                if response.status_code == 405:
                    if not _BLOCKED.is_set():
                        logger.error(
                            "Portal returned 405 Security Page (IP blocked) on "
                            f"{url} -- stopping the run, no more requests will be sent"
                        )
                    _BLOCKED.set()
                    raise PortalBlocked(url)
                # JurisBound change: 503 "No available server to handle this
                # request" is the site being busy for a moment, not a block.
                # Original returned it and the caller failed the whole task
                # (seen 3 times on 2026-09-29). Wait 5 s, 10 s, 20 s and try
                # again; if it is still busy, return it as before.
                if response.status_code == 503 and attempt < max_retries:
                    delay = 5.0 * (2**attempt) + random.uniform(0, 1)
                    logger.warning(
                        f"Site busy (503), attempt {attempt + 1}/{max_retries + 1}. "
                        f"Retrying in {delay:.0f}s..."
                    )
                    time.sleep(delay)
                    continue
                return response
            except (
                ConnectionError,
                Timeout,
                ChunkedEncodingError,
                urllib3.exceptions.ProtocolError,
                requests.exceptions.RequestException,
            ) as e:
                last_exception = e
                if attempt < max_retries:
                    delay = min(1.0 * (2**attempt) + random.uniform(0, 1), 30.0)
                    logger.warning(
                        f"Network error (attempt {attempt + 1}/{max_retries + 1}): {e}. "
                        f"Retrying in {delay:.1f}s..."
                    )
                    time.sleep(delay)
                else:
                    raise
        raise last_exception

    def download_pdf(self, order_data: dict) -> Optional[bytes]:
        """Download PDF for an order using displayPdf parameters"""
        onclick = order_data.get("onclick", "")

        # Extract displayPdf parameters: displayPdf('normal_v','case_val','court_code','filename','appFlag')
        pattern = r"displayPdf\s*\(\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*'([^']*)'\s*\)"
        match = re.search(pattern, onclick)

        if not match:
            logger.debug(
                f"Could not extract displayPdf parameters from: {onclick[:100]}"
            )
            return None

        normal_v, case_val, court_code, filename, app_flag = match.groups()

        # Call the display_pdf endpoint to get PDF URL
        url = f"{BASE_URL}?p=home/display_pdf"
        data = {
            "normal_v": normal_v,
            "case_val": case_val,
            "court_code": court_code,
            "filename": filename,
            "appFlag": app_flag,
            "ajax_req": "true",
            "app_token": self.app_token,
        }

        try:
            response = self._fetch_with_retry(
                "POST", url, data=data, timeout=60, verify=False
            )
            response.raise_for_status()

            result = response.json()
            self._update_token(result)

            # Get PDF URL from response (presence of 'order' indicates success)
            pdf_path = result.get("order", "")
            if not pdf_path:
                logger.debug(f"No PDF path in response: {result}")
                return None

            # Make URL absolute
            if not pdf_path.startswith("http"):
                pdf_url = f"{BASE_URL}{pdf_path.lstrip('/')}"
            else:
                pdf_url = pdf_path

            # Download the actual PDF with retry
            pdf_response = self._fetch_with_retry(
                "GET", pdf_url, timeout=120, verify=False
            )
            if pdf_response.status_code == 200 and len(pdf_response.content) > 100:
                # Verify it's a PDF
                if pdf_response.content[:4] == b"%PDF":
                    return pdf_response.content
                else:
                    logger.debug(f"Response is not a PDF: {pdf_response.content[:50]}")
                    return None

        except json.JSONDecodeError:
            logger.debug(f"Non-JSON response from display_pdf: {response.text[:100]}")
        except Exception as e:
            logger.error(f"Error downloading PDF: {e}")

        return None

    def _compress_pdf_bytes(self, pdf_content: bytes) -> bytes:
        """
        Compress PDF content (bytes) using Ghostscript.
        Returns compressed bytes if successful, original bytes otherwise.
        """
        import tempfile

        try:
            # Write to temp file
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_in:
                tmp_in.write(pdf_content)
                tmp_in_path = Path(tmp_in.name)

            original_size = len(pdf_content)

            # Compress the PDF
            compressed_path = compress_pdf_if_enabled(
                tmp_in_path, COMPRESSION_AVAILABLE
            )

            # Read the result
            with open(compressed_path, "rb") as f:
                result_content = f.read()

            compressed_size = len(result_content)

            # Clean up temp file
            if tmp_in_path.exists():
                tmp_in_path.unlink()

            # Log compression result
            if compressed_size < original_size:
                reduction = (1 - compressed_size / original_size) * 100
                logger.debug(
                    f"Compressed PDF: {original_size} -> {compressed_size} bytes ({reduction:.1f}% reduction)"
                )

            return result_content

        except Exception as e:
            logger.debug(f"PDF compression failed: {e}")
            return pdf_content

    def process_order(self, order_data: dict) -> bool:
        """Process a single order - download PDF and save metadata"""
        cnr = order_data.get("cnr", "")

        # Extract year from order date or use task date
        #
        # JurisBound change: keep the parsed date, not just the year. Files used to be
        # named "{cnr}.pdf" / "{cnr}.json", so every order of a case produced the
        # same name and the file_exists() checks below treated the second, third,
        # ... order as already downloaded and skipped them -- silently, with no
        # error. The date (and, below, the order number) now go into the name.
        # Moved up from after the case lookup: the already-saved check below
        # needs the year, and it must run before the lookup.
        order_date = None
        try:
            # Try to parse order date from order_data
            order_date_str = order_data.get("order_date", "")
            if order_date_str:
                order_date = parse_date_from_api(order_date_str)
            else:
                # Fall back to task from_date
                order_date = parse_date_from_api(self.task.from_date)
            year = order_date.year
        except (ValueError, KeyError, AttributeError):
            year = datetime.now().year

        # JurisBound change: skip orders already saved BEFORE the case lookup. The
        # file name needs the CNR, which only the lookup (a CAPTCHA plus ~3
        # requests) provides, so original paid that cost for every order it
        # already had -- on every look-back day and every retried day. The
        # search line's PDF code identifies the document without it.
        pdf_code = self._pdf_code(order_data)
        if self._already_saved(order_data):
            return False

        # Fetch detailed case information if enabled
        case_details = {}
        if self.fetch_case_details_enabled:
            self.lookup_not_found = False
            try:
                case_details = self.lookup_case(order_data, pdf_code)
                # Update CNR if we got a real one from case details
                if case_details.get("cnr"):
                    cnr = case_details["cnr"]
                    logger.debug(f"Got CNR from case details: {cnr}")
            except Exception as e:
                logger.debug(f"Failed to fetch case details: {e}")
            # JurisBound change: if the lookup failed because the portal blocked us
            # (the helpers above swallow errors, so check the flag), do not go
            # on to save this order under an UNKNOWN_ name -- stop here; the
            # day will be retried.
            if _BLOCKED.is_set():
                raise PortalBlocked("blocked during case lookup")
            # JurisBound change: the lookup failed (CAPTCHA gave up, case type not
            # known, case not found, site error). Original saved the order
            # anyway under the search line's case number ("OS_266_2023"),
            # which then went into the database as if it were the eCourts
            # case ID, with no case details -- and was never corrected, since
            # the order then counted as saved. Now it is not saved: it counts
            # as failed, so the day is not marked done and the next run tries
            # the lookup again.
            if not case_details.get("cnr"):
                # JurisBound change: "Record not found" from the court -- after
                # NOT_FOUND_STRIKES runs it stays listed but stops holding the
                # day back (see record_not_found).
                if self.lookup_not_found and pdf_code and self.archive_manager is not None:
                    strikes = record_not_found(self.task, pdf_code, order_data)
                    if strikes >= NOT_FOUND_STRIKES:
                        logger.warning(
                            f"Case search says 'Record not found' for "
                            f"{order_data.get('case_number')} ({order_data.get('order_date')}) "
                            f"on {strikes} separate runs -- not saved, kept on the "
                            f"not-found list, no longer holds the day back"
                        )
                        return False
                    if strikes:
                        logger.warning(
                            f"Case search says 'Record not found' for "
                            f"{order_data.get('case_number')} ({order_data.get('order_date')}) "
                            f"-- strike {strikes} of {NOT_FOUND_STRIKES}"
                        )
                self.failed_orders += 1
                logger.warning(
                    f"Case lookup failed for {order_data.get('case_number')} "
                    f"({order_data.get('order_date')}) -- not saved, day will be retried"
                )
                return False

        if not cnr:
            # Generate a unique ID from the raw HTML
            cnr = f"UNKNOWN_{uuid.uuid4().hex[:12]}"

        # JurisBound change: the order's own number, read off the case history page
        # the lookup above already fetched (its order table lists every order
        # with the same PDF code the search line carries). Without it two orders
        # of one case on one date got the same name and the second was skipped
        # as "already downloaded" -- seen twice in 44 orders on 2026-09-25.
        # Named like the High Court files: <cnr>_<order no>_<date>. If no line
        # matches, fall back to the old <cnr>_<date> name and say so.
        order_no = (case_details.get("_order_no_by_code") or {}).get(pdf_code)
        date_slug = order_date.strftime("%Y-%m-%d") if order_date else "unknown-date"
        if order_no:
            file_stem = f"{cnr}_{order_no}_{date_slug}"
        else:
            file_stem = f"{cnr}_{date_slug}"
            if self.fetch_case_details_enabled:
                logger.warning(
                    f"Order number not found on the case page for "
                    f"{order_data.get('case_number')} ({order_data.get('order_date')}); "
                    f"saving as {file_stem}"
                )

        # Check if metadata already exists
        metadata_filename = f"{file_stem}.json"
        # JurisBound change: original built this dict only when the details file was
        # not yet in the archive. It is now built for every order, but it is
        # still only USED (details file, PDF, database row) when the file is
        # missing from the archive -- an order whose PDF is already archived
        # gets no database row written here.
        metadata = {
            "cnr": cnr,
            "order_no": order_no,
            "pdf_code": pdf_code,
            "state_code": self.task.state_code,
            "state_name": self.task.state_name,
            "district_code": self.task.district_code,
            "district_name": self.task.district_name,
            "complex_code": self.task.complex_code,
            "complex_name": self.task.complex_name,
            "raw_html": order_data.get("raw_html", ""),
            "scraped_at": datetime.now(IST).isoformat(),
        }

        # Add order data fields (excluding internal fields)
        internal_fields = {"raw_html", "onclick", "pdf_href", "cnr"}
        for key, value in order_data.items():
            if key not in internal_fields and key not in metadata:
                metadata[key] = value

        # Add detailed case information if available
        if case_details:
            # Exclude the raw HTML from case details to avoid bloat
            internal_case_fields = {"case_details_html", "_order_no_by_code"}
            for key, value in case_details.items():
                if key not in internal_case_fields and key not in metadata:
                    metadata[key] = value

        if not self.archive_manager.file_exists(
            year,
            self.task.state_code,
            self.task.district_code,
            self.task.complex_code,
            "metadata",
            metadata_filename,
        ):
            self.archive_manager.add_to_archive(
                year,
                self.task.state_code,
                self.task.district_code,
                self.task.complex_code,
                "metadata",
                metadata_filename,
                json.dumps(metadata, indent=2, ensure_ascii=False),
            )

        # Check if PDF already exists
        pdf_filename = f"{file_stem}.pdf"
        if not self.archive_manager.file_exists(
            year,
            self.task.state_code,
            self.task.district_code,
            self.task.complex_code,
            "data",
            pdf_filename,
        ):
            # Download PDF
            pdf_content = self.download_pdf(order_data)
            if pdf_content:
                # Compress PDF if enabled
                if self.compress_pdfs:
                    pdf_content = self._compress_pdf_bytes(pdf_content)

                self.archive_manager.add_to_archive(
                    year,
                    self.task.state_code,
                    self.task.district_code,
                    self.task.complex_code,
                    "data",
                    pdf_filename,
                    pdf_content,
                    pdf_code=pdf_code,
                )

                # JurisBound change: their tar archive is bulk storage -- a PDF inside
                # it has no address of its own, so our OCR/analyse/index pipeline
                # cannot open it. Mirror this one order to our own bucket as an
                # addressable file and write the matching database row. The tar
                # above is left exactly as it was.
                if jb_db.record_order(
                    metadata,
                    pdf_content,
                    year=year,
                    pdf_filename=pdf_filename,
                    metadata_filename=metadata_filename,
                ):
                    # JurisBound change: if this order was on the missing-PDF
                    # list (a re-check found it), mark it found.
                    jb_db.mark_missing_found(self.task.complex_code, pdf_code, pdf_filename)
                return True
            # JurisBound change: the PDF did not download. If it fails in
            # MISSING_PDF_STRIKES separate runs (the court has not uploaded the
            # file), it is set aside on the missing_orders list and no longer
            # holds the day back; recheck_missing.py tries it again weekly.
            if pdf_code:
                strikes, status = jb_db.record_missing_pdf(self.task, pdf_code, order_data, _RUN_ID)
                if status in ("set_aside", "given_up"):
                    logger.warning(
                        f"PDF not saved for {order_data.get('case_number')} "
                        f"({order_data.get('order_date')}) on {strikes} separate runs "
                        f"-- {status} on the missing-PDF list, no longer holds the day back"
                    )
                    return False
                if strikes:
                    logger.warning(
                        f"PDF not saved for {order_data.get('case_number')} "
                        f"({order_data.get('order_date')}) -- strike {strikes} of "
                        f"{jb_db.MISSING_PDF_STRIKES}"
                    )
            # JurisBound change: count it, so download() leaves the day not-done.
            self.failed_orders += 1
            logger.warning(
                f"PDF not saved for {order_data.get('case_number')} "
                f"({order_data.get('order_date')}) -- day will be retried"
            )

        return False

    def download(self) -> bool:
        """Process the task - search and download orders.

        JurisBound change: returns True only when the task is done -- the search
        answered and every order's PDF is saved (or already was). Original
        marked the task done in every case, including a failed search and a
        blocked run, so those days were skipped by every later run and their
        orders never collected.
        """
        # Check if task already completed (BEFORE hitting the server)
        if is_task_completed(self.task):
            logger.debug(f"Skipping already completed task: {self.task}")
            return True
        if _BLOCKED.is_set():
            return False
        # JurisBound change: time limit reached -- do not start this day.
        if time_up():
            return False

        try:
            # JurisBound change: pause before each court/date search too, not only
            # between cases -- a daily run is ~123 searches for Telangana alone,
            # most with few or no cases, so without this they would run
            # back to back.
            _polite_pause()
            self.init_session()

            # Set court data
            if not self.set_court_data():
                logger.error(f"Failed to set court data for task: {self.task}")
                return False

            # Search for orders
            html = self.search_orders()
            if not html:
                # JurisBound change: None means the search itself failed (error,
                # CAPTCHA give-up, network) -- an empty day comes back as a
                # "Record not found" page instead, handled below. Was marked
                # done here.
                logger.warning(f"Search failed, day will be retried: {self.task}")
                return False

            # Parse results
            orders = self.parse_order_results(html)
            if not orders:
                # JurisBound change: only mark done when the portal actually said
                # there is nothing (verified 2026-09-28: an empty day returns
                # status 1 with id='nodata' "Record not found"). Any other page
                # that parses to no rows is unexpected, so retry it.
                if "nodata" in html or "Record not found" in html:
                    logger.debug(f"No orders found for task: {self.task}")
                    save_completed_task(get_task_key(self.task))
                    return True
                logger.warning(
                    f"Search returned no rows and no 'Record not found', "
                    f"day will be retried: {self.task}"
                )
                return False

            logger.info(f"Found {len(orders)} orders for task: {self.task}")

            # Process each order with progress bar
            downloaded = 0
            pbar = tqdm(
                orders,
                desc=f"PDFs ({self.task.complex_name[:20]})",
                leave=False,
                unit="pdf",
            )
            pause_next = False
            stopped_for_time = False
            for order in pbar:
                # JurisBound change: stop at once if the portal blocked us -- the
                # remaining cases would all fail.
                if _BLOCKED.is_set():
                    break
                # JurisBound change: time limit reached -- stop between orders (one
                # search can hold years of orders, so checking only between
                # days would not be enough).
                if time_up():
                    stopped_for_time = True
                    break
                # JurisBound change: an order already saved (PDF code in the index)
                # sends no request, so it needs no pause either.
                if self._already_saved(order):
                    continue
                # JurisBound change: pause between cases (see CASE_PAUSE_SECONDS).
                if pause_next:
                    _polite_pause()
                pause_next = True
                if self.process_order(order):
                    downloaded += 1
                    pbar.set_postfix({"new": downloaded})

            logger.info(
                f"Downloaded {downloaded} new PDFs out of {len(orders)} orders for task: {self.task}"
            )

            # JurisBound change: mark done only if nothing was lost.
            if _BLOCKED.is_set():
                logger.warning(f"Blocked mid-task, day will be retried: {self.task}")
                return False
            if stopped_for_time:
                logger.warning(f"Time limit reached mid-task, day will be retried: {self.task}")
                return False
            if self.failed_orders:
                logger.warning(
                    f"{self.failed_orders} of {len(orders)} orders not saved, "
                    f"day will be retried: {self.task}"
                )
                return False

            # Mark task as completed
            save_completed_task(get_task_key(self.task))
            return True

        except PortalBlocked:
            return False
        except Exception as e:
            logger.error(f"Error processing task {self.task}: {e}")
            traceback.print_exc()
            return False


def process_task(
    task: DistrictCourtTask,
    archive_manager: S3ArchiveManager,
    compress_pdfs: bool = True,
) -> bool:
    """Process a single task. JurisBound change: returns whether it is done."""
    try:
        downloader = Downloader(task, archive_manager, compress_pdfs=compress_pdfs)
        return downloader.download()
    except PortalBlocked:
        return False
    except Exception as e:
        logger.error(f"Error processing task {task}: {e}")
        traceback.print_exc()
        return False
    finally:
        save_register(archive_manager, task)


def save_register(archive_manager, task: DistrictCourtTask) -> None:
    """JurisBound change: upload this court's new bundle part and its index (the
    register of saved files and fingerprints) as soon as the task ends --
    done, failed or blocked.

    Original uploaded them only when the whole run finished, while each PDF
    and database row is saved at once. A run that stopped early (18 Sep 2026,
    Medchal: 15 orders) left those orders saved but missing from the
    register, so the next run looked them up and downloaded them again.
    Now at most the task in progress is repeated.
    """
    if archive_manager is None:
        return
    try:
        with archive_manager.lock:
            years = {
                key[0]
                for key in list(archive_manager.archives.keys())
                if key[1:4] == (task.state_code, task.district_code, task.complex_code)
            }
            for year in sorted(years):
                archive_manager.flush_complex_year(
                    year, task.state_code, task.district_code, task.complex_code
                )
    except Exception as e:
        logger.error(f"Could not save the register for {task}: {e}")


def run(
    courts: List[CourtComplex],
    start_date: str,
    end_date: str,
    day_step: int = 1,
    # JurisBound change: was 5. Every worker multiplies the request rate, and the
    # district portal blocks on rate. See CASE_PAUSE_SECONDS. Tested 2026-09-29
    # from one IP: 2 workers + 5 s pause ran clean (~1 req/s); 2 workers + 3 s
    # pause was blocked within 90 s.
    max_workers: int = 2,
    archive_manager: Optional[S3ArchiveManager] = None,
    compress_pdfs: bool = True,
):
    """Run the downloader for all courts and date ranges"""
    # Create archive manager if not provided
    if archive_manager is None:
        archive_manager = S3ArchiveManager(
            s3_bucket=S3_BUCKET,
            s3_prefix=S3_PREFIX,
            local_dir=LOCAL_DIR,
            # JurisBound change: was True, so the tar parts and — more importantly —
            # the index never left the local disk. The index is what a later run
            # reads to know what it already has, so keeping it local means
            # losing the machine loses all resume state.
            local_only=False,
        )

    # Generate tasks
    tasks = list(generate_tasks(courts, start_date, end_date, day_step))
    logger.info(f"Generated {len(tasks)} tasks")

    # Process tasks
    # JurisBound change: collect which tasks finished, so run_sync_s3 can record how
    # far each court is really done (see sync_s3.write_scraped_through).
    results: List[tuple] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(process_task, task, archive_manager, compress_pdfs): task
            for task in tasks
        }

        for i, future in enumerate(
            tqdm(
                concurrent.futures.as_completed(futures),
                total=len(futures),
                desc="Processing tasks",
            )
        ):
            try:
                done = bool(future.result())
            except Exception as e:
                logger.error(f"Task failed: {e}")
                done = False
            results.append((futures[future], done))

    not_done = sum(1 for _, done in results if not done)
    if _BLOCKED.is_set():
        logger.error(
            f"Stopped: portal blocked this IP. {not_done} of {len(results)} "
            f"tasks not done; they will be retried next run."
        )
    elif time_up():
        logger.warning(
            f"Stopped: time limit reached. {not_done} of {len(results)} tasks not "
            f"done; the next run continues from them."
        )
    elif not_done:
        logger.warning(f"{not_done} of {len(results)} tasks not done; will be retried.")
    else:
        logger.info("All tasks completed")
    if mobile_lookup.LOOKUP_MODE != "web":
        logger.info(f"App lookup ({mobile_lookup.LOOKUP_MODE}): {mobile_lookup.stats()}")
    return results


def main():
    parser = argparse.ArgumentParser(
        description="JurisBound District Court scraper"
    )
    parser.add_argument(
        "--start_date",
        type=str,
        default=None,
        help="Start date in YYYY-MM-DD format",
    )
    parser.add_argument(
        "--end_date",
        type=str,
        default=None,
        help="End date in YYYY-MM-DD format",
    )
    parser.add_argument(
        "--day_step",
        type=int,
        default=2100,  # Large default to minimize chunks & districts has no pagination & ~5yrs data
        help="Number of days per chunk",
    )
    parser.add_argument(
        "--max_workers",
        type=int,
        # JurisBound change: same as original (2), now paired with the 5 s pause --
        # see run(). Do not raise it or shorten the pause from one IP.
        default=2,
        help="Number of parallel workers (default: 2 -- the portal blocks on request rate)",
    )
    parser.add_argument(
        "--state_code",
        type=str,
        default=None,
        help="Filter by state code",
    )
    parser.add_argument(
        "--district_code",
        type=str,
        default=None,
        help="Filter by district code",
    )
    parser.add_argument(
        "--complex_code",
        type=str,
        default=None,
        help="Filter by complex code",
    )
    parser.add_argument(
        "--courts_csv",
        type=str,
        default=str(Path(__file__).parent.parent / "courts.csv"),
        help="Path to courts.csv file",
    )
    parser.add_argument(
        "--sync-s3",
        action="store_true",
        default=False,
        help="Sync mode (incremental updates)",
    )
    parser.add_argument(
        "--sync-s3-fill",
        action="store_true",
        default=False,
        help="Gap-filling mode (historical backfill)",
    )
    parser.add_argument(
        "--timeout-hours",
        type=float,
        # JurisBound change: was 5.5 and only read by --sync-s3-fill (which still
        # gets 5.5 when the flag is omitted, see below). Now also honoured by
        # --sync-s3 and manual runs; omitted there = no limit, as before.
        default=None,
        help="Stop starting new work after this many hours, then save and exit "
             "(no limit if omitted; --sync-s3-fill defaults to 5.5)",
    )
    parser.add_argument(
        "--no-compress",
        action="store_true",
        default=False,
        help="Disable PDF compression (compression is enabled by default)",
    )
    parser.add_argument(
        "--upload-local",
        action="store_true",
        default=False,
        help="Upload existing local TAR files to S3 (no downloading)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Show what would be uploaded without actually uploading (use with --upload-local)",
    )
    args = parser.parse_args()

    # Handle PDF compression settings
    compress_pdfs = not args.no_compress
    if compress_pdfs:
        if COMPRESSION_AVAILABLE:
            logger.info("PDF compression enabled (using Ghostscript)")
        else:
            logger.warning(
                "PDF compression requested but Ghostscript not available - compression disabled"
            )
            compress_pdfs = False
    else:
        logger.info("PDF compression disabled (--no-compress flag)")

    # Load courts
    courts_path = Path(args.courts_csv)
    if not courts_path.exists():
        logger.error(f"Courts file not found: {courts_path}")
        logger.info("Run 'python scrape_courts.py' first to generate courts.csv")
        sys.exit(1)

    courts = load_courts_csv(courts_path)
    logger.info(f"Loaded {len(courts)} court complexes")

    # Apply filters
    if args.state_code:
        courts = [c for c in courts if c.state_code == args.state_code]
    if args.district_code:
        courts = [c for c in courts if c.district_code == args.district_code]
    if args.complex_code:
        courts = [c for c in courts if c.complex_code == args.complex_code]

    if not courts:
        logger.error("No courts match the specified filters")
        sys.exit(1)

    logger.info(f"Processing {len(courts)} court complexes")

    if args.upload_local:
        from upload_local import run_upload_local

        run_upload_local(
            s3_bucket=S3_BUCKET,
            s3_prefix=S3_PREFIX,
            local_dir=LOCAL_DIR,
            state_code=args.state_code,
            district_code=args.district_code,
            complex_code=args.complex_code,
            dry_run=args.dry_run,
        )
    elif args.sync_s3_fill:
        from sync_s3_fill import sync_s3_fill_gaps

        sync_s3_fill_gaps(
            s3_bucket=S3_BUCKET,
            s3_prefix=S3_PREFIX,
            local_dir=LOCAL_DIR,
            courts=courts,
            start_date=args.start_date,
            end_date=args.end_date,
            day_step=args.day_step,
            max_workers=args.max_workers,
            timeout_hours=args.timeout_hours or 5.5,
            compress_pdfs=compress_pdfs,
        )
    elif args.sync_s3:
        from sync_s3 import run_sync_s3

        set_time_limit(args.timeout_hours)
        run_sync_s3(
            s3_bucket=S3_BUCKET,
            s3_prefix=S3_PREFIX,
            local_dir=LOCAL_DIR,
            courts=courts,
            start_date=args.start_date,
            end_date=args.end_date,
            day_step=args.day_step,
            max_workers=args.max_workers,
            compress_pdfs=compress_pdfs,
        )
    else:
        # Default: local download mode
        if not args.start_date:
            logger.error("--start_date is required for local download mode")
            sys.exit(1)

        end_date = args.end_date or datetime.now().strftime("%Y-%m-%d")
        set_time_limit(args.timeout_hours)

        with S3ArchiveManager(
            s3_bucket=S3_BUCKET,
            s3_prefix=S3_PREFIX,
            local_dir=LOCAL_DIR,
            # JurisBound change: was True, so the tar parts and — more importantly —
            # the index never left the local disk. The index is what a later run
            # reads to know what it already has, so keeping it local means
            # losing the machine loses all resume state.
            local_only=False,
        ) as archive_manager:
            results = run(
                courts=courts,
                start_date=args.start_date,
                end_date=end_date,
                day_step=args.day_step,
                max_workers=args.max_workers,
                archive_manager=archive_manager,
                compress_pdfs=compress_pdfs,
            )

        # JurisBound change: a manual run also records how far each court is done,
        # so the daily automatic run continues after it instead of starting
        # from 1 January. See sync_s3.record_scraped_through for when a
        # court's date moves (an old backfill never moves it).
        from sync_s3 import record_scraped_through

        record_scraped_through(S3_BUCKET, results, courts, args.start_date)

    # JurisBound change: a blocked run must not look like a success to whatever
    # launched it (scheduler, workflow) -- exit 2 so it can alert.
    # In --sync-s3 mode the tasks run in the second copy of this file
    # (imported by sync_s3.py as "download"), whose flag is a different object
    # -- check that one too, or a blocked automatic run exits 0.
    second_copy = sys.modules.get("download")
    if _BLOCKED.is_set() or (second_copy is not None and second_copy._BLOCKED.is_set()):
        sys.exit(2)


if __name__ == "__main__":
    main()
