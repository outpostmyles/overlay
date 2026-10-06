"""Weekend lotto parlays: only sensible legs reach the pool (favorites, no open questions, this weekend),
the research legs are marked, every running board feeds it, and a cross-sport ticket can be logged."""
import importlib
import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from backend import config, lotto

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def _game(dedup, a, b, pa, ko="2026-10-11T17:00Z", uncertain=(), dk=None, top=None, legs=()):
    return {"dedup": dedup, "team_a": a, "team_b": b, "date": ko[:10], "kickoff_iso": ko,
            "market": (pa, 0.0, round(1 - pa, 4)), "legs": list(legs),
            "research": {"ml": {a: pa, b: round(1 - pa, 4)}, "uncertain": list(uncertain), "cushion": {"game": 0.02},
                         "top": top, "dk": dk}}


def _boards():
    return {
        "nfl": {"ts": time.time(), "names": {"kansas city chiefs": "Kansas City Chiefs"}, "games": [
            _game("g1", "kansas city chiefs", "las vegas raiders", 0.82, top="kansas city chiefs",
                  dk={"book": "DraftKings", "ml": {"kansas city chiefs": -400}, "ev": {"kansas city chiefs": 0.025}}),
            _game("g2", "buffalo bills", "new york jets", 0.55),                                   # coin flip: out
            _game("g3", "detroit lions", "chicago bears", 0.75, uncertain=["qb"]),                # open question
            _game("g4", "green bay packers", "minnesota vikings", 0.68, ko="2026-10-18T17:00Z"),  # next weekend
            _game("g5", "new england patriots", "miami dolphins", 0.64, ko="2026-10-09T11:00Z"),  # already started
            _game("g6", "seattle seahawks", "arizona cardinals", 0.30,                            # Arizona 70%
                  legs=[{"key": "total_goals", "side": "over", "line": 44.5, "prob": 0.52, "research_prob": 0.495}]),
        ]},
        "cfb": {"ts": time.time(), "names": {"texas a and m": "Texas A&M"}, "games": [
            _game("c1", "texas a and m", "arkansas", 0.91),
            _game("c2", "notre dame", "stanford", 0.97, uncertain=["no_report"])]},          # kept, labeled
    }


def test_the_pool_keeps_only_sensible_legs_and_marks_the_research():
    legs = lotto.candidates(_boards(), now=NOW)
    got = {(l["dedup"], l["kind"]): l for l in legs}
    assert set(got) == {("g1", "ml"), ("g6", "ml"), ("g6", "total"), ("c1", "ml"), ("c2", "ml")}
    assert [l["dedup"] for l in legs][:2] == ["c2", "c1"]              # most likely first
    assert got[("c2", "ml")]["flags"] == ["heavy", "no_report"]
    chiefs = got[("g1", "ml")]
    assert chiefs["label"] == "Kansas City Chiefs ML" and set(chiefs["flags"]) == {"heavy", "value", "top"}
    assert chiefs["dk"] == -400
    assert got[("g6", "ml")]["team"] == "arizona cardinals" and got[("g6", "ml")]["p"] == 0.7
    wind = got[("g6", "total")]
    assert (wind["dir"], wind["p"], wind["flags"], wind["label"]) == ("under", 0.505, ["research"], "Under 44.5")
    assert got[("c1", "ml")]["label"] == "Texas A&M ML" and got[("c1", "ml")]["sport"] == "cfb"


def test_the_window_always_covers_the_whole_coming_weekend():
    tue = datetime(2026, 10, 6, 9, tzinfo=timezone.utc)                   # Tuesday morning
    assert lotto.weekend_end(tue) == datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
    sat = datetime(2026, 10, 10, 18, tzinfo=timezone.utc)
    assert lotto.weekend_end(sat) == datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
    sun = datetime(2026, 10, 11, 15, tzinfo=timezone.utc)                  # Sunday's games stay in
    assert lotto.weekend_end(sun) == datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
    mon = datetime(2026, 10, 12, 20, tzinfo=timezone.utc)                  # Monday night's game is still ahead
    assert lotto.weekend_end(mon) == datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
    late = datetime(2026, 10, 13, 2, tzinfo=timezone.utc)                  # it is underway: next weekend
    assert lotto.weekend_end(late) == datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    assert lotto.freeze_at(datetime(2026, 10, 13, 12, tzinfo=timezone.utc)) == datetime(2026, 10, 10, 15, tzinfo=timezone.utc)


