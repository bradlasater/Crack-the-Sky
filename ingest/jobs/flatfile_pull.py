"""Pull Massive flat files (S3) for the previous trading day and filter to SPY/SPX.

For each dataset (``trades_v1``, ``minute_aggs_v1``, ``day_aggs_v1``) the job
HEADs ``us_options_opra/{ds}/{YYYY}/{MM}/{YYYY-MM-DD}.csv.gz`` on bucket
``flatfiles`` (endpoint ``https://files.massive.com``, signature v4). While
the file is not yet published (404) and it is before 12:00 ET, the job sleeps
300s and retries. The object is downloaded to
``raw/flatfiles/{ds}/dt={date}/``, gzip-validated, then stream-filtered to
tickers whose OPRA *root* is SPY, SPX or SPXW (a plain prefix match also
swept in unrelated underlyings -- SPXL/SPXS/SPYG and friends are leveraged
ETFs, not SPY or SPX) and written as
clean parquet (``option_trades`` / ``option_minute_bars`` / ``option_day_bars``
with ``src='flatfile'``). A manifest entry is appended to
``_meta/flatfile_manifest.json``.

S3 auth failures (InvalidSignature / AccessDenied / 403) print a clear
"fix S3 creds in .env" message and exit 3.

Default ``--date`` is the previous trading day (so the Tue-Sat 11:05 cron
always targets yesterday); pass ``--date`` explicitly to backfill.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import sys
import time
from datetime import date
from datetime import time as dtime
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError

from ingest.common import landing, market_gate
from ingest.common.cli import run_job
from ingest.common.config import Settings
from ingest.common.logging_utils import JsonlLogger
from ingest.common.s3 import s3_client
from ingest.jobs import OPTION_ROOTS, keep_ticker  # noqa: F401  (shared root filter)

JOB = "flatfile_pull"
DATASETS = ("trades_v1", "minute_aggs_v1", "day_aggs_v1")
CLEAN_DATASET = {
    "trades_v1": "option_trades",
    "minute_aggs_v1": "option_minute_bars",
    "day_aggs_v1": "option_day_bars",
}

RETRY_SLEEP_S = 300
RETRY_UNTIL_ET = dtime(12, 0)  # T-1 file is normally published ~11:00 ET
CHUNK = 1024 * 1024

_AUTH_ERROR_CODES = {
    "InvalidSignature",
    "SignatureDoesNotMatch",
    "InvalidAccessKeyId",
    "AccessDenied",
    "Forbidden",
    "403",
}


# Re-exported so existing imports (and tests) keep working; the single source
# of truth now lives in market_gate.
previous_trading_day = market_gate.previous_trading_day


def s3_key(dataset: str, d: date) -> str:
    """Flat-file object key for ``dataset`` on ``d``."""
    return f"us_options_opra/{dataset}/{d:%Y}/{d:%m}/{d.isoformat()}.csv.gz"


def _s3_client(settings: Settings) -> Any:
    """Shared S3 client, after the creds precheck that exits 3 when unset."""
    if not settings.massive_s3_access_key_id or not settings.massive_s3_secret_access_key:
        _creds_fail("MASSIVE_S3_ACCESS_KEY_ID / MASSIVE_S3_SECRET_ACCESS_KEY are unset")
    return s3_client(settings)


def _creds_fail(detail: str) -> None:
    """Print the actionable creds message and exit 3 (per SPEC)."""
    print(
        "ERROR: Massive flat-file S3 authentication failed "
        f"({detail}).\nFix the S3 creds in .env "
        "(MASSIVE_S3_ACCESS_KEY_ID / MASSIVE_S3_SECRET_ACCESS_KEY from the "
        "Massive dashboard -> S3 Access Keys), then re-run.",
        file=sys.stderr,
    )
    sys.exit(3)


def _is_auth_error(exc: ClientError) -> bool:
    code = str(exc.response.get("Error", {}).get("Code", ""))
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in _AUTH_ERROR_CODES or status == 403


def _credentials_work(s3: Any, bucket: str) -> bool:
    """True when the current credentials can still talk to the bucket.

    A 403 on ``head_object`` is ambiguous: bad credentials, a dataset above
    the plan tier (``quotes_v1``), or a date outside the entitled history
    window -- the vendor returns 403 rather than 404 for 2013 dates even
    though ``trades_v1`` listings start at 2014. Treating all three as "your
    keys are broken" made a backfill exit 3 on its first too-old date. A
    successful list proves the keys are fine and the 403 was about the object.
    """
    try:
        s3.list_objects_v2(Bucket=bucket, MaxKeys=1)
        return True
    except Exception:  # noqa: BLE001 - any failure here means assume auth broke
        return False


def _head_with_retry(
    s3: Any,
    bucket: str,
    key: str,
    logger: JsonlLogger,
    wait_for_publish: bool = True,
) -> bool:
    """HEAD the object; returns True when it exists. Auth errors exit 3.

    When ``wait_for_publish`` (the T-1 cron case) a 404 is retried every 300s
    until 12:00 ET, because the vendor publishes yesterday's file around
    11:00. For any older date that retry is wrong: a 404 there means the file
    genuinely does not exist, and sleeping burns the whole window three times
    over per missing date, which is what made backfilling unusable. Callers
    pass ``wait_for_publish=False`` for historical dates so a 404 is an
    immediate, logged miss.
    """
    while True:
        try:
            s3.head_object(Bucket=bucket, Key=key)
            return True
        except ClientError as exc:
            if _is_auth_error(exc):
                if not _credentials_work(s3, bucket):
                    _creds_fail(f"{exc.response.get('Error', {}).get('Code')}: {exc}")
                # Keys are fine: this object is out of entitlement (dataset
                # above the tier, or a date before the history window).
                logger.log("flatfile_not_entitled", key=key)
                return False
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code not in ("404", "NoSuchKey", "NotFound"):
                raise
            if not wait_for_publish:
                logger.log("flatfile_absent", key=key)
                return False
            now = market_gate.now_et()
            if now.time() >= RETRY_UNTIL_ET:
                logger.log("flatfile_not_ready_giving_up", key=key, now=now.isoformat())
                return False
            logger.log("flatfile_not_ready", key=key, retry_in_s=RETRY_SLEEP_S)
            time.sleep(RETRY_SLEEP_S)
        except (EndpointConnectionError, NoCredentialsError) as exc:
            if isinstance(exc, NoCredentialsError):
                _creds_fail(str(exc))
            raise


def _download(s3: Any, bucket: str, key: str, dest: Path) -> tuple[int, str]:
    """Download object to ``dest``; returns (bytes, md5_hex)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    md5 = hashlib.md5()
    size = 0
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if _is_auth_error(exc):
            _creds_fail(f"{exc.response.get('Error', {}).get('Code')}: {exc}")
        raise
    body = obj["Body"]
    with open(dest, "wb") as fh:
        while True:
            chunk = body.read(CHUNK)
            if not chunk:
                break
            fh.write(chunk)
            md5.update(chunk)
            size += len(chunk)
    return size, md5.hexdigest()


