"""Server-side CAD outputs from the nationwide development review's frozen FACTs.

Only confirmed geometry and provider-verified or registered official source layers
are exported. This is a preliminary survey drawing, not an approved cadastral map.
"""
import copy
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
from shapely.geometry import shape
from shapely.ops import transform

CRS = 'EPSG:5179'
MAX_FEATURES = 6000
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


def dxf_bytes(item, snapshot, metadata):
    site = shape(snapshot['geometry'])
    if site.is_empty or not site.is_valid:
        raise HTTPException(422, '확정 구역계 도형이 유효하지 않습니다.')
    convert = Transformer.from_crs('EPSG:4326', CRS, always_xy=True).transform
    projected = transform(convert, site)
    doc = ezdxf.new('R2010')
    doc.header['$INSUNITS'] = 6  # metres
    doc.header['$MEASUREMENT'] = 1
    doc.header['$LUPREC'] = 3
    doc.layers.new('SITE_BOUNDARY', dxfattribs={'color': 1})
    doc.layers.new('CADASTRE', dxfattribs={'color': 8})
    doc.layers.new('PARCEL_LABEL', dxfattribs={'color': 7})
    theme_layer = 'SURVEY_' + item['id'].replace('-', '_')
    doc.layers.new(theme_layer, dxfattribs={'color': 5})
    model = doc.modelspace()
    counts = {'polylines': 0, 'points': 0, 'parcels': 0, 'thematic_features': 0}
    _entities(model, projected, 'SITE_BOUNDARY', counts)
    parcels = snapshot.get('_report_layers', {}).get('LP_PA_CBND_BUBUN', {})
    if parcels.get('status') == 'COMPLETE':
        source = parcels.get('features') or []
        if len(source) > MAX_FEATURES:
            raise HTTPException(413, '지적도형이 CAD 출력 한도를 초과합니다.')
        for feature in source:
            try:
                geom = shape(feature['geometry'])
                if not geom.is_valid or geom.is_empty:
                    continue
                _entities(model, transform(convert, geom), 'CADASTRE', counts)
                counts['parcels'] += 1
                props = feature.get('properties') or {}
                jibun = props.get('jibun') or props.get('JIBUN')
                portion = geom.intersection(site)
                if jibun and not portion.is_empty:
                    pt = transform(convert, portion.representative_point())
                    model.add_text(str(jibun)[:60], dxfattribs={
                        'layer': 'PARCEL_LABEL', 'height': 2.4,
                        'insert': (float(pt.x), float(pt.y)),
                    })
            except (ValueError, TypeError, KeyError):
                raise HTTPException(422, '지적 원자료의 CAD 도형 변환에 실패했습니다.') from None
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
    return content


def manifest_item(item, snapshot, data):
    cadastral = snapshot.get('_report_layers', {}).get('LP_PA_CBND_BUBUN', {})
    return {
        'id': item['id'], 'name': item['name'], 'status': item.get('status'),
        'source': item.get('source'), 'as_of': item.get('as_of') or data.get('as_of'),
        'exportable': _exportable(item, snapshot),
        'cadastral_status': cadastral.get('status', 'UNAVAILABLE'),
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


def register_cad(router, output, snapshots, lock):
    def context(token):
        report = output(token)  # Same enriched FACT used by web and XLSX.
        snapshot = _verified_snapshot(token, report, snapshots, lock)
        return report, snapshot

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
                'fact_revisions': report.get('fact_revisions', {}),
                'notice': NOTICE, 'items': entries,
            }
            bundle.writestr('survey_manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
            bundle.writestr('README.txt',
                '도시계획 기초현황조사 DXF 참고 성과품\n'
                '좌표계 EPSG:5179(KGD2002 Unified CS), 단위 m, AutoCAD R2010 DXF\n'
                '레이어: SITE_BOUNDARY / CADASTRE / PARCEL_LABEL / SURVEY_<항목번호>\n'
                'CAD 파일이 없는 검토항목은 survey_manifest.json의 exportable=false를 참고하세요.\n'
                '연속지적은 서버 직접 확보된 사실자료만 CAD에 포함하며, 보조자료는 제외합니다.\n'
                + NOTICE + '\n')
        if target.tell() > MAX_ZIP_BYTES:
            raise HTTPException(413, '일괄 CAD 용량이 제한을 초과했습니다. 항목별로 다운로드하세요.')
        return Response(target.getvalue(), media_type='application/zip', headers={
            'Content-Disposition': 'attachment; filename="survey_cad_bundle.zip"',
            'Cache-Control': 'no-store',
        })