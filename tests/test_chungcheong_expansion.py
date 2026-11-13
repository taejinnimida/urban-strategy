"""Chungcheong first-phase expansion: 13 published Chungnam plans and pending Cheongyang."""
from fastapi.testclient import TestClient
from related_plans import PlanFields, LinkInput, Store, create_app, seed_plans

BASE="/api/related-plans"
KNOWN_CHUNGNAM={"44130","44150","44180","44200","44210","44230","44250","44270","44710","44760","44800","44810","44825"}
EXPECTED_ALL=set(KNOWN_CHUNGNAM)|{"44790","44770"}

def gather(client,region,category="all"):
    response=client.get(BASE,params={"region":region,"category":category})
    assert response.status_code==200,response.text
    d=response.json()
    items=d["items"][:]
    for page in range(2,(d["total"]+29)//30+1):
        r=client.get(BASE,params={"region":region,"category":category,"page":page})
        assert r.status_code==200,r.text
        items.extend(r.json()["items"])
    assert len(items)==d["total"]
    return {x["id"]:x for x in items}

def test_seed_files_cover_13_published_and_pending_cheongyang():
    entries=[p for p in seed_plans() if p["id"].startswith(("chungnam-basic-","chungnam-cheongyang-","chungnam-comprehensive-","daejeon-urban-","daejeon-industrial-","sejong-urban-"))]
    assert len(entries)==19
    assert len({p["id"] for p in entries})==19
    basic=[p for p in entries if p["id"].startswith("chungnam-basic-")]
    assert len(basic)==13
    assert {p["region"] for p in basic}==KNOWN_CHUNGNAM
    assert "44770" in EXPECTED_ALL - {p["region"] for p in entries}
    assert len(seed_plans())==221
    for p in entries:
        PlanFields.model_validate({k:p[k] for k in PlanFields.model_fields})
        for l in p["links"]:
            LinkInput.model_validate({k:l[k] for k in LinkInput.model_fields})

def test_province_scope_and_sibling_exclusion(tmp_path):
    for db in ("",str(tmp_path/"chungcheong.sqlite3")):
        c=TestClient(create_app(Store(db_path=db,database_url="")))
        assert c.get(BASE+"/catalog").json()["total"]==221
        chn=gather(c,"44")
        assert "chungnam-comprehensive-2021-2040" in chn
        assert "chungnam-basic-44130-2035" in chn
        assert "chungnam-basic-44130-2035" in gather(c,"44130")
        assert "chungnam-basic-44130-2035" not in gather(c,"44150")
        assert "chungnam-cheongyang-basic-2040-drafting" in gather(c,"44790")
        assert "chungnam-basic-44760-2040" not in gather(c,"44790")
        assert "daejeon-urban-basic-2040" in gather(c,"30")
        assert "sejong-urban-basic-2040" in gather(c,"36")
        assert "daejeon-industrial-basic-2030" not in gather(c,"36")
        assert "national-industrial-concentration-2019" in gather(c,"36")

def test_planning_statuses_and_source_dates_remain_distinct():
    rec={p["id"]:p for p in seed_plans()}
    assert rec["chungnam-cheongyang-basic-2040-drafting"]["status"]=="drafting"
    assert rec["chungnam-cheongyang-industrial-drafting"]["status"]=="drafting"
    assert rec["daejeon-urban-basic-2040"]["status"]=="approved"
    assert rec["sejong-urban-basic-2040"]["milestone_label"].find("등록일")>=0
    assert rec["chungnam-basic-44810-2040"]["milestone_label"].find("승인일 아님")>=0
