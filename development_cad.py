"""Server-side CAD outputs from the nationwide development review's frozen FACTs.

Only confirmed geometry and provider-verified or registered official source layers
are exported. This is a preliminary survey drawing, not an approved cadastral map.
"""
import copy
import hashlib
import io
import json
import math
import re
import zipfile
from datetime import datetime
from zoneinfo import ZoneInfo

import ezdxf
from fastapi import HTTPException
from fastapi.responses import Response
from pyproj import Transformer
from shapely.geometry import box, mapping, shape
from shapely.ops import transform, unary_union
from ezdxf.enums import TextEntityAlignment

CRS = 'EPSG:5179'
FRAME_MULTIPLIER = 2.0  # Double projected bbox width and height; drawing footprint = 4x bbox.
MAX_FEATURES = 6000
MAX_TILE_CALLS = 64  # bounded calls on the shared, rate-limited VWorld channel
MAX_TILE_AREA_M2 = 4_000_000  # each query well below the 10 sq km request guard
MAX_TILE_DEPTH = 4
TILE_OVERLAP_M = 2  # avoid seams from inverse projection and provider geometry tolerances
CAD_CACHE_VERSION = 2
MAX_DXF_BYTES = 18_000_000
MAX_ZIP_BYTES = 28_000_000
NOTICE = ('기초조사 참고 도면. 원자료 최신성·수록범위·법정 고시도면과의 일치 및 '
          '관할청 제출규격은 별도로 검증해야 합니다. EPSG:5179(미터)로 변환된 벡터입니다.')


def _exportable(item, snapshot):
    if item['id'] == '1-1':
        return True
    # A successfully queried browser fallback is not an official CAD source.
    if item.get('provider_verified') is True and item.get('status') in ('공간자료 확보', '일부 확보'):
        return True
    official = snapshot.get('_national_sources', {}).get('file:' + item['id'], {})
    if official.get('status') in ('DATA', 'PARTIAL') and item.get('features'):
        return True
    return False


def _entities(model, geom, layer, counts):
    """Add actual ground-coordinate primitives; never generate a raster-shaped DXF."""
    if geom.is_empty:
        return
    kind = geom.geom_type
    if kind == 'Polygon':
        for ring in [geom.exterior, *geom.interiors]:
            pts = [(float(x), float(y)) for x, y, *_ in list(ring.coords)[:-1]]
            if len(pts) >= 3:
                model.add_lwpolyline(pts, close=True, dxfattribs={'layer': layer})
                counts['polylines'] += 1
    elif kind == 'LineString' or kind == 'LinearRing':
        pts = [(float(x), float(y)) for x, y, *_ in geom.coords]
        if len(pts) >= 2:
            model.add_lwpolyline(pts, dxfattribs={'layer': layer})
            counts['polylines'] += 1
    elif kind == 'Point':
        model.add_point((float(geom.x), float(geom.y)), dxfattribs={'layer': layer})
        counts['points'] += 1
    elif hasattr(geom, 'geoms'):
        for child in geom.geoms:
            _entities(model, child, layer, counts)


def _drawing_frame(site):
    """Expanded survey frame in projected metres, independent from review geometry."""
    project = Transformer.from_crs('EPSG:4326', CRS, always_xy=True).transform
    projected = transform(project, site)
    xmin, ymin, xmax, ymax = projected.bounds
    cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
    half_w, half_h = (xmax - xmin) * FRAME_MULTIPLIER / 2, (ymax - ymin) * FRAME_MULTIPLIER / 2
    if min(half_w, half_h) <= 0:
        raise HTTPException(422, '도곽 설정을 위한 구역계 너비·높이가 필요합니다.')
    return box(cx - half_w, cy - half_h, cx + half_w, cy + half_h)


def _four_tiles(tile):
    x0, y0, x1, y1 = tile.bounds
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    return [box(a, b, c, d) for a, b, c, d in (
        (x0, y0, mx, my), (mx, y0, x1, my),
        (x0, my, mx, y1), (mx, my, x1, y1))]


