#!/usr/bin/env python3
"""Query indexed 2025 land-cover tiles for a WGS84 GeoJSON polygon.

Returns one shared FACT for map, area tables, Excel and DXF preparation.
Requires at runtime: shapely and pyproj (already in urban-strategy Dockerfile)
Usage:
  python query_landcover_gpkg.py --data-dir landcover_2025_indexed --site site.geojson --out report.geojson
"""
import argparse
import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path

import struct
from shapely import wkb
from pyproj import Transformer
from shapely.geometry import mapping, shape, Polygon
from shapely import make_valid
from shapely.ops import transform, unary_union

CLASS_LEVELS = {1: ('L1_CODE', 'L1_NAME'), 2: ('L2_CODE', 'L2_NAME'), 3: ('L3_CODE', 'L3_NAME')}


def _decode_gpkg_geometry(data):
    """Read standard GeoPackage geometry blobs, using Shapely already in the app."""
    if not data or len(data) < 8 or data[:2] != b'GP' or data[2] != 0:
        raise ValueError('Invalid GeoPackage binary geometry header')
    flags = data[3]
    envelope_indicator = (flags >> 1) & 7
    lengths = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}
    if envelope_indicator not in lengths:
        raise ValueError('Unsupported GeoPackage envelope type')
    offset = 8 + lengths[envelope_indicator]
    return wkb.loads(data[offset:])


def select_tiles(data_dir, site):
    x1, y1, x2, y2 = site.bounds
    with sqlite3.connect(Path(data_dir) / 'tile_index.sqlite') as con:
        # Tile-index RTree works in WGS84; per-file GeoPackage RTree is native CRS.
        rows = con.execute('''SELECT t.tile_code,t.file,t.feature_count,t.source_crs_wkt
          FROM tiles t JOIN tile_rtree r ON r.id=t.id
          WHERE r.max_lon >= ? AND r.min_lon <= ?
            AND r.max_lat >= ? AND r.min_lat <= ? ORDER BY t.tile_code''',
            (x1, x2, y1, y2)).fetchall()
    return rows


