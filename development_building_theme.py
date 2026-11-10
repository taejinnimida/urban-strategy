"""Building survey maps: plot every official footprint, classify only verified attributes.

VWorld building outlines and building-register rows are independent source FACTs.
Lack of a register join does not suppress a building from maps or CAD.
"""
import hashlib
import math
from collections import Counter

from shapely.geometry import shape, mapping

FIELDS = {'3-1': 'use', '3-2': 'structure', '3-3': 'floors', '3-4': 'age',
          '3-5': 'floor_area', '3-6': 'coverage', '3-7': 'far'}
TITLES = {'3-1': '용도별', '3-2': '구조별', '3-3': '층수별',
          '3-4': '경과연도별', '3-5': '연면적별', '3-6': '건폐율별', '3-7': '용적률별'}
BINS = {
    '3-3': ('1층', '2층', '3층', '4층', '5층 이상'),
    '3-4': ('20년 미만', '20~30년', '30~40년', '40년 이상'),
    '3-5': ('60㎡ 미만', '60~85㎡', '85~115㎡', '115~165㎡', '165~295㎡', '295㎡ 이상'),
    # Preserve the sample's intervals and add an underflow/overflow class, if needed.
    '3-6': ('20% 미만', '20~40%', '40~70%', '70% 이상'),
    '3-7': ('100% 미만', '100~200%', '200~300%', '300% 이상'),
}
OTHER = '기타(값 없음 등)'
UNKNOWN = '대장 미확인'
PALETTE = ('#4477aa', '#ee8866', '#228833', '#aa3377', '#66ccee',
           '#ccbb44', '#8844aa', '#44aa99', '#bb6677', '#999933')


def _classify(item_id, value):
    if value is None or value == '':
        return OTHER
    if item_id in ('3-1', '3-2'):
        text = str(value).strip()
        return text if text and text not in ('0', '0.0', 'None') else OTHER
    try:
        number = float(value)
        if not math.isfinite(number):
            return OTHER
    except (ValueError, TypeError):
        return OTHER
    if item_id == '3-3':
        return (f'{int(number)}층' if number in (1, 2, 3, 4)
                else '5층 이상' if number >= 5 and number.is_integer() else OTHER)
    if item_id == '3-4':
        if number < 0:
            return OTHER
        return ('20년 미만' if number < 20 else '20~30년' if number < 30
                else '30~40년' if number < 40 else '40년 이상')
    if number <= 0:
        return OTHER
    if item_id == '3-5':
        bounds = (60, 85, 115, 165, 295)
        return next((name for limit, name in zip(bounds, BINS[item_id])
                     if number < limit), '295㎡ 이상')
    if item_id == '3-6':
        return ('20% 미만' if number < 20 else '20~40%' if number < 40
                else '40~70%' if number < 70 else '70% 이상')
    if item_id == '3-7':
        return ('100% 미만' if number < 100 else '100~200%' if number < 200
                else '200~300%' if number < 300 else '300% 이상')
    return OTHER


# VWorld GIS-building metadata varies by provider/version: only explicitly
# present named attributes are used. No area / floor / age is inferred.
GIS_FIELDS = {
    'use': ('mainpurpscdnm', 'main_prpos_code_nm', 'mainprposcodenm', 'main_prpos_nm', 'mainpurpsnm'),
    'structure': ('strctcdnm', 'strct_cd_nm', 'structure_nm'),
    'floors': ('grndflrcnt', 'grnd_flr_cnt', 'ground_floor_count'),
    'floor_area': ('totarea', 'tot_area', 'total_floor_area'),
    'coverage': ('bcrat', 'bc_rat'),
    'far': ('vlrat', 'vl_rat'),
}
AGE_DATES = ('useaprday', 'use_apr_day', 'useconfmde', 'use_confm_de')


