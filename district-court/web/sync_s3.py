"""
S3 Sync Module for District Court Judgments
Handles syncing with S3 and incremental downloads
"""

import json
import logging
import sys
import tarfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from src.utils.court_utils import CourtComplex

logger = logging.getLogger(__name__)

# JurisBound change: the two resume helpers below used an anonymous AWS client and
# the original layout (metadata/tar/year=YYYY/state=XX/...). Neither works for
# us: that path no longer exists after the move to
# courts/district/<state>/<district>/<complex>/<year>/archive/, and Supabase
# refuses unsigned reads. Both silently returned "nothing found", so every
# `--sync-s3` run concluded the scraper had never run and restarted from
# 1 January of the current year -- re-solving a CAPTCHA per date for orders it
# already had. Explicit --start_date runs never call these.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_DISTRICT_ROOT = "courts/district/"


def _s3_client():
    """Authenticated client, same one the archive code uploads with."""
    from archive_manager import _make_s3_client

    return _make_s3_client()


def _state_prefix(state_code: str) -> str:
    """Narrow the scan to one state's tree when its name is known.

    The new layout uses slugged names, not codes, so the code is resolved
    through courts.csv. Falls back to the whole district tree, which is correct
    but slower.
    """
    from archive_manager import _slug

    try:
        import csv

        with open(
            Path(__file__).resolve().parent.parent / "courts.csv", encoding="utf-8"
        ) as fh:
            for row in csv.DictReader(fh):
                if row["state_code"] == str(state_code):
                    return f"{_DISTRICT_ROOT}{_slug(row['state_name'])}/"
    except Exception as e:  # pragma: no cover
        logger.debug(f"Could not resolve state {state_code} from courts.csv: {e}")
    return _DISTRICT_ROOT


def get_latest_index_date(s3_bucket: str, state_code: str) -> Optional[datetime]:
    """
    Get the latest updated_at date from index files for a state.
    Returns the most recent date across all districts/complexes.
    """
    s3 = _s3_client()

    latest_date = None
    prefix = _state_prefix(state_code)

    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=s3_bucket, Prefix=prefix):
        if "Contents" not in page:
            continue

        for obj in page["Contents"]:
            key = obj["Key"]
            if not key.endswith(".index.json"):
                continue

            try:
                response = s3.get_object(Bucket=s3_bucket, Key=key)
                index_data = json.loads(response["Body"].read().decode("utf-8"))

                # The state used to be readable from the key ("/state=29/").
                # The new layout carries names, not codes, so it is read from
                # the index body, which records all three codes.
                if str(index_data.get("state_code", "")) != str(state_code):
                    continue

                if "updated_at" in index_data:
                    updated_at = datetime.fromisoformat(
                        index_data["updated_at"].replace("Z", "+00:00")
                    )
                    if latest_date is None or updated_at > latest_date:
                        latest_date = updated_at

            except Exception as e:
                logger.debug(f"Could not read index {key}: {e}")

    return latest_date


def get_latest_metadata_date_from_tar(
    s3_bucket: str, state_code: str
) -> Optional[datetime]:
    """
    Fall back to parsing metadata TAR files to find the latest order date.
    """
    s3 = _s3_client()

    latest_date = None
    current_year = datetime.now().year
    prefix = _state_prefix(state_code)

    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=s3_bucket, Prefix=prefix):
        if "Contents" not in page:
            continue

        for obj in page["Contents"]:
            key = obj["Key"]
            # …/<year>/archive/metadata.tar, plus the "metadata-part-<ts>.tar"
            # parts written when an archive is split.
            if f"/{current_year}/archive/" not in key:
                continue
            name = key.rsplit("/", 1)[-1]
            if not (name.startswith("metadata") and name.endswith(".tar")):
                continue

            # Download and parse
            import tempfile

            with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tmp:
                tmp_path = Path(tmp.name)

            try:
                s3.download_file(s3_bucket, key, str(tmp_path))

                with tarfile.open(tmp_path, "r") as tf:
                    for member in tf.getmembers():
                        if not member.name.endswith(".json"):
                            continue

                        f = tf.extractfile(member)
                        if not f:
                            continue

                        try:
                            data = json.load(f)
                            scraped_at = data.get("scraped_at", "")
                            if scraped_at:
                                dt = datetime.fromisoformat(
                                    scraped_at.replace("Z", "+00:00")
                                )
                                if latest_date is None or dt > latest_date:
                                    latest_date = dt
                        except Exception:
                            pass

            except Exception as e:
                logger.debug(f"Could not process {key}: {e}")
            finally:
                tmp_path.unlink(missing_ok=True)

    return latest_date


