"""The deployed app must expose the catalogue and reuse its existing admin auth."""
import pytest
from fastapi.testclient import TestClient

import app as platform
from related_plans import seed_plans


@pytest.fixture
def client(monkeypatch):
    # Exercise the real installed routes without contacting an operational DB.
    monkeypatch.setattr(platform.app.state.related_plans_store, "writable", False)
    monkeypatch.setenv("ADMIN_PASSWORD", "integration-test-password")
    return TestClient(platform.app)


def test_catalogue_and_assets_are_mounted_on_deployed_app(client):
    page = client.get("/plans")
    assert page.status_code == 200
    assert "/plans/assets/plans.js" in page.text
    assert client.get("/plans/assets/plans.js").status_code == 200
    assert client.get("/plans/assets/plans.css").status_code == 200
    catalog = client.get("/api/related-plans/catalog")
    assert catalog.status_code == 200
    assert catalog.json()["total"] == len(seed_plans())
    assert catalog.json()["counts"]["11"] == 32
    result = client.get("/api/related-plans", params={"region": "11"})
    assert result.status_code == 200
    next_page = client.get("/api/related-plans", params={"region": "11", "page": 2})
    assert next_page.status_code == 200
    assert sum(len(p["links"]) for p in result.json()["items"] + next_page.json()["items"]) == 53


def test_selection_page_serves_plan_entry(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "/api/development/ui.js" in page.text
    script = client.get("/api/development/ui.js")
    assert script.status_code == 200
    assert "devRelatedPlansAction" in script.text


def test_catalogue_management_uses_platform_admin_session(client):
    assert client.get("/plans/manage").status_code == 401
    assert client.get("/plans/manage", auth=("admin", "integration-test-password")).status_code == 200
    client.cookies.set(platform.ADMIN_SESSION_COOKIE, platform._admin_session_token())
    assert client.get("/plans/manage").status_code == 200
