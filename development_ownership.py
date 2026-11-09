"""2-1 ownership-category thematic FACT using official parcel ledger and cadastral PNU.

The source's ``posesnSeCodeNm`` is an ownership category, not a person's name
or a verified legal owner.  The palette is visual-only, not statutory symbology.
Unknown or unverified values remain explicitly unclassified.
"""
from collections import defaultdict

from shapely.geometry import mapping, shape
from shapely.ops import unary_union

# Stable display colors across survey sites; not official cadastral color codes.
CATEGORIES = (
    ('국유', '#7fa9d4'),
    ('공유', '#e2b44e'),
    ('개인', '#e9918d'),
    ('법인', '#a2cba1'),
    ('사유(상세 미확인)', '#e9a7aa'),
    ('종중', '#b8a0ce'),
    ('기타', '#c1be9e'),
)
COLORS = {label: color for label, color in CATEGORIES}
UNKNOWN = ('소유구분 미확보', '#e1e4e8')


def ownership_class(value):
    """Conservative mapping: do not turn unidentified values into 'private'."""
    if not isinstance(value, str):
        return None
    raw = ''.join(value.split())
    if not raw or len(raw) > 40:
        return None
    if raw in ('국유', '국유지', '국가', '국가소유', '국유재산'):
        return '국유'
    if raw in ('공유', '공유지', '공유재산', '시유', '시유지', '도유', '도유지',
               '군유', '군유지', '구유', '구유지', '지자체', '지방자치단체'):
        return '공유'
    if raw in ('개인', '개인소유', '사인'):
        return '개인'
    if raw in ('법인', '법인소유'):
        return '법인'
    if raw in ('사유', '사유지', '사유재산'):
        return '사유(상세 미확인)'
    if raw in ('종중', '종중소유'):
        return '종중'
    if raw in ('기타', '기타소유'):
        return '기타'
    return None


def attach_ownership_theme(data, snapshot, area):
    """Map only verified ownership categories to site-clipped cadastral polygons.

    Do not classify unrecognized source text or missing ledger responses.
    Shared geometry, class color, and area summaries drive map/CAD/XLSX.
    """
    item = next((v for v in data.get('items', ()) if v.get('id') == '2-1'), None)
    if item is None:
        return
    cadastral = snapshot.get('_report_layers', {}).get('LP_PA_CBND_BUBUN', {})
    assurance = cadastral.get('status')
    if assurance not in ('COMPLETE', 'REFERENCE'):
        return
    land = {str(row['pnu']): row for row in data.get('land', ()) if row.get('pnu')}
    if not land:
        return
    site = shape(snapshot['geometry'])
    if site.is_empty or not site.is_valid:
        return
    grouped = defaultdict(list)
    for feature in cadastral.get('features', ()):
        properties = {str(k).lower(): v for k, v in (feature.get('properties') or {}).items()}
        pnu = str(properties.get('pnu') or feature.get('id') or '')
        if pnu not in land:
            continue
        try:
            geom = shape(feature['geometry'])
            if geom.is_empty or not geom.is_valid:
                continue
            clipped = geom.intersection(site)
            if clipped.is_empty or area(clipped) <= 0:
                continue
            grouped[pnu].append(clipped)
        except (KeyError, TypeError, ValueError):
            continue
    if not grouped:
        return

    parcel_geoms = {pnu: unary_union(parts) for pnu, parts in grouped.items()}
    rows = []
    valid_count = 0
    for pnu, geom in sorted(parcel_geoms.items()):
        row = land[pnu]
        category = ownership_class(row.get('ownership')) if row.get('ledger_status') == '확보' else None
        if category is not None:
            valid_count += 1
        rows.append((pnu, geom, category))
    if valid_count == 0:
        return  # Unknown ownership is not a usable official ownership thematic map.

    legend_geoms = defaultdict(list)
    legend_counts = defaultdict(int)
    features = []
    ordered_classes = [category for category, _ in CATEGORIES]
    present = [name for name in ordered_classes if any(cat == name for _, _, cat in rows)]
    codes = {name: f'C{i + 1:02d}' for i, name in enumerate(present)}
    for pnu, geom, category in rows:
        name = category or UNKNOWN[0]
        code = codes[category] if category else 'C00'
        color = COLORS[category] if category else UNKNOWN[1]
        legend_geoms[name].append(geom)
        legend_counts[name] += 1
        features.append({'type': 'Feature', 'geometry': mapping(geom),
                         'properties': {'pnu': pnu, 'name': name,
                                        'class_code': code, 'color': color,
                                        'missing': category is None}})

    legend = []
    for name in present + ([UNKNOWN[0]] if UNKNOWN[0] in legend_geoms else []):
        category = name if name in codes else None
        legend.append({'name': name, 'class_code': codes[category] if category else 'C00',
                       'color': COLORS[category] if category else UNKNOWN[1],
                       'count': legend_counts[name],
                       'area_m2': round(area(unary_union(legend_geoms[name])), 2)})
    verified = assurance == 'COMPLETE'
    complete = len(parcel_geoms) == len(land) and valid_count == len(land)
    item.update({
        'status': ('공간자료 확보' if complete else '일부 확보') if verified else '보조 조회 참고자료',
        'detail': f'토지대장 소유구분·연속지적 PNU 결합 {valid_count}/{len(land)}필지 · '
                  f'도형 {len(parcel_geoms)}/{len(land)}필지 · 개인 성명/등기 소유자 미조회 · '
                  + ('대상지와 교차한 도형면적 기준' if verified else '보조 지적도형 사용 · CAD 제외'),
        'source': '연속지적(VWorld) · 토지대장 소유구분',
        'features': features,
        'legend': legend,
        'thematic_type': 'ownership',
        'provider_verified': verified,
    })
    stats = data.setdefault('statistics', {}).setdefault('2-1', {})
    stats.update({'mapped_groups': legend, 'mapped_parcel_count': len(parcel_geoms),
                  'classified_parcel_count': valid_count,
                  'unclassified_parcel_count': len(land) - valid_count,
                  'mapped_area_m2': round(area(unary_union(list(parcel_geoms.values()))), 2),
                  'map_assurance': assurance,
                  'note_map': '소유구분은 토지대장 속성의 범주별 참고표시이며 개인별 소유자 분석이 아님. '
                              '분류 미확보를 사유지·국유지 등으로 추정하지 않음.'})
    item['statistics'] = stats