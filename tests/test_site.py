"""One site for every board: each sport has an address (/nfl/, /cfb/, ...), nginx routes it to the board's
own process, the front door picks the board with games coming up, an old per-port bookmark is sent to the
board's address, and the page only ever asks for its files and API relative to its own address."""
import re
import time
from pathlib import Path

from fastapi.testclient import TestClient

from backend import config, sports
from backend.main import app
from backend.store import livelegs

ROOT = Path(__file__).resolve().parents[1]


def test_every_board_has_its_own_address_in_season_order():
    boards = sports.boards()
    assert [b.key for b in boards] == ["nfl", "cfb", "nhl", "mlb", "wc26"]
    assert [b.site_path for b in boards] == ["nfl", "cfb", "nhl", "mlb", "wc"]
    assert len({b.code for b in boards}) == len(boards) and all(b.code for b in boards)


def test_the_front_door_picks_the_first_board_with_games_coming_up():
    assert sports.pick_board({"nfl": 12, "mlb": 3}).key == "nfl"
    assert sports.pick_board({"nfl": 0, "cfb": 0, "nhl": 0, "mlb": 3}).key == "mlb"     # the summer
    assert sports.pick_board({}).key == "nfl"                                          # nothing running
    now = time.time()
    boards = {"nfl": {"games": [{"kickoff_iso": "2026-10-11T17:00Z"}, {"kickoff_iso": "2026-10-01T17:00Z"},
                                {"kickoff_iso": None}]}}
    assert livelegs.upcoming(boards, now=1791360000) == {"nfl": 1}                    # 2026-10-07: one ahead
    assert livelegs.upcoming({}, now=now) == {}


def test_the_front_door_and_old_bookmarks_redirect(monkeypatch):
    client = TestClient(app)          # no `with`: the startup hooks (heartbeat) must not run in a test
    monkeypatch.setattr(livelegs, "read_all", lambda: {"cfb": {"games": [{"kickoff_iso": "2099-01-01T00:00Z"}]}})
    door = client.get("/go", follow_redirects=False)
    assert door.status_code == 302 and door.headers["location"] == "/cfb/"
    monkeypatch.setattr(config, "ONE_SITE", True)
    old = client.get("/", follow_redirects=False)                      # a board's own port, no nginx
    path = sports.active().site_path
    assert old.status_code == 302 and old.headers["location"] == f"http://testserver/{path}/"
    via_site = client.get("/", headers={"X-Forwarded-Prefix": f"/{path}"}, follow_redirects=False)
    assert via_site.status_code == 200 and "<html" in via_site.text.lower()
    port_host = client.get("/", headers={"Host": "203.0.113.7:8003"}, follow_redirects=False)
    assert port_host.headers["location"] == f"http://203.0.113.7/{path}/"     # the board's port is dropped
    tunnel = client.get("/", headers={"Host": "localhost:8000"}, follow_redirects=False)
    assert tunnel.status_code == 200                                    # an SSH tunnel is never redirected
    assert client.get("/api/health").status_code == 200                # the API never redirects
    monkeypatch.setattr(config, "ONE_SITE", False)
    assert client.get("/", follow_redirects=False).status_code == 200   # local development: served as is


def test_the_page_asks_for_everything_relative_to_its_own_address():
    js = (ROOT / "frontend" / "app.js").read_text()
    html = (ROOT / "frontend" / "index.html").read_text()
    assert '"/api/' not in js and "'/api/" not in js and "`/api/" not in js
    assert re.search(r'href="styles\.css\?v=\d+"', html) and re.search(r'src="app\.js\?v=\d+"', html)
    assert not re.search(r'(href|src)="/(?!/)', html)                   # no root-absolute asset paths


def test_nginx_routes_every_board_to_the_port_its_unit_listens_on():
    script = (ROOT / "deploy" / "setup-nginx.sh").read_text()
    routes = dict(b.split(":") for b in re.search(r'BOARDS="([^"]+)"', script).group(1).split())
    assert set(routes) == {b.site_path for b in sports.boards()}
    for b in sports.boards():
        unit = ROOT / "deploy" / ("overlay.service" if b.key == "wc26" else f"overlay-{b.key}.service")
        text = unit.read_text()
        assert re.search(rf"--port {routes[b.site_path]}\b", text), b.key
        assert b.key == "wc26" or f"SPORT={b.key}" in text
    assert "X-Forwarded-Prefix /${path}" in script and "proxy_pass http://127.0.0.1:8000/go;" in script
    # the trailing slash strips the board's prefix: /nfl/api/snapshot reaches the board as /api/snapshot
    assert "proxy_pass http://127.0.0.1:${port}/;" in script
    assert "text/javascript" in script                                  # app.js's type on Python 3.12
    assert "location = /favicon.ico" in script and "location / { return 302 /; }" in script
