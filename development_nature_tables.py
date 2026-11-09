"""Conservative FACT-only nature survey tables (4-1 through 4-8).

Never infer area from WMS pixels, fill NoData, convert map categories into
statutory grades, or describe partial weather as annual climatology.
"""
from collections import defaultdict
import math

NATURE = {
    '4-1': '표고분석', '4-2': '경사분석', '4-3': '생태자연도',
    '4-4': '국토환경성평가', '4-5': '임상도', '4-6': '수리 수문',
    '4-7': '기상 기후', '4-8': '단면분석',
}
NUMERIC_COLUMNS = [('label', '구분'), ('value', '값'), ('unit', '단위'), ('note', '비고')]
AREA_COLUMNS = [('label', '구분'), ('area_m2', '면적(㎡)'), ('pct', '구역계 대비(%)'),
                ('count', '도형수'), ('note', '비고')]
PROFILE_COLUMNS = [('label', '단면방향'), ('count', '유효 표본수'),
                   ('min_m', '최저표고(m)'), ('max_m', '최고표고(m)'), ('note', '비고')]


def _n(value, minimum=None):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        return None
    return number


def _rounded(value, digits=2):
    n = _n(value)
    return round(n, digits) if n is not None else None


def _row(label, value, unit, note=''):
    return {'kind': 'metric', 'label': label, 'value': _rounded(value),
            'unit': unit, 'note': note}


def _fill_terrain(table, info, key, status):
    vals = (info or {}).get('values')
    data = vals if isinstance(vals, dict) else None
    if not data:
        table['notes'].append('공식 DEM 통계값 미확보. 지도·항공사진의 색상으로 지형수치를 추정하지 않습니다.')
        return
    metric = '표고' if key == '4-1' else '경사'
    unit = 'm' if key == '4-1' else '°'
    values = [_n(data.get(k)) for k in ('min', 'max', 'mean')]
    if any(x is None for x in values):
        table['notes'].append('DEM 최저·최고·평균값 중 미확보가 있어 통계표를 작성하지 않습니다.')
        return
    covered = _n(info.get('elevation_coverage_pct' if key == '4-1'
                          else 'slope_coverage_pct'))
    if covered is None or covered > 100.01:
        table['notes'].append('DEM 유효격자 면적 범위 확인 불가. 통계표를 작성하지 않습니다.')
        return
    table['columns'] = NUMERIC_COLUMNS
    table['rows'] = [
        _row('최저'+metric, values[0], unit),
        _row('최고'+metric, values[1], unit),
        _row('면적가중 평균'+metric, values[2], unit),
        _row('유효격자 면적비율', covered, '%', '유효격자 면적 / 구역계 면적'),
        _row('DEM 격자 크기', info.get('resolution_m'), 'm', '원자료 격자해상도'),
    ]
    table['status'] = 'COMPLETE' if status == 'DATA' and covered >= 99.9 else 'PARTIAL'
    table['analysis_text'] = (
        f'확보된 DEM 유효격자 기준 {metric}은 최저 {values[0]:,.2f}{unit}, '
        f'최고 {values[1]:,.2f}{unit}, 면적가중 평균 {values[2]:,.2f}{unit}로 분석된다. '
        f'{metric} 통계 산정에 사용된 격자의 면적비율은 {covered:.1f}%이다. '
        '이 수치는 원자료 해상도와 확보범위에 따른 기초조사 참고값이며 법적 판정 결과가 아니다.'
    )
    table['notes'].append('최소·최대는 유효격자의 관측값이며 평균만 면적가중 수치입니다.')
    if covered < 99.9:
        table['notes'].append('유효격자 미확보 면적이 있어 구역계 전체의 대표값으로 확정할 수 없습니다.')


