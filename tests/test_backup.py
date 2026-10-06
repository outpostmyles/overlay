"""The nightly ledger backup: a checked, compressed copy, and a pruning rule that keeps a month of dailies
plus each month's first copy for a year."""
import gzip
import importlib.util
import sqlite3
import tempfile
from datetime import date, timedelta
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "backup_ledger", Path(__file__).resolve().parent.parent / "deploy" / "backup_ledger.py")
bk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bk)


def test_backup_copies_checks_and_compresses_the_ledger():
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "poly.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE forecasts (id INTEGER PRIMARY KEY, match TEXT)")
        c.executemany("INSERT INTO forecasts (match) VALUES (?)", [("a",), ("b",), ("c",)])
    out = bk.backup(db=db, out=tmp / "backups", today=date(2026, 10, 7))
    assert out.name == "poly-2026-10-07.db.gz"
    restored = tmp / "restored.db"
    restored.write_bytes(gzip.open(out).read())
    with sqlite3.connect(restored) as c:
        assert c.execute("SELECT COUNT(*) FROM forecasts").fetchone()[0] == 3
    assert not list((tmp / "backups").glob(".poly-*"))           # no half-written copy left behind


def test_prune_keeps_a_month_of_dailies_and_each_months_first():
    out = Path(tempfile.mkdtemp())
    today = date(2026, 10, 7)
    days = [today - timedelta(days=i) for i in range(0, 420, 3)]
    for d in days:
        (out / f"poly-{d}.db.gz").write_bytes(b"x")
    (out / "notes.txt").write_text("not a backup")
    removed = {p.name for p in bk.prune(out=out, today=today)}
    kept = {p.name for p in out.glob("poly-*.db.gz")}
    assert f"poly-{today}.db.gz" in kept and f"poly-{today - timedelta(days=30)}.db.gz" in kept
    assert f"poly-{today - timedelta(days=33)}.db.gz" in removed     # an ordinary day past 30
    firsts = {}
    for d in sorted(days):
        firsts.setdefault((d.year, d.month), d)
    recent_firsts = [d for d in firsts.values() if d >= today - timedelta(days=31 * 12)]
    assert all(f"poly-{d}.db.gz" in kept for d in recent_firsts)
    assert (out / "notes.txt").exists()
