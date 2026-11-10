"""Independent, auditable building FACT for nationwide preliminary surveys.

A map footprint, a GIS attribute and a register title are different observations.
Only equality of an actual building-register management key joins two identities.
Parcel intersections and similar shapes are never treated as title matches.
"""
from datetime import date
import hashlib
import json
import math
import re
from shapely.geometry import mapping, shape

FIELDS = ('use', 'structure', 'floors', 'age', 'floor_area', 'coverage', 'far')
GIS_ALIASES = {
    'use': ('mainpurpscdnm', 'main_prpos_code_nm', 'mainprposcodenm', 'main_prpos_nm', 'mainpurpsnm'),
    'structure': ('strctcdnm', 'strct_cd_nm', 'structure_nm'),
    'floors': ('grndflrcnt', 'grnd_flr_cnt', 'ground_floor_count'),
    'floor_area': ('totarea', 'tot_area', 'total_floor_area'),
    'coverage': ('bcrat', 'bc_rat'),
    'far': ('vlrat', 'vl_rat'),
}
AGE_DATES = ('useaprday', 'use_apr_day', 'useconfmde', 'use_confm_de')
TITLE_KEYS = ('mgmbldrgstpk', 'mgm_bldrgst_pk', 'bldrgst_pk')
ADDRESS_KEYS = ('bd_mgt_sn', 'bdmgtsn')
PNU_PATTERN = re.compile(r'^\d{19}$')
SOURCE_NAMES = ('LT_C_BLDGINFO', 'LT_C_SPBD')


def _nonempty(value):
    return value is not None and str(value).strip() not in ('', 'None', 'null')


def _age_from_date(value, as_of):
    try:
        text = str(value).strip().replace('-', '').replace('.', '')
        if not (len(text) == 8 and text.isdigit()):
            return None
        approved = date(int(text[:4]), int(text[4:6]), int(text[6:8]))
        today = date.fromisoformat(str(as_of)[:10])
        if approved > today:
            return None
        return today.year - approved.year - ((today.month, today.day) < (approved.month, approved.day))
    except (TypeError, ValueError, OverflowError):
        return None


def _gis_field(field, props, as_of):
    if field == 'age':
        for key in AGE_DATES:
            if _nonempty(props.get(key)):
                age = _age_from_date(props[key], as_of)
                if age is not None:
                    return age, key
        return None, None
    for key in GIS_ALIASES.get(field, ()):
        if _nonempty(props.get(key)):
            return props[key], key
    return None, None


def _agree(field, left, right):
    if field in ('use', 'structure'):
        return str(left).strip().replace(' ', '') == str(right).strip().replace(' ', '')
    try:
        a, b = float(left), float(right)
        return math.isfinite(a) and math.isfinite(b) and abs(a - b) <= max(.01, .001 * max(abs(a), abs(b)))
    except (TypeError, ValueError):
        return False


def _source_features(layer, site, code):
    if layer.get('status') not in ('COMPLETE', 'REFERENCE', 'PARTIAL'):
        return [], 0
    result, invalid = [], 0
    for feature in layer.get('features') or []:
        try:
            geom = shape(feature['geometry'])
            if geom.is_empty or not geom.is_valid or geom.geom_type not in ('Polygon', 'MultiPolygon'):
                invalid += 1
                continue
            clipped = geom.intersection(site)
            if clipped.is_empty or clipped.area <= 0:
                continue
            props = {str(k).lower(): v for k, v in (feature.get('properties') or {}).items()}
            key = next((str(props[k]).strip() for k in TITLE_KEYS if _nonempty(props.get(k))), None)
            address_id = next((str(props[k]).strip() for k in ADDRESS_KEYS if _nonempty(props.get(k))), None)
            pnu = str(props.get('pnu') or '').strip()
            native_id = str(feature.get('id') or '').strip()
            digest = hashlib.sha256(json.dumps([code, native_id, mapping(clipped)], sort_keys=True,
                                               ensure_ascii=False).encode('utf-8')).hexdigest()[:20]
            result.append({'id': code + ':' + digest, 'geometry': clipped,
                           'props': props, 'source': code, 'title_key': key,
                           'address_id': address_id,
                           'provider_pnu': pnu if PNU_PATTERN.fullmatch(pnu) else None})
        except Exception:
            # A single malformed provider geometry cannot erase accepted peers.
            # The omission stays visible in the quality/coverage counters.
            invalid += 1
    return result, invalid


