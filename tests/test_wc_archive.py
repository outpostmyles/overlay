"""The finished World Cup: the bracket is rebuilt from the settled Model Ledger, not the ESPN results
window (which by October no longer reached July and left the board projecting Germany as champion).
The fixture is the real knockout record as the ledger holds it: 27 settled games, three of them level
after extra time (decided on penalties, which the ledger does not record) and five Round of 32 ties
missing altogether (Germany v Paraguay among them)."""
from backend import aggregator, config, sports
from backend.models import Market, Quote, Selection

# (date, team_a, team_b, goals_a, goals_b, match label), exactly as the live ledger settled them
_LEDGER = [
    ("2026-07-19", "argentina", "spain", 0, 1, "Spain vs Argentina"),
    ("2026-07-18", "england", "france", 6, 4, "France vs England"),          # third-place match
    ("2026-07-15", "argentina", "england", 2, 1, "England vs Argentina"),
    ("2026-07-14", "france", "spain", 0, 2, "France vs Spain"),
    ("2026-07-11", "argentina", "switzerland", 3, 1, "Argentina vs Switzerland"),
    ("2026-07-11", "england", "norway", 2, 1, "Norway vs England"),
    ("2026-07-10", "belgium", "spain", 1, 2, "Spain vs Belgium"),
    ("2026-07-09", "france", "morocco", 2, 0, "France vs Morocco"),
    ("2026-07-07", "colombia", "switzerland", 0, 0, "Switzerland vs Colombia"),   # penalties
    ("2026-07-07", "argentina", "egypt", 3, 2, "Argentina vs Egypt"),
    ("2026-07-06", "portugal", "spain", 0, 1, "Portugal vs Spain"),
    ("2026-07-06", "belgium", "united states", 4, 1, "USA vs Belgium"),
    ("2026-07-05", "england", "mexico", 3, 2, "Mexico vs England"),
    ("2026-07-05", "brazil", "norway", 1, 2, "Brazil vs Norway"),
    ("2026-07-04", "france", "paraguay", 1, 0, "Paraguay vs France"),
    ("2026-07-04", "canada", "morocco", 0, 3, "Canada vs Morocco"),
    ("2026-07-03", "argentina", "cabo verde", 3, 2, "Argentina vs Cape Verde"),
    ("2026-07-03", "australia", "egypt", 1, 1, "Australia vs Egypt"),               # penalties
    ("2026-07-02", "croatia", "portugal", 1, 2, "Portugal vs Croatia"),
    ("2026-07-02", "austria", "spain", 0, 3, "Spain vs Austria"),
    ("2026-07-01", "bosnia and herzegovina", "united states", 0, 2, "USA vs Bosnia and Herzegovina"),
    ("2026-07-01", "dr congo", "england", 1, 2, "England vs Congo DR"),
    ("2026-07-01", "belgium", "senegal", 3, 2, "Belgium vs Senegal"),
    ("2026-06-30", "cote divoire", "norway", 1, 2, "Ivory Coast vs Norway"),
    ("2026-06-30", "france", "sweden", 3, 0, "France vs Sweden"),
    ("2026-06-30", "ecuador", "mexico", 0, 2, "Mexico vs Ecuador"),
    ("2026-06-29", "morocco", "netherlands", 1, 1, "Netherlands vs Morocco"),       # penalties
]

_BRACKET_TEAMS = [t for pair in aggregator._BRACKET_R32 for t in pair]


def _row(d, a, b, ga, gb, match, status="settled"):
    out = "a" if ga > gb else "b" if gb > ga else "draw"
    return {"commence_time": d, "team_a": a, "team_b": b, "actual_a": ga, "actual_b": gb,
            "actual_outcome": out, "pens": 1 if ga == gb else 0, "match": match, "status": status}


def _ledger(extra=()):
    return [_row(*g) for g in _LEDGER] + list(extra)


def _field(together=None):
    """12 groups of 4 holding all 32 knockout teams (plus fillers), no knockout pair sharing a group
    unless `together` names one (its second team is moved into the first team's group)."""
    groups = {f"grp{i}": [] for i in range(12)}
    for i, t in enumerate(_BRACKET_TEAMS):
        groups[f"grp{i % 12}"].append(t)
    if together:
        a, b = together
        ga = next(g for g, ts in groups.items() if a in ts)
        gb = next(g for g, ts in groups.items() if b in ts)
        out = next(t for t in groups[ga] if t != a)
        groups[ga][groups[ga].index(out)], groups[gb][groups[gb].index(b)] = b, out
    filler = 0
    for ts in groups.values():
        while len(ts) < 4:
            ts.append(f"filler{filler}")
            filler += 1
    return groups


