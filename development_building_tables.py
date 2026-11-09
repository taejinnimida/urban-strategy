"""FACT-derived descriptive building survey tables (items 3-1 through 3-7).

The denominator is the number of deduplicated Building HUB title records
linked to selected cadastral parcels, NOT the number of physically confirmed
buildings inside the site. No statutory aging or building eligibility rules.

The building table deliberately contains NO spatial area or mapped-footprint
assertion: cadastral parcel overlap is not proof of individual building overlap.
"""
from collections import Counter
from datetime import datetime
import math

BUILDING_FIELDS = {
    '3-1': ('use', '용도별'),
    '3-2': ('structure', '구조별'),
    '3-3': ('floors', '층수별'),
    '3-4': ('age', '경과연도별'),
    '3-5': ('floor_area', '연면적별'),
    '3-6': ('coverage', '건폐율별'),
    '3-7': ('far', '용적률별'),
}


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return n if math.isfinite(n) and n >= 0 else None


def _range_label(n, cuts, unit):
    """Return a descriptive left-inclusive class, not a legal threshold."""
    prev = 0
    for edge in cuts:
        if n < edge:
            if prev == 0:
                return f'{edge:,}{unit} 미만'
            return f'{prev:,}~{edge:,}{unit} 미만'
        prev = edge
    return f'{cuts[-1]:,}{unit} 이상'


def _classification(item_id, value):
    if item_id in ('3-1', '3-2'):
        if not isinstance(value, str) or not value.strip():
            return None
        # Use official raw category, without inventing broader use/structure groups.
        return value.strip()
    v = _number(value)
    if v is None:
        return None
    if item_id == '3-3':
        return f'{int(v)}층' if v == int(v) else None
    if item_id == '3-4':
        return _range_label(v, (10, 20, 30), '년')
    if item_id == '3-5':
        return _range_label(v, (100, 200, 500, 1000), '㎡')
    if item_id == '3-6':
        if v > 100:
            return '100% 초과(원자료 확인 필요)'
        return _range_label(v, (20, 40, 60, 80, 100), '%') if v < 100 else '100%'
    if item_id == '3-7':
        return _range_label(v, tuple(range(50, 1001, 50)), '%')
    return None


def _ordered_labels(item_id, counts):
    if item_id == '3-3':
        return sorted(counts, key=lambda x: int(x[:-1]))
    if item_id in ('3-4', '3-5', '3-6', '3-7'):
        # Range labels sort by numeric lower bounds, not lexicographically.
        def rank(label):
            if label.startswith('100% 초과'):
                return float('inf')
            if label == '100%':
                return 100
            if '~' in label:
                return float(label.split('~', 1)[0].replace(',', ''))
            if '이상' in label:
                return float(label.split('%', 1)[0].split('㎡', 1)[0].split('년', 1)[0].replace(',', ''))
            return 0
        return sorted(counts, key=lambda x: (rank(x), x))
    return sorted(counts, key=lambda x: (-counts[x], x))


