"""The kickoff cache must prune both halves on the ESPN scoreboard date, never on the kickoff's own
UTC value. ESPN files a slate under its US-local date, so a 21:40-ET first pitch carries the NEXT UTC
date; judging entries by that value evicted every late game at UTC midnight while its date stayed
marked "already fetched", so the kickoff was never re-asked for and the game could never lock."""
import asyncio
from datetime import datetime, timezone

from backend import aggregator

LATE = frozenset({"cincinnati reds", "seattle mariners"})     # 21:40 ET -> 2026-07-22T01:40Z
EARLY = frozenset({"cleveland guardians", "minnesota twins"})  # 18:40 ET -> 2026-07-21T22:40Z
SLATE = {LATE: "2026-07-22T01:40Z", (LATE, "2026-07-21"): "2026-07-22T01:40Z",
         EARLY: "2026-07-21T22:40Z", (EARLY, "2026-07-21"): "2026-07-21T22:40Z"}


def _freeze(monkeypatch, iso: str, mono: float, calls: list):
    """Pin aggregator's wall clock and monotonic clock, and record every ESPN scoreboard fetch."""
    now = datetime.fromisoformat(iso)

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now

    async def fake_fetch(client, dates):
        calls.append(sorted(dates))
        return dict(SLATE) if "20260721" in dates else {}

    monkeypatch.setattr(aggregator, "datetime", _DT)
    monkeypatch.setattr(aggregator.time, "monotonic", lambda: mono)
    monkeypatch.setattr(aggregator.espn, "fetch_kickoffs", fake_fetch)


def test_late_kickoff_survives_utc_midnight(monkeypatch):
    monkeypatch.setattr(aggregator, "_kickoff_cache", {"map": {}, "ts": 0.0, "dates": set()})
    calls: list = []

    # 23:00Z on 7/21: both games cached, keyed on the date we queried
    _freeze(monkeypatch, "2026-07-21T23:00:00+00:00", 1_000.0, calls)
    kicks = asyncio.run(aggregator.get_kickoffs(["20260721"]))
    assert kicks[(LATE, "2026-07-21")] == "2026-07-22T01:40Z"
    assert aggregator._kickoff_cache["dates"] == {"20260721"}

    # 00:30Z on 7/22, past the TTL: the prune runs with today=20260722. The late game's own UTC date is
    # now 20260722, but its scoreboard date is still strictly past, so it must NOT be evicted: its lock
    # window (kickoff - 75min) does not even open until 00:25Z.
    _freeze(monkeypatch, "2026-07-22T00:30:00+00:00",
            1_000.0 + aggregator.config.RESULTS_CACHE_TTL + 1, calls)
    kicks = asyncio.run(aggregator.get_kickoffs(["20260721"]))
    assert kicks[(LATE, "2026-07-21")] == "2026-07-22T01:40Z"
    assert kicks[(EARLY, "2026-07-21")] == "2026-07-21T22:40Z"
    assert calls == [["20260721"]]            # a strictly-past scoreboard date is fetched once ever


def test_all_late_slate_marks_its_queried_date_covered(monkeypatch):
    """`covered` is built from the queried dates: a West-Coast-only slate carries nothing but next-day
    UTC kickoffs, and must still count as fetched instead of being re-requested every refresh."""
    monkeypatch.setattr(aggregator, "_kickoff_cache", {"map": {}, "ts": 0.0, "dates": set()})
    calls: list = []
    _freeze(monkeypatch, "2026-07-21T23:00:00+00:00", 2_000.0, calls)
    monkeypatch.setattr(aggregator.espn, "fetch_kickoffs",
                        lambda client, dates: _only_late(dates, calls))

    asyncio.run(aggregator.get_kickoffs(["20260721"]))
    assert aggregator._kickoff_cache["dates"] == {"20260721"}
    asyncio.run(aggregator.get_kickoffs(["20260721"]))
    assert calls == [["20260721"]]            # already covered → no second scoreboard call


async def _only_late(dates, calls):
    calls.append(sorted(dates))
    return {LATE: "2026-07-22T01:40Z", (LATE, "2026-07-21"): "2026-07-22T01:40Z"}
