"""Per-prop "read" — free deterministic heuristic + optional on-demand AI take.

read(prop)  -> a lean (over/under/avoid/neutral) + trap flag + tier + one-line reason for EVERY
              prop, using only free signals (line type, popularity, position, favorite/underdog,
              our model's expected goals). No LLM cost. Runs on every row at snapshot time.
ai_read(prop) -> one cheap Haiku call (cached) for a written verdict, only when the user clicks
              "Read" on a row. Sends the heuristic read as context so the model reasons over the
              same signals. Graceful no-op (returns the heuristic) without an Anthropic key.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time

from . import config

_SHOT_STATS = {"Shots", "Shots On Target"}
_SCORING_STATS = _SHOT_STATS | {"Goals", "Assists"}


def read(p: dict) -> dict:
    stat = p.get("stat_type") or ""
    pos = p.get("position") or ""
    odds = p.get("odds_type") or "standard"
    pop = p.get("popularity") or 0
    line = p.get("line")
    on_fav = bool(p.get("on_favorite"))
    ttover = p.get("ttover")          # team-total-Over-1.5 model %  (0-100) or None
    favpct = p.get("fav_fair_pct")    # favorite fair %             (0-100) or None
    mp = p.get("model_prob")          # model P(over), 0-1, or None (goals/shots only)

    hot = pop >= 5000
    warm = 1500 <= pop < 5000
    env_high = on_fav and ttover is not None and ttover >= 60
    env_low = (not on_fav) and ttover is not None and ttover < 45
    is_gk, is_att = pos == "Goalkeeper", pos == "Attacker"
    is_mid, is_def = pos == "Midfielder", pos == "Defender"

    lean, avoid, driver = "neutral", False, ""
    if (is_gk and stat in _SCORING_STATS) or (is_def and stat in {"Goals", "Shots", "Shots On Target"}) or (is_mid and stat == "Goals"):
        lean, avoid = "avoid", True
        driver = f"{pos.lower() or 'this player'} rarely produces {stat.lower()} overs"
    elif odds == "demon" and is_att and env_low and stat in {"Goals", "Shots On Target", "Shots"}:
        lean, avoid = "avoid", True
        driver = "underdog attacker in a low-scoring projection on a juiced demon line"
    elif hot and odds == "demon" and stat in _SHOT_STATS:
        lean, driver = "under", "popular demon — the crowd reaching at juiced odds"
    elif hot and odds == "demon" and stat == "Goals":
        lean, avoid = "avoid", True
        driver = "popular demon goals — can't cleanly bet 'no goal'"
    elif odds == "goblin" and is_att and on_fav and (favpct or 0) >= 60 and stat in {"Shots", "Shots On Target", "Goals"}:
        lean, driver = "over", "easier goblin line on a favorite's attacker"
    elif stat == "Goals" and is_att and env_high and odds in {"goblin", "standard"}:
        lean = "over"
        driver = f"favorite{f' {favpct:.0f}%' if favpct else ''} attacker in a {ttover:.0f}% Over-1.5 script"
    elif stat == "Goals" and is_att and odds == "demon" and env_high and pop < 1500:
        lean, driver = "over", "favorite attacker, high-scoring script, line not yet crowded"
    elif stat in _SHOT_STATS and is_att and env_high and not hot and odds in {"standard", "goblin"}:
        lean, driver = "over", "favorite attacker in a high-scoring script"
    elif stat == "Passes Attempted":
        if (is_mid or is_def) and not hot:
            lean, driver = "over", "deep-lying volume role"
        else:
            driver = "volume prop"

    # for priced props (goals/shots) the model probability is authoritative for the lean
    mv = p.get("model_value")
    if mp is not None:
        if mv == "fade":
            lean, avoid = "avoid", True
            driver = driver or f"model ~{mp * 100:.0f}% — the over is -EV"
        elif mv in ("value", "lean") and not avoid:
            lean = "over"
            driver = driver or f"model ~{mp * 100:.0f}% to hit the over"
        elif mv == "none" and lean == "over" and not avoid:
            lean = "neutral"
            driver = f"model ~{mp * 100:.0f}% — no real edge on the over"

    # trap risk
    trap_kind = None
    if odds == "demon" and hot and stat in {"Shots", "Shots On Target", "Goals", "Assists"}:
        trap_kind = "primary"
    elif odds == "demon" and warm and on_fav and stat in {"Shots", "Shots On Target", "Goals"}:
        trap_kind = "secondary"
    elif (not on_fav) and is_att and ttover is not None and ttover < 45 and pop >= 1500 and lean == "over":
        trap_kind = "script"
    if odds == "goblin" or stat == "Passes Attempted":
        trap_kind = None
    trap = trap_kind is not None

    # score
    s = 50
    if lean == "over" and odds == "goblin":
        s += 18
    if env_high:
        s += 15
    if stat in _SHOT_STATS and is_att and on_fav:
        s += 8
    if (favpct or 0) >= 70:
        s += 6
    if trap_kind == "primary":
        s -= 25
    elif trap_kind == "secondary":
        s -= 12
    elif trap_kind == "script":
        s -= 15
    if avoid:
        s -= 20
    if odds == "demon" and hot:
        s -= 10
    if stat == "Passes Attempted" and lean == "over" and not hot:
        s += 10
    if warm and lean == "over" and odds != "demon":
        s += 4
    if mp is not None:
        s += round((mp - 0.50) * 55)   # the model probability, blended in (±~27)
    s = max(0, min(100, s))

    if avoid or s <= 30:
        tier = "avoid"
    elif s < 58:
        tier = "neutral"
    elif s < 72:
        tier = "lean"
    else:
        tier = "strong"
    if odds == "goblin" and line is not None and line <= 0.5 and tier == "strong":
        tier = "lean"  # trivial near-certain edge, don't over-rate

    verb = {"over": "Over", "under": "Under", "avoid": "Avoid", "neutral": "No lean"}[lean]
    trap_clause = ""
    if trap_kind == "primary":
        trap_clause = f" · TRAP: {pop // 1000}k on a demon line"
    elif trap_kind == "secondary":
        trap_clause = " · soft trap: popular fav-star demon"
    elif trap_kind == "script":
        trap_clause = " · trap: underdog over in a low-scoring projection"
    model_clause = f" · model ~{mp * 100:.0f}%" if mp is not None else ""
    rationale = (f"{verb} {stat} {line}: {driver}{model_clause}{trap_clause}."
                 if driver else f"{verb} {stat} {line}{model_clause}.")

    return {"lean": lean, "trap_risk": trap, "trap_kind": trap_kind,
            "read_score": s, "tier": tier, "rationale": rationale,
            "model_pct": round(mp * 100) if mp is not None else None}


# --------------------------------------------------------------------------- #
# On-demand AI read (cheap, cached, manual per-row)
# --------------------------------------------------------------------------- #
_SYSTEM = ("You are a sharp soccer betting analyst judging ONE PrizePicks prop for a World Cup "
           "match. Books shade popular props; goblin = easier line, demon = harder; a popular "
           "(hot) demon star over is the canonical trap. You're given the prop, its match context, "
           "and a deterministic heuristic read — reason over those signals. Answer in 1-2 sentences. "
           "Output JSON {lean: over|under|avoid|neutral, confidence: 1-5, why: string<=200 chars}. "
           "You have no live lineup data; if it would hinge on the XI, say to confirm it.")

_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "lean": {"type": "string", "enum": ["over", "under", "avoid", "neutral"]},
        "confidence": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "why": {"type": "string"},
    },
    "required": ["lean", "confidence", "why"],
}


def _cache_key(p: dict, web: bool) -> str:
    sig = f"prop|{p.get('match')}|{p.get('player')}|{p.get('stat_type')}|{p.get('line')}|{p.get('odds_type')}|{web}|{time.strftime('%Y-%m-%d')}"
    return hashlib.sha256(sig.encode()).hexdigest()[:16]


def _load_cache() -> dict:
    p = config.PROPREAD_CACHE_PATH
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _save_cache(c: dict) -> None:
    try:
        config.PROPREAD_CACHE_PATH.write_text(json.dumps(c))
    except Exception as exc:  # noqa: BLE001
        print(f"[propread] cache save failed: {exc}")


async def ai_read(prop: dict, web: bool = False) -> dict:
    heuristic = read(prop)
    if not config.has_anthropic():
        return {**heuristic, "source": "heuristic", "cached": False,
                "why": heuristic["rationale"], "confidence": None}

    key = _cache_key(prop, web)
    cache = _load_cache()
    if key in cache:
        return {**cache[key], "cached": True}

    import anthropic
    payload = {k: prop.get(k) for k in
               ("player", "team", "position", "stat_type", "line", "odds_type",
                "popularity", "opponent", "on_favorite", "match", "ttover", "fav_fair_pct")}
    payload["heuristic_read"] = {k: heuristic[k] for k in ("lean", "tier", "trap_kind", "rationale")}
    tools = ([{"type": config.WEB_SEARCH_TOOL, "name": "web_search",
               "max_uses": 1, "allowed_callers": ["direct"]}] if web else None)
    try:
        async with anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY) as client:
            kwargs = dict(model=config.ANTHROPIC_MODEL, max_tokens=400,
                          system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
                          messages=[{"role": "user", "content": json.dumps(payload, sort_keys=True)}])
            if web:
                kwargs["tools"] = tools
            else:
                kwargs["output_config"] = {"format": {"type": "json_schema", "schema": _SCHEMA}}
            resp = await client.messages.create(**kwargs)
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "{}")
        data = json.loads(text) if not web else _loose_json(text)
        out = {"lean": data.get("lean", heuristic["lean"]),
               "confidence": data.get("confidence"), "why": data.get("why", heuristic["rationale"]),
               "source": "haiku"}
    except Exception as exc:  # noqa: BLE001
        print(f"[propread] ai_read failed: {exc}")
        return {**heuristic, "source": "heuristic", "cached": False,
                "why": heuristic["rationale"], "confidence": None}

    cache[key] = out
    _save_cache(cache)
    return {**out, "cached": False}


def _loose_json(text: str) -> dict:
    try:
        i, j = text.index("{"), text.rindex("}")
        return json.loads(text[i:j + 1])
    except Exception:  # noqa: BLE001
        return {}
