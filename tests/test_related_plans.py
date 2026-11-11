"""Catalogue contracts: durable contributions, scope, moderation and input boundaries."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from related_plans import Store, create_app, scope_codes

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
    assert catalog["total"]==9
    assert not catalog["contributions_enabled"]
    items=c.get(BASE).json()["items"]
    assert sum(len(p["links"]) for p in items)==19
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
    assert client.get(BASE,params={"q":"2040","category":"urban"}).json()["total"]==1
