from pathlib import Path
from uuid import uuid4
from time import time
import math, os, re, io, json, zipfile, tempfile, base64, html, threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import requests
import shapefile
import ezdxf
from fastapi import FastAPI, HTTPException, Query, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse, Response
from pydantic import BaseModel
from shapely.geometry import shape, Point, mapping, Polygon, MultiPolygon, box
from shapely.ops import unary_union, transform
from pyproj import Geod, Transformer, CRS

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
    key=(os.getenv("VWORLD_API_KEY") or os.getenv("VWORLD_CLIENT_KEY") or "").strip()
    if not key:
        raise HTTPException(503,"VWorld API 키(VWORLD_API_KEY)가 설정되지 않았습니다.")
    return key

def _seoul_open_data_key():
    for env_name in ("SEOUL_OPEN_DATA_KEY","data.seoul.go.kr_KEY","DATA_SEOUL_GO_KR_KEY"):
        value=(os.getenv(env_name) or "").strip()
        if value:
            return value
    aliases={"seoulopendatakey","dataseoulgokrkey"}
    for env_name,raw in os.environ.items():
        normalized=re.sub(r"[^a-z0-9]","",str(env_name).lower())
        if normalized in aliases and str(raw or "").strip():
            return str(raw).strip()
    return ""


def _domain():
    raw=((os.getenv("VWORLD_DOMAIN") or "").strip() or (os.getenv("RENDER_EXTERNAL_HOSTNAME") or "").strip() or "localhost")
    raw=raw.rstrip("/")
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    if raw=="localhost" or raw.startswith("localhost:"):
        return "http://"+raw
    return "https://"+raw

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

class ViewpointRankReq(BaseModel):
    candidates:list[dict]
    target_per_band:int=5
    policy:str="national_practical_five"

def _landscape_review_html():
    html=(BASE/"landscape-review"/"index.html").read_text(encoding="utf-8")
    # Never expose the server-only VWORLD_API_KEY in HTML or WebGL script URLs.
    browser_key=(os.getenv("VWORLD_CLIENT_KEY") or "").strip()
    return html.replace("__VWORLD_CLIENT_KEY__",browser_key)

@app.get("/",response_class=HTMLResponse)
def home():
    return HTMLResponse((BASE/"landscape-review"/"selector.html").read_text(encoding="utf-8"),headers={"Cache-Control":"no-store"})

@app.get("/selector",response_class=HTMLResponse)
def selector():
    return HTMLResponse((BASE/"landscape-review"/"selector.html").read_text(encoding="utf-8"),headers={"Cache-Control":"no-store"})

@app.get("/landscape-review",response_class=HTMLResponse)
def landscape_review(site:str|None=None):
    return HTMLResponse(_landscape_review_html(),headers={"Cache-Control":"no-store"})


@app.get("/landscape-review/vworld3d-adapter.js")
def vworld3d_adapter():
    return FileResponse(BASE/"landscape-review"/"vworld3d-adapter.js",media_type="application/javascript",headers={"Cache-Control":"no-store"})

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



def _polygonal_only(geom):
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type in ("Polygon","MultiPolygon"):
        return geom
    if geom.geom_type=="GeometryCollection":
        parts=[g for g in geom.geoms if g.geom_type in ("Polygon","MultiPolygon") and not g.is_empty]
        if not parts:
            return None
        return unary_union(parts)
    return None

def _to_wgs84(geom, source_crs):
    crs=CRS.from_user_input(source_crs)
    if crs.to_epsg()==4326:
        return geom
    transformer=Transformer.from_crs(crs,4326,always_xy=True).transform
    return transform(transformer,geom)

def _read_shp_zip(raw: bytes, fallback_crs: str):
    try:
        zf=zipfile.ZipFile(io.BytesIO(raw))
    except Exception as exc:
        raise HTTPException(422,"SHP ZIP 파일을 읽지 못했습니다.") from exc
    names=[n for n in zf.namelist() if not n.endswith("/")]
    shp_names=[n for n in names if n.lower().endswith(".shp")]
    if not shp_names:
        raise HTTPException(422,"ZIP 안에 .shp 파일이 없습니다.")
    errors=[]
    for shp_name in shp_names:
        stem=shp_name[:-4]
        def member(ext):
            target=(stem+ext).lower()
            return next((n for n in names if n.lower()==target),None)
        dbf_name=member(".dbf")
        shx_name=member(".shx")
        prj_name=member(".prj")
        if not dbf_name:
            errors.append(f"{shp_name}: DBF 없음")
            continue
        kwargs={
            "shp":io.BytesIO(zf.read(shp_name)),
            "dbf":io.BytesIO(zf.read(dbf_name)),
            "encoding":"cp949",
            "encodingErrors":"replace",
        }
        if shx_name:
            kwargs["shx"]=io.BytesIO(zf.read(shx_name))
        try:
            reader=shapefile.Reader(**kwargs)
            geoms=[]
            for sr in reader.iterShapeRecords():
                try:
                    g=shape(sr.shape.__geo_interface__)
                    if not g.is_valid:
                        g=g.buffer(0)
                    g=_polygonal_only(g)
                    if g is not None and not g.is_empty:
                        geoms.append(g)
                except Exception:
                    continue
            if not geoms:
                errors.append(f"{shp_name}: Polygon 형상 없음")
                continue
            geom=unary_union(geoms)
            source_crs=fallback_crs
            if prj_name:
                try:
                    prj=zf.read(prj_name).decode("utf-8",errors="ignore").strip()
                    if prj:
                        source_crs=CRS.from_wkt(prj)
                except Exception:
                    source_crs=fallback_crs
            geom=_to_wgs84(geom,source_crs)
            if not geom.is_valid:
                geom=geom.buffer(0)
            geom=_polygonal_only(geom)
            if geom is None or geom.is_empty:
                raise ValueError("polygon")
            return geom, str(CRS.from_user_input(source_crs).to_string()), shp_name
        except HTTPException:
            raise
        except Exception as exc:
            errors.append(f"{shp_name}: {str(exc)[:80]}")
    raise HTTPException(422,"SHP 구역계를 인식하지 못했습니다. " + " / ".join(errors[:3]))

