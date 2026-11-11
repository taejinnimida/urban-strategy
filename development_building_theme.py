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
    """Attach thematic output from one quality-graded, independent building FACT."""
    from development_building_fact import assemble_building_fact

    fact = assemble_building_fact(data, snapshot)
    data['building_fact'] = fact
    records = fact['records']
    parcel_records = fact.get('parcel_register_records') or []
    # Keep building-footprint and PNU-registered parcel geometry distinct.
    # The latter is a thematic map proxy, never legal building polygon count.
    map_records = records + parcel_records
    n = len(map_records)
    from collections import Counter

    for item in data['items']:
        item_id = item['id']
        field = FIELDS.get(item_id)
        if not field:
            continue
        if fact['geometry_status'] == 'UNAVAILABLE' and not parcel_records:
            item.update(features=[], provider_verified=False, geometry_status='UNAVAILABLE',
                        detail=(item.get('detail') or '')+' · 건축물 공간자료 미확보(0건으로 간주하지 않음)')
            continue
        # Conflicting attributes are never silently resolved. The footprint
        # remains on the map in a separately reviewable category.
        labels = []
        for rec in map_records:
            field_data = rec['attributes'][field]
            if field_data['value'] is None and field_data['assurance'] in ('CONFLICT','SPATIAL_CONFLICT'):
                label = '자료 불일치(검토)'
            elif field_data['value'] is None:
                label = (OTHER if rec['join_status'] in ('EXACT_ID','PNU_PARCEL_REGISTER') else UNKNOWN)
            else:
                label = _classify(item_id, field_data['value'])
            labels.append(label)
        totals = Counter(labels)
        ordered = list(BINS.get(item_id) or [])
        if not ordered:
            ordered = sorted((k for k in totals if k not in (OTHER,UNKNOWN,'자료 불일치(검토)')),
                             key=lambda k: (-totals[k],k))
        ordered += [k for k in (OTHER, UNKNOWN, '자료 불일치(검토)') if totals.get(k)]
        categories = {}
        for index,label in enumerate(ordered):
            if label == UNKNOWN:
                code,color='C99','#a1a1aa'
            elif label == OTHER:
                code,color='C98','#d6d3d1'
            elif label == '자료 불일치(검토)':
                code,color='C97','#dc2626'
            else:
                code,color=f'C{index+1:02d}',PALETTE[index % len(PALETTE)]
            categories[label] = {'name':label,'class_code':code,'color':color,
                                 'count':totals.get(label,0),
                                 'pct':round(100*totals.get(label,0)/n,1) if n else 0}
        features=[]
        for rec,label in zip(map_records,labels):
            item_value = rec['attributes'][field]
            features.append({'type':'Feature','geometry':rec['geometry'],
                             'properties':{'name':label,
                                           'color':categories[label]['color'],
                                           'class_code':categories[label]['class_code'],
                                           'join_status':rec['join_status'],
                                           'geometry_basis':rec.get('geometry_basis','BUILDING_FOOTPRINT'),
                                           'quality_grade':rec['quality_grade'],
                                           'attribute_assurance':item_value['assurance'],
                                           'join_basis':rec.get('match_basis'),
                                           'spatial_parcel_pnu':rec.get('spatial_parcel_pnu'),
                                           'spatial_overlap_pct':rec.get('spatial_overlap_pct'),
                                           'source':item_value['source'],
                                           'building_fact_id':rec['id'],
                                           'register_id':rec.get('register_id')}})
        rows=[{'kind':'total','label':'합계','count':n,
               'pct':100.0 if n else 0.0,'join_status':'건물 외곽선·대장기반 필지 표시단위',
               'note':f"건물도형 {len(records)}개 + PNU대장 필지도형 {len(parcel_records)}개 · 법정 건축물 동수와 구분"}]
        for label in ordered:
            row=categories[label]
            rows.append({'kind':'category','label':label,'count':row['count'],
                         'pct':row['pct'],'join_status':(
                             '자료 불일치' if label=='자료 불일치(검토)' else
                             '속성 미확인' if label==UNKNOWN else
                             '항목값 없음' if label==OTHER else '출처별 확보'),
                         'note': ' · '.join(x for x in (
                             ('잠정 필지 공간연계 '+str(sum(
                                 rec['join_status']=='PARCEL_SPATIAL_REFERENCE' and
                                 labels[pos]==label for pos,rec in enumerate(map_records)))+'개'
                              if any(rec['join_status']=='PARCEL_SPATIAL_REFERENCE' and
                                     labels[pos]==label for pos,rec in enumerate(map_records)) else ''),
                             ('대장 PNU 필지도형 '+str(sum(
                                 rec['join_status']=='PNU_PARCEL_REGISTER' and
                                 labels[pos]==label for pos,rec in enumerate(map_records)))+'개 · 건물 외곽선 아님'
                              if any(rec['join_status']=='PNU_PARCEL_REGISTER' and
                                     labels[pos]==label for pos,rec in enumerate(map_records)) else ''),
                             ('플랫폼 내부산정 '+str(sum(
                                 rec['attributes'][field]['assurance'] in
                                 ('INTERNAL_CALC','INTERNAL_CALC_PROVISIONAL','INTERNAL_CALC_PARCEL') and
                                 labels[pos]==label for pos,rec in enumerate(map_records)))+'개 · 법정값 아님'
                              if any(rec['attributes'][field]['assurance'] in
                                     ('INTERNAL_CALC','INTERNAL_CALC_PROVISIONAL','INTERNAL_CALC_PARCEL') and
                                     labels[pos]==label for pos,rec in enumerate(map_records)) else '')
                         ) if x)})
        note=fact['note']
        if not n:
            note+=' 공간도형 수집결과 0건은 실제 건축물 부존재를 증명하지 않음.'
        if parcel_records:
            note+=f' 건축물 외곽선과 연결되지 않은 대장 {len(parcel_records)}건은 관련 PNU의 필지도형에 별도 표시하며, 건물동수로 계산하지 않음.'
        if fact['geometry_status'] != 'COMPLETE':
            note+=' 일부/보조 공간자료이며 도형 수는 확정되지 않음.'
        sources='VWorld GIS건물통합정보 · 도로명주소 건물도형(보조) · 국토교통부 건축HUB · 토지대장(플랫폼 내부산정 시)'
        item.update(features=features,
                    legend=[categories[k] for k in ordered if totals.get(k)],
                    thematic_type='building',provider_verified=fact['provider_verified'],
                    cad_exportable=(any(v.get('status') == 'COMPLETE' for v in fact['source_info'].values()) or
                                    bool(parcel_records)),
                    geometry_status=('PARTIAL' if parcel_records and fact['geometry_status']=='UNAVAILABLE'
                                     else fact['geometry_status']),
                    source=sources,
                    status='공간자료 확보' if fact['geometry_status']=='COMPLETE' else
                           '일부 공간자료 확보' if fact['geometry_status']=='PARTIAL' else '보조 조회 참고자료',
                    detail=(item.get('detail') or '') +
                           f" · 건물도형 {len(records)}개 · 대장 PNU 필지도형 {len(parcel_records)}개"
                           f", 대장 직접연결 {fact['counts']['direct_register_matches']}개"
                           f", 필지 공간연계 잠정 {fact['counts']['spatial_register_candidates']}개"
                           f", GIS 속성만 확보 {fact['counts']['gis_attribute_only']}개"
                           f", 검토 필요 {fact['counts']['conflicting_shapes']}개"
                           ' · '+note,
                    survey_table={'title':f'[표 {item_id} {TITLES[item_id]} 현황]',
                                  'status':('PARTIAL' if parcel_records and fact['geometry_status']=='UNAVAILABLE'
                                            else fact['geometry_status']),'rows':rows,
                                  'source':sources,'as_of':data.get('as_of'),
                                  'notes':[note]})
        stat=data.get('statistics',{}).get(item_id)
        if isinstance(stat,dict):
            stat.update(footprint_count=n,verified_ledger_joins=fact['counts']['direct_register_matches'],
                        unmatched_footprints=n-fact['counts']['direct_register_matches']-fact['counts']['spatial_register_candidates'],
                        provisional_spatial_joins=fact['counts']['spatial_register_candidates'],
                        footprint_status=fact['geometry_status'],
                        mapped_groups=[categories[k] for k in ordered],
                        mapped_total=n,mapped_unit='건물 외곽선+필지 PNU 대장 주제도형(건축물 동수 아님)',
                        note=(stat.get('note') or '')+' '+note)
