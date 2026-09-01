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

A local copy whose md5 matches the manifest is reused instead of downloaded,
so re-filtering history after a roots change costs no bandwidth;
``--force-download`` overrides.

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

from ingest import schemas
from ingest.common import landing, market_gate
from ingest.common.cli import run_job
from ingest.common.config import Settings
from ingest.common.logging_utils import JsonlLogger
from ingest.common.s3 import s3_client
from ingest.jobs import OPTION_ROOTS, keep_ticker, strip_flag  # noqa: F401  (shared root filter)

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


def _manifest_md5(data_root: Path, dataset: str, d: date) -> str | None:
    """The md5 recorded for this dataset+date, if we have pulled it before."""
    path = landing.meta_path("flatfile_manifest.json", data_root)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, list):
        return None
    for entry in manifest:
        if (isinstance(entry, dict) and entry.get("dataset") == dataset
                and entry.get("date") == d.isoformat()):
            return entry.get("md5")
    return None


def _file_md5(path: Path) -> tuple[int, str]:
    """``(bytes, md5_hex)`` of a local file, read in chunks."""
    md5 = hashlib.md5()
    size = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            md5.update(chunk)
            size += len(chunk)
    return size, md5.hexdigest()


def reuse_local(
    dest: Path, data_root: Path, dataset: str, d: date
) -> tuple[int, str] | None:
    """``(bytes, md5)`` when the local copy is byte-identical to what we pulled.

    Re-filtering history -- after widening the ticker roots, say -- otherwise
    re-downloads tens of gigabytes to produce the same bytes we already hold.
    Reuse is gated on the manifest md5, so a truncated or half-written file is
    still fetched again rather than silently trusted.
    """
    if not dest.is_file():
        return None
    recorded = _manifest_md5(data_root, dataset, d)
    if not recorded:
        return None
    size, actual = _file_md5(dest)
    return (size, actual) if actual == recorded else None


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
    """Parse an integer field, without routing it through a float.

    ``int(float(v))`` silently corrupted nanosecond epochs: they need ~61 bits
    and float64 carries a 53-bit mantissa, so the low ~8 bits were rounded --
    measured at 75% of landed trade timestamps, off by up to 128ns. Bar
    timestamps survived only because they are multiples of 60e9 and so have
    11 trailing zero bits. The repo's own convention is that timestamps are
    stored exactly as delivered; this restores that.
    """
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        # Genuinely fractional (e.g. "1.0"); float is fine at these magnitudes.
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


_KEEP_RE = r"^O:(" + "|".join(OPTION_ROOTS) + r")\d{6}[CP]\d+$"


def _cast_int_column(col: Any, type_: Any) -> Any:
    """Cast a string column to an integer type the way ``_int_or_none`` does.

    A plain pyarrow cast rejects decimal spellings: ``"1.0"`` raises rather
    than yielding 1, which the row-at-a-time path accepts. The fix must not
    reintroduce the bug this branch exists to remove, so a trailing all-zero
    fraction is stripped *textually* -- no float ever touches the digits, and
    a 19-digit nanosecond epoch still lands exactly. Only a genuinely
    fractional value falls back to float truncation, matching
    ``int(float(v))``; those are small magnitudes where float64 is lossless.
    """
    import pyarrow as pa
    import pyarrow.compute as pc

    stripped = pc.replace_substring_regex(col, r"\.0*$", "")
    try:
        return stripped.cast(type_)
    except pa.ArrowInvalid:
        return pc.trunc(stripped.cast(pa.float64())).cast(type_)


