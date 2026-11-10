"""Verified cadastral + land-ledger join for the 2-2 jimok thematic map.

Display classifications are not statutory land-use decisions. The same clipped
parcel geometries, category IDs and colors are passed to screen and CAD.
"""
from collections import defaultdict
from shapely.geometry import shape, mapping
from shapely.ops import unary_union

# Display palette only; no claim that these are an official cadastral legend.
_PALETTE = (
    '#efbca1', '#e7d889', '#accf9d', '#9ccdcc', '#a8bfe7',
    '#d3b5e4', '#e6a6b5', '#c7c7a6', '#8dd8b9', '#e3bd87',
    '#b8c8da', '#e5b6d5', '#b6d89b', '#edc5a5', '#d4d2a2',
    '#99baca', '#c2b2d9', '#dbbaa8', '#acd4d8', '#e3d19d',
)


def attach_jimok_theme(data, snapshot, area):
    """Add thematic vectors only when parcel PNU matches acquired land rows.

    A REFERENCE cadastral set may be previewed but is never promoted to an
    official CAD export. Missing categories remain unknown, not 'other'.
    """
    item = next((v for v in data.get('items', ()) if v.get('id') == '2-2'), None)
    if item is None:
        return
    source = snapshot.get('_report_layers', {}).get('LP_PA_CBND_BUBUN', {})
    assurance = source.get('status')
    if assurance not in ('COMPLETE', 'REFERENCE'):
        return
    land = {str(v.get('pnu')): v for v in data.get('land', ()) if v.get('pnu')}
    if not land:
        return
    site = shape(snapshot['geometry'])
    parcels = defaultdict(list)
    for feature in source.get('features', ()):
        attrs = {str(k).lower(): v for k, v in (feature.get('properties') or {}).items()}
        pnu = str(attrs.get('pnu') or feature.get('id') or '')
        if pnu not in land:
            continue
        geom = shape(feature['geometry'])
        if geom.is_empty or not geom.is_valid:
            continue
        clipped = geom.intersection(site)
        if clipped.is_empty or area(clipped) <= 0:
            continue
        parcels[pnu].append(clipped)

    records = []
    for pnu, parts in parcels.items():
        category = land[pnu].get('category')
        if category is None or not str(category).strip():
            continue
        geom = unary_union(parts)
        if geom.is_empty or area(geom) <= 0:
            continue
        records.append((pnu, str(category).strip(), geom))
    if not records:
        return

    names = sorted({category for _, category, _ in records})
    classes = {name: (f'C{i + 1:02d}', _PALETTE[i % len(_PALETTE)])
               for i, name in enumerate(names)}
    groups = defaultdict(list)
    counts = defaultdict(int)
    features = []
    for pnu, name, geom in sorted(records):
        code, color = classes[name]
        groups[name].append(geom)
        counts[name] += 1
        features.append({'type': 'Feature', 'geometry': mapping(geom),
                         'properties': {'pnu': pnu, 'name': name,
                                        'class_code': code, 'color': color}})

    legend = [{'name': name, 'class_code': classes[name][0],
               'color': classes[name][1], 'count': counts[name],
               'area_m2': round(area(unary_union(groups[name])), 2)}
              for name in names]
    mapped = area(unary_union([g for _, _, g in records]))
    full_rows = len(records) == len(land) and len(parcels) == len(land)
    official = assurance == 'COMPLETE'
    item.update({
        'status': ('공간자료 확보' if full_rows else '일부 확보') if official else '보조 조회 참고자료',
        'detail': (f'지적 PNU-토지대장 지목 결합 {len(records)}/{len(land)}필지 · '
                   f'분류 {len(legend)}종 · 대상지와 교차된 면적 기준 · '
                   + ('서버 직접 지적 수신(전체 수록 별도 검증)' if official
                      else '보조 지적 참고도형(원문 진위·수록 완전성 미검증)')),
        'source': '연속지적(VWorld) · 필지별 토지대장',
        'features': features,
        'legend': legend,
        'thematic_type': 'jimok',
        'provider_verified': official,
    })
    stats = data.setdefault('statistics', {}).setdefault('2-2', {})
    stats['mapped_groups'] = legend
    stats['mapped_parcel_count'] = len(records)
    stats['mapped_area_m2'] = round(mapped, 2)
    stats['map_assurance'] = assurance
    stats['note_map'] = ('표시 색상은 시각적 분류용(법정 색상 아님). '
                         '누락 지목·도형은 미확보로 남기며 대상지 전체의 지목별 면적을 추정하지 않음.')
    item['statistics'] = stats