def _dxf_polyline_candidates(doc):
    out=[]
    msp=doc.modelspace()
    for e in msp.query("LWPOLYLINE"):
        try:
            if not bool(e.closed):
                continue
            pts=[(float(x),float(y)) for x,y in e.get_points("xy")]
            if len(pts)>=3:
                out.append((pts, e.dxf.layer or "0"))
        except Exception:
            continue
    for e in msp.query("POLYLINE"):
        try:
            if not bool(e.is_closed):
                continue
            pts=[(float(v.dxf.location.x),float(v.dxf.location.y)) for v in e.vertices]
            if len(pts)>=3:
                out.append((pts, e.dxf.layer or "0"))
        except Exception:
            continue
    for e in msp.query("HATCH"):
        try:
            for path in e.paths:
                vertices=getattr(path,"vertices",None)
                if not vertices or len(vertices)<3:
                    continue
                pts=[(float(v[0]),float(v[1])) for v in vertices]
                out.append((pts,e.dxf.layer or "0"))
        except Exception:
            continue
    return out

def _read_dxf(raw: bytes, source_crs: str):
    suffix=".dxf"
    with tempfile.NamedTemporaryFile(suffix=suffix,delete=False) as fp:
        fp.write(raw)
        tmp=fp.name
    try:
        doc=ezdxf.readfile(tmp)
    except Exception as exc:
        raise HTTPException(422,"DXF 파일을 읽지 못했습니다.") from exc
    finally:
        try: os.unlink(tmp)
        except Exception: pass
    polys=[]
    for pts,layer in _dxf_polyline_candidates(doc):
        try:
            if pts[0]!=pts[-1]:
                pts=pts+[pts[0]]
            g=Polygon(pts)
            if not g.is_valid:
                g=g.buffer(0)
            g=_polygonal_only(g)
            if g is not None and not g.is_empty and g.area>0:
                polys.append((g,layer))
        except Exception:
            continue
    if not polys:
        raise HTTPException(422,"DXF에서 폐합 LWPOLYLINE/POLYLINE/HATCH 구역계를 찾지 못했습니다.")
    # CAD 파일에 여러 폐합선이 있으면 면적이 가장 큰 외곽선을 기본 구역계로 사용한다.
    polys.sort(key=lambda item:item[0].area,reverse=True)
    geom,layer=polys[0]
    geom=_to_wgs84(geom,source_crs)
    if not geom.is_valid:
        geom=geom.buffer(0)
    geom=_polygonal_only(geom)
    if geom is None or geom.is_empty:
        raise HTTPException(422,"DXF 구역계 변환에 실패했습니다.")
    return geom, str(CRS.from_user_input(source_crs).to_string()), layer, len(polys)

