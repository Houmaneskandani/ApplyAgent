"""
Contract-level smoke tests over the FastAPI app.

We don't connect to a real Postgres here — these tests just confirm that:
  - The app boots cleanly with valid env.
  - Unauthenticated endpoints work.
  - Protected endpoints reject without a token (401).
"""
from fastapi.testclient import TestClient


def test_app_boots_and_root_responds():
    from api.main import app
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    assert "status" in r.json()


def test_protected_endpoints_require_auth():
    from api.main import app
    client = TestClient(app)
    # No Authorization header → 403 (FastAPI HTTPBearer default) or 401.
    for path in ("/jobs/", "/jobs/stats", "/profile/", "/credits/", "/queue/", "/auto-apply/"):
        r = client.get(path)
        assert r.status_code in (401, 403), f"{path}: got {r.status_code}"


def test_credits_packages_is_public_and_returns_list():
    from api.main import app
    client = TestClient(app)
    r = client.get("/credits/packages")
    assert r.status_code == 200
    pkgs = r.json()
    assert isinstance(pkgs, list) and len(pkgs) >= 1
    for p in pkgs:
        assert {"id", "credits", "price_cents"}.issubset(p.keys())