def attach_building_survey_tables(data):
    """Add non-spatial tables and conservative prose to each of seven items.

    Must be invoked after buildings() deduplicates Building HUB title records.
    Thus this consumes the same FACT as the existing building inventory.
    """
    buildings = list(data.get('buildings') or [])
    land = list(data.get('land') or [])
    parcel_status = data.get('parcel_status')
    completed = sum(row.get('building_status') in ('확보', '대장 응답 0건') for row in land)
    all_queried = bool(land) and completed == len(land)
    expected_total = len(buildings)
    as_of = data.get('as_of')
    try:
        datetime.fromisoformat(str(as_of))
        valid_as_of = True
    except (TypeError, ValueError):
        valid_as_of = False

    for item in data.get('items') or []:
        item_id = item.get('id')
        if item_id not in BUILDING_FIELDS:
            continue
        field, class_name = BUILDING_FIELDS[item_id]
        table = {
            'item_id': item_id,
            'title': f'표 {item_id} 건축물 {class_name} 현황',
            'status': 'MISSING',
            'count_header': '대장 건수(건)',
            'count_basis': '편입필지 관련 건축물대장 표제부 · 건물 도형 편입 미확인',
            'as_of': as_of,
            'source': '국토교통부 건축HUB 건축물대장 표제부',
            'rows': [], 'notes': [], 'analysis_text': '',
            'known_count': None, 'total_count': None, 'missing_count': None,
        }
        item['survey_table'] = table
        if not valid_as_of or parcel_status not in ('COMPLETE', 'REFERENCE') or not land:
            table['notes'] = ['지적자료 또는 기준일 미확보. 관련 건축물대장 분포를 산정하지 않습니다.',
                              '미확보는 0건 또는 건축물이 없다는 뜻이 아닙니다.']
            continue
        if completed == 0:
            table['notes'] = ['건축물대장 조회가 완료되지 않았습니다. 0건으로 표시하지 않습니다.']
            continue
        if expected_total == 0:
            # A fully queried empty title response is not proof of no buildings.
            if all_queried:
                table.update(status='PARTIAL', total_count=0, known_count=0, missing_count=0,
                             rows=[{'kind': 'total', 'label': '조회 대장 계', 'count': 0,
                                    'pct': None, 'note': '대장 응답 0건'}],
                             analysis_text='조회한 편입필지 관련 건축물대장 표제부의 응답은 0건이다. 실제 건축물이 없다는 의미는 아니다.')
                table['notes'] = ['대장 표제부 응답 0건은 무허가·미등재 건축물의 부재를 증명하지 않습니다.',
                                  '건물별 구역계 편입 여부는 확인되지 않았습니다.']
            else:
                table['notes'] = ['일부 필지에서만 건축물대장 조회 완료. 미조회 필지의 건축물 수를 알 수 없습니다.']
            continue

        counts = Counter()
        missing = 0
        for row in buildings:
            cat = _classification(item_id, row.get(field))
            if cat is None:
                missing += 1
            else:
                counts[cat] += 1
        known = sum(counts.values())
        total = expected_total
        rows = [{'kind': 'total', 'label': '조회 대장 계', 'count': total,
                 'pct': 100.0, 'note': '확보된 표제부 기준'}]
        for label in _ordered_labels(item_id, counts):
            rows.append({'kind': 'category', 'label': label, 'count': counts[label],
                         'pct': round(counts[label] / total * 100, 1), 'note': ''})
        if missing:
            rows.append({'kind': 'missing', 'label': '해당 속성 미확보', 'count': missing,
                         'pct': round(missing / total * 100, 1), 'note': '0 또는 미해당이 아님'})
        source_complete = parcel_status == 'COMPLETE' and all_queried
        missing_ids = sum(not row.get('id') for row in buildings)
        # "COMPLETE" refers to THIS TABLE's source records, not physical footprints.
        has_outlier = item_id == '3-6' and '100% 초과(원자료 확인 필요)' in counts
        complete = source_complete and missing == 0 and missing_ids == 0 and not has_outlier
        table.update(status='COMPLETE' if complete else 'PARTIAL', rows=rows,
                     total_count=total, known_count=known, missing_count=missing)
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:2]
        summary = ' · '.join(f'{label} {cnt}건({cnt / total * 100:.1f}%)' for label, cnt in top)
        description = (f'조회된 편입필지 관련 건축물대장 표제부 총 {total}건을 기준으로 '
                       f'{class_name} 분포를 분석한 결과, 속성이 확인된 대장은 {known}건이다.')
        if summary:
            description += f' 주요 분류는 {summary}이다.'
        if missing:
            description += f' 해당 속성 미확보는 {missing}건({missing / total * 100:.1f}%)이다.'
        if not all_queried:
            description += f' 대상지 관련 {len(land)}필지 중 건축물대장 조회 완료는 {completed}필지로, 미조회 필지의 건축물은 집계에 포함되지 않았다.'
        description += ' 이 통계는 관련 대장 기준이며, 건물별 구역계 편입 여부는 확인되지 않았다.'
        if item_id == '3-4':
            description += ' 경과연수 분류는 법정 노후·불량 건축물 판정이 아니다.'
        table['analysis_text'] = description
        table['notes'] = [
            '구성비의 분모는 조회·확보된 표제부 건수이며, 건축물 실재 동수·구역 내 편입 동수와 다를 수 있습니다.',
            '분류별 대장 건수의 구성비이며, 대지면적·건축면적·연면적 대비 비율이 아닙니다.',
            '구성비는 소수 첫째 자리 반올림으로 합계에 오차가 있을 수 있습니다.',
        ]
        if not source_complete:
            table['notes'].append(f'지적 확보수준 {parcel_status}, 관련 필지 {len(land)}필지 중 대장 조회 {completed}필지: 잠정집계입니다.')
        if missing:
            table['notes'].append('원자료 속성 미확보 분류를 포함하며 임의 분류·보정하지 않았습니다.')
        if missing_ids:
            table['notes'].append(f'관리번호 미확보 {missing_ids}건: 중복 건축물대장 여부 확인 필요.')
        if item_id == '3-4':
            table['notes'].append('사용승인일부터 기준일 현재 만 경과연수. 10년 미만/10~20년 미만/20~30년 미만/30년 이상 설명용 구간이며 법정 노후도 판정 아님.')
        if item_id in ('3-5', '3-6', '3-7'):
            table['notes'].append('설명용 계급 구간이며 법정 규제기준이나 사업판정 기준이 아닙니다. 원자료 대장 수치를 그대로 사용합니다.')
        if item_id == '3-6' and any(row.get('label','').startswith('100% 초과') for row in rows):
            table['notes'].append('100% 초과 건폐율 원자료 이상치가 있어 대장 확인 필요.')
