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


def _parcel_register_matches(source_shapes, registers, keys):
    """Assign GIS-building outlines by related PNU without imposing 1:1 counts.

    The register remains authoritative for building statistics. A footprint is
    only a map candidate if it belongs to a PNU related to a register title.
    For a single title, largest outline is the main building and remaining
    outlines are auxiliary. For multiple titles, explicit building IDs and
    unique GIS attribute agreements establish individual title matching;
    otherwise the PNU group is preserved without inventing a title identity.
    Extra unverified standalone outlines are not automatically counted.
    """
    from collections import defaultdict
    from development_building_fact import _register_pnus

    keyed = dict(zip(keys, registers))
    by_pnu = defaultdict(set)
    for key, row in keyed.items():
        for pnu in _register_pnus(row.get('pnus')):
            by_pnu[pnu].add(key)
    groups = defaultdict(list)
    omitted = []
    for record in source_shapes:
        pnu = record.get('spatial_parcel_pnu') or record.get('provider_pnu')
        if not pnu and record.get('join_status') == 'EXACT_ID':
            key = record.get('register_row_key') or record.get('register_id')
            if key in keyed:
                pnus = _register_pnus(keyed[key].get('pnus'))
                if len(pnus) == 1:
                    pnu = next(iter(pnus))
        if pnu and pnu in by_pnu:
            groups[pnu].append(record)
        else:
            omitted.append({'record': record, 'reason': 'NO_REGISTER_PNU'
                            if pnu else 'NO_SPATIAL_PNU'})
    assigned = []
    def geom_area(r):
        try:
            return shape(r['geometry']).area
        except (TypeError, KeyError, ValueError):
            return 0
    def gis_field(r, field):
        v=(r.get('attributes') or {}).get(field) or {}
        if v.get('source') in ('GIS_BUILDING','GIS+BUILDING_REGISTER'):
            return v.get('value')
        if v.get('gis_value') is not None:
            return v['gis_value']
        return None
    for pnu, records in groups.items():
        title_keys=sorted(by_pnu[pnu])
        ordered=sorted(records, key=lambda r:(-geom_area(r),r['id']))
        if len(title_keys)==1:
            key=title_keys[0]
            exact=[r for r in ordered if r.get('join_status')=='EXACT_ID' and
                   (r.get('register_row_key') or r.get('register_id'))==key]
            main=exact[0] if exact else ordered[0]
            for r in ordered:
                assigned.append({'record':r,'ledger_key':key,'pnu':pnu,
                                 'role':'MAIN' if r is main else 'AUXILIARY',
                                 'assurance':'EXACT_ID' if r in exact else 'PNU_GROUP',
                                 'title_count':1})
            continue
        available=set(title_keys)
        matched={}
        for r in ordered:
            key=r.get('register_row_key') or r.get('register_id')
            if r.get('join_status')=='EXACT_ID' and key in available:
                matched[r['id']]=key
                available.remove(key)
        # Unambiguous attribute matches only: do not use order or parcel
        # number to invent which building-register title a GIS polygon is.
        for r in ordered:
            if r['id'] in matched or not available:
                continue
            scores={}
            for key in sorted(available):
                ledger=keyed[key]
                score=0
                evidence=0
                for fld,weight in (('use',4),('structure',2),('floors',3),('floor_area',2)):
                    gv=gis_field(r,fld);tv=ledger.get(fld)
                    if gv is None or tv is None:
                        continue
                    evidence+=1
                    if fld in ('use','structure'):
                        if str(gv).replace(' ','')==str(tv).replace(' ',''):
                            score+=weight
                    else:
                        try:
                            v=float(gv);t=float(tv)
                            if abs(v-t)<=max(1,abs(t)*.12):score+=weight
                        except (ValueError,TypeError):
                            pass
                if evidence:
                    scores[key]=score
            if scores:
                best=max(scores.values())
                winners=[k for k,v in scores.items() if v==best]
                if best>=4 and len(winners)==1:
                    key=winners[0]
                    matched[r['id']]=key
                    available.remove(key)
        # Known PNU group, with ambiguous individual records:
        # retain up to the remaining number of registered building titles as
        # candidates; only smaller leftovers are plausible auxiliary buildings.
        unresolved=[r for r in ordered if r['id'] not in matched]
        candidate_count=min(len(available),len(unresolved))
        main_candidates=unresolved[:candidate_count]
        for r in ordered:
            key=matched.get(r['id'])
            if key:
                assigned.append({'record':r,'ledger_key':key,'pnu':pnu,
                                 'role':'MAIN','assurance':'EXACT_ID'
                                 if r.get('join_status')=='EXACT_ID'
                                 else 'ATTRIBUTE_CANDIDATE','title_count':len(title_keys)})
            elif r in main_candidates:
                assigned.append({'record':r,'ledger_key':None,'pnu':pnu,
                                 'role':'PNU_GROUP','assurance':'MULTIPLE_TITLES',
                                 'title_count':len(title_keys),
                                 'ledger_candidates':sorted(available)})
            else:
                # Surplus polygons lacking an individual register match
                # are not presumed separate registered buildings.
                omitted.append({'record':r,'reason':'EXTRA_UNVERIFIED_FOOTPRINT',
                                'pnu':pnu})
    return assigned, omitted


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

    # Parcel vacancy follows verified data absence, never an unavailable GIS
    # response. A register title without a matched footprint is NOT vacant.
    from development_building_fact import _register_pnus, _parcel_geometry_index
    cadastral_layer=(snapshot.get('_report_layers') or {}).get('LP_PA_CBND_BUBUN') or {}
    gis_layer=(snapshot.get('_report_layers') or {}).get('LT_C_BLDGINFO') or {}
    cadastral_shapes=_parcel_geometry_index(cadastral_layer,shape(snapshot['geometry']))
    registered_pnus=set()
    for row in registers:
        registered_pnus.update(_register_pnus(row.get('pnus')))
    parcel_building_status={}
    for pnu, parcel_geometry in cadastral_shapes.items():
        if pnu in registered_pnus:
            status='BUILDING_REGISTERED'
        elif any(shape(r['geometry']).intersects(parcel_geometry) and
                 shape(r['geometry']).intersection(parcel_geometry).area>0
                 for r in source_shapes):
            status='GIS_FOOTPRINT_WITHOUT_REGISTER'
        elif cadastral_layer.get('status')=='COMPLETE' and gis_layer.get('status')=='COMPLETE' and not fact['counts'].get('invalid_provider_shapes'):
            status='VACANT_BY_VERIFIED_SOURCE'
        else:
            status='UNCONFIRMED'
        parcel_building_status[pnu]=status
    fact['parcel_building_status']=parcel_building_status
    fact['counts']['source_verified_vacant_parcels']=sum(
        status=='VACANT_BY_VERIFIED_SOURCE'
        for status in parcel_building_status.values())
    fact['counts']['parcels_with_register_no_footprint']=sum(
        pnu in registered_pnus and not any(
            shape(r['geometry']).intersects(parcels_geom) and
            shape(r['geometry']).intersection(parcels_geom).area>0
            for r in source_shapes)
        for pnu,parcels_geom in cadastral_shapes.items())
    matches, omitted = _parcel_register_matches(source_shapes, registers, ledger_keys)
    mapped_keys = {a['ledger_key'] for a in matches if a.get('ledger_key')}
    role_totals = Counter(a['role'] for a in matches)
    assurance_totals = Counter(a['assurance'] for a in matches)
    fact['counts']['ledger_survey_rows'] = n
    fact['counts']['ledger_survey_mapped'] = len(mapped_keys)
    fact['counts']['source_gis_footprints_drawn'] = len(matches)
    fact['counts']['main_building_footprints'] = role_totals['MAIN']
    fact['counts']['auxiliary_building_footprints'] = role_totals['AUXILIARY']
    fact['counts']['multi_title_group_footprints'] = role_totals['PNU_GROUP']
    fact['counts']['source_footprints_excluded_from_survey'] = len(omitted)
    fact['counts']['direct_geometry_titles'] = len({
        a['ledger_key'] for a in matches if a['assurance']=='EXACT_ID'})
    fact['counts']['provisional_geometry_titles'] = len({
        a['ledger_key'] for a in matches if a['assurance'] in ('PNU_GROUP','ATTRIBUTE_CANDIDATE')})
    fact['counts']['unlinked_gis_footprints_drawn'] = role_totals['PNU_GROUP']
    fact['counts']['ambiguous_ledger_geometry_hidden'] = 0
    fact['display_exclusion'] = {
        'reason':'REGISTER_PNU_SCOPE_AND_SURPLUS_VALIDATION',
        'source_shapes_hidden':len(omitted),
        'source_shapes_drawn':len(matches),
        'cadastral_parcels_excluded_from_building_theme':len(parcel_records),
        'historical_status':'UNCONFIRMED',
        'excluded_reasons':dict(Counter(v['reason'] for v in omitted)),
        'note':('GIS 건물 외곽선은 대장 관련 PNU 내에서만 현황도 후보로 사용. '
                '대장 1건에 여러 외곽선이 있으면 대표와 부속으로 관리. '
                '복수 대장은 개별 속성 일치 시 연결하고 그렇지 않으면 '
                '필지 그룹 미확정으로 표시. 필지 경계 자체는 색칠하지 않음.'),
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
        for assoc in matches:
            record=assoc['record']
            key=assoc.get('ledger_key')
            row=registered.get(key)
            role=assoc['role']
            assurance=assoc['assurance']
            field_record=(record.get('attributes') or {}).get(field) or {}
            gis_value=(field_record.get('value')
                       if field_record.get('source') in ('GIS_BUILDING','GIS+BUILDING_REGISTER')
                       else field_record.get('gis_value'))
            if row:
                # Registry is authoritative for the main structure. Auxiliary
                # structures keep their own GIS values when explicitly supplied.
                value=(gis_value if role=='AUXILIARY' and gis_value is not None
                       else row.get(field))
                if value is None:value=gis_value
                basis=row.get(field+'_basis') if field in ('coverage','far') else None
                attribute_assurance=('GIS_AUXILIARY_ATTRIBUTE'
                                     if role=='AUXILIARY' and gis_value is not None
                                     else 'INTERNAL_CALC_PROVISIONAL'
                                     if basis and basis!='건축HUB 원문'
                                     else 'REGISTER_PNU_GROUP')
            else:
                # Multiple legal titles share the same PNU but the individual
                # correspondence is not proven. Only source-specific GIS
                # properties (or a unanimous register value) can be shown.
                group_rows=[registered[k] for k in assoc.get('ledger_candidates',[])]
                values={str(r.get(field)) for r in group_rows
                        if r.get(field) is not None}
                all_have_value=(bool(group_rows) and
                                all(r.get(field) is not None for r in group_rows))
                value=(gis_value if gis_value is not None else
                       group_rows[0].get(field)
                       if all_have_value and len(values)==1 else None)
                attribute_assurance=('GIS_ATTRIBUTE' if gis_value is not None else
                                     'GROUP_UNANIMOUS' if value is not None
                                     else 'UNKNOWN')
            display_join=('EXACT_ID' if assurance=='EXACT_ID' else
                          'PARCEL_SPATIAL_REFERENCE' if key else
                          'PNU_GROUP_REFERENCE')
            label = _classify(item_id, value) if attribute_assurance != 'UNKNOWN' else UNKNOWN
            category = category_for(label)
            properties = {
                'name': label, 'color': category['color'],
                'class_code': category['class_code'],
                'join_status': display_join,
                'source_join_status': record.get('join_status'),
                'geometry_basis': 'BUILDING_FOOTPRINT',
                'building_role': role,
                'parcel_pnu': assoc['pnu'],
                'title_count_on_parcel': assoc['title_count'],
                'geometry_source': 'LT_C_BLDGINFO',
                'quality_grade': record.get('quality_grade'),
                'attribute_assurance': attribute_assurance,
                'join_basis': assurance,
                'source': 'GIS_BUILDING',
                'building_fact_id': record.get('id'),
                'register_id': key,
            }
            features.append({'type': 'Feature', 'geometry': record['geometry'],
                             'properties': properties})
            map_totals[label] += 1
            join_totals[display_join] += 1

        rows = [{
            'kind': 'total', 'label': '합계', 'count': n,
            'pct': 100.0 if n else 0.0, 'join_status': 'BUILDING_REGISTER',
            'note': (f'건축물조서 {n}건 기준 · GIS건물통합정보 외곽선 '
                     f'{len(features)}개 표시(대표 {role_totals["MAIN"]} · '
                     f'부속 {role_totals["AUXILIARY"]} · '
                     f'복수대장 필지 미확정 {role_totals["PNU_GROUP"]}) · '
                     '조서 건수와 건물 외곽선 수는 별도 단위'),
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
            f'건축물조서 {n}건을 통계 기준으로 하고 관련 PNU의 GIS건물통합정보 '
            f'{len(features)}개 외곽선을 표시. 대장 1건과 여러 외곽선은 최대 '
            '건물도형을 주건축물, 나머지를 부속으로 관리. 복수대장 필지는 개별 '
            '속성이 확인되는 경우 개별 연계, 그렇지 않으면 PNU 그룹 참고로 표시. '
            f'대장 관련 PNU 미확인 및 부속으로 판단할 수 없는 초과 도형 '
            f'{len(omitted)}개는 현황도에서 제외. 지적 필지는 건물처럼 색칠하지 않음. '
            '도형 부재와 GIS 조회 실패는 구분하고, 건폐율·용적률 내부산정은 '
            '법정 확정치와 구분.'
        )
        if n > len(mapped_keys):
            note += f' 대장 {n-len(mapped_keys)}건은 GIS 외곽선과 직접·잠정 연계 미확인.'
        if fact['geometry_status'] != 'COMPLETE':
            note += ' GIS 자료는 부분·보조 수신일 수 있어 전체 외곽선 확보를 보장하지 않음.'

        gis_complete = fact['source_info'].get('LT_C_BLDGINFO', {}).get('status') == 'COMPLETE'
        full_match = bool(features) and gis_complete and not omitted and (
            role_totals['PNU_GROUP']==0)
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
                hidden_unmatched_source_footprints=len(omitted),
                ambiguous_registered_geometries=role_totals['PNU_GROUP'],
                verified_ledger_joins=assurance_totals['EXACT_ID'],
                provisional_spatial_joins=join_totals['PARCEL_SPATIAL_REFERENCE'],
                gis_only_footprints=0,
                auxiliary_footprints=role_totals['AUXILIARY'],
                omitted_unverified_footprints=len(omitted),
                footprint_status=fact['geometry_status'],
                mapped_groups=[{**categories[label], 'count': map_totals[label]}
                               for label in categories if map_totals[label]],
                mapped_total=len(features),
                mapped_unit='VWorld GIS건물통합정보 건물외곽선(개)',
                survey_total=n,
                note=(stat.get('note') or '') + ' ' + note,
            )
