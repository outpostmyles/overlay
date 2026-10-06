"""Weekend lotto parlays: only sensible legs reach the pool (favorites, no open questions, this weekend),
the research legs are marked, every running board feeds it, and a cross-sport ticket can be logged."""
import importlib
import json
import math
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
    # a full-length lotto ticket loaded into the Bet Slip logs too (20 legs, the lotto's own limit)
    many = {f"x{i}": {"dedup": f"x{i}", "team_a": f"a{i}", "team_b": f"b{i}", "date": "2026-10-10",
                      "market": (0.7, 0.0, 0.3), "sport": "cfb"} for i in range(lotto.MAX_LEGS)}
    big = mybets.log_bet({"legs": [{"dedup": k, "kind": "ml", "team": g["team_a"]} for k, g in many.items()],
                          "price": 20000, "stake": 5}, many)
    assert len(big["legs"]) == lotto.MAX_LEGS and mybets.MAX_LEGS >= lotto.MAX_LEGS
    single = mybets.log_bet({"legs": [{"dedup": "g1", "kind": "ml", "team": "kansas city chiefs"}],
                             "price": -400, "stake": 3}, games)
    assert single["sport"] == "nfl"
    # an NFL-only bet stays on the NFL board (the college board sees the cross-sport and college tickets)
    assert {b["id"] for b in mybets.list_bets()} == {bet["id"], big["id"]}


# --- payout-target tickets ------------------------------------------------------------------------- #
def _priced(i, p, cost=0.06, flags=(), dedup=None):
    """A leg DraftKings prices at `cost` margin per unit of payout (about what its favorites carry)."""
    from backend.engine.odds_math import decimal_to_american
    dk = decimal_to_american((1 / p) ** (1 / (1 + cost)))
    return {"sport": "cfb", "dedup": dedup or f"g{i}", "kind": "ml", "team": f"t{i}", "p": p, "label": f"T{i} ML",
            "game": f"T{i} v X", "kickoff_iso": f"2026-10-10T{10 + i % 10:02d}:00Z", "flags": list(flags),
            "dk": dk, "cost": lotto.leg_cost(p, dk)}


def _pool(ps, flags=None):
    return [_priced(i, p, flags=(flags or {}).get(i, [])) for i, p in enumerate(ps)]


def test_a_ticket_reaches_its_payout_at_the_book_with_the_most_likely_legs_it_can():
    ps = [0.99, 0.98, 0.95, 0.9, 0.88, 0.86, 0.85, 0.83, 0.82, 0.8, 0.8, 0.79, 0.78, 0.77, 0.76, 0.76, 0.75,
          0.74, 0.72, 0.71, 0.7, 0.69, 0.68, 0.67, 0.66, 0.65, 0.64, 0.63, 0.62, 0.61, 0.6, 0.6]
    pool = _pool(ps)
    t = lotto.build_ticket(pool, 2, 1000)
    assert t["reached"] and len(t["legs"]) == 20 and t["dk_payout"] >= 1000
    assert t["fair_payout"] > t["dk_payout"]                                # the book's cut compounds
    assert t["dk_priced"] == 20 and t["dk_price"] > 40000                   # one price for the whole ticket
    assert min(l["p"] for l in t["legs"]) >= 0.6 and len({l["dedup"] for l in t["legs"]}) == 20
    # the window slid only as far as the target needed: one step safer (the favorite just above it in,
    # its riskiest leg out) would no longer pay $1,000 at the book
    dec = {l["dedup"]: lotto.book_decimal(l, 0) for l in pool}
    window = sorted(t["legs"], key=lambda l: -l["p"])
    top_i = ps.index(window[0]["p"])
    assert top_i > 0 and t["dk_payout"] / dec[window[-1]["dedup"]] * dec[pool[top_i - 1]["dedup"]] < 1000
    assert [l["kickoff_iso"] for l in t["legs"]] == sorted(l["kickoff_iso"] for l in t["legs"])
    big = lotto.build_ticket(pool, 2, 50000)
    assert not big["reached"] and len(big["legs"]) == 20                   # the biggest sensible ticket instead


def test_a_leg_without_a_book_price_is_estimated_at_the_weekends_typical_margin():
    pool = _pool([0.8, 0.75, 0.7])
    assert abs(lotto.typical_cost(pool) - 0.06) < 0.01
    bare = {**pool[0], "dedup": "bare", "dk": None, "cost": None}
    d = lotto.book_decimal(bare, 0.06)
    assert 1 / 0.8 > d > 1 and abs(-math.log(0.8) / math.log(d) - 1.06) < 1e-9
    t = lotto.build_ticket([bare] + pool[1:], 1, 1000)
    assert t["dk_priced"] == 2 and t["dk_price"] is None                   # no single price until all are priced
    assert lotto.typical_cost([]) == lotto.TYPICAL_COST
    # heavy favorites carry more margin per unit than moderate prices, so an estimate looks near its price
    mixed = [_priced(i, p, cost=0.15) for i, p in enumerate((0.88, 0.86, 0.84))] \
        + [_priced(10 + i, p, cost=0.05) for i, p in enumerate((0.66, 0.64, 0.62, 0.6))]
    assert abs(lotto.typical_cost(mixed, 0.85) - 0.15) < 0.01 and abs(lotto.typical_cost(mixed, 0.63) - 0.05) < 0.01
    assert abs(lotto.typical_cost(mixed, 0.75) - 0.05) < 0.01                # too few near it: the overall median


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


