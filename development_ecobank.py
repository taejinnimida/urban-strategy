"""Server-only EcoBank ecosystem/nature WFS collector for survey item 4-3.

The provider response is *reference spatial data*, not proof of the latest
legally notified ecosystem/nature designation. Never expose serviceKey, URL
with query parameters, or raw error responses to the browser.

Official guide: https://www.nie-ecobank.kr/opn/api/openApiSvcGuide.do
"""
import hashlib
import json
import math
import os
import re
from collections import defaultdict
from datetime import datetime
from urllib.parse import unquote
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import requests
from pyproj import Transformer
from shapely.geometry import Polygon, box, mapping, shape
from shapely.ops import transform, unary_union

# Fixed allowlisted provider and feature type; never accept URLs/layers from clients.
WFS_URL = 'https://apis.data.go.kr/B553084/ecoapi/EcologyzmpService/wfs/getEcologyzmpWFS'
WFS_LAYER = 'tbl_opn_eczm'
WFS_CRS = 'EPSG:5186'
MAX_PER_REQUEST = 500
MAX_CALLS = 28
MAX_DEPTH = 5
MAX_FEATURES = 6000
MAX_RESPONSE_BYTES = 8_000_000
MAX_INPUT_AREA_M2 = 10_000_000
SPLIT_AT_AREA_M2 = 1_000_000
SOURCE_GUIDE = 'https://www.nie-ecobank.kr/opn/api/openApiSvcGuide.do'
DEFAULT_GRADE_COLORS = {'1등급': '#d65c64', '2등급': '#e7ac59', '3등급': '#efe5a0',
                        '별도관리지역': '#b7c9e4', '등급 미확보': '#dedede'}
GRADE_CODES = {'1등급': 'C01', '2등급': 'C02', '3등급': 'C03',
               '별도관리지역': 'C04', '등급 미확보': 'C00'}


class EcoBankError(Exception):
    """Only a fixed error code may leave this source module."""
    def __init__(self, code='PROVIDER_FORMAT', http_status=None):
        self.code = code if code in (
            'PROVIDER_FORMAT', 'PROVIDER_REJECTED', 'RATE_LIMIT',
            'PROVIDER_HTTP_ERROR', 'VALIDATION_FAILED',
            'CONNECT_TIMEOUT', 'READ_TIMEOUT', 'CONNECTION_ERROR',
        ) else 'PROVIDER_FORMAT'
        self.http_status = http_status if isinstance(http_status, int) else None
        super().__init__(self.code)


def _local(tag):
    return str(tag).rsplit('}', 1)[-1].rsplit(':', 1)[-1].lower()


def _children(node, *tags):
    wanted = {x.lower() for x in tags}
    return [v for v in node if _local(v.tag) in wanted]


def _first_child(node, *tags):
    return next(iter(_children(node, *tags)), None)


def _coord_pairs(node):
    tag = _local(node.tag)
    text = (node.text or '').strip()
    if not text:
        raise EcoBankError('PROVIDER_FORMAT')
    try:
        if tag == 'coordinates':
            cs = node.attrib.get('cs', ',')
            ts = node.attrib.get('ts', ' ')
            parts = text.replace(ts, ' ').split()
            pairs = [tuple(float(z) for z in p.split(cs)[:2]) for p in parts]
        elif tag == 'poslist':
            dimension = int(node.attrib.get('srsDimension', node.attrib.get('dimension', '2')))
            if dimension != 2:
                raise EcoBankError('PROVIDER_FORMAT')
            vals = [float(z) for z in text.split()]
            if len(vals) % 2:
                raise EcoBankError('PROVIDER_FORMAT')
            pairs = list(zip(vals[::2], vals[1::2]))
        elif tag == 'pos':
            vals = [float(z) for z in text.split()]
            pairs = [tuple(vals)]
        else:
            raise EcoBankError('PROVIDER_FORMAT')
        if not pairs or any(len(p) != 2 or not all(math.isfinite(x) for x in p) for p in pairs):
            raise EcoBankError('PROVIDER_FORMAT')
    except (ValueError, OverflowError, TypeError) as exc:
        raise EcoBankError('PROVIDER_FORMAT') from None
    return pairs


