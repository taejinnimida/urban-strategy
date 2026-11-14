"""Gangwon expanded statutory plan catalogue: statuses, query scope, source linkage."""
from fastapi.testclient import TestClient
from related_plans import Store, create_app, seed_plans, PlanFields, LinkInput

BASE = "/api/related-plans"

def query(client, region, **filters):
    first = client.get(BASE,params={"region":region,**filters})
    assert first.status_code==200,first.text
    d=first.json()
    items=d["items"][:]
    for page in range(2,(d["total"]+29)//30+1):
        nxt=client.get(BASE,params={"region":region,"page":page,**filters})
        assert nxt.status_code==200,nxt.text
        items.extend(nxt.json()["items"])
    assert len(items)==d["total"]
    return {p["id"]:p for p in items}

def test_all_sector_seeds_validate_and_have_distinct_ids():
    seeds=[p for p in seed_plans() if p["id"].startswith("gangwon-sector-")]
    assert len(seeds)==20
    assert sum(len(p["links"]) for p in seeds)==27
    assert len({p["id"] for p in seeds})==20
    assert {p["region"] for p in seeds}=={"51130","51110","51150","51210","51190"}
    for p in seeds:
        PlanFields.model_validate({k:p[k] for k in PlanFields.model_fields})
        for link in p["links"]:
            LinkInput.model_validate({k:link[k] for k in LinkInput.model_fields})

def test_sector_lists_preserve_province_and_city_boundaries(tmp_path):
    for path in ("",str(tmp_path/"gangwon-sector.sqlite3")):
        c=TestClient(create_app(Store(db_path=path,database_url="")))
        assert c.get(BASE+"/catalog").json()["total"]== 267
        province=query(c,"51")
        assert len(province)==53
        assert "gangwon-sector-wonju-renewal-2030" in province
        assert "gangwon-sector-chuncheon-regen-strategy-2026" in province
        wonju=query(c,"51130")
        chuncheon=query(c,"51110")
        assert "gangwon-sector-wonju-renewal-2030" in wonju
        assert "gangwon-sector-chuncheon-regen-strategy-2026" not in wonju
        assert "gangwon-sector-chuncheon-regen-strategy-2026" in chuncheon
        assert "gangwon-sector-wonju-renewal-2030" not in chuncheon
        assert "gangwon-sector-wonju-industry-2035" in query(c,"51130",category="urban",plan_type="sector")
        assert "gangwon-sector-wonju-renewal-2030" in query(c,"51130",category="renewal")
        assert "gangwon-sector-wonju-regen-jungang-2026" in query(c,"51130",category="regeneration")

def test_drafts_are_not_assumed_to_be_approved():
    records={p["id"]:p for p in seed_plans()}
    assert records["gangwon-sector-wonju-industry-2035"]["status"]=="drafting"
    assert records["gangwon-sector-wonju-parks-2040"]["status"]=="unknown"
    assert records["gangwon-sector-chuncheon-basic-2030-amend-draft-2026"]["status"]=="hearing"
    assert records["gangwon-sector-taebaek-regen-strategy-2035"]["status"]=="drafting"
    assert records["gangwon-sector-wonju-renewal-2030"]["status"]=="approved"
    assert records["gangwon-sector-wonju-regen-jungang-2026"]["status"]=="amended"