def test_boards_share_their_games_through_fresh_files(monkeypatch):
    from backend.store import livelegs
    root = Path(tempfile.mkdtemp())
    monkeypatch.setattr(config, "ROOT", root)
    live = [{"sport": "nfl", "dedup": "g1", "team_a": "a", "team_b": "b", "date": "2026-10-11",
             "kickoff_iso": "2026-10-11T17:00Z", "market": (0.8, 0.0, 0.2),
             "legs": [{"key": "spread", "team": "a", "line": 7.5, "side": "cover", "prob": 0.5},
                      {"key": "player_prop", "stat": "receptions", "prob": 0.5}],
             "research": {"ml": {"a": 0.8, "b": 0.2}, "uncertain": [], "cushion": {"game": 0.02},
                          "factors": [{"key": "top_traders", "target": {"kind": "ml", "team": "a"}}], "dk": None}}]
    livelegs.write("nfl", live, {"a": "Team A"})
    boards = livelegs.read_all()
    g = boards["nfl"]["games"][0]
    assert boards["nfl"]["names"] == {"a": "Team A"} and g["research"]["top"] == "a"
    assert [leg["key"] for leg in g["legs"]] == ["spread"]                # props stay home
    stale = json.loads((root / "poly_live_nfl.json").read_text())
    stale["ts"] = time.time() - 3 * 3600
    (root / "poly_live_cfb.json").write_text(json.dumps({**stale, "sport": "cfb"}))
    assert set(livelegs.read_all()) == {"nfl"}                            # a board that stopped publishing drops out
    assert set(livelegs.games_by_dedup(livelegs.read_all())) == {"g1"}


def test_a_cross_sport_ticket_logs_as_multi_and_shows_on_every_board(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "nfl")
    config.DB_PATH = tempfile.mktemp(suffix=".db")
    from backend.store import mybets, paper
    importlib.reload(paper)
    importlib.reload(mybets)
    paper.init_paper()
    mybets.init()
    games = {g["dedup"]: {**g, "sport": s} for s, b in _boards().items() for g in b["games"]}
    bet = mybets.log_bet({"legs": [{"dedup": "g1", "kind": "ml", "team": "kansas city chiefs"},
                                   {"dedup": "c1", "kind": "ml", "team": "texas a and m"}],
                          "price": 150, "stake": 2, "book": "FanDuel"}, games)
    assert bet["sport"] == "multi" and {l["sport"] for l in bet["legs"]} == {"nfl", "cfb"}
    monkeypatch.setattr(config, "SPORT", "cfb")
    assert [b["id"] for b in mybets.list_bets()] == [bet["id"]]           # the college board sees it too
    single = mybets.log_bet({"legs": [{"dedup": "g1", "kind": "ml", "team": "kansas city chiefs"}],
                             "price": -400, "stake": 3}, games)
    assert single["sport"] == "nfl"
    assert [b["id"] for b in mybets.list_bets()] == [bet["id"]]           # an NFL-only bet stays on the NFL board


# --- payout-target tickets ------------------------------------------------------------------------- #
def _pool(ps, flags=None):
    return [{"sport": "cfb", "dedup": f"g{i}", "kind": "ml", "team": f"t{i}", "p": p, "label": f"T{i} ML",
             "game": f"T{i} v X", "kickoff_iso": f"2026-10-10T{10 + i % 10:02d}:00Z", "flags": (flags or {}).get(i, []),
             "dk": -150} for i, p in enumerate(ps)]


def test_a_ticket_reaches_its_payout_with_the_most_likely_legs_it_can():
    ps = [0.99, 0.98, 0.95, 0.9, 0.88, 0.86, 0.85, 0.83, 0.82, 0.8, 0.8, 0.79, 0.78, 0.77, 0.76, 0.76, 0.75,
          0.74, 0.72, 0.71, 0.7, 0.69, 0.68, 0.67, 0.66, 0.65, 0.64, 0.63, 0.62, 0.61, 0.6, 0.6]
    t = lotto.build_ticket(_pool(ps), 2, 1000)
    assert t["reached"] and len(t["legs"]) == 20 and t["fair_payout"] >= 1000
    assert min(l["p"] for l in t["legs"]) >= 0.6 and len({l["dedup"] for l in t["legs"]}) == 20
    # the window slid only as far as the target needed: one step safer (the favorite just above it in,
    # its riskiest leg out) would no longer pay $1,000
    window = sorted(t["legs"], key=lambda l: -l["p"])
    top_i = ps.index(window[0]["p"])
    assert top_i > 0 and 2 / (t["p"] / window[-1]["p"] * ps[top_i - 1]) < 1000
    assert [l["kickoff_iso"] for l in t["legs"]] == sorted(l["kickoff_iso"] for l in t["legs"])
    assert t["dk_payout"] is not None                                     # every leg priced at DraftKings
    big = lotto.build_ticket(_pool(ps), 2, 50000)
    assert not big["reached"] and len(big["legs"]) == 20                   # the biggest sensible ticket instead


