from pathlib import Path
from uuid import uuid4
from time import time
import math, os, re, io, json, zipfile, tempfile
import requests
import shapefile
import ezdxf
from fastapi import FastAPI, HTTPException, Query, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
from shapely.geometry import shape, Point, mapping, Polygon, MultiPolygon
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
    key=(os.getenv("VWORLD_API_KEY") or "").strip()
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
    policy:str="top5_plus_designated"

@app.get("/",response_class=HTMLResponse)
def home():
    return HTMLResponse((BASE/"landscape-review"/"selector.html").read_text(encoding="utf-8"),headers={"Cache-Control":"no-store"})

@app.get("/landscape-review",response_class=HTMLResponse)
def landscape_review(site:str|None=None):
    html=(BASE/"landscape-review"/"index.html").read_text(encoding="utf-8")
    browser_key=(os.getenv("VWORLD_CLIENT_KEY") or os.getenv("VWORLD_API_KEY") or "").strip()
    html=html.replace("__VWORLD_CLIENT_KEY__",browser_key)
    return HTMLResponse(html,headers={"Cache-Control":"no-store"})


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


def _candidate_score(row:dict):
    raw=row.get("pedestrianScore",row.get("pedestrian_score"))
    try:
        score=float(raw)
        return score if math.isfinite(score) else None
    except Exception:
        return None

@app.get("/api/integrations/status")
def integration_status():
    return {
        "seoul_open_data":{"configured":bool(_seoul_open_data_key()),"purpose":"pedestrian/population ranking"},
        "vworld3d":{"configured":bool((_vworld_key() if (os.getenv("VWORLD_API_KEY") or "").strip() else "")),"adapter":"prepared","engine":"WebGL 3D API 3.0","building_feature_hide":True,"plan_mass_polygon_supported":True,"glb_supported":True},
        "viewpoint_policy":{
            "target_per_band":5,
            "bands":{"near":"0-250m","mid":"250-500m","far":"500-1000m"},
            "designated_policy":"top5_plus_designated",
            "unscored_candidates":"never auto-selected"
        },
        "candidate_adapters":{
            "transit_park_heritage":"development fallback currently available",
            "road_intersection_20m":"official road-width adapter required",
            "landscape_plan_viewpoint":"official dataset adapter/upload required",
            "pedestrian":"SEOUL_OPEN_DATA_KEY + spatial score adapter required"
        }
    }

@app.post("/api/viewpoint/rank")
def viewpoint_rank(data:ViewpointRankReq):
    target=max(1,min(int(data.target_per_band or 5),20))
    policy=(data.policy or "top5_plus_designated").strip()
    bands=("near","mid","far")
    result={"policy":policy,"target_per_band":target,"bands":{},"selected_ids":[],"designated_ids":[]}
    selected_ids=[]
    designated_ids=[]
    for band in bands:
        rows=[dict(r) for r in data.candidates if str(r.get("band") or "")==band and not bool(r.get("userExcluded"))]
        designated=[r for r in rows if bool(r.get("mandatory")) or str(r.get("category") or "")=="designated"]
        manual=[r for r in rows if bool(r.get("userSelected")) and r not in designated]
        scored=[]
        unresolved=[]
        for r in rows:
            if r in designated or r in manual:
                continue
            score=_candidate_score(r)
            if score is None:
                unresolved.append(r)
            else:
                r["_score"]=score
                scored.append(r)
        scored.sort(key=lambda r:(-r["_score"],float(r.get("distance") or 1e18),str(r.get("id") or "")))
        if policy=="designated_within_target":
            pool=[]
            seen=set()
            for r in designated+manual+scored:
                rid=str(r.get("id") or "")
                if rid and rid not in seen:
                    seen.add(rid);pool.append(r)
            selected=pool[:target]
        else:
            selected=[]
            seen=set()
            # top N regular/manual first, then append all designated viewpoints.
            for r in manual+scored:
                rid=str(r.get("id") or "")
                if rid and rid not in seen and len(selected)<target:
                    seen.add(rid);selected.append(r)
            for r in designated:
                rid=str(r.get("id") or "")
                if rid and rid not in seen:
                    seen.add(rid);selected.append(r)
        band_designated=[str(r.get("id") or "") for r in designated if r.get("id")]
        band_selected=[str(r.get("id") or "") for r in selected if r.get("id")]
        designated_ids.extend(band_designated)
        selected_ids.extend(band_selected)
        ranked=[{
            "id":str(r.get("id") or ""),
            "name":r.get("name"),
            "score":r["_score"],
            "distance":r.get("distance"),
            "category":r.get("category"),
            "rank":i+1
        } for i,r in enumerate(scored)]
        result["bands"][band]={
            "candidate_count":len(rows),
            "scored_count":len(scored),
            "unscored_count":len(unresolved),
            "designated_count":len(designated),
            "manual_count":len(manual),
            "selected_ids":band_selected,
            "designated_ids":band_designated,
            "ranked":ranked,
            "auto_selection_complete":len(scored)>=target
        }
    result["selected_ids"]=list(dict.fromkeys(selected_ids))
    result["designated_ids"]=list(dict.fromkeys(designated_ids))
    result["auto_selection_complete"]=all(result["bands"][b]["auto_selection_complete"] for b in bands)
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
