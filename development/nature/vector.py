"""Official vector FACT -> classified map + survey table for nature items 4-4..4-6.

4-3 EcoBank has its own authoritative WFS class/area engine and is intentionally
not recalculated here. All values here derive from the SAME site-clipped source
features that are emitted for SVG/DXF/web map. Not a statutory interpretation.

Polygon areas / line lengths are measured in EPSG:5179 metres. Point geometries
are counted, never converted into invented areas. Empty/no-source remains MISSING.
"""
from collections import defaultdict
from pyproj import Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform, unary_union

KEYS = frozenset(('4-4', '4-5', '4-6'))
PALETTE = (
    '#b4d2ef', '#729ecb', '#245b95', '#75beae', '#377e73',
    '#f5d67d', '#e99b62', '#cc7378', '#a995d1', '#848a97',
)
PROJECT = Transformer.from_crs('EPSG:4326', 'EPSG:5179', always_xy=True).transform
INVERSE = Transformer.from_crs('EPSG:5179', 'EPSG:4326', always_xy=True).transform
MAX_SOURCE_FEATURES = 5000
MAX_CLASSES = 99

COLUMNS = [
    ('label', '구분'), ('area_m2', '면적(㎡)'),
    ('pct', '구역계 대비(%)'), ('value', '연장(m)'),
    ('count', '도형수·개소수'), ('note', '비고'),
]


def _parts(geom):
    if geom.is_empty:
        return
    if geom.geom_type in ('Polygon', 'LineString', 'LinearRing', 'Point'):
        yield geom
    elif hasattr(geom, 'geoms'):
        for part in geom.geoms:
            yield from _parts(part)


def _dimension(part):
    if part.geom_type == 'Polygon':
        return '면'
    if part.geom_type in ('LineString', 'LinearRing'):
        return '선'
    if part.geom_type == 'Point':
        return '점'
    return None


def _is_missing(label):
    return not label.strip() or label.strip() in ('분류 미확보', '명칭 미확보', '미분류')


def _round(n):
    return round(n, 2) if n is not None else None


def _empty(item, message):
    item['table_available'] = False
    item['provider_verified'] = False
    item['nature_vector_status'] = 'MISSING'
    item['nature_vector_reason'] = message
    # Existing source status / raw map availability are NOT overwritten.
    item['features'] = []
    item.pop('legend', None)
    item.pop('nature_vector_summary', None)


