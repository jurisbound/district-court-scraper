"""Weekly re-check of orders whose PDF the court had not uploaded.

JurisBound (2026-10-15). An order whose PDF fails to download in 3 separate
runs is "set_aside" in the table missing_orders (see jb_db.record_missing_pdf)
so its day can be marked done. The court may upload the file later, so this
script tries each set-aside order again once a week, 4 times:

  * due = status 'set_aside', fewer than RECHECKS re-checks, last tried at
    least 6 days ago;
  * for each order date, the normal scraper (download.py --sync-s3) is run for
    that one day on just the court complexes that have a due order. Orders
    already saved are skipped; a set-aside PDF that now downloads is saved
    like any order and its row becomes "found";
  * every due order still not found counts one re-check; after RECHECKS
    re-checks it becomes "given_up" (kept in the table, not tried again).

A complex's "done up to" date never moves back, so re-running an old day is
safe.

    python recheck_missing.py --list           # show what is due, run nothing
    python recheck_missing.py                  # re-check (Azure settings from .env)
    python recheck_missing.py --timeout-hours 5.5
"""
import argparse
import csv
import logging
import os
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # district-court/
import jb_db  # noqa: E402  -- puts the repo root on sys.path and loads .env

RECHECKS = int(os.environ.get("DC_MISSING_RECHECKS", "4"))
RECHECK_EVERY_DAYS = 6          # "once a week", with a day's slack for late runs
COURTS_CSV = Path(__file__).resolve().parent.parent / "courts.csv"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("recheck_missing")


def due_orders():
    from sqlalchemy import text

    from storage.database import SyncSessionLocal

    with SyncSessionLocal() as session:
        return session.execute(
            text(
                """
                SELECT id, state_code, district_code, complex_code, complex_name,
                       case_number, order_date, rechecks
                FROM missing_orders
                WHERE court_type = 'district' AND status = 'set_aside'
                  AND rechecks < :max
                  AND (last_tried IS NULL OR last_tried <= now() - make_interval(days => :every))
                ORDER BY order_date, complex_code
                """
            ),
            {"max": RECHECKS, "every": RECHECK_EVERY_DAYS},
        ).all()


def count_recheck(ids):
    """One re-check for each order still not found; given_up after RECHECKS."""
    from sqlalchemy import text

    from storage.database import SyncSessionLocal

    with SyncSessionLocal() as session:
        session.execute(
            text(
                """
                UPDATE missing_orders SET
                    rechecks = rechecks + 1,
                    status = CASE WHEN rechecks + 1 >= :max THEN 'given_up' ELSE status END
                WHERE id = ANY(:ids) AND status = 'set_aside'
                """
            ),
            {"ids": list(ids), "max": RECHECKS},
        )
        session.commit()


def courts_file(complex_keys, out_path):
    with open(COURTS_CSV, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = [r for r in reader
                if (r["state_code"], r["district_code"], r["complex_code"]) in complex_keys]
        fields = reader.fieldnames
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--list", action="store_true", help="Show the due orders and stop")
    ap.add_argument("--timeout-hours", type=float, default=5.5,
                    help="Stop starting new days after this many hours")
    args = ap.parse_args()

    rows = due_orders()
    logger.info(f"{len(rows)} set-aside order(s) due for a re-check")
    by_date = defaultdict(list)
    for r in rows:
        by_date[r.order_date].append(r)
    for day, items in sorted(by_date.items()):
        for r in items:
            logger.info(f"  {day}  {r.complex_name}  {r.case_number}  (re-check {r.rechecks + 1} of {RECHECKS})")
    if args.list or not rows:
        return

    deadline = time.time() + args.timeout_hours * 3600
    for day, items in sorted(by_date.items()):
        left_h = (deadline - time.time()) / 3600
        if left_h <= 0.1:
            logger.warning(f"Time limit reached; {day} and later dates are left for the next re-check")
            break
        keys = {(r.state_code, r.district_code, r.complex_code) for r in items}
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "courts.csv"
            n = courts_file(keys, csv_path)
            d = day.strftime("%Y-%m-%d")
            cmd = [sys.executable, "download.py", "--sync-s3", "--courts_csv", str(csv_path),
                   "--start_date", d, "--end_date", d, "--timeout-hours", f"{left_h:.2f}"]
            logger.info(f"Re-checking {d}: {len(items)} order(s) in {n} court complex(es)")
            logger.info("Running: " + " ".join(cmd))
            # Search the day again even if this machine finished it before.
            env = dict(os.environ, DC_IGNORE_COMPLETED="1")
            code = subprocess.call(cmd, cwd=str(Path(__file__).resolve().parent), env=env)
        count_recheck(r.id for r in items)
        if code == 2:
            logger.error("The court website blocked this address -- stopping the re-check")
            sys.exit(2)


if __name__ == "__main__":
    main()