def _fill_vector(table, item, st, raw, site_area):
    """Group source classifications only; retain possibly overlapping areas."""
    groups = (item.get('legend') or []) if item['id'] == '4-3' else []
    if not groups:
        groups = (st or {}).get('groups') or []
    raw_status = (raw or {}).get('status')
    if not groups or raw_status not in ('DATA', 'PARTIAL'):
        table['notes'].append('공식 분류별 벡터 면적·속성 미확보. WMS 화면을 면적표로 환산하지 않습니다.')
        if item['id'] == '4-4' and item.get('map_available'):
            table['notes'].append('국토환경성평가 WMS 도면만 확보: 등급별 면적과 구성비는 미산정합니다.')
        return
    if site_area is None or site_area <= 0:
        table['notes'].append('확정 구역계 면적 미확보. 공간 통계표를 작성하지 않습니다.')
        return
    classified = defaultdict(lambda: {'area_m2': 0.0, 'count': 0})
    unclassified = False
    for entry in groups:
        name = str(entry.get('name') or '').strip()
        area = _n(entry.get('area_m2'), 0)
        count = entry.get('count', 1)
        if not name or area is None or isinstance(count, bool) or not isinstance(count, int) or count < 0:
            table['notes'].append('벡터 원자료 분류·면적·도형수 검증 실패. 불완전 통계표를 작성하지 않습니다.')
            return
        classified[name]['area_m2'] += area
        classified[name]['count'] += count
        if '미확보' in name or '미분류' in name:
            unclassified = True
    covered_area = _n((st or {}).get('overlap_area_m2'), 0)
    if covered_area is None:
        table['notes'].append('원도형 합집합 면적 미확보. 분류별 총면적을 검증할 수 없습니다.')
        return
    class_sum = sum(r['area_m2'] for r in classified.values())
    overlap = max(0.0, class_sum - covered_area)
    table['columns'] = AREA_COLUMNS
    table['rows'] = [{
        'kind': 'total', 'label': '조회도형 합집합', 'area_m2': round(covered_area, 2),
        'pct': round(100 * covered_area / site_area, 1), 'count': None,
        'note': '중복 도형은 합집합으로 제거',
    }]
    for label in sorted(classified):
        entry = classified[label]
        table['rows'].append({
            'kind': 'category', 'label': label,
            'area_m2': round(entry['area_m2'], 2),
            'pct': round(100 * entry['area_m2'] / site_area, 1),
            'count': entry['count'], 'note': '분류별 면적(중복 가능)',
        })
    if overlap > max(0.05, site_area * 0.00001):
        table['rows'].append({
            'kind': 'warning', 'label': '분류면적 단순합과 합집합 차이',
            'area_m2': round(overlap, 2),
            'pct': round(100 * overlap / site_area, 1),
            'count': None, 'note': '등급·분류 간 중첩 주의',
        })
    source_verified = raw_status == 'DATA' and item.get('provider_verified') is True
    site_cover_ok = abs(site_area - covered_area) <= max(0.5, site_area * 0.001)
    table['status'] = ('COMPLETE' if source_verified and not unclassified
                       and site_cover_ok and overlap <= max(0.05, site_area * 0.00001) else 'PARTIAL')
    table['analysis_text'] = (
        f"확정 구역계와 교차하는 {table['title'].replace('표 '+item['id']+' ', '')} "
        f'원도형의 합집합 면적은 {covered_area:,.2f}㎡로 구역계의 '
        f'{100 * covered_area / site_area:.1f}%이다. '
        '분류별 면적은 제공된 원도형을 기준으로 작성하였으며 분류 간 중첩이나 미확보 면적을 별도로 확인해야 한다.'
    )
    table['notes'].extend([
        '구성비는 확정 구역계 면적 대비이며 조회된 자료가 전체 구역계를 피복한다고 가정하지 않습니다.',
        '분류별 면적은 원도형 면적의 합계이며, 분류 간 중첩 시 구성비를 단순 합산하면 100%를 초과할 수 있습니다.',
        '등급·분류는 공식 원도형의 원문값으로, 법정 입지 적합성 판정과 다릅니다.',
    ])
    if not site_cover_ok:
        table['notes'].append('조회도형 합집합 면적과 확정 구역계 면적에 차이가 있어 피복범위를 확인해야 합니다.')
    if unclassified:
        table['notes'].append('원자료 분류 미확보 도형이 있어 등급의 완전한 분포를 확정하지 않습니다.')
    if overlap > max(0.05, site_area * 0.00001):
        table['notes'].append(f'분류 간 중첩 가능 면적 차이 {overlap:,.2f}㎡. 분류값별 면적 합계와 도형 합집합의 차이입니다.')
    if item['id'] == '4-3':
        table['notes'].append('EcoBank WFS 등급값은 최신 생태·자연도 고시 원본과의 일치 여부를 별도로 확인합니다.')
    if item['id'] == '4-5':
        table['notes'].append('임상·영급·수종·경급 중 해당 원자료에 연결된 분류만 표시합니다. 없는 속성을 보간하지 않습니다.')
    if item['id'] == '4-6':
        table['notes'].append('수리·수문 원도형 교차현황이며 홍수위·침수위험의 법정 판정은 수행하지 않습니다.')

