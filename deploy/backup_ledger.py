"""Nightly ledger backup: a consistent copy of poly.db, checked, compressed, and pruned.

Every graded game lives in one SQLite file on one server, so this keeps dated copies beside it and the
owner's Mac pulls them off the server every day (deploy/com.overlay.backup-pull.plist). It uses SQLite's
online backup, which is safe while the boards keep writing, then runs an integrity check on the copy
before keeping it. It keeps the last 30 days, plus the first copy of each month for a year.

Run by overlay-backup.timer as the overlay user:
    /opt/overlay/.venv/bin/python /opt/overlay/deploy/backup_ledger.py
"""
from __future__ import annotations

import gzip
import re
import shutil
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "poly.db"
OUT = ROOT / "backups"
KEEP_DAYS = 30
KEEP_MONTHS = 12
_NAME = re.compile(r"^poly-(\d{4}-\d{2}-\d{2})\.db\.gz$")


def backup(db: Path = DB, out: Path = OUT, today: date | None = None) -> Path:
    """Copy, check and compress the ledger to out/poly-YYYY-MM-DD.db.gz; returns the file."""
    today = today or date.today()
    out.mkdir(exist_ok=True)
    tmp = out / f".poly-{today}.db"
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
        ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
        rows = dst.execute("SELECT COUNT(*) FROM forecasts").fetchone()[0]
    finally:
        src.close()
        dst.close()
    if ok != "ok":
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"integrity check failed on the copy: {ok}")
    final = out / f"poly-{today}.db.gz"
    with open(tmp, "rb") as f, gzip.open(final, "wb") as g:
        shutil.copyfileobj(f, g)
    tmp.unlink()
    print(f"[backup] {final.name}: {rows} forecast rows, {final.stat().st_size // 1024} KB")
    return final


def prune(out: Path = OUT, today: date | None = None) -> list[Path]:
    """Delete copies older than KEEP_DAYS, except each month's first copy within KEEP_MONTHS."""
    today = today or date.today()
    dated = sorted((date.fromisoformat(m.group(1)), p) for p in out.glob("poly-*.db.gz")
                   if (m := _NAME.match(p.name)))
    first_of_month: dict = {}
    for d, p in dated:
        first_of_month.setdefault((d.year, d.month), p)
    removed = []
    for d, p in dated:
        if d >= today - timedelta(days=KEEP_DAYS):
            continue
        if p in first_of_month.values() and d >= today - timedelta(days=31 * KEEP_MONTHS):
            continue
        p.unlink()
        removed.append(p)
    return removed


def main() -> int:
    try:
        backup()
    except Exception as exc:  # noqa: BLE001 (a failed night must show in the journal, then exit non-zero)
        print(f"[backup] FAILED: {exc}")
        return 1
    for p in prune():
        print(f"[backup] pruned {p.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
