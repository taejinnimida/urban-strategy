"""Nationwide plans branch: metro-wide inheritance, accuracy and independent operation."""
from fastapi.testclient import TestClient
import related_plans as rp

API = "/api/related-plans"


def entries(client, region, **params):
    response = client.get(API, params={"region": region, **params})
    assert response.status_code == 200, response.text
    data = response.json()
    items = data["items"][:]
    for page in range(2, (data["total"] + 29) // 30 + 1):
        next_page = client.get(API, params={"region": region, "page": page, **params})
        assert next_page.status_code == 200
        items.extend(next_page.json()["items"])
    assert len(items) == data["total"]
    return {x["id"]: x for x in items}


def test_capital_area_plan_is_one_record_across_three_provinces():
    client = TestClient(rp.create_app(rp.Store(db_path="", database_url="")))
    plan_id = "capital-area-reorganization-2021-2040"
    for region in ("11", "28", "41", "11140", "41110"):
        assert plan_id in entries(client, region)
    for region in ("26", "29", "42"):
        assert plan_id not in entries(client, region)
    plan = entries(client, "KR")[plan_id]
    assert plan["coverage_regions"] == ["11", "28", "41"]
    assert len([p for p in rp.seed_plans() if p["id"] == plan_id]) == 1


def test_capital_area_scope_is_allowed_but_unrelated_municipalities_are_rejected():
    accepted = rp.PlanFields.model_validate({
        "region": "11", "category": "urban", "title": "수도권 공통 시범계획",
        "coverage_regions": ["11", "28", "41"]
    })
    assert accepted.coverage_regions == ["11", "28", "41"]
    from pydantic import ValidationError
    try:
        rp.PlanFields.model_validate({
            "region": "41110", "category": "urban", "title": "시군간 잘못된 범위",
            "coverage_regions": ["41130"]
        })
    except ValidationError:
        pass
    else:
        raise AssertionError("Unrelated municipalities should not be a permitted shared scope")


def test_renewal_regeneration_and_industrial_plan_queries():
    client = TestClient(rp.create_app(rp.Store(db_path="", database_url="")))
    seoul = entries(client, "11")
    gyeonggi = entries(client, "41")
    assert "seoul-renewal-seun-2026-581" in seoul
    assert "seoul-regeneration-seoul-station-2026-564" in seoul
    assert "gyeonggi-industry-41170-2030" in gyeonggi
    assert "gyeonggi-industry-41190-2033" in gyeonggi
    draft = entries(client, "41110")["gyeonggi-regeneration-suwon-yeonghwa-2026"]
    assert draft["status"] == "hearing"
    assert entries(client, "11170")["seoul-regeneration-namsan-2026"]["status"] == "unknown"


def test_metro_seed_validation_and_record_uniqueness():
    seeds = rp.seed_plans()
    assert len(seeds) == 105
    assert len({p["id"] for p in seeds}) == 105
    for p in seeds:
        rp.PlanFields.model_validate({key: p[key] for key in rp.PlanFields.model_fields})
        for link in p["links"]:
            rp.LinkInput.model_validate({key: link[key] for key in rp.LinkInput.model_fields})
