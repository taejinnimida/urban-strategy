"""Building survey maps: registry statistics with verified GIS footprint themes.

Only VWorld GIS-building footprints directly identified by a building-register
management key can be shaded. A cadastral PNU is a parcel, not a footprint.
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
UNKNOWN = '대장·도형 미연계'
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
    """Keep the legal register table and the GIS footprint drawing independent.

    Only LT_C_BLDGINFO provides building geometry. Every accepted footprint
    is drawn, while the legend states how its thematic attribute was obtained.
    PNU cadastral parcels are *never* shaded as buildings.
    """
    from collections import defaultdict
    from development_building_fact import assemble_building_fact

    fact = assemble_building_fact(data, snapshot)
    data['building_fact'] = fact
    registers = list(data.get('buildings') or [])
    ledger_keys = [(str(row.get('id') or '').strip() or
                    'UNKEYED_REGISTER_ROW:' + str(i))
                   for i, row in enumerate(registers)]
    registered = dict(zip(ledger_keys, registers))
    n = len(registers)
    source_shapes = [r for r in fact['records']
                     if r.get('geometry_source') == 'LT_C_BLDGINFO']
    parcel_records = fact.get('parcel_register_records') or []

    direct_by_key = defaultdict(list)
    provisional_by_key = defaultdict(list)
    for record in source_shapes:
        key = record.get('register_row_key') or record.get('register_id')
        if key not in registered:
            continue
        if record.get('join_status') == 'EXACT_ID':
            direct_by_key[key].append(record)
        elif record.get('join_status') == 'PARCEL_SPATIAL_REFERENCE':
            provisional_by_key[key].append(record)
    ambiguous = {key for key, records in direct_by_key.items() if len(records) != 1}
    direct_keys = {key for key, records in direct_by_key.items() if len(records) == 1}
    provisional_keys = {key for key, records in provisional_by_key.items()
                        if len(records) == 1 and key not in direct_keys}
    mapped_keys = direct_keys | provisional_keys

    # The building-register row count is the table denominator. GIS outlines
    # are map observations, not a count of legally extant buildings.
    fact['counts']['ledger_survey_rows'] = n
    fact['counts']['ledger_survey_mapped'] = len(mapped_keys)
    fact['counts']['source_gis_footprints_drawn'] = len(source_shapes)
    fact['counts']['direct_geometry_titles'] = len(direct_keys)
    fact['counts']['provisional_geometry_titles'] = len(provisional_keys)
    fact['counts']['unlinked_gis_footprints_drawn'] = sum(
        r.get('join_status') == 'UNMATCHED' for r in source_shapes)
    fact['counts']['ambiguous_ledger_geometry_hidden'] = len(ambiguous)
    fact['display_exclusion'] = {
        'reason': 'CADASTRAL_PARCELS_ARE_NOT_BUILDING_FOOTPRINTS',
        'source_shapes_hidden': 0,
        'source_shapes_drawn': len(source_shapes),
        'cadastral_parcels_excluded_from_building_theme': len(parcel_records),
        'ambiguous_registers': len(ambiguous),
        'historical_status': 'UNCONFIRMED',
        'note': ('GIS건물통합정보 건물 외곽선은 출처 그대로 표시하되, 현재 건축물 '
                 '존재·멸실 여부와 대장 일치 여부는 별도 확인 필요. '
                 '연속지적 필지는 건축물 외곽선으로 대체하지 않음.'),
    }

    for item in data['items']:
        item_id = item['id']
        field = FIELDS.get(item_id)
        if not field:
            continue
        ledger_labels = [_classify(item_id, row.get(field)) for row in registers]
        ledger_totals = Counter(ledger_labels)
        ordered = list(BINS.get(item_id) or [])
        if not ordered:
            ordered = sorted((k for k in ledger_totals if k != OTHER),
                             key=lambda k: (-ledger_totals[k], k))
        if ledger_totals.get(OTHER):
            ordered.append(OTHER)

        categories = {}
        def category_for(label):
            if label not in categories:
                if label in (OTHER, UNKNOWN):
                    code, color = ('C98', '#d6d3d1') if label == OTHER else ('C99', '#b8c0c9')
                else:
                    index = sum(k not in (OTHER, UNKNOWN) for k in categories)
                    code, color = f'C{index + 1:02d}', PALETTE[index % len(PALETTE)]
                categories[label] = {'name': label, 'class_code': code,
                                     'color': color,
                                     'count': ledger_totals.get(label, 0),
                                     'pct': round(100 * ledger_totals.get(label, 0) / n, 1)
                                            if n else 0}
            return categories[label]
        for label in ordered:
            category_for(label)

        features = []
        map_totals = Counter()
        join_totals = Counter()
        for record in source_shapes:
            key = record.get('register_row_key') or record.get('register_id')
            joined = record.get('join_status')
            if joined == 'EXACT_ID' and key in direct_keys:
                row = registered[key]
                value = row.get(field)
                basis = row.get(field + '_basis') if field in ('coverage', 'far') else None
                assurance = ('INTERNAL_CALC' if basis and basis != '건축HUB 원문'
                             else 'DIRECT_LEDGER')
                display_join = 'EXACT_ID'
            elif joined == 'PARCEL_SPATIAL_REFERENCE' and key in provisional_keys:
                row = registered[key]
                value = row.get(field)
                if value is None or value == '':
                    value = (record.get('attributes') or {}).get(field, {}).get('value')
                basis = row.get(field + '_basis') if field in ('coverage', 'far') else None
                assurance = ('INTERNAL_CALC_PROVISIONAL'
                             if basis and basis != '건축HUB 원문'
                             else 'PROVISIONAL_PNU')
                display_join = 'PARCEL_SPATIAL_REFERENCE'
            else:
                field_record = (record.get('attributes') or {}).get(field) or {}
                value = field_record.get('value') if (
                    field_record.get('source') == 'GIS_BUILDING') else None
                assurance = 'GIS_ATTRIBUTE' if value is not None else 'UNKNOWN'
                display_join = 'GIS_ONLY'
            label = _classify(item_id, value) if assurance != 'UNKNOWN' else UNKNOWN
            category = category_for(label)
            properties = {
                'name': label, 'color': category['color'],
                'class_code': category['class_code'],
                'join_status': display_join,
                'source_join_status': joined,
                'geometry_basis': 'BUILDING_FOOTPRINT',
                'geometry_source': 'LT_C_BLDGINFO',
                'quality_grade': record.get('quality_grade'),
                'attribute_assurance': assurance,
                'join_basis': record.get('match_basis'),
                'source': 'GIS_BUILDING',
                'building_fact_id': record.get('id'),
                'register_id': key if display_join != 'GIS_ONLY' else None,
            }
            features.append({'type': 'Feature', 'geometry': record['geometry'],
                             'properties': properties})
            map_totals[label] += 1
            join_totals[display_join] += 1

        rows = [{
            'kind': 'total', 'label': '합계', 'count': n,
            'pct': 100.0 if n else 0.0, 'join_status': 'BUILDING_REGISTER',
            'note': (f'건축물조서 {n}건 기준 · GIS건물통합정보 외곽선 '
                     f'{len(features)}개 별도 표시(직접 {join_totals["EXACT_ID"]} · '
                     f'PNU 잠정 {join_totals["PARCEL_SPATIAL_REFERENCE"]} · '
                     f'GIS 단독 {join_totals["GIS_ONLY"]}) · '
                     'GIS 도형 수는 실제 건축물 동수와 다를 수 있음'),
        }]
        for label in ordered:
            category = category_for(label)
            rows.append({
                'kind': 'category', 'label': label,
                'count': category['count'], 'pct': category['pct'],
                'join_status': 'BUILDING_REGISTER',
                'note': (f'대장 {category["count"]}건 · GIS 현황도 분류 {map_totals[label]}개'
                         + (' · 대장값 없음/미확보' if label == OTHER else '')
                         + (' · 건폐율·용적률 내부산정은 법정 확정치 아님'
                            if field in ('coverage', 'far') else '')),
            })

        note = (
            f'통계는 건축물조서 {n}건, 현황도는 GIS건물통합정보 건물외곽선 '
            f'{len(features)}개를 별도 단위로 작성. 색상은 대장관리번호 직접일치, '
            'PNU에 의한 잠정연계, GIS 원천속성 순으로 분류하며 '
            '속성 미확보 건물은 회색으로 표시. PNU 지적 필지는 색칠하지 않음. '
            'GIS 건물도형의 최신성·멸실 여부는 자동 확정하지 않으며 '
            '대장 수치와 도형 수가 달라도 임의로 일치시키지 않음. '
            '건폐율·용적률 내부산정은 법정 확정치와 구분.'
        )
        if ambiguous:
            note += f' 동일관리번호 중복 GIS 후보 {len(ambiguous)}개 관리번호는 직접연계로 인정하지 않음.'
        if n > len(mapped_keys):
            note += f' 대장 {n-len(mapped_keys)}건은 GIS 외곽선과 직접·잠정 연계 미확인.'
        if fact['geometry_status'] != 'COMPLETE':
            note += ' GIS 자료는 부분·보조 수신일 수 있어 전체 외곽선 확보를 보장하지 않음.'

        gis_complete = fact['source_info'].get('LT_C_BLDGINFO', {}).get('status') == 'COMPLETE'
        full_match = bool(features) and gis_complete and len(features) == n and (
            join_totals['EXACT_ID'] == len(features))
        geometry_status = 'COMPLETE' if full_match else (
            'PARTIAL' if features else 'UNAVAILABLE')
        sources = ('국토교통부 건축HUB 대장 통계 · '
                   'VWorld GIS건물통합정보 건물 외곽선 · PNU 필지 대체표시 없음')
        item.update(
            features=features,
            legend=[{**categories[label], 'count': map_totals[label],
                     'pct': round(100 * map_totals[label] / len(features), 1)
                            if features else 0,
                     'unit': 'GIS건물도형'}
                    for label in categories if map_totals[label]],
            thematic_type='building',
            provider_verified=bool(features) and gis_complete,
            cad_exportable=bool(features) and gis_complete,
            geometry_status=geometry_status,
            source=sources,
            status=('공간자료 확보' if geometry_status == 'COMPLETE'
                    else '일부 공간자료 확보' if features else
                    '대장 통계 확보·도면 미연계' if n else '대장·도형 미확보'),
            detail=(item.get('detail') or '') + ' · ' + note,
            survey_table={'title': f'[표 {item_id} {TITLES[item_id]} 현황]',
                          'status': 'COMPLETE' if n and not ledger_totals.get(OTHER) else 'PARTIAL',
                          'rows': rows, 'source': sources, 'as_of': data.get('as_of'),
                          'notes': [note]},
        )
        stat = data.get('statistics', {}).get(item_id)
        if isinstance(stat, dict):
            stat.update(
                register_count=n,
                source_footprint_records=len(source_shapes),
                footprint_count=len(source_shapes),
                pnu_register_parcel_features=len(parcel_records),
                cadastral_parcels_excluded_from_theme=len(parcel_records),
                displayed_registered_features=len(mapped_keys),
                displayed_gis_footprints=len(features),
                hidden_unmatched_source_footprints=0,
                ambiguous_registered_geometries=len(ambiguous),
                verified_ledger_joins=join_totals['EXACT_ID'],
                provisional_spatial_joins=join_totals['PARCEL_SPATIAL_REFERENCE'],
                gis_only_footprints=join_totals['GIS_ONLY'],
                footprint_status=fact['geometry_status'],
                mapped_groups=[{**categories[label], 'count': map_totals[label]}
                               for label in categories if map_totals[label]],
                mapped_total=len(features),
                mapped_unit='VWorld GIS건물통합정보 건물외곽선(개)',
                survey_total=n,
                note=(stat.get('note') or '') + ' ' + note,
            )
