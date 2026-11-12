from pathlib import Path
from uuid import uuid4
from time import time
import math, os, re
import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from shapely.geometry import shape, Point
from shapely.ops import unary_union
from pyproj import Geod

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
class BBoxReq(BaseModel):
    bbox:list[float]
class SnapshotReq(BaseModel):
    geometry:dict

@app.get("/",response_class=HTMLResponse)
def home():
    return HTMLResponse((BASE/"landscape-review"/"selector.html").read_text(encoding="utf-8"),headers={"Cache-Control":"no-store"})

@app.get("/landscape-review",response_class=HTMLResponse)
def landscape_review(site:str|None=None):
    return HTMLResponse((BASE/"landscape-review"/"index.html").read_text(encoding="utf-8"),headers={"Cache-Control":"no-store"})

@app.get("/api/geocode")
def geocode(q:str=Query(min_length=2,max_length=200)):
    try:
        r=requests.get("https://nominatim.openstreetmap.org/search",params={"q":q,"format":"json","limit":1,"countrycodes":"kr"},headers={"User-Agent":"urban-landscape-review/1.0"},timeout=15)
        r.raise_for_status(); rows=r.json()
    except Exception as exc:
        raise HTTPException(502,"주소 검색 연결 실패") from exc
    if not rows: raise HTTPException(404,"주소를 찾지 못했습니다.")
    return {"lat":float(rows[0]["lat"]),"lon":float(rows[0]["lon"]),"display_name":rows[0].get("display_name",q)}

@app.post("/api/parcel-view")
def parcel_view(data:BBoxReq):
    if len(data.bbox)!=4: raise HTTPException(422,"bbox 형식 오류")
    return {"status":"COMPLETE","features":_vworld(data.bbox,1000)}

@app.post("/api/parcel-at-point")
def parcel_at_point(data:PointReq):
    lon,lat=float(data.lon),float(data.lat)
    d=0.00015
    fs=_vworld([lon-d,lat-d,lon+d,lat+d],100)
    pt=Point(lon,lat); hits=[]
    for f in fs:
        try:
            if shape(f["geometry"]).covers(pt):hits.append(f)
        except Exception: pass
    if len(hits)==1:return {"status":"RESOLVED","feature":hits[0]}
    if len(hits)>1:return {"status":"AMBIGUOUS","message":"필지 경계에 걸렸습니다. 필지 안쪽을 다시 클릭하세요."}
    return {"status":"NOT_FOUND","message":"클릭점이 포함된 필지를 찾지 못했습니다."}

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
