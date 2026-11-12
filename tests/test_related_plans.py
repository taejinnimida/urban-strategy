"""Catalogue contracts: durable contributions, scope, moderation and input boundaries."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from related_plans import PlanFields, LinkInput, Store, create_app, scope_codes, seed_plans

BASE = "/api/related-plans"
HEADERS = {"X-Plan-Contribution": "1"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "test-password")
    return TestClient(create_app(Store(db_path=str(tmp_path / "plans.db"), database_url="")))


def payload(**overrides):
    return {"region":"41110", "category":"urban", "title":"2040 수원시 테스트 계획", "status":"tender",
            "target_year":2040, "milestone_date":"2026-02", "milestone_label":"발주공고",
            "link":{"url":"https://www.suwon.go.kr/test?id=1", "label":"발주공고", "kind":"tender"}, **overrides}


def post(c, path, data, admin=False):
    return c.post(BASE+path, json=data, headers=HEADERS, auth=("admin","test-password") if admin else None)


def plan(c, ident):
    return next(p for p in c.get(BASE,params={"region":"KR"}).json()["items"] if p["id"]==ident)


def test_seoul_seed_and_readonly_mode():
    c=TestClient(create_app(Store(db_path="",database_url="")))
    catalog=c.get(BASE+"/catalog").json()
    assert catalog["total"]==24
    assert not catalog["contributions_enabled"]
    items=c.get(BASE).json()["items"]
    assert sum(len(p["links"]) for p in items)==40
    assert all(p["region"]=="11" for p in items)
    assert c.get(BASE,params={"region":"26"}).json()["total"]==0
    assert post(c,"/plans",payload()).status_code==503


def test_ancestor_scope_does_not_include_sibling_districts():
    assert {"KR","41","41110","41111"} <= set(scope_codes("41111"))
    assert "41113" not in scope_codes("41111")
    assert {"41111","41113","41115","41117"} <= set(scope_codes("41110"))
    assert "11110" in scope_codes("11")


def test_persists_across_clients_and_restart(client):
    r=post(client,"/plans",payload()); assert r.status_code==201, r.text
    ident=r.json()["id"]
    path=client.app.state.related_plans_store.path
    c2=TestClient(create_app(Store(db_path=path,database_url="")))
    p=plan(c2,ident)
    assert p["verification"]=="user" and p["checked_at"] is None
    assert p["target_year"]==2040 and p["milestone_date"]=="2026-02"
    assert c2.get(BASE,params={"region":"41111"}).json()["total"]==1
    assert c2.get(BASE,params={"region":"41130"}).json()["total"]==0
    assert c2.get(BASE+f"/plans/{ident}/history").json()["items"][0]["action"]=="plan_added"


def test_duplicate_title_and_url(client):
    ident=post(client,"/plans",payload()).json()["id"]
    assert post(client,"/plans",payload(title="2040수원시 테스트계획")).status_code==409
    duplicate={"url":"http://www.suwon.go.kr/test?utm_source=community&id=1#page=5"}
    assert post(client,f"/plans/{ident}/links",duplicate).status_code==409
    assert post(client,f"/plans/{ident}/links",{"url":"https://www.suwon.go.kr/test?id=2"}).status_code==201
    assert len(plan(client,ident)["links"])==2


def test_concurrent_duplicate_is_one_record(client):
    client.get(BASE+"/catalog")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:post(client,"/plans",payload()).status_code,range(4)))
    assert sorted(results)==[201,409,409,409]


@pytest.mark.parametrize("url",["javascript:alert(1)","file:///etc/passwd","https://127.0.0.1/x","https://localhost/x","https://a.internal/x","https://user:pass@example.com/x","https://example.com\\@bad.example/x"])
def test_invalid_links_rejected(client,url):
    assert post(client,"/plans",payload(link={"url":url})).status_code==422


@pytest.mark.parametrize("change",[{"region":"99999"},{"milestone_date":"2026-02-30"},{"milestone_date":"26년02월"},{"target_year":3000},{"verification":"source"},{"title":"   "}])
def test_invalid_metadata_or_forged_verification(client,change):
    assert post(client,"/plans",payload(**change)).status_code==422


def test_csrf_and_admin_auth(client):
    assert client.post(BASE+"/plans",json=payload()).status_code==403
    assert client.post(BASE+"/plans",json=payload(),headers={**HEADERS,"Origin":"https://other.example"}).status_code==403
    assert client.get(BASE+"/admin").status_code==401
    assert client.get("/plans/manage").status_code==401
    assert client.get(BASE+"/admin",auth=("admin","wrong-password")).status_code==401


def test_moderation_version_reports_and_history(client):
    ident=post(client,"/plans",payload()).json()["id"]
    assert post(client,f"/plans/{ident}/reports",{"reason":"incorrect","message":"날짜 확인 요청"}).status_code==201
    report=client.get(BASE+"/admin",auth=("admin","test-password")).json()["reports"][0]
    action={"version":1,"action":"verify","note":"공식 고시 확인"}
    assert post(client,f"/admin/plans/{ident}",action).status_code==401
    assert post(client,f"/admin/plans/{ident}",action,admin=True).status_code==200
    assert post(client,f"/admin/plans/{ident}",action,admin=True).status_code==409
    assert plan(client,ident)["verification"]=="source"
    assert post(client,f"/admin/plans/{ident}",{**action,"version":2,"action":"resolve_report","report_id":report["id"]},admin=True).status_code==200
    assert client.get(BASE+"/admin",auth=("admin","test-password")).json()["reports"]==[]
    assert post(client,f"/admin/plans/{ident}",{**action,"version":3,"action":"hide"},admin=True).status_code==200
    assert client.get(BASE,params={"region":"41110"}).json()["total"]==0
    assert client.get(BASE+f"/plans/{ident}/history").status_code==404
    assert post(client,f"/admin/plans/{ident}",{**action,"version":4,"action":"restore"},admin=True).status_code==200
    assert plan(client,ident)["version"]==5
    with sqlite3.connect(client.app.state.related_plans_store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM rp_history WHERE plan_id=?",(ident,)).fetchone()[0]==5


def test_edit_metadata_preserves_links_and_invalidates_check(client):
    ident=post(client,"/plans",payload()).json()["id"]
    fields={k:v for k,v in payload(status="drafting",milestone_date="2026-03").items() if k!="link"}
    r=post(client,f"/admin/plans/{ident}/edit",{"version":1,"plan":fields,"note":"착수 자료 확인"},admin=True)
    assert r.status_code==200,r.text
    p=plan(client,ident)
    assert p["status"]=="drafting" and p["version"]==2 and len(p["links"])==1
    assert p["verification"]=="user"
    assert post(client,f"/admin/plans/{ident}/edit",{"version":1,"plan":fields,"note":"이전 화면 수정"},admin=True).status_code==409


def test_link_moderation_is_scoped_and_preserves_seed_after_restart(client):
    p=client.get(BASE).json()["items"][0]
    link=p["links"][0]
    action={"version":1,"action":"hide","link_id":link["id"],"note":"변경 자료 확인 필요"}
    assert post(client,f"/admin/plans/{p['id']}",action,admin=True).status_code==200
    c2=TestClient(create_app(Store(db_path=client.app.state.related_plans_store.path,database_url="")))
    assert len(plan(c2,p["id"])["links"])==len(p["links"])-1
    other=next(x for x in c2.get(BASE).json()["items"] if x["id"]!=p["id"])
    assert post(c2,f"/admin/plans/{other['id']}",action,admin=True).status_code==404


def test_rate_limit_is_shared_across_instances(client):
    ident=post(client,"/plans",payload()).json()["id"]
    c2=TestClient(create_app(Store(db_path=client.app.state.related_plans_store.path,database_url="")))
    for i in range(19):
        assert post(c2,f"/plans/{ident}/reports",{"reason":"broken"}).status_code==201
    assert post(client,f"/plans/{ident}/reports",{"reason":"broken"}).status_code==429


def test_routes_and_search(client):
    assert client.get("/plans").status_code==200
    assert client.get("/plans/assets/plans.js").status_code==200
    assert client.get("/plans/assets/seoul-plans.json").status_code==404
    assert client.get(BASE+"/map/11").json()["features"]
    assert client.get(BASE,params={"region":"not-a-region"}).status_code==422
    assert client.get(BASE,params={"q":"2040","category":"urban"}).json()["total"]==2


def test_seed_metadata_and_links_are_valid():
    for p in seed_plans():
        PlanFields.model_validate({k:p[k] for k in PlanFields.model_fields})
        for link in p["links"]:
            LinkInput.model_validate({k:link[k] for k in LinkInput.model_fields})


def test_shared_area_is_shown_in_both_districts_but_not_siblings(client):
    def ids(region):
        return {p["id"] for p in client.get(BASE,params={"region":region,"q":"창동"}).json()["items"]}
    expected={"seoul-changdong-sanggye-masterplan","seoul-changdong-sanggye-regeneration"}
    assert ids("11320")==ids("11350")==ids("11")==expected
    assert ids("11680")==set()
    catalog=client.get(BASE+"/catalog").json()
    assert catalog["counts"]["KR"]==catalog["counts"]["11"]==24
    assert catalog["counts"]["11320"]==catalog["counts"]["11350"]==2


def test_policy_type_and_alias_search(client):
    result=client.get(BASE,params={"category":"parks","q":"5 분 도시","plan_type":"policy"}).json()
    assert result["total"]==1
    p=result["items"][0]
    assert p["id"]=="seoul-garden-city" and p["status"]=="announced"
    assert client.get(BASE,params={"category":"parks","plan_type":"basic"}).json()["items"][0]["id"]=="seoul-parks-2040"
    assert client.get(BASE,params={"plan_type":"living"}).json()["total"]==1
    assert client.get(BASE,params={"plan_type":"not-a-type"}).status_code==422


def test_scope_and_tags_roundtrip_and_edit(client):
    data=payload(region="11",title="시험용 공동 정원 정책",category="parks",plan_type="policy",scope_label="도봉·노원 생활권",coverage_regions=["11320","11350"],tags=["녹지 연결","동북 정원"],status="announced")
    r=post(client,"/plans",data); assert r.status_code==201,r.text
    ident=r.json()["id"]
    c2=TestClient(create_app(Store(db_path=client.app.state.related_plans_store.path,database_url="")))
    p=plan(c2,ident)
    assert p["coverage_regions"]==data["coverage_regions"] and p["tags"]==data["tags"]
    assert c2.get(BASE,params={"region":"11320","q":"동북정원"}).json()["total"]==1
    fields={k:v for k,v in data.items() if k!="link"}
    fields.update(coverage_regions=["11170"],scope_label="용산 생활권",tags=["용산 정원"])
    assert post(c2,f"/admin/plans/{ident}/edit",{"version":1,"plan":fields,"note":"대상 범위 정정"},admin=True).status_code==200
    assert c2.get(BASE,params={"region":"11320","q":"시험용"}).json()["total"]==0
    assert c2.get(BASE,params={"region":"11170","q":"용산정원"}).json()["total"]==1
    assert plan(c2,ident)["tags"]==["용산 정원"]


@pytest.mark.parametrize("extra",[{"coverage_regions":["99999"]},{"coverage_regions":["26"]},{"tags":["x"*41]},{"tags":[""]},{"plan_type":"invented"}])
def test_invalid_scope_metadata_rejected(client,extra):
    assert post(client,"/plans",payload(region="11",**extra)).status_code==422


def test_legacy_database_upgrade_preserves_data_and_is_concurrent(tmp_path):
    path=str(tmp_path/"legacy.sqlite3")
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE rp_plans (
            id TEXT PRIMARY KEY,region TEXT NOT NULL,category TEXT NOT NULL,title TEXT NOT NULL,
            title_key TEXT NOT NULL,target_year INTEGER,status TEXT NOT NULL,milestone_date TEXT NOT NULL,
            milestone_label TEXT NOT NULL,note TEXT NOT NULL,verification TEXT NOT NULL,checked_at TEXT,
            created_at TEXT NOT NULL,updated_at TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,
            hidden INTEGER NOT NULL DEFAULT 0,UNIQUE(region,category,title_key))""")
        db.execute("INSERT INTO rp_plans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",("legacy","41110","urban","기존 이용자 계획","기존이용자계획",2040,"drafting","2026-02","착수","보존할 메모","user",None,"2026-01-01","2026-02-01",7,0))
        seed=seed_plans()[0]
        keys=[x[1] for x in db.execute("PRAGMA table_info(rp_plans)")]
        untouched={**seed,"title_key":seed["title"].replace(" ","")}
        db.execute("INSERT INTO rp_plans VALUES ("+",".join("?" for _ in keys)+")",tuple(untouched[k] for k in keys))
    stores=[Store(db_path=path,database_url="") for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs=list(pool.map(lambda s:s.all_plans(),stores))
    for output in outputs:
        old=next(p for p in output if p["id"]=="legacy")
        assert old["version"]==7 and old["note"]=="보존할 메모" and old["status"]=="drafting"
        assert old["coverage_regions"]==old["tags"]==[] and old["plan_type"]=="unspecified"
        assert next(p for p in output if p["id"]==seed["id"])["plan_type"]=="basic"
    assert len(stores[0].all_plans())==25
