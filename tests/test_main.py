"""M0 done-when check: the app boots and GET /healthz returns 200."""

from fastapi.testclient import TestClient

from lab_orchestrator.main import app


def test_healthz_returns_ok():
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
