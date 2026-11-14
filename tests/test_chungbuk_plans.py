"""Chungbuk plan seed coverage and regional lookup regression contracts."""
from fastapi.testclient import TestClient
from related_plans import LinkInput, PlanFields, Store, create_app, seed_plans

API = "/api/related-plans"
MUNICIPALITIES = {
    "43110", "43130", "43150", "43720", "43730", "43740",
    "43745", "43750", "43760", "43770", "43800",
}


def items(client, region, **params):
    first = client.get(API, params={"region": region, **params})
    assert first.status_code == 200, first.text
    result = first.json()
    found = result["items"][:]
    for page in range(2, (result["total"] + 29) // 30 + 1):
        response = client.get(API, params={"region": region, "page": page, **params})
        assert response.status_code == 200, response.text
        found.extend(response.json()["items"])
    assert len(found) == result["total"]
    return found


def test_all_eleven_chungbuk_municipalities_have_basic_plan_sources():
    chungbuk = [x for x in seed_plans() if x["id"].startswith("chungbuk-")]
    assert len(chungbuk) == 26
    assert sum(len(x["links"]) for x in chungbuk) == 34
    plans = [x for x in chungbuk if x["id"].startswith("chungbuk-basic-")]
    assert len(plans) == 11
    assert {x["region"] for x in plans} == MUNICIPALITIES
    assert all(x["category"] == "urban" and x["plan_type"] == "basic" for x in plans)
    assert all(any("eum.go.kr" in link["url"] for link in x["links"]) for x in plans)
    for record in chungbuk:
        PlanFields.model_validate({key: record[key] for key in PlanFields.model_fields})
        for link in record["links"]:
            LinkInput.model_validate({key: link[key] for key in LinkInput.model_fields})


def test_province_and_city_query_do_not_include_sibling_city_plans(tmp_path):
    for writable in (False, True):
        db = str(tmp_path / "chungbuk.sqlite3") if writable else ""
        client = TestClient(create_app(Store(db_path=db, database_url="")))
        entire = items(client, "43")
        assert len(entire) == 37
        ids = {p["id"] for p in entire}
        assert "chungbuk-basic-43110-2040" in ids
        assert "chungbuk-basic-43770-2030" in ids
        cheongju = {x["id"] for x in items(client, "43110")}
        eumseong = {x["id"] for x in items(client, "43770")}
        assert "chungbuk-basic-43110-2040" in cheongju
        assert "chungbuk-basic-43110-2040" not in eumseong
        assert "chungbuk-basic-43770-2030" in eumseong
        assert "chungbuk-basic-43770-2030" not in cheongju
        assert "chungbuk-comprehensive-2021-2040" in cheongju & eumseong
        assert {x["id"] for x in items(client, "KR")} >= ids
        assert client.get(API + "/catalog").json()["total"] == 291


def test_followup_drafts_not_mislabelled_as_approved():
    records = {p["id"]: p for p in seed_plans()}
    assert records["chungbuk-cheongju-basic-2045-in-progress"]["status"] == "drafting"
    assert records["chungbuk-jeungpyeong-basic-2040-draft"]["status"] == "hearing"
    assert records["chungbuk-regional-dev-2027-2036-draft"]["status"] == "hearing"
    assert records["chungbuk-cheongju-regeneration-strategy-2025"]["status"] == "unknown"
    assert records["chungbuk-cheongju-industrial-2030"]["plan_type"] == "sector"
    for p in records.values():
        if p["id"].startswith("chungbuk-basic-"):
            assert "게시일" in p["milestone_label"]