@app.post("/api/boundary-file")
async def boundary_file(file: UploadFile = File(...), crs: str = Form("EPSG:5179")):
    name=(file.filename or "").strip()
    ext=os.path.splitext(name.lower())[1]
    raw=await file.read()
    if not raw:
        raise HTTPException(422,"빈 파일입니다.")
    if len(raw)>50*1024*1024:
        raise HTTPException(413,"구역계 파일은 50MB 이하만 지원합니다.")
    try:
        CRS.from_user_input(crs)
    except Exception:
        raise HTTPException(422,"지원하지 않는 좌표계입니다.")
    if ext==".zip":
        geom,source_crs,source_name=_read_shp_zip(raw,crs)
        source_type="SHP"
        meta={"source_file":source_name}
    elif ext==".dxf":
        geom,source_crs,layer,candidate_count=_read_dxf(raw,crs)
        source_type="DXF"
        meta={"layer":layer,"candidate_count":candidate_count}
    elif ext in (".json",".geojson"):
        try:
            data=json.loads(raw.decode("utf-8-sig"))
            g=shape(data["geometry"] if data.get("type")=="Feature" else data)
            if data.get("type")=="FeatureCollection":
                g=unary_union([shape(f["geometry"]) for f in data.get("features",[]) if f.get("geometry")])
            g=_polygonal_only(g)
            if g is None or g.is_empty:
                raise ValueError()
            geom=_to_wgs84(g,crs)
            source_crs=str(CRS.from_user_input(crs).to_string())
            source_type="GeoJSON"
            meta={}
        except Exception as exc:
            raise HTTPException(422,"GeoJSON 구역계를 인식하지 못했습니다.") from exc
    else:
        raise HTTPException(422,"지원 형식은 SHP ZIP, DXF, GeoJSON입니다.")
    area=abs(GEOD.geometry_area_perimeter(geom)[0])
    return {
        "status":"RESOLVED",
        "source_type":source_type,
        "source_crs":source_crs,
        "geometry":mapping(geom),
        "area_m2":round(area,2),
        **meta,
    }



@app.post("/api/plan-footprints")
async def plan_footprints(file: UploadFile = File(...), crs: str = Form("EPSG:5179")):
    name=(file.filename or "").strip()
    ext=os.path.splitext(name.lower())[1]
    raw=await file.read()
    if not raw:
        raise HTTPException(422,"빈 파일입니다.")
    if len(raw)>50*1024*1024:
        raise HTTPException(413,"계획안 파일은 50MB 이하만 지원합니다.")
    try:
        source_crs=CRS.from_user_input(crs)
    except Exception:
        raise HTTPException(422,"지원하지 않는 좌표계입니다.")

    geoms=[]
    source_type=""
    meta={}

    if ext==".dxf":
        with tempfile.NamedTemporaryFile(suffix=".dxf",delete=False) as fp:
            fp.write(raw)
            tmp=fp.name
        try:
            doc=ezdxf.readfile(tmp)
        except Exception as exc:
            raise HTTPException(422,"DXF 파일을 읽지 못했습니다.") from exc
        finally:
            try: os.unlink(tmp)
            except Exception: pass
        candidates=_dxf_polyline_candidates(doc)
        for pts,layer in candidates:
            try:
                if pts[0]!=pts[-1]:
                    pts=pts+[pts[0]]
                g=Polygon(pts)
                if not g.is_valid:
                    g=g.buffer(0)
                g=_polygonal_only(g)
                if g is None or g.is_empty or g.area<=0:
                    continue
                geoms.append((g,{"layer":layer}))
            except Exception:
                continue
        source_type="DXF"
        meta={"candidate_count":len(geoms)}
    elif ext in (".json",".geojson"):
        try:
            data=json.loads(raw.decode("utf-8-sig"))
        except Exception as exc:
            raise HTTPException(422,"GeoJSON 파일을 읽지 못했습니다.") from exc
        features=[]
        if data.get("type")=="FeatureCollection":
            features=data.get("features") or []
        elif data.get("type")=="Feature":
            features=[data]
        elif data.get("type") in ("Polygon","MultiPolygon"):
            features=[{"type":"Feature","properties":{},"geometry":data}]
        else:
            raise HTTPException(422,"Polygon/MultiPolygon GeoJSON만 지원합니다.")
        for ft in features:
            try:
                g=shape(ft.get("geometry"))
                if not g.is_valid:
                    g=g.buffer(0)
                g=_polygonal_only(g)
                if g is None or g.is_empty:
                    continue
                if g.geom_type=="Polygon":
                    geoms.append((g,dict(ft.get("properties") or {})))
                else:
                    for part in g.geoms:
                        geoms.append((part,dict(ft.get("properties") or {})))
            except Exception:
                continue
        source_type="GeoJSON"
    else:
        raise HTTPException(422,"계획안 외곽선은 DXF 또는 GeoJSON만 지원합니다.")

    if not geoms:
        raise HTTPException(422,"건축물 폐합 외곽선을 찾지 못했습니다.")

    out=[]
    for i,(g,props) in enumerate(geoms, start=1):
        try:
            gw=_to_wgs84(g,source_crs)
            if not gw.is_valid:
                gw=gw.buffer(0)
            gw=_polygonal_only(gw)
            if gw is None or gw.is_empty:
                continue
            out.append({
                "type":"Feature",
                "id":f"plan-{i}",
                "properties":{"index":i,**props},
                "geometry":mapping(gw)
            })
        except Exception:
            continue

    if not out:
        raise HTTPException(422,"건축물 외곽선 좌표변환에 실패했습니다.")

    return {
        "status":"RESOLVED",
        "source_type":source_type,
        "source_crs":source_crs.to_string(),
        "feature_count":len(out),
        "features":out,
        **meta,
    }


