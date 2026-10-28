"""Versioned official UQ153 snapshot. No name-based deletion or legal inference."""
import copy
import hashlib
import json
import threading
from pathlib import Path
import shapefile
from pyproj import CRS, Transformer
from shapely.geometry import shape, mapping
from shapely.ops import transform
from shapely.strtree import STRtree

DATA_DIR = Path(__file__).parent / 'data' / 'seoul_uq153_202609'
LAYER = 'LT_C_UPISUQ153'
_lock = threading.Lock()
_snapshot = None

def load_snapshot():
    global _snapshot
    with _lock:
        if _snapshot is not None:
            return _snapshot
        root = DATA_DIR
        meta = json.loads((root / 'manifest.json').read_text())
        for suffix in ('.shp', '.shx', '.dbf', '.prj'):
            name = 'UPIS_C_UQ153' + suffix
            if hashlib.sha256((root / name).read_bytes()).hexdigest() != meta['files'][name]:
                raise ValueError('snapshot checksum mismatch')
        crs = CRS.from_wkt((root / 'UPIS_C_UQ153.prj').read_text())
        to_geo = Transformer.from_crs(crs, 4326, always_xy=True)
        geometries, features = [], []
        with shapefile.Reader(str(root / 'UPIS_C_UQ153.shp'), encoding='cp949') as reader:
            if len(reader) != meta['record_count']:
                raise ValueError('incomplete snapshot')
            for idx, sr in enumerate(reader.iterShapeRecords()):
                geom = shape(sr.shape.__geo_interface__)
                repaired = not geom.is_valid
                if repaired:
                    geom = geom.buffer(0)
                if geom.is_empty or not geom.is_valid or geom.geom_type not in ('Polygon', 'MultiPolygon'):
                    raise ValueError('unusable polygon')
                geom = transform(to_geo.transform, geom)
                if not geom.is_valid:
                    geom = geom.buffer(0)
                    repaired = True
                if geom.is_empty or not geom.is_valid:
                    raise ValueError('invalid transformed polygon')
                x1,y1,x2,y2 = geom.bounds
                if not (125 < x1 <= x2 < 128 and 36 < y1 <= y2 < 39):
                    raise ValueError('unexpected coordinate reference system')
                props = {k: v.isoformat() if hasattr(v, 'isoformat') else v for k,v in sr.record.as_dict().items()}
                props.update(_source=meta['source'], _source_date=meta['source_date'], _source_dataset=meta['dataset'], _geometry_repaired=repaired)
                features.append({'type':'Feature', 'id':f"seoul-uq153-{idx}-{props['PRESENT_SN']}", 'geometry':mapping(geom), 'properties':props})
                geometries.append(geom)
        _snapshot = (meta, features, geometries, STRtree(geometries))
        return _snapshot

def query(geometry):
    target = shape(geometry)
    if target.is_empty or target.geom_type not in ('Polygon','MultiPolygon'):
        raise ValueError('invalid target')
    if not target.is_valid:
        target = target.buffer(0)
    if target.is_empty or not target.is_valid:
        raise ValueError('invalid target')
    meta, features, geometries, tree = load_snapshot()
    # No joins with fallback rows: a complete zero-result snapshot is authoritative.
    indices = sorted(int(i) for i in tree.query(target, predicate='intersects'))
    return [copy.deepcopy(features[i]) for i in indices]
