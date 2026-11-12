"""Contour-height SHP -> TIN -> statutory 10 m grid slope reference.

Never interpolate a slope without observed height attributes (or source Z).
This is a reproducible *preliminary* calculation: a Shapely Delaunay mesh
does not enforce contour breaklines, and professional survey validation is
required before a statutory 산지전용 평균경사도조사서 is submitted.
"""
import math
from collections import defaultdict

from pyproj import Transformer
from shapely.geometry import MultiPoint, Point, box, mapping, shape
from shapely.ops import transform, triangulate, unary_union
from shapely.strtree import STRtree

GRID_M = 10.0
MAX_CONTOUR_POINTS = 12000
MAX_TRIANGLES = 24000
MAX_CELLS = 25000
MAX_OUTPUT_FEATURES = 5000
HEIGHT_FIELDS = (
    "elev", "elevation", "height", "contour", "altitude", "z",
    "elev_m", "alt", "level", "hgt", "등고값", "등고선표고", "표고", "고도",
)
CLASSES = [
    (0, 5, "5° 미만", "#3579bc"),
    (5, 10, "5~10°", "#5da4c9"),
    (10, 15, "10~15°", "#8cc9ab"),
    (15, 20, "15~20°", "#c2d78a"),
    (20, 25, "20~25°", "#ebd774"),
    (25, 30, "25~30°", "#eaa666"),
    (30, 35, "30~35°", "#df7951"),
    (35, float("inf"), "35° 이상", "#bc4848"),
]


def _number(value):
    try:
        if value is None or str(value).strip() == "":
            return None
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def _source_height(properties, geometry, specified_field):
    props = {str(k).strip().lower(): v for k, v in (properties or {}).items()}
    names = ((specified_field.lower(),) if specified_field else HEIGHT_FIELDS)
    for field in names:
        h = _number(props.get(field))
        if h is not None:
            return h, field
    if specified_field:
        return None, None
    # As a fallback, accept real 3-D SHP coordinates only when the whole
    # line has one height. An arbitrary Z of 0 in a 2-D dataset is not height.
    if geometry.has_z:
        try:
            members = geometry.geoms if geometry.geom_type == "MultiLineString" else (geometry,)
            values = [float(c[2]) for line in members for c in line.coords if len(c) >= 3]
            if values and max(values)-min(values) < 0.01 and all(math.isfinite(z) for z in values):
                return values[0], "GEOMETRY_Z"
        except (TypeError, AttributeError):
            pass
    return None, None


def _points_for_line(line, z, study, result):
    if line.is_empty or not line.intersects(study):
        return
    clipped = line.intersection(study)
    segments = (clipped.geoms if hasattr(clipped, "geoms") else (clipped,))
    for part in segments:
        if part.geom_type == "Point":
            result.append((part.x, part.y, z))
            continue
        if part.geom_type != "LineString" or part.length <= 0:
            continue
        # Preserve every contour vertex while filling long source segments.
        coords = list(part.coords)
        for i in range(len(coords) - 1):
            a, b = coords[i], coords[i + 1]
            distance = math.hypot(b[0]-a[0], b[1]-a[1])
            steps = max(1, math.ceil(distance / GRID_M))
            for j in range(steps):
                t = j / steps
                result.append((a[0]+(b[0]-a[0])*t, a[1]+(b[1]-a[1])*t, z))
        result.append((coords[-1][0], coords[-1][1], z))


