"""FACT-derived survey tables for land-status report items 2-1 through 2-6.

Tables are ABOVE maps in the UI and remain independent of DXF/paper layouts.
All class areas and labels are taken from the *same* classified map FACT.
The grand total uses the geometric union, not the sum of potentially
overlapping classes. Missing classes/coverage are never silently normalized
to 100 percent.
"""
import math
from shapely.geometry import shape
from shapely.ops import unary_union

LAND_ITEMS = ('2-1', '2-2', '2-3', '2-4', '2-5', '2-6')


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return v if math.isfinite(v) and v >= 0 else None


def attach_land_survey_tables(data, snapshot, area):
    site_area = _number(data.get('area_m2'))
    if not site_area or site_area <= 0:
        return
    try:
        site = shape(snapshot['geometry'])
        if site.is_empty or not site.is_valid:
            return
    except (KeyError, TypeError, ValueError):
        return
    land_count = len(data.get('land') or []) if data.get('parcel_status') == 'COMPLETE' else None

    for item in data.get('items', ()):
        item_id = item.get('id')
        if item_id not in LAND_ITEMS:
            continue
        count_is_parcel = item_id != '2-6'
        table = {
            'status': 'MISSING', 'item_id': item_id,
            'title': f'표 {item_id} {item.get("name") or "토지현황"} 현황',
            'count_header': '필지수(필지)' if count_is_parcel else '도형수(개)',
            'rows': [], 'notes': [],
            'source': item.get('source'), 'as_of': item.get('as_of') or data.get('as_of'),
        }
        item['survey_table'] = table

        # WMS-only land cover is a picture, never an area/count table.
        groups = item.get('legend') or []
        features = item.get('features') or []
        if not groups or not features:
            table['notes'] = [
                '분류별 공간도형·면적자료 미확보. 도면만으로 통계를 추정하지 않습니다.',
                '값 미확보는 0 또는 비해당을 뜻하지 않습니다.',
            ]
            continue

        category_rows = []
        invalid_group = False
        for group in groups:
            name = str(group.get('name') or '').strip()
            area_m2 = _number(group.get('area_m2'))
            n = group.get('count')
            if not name or area_m2 is None or isinstance(n, bool) or not isinstance(n, int) or n < 0:
                invalid_group = True
                break
            missing = group.get('class_code') == 'C00' or '미확보' in name or '미분류' in name
            category_rows.append({
                'kind': 'category', 'label': name, 'color': group.get('color'),
                'area_m2': round(area_m2, 2),
                'pct': round(100 * area_m2 / site_area, 1),
                'count': n, 'note': '분류 미확보' if missing else '',
                'missing': missing,
            })
        if invalid_group or not category_rows:
            table['notes'] = ['분류별 FACT 필드 검증 실패. 통계표를 작성하지 않습니다.']
            continue

        try:
            clipped = []
            for feature in features:
                geom = shape(feature['geometry'])
                if geom.is_empty or not geom.is_valid:
                    raise ValueError('invalid thematic geometry')
                g = geom.intersection(site)
                if not g.is_empty and area(g) > 0:
                    clipped.append(g)
            if not clipped:
                raise ValueError('empty thematic geometry')
            mapped_area = _number(area(unary_union(clipped)))
            if mapped_area is None:
                raise ValueError('invalid union area')
        except (KeyError, TypeError, ValueError):
            table['notes'] = ['주제도형의 면적 검증 실패. 통계표를 작성하지 않습니다.']
            continue

        mapped_area = round(mapped_area, 2)
        difference = round(site_area - mapped_area, 2)  # signed, not a fake zero
        category_sum = round(sum(x['area_m2'] for x in category_rows), 2)
        overlap = round(max(0.0, category_sum - mapped_area), 2)
        unique_pnus = {
            str((f.get('properties') or {}).get('pnu'))
            for f in features if (f.get('properties') or {}).get('pnu')
        }
        mapped_count = len(unique_pnus) if count_is_parcel and unique_pnus else (
            len(features) if not count_is_parcel else None
        )
        if item_id == '2-6':
            site_count = None
        else:
            site_count = land_count

        table['rows'] = [
            {'kind': 'boundary', 'label': '구역계 면적',
             'area_m2': round(site_area, 2), 'pct': 100.0,
             'count': site_count, 'note': '확정 구역계'},
            {'kind': 'difference', 'label': '면적차이',
             'area_m2': difference, 'pct': round(100 * difference / site_area, 1),
             'count': None, 'note': '구역계 − 집계면적'},
            {'kind': 'total', 'label': '계',
             'area_m2': mapped_area, 'pct': round(100 * mapped_area / site_area, 1),
             'count': mapped_count, 'note': '분류도형 합집합'},
        ] + category_rows
        if overlap > max(0.05, site_area * 0.00001):
            table['rows'].insert(3, {
                'kind': 'overlap', 'label': '분류 간 중첩',
                'area_m2': overlap, 'pct': round(100 * overlap / site_area, 1),
                'count': None, 'note': '분류면적 합계와 합집합의 차이',
            })
        missing_area = sum(x['area_m2'] for x in category_rows if x['missing'])
        category_complete = missing_area <= 0.001 and not any(x['missing'] for x in category_rows)
        gap_ok = abs(difference) <= max(0.5, site_area * .001)
        overlap_ok = overlap <= max(0.05, site_area * .00001)
        table['status'] = ('COMPLETE' if (item.get('provider_verified') is True
                                        and gap_ok and overlap_ok and category_complete)
                           else 'PARTIAL')
        table['site_area_m2'] = round(site_area, 2)
        table['mapped_area_m2'] = mapped_area
        table['mapped_ratio_pct'] = round(100 * mapped_area / site_area, 1)
        table['missing_category_area_m2'] = round(missing_area, 2)
        table['overlap_area_m2'] = overlap
        table['notes'] = [
            '구성비는 확정 구역계 면적 대비이며, 지목·소유·필지면적·지가·형상 구분은 대상지에 편입된 도형면적을 집계합니다.',
            '면적차이는 확정 구역계 면적에서 주제도형의 합집합 면적을 뺀 값입니다. 좌표계·원자료 수록범위 차이가 포함될 수 있습니다.',
        ]
        if not category_complete:
            table['notes'].append('분류 미확보 면적이 포함되어 있습니다. 미확보를 특정 분류로 추정하지 않습니다.')
        if not gap_ok:
            table['notes'].append('구역계와 주제도형 집계면적이 일치하지 않습니다. 누락 필지·도형 및 좌표계를 확인해야 합니다.')
        if not overlap_ok:
            table['notes'].append('서로 다른 분류가 중첩됩니다. 분류별 면적의 단순 합산은 전체 면적과 다를 수 있습니다.')
        if item_id == '2-3':
            table['notes'].append('면적 구간은 필지의 공부면적 기준이며, 표의 면적은 대상지 편입 도형면적입니다.')
        if item_id == '2-4':
            table['notes'].append('지가 구간은 기준연도 개별공시지가(원/㎡)이며, 표의 면적은 대상지 편입 도형면적입니다.')
        if item_id == '2-5':
            table['notes'].append('토지특성 형상 분류는 사업별 법정 부정형 필지 판정과 다릅니다.')
        if item_id == '2-1':
            table['notes'].append('소유구분은 토지대장 분류로서 등기상 개인별 소유자 확인을 대체하지 않습니다.')
        if item_id == '2-6':
            table['notes'].append('도형수는 토지피복 분류도형 건수이며 필지 수가 아닙니다. WMS 이미지는 면적 산정에서 제외합니다.')
        if item.get('provider_verified') is not True:
            table['notes'].append('검증된 전체 원자료가 아니므로 잠정 집계이며 최종 현황으로 확정할 수 없습니다.')
