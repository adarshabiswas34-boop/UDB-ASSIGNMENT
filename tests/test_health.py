from fastapi.testclient import TestClient
from src.main import app

client = TestClient(app)


def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "environment" in data
    assert "timestamp" in data


def test_root_endpoint():
    """Root now returns an HTML landing page."""
    response = client.get("/")
    assert response.status_code == 200
    # Root returns HTML landing page (not JSON)
    assert "text/html" in response.headers.get("content-type", "")
    assert "Rate Limiter" in response.text
    assert "/health" in response.text
    assert "/api/data" in response.text
