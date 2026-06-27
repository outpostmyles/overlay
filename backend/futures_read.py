"""Per-row "read" for the Futures tab: a reasoned verdict on where the model and the sharp market
disagree on a knockout-stage or winner price, weighing the user's own scouting notes (what they saw
watching the games) as the signal the market may have buried.

read(payload)    -> a free, deterministic default (almost always "pass": with no eye-test there is no
                    edge over the market) plus a one-line framing of the model-vs-market gap.
ai_read(payload) -> one cheap, cached Haiku call, only when the user clicks Read on a row. It is handed
                    the team's actual tournament record, the model %, the de-vigged market %, and the
                    user's scouting notes, and returns back/fade/pass. Graceful no-op (the heuristic)
                    without an Anthropic key.
"""
from __future__ import annotations

import hashlib
import json
import time

from . import config


def read(p: dict) -> dict:
    """Deterministic default. The market is the sharp anchor, so with no extra information the honest
    read is always 'pass'; the gap to the model is framed as context, not a signal."""
    mk = p.get("market_pct")
    md = p.get("model_pct")
    if mk is None or md is None:
        return {"lean": "pass", "why": "not enough market/model data to compare", "tier": "neutral"}
    gap = md - mk
    if gap >= 8:
        why = (f"model {md}% sits well above the market's {mk}% - the model tends to over-rate teams "
               f"with dominant long-run scoring; trust the market unless your notes say otherwise")
    elif gap <= -8:
        why = (f"model {md}% sits well below the market's {mk}% - the model under-rates value the market "
               f"prices beyond goals (squad, pedigree, draw); trust the market unless your notes say otherwise")
    else:
        why = f"model {md}% and market {mk}% roughly agree - no model-vs-market disagreement to read into"
    return {"lean": "pass", "why": why, "tier": "neutral"}


_SYSTEM = (
    "You are a sharp soccer futures analyst for the World Cup. The de-vigged prediction-market price is "
    "the SHARP anchor and is usually right; an independent stats model is a weak second opinion. You are "
    "given one team and one futures market (win the cup, or reach a knockout round), the market %, the "
    "model %, the team's recent results, and the USER'S OWN SCOUTING NOTES from watching "
    "the games. Your job: decide whether the user has a real reason to BACK (take the team over the market "
    "price), FADE (bet against it), or PASS. Weight the user's notes heavily - that eye-test is the one "
    "signal the market may have buried - but do NOT invent an edge: if the notes do not clearly bear on "
    "this team, or only the model (not the notes) disagrees with the market, default to PASS, because a "
    "stats model does not beat a sharp market. Be concrete and honest. Output JSON "
    "{lean: back|fade|pass, confidence: 1-5, why: string<=240 chars}."
)

_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "lean": {"type": "string", "enum": ["back", "fade", "pass"]},
        "confidence": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "why": {"type": "string"},
    },
    "required": ["lean", "confidence", "why"],
}


def _cache_key(p: dict, web: bool) -> str:
    notes = (p.get("notes") or "").strip()
    sig = (f"fut|{p.get('team')}|{p.get('kind')}|{p.get('market_pct')}|{p.get('model_pct')}|"
           f"{hashlib.sha256(notes.encode()).hexdigest()[:8]}|{web}|{time.strftime('%Y-%m-%d')}")
    return hashlib.sha256(sig.encode()).hexdigest()[:16]


def _load_cache() -> dict:
    p = config.FUTURESREAD_CACHE_PATH
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _save_cache(c: dict) -> None:
    try:
        config.FUTURESREAD_CACHE_PATH.write_text(json.dumps(c))
    except Exception as exc:  # noqa: BLE001
        print(f"[futures_read] cache save failed: {exc}")


def _loose_json(text: str) -> dict:
    try:
        i, j = text.index("{"), text.rindex("}")
        return json.loads(text[i:j + 1])
    except Exception:  # noqa: BLE001
        return {}


async def ai_read(payload: dict, web: bool = False) -> dict:
    heuristic = read(payload)
    if not config.has_anthropic():
        return {**heuristic, "source": "heuristic", "cached": False, "confidence": None}

    key = _cache_key(payload, web)
    cache = _load_cache()
    if key in cache:
        return {**cache[key], "cached": True}

    import anthropic
    body = {k: payload.get(k) for k in ("team", "kind", "market_pct", "model_pct", "record", "notes")}
    tools = ([{"type": config.WEB_SEARCH_TOOL, "name": "web_search",
               "max_uses": 1, "allowed_callers": ["direct"]}] if web else None)
    try:
        async with anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY) as client:
            kwargs = dict(model=config.ANTHROPIC_MODEL, max_tokens=400,
                          system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
                          messages=[{"role": "user", "content": json.dumps(body, sort_keys=True)}])
            if web:
                kwargs["tools"] = tools
            else:
                kwargs["output_config"] = {"format": {"type": "json_schema", "schema": _SCHEMA}}
            resp = await client.messages.create(**kwargs)
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "{}")
        data = json.loads(text) if not web else _loose_json(text)
        out = {"lean": data.get("lean", "pass"), "confidence": data.get("confidence"),
               "why": data.get("why", heuristic["why"]), "source": "haiku"}
    except Exception as exc:  # noqa: BLE001
        print(f"[futures_read] ai_read failed: {exc}")
        return {**heuristic, "source": "heuristic", "cached": False, "confidence": None}

    cache[key] = out
    _save_cache(cache)
    return {**out, "cached": False}
