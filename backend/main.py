"""FastAPI app: JSON API + serves the dashboard. Run with `./run.sh` or:

    uvicorn backend.main:app --reload --port 8000
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import aggregator, config, propread
from .store import paper

app = FastAPI(title="poly — World Cup betting dashboard")


@app.on_event("startup")
def _startup() -> None:
    paper.init_paper()


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "sportsbooks_enabled": config.has_sportsbooks()}


@app.post("/api/propread")
async def propread_ep(prop: dict, web: bool = False) -> dict:
    """On-demand AI read for one prop (cheap, cached). Falls back to the heuristic without a key."""
    return await propread.ai_read(prop, web=web)


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