def _group_games():
    """Group-stage wins for Germany and Paraguay over a groupmate: earlier games outside their Round of 32
    tie, which must not read as either side having played on."""
    return [_row("2026-06-15", "germany", "united states", 2, 0, "Germany vs USA"),
            _row("2026-06-16", "bosnia and herzegovina", "paraguay", 0, 1, "Paraguay vs Bosnia")]


def test_knockout_winners_come_from_the_ledger_when_espn_has_aged_out():
    w = aggregator._knockout_winners([], _field(), _ledger(_group_games()))
    win = lambda a, b: w.get(frozenset((a, b)))  # noqa: E731
    assert win("argentina", "spain") == "spain"                  # the final, 1-0
    assert win("france", "spain") == "spain" and win("argentina", "england") == "argentina"
    # level after extra time: the winner is whoever played on in the next round
    assert win("netherlands", "morocco") == "morocco"            # met Canada next
    assert win("australia", "egypt") == "egypt"
    assert win("switzerland", "colombia") == "switzerland"
    # missing from the ledger entirely: the side that played on
    assert win("germany", "paraguay") == "paraguay"
    assert win("south africa", "canada") == "canada"
    assert win("brazil", "japan") == "brazil"
    assert win("switzerland", "algeria") == "switzerland" and win("colombia", "ghana") == "colombia"
    assert len(w) == 31                                          # every tie of the draw is decided


def test_bracket_champion_and_final_are_the_real_result():
    br = aggregator._build_bracket({}, None, [], _field(), _ledger())
    assert br["champion"] == "spain"
    assert br["final"] == {"winner": "spain", "runner_up": "argentina", "winner_name": "Spain",
                           "runner_up_name": "Argentina", "score_winner": 1, "score_runner_up": 0,
                           "date": "2026-07-19", "pens": False}
    final = br["rounds"][-1]["matchups"][0]
    assert {final["a"], final["b"]} == {"spain", "argentina"} and final["winner"] == "spain"
    r32 = {frozenset((m["a"], m["b"])): m for m in br["rounds"][0]["matchups"]}
    pens = r32[frozenset(("netherlands", "morocco"))]
    assert (pens["winner"], pens["score_a"], pens["score_b"], pens["pens"]) == ("morocco", 1, 1, True)
    lost = r32[frozenset(("germany", "paraguay"))]
    assert lost["winner"] == "paraguay" and lost["score_a"] is None and lost["pens"] is None
    assert all(m["winner"] for r in br["rounds"] for m in r["matchups"])


def test_undecided_tie_is_never_handed_to_the_first_listed_side():
    """With no results and a settled market flooring every team at 0.1%, the old projection picked side
    a of every tie and crowned Germany. Equal or missing prices now leave the slot open."""
    flat = {"win_cup": {t: 0.001 for t in _BRACKET_TEAMS}}
    br = aggregator._build_bracket(flat, None, [], _field(), [])
    assert br["champion"] is None and br["final"] is None
    assert all(m["winner"] is None for m in br["rounds"][0]["matchups"])
    assert all(m["a"] is None and m["b"] is None for m in br["rounds"][1]["matchups"])
    # a market that does separate two teams still projects the favorite through
    priced = {"reach_r16": {"germany": 0.6, "paraguay": 0.4}}
    br = aggregator._build_bracket(priced, None, [], _field(), [])
    assert br["rounds"][1]["matchups"][0]["a"] == "germany"


def test_level_semi_goes_to_the_side_that_met_the_other_semis_winner():
    """A beaten semi-finalist plays again (the third-place match), so 'played on' alone can not settle a
    level semi. France and Spain level here: Spain played Argentina (the other semi's winner) in the
    final, France played England (its loser) for third."""
    led = [r for r in _ledger() if frozenset((r["team_a"], r["team_b"])) != frozenset(("france", "spain"))]
    led.append(_row("2026-07-14", "france", "spain", 1, 1, "France vs Spain"))
    w = aggregator._knockout_winners([], _field(), led)
    assert w[frozenset(("france", "spain"))] == "spain"
    # before the final is played, the third-place match alone still decides it (France met England)
    early = [r for r in led if r["commence_time"] != "2026-07-19"]
    w = aggregator._knockout_winners([], _field(), early)
    assert w[frozenset(("france", "spain"))] == "spain"
    assert frozenset(("argentina", "spain")) not in w


def test_both_semis_level_needs_both_later_games():
    led = [r for r in _ledger() if r["commence_time"] not in ("2026-07-14", "2026-07-15")]
    led += [_row("2026-07-14", "france", "spain", 1, 1, "France vs Spain"),
            _row("2026-07-15", "argentina", "england", 2, 2, "England vs Argentina")]
    w = aggregator._knockout_winners([], _field(), led)
    assert w[frozenset(("france", "spain"))] == "spain"          # in the later game (the final)
    assert w[frozenset(("argentina", "england"))] == "argentina"
    only_third = [r for r in led if r["commence_time"] != "2026-07-19"]
    w = aggregator._knockout_winners([], _field(), only_third)
    assert frozenset(("france", "spain")) not in w               # one game on record: can not tell which