def _vertices(entry, root, site):
    from development_sources import official_features
    source_features, source_crs = official_features(entry, root)
    if not source_crs:
        raise ValueError("Contour SHP requires source CRS (PRJ or manifest crs)")
    t = Transformer.from_crs(source_crs, "EPSG:5179", always_xy=True)
    if not t.target_crs.is_projected:
        raise ValueError("TIN requires projected coordinates")
    study = site.buffer(150)
    height_field = entry.get("elevation_field")
    candidates, accepted, missing = [], 0, 0
    for feature in source_features:
        source_geometry = shape(feature["geometry"])
        if source_geometry.is_empty or source_geometry.geom_type not in (
                "LineString", "MultiLineString", "Point"):
            continue
        geom = transform(t.transform, source_geometry)
        if not geom.intersects(study):
            continue
        z, actual_field = _source_height(feature.get("properties"), source_geometry,
                                         height_field)
        if z is None:
            missing += 1
            continue
        accepted += 1
        if geom.geom_type == "Point":
            candidates.append((geom.x, geom.y, z))
        else:
            _points_for_line(geom, z, study, candidates)
        if len(candidates) > MAX_CONTOUR_POINTS * 3:
            raise ValueError("Contour point density too high; register a site-clipped SHP")
    if accepted == 0:
        raise ValueError("No contour/spot-height features with explicit elevation")
    if missing:
        raise ValueError("Some intersecting contour lines have no valid elevation")
    vertices = {}
    for x, y, z in candidates:
        key = (round(x, 5), round(y, 5))
        old = vertices.get(key)
        if old is not None and abs(old - z) > 0.05:
            raise ValueError("Conflicting heights at the same contour coordinate")
        vertices[key] = z
    if len(vertices) < 3 or len(vertices) > MAX_CONTOUR_POINTS:
        raise ValueError("Insufficient or excessive unique contour vertices")
    if len(set(round(z, 3) for z in vertices.values())) < 2:
        raise ValueError("At least two elevations required to establish a slope surface")
    return vertices, accepted, height_field or "source ELEV/HEIGHT or Z"


def _triangle_slope(coordinates, values):
    (ax, ay), (bx, by), (cx, cy) = coordinates
    za, zb, zc = [values[(round(x, 5), round(y, 5))] for x, y in coordinates]
    ux, uy, uz = bx-ax, by-ay, zb-za
    vx, vy, vz = cx-ax, cy-ay, zc-za
    det = ux*vy-uy*vx
    if abs(det) < 1e-9:
        return None
    dzdx = (uz*vy-uy*vz)/det
    dzdy = (ux*vz-uz*vx)/det
    return math.degrees(math.atan(math.hypot(dzdx, dzdy)))


def _category(value):
    return next((i for i, (lo, hi, name, color) in enumerate(CLASSES)
                 if lo <= value < hi), len(CLASSES)-1)


