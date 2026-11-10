"""2-5 land-shape map: VWorld characteristic shape attribute + cadastral PNU.

The original class name is not a statutory irregular-parcel determination.
Unknowns are never inferred; auxiliary cadastral data cannot enable official CAD.
"""
from collections import defaultdict
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

COLORS = (
    '#e8d8b4', '#bad4a1', '#97c9d8', '#abc4ef', '#e7b4c2',
    '#d6b7e9', '#efc28c', '#c7d5a7', '#c9d5c8', '#dbb0a7',
    '#b7d6cb', '#ddcf98', '#b9b0dc', '#d7c5b6', '#d5dce6')
UNKNOWN_NAME, UNKNOWN_COLOR = '형상 미확보', '#e2e5e9'


def attach_land_shape_theme(data, snapshot, area):
    item = next((x for x in data.get('items', ()) if x.get('id') == '2-5'), None)
    if item is None:
        return
    cadastre = snapshot.get('_report_layers', {}).get('LP_PA_CBND_BUBUN', {})
    assurance = cadastre.get('status')
    if assurance not in ('COMPLETE', 'REFERENCE'):
        return
    site = shape(snapshot['geometry'])
    if site.is_empty or not site.is_valid:
        return
    land = {str(x['pnu']): x for x in data.get('land', ()) if x.get('pnu')}
    if not land:
        return
    grouped = defaultdict(list)
    for f in cadastre.get('features', ()):
        props = {str(k).lower(): v for k, v in (f.get('properties') or {}).items()}
        pnu = str(props.get('pnu') or f.get('id') or '')
        if pnu not in land:
            continue
        try:
            geom = shape(f['geometry'])
            if geom.is_empty or not geom.is_valid:
                continue
            clipped = geom.intersection(site)
            if not clipped.is_empty and area(clipped) > 0:
                grouped[pnu].append(clipped)
        except (KeyError, TypeError, ValueError):
            continue
    if not grouped:
        return
    parcel_geoms = {p: unary_union(gs) for p, gs in grouped.items()}
    facts = snapshot.get('_national_sources', {})
    known = {}
    for pnu in parcel_geoms:
        source = facts.get('characteristics:' + pnu, {})
        record = source.get('record') or {}
        if source.get('status') != 'ATTRIBUTE_ONLY' or str(record.get('pnu')) != pnu:
            continue
        name = record.get('topgrphFrmCodeNm')
        if isinstance(name, str) and name.strip():
            known[pnu] = name.strip()[:80]
    if not known:
        # A blank thematic result must report whether VWorld returned a
        # characteristic record or the request itself failed.
        counts = defaultdict(int)
        for pnu in land:
            entry = facts.get('characteristics:' + pnu, {})
            counts[entry.get('status') or 'NOT_QUERIED'] += 1
        item.update({
            'status':'미확보',
            'detail':f'지형형상명 0/{len(land)}필지 · VWorld 토지특성 원문 유효값 미확보 · '
                     + ', '.join(f'{k} {v}건' for k,v in sorted(counts.items()))
                     + ' · 토지형상을 임의 분류하지 않습니다.',
            'source':'VWorld 토지특성정보',
            'provider_verified':False,
        })
        return
    names = sorted(set(known.values()))
    if len(names) > 95:
        return  # Native CAD layer names use C01..C95; do not wrap classifications.
    classes = {n: (f'C{i+1:02d}', COLORS[i % len(COLORS)]) for i, n in enumerate(names)}
    shapes_by_class = defaultdict(list)
    counts = defaultdict(int)
    features = []
    for pnu, geom in sorted(parcel_geoms.items()):
        name = known.get(pnu, UNKNOWN_NAME)
        code, color = classes[name] if name in classes else ('C00', UNKNOWN_COLOR)
        shapes_by_class[name].append(geom)
        counts[name] += 1
        features.append({'type': 'Feature', 'geometry': mapping(geom),
                         'properties': {'pnu': pnu, 'name': name, 'class_code': code,
                                        'color': color, 'missing': pnu not in known}})
    legend = []
    for name in names + ([UNKNOWN_NAME] if UNKNOWN_NAME in shapes_by_class else []):
        code, color = classes[name] if name in classes else ('C00', UNKNOWN_COLOR)
        legend.append({'name': name, 'class_code': code, 'color': color,
                       'count': counts[name],
                       'area_m2': round(area(unary_union(shapes_by_class[name])), 2)})
    official = assurance == 'COMPLETE'
    complete = len(parcel_geoms) == len(land) and len(known) == len(land)
    item.update({
        'status': ('공간자료 확보' if complete else '일부 확보') if official else '보조 조회 참고자료',
        'detail': f'VWorld 토지특성정보 형상분류 PNU 결합 {len(known)}/{len(land)}필지 · '
                  f'교차도형 {len(parcel_geoms)}/{len(land)}필지 · '
                  '개별공시지가 조사 형상명(사업별 부정형 판정 아님) · '
                  + ('대상지 교차면적 기준, 원자료 전체 수록 여부 별도 검증' if official
                     else '보조 지적도형 기준 · 정식 CAD 제외'),
        'source': 'VWorld 토지특성정보 · 연속지적',
        'features': features, 'legend': legend, 'thematic_type': 'land_shape',
        'provider_verified': official,
    })
    stats = data.setdefault('statistics', {}).setdefault('2-5', {})
    stats.update({'mapped_groups': legend, 'mapped_parcel_count': len(parcel_geoms),
                  'known_parcel_count': len(known),
                  'unclassified_parcel_count': len(land) - len(known),
                  'mapped_area_m2': round(area(unary_union(list(parcel_geoms.values()))), 2),
                  'map_assurance': assurance,
                  'note_map': '토지특성정보 지형형상 분류. 법정 부정형 판정이 아님. '
                              '조회 누락은 미확보로 표시하며, 면적은 대상지 편입 도형면적.'})
    item['statistics'] = stats
