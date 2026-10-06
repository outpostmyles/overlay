"""The dashboard's files must tell browsers to revalidate, or an update can pair new JavaScript with a
stale stylesheet; unchanged files still come back as a cheap 304."""
from fastapi.testclient import TestClient

from backend.main import app


def test_frontend_files_revalidate_and_answer_304_when_unchanged():
    client = TestClient(app)          # no `with`: the startup hooks (heartbeat) must not run in a test
    css = client.get("/styles.css")
    assert css.status_code == 200 and css.headers["cache-control"] == "no-cache"
    again = client.get("/styles.css", headers={"If-None-Match": css.headers["etag"]})
    assert again.status_code == 304                          # revalidation is a round trip, not a download
    assert client.get("/").headers["cache-control"] == "no-cache"
    assert "cache-control" not in client.get("/api/health").headers   # API responses untouched
