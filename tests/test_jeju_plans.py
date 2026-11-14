"""Jeju statutory-plan catalogue and two administrative cities."""
from fastapi.testclient import TestClient
from related_plans import Store, create_app, seed_plans, PlanFields, LinkInput

API = "/api/related-plans"

def ids(client, region):
    first = client.get(API, params={"region": region})
    assert first.status_code == 200, first.text
    data = first.json()
    items = data["items"][:]
    for page in range(2, (data["total"]+29)//30+1):
        response = client.get(API, params={"region": region, "page": page})
        assert response.status_code == 200, response.text
        items.extend(response.json()["items"])
    assert len(items) == data["total"]
    return {p["id"] for p in items}

def test_jeju_plan_seed_schema_and_sources():
    plans = [p for p in seed_plans() if p["id"].startswith("jeju-")]
    assert len(plans) == 24
    assert sum(len(p["links"]) for p in plans) == 31
    assert len({p["id"] for p in plans}) == 24
    for p in plans:
        PlanFields.model_validate({k: p[k] for k in PlanFields.model_fields})
        for l in p["links"]:
            LinkInput.model_validate({k: l[k] for k in LinkInput.model_fields})

def test_inheritance_and_local_activation_scope(tmp_path):
    for db in ("", str(tmp_path / "jeju.sqlite3")):
        client = TestClient(create_app(Store(db_path=db, database_url="")))
        assert client.get(API + "/catalog").json()["total"] == 317
        province = ids(client, "50")
        jeju_city = ids(client, "50110")
        seogwipo = ids(client, "50130")
        for name in ("jeju-international-free-city-comprehensive-3-2022",
                     "jeju-jdc-implementation-3-2022",
                     "jeju-urban-basic-2040",
                     "jeju-carbon-neutral-green-basic-1-2024"):
            assert name in province & jeju_city & seogwipo
        activation = "jeju-seogwipo-cheonji-regeneration-2026-hearing"
        assert activation in seogwipo
        assert activation not in jeju_city
        assert "jeju-urban-basic-2040" not in ids(client, "52110")

def test_jeju_stages_not_assumed_approved():
    records = {p["id"]: p for p in seed_plans()}
    assert records["jeju-international-free-city-comprehensive-3-revision-2026"]["status"] == "drafting"
    assert records["jeju-regeneration-strategy-2035-draft-2026"]["status"] == "hearing"
    assert records["jeju-landscape-4-2026-2030-hearing"]["status"] == "hearing"
    assert records["jeju-industrial-area-basic-2030"]["status"] == "unknown"
    assert records["jeju-jdc-implementation-3-2022"]["plan_type"] == "sector"