def test_groupmates_meeting_in_the_knockouts_still_count():
    """The ledger opened in the knockouts, so two groupmates who meet again later have only that one
    game on record. It is dated after the Round of 32 began, so it is a knockout game, not their group
    game, while a real group game (before that date) never is."""
    field = _field(together=("argentina", "spain"))
    assert any({"argentina", "spain"} <= set(ts) for ts in field.values())
    br = aggregator._build_bracket({}, None, [], field, _ledger())
    assert br["champion"] == "spain" and br["final"]["score_winner"] == 1
    meet = aggregator._knockout_meetings([], _ledger(_group_games()), _field())
    assert frozenset(("germany", "united states")) not in meet


def test_espn_penalties_flag_still_counts_and_merges_with_the_ledger():
    """While ESPN still has a game, its winner flag decides a level tie, and the same game in both sources
    (dated a day apart) is one meeting, not a rematch."""
    led = [_row("2026-06-29", "morocco", "netherlands", 1, 1, "Netherlands vs Morocco")]
    espn = [{"date": "2026-06-30", "goals": {"netherlands": 1, "morocco": 1}, "winner": "morocco"}]
    meet = aggregator._knockout_meetings(espn, led, _field())
    m = meet[frozenset(("netherlands", "morocco"))]
    assert (m["date"], m["winner"]) == ("2026-06-29", "morocco")
    assert aggregator._knockout_winners(espn, _field(), led) == {frozenset(("netherlands", "morocco")): "morocco"}


class _Model:
    attack: dict = {}
    defense: dict = {}

    def expected_goals(self, a, b):
        return (1.3, 1.1)

    def match_probs(self, a, b):
        return {a: 0.4, "draw": 0.3, b: 0.3}


def _winner_market(prices):
    sels = [Selection(key=t, label=t.title(),
                      quotes=[Quote("polymarket", "prediction_market", 1 / p, p, mid_prob=p)])
            for t, p in prices.items()]
    return Market(market_id="wc-winner", event="World Cup Winner", market_type="winner_outright", selections=sels)


def test_archived_futures_rows_are_results_not_live_value(monkeypatch):
    monkeypatch.setattr(config, "TOURNAMENT_SIMS", 40)
    monkeypatch.setattr(aggregator, "_load_groups_disk", _field)
    monkeypatch.setattr(aggregator, "_ledger_results", _ledger)
    market = _winner_market({"spain": 0.95, "england": 0.001, "argentina": 0.001, "germany": 0.001})
    out = aggregator._compute_futures([market], _Model(), [])
    assert out["archived"] is True
    assert out["final"]["winner"] == "spain" and out["bracket"]["champion"] == "spain"
    rows = {r["team"]: r for r in out["rows"]}
    assert rows["spain"]["resolved"] is True and rows["spain"]["won"] is True
    assert rows["germany"]["won"] is False and rows["argentina"]["won"] is False
    assert all(r["gap_pp"] is None for r in out["rows"])         # no Back/Fade value left to read
    assert rows["spain"]["model_pct"] == 100.0                   # the sim plays out the real results


def test_futures_bracket_survives_the_markets_closing(monkeypatch):
    monkeypatch.setattr(aggregator, "_load_groups_disk", _field)
    monkeypatch.setattr(aggregator, "_ledger_results", _ledger)
    out = aggregator._compute_futures([], _Model(), [])
    assert out["rows"] == [] and out["archived"] is True
    assert out["bracket"]["champion"] == "spain"


def test_live_tournament_is_not_archived(monkeypatch):
    monkeypatch.setattr(config, "TOURNAMENT_SIMS", 40)
    monkeypatch.setattr(aggregator, "_load_groups_disk", _field)
    through_r16 = [r for r in _ledger() if r["commence_time"] < "2026-07-09"]
    monkeypatch.setattr(aggregator, "_ledger_results", lambda: through_r16)
    market = _winner_market({"spain": 0.3, "argentina": 0.25, "france": 0.2, "england": 0.15})
    out = aggregator._compute_futures([market], _Model(), [])
    assert out["archived"] is False and out["final"] is None
    assert all(r["resolved"] is False and r["gap_pp"] is not None for r in out["rows"])


def test_wc26_adapter_is_archived_and_the_others_are_not():
    assert sports.get("wc26").archived is True
    assert not any(sports.get(k).archived for k in sports.keys() if k != "wc26")
