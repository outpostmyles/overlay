"""FastAPI app: JSON API + serves the dashboard. Run with `./run.sh` or:

    uvicorn backend.main:app --reload --port 8000
"""
from __future__ import annotations

import asyncio

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import aggregator, config, futures_read, propread
from .store import leans, paper

app = FastAPI(title="poly — World Cup betting dashboard")

_heartbeat_task: asyncio.Task | None = None


@app.on_event("startup")
def _startup() -> None:
    paper.init_paper()


@app.on_event("startup")
async def _start_heartbeat() -> None:
    """On an always-on host the browser is not there to poll /api/snapshot, so nothing would refresh the
    data. This in-process tick does it instead, on the FREE-feed path only, so the Model Ledger locks each
    forecast before kickoff and settles finished games without anyone watching. Off unless HEARTBEAT_ENABLED
    is set (so it never runs during local laptop use or tests). See DEPLOY.md."""
    global _heartbeat_task
    if not config.HEARTBEAT_ENABLED or _heartbeat_task is not None:
        return
    _heartbeat_task = asyncio.create_task(_heartbeat_loop())
    print(f"[heartbeat] enabled, refreshing free feeds every {config.HEARTBEAT_INTERVAL_SECONDS}s")


async def _heartbeat_loop() -> None:
    interval = max(60, config.HEARTBEAT_INTERVAL_SECONDS)
    await asyncio.sleep(5)        # let startup settle before the first refresh
    while True:
        try:
            # force=True re-pulls the free feeds ONLY; refresh_odds/reason stay False so no money is spent
            await aggregator.build_snapshot(force=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 (never let one bad cycle kill the loop)
            print(f"[heartbeat] refresh failed: {exc}")
        await asyncio.sleep(interval)


@app.on_event("shutdown")
async def _stop_heartbeat() -> None:
    if _heartbeat_task:
        _heartbeat_task.cancel()


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "sportsbooks_enabled": config.has_sportsbooks()}


@app.post("/api/propread")
async def propread_ep(prop: dict, web: bool = False) -> dict:
    """On-demand AI read for one prop (cheap, cached). Falls back to the heuristic without a key."""
    return await propread.ai_read(prop, web=web)


@app.post("/api/futuresread")
async def futuresread_ep(payload: dict, web: bool = False) -> dict:
    """On-demand AI read for one futures row: weighs the team's record, the model %, the de-vigged
    market %, and the user's scouting notes into a back/fade/pass. Cheap, cached, heuristic without a key."""
    return await futures_read.ai_read(payload, web=web)


@app.post("/api/futures/lean")
async def add_lean_ep(lean: dict) -> dict:
    """Log a futures lean (back/fade) at the current market %, so the ledger can track its CLV."""
    try:
        entry = float(lean.get("entry_pct"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="entry_pct (number) is required")
    if not lean.get("team") or not lean.get("kind"):
        raise HTTPException(status_code=400, detail="team and kind are required")
    return leans.add(lean["team"], lean["kind"], lean.get("direction", "back"), entry, lean.get("note", ""))


@app.delete("/api/futures/lean/{lean_id}")
async def remove_lean_ep(lean_id: str) -> dict:
    return {"removed": leans.remove(lean_id)}


@app.post("/api/futures/scenario")
async def futures_scenario_ep(payload: dict) -> dict:
    """What-if: pin knockout ties ([{a,b,winner}]) and get how the model's deep-run odds shift."""
    return await aggregator.futures_scenario(payload.get("pins") or [])


@app.get("/api/snapshot")
async def snapshot(force: bool = False, refresh_odds: bool = False,
                   reason: bool = False) -> JSONResponse:
    """force=true re-pulls the FREE feeds; refresh_odds=true spends Odds API credits (moneyline lines
    for the whole slate + total-corner lines for the near slate, ~1 credit each); reason=true runs AI
    analysis (a few cents) on uncached matches and commits the slate's picks to the ledger."""
    try:
        data = await aggregator.build_snapshot(force=force, refresh_odds=refresh_odds, reason=reason)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"snapshot failed: {exc}") from exc
    return JSONResponse(data)


# --- Paper-trading proof ledger (the single ledger; absorbs the old Bet Log) -------------- #
@app.get("/api/paper")
async def get_paper() -> dict:
    picks = paper.list_picks()
    await aggregator.attach_kickoffs(picks)   # real kickoff time → ledger orders by next game first
    return {"picks": picks, "summary": paper.summary()}


@app.patch("/api/paper/{pick_id}")
async def patch_paper(pick_id: int, data: dict) -> dict:
    paper.update_pick(pick_id, status=data.get("status"),
                      real_money=data.get("real_money"))
    return {"ok": True}


@app.delete("/api/paper/{pick_id}")
async def remove_paper(pick_id: int) -> dict:
    paper.delete_pick(pick_id)
    return {"ok": True}


# --- Frontend ------------------------------------------------------------- #
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(config.FRONTEND_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(config.FRONTEND_DIR)), name="static")