def _gis_value(field, props, as_of):
    names = GIS_FIELDS.get(field, ())
    for name in names:
        value = props.get(name)
        if value is not None and str(value).strip():
            return value
    if field == 'age':
        from datetime import date
        year = int(str(as_of)[:4])
        for name in AGE_DATES:
            value = str(props.get(name) or '').strip().replace('-', '').replace('.', '')
            if len(value) == 8 and value.isdigit():
                try:
                    day = date(int(value[:4]), int(value[4:6]), int(value[6:8]))
                    today = date.fromisoformat(str(as_of)[:10])
                    return max(0, today.year - day.year - ((today.month, today.day) < (day.month, day.day)))
                except ValueError:
                    pass
    return None


def attach_building_themes(data, snapshot):
    """Attach seven thematic map layers without changing ledger inventory statistics."""
    layers = snapshot.get('_report_layers', {})
    integrated = layers.get('LT_C_BLDGINFO') or {}
    secondary = layers.get('LT_C_SPBD') or {}
    # Use GIS-building integrated information first. Only when it has no usable
    # polygons may the independent address-building footprint serve as fallback.
    site = shape(snapshot['geometry'])
    by_id = {str(r['id']).strip(): r for r in data['buildings'] if r.get('id')}
    footprints, skipped = [], 0
    chosen, layer_name = None, None
    for name, candidate in (('LT_C_BLDGINFO', integrated), ('LT_C_SPBD', secondary)):
        if candidate.get('status') not in ('COMPLETE', 'REFERENCE'):
            continue
        shapes = []
        local_skipped = 0
        for feature in candidate.get('features') or []:
            try:
                geom = shape(feature['geometry'])
                if geom.is_empty or not geom.is_valid or geom.geom_type not in ('Polygon', 'MultiPolygon'):
                    local_skipped += 1
                    continue
                clipped = geom.intersection(site)
                if clipped.is_empty or clipped.area <= 0:
                    continue
                props = {str(k).lower(): v for k, v in (feature.get('properties') or {}).items()}
                mgm = str(props.get('mgmbldrgstpk') or props.get('mgm_bldrgst_pk') or '').strip()
                matched = by_id.get(mgm) if mgm else None
                shapes.append({'geometry': mapping(clipped), 'ledger': matched,
                               'props': props, 'layer_name': name,
                               'join_status': 'EXACT_ID' if matched else 'UNMATCHED'})
            except (TypeError, ValueError, KeyError):
                local_skipped += 1
        # A confirmed 0-result is meaningful only if no other source has shapes.
        if shapes:
            chosen, layer_name = candidate, name
            footprints, skipped = shapes, local_skipped
            break
        if chosen is None:
            chosen, layer_name, skipped = candidate, name, local_skipped
    fetched = chosen is not None
    verified = fetched and chosen.get('status') == 'COMPLETE'
    if not footprints and fetched and layer_name == 'LT_C_BLDGINFO' and secondary.get('status') in ('COMPLETE', 'REFERENCE'):
        # Keep an official empty response distinct from an unavailable one.
        # Alternative source shapes are considered on the next refresh.
        pass

    n = len(footprints)
    matches = sum(1 for f in footprints if f['ledger'])
    for item in data['items']:
        item_id = item['id']
        field = FIELDS.get(item_id)
        if field is None:
            continue
        if not fetched:
            item.update(features=[], provider_verified=False, geometry_status='UNAVAILABLE',
                        detail=(item.get('detail') or '') + ' · 건축물 공간도형 미확보')
            continue

        mapped = []
        for f in footprints:
            value = None
            if f['layer_name'] == 'LT_C_BLDGINFO':
                value = _gis_value(field, f['props'], data.get('as_of'))
            if value is None and f['ledger']:
                value = f['ledger'].get(field)
            label = _classify(item_id, value) if value is not None else (OTHER if f['ledger'] else UNKNOWN)
            mapped.append(label)
        totals = Counter(mapped)

        fixed = list(BINS.get(item_id, ()))
        if fixed:
            order = fixed + ([OTHER] if totals.get(OTHER) else []) + ([UNKNOWN] if totals.get(UNKNOWN) else [])
        else:
            order = sorted((k for k in totals if k not in (OTHER, UNKNOWN)),
                           key=lambda k: (-totals[k], k))
            order += ([OTHER] if totals.get(OTHER) else []) + ([UNKNOWN] if totals.get(UNKNOWN) else [])

        classes = {}
        for i, label in enumerate(order):
            if label == UNKNOWN:
                color, code = '#a1a1aa', 'C99'
            elif label == OTHER:
                color, code = '#d6d3d1', 'C98'
            else:
                color = PALETTE[i % len(PALETTE)]
                code = 'C' + str(min(i + 1, 97)).zfill(2)
            classes[label] = {'name': label, 'class_code': code,
                              'color': color, 'count': totals[label],
                              'pct': round(totals[label] / n * 100, 1) if n else 0.0}

        features = []
        for f, label in zip(footprints, mapped):
            meta = classes[label]
            features.append({'type': 'Feature', 'geometry': f['geometry'],
                             'properties': {'name': label, 'class_code': meta['class_code'],
                                            'color': meta['color'],
                                            'join_status': f['join_status']}})

        rows = [{'kind': 'total', 'label': '합계', 'count': n,
                 'pct': 100.0 if n else 0.0,
                 'join_status': '도형 기준', 'note': '건축물 공간도형 수'}]
        for label in order:
            meta = classes[label]
            rows.append({'kind': 'category', 'label': label, 'count': meta['count'],
                         'pct': meta['pct'], 'join_status': (
                             '속성 미확인' if label == UNKNOWN else
                             '항목값 없음' if label == OTHER else '공식 속성자료'),
                         'note': ''})
        note = ('VWorld 건축물 외곽선 기준. 건물도형 하나를 건축물 한 동으로 최종 확정하지 않음. '
                '공간도형과 건축물대장 관리번호가 일치하지 않으면 대장 미확인으로 별도 집계. '
                '미확인 건축물을 무허가 또는 철거 건축물로 단정하지 않음.')
        if not n:
            note += ' 건축물 도형 조회 결과 0건으로, 실제 건축물 부존재를 보증하지 않음.'
        if layer_name == 'LT_C_SPBD':
            note += ' 도로명주소 건물도형은 속성 확인되지 않으면 미확인으로 분류.'
        if skipped:
            note += f' 비정상 도형 {skipped}건 표시 제외(검증 필요).'
        item.update(
            features=features,
            legend=[classes[label] for label in order if totals[label] > 0],
            thematic_type='building', provider_verified=verified,
            geometry_status='COMPLETE' if verified else 'REFERENCE',
            source=('VWorld GIS건물통합정보' if layer_name == 'LT_C_BLDGINFO' else 'VWorld 도로명주소 건물도형(보조)') + ' · 건축HUB',
            status='공간자료 확보' if verified else '보조 조회 참고자료',
            detail=(item.get('detail') or '') +
                   f' · 도형 {n}개, 대장 관리번호 직접연계 {matches}개, 미연계 {n-matches}개 · 도형출처 {layer_name}'
                   + ' · ' + note,
            survey_table={
                'title': f'[표 {item_id} {TITLES[item_id]} 현황]',
                'status': 'COMPLETE' if verified else 'REFERENCE',
                'rows': rows, 'source': item['source'],
                'as_of': data.get('as_of'), 'notes': [note],
            },
        )
        stat = data.get('statistics', {}).get(item_id)
        if isinstance(stat, dict):
            stat.update(footprint_count=n, verified_ledger_joins=matches,
                        unmatched_footprints=n - matches, invalid_features=skipped,
                        footprint_status=item['geometry_status'],
                        mapped_groups=[classes[label] for label in order],
                        mapped_total=n, mapped_unit='건축물 공간도형(개)',
                        note=(stat.get('note') or '') + ' ' + note)
