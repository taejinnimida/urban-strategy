"""Yeongnam plan seed coverage: five provinces and cross-boundary metropolitan scope."""
from fastapi.testclient import TestClient
from related_plans import Store, create_app, seed_plans, PlanFields, LinkInput

BASE="/api/related-plans"

def ids(client,region,**filters):
    r=client.get(BASE,params={"region":region,**filters})
    assert r.status_code==200,r.text
    data=r.json()
    items=data["items"][:]
    for n in range(2,(data["total"]+29)//30+1):
        extra=client.get(BASE,params={"region":region,"page":n,**filters})
        assert extra.status_code==200,extra.text
        items.extend(extra.json()["items"])
    assert len(items)==data["total"]
    return {p["id"] for p in items}

def test_seed_integrity():
    entries=[p for p in seed_plans() if p["id"].startswith("yeongnam-")]
    assert len(entries)==26
    assert sum(len(p["links"]) for p in entries)==28
    assert len({p["id"] for p in entries})==26
    assert {"26","27","31","47","48"}<=set(p["region"] for p in entries)
    for p in entries:
        PlanFields.model_validate({k:p[k] for k in PlanFields.model_fields})
        for link in p["links"]:
            LinkInput.model_validate({k:link[k] for k in LinkInput.model_fields})

def test_five_provinces_and_cross_boundary_query(tmp_path):
    for path in ("",str(tmp_path/"yeongnam.sqlite3")):
        c=TestClient(create_app(Store(db_path=path,database_url="")))
        assert c.get(BASE+"/catalog").json()["total"]==317
        for region,ident in {
            "26":"yeongnam-busan-urban-basic-2040",
            "27":"yeongnam-daegu-urban-basic-2040",
            "31":"yeongnam-ulsan-urban-basic-2035",
            "47":"yeongnam-gyeongbuk-comprehensive-2021-2040",
            "48":"yeongnam-gyeongnam-comprehensive-fourth-2021-2040"
        }.items():
            assert ident in ids(c,region)
        shared="yeongnam-busan-metropolitan-2020"
        for region in ("26","48250","48330"):
            assert shared in ids(c,region)
        for region in ("27","47190","48120"):
            assert shared not in ids(c,region)
        assert "yeongnam-yeongcheon-urban-basic-2040" in ids(c,"47230")
        assert "yeongnam-yeongcheon-urban-basic-2040" not in ids(c,"47250")

def test_historical_draft_and_scope():
    ps={p["id"]:p for p in seed_plans()}
    assert ps["yeongnam-ulsan-urban-basic-2040-hearing"]["status"]=="hearing"
    assert ps["yeongnam-busan-oldplanned-dadae-2035-draft"]["status"]=="hearing"
    assert ps["yeongnam-busan-oldplanned-haeundae-2035"]["status"]=="approved"
    assert ps["yeongnam-busan-metropolitan-2020"]["status"]=="historical"
    for name in ("yeongnam-miryang-urban-basic-2040","yeongnam-yangsan-urban-basic-2040",
                 "yeongnam-yeongju-urban-basic-2030"):
        assert "eum.go.kr" in ps[name]["links"][0]["url"]
