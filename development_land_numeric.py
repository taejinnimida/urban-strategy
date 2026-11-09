"""Development-only numeric cadastral themes for 2-3 and 2-4.

Official parcel/price attributes are joined by PNU to verified cadastral geometry.
Classification is descriptive, NOT a statutory area or price threshold.
"""
import math
from collections import defaultdict
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

AREA_BREAKS = (100, 200, 500, 1000)  # m2, descriptive map bands only
COLORS = ('#edf2f7', '#c5e0ef', '#8bc6e0', '#5097c5', '#205f93')
NO_DATA_COLOR = '#e3e5e7'


def _valid_number(value):
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) and n > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _quantile_breaks(values):
    """Up to four observed-value 20/40/60/80% boundaries; tie-safe."""
    ordered = sorted(values)
    return tuple(sorted({ordered[math.ceil(i * len(ordered) / 5) - 1]
                         for i in range(1, 5)}))


def _bands(breaks, unit):
    labels = []
    lower = None
    for limit in breaks:
        text = f'{limit:,.0f}{unit} 이하' if lower is None else f'{lower:,.0f}{unit} 초과~{limit:,.0f}{unit} 이하'
        labels.append(text)
        lower = limit
    labels.append(f'{lower:,.0f}{unit} 초과' if lower is not None else '전체')
    return labels


def attach_numeric_land_themes(data, snapshot, area):
    """Attach display features, area/count stats, source provenance, CAD classes.

    Mapped thematic area means the area of site-clipped parcel *geometry*.
    2-3 classification is based on full registered parcel area (official_area),
    NOT the site-clipped area. 2-4 is annual official price per m2.
    """
    lookup = {x['id']: x for x in data.get('items', ()) if x.get('id') in ('2-3', '2-4')}
    if not lookup:
        return
    cadastral = snapshot.get('_report_layers', {}).get('LP_PA_CBND_BUBUN', {})
    assurance = cadastral.get('status')
    if assurance not in ('COMPLETE', 'REFERENCE'):
        return
    site = shape(snapshot['geometry'])
    if site.is_empty or not site.is_valid:
        return
    land = {str(row['pnu']): row for row in data.get('land', ()) if row.get('pnu')}
    if not land:
        return
    parcel_geoms = defaultdict(list)
    for feature in cadastral.get('features', ()):
        p = {str(k).lower(): v for k, v in (feature.get('properties') or {}).items()}
        pnu = str(p.get('pnu') or feature.get('id') or '')
        if pnu not in land:
            continue
        try:
            geometry = shape(feature['geometry'])
            if geometry.is_empty or not geometry.is_valid:
                continue
            clipped = geometry.intersection(site)
            if not clipped.is_empty and area(clipped) > 0:
                parcel_geoms[pnu].append(clipped)
        except (KeyError, TypeError, ValueError):
            continue
    parcels = {pnu: unary_union(parts) for pnu, parts in parcel_geoms.items()}
    if not parcels:
        return

    for item_id, item in lookup.items():
        price_theme = item_id == '2-4'
        field = 'price' if price_theme else 'official_area'
        valid = {}
        expected_year = int(str(data.get('as_of', ''))[:4] or 0)
        for pnu in parcels:
            row = land[pnu]
            if row.get('price_status' if price_theme else 'ledger_status') != '확보':
                continue
            if price_theme and str(row.get('price_year') or '') != str(expected_year):
                continue
            v = _valid_number(row.get(field))
            if v is not None:
                valid[pnu] = v
        if not valid:
            continue  # Do not publish an empty or falsely completed thematic map.
        breaks = _quantile_breaks(tuple(valid.values())) if price_theme else AREA_BREAKS
        units = '원/㎡' if price_theme else '㎡'
        labels = _bands(breaks, units)
        classes = defaultdict(list)
        features = []
        for pnu, geometry in sorted(parcels.items()):
            if pnu not in valid:
                index = None
                name, code, color = '속성 미확보', 'C00', NO_DATA_COLOR
            else:
                value = valid[pnu]
                index = next((j for j, edge in enumerate(breaks) if value <= edge), len(breaks))
                name, code, color = labels[index], f'C{index + 1:02d}', COLORS[min(index, len(COLORS) - 1)]
            classes[(code, name, color)].append(geometry)
            features.append({'type': 'Feature', 'geometry': mapping(geometry),
                             'properties': {'pnu': pnu, 'name': name, 'class_code': code,
                                            'color': color, 'missing': index is None,
                                            **({field: valid[pnu]} if index is not None else {})}})
        legend = []
        for (code, name, color), shapes in sorted(classes.items()):
            legend.append({'class_code': code, 'name': name, 'color': color,
                           'count': len(shapes), 'area_m2': round(area(unary_union(shapes)), 2)})
        known = [parcels[pnu] for pnu in valid]
        mapped_known = area(unary_union(known))
        mapped_all = area(unary_union(list(parcels.values())))
        complete = len(valid) == len(land) and len(parcels) == len(land)
        official = assurance == 'COMPLETE'
        note = ('공부면적 기준 고정 구간(사업별 법정 과소필지 기준 아님)' if not price_theme
                else f'{expected_year}년 개별공시지가(원/㎡) 조회값의 최대 5분위. 동일 값은 동일 구간')
        item.update({
            'status': ('공간자료 확보' if complete else '일부 확보') if official else '보조 조회 참고자료',
            'detail': (f'{len(valid)}/{len(land)}필지 유효값 · 대상지 교차도형 {len(parcels)}/{len(land)}필지 · '
                       f'{note} · 범례 면적은 편입 도형면적 · '
                       + ('서버 직접 지적 수신(전체 수록 별도 검증)' if official
                          else '보조 지적 참고도형(원문 진위·수록 완전성 미검증)')),
            'source': ('VWorld 연속지적 · 토지대장 공부면적' if not price_theme
                       else 'VWorld 연속지적 · 개별공시지가'),
            'features': features,
            'legend': legend,
            'thematic_type': 'land_numeric',
            'provider_verified': official,
        })
        stats = data.setdefault('statistics', {}).setdefault(item_id, {})
        stats.update({'mapped_groups': legend, 'mapped_parcel_count': len(parcels),
                      'known_parcel_count': len(valid), 'missing_parcel_count': len(land) - len(valid),
                      'mapped_known_area_m2': round(mapped_known, 2),
                      'mapped_all_area_m2': round(mapped_all, 2),
                      'map_assurance': assurance, 'classification_basis': note,
                      'note_map': '값의 구간분류와 실제 편입 도형면적은 구분. 원자료 미확보 필지는 회색으로 표시.'})
        item['statistics'] = stats
