from pathlib import Path
from uuid import uuid4
from time import time
import math, os, re
import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from shapely.geometry import shape, Point, mapping
from shapely.ops import unary_union, transform
from pyproj import Geod, Transformer

BASE=Path(__file__).parent
app=FastAPI(title="Urban Landscape Review")
GEOD=Geod(ellps="WGS84")
SNAPSHOTS={}
TTL=60*60*4

def _cleanup():
    now=time()
    for k in list(SNAPSHOTS):
        if now-SNAPSHOTS[k]["created_at"]>TTL:
            SNAPSHOTS.pop(k,None)

def _vworld_key():
    key=(os.getenv("VWORLD_API_KEY") or "").strip()
    if not key:
        raise HTTPException(503,"VWorld API 키(VWORLD_API_KEY)가 설정되지 않았습니다.")
    return key

def _domain():
    host=(os.getenv("RENDER_EXTERNAL_HOSTNAME") or "localhost").strip()
    return ("https://"+host) if not host.startswith("http") and host!="localhost" else ("http://localhost" if host=="localhost" else host)

def _vworld(bbox,size):
    minx,miny,maxx,maxy=[float(x) for x in bbox]
    params={"key":_vworld_key(),"domain":_domain(),"service":"data","version":"2.0","request":"getfeature","format":"json","size":str(size),"page":"1","geometry":"true","attribute":"true","crs":"EPSG:4326","data":"LP_PA_CBND_BUBUN","geomfilter":f"BOX({minx},{miny},{maxx},{maxy})"}
    try:
        r=requests.get("https://api.vworld.kr/req/data",params=params,timeout=(5,20),headers={"Referer":_domain()+"/","User-Agent":"urban-landscape-review/1.0"})
        r.raise_for_status()
        payload=r.json()
    except Exception as exc:
        raise HTTPException(502,"VWorld 연속지적 조회 실패") from exc
    rsp=payload.get("response") or {}
    if rsp.get("status")=="NOT_FOUND": return []
    if rsp.get("status")!="OK":
        raise HTTPException(502,"VWorld 응답 오류: "+str((rsp.get("error") or {}).get("code") or rsp.get("status")))
    fs=(((rsp.get("result") or {}).get("featureCollection") or {}).get("features") or [])
    out=[]
    for f in fs:
        if not isinstance(f,dict) or (f.get("geometry") or {}).get("type") not in ("Polygon","MultiPolygon"): continue
        p=dict(f.get("properties") or {})
        low={str(k).lower():v for k,v in p.items()}
        raw=str(low.get("pnu") or f.get("id") or "")
        m=re.search(r"\d{19}",raw)
        if m:p["pnu"]=m.group(0)
        if low.get("jibun") is not None:p["jibun"]=low.get("jibun")
        out.append({"type":"Feature","id":p.get("pnu") or f.get("id"),"properties":p,"geometry":f["geometry"]})
    return out

class PointReq(BaseModel):
    lon:float
    lat:float
class SiteReq(BaseModel):
    geometry:dict
class AddressReq(BaseModel):
    address:str
class SnapshotReq(BaseModel):
    geometry:dict

@app.get("/",response_class=HTMLResponse)
def home():
    return HTMLResponse((BASE/"landscape-review"/"selector.html").read_text(encoding="utf-8"),headers={"Cache-Control":"no-store"})

@app.get("/landscape-review",response_class=HTMLResponse)
def landscape_review(site:str|None=None):
    return HTMLResponse((BASE/"landscape-review"/"index.html").read_text(encoding="utf-8"),headers={"Cache-Control":"no-store"})

@app.post("/api/address")
def address(data:AddressReq):
    address=str(data.address or "").strip()
    if not re.match(r"^(서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충청|충북|충남|전라|전북|전남|경상|경북|경남|제주)",address):
        raise HTTPException(422,"시·도부터 정식 지번주소를 입력하세요.")
    try:
        params={"key":_vworld_key(),"domain":_domain(),"service":"search","version":"2.0","request":"search","format":"json","size":"10","page":"1","query":address,"type":"ADDRESS","category":"parcel","crs":"EPSG:4326"}
        r=requests.get("https://api.vworld.kr/req/search",params=params,timeout=(5,20),headers={"Referer":_domain()+"/","User-Agent":"urban-landscape-review/1.0"})
        r.raise_for_status()
        payload=(r.json().get("response") or {})
        if payload.get("status") not in ("OK","NOT_FOUND"):
            raise ValueError("provider")
        norm=lambda v:re.sub(r"\s+","",str(v or ""))
        hits=[it for it in ((payload.get("result") or {}).get("items") or []) if norm((it.get("address") or {}).get("parcel"))==norm(address)]
        if len(hits)!=1:
            return {"status":"not_found","feature":None,"message":"정확히 일치하는 주소가 없습니다. 정식 시·도명과 지번을 확인하세요."}
        item=hits[0]
        point=(item.get("point") or {})
        x=float(point.get("x")); y=float(point.get("y"))
        feature_result=parcel_at_point(PointReq(lon=x,lat=y))
        if feature_result.get("status")!="RESOLVED":
            return {"status":"not_found","feature":None,"message":"주소 위치의 지적 필지를 확인하지 못했습니다."}
        return {"status":"resolved","feature":feature_result["feature"],"pnu":feature_result["feature"]["properties"].get("pnu"),"address":address}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502,"주소자료 조회 실패. 관리자 연결상태를 확인하세요.") from exc