def _candidate_pedestrian_volume(row:dict):
    raw=row.get("pedestrianVolume",row.get("pedestrian_volume"))
    try:
        value=float(raw)
        return value if math.isfinite(value) else None
    except Exception:
        return None


# Cache successful development-source responses per site. This reduces repeat
# requests to public Overpass servers and lets the UI retain recent results.
VIEWPOINT_CACHE={}
VIEWPOINT_CACHE_TTL=60*60*6
VIEWPOINT_STALE_TTL=60*60*48

def _overpass_viewpoint_query(lat:float,lng:float,radius:int):
    r=max(100,min(int(radius),2000))
    # Keep the query small: nwr on every feature type makes a city-centre
    # request expensive and increases the risk of Overpass 429/504/timeouts.
    # No OSM highway tag is treated as verified 20 m road width or traffic.
    return f"""[out:json][timeout:12];
(
way(around:{r},{lat},{lng})["highway"="pedestrian"];
node(around:{r},{lat},{lng})["leisure"="park"];
way(around:{r},{lat},{lng})["leisure"="park"];
node(around:{r},{lat},{lng})["place"="square"];
way(around:{r},{lat},{lng})["place"="square"];
node(around:{r},{lat},{lng})["railway"="subway_entrance"];
node(around:{r},{lat},{lng})["railway"="station"]["station"="subway"];
node(around:{r},{lat},{lng})["amenity"="university"];
way(around:{r},{lat},{lng})["amenity"="university"];
node(around:{r},{lat},{lng})["entrance"="main"];
node(around:{r},{lat},{lng})["barrier"="gate"];
node(around:{r},{lat},{lng})["historic"];
way(around:{r},{lat},{lng})["historic"];
node(around:{r},{lat},{lng})["heritage"];
way(around:{r},{lat},{lng})["heritage"];
node(around:{r},{lat},{lng})["tourism"="viewpoint"];
node(around:{r},{lat},{lng})["highway"="traffic_signals"];
);
out center;"""

# Restricted VWorld WMTS adapter. The server key stays on the server;
# only these fixed basemap layers and valid XYZ tiles can be requested.
@app.get("/api/viewpoint/basemap/{layer}/{z}/{x}/{y}")
def viewpoint_basemap(layer:str,z:int,x:int,y:int):
    if layer not in ("Satellite","Base","Hybrid") or not (6<=z<=19) or not (0<=x<2**z and 0<=y<2**z):
        raise HTTPException(400,"지원하지 않는 지도 타일입니다.")
    key=(os.getenv("VWORLD_API_KEY") or os.getenv("VWORLD_CLIENT_KEY") or "").strip()
    if not key:
        raise HTTPException(503,"배경지도 인증이 설정되지 않았습니다.")
    extension="jpeg" if layer=="Satellite" else "png"
    url=f"https://api.vworld.kr/req/wmts/1.0.0/{key}/{layer}/{z}/{y}/{x}.{extension}"
    try:
        r=requests.get(url,timeout=(3,8),headers={"Referer":_domain(),"User-Agent":"urban-landscape-review/1.2"})
        r.raise_for_status()
        if layer=="Satellite":
            valid=r.content.startswith(bytes((0xff,0xd8,0xff)))
        else:
            valid=r.content.startswith(bytes((0x89,0x50,0x4e,0x47)))
        if not valid:
            raise ValueError("invalid tile")
        return Response(content=r.content,media_type="image/jpeg" if layer=="Satellite" else "image/png",
                        headers={"Cache-Control":"public, max-age=300","X-Content-Type-Options":"nosniff"})
    except (requests.exceptions.RequestException,ValueError):
        raise HTTPException(502,"VWorld 배경지도 조회 실패: 인증 도메인 또는 원격 서비스 확인 필요")

# An official viewpoint is never synthesized from a place-name geocode.
# Only a curated, verified coordinate file may be returned as authoritative.

# Seoul zoning overlay: same authoritative 서울시 GeoServer WMS layer
# as the main urban-information map, not a guessed VWorld land-use code.
_SEOUL_ZONING_LAYER="tile_map:upis_c_uq111"
_SEOUL_ZONING_GATE=threading.BoundedSemaphore(3)

@lru_cache(maxsize=1)
def _seoul_zoning_style():
    r=requests.get("https://map.seoul.go.kr/geoserver/wms",params={
        "service":"WMS","request":"GetStyles","version":"1.1.1",
        "layers":_SEOUL_ZONING_LAYER},timeout=(3,12))
    r.raise_for_status()
    root=ET.fromstring(r.content)
    ns="{http://www.opengis.net/sld}"
    if not root.findall(ns+"NamedLayer"):
        raise ValueError("missing official Seoul map style")
    # The main platform suppresses WMS text labels and separately places
    # readable labels using WFS representative points.
    for parent in root.iter():
        for child in list(parent):
            if child.tag==ns+"TextSymbolizer":
                parent.remove(child)
    return ET.tostring(root,encoding="unicode")