def _ring(boundary):
    linear = next((x for x in boundary.iter() if _local(x.tag) == 'linearring'), None)
    if linear is None:
        raise EcoBankError('PROVIDER_FORMAT')
    pair_list = next((x for x in linear.iter() if _local(x.tag) in ('coordinates', 'poslist')), None)
    if pair_list is not None:
        coords = _coord_pairs(pair_list)
    else:
        coords = [p for node in linear.iter() if _local(node.tag) == 'pos' for p in _coord_pairs(node)]
    if len(coords) < 4 or coords[0] != coords[-1]:
        raise EcoBankError('PROVIDER_FORMAT')
    return coords


def _gml_polygons(feature):
    polygons = []
    for p in feature.iter():
        if _local(p.tag) != 'polygon':
            continue
        exterior = _first_child(p, 'exterior', 'outerBoundaryIs')
        if exterior is None:
            raise EcoBankError('PROVIDER_FORMAT')
        shell = _ring(exterior)
        holes = [_ring(n) for n in _children(p, 'interior', 'innerBoundaryIs')]
        poly = Polygon(shell, holes)
        if poly.is_empty or not poly.is_valid or poly.area <= 0:
            raise EcoBankError('PROVIDER_FORMAT')
        polygons.append(poly)
    if not polygons:
        raise EcoBankError('PROVIDER_FORMAT')
    geom = unary_union(polygons)
    if geom.is_empty or not geom.is_valid:
        raise EcoBankError('PROVIDER_FORMAT')
    return geom


def _grade(props):
    norm = {re.sub(r'[^a-z0-9]', '', str(k).lower()): str(v).strip()
            for k, v in (props or {}).items() if v is not None}
    value = next((norm[k] for k in ('eczmgr', 'eczmgrade') if k in norm), None)
    if value in ('1', '1.0', '1등급'):
        return '1등급'
    if value in ('2', '2.0', '2등급'):
        return '2등급'
    if value in ('3', '3.0', '3등급'):
        return '3등급'
    # A special management class is only accepted when explicitly named.
    if value in ('별도관리지역', '별도관리'):
        return '별도관리지역'
    return '등급 미확보'


def _gml_features(payload):
    if not payload or b'<!DOCTYPE' in payload.upper() or b'<!ENTITY' in payload.upper():
        raise EcoBankError('PROVIDER_FORMAT')
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        raise EcoBankError('PROVIDER_FORMAT') from None
    if any(_local(n.tag) in ('serviceexceptionreport', 'exceptionreport', 'errorresponse', 'response')
           for n in (root,)):
        raise EcoBankError('PROVIDER_REJECTED')
    if _local(root.tag) != 'featurecollection':
        raise EcoBankError('PROVIDER_FORMAT')
    # GML2 gml:featureMember, GML3 wfs:member, and gml:featureMembers.
    records = []
    for member in root.iter():
        if _local(member.tag) in ('featuremember', 'member', 'featuremembers'):
            records.extend(n for n in member if _local(n.tag) == WFS_LAYER)
    if not records:
        records = [n for n in root.iter() if _local(n.tag) == WFS_LAYER]
    out = []
    for record in records:
        attr = {}
        for field in record:
            if any(_local(x.tag) in ('polygon', 'multipolygon', 'multisurface') for x in field.iter()):
                continue
            if len(field) == 0:
                attr[_local(field.tag)] = (field.text or '').strip()[:120]
        fid = next((v for k, v in record.attrib.items() if _local(k) in ('id', 'fid')), '')
        out.append((str(fid), _gml_polygons(record), attr))
    return out