def _overlap_class(a, b):
    if not a.intersects(b):
        return 'DISJOINT'
    try:
        common = a.intersection(b).area
        if common <= 0:
            return 'DISJOINT'
        union = a.area + b.area - common
        if union > 0 and common / union >= .97:
            return 'SAME_SHAPE_CANDIDATE'
        return 'OVERLAP_REVIEW'
    except Exception:
        return 'OVERLAP_REVIEW'


def assemble_building_fact(data, snapshot):
    """Build one serializable FACT. No direct HTTP, no district-specific rules."""
    layers = snapshot.get('_report_layers') or {}
    site = shape(snapshot['geometry'])
    source_info = {key: {'status': (layers.get(key) or {}).get('status', 'UNAVAILABLE'),
                         'queried_at': (layers.get(key) or {}).get('queried_at')}
                   for key in SOURCE_NAMES}
    gis, gis_invalid = _source_features(layers.get(SOURCE_NAMES[0]) or {}, site, SOURCE_NAMES[0])
    address, address_invalid = _source_features(layers.get(SOURCE_NAMES[1]) or {}, site, SOURCE_NAMES[1])

    # Preserve GIS records first. Add address polygons only when truly disjoint.
    # Ambiguous overlaps are never silently counted twice.
    selected = list(gis)
    secondary_duplicate = secondary_overlap_review = 0
    for extra in address:
        if gis:
            outcomes = {_overlap_class(extra['geometry'], parent['geometry']) for parent in gis
                        if extra['geometry'].bounds[0] <= parent['geometry'].bounds[2]
                        and extra['geometry'].bounds[2] >= parent['geometry'].bounds[0]
                        and extra['geometry'].bounds[1] <= parent['geometry'].bounds[3]
                        and extra['geometry'].bounds[3] >= parent['geometry'].bounds[1]}
            if 'SAME_SHAPE_CANDIDATE' in outcomes:
                secondary_duplicate += 1
                continue
            if 'OVERLAP_REVIEW' in outcomes:
                secondary_overlap_review += 1
                continue
        selected.append(extra)

    by_title = {}
    for row in data.get('buildings') or []:
        key = str(row.get('id') or '').strip()
        if key:
            by_title.setdefault(key, []).append(row)
    used_title_keys = set()
    records = []
    conflicts = 0
    for observation in selected:
        possible = by_title.get(observation['title_key']) if observation['title_key'] else None
        ledger = possible[0] if possible and len(possible) == 1 else None
        if ledger:
            used_title_keys.add(observation['title_key'])
        attrs, disagreements = {}, []
        gis_props = observation['props'] if observation['source'] == SOURCE_NAMES[0] else {}
        for field in FIELDS:
            gis_value, gis_key = _gis_field(field, gis_props, data.get('as_of'))
            title_value = ledger.get(field) if ledger else None
            if _nonempty(gis_value) and _nonempty(title_value):
                if _agree(field, gis_value, title_value):
                    values = {'value': title_value, 'source': 'GIS+BUILDING_REGISTER',
                              'assurance': 'DIRECT_AGREEMENT', 'gis_field': gis_key}
                else:
                    disagreements.append(field)
                    values = {'value': None, 'source': 'GIS+BUILDING_REGISTER',
                              'assurance': 'CONFLICT', 'gis_field': gis_key}
            elif _nonempty(gis_value):
                values = {'value': gis_value, 'source': 'GIS_BUILDING',
                          'assurance': 'SOURCE_ATTRIBUTE', 'gis_field': gis_key}
            elif _nonempty(title_value):
                values = {'value': title_value, 'source': 'BUILDING_REGISTER',
                          'assurance': 'DIRECT_LEDGER', 'gis_field': None}
            else:
                values = {'value': None, 'source': None, 'assurance': 'UNKNOWN', 'gis_field': None}
            attrs[field] = values
        if disagreements:
            grade = 'E_CONFLICT'
            conflicts += 1
        elif ledger:
            grade = 'A_DIRECT_TITLE'
        elif any(v['assurance'] == 'SOURCE_ATTRIBUTE' for v in attrs.values()):
            grade = 'B_GIS_ATTRIBUTE'
        else:
            grade = 'C_GEOMETRY_ONLY'
        records.append({
            'id': observation['id'],
            'geometry': mapping(observation['geometry']),
            'geometry_source': observation['source'],
            'source_status': source_info[observation['source']]['status'],
            'register_id': observation['title_key'] if ledger else None,
            'join_status': 'EXACT_ID' if ledger else 'UNMATCHED',
            'quality_grade': grade,
            'provider_pnu': observation['provider_pnu'],
            'registered_pnus': ledger.get('pnus') if ledger else None,
            'address_building_id': observation['address_id'],
            'attributes': attrs, 'conflicts': disagreements,
        })
    unmatched_registers = [
        {'register_id': key, 'pnus': ', '.join(str(r.get('pnus') or '') for r in rows),
         'reason': '관련 필지 대장은 확인되나 이 구역에 대응하는 도형의 관리번호 직접연계 없음'}
        for key, rows in sorted(by_title.items()) if key not in used_title_keys
    ]
    raw_statuses = [source_info[name]['status'] for name in SOURCE_NAMES]
    acquired = [x for x in raw_statuses if x in ('COMPLETE', 'REFERENCE', 'PARTIAL')]
    invalid = gis_invalid + address_invalid
    zero_confirmed = (all(x == 'COMPLETE' for x in raw_statuses)
                      and not selected and not invalid)
    if not acquired:
        status = 'UNAVAILABLE'
    elif all(x == 'COMPLETE' for x in raw_statuses) and not invalid and not secondary_overlap_review:
        status = 'COMPLETE'
    elif any(x == 'REFERENCE' for x in acquired):
        status = 'REFERENCE'
    else:
        status = 'PARTIAL'
    field_assurance_counts = {
        field: {assurance: sum(rec['attributes'][field]['assurance'] == assurance
                               for rec in records)
                for assurance in ('DIRECT_AGREEMENT', 'DIRECT_LEDGER',
                                  'SOURCE_ATTRIBUTE', 'CONFLICT', 'UNKNOWN')}
        for field in FIELDS
    }
    counts = {
        'mapped_shapes': len(records),
        'gis_shapes': len(gis),
        'address_shapes': len(address),
        'address_duplicates_suppressed': secondary_duplicate,
        'address_overlaps_needing_review': secondary_overlap_review,
        'invalid_provider_shapes': invalid,
        'direct_register_matches': sum(r['join_status'] == 'EXACT_ID' for r in records),
        'gis_attribute_only': sum(r['quality_grade'] == 'B_GIS_ATTRIBUTE' for r in records),
        'geometry_only': sum(r['quality_grade'] == 'C_GEOMETRY_ONLY' for r in records),
        'conflicting_shapes': conflicts,
        'register_without_direct_geometry': len(unmatched_registers),
        'zero_confirmed': zero_confirmed,
    }
    return {'records': records, 'unmatched_registers': unmatched_registers,
            'source_info': source_info, 'geometry_status': status,
            'provider_verified': status == 'COMPLETE',
            'counts': counts, 'field_assurance_counts': field_assurance_counts,
            'note': ('건물 공간도형 수는 건축물 법정 동수가 아님. 건축물대장 직접연결은 관리번호 일치만 인정. '
                     '겹치는 보조 건물도형은 중복 우려로 제외·별도 집계하고, 미연계는 무허가나 멸실로 단정하지 않음. '
                     '건축물대장은 지적 편입필지 관련 조회분이며 전국 전체 건축물대장과 동등하지 않음.')}
