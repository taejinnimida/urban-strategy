"""Private Cloudflare R2 adapter for indexed official 2025 land-cover GeoPackages.

The web browser never receives R2 credentials or object links. Only the fixed
bucket/prefix and spatially selected tile objects are read. The downloaded
GeoPackage cache is ephemeral; the original R2 objects stay unchanged.
"""
from __future__ import annotations

import os
import re
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from shapely.geometry import shape

SOURCE = 'https://aid.mcee.go.kr/'
DEFAULT_BUCKET = 'urban-strategy-data'
DEFAULT_PREFIX = 'landcover_2025_indexed'
MAX_INDEX_BYTES = 2_000_000
MAX_TILE_BYTES = 80_000_000
MAX_REQUEST_BYTES = 180_000_000
MAX_TILES = 12


def _settings():
    keys = ('R2_ACCOUNT_ID', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY')
    values = [os.environ.get(k, '').strip() for k in keys]
    if not all(values):
        return None
    account, access, secret = values
    if not re.fullmatch(r'[0-9a-fA-F]{32}', account):
        raise ValueError('Invalid R2 account identifier')
    bucket = os.environ.get('R2_BUCKET', DEFAULT_BUCKET).strip()
    prefix = os.environ.get('R2_LANDCOVER_PREFIX', DEFAULT_PREFIX).strip().strip('/')
    if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', bucket):
        raise ValueError('Invalid R2 bucket name')
    if not re.fullmatch(r'[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*', prefix):
        raise ValueError('Invalid R2 prefix')
    return account, access, secret, bucket, prefix


def _client(settings):
    import boto3
    from botocore.config import Config
    account, access, secret, _, _ = settings
    return boto3.client(
        's3',
        endpoint_url=f'https://{account}.r2.cloudflarestorage.com',
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        region_name='auto',
        config=Config(signature_version='s3v4', connect_timeout=4,
                      read_timeout=18, retries={'max_attempts': 2},
                      s3={'addressing_style': 'path'}),
    )


def _download(client, bucket, key, destination, limit):
    """Bound object sizes and use atomic installation to prevent corrupt cache hits."""
    response = client.get_object(Bucket=bucket, Key=key)
    body = response['Body']
    try:
        advertised = response.get('ContentLength')
        if advertised is not None and (advertised <= 0 or advertised > limit):
            raise ValueError('Object exceeds download limit')
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.r2-', delete=False) as fp:
            temp_path = Path(fp.name)
            size = 0
            try:
                while True:
                    chunk = body.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise ValueError('Object exceeds download limit')
                    fp.write(chunk)
                if not size or (advertised is not None and size != advertised):
                    raise ValueError('Incomplete R2 object')
            except Exception:
                temp_path.unlink(missing_ok=True)
                raise
        if not _valid_sqlite(temp_path):
            temp_path.unlink(missing_ok=True)
            raise ValueError('Invalid SQLite data object')
        os.replace(temp_path, destination)
        return size
    finally:
        body.close()


def _valid_sqlite(path):
    try:
        with open(path, 'rb') as fp:
            if fp.read(16) != b'SQLite format 3\x00':
                return False
        with closing(sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True)) as conn:
            return conn.execute('PRAGMA quick_check(1)').fetchone() == ('ok',)
    except (OSError, sqlite3.DatabaseError):
        return False


def _cache_root():
    return Path(os.environ.get('LANDCOVER_CACHE_DIR', '/tmp/urban-landcover-indexed')).resolve()


def _ensure_index(client, bucket, prefix, root):
    index = root / 'tile_index.sqlite'
    if not _valid_sqlite(index):
        index.unlink(missing_ok=True)
        _download(client, bucket, f'{prefix}/tile_index.sqlite', index, MAX_INDEX_BYTES)
    with closing(sqlite3.connect(index)) as conn:
        conn.execute('SELECT tile_code,file,feature_count,source_crs_wkt FROM tiles LIMIT 1')
        conn.execute('SELECT id,min_lon,max_lon,min_lat,max_lat FROM tile_rtree LIMIT 1')
    return index


