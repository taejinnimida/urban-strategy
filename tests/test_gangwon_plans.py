"""Gangwon regional seed: all 18 basic plans, province scope, and legal stage distinctions."""
from fastapi.testclient import TestClient
from related_plans import PlanFields, LinkInput, Store, create_app, seed_plans

API="/api/related-plans"
CITIES={
    "51110", "51130", "51150", "51170", "51190", "51210", "51230",
    "51720", "51730", "51750", "51760", "51770", "51780",
    "51790", "51800", "51810", "51820", "51830"
}

def items(client,region,**filters):
    response=client.get(API,params={"region":region,**filters})
    assert response.status_code==200,response.text
    result=response.json()
    out=result["items"][:]
    for n in range(2,(result["total"]+29)//30+1):
        r=client.get(API,params={"region":region,"page":n,**filters})
        assert r.status_code==200,r.text
        out.extend(r.json()["items"])
    assert len(out)==result["total"]
    return out

def test_all_18_municipalities_have_public_source_records():
    gangwon=[p for p in seed_plans() if p["id"].startswith("gangwon-")]
    assert len(gangwon)==26
    assert sum(len(p["links"]) for p in gangwon)==31
    basic=[p for p in gangwon if p["id"].startswith("gangwon-basic-")]
    assert len(basic)==18
    assert {p["region"] for p in basic}==CITIES
    assert len({p["id"] for p in gangwon})==26
    for p in gangwon:
        PlanFields.model_validate({k:p[k] for k in PlanFields.model_fields})
        for link in p["links"]:
            LinkInput.model_validate({k:link[k] for k in LinkInput.model_fields})

def test_area_lookup_inherits_gangwon_but_not_other_city(tmp_path):
    for writable in (False,True):
        path=str(tmp_path/"gangwon.sqlite3") if writable else ""
        client=TestClient(create_app(Store(db_path=path,database_url="")))
        assert len(items(client,"51"))==32   # 26 local + 6 nationally shared
        assert client.get(API+"/catalog").json()["total"]==182
        wonju={p["id"] for p in items(client,"51130")}
        inje={p["id"] for p in items(client,"51810")}
        assert "gangwon-comprehensive-2021-2040" in wonju & inje
        assert "gangwon-basic-51130-2040" in wonju
        assert "gangwon-basic-51130-2040" not in inje
        assert "gangwon-basic-51810-2040" in inje
        assert "gangwon-basic-51810-2040" not in wonju
        assert "national-industrial-concentration-2019" in wonju & inje
        assert "gangwon-basic-51810-2040" not in {p["id"] for p in items(client,"43")}
        assert len(items(client,"KR"))==182

def test_plans_keep_pending_and_past_status_separate():
    plans={p["id"]:p for p in seed_plans()}
    assert plans["gangwon-tourism-eighth-2027-2031-draft"]["status"]=="drafting"
    assert plans["gangwon-tourism-seventh-2022-2026"]["status"]=="approved"
    assert plans["gangwon-chuncheon-basic-2040-ongoing"]["status"]=="drafting"
    assert plans["gangwon-basic-51750-2035"]["status"]=="amended"
    assert plans["gangwon-comprehensive-2012-2020"]["status"]=="historical"
    assert plans["gangwon-architecture-assets-2019"]["status"]=="unknown"
