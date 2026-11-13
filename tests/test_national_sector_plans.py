"""Nationwide industry/architecture-assets/tourism plans and provincial implementation inheritance."""
from fastapi.testclient import TestClient
from related_plans import PlanFields, LinkInput, Store, create_app, seed_plans

API="/api/related-plans"
REGIONS=["11","41","43","44","30","36"]

def get_ids(client,region,category="all"):
    response=client.get(API,params={"region":region,"category":category})
    assert response.status_code==200,response.text
    data=response.json()
    items=data["items"][:]
    for page in range(2,(data["total"]+29)//30+1):
        r=client.get(API,params={"region":region,"category":category,"page":page})
        assert r.status_code==200
        items.extend(r.json()["items"])
    assert len(items)==data["total"]
    return {p["id"] for p in items}


def test_countrywide_plans_inherit_to_seoul_gyeonggi_and_chungcheong():
    client=TestClient(create_app(Store(db_path="",database_url="")))
    national={"national-industrial-concentration-2019","national-architecture-assets-1-2016",
              "national-architecture-assets-2-2021","national-architecture-assets-3-2026-draft",
              "national-tourism-development-4-2022"}
    seeds=seed_plans()
    assert len(seeds)==202
    assert {p["id"] for p in seeds if p["id"] in national}==national
    for region in REGIONS:
        assert national<=get_ids(client,region)
        assert "national-industrial-concentration-2019" in get_ids(client,region,category="industry")
    assert get_ids(client,"26",category="industry")=={"national-industrial-concentration-2019"}


def test_provincial_assets_implementation_is_distinct_from_national_basic_plan():
    client=TestClient(create_app(Store(db_path="",database_url="")))
    by_region={"11":"seoul-architecture-assets-1-2019","41":"gyeonggi-architecture-assets-2-2023",
        "43":"chungbuk-architecture-assets-1-2020","44":"chungnam-architecture-assets-1-2019",
        "30":"daejeon-architecture-assets-1-2019","36":"sejong-architecture-assets-2-2024"}
    for region,ident in by_region.items():
        records=get_ids(client,region,category="architecture")
        assert ident in records
        assert "national-architecture-assets-2-2021" in records
        for other in REGIONS:
            if other!=region:
                assert ident not in get_ids(client,other,category="architecture")


def test_sector_categories_and_regional_tourism_available_with_existing_categories():
    client=TestClient(create_app(Store(db_path="",database_url="")))
    catalogue=client.get(API+"/catalog").json()
    assert catalogue["categories"]["industry"]=="산업·산업입지"
    assert catalogue["categories"]["tourism"]=="관광"
    assert catalogue["total"]==202
    regional={"11":"seoul","41":"gyeonggi","43":"chungbuk","44":"chungnam","30":"daejeon","36":"sejong"}
    for region,slug in regional.items():
        assert "tourism-development-7-"+slug in get_ids(client,region,category="tourism")
        assert "national-tourism-development-4-2022" in get_ids(client,region,category="tourism")


def test_metadata_links_and_stage_not_claiming_2026_national_approval():
    records={p["id"]:p for p in seed_plans()}
    for record in records.values():
        if record["id"].startswith(("national-architecture-assets-","national-industrial-","national-tourism-","tourism-development-7-")) or "-architecture-assets-" in record["id"]:
            PlanFields.model_validate({k:record[k] for k in PlanFields.model_fields})
            for link in record["links"]:
                LinkInput.model_validate({k:link[k] for k in LinkInput.model_fields})
    assert records["national-architecture-assets-3-2026-draft"]["status"]=="hearing"
    assert records["national-industrial-concentration-2019"]["status"]=="unknown"
    assert records["chungbuk-architecture-assets-2-draft"]["status"]=="unknown"