@lru_cache(maxsize=180)
def _seoul_zoning_tile(z:int,x:int,y:int,epoch:int):
    n=2**z
    west,east=x/n*360-180,(x+1)/n*360-180
    north=math.degrees(math.atan(math.sinh(math.pi*(1-2*y/n))))
    south=math.degrees(math.atan(math.sinh(math.pi*(1-2*(y+1)/n))))
    # Guard against expensive requests outside Seoul. The Seoul overlay is
    # intentionally not presented as nationwide zoning coverage.
    if east<126.5 or west>127.4 or north<37.2 or south>37.85:
        return None
    half=20037508.342789244
    bbox=[x/n*2*half-half,half-(y+1)/n*2*half,
          (x+1)/n*2*half-half,half-y/n*2*half]
    r=requests.post("https://map.seoul.go.kr/geoserver/wms",data={
        "service":"WMS","request":"GetMap","version":"1.1.1",
        "layers":_SEOUL_ZONING_LAYER,"styles":"","srs":"EPSG:3857",
        "bbox":",".join(map(str,bbox)),"width":512,"height":512,
        "format":"image/png","transparent":"true",
        "sld_body":_seoul_zoning_style()},timeout=(4,18))
    r.raise_for_status()
    if not r.content.startswith(bytes((0x89,0x50,0x4e,0x47))):
        raise ValueError("Seoul WMS returned non-image content")
    labels=[]
    if z>=14:
        try:
            rr=requests.get("https://map.seoul.go.kr/geoserver/wfs",params={
                "service":"WFS","request":"GetFeature","version":"1.0.0",
                "typeName":_SEOUL_ZONING_LAYER,"outputFormat":"application/json",
                "srsName":"EPSG:4326",
                "bbox":f"{west},{south},{east},{north},EPSG:4326",
                "maxFeatures":450},timeout=(3,12))
            rr.raise_for_status()
            features=rr.json().get("features") or []
            clip=box(west,south,east,north)
            occupied=[]
            for feature in features:
                name=str((feature.get("properties") or {}).get("dgm_nm") or "").strip()
                if not name or len(name)>80 or not feature.get("geometry"):continue
                try:
                    geom=shape(feature["geometry"])
                    if not geom.is_valid:geom=geom.buffer(0)
                    geom=geom.intersection(clip)
                    if geom.is_empty:continue
                    pt=geom.representative_point()
                    px=((pt.x+180)/360*n-x)*512
                    py=((1-math.asinh(math.tan(math.radians(pt.y)))/math.pi)/2*n-y)*512
                    size=13
                    width=sum(size if ord(c)>255 else size*.6 for c in name)
                    if width>490:continue
                    px=max(width/2+6,min(506-width/2,px))
                    py=max(16,min(498,py))
                    rect=(px-width/2-4,py-10,px+width/2+4,py+10)
                    if any(rect[0]<v[2] and rect[2]>v[0] and rect[1]<v[3] and rect[3]>v[1] for v in occupied):continue
                    occupied.append(rect)
                    labels.append(f'<text x="{px:.1f}" y="{py:.1f}" font-size="13">{html.escape(name)}</text>')
                except (TypeError,ValueError):
                    continue
        except (requests.exceptions.RequestException,ValueError,TypeError):
            pass # The official colored zoning map still displays without labels.
    png=base64.b64encode(r.content).decode("ascii")
    svg=('<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512">'
        f'<image width="512" height="512" href="data:image/png;base64,{png}"/>'
        '<g font-family="Malgun Gothic,Apple SD Gothic Neo,Noto Sans CJK KR,sans-serif" '
        'text-anchor="middle" fill="#181818" stroke="white" stroke-width="2.5" '
        'stroke-linejoin="round" paint-order="stroke fill">'+"".join(labels)+'</g></svg>')
    return svg.encode("utf-8")