def _filter_table(path: Path, dataset: str) -> tuple[Any, int, int]:
    """Filter a csv.gz to the allowlisted roots with pyarrow.

    Returns ``(table, rows_in, rows_kept)`` where ``table`` already matches
    ``SCHEMAS[CLEAN_DATASET[dataset]]``.

    The row-at-a-time csv.DictReader path spent ~140s parsing a 12.7M-row
    trades file, which made re-filtering history impractical (139h for the
    989 days on disk). Reading columnar and filtering with a regex does the
    same work in ~13s. Semantics are unchanged: every column is read as a
    string and cast explicitly, so an empty field becomes null exactly as
    ``_float_or_none`` / ``_int_or_none`` did, and a column absent from the
    file is filled with nulls rather than guessed at.
    """
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.csv as pacsv

    clean = CLEAN_DATASET[dataset]
    schema = schemas.SCHEMAS[clean]

    with gzip.open(path, "rb") as fh:
        raw = pacsv.read_csv(
            fh,
            read_options=pacsv.ReadOptions(block_size=1 << 24),
            # Everything as string, then cast -- pyarrow's own inference would
            # otherwise decide types per file and drift between days.
            convert_options=pacsv.ConvertOptions(
                column_types=dict.fromkeys(raw_columns(path), pa.string())
            ),
        )
    rows_in = raw.num_rows
    kept = raw.filter(pc.match_substring_regex(raw.column("ticker"), _KEEP_RE))
    rows_kept = kept.num_rows

    # Source column for each schema field; None means "not in the flat file".
    src_col = {
        "sip_timestamp_ns": "sip_timestamp",
        "participant_timestamp_ns": "participant_timestamp",
        "window_start_ns": "window_start",
    }
    arrays = []
    for field in schema:
        if field.name == "src":
            arrays.append(pa.array(["flatfile"] * rows_kept, type=pa.string()))
            continue
        name = src_col.get(field.name, field.name)
        if name not in kept.column_names:
            arrays.append(pa.nulls(rows_kept, type=field.type))
            continue
        col = kept.column(name)
        # "" -> null, matching the row-at-a-time behaviour for every type.
        col = pc.if_else(pc.equal(col, ""), pa.nulls(rows_kept, pa.string()), col)
        if field.type == pa.string():
            arrays.append(col)
        elif pa.types.is_integer(field.type):
            arrays.append(_cast_int_column(col, field.type))
        else:
            arrays.append(col.cast(field.type))
    return pa.Table.from_arrays(arrays, schema=schema), rows_in, rows_kept


def raw_columns(path: Path) -> list[str]:
    """Header names of a gzipped CSV."""
    with gzip.open(path, "rt", newline="", encoding="utf-8") as fh:
        return next(csv.reader(fh))


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
    dest = (Path(settings.data_root) / "raw" / "flatfiles" / dataset
            / f"dt={d.isoformat()}" / f"{d.isoformat()}.csv.gz")

    # Reuse is decided BEFORE touching S3. A re-filter of bytes already on
    # disk must not depend on the vendor being reachable, the credentials
    # being current, or the object still existing -- HEADing first would make
    # a local, manifest-verified rewrite fail for reasons that have nothing
    # to do with it.
    reused = None if getattr(args, "force_download", False) else reuse_local(
        dest, Path(settings.data_root), dataset, d
    )

    if reused is None:
        logger.log("flatfile_head", dataset=dataset, key=key)
        # Only wait for publication when this is the file the vendor is about
        # to publish (yesterday's). Older dates resolve a 404 immediately.
        wait_for_publish = d >= previous_trading_day(market_gate.today_et())
        if not _head_with_retry(s3, bucket, key, logger, wait_for_publish):
            return None
        if args.dry_run:
            logger.log("flatfile_dry_run", dataset=dataset, key=key)
            return None
        size, md5 = _download(s3, bucket, key, dest)
        logger.log("flatfile_downloaded", dataset=dataset, path=str(dest),
                   bytes=size, md5=md5)
        _gzip_test(dest)
        logger.log("flatfile_gzip_ok", dataset=dataset)
    else:
        if args.dry_run:
            logger.log("flatfile_dry_run", dataset=dataset, key=key, reuse=True)
            return None
        size, md5 = reused
        logger.log("flatfile_reused", dataset=dataset, path=str(dest),
                   bytes=size, md5=md5)

    table, rows_in, rows_kept = _filter_table(dest, dataset)
    if args.limit is not None:
        table = table.slice(0, args.limit)

    clean = CLEAN_DATASET[dataset]
    # Snapshot what is already here, write, and only then move the old files
    # aside. Quarantining first would empty the partition if the write below
    # failed -- and during a long refilter the likely cause of that failure,
    # a full disk, is exactly when losing the only copy hurts most.
    prior = (landing.clean_files(clean, d, JOB, settings.data_root)
             if getattr(args, "replace", False) else [])
    clean_path = landing.write_clean_table(
        clean, d, table, job=JOB, data_root=settings.data_root
    )
    if prior:
        moved = landing.quarantine_prior(clean, d, JOB, settings.data_root, only=prior)
        if moved:
            logger.log("flatfile_prior_quarantined", dataset=dataset,
                       files=len(moved), to=str(moved[0].parent))
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
    argv, force_download = strip_flag(argv, "--force-download")
    argv, replace = strip_flag(argv, "--replace")
    if "--date" not in argv:
        # Resolve against DATA_ROOT without requiring full Settings (the
        # market gate only needs the holidays cache location).
        prev = previous_trading_day(market_gate.today_et())
        argv += ["--date", prev.isoformat()]
    def main_fn(a, st, log):
        a.force_download = force_download
        a.replace = replace
        return _main(a, st, log)

    return run_job(JOB, main_fn, argv)  # run_job exits; return is for tests


if __name__ == "__main__":
    sys.exit(main())
