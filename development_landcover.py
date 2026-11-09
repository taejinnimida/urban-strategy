"""2-6 land-cover WMS reference versus verifiable official vector survey FACT.

WMS pixels are *not* polygon features and cannot be used for DXF or area
statistics. The official-vector branch is enabled only with manifest metadata,
verified coverage and complete non-overlapping classified geometry.
"""
from collections import defaultdict
from shapely.geometry import shape, mapping
from shapely.ops import unary_union

WMS_SOURCE = 'https://aid.mcee.go.kr/api/land.do'
WMS_URL = 'https://api.mcee.go.kr/geoserver/gwc/service/wms'
WMS_LAYER = 'EGIS:lv3_2025y'
COLORS = ('#d8c277','#a0cba2','#b3c8e5','#e5b1ab','#e7cbb1',
          '#add3d2','#c8b7d8','#f4d18e','#d6e9bb','#9cabc7')
MISSING = '분류 미확보'


def attach_landcover_theme(data, snapshot, area):
    item = next((v for v in data.get('items', ()) if v.get('id') == '2-6'), None)
    if item is None:
        return
    item['landcover_wms'] = True
    item['wms_layer'] = WMS_LAYER
    item['wms_source_url'] = WMS_SOURCE
    sources = snapshot.get('_national_sources', {})
    wms = sources.get('landcover_wms', {})
    wfs = sources.get('landcover_wfs', {})
    if wms.get('status') == 'MAP_ONLY':
        item['landcover_map_available'] = True
        item['landcover_map_bounds'] = wms.get('bounds')
    source = sources.get('file:2-6', {})
    official_wfs = (wfs.get('verified_feature_type') is True
                    and wfs.get('complete_page') is True
                    and wfs.get('layer') == WMS_LAYER
                    and wfs.get('status') == 'PARTIAL')
    if source.get('status') not in ('DATA', 'PARTIAL') and official_wfs:
        source = wfs
    if source.get('status') not in ('DATA', 'PARTIAL'):
        wfs_state = wfs.get('status') or 'NOT_QUERIED'
        wms_state = wms.get('status') or 'NOT_QUERIED'
        item.update({
            'status':'WMS 참고도 확보' if wms_state == 'MAP_ONLY' else 'WMS 미확보',
            'detail':f'2025년 세분류 토지피복 WMS: {wms_state} · '
                     f'벡터 WFS 제공 상태: {wfs_state} · '
                     '공식 벡터 원자료의 면적·분류 완전성 미검증 · CAD 및 유형별 면적 산출 불가',
            'source':'환경공간정보서비스 WMS',
            'source_url':WMS_SOURCE,
            'provider_verified':False,
        })
        return
    target = shape(snapshot['geometry'])
    if target.is_empty or not target.is_valid:
        return
    geoms, counts = defaultdict(list), defaultdict(int)
    for feature in source.get('features', ()):
        try:
            geom = shape(feature['geometry'])
            if geom.is_empty or not geom.is_valid:
                continue
            clipped = geom.intersection(target)
            if clipped.is_empty or area(clipped) <= 0:
                continue
            name = str((feature.get('properties') or {}).get('name') or '').strip()
        except (KeyError, TypeError, ValueError):
            continue
        name = name if name and name != MISSING else MISSING
        geoms[name].append(clipped)
        counts[name] += 1
    known = sorted(n for n in geoms if n != MISSING)
    if not known or len(known) > 95:
        item.update({'status':'일부 확보', 'provider_verified':False,
                     'detail':'토지피복 등록 벡터에서 유효한 분류도형을 검증하지 못했습니다.'})
        return
    classes = {name:(f'C{i+1:02d}',COLORS[i%len(COLORS)]) for i,name in enumerate(known)}
    legend, features = [], []
    for name in known + ([MISSING] if MISSING in geoms else []):
        code,color=classes.get(name,('C00','#e2e5e9'))
        for geom in geoms[name]:
            features.append({'type':'Feature','geometry':mapping(geom),
                             'properties':{'name':name,'class_code':code,'color':color}})
        legend.append({'name':name,'class_code':code,'color':color,'count':counts[name],
                       'area_m2':round(area(unary_union(geoms[name])),2)})
    union = unary_union([g for group in geoms.values() for g in group])
    covered_area, site_area = area(union),area(target)
    overlaid = max(0.0,sum(area(unary_union(gs)) for gs in geoms.values())-covered_area)
    coverage_ok=abs(covered_area-site_area)<=max(1.0,site_area*.001)
    overlap_ok=overlaid<=max(1.0,site_area*.001)
    complete = ((source['status']=='DATA' or (source is wfs and official_wfs))
                and coverage_ok and overlap_ok and MISSING not in geoms)
    item.update({
        'status':'공간자료 확보' if complete else '일부 확보',
        'detail':f'토지피복 등록 벡터 {len(features)}도형 · 실제 편입 {covered_area:,.2f}㎡ / '
                 f'대상지 {site_area:,.2f}㎡ · 분류 간 중첩 {overlaid:,.2f}㎡ · '
                 + ('기하학적 수록범위 검증 통과, 최신성 별도 확인' if complete else
                    '분류 또는 수록범위 불완전 · 정식 CAD 불가'),
        'source':source.get('source') or '공식 등록 벡터',
        'source_url':source.get('source_url') or WMS_SOURCE,
        'features':features,'legend':legend,'thematic_type':'landcover',
        'provider_verified':complete,'as_of':source.get('as_of'),
    })
    stats=data.setdefault('statistics',{}).setdefault('2-6',{})
    stats.update({'mapped_groups':legend,'mapped_area_m2':round(covered_area,2),
                  'site_area_m2':round(site_area,2),'mapped_polygon_count':len(features),
                  'missing_category_count':counts[MISSING],'overlap_warning_m2':round(overlaid,2),
                  'coverage_complete':coverage_ok,'note_map':'원본 벡터와 대상지의 교차면적만 계산. '
                  'WMS 이미지로 면적을 추정하지 않음. 최신성은 별도 확인.'})
    item['statistics']=stats