def _json_features(payload):
    try:
        obj = json.loads(payload)
    except (ValueError, UnicodeError):
        raise EcoBankError('PROVIDER_FORMAT') from None
    if not isinstance(obj, dict) or obj.get('type') != 'FeatureCollection' or not isinstance(obj.get('features'), list):
        raise EcoBankError('PROVIDER_FORMAT')
    crs = (obj.get('crs') or {}).get('properties', {}).get('name', WFS_CRS)
    if crs not in (WFS_CRS, 'urn:ogc:def:crs:EPSG::5186'):
        raise EcoBankError('PROVIDER_FORMAT')
    result = []
    for item in obj['features']:
        try:
            g = shape(item['geometry'])
            if g.geom_type not in ('Polygon', 'MultiPolygon') or g.is_empty or not g.is_valid:
                raise ValueError()
            result.append((str(item.get('id') or ''), g, item.get('properties') or {}))
        except (KeyError, ValueError, TypeError):
            raise EcoBankError('PROVIDER_FORMAT') from None
    return result


def _parse(payload):
    return _json_features(payload) if payload.lstrip().startswith(b'{') else _gml_features(payload)


def _tiles(tile):
    x0, y0, x1, y1 = tile.bounds
    xm, ym = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    return [box(a, b, c, d) for a, b, c, d in (
        (x0, y0, xm, ym), (xm, y0, x1, ym),
        (x0, ym, xm, y1), (xm, ym, x1, y1))]


def _fetch(tile, key, get):
    a, b, c, d = tile.bounds
    params = {'serviceKey': unquote(key), 'typeName': WFS_LAYER,
              'bbox': f'{a:.3f},{b:.3f},{c:.3f},{d:.3f},{WFS_CRS}',
              'maxFeatures': MAX_PER_REQUEST}
    try:
        with get(WFS_URL, params=params, timeout=(4, 16), stream=True, allow_redirects=False) as response:
            status = response.status_code
            if status == 429:
                raise EcoBankError('RATE_LIMIT', 429)
            if status in (401, 403):
                raise EcoBankError('PROVIDER_REJECTED', status)
            if 300 <= status < 400:
                raise EcoBankError('PROVIDER_REJECTED', status)
            if status != 200:
                raise EcoBankError('PROVIDER_HTTP_ERROR', status)
            chunks, size = [], 0
            for chunk in response.iter_content(chunk_size=65536):
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise EcoBankError('PROVIDER_FORMAT')
                chunks.append(chunk)
            return _parse(b''.join(chunks))
    except requests.ConnectTimeout:
        raise EcoBankError('CONNECT_TIMEOUT') from None
    except requests.ReadTimeout:
        raise EcoBankError('READ_TIMEOUT') from None
    except requests.RequestException:
        raise EcoBankError('CONNECTION_ERROR') from None


