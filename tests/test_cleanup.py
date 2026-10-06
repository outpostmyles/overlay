"""The site clean-up: the weekend ticket starts at the Saturday freeze, the page gets lighter payloads (lotto
legs by reference, recent ledger cards with full tallies), a public Refresh cannot hammer the feeds, and
football-only text stays on the football boards."""
import asyncio
import importlib
import tempfile
import time
from datetime import datetime, timezone

from backend import aggregator, config, lotto, research


def _board(games):
    return {"nfl": {"ts": time.time(), "names": {}, "games": [
        {"dedup": d, "team_a": f"a{d}", "team_b": f"b{d}", "date": ko[:10], "kickoff_iso": ko,
         "market": (0.75, 0.0, 0.25), "legs": [],
         "research": {"ml": {f"a{d}": 0.75, f"b{d}": 0.25}, "uncertain": [], "cushion": {"game": 0.02}}}
        for d, ko in games]}}


def test_the_weekend_ticket_starts_at_the_saturday_freeze():
    tue = datetime(2026, 10, 6, 20, tzinfo=timezone.utc)
    legs = lotto.candidates(_board([("tue", "2026-10-06T23:00Z"), ("thu", "2026-10-09T00:15Z"),
                                    ("fri", "2026-10-10T00:00Z"), ("sat", "2026-10-10T16:00Z"),
                                    ("mon", "2026-10-13T00:15Z")]), now=tue)
    assert {l["dedup"] for l in legs} == {"sat", "mon"}          # the freeze is Saturday 15:00 UTC
    sat = datetime(2026, 10, 10, 18, tzinfo=timezone.utc)
    later = lotto.candidates(_board([("sat", "2026-10-10T16:00Z"), ("sun", "2026-10-11T17:00Z")]), now=sat)
    assert {l["dedup"] for l in later} == {"sun"}                 # a started game drops off


def test_tickets_name_their_legs_by_position_in_the_pool():
    pool = lotto.candidates(_board([(f"g{i}", f"2026-10-11T{10 + i}:00Z") for i in range(6)]),
                            now=datetime(2026, 10, 9, 12, tzinfo=timezone.utc))
    tickets = lotto.tickets(pool)
    slim = lotto.by_reference(tickets, pool)
    assert "legs" not in slim[0] and len(slim) == len(tickets)
    assert [pool[i]["dedup"] for i in slim[0]["leg_ids"]] == [l["dedup"] for l in tickets[0]["legs"]]


def test_the_ledger_ships_recent_cards_but_tallies_every_graded_game():
    def row(i, status="settled", result="won"):
        return {"id": i, "status": status, "commence_time": f"2026-07-{1 + i % 28:02d}", "legs_json": "[...]",
                "research_json": "{}", "legs": [{"key": "total_goals", "result": result}]}
    rows = [row(i, result="won" if i % 2 else "lost") for i in range(100)] + [row(500, status="locked")]
    out = aggregator._ledger_rows(rows)
    settled = [r for r in out["rows"] if r["status"] == "settled"]
    assert len(settled) == aggregator.LEDGER_SETTLED_SHOWN and out["settled_total"] == 100
    assert any(r["status"] == "locked" for r in out["rows"])         # every locked game stays
    assert all("legs_json" not in r and "research_json" not in r for r in out["rows"])
    assert out["leg_tally"]["total_goals"] == [50, 100]               # counted over all 100, not the 60 shown


def test_a_board_counts_only_its_own_paper_picks(monkeypatch):
    config.DB_PATH = tempfile.mktemp(suffix=".db")
    from backend.store import paper
    importlib.reload(paper)
    paper.init_paper()
    monkeypatch.setattr(config, "SPORT", "nfl")
    assert paper.count_picks() == 0


def test_a_public_refresh_within_a_minute_is_served_from_the_cache(monkeypatch):
    from backend import main
    seen = []

    async def fake_build(force=False, refresh_odds=False, reason=False):
        seen.append(force)
        return {"ok": True}
    monkeypatch.setattr(aggregator, "build_snapshot", fake_build)
    monkeypatch.setattr(aggregator, "free_age_seconds", lambda: 10.0)
    asyncio.run(main.snapshot(force=True))
    monkeypatch.setattr(aggregator, "free_age_seconds", lambda: 120.0)
    asyncio.run(main.snapshot(force=True))
    assert seen == [False, True]


def test_the_nfl_history_only_speaks_for_the_football_boards():
    nhl = {f["key"]: f for f in research.catalog("nhl")}
    nfl = {f["key"]: f for f in research.catalog("nfl")}
    assert nhl["fav_bias"]["history"] is None and nfl["fav_bias"]["history"]
