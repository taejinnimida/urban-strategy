"""Isolated tests: no network calls, no real R2 credentials or production data."""
import io
import os
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shapely import wkb
from shapely.geometry import box, mapping

import development_landcover_r2 as mod

SITE = mapping(box(127.0001, 37.0001, 127.0099, 37.0099))
POLY = box(127.0, 37.0, 127.01, 37.01)
CODE = '37608093'


def _index(path):
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE tiles(id INTEGER PRIMARY KEY, tile_code TEXT, file TEXT,
              feature_count INTEGER, source_crs_wkt TEXT);
            CREATE VIRTUAL TABLE tile_rtree USING rtree(id,min_lon,max_lon,min_lat,max_lat);
        ''')
        db.execute('INSERT INTO tiles VALUES(1,?,?,?,?)', (CODE, f'tiles/{CODE}.gpkg', 1, 'EPSG:4326'))
        db.execute('INSERT INTO tile_rtree VALUES(1,127,127.01,37,37.01)')
    return path.read_bytes()


def _tile(path):
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE landcover(fid INTEGER PRIMARY KEY,geom BLOB,L1_CODE TEXT,L1_NAME TEXT,
              L2_CODE TEXT,L2_NAME TEXT,L3_CODE TEXT,L3_NAME TEXT);
            CREATE VIRTUAL TABLE rtree_landcover_geom USING rtree(id,minx,maxx,miny,maxy);
        ''')
        blob = b'GP\x00\x01' + struct.pack('<i', 4326) + wkb.dumps(POLY)
        db.execute('INSERT INTO landcover VALUES(1,?,?,?,?,?,?,?)',
                   (blob, '100', '시가화', '110', '주거', '111', '단독주거'))
        db.execute('INSERT INTO rtree_landcover_geom VALUES(1,127,127.01,37,37.01)')
    return path.read_bytes()


class FakeClient:
    def __init__(self, items):
        self.items = items
        self.calls = []

    def get_object(self, *, Bucket, Key):
        assert Bucket == 'urban-strategy-data'
        self.calls.append(Key)
        if Key not in self.items:
            raise KeyError('Missing object')
        value = self.items[Key]
        return {'Body': io.BytesIO(value), 'ContentLength': len(value)}


class LandcoverR2Tests(unittest.TestCase):
    def test_missing_configuration_does_not_access_network(self):
        with patch.dict(os.environ, {'R2_ACCOUNT_ID': '', 'R2_ACCESS_KEY_ID': '', 'R2_SECRET_ACCESS_KEY': ''}):
            with patch.object(mod, '_client', side_effect=AssertionError('network not allowed')):
                r = mod.fetch_indexed_landcover(SITE)
        self.assertEqual(r['status'], 'MISSING_SOURCE')

    def test_private_index_selects_tile_and_caches_it(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            idx = _index(root / 'index-src.sqlite')
            tile = _tile(root / 'tile-src.sqlite')
            prefix = 'landcover_2025_indexed'
            client = FakeClient({f'{prefix}/tile_index.sqlite': idx,
                                 f'{prefix}/tiles/{CODE}.gpkg': tile})
            env = {'R2_ACCOUNT_ID': 'a'*32, 'R2_ACCESS_KEY_ID': 'fake',
                   'R2_SECRET_ACCESS_KEY': 'fake-secret', 'R2_BUCKET': 'urban-strategy-data',
                   'R2_LANDCOVER_PREFIX': prefix,
                   'LANDCOVER_CACHE_DIR': str(root/'cache')}
            with patch.dict(os.environ, env), patch.object(mod, '_client', return_value=client):
                result = mod.fetch_indexed_landcover(SITE)
                self.assertEqual(result['status'], 'DATA', result)
                self.assertEqual(result['polygon_count'], 1)
                self.assertEqual(result['features'][0]['properties']['L3_NAME'], '단독주거')
                self.assertEqual(len(client.calls), 2)
                result2 = mod.fetch_indexed_landcover(SITE)
                self.assertEqual(result2['status'], 'DATA')
                self.assertEqual(len(client.calls), 2, 'cached files should not be redownloaded')

    def test_invalid_remote_file_never_becomes_complete_fact(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            idx = _index(root / 'index-src.sqlite')
            prefix = 'landcover_2025_indexed'
            client = FakeClient({f'{prefix}/tile_index.sqlite': idx,
                                 f'{prefix}/tiles/{CODE}.gpkg': b'broken-data'})
            env = {'R2_ACCOUNT_ID': 'a'*32, 'R2_ACCESS_KEY_ID': 'fake',
                   'R2_SECRET_ACCESS_KEY': 'fake-secret', 'R2_BUCKET': 'urban-strategy-data',
                   'LANDCOVER_CACHE_DIR': str(root/'cache')}
            with patch.dict(os.environ, env), patch.object(mod, '_client', return_value=client):
                result = mod.fetch_indexed_landcover(SITE)
            self.assertEqual(result['status'], 'ERROR')
            self.assertNotIn('fake-secret', str(result))


if __name__ == '__main__':
    unittest.main()