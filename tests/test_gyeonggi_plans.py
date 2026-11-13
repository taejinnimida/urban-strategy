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
    assert len(gg) == 70
    assert sum(len(p["links"]) for p in gg) == 98
    municipalities = {code for code, r in rp.region_index().items() if r["parent"] == "41"}
    published = [p for p in gg if p["category"] == "urban" and p["plan_type"] == "basic"
                 and p["status"] in {"approved", "amended"} and p["region"] != "41"]
    assert len(municipalities) == len(published) == 31
    assert {p["region"] for p in published} == municipalities
    assert len({p["id"] for p in seeds}) == len(seeds)
    assert len({(p["region"], p["category"], rp.search_key(p["title"])) for p in seeds}) == len(seeds)
    for p in gg:
        assert p["links"] and p["checked_at"] in {"2026-11-12", "2026-11-13"}
        assert p["created_at"].startswith(p["checked_at"])
        assert len({rp.canonical_url(l["url"]) for l in p["links"]}) == len(p["links"])
        for link in p["links"]:
            assert urlsplit(link["url"]).hostname.endswith(".go.kr")


def test_gyeonggi_catalogue_and_pagination_preserve_seoul(client):
    gg = all_items(client, region="41")
    assert len(gg) == 86
    assert all(p["region"].startswith("41") or p["region"] == "KR" or p["id"] in {"capital-area-reorganization-2021-2040", "metropolitan-capital-2020", "metropolitan-capital-2040-hearing"} for p in gg)
    seoul = all_items(client, region="11")
    assert len(seoul) == 40
    assert sum(len(p["links"]) for p in seoul) == 63
    assert len(all_items(client, region="KR")) == 221
    assert [len(client.get(BASE, params={"region": "41", "page": n}).json()["items"])
            for n in range(1, 5)] == [30, 30, 26, 0]
    catalog = client.get(BASE + "/catalog").json()
    assert catalog["counts"]["41"] == 80
    assert catalog["counts"]["KR"] == 221
    assert client.get(BASE + "/map/41").json()["features"]
    assert client.get("/plans/assets/gyeonggi-plans.json").status_code == 404


def test_city_and_district_inherit_province_without_sibling_plans(client):
    def ids(region):
        return {p["id"] for p in all_items(client, region=region)}
    expected = {p["id"] for p in rp.seed_plans() if p["region"] in {"41", "41110"}}
    assert len(expected) == 13
    nationwide = {p["id"] for p in rp.seed_plans() if p["region"] == "KR"}
    assert ids("41110") == ids("41111") == ids("41113") == expected | nationwide | {"capital-area-reorganization-2021-2040", "metropolitan-capital-2020", "metropolitan-capital-2040-hearing"}
    assert "gyeonggi-urban-41130-2035" not in ids("41110")
    assert "gyeonggi-urban-41110-2040" not in ids("41830")
    assert {p["region"] for p in all_items(client, region="41830")} == {"11", "41", "41830", "KR"}


def test_plan_status_search_and_posted_dates_are_not_approval_dates(client):
    goyang = {p["target_year"]: p for p in all_items(client, region="41280", category="urban", q="고양")}
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


@pytest.mark.parametrize("city,included,excluded", [
    ("41130", ["41135"], ["41131", "41133"]),
    ("41170", ["41173"], ["41171"]),
    ("41190", ["41192"], ["41194", "41196"]),
    ("41280", ["41285", "41287"], ["41281"]),
])
def test_newtown_plans_are_visible_only_in_their_districts(client, city, included, excluded):
    ident = f"gyeonggi-newtown-{city}-2035"
    for region in ["41", city, *included]:
        found = {p["id"] for p in all_items(client, region=region, category="renewal", q="신도시")}
        assert ident in found
    for region in excluded:
        found = {p["id"] for p in all_items(client, region=region)}
        assert ident not in found
        assert "gyeonggi-comprehensive-2040" in found
    # Map counts track local/descendant registrations; lists also inherit
    # province/city plans. A district with no local plans still has a list.
    counts = client.get(BASE + "/catalog").json()["counts"]
    assert counts[city] == {"41130": 6, "41170": 5, "41190": 5, "41280": 4}[city]
    for region in included:
        assert counts[region] == 1
    for region in excluded:
        assert counts[region] == (1 if city == "41130" else 0)
        assert len(all_items(client, region=region)) >= 6


def test_sector_search_preserves_plan_kind_stage_and_missing_dates(client):
    for category in rp.CATEGORIES:
        found = all_items(client, region="41", category=category)
        assert found and all(p["category"] == category for p in found)
    industrial = all_items(client, region="41", category="urban", plan_type="sector", q="공업지역")
    assert {p["id"] for p in industrial} == {"gyeonggi-industry-41110-2030", "gyeonggi-industry-41270-2030", "gyeonggi-industry-41170-2030", "gyeonggi-industry-41190-2033"}
    regeneration = {p["id"]: p for p in all_items(client, region="41110", category="regeneration")}
    assert regeneration["gyeonggi-regeneration-41110-published"]["target_year"] is None
    assert regeneration["gyeonggi-regeneration-41110-published"]["status"] == "unknown"
    assert regeneration["gyeonggi-regeneration-41110-2036"]["status"] == "hearing"
    assert regeneration["gyeonggi-regeneration-41110-2036"]["milestone_date"] == "2026-08-07"
    gwangju = all_items(client, region="41610", category="landscape", q="광주시")
    assert len(gwangju) == 1 and gwangju[0]["region"] == "41610"
    assert gwangju[0]["milestone_date"] == ""
    assert all(l["kind"] == "reference" for l in all_items(client, region="41410", q="산본신도시")[0]["links"])


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
