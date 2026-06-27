"""AI reasoning layer — web-grounded bet / lean / pass verdicts scoped to the user's archetypes.

Two steps per match (web search returns citations, which can't be combined with structured JSON
output in one call):
  1. RESEARCH — a `web_search`-grounded brief: recent form, injuries, expected lineup / rotation
     risk, group-stage situation (clinched? must-win?), motivation, venue/weather.
  2. VERDICT — a structured call (output_config JSON schema) that reasons over that brief PLUS our
     quantitative signals (sharp market %, model %, PrizePicks popularity, goblin/demon) and emits
     recommended bets, fades (traps), an SGP, confidence, and a key risk.

COST CONTROL: manual-trigger only (never on the 60s auto-refresh), bounded to REASONING_MAX_MATCHES,
web searches capped per match (WEB_SEARCH_MAX_USES), run concurrently, and disk-cached keyed by the
match inputs + the calendar day — so re-analyzing the same slate the same day spends nothing, while
a new day re-pulls fresh news.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time

from . import config

_SYSTEM = """You are a sharp, honest World Cup 2026 betting analyst. You advise a disciplined \
bettor whose ONLY bet types are: (A) heavy-favorite moneylines, (B) anytime goalscorer on a \
favorite's main scorer, (C) star attacker shots / shots-on-target overs, (D) team total goals \
Over 1.5, and (E) popular PrizePicks props (passes attempted, shots). He often stacks A+B+D into \
a same-game parlay. Only ever discuss these archetypes.

