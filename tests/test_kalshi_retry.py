"""Kalshi rate-limits bursts (four boards share one IP): a refused page is retried, and a series that still
fails shows its last good pull for a while instead of dropping off the board for a cycle."""
import asyncio

import httpx

from backend.sources import kalshi


def _client(statuses: list) -> tuple[httpx.AsyncClient, list]:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params.get("series_ticker"))
        code = statuses.pop(0) if statuses else 200
        if code != 200:
            return httpx.Response(code, headers={"retry-after": "0"})
        return httpx.Response(200, json={"markets": [{"ticker": "KXNFLGAME-1-DAL"}], "cursor": ""})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), calls


def test_a_refused_page_is_retried(monkeypatch):
    async def no_wait(_):
        return None
    monkeypatch.setattr(kalshi.asyncio, "sleep", no_wait)
    client, calls = _client([429, 503])
    rows = asyncio.run(kalshi._fetch_series(client, "KXNFLGAME"))
    assert rows.ok and len(rows) == 1 and len(calls) == 3                 # two refusals, then the page


def test_a_series_that_keeps_failing_shows_its_last_good_pull(monkeypatch):
    async def no_wait(_):
        return None
    monkeypatch.setattr(kalshi.asyncio, "sleep", no_wait)
    kalshi._LAST_GOOD.clear()
    good, _ = _client([])
    assert len(asyncio.run(kalshi._series_or_last_good(good, "KXNFLGAME"))) == 1
    refused, _ = _client([429, 429, 429])
    kept = asyncio.run(kalshi._series_or_last_good(refused, "KXNFLGAME"))
    assert kept == [{"ticker": "KXNFLGAME-1-DAL"}]                        # the board keeps the game
    stamp, rows = kalshi._LAST_GOOD["KXNFLGAME"]
    kalshi._LAST_GOOD["KXNFLGAME"] = (stamp - kalshi.LAST_GOOD_SECONDS - 1, rows)
    refused, _ = _client([429, 429, 429])
    assert asyncio.run(kalshi._series_or_last_good(refused, "KXNFLGAME")) == []   # too old to show
    kalshi._LAST_GOOD.clear()
