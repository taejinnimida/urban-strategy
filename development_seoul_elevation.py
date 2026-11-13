"""Existing Seoul 20m terrain reference -> 4-1 elevation survey fallback.

Uses the bundled 2025 서울시 1:5,000 contour/spot-height-derived
grid only for sites covered by non-nodata cells. Not official DEM and not
a statutory terrain slope/height survey. No changes to app.py or RULEs.
"""
from array import array
from functools import lru_cache
from pathlib import Path
from hashlib import sha256
import json
import math
import sys
import zlib

from pyproj import Transformer
from shapely.geometry import box, mapping, shape
from shapely.ops import transform

GRID_META = "hill_terrain_20m_meta.json"
GRID_HEIGHT = "hill_elevation_20m_i16.zlib"
MAX_CELLS = 160000
MAX_RAW_FEATURES = 5000
SOURCE_LINK = "https://data.seoul.go.kr/dataList/OA-22241/F/1/datasetView.do"


@lru_cache(maxsize=1)
def _load_reference():
    root = Path(__file__).resolve().parent
    meta_path, data_path = root / GRID_META, root / GRID_HEIGHT
    if not meta_path.is_file() or not data_path.is_file():
        return None, None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if meta.get("format_version") != "HILL_TERRAIN_GRID_V1":
        raise ValueError("Unsupported Seoul terrain grid version")
    if meta.get("source_type") != "PLATFORM_DERIVED_REFERENCE":
        raise ValueError("Unexpected terrain provenance")
    if meta.get("grid_crs") != "EPSG:5174" or meta.get("row_order") != "north_to_south":
        raise ValueError("Invalid terrain grid CRS/order")
    dx = float(meta["resolution_m"])
    width, height = int(meta["width"]), int(meta["height"])
    if dx <= 0 or width <= 0 or height <= 0 or width * height > 4_000_000:
        raise ValueError("Invalid Seoul terrain dimensions")
    blob = data_path.read_bytes()
    expected_hash = meta["files"]["elevation"]["sha256"]
    if sha256(blob).hexdigest() != expected_hash:
        raise ValueError("Seoul terrain elevation source checksum mismatch")
    decompressed = zlib.decompress(blob)
    if len(decompressed) != 2 * width * height:
        raise ValueError("Seoul terrain elevation grid size mismatch")
    values = array("h")
    values.frombytes(decompressed)
    if sys.byteorder != "little":
        values.byteswap()
    return meta, values


def seoul_elevation_reference(geometry):
    """Return a labelled reference 4-1 DEM-like FACT, not verified official DEM.

    If the site is outside the coverage or has zero valid source cells, allow
    the existing official DEM failure to remain visible instead of inventing
    heights. Large sites are bounded to prevent memory/CPU exhaustion.
    """
    meta, values = _load_reference()
    if meta is None:
        return {"status": "MISSING_SOURCE",
                "detail": "기존 서울 20m 지형참조 격자 파일 미설치"}
    site_wgs = shape(geometry)
    if site_wgs.is_empty or not site_wgs.is_valid or site_wgs.area <= 0:
        raise ValueError("Invalid site boundary")
    to = Transformer.from_crs("EPSG:4326", "EPSG:5174", always_xy=True)
    back = Transformer.from_crs("EPSG:5174", "EPSG:4326", always_xy=True)
    metric = transform(to.transform, site_wgs)
    dx = float(meta["resolution_m"])
    ox = float(meta["origin_x_west_edge"])
    oy = float(meta["origin_y_north_edge"])
    width, height = int(meta["width"]), int(meta["height"])
    grid_extent = box(ox, oy - height * dx, ox + width * dx, oy)
    if not grid_extent.intersects(metric) or metric.intersection(grid_extent).area <= 0:
        return {"status": "OUTSIDE_COVERAGE",
                "detail": "서울 20m 지형참조 격자 수록범위 밖"}
    minx, miny, maxx, maxy = metric.bounds
    c0 = max(0, math.floor((minx-ox)/dx))
    c1 = min(width-1, math.ceil((maxx-ox)/dx))
    r0 = max(0, math.floor((oy-maxy)/dx))
    r1 = min(height-1, math.ceil((oy-miny)/dx))
    cols, rows = c1-c0+1, r1-r0+1
    if cols <= 0 or rows <= 0:
        return {"status": "OUTSIDE_COVERAGE",
                "detail": "서울 20m 지형참조 수록범위 밖"}
    if cols*rows > MAX_CELLS:
        return {"status": "TOO_LARGE",
                "detail": "대상지 지형격자 16만 셀 초과 · 분석구역 분할 필요"}
    elevations, valid_cells, features = [], [], []
    area_covered = 0.
    nodata = int(meta.get("nodata_i16", -32768))
    scale = float(meta["scales"]["elevation_per_meter"])
    for rr in range(r0, r1+1):
        north = oy - rr*dx
        for cc in range(c0, c1+1):
            west = ox + cc*dx
            intersection = box(west,north-dx,west+dx,north).intersection(metric)
            if intersection.area <= dx*dx*1e-8:
                continue
            raw = values[rr*width+cc]
            if raw == nodata:
                continue
            z = raw/scale
            a = intersection.area
            area_covered += a
            elevations.append((z,a))
            # _elevation_polygonize reads south->north and west->east.
            valid_cells.append((r1-rr,cc-c0,z))
            if len(features)<MAX_RAW_FEATURES:
                features.append({"type":"Feature",
                                 "geometry":mapping(transform(back.transform,intersection)),
                                 "properties":{"elevation_m":z,"area_m2":a}})
    if not elevations:
        return {"status":"OUTSIDE_COVERAGE",
                "detail":"대상지 내부 서울 20m 참조 표고 유효격자 없음"}
    from development_sources import _elevation_groups, _elevation_polygonize
    groups, mapped, coverage_pct, is_complete = _elevation_groups(
        elevations,features,metric.area)
    if not is_complete:
        mapped, is_complete = _elevation_polygonize(
            valid_cells,rows,cols,ox+c0*dx,oy-(r1+1)*dx,dx,metric,back,groups)
    weights = sum(a for _,a in elevations)
    heights = [z for z,_ in elevations]
    stats = {"min":min(heights), "max":max(heights),
             "mean":sum(z*a for z,a in elevations)/weights}
    status = "DATA" if coverage_pct >= 99.999 else "PARTIAL"
    return {
        "status":"REFERENCE",
        "source_type":"PLATFORM_DERIVED_REFERENCE",
        "elevation_status":status,
        "detail":("서울시 수치지형도 등고선·표고점의 플랫폼 가공 20m 표고격자. "
                  "공식 DEM 원도형 및 법정 표고·경사도 조사서가 아닌 초기검토용 참조자료."
                  f" · 유효 표고면적 {coverage_pct:.2f}%."),
        "source":"서울시 1:5,000 수치지형 기반 20m 플랫폼 표고참조",
        "source_url":SOURCE_LINK,
        "as_of":meta["source_extract_date"],
        "resolution_m":dx,
        "elevation":stats,
        "elevation_groups":groups,
        "elevation_features":mapped,
        "elevation_map_complete":is_complete,
        "elevation_coverage_pct":coverage_pct,
        "dem_site_area_m2":metric.area,
        "map_truncated":not is_complete,
        "missing_elevation_cells":sum(1 for _ in []) if status == "DATA" else None,
        "legal_submission_ready":False,
    }