def query(data_dir, site_geojson, *, max_features=15000, include_features=False):
    data_dir = Path(data_dir).resolve()
    if site_geojson.get('type') == 'Feature':
        site_geojson = site_geojson['geometry']
    site = shape(site_geojson)
    if site.is_empty or not site.is_valid or site.geom_type not in ('Polygon','MultiPolygon'):
        raise ValueError('Site must be a valid WGS84 polygon')
    if not (123 < site.bounds[0] < site.bounds[2] < 133 and
            32 < site.bounds[1] < site.bounds[3] < 40):
        raise ValueError('Site is outside the supported Korean coordinate bounds')
    to_metric = Transformer.from_crs('EPSG:4326','EPSG:5179',always_xy=True).transform
    site_metric = transform(to_metric, site)
    site_area = site_metric.area
    if site_area <= 0:
        raise ValueError('Site is degenerate')
    tile_rows = select_tiles(data_dir, site)
    if not tile_rows:
        return {'status': 'NOT_COVERED', 'source_year': '2025', 'tiles': [],
                'coverage_percent': 0, 'site_area_m2': round(site_area,2),
                'note': 'No indexed tile intersects the site; this is not evidence of zero landcover.',
                'features': [], 'groups': {}}
    pieces = []
    selected_features = []
    from_metric = Transformer.from_crs("EPSG:5179", "EPSG:4326", always_xy=True).transform if include_features else None
    repaired_count = 0
    discarded_count = 0
    geometry_by_level = {i: defaultdict(list) for i in (1,2,3)}
    groups_counts = {i: defaultdict(int) for i in (1,2,3)}
    for tile_code, relpath, expected, native_crs in tile_rows:
        from_wgs84 = Transformer.from_crs('EPSG:4326',native_crs,always_xy=True).transform
        native_to_metric = Transformer.from_crs(native_crs,'EPSG:5179',always_xy=True).transform
        search_native = transform(from_wgs84, site)
        path = (data_dir / relpath).resolve()
        if not path.is_relative_to(data_dir) or not path.is_file():
            raise ValueError('Indexed tile is unavailable')
        x1,y1,x2,y2 = search_native.bounds
        with sqlite3.connect(path) as tile_con:
            rows = tile_con.execute('''SELECT c.geom,c.L1_CODE,c.L1_NAME,c.L2_CODE,c.L2_NAME,
                   c.L3_CODE,c.L3_NAME FROM landcover c
                   JOIN rtree_landcover_geom r ON c.fid=r.id
                   WHERE r.maxx >= ? AND r.minx <= ? AND r.maxy >= ? AND r.miny <= ?''',
                   (x1,x2,y1,y2)).fetchall()
        for blob,l1c,l1n,l2c,l2n,l3c,l3n in rows:
            geom = _decode_gpkg_geometry(blob)
            if geom is None or geom.is_empty:
                discarded_count += 1
                continue
            if not geom.is_valid:
                geom = make_valid(geom)
                repaired_count += 1
                if geom.geom_type not in ('Polygon', 'MultiPolygon'):
                    polys = [p for p in getattr(geom, 'geoms', ())
                             if p.geom_type in ('Polygon', 'MultiPolygon')]
                    geom = unary_union(polys) if polys else Polygon()
                if geom.is_empty:
                    discarded_count += 1
                    continue
            metric = transform(native_to_metric, geom).intersection(site_metric)
            if metric.is_empty or metric.area < 1e-8:
                continue
            if not metric.is_valid:
                raise ValueError('Invalid clipped geometry: ' + tile_code)
            attrs = {1:(str(l1c or ''),str(l1n or '')),2:(str(l2c or ''),str(l2n or '')),
                     3:(str(l3c or ''),str(l3n or ''))}
            if any(not c.strip() or not n.strip() for c,n in attrs.values()):
                raise ValueError('Unclassified original landcover feature in ' + tile_code)
            for lev,(code,name) in attrs.items():
                geometry_by_level[lev][(code,name)].append(metric)
                groups_counts[lev][(code,name)] += 1
            pieces.append(metric)
            if include_features:
                selected_features.append({
                    'type': 'Feature',
                    'geometry': mapping(transform(from_metric, metric)),
                    'properties': {'name': attrs[3][1], 'code': attrs[3][0],
                                   'L1_CODE': attrs[1][0], 'L1_NAME': attrs[1][1],
                                   'L2_CODE': attrs[2][0], 'L2_NAME': attrs[2][1],
                                   'L3_CODE': attrs[3][0], 'L3_NAME': attrs[3][1],
                                   'tile_code': tile_code, 'area_m2_epsg5179': round(metric.area,2)}
                })
            if len(pieces)>max_features:
                raise ValueError('Too many polygons. Reduce target size or increase max_features after performance tests.')
    if not pieces:
        return {'status': 'PARTIAL', 'source_year': '2025',
                'tiles': [r[0] for r in tile_rows], 'coverage_percent': 0,
                'site_area_m2': round(site_area,2),
                'note': 'No polygons within available tiles; zero landcover is not confirmed.',
                'features': [], 'groups': {}}
    union = unary_union(pieces)
    coverage = min(100, 100 * union.area / site_area)
    out_groups = {}
    for lev, categories in geometry_by_level.items():
        out_groups[f'L{lev}'] = [
            {'code': code, 'name': name, 'area_m2': round(unary_union(gs).area,2),
             'percent_of_site': round(100*unary_union(gs).area/site_area,2),
             'source_polygon_count': groups_counts[lev][(code,name)]}
            for (code,name),gs in sorted(categories.items())
        ]
    overlap = max(0, sum(x['area_m2'] for x in out_groups['L3']) - union.area)
    status = ('COMPLETE_GEOMETRY' if abs(site_area-union.area) <= max(1.0,site_area*.001)
              and overlap<=max(1.0,site_area*.001) and repaired_count == 0 and discarded_count == 0 else 'PARTIAL')
    # GeoJSON is optional for reports to avoid returning thousands of huge features by default.
    return {'status': status, 'source_year': '2025', 'source':'2025 세분류 토지피복지도 SHP',
            'tiles': [r[0] for r in tile_rows], 'polygon_count': len(pieces),
            'site_area_m2': round(site_area,2), 'covered_area_m2': round(union.area,2),
            'coverage_percent': round(coverage,2), 'cross_class_overlap_m2': round(overlap,2),
            'source_geometries_repaired': repaired_count, 'source_geometries_discarded': discarded_count,
            'groups': out_groups,
            'note': ('Geometry completeness is within stored tiles only; dataset currency and legal coverage require separate validation.'),
            'features': selected_features}


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--data-dir',required=True)
    p.add_argument('--site',required=True)
    p.add_argument('--out')
    p.add_argument('--with-features', action='store_true', help='Include clipped WGS84 GeoJSON polygons')
    args=p.parse_args()
    geo=json.loads(Path(args.site).read_text(encoding='utf-8'))
    result=query(args.data_dir,geo,include_features=args.with_features)
    body=json.dumps(result,ensure_ascii=False,indent=2)
    if args.out:
        Path(args.out).write_text(body,encoding='utf-8')
    else:
        print(body)