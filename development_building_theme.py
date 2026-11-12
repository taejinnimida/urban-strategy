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
    """Ledger-first survey: registered buildings are statistical units.

    The GIS observation set is retained for diagnostics only. Unlinked
    footprints may represent demolition history, duplicate source polygons,
    ancillary structures, or valid unregistered buildings; none can be
    declared current OR demolished solely from an unmatched spatial shape.
    """
    from collections import defaultdict
    from development_building_fact import assemble_building_fact

    fact = assemble_building_fact(data, snapshot)
    data['building_fact'] = fact
    registers = list(data.get('buildings') or [])
    source_shapes = fact['records']
    parcel_records = fact.get('parcel_register_records') or []
    ledger_keys = [(str(row.get('id') or '').strip() or
                    'UNKEYED_REGISTER_ROW:' + str(i)) for i, row in enumerate(registers)]
    registered = dict(zip(ledger_keys, registers))
    n = len(registers)

    # Suppress source-only polygons from the statutory-looking survey map.
    # Keep them in building_fact.records for independent quality review.
    # A footprint with only a spatial PNU association could be a historic,
    # demolished shape. Do not draw it as the *current* building. Only true
    # register management-ID equality establishes a footprint identity.
    from development_building_fact import _parcel_geometry_index, _register_pnus
    site = shape(snapshot['geometry'])
    cadastral_layer = (snapshot.get('_report_layers') or {}).get('LP_PA_CBND_BUBUN') or {}
    parcels = _parcel_geometry_index(cadastral_layer, site)
    pnu_to_titles = defaultdict(set)
    single_pnu_by_title = {}
    for key, row in registered.items():
        pnus = _register_pnus(row.get('pnus'))
        if len(pnus) == 1:
            pnu = next(iter(pnus))
            single_pnu_by_title[key] = pnu
            pnu_to_titles[pnu].add(key)

    candidate_by_title = defaultdict(list)
    for r in source_shapes:
        if r.get('join_status') != 'EXACT_ID':
            continue
        key = r.get('register_row_key') or r.get('register_id')
        if key in registered:
            candidate_by_title[key].append(r)

    map_by_title = {}
    ambiguous_titles = []
    for key in ledger_keys:
        exact = candidate_by_title.get(key, [])
        if len(exact) > 1:
            ambiguous_titles.append(key)
            continue
        if len(exact) == 1:
            map_by_title[key] = exact[0]
            continue
        # 1:1 register–PNU and available cadastral geometry gives a reliable
        # *location*, not proof of a particular building outline.
        pnu = single_pnu_by_title.get(key)
        if pnu and pnu in parcels and len(pnu_to_titles[pnu]) == 1:
            map_by_title[key] = {
                'id': 'CADASTRAL_PNU:' + pnu,
                'geometry': mapping(parcels[pnu]),
                'join_status': 'PNU_PARCEL_REGISTER',
                'geometry_basis': 'CADASTRAL_PNU',
                'quality_grade': 'P_PARCEL_LEDGER',
                'match_basis': 'EXACT_CADASTRAL_PNU_NOT_BUILDING_FOOTPRINT',
                'spatial_parcel_pnu': pnu,
                'spatial_overlap_pct': None
            }

    map_records = [(key, registered[key], map_by_title[key])
                   for key in ledger_keys if key in map_by_title]
    hidden_gis = [r for r in source_shapes if
                  r.get('join_status') != 'EXACT_ID']
    fact['counts']['ledger_survey_rows'] = n
    fact['counts']['ledger_survey_mapped'] = len(map_records)
    fact['counts']['unmatched_gis_hidden_from_survey'] = len(hidden_gis)
    fact['counts']['ambiguous_ledger_geometry_hidden'] = len(ambiguous_titles)
    fact['display_exclusion'] = {
        'reason': 'NON_EXACT_GIS_NOT_PROOF_OF_CURRENT_BUILDING',
        'source_shapes_hidden': len(hidden_gis),
        'ambiguous_registers': len(ambiguous_titles),
        'historical_status': 'UNCONFIRMED',
        'provisional_footprints_excluded': sum(r.get('join_status') == 'PARCEL_SPATIAL_REFERENCE' for r in source_shapes),
        'note': '원천 GIS를 삭제하지 않고 현황도에서만 제외. 멸실 여부는 말소대장·공부와 현장자료로 확인.'
    }

    for item in data['items']:
        item_id = item['id']
        field = FIELDS.get(item_id)
        if not field:
            continue

        # One row in 건축물조서 is one statistical observation. Even where
        # polygons are unavailable, registered attributes remain countable.
        ledger_labels = [_classify(item_id, row.get(field)) for row in registers]
        totals = Counter(ledger_labels)
        ordered = list(BINS.get(item_id) or [])
        if not ordered:
            ordered = sorted((k for k in totals if k != OTHER),
                             key=lambda k: (-totals[k], k))
        if totals.get(OTHER):
            ordered.append(OTHER)
        categories = {}
        for index, label in enumerate(ordered):
            if label == OTHER:
                code, color = 'C98', '#d6d3d1'
            else:
                code, color = f'C{index + 1:02d}', PALETTE[index % len(PALETTE)]
            categories[label] = {
                'name': label, 'class_code': code, 'color': color,
                'count': totals.get(label, 0),
                'pct': round(100 * totals.get(label, 0) / n, 1) if n else 0
            }

        features = []
        feature_labels = Counter()
        for key, ledger, record in map_records:
            label = _classify(item_id, ledger.get(field))
            category = categories[label]
            basis = ledger.get(field + '_basis') if field in ('coverage', 'far') else None
            derived = bool(basis and basis != '건축HUB 원문')
            is_parcel = record.get('join_status') == 'PNU_PARCEL_REGISTER'
            features.append({
                'type': 'Feature', 'geometry': record['geometry'],
                'properties': {
                    'name': label, 'color': category['color'],
                    'class_code': category['class_code'],
                    'join_status': record.get('join_status'),
                    'geometry_basis': 'CADASTRAL_PNU' if is_parcel else 'BUILDING_FOOTPRINT',
                    'quality_grade': record.get('quality_grade'),
                    'attribute_assurance': ('INTERNAL_CALC_PARCEL' if is_parcel else
                                            'INTERNAL_CALC') if derived else 'DIRECT_LEDGER',
                    'join_basis': record.get('match_basis'),
                    'spatial_parcel_pnu': record.get('spatial_parcel_pnu'),
                    'spatial_overlap_pct': record.get('spatial_overlap_pct'),
                    'source': 'PLATFORM_DERIVED' if derived else 'BUILDING_REGISTER',
                    'building_fact_id': record.get('id'),
                    'register_id': ledger.get('id'),
                }
            })
            feature_labels[label] += 1

        rows = [{
            'kind': 'total', 'label': '합계', 'count': n,
            'pct': 100.0 if n else 0.0, 'join_status': 'BUILDING_REGISTER',
            'note': (f'건축물조서 {n}건 기준 · 현황도에 표시 가능한 대장기반 도형 '
                     f'{len(features)}개(건물외곽선과 PNU 필지도형 구분) · '
                     f'원천 GIS 도형 수는 건축물 동수가 아님')
        }]
        for label in ordered:
            category = categories[label]
            mapped_count = feature_labels[label]
            rows.append({
                'kind': 'category', 'label': label,
                'count': category['count'], 'pct': category['pct'],
                'join_status': 'BUILDING_REGISTER',
                'note': (f'대장 {category["count"]}건 · 현황도 표시 {mapped_count}개'
                         + (' · 대장값 없음/미확보' if label == OTHER else '')
                         + (' · 건폐율·용적률은 대장 원문과 내부산정 구분'
                            if field in ('coverage', 'far') else ''))
            })

        note = (
            f'건축물조서 {n}건을 모집단으로 집계. 지도는 건축물조서와 연결 가능한 '
            f'{len(features)}개 도형만 표시(대장 연계 건물도형·대장 PNU 필지대체 포함). '
            f'건물관리번호가 직접 연결되지 않은 GIS 도형 {len(hidden_gis)}개는 '
            '현황도·CAD에서 제외하고 검증정보에만 보관. '
            '이들은 멸실 확정이 아니라 최신성·중복 여부 미확인 도형임. '
            '건축물대장 없는 건축물의 실제 부존재를 뜻하지 않음. '
            '건폐율·용적률 플랫폼 내부산정은 법정 확정치와 구별.'
        )
        if ambiguous_titles:
            note += f' 대장 {len(ambiguous_titles)}건의 동일관리번호 중복후보는 도면 표시 보류.'
        if len(features) != n:
            note += f' 대장 {n-len(features)}건은 통계에 포함하되 표시할 확정 공간도형이 없음.'
        if fact['geometry_status'] != 'COMPLETE':
            note += ' GIS 자료는 일부·보조 수신일 수 있으며 전체 건물도형 확보를 보장하지 않음.'

        mapped_geometry_status = (
            'COMPLETE' if n and len(features) == n and
            fact['geometry_status'] == 'COMPLETE' and
            not any(rec.get('join_status') == 'PNU_PARCEL_REGISTER'
                    for _, _, rec in map_records) else
            'PARTIAL' if features else 'UNAVAILABLE'
        )
        sources = '국토교통부 건축HUB 대장 기준 · VWorld 건물도형·연속지적 위치참고'
        item.update(
            features=features,
            legend=[categories[k] for k in ordered if totals.get(k)],
            thematic_type='building',
            provider_verified=bool(features) and fact['provider_verified'],
            cad_exportable=bool(features) and (
                any(v.get('status') == 'COMPLETE'
                    for v in fact['source_info'].values()) or bool(parcel_records)),
            geometry_status=mapped_geometry_status,
            source=sources,
            status='공간자료 확보' if mapped_geometry_status == 'COMPLETE' else
                   '일부 공간자료 확보' if features else
                   '대장 통계 확보·도면 미연계' if n else '대장·도형 미확보',
            detail=(item.get('detail') or '') + ' · ' + note,
            survey_table={
                'title': f'[표 {item_id} {TITLES[item_id]} 현황]',
                'status': 'COMPLETE' if n and not totals.get(OTHER) else 'PARTIAL',
                'rows': rows, 'source': sources, 'as_of': data.get('as_of'),
                'notes': [note]
            })
        stat = data.get('statistics', {}).get(item_id)
        if isinstance(stat, dict):
            stat.update(
                register_count=n,
                source_footprint_records=len(source_shapes),
                footprint_count=len(source_shapes),
                pnu_register_parcel_features=len(parcel_records),
                displayed_registered_features=len(features),
                hidden_unmatched_source_footprints=len(hidden_gis),
                ambiguous_registered_geometries=len(ambiguous_titles),
                verified_ledger_joins=fact['counts']['direct_register_matches'],
                provisional_spatial_joins=fact['counts']['spatial_register_candidates'],
                footprint_status=fact['geometry_status'],
                mapped_groups=[categories[k] for k in ordered if totals.get(k)],
                mapped_total=len(features),
                mapped_unit='대장기반 현황도 표시단위(건물외곽선 또는 PNU 필지대체)',
                survey_total=n,
                note=(stat.get('note') or '') + ' ' + note
            )
