"""Honam historical-plan coverage in the integrated Jeonnam-Gwangju region."""
from fastapi.testclient import TestClient
from related_plans import Store, PlanFields, LinkInput, seed_plans, create_app

API="/api/related-plans"
GWANGJU=("12210","12240","12270","12300","12330")

def pages(client,region):
    first=client.get(API,params={"region":region})
    assert first.status_code==200,first.text
    d=first.json()
    found=d["items"][:]
    for page in range(2,(d["total"]+29)//30+1):
        response=client.get(API,params={"region":region,"page":page})
        assert response.status_code==200,response.text
        found.extend(response.json()["items"])
    return {p["id"]:p for p in found}

def test_honam_seeds_present_and_metadata_valid():
    honam=[p for p in seed_plans() if p["id"].startswith("honam-")]
    assert len(honam)==29
    assert sum(len(x["links"]) for x in honam)==34
    assert len({x["id"] for x in honam})==29
    for p in honam:
        PlanFields.model_validate({key:p[key] for key in PlanFields.model_fields})
        for l in p["links"]:
            LinkInput.model_validate({key:l[key] for key in LinkInput.model_fields})

def test_integrated_regions_preserve_legacy_subregion_scopes(tmp_path):
    for path in ("",str(tmp_path/"honam.sqlite3")):
        c=TestClient(create_app(Store(db_path=path,database_url="")))
        assert c.get(API+"/catalog").json()["total"]==267
        gwangju=pages(c,"12210")
        suncheon=pages(c,"12150")
        jeonju=pages(c,"52110")
        assert "honam-gwangju-urban-2040" in gwangju
        assert "honam-gwangju-urban-2040" not in suncheon
        assert "honam-jeonnam-comprehensive-2021-2040" in suncheon
        assert "honam-jeonnam-comprehensive-2021-2040" not in gwangju
        assert "honam-jeonbuk-comprehensive-2021-2040" in jeonju
        assert "honam-jeonbuk-comprehensive-2021-2040" not in suncheon
        assert "honam-basic-12150-2040" in suncheon
        assert "honam-basic-12150-2040" not in jeonju
        for region in GWANGJU:
            assert "honam-gwangju-renewal-2030-2022" in pages(c,region)

def test_public_notice_and_draft_distinction():
    p={a["id"]:a for a in seed_plans()}
    assert p["honam-jeonju-renewal-2030-2026-corrected"]["status"]=="amended"
    assert p["honam-namwon-basic-2040-2026-draft"]["status"]=="hearing"
    assert p["honam-yeosu-smartcity-2025"]["status"]=="unknown"
    assert p["honam-haenam-management-2030-2026"]["status"]=="amended"
