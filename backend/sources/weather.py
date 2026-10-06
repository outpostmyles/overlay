"""Kickoff weather from Open-Meteo (free, no key): wind, gusts, precipitation and temperature for the
hour a game kicks off, at the stadium's city.

Two calls, both cached. The geocoder turns ESPN's venue city and state into coordinates once, ever (a
stadium does not move), persisted to disk. The forecast is fetched per game and hour and kept for an hour
near kickoff, three hours further out, so the line that locks 75 minutes before kickoff carries a forecast
under an hour old. Volume stays far inside Open-Meteo's free limits (a football week is under 70 venues).
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import httpx

from .. import config

GEO_CACHE_PATH = config.ROOT / "poly_geo_cache.json"
_GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
_WX_URL = "https://api.open-meteo.com/v1/forecast"

_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}

_geo: dict | None = None                  # "city|state|country" -> [lat, lon] (or None: not found)
_wx: dict = {}                            # (lat, lon, hour) -> (fetched_at, reading)


def _load_geo() -> dict:
    global _geo
    if _geo is None:
        try:
            _geo = json.loads(GEO_CACHE_PATH.read_text()) if GEO_CACHE_PATH.exists() else {}
        except (OSError, ValueError):
            _geo = {}
    return _geo


def _save_geo() -> None:
    try:
        GEO_CACHE_PATH.write_text(json.dumps(_geo or {}))
    except OSError as exc:
        print(f"[weather] geocode cache save failed: {exc}")


def pick_place(results: list[dict], state: str | None, country: str | None) -> tuple | None:
    """The geocoder's best match for a venue: same US state when ESPN gives one, else the first result
    (a city name alone is ambiguous: there is a Foxborough in four states)."""
    if not results:
        return None
    want = _STATES.get((state or "").upper())
    if want:
        for r in results:
            if r.get("admin1") == want:
                return (r["latitude"], r["longitude"])
        if (country or "USA") in ("USA", "US", "United States"):
            return None                       # a US venue the geocoder cannot place in its state: skip it
    return (results[0]["latitude"], results[0]["longitude"])


async def geocode(client: httpx.AsyncClient, city: str | None, state: str | None,
                  country: str | None) -> tuple | None:
    if not city:
        return None
    cache = _load_geo()
    key = f"{city}|{state or ''}|{country or ''}"
    if key in cache:
        return tuple(cache[key]) if cache[key] else None
    params = {"name": city, "count": 10, "language": "en", "format": "json"}
    if _STATES.get((state or "").upper()):
        params["countryCode"] = "US"
    try:
        r = await client.get(_GEO_URL, params=params, timeout=10)
        results = r.json().get("results") or [] if r.status_code == 200 else None
    except Exception as exc:  # noqa: BLE001
        print(f"[weather] geocode {city} failed: {exc}")
        return None
    if results is None:
        return None                           # a failed call is retried later; only a real miss is cached
    place = pick_place(results, state, country)
    cache[key] = list(place) if place else None
    _save_geo()
    return place


def reading_at(hourly: dict, kickoff: datetime) -> dict | None:
    """The forecast at the kickoff hour (UTC). Precipitation takes the wetter of the kickoff hour and the
    next one, since a shower that starts at the opening drive plays the whole first quarter."""
    times = hourly.get("time") or []
    stamp = kickoff.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")
    if stamp not in times:
        return None
    i = times.index(stamp)

    def at(name, j):
        vals = hourly.get(name) or []
        return vals[j] if 0 <= j < len(vals) else None

    precip = [x for x in (at("precipitation", i), at("precipitation", i + 1)) if x is not None]
    return {"wind_mph": at("wind_speed_10m", i), "gust_mph": at("wind_gusts_10m", i),
            "precip_in": max(precip) if precip else None, "temp_f": at("temperature_2m", i)}


async def kickoff_weather(client: httpx.AsyncClient, city: str | None, state: str | None,
                          country: str | None, kickoff_iso: str | None) -> dict | None:
    """Forecast wind, gusts, precipitation and temperature at a venue's kickoff hour, or None."""
    try:
        kickoff = datetime.fromisoformat((kickoff_iso or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    place = await geocode(client, city, state, country)
    if not place:
        return None
    hour = kickoff.astimezone(timezone.utc).strftime("%Y-%m-%dT%H")
    key = (round(place[0], 3), round(place[1], 3), hour)
    now = time.time()
    near = kickoff - datetime.now(timezone.utc) < timedelta(hours=12)
    hit = _wx.get(key)
    if hit and now - hit[0] < (3600 if near else 3 * 3600):
        return hit[1]
    day = kickoff.astimezone(timezone.utc).date()
    params = {"latitude": place[0], "longitude": place[1], "timezone": "GMT",
              "hourly": "wind_speed_10m,wind_gusts_10m,precipitation,temperature_2m",
              "wind_speed_unit": "mph", "temperature_unit": "fahrenheit", "precipitation_unit": "inch",
              "start_date": day.isoformat(), "end_date": (day + timedelta(days=1)).isoformat()}
    try:
        r = await client.get(_WX_URL, params=params, timeout=10)
        reading = reading_at(r.json().get("hourly") or {}, kickoff) if r.status_code == 200 else None
    except Exception as exc:  # noqa: BLE001
        print(f"[weather] forecast {city} failed: {exc}")
        return hit[1] if hit else None
    if reading is not None:
        _wx[key] = (now, reading)
    return reading
