"""History/heritage and metropolitan plan catalogue contracts."""
from fastapi.testclient import TestClient
from related_plans import LinkInput, PlanFields, Store, create_app, seed_plans

BASE = "/api/related-plans"


def ids(client, region, category="all", plan_type="all"):
    response = client.get(BASE, params={"region": region, "category": category, "plan_type": plan_type})
    assert response.status_code == 200, response.text
    result = response.json()
    items = result["items"]
    for n in range(2, (result["total"] + 29) // 30 + 1):
        r = client.get(BASE, params={"region": region, "category": category, "plan_type": plan_type, "page": n})
        assert r.status_code == 200
        items += r.json()["items"]
    return {x["id"] for x in items}


def test_new_categories_and_plan_types_validate():
    plans = [p for p in seed_plans() if p["id"].startswith(("heritage-", "metropolitan-"))]
    assert len(plans) == 6
    assert sum(len(p["links"]) for p in plans) == 10
    for p in plans:
        PlanFields.model_validate({k:p[k] for k in PlanFields.model_fields})
        for link in p["links"]:
            LinkInput.model_validate({k:link[k] for k in LinkInput.model_fields})


def test_history_plan_is_available_nationwide_but_asan_plan_is_local():
    c = TestClient(create_app(Store(db_path="",database_url="")))
    national = "heritage-historic-zones-master-2022-2026"
    for region in ("11", "28", "41", "43", "44", "44200"):
        assert national in ids(c,region,category="heritage")
    asan = "heritage-asan-eupnae-dongsanseong-2026"
    assert asan in ids(c,"44200",category="heritage")
    assert asan not in ids(c,"43",category="heritage")
    assert "heritage-ancient-capitals-master-2024" in ids(c,"44150",category="heritage")


def test_metro_plan_shares_scope_and_older_cheongju_plan_excludes_sibling():
    c = TestClient(create_app(Store(db_path="",database_url="")))
    for region in ("11", "28", "41", "11110", "41110"):
        assert {"metropolitan-capital-2020", "metropolitan-capital-2040-hearing"} <= ids(c,region,plan_type="metropolitan")
    for region in ("26", "43"):
        assert "metropolitan-capital-2040-hearing" not in ids(c,region,plan_type="metropolitan")
    for region in ("43110","43720","43745","43750","43760","43770"):
        assert "metropolitan-cheongju-2020" in ids(c,region,plan_type="metropolitan")
    assert "metropolitan-cheongju-2020" not in ids(c,"43130",plan_type="metropolitan")
    records = {p["id"]:p for p in seed_plans()}
    assert records["metropolitan-capital-2020"]["status"] == "historical"
    assert records["metropolitan-capital-2040-hearing"]["status"] == "hearing"


def test_total_count_and_heritage_filter(tmp_path):
    for db in ("",str(tmp_path/"heritage.sqlite3")):
        c = TestClient(create_app(Store(db_path=db,database_url="")))
        cat = c.get(BASE+"/catalog").json()
        assert cat["total"] == 238
        assert cat["categories"]["heritage"] == "역사·문화유산"
        assert cat["plan_types"]["metropolitan"] == "광역도시계획"
        assert "heritage-asan-eupnae-dongsanseong-2026" in ids(c,"44200","heritage")
