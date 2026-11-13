"""Standalone landscape roadway adapter, ported from Seoul urban-redevelopment app.py.

Source fields: TL_SPRD_MANAGE / ROAD_BT / RDS_MAN_NO. Original local SHP
loader uses pyshp iterShapeRecords(bbox=EPSG:5179) and Korean cp949 encoding.
This module deliberately DOES NOT import Seoul redevelopment app.py or its RULEs.
For nationwide use point LANDSCAPE_ROAD_SHP_DIR at a validated dataset with
the same schema; never assume the Seoul bundle covers other provinces.
"""
import math
import os
import re
from pathlib import Path

import shapefile
from shapely.geometry import Point, LineString, MultiLineString, shape
from shapely.ops import transform
from pyproj import Transformer

SOURCE = "TL_SPRD_MANAGE ROAD_BT"


def _road_width_m(properties):
    """Same field order, parsing and 1~100m bounds as Seoul main app.py."""
    by_upper = {str(key).upper(): value for key,value in (properties or {}).items()}
    for key in ("ROAD_BT","ROAD_WIDTH","ROAD_W","WIDTH"):
        value = by_upper.get(key)
        if value in (None,""):
            continue
        if isinstance(value,(int,float)):
            width=float(value)
        else:
            matched=re.search(r"\d+(?:\.\d+)?",str(value).replace(",",""))
            if not matched:continue
            width=float(matched.group(0))
        if math.isfinite(width) and 1<=width<=100:
            return width
    return None


def _road_name(properties):
    by_upper={str(k).upper():v for k,v in (properties or {}).items()}
    return str(by_upper.get("RN") or by_upper.get("ROAD_NM") or by_upper.get("ROAD_NAME") or "").strip()


def _source_shp():
    directory=(os.getenv("LANDSCAPE_ROAD_SHP_DIR") or "").strip()
    if not directory:return None
    base=Path(directory)/"TL_SPRD_MANAGE"
    if all(base.with_suffix(ext).is_file() for ext in (".shp",".shx",".dbf")):
        return str(base)
    return None


def _road_shape_records(base,bbox_metric):
    """Ported from app.py _road_shape_records; bbox filter avoids full SHP loading."""
    reader=shapefile.Reader(base,encoding="cp949",encodingErrors="replace")
    fields=[f[0] for f in reader.fields[1:]]
    result=[]
    try:
        for sr in reader.iterShapeRecords(bbox=bbox_metric):
            try:
                props=dict(zip(fields,list(sr.record)))
                geom=shape(sr.shape.__geo_interface__)
                if geom.is_empty:continue
                quality="valid"
                if not geom.is_valid:
                    repaired=geom.buffer(0)
                    if repaired.is_empty or not repaired.is_valid:continue
                    geom=repaired;quality="repaired_buffer0"
                result.append({"geometry":geom,"properties":props,"geometry_quality":quality})
            except (ValueError,TypeError,AttributeError):
                continue
    finally:
        reader.close()
    return result


def _line_parts(geom):
    if geom.geom_type=="LineString":return [geom]
    if geom.geom_type=="MultiLineString":return list(geom.geoms)
    return []


def _angle_ok(a,b,p):
    def tangent(line):
        d=line.project(p)
        x=line.interpolate(max(0,d-8))
        y=line.interpolate(min(line.length,d+8))
        vx,vy=y.x-x.x,y.y-x.y
        length=math.hypot(vx,vy)
        return (vx/length,vy/length) if length>0.1 else None
    u,v=tangent(a),tangent(b)
    return bool(u and v and abs(u[0]*v[0]+u[1]*v[1])<=math.cos(math.radians(25)))


def candidates_from_centerlines(lat,lng,radius=1150):
    base=_source_shp()
    if not base:
        return {"status":"DATA_UNAVAILABLE","source":SOURCE,"candidates":[],
                "detail":"TL_SPRD_MANAGE 중심선 원자료 미배치 · LANDSCAPE_ROAD_SHP_DIR 확인 필요"}
    radius=max(100,min(int(radius),2000))
    to_metric=Transformer.from_crs(4326,5179,always_xy=True)
    to_wgs=Transformer.from_crs(5179,4326,always_xy=True)
    x,y=to_metric.transform(lng,lat)
    pad=radius+80
    try:
        rows=_road_shape_records(base,[x-pad,y-pad,x+pad,y+pad])
    except (OSError,ValueError,shapefile.ShapefileException):
        return {"status":"DATA_UNAVAILABLE","source":SOURCE,"candidates":[],"detail":"도로 SHP 조회 실패"}
    if len(rows)>5000:
        return {"status":"PARTIAL","source":SOURCE,"candidates":[],"detail":"도로 공간검색 상한 초과"}
    lines=[]
    width_unknown=0
    for row in rows:
        width=_road_width_m(row["properties"])
        if width is None:
            width_unknown+=1
            continue
        if width<20:continue
        props=row["properties"]
        for geom in _line_parts(row["geometry"]):
            if geom.length>=1:
                lines.append((str(props.get("RDS_MAN_NO") or ""),_road_name(props),width,geom))
    if len(lines)>800:
        return {"status":"PARTIAL","source":SOURCE,"candidates":[],"detail":"폭 20m 이상 중심선 과밀 · 범위 축소 필요"}
    points={}
    centre=Point(x,y)
    for i,(ida,na,wa,ga) in enumerate(lines):
        for idb,nb,wb,gb in lines[i+1:]:
            if ida and ida==idb:continue
            if not (ga.bounds[0]<=gb.bounds[2] and gb.bounds[0]<=ga.bounds[2]
                    and ga.bounds[1]<=gb.bounds[3] and gb.bounds[1]<=ga.bounds[3]):
                continue
            hit=ga.intersection(gb)
            if hit.is_empty:continue
            hits=[hit] if hit.geom_type=="Point" else list(hit.geoms) if hit.geom_type=="MultiPoint" else []
            for pt in hits:
                if pt.distance(centre)>radius or not _angle_ok(ga,gb,pt):
                    continue
                key=(round(pt.x/25),round(pt.y/25))
                lon,latout=to_wgs.transform(pt.x,pt.y)
                candidate={"id":f"road-manage-{key[0]}-{key[1]}",
                           "lat":latout,"lng":lon,"name":(na+" · "+nb).strip(" ·") or "주요교차로 후보",
                           "category":"intersection","roadWidthM":min(wa,wb),
                           "trafficVolume":None,"source":SOURCE,
                           "requiresVerification":False,
                           "verification":"도로중심선의 기하학적 교차후보 · 실제 평면교차·접근성·시야 확인 필요",
                           "junctionVerified":False}
                if key not in points or candidate["roadWidthM"]>points[key]["roadWidthM"]:
                    points[key]=candidate
    return {"status":"OK","source":SOURCE,"candidates":list(points.values())[:100],
            "road_count":len(rows),"width_known_20m_lines":len(lines),
            "width_unknown_count":width_unknown,
            "detail":"SHP ROAD_BT 기준 · 실제 교차로 여부와 시야차폐는 미검증"}

