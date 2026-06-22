"""Track Record ordering: same-day games must sort by real kickoff time (ESPN), not alphabetically.
Picks store only the game DATE (Kalshi), so attach_kickoffs stamps the real time and falls back to
end-of-day when ESPN has no match — so a timed game always sorts before an unknown one."""
import asyncio

from backend import aggregator


def test_attach_kickoffs_orders_by_time_with_fallback(monkeypatch):
    async def fake_kicks(dates):
        return {frozenset({"spain", "saudi arabia"}): "2026-06-21T16:00Z",
                frozenset({"belgium", "ir iran"}): "2026-06-21T19:00Z"}

    monkeypatch.setattr(aggregator, "get_kickoffs", fake_kicks)
    picks = [
        {"match": "Belgium vs IR Iran", "commence_time": "2026-06-21"},
        {"match": "Spain vs Saudi Arabia", "commence_time": "2026-06-21"},
        {"match": "Mystery vs Unknown", "commence_time": "2026-06-21"},   # no ESPN match → end of day
    ]
    asyncio.run(aggregator.attach_kickoffs(picks))
    ko = {p["match"]: p["kickoff"] for p in picks}
    assert ko["Spain vs Saudi Arabia"] == "2026-06-21T16:00Z"
    assert ko["Belgium vs IR Iran"] == "2026-06-21T19:00Z"
    assert ko["Mystery vs Unknown"] == "2026-06-21T23:59:59"     # unknown → sorts last in its day
    # the ledger sorts on this key → Spain (16:00) before Belgium (19:00) before the unknown
    order = sorted(picks, key=lambda p: p["kickoff"])
    assert [p["match"] for p in order] == [
        "Spain vs Saudi Arabia", "Belgium vs IR Iran", "Mystery vs Unknown"]