# ── JurisBound change: real "scraped through" date per court complex ─────────────
# Automatic mode used to start from get_latest_index_date() + 1 day. That is the
# time the index file was last UPLOADED, not the last order date collected: a
# backfill of old dates run today made the next automatic run start tomorrow and
# skip everything in between, and a blocked or failed day was never revisited.
# Instead each complex now gets a small marker file recording the last date up
# to which every day is done (no gaps), written only after the run, and the
# next run starts a few days before it -- the same idea as the High Court
# scraper's scraped_through_date plus its overlap window.
#
#   courts/district/<state>/<district>/<complex>/scraped_through.json
#
# LOOKBACK_DAYS re-checks the last few days because orders are often uploaded
# to the portal after their order date. Already-saved orders are skipped by
# their fingerprint before any case lookup, so a re-checked day costs about
# one search. Override with DC_LOOKBACK_DAYS.
import os  # noqa: E402

LOOKBACK_DAYS = int(os.environ.get("DC_LOOKBACK_DAYS", "3"))
_MARKER_NAME = "scraped_through.json"


def _marker_key(court: CourtComplex) -> str:
    from archive_manager import _court_names

    state, district, complex_name = _court_names(
        court.state_code, court.district_code, court.complex_code
    )
    return f"{_DISTRICT_ROOT}{state}/{district}/{complex_name}/{_MARKER_NAME}"


def read_scraped_through(s3, s3_bucket: str, court: CourtComplex):
    """The complex's scraped-through date, or None if it has never finished one."""
    try:
        body = s3.get_object(Bucket=s3_bucket, Key=_marker_key(court))["Body"].read()
        return datetime.strptime(
            json.loads(body)["scraped_through_date"], "%Y-%m-%d"
        ).date()
    except Exception as e:
        logger.debug(f"No scraped_through marker for {court.complex_code}: {e}")
        return None


def write_scraped_through(s3, s3_bucket: str, court: CourtComplex, through) -> None:
    """Advance the marker; never move it backwards."""
    existing = read_scraped_through(s3, s3_bucket, court)
    if existing and existing >= through:
        return
    s3.put_object(
        Bucket=s3_bucket,
        Key=_marker_key(court),
        Body=json.dumps(
            {
                "scraped_through_date": through.strftime("%Y-%m-%d"),
                "state_code": court.state_code,
                "district_code": court.district_code,
                "complex_code": court.complex_code,
                "updated_at": datetime.now().isoformat(),
            },
            indent=2,
        ).encode("utf-8"),
        ContentType="application/json",
    )
    logger.info(f"{court.complex_name}: scraped through {through}")


def done_through(results, court: CourtComplex):
    """Last date up to which every task for this complex is done, walking from
    the earliest; stops at the first task not done, so a gap is never jumped."""
    mine = sorted(
        (
            (datetime.strptime(t.from_date, "%d-%m-%Y").date(),
             datetime.strptime(t.to_date, "%d-%m-%Y").date(), done)
            for t, done in results
            if t.complex_code == court.complex_code
            and t.district_code == court.district_code
            and t.state_code == court.state_code
        )
    )
    through = None
    for _, to_d, done in mine:
        if not done:
            break
        through = to_d
    return through


def record_scraped_through(s3_bucket: str, results, courts, run_start: str) -> None:
    """Advance each complex's marker after a run -- automatic or manual.

    A complex's marker moves to the last day of the unbroken run of done tasks
    from the run's start, but only when that run joins up with what is already
    recorded: the complex has no marker yet, or the run started no later than
    the day after it. So:
      - manual month (1-31 Aug) on a fresh complex -> marker 31 Aug; the daily
        run then continues from there;
      - daily run starting inside the look-back -> marker moves to today;
      - backfill of an old period (e.g. 2019) on a complex that already has a
        marker -> joins up, but ends before the marker, which never moves back:
        no change;
      - a run that leaves a gap after the marker -> no change, so the gap is
        still scraped by the next automatic run.
    One consequence: the FIRST run on a complex sets its marker, so run the
    recent month before any backfill of older years.
    """
    if not results:
        return
    start = datetime.strptime(run_start, "%Y-%m-%d").date()
    s3 = _s3_client()
    for court in courts:
        through = done_through(results, court)
        if not through:
            continue
        existing = read_scraped_through(s3, s3_bucket, court)
        if existing is not None and start > existing + timedelta(days=1):
            logger.info(
                f"{court.complex_name}: run starts {start}, after the gap from "
                f"{existing} -- scraped-through date not moved"
            )
            continue
        try:
            write_scraped_through(s3, s3_bucket, court, through)
        except Exception as e:
            logger.error(f"Could not write marker for {court.complex_name}: {e}")


