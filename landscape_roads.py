"""Nationwide road-facility candidate index for landscape review.

Source: MOLIT road (current) 2025-06-25, VWorld dataset 30074 / C_UQ151.
This is a *planning-facility* map, not proof that a road is opened.
Usage:
  python landscape_roads.py inspect /path/to/C_UQ151.zip
  python landscape_roads.py build /path/to/C_UQ151.zip /secure/path/roads.sqlite \
      --width-field VERIFIED_FIELD --source-crs EPSG:5179

Only an exact, user-verified field may be mapped as numeric width.
For 광로/대로 classification, conservative class minima may be used as
a distinct non-measured inference; no widths are inferred from geometry.
Never distribute or commercially reuse unlicensed source data.
"""
import argparse
import io
import json
import math
import os
import re
import sqlite3
import zipfile
from pathlib import Path

import shapefile
from pyproj import CRS, Transformer
from shapely.geometry import shape, LineString, MultiLineString, Point, Polygon
from shapely.ops import transform
from shapely import wkb

SOURCE_ID = "MOLIT_ROAD_CURRENT_20250625"
SOURCE_URL = "https://www.data.go.kr/data/15125057/fileData.do"
SCHEMA_VERSION = "1"
WIDTH_CLASS_MIN = {"광로": 40.0, "대로": 25.0}
WIDTH_PATTERN = re.compile(r"(?<![0-9])([0-9]+(?:\\.[0-9]+)?)\\s*(?:m|미터)(?![a-zA-Z])", re.I)


def _read_source(path):
    path = Path(path)
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            files = [p for p in z.namelist() if p.lower().endswith(".shp")]
            if len(files) != 1:
                raise ValueError("SHP archive must contain exactly one .shp (or extract the dataset first)")
            base = files[0][:-4]
            def member(ext):
                matches = [x for x in z.namelist() if x.lower() == (base + ext).lower()]
                return io.BytesIO(z.read(matches[0])) if matches else None
            shp, shx, dbf = member(".shp"), member(".shx"), member(".dbf")
            prj = member(".prj")
            if dbf is None:
                raise ValueError("SHP archive lacks DBF attributes")
            wkt = prj.read().decode("utf-8", "replace") if prj else None
            reader = shapefile.Reader(shp=shp, shx=shx, dbf=dbf, encoding="cp949", encodingErrors="replace")
            return reader, wkt
    shp = path if path.suffix.lower() == ".shp" else path.with_suffix(".shp")
    if not shp.is_file():
        raise FileNotFoundError(shp)
    prj = shp.with_suffix(".prj")
    return shapefile.Reader(str(shp), encoding="cp949", encodingErrors="replace"), (prj.read_text(errors="replace") if prj.exists() else None)


def inspect_source(path):
    reader, wkt = _read_source(path)
    try:
        fields = [f[0] for f in reader.fields[1:]]
        first = next(reader.iterShapeRecords(), None)
        props = dict(zip(fields, first.record)) if first else {}
        return {"source": SOURCE_ID, "count": len(reader), "shape_type": reader.shapeTypeName,
                "fields": fields, "sample_properties": {k: str(v)[:100] for k,v in props.items()},
                "has_prj": bool(wkt)}
    finally:
        reader.close()


def _min_road_width(attrs, field):
    if field:
        raw = attrs.get(field)
        if raw is not None and str(raw).strip():
            try:
                width = float(str(raw).strip().replace(",", ""))
            except (TypeError, ValueError):
                width = math.nan
            if math.isfinite(width) and 0 < width < 200:
                return width, "numeric_source_field"
    # Officially understood broad road classes only. Treat as lower bound,
    # not as actual surveyed width. Ignore 중로 (12~25m: ambiguous at 20m).
    names = " ".join(str(v or "") for k,v in attrs.items()
                     if re.search(r"(?:DGM_NM|ROAD_CLASS|ROAD_TYPE|도로종류|시설명|결정명)$", k, re.I))
    for label, minimum in WIDTH_CLASS_MIN.items():
        if label in names:
            return minimum, "classification_minimum_not_measured"
    return None, "unverified"


