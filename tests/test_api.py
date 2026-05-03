import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("RATE_LIMIT", "5")
    import main
    importlib.reload(main)  # pick up the per-test DB path and rate limit
    with TestClient(main.app) as c:
        yield c


def create(client, **body):
    return client.post("/api/links", json={"url": "https://example.com/page", **body})


def test_create_and_redirect(client):
    res = create(client)
    assert res.status_code == 201
    code = res.json()["code"]
    assert len(code) == 7
    r = client.get(f"/{code}", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "https://example.com/page"


def test_invalid_url_rejected(client):
    res = client.post("/api/links", json={"url": "not-a-url"})
    assert res.status_code == 422


def test_custom_code_and_conflict(client):
    assert create(client, custom_code="my-link").status_code == 201
    assert create(client, custom_code="my-link").status_code == 409
    assert create(client, custom_code="docs").status_code == 400


def test_unknown_code_404(client):
    assert client.get("/nope123", follow_redirects=False).status_code == 404


def test_stats_require_owner_key(client):
    data = create(client).json()
    code, key = data["code"], data["owner_key"]
    client.get(f"/{code}", follow_redirects=False, headers={"referer": "https://google.com"})
    client.get(f"/{code}", follow_redirects=False)

    assert client.get(f"/api/links/{code}/stats").status_code == 403
    stats = client.get(f"/api/links/{code}/stats", headers={"X-Owner-Key": key}).json()
    assert stats["total_clicks"] == 2
    referrers = {r["referrer"] for r in stats["top_referrers"]}
    assert referrers == {"https://google.com", "direct"}


def test_delete(client):
    data = create(client).json()
    code, key = data["code"], data["owner_key"]
    assert client.delete(f"/api/links/{code}", headers={"X-Owner-Key": "wrong"}).status_code == 403
    assert client.delete(f"/api/links/{code}", headers={"X-Owner-Key": key}).status_code == 204
    assert client.get(f"/{code}", follow_redirects=False).status_code == 404


def test_rate_limit(client):
    statuses = [create(client).status_code for _ in range(7)]
    assert statuses[:5] == [201] * 5
    assert statuses[5] == 429