For each match you get: favorite/underdog, the sharp prediction-market fair win % (the true price), \
our independent model's % (second opinion), our model's team-total-Over-1.5 %, the PrizePicks props \
(with 🔥popularity, line type — goblin = easier, demon = harder — and `model_pct`, our model's \
estimated chance the OVER hits: <~45% means the over is -EV crowd-bait to fade, >~58% is a genuine \
edge; null = unpriceable, judge on context), a `research` brief of CURRENT \
web-sourced facts (form, injuries, expected lineup, rotation risk, group situation, motivation, \
weather), and `smart_money_match` — Polymarket whale positions and recent money flow on THIS GAME's \
market for the favorite and the underdog to win (top_holder_shares = size of the biggest backer, \
flow = buying/selling/flat). Treat smart money as a conviction signal, not gospel: heavy whale buying \
on a side is supporting evidence; whales selling/flat or a thin holder base is a caution flag. This is \
game-level money (more relevant than tournament odds), but game-market volume is thin — weight it as \
a tiebreaker, not a driver. You may also get `favorite_best_price_american` + `favorite_best_book` (the \
best book price available for the favorite's moneyline) and `favorite_market_ev_pct` (that price's edge \
vs the sharp fair line). Use it honestly: a POSITIVE ev_pct means a book is genuinely mispricing the \
favorite in your favor (real value — say so and name the book); zero/negative means the market is \
efficient and there is NO price edge — recommend the ML only on conviction, never imply value that \
isn't there, and prefer flagging it as an SGP leg over a standalone bet.

`confirmed_lineup` (when present) is the OFFICIAL starting XI + formation for the favorite and/or \
underdog (posted ~1h before kickoff). It is GROUND TRUTH — trust it over any projection. If the \
favorite's main goalscorer / star attacker is NOT in the listed XI, his anytime-goalscorer and \
shots/SOT props are DEAD (fade them) and the team-total-over and ML weaken; if he is confirmed \
starting, that's a green light for his props. When `confirmed_lineup` is absent the XI isn't out yet \
— lean on the research brief's projected lineup and tell the user to confirm before kickoff.

Weigh the research brief HEAVILY — it is where your edge over the raw numbers comes from. Examples: \
a favorite that has already clinched and may rest starters is a fade on its ML and team-total over, \
and its stars' prop overs become traps; a desperate underdog that's must-win or hasn't reached a \
World Cup in years raises draw/upset risk against a flat favorite. When the market and our model \
disagree, weigh it — but an EXTREME gap (e.g. market 85% vs model 35%) almost always means our \
model is UNDER-rating a heavy favorite, NOT that the favorite is a fade; treat a big gap as likely \
model error and only fade a favorite on a real situational reason (clinched + resting, must-win \
desperate underdog, key injuries). Don't reflexively fade chalk because the model is low.

Rules: `recommended_bets` = ONLY bets to actually place (each with a real, explainable reason) — never \
put the same selection in both recommended_bets and fades; a leg that's only worth it inside the \
parlay goes in `sgp_legs`, not recommended_bets. Each `selection` MUST be ONE auto-gradeable bet: a \
single player and a single numeric line (e.g. 'Pedri Over 73.5 Passes Attempted', 'Kylian Mbappe \
Anytime Goalscorer 0.5'); NEVER combine options with 'or' (no 'shots or shots-on-target', no 'Kane \
shots or Saka shots') and never omit the line on a shots/SOT/passes prop. FADE popular props that look like traps. Be willing \
to PASS (empty recommended_bets) — a confident "nothing here" beats a forced pick. Suggest an SGP \
only when 2-3 legs genuinely reinforce. If the brief is missing or says 'unknown', say so and tell \
the user to confirm lineups. NEVER invent injuries or news beyond the brief.

STYLE: `headline` = ONE punchy sentence, max ~90 characters, sentence case, stating the single main \
call — no semicolon-chained lists, and do NOT open with the market-vs-model gap (lead with the \
sharpest real edge, usually lineup/situation). Keep each rationale to one or two tight sentences. \
NEVER use em dashes or en dashes anywhere in your output; use periods, commas, or middle dots (·) instead."""

_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "headline": {"type": "string"},
        "confidence": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "recommended_bets": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "archetype": {"type": "string", "enum": [
                        "favorite_ml", "team_total_over", "anytime_goalscorer", "shots_sot", "popular_prop"]},
                    "selection": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": ["archetype", "selection", "rationale"],
            },
        },
        "fades": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"selection": {"type": "string"}, "why": {"type": "string"}},
                "required": ["selection", "why"],
            },
        },
        "sgp_legs": {"type": "array", "items": {"type": "string"}},
        "key_risk": {"type": "string"},
    },
    "required": ["headline", "confidence", "recommended_bets", "fades", "sgp_legs", "key_risk"],
}


def _key(bundle: dict, day: str) -> str:
    # Key on STABLE identifiers only — match + favorite + calendar day. Do NOT hash the whole
    # bundle: it carries volatile fields (PrizePicks popularity, smart-money flow) that tick every
    # minute, which would miss the cache on every auto-refresh (cards vanish) and re-spend on every
    # Analyze. Stable key => analyze once/day, then it persists and re-runs are free.
    sig = f"{bundle.get('match')}|{bundle.get('favorite')}|{day}"
    return hashlib.sha256(sig.encode()).hexdigest()[:16]


def _load_cache() -> dict:
    p = config.REASONING_CACHE_PATH
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return {}


def _save_cache(cache: dict) -> None:
    try:
        config.REASONING_CACHE_PATH.write_text(json.dumps(cache))
    except Exception as exc:  # noqa: BLE001
        print(f"[reasoning] cache save failed: {exc}")


def attach_cached(bundles: list[dict]) -> dict:
    """Return {match: verdict} for bundles already cached for today. Spends nothing."""
    if not config.has_anthropic():
        return {}
    day = time.strftime("%Y-%m-%d")
    cache = _load_cache()
    out = {}
    for b in bundles:
        v = cache.get(_key(b, day))
        if v:
            out[b["match"]] = v
    return out


async def _research(client, bundle: dict, today: str) -> str:
    """Web-search-grounded factual brief for one match. Returns "" on failure."""
    if not config.REASONING_WEB_SEARCH:
        return ""
    xi_known = bool((bundle.get("confirmed_lineup") or {}).get("favorite"))
    base = (f"Today is {today}. Research the 2026 FIFA World Cup match {bundle['favorite']} vs "
            f"{bundle.get('underdog')} (kickoff {bundle.get('commence_time')}). You have at most TWO web "
            "searches — spend them on what moves these bets. ")
    if xi_known:
        q = base + ("The official starting XIs are ALREADY KNOWN (given to you separately as "
                    "confirmed_lineup) — do NOT search for lineups. Search 1 (FITNESS/NEWS): late "
                    "injuries, knocks, or suspensions to the favorite's key attackers not reflected in "
                    "the XI, and any last-minute team news. Search 2 (SITUATION): group-stage state "
                    "(clinched / must-win / dead rubber), recent form, and motivation/narrative. ")
    else:
        q = base + ("Search 1 (LINEUPS, the priority): the probable starting XI and any "
                    "injuries/suspensions/doubts to KEY attackers and the main goalscorer — and whether a "
                    "side that has already advanced is expected to REST starters. Search 2 (SITUATION): "
                    "group-stage state (clinched / must-win for someone), recent form, and "
                    "motivation/narrative (long World Cup absence, rivalry, dead rubber). ")
    q += ("Write a concise factual brief under 140 words IN PLAIN PROSE (no markdown headings or **bold**), "
          "leading with the fitness/lineup status of the favorite's stars. Frame facts as of today and "
          "don't assert exact volatile numbers (group points, weather) you can't confirm. If something "
          "isn't found, write 'unknown'. NEVER speculate or invent injuries, lineups, or news. "
          "Do not use em dashes or en dashes; use periods, commas, or middle dots instead.")
    msgs = [{"role": "user", "content": q}]
    # allowed_callers=["direct"] disables programmatic/dynamic-filtering tool calling, which the
    # cheap Haiku tier doesn't support — the model calls web_search directly instead.
    tools = [{"type": config.WEB_SEARCH_TOOL, "name": "web_search",
              "max_uses": config.WEB_SEARCH_MAX_USES, "allowed_callers": ["direct"]}]
    try:
        resp = await client.messages.create(model=config.ANTHROPIC_MODEL, max_tokens=1200, messages=msgs, tools=tools)
        guard = 0
        while resp.stop_reason == "pause_turn" and guard < 4:
            guard += 1
            msgs = [{"role": "user", "content": q}, {"role": "assistant", "content": resp.content}]
            resp = await client.messages.create(model=config.ANTHROPIC_MODEL, max_tokens=1200, messages=msgs, tools=tools)
        return " ".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    except Exception as exc:  # noqa: BLE001
        print(f"[reasoning] web research failed for {bundle.get('match')}: {exc}")
        return ""


async def _verdict(client, bundle: dict, research: str) -> dict | None:
    payload = dict(bundle)
    payload["research"] = research or "(no web research available — confirm lineups before betting)"
    try:
        resp = await client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=1024,
            system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": json.dumps(payload, sort_keys=True)}],
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
        )
        text = next((b.text for b in resp.content if b.type == "text"), "{}")
        verdict = json.loads(text)
        verdict["research"] = research  # surface the brief to the UI
        return verdict
    except Exception as exc:  # noqa: BLE001
        print(f"[reasoning] verdict failed for {bundle.get('match')}: {exc}")
        return None


async def _analyze_match(client, sem, bundle: dict, today: str) -> dict | None:
    async with sem:
        research = await _research(client, bundle, today)
        return await _verdict(client, bundle, research)


async def run(bundles: list[dict], max_matches: int) -> dict:
    """Research + analyze up to max_matches, reusing today's disk cache for unchanged matches.
    Returns {match: verdict} for every bundle that has a verdict (cached or fresh)."""
    if not config.has_anthropic() or not bundles:
        return {}
    import anthropic

    today = time.strftime("%Y-%m-%d")
    bundles = bundles[:max_matches]
    cache = _load_cache()
    out: dict = {}
    todo = []
    for b in bundles:
        k = _key(b, today)
        cur_xi = bool((b.get("confirmed_lineup") or {}).get("favorite"))
        hit = cache.get(k)
        # reuse a cached verdict only if the confirmed-XI state matches — so re-analyzing once the
        # official XI drops (~1h pre-KO) regenerates just that match with the real lineup, not the slate.
        if hit and bool(hit.get("_xi")) == cur_xi:
            out[b["match"]] = hit
        else:
            todo.append((k, b, cur_xi))

    if todo:
        sem = asyncio.Semaphore(5)
        async with anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY) as client:
            results = await asyncio.gather(*[_analyze_match(client, sem, b, today) for _, b, _ in todo])
        for (k, b, cur_xi), verdict in zip(todo, results):
            if verdict:
                verdict["_xi"] = cur_xi
                cache[k] = verdict
                out[b["match"]] = verdict
        _save_cache(cache)
        n = sum(1 for r in results if r)
        web = "web-grounded" if config.REASONING_WEB_SEARCH else "no-web"
        print(f"[reasoning] generated {n} new {web} verdicts (model={config.ANTHROPIC_MODEL})")
    return out