def _collect_surrounding_cadastre(frame, fetch_layer, layer_type):
    """Query the entire 2x drawing frame using bounded, overlapping subareas.

    A complete provider response means the request pages completed, not legal cadastral
    coverage certification. Tiles with provider errors are never silently omitted.
    """
    inverse = Transformer.from_crs(CRS, 'EPSG:4326', always_xy=True).transform
    project = Transformer.from_crs('EPSG:4326', CRS, always_xy=True).transform
    found = {}  # (provider identity, geometry hash) -> original feature
    count = {'requests': 0, 'tiles_complete': 0, 'tiles_empty': 0, 'duplicate_features': 0}

    def get_tile(tile, depth):
        if tile.area > MAX_TILE_AREA_M2:
            if depth >= MAX_TILE_DEPTH:
                raise HTTPException(413, '도곽이 넓어 안전한 분할조회 한도를 초과합니다. 도면 범위를 줄이세요.')
            for part in _four_tiles(tile):
                get_tile(part, depth + 1)
            return
        if count['requests'] >= MAX_TILE_CALLS:
            raise HTTPException(413, '주변 지적 수집의 API 요청 한도에 도달했습니다. 도면 범위를 줄이세요.')
        count['requests'] += 1
        # Expansion is only for the provider request; CAD output is clipped to frame.
        query = transform(inverse, tile.buffer(TILE_OVERLAP_M))
        try:
            result = fetch_layer(layer_type(geometry=mapping(query), layer='LP_PA_CBND_BUBUN'))
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, '주변 지적 서버 호출 중 오류가 발생했습니다.') from None
        if result.get('status') != 'COMPLETE':
            code = str(result.get('error_code') or 'REQUEST_FAILED')
            # Provider page cap can be resolved by smaller requests; transport/auth errors cannot.
            if code in ('PROVIDER_FORMAT', 'INVALID_SIZE', 'INVALID_RANGE') and depth < MAX_TILE_DEPTH:
                for part in _four_tiles(tile):
                    get_tile(part, depth + 1)
                return
            raise HTTPException(503, '주변 지적 분할조회 실패: '+code)
        items = result.get('features')
        if not isinstance(items, list):
            raise HTTPException(503, '주변 지적 서버 응답 형식이 잘못되었습니다.')
        count['tiles_complete'] += 1
        if not items:
            count['tiles_empty'] += 1
        for feature in items:
            if not isinstance(feature, dict) or not feature.get('geometry'):
                raise HTTPException(422, '주변 지적 원자료에 유효하지 않은 도형이 포함되어 있습니다.')
            try:
                geom = shape(feature['geometry'])
                if geom.is_empty or not geom.is_valid:
                    raise ValueError()
                if not transform(project, geom).intersects(frame):
                    continue
            except (ValueError, TypeError, KeyError):
                raise HTTPException(422, '주변 지적 원자료의 도형 검증에 실패했습니다.') from None
            props = feature.get('properties') or {}
            identity = str(props.get('pnu') or props.get('PNU') or feature.get('id') or '')
            digest = hashlib.sha256(json.dumps(feature['geometry'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            key = (identity, digest)
            if key in found:
                count['duplicate_features'] += 1
                continue
            found[key] = feature
            if len(found) > MAX_FEATURES:
                raise HTTPException(413, '도곽 주변 필지가 CAD 출력 상한을 초과합니다. 도면 범위를 줄이세요.')

    # Always split the drawing frame into four tiles: avoid one large opaque request.
    for part in _four_tiles(frame):
        get_tile(part, 1)
    if not found:
        raise HTTPException(503, '확장 도곽의 연속지적 원자료를 확보하지 못했습니다.')
    return {'status': 'COMPLETE', 'features': list(found.values()),
            'request_count': count['requests'], 'tile_count': count['tiles_complete'],
            'empty_tile_count': count['tiles_empty'], 'duplicates_removed': count['duplicate_features'],
            'coverage_assurance': 'PROVIDER_RESPONSE_ONLY',
            'cache_version': CAD_CACHE_VERSION}


def dxf_bytes(item, snapshot, metadata):
    site = shape(snapshot['geometry'])
    if site.is_empty or not site.is_valid:
        raise HTTPException(422, '확정 구역계 도형이 유효하지 않습니다.')
    convert = Transformer.from_crs('EPSG:4326', CRS, always_xy=True).transform
    projected = transform(convert, site)
    frame = _drawing_frame(site)
    doc = ezdxf.new('R2010')
    doc.header['$INSUNITS'] = 6  # metres
    doc.header['$MEASUREMENT'] = 1
    doc.header['$LUPREC'] = 3
    doc.layers.new('DRAWING_FRAME', dxfattribs={'color': 9, 'lineweight': 25})
    doc.layers.new('SITE_BOUNDARY', dxfattribs={'color': 1, 'lineweight': 50})
    doc.layers.new('CADASTRE', dxfattribs={'color': 8, 'lineweight': 13})
    doc.layers.new('PARCEL_LABEL', dxfattribs={'color': 7, 'lineweight': 13})
    doc.styles.new('KOREAN', dxfattribs={'font': 'malgun.ttf'})
    theme_layer = 'SURVEY_' + item['id'].replace('-', '_')
    doc.layers.new(theme_layer, dxfattribs={'color': 5, 'lineweight': 25})
    model = doc.modelspace()
    counts = {'polylines': 0, 'points': 0, 'parcels': 0, 'thematic_features': 0}
    _entities(model, frame, 'DRAWING_FRAME', counts)
    _entities(model, projected, 'SITE_BOUNDARY', counts)
    parcels = snapshot.get('_cad_background', {})
    if parcels.get('status') != 'COMPLETE':
        raise HTTPException(503, '확장 도곽의 주변 연속지적 서버조회가 완료되지 않아 불완전한 DXF를 생성하지 않습니다.')
    # Prepare all clipped cadastral polygons first. A topologically united boundary
    # avoids drawing every shared parcel edge twice in CAD.
    source = parcels.get('features') or []
    if len(source) > MAX_FEATURES:
        raise HTTPException(413, '지적도형이 CAD 출력 한도를 초과합니다.')
    clipped_parcels = []
    for feature in source:
        try:
            geom = shape(feature['geometry'])
            if not geom.is_valid or geom.is_empty:
                raise ValueError('invalid parcel geometry')
            clipped = transform(convert, geom).intersection(frame)
            if clipped.is_empty or clipped.area <= 1e-8:
                continue
            clipped_parcels.append((clipped, feature.get('properties') or {}))
        except (ValueError, TypeError, KeyError):
            raise HTTPException(422, '지적 원자료의 CAD 도형 변환에 실패했습니다.') from None
    if not clipped_parcels:
        raise HTTPException(503, '확장 도곽 내의 지적이 없어 CAD 도면을 생성하지 않습니다.')
    try:
        cadastral_boundary = unary_union([geom.boundary for geom, _ in clipped_parcels])
        _entities(model, cadastral_boundary, 'CADASTRE', counts)
    except (ValueError, TypeError):
        raise HTTPException(422, '지적 경계 중복 정리 중 공간 위상 오류가 발생했습니다.') from None
    counts['parcels'] = len(clipped_parcels)
    counts['labels'] = 0
    counts['labels_omitted'] = 0
    # Text sizes reflect the drawing frame and actual interior space of each parcel.
    # Labels too small to read or colliding with earlier labels are intentionally omitted.
    frame_short = min(frame.bounds[2] - frame.bounds[0], frame.bounds[3] - frame.bounds[1])
    target_height = min(6.0, max(0.8, 0.013 * frame_short))
    occupied = []
    site_border = projected.boundary
    for geom, props in clipped_parcels:
        raw_label = props.get('jibun') or props.get('JIBUN')
        if not raw_label:
            continue
        label = str(raw_label)[:45].strip()
        if not label:
            continue
        try:
            polygon_parts = ([geom] if geom.geom_type == 'Polygon' else
                             [g for g in getattr(geom, 'geoms', ()) if g.geom_type == 'Polygon'])
            if not polygon_parts:
                counts['labels_omitted'] += 1
                continue
            part = max(polygon_parts, key=lambda g: g.area)
            point = part.representative_point()
            clearance = point.distance(part.boundary)
            # Approximate glyph widths: Korean chars ~1 em, Arabic digits ~0.7 em.
            ems = sum(1.0 if ord(ch) > 127 else 0.68 for ch in label)
            required_radius = math.hypot(ems / 2, 0.55)
            font_height = min(target_height, 1.65 * clearance / max(required_radius, 0.7))
            if font_height < max(0.65, target_height * 0.35):
                counts['labels_omitted'] += 1
                continue
            half_w = ems * font_height / 2 * 1.06
            half_h = font_height * 0.62
            padding = 0.2 * font_height
            selected = None
            # Move text away from the highlighted site boundary when the parcel
            # has room. The central position is preferred if already clear.
            shifts = ((0.0, 0.0), (0.0, 1.7), (0.0, -1.7),
                      (1.3, 0.0), (-1.3, 0.0),
                      (1.0, 1.5), (-1.0, 1.5), (1.0, -1.5), (-1.0, -1.5))
            for sx, sy in shifts:
                x, y = point.x + sx * font_height, point.y + sy * font_height
                label_area = box(x - half_w, y - half_h, x + half_w, y + half_h)
                if not part.covers(label_area):
                    continue
                if label_area.distance(site_border) < max(0.5, font_height * 0.35):
                    continue
                rect = (x - half_w - padding, y - half_h - padding,
                        x + half_w + padding, y + half_h + padding)
                if any(rect[0] < other[2] and rect[2] > other[0] and
                       rect[1] < other[3] and rect[3] > other[1] for other in occupied):
                    continue
                selected = (x, y, rect)
                break
            if selected is None:
                counts['labels_omitted'] += 1
                continue
            x, y, rect = selected
            model.add_text(label, dxfattribs={
                'layer': 'PARCEL_LABEL', 'style': 'KOREAN', 'height': round(font_height, 3)
            }).set_placement((float(x), float(y)), align=TextEntityAlignment.MIDDLE_CENTER)
            occupied.append(rect)
            counts['labels'] += 1
        except (ValueError, TypeError):
            counts['labels_omitted'] += 1
    theme = item.get('features') or []
    if len(theme) > MAX_FEATURES:
        raise HTTPException(413, '분석도형이 CAD 출력 한도를 초과합니다.')
    for feature in theme:
        try:
            g = shape(feature['geometry'])
            if g.is_empty or not g.is_valid:
                raise ValueError('invalid theme geometry')
            clipped = g.intersection(site)
            if clipped.is_empty:
                continue
            _entities(model, transform(convert, clipped), theme_layer, counts)
            counts['thematic_features'] += 1
        except (ValueError, KeyError, TypeError):
            raise HTTPException(422, '검토항목 공간자료의 CAD 변환에 실패했습니다.') from None
    stream = io.StringIO()
    doc.write(stream)
    content = stream.getvalue().encode('utf-8')
    if len(content) > MAX_DXF_BYTES:
        raise HTTPException(413, 'CAD 도면이 파일 크기 한도를 초과합니다.')
    metadata.update(counts)
    metadata['frame_bbox_epsg5179'] = [round(v, 3) for v in frame.bounds]
    metadata['frame_width_height_multiplier'] = FRAME_MULTIPLIER
    metadata['cadastre_scope'] = 'expanded_frame'
    metadata['cadastre_boundary_style'] = 'shared_edges_merged'
    metadata['parcel_label_placement'] = 'inside_polygon_adaptive_height_collision_avoidance'
    metadata['cadastre_provider_calls'] = parcels.get('request_count')
    metadata['cadastre_duplicate_features_removed'] = parcels.get('duplicates_removed')
    metadata['cadastre_coverage_assurance'] = parcels.get('coverage_assurance') 
    metadata['cadastre_coverage_note'] = '조회 완료는 도곽 내부 모든 필지 수록의 최종 검증을 보증하지 않습니다.'
    return content


def manifest_item(item, snapshot, data):
    cadastral = snapshot.get('_report_layers', {}).get('LP_PA_CBND_BUBUN', {})
    return {
        'id': item['id'], 'name': item['name'], 'status': item.get('status'),
        'source': item.get('source'), 'as_of': item.get('as_of') or data.get('as_of'),
        'exportable': _exportable(item, snapshot),
        'cadastral_status': cadastral.get('status', 'UNAVAILABLE'),
        'cadastre_frame_width_height_multiplier': FRAME_MULTIPLIER,
        'cadastre_background_status': snapshot.get('_cad_background', {}).get('status', 'UNAVAILABLE'),
        'cadastre_coverage_assurance': snapshot.get('_cad_background', {}).get('coverage_assurance', 'UNVERIFIED'),
        'crs': CRS, 'units': 'metres', 'notice': NOTICE,
    }


def _verified_snapshot(token, data, snapshots, lock):
    with lock:
        raw = snapshots.get(token)
        if raw is None:
            raise HTTPException(404, '확정 대상지가 만료되었습니다.')
        snapshot = copy.deepcopy(raw)
    for key, revision in data.get('fact_revisions', {}).items():
        if snapshot.get('_report_layers', {}).get(key, {}).get('fact_revision') != revision:
            raise HTTPException(409, '자료 갱신 중입니다. 조서를 다시 조회하세요.')
    return snapshot


def register_cad(router, output, snapshots, lock, fetch_layer, layer_type):
    # Preserve the source FACT: nearby cadastral geometry is a separate cached drawing context.
    import threading
    cad_gate = threading.Lock()

    def context(token):
        report = output(token)  # Same enriched FACT used by web and XLSX.
        snapshot = _verified_snapshot(token, report, snapshots, lock)
        site_fact = snapshot.get('_report_layers', {}).get('LP_PA_CBND_BUBUN', {})
        if site_fact.get('status') != 'COMPLETE':
            raise HTTPException(409, '서버 직접 확보된 대상지 연속지적이 필요합니다.')
        revision = site_fact.get('fact_revision')
        with lock:
            cache = snapshots.get(token, {}).get('_cad_background', {})
            if cache.get('source_revision') == revision and cache.get('status') == 'COMPLETE' and cache.get('cache_version') == CAD_CACHE_VERSION:
                snapshot['_cad_background'] = copy.deepcopy(cache)
                return report, snapshot
        if not cad_gate.acquire(False):
            raise HTTPException(429, '주변 지적 도면을 생성 중입니다. 잠시 후 재시도하세요.')
        try:
            with lock:
                cache = snapshots.get(token, {}).get('_cad_background', {})
                if cache.get('source_revision') == revision and cache.get('status') == 'COMPLETE' and cache.get('cache_version') == CAD_CACHE_VERSION:
                    snapshot['_cad_background'] = copy.deepcopy(cache)
                    return report, snapshot
            frame = _drawing_frame(shape(snapshot['geometry']))
            background = _collect_surrounding_cadastre(frame, fetch_layer, layer_type)
            background['source_revision'] = revision
            background['queried_at'] = datetime.now(ZoneInfo('Asia/Seoul')).isoformat(timespec='seconds')
            with lock:
                live = snapshots.get(token)
                if live is None or live.get('_report_layers', {}).get('LP_PA_CBND_BUBUN', {}).get('fact_revision') != revision:
                    raise HTTPException(409, '지적자료가 변경되었습니다. 도면을 다시 요청하세요.')
                live['_cad_background'] = copy.deepcopy(background)
            snapshot['_cad_background'] = background
            return report, snapshot
        finally:
            cad_gate.release()

    @router.get('/reports/{token}/dxf/{item_id}')
    def download_dxf(token: str, item_id: str):
        report, snapshot = context(token)
        item = next((x for x in report['items'] if x['id'] == item_id), None)
        if not item:
            raise HTTPException(404, '등록되지 않은 검토항목입니다.')
        if not _exportable(item, snapshot):
            raise HTTPException(409, '이 항목의 검증된 공간도형이 없어 CAD를 생성하지 않습니다.')
        details = manifest_item(item, snapshot, report)
        payload = dxf_bytes(item, snapshot, details)
        return Response(payload, media_type='application/dxf', headers={
            'Content-Disposition': f'attachment; filename="survey_{item_id.replace("-", "_")}.dxf"',
            'Cache-Control': 'no-store',
        })

    @router.get('/reports/{token}/dxf.zip')
    def download_zip(token: str):
        report, snapshot = context(token)
        created = datetime.now(ZoneInfo('Asia/Seoul')).isoformat(timespec='seconds')
        entries = []
        target = io.BytesIO()
        with zipfile.ZipFile(target, mode='w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
            for item in report['items']:
                record = manifest_item(item, snapshot, report)
                record['filename'] = None
                if record['exportable']:
                    name = 'survey_' + item['id'].replace('-', '_') + '.dxf'
                    payload = dxf_bytes(item, snapshot, record)
                    bundle.writestr(name, payload)
                    record['filename'] = name
                entries.append(record)
                if target.tell() > MAX_ZIP_BYTES:
                    raise HTTPException(413, '일괄 CAD 용량이 제한을 초과했습니다. 항목별로 다운로드하세요.')
            manifest = {
                'created_at': created, 'crs': CRS, 'units': 'metres',
                'site_area_m2': snapshot.get('area_m2'),
                'frame_width_height_multiplier': FRAME_MULTIPLIER,
                'cadastre_request_count': snapshot.get('_cad_background', {}).get('request_count'),
                'cadastre_duplicates_removed': snapshot.get('_cad_background', {}).get('duplicates_removed'),
                'fact_revisions': report.get('fact_revisions', {}),
                'notice': NOTICE, 'items': entries,
            }
            bundle.writestr('survey_manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
            bundle.writestr('README.txt',
                '도시계획 기초현황조사 DXF 참고 성과품\n'
                '좌표계 EPSG:5179(KGD2002 Unified CS), 단위 m, AutoCAD R2010 DXF\n'
                '레이어: DRAWING_FRAME / SITE_BOUNDARY / CADASTRE / PARCEL_LABEL / SURVEY_<항목번호>\n'
                '도곽: 대상지 EPSG:5179 바운딩박스의 가로·세로를 각각 2배 확장, 중심 동일. 주변 필지 별도 조회.\n'
                'CAD 파일이 없는 검토항목은 survey_manifest.json의 exportable=false를 참고하세요.\n'
                '연속지적은 서버 직접 확보된 사실자료만 CAD에 포함하며, 보조자료는 제외합니다.\n'
                + NOTICE + '\n')
        if target.tell() > MAX_ZIP_BYTES:
            raise HTTPException(413, '일괄 CAD 용량이 제한을 초과했습니다. 항목별로 다운로드하세요.')
        return Response(target.getvalue(), media_type='application/zip', headers={
            'Content-Disposition': 'attachment; filename="survey_cad_bundle.zip"',
            'Cache-Control': 'no-store',
        })