def attach_nature_vector_themes(data, snapshot):
    """Attach official classified 4-4..4-6 features, summary, and safety flags.

    Do not silently turn invalid geometry / missing source into "no restrictions".
    Each category uses a UNION of its original features (not a sum of overlaps).
    Distinct layers and geometry dimensions remain separate, particularly 4-6.
    """
    items = {x.get('id'): x for x in data.get('items') or [] if x.get('id') in KEYS}
    facts = snapshot.get('_national_sources') or {}
    try:
        site = transform(PROJECT, shape(snapshot['geometry']))
        if site.is_empty or not site.is_valid or site.area <= 0:
            raise ValueError('invalid site')
    except (KeyError, TypeError, ValueError):
        for item in items.values():
            _empty(item, '확정 구역계 좌표·면적 검증 실패')
        return

    for key, item in items.items():
        raw = facts.get('file:' + key) or {}
        if raw.get('status') not in ('DATA', 'PARTIAL'):
            # Keep WMS-only 4-4 map display, but forbid its raster as statistics.
            _empty(item, '공식 벡터 원도형 미확보 · WMS 영상으로 면적을 추정하지 않음')
            continue

        incoming = raw.get('features')
        if not isinstance(incoming, list) or not incoming:
            _empty(item, '공식 원도형이 대상지와 교차하지 않음 또는 도형 미확보 · 비해당 확정 불가')
            continue
        if len(incoming) > MAX_SOURCE_FEATURES:
            _empty(item, '주제도형 수가 출력 상한을 초과하여 불완전한 도면 생성 차단')
            continue

        # Key = (original source layer, original class, geometry dimension).
        group_shapes = defaultdict(list)
        group_feature_ids = defaultdict(set)
        staged = []
        missing_class = False
        invalid = False
        try:
            for index, feature in enumerate(incoming):
                geom = shape(feature['geometry'])
                if geom.is_empty or not geom.is_valid:
                    raise ValueError('invalid original geometry')
                source_part = transform(PROJECT, geom).intersection(site)
                if source_part.is_empty:
                    continue
                props = feature.get('properties') or {}
                name = str(props.get('name') or '분류 미확보').strip()[:120]
                source_layer = str(props.get('source_layer') or
                                   props.get('layer') or key).strip()[:100] or key
                if _is_missing(name):
                    missing_class = True
                    name = '분류 미확보'
                for part in _parts(source_part):
                    dimension = _dimension(part)
                    if dimension is None:
                        raise ValueError('unhandled spatial dimension')
                    if key in ('4-4', '4-5') and dimension != '면':
                        # These two reports require classification-area polygons.
                        raise ValueError('non-polygon environmental vector')
                    if ((dimension == '면' and part.area <= 0) or
                            (dimension == '선' and part.length <= 0)):
                        continue
                    groupkey = (source_layer, name, dimension)
                    group_shapes[groupkey].append(part)
                    group_feature_ids[groupkey].add(index)
                    staged.append((groupkey, part))
            if not staged:
                _empty(item, '공식 원도형 교차 도형 0건 · 면적표 미산정')
                continue
            keys = sorted(group_shapes)
            if len(keys) > MAX_CLASSES:
                raise ValueError('too many category classes')
            code = {label: (f'C{i+1:02d}', PALETTE[i % len(PALETTE)])
                    for i, label in enumerate(keys)}
            transformed = []
            for label, geom in staged:
                class_code, color = code[label]
                source_layer, name, dimension = label
                caption = (name if key != '4-6' else f'{source_layer} · {name} ({dimension})')
                transformed.append({
                    'type': 'Feature',
                    'geometry': mapping(transform(INVERSE, geom)),
                    'properties': {'name': caption, 'class_code': class_code,
                                   'color': color, 'source_layer': source_layer,
                                   'dimension': dimension},
                })
            if len(transformed) > MAX_SOURCE_FEATURES:
                raise ValueError('too many clipped parts')
            grouped = []
            all_polys = []
            all_lines = []
            for label in keys:
                source_layer, name, dimension = label
                union = unary_union(group_shapes[label])
                if union.is_empty or not union.is_valid:
                    raise ValueError('invalid category union')
                c, color = code[label]
                area_m2 = union.area if dimension == '면' else None
                length_m = union.length if dimension == '선' else None
                if dimension == '면':
                    all_polys.append(union)
                elif dimension == '선':
                    all_lines.append(union)
                caption = (name if key != '4-6' else f'{source_layer} · {name} ({dimension})')
                grouped.append({'name': caption, 'class_code': c, 'color': color,
                                'source_layer': source_layer, 'dimension': dimension,
                                'area_m2': _round(area_m2), 'length_m': _round(length_m),
                                'count': len(group_feature_ids[label])})
            poly_union = unary_union(all_polys) if all_polys else None
            line_union = unary_union(all_lines) if all_lines else None
            total_area = poly_union.area if poly_union is not None else None
            total_line = line_union.length if line_union is not None else None
            area_overlap = (max(0, sum(g['area_m2'] for g in grouped
                                       if g['area_m2'] is not None) - total_area)
                            if total_area is not None else 0)
            line_overlap = (max(0, sum(g['length_m'] for g in grouped
                                       if g['length_m'] is not None) - total_line)
                            if total_line is not None else 0)
            rows = []
            if total_area is not None:
                rows.append({'kind': 'total', 'label': '면 도형 합집합',
                             'area_m2': _round(total_area),
                             'pct': round(100 * total_area / site.area, 1),
                             'value': None, 'count': None,
                             'note': 'EPSG:5179 평면면적 · 중첩 도형 합집합'})
            if total_line is not None:
                rows.append({'kind': 'total', 'label': '선 도형 합집합',
                             'area_m2': None, 'pct': None, 'value': _round(total_line),
                             'count': None, 'note': 'EPSG:5179 평면연장 · 중복선 합집합'})
            for g in grouped:
                rows.append({'kind': 'category', 'label': g['name'],
                             'area_m2': g['area_m2'],
                             'pct': round(g['area_m2'] * 100 / site.area, 1)
                             if g['area_m2'] is not None else None,
                             'value': g['length_m'], 'count': g['count'],
                             'note': ('분류 미확보 · 수치 검증 필요' if '분류 미확보' in g['name']
                                      else g['dimension']+' 도형 분류')})
            if area_overlap > max(.05, site.area * .00001):
                rows.append({'kind': 'warning', 'label': '면 분류간 중첩',
                             'area_m2': _round(area_overlap),
                             'pct': round(100 * area_overlap / site.area, 1),
                             'value': None, 'count': None,
                             'note': '분류별 합과 전체 면 합집합의 차이'})
            if line_overlap > .05:
                rows.append({'kind': 'warning', 'label': '선 분류간 중복',
                             'area_m2': None, 'pct': None, 'value': _round(line_overlap),
                             'count': None, 'note': '분류별 연장의 합과 전체 선 합집합의 차이'})

            dominant = max((g for g in grouped if g['area_m2'] is not None),
                           key=lambda g: g['area_m2'], default=None)
            intro = {
                '4-4': '국토환경성평가',
                '4-5': '임상도',
                '4-6': '수리·수문',
            }[key]
            description = f'{intro}는 등록된 공식 원도형과 확정 구역계의 교차현황을 분석하였다.'
            if total_area is not None:
                description += f' 면 도형 합집합은 {total_area:,.2f}㎡로 구역계의 {total_area/site.area*100:.1f}%이다.'
            if total_line is not None:
                description += f' 선 도형 합집합 연장은 {total_line:,.2f}m이다.'
            if dominant:
                description += f" 주요 면적 분류는 '{dominant['name']}'이며 {dominant['area_m2']:,.2f}㎡이다."
            description += (' 본 결과는 원도형 분류와 중첩현황을 나타내는 기초조사이며 '
                            '개발·재해의 법정 적합성 판정이 아니다.')
            if raw.get('status') == 'PARTIAL':
                description += ' 일부 원자료 분류가 미확보되어 잠정집계이다.'
            notes = [
                '원자료 분류명·레이어명을 변경하지 않고 사용했습니다.',
                '면적은 EPSG:5179의 구역계 교차도형 평면면적 기준입니다.',
                '면적·연장·개소수는 서로 다른 측정단위이며 합산하지 않습니다.',
                '필요한 원도형 미수록·미확보를 법정 비해당으로 해석하지 않습니다.',
            ]
            if key == '4-5':
                notes.append('임상·영급·수종·경급 중 제공기관 원본에 등록된 분류만 표시하며 임의 보간하지 않습니다.')
            if key == '4-6':
                notes.append('하천·수계·유역·홍수 관련 벡터는 실제 등록된 레이어에 한해 집계합니다. 인접거리 및 홍수위 검토는 별도입니다.')
            if area_overlap > max(.05, site.area * .00001) or line_overlap > .05:
                notes.append('분류별 단순 합산은 중첩을 포함하므로 합집합 합계와 다릅니다.')
            if missing_class:
                notes.append('원자료에 분류 미확보가 포함되므로 등급을 임의 추정하지 않습니다.')

            complete = (raw.get('status') == 'DATA' and not missing_class and
                        area_overlap <= max(.05, site.area * .00001))
            item['features'] = transformed
            item['legend'] = grouped
            item['thematic_type'] = 'nature_vector'
            item['provider_verified'] = bool(complete)
            item['table_available'] = True
            item['nature_vector_status'] = 'COMPLETE' if complete else 'PARTIAL'
            item['nature_vector_summary'] = {
                'status': item['nature_vector_status'], 'columns': COLUMNS,
                'rows': rows, 'notes': notes,
                'analysis_text': description,
                'source': raw.get('source'), 'source_as_of': raw.get('as_of'),
                'site_area_m2_epsg5179': round(site.area, 2),
                'overlap_area_m2_epsg5179': _round(total_area),
                'length_m_epsg5179': _round(total_line),
            }
            data.setdefault('statistics', {})[key] = {
                'groups': grouped,
                'overlap_area_m2': _round(total_area),
                'length_m': _round(total_line),
                'site_area_m2_epsg5179': round(site.area, 2),
                'overlap_warning_m2': _round(area_overlap),
                'overlap_warning_length_m': _round(line_overlap),
                'note': '공식 원도형 원문 분류별 집계. 면·선·점 단위 분리.',
            }
        except (KeyError, ValueError, TypeError, OverflowError):
            invalid = True
        if invalid:
            _empty(item, '공식 원도형 분류·위상·변환 검증 오류로 도면·면적표 생성을 보류합니다.')
