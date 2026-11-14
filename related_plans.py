"""Shared, append-first plan link catalogue. Independent of Seoul business RULEs.

Storage: RELATED_PLANS_DATABASE_URL or DATABASE_URL (PostgreSQL); alternatively
RELATED_PLANS_DB_PATH on a durable disk. Without either, serve seeds read-only.
No outbound requests are made for user-supplied URLs.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

ASSETS = Path(__file__).with_name("plan_catalog_assets")
SEED_FILES = ("seoul-plans.json", "gyeonggi-plans.json", "metro-plans-additions.json", "chungbuk-plans.json", "heritage-metropolitan-plans.json", "national-sector-plans.json", "gangwon-plans.json", "gangwon-sector-additions.json", "chungcheong-expansion.json", "related-plans-catchup.json", "honam-plans.json", "jeju-plans.json")
CATEGORIES = {"urban":"도시계획", "architecture":"건축", "landscape":"경관", "parks":"공원·녹지", "renewal":"정비", "transport":"교통", "regeneration":"재생", "heritage":"역사·문화유산", "industry":"산업·산업입지", "tourism":"관광", "environment":"환경·기후", "other":"기타"}
PLAN_TYPES = {"unspecified":"미분류", "metropolitan":"광역도시계획", "basic":"기본·종합계획", "sector":"부문별계획", "living":"생활권계획", "area":"도심·중심지 특화계획", "policy":"정책·사업구상"}
STATUSES = {"unknown":"상태 미확인", "tender":"발주 확인", "drafting":"수립 중", "hearing":"공람·공청회", "approved":"수립완료", "amended":"변경완료", "announced":"계획·정책 발표", "historical":"이전 계획"}
KINDS = {"document":"계획서 원문", "notice":"고시·공고", "tender":"발주공고", "hearing":"공람·공청회", "reference":"관련자료"}
METADATA_COLUMNS = {"plan_type":"TEXT NOT NULL DEFAULT 'unspecified'", "scope_label":"TEXT NOT NULL DEFAULT ''", "coverage_regions":"TEXT NOT NULL DEFAULT '[]'", "tags":"TEXT NOT NULL DEFAULT '[]'"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@lru_cache(maxsize=1)
def region_data():
    return json.loads((ASSETS/"regions.json").read_text(encoding="utf-8"))


def region_index():
    return {r["id"]:r for r in region_data()["regions"]}


def ancestors(code):
    index = region_index()
    result = []
    while code:
        result.append(code)
        code = index[code]["parent"]
    return result


def scope_codes(code):
    index = region_index()
    if code not in index:
        raise HTTPException(422, "등록된 행정구역을 선택해주세요.")
    return sorted(set(ancestors(code)) | {r for r in index if code in ancestors(r)})


def plan_regions(plan):
    return plan.get("coverage_regions") or [plan["region"]]


def search_key(value):
    return re.sub(r"\s+", "", value).casefold()


def stored_fields(fields):
    return {k:json.dumps(v,ensure_ascii=False) if k in {"coverage_regions","tags"} else v for k,v in fields.items()}


def safe_url(value):
    value = value.strip()
    if len(value) > 2000 or re.search(r"[\s\\\x00-\x1f\x7f]", value):
        raise ValueError("공개된 웹페이지의 http 또는 https 주소를 입력해주세요.")
    try:
        u = urlsplit(value)
        host = (u.hostname or "").lower().rstrip(".")
        port = u.port
    except ValueError as exc:
        raise ValueError("링크 형식을 확인해주세요.") from exc
    if u.scheme.lower() not in ("http", "https") or not host or u.username or u.password or port not in (None,80,443):
        raise ValueError("공개된 웹페이지의 http 또는 https 주소를 입력해주세요.")
    if "." not in host or host.endswith((".local", ".localhost", ".internal")):
        raise ValueError("공개된 기관의 웹주소를 입력해주세요.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("IP 주소 대신 기관의 공개 웹주소를 입력해주세요.")
    host = host.encode("idna").decode("ascii")
    if not re.fullmatch(r"[a-z0-9.-]+",host):
        raise ValueError("웹주소의 도메인을 확인해주세요.")
    return urlunsplit((u.scheme.lower(),host+(f":{port}" if port and port not in (80,443) else ""),u.path or "/",u.query,u.fragment))


def canonical_url(url):
    u = urlsplit(url)
    params = [(k,v) for k,v in parse_qsl(u.query,keep_blank_values=True) if not k.lower().startswith("utm_") and k.lower() not in {"fbclid","gclid","tr_code"}]
    return urlunsplit(("https",u.hostname or "",u.path.rstrip("/") or "/",urlencode(sorted(params)),""))


def partial_date(value):
    value = value.strip()
    if not value:
        return ""
    if not re.fullmatch(r"(?:19|20|21)\d{2}(?:-(?:0[1-9]|1[0-2])(?:-(?:0[1-9]|[12]\d|3[01]))?)?",value):
        raise ValueError("날짜는 YYYY, YYYY-MM 또는 YYYY-MM-DD로 입력해주세요.")
    if len(value) == 10:
        date.fromisoformat(value)
    return value


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LinkInput(Input):
    url: str = Field(min_length=8,max_length=2000)
    label: str = Field(default="관련자료",min_length=1,max_length=120)
    kind: Literal["document","notice","tender","hearing","reference"] = "reference"
    note: str = Field(default="",max_length=600)
    _url = field_validator("url")(safe_url)


class PlanFields(Input):
    region: str = Field(max_length=5)
    category: Literal["urban","architecture","landscape","parks","renewal","transport","regeneration","heritage","industry","tourism","environment","other"]
    plan_type: Literal["unspecified","metropolitan","basic","sector","living","area","policy"] = "unspecified"
    scope_label: str = Field(default="",max_length=120)
    coverage_regions: list[str] = Field(default_factory=list,max_length=60)
    tags: list[str] = Field(default_factory=list,max_length=20)
    title: str = Field(min_length=3,max_length=180)
    target_year: int | None = Field(default=None,ge=1900,le=2199)
    status: Literal["unknown","tender","drafting","hearing","approved","amended","announced","historical"] = "unknown"
    milestone_date: str = Field(default="",max_length=10)
    milestone_label: str = Field(default="",max_length=100)
    note: str = Field(default="",max_length=1000)
    _date = field_validator("milestone_date")(partial_date)

    @field_validator("region")
    @classmethod
    def region_valid(cls,value):
        if value not in region_index():
            raise ValueError("등록된 행정구역을 선택해주세요.")
        return value

    @field_validator("coverage_regions")
    @classmethod
    def coverage_valid(cls,value,info):
        index=region_index()
        primary = info.data.get("region")
        for code in value:
            if code not in index or primary not in index:
                raise ValueError("등록된 행정구역을 선택해주세요.")
            within = primary in ancestors(code)
            cross_province = index[primary]["parent"] == "KR" and index[code]["parent"] == "KR"
            if not (within or cross_province):
                raise ValueError("대상 범위는 선택한 지역 안 또는 동일 단계의 시·도에서 지정해주세요.")
        return list(dict.fromkeys(value))

    @field_validator("tags")
    @classmethod
    def tags_valid(cls,value):
        value=[tag.strip() for tag in value]
        if any(not tag or len(tag)>40 for tag in value):
            raise ValueError("검색어는 각각 1~40자로 입력해주세요.")
        return list(dict.fromkeys(value))


class PlanInput(PlanFields):
    link: LinkInput


class EditInput(Input):
    version: int = Field(ge=1)
    plan: PlanFields
    note: str = Field(min_length=3,max_length=1000)


class ReportInput(Input):
    reason: Literal["broken","outdated","incorrect","duplicate","spam"]
    message: str = Field(default="",max_length=1000)


class AdminInput(Input):
    version: int = Field(ge=1)
    action: Literal["verify","unverify","hide","restore","resolve_report"]
    link_id: str | None = Field(default=None,max_length=80)
    report_id: str | None = Field(default=None,max_length=80)
    note: str = Field(min_length=3,max_length=1000)


class Store:
    def __init__(self, db_path=None, database_url=None):
        self.url = database_url if database_url is not None else (os.getenv("RELATED_PLANS_DATABASE_URL") or os.getenv("DATABASE_URL") or "").strip()
        self.path = db_path if db_path is not None else os.getenv("RELATED_PLANS_DB_PATH", "").strip()
        if not self.path and not self.url and Path("/var/data").is_dir():
            self.path = "/var/data/related_plans.sqlite3"
        self.writable = bool(self.path or self.url)
        self.ready = False
        self.lock = threading.Lock()
        self.salt = os.getenv("RELATED_PLANS_RATE_SALT") or os.getenv("ADMIN_PASSWORD") or "related-plans-rate-v1"

    @contextmanager
    def connection(self):
        if self.url:
            import psycopg
            from psycopg.rows import dict_row
            conn = psycopg.connect(self.url, row_factory=dict_row, connect_timeout=5)
        else:
            Path(self.path).parent.mkdir(parents=True,exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=10)
            conn.row_factory = sqlite3.Row
        try:
            if not self.url:
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA busy_timeout=10000")
                # Concurrent first connections can race while enabling WAL;
                # SQLite's busy timeout does not cover every journal-mode lock.
                deadline = time.monotonic() + 10
                while True:
                    try:
                        if conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                            conn.execute("PRAGMA journal_mode=WAL")
                        break
                    except sqlite3.OperationalError as exc:
                        if getattr(exc,"sqlite_errorcode",None) not in {sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED} or time.monotonic() >= deadline:
                            raise
                        time.sleep(0.05)
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def sql(self, conn, query, values=()):
        return conn.execute(query.replace("?","%s") if self.url else query,values)

    def ensure(self):
        if self.ready or not self.writable:
            return
        with self.lock:
            if self.ready:
                return
            with self.connection() as c:
                if self.url:
                    self.sql(c,"SELECT pg_advisory_xact_lock(701110)")
                else:
                    self.sql(c,"BEGIN IMMEDIATE")
                self.sql(c,"""CREATE TABLE IF NOT EXISTS rp_plans (
                    id TEXT PRIMARY KEY,region TEXT NOT NULL,category TEXT NOT NULL,title TEXT NOT NULL,
                    title_key TEXT NOT NULL,target_year INTEGER,status TEXT NOT NULL,milestone_date TEXT NOT NULL,
                    milestone_label TEXT NOT NULL,note TEXT NOT NULL,verification TEXT NOT NULL,checked_at TEXT,
                    created_at TEXT NOT NULL,updated_at TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,
                    hidden INTEGER NOT NULL DEFAULT 0,UNIQUE(region,category,title_key))""")
                # Additive migration: preserve existing community entries and moderation.
                existing=set() if self.url else {r["name"] for r in self.sql(c,"PRAGMA table_info(rp_plans)").fetchall()}
                for name,definition in METADATA_COLUMNS.items():
                    if self.url or name not in existing:
                        self.sql(c,f"ALTER TABLE rp_plans ADD COLUMN {'IF NOT EXISTS ' if self.url else ''}{name} {definition}")
                self.sql(c,"""CREATE TABLE IF NOT EXISTS rp_links (
                    id TEXT PRIMARY KEY,plan_id TEXT NOT NULL REFERENCES rp_plans(id),url TEXT NOT NULL,
                    canonical TEXT NOT NULL,label TEXT NOT NULL,kind TEXT NOT NULL,note TEXT NOT NULL,
                    verification TEXT NOT NULL,checked_at TEXT,created_at TEXT NOT NULL,hidden INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(plan_id,canonical))""")
                self.sql(c,"CREATE TABLE IF NOT EXISTS rp_history (id TEXT PRIMARY KEY,plan_id TEXT NOT NULL REFERENCES rp_plans(id),action TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL)")
                self.sql(c,"CREATE TABLE IF NOT EXISTS rp_reports (id TEXT PRIMARY KEY,plan_id TEXT NOT NULL REFERENCES rp_plans(id),reason TEXT NOT NULL,message TEXT NOT NULL,created_at TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'open')")
                self.sql(c,"CREATE TABLE IF NOT EXISTS rp_limits (bucket TEXT PRIMARY KEY,window INTEGER NOT NULL,count INTEGER NOT NULL)")
                self.sql(c,"CREATE INDEX IF NOT EXISTS rp_plans_region ON rp_plans(region,hidden)")
                self.sql(c,"CREATE INDEX IF NOT EXISTS rp_links_plan ON rp_links(plan_id)")
                for p in seed_plans():
                    self.insert_plan(c,p,seed=True)
                    # Only enrich untouched v1 seeds; never overwrite user edits or hidden rows.
                    values=stored_fields({k:p[k] for k in METADATA_COLUMNS})
                    self.sql(c,"UPDATE rp_plans SET "+",".join(k+"=?" for k in values)+" WHERE id=? AND version=1 AND hidden=0 AND plan_type='unspecified'",(*values.values(),p["id"]))
            self.ready = True

    def insert_plan(self,c,p,seed=False):
        keys = ("id","region","category","title","title_key","target_year","status","milestone_date","milestone_label","note","verification","checked_at","created_at","updated_at",*METADATA_COLUMNS)
        p = stored_fields({**p,"title_key":search_key(p["title"])})
        self.sql(c,f"INSERT INTO rp_plans ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})"+(" ON CONFLICT DO NOTHING" if seed else ""),tuple(p.get(k) for k in keys))
        if seed and not self.sql(c,"SELECT id FROM rp_plans WHERE id=?",(p["id"],)).fetchone():
            # A community entry may already own the same region/category/title.
            # Preserve it; links cannot reference the seed ID that was skipped.
            return
        for link in p["links"]:
            self.insert_link(c,p["id"],link,seed=seed)

    def insert_link(self,c,plan_id,link,seed=False):
        keys=("id","plan_id","url","canonical","label","kind","note","verification","checked_at","created_at")
        link={**link,"plan_id":plan_id,"canonical":canonical_url(link["url"])}
        self.sql(c,f"INSERT INTO rp_links ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})"+(" ON CONFLICT DO NOTHING" if seed else ""),tuple(link.get(k) for k in keys))

    def history(self,c,plan_id,action,payload):
        self.sql(c,"INSERT INTO rp_history VALUES (?,?,?,?,?)",(str(uuid.uuid4()),plan_id,action,json.dumps(payload,ensure_ascii=False),now()))

    def rate_limit(self,c,request):
        # Use the ASGI client supplied by the configured trusted proxy, not a spoofable header.
        ip = request.client.host if request.client else "unknown"
        window = int(time.time())//600
        bucket = hashlib.sha256((self.salt+":"+ip+":"+str(window)).encode()).hexdigest()
        row = self.sql(c,"INSERT INTO rp_limits(bucket,window,count) VALUES (?,?,1) ON CONFLICT(bucket) DO UPDATE SET count=rp_limits.count+1 RETURNING count",(bucket,window)).fetchone()
        if row["count"] > 20:
            raise HTTPException(429,"등록 요청이 많습니다. 잠시 후 다시 시도해주세요.",headers={"Retry-After":"600"})
        self.sql(c,"DELETE FROM rp_limits WHERE window < ?",(window-2,))

    def all_plans(self,include_hidden=False):
        if not self.writable:
            return seed_plans()
        self.ensure()
        with self.connection() as c:
            plans = [dict(x) for x in self.sql(c,"SELECT * FROM rp_plans"+("" if include_hidden else " WHERE hidden=0")).fetchall()]
            links = [dict(x) for x in self.sql(c,"SELECT * FROM rp_links"+("" if include_hidden else " WHERE hidden=0")).fetchall()]
        by_plan = {}
        for link in links:
            link.pop("canonical",None)
            by_plan.setdefault(link["plan_id"],[]).append(link)
        for p in plans:
            p.pop("title_key",None)
            for key in ("coverage_regions","tags"):
                p[key]=json.loads(p[key])
            p["links"] = by_plan.get(p["id"],[])
        return plans


def seed_plans():
    result = []
    for filename in SEED_FILES:
        result.extend(json.loads((ASSETS/filename).read_text(encoding="utf-8"))["plans"])
    for p in result:
        p.update(version=1,hidden=0)
        for key,value in {"plan_type":"unspecified","scope_label":"","coverage_regions":[],"tags":[]}.items():
            p.setdefault(key,value)
        p.setdefault("created_at","2026-11-11T00:00:00+00:00")
        p.setdefault("updated_at",p["created_at"])
        for i,l in enumerate(p["links"]):
            l.setdefault("id",p["id"]+"-link-"+str(i+1))
            l.setdefault("created_at",p["created_at"])
            l.setdefault("note","")
    return result


def contribution_guard(request: Request):
    if request.headers.get("x-plan-contribution") != "1":
        raise HTTPException(403,"등록 화면에서 다시 시도해주세요.")
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403,"다른 사이트에서의 등록 요청은 허용하지 않습니다.")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403,"다른 사이트에서의 등록 요청은 허용하지 않습니다.")


def default_admin(request: Request):
    from base64 import b64decode
    password = os.getenv("ADMIN_PASSWORD","")
    header = request.headers.get("authorization","")
    try:
        user,supplied=b64decode(header[6:],validate=True).decode().split(":",1) if header.startswith("Basic ") else ("","")
    except Exception:
        user,supplied="",""
    if not password or user != "admin" or not secrets.compare_digest(password.encode(),supplied.encode()):
        raise HTTPException(401,"관리자 인증이 필요합니다.",headers={"WWW-Authenticate":"Basic"})
    return True


def install(app: FastAPI, admin_auth=None, store=None):
    store=store or Store()
    admin_auth=admin_auth or default_admin
    api=APIRouter(prefix="/api/related-plans")

    def ready_write():
        if not store.writable:
            raise HTTPException(503,"자료 등록을 준비하고 있습니다. 지금은 등록된 자료를 조회할 수 있습니다.")
        store.ensure()

    @app.get("/plans", include_in_schema=False)
    def page():
        return FileResponse(ASSETS/"index.html",headers={"Cache-Control":"no-cache","X-Content-Type-Options":"nosniff"})

    @app.get("/plans/manage", include_in_schema=False, dependencies=[Depends(admin_auth)])
    def management():
        return FileResponse(ASSETS/"index.html",headers={"Cache-Control":"no-store"})

    @app.get("/plans/assets/{name}",include_in_schema=False)
    def assets(name:str):
        if name not in {"plans.js","plans.css"}:
            raise HTTPException(404)
        return FileResponse(ASSETS/name,headers={"Cache-Control":"no-cache","X-Content-Type-Options":"nosniff"})

    @api.get("/catalog")
    def catalog():
        plans=store.all_plans()
        counts={code:0 for code in region_index()}
        for p in plans:
            for code in {a for target in plan_regions(p) for a in ancestors(target)}:
                counts[code]+=1
        return {"regions":region_data()["regions"],"categories":CATEGORIES,"plan_types":PLAN_TYPES,"statuses":STATUSES,"link_kinds":KINDS,
                "counts":counts,"total":len(plans),"contributions_enabled":store.writable,"map_source":region_data()["metadata"]}

    @api.get("/map/{region}")
    def region_map(region:str):
        if region not in region_index():
            raise HTTPException(404)
        return {"features":region_data()["maps"].get(region,[]),"viewBox":region_data().get("viewboxes",{}).get(region,"0 0 500 550")}

    @api.get("")
    def listing(region:str="11",category:str="all",status:str="all",plan_type:str="all",q:str=Query(default="",max_length=180),page:int=Query(default=1,ge=1)):
        codes=set(scope_codes(region))
        if category not in {"all",*CATEGORIES} or status not in {"all",*STATUSES} or plan_type not in {"all",*PLAN_TYPES}:
            raise HTTPException(422,"검색조건을 확인해주세요.")
        index=region_index()
        plans=[p for p in store.all_plans() if codes.intersection(plan_regions(p))
               and (category=="all" or p["category"]==category) and (status=="all" or p["status"]==status)
               and (plan_type=="all" or p["plan_type"]==plan_type)
               and search_key(q) in search_key(" ".join([p["title"],p["scope_label"],*p["tags"],*(index[r]["name"] for r in plan_regions(p))]))]
        plans.sort(key=lambda p:(p["milestone_date"],p["target_year"] or 0,p["title"]),reverse=True)
        return {"items":plans[(page-1)*30:page*30],"total":len(plans),"page":page,"page_size":30,"region":region,"schema_version":2}

    @api.get("/plans/{plan_id}/history")
    def history(plan_id:str):
        plans={p["id"]:p for p in store.all_plans()}
        if plan_id not in plans:
            raise HTTPException(404,"계획을 찾을 수 없습니다.")
        if not store.writable:
            return {"items":[]}
        with store.connection() as c:
            rows=store.sql(c,"SELECT action,created_at FROM rp_history WHERE plan_id=? ORDER BY created_at DESC LIMIT 100",(plan_id,)).fetchall()
        return {"items":[dict(x) for x in rows]}

    @api.post("/plans",status_code=201,dependencies=[Depends(contribution_guard)])
    def add_plan(data:PlanInput,request:Request):
        ready_write()
        plan=data.model_dump(exclude={"link"})
        plan.update(id=str(uuid.uuid4()),verification="user",checked_at=None,created_at=now(),updated_at=now())
        link=data.link.model_dump()
        link.update(id=str(uuid.uuid4()),verification="user",checked_at=None,created_at=now())
        plan["links"]=[link]
        with store.connection() as c:
            store.rate_limit(c,request)
            # Serialise catalogue mutations across processes, including PostgreSQL workers.
            if store.url:
                store.sql(c,"SELECT pg_advisory_xact_lock(701111)")
            key=re.sub(r"\s+","",plan["title"]).casefold()
            duplicate=store.sql(c,"SELECT id,hidden FROM rp_plans WHERE region=? AND category=? AND title_key=?",(plan["region"],plan["category"],key)).fetchone()
            if duplicate:
                raise HTTPException(409,{"message":"이미 등록된 계획입니다. 기존 계획에 자료를 추가해주세요.","plan_id":duplicate["id"]})
            store.insert_plan(c,plan)
            store.history(c,plan["id"],"plan_added",{"title":plan["title"]})
        return {"ok":True,"id":plan["id"],"verification":"user"}

    @api.post("/plans/{plan_id}/links",status_code=201,dependencies=[Depends(contribution_guard)])
    def add_link(plan_id:str,data:LinkInput,request:Request):
        ready_write()
        link=data.model_dump()
        link.update(id=str(uuid.uuid4()),verification="user",checked_at=None,created_at=now())
        with store.connection() as c:
            store.rate_limit(c,request)
            if store.url:
                store.sql(c,"SELECT pg_advisory_xact_lock(701111)")
            p=store.sql(c,"SELECT id FROM rp_plans WHERE id=? AND hidden=0",(plan_id,)).fetchone()
            if not p:
                raise HTTPException(404,"계획을 찾을 수 없습니다.")
            if store.sql(c,"SELECT id FROM rp_links WHERE plan_id=? AND canonical=?",(plan_id,canonical_url(link["url"]))).fetchone():
                raise HTTPException(409,"이미 등록된 링크입니다.")
            store.insert_link(c,plan_id,link)
            store.sql(c,"UPDATE rp_plans SET updated_at=?,version=version+1 WHERE id=?",(now(),plan_id))
            store.history(c,plan_id,"link_added",{"id":link["id"],"url":link["url"],"note":link["note"]})
        return {"ok":True,"id":link["id"]}

    @api.post("/plans/{plan_id}/reports",status_code=201,dependencies=[Depends(contribution_guard)])
    def report(plan_id:str,data:ReportInput,request:Request):
        ready_write()
        with store.connection() as c:
            store.rate_limit(c,request)
            if not store.sql(c,"SELECT id FROM rp_plans WHERE id=? AND hidden=0",(plan_id,)).fetchone():
                raise HTTPException(404,"계획을 찾을 수 없습니다.")
            store.sql(c,"INSERT INTO rp_reports(id,plan_id,reason,message,created_at) VALUES (?,?,?,?,?)",(str(uuid.uuid4()),plan_id,data.reason,data.message,now()))
        return {"ok":True}

    @api.get("/admin",dependencies=[Depends(admin_auth)])
    def admin_data():
        ready_write()
        with store.connection() as c:
            reports=[dict(x) for x in store.sql(c,"SELECT * FROM rp_reports WHERE status='open' ORDER BY created_at DESC LIMIT 200").fetchall()]
        return {"plans":store.all_plans(include_hidden=True),"reports":reports}

    @api.post("/admin/plans/{plan_id}",dependencies=[Depends(admin_auth),Depends(contribution_guard)])
    def moderate(plan_id:str,data:AdminInput):
        ready_write()
        with store.connection() as c:
            p=store.sql(c,"SELECT * FROM rp_plans WHERE id=?",(plan_id,)).fetchone()
            if not p:
                raise HTTPException(404)
            changed=store.sql(c,"UPDATE rp_plans SET version=version+1,updated_at=? WHERE id=? AND version=?",(now(),plan_id,data.version))
            if changed.rowcount != 1:
                raise HTTPException(409,"다른 수정이 반영되었습니다. 새로고침 후 확인해주세요.")
            if data.action=="resolve_report":
                changed=store.sql(c,"UPDATE rp_reports SET status='resolved' WHERE id=? AND plan_id=?",(data.report_id,plan_id))
                if changed.rowcount != 1:
                    raise HTTPException(404,"신고를 찾을 수 없습니다.")
            else:
                table="rp_links" if data.link_id else "rp_plans"
                condition="id=? AND plan_id=?" if data.link_id else "id=?"
                ids=(data.link_id,plan_id) if data.link_id else (plan_id,)
                before_link = store.sql(c,"SELECT * FROM rp_links WHERE id=? AND plan_id=?",ids).fetchone() if data.link_id else None
                if data.action in {"hide","restore"}:
                    changed=store.sql(c,f"UPDATE {table} SET hidden=? WHERE {condition}",(int(data.action=="hide"),*ids))
                else:
                    verification="source" if data.action=="verify" else "user"
                    changed=store.sql(c,f"UPDATE {table} SET verification=?,checked_at=? WHERE {condition}",(verification,now() if verification=="source" else None,*ids))
                if changed.rowcount != 1:
                    raise HTTPException(404,"자료를 찾을 수 없습니다.")
            store.history(c,plan_id,"admin_"+data.action,{"before":dict(p),"before_link":dict(before_link) if data.action!="resolve_report" and before_link else None,**data.model_dump()})
        return {"ok":True}

    @api.post("/admin/plans/{plan_id}/edit",dependencies=[Depends(admin_auth),Depends(contribution_guard)])
    def edit_plan(plan_id:str,data:EditInput):
        ready_write()
        fields=data.plan.model_dump()
        fields["title_key"]=re.sub(r"\s+","",fields["title"]).casefold()
        with store.connection() as c:
            if store.url:
                store.sql(c,"SELECT pg_advisory_xact_lock(701111)")
            # Acquire SQLite's writer lock before reading and checking duplicates.
            changed=store.sql(c,"UPDATE rp_plans SET version=version+1 WHERE id=? AND version=?",(plan_id,data.version))
            if changed.rowcount!=1:
                raise HTTPException(409,"다른 수정이 반영되었거나 계획이 없습니다. 새로고침 후 확인해주세요.")
            previous=store.sql(c,"SELECT * FROM rp_plans WHERE id=?",(plan_id,)).fetchone()
            duplicate=store.sql(c,"SELECT id FROM rp_plans WHERE region=? AND category=? AND title_key=? AND id<>?",(fields["region"],fields["category"],fields["title_key"],plan_id)).fetchone()
            if duplicate:
                raise HTTPException(409,"동일한 이름의 계획이 이미 등록되어 있습니다.")
            fields.update(updated_at=now(),verification="user",checked_at=None)
            store.sql(c,"UPDATE rp_plans SET "+",".join(k+"=?" for k in fields)+" WHERE id=?",(*stored_fields(fields).values(),plan_id))
            before=dict(previous);before["version"]=data.version
            store.history(c,plan_id,"admin_edit",{"before":before,"after":fields,"note":data.note})
        return {"ok":True}

    app.include_router(api)
    app.state.related_plans_store=store


def create_app(store=None):
    application=FastAPI(title="상위 및 관련계획",docs_url=None,redoc_url=None)
    install(application,store=store)
    return application
