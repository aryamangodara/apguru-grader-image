"""Integration tests for health check endpoints."""


async def test_health_returns_ok(client):
    resp = await client.get("/api/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["message"] == "Service is healthy"


async def test_ping_returns_pong(client):
    resp = await client.get("/api/v1/health/ping")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pong"