def analyze(geometry):
    """Read official contour SHP from national_data manifest's 'contours' entry.

    Inputs: 2-D EPSG:4326 site Polygon/MultiPolygon; metric TIN uses EPSG:5179.
    Output: grid-level source traceability, area-weighted statistics, thematic
    polygon maps and uncertainty flags; never a legal permit determination.
    """
    from development_sources import manifest_entry
    entry, root = manifest_entry("contours")
    if not entry:
        return {"status": "MISSING_SOURCE", "source": None,
                "detail": "수치지형도 등고선 SHP 원본 미등록 · 법정 평균경사도 참고 분석 불가"}
    target = shape(geometry)
    if target.is_empty or not target.is_valid or target.area <= 0:
        raise ValueError("Invalid analysis boundary")
    coverage = shape(entry["coverage"])
    if not coverage.is_valid or not coverage.covers(target):
        return {"status": "OUTSIDE_COVERAGE", "source": entry["provider"],
                "detail": "등록 수치지형도 수록범위가 분석대상지를 덮지 못함"}
    project = Transformer.from_crs("EPSG:4326", "EPSG:5179", always_xy=True)
    inverse = Transformer.from_crs("EPSG:5179", "EPSG:4326", always_xy=True)
    site = transform(project.transform, target)
    points, source_count, height_field = _vertices(entry, root, site)
    triangulated = triangulate(MultiPoint(list(points)))
    triangles, slopes = [], []
    for triangle in triangulated:
        if triangle.area <= 1e-9 or not triangle.intersects(site):
            continue
        coords = [tuple(c[:2]) for c in list(triangle.exterior.coords)[:3]]
        slope = _triangle_slope(coords, points)
        if slope is None:
            continue
        triangles.append(triangle)
        slopes.append(slope)
    if not triangles or len(triangles) > MAX_TRIANGLES:
        raise ValueError("No usable TIN triangles or TIN too large")
    tree = STRtree(triangles)
    west, south, east, north = site.bounds
    cols = math.ceil((east-west)/GRID_M)
    rows = math.ceil((north-south)/GRID_M)
    if cols*rows > MAX_CELLS:
        raise ValueError("Site exceeds 25,000 statutory 10m grid cells")
    groups = defaultdict(lambda: {"area_m2": 0.0, "cells": 0})
    records, covered, weighted, steep_area = [], 0.0, 0.0, 0.0
    # Regulatory origin: intersection of west and north boundary tangents.
    for row in range(rows):
        y1 = north-row*GRID_M
        y0 = max(south, y1-GRID_M)
        for col in range(cols):
            x0 = west+col*GRID_M
            x1 = min(east, x0+GRID_M)
            cell = box(x0, y0, x1, y1).intersection(site)
            if cell.is_empty or cell.area <= 1e-8:
                continue
            pieces, piece_slopes = [], []
            for i in tree.query(cell):
                index = int(i)
                overlap = triangles[index].intersection(cell)
                if not overlap.is_empty and overlap.area > 1e-8:
                    pieces.append(overlap)
                    piece_slopes.append((slopes[index], overlap.area))
            area = sum(a for _, a in piece_slopes)
            if area <= 1e-8:
                continue
            cell_slope = sum(s*a for s, a in piece_slopes)/area
            if not math.isfinite(cell_slope):
                continue
            # Slight negative differences may be GEOS precision only; never
            # silently represent substantial missing TIN coverage as complete.
            valid_geom = cell if area >= cell.area*(1-1e-8) else unary_union(pieces)
            index = _category(cell_slope)
            groups[index]["area_m2"] += area
            groups[index]["cells"] += 1
            covered += area
            weighted += area*cell_slope
            if cell_slope >= 25:
                steep_area += area
            records.append({"col": col, "row": row, "mean_slope_deg": cell_slope,
                            "area_m2": area, "grid_coverage_pct": min(100, 100*area/cell.area),
                            "class_index": index, "geometry": mapping(transform(inverse.transform, valid_geom))})
    coverage_pct = min(100, 100*covered/site.area) if site.area > 0 else 0
    if not records:
        return {"status": "INSUFFICIENT_TIN", "source": entry["provider"],
                "detail": "등고선 TIN이 대상지에 교차하지 않음 · 경사도 산출 불가"}
    mapped = len(records) <= MAX_OUTPUT_FEATURES
    features = []
    if mapped:
        for item in records:
            index = item["class_index"]
            _, _, name, color = CLASSES[index]
            features.append({"type": "Feature", "geometry": item["geometry"],
                             "properties": {"name": name, "color": color,
                                            "class_code": f"C{index+1:02d}",
                                            "grid_col": item["col"], "grid_row": item["row"],
                                            "slope_deg": round(item["mean_slope_deg"], 4),
                                            "area_m2": item["area_m2"]}})
    legend = [
        {"class_code": f"C{index+1:02d}", "name": name, "color": color,
         "area_m2": round(groups[index]["area_m2"], 2),
         "pct": round(100*groups[index]["area_m2"]/site.area, 2),
         "count": groups[index]["cells"]}
        for index, (lo, hi, name, color) in enumerate(CLASSES) if groups[index]["cells"]
    ]
    full = coverage_pct >= 99.999 and mapped
    return {
        "status": "DATA" if coverage_pct >= 99.999 else "PARTIAL",
        "detail": "공식 수치지형도 등고선 표고속성으로 Delaunay TIN 생성 후 "
                  "10m×10m 격자별 삼각면 면적가중 경사도 계산. "
                  "비구속 Delaunay TIN이며 등고선 breakline 보전 여부와 "
                  "원본 최신성은 전문가 검증 전, 산지전용허가 적합판정 아님.",
        "source": entry["provider"], "source_url": entry["source_url"],
        "as_of": entry["as_of"], "height_field": height_field,
        "grid_size_m": GRID_M, "grid_origin_epsg5179": [west, north],
        "crs": "EPSG:5179", "contour_features_used": source_count,
        "tin_method": "UNCONSTRAINED_DELAUNAY_CONTOUR_VERTICES",
        "tin_triangles": len(triangles), "tin_vertices": len(points),
        "site_area_m2": site.area, "covered_area_m2": covered,
        "coverage_pct": coverage_pct, "valid_grid_count": len(records),
        "mean_slope_deg": weighted/covered,
        "steep_25deg_area_m2": steep_area,
        "steep_25deg_pct_of_site": 100*steep_area/site.area,
        "legend": legend, "features": features, "map_complete": mapped,
        "grid_records": [{k:v for k,v in item.items() if k!="geometry"} for item in records],
        "legal_submission_ready": False,
        "legal_review_note": "제출용 평균경사도조사서는 자격자 작성 및 "
                             "수치지형도·TIN 조건·격자 면적 검증 별도 필요",
    }