@app.post("/api/parcel-view")
def parcel_view(data:SiteReq):
    try:
        g=shape(data.geometry)
        if g.geom_type not in ("Polygon","MultiPolygon") or g.is_empty or not g.is_valid:
            raise ValueError()
        a,b,c,d=g.bounds
        items=_vworld([a,b,c,d],1000)
        features=[]; seen=set()
        for f in items[:1000]:
            try:
                polygon=shape(f["geometry"])
                if polygon.geom_type not in ("Polygon","MultiPolygon") or not polygon.is_valid or not polygon.intersects(g):
                    continue
                p=dict(f.get("properties") or {})
                pnu=str(p.get("pnu") or f.get("id") or "")
                if not re.fullmatch(r"\d{19}",pnu) or pnu in seen:
                    continue
                seen.add(pnu)
                jibun=str(p.get("jibun") or "")
                if not jibun:
                    main,sub=int(pnu[11:15]),int(pnu[15:19])
                    jibun=("산 " if pnu[10]=="2" else "")+str(main)+(f"-{sub}" if sub else "")
                label_point=polygon.intersection(g).representative_point()
                features.append({"type":"Feature","id":pnu,"geometry":f["geometry"],"properties":{"pnu":pnu,"jibun":jibun,"label_point":[label_point.x,label_point.y]}})
            except Exception:
                continue
        return {"status":"PARTIAL" if len(items)>=1000 else "COMPLETE","source":"VWorld","features":features,"display_only":True}
    except Exception as exc:
        if isinstance(exc,HTTPException): raise
        return {"status":"UNKNOWN","features":[],"display_only":True,"message":"지적 배경 미확보 · 잠시 후 다시 조회하세요."}

@app.post("/api/parcel-at-point")
def parcel_at_point(data:PointReq):
    try:
        lon,lat=float(data.lon),float(data.lat)
        project=Transformer.from_crs(4326,5179,always_xy=True).transform
        inverse=Transformer.from_crs(5179,4326,always_xy=True).transform
        point=Point(lon,lat)
        probe=transform(inverse,transform(project,point).buffer(8))
        a,b,c,d=probe.bounds
        features=_vworld([a,b,c,d],100)
        hits={}
        for f in features:
            try:
                polygon=shape(f["geometry"])
                if not polygon.is_valid or not polygon.covers(point):
                    continue
                props=dict(f.get("properties") or {})
                low={str(k).lower():v for k,v in props.items()}
                pnu=next((str(v) for v in [low.get("pnu"),f.get("id")] if re.fullmatch(r"\d{19}",str(v or ""))),None)
                if not pnu:
                    continue
                props["pnu"]=pnu
                if low.get("jibun") is not None:
                    props["jibun"]=low["jibun"]
                hits[pnu]={"type":"Feature","id":pnu,"geometry":f["geometry"],"properties":props}
            except Exception:
                continue
        if len(hits)>1:
            return {"status":"AMBIGUOUS","feature":None,"message":"클릭점이 필지 경계에 걸렸습니다. 필지 안쪽을 클릭하세요."}
        if not hits:
            return {"status":"NOT_FOUND","feature":None,"message":"클릭점이 포함된 지적 필지를 확인하지 못했습니다. 필지 안쪽을 다시 클릭하세요."}
        feature=next(iter(hits.values()))
        return {"status":"RESOLVED","feature":feature,"pnu":feature["properties"]["pnu"]}
    except HTTPException:
        raise
    except Exception:
        return {"status":"UNKNOWN","feature":None,"message":"클릭 지적자료를 확보하지 못했습니다."}

@app.post("/api/snapshots")
def create_snapshot(data:SnapshotReq):
    try:
        g=shape(data.geometry)
        if g.geom_type not in ("Polygon","MultiPolygon") or g.is_empty or not g.is_valid: raise ValueError()
        area=abs(GEOD.geometry_area_perimeter(g)[0])
    except Exception:
        raise HTTPException(422,"유효한 Polygon/MultiPolygon 구역계가 필요합니다.")
    _cleanup(); sid=uuid4().hex
    item={"id":sid,"geometry":data.geometry,"area_m2":round(area,2),"created_at":time()}
    SNAPSHOTS[sid]=item
    return {k:v for k,v in item.items() if k!="created_at"}

@app.get("/api/snapshots/{sid}")
def get_snapshot(sid:str):
    _cleanup(); item=SNAPSHOTS.get(sid)
    if not item: raise HTTPException(404,"대상지 스냅샷을 찾지 못했습니다.")
    return {k:v for k,v in item.items() if k!="created_at"}
