#!/usr/bin/env python
"""Strip non-OPTION_ROOTS rows (SPXO, B1SPY) out of already-landed clean data.

From 2026-11-09 the vendor listed SPXO under SPX and B1SPY under SPY. Until
PR #91, trades_watchlist landed SPXO prints in ``option_trades`` and
snapshot_sweep wrote both roots into ``option_snapshots``. The snapshot
contamination also reached ``forwards``: parity pairs calls and puts by
(expiry, strike) without looking at the root, so an SPXO quote could stand in
for an SPX/SPXW one (one sampled 2026-11-18 SPX forward was off by 25 points),
and SPXO-only expiries got forwards of their own.

What this does, per date in [--start, --end]:

* ``option_trades`` / ``option_snapshots``: every file holding a ticker that
  fails ``keep_ticker`` is rewritten without those rows.
* ``forwards``: the forwards file written alongside each contaminated
  snapshot file is recomputed from the filtered rows. Before trusting the
  recompute, the *unfiltered* rows must reproduce the stored file exactly
  (same expiries, same forwards); if they don't, the pair is left untouched
  and reported as unverified.

Nothing is deleted. Each original is hard-linked into
``_quarantine/foreign-roots/<dataset>/dt=<day>/`` and then atomically
replaced by its filtered version under the *same filename* -- readers parse
the trailing stamp (coverage_audit._sweep_stamps, catalog.files_by_underlying),
so names must not change. A file left with zero rows is moved to quarantine
and not replaced. Every move is recorded in a JSONL receipt.

Dry run by default; ``--apply`` makes changes and refuses to run on a weekday
between 09:00 and 17:00 ET, when snapshot_sweep and trades_watchlist write to
these same partitions.

    venv/bin/python scripts/purge_foreign_roots.py                 # dry run
    venv/bin/python scripts/purge_foreign_roots.py --apply

Derived datasets (vol_surface, atm_term_structure) read forwards, so after an
--apply that rewrote forwards the script prints the rebuild commands for the
affected range; it does not run them.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ingest.common.config import Settings  # noqa: E402
from ingest.common.rates import RateCurveError, load_curve  # noqa: E402
from ingest.jobs import _OPTION_TICKER_RE, forward_from_parity  # noqa: E402

ET = ZoneInfo("America/New_York")
FIRST_BAD_DAY = date(2026, 11, 9)
ROW_DATASETS = ("option_trades", "option_snapshots")
QUARANTINE = Path("_quarantine") / "foreign-roots"
# Parity inputs must match exactly; the forward only to this, because the
# rate curve has been revised since the sweep (a few bp at the 5y end moves
# a 2031 forward by ~1.5e-4).
FWD_REL_TOL = 1e-3
# The only fields forward_from_parity reads.
FWD_COLS = ("ticker", "day_close", "day_last_updated_ns", "details_contract_type",
            "details_expiration_date", "details_strike_price", "underlying_ticker")


def _days(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _split_name(path: Path) -> tuple[str, int]:
    """``snapshot_sweep-eod-I:SPX-1795010714703.parquet`` -> (label, stamp)."""
    label, _, stamp = path.stem.rpartition("-")
    return label, int(stamp)


def _prefix(ticker: str | None) -> str:
    t = str(ticker or "")
    return t[2:7] if t.startswith("O:") else t[:5] or "?"


def _keep_mask(table: pa.Table) -> pa.Array:
    return pc.fill_null(
        pc.match_substring_regex(table.column("ticker"), _OPTION_TICKER_RE.pattern), False)


def _forwards(rows: list[dict], run_date: date, rate_fn) -> dict[str, dict]:
    return {f["expiration_date"]: f
            for f in forward_from_parity(rows, rate_for_expiry=rate_fn, asof_date=run_date)}


def _inputs(rec: dict) -> tuple:
    """What parity chose for an expiry -- independent of the rate curve."""
    return (rec["atm_strike"], rec["pairs"], rec["call_price"], rec["put_price"])


def _reproduces(recomputed: dict[str, dict], stored: dict[str, dict]) -> bool:
    """Does today's recompute of the unfiltered rows match what was stored?

    Parity's choices (expiries, ATM strike, pair count, prices) must match
    exactly. The forward itself only to FWD_REL_TOL: the sweep used the rate
    curve as it stood that day, and the warehouse has been revised since.
    """
    if recomputed.keys() != stored.keys():
        return False
    for k, s in stored.items():
        r = recomputed[k]
        if _inputs(r) != _inputs(s):
            return False
        if abs(r["forward"] - s["forward"]) > FWD_REL_TOL * abs(s["forward"]):
            return False
    return True


def _pair_forward(snap: Path, fwd_files: dict[str, list[tuple[int, Path]]],
                  snap_stamps: dict[str, list[int]]) -> Path | None:
    """The forwards file the sweep wrote right after ``snap``.

    Same label, the first stamp at or after the snapshot's, and before that
    label's next snapshot -- so a sweep that wrote no forwards is not matched
    to the following sweep's file.
    """
    label, stamp = _split_name(snap)
    later = [s for s in snap_stamps.get(label, []) if s > stamp]
    upper = min(later) if later else None
    for st, f in fwd_files.get(label, []):          # sorted by stamp
        if st >= stamp and (upper is None or st < upper):
            return f
    return None


class Run:
    def __init__(self, root: Path, apply: bool, receipt: Path | None) -> None:
        self.root, self.apply, self.receipt = root, apply, receipt
        self.stats = collections.defaultdict(lambda: {"files": 0, "rows": 0, "emptied": 0,
                                                     "days": set()})
        self.dropped: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
        self.fwd_notes: list[str] = []
        self.unverified: list[dict] = []
        self.forward_days: set[str] = set()
        self.pairs_only = 0

    def change(self, dataset: str, path: Path, new_table: pa.Table | None,
               rows_before: int, dropped: dict, why: str) -> None:
        rows_after = 0 if new_table is None else new_table.num_rows
        st = self.stats[dataset]
        st["files"] += 1
        st["rows"] += rows_before - rows_after
        st["emptied"] += new_table is None
        st["days"].add(path.parent.name[3:])
        if dataset != "forwards":
            self.dropped[dataset].update(dropped)
        if not self.apply:
            return
        tmp = path.with_name(f".{path.name}.purge-tmp")
        if new_table is not None:
            pq.write_table(new_table, tmp)
            check = pq.read_table(tmp)
            bad = check.num_rows != rows_after or (
                "ticker" in check.column_names
                and not pc.all(_keep_mask(check)).as_py())
            if bad:
                tmp.unlink()
                raise RuntimeError(f"verification failed writing {tmp}; {path} untouched")
        target = _quarantine_target(self.root, dataset, path)
        os.link(path, target)                       # original now safe in quarantine
        if new_table is not None:
            os.replace(tmp, path)                   # atomic swap, same name
        else:
            path.unlink()
        with self.receipt.open("a") as out:
            out.write(json.dumps({
                "ts": datetime.now(ET).isoformat(), "dataset": dataset,
                "path": str(path), "quarantined": str(target),
                "rows_before": rows_before, "rows_after": rows_after,
                "dropped": dropped, "why": why,
            }) + "\n")


def _rate_fn(root: Path, day: date):
    try:
        curve = load_curve(day, data_root=root)
    except RateCurveError:
        return None
    return lambda e: curve.at(max((e - day).days, 1) / 365.0)


def process_day(run: Run, day: date) -> None:
    dt = day.isoformat()
    clean = run.root / "clean"

    part = clean / "option_trades" / f"dt={dt}"
    for f in sorted(part.glob("*.parquet")) if part.is_dir() else []:
        table = pq.read_table(f)
        mask = _keep_mask(table)
        if pc.all(mask).as_py():
            continue
        gone = table.filter(pc.invert(mask)).column("ticker").to_pylist()
        kept = table.filter(mask)
        run.change("option_trades", f, kept if kept.num_rows else None, table.num_rows,
                   dict(collections.Counter(map(_prefix, gone))), "foreign roots")

    part = clean / "option_snapshots" / f"dt={dt}"
    if not part.is_dir():
        return
    files = sorted(part.glob("*.parquet"))
    snap_stamps: dict[str, list[int]] = collections.defaultdict(list)
    for f in files:
        lab, st = _split_name(f)
        snap_stamps[lab].append(st)
    fwd_files: dict[str, list[tuple[int, Path]]] = collections.defaultdict(list)
    fdir = clean / "forwards" / f"dt={dt}"
    for f in fdir.glob("*.parquet") if fdir.is_dir() else []:
        lab, st = _split_name(f)
        fwd_files[lab].append((st, f))
    for v in fwd_files.values():
        v.sort()
    rate_fn = _rate_fn(run.root, day)

    for f in files:
        table = pq.read_table(f)
        mask = _keep_mask(table)
        if pc.all(mask).as_py():
            continue
        gone = table.filter(pc.invert(mask)).column("ticker").to_pylist()
        kept = table.filter(mask)

        # Decide the forwards file before touching the snapshot, from the same
        # in-memory table, so the result does not depend on write order.
        fwd_action = None
        fwd = _pair_forward(f, fwd_files, snap_stamps)
        cols = [c for c in FWD_COLS if c in table.column_names]
        before = _forwards(table.select(cols).to_pylist(), day, rate_fn)
        if fwd is None:
            if before:
                run.unverified.append({"snapshot": str(f), "reason": "no paired forwards file"})
        else:
            stored_tbl = pq.read_table(fwd)
            stored_rows = stored_tbl.to_pylist()
            stored = {r["expiration_date"]: r for r in stored_rows}
            if not _reproduces(before, stored):
                run.unverified.append({"snapshot": f.name, "forwards": fwd.name, "dt": dt,
                                       "reason": "unfiltered recompute does not reproduce "
                                                 "the stored forwards"})
            else:
                after = _forwards(kept.select(cols).to_pylist(), day, rate_fn)
                # Rewrite only expiries whose parity inputs changed; every
                # other row is kept exactly as stored (original rate curve).
                diff: dict[str, object] = {}
                new_rows = []
                for row in stored_rows:
                    k = row["expiration_date"]
                    if k not in after:
                        diff[k] = "removed"
                    elif _inputs(after[k])[0::2] == _inputs(row)[0::2] and \
                            after[k]["put_price"] == row["put_price"]:
                        # Same ATM strike and prices, fewer pairs: the forward
                        # is unchanged, so keep the stored value and only
                        # correct the count.
                        if after[k]["pairs"] != row["pairs"]:
                            new_rows.append({**row, "pairs": after[k]["pairs"]})
                            diff.setdefault("_pairs_only", 0)
                            diff["_pairs_only"] += 1  # type: ignore[operator]
                        else:
                            new_rows.append(row)
                    elif _inputs(after[k]) != _inputs(row):
                        diff[k] = round(after[k]["forward"] - row["forward"], 4)
                        new_rows.append(after[k])
                    else:
                        new_rows.append(row)
                if diff:
                    new_tbl = (pa.Table.from_pylist(new_rows, schema=stored_tbl.schema)
                               if new_rows else None)
                    fwd_action = (fwd, new_tbl, stored_tbl.num_rows, diff)

        run.change("option_snapshots", f, kept if kept.num_rows else None, table.num_rows,
                   dict(collections.Counter(map(_prefix, gone))), "foreign roots")
        if fwd_action:
            fwd, new_tbl, n_before, diff = fwd_action
            run.change("forwards", fwd, new_tbl, n_before,
                       {"expiries_changed": len(diff)}, f"recomputed without foreign roots: {diff}")
            real = {k: v for k, v in diff.items() if k != "_pairs_only"}
            if real:
                run.fwd_notes.append(f"{dt} {fwd.name}: {real}")
                run.forward_days.add(dt)
            else:
                run.pairs_only += 1


def _quarantine_target(root: Path, dataset: str, path: Path) -> Path:
    dest = root / QUARANTINE / dataset / path.parent.name
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / path.name
    n = 1
    while target.exists():
        target = dest / f"{path.stem}.{n}{path.suffix}"
        n += 1
    return target


def report(run: Run) -> None:
    for ds in ("option_trades", "option_snapshots", "forwards"):
        st = run.stats.get(ds, {"files": 0, "rows": 0, "emptied": 0, "days": set()})
        days = st["days"]
        span = f"{min(days)}..{max(days)}" if days else "-"
        print(f"{ds:17} files={st['files']:5}  rows_removed={st['rows']:10,}  "
              f"emptied={st['emptied']:3}  dates={span}")
        if run.dropped.get(ds):
            print(f"{'':17} by prefix: {dict(run.dropped[ds].most_common())}")
    print(f"  forwards files with only a pair-count correction: {run.pairs_only}")
    print(f"  forwards files with a changed or removed forward: {len(run.fwd_notes)}")
    for note in run.fwd_notes[:15]:
        print(f"  forwards {note}")
    if len(run.fwd_notes) > 15:
        print(f"  ... {len(run.fwd_notes) - 15} more forwards files")
    if run.unverified:
        print(f"UNVERIFIED forwards (left untouched): {len(run.unverified)}")
        for u in run.unverified[:10]:
            print("  ", json.dumps(u))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--start", default=FIRST_BAD_DAY.isoformat())
    p.add_argument("--end", default=date.today().isoformat())
    p.add_argument("--apply", action="store_true", help="make the changes (default: dry run)")
    p.add_argument("--data-root", help="override DATA_ROOT")
    args = p.parse_args(argv)

    root = Path(args.data_root) if args.data_root else Path(Settings.load().data_root)
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if start > end:
        p.error("--start is after --end")

    now = datetime.now(ET)
    if args.apply and now.weekday() < 5 and 9 <= now.hour < 17:
        print(f"refusing --apply at {now:%a %H:%M} ET: sweeps write these partitions "
              "09:00-17:00 on weekdays", file=sys.stderr)
        return 2

    receipt = None
    if args.apply:
        receipt = root / QUARANTINE / f"receipt-{now:%Y%m%dT%H%M%S}.jsonl"
        receipt.parent.mkdir(parents=True, exist_ok=True)
    run = Run(root, args.apply, receipt)
    print(f"{'APPLY' if args.apply else 'DRY RUN'}  {start}..{end}  root={root}", flush=True)
    for day in _days(start, end):
        process_day(run, day)
        print(f"  {day} scanned", file=sys.stderr, flush=True)
    report(run)
    if not args.apply:
        print("dry run: nothing changed. Re-run with --apply.")
        return 0
    print(f"done; receipt {receipt}")
    if run.forward_days:
        lo, hi = min(run.forward_days), max(run.forward_days)
        print("forwards changed; rebuild derived data for that range:")
        print(f"  venv/bin/python scripts/build_surface.py --start {lo} --end {hi} --force")
        print(f"  venv/bin/python scripts/build_term_structure.py --start {lo} --end {hi} --force")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