def test_the_research_construction_opens_with_the_flagged_legs():
    ps = [0.95, 0.9, 0.85, 0.8, 0.75, 0.7, 0.68, 0.66, 0.64, 0.62, 0.61, 0.6] * 3
    pool = _pool(ps, flags={5: ["value"], 9: ["top"]})
    t = lotto.build_ticket(pool, 2, 1000, "research")
    assert {"g5", "g9"} <= {l["dedup"] for l in t["legs"]}
    assert t["variant"] == "research" and t["reached"]
    assert len(lotto.tickets(pool)) == len(lotto.STAKES) * len(lotto.TARGETS) * len(lotto.VARIANTS)


# --- the record ------------------------------------------------------------------------------------- #
def test_weekend_tickets_freeze_once_and_grade_every_leg(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "cfb")
    config.DB_PATH = tempfile.mktemp(suffix=".db")
    from backend.store import lottotrack, paper
    importlib.reload(paper)
    importlib.reload(lottotrack)
    paper.init_paper()
    lottotrack.init()
    legs = [{"sport": "cfb", "dedup": "fc|2026-10-10|a|b", "kind": "ml", "team": "a", "p": 0.8, "flags": ["heavy"],
             "label": "A ML", "game": "A v B", "kickoff_iso": "2026-10-10T16:00Z"},
            {"sport": "nfl", "dedup": "fc|2026-10-11|c|d", "kind": "ml", "team": "c", "p": 0.7, "flags": [],
             "label": "C ML", "game": "C v D", "kickoff_iso": "2026-10-11T17:00Z"},
            {"sport": "nfl", "dedup": "fc|2026-10-11|e|f", "kind": "total", "dir": "under", "line": 44.5, "p": 0.55,
             "flags": ["research"], "label": "Under 44.5", "game": "E v F", "kickoff_iso": "2026-10-11T17:00Z"}]
    ticket = {"variant": "research", "stake": 2, "target": 1000, "legs": legs, "p": 0.308, "fair_payout": 6.49,
              "reached": False}
    end, fz = datetime(2026, 10, 13, 12, tzinfo=timezone.utc), datetime(2026, 10, 10, 15, tzinfo=timezone.utc)
    assert lottotrack.maybe_freeze([ticket], end, fz, now=datetime(2026, 10, 9, 12, tzinfo=timezone.utc)) == 0
    assert lottotrack.maybe_freeze([ticket], end, fz, now=datetime(2026, 10, 10, 15, 5, tzinfo=timezone.utc)) == 1
    assert lottotrack.maybe_freeze([ticket], end, fz, now=datetime(2026, 10, 10, 16, tzinfo=timezone.utc)) == 0
    with paper._conn() as c:                                              # three finals, one each way
        for key, a, b, sa, sb in (("fc|2026-10-10|a|b", "a", "b", 31, 10), ("fc|2026-10-11|c|d", "c", "d", 13, 20),
                                  ("fc|2026-10-11|e|f", "e", "f", 17, 14)):
            c.execute("INSERT INTO forecasts (match, team_a, team_b, commence_time, logged_at, status, dedup_key, "
                      "market_a, market_draw, market_b, actual_a, actual_b, actual_outcome) "
                      "VALUES ('x',?,?,?,?, 'settled', ?, 0.5, 0, 0.5, ?, ?, ?)",
                      (a, b, key[3:13], "t", key, sa, sb, "a" if sa > sb else "b"))
    assert lottotrack.settle() == 1
    t = lottotrack.list_tickets()[0]
    assert (t["status"], t["legs_won"], t["legs_lost"]) == ("missed", 2, 1)
    assert [l["result"] for l in t["legs"]] == ["won", "lost", "won"]
    st = lottotrack.study()
    by = {r["key"]: r for r in st["legs"]}
    assert (by["all:all"]["n"], by["all:all"]["won"]) == (3, 2)
    assert by["sport:nfl"]["n"] == 2 and by["flag:research"]["won"] == 1 and by["band:70-80%"]["won"] == 0
    assert by["style:research"]["n"] == 3 and st["hits"] == 0 and st["closest"][0]["won"] == 2
    # a second ticket the same weekend riding the same three legs adds no new leg evidence
    lottotrack.freeze("2026-10-13", [{**ticket, "target": 2500}], "2026-10-10T15:06:00Z")
    lottotrack.settle()
    st = lottotrack.study()
    assert {r["key"]: r for r in st["legs"]}["all:all"]["n"] == 3 and st["tickets"] == 2