def collect_ecobank(site_geojson, api_key=None, get=None):
    """Fetch all intersecting features or fail closed; no partial tile success.

    Empty WFS results and unclassified attributes are never converted into
    'no ecological constraint' or grade 3. Does not make legal eligibility calls.
    """
    key = (api_key if api_key is not None else os.getenv('ECOBANK_API_KEY') or '').strip()
    if not key:
        return {'status': 'MISSING_CREDENTIAL', 'detail': 'EcoBank 인증키 환경변수 등록 필요',
                'source': '국립생태원 EcoBank', 'features': []}
    site = shape(site_geojson)
    if site.geom_type not in ('Polygon', 'MultiPolygon') or site.is_empty or not site.is_valid:
        raise EcoBankError('VALIDATION_FAILED')
    to_api = Transformer.from_crs('EPSG:4326', WFS_CRS, always_xy=True).transform
    to_wgs = Transformer.from_crs(WFS_CRS, 'EPSG:4326', always_xy=True).transform
    site_xy = transform(to_api, site)
    if not site_xy.is_valid or site_xy.area > MAX_INPUT_AREA_M2:
        raise EcoBankError('VALIDATION_FAILED')
    calls = 0
    unique = {}
    getter = get or requests.get

    def scan(tile, depth):
        nonlocal calls
        if tile.area > SPLIT_AT_AREA_M2:
            if depth >= MAX_DEPTH:
                raise EcoBankError('VALIDATION_FAILED')
            for child in _tiles(tile):
                scan(child, depth + 1)
            return
        if calls >= MAX_CALLS:
            raise EcoBankError('PROVIDER_FORMAT')
        calls += 1
        records = _fetch(tile, key, getter)
        if len(records) >= MAX_PER_REQUEST:
            # A full page could be truncated: subdivide rather than silently
            # interpreting provider's maxFeatures=500 as complete coverage.
            if depth >= MAX_DEPTH or min(tile.bounds[2]-tile.bounds[0], tile.bounds[3]-tile.bounds[1]) < 10:
                raise EcoBankError('PROVIDER_FORMAT')
            for child in _tiles(tile):
                scan(child, depth + 1)
            return
        for fid, geom, props in records:
            if geom.is_empty or not geom.is_valid or not geom.intersects(site_xy):
                continue
            # Include all geometry revisions with different coordinates, while
            # collapsing responses repeated at subquery tile boundaries.
            digest = hashlib.sha256(geom.wkb).hexdigest()
            unique[(fid, digest)] = (geom, props)
            if len(unique) > MAX_FEATURES:
                raise EcoBankError('PROVIDER_FORMAT')

    scan(box(*site_xy.bounds), 0)
    groups = defaultdict(list)
    features = []
    missing = 0
    for (fid, _), (geom, props) in sorted(unique.items()):
        clipped_xy = geom.intersection(site_xy)
        if clipped_xy.is_empty or clipped_xy.area <= 1e-8:
            continue
        grade = _grade(props)
        if grade == '등급 미확보':
            missing += 1
        groups[grade].append(clipped_xy)
        geo = transform(to_wgs, clipped_xy)
        features.append({'type': 'Feature', 'geometry': mapping(geo),
                         'properties': {'name': grade, 'grade': grade,
                                        'class_code': GRADE_CODES[grade],
                                        'color': DEFAULT_GRADE_COLORS[grade],
                                        'ecobank_id': fid[:100]}})
    union_xy = unary_union([geom for batch in groups.values() for geom in batch]) if groups else None
    area_all = round(union_xy.area, 2) if union_xy is not None else 0.0
    legend = []
    for grade in ('1등급', '2등급', '3등급', '별도관리지역', '등급 미확보'):
        geoms = groups.get(grade)
        if geoms:
            legend.append({'name': grade, 'class_code': GRADE_CODES[grade],
                           'color': DEFAULT_GRADE_COLORS[grade], 'count': len(geoms),
                           'area_m2': round(unary_union(geoms).area, 2)})
    sum_areas = sum(v['area_m2'] for v in legend)
    overlap = max(0.0, sum_areas - area_all)
    status = ('EMPTY' if not features else
              'PARTIAL' if missing or overlap > max(1.0, area_all * 0.001) else 'DATA')
    note = ('WFS 수신 0건 · 도면 미확보 또는 비해당 확정 불가' if status == 'EMPTY'
            else f'WFS 대상지 교차 {len(features)}건 · 등급 미확보 {missing}건 · 교차 중복면적 {overlap:.2f}㎡')
    return {'status': status, 'source': '국립생태원 EcoBank',
            'source_url': SOURCE_GUIDE, 'source_layer': WFS_LAYER, 'source_crs': WFS_CRS,
            'scope': 'fixed_site', 'detail': note + ' · 최신 고시 원본과 일치 여부 별도 확인',
            'features': features, 'legend': legend,
            'overlap_area_m2': area_all, 'site_area_m2_epsg5186': round(site_xy.area, 2),
            'unclassified_count': missing, 'overlap_warning_m2': round(overlap, 2),
            'provider_calls': calls, 'as_of': None,
            'queried_at': datetime.now(ZoneInfo('Asia/Seoul')).isoformat(timespec='seconds')}