def run_sync_s3(
    s3_bucket: str,
    s3_prefix: str,
    local_dir: Path,
    courts: List[CourtComplex],
    start_date: Optional[str],
    end_date: Optional[str],
    day_step: int,
    max_workers: int,
    compress_pdfs: bool = True,
):
    """
    Run the sync-s3 operation: check latest date in S3 and download new data.

    If start_date is provided, uses it directly (skips S3 auto-detection).
    If start_date is not provided, auto-detects from S3 metadata.
    """
    from archive_manager import S3ArchiveManager
    from download import run
    from process_metadata import DistrictCourtMetadataProcessor

    # Get unique states from courts
    states = set(c.state_code for c in courts)
    today = datetime.now().date()

    # Determine date range(s). JurisBound change: a list of (start, end, courts)
    # groups -- in automatic mode each complex starts from its own marker, so
    # one complex that is behind does not drag every other one back with it.
    groups = []
    if start_date:
        # User provided explicit dates - use them directly
        actual_end = end_date if end_date else today.strftime("%Y-%m-%d")
        groups.append((start_date, actual_end, list(courts)))
        logger.info(f"Using provided date range: {start_date} to {actual_end}")
    else:
        logger.info("Reading each complex's scraped-through date...")
        s3 = _s3_client()
        by_start = {}
        for court in courts:
            through = read_scraped_through(s3, s3_bucket, court)
            if through is None:
                # Never finished a day: original's "no data" default.
                start = datetime(today.year, 1, 1).date()
            else:
                start = through - timedelta(days=LOOKBACK_DAYS)
            by_start.setdefault(start, []).append(court)
        for start in sorted(by_start):
            logger.info(
                f"{len(by_start[start])} complex(es) from {start} to {today} "
                f"(look-back {LOOKBACK_DAYS} days)"
            )
            groups.append(
                (start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"), by_start[start])
            )

    actual_start = min(g[0] for g in groups) if groups else None
    actual_end = max(g[1] for g in groups) if groups else None

    # Track changes
    changes_made = False
    all_changes = {}
    results = []

    from download import _BLOCKED, time_up

    with S3ArchiveManager(
        s3_bucket, s3_prefix, local_dir, immediate_upload=True
    ) as archive_manager:
        for g_start, g_end, g_courts in groups:
            # JurisBound change: also stop at the time limit (--timeout-hours).
            if _BLOCKED.is_set() or time_up():
                break
            # Run the downloader
            results += run(
                courts=g_courts,
                start_date=g_start,
                end_date=g_end,
                day_step=day_step,
                max_workers=max_workers,
                archive_manager=archive_manager,
                compress_pdfs=compress_pdfs,
            ) or []
            changes_made = True

        if changes_made:
            all_changes = archive_manager.get_all_changes()

    # JurisBound change: record how far each complex is really done -- for
    # automatic and explicit-date runs alike; record_scraped_through only
    # moves a marker when the run joins up with it, so an old backfill cannot.
    for g_start, _, g_courts in groups:
        record_scraped_through(
            s3_bucket,
            [(t, ok) for t, ok in results if any(
                t.complex_code == c.complex_code and t.district_code == c.district_code
                and t.state_code == c.state_code for c in g_courts)],
            g_courts,
            g_start,
        )

    # Log summary
    if changes_made and all_changes:
        logger.info("\nSync Summary:")
        logger.info(f"  Date range: {actual_start} to {actual_end}")
        for location, archives in all_changes.items():
            logger.info(f"  {location}:")
            for archive_type, files in archives.items():
                logger.info(f"    {archive_type}: {len(files)} files")

    # JurisBound change: the parquet step is skipped. It reads the original layout
    # (metadata/tar/...) with an anonymous AWS client, so against our Supabase
    # bucket it always failed with AccessDenied (seen 2026-09-29). Nothing in
    # the district scraper reads the parquet file -- the start date comes from
    # scraped_through.json and the case details are in our database.
    if changes_made:
        logger.info("Parquet summary skipped (not used by JurisBound)")

    # Clean up local directory only if we made changes (uploaded data)
    import shutil

    if changes_made and local_dir.exists():
        shutil.rmtree(local_dir)
        logger.info(f"Cleaned up local directory: {local_dir}")
    elif local_dir.exists():
        logger.warning(
            f"Local directory NOT cleaned up (no sync performed): {local_dir}"
        )
