#!/usr/bin/env python3
"""Build spatially indexed, per-tile GeoPackages from nested Korean landcover ZIP.

Usage:
    python convert_landcover_to_gpkg.py --input 토지피복도.zip --output landcover_2025_indexed

Dependencies (conversion machine only): pip install pyogrio geopandas pyproj
The converter never modifies the source, and all output files are local.
It stores the original shapefile columns and CRS; it does not claim regional coverage.
"""
import argparse
import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pyogrio
from pyproj import CRS, Transformer

REQUIRED = ("L1_CODE", "L1_NAME", "L2_CODE", "L2_NAME", "L3_CODE", "L3_NAME")


def _hash_stream(filename):
    h = hashlib.sha256()
    with open(filename, 'rb') as fh:
        for chunk in iter(lambda: fh.read(4 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _safe_inner_members(nested):
    out = {}
    for member in nested.infolist():
        name = Path(member.filename).name
        ext = Path(name).suffix.lower()
        if ext in ('.shp', '.shx', '.dbf', '.prj'):
            stem = Path(name).stem
            if not re.fullmatch(r'\d{8}', stem):
                raise ValueError(f'Unexpected SHP stem: {name}')
            if ext in out:
                raise ValueError(f'Duplicate {ext} in {nested.filename}')
            out[ext] = (name, member)
    if set(out) != {'.shp', '.shx', '.dbf', '.prj'}:
        raise ValueError(f'Missing SHP parts: {nested.filename}')
    stem = Path(out['.shp'][0]).stem
    if any(Path(v[0]).stem != stem for v in out.values()):
        raise ValueError('Mixed tile stems in one archive')
    return stem, out


def _initialize_index(index_path):
    con = sqlite3.connect(index_path)
    con.executescript('''
        PRAGMA journal_mode=DELETE;
        CREATE TABLE IF NOT EXISTS tiles (
          id INTEGER PRIMARY KEY, tile_code TEXT NOT NULL UNIQUE,
          file TEXT NOT NULL, feature_count INTEGER NOT NULL,
          source_zip TEXT NOT NULL, archive_sha256 TEXT NOT NULL,
          source_crs_wkt TEXT NOT NULL,
          min_lon REAL NOT NULL, min_lat REAL NOT NULL,
          max_lon REAL NOT NULL, max_lat REAL NOT NULL,
          gpkg_bytes INTEGER NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS tile_rtree USING rtree(
          id, min_lon, max_lon, min_lat, max_lat
        );
        CREATE INDEX IF NOT EXISTS idx_tiles_code ON tiles(tile_code);
    ''')
    return con


def _bbox_wgs84(native_bounds, native_crs):
    transformer = Transformer.from_crs(CRS.from_user_input(native_crs), 'EPSG:4326', always_xy=True)
    x1, y1, x2, y2 = (float(v) for v in native_bounds)
    xs, ys = [], []
    # Densification makes the approximate lat/lon envelope safer over large tiles.
    for i in range(9):
        t = i / 8
        for x, y in ((x1 + (x2 - x1) * t, y1), (x1 + (x2 - x1) * t, y2),
                     (x1, y1 + (y2 - y1) * t), (x2, y1 + (y2 - y1) * t)):
            a, b = transformer.transform(x, y)
            xs.append(a)
            ys.append(b)
    if not (123 < min(xs) < max(xs) < 133 and 32 < min(ys) < max(ys) < 40):
        raise ValueError('Tile bbox outside expected Korean coordinate extent')
    return min(xs), min(ys), max(xs), max(ys)


def convert(source_zip, output, limit=None, progress=10):
    source_zip = Path(source_zip).resolve()
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    tile_dir = output / 'tiles'
    tile_dir.mkdir(exist_ok=True)
    index_path = output / 'tile_index.sqlite'
    con = _initialize_index(index_path)
    source_sha = _hash_stream(source_zip)
    source_ref = output / 'source_fingerprint.json'
    if source_ref.exists():
        prior = json.loads(source_ref.read_text(encoding='utf-8'))
        if prior.get('input_sha256') != source_sha:
            raise ValueError('Existing output index refers to a different source ZIP. Use another output directory.')
    else:
        source_ref.write_text(json.dumps({'input_file': source_zip.name,
                                          'input_sha256': source_sha,
                                          'format': 'nested-zip-shapefile-2025',
                                          'created_utc': datetime.now(timezone.utc).isoformat()},
                                         ensure_ascii=False, indent=2), encoding='utf-8')
    done = 0
    with zipfile.ZipFile(source_zip) as outer:
        archives = sorted((m for m in outer.infolist() if m.filename.lower().endswith('.zip')), key=lambda m: m.filename)
        if limit is not None:
            archives = archives[:limit]
        print(f'Nested archives: {len(archives)}; source_sha256={source_sha[:16]}...', flush=True)
        for n, member in enumerate(archives, start=1):
            started = time.monotonic()
            if member.file_size > 80_000_000:
                raise ValueError(f'Oversized inner ZIP: {member.filename}')
            archive_bytes = outer.read(member)
            digest = hashlib.sha256(archive_bytes).hexdigest()
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as inner:
                tile_code, contents = _safe_inner_members(inner)
                dest = tile_dir / f'{tile_code}.gpkg'
                old = con.execute('SELECT archive_sha256 FROM tiles WHERE tile_code=?', (tile_code,)).fetchone()
                if old and old[0] == digest and dest.exists():
                    done += 1
                    continue
                with tempfile.TemporaryDirectory(prefix='landcover_tile_') as tempdir:
                    for ext, (name, info) in contents.items():
                        if info.file_size > 200_000_000:
                            raise ValueError('SHP component unexpectedly oversized')
                        target_path = Path(tempdir) / name
                        with target_path.open('wb') as out:
                            out.write(inner.read(info))
                    shp = Path(tempdir) / contents['.shp'][0]
                    gdf = pyogrio.read_dataframe(shp)
                    if not gdf.crs or not set(REQUIRED).issubset(gdf.columns):
                        raise ValueError(f'Missing projection or classification columns for {tile_code}')
                    if not len(gdf) or gdf.geometry.isna().any() or gdf.geometry.is_empty.any():
                        raise ValueError(f'Missing geometries for {tile_code}')
                    bounds = _bbox_wgs84(gdf.total_bounds, gdf.crs)
                    # RTree auto-creation is a GeoPackage standard feature.
                    # Write to same directory so atomic rename works reliably.
                    temporary_gpkg = tile_dir / f'.{tile_code}.tmp.gpkg'
                    try:
                        temporary_gpkg.unlink(missing_ok=True)
                        pyogrio.write_dataframe(gdf, temporary_gpkg, layer='landcover',
                                               driver='GPKG', spatial_index=True,
                                               dataset_metadata={'source_archive': member.filename,
                                                                 'source_sha256': digest,
                                                                 'classification_year': '2025'})
                        chk = sqlite3.connect(temporary_gpkg)
                        try:
                            if not chk.execute("SELECT 1 FROM sqlite_master WHERE name='rtree_landcover_geom' ").fetchone():
                                raise ValueError('GeoPackage RTree missing')
                        finally:
                            chk.close()
                        os.replace(temporary_gpkg, dest)
                    finally:
                        temporary_gpkg.unlink(missing_ok=True)
                    size = dest.stat().st_size
                    row = (tile_code, f'tiles/{tile_code}.gpkg', len(gdf), member.filename, digest,
                           gdf.crs.to_wkt(), bounds[0], bounds[1], bounds[2], bounds[3], size)
                    with con:
                        con.execute('''INSERT INTO tiles(tile_code,file,feature_count,source_zip,
                          archive_sha256,source_crs_wkt,min_lon,min_lat,max_lon,max_lat,gpkg_bytes)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?)
                          ON CONFLICT(tile_code) DO UPDATE SET
                          file=excluded.file, feature_count=excluded.feature_count,
                          source_zip=excluded.source_zip, archive_sha256=excluded.archive_sha256,
                          source_crs_wkt=excluded.source_crs_wkt,
                          min_lon=excluded.min_lon,min_lat=excluded.min_lat,
                          max_lon=excluded.max_lon,max_lat=excluded.max_lat,
                          gpkg_bytes=excluded.gpkg_bytes''', row)
                        pk = con.execute('SELECT id FROM tiles WHERE tile_code=?', (tile_code,)).fetchone()[0]
                        con.execute('INSERT OR REPLACE INTO tile_rtree VALUES (?,?,?,?,?)',
                                    (pk, bounds[0], bounds[2], bounds[1], bounds[3]))
            done += 1
            if n == 1 or n % progress == 0 or n == len(archives):
                print(f'[{n}/{len(archives)}] {tile_code}: {len(gdf)} polygons, '
                      f'{size/1e6:.1f} MB, {time.monotonic()-started:.1f}s', flush=True)
    totals = con.execute('''SELECT count(*),sum(feature_count),sum(gpkg_bytes),
                         min(min_lon),min(min_lat),max(max_lon),max(max_lat) FROM tiles''').fetchone()
    stats = {'input_file': source_zip.name, 'input_sha256': source_sha,
             'tile_count': totals[0], 'feature_count': totals[1],
             'total_gpkg_bytes': totals[2], 'tile_bbox_extent_wgs84': list(totals[3:]),
             'classification': ['L1_CODE','L1_NAME','L2_CODE','L2_NAME','L3_CODE','L3_NAME'],
             'spatial_index': 'GeoPackage RTree per tile, SQLite RTree for tile discovery',
             'note': 'Bounding-box extent does not demonstrate complete Seoul/Gyeonggi coverage. ',
             'created_utc': datetime.now(timezone.utc).isoformat()}
    (output/'manifest.json').write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding='utf-8')
    con.close()
    print('DONE', json.dumps(stats, ensure_ascii=False), flush=True)
    return stats


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--max-tiles', type=int, default=None)
    args = parser.parse_args()
    convert(args.input, args.output, limit=args.max_tiles)