def build_index(source, destination, *, width_field=None, source_crs=None):
    reader, wkt = _read_source(source)
    try:
        fields = [f[0] for f in reader.fields[1:]]
        if width_field and width_field not in fields:
            raise ValueError("Specified width field not in DBF: " + width_field)
        if not (source_crs or wkt):
            raise ValueError("CRS missing: provide --source-crs after verifying dataset coordinates")
        src = CRS.from_user_input(source_crs or wkt)
        if not src.is_projected and not src.is_geographic:
            raise ValueError("Unsupported source CRS")
        project = Transformer.from_crs(src, CRS.from_epsg(5179), always_xy=True).transform
        out = Path(destination)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".building")
        if tmp.exists():
            tmp.unlink()
        db = sqlite3.connect(str(tmp))
        try:
            db.executescript("""
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE roads(id INTEGER PRIMARY KEY, width_min REAL, width_basis TEXT NOT NULL,
                               attrs TEXT NOT NULL, geom BLOB NOT NULL);
            CREATE VIRTUAL TABLE road_bounds USING rtree(id,minx,maxx,miny,maxy);
            """)
            entries = {"schema_version":SCHEMA_VERSION, "source_id":SOURCE_ID,
                       "source_url":SOURCE_URL, "source_crs":str(src), "width_field":width_field or "",
                       "road_status":"planning_facility_not_verified_open"}
            db.executemany("INSERT INTO metadata VALUES (?,?)", list(entries.items()))
            included = rejected = 0
            for rec in reader.iterShapeRecords():
                try:
                    attrs = dict(zip(fields, list(rec.record)))
                    width, basis = _min_road_width(attrs, width_field)
                    if width is None or width < 20:
                        rejected += 1
                        continue
                    geom = shape(rec.shape.__geo_interface__)
                    if geom.is_empty or not geom.is_valid:
                        rejected += 1
                        continue
                    if geom.geom_type not in ("LineString", "MultiLineString", "Polygon", "MultiPolygon"):
                        rejected += 1
                        continue
                    geom = transform(project, geom)
                    if geom.is_empty or not geom.is_valid:
                        rejected += 1
                        continue
                    a,b,c,d = geom.bounds
                    cursor = db.execute("INSERT INTO roads(width_min,width_basis,attrs,geom) VALUES (?,?,?,?)",
                        (width,basis,json.dumps(attrs,ensure_ascii=False,default=str),sqlite3.Binary(geom.wkb)))
                    db.execute("INSERT INTO road_bounds VALUES (?,?,?,?,?)", (cursor.lastrowid,a,c,b,d))
                    included += 1
                    if included % 3000 == 0:
                        db.commit()
                except (ValueError, TypeError, AttributeError):
                    rejected += 1
            db.execute("INSERT INTO metadata VALUES (?,?)", ("indexed_features",str(included)))
            db.execute("INSERT INTO metadata VALUES (?,?)", ("unindexed_features",str(rejected)))
            db.commit()
        except Exception:
            db.close()
            tmp.unlink(missing_ok=True)
            raise
        db.close()
        os.replace(tmp,out)
        return {"status":"BUILT","indexed":included,"unindexed":rejected,"path":str(out),
                "source":SOURCE_ID,"width_field":width_field}
    finally:
        reader.close()


def _road_rows(db, bounds, maximum=1200):
    a,b,c,d = bounds
    rows = db.execute("""
      SELECT r.id,r.width_min,r.width_basis,r.geom
      FROM road_bounds bb JOIN roads r ON r.id=bb.id
      WHERE bb.maxx>=? AND bb.minx<=? AND bb.maxy>=? AND bb.miny<=?
      ORDER BY r.id LIMIT ?
    """, (a,c,b,d,maximum+1)).fetchall()
    if len(rows)>maximum:
        return None
    return [(ident,width,basis,wkb.loads(geom)) for ident,width,basis,geom in rows]


def _line_parts(geom):
    if geom.geom_type=="LineString":
        return [geom]
    if geom.geom_type=="MultiLineString":
        return list(geom.geoms)
    return []


def _angle_ok(line_a, line_b, point):
    # Approximate tangent near crossing with nearby segment vectors.
    def vector(line):
        d=line.project(point)
        start=max(0,d-10)
        stop=min(line.length,d+10)
        if stop-start < 0.5:
            return None
        x=line.interpolate(start); y=line.interpolate(stop)
        vx,vy=y.x-x.x,y.y-x.y
        norm=math.hypot(vx,vy)
        return (vx/norm,vy/norm) if norm>0 else None
    a,b=vector(line_a),vector(line_b)
    if not a or not b:return False
    dot=abs(a[0]*b[0]+a[1]*b[1])
    return dot <= math.cos(math.radians(25))


