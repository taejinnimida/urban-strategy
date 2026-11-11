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
from collections import Counter, defaultdict
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



def _parcel_geometry_index(layer, site):
    """Only provider-confirmed cadastral polygons can support parcel association."""
    # A provider-returned REFERENCE cadastral outline can support a clearly
    # provisional site-to-parcel spatial candidate; never call that a direct
    # building-register identity or a complete cadastral inventory.
    if layer.get('status') not in ('COMPLETE', 'REFERENCE', 'PARTIAL'):
        return {}
    grouped = defaultdict(list)
    for feature in layer.get('features') or []:
        props = {str(k).lower(): v for k, v in (feature.get('properties') or {}).items()}
        pnu = str(props.get('pnu') or feature.get('id') or '').strip()
        if not PNU_PATTERN.fullmatch(pnu):
            continue
        try:
            geom = shape(feature['geometry'])
            if not geom.is_valid or geom.is_empty or geom.geom_type not in ('Polygon', 'MultiPolygon'):
                continue
            clipped = geom.intersection(site)
            if not clipped.is_empty and clipped.area > 0:
                grouped[pnu].append(clipped)
        except Exception:
            continue
    from shapely.ops import unary_union
    return {pnu: unary_union(parts) for pnu, parts in grouped.items()}


def _spatial_parcel_candidate(building, parcels):
    """Find a near-contained, unambiguous parcel; never an exact building identity."""
    geom = building['geometry']
    if not parcels or geom.is_empty or geom.area <= 0:
        return None
    scores = []
    for pnu, parcel in parcels.items():
        if not geom.intersects(parcel):
            continue
        ratio = geom.intersection(parcel).area / geom.area
        if ratio > 0:
            scores.append((ratio, pnu))
    scores.sort(reverse=True)
    if not scores or scores[0][0] < 0.95:
        return None
    if len(scores) > 1 and scores[1][0] > 0.03:
        return None
    return scores[0][1], round(100 * scores[0][0], 2)


def _register_pnus(value):
    return {pnu.strip() for pnu in str(value or '').split(',')
            if PNU_PATTERN.fullmatch(pnu.strip())}


