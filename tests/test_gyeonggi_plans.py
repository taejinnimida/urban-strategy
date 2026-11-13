"""Regional catalogue expansion, inheritance, pagination and safe seed upgrades."""
from copy import deepcopy
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

import related_plans as rp

BASE = "/api/related-plans"
HEADERS = {"X-Plan-Contribution": "1"}


def all_items(client, **params):
    first = client.get(BASE, params={**params, "page": 1})
    assert first.status_code == 200
    result = first.json()
    items = result["items"]
    for page in range(2, (result["total"] + result["page_size"] - 1) // result["page_size"] + 1):
        response = client.get(BASE, params={**params, "page": page})
        assert response.status_code == 200
        items.extend(response.json()["items"])
    assert len(items) == result["total"]
    assert len({p["id"] for p in items}) == len(items)
    return items


@pytest.fixture(params=[False, True], ids=["readonly", "sqlite"])
def client(request, tmp_path):
    path = str(tmp_path / "plans.db") if request.param else ""
    return TestClient(rp.create_app(rp.Store(db_path=path, database_url="")))


def test_all_31_municipalities_have_published_basic_plan_and_official_sources():
    seeds = rp.seed_plans()
    gg = [p for p in seeds if p["id"].startswith("gyeonggi-")]
    assert len(gg) == 50
    assert sum(len(p["links"]) for p in gg) == 62
    municipalities = {code for code, r in rp.region_index().items() if r["parent"] == "41"}
    published = [p for p in gg if p["category"] == "urban" and p["plan_type"] == "basic"
                 and p["status"] in {"approved", "amended"} and p["region"] != "41"]
    assert len(municipalities) == len(published) == 31
    assert {p["region"] for p in published} == municipalities
    assert len({p["id"] for p in seeds}) == len(seeds)
    assert len({(p["region"], p["category"], rp.search_key(p["title"])) for p in seeds}) == len(seeds)
    for p in gg:
        assert p["links"] and p["checked_at"] == "2026-11-12"
        assert p["created_at"].startswith(p["checked_at"])
        assert len({rp.canonical_url(l["url"]) for l in p["links"]}) == len(p["links"])
        for link in p["links"]:
            assert urlsplit(link["url"]).hostname.endswith(".go.kr")


def test_gyeonggi_catalogue_and_pagination_preserve_seoul(client):
    gg = all_items(client, region="41")
    assert len(gg) == 50
    assert all(p["region"].startswith("41") for p in gg)
    seoul = all_items(client, region="11")
    assert len(seoul) == 24
    assert sum(len(p["links"]) for p in seoul) == 40
    assert len(all_items(client, region="KR")) == 74
    assert client.get(BASE, params={"region": "41", "page": 3}).json()["items"] == []
    catalog = client.get(BASE + "/catalog").json()
    assert catalog["counts"]["41"] == 50
    assert catalog["counts"]["KR"] == 74
    assert client.get(BASE + "/map/41").json()["features"]
    assert client.get("/plans/assets/gyeonggi-plans.json").status_code == 404


def test_city_and_district_inherit_province_without_sibling_plans(client):
    def ids(region):
        return {p["id"] for p in all_items(client, region=region)}
    expected = {p["id"] for p in rp.seed_plans() if p["region"] in {"41", "41110"}}
    assert len(expected) == 7
    assert ids("41110") == ids("41111") == ids("41113") == expected
    assert "gyeonggi-urban-41130-2035" not in ids("41110")
    assert "gyeonggi-urban-41110-2040" not in ids("41830")
    assert {p["region"] for p in all_items(client, region="41830")} == {"41", "41830"}


def test_plan_status_search_and_posted_dates_are_not_approval_dates(client):
    goyang = {p["target_year"]: p for p in all_items(client, region="41280", q="고양")}
    assert goyang[2035]["status"] == "approved"
    assert goyang[2040]["status"] == "drafting"
    assert goyang[2035]["milestone_label"] == "토지이음 자료 게시"
    assert len(all_items(client, region="41650", status="tender")) == 1
    living = all_items(client, region="41360", q="생활권계획")
    assert [p["id"] for p in living] == ["gyeonggi-urban-41360-2040"]
    parks = all_items(client, region="41270", category="parks")
    assert [p["id"] for p in parks] == ["gyeonggi-parks-41270-2040"]
    assert all(p["region"] != "29" for p in all_items(client, region="41610", q="광주"))
    gwangju = next(p for p in rp.seed_plans() if p["id"] == "gyeonggi-urban-41610-2040")
    assert gwangju["links"][0]["url"].startswith("https://www.gjcity.go.kr/")


def test_expansion_preserves_community_duplicate_and_moderation(tmp_path, monkeypatch):
    seeds = rp.seed_plans()
    seoul_seeds = [p for p in seeds if p["region"] == "11"]
    new_seed = next(p for p in seeds if p["id"] == "gyeonggi-urban-41110-2040")
    path = str(tmp_path / "existing.sqlite3")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-password")
    with monkeypatch.context() as legacy:
        legacy.setattr(rp, "seed_plans", lambda: deepcopy(seoul_seeds))
        old = TestClient(rp.create_app(rp.Store(db_path=path, database_url="")))
        payload = {k: deepcopy(new_seed[k]) for k in rp.PlanFields.model_fields}
        payload.update(note="기존 사용자가 보완한 설명", link={"url": "https://www.suwon.go.kr/community-plan"})
        response = old.post(BASE + "/plans", json=payload, headers=HEADERS)
        assert response.status_code == 201
        ident = response.json()["id"]
        auth = ("admin", "test-password")
        assert old.post(BASE + f"/admin/plans/{ident}", json={"version": 1, "action": "verify", "note": "기존 운영자 확인"}, headers=HEADERS, auth=auth).status_code == 200
        hidden_seed = seoul_seeds[0]
        assert old.post(BASE + f"/admin/plans/{hidden_seed['id']}", json={"version": 1, "action": "hide", "note": "기존 운영자 숨김"}, headers=HEADERS, auth=auth).status_code == 200

    # Both first migration and subsequent restarts must be idempotent.
    for _ in range(2):
        store = rp.Store(db_path=path, database_url="")
        visible = {p["id"]: p for p in store.all_plans()}
        assert len(visible) == len(seeds) - 1
        assert new_seed["id"] not in visible
        assert hidden_seed["id"] not in visible
        retained = visible[ident]
        assert retained["version"] == 2 and retained["verification"] == "source"
        assert retained["note"] == "기존 사용자가 보완한 설명"
        assert [l["url"] for l in retained["links"]] == ["https://www.suwon.go.kr/community-plan"]
        assert "gyeonggi-comprehensive-2040" in visible
        c = TestClient(rp.create_app(store))
        assert len(c.get(BASE + f"/plans/{ident}/history").json()["items"]) == 2