def candidates_from_index(index_path, lat, lng, radius=1150):
    if not Path(index_path).is_file():
        return {"status":"DATA_UNAVAILABLE","candidates":[],"detail":"전국 도로 SHP 인덱스 미배치"}
    radius = max(100,min(int(radius),2000))
    project = Transformer.from_crs(4326,5179,always_xy=True)
    inverse = Transformer.from_crs(5179,4326,always_xy=True)
    x,y=project.transform(lng,lat)
    db=sqlite3.connect(f"file:{Path(index_path).resolve()}?mode=ro",uri=True)
    try:
        metadata=dict(db.execute("SELECT key,value FROM metadata"))
        if metadata.get("schema_version") != SCHEMA_VERSION or metadata.get("source_id") != SOURCE_ID:
            return {"status":"DATA_UNAVAILABLE","candidates":[],"detail":"도로 인덱스 버전/원천 불일치"}
        rows=_road_rows(db,(x-radius-70,y-radius-70,x+radius+70,y+radius+70))
        if rows is None:
            return {"status":"PARTIAL","candidates":[],"detail":"조회범위의 도로가 너무 많아 안전 상한 초과"}
        # Line crossings are geometrically testable. Planning-road polygons
        # alone do not establish true traversable intersection topology.
        lines=[]
        polygon_count=0
        for ident,width,basis,geom in rows:
            parts=_line_parts(geom)
            if not parts:
                polygon_count+=1
            for part in parts:
                if part.length>0.5:lines.append((ident,width,basis,part))
        from shapely.strtree import STRtree
        # Pairwise guarded calculation on local subset. Coordinates in metres.
        if len(lines)>900:
            return {"status":"PARTIAL","candidates":[],"detail":"중심선 밀집으로 안전 상한 초과"}
        output={}
        for i,(ident,wa,ba,ga) in enumerate(lines):
            for other_id,wb,bb,gb in lines[i+1:]:
                if ident==other_id or not ga.bounds[0]<=gb.bounds[2] or ga.bounds[2]<gb.bounds[0]:
                    continue
                if ga.bounds[1]>gb.bounds[3] or ga.bounds[3]<gb.bounds[1]:
                    continue
                hit=ga.intersection(gb)
                if hit.is_empty:continue
                pts = [hit] if hit.geom_type=="Point" else list(hit.geoms) if hit.geom_type=="MultiPoint" else []
                for pt in pts:
                    if pt.distance(Point(x,y))>radius or not _angle_ok(ga,gb,pt):
                        continue
                    # 25-metre clustering: prevent 4 arms of one junction
                    # from generating 6 duplicate recommendations.
                    key=(round(pt.x/25),round(pt.y/25))
                    lon,lat_out=inverse.transform(pt.x,pt.y)
                    candidate={"id":f"national-road-{key[0]}-{key[1]}",
                               "name":"주요도로 교차점 (계획시설 도로)",
                               "lat":lat_out,"lng":lon,"category":"intersection",
                               "roadWidthM":min(wa,wb),"trafficVolume":None,
                               "widthBasis":ba if ba==bb else "mixed_numeric_and_class_minimum",
                               "source":SOURCE_ID,"requiresVerification":False,
                               "verification":"계획시설 도로 · 실제 개설/입체교차/조망 가능성 확인 필요",
                               "roadOpenStatus":"UNKNOWN","junctionVerified":False}
                    if key not in output or candidate["roadWidthM"]>output[key]["roadWidthM"]:
                        output[key]=candidate
        return {"status":"OK" if rows else "EMPTY","candidates":list(output.values())[:100],
                "source":SOURCE_ID,"road_count":len(rows),"line_count":len(lines),
                "polygon_count":polygon_count,
                "detail":("원자료가 면형 위주라면 교차로를 임의 생성하지 않음. 중심선 별도 확보 필요."
                          if polygon_count and not lines else
                          "중심선 교차는 기하 후보이며 실제 평면교차 및 보행접근 확인 필요")}
    finally:
        db.close()


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest="action",required=True)
    p=sub.add_parser("inspect");p.add_argument("source")
    p=sub.add_parser("build");p.add_argument("source");p.add_argument("output")
    p.add_argument("--width-field");p.add_argument("--source-crs")
    args=parser.parse_args()
    print(json.dumps(inspect_source(args.source) if args.action=="inspect" else build_index(
        args.source,args.output,width_field=args.width_field,source_crs=args.source_crs),
        ensure_ascii=False,indent=2))