def _positive_numeric(value):
    try:
        number = float(value)
        return math.isfinite(number) and number > 0
    except (TypeError, ValueError):
        return False


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
    unkeyed_register_rows = 0
    for n, row in enumerate(data.get('buildings') or []):
        key = str(row.get('id') or '').strip()
        if not key:
            # No legal registry-management ID is invented; an internal
            # per-response row key preserves real recorded attributes for
            # unique-parcel provisional association and audit.
            unkeyed_register_rows += 1
            key = 'UNKEYED_REGISTER_ROW:' + str(n)
        by_title.setdefault(key, []).append(row)

    # A footprint and a building-register row may have no common identifier.
    # Allow a labelled, provisional association ONLY where one cadastral parcel
    # contains >=95% of the footprint, no competing parcel exceeds 3% and
    # exactly one register title belongs to that parcel. No forced matching by
    # counts, order, addresses, or coincident coverage/FAR.
    parcels = _parcel_geometry_index(layers.get('LP_PA_CBND_BUBUN') or {}, site)
    titles_by_pnu = defaultdict(set)
    for key, rows in by_title.items():
        if len(rows) == 1:
            for pnu in _register_pnus(rows[0].get('pnus')):
                titles_by_pnu[pnu].add(key)
    tentative = {}
    for observation in selected:
        if observation['title_key'] and len(by_title.get(observation['title_key'], [])) == 1:
            continue
        parcel_hit = _spatial_parcel_candidate(observation, parcels)
        supplied_pnu = observation.get('provider_pnu')
        # When a GIS feature has a valid 19-digit PNU, it can independently
        # propose a labelled provisional relationship to the building title.
        # A conflicting geometric parcel observation disqualifies that fallback.
        if parcel_hit and supplied_pnu and parcel_hit[0] != supplied_pnu:
            continue
        if parcel_hit:
            pnu, overlap_pct = parcel_hit
            basis = ('UNIQUE_PARCEL_OVERLAY_PROVISIONAL' if
                     (layers.get('LP_PA_CBND_BUBUN') or {}).get('status') == 'COMPLETE'
                     else 'REFERENCE_PARCEL_OVERLAY_PROVISIONAL')
        elif supplied_pnu:
            pnu, overlap_pct = supplied_pnu, None
            basis = 'GIS_PNU_ATTRIBUTE_PROVISIONAL'
        else:
            continue
        possible = titles_by_pnu.get(pnu, set())
        if len(possible) != 1:
            continue
        key = next(iter(possible))
        tentative[observation['id']] = {'register_id': key, 'parcel_pnu': pnu,
                                         'overlap_pct': overlap_pct, 'basis': basis}
    # Prevent reuse of an exact-ID title by a spatial-only footprint.
    exact_titles = {obs['title_key'] for obs in selected
                    if obs['title_key'] and len(by_title.get(obs['title_key'], [])) == 1}
    tentative = {k: v for k, v in tentative.items() if v['register_id'] not in exact_titles}
    # Prevent one register title from being assigned to multiple footprints.
    tentative_counts = Counter(c['register_id'] for c in tentative.values())
    tentative = {k: v for k, v in tentative.items()
                 if tentative_counts[v['register_id']] == 1}
    used_title_keys = set()
    spatial_title_keys = set()
    records = []
    conflicts = 0
    for observation in selected:
        possible = by_title.get(observation['title_key']) if observation['title_key'] else None
        exact_ledger = possible[0] if possible and len(possible) == 1 else None
        candidate = tentative.get(observation['id']) if not exact_ledger else None
        candidate_key = candidate['register_id'] if candidate else None
        provisional_ledger = by_title[candidate_key][0] if candidate else None
        ledger = exact_ledger or provisional_ledger
        if exact_ledger:
            used_title_keys.add(observation['title_key'])
        if provisional_ledger:
            spatial_title_keys.add(candidate_key)
        attrs, disagreements = {}, []
        gis_props = observation['props'] if observation['source'] == SOURCE_NAMES[0] else {}
        for field in FIELDS:
            gis_value, gis_key = _gis_field(field, gis_props, data.get('as_of'))
            title_value = ledger.get(field) if ledger else None
            internal_basis = ledger.get(field+'_basis') if ledger and field in ('coverage','far') else None
            is_internal = bool(internal_basis and internal_basis != '건축HUB 원문')
            # Zero coverage/FAR on a footprint is an unverified placeholder,
            # not positive evidence of an actual 0% building.
            if field in ('coverage', 'far'):
                if _nonempty(gis_value) and not _positive_numeric(gis_value):
                    gis_value = None
                if _nonempty(title_value) and not _positive_numeric(title_value):
                    title_value = None
            if is_internal and _nonempty(gis_value):
                # A derived title-side number cannot override the observed GIS
                # attribute or be presented as official direct agreement.
                values = {'value': gis_value, 'source': 'GIS_BUILDING',
                          'assurance': 'SOURCE_ATTRIBUTE', 'gis_field': gis_key,
                          'alternate_internal_value': title_value,
                          'alternate_internal_basis': internal_basis}
            elif _nonempty(gis_value) and _nonempty(title_value):
                if _agree(field, gis_value, title_value):
                    values = {'value': title_value,
                              'source': 'GIS+BUILDING_REGISTER',
                              'assurance': 'DIRECT_AGREEMENT' if exact_ledger else 'SPATIAL_AGREEMENT',
                              'gis_field': gis_key}
                else:
                    # The register attribute is not invalidated by a GIS disagreement.
                    # Only the map-to-register association may be provisional.
                    disagreements.append(field)
                    values = {'value': title_value, 'source': 'BUILDING_REGISTER',
                              'assurance': ('LEDGER_GIS_CONFLICT' if exact_ledger else
                                            'SPATIAL_LEDGER_GIS_CONFLICT'),
                              'gis_field': gis_key, 'gis_value': gis_value,
                              'disagreement': True}
            elif _nonempty(gis_value):
                values = {'value': gis_value, 'source': 'GIS_BUILDING',
                          'assurance': 'SOURCE_ATTRIBUTE', 'gis_field': gis_key}
            elif _nonempty(title_value):
                values = {'value': title_value,
                          'source': 'PLATFORM_DERIVED' if is_internal else 'BUILDING_REGISTER',
                          'assurance': ('INTERNAL_CALC' if exact_ledger else 'INTERNAL_CALC_PROVISIONAL')
                              if is_internal else ('DIRECT_LEDGER' if exact_ledger else 'SPATIAL_REFERENCE'),
                          'gis_field': None,
                          'basis': internal_basis if is_internal else None}
            else:
                values = {'value': None, 'source': None, 'assurance': 'UNKNOWN', 'gis_field': None}
            attrs[field] = values
        if disagreements:
            grade = 'E_CONFLICT' if exact_ledger else 'E_SPATIAL_REVIEW'
            conflicts += 1
        elif exact_ledger:
            grade = 'A_DIRECT_TITLE'
        elif provisional_ledger:
            grade = 'B_SPATIAL_REFERENCE'
        elif any(v['assurance'] == 'SOURCE_ATTRIBUTE' for v in attrs.values()):
            grade = 'B_GIS_ATTRIBUTE'
        else:
            grade = 'C_GEOMETRY_ONLY'
        records.append({
            'id': observation['id'],
            'geometry': mapping(observation['geometry']),
            'geometry_source': observation['source'],
            'source_status': source_info[observation['source']]['status'],
            'register_id': (observation['title_key'] if exact_ledger else
                            str(ledger.get('id') or '').strip() or None if ledger else None),
            'register_row_key': candidate_key if candidate else observation['title_key'],
            'join_status': ('EXACT_ID' if exact_ledger else
                            'PARCEL_SPATIAL_REFERENCE' if provisional_ledger else 'UNMATCHED'),
            'quality_grade': grade,
            'provider_pnu': observation['provider_pnu'],
            'spatial_parcel_pnu': candidate['parcel_pnu'] if candidate else None,
            'spatial_overlap_pct': candidate['overlap_pct'] if candidate else None,
            'match_basis': ('BUILDING_REGISTER_ID' if exact_ledger else
                            candidate.get('basis') if candidate else None),
            'registered_pnus': ledger.get('pnus') if ledger else None,
            'site_area_m2': ledger.get('site_area') if ledger else None,
            'site_area_basis': ledger.get('site_area_source') if ledger else None,
            'address_building_id': observation['address_id'],
            'attributes': attrs, 'conflicts': disagreements,
        })
    # Independent, ledger-first PNU mapping: a PNU identifies a cadastral parcel,
    # NOT a building footprint. If a title has one unambiguous PNU and the
    # displayed parcel has that official cadastral ID, its real ledger values may
    # be plotted on that parcel even without any matching GIS building identity.
    # This never creates an EXACT_ID building-footprint join.
    pnu_to_titles = defaultdict(set)
    for key, rows in by_title.items():
        if len(rows) == 1:
            for pnu in _register_pnus(rows[0].get('pnus')):
                pnu_to_titles[pnu].add(key)
    parcel_register_records = []
    parcel_record_keys = set()
    for pnu, keys in sorted(pnu_to_titles.items()):
        if len(keys) != 1 or pnu not in parcels:
            continue
        key = next(iter(keys))
        ledger = by_title[key][0]
        # One building title across several parcels must not be counted once
        # for each parcel, or visually assigned to any one parcel arbitrarily.
        if len(_register_pnus(ledger.get('pnus'))) != 1:
            continue
        if key in used_title_keys or key in spatial_title_keys:
            continue
        attrs = {}
        for field in FIELDS:
            value = ledger.get(field)
            basis = ledger.get(field + '_basis') if field in ('coverage', 'far') else None
            internal = bool(basis and basis != '건축HUB 원문')
            if field in ('coverage', 'far') and not _positive_numeric(value):
                value = None
            attrs[field] = {
                'value': value,
                'source': ('PLATFORM_DERIVED' if internal else 'BUILDING_REGISTER') if _nonempty(value) else None,
                'assurance': ('INTERNAL_CALC_PARCEL' if internal else 'DIRECT_LEDGER_PNU')
                             if _nonempty(value) else 'UNKNOWN',
                'basis': basis if internal else None,
            }
        parcel_register_records.append({
            'id': 'CADASTRAL_PNU:' + pnu,
            'geometry': mapping(parcels[pnu]),
            'geometry_source': 'LP_PA_CBND_BUBUN',
            'geometry_basis': 'CADASTRAL_PNU',
            'source_status': (layers.get('LP_PA_CBND_BUBUN') or {}).get('status'),
            'register_id': key,
            'registered_pnus': ledger.get('pnus'),
            'spatial_parcel_pnu': pnu,
            'join_status': 'PNU_PARCEL_REGISTER',
            'match_basis': 'EXACT_CADASTRAL_PNU_NOT_BUILDING_FOOTPRINT',
            'quality_grade': 'P_PARCEL_LEDGER',
            'attributes': attrs,
            'conflicts': [],
        })
        parcel_record_keys.add(key)
    unmatched_registers = [
        {'register_id': key, 'pnus': ', '.join(str(r.get('pnus') or '') for r in rows),
         'reason': ('필지 공간중첩으로 잠정 후보만 확인. 대장관리번호 직접 일치 아님'
                    if key in spatial_title_keys else
                    '관련 PNU의 필지 도형으로 대장 속성 표시 가능. 개별 건물 외곽선과 직접연계는 미확정'
                    if key in parcel_record_keys else
                    '관련 필지 대장은 확인되나 이 구역에 대응하는 도형의 관리번호 직접연계 없음')}
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
                                  'SPATIAL_AGREEMENT', 'SPATIAL_REFERENCE', 'SPATIAL_CONFLICT',
                                  'INTERNAL_CALC', 'INTERNAL_CALC_PROVISIONAL',
                                  'SOURCE_ATTRIBUTE', 'CONFLICT', 'UNKNOWN',
                                  'LEDGER_GIS_CONFLICT', 'SPATIAL_LEDGER_GIS_CONFLICT')}
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
        'spatial_register_candidates': sum(r['join_status'] == 'PARCEL_SPATIAL_REFERENCE' for r in records),
        'spatial_corroborated_candidates': sum(
            r['join_status'] == 'PARCEL_SPATIAL_REFERENCE' and
            any(v['assurance'] == 'SPATIAL_AGREEMENT' for v in r['attributes'].values())
            for r in records),
        'register_without_any_candidate': len(set(by_title) - used_title_keys - spatial_title_keys - parcel_record_keys),
        'register_with_cadastral_pnu': len(parcel_record_keys),
        'cadastral_pnu_features': len(parcel_register_records),
        'gis_attribute_only': sum(r['quality_grade'] == 'B_GIS_ATTRIBUTE' for r in records),
        'unkeyed_register_rows': unkeyed_register_rows,
        'spatial_candidates_from_reference_cadastre': sum(
            r.get('match_basis') == 'REFERENCE_PARCEL_OVERLAY_PROVISIONAL'
            for r in records),
        'spatial_candidates_from_provider_pnu': sum(
            r.get('match_basis') == 'GIS_PNU_ATTRIBUTE_PROVISIONAL'
            for r in records),
        'geometry_only': sum(r['quality_grade'] == 'C_GEOMETRY_ONLY' for r in records),
        'conflicting_shapes': conflicts,
        'register_without_direct_geometry': len(unmatched_registers),
        'zero_confirmed': zero_confirmed,
    }
    return {'records': records, 'parcel_register_records': parcel_register_records,
            'unmatched_registers': unmatched_registers,
            'source_info': source_info,
            'spatial_parcel_source_status': (layers.get('LP_PA_CBND_BUBUN') or {}).get('status', 'UNAVAILABLE'),
            'geometry_status': status,
            'provider_verified': status == 'COMPLETE',
            'counts': counts, 'field_assurance_counts': field_assurance_counts,
            'note': ('조회된 건축물대장 원문 속성은 우선 활용. GIS 속성과 충돌하면 대장값을 유지하고 차이를 별도 기록. '
                     '대장 내용의 신뢰성과 지도 도형·대장 연결 신뢰도는 별개. 관련 PNU가 연속지적과 일치하면 필지단위 대장 현황도로 표시하되 건축물 외곽선으로 가장하지 않음. 보조 지적 및 도형 제공 PNU도 직접 건물관리번호 일치가 아님. '
                     '건폐율·용적률 플랫폼 내부산정·개략추정은 원문과 구별하고 법정 확정치로 사용하지 않음. '
                     '공간도형 수는 법정 건축물 동수와 일치한다고 단정하지 않음.')}