# --- underdogs and the efficient construction ------------------------------------------------------ #
def test_only_moderate_underdogs_with_a_real_price_reach_the_pool():
    boards = {"nfl": {"ts": time.time(), "names": {}, "games": [
        # a 42% dog DraftKings pays +160 on (fair about +138): value
        _game("d1", "chicago bears", "green bay packers", 0.58,
              dk={"book": "DraftKings", "ml": {"green bay packers": 160}, "ev": {"green bay packers": 0.092}}),
        # a 42% dog with no price anywhere: left out
        _game("d2", "buffalo bills", "new york jets", 0.58),
        # a 30% dog is a long shot, even at a good price: left out
        _game("d3", "dallas cowboys", "tampa bay buccaneers", 0.70,
              dk={"book": "DraftKings", "ml": {"tampa bay buccaneers": 300}, "ev": {"tampa bay buccaneers": 0.2}}),
        # a 40% dog at +140 (a hair under fair): in, priced, but no value flag
        _game("d5", "houston texans", "indianapolis colts", 0.60,
              dk={"book": "DraftKings", "ml": {"indianapolis colts": 140}, "ev": {"indianapolis colts": -0.04}}),
    ]}}
    # a 45% dog on Kalshi at a 40c ask (about 41.7c after the fee): value on the exchange
    k = _game("d4", "seattle seahawks", "los angeles rams", 0.55)
    k["kalshi_ask"] = {"los angeles rams": 0.40}
    boards["nfl"]["games"].append(k)
    legs = lotto.candidates(boards, now=NOW)
    dogs = {l["dedup"]: l for l in legs if "dog" in l["flags"]}
    assert set(dogs) == {"d1", "d4", "d5"}
    assert dogs["d1"]["edge"]["venue"] == "DraftKings" and dogs["d1"]["flags"][:2] == ["dog", "value"]
    assert dogs["d1"]["cost"] < 0 < dogs["d5"]["cost"]                     # value costs less than nothing
    assert dogs["d4"]["edge"]["venue"] == "Kalshi" and dogs["d4"]["edge"]["ev"] > 0.07
    assert dogs["d5"]["flags"] == ["dog"] and dogs["d5"]["edge"] is None
    assert not any(l["dedup"] == "d3" and "dog" in l["flags"] for l in legs)
    for v in ("favorites", "research"):                                    # only the efficient tickets take dogs
        assert lotto.build_ticket(legs, 5, 1000, v)["dogs"] == 0


def test_the_efficient_construction_loses_the_least_to_the_book():
    heavy = [_priced(i, p, cost=0.15) for i, p in enumerate((0.9, 0.88, 0.86, 0.85, 0.84, 0.83, 0.82, 0.81, 0.8))]
    middle = [_priced(20 + i, p, cost=0.05) for i, p in enumerate((0.75, 0.72, 0.7, 0.68, 0.66, 0.64, 0.62, 0.6) * 2)]
    dogs = [_priced(40 + i, p, cost=0.03, flags=["dog"]) for i, p in enumerate((0.45, 0.42, 0.40))]
    value_dog = _priced(50, 0.44, cost=-0.05, flags=["dog", "value"])
    unpriced = {**_priced(60, 0.97), "dk": None, "cost": None}
    pool = sorted(heavy + middle + dogs + [value_dog, unpriced], key=lambda l: -l["p"])
    eff = lotto.build_ticket(pool, 5, 1000, "efficient")
    fav = lotto.build_ticket(pool, 5, 1000, "favorites")
    assert eff["reached"] and fav["reached"] and eff["dk_payout"] >= 1000 and fav["dk_payout"] >= 1000
    assert eff["dogs"] == 2 and "g50" in {l["dedup"] for l in eff["legs"]}  # the value dog first, never three
    assert eff["dk_priced"] == len(eff["legs"]) and eff["dk_price"]         # every leg priced at DraftKings
    assert eff["p"] > fav["p"] * 1.15                                        # same payout, a likelier ticket
    assert len(eff["legs"]) < len(fav["legs"])
    assert eff["dk_payout"] < 1100 and fav["dk_payout"] < 1100             # tightened: little payout wasted
    # a heavy favorite only to top off the payout: they cost the most per unit of it
    assert len({l["dedup"] for l in eff["legs"]} & {l["dedup"] for l in heavy}) <= 1
