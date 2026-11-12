"""Build reference-only region selectors; never used for a legal/spatial FACT.

Input: vuski/admdongkor ver20261001/HangJeongDong_ver20261001.geojson.
The downloaded original stays outside the repository. Output includes provenance.
"""
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform, unary_union


def build(source, destination):
    raw = Path(source).read_bytes()
    groups = defaultdict(list)
    regions = {"KR": {"id": "KR", "name": "전국", "short_name": "전국", "parent": None}}
    project = Transformer.from_crs(4326, 5179, always_xy=True).transform
    for f in json.loads(raw)["features"]:
        p = f["properties"]
        province, district = p["sido"], p["sgg"]
        regions[province] = {"id": province, "name": p["sidonm"], "short_name": p["sidonm"], "parent": "KR"}
        parent = province
        district_name = p["sggnm"]
        # Preserve municipality -> non-autonomous district ancestry (e.g. Suwon).
        municipality = re.fullmatch(r"(.+?시)\s*(.+구)", district_name)
        if municipality:
            city, ward = municipality.groups()
            district_name = city + " " + ward
            parent = district[:4] + "0"
            regions[parent] = {"id": parent, "name": p["sidonm"] + " " + city, "short_name": city, "parent": province}
        regions[district] = {"id": district, "name": p["sidonm"] + " " + district_name, "short_name": district_name, "parent": parent}
        geom = transform(project, shape(f["geometry"]))
        if not geom.is_valid:
            geom = geom.buffer(0)
        groups[district].append(geom)
    geometries = {key: unary_union(values) for key, values in groups.items()}
    for key in list(regions):
        if key == "KR" or key in geometries:
            continue
        children = [v for v in regions.values() if v["parent"] == key]
        if children and all(c["id"] in geometries for c in children):
            geometries[key] = unary_union([geometries[c["id"]] for c in children])
    # Province unions follow municipality unions.
    for key, region in regions.items():
        if region["parent"] == "KR":
            geometries[key] = unary_union([g for code, g in geometries.items() if len(code) == 5 and code.startswith(key)])
    maps, viewboxes = {}, {}
    for key in regions:
        children = [r for r in regions.values() if r["parent"] == key]
        if not children:
            continue
        combined = unary_union([geometries[r["id"]] for r in children])
        x0, y0, x1, y1 = combined.bounds
        scale = min(450 / (x1-x0), 500 / (y1-y0))
        ox, oy = (500-(x1-x0)*scale)/2, (550-(y1-y0)*scale)/2
        viewboxes[key] = " ".join(str(round(v,2)) for v in (ox-20,oy-20,(x1-x0)*scale+40,(y1-y0)*scale+40))
        def point(x, y):
            return round((x-x0)*scale+ox, 2), round((y1-y)*scale+oy, 2)
        features = []
        for r in children:
            g = geometries[r["id"]].simplify(220 if key == "KR" else 60, preserve_topology=True)
            polygons = [g] if g.geom_type == "Polygon" else list(g.geoms)
            paths = []
            for poly in polygons:
                for ring in [poly.exterior, *poly.interiors]:
                    xy = [point(*c) for c in ring.coords]
                    paths.append("M" + "L".join(f"{x},{y}" for x,y in xy) + "Z")
            largest = max(polygons, key=lambda p: p.area)
            center = largest.representative_point()
            features.append({"id": r["id"], "d": "".join(paths), "label": point(center.x, center.y)})
        maps[key] = features
    metadata = {
        "date": "2026-10-01", "source_url": "https://github.com/vuski/admdongkor/tree/master/ver20261001",
        "sha256": hashlib.sha256(raw).hexdigest(), "license": "CC BY 4.0 / SGIS 공공누리 제1유형",
        "attribution": "통계청 통계지리정보서비스(SGIS) 행정동 경계(공공누리 제1유형), 가공: vuski/admdongkor(CC BY 4.0). 본 플랫폼에서 시도·시군구 통합 및 지도 표시용 단순화.",
        "purpose": "행정구역 선택용 참고 지도. 법정 경계 또는 대상지 편입 판정에 사용하지 않음."
    }
    out = Path(destination)
    out.mkdir(parents=True, exist_ok=True)
    (out/"regions.json").write_text(json.dumps({"metadata":metadata,"regions":list(regions.values()),"maps":maps,"viewboxes":viewboxes}, ensure_ascii=False,separators=(",",":")),encoding="utf-8")
    print(f"Built {len(regions)} regions, {len(maps)} maps")


if __name__ == "__main__":
    build(*sys.argv[1:])