def _download_tiles(client, bucket, prefix, root, selected):
    if len(selected) > MAX_TILES:
        raise ValueError('Too many tiles in one request')
    budget = 0
    for code, relpath, count, crs in selected:
        if not re.fullmatch(r'\d{8}', str(code)) or relpath != f'tiles/{code}.gpkg':
            raise ValueError('Unsafe or mismatched tile path')
        if not isinstance(count, int) or count < 0:
            raise ValueError('Invalid tile count')
        dest = root / relpath
        if not _valid_sqlite(dest):
            dest.unlink(missing_ok=True)
            _download(client, bucket, f'{prefix}/{relpath}', dest, MAX_TILE_BYTES)
        budget += dest.stat().st_size
        if budget > MAX_REQUEST_BYTES:
            raise ValueError('Selected tiles exceed request budget')
        with closing(sqlite3.connect(f'file:{dest.as_posix()}?mode=ro', uri=True)) as conn:
            cols = {row[1] for row in conn.execute('PRAGMA table_info(landcover)')}
            if not {'geom', 'L1_CODE', 'L1_NAME', 'L2_CODE', 'L2_NAME', 'L3_CODE', 'L3_NAME'}.issubset(cols):
                raise ValueError('Missing land-cover class columns')
            if conn.execute('SELECT COUNT(*) FROM landcover').fetchone()[0] != count:
                raise ValueError('R2 tile count mismatches tile index')
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='rtree_landcover_geom'").fetchone() is None:
                raise ValueError('Missing RTree spatial index')


def fetch_indexed_landcover(geometry):
    """Return original site-clipped class features to the established 2-6 FACT path.

    No R2 configuration -> MISSING_SOURCE, leaving existing local-file/WMS
    providers available. Errors disclose neither keys nor internal endpoints.
    """
    settings = None
    try:
        settings = _settings()
        if settings is None:
            return {'status': 'MISSING_SOURCE', 'source': 'R2 공식 등록 원자료',
                    'detail': '비공개 공간자료 저장소 연결 미설정'}
        target = shape(geometry)
        if not target.is_valid or target.is_empty or target.geom_type not in ('Polygon', 'MultiPolygon'):
            raise ValueError('Invalid site')
        from query_landcover_gpkg import query, select_tiles
        root = _cache_root()
        root.mkdir(parents=True, exist_ok=True)
        account, access, secret, bucket, prefix = settings
        client = _client(settings)
        _ensure_index(client, bucket, prefix, root)
        selected = select_tiles(root, target)
        if not selected:
            return {'status': 'OUTSIDE_COVERAGE', 'source': '환경공간정보서비스 2025 세분류 SHP',
                    'source_url': SOURCE, 'detail': '등록된 도엽 범위 밖 · 토지피복 비해당을 의미하지 않음'}
        _download_tiles(client, bucket, prefix, root, selected)
        result = query(root, geometry, max_features=5000, include_features=True)
        complete = result['status'] == 'COMPLETE_GEOMETRY'
        groups = result.get('groups', {})
        return {'status': 'DATA' if complete else 'PARTIAL',
                'source': '환경공간정보서비스 2025 세분류 토지피복지도 SHP / 비공개 R2 보관',
                'source_url': SOURCE, 'as_of': '2025',
                'features': result.get('features', []),
                'groups': groups,
                'tile_count': len(selected),
                'polygon_count': result.get('polygon_count', 0),
                'repaired_count': result.get('source_geometries_repaired', 0),
                'detail': f"공간색인 도엽 {len(selected)}개 · 교차도형 {result.get('polygon_count', 0)}개 · "
                          f"피복률 {result.get('coverage_percent', 0):.2f}% · "
                          f"원본 도형 보정 {result.get('source_geometries_repaired', 0)}건 · "
                          + ('분류·피복범위 검증 대상' if complete else '공간도형 일부 검증 필요')}
    except Exception:
        # Keep secrets, account IDs, bucket names and network error bodies out of
        # API responses and logs. Retain a fixed exception class for diagnostics.
        return {'status': 'ERROR', 'error_code': 'R2_LANDCOVER_UNAVAILABLE',
                'source': 'R2 공식 등록 원자료',
                'detail': '등록된 토지피복 공간자료를 검증 또는 조회하지 못했습니다. 기존 WMS 참고도는 유지됩니다.'}