def _gzip_test(path: Path) -> None:
    """Validate the gzip stream by decompressing it fully (raises on corruption)."""
    with gzip.open(path, "rb") as fh:
        while fh.read(CHUNK):
            pass


def _int_or_none(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    return int(float(value))


def _float_or_none(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def map_row(dataset: str, row: dict[str, str]) -> dict[str, Any]:
    """Map one flat-file CSV row to a clean-schema record (src='flatfile').

    Timestamps are stored exactly as delivered (ns epoch ints, UTC).
    """
    if dataset == "trades_v1":
        return {
            "ticker": row.get("ticker"),
            "price": _float_or_none(row.get("price")),
            "size": _int_or_none(row.get("size")),
            "exchange": _int_or_none(row.get("exchange")),
            "conditions": row.get("conditions") or None,
            "correction": _int_or_none(row.get("correction")),
            "trade_id": row.get("trade_id") or None,
            "sequence_number": _int_or_none(row.get("sequence_number")),
            "sip_timestamp_ns": _int_or_none(row.get("sip_timestamp")),
            "participant_timestamp_ns": _int_or_none(row.get("participant_timestamp")),
            "src": "flatfile",
        }
    # minute_aggs_v1 / day_aggs_v1 share columns
    return {
        "ticker": row.get("ticker"),
        "window_start_ns": _int_or_none(row.get("window_start")),
        "window_end_ns": None,
        "open": _float_or_none(row.get("open")),
        "high": _float_or_none(row.get("high")),
        "low": _float_or_none(row.get("low")),
        "close": _float_or_none(row.get("close")),
        "volume": _float_or_none(row.get("volume")),
        "vwap": _float_or_none(row.get("vwap")),
        "transactions": _int_or_none(row.get("transactions")),
        "src": "flatfile",
    }


def _filter_file(
    path: Path, dataset: str, limit: int | None
) -> tuple[list[dict[str, Any]], int, int]:
    """Stream-filter a csv.gz to SPY/SPX records; returns (records, rows_in, rows_kept).

    ``rows_in`` counts every data row read; ``rows_kept`` counts rows matching
    the ticker roots (``--limit`` only caps how many records are *returned*).
    """
    records: list[dict[str, Any]] = []
    rows_in = 0
    rows_kept = 0
    with gzip.open(path, "rt", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            rows_in += 1
            ticker = row.get("ticker") or ""
            if keep_ticker(ticker):
                rows_kept += 1
                if limit is None or rows_kept <= limit:
                    records.append(map_row(dataset, row))
    return records, rows_in, rows_kept


def _update_manifest(data_root: Path, entry: dict[str, Any]) -> Path:
    """Append/replace an entry in _meta/flatfile_manifest.json (keyed by dataset+date)."""
    path = landing.meta_path("flatfile_manifest.json", data_root)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        manifest = []
    manifest = [e for e in manifest
                if not (e.get("dataset") == entry["dataset"] and e.get("date") == entry["date"])]
    manifest.append(entry)
    manifest.sort(key=lambda e: (e.get("date", ""), e.get("dataset", "")))
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return path


def _pull_dataset(s3: Any, settings: Settings, dataset: str, d: date,
                  logger: JsonlLogger, args: Any) -> dict[str, Any] | None:
    """Pull one dataset for one date; returns the manifest entry or None."""
    bucket = settings.massive_s3_bucket
    key = s3_key(dataset, d)
    logger.log("flatfile_head", dataset=dataset, key=key)
    # Only wait for publication when this is the file the vendor is about to
    # publish (yesterday's). Older dates resolve a 404 immediately.
    wait_for_publish = d >= previous_trading_day(market_gate.today_et())
    if not _head_with_retry(s3, bucket, key, logger, wait_for_publish):
        return None

    dest = (Path(settings.data_root) / "raw" / "flatfiles" / dataset
            / f"dt={d.isoformat()}" / f"{d.isoformat()}.csv.gz")
    if args.dry_run:
        logger.log("flatfile_dry_run", dataset=dataset, key=key)
        return None
    size, md5 = _download(s3, bucket, key, dest)
    logger.log("flatfile_downloaded", dataset=dataset, path=str(dest), bytes=size, md5=md5)
    _gzip_test(dest)
    logger.log("flatfile_gzip_ok", dataset=dataset)

    records, rows_in, rows_kept = _filter_file(dest, dataset, args.limit)
    clean_path = landing.write_clean(
        CLEAN_DATASET[dataset], d, records, job=JOB, data_root=settings.data_root
    )
    logger.log("flatfile_clean_written", dataset=dataset, path=str(clean_path),
               rows_in=rows_in, rows_kept=rows_kept)

    entry = {
        "dataset": dataset,
        "date": d.isoformat(),
        "bytes": size,
        "rows_in": rows_in,
        "rows_kept": rows_kept,
        "md5": md5,
    }
    _update_manifest(Path(settings.data_root), entry)
    return entry


def _main(args: Any, settings: Settings, logger: JsonlLogger) -> dict[str, Any]:
    d = date.fromisoformat(args.date)  # always set: main() injects the default
    s3 = _s3_client(settings)
    entries = []
    for dataset in DATASETS:
        entry = _pull_dataset(s3, settings, dataset, d, logger, args)
        if entry is not None:
            entries.append(entry)
    return {
        "rows": sum(e["rows_kept"] for e in entries),
        "bytes": sum(e["bytes"] for e in entries),
        "datasets_ok": len(entries),
        "datasets_missing": len(DATASETS) - len(entries),
    }


def main(argv: list[str] | None = None) -> int:
    """Entry point; defaults --date to the previous trading day, then run_job."""
    argv = list(argv) if argv is not None else sys.argv[1:]
    if "--date" not in argv:
        # Resolve against DATA_ROOT without requiring full Settings (the
        # market gate only needs the holidays cache location).
        prev = previous_trading_day(market_gate.today_et())
        argv += ["--date", prev.isoformat()]
    return run_job(JOB, _main, argv)  # run_job exits; return is for tests


if __name__ == "__main__":
    sys.exit(main())
