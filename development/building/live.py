"""Independent GIS-building WFS collector for the urban survey.

Primary source: MOLIT GIS building integrated WFS (VWorld NED).
No Seoul redevelopment imports, no parcel-to-building geometry substitution.
All polygons are clipped to the user's frozen boundary in EPSG:4326.
Provider truncation, bad CRS, invalid geometry and duplicate conflicting IDs
are surfaced as errors, never as confirmed zero buildings.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from hashlib import sha256
from json import dumps
import math
from zoneinfo import ZoneInfo

from shapely.geometry import shape, mapping
from shapely.ops import unary_union

from facts.vworld import VWorldFacts, FactTransportError

URL = "https://api.vworld.kr/ned/wfs/getBldgisSpceWFS"
PAGE_LIMIT = 500
MAX_FEATURES = 5000
MAX_REQUESTS = 85
MAX_SPLITS = 3
NAME = "국토교통부 GIS건물통합정보 WFS (VWorld)"

# Reference attribute names are those of the official GIS building standard,
# not HUB mgmBldrgstPk. Never conflate A1/A19 with the HUB management key.
FIELDS = {
    "building_id": ("a1", "gis_bld_id", "gisbuildid", "building_id"),
    "shape_id": ("a0", "id", "fid"),
    "pnu": ("a2", "pnu"),
    "use": ("a9", "mainpurpscdnm", "building_use", "use"),
    "structure": ("a11", "strctcdnm", "building_structure", "structure"),
    "approval_date": ("a13", "useaprday", "use_approval_date"),
    "building_area": ("a12", "archarea", "building_area"),
    "total_floor_area": ("a14", "totarea", "total_floor_area"),
    "building_height": ("a16", "heit", "height"),
    "coverage": ("a17", "bcrat", "coverage"),
    "far": ("a18", "vlrat", "far"),
    "hub_management_id": ("mgmbldrgstpk", "mgm_bld_rgst_pk"),
}


class BuildingSourceError(RuntimeError):
    def __init__(self, code):
        self.code=code
        super().__init__(code)


def normalize_properties(properties):
    """Interpret named official columns; do not infer data from parcel identity."""
    raw={str(k).strip().lower():v for k,v in (properties or {}).items()}
    normalized={}
    for field,keys in FIELDS.items():
        value=next((raw[k] for k in keys if k in raw and raw[k] not in (None,"")),None)
        if value is not None:
            normalized[field]=str(value).strip()[:200]
    pnu=normalized.get("pnu")
    if pnu and (len(pnu)!=19 or not pnu.isdigit()):
        normalized.pop("pnu",None)
    if normalized.get("building_id") in (None,"","0"):
        normalized.pop("building_id",None)
    return normalized


def _extract(response):
    """Reject XML/HTML, errors and unrecognized response envelopes."""
    if not isinstance(response,dict):
        raise BuildingSourceError("PROVIDER_FORMAT")
    def status_bad(value):
        return value is not None and str(value).upper() not in ("OK","SUCCESS","00","0")
    if status_bad(response.get("status")) and response.get("type")!="FeatureCollection":
        raise BuildingSourceError("PROVIDER_REJECTED")
    if "exception" in str(response.get("type","")).lower() or "error" in response:
        raise BuildingSourceError("PROVIDER_REJECTED")
    candidates=[response,
                response.get("response",{}).get("result",{}).get("featureCollection",{}),
                response.get("result",{}),
                response.get("featureCollection",{})]
    for item in candidates:
        if isinstance(item,dict) and isinstance(item.get("features"),list):
            count=item.get("totalFeatures",item.get("totalfeatures",
                    response.get("totalFeatures")))
            if count in (None,"unknown","UNKNOWN"): count=None
            try:count=int(count) if count is not None else None
            except (ValueError,TypeError):raise BuildingSourceError("PROVIDER_FORMAT")
            if count is not None and count<0:raise BuildingSourceError("PROVIDER_FORMAT")
            return item["features"],count
    raise BuildingSourceError("PROVIDER_FORMAT")


def _validate_bounds(box):
    if len(box)!=4 or not all(isinstance(v,(int,float)) and math.isfinite(v) for v in box):
        raise BuildingSourceError("INVALID_BBOX")
    x1,y1,x2,y2=box
    if not (120<x1<x2<135 and 30<y1<y2<45):
        raise BuildingSourceError("INVALID_BBOX")


def _split(box):
    a,b,c,d=box
    mx,my=(a+c)/2,(b+d)/2
    return [(a,b,mx,my),(mx,b,c,my),(a,my,mx,d),(mx,my,c,d)]


def collect_gis_buildings(geometry, facts=None, *,max_features=MAX_FEATURES):
    """Acquire georeferenced GIS buildings; return a source result for report.

    Calls at most 85 bounded WFS requests (site bbox recursively divided if
    provider maxFeatures is hit). Does not treat empty results as missing
    credentials or vice versa.
    """
    provider=facts if facts is not None else VWorldFacts()
    site=shape(geometry)
    if site.is_empty or not site.is_valid or site.geom_type not in ("Polygon","MultiPolygon"):
        raise BuildingSourceError("INVALID_BOUNDARY")
    bbox=site.bounds
    _validate_bounds(bbox)
    if hasattr(provider,"configured") and not provider.configured():
        return {"status":"MISSING_SOURCE","error_code":"SERVICE_NOT_READY",
                "detail":"GIS건물통합정보 WFS 인증키 설정 필요 · 건축물 없음으로 판단하지 않습니다.",
                "source":NAME,"features":[]}
    calls=0
    raw=[]
    def fetch(box,depth):
        nonlocal calls
        if calls>=MAX_REQUESTS:raise BuildingSourceError("SOURCE_TRUNCATED")
        calls+=1
        # NED getBldgisSpceWFS: dt_d010. Official reference's
        # EPSG:4326 bbox parameter uses LAT/LON ordering, plus CRS suffix.
        x0,y0,x1,y1=box
        params={
            "typename":"dt_d010",
            "srsName":"EPSG:4326",
            "bbox":",".join(format(n,".10f") for n in (y0,x0,y1,x1))+",EPSG:4326",
            "maxFeatures":str(PAGE_LIMIT),
            "resultType":"results",
            "output":"application/json",
        }
        try:
            payload=provider.fetch(URL,params,expected="json")
        except FactTransportError as exc:
            raise BuildingSourceError(exc.code) from None
        features,total=_extract(payload)
        if total is not None and total<len(features):
            raise BuildingSourceError("PROVIDER_FORMAT")
        if len(features)>=PAGE_LIMIT or (total is not None and total>len(features)):
            if depth>=MAX_SPLITS:
                raise BuildingSourceError("SOURCE_TRUNCATED")
            for cell in _split(box):
                fetch(cell,depth+1)
            return
        raw.extend(features)
        if len(raw)>max_features*4:
            raise BuildingSourceError("SOURCE_TRUNCATED")
    fetch(bbox,0)

    # The same original building can occur in two WFS bbox tiles.
    # Group by stable GIS ID when present, otherwise full-geometry digest.
    collected={}
    for feature in raw:
        if not isinstance(feature,dict) or not isinstance(feature.get("geometry"),dict):
            raise BuildingSourceError("PROVIDER_FORMAT")
        g=shape(feature["geometry"])
        if not g.is_valid or g.is_empty or g.geom_type not in ("Polygon","MultiPolygon"):
            raise BuildingSourceError("PROVIDER_FORMAT")
        lo,la,hi,ha=g.bounds
        if not (120<lo<135 and 120<hi<135 and 30<la<45 and 30<ha<45):
            raise BuildingSourceError("INVALID_CRS")
        if not g.intersects(site):
            continue
        clip=g.intersection(site)
        if clip.is_empty or clip.area<=0:
            continue
        geom_hash=sha256(g.wkb).hexdigest()
        props=normalize_properties(feature.get("properties"))
        key=props.get("building_id") or props.get("shape_id") or geom_hash
        if key in collected:
            prior=collected[key]
            if prior["geometry_hash"]!=geom_hash or prior["properties"]!=props:
                raise BuildingSourceError("DUPLICATE_ID_CONFLICT")
            continue
        collected[key]={"shape":clip,"geometry_hash":geom_hash,"properties":props,"id":str(key)}
        if len(collected)>max_features:raise BuildingSourceError("SOURCE_TRUNCATED")
    out=[]
    for entry in collected.values():
        props=entry["properties"]
        out.append({"type":"Feature","geometry":mapping(entry["shape"]),
            "properties":{
                "building_id":entry["id"],
                "use":props.get("use") or "",
                "structure":props.get("structure") or "",
                "pnu":props.get("pnu") or "",
                "approval_date":props.get("approval_date") or "",
                "building_area":props.get("building_area") or "",
                "total_floor_area":props.get("total_floor_area") or "",
                "coverage":props.get("coverage") or "",
                "far":props.get("far") or "",
                "hub_management_id":props.get("hub_management_id") or "",
            }})
    missing_use=sum(not f["properties"]["use"] for f in out)
    missing_structure=sum(not f["properties"]["structure"] for f in out)
    # If the source has no polygon(s), it is *zero from this bounded WFS
    # request*, not a verified finding that no building exists in reality.
    status="DATA" if out and not missing_use and not missing_structure else "PARTIAL" if out else "EMPTY_RESPONSE"
    return {"status":status,"detail":(
        f"GIS 건물 원도형 수신 {len(out)}건 · 용도 미분류 {missing_use}건"
        f" · 구조 미분류 {missing_structure}건 · WFS 조회 {calls}회"
        " · 실제 건축물 전수·대장번호 대조는 별도 확인"),
        "source":NAME,"source_url":"https://www.data.go.kr/data/15123970/openapi.do",
        "queried_at":datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds"),
        "features":out,
        "acquisition":"OFFICIAL_WFS",
        "coverage_assurance":"BOUNDED_PROVIDER_RESPONSE",
        "total_features":len(out),
        "missing_use":missing_use,
        "missing_structure":missing_structure,
        "request_count":calls}