@app.get("/api/viewpoint/seoul-zoning/{z}/{x}/{y}.svg")
def viewpoint_seoul_zoning(z:int,x:int,y:int):
    if not (0<=z<=18 and 0<=x<2**z and 0<=y<2**z):
        raise HTTPException(400,"invalid Seoul map tile")
    if not _SEOUL_ZONING_GATE.acquire(timeout=10):
        raise HTTPException(503,"Seoul zoning tile busy")
    try:
        content=_seoul_zoning_tile(z,x,y,int(time()//3600))
        if content is None:
            return Response(content=b"",media_type="image/svg+xml")
        return Response(content=content,media_type="image/svg+xml",
                        headers={"Cache-Control":"public, max-age=1800",
                                 "X-Content-Type-Options":"nosniff"})
    except (requests.exceptions.RequestException,ValueError,ET.ParseError):
        raise HTTPException(502,"Seoul official zoning WMS temporarily unavailable")
    finally:
        _SEOUL_ZONING_GATE.release()

@app.get("/api/viewpoint/official")
def viewpoint_official():
    path=BASE/"landscape-review"/"official_viewpoints.geojson"
    if not path.is_file():
        return {"status":"DATA_UNAVAILABLE","features":[],"message":"공식 경관계획 조망점 좌표 DB 미구축"}
    try:
        data=json.loads(path.read_text(encoding="utf-8"))
        if data.get("type")!="FeatureCollection" or not isinstance(data.get("features"),list):
            raise ValueError("invalid official GeoJSON")
        verified=[]
        for feature in data["features"]:
            geom=feature.get("geometry") or {}
            coords=geom.get("coordinates") or []
            props=feature.get("properties") or {}
            if geom.get("type")!="Point" or len(coords)<2 or props.get("verified") is not True:
                continue
            lon,lat=float(coords[0]),float(coords[1])
            if math.isfinite(lon) and math.isfinite(lat) and 124<=lon<=132 and 33<=lat<=39:
                verified.append(feature)
        return {"status":"OK","features":verified,"source":"verified official viewpoint database"}
    except (ValueError,TypeError,KeyError,OSError):
        return {"status":"DATA_UNAVAILABLE","features":[],"message":"공식 좌표 DB 검증 실패"}


def _overpass_candidates(lat:float,lng:float,radius:int):
    query=_overpass_viewpoint_query(lat,lng,radius)
    attempts=[]
    for endpoint in ("https://overpass-api.de/api/interpreter",
                     "https://overpass.kumi.systems/api/interpreter",
                     "https://overpass.private.coffee/api/interpreter"):
        host=endpoint.split("/")[2]
        try:
            response=requests.post(endpoint,data={"data":query},
                timeout=(2,6),headers={"User-Agent":"urban-landscape-review/1.3","Accept":"application/json"})
            response.raise_for_status()
            payload=response.json()
            if not isinstance(payload,dict) or not isinstance(payload.get("elements"),list):
                raise ValueError("invalid Overpass payload")
            if payload.get("remark") and not payload["elements"]:
                raise ValueError("Overpass query rejected")
            return payload["elements"],{"name":"OSM Overpass","state":"OK","host":host}
        except requests.exceptions.Timeout:
            attempts.append(host+":timeout")
        except requests.exceptions.HTTPError:
            attempts.append(host+":HTTP-"+str(response.status_code))
        except (requests.exceptions.RequestException,ValueError,TypeError):
            attempts.append(host+":unavailable")
    return [],{"name":"OSM Overpass","state":"FAILED","detail":"; ".join(attempts)}


def _vworld_poi_candidates(lat:float,lng:float,radius:int):
    # VWorld public place search provides additional nationwide candidates;
    # unlike OSM, it does not depend on the same external Overpass instances.
    key=(os.getenv("VWORLD_API_KEY") or os.getenv("VWORLD_CLIENT_KEY") or "").strip()
    if not key:
        return [],{"name":"VWorld POI","state":"NO_KEY"}
    radius=max(100,min(int(radius),2000))
    dlat=radius/110540
    dlon=radius/(111320*max(0.2,math.cos(math.radians(lat))))
    bbox=f"{lng-dlon:.6f},{lat-dlat:.6f},{lng+dlon:.6f},{lat+dlat:.6f}"
    # A subway station representative point is a practical preliminary viewpoint.
    # Its precise entrance/visibility is checked during the 3D review.
    # University and pedestrian-road representative points remain review-only.
    keywords=(
        ("공원","park",False),("광장","square",False),
        ("전망대","viewpoint",False),("문화유산","heritage",False),
        ("보행자전용도로","pedestrian",True),
        ("지하철역","subway",False),("대학교","university",True),
    )
    def query_one(entry):
        keyword,category,unverified=entry
        try:
            res=requests.get("https://api.vworld.kr/req/search",params={
                "key":key,"domain":_domain(),"service":"search","request":"search","version":"2.0",
                "format":"json","type":"PLACE","query":keyword,"crs":"EPSG:4326",
                "bbox":bbox,"size":"100","page":"1",
            },timeout=(3,8),headers={"Referer":_domain().rstrip("/")+"/","User-Agent":"urban-landscape-review/1.3"})
            res.raise_for_status()
            payload=res.json()
            rsp=payload.get("response") or {}
            if rsp.get("status")=="NOT_FOUND":
                return [],True
            if rsp.get("status")!="OK":
                return [],False
            items=(rsp.get("result") or {}).get("items") or []
            if isinstance(items,dict):items=items.get("item") or []
            if not isinstance(items,list):return [],False
            results=[]
            for item in items:
                try:
                    pt=item.get("point") or {}
                    x=float(pt.get("x"));y=float(pt.get("y"))
                    if not(124<=x<=132 and 33<=y<=39):continue
                    dy=(y-lat)*110540
                    dx=(x-lng)*111320*math.cos(math.radians(lat))
                    if math.hypot(dx,dy)>radius+40:continue
                    title=str(item.get("title") or keyword).strip()
                    if not title:continue
                    results.append({
                        "id":"vworld-poi-"+category+"-"+str(item.get("id") or f"{x:.6f}-{y:.6f}"),
                        "lat":y,"lng":x,"name":title,
                        "category":category,"source":"VWorld 장소검색",
                        "verification":("시설 대표좌표 · 출입구/정문이 아닌 검토후보" if unverified
                                        else "POI 대표좌표 · 정확한 조망위치 및 시야는 시뮬레이션에서 확인"),
                        "requiresVerification":unverified
                    })
                except (TypeError,ValueError,AttributeError):
                    continue
            return results,True
        except (requests.exceptions.RequestException,ValueError,TypeError):
            return [],False
    with ThreadPoolExecutor(max_workers=4) as pool:
        outputs=list(pool.map(query_one,keywords))
    combined=[]
    seen=set()
    for rows,ok in outputs:
        for row in rows:
            ident=(row["category"],round(row["lat"],5),round(row["lng"],5))
            if ident not in seen:
                seen.add(ident);combined.append(row)
    success=sum(int(ok) for _,ok in outputs)
    state="OK" if success==len(keywords) else "PARTIAL" if success else "FAILED"
    return combined,{"name":"VWorld POI","state":state,"queries_ok":success,"queries_total":len(keywords)}


@app.get("/api/viewpoint/candidates")
def viewpoint_candidates(
    lat:float=Query(...,ge=-90,le=90),
    lng:float=Query(...,ge=-180,le=180),
    radius:int=Query(1150,ge=100,le=2000)
):
    key=(round(lat,5),round(lng,5),radius)
    cached=VIEWPOINT_CACHE.get(key)
    now=time()
    if cached and now-cached["timestamp"]<VIEWPOINT_CACHE_TTL:
        return {**cached["payload"],"cached":True}
    with ThreadPoolExecutor(max_workers=2) as pool:
        osm=pool.submit(_overpass_candidates,lat,lng,radius)
        poi=pool.submit(_vworld_poi_candidates,lat,lng,radius)
        elements,osm_state=osm.result()
        candidates,poi_state=poi.result()
    sources=[osm_state,poi_state]
    success=any(item["state"] in ("OK","PARTIAL") for item in sources)
    if success:
        payload={
            "status":"OK",
            "source":"OSM + VWorld POI (development candidates)",
            "elements":elements,
            "candidates":candidates,
            "sources":sources,
            "partial":any(item["state"] not in ("OK",) for item in sources),
            "empty":not bool(elements or candidates),
        }
        VIEWPOINT_CACHE[key]={"timestamp":now,"payload":payload}
        if len(VIEWPOINT_CACHE)>100:
            for old in list(VIEWPOINT_CACHE)[:30]:VIEWPOINT_CACHE.pop(old,None)
        return {**payload,"cached":False}
    if cached and now-cached["timestamp"]<VIEWPOINT_STALE_TTL:
        return {**cached["payload"],"cached":True,"stale":True,
                "warning":"공간정보 서비스 장애로 직전 조회자료를 표시합니다.",
                "sources":sources}
    raise HTTPException(502,detail={"message":"OSM 및 VWorld 후보자료 서비스 모두 조회되지 않았습니다.",
                                    "sources":sources})

@app.get("/api/integrations/status")
def integration_status():
    return {
        "seoul_open_data":{"configured":bool(_seoul_open_data_key()),"purpose":"raw pedestrian volume lookup"},
        "vworld3d":{"configured":bool((_vworld_key() if (os.getenv("VWORLD_API_KEY") or "").strip() else "")),"adapter":"prepared","engine":"WebGL 3D API 3.0","building_feature_hide":True,"plan_mass_polygon_supported":True,"glb_supported":True},
        "viewpoint_policy":{
            "target_per_band":5,
            "bands":{"near":"0-250m","mid":"250-500m","far":"500-1000m"},
            "designated_policy":"mandatory_outside_top5",
            "selection":"priority_top5_then_manual_toggle",
            "intersection_fallback":"verified_width_20m_plus_raw_traffic"
        },
        "candidate_adapters":{
            "transit_park_heritage":"development fallback currently available",
            "road_intersection_20m":"requires verified intersection point + road width + traffic volume; file import supported",
            "landscape_plan_viewpoint":"2016 Seoul Landscape Plan official coordinates to be embedded",
            "pedestrian":"not required for nationwide priority selection"
        }
    }

# Nationwide priority rules: first public pedestrian places, then only
# verified >=20 m intersections with measured traffic volumes.
# Practical nationwide viewpoint selection: 5 proposals TOTAL, not 5 per band.
# Officially designated viewpoints remain separately included when verified.
VIEWPOINT_CATEGORY_PRIORITY={
    "subway":0,"park":1,"square":1,"pedestrian":2,
    "viewpoint":3,"bus":4,"university":5,"heritage":6,"intersection":7,
}

def _viewpoint_traffic(row:dict):
    raw=row.get("trafficVolume",row.get("traffic_volume"))
    try:
        value=float(raw)
        return value if math.isfinite(value) and value>=0 else None
    except (TypeError,ValueError):
        return None

def _viewpoint_width(row:dict):
    raw=row.get("roadWidthM",row.get("road_width_m"))
    try:
        value=float(raw)
        return value if math.isfinite(value) and value>=0 else None
    except (TypeError,ValueError):
        return None

def _viewpoint_auto_eligible(row:dict):
    category=str(row.get("category") or "")
    if category not in VIEWPOINT_CATEGORY_PRIORITY:
        return False
    if bool(row.get("requiresVerification")):
        return False
    if category=="intersection":
        width=_viewpoint_width(row)
        # Traffic is OPTIONAL. Unknown traffic is not a zero traffic count.
        return width is not None and width>=20
    return True

def _viewpoint_sort_key(row:dict):
    category=str(row.get("category") or "")
    priority=VIEWPOINT_CATEGORY_PRIORITY.get(category,99)
    try:
        distance=float(row.get("distance"))
        if not math.isfinite(distance): distance=1e18
    except (TypeError,ValueError):
        distance=1e18
    if category=="intersection":
        # Measured traffic may break ties among intersections; never fabricate it.
        traffic=_viewpoint_traffic(row)
        return (priority,traffic is None,-traffic if traffic is not None else 0,distance,str(row.get("id") or ""))
    return (priority,False,0,distance,str(row.get("id") or ""))

@app.post("/api/viewpoint/rank")
def viewpoint_rank(data:ViewpointRankReq):
    target=max(1,min(int(data.target_per_band or 5),20))
    bands=("near","mid","far")
    rows=[dict(r) for r in data.candidates if r.get("id") and str(r.get("band") or "") in bands]
    designated=[r for r in rows if bool(r.get("mandatory")) or r.get("category")=="designated"]
    regular=[r for r in rows if r not in designated]
    eligible=sorted((r for r in regular if _viewpoint_auto_eligible(r)),key=_viewpoint_sort_key)
    baseline=[]
    # One representative from each available range first, then fill to 5 by type priority.
    for band in bands:
        first=next((r for r in eligible if r["band"]==band and r not in baseline),None)
        if first is not None and len(baseline)<target:
            baseline.append(first)
    for r in eligible:
        if len(baseline)>=target:break
        if r not in baseline:baseline.append(r)
    # Excluding a viewpoint does not silently backfill with another candidate.
    selected=[r for r in baseline if not bool(r.get("userExcluded"))]
    selected_ids={str(r["id"]) for r in selected}
    for r in regular:
        rid=str(r["id"])
        if r.get("userSelected") and not r.get("userExcluded") and rid not in selected_ids:
            selected.append(r);selected_ids.add(rid)
    for r in designated:
        rid=str(r["id"])
        if rid not in selected_ids:
            selected.append(r);selected_ids.add(rid)
    result={
        "policy":"national_practical_five",
        "target_total":target,"target_per_band":None,
        "baseline_ids":[str(r["id"]) for r in baseline],
        "selected_ids":[str(r["id"]) for r in selected],
        "designated_ids":[str(r["id"]) for r in designated],
        "auto_selection_complete":len(baseline)>=target,
        "auto_shortfall":max(0,target-len(baseline)),
        "bands":{}
    }
    for band in bands:
        candidates=[r for r in rows if r["band"]==band]
        chosen=[r for r in selected if r["band"]==band]
        automatic=[r for r in baseline if r["band"]==band]
        result["bands"][band]={
            "candidate_count":len(candidates),
            "priority_candidate_count":sum(r.get("category")!="intersection" for r in eligible if r["band"]==band),
            "traffic_intersection_count":sum(r.get("category")=="intersection" and _viewpoint_traffic(r) is not None for r in eligible if r["band"]==band),
            "auto_selected_count":len(automatic),
            "auto_shortfall":0,
            "manual_added_count":len([r for r in chosen if r not in automatic and r not in designated]),
            "manual_excluded_count":len([r for r in automatic if r.get("userExcluded")]),
            "designated_count":len([r for r in designated if r["band"]==band]),
            "selected_ids":[str(r["id"]) for r in chosen],
            "baseline_ids":[str(r["id"]) for r in automatic],
            "designated_ids":[str(r["id"]) for r in designated if r["band"]==band],
            "ranked":[{
                "id":str(r["id"]),"category":r.get("category"),
                "priority":VIEWPOINT_CATEGORY_PRIORITY.get(str(r.get("category") or "")),
                "distance":r.get("distance"),
                "traffic_volume":_viewpoint_traffic(r) if r.get("category")=="intersection" else None,
                "road_width_m":_viewpoint_width(r) if r.get("category")=="intersection" else None
            } for r in eligible if r["band"]==band],
            "auto_selection_complete":True
        }
    return result

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
