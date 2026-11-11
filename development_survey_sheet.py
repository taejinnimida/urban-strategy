"""One print-sheet geometry spec for vector SVG previews and editable A3 DXF layouts.

Only the authenticated nationwide report's frozen FACT is accepted.  No new
external provider calls or client API credentials are added here.
"""
import math
import re
from xml.sax.saxutils import escape
from shapely.geometry import Polygon, LineString, Point

from pyproj import Transformer
from shapely.geometry import box, shape
from shapely.ops import transform
from development_planning_symbols import style_for, style_from_properties, hatch_pattern, stroke_line_symbol, rgb_for_aci, color_aci

SHEET_W, SHEET_H = 580.0, 380.0
MAP = (2.0, 2.0, 485.0, 376.0)  # x, y, width, height
PANEL_X, PANEL_W = 491.0, 82.0
PAPER_W, PAPER_H = 420.0, 297.0
SCALE = (PAPER_W - 8.0) / SHEET_W
PAPER_DY = (PAPER_H - SHEET_H * SCALE) / 2.0
_CRS = Transformer.from_crs('EPSG:4326', 'EPSG:5179', always_xy=True).transform
_COLOR = re.compile(r'^#[0-9a-fA-F]{6}$')


def _color(value, default):
    return value if isinstance(value, str) and _COLOR.fullmatch(value) else default


def _title(item):
    return {'1-1': '위치 및 면적', '2-1': '소유별', '2-2': '지목별',
            '2-3': '면적별', '2-4': '공시지가별',
            '2-5': '토지형상별', '2-6': '토지피복별',
            '5-2': '용도지역 현황도', '5-3': '용도지구 현황도',
            '5-4': '용도구역 현황도', '5-5': '교통시설 현황도',
            '5-6': '공간시설 현황도', '5-7': '기타 도시계획시설 현황도',
            '5-8': '지구단위계획구역 현황도'}.get(item.get('id'),
            str(item.get('name') or '현황도')[:16])


def _poly_points(poly, xy):
    rings = [[xy(x, y) for x, y in list(poly.exterior.coords)]]
    rings.extend([[xy(x, y) for x, y in list(r.coords)] for r in poly.interiors])
    return rings


def _geometry_parts(geom):
    if geom.is_empty:
        return
    if geom.geom_type == 'Polygon':
        yield geom
    elif hasattr(geom, 'geoms'):
        for part in geom.geoms:
            yield from _geometry_parts(part)


def make_sheet(item, snapshot):
    """Return rendering primitives in a 580x380 page coordinate system."""
    site = transform(_CRS, shape(snapshot['geometry']))
    if site.is_empty or not site.is_valid or site.area <= 0:
        raise ValueError('invalid site boundary')
    cadastre = snapshot.get('_cad_background') or {}
    if len(cadastre.get('features') or []) > 6000 or len(item.get('features') or []) > 6000:
        raise ValueError('drawing feature count exceeds audited limit')
    if cadastre.get('status') != 'COMPLETE':
        raise ValueError('surrounding official cadastral context not acquired')
    xmin, ymin, xmax, ymax = site.bounds
    cx, cy = (xmin + xmax) / 2.0, (ymin + ymax) / 2.0
    width, height = max(xmax - xmin, 0.01), max(ymax - ymin, 0.01)
    metres_per_px = max(2.0 * width / MAP[2], 2.0 * height / MAP[3])
    view = box(cx - MAP[2] * metres_per_px / 2.0,
               cy - MAP[3] * metres_per_px / 2.0,
               cx + MAP[2] * metres_per_px / 2.0,
               cy + MAP[3] * metres_per_px / 2.0)

    def xy(x, y):
        return (MAP[0] + MAP[2] / 2.0 + (x - cx) / metres_per_px,
                MAP[1] + MAP[3] / 2.0 - (y - cy) / metres_per_px)

    def primitive(kind, **kwargs):
        return {'type': kind, **kwargs}

    ops = [primitive('rect', x=0.5, y=0.5, w=579, h=379, fill='#ffffff', stroke='#949494', weight=1)]
    # Draw only the clipped cadastral context; do not fill the surroundings.
    for feature in (cadastre.get('features') or [])[:6000]:
        try:
            geom = transform(_CRS, shape(feature['geometry']))
            if geom.is_empty or not geom.is_valid or not geom.intersects(view):
                continue
            cut = geom.intersection(view)
            for poly in _geometry_parts(cut):
                ops.append(primitive('polygon', rings=_poly_points(poly, xy), fill='none',
                                     stroke='#cbcbcb', weight=0.63, layer='SHEET_CADASTRE'))
        except (KeyError, TypeError, ValueError):
            continue

    # Legible cadastral lot labels (separate from theme classification),
    # limited to avoid visual collision/overplotting at small scales.
    context_end = len(ops)
    # Use only the original, mapped FACT; never invent classifications.
    zone_context = snapshot.get('_cad_zone_background', {})
    use_zone_context = item.get('id') == '5-2' and zone_context.get('status') == 'COMPLETE'
    display_features = zone_context.get('features', []) if use_zone_context else (item.get('features') or [])
    for feature in display_features[:6000]:
        try:
            g = shape(feature['geometry'])
            if not g.is_valid or g.is_empty:
                continue
            projected_g = transform(_CRS, g)
            cut = projected_g.intersection(view) if use_zone_context else projected_g.intersection(site).intersection(view)
            props = feature.get('properties') or {}
            color = _color(props.get('color'), '#afd0e4')
            plan_group = ('zoning' if item.get('id') == '5-2' else
                          'district' if item.get('id') == '5-3' else
                          'area' if item.get('id') in ('5-4', '5-8') else
                          'facility' if item.get('id') in ('5-5', '5-6', '5-7') else None)
            draw_style = style_from_properties(props, plan_group) if plan_group else None
            cls = str(props.get('class_code') or 'C01')
            if not re.fullmatch(r'C\d{2}', cls):
                cls = 'C01'
            boundary_color = (rgb_for_aci(color_aci(draw_style.get('boundary_color') or
                              draw_style.get('line_color') or 'red')) if
                              draw_style and draw_style.get('group') != 'zoning' else '#a5a5a5')
            for poly in _geometry_parts(cut):
                ops.append(primitive('polygon', rings=_poly_points(poly, xy),
                                     fill=('none' if draw_style and draw_style.get('fill_mode') != 'solid' else color),
                                     stroke=boundary_color, weight=0.32, layer='SHEET_THEME_' + cls,
                                     symbol=draw_style))
            if draw_style and draw_style.get('group') == 'facility':
                lines = [cut] if cut.geom_type == 'LineString' else [
                    q for q in getattr(cut, 'geoms', []) if q.geom_type == 'LineString']
                for ln in lines:
                    ops.append(primitive('polyline', points=[xy(x,y) for x,y in ln.coords],
                                         fill='none', stroke=rgb_for_aci(color_aci(draw_style.get('line_color') or 'red')),
                                         weight=0.7, layer='SHEET_THEME_' + cls, symbol=draw_style))
        except (KeyError, TypeError, ValueError):
            continue

    # Printed plan names use the guideline's group-specific ink and remain
    # physically inside an actual source polygon. Do not invent facility IDs.
    plan_labels = []
    if item.get('id') in ('5-2','5-3','5-4','5-5','5-6','5-7','5-8'):
        occupied_plan = []
        for feature in display_features[:6000]:
            try:
                p = feature.get('properties') or {}
                group = ('zoning' if item['id']=='5-2' else
                         'district' if item['id']=='5-3' else
                         'area' if item['id'] in ('5-4','5-8') else 'facility')
                st = style_from_properties(p,group)
                if not st.get('verified_style'):continue
                geom = transform(_CRS,shape(feature['geometry']))
                clipped = geom.intersection(view if use_zone_context else site.intersection(view))
                for poly in _geometry_parts(clipped):
                    center = poly.representative_point()
                    px,py=xy(center.x,center.y)
                    if not (MAP[0]+30<px<MAP[0]+MAP[2]-30 and MAP[1]+9<py<MAP[1]+MAP[3]-9):continue
                    label = st.get('name') or p.get('name')
                    if not label or len(label)>24:continue
                    text_width = max(25, len(label)*5.2)
                    if poly.area < (metres_per_px*text_width)*(metres_per_px*9):continue
                    rect=(px-text_width/2,py-6,px+text_width/2,py+6)
                    if any(rect[0]<b[2] and rect[2]>b[0] and rect[1]<b[3] and rect[3]>b[1] for b in occupied_plan):continue
                    occupied_plan.append(rect)
                    text_color=rgb_for_aci(color_aci(st.get('label_color') or 'black'))
                    plan_labels.append(primitive('text',x=px,y=py+2.0,text=label,size=7,
                                                 color=text_color,anchor='middle',layer='SHEET_PLAN_LABEL'))
                    if st.get('school_variant'):
                        plan_labels.append(primitive('school_mark', x=px,y=py+9.0,
                                                     variant=st['school_variant'],size=6.0,
                                                     layer='SHEET_PLAN_SYMBOL'))
                    if len(plan_labels)>=120:break
                if len(plan_labels)>=120:break
            except (ValueError,TypeError,KeyError):continue

    # The same paint order is used for the SVG and DXF: fill, cadastral
    # outlines, labels, then red site border. No independent invented FACT.
    ops[1:] = ops[context_end:] + ops[1:context_end]
    ops.extend(plan_labels)
    # Only an unobstructed subset of lot numbers is plotted. Full PNU remains
    # in the model-space cadastral geometry and inventory; no label is invented.
    occupied_labels = []
    for feature in (cadastre.get('features') or [])[:6000]:
        props = {str(k).lower(): v for k, v in (feature.get('properties') or {}).items()}
        label = str(props.get('jibun') or '').strip()
        if not label or len(label) > 16:
            continue
        try:
            g = transform(_CRS, shape(feature['geometry']))
            if not g.is_valid or not g.intersects(view):
                continue
            centroid = g.intersection(view).representative_point()
            x, y = xy(centroid.x, centroid.y)
            if not (MAP[0]+8 < x < MAP[0]+MAP[2]-8 and MAP[1]+6 < y < MAP[1]+MAP[3]-6):
                continue
            width = max(10, 3.5 * len(label))
            extent = (x-width/2-2,y-4,x+width/2+2,y+4)
            if any(extent[0]<b[2] and extent[2]>b[0] and extent[1]<b[3] and extent[3]>b[1] for b in occupied_labels):
                continue
            occupied_labels.append(extent)
            ops.append(primitive('text',x=x,y=y+1.8,text=label,size=5.8,anchor='middle',layer='SHEET_PARCEL_LABEL'))
            if len(occupied_labels)>=300:
                break
        except (TypeError,ValueError,KeyError):
            continue

    # Draw the site outline over the thematic fill and cadastral context.
    for poly in _geometry_parts(site.intersection(view)):
        for ring in _poly_points(poly, xy):
            ops.append(primitive('polyline', points=ring, stroke='#fa3737', weight=2.2,
                                 dash='7,4', layer='SHEET_SITE'))

    # Fixed-page title, legend and north/scale columns: same for SVG and DXF.
    ops.extend([
        primitive('rect', x=PANEL_X, y=5, w=PANEL_W, h=28, fill='#ffffff', stroke='#aaa', weight=0.8),
        primitive('text', x=532, y=23, text=_title(item), size=11.5, bold=True, anchor='middle'),
        primitive('rect', x=PANEL_X, y=39, w=PANEL_W, h=280, fill='#ffffff', stroke='#aaa', weight=0.8),
        primitive('text', x=532, y=54, text='범 례', size=9.3, bold=True, anchor='middle'),
        primitive('rect', x=PANEL_X, y=322, w=PANEL_W, h=54, fill='#ffffff', stroke='#aaa', weight=0.8),
    ])
    legend = list(zone_context.get('legend') or item.get('legend') or []) if use_zone_context else list(item.get('legend') or [])
    if item.get('id') == '1-1':
        # 1-1 is a location/boundary map, not an absent thematic polygon.
        # Explain the real geometry already printed on the paper sheet.
        ops.append(primitive('polyline', points=[(497, 69), (504, 69)],
                             stroke='#fa3737', weight=1.6, dash='7,4',
                             layer='SHEET_DECOR'))
        ops.append(primitive('text', x=507, y=71.2, text='확정 대상지', size=6.9))
        ops.append(primitive('polyline', points=[(497, 81), (504, 81)],
                             stroke='#cbcbcb', weight=0.7, layer='SHEET_DECOR'))
        ops.append(primitive('text', x=507, y=83.2, text='주변 연속지적', size=6.9))
    elif item.get('thematic_type') == 'building' and item.get('geometry_status') == 'COMPLETE' and not display_features:
        ops.append(primitive('text', x=497, y=73, text='건물 도형 조회 0건', size=7.0))
        ops.append(primitive('text', x=497, y=84, text='실제 부존재 미확정', size=6.1))
    elif not legend and not display_features:
        ops.append(primitive('text', x=497, y=73, text='주제도형 미확보', size=7.3))
    for i, entry in enumerate(legend[:27]):
        y = 66 + i * 9.0
        color = _color(entry.get('color'), '#dddddd')
        plan_group = ('zoning' if item.get('id') == '5-2' else
                      'district' if item.get('id') == '5-3' else
                      'area' if item.get('id') in ('5-4', '5-8') else
                      'facility' if item.get('id') in ('5-5', '5-6', '5-7') else None)
        draw_style = style_for(entry.get('name'), plan_group) if plan_group else None
        ops.append(primitive('rect', x=496, y=y - 5.5, w=7.5, h=7.5,
                             fill=('none' if draw_style and draw_style.get('fill_mode') != 'solid' else color),
                             stroke='#777777', weight=0.35, symbol=draw_style))
        name = str(entry.get('name') or '분류 미확인')
        # Full category values remain in FACT and report tables.
        label = (name[:10] + f' {entry.get("count", 0)}동') if item.get('thematic_type') == 'building' else name[:14]
        ops.append(primitive('text', x=506, y=y + 0.7, text=label, size=7.0))
    if item.get('id') == '5-2' and not use_zone_context:
        ops.append(primitive('text', x=497, y=307, text='주변 용도지역: 미조회', size=6.2))
    if len(legend) > 27:
        ops.append(primitive('text', x=497, y=312, text=f'외 {len(legend) - 27}개: 통계표 참조', size=6.5))

    # Graphic north arrow, fully editable CAD vector.
    ops.extend([
        primitive('text', x=507, y=330, text='N', size=8.3, bold=True, anchor='middle'),
        primitive('circle', x=507, y=340, r=7, stroke='#666666', weight=0.6),
        primitive('polyline', points=[(507, 333), (507, 347)], stroke='#666666', weight=0.65),
        primitive('polygon', rings=[[(507, 334), (504.6, 340), (507, 339.2)]],
                  fill='#333333', stroke='#333333', weight=0.4, layer='SHEET_DECOR'),
    ])
    # Choose a true measured distance whose displayed width fits the panel.
    # Two long segments prevent scale labels from overlapping at small sizes.
    target_distance = 57.0 * metres_per_px
    exp = math.floor(math.log10(max(target_distance, 1e-9)))
    choices = [m * 10 ** k for k in range(exp - 1, exp + 2)
               for m in (1, 2, 4, 5, 10)]
    feasible = [n for n in choices if 42 <= n / metres_per_px <= 66]
    if not feasible:
        feasible = [n for n in choices if n / metres_per_px <= 67]
    total_m = min(feasible or choices, key=lambda n: abs(n / metres_per_px - 57))
    total_px = total_m / metres_per_px
    half_px = total_px / 2
    x0, y0 = 497.0, 365.0
    for i in range(2):
        x = x0 + i * half_px
        ops.append(primitive('rect', x=x, y=y0 - 4, w=half_px, h=4,
                             fill='#333333' if i == 0 else '#ffffff',
                             stroke='#222222', weight=0.3))
    ops.append(primitive('text', x=x0, y=y0 - 6.5, text='0', size=5.6))
    ops.append(primitive('text', x=x0 + half_px, y=y0 - 6.5, text=f'{total_m / 2:g}',
                         size=5.6, anchor='middle'))
    ops.append(primitive('text', x=x0 + total_px, y=y0 - 6.5, text=f'{total_m:g}m',
                         size=5.6, anchor='end'))
    ops.append(primitive('polyline', points=[(x0, y0), (x0 + total_px, y0)],
                         stroke='#111111', weight=0.6))

    ops.append(primitive('rect', x=MAP[0], y=MAP[1], w=MAP[2], h=MAP[3],
                         fill='none', stroke='#acacac', weight=0.65))
    return ops


def _school_mark_shapes(x, y, variant, size):
    """Outlined architectural symbols from the guideline examples (black ink)."""
    k = size/8.0
    def p(*coordinates):
        return [(x+coordinates[i]*k,y+coordinates[i+1]*k)
                for i in range(0,len(coordinates),2)]
    if variant == 'university':
        return [p(-4,-2,0,-4,4,-2), p(-3,-2,-1.8,-2,-1.8,2,-3,2),
                p(1.8,-2,3,-2,3,2,1.8,2), p(-4,2,4,2,4,3,-4,3)]
    if variant == 'secondary':
        return [p(-4,-3,-2.4,-3,-2.4,2,-4,2),
                p(2.4,-3,4,-3,4,2,2.4,2),
                p(-2.4,1,2.4,1,2.4,2.2,-2.4,2.2)]
    if variant == 'elementary':
        return [p(-4,-3,-2,-3,-2,1.5,2.5,1.5,2.5,3,-4,3)]
    return []


def _svg_pattern(style, pid):
    """Physical 1:5,000 guideline spacing interpreted at A3 paper scale."""
    kind = style.get('pattern')
    if kind not in ('diagonal', 'vertical', 'circles'):
        return ''
    pitch = max(0.5, float(style.get('spacing_mm') or 5) / SCALE)
    r = max(0.1, float(style.get('diameter_mm') or 2) / (2 * SCALE))
    from development_planning_symbols import rgb_for_aci
    line = rgb_for_aci(style.get('pattern_aci'))
    out = [f'<pattern id="{pid}" patternUnits="userSpaceOnUse" width="{pitch:.3f}" height="{pitch:.3f}">']
    if style.get('fill_mode') == 'solid':
        out.append(f'<rect width="{pitch:.3f}" height="{pitch:.3f}" fill="{style["color"]}"/>')
    if kind == 'diagonal':
        out.append(f'<path d="M0 {pitch:.3f}L{pitch:.3f} 0" stroke="{line}" stroke-width="0.45"/>')
    elif kind == 'vertical':
        out.append(f'<path d="M{pitch/2:.3f} 0L{pitch/2:.3f} {pitch:.3f}" stroke="{line}" stroke-width="0.45"/>')
    elif kind == 'circles':
        out.append(f'<circle cx="{pitch/2:.3f}" cy="{pitch/2:.3f}" r="{r:.3f}" fill="none" stroke="{line}" stroke-width="0.4"/>')
    out.append('</pattern>')
    return ''.join(out)


def _svg_boundary_marks(rings, symbol):
    """Scaled edge dots/ticks and oil/gas pipeline beads in printed SVG."""
    import math
    if not symbol or not symbol.get('verified_style'):
        return []
    result = []
    kind = symbol.get('boundary_pattern')
    edge_ticks = symbol.get('pattern') == 'edge_ticks'
    line_dots = symbol.get('pattern') == 'line_dots'
    if kind != 'dots' and not edge_ticks and not line_dots:
        return result
    if line_dots:
        pitch = float(symbol.get('spacing_mm') or 10) / SCALE
        radius = float(symbol.get('diameter_mm') or 1) / (2 * SCALE)
        color = rgb_for_aci(color_aci(symbol.get('line_color') or 'green'))
    elif edge_ticks:
        pitch = float(symbol.get('spacing_mm') or 4) / SCALE
        radius = 0.0
        color = rgb_for_aci(color_aci(symbol.get('pattern_color') or 'red'))
    else:
        pitch = float(symbol.get('boundary_spacing_mm') or 3) / SCALE
        radius = float(symbol.get('boundary_diameter_mm') or 1.5) / (2 * SCALE)
        color = rgb_for_aci(color_aci(symbol.get('boundary_color') or 'red'))
    total = 0
    for ring in rings:
        if len(ring) < 2:
            continue
        ln = LineString(ring)
        if ln.length < 0.001:
            continue
        n = int(ln.length / pitch)
        if total + n > 5000:
            raise ValueError('regulation symbol count exceeds print-sheet limit')
        for i in range(n + 1):
            d = min(ln.length, i * pitch)
            p = ln.interpolate(d)
            if edge_ticks:
                delta = max(.1, min(.5, pitch / 4))
                before = ln.interpolate(max(0, d-delta))
                after = ln.interpolate(min(ln.length, d+delta))
                dx, dy = after.x-before.x, after.y-before.y
                length = math.hypot(dx,dy)
                if length <= .00001:
                    continue
                nx,ny = -dy/length,dx/length
                end_x,end_y = p.x+nx/SCALE,p.y+ny/SCALE
                result.append(f'<path d="M{p.x:.3f},{p.y:.3f}L{end_x:.3f},{end_y:.3f}" fill="none" stroke="{color}" stroke-width=".35"/>')
            else:
                result.append(f'<circle cx="{p.x:.3f}" cy="{p.y:.3f}" r="{radius:.3f}" fill="none" stroke="{color}" stroke-width=".35"/>')
            total += 1
    return result


def svg_sheet(ops):
    """Editable vector SVG and DXF share the same source symbol metadata."""
    buf = [f'<svg xmlns="http://www.w3.org/2000/svg" width="1160" height="760" viewBox="0 0 {SHEET_W:g} {SHEET_H:g}">',
           '<rect x="0" y="0" width="580" height="380" fill="#fff"/>']
    patterns = {}
    for op in ops:
        symbol = op.get('symbol') or {}
        if symbol.get('pattern') in ('diagonal', 'vertical', 'circles'):
            key = (symbol.get('name'), symbol.get('group'))
            if key not in patterns:
                patterns[key] = 'survey-pat-' + str(len(patterns) + 1)
    if patterns:
        buf.append('<defs>')
        for (name, group), ident in patterns.items():
            symbol = next((op['symbol'] for op in ops if (op.get('symbol') or {}).get('name') == name and
                           (op.get('symbol') or {}).get('group') == group), None)
            if symbol:
                buf.append(_svg_pattern(symbol, ident))
        buf.append('</defs>')
    for op in ops:
        symbol = op.get('symbol') or {}
        if symbol.get('pattern') in ('diagonal', 'vertical', 'circles'):
            fill_override = 'url(#' + patterns[(symbol.get('name'), symbol.get('group'))] + ')'
        else:
            fill_override = None
        kind, stroke, weight = op['type'], _color(op.get('stroke'), '#000000') if op.get('stroke') != 'none' else 'none', op.get('weight', 0.5)
        fill = fill_override or (_color(op.get('fill'), '#ffffff') if op.get('fill') not in (None, 'none') else 'none')
        if kind == 'rect':
            buf.append(f'<rect x="{op["x"]:.3f}" y="{op["y"]:.3f}" width="{op["w"]:.3f}" height="{op["h"]:.3f}" fill="{fill}" stroke="{stroke}" stroke-width="{weight}"/>')
        elif kind in ('polygon', 'polyline'):
            paths = op['rings'] if kind == 'polygon' else [op['points']]
            d = ' '.join('M ' + ' L '.join(f'{x:.3f},{y:.3f}' for x, y in ring) + (' Z' if kind == 'polygon' else '') for ring in paths)
            dash = ' stroke-dasharray="7 4"' if op.get('dash') else ''
            buf.append(f'<path d="{d}" fill="{fill}" fill-rule="evenodd" stroke="{stroke}" stroke-width="{weight}" stroke-linejoin="round"{dash}/>')
            if kind == 'polygon' and symbol.get('boundary_pattern') == 'dots':
                buf.extend(_svg_boundary_marks(paths, symbol))
            elif kind == 'polygon' and symbol.get('pattern') == 'edge_ticks':
                buf.extend(_svg_boundary_marks(paths, symbol))
            elif kind == 'polyline' and symbol.get('pattern') == 'line_dots':
                buf.extend(_svg_boundary_marks(paths, symbol))
        elif kind == 'text':
            weight_tag = ' font-weight="bold"' if op.get('bold') else ''
            anchor = {'middle': 'middle', 'end': 'end'}.get(op.get('anchor'), 'start')
            text_color = _color(op.get('color'), '#111111')
            buf.append(f'<text x="{op["x"]:.3f}" y="{op["y"]:.3f}" fill="{text_color}" font-family="NanumSquare, Noto Serif CJK KR, Malgun Gothic, sans-serif" font-size="{op.get("size", 7):g}" text-anchor="{anchor}"{weight_tag}>{escape(str(op["text"]))}</text>')
        elif kind == 'school_mark':
            for poly in _school_mark_shapes(op['x'],op['y'],op.get('variant'),op.get('size',6)):
                path='M '+' L '.join(f'{x:.3f},{y:.3f}' for x,y in poly)+' Z'
                buf.append(f'<path d="{path}" fill="#000000" stroke="#000000" stroke-width=".15"/>')
        elif kind == 'circle':
            buf.append(f'<circle cx="{op["x"]:.3f}" cy="{op["y"]:.3f}" r="{op["r"]:.3f}" fill="none" stroke="{stroke}" stroke-width="{weight}"/>')
    buf.append('</svg>')
    return ''.join(buf).encode('utf-8')


def dxf_paper_sheet(doc, ops):
    """Editable A3 paper geometry; original EPSG:5179 model space is untouched."""
    from ezdxf.colors import rgb2int
    from ezdxf.enums import TextEntityAlignment
    layout = doc.layouts.get('SURVEY_A3') if 'SURVEY_A3' in doc.layouts else doc.layouts.new('SURVEY_A3')
    layout.page_setup(size=(420, 297), margins=(0, 0, 0, 0), units='mm', rotation=0)
    if 'SURVEY_KOR' not in doc.styles:
        doc.styles.new('SURVEY_KOR', dxfattribs={'font': 'malgun.ttf'})
    if 'SURVEY_DASH' not in doc.linetypes:
        doc.linetypes.new('SURVEY_DASH', dxfattribs={'description': 'Survey site outline', 'pattern': [8.0, 5.0, -3.0]})

    def mm(x, y):
        return (4.0 + x * SCALE, PAPER_H - PAPER_DY - y * SCALE)

    def attrs(op):
        color = _color(op.get('color') if op['type']=='text' and op.get('color') else
                       op.get('stroke') if op.get('stroke') not in (None, 'none') else op.get('fill'), '#111111')
        layer_name = op.get('layer') or ('SHEET_TEXT' if op['type'] == 'text' else 'SHEET_FRAME')
        rgb = rgb2int(tuple(int(color[i:i + 2], 16) for i in (1, 3, 5)))
        if layer_name not in doc.layers:
            doc.layers.new(layer_name, dxfattribs={'color': 7, 'true_color': rgb})
        result = {'layer': layer_name, 'true_color': rgb,
                  'lineweight': max(9, min(53, round(op.get('weight', 0.5) * 24)))}
        if op.get('dash'):
            result['linetype'] = 'SURVEY_DASH'
        return result

    for op in ops:
        kind = op['type']
        a = attrs(op)
        if kind == 'rect':
            x, y, w, h = op['x'], op['y'], op['w'], op['h']
            points = [mm(x, y), mm(x + w, y), mm(x + w, y + h), mm(x, y + h)]
            sym = op.get('symbol') or {}
            if sym.get('verified_style'):
                hatch_pattern(layout, Polygon(points), a['layer'], sym, 1.0)
            elif op.get('fill') != 'none':
                hatch = layout.add_hatch(color=7, dxfattribs=a)
                hatch.set_solid_fill(color=7)
                hatch.dxf.true_color = rgb2int(tuple(int(_color(op.get('fill'), '#ffffff')[i:i + 2], 16) for i in (1, 3, 5)))
                hatch.paths.add_polyline_path(points, is_closed=True)
            if op.get('stroke') != 'none':
                layout.add_lwpolyline(points, close=True, dxfattribs=a)
        elif kind in ('polygon', 'polyline'):
            rings = op['rings'] if kind == 'polygon' else [op['points']]
            sym = op.get('symbol') or {}
            if kind == 'polygon' and sym.get('verified_style') and op.get('layer', '').startswith('SHEET_THEME'):
                geom = Polygon([mm(x, y) for x, y in rings[0]],
                               [[mm(x, y) for x, y in hole] for hole in rings[1:]])
                if geom.is_valid and not geom.is_empty:
                    hatch_pattern(layout, geom, a['layer'], sym, 1.0)
            elif op.get('fill') != 'none' and kind == 'polygon':
                hatch = layout.add_hatch(color=7, dxfattribs=a)
                hatch.set_solid_fill(color=7)
                hatch.dxf.true_color = rgb2int(tuple(int(_color(op['fill'], '#ffffff')[i:i + 2], 16) for i in (1, 3, 5)))
                for j, ring in enumerate(rings):
                    hatch.paths.add_polyline_path([mm(x, y) for x, y in ring[:-1] if ring],
                                                  is_closed=True, flags=1 if j == 0 else 0)
            if op.get('stroke') != 'none':
                if kind == 'polyline' and sym.get('verified_style') and sym.get('group') == 'facility':
                    stroke_line_symbol(layout, LineString([mm(x,y) for x,y in rings[0]]),
                                       a['layer'], sym, 1.0)
                else:
                    for ring in rings:
                        layout.add_lwpolyline([mm(x, y) for x, y in ring], close=(kind == 'polygon'), dxfattribs=a)
        elif kind == 'text':
            align = {'middle': TextEntityAlignment.MIDDLE_CENTER, 'end': TextEntityAlignment.MIDDLE_RIGHT}.get(op.get('anchor'), TextEntityAlignment.MIDDLE_LEFT)
            layout.add_text(str(op['text']), dxfattribs={**a, 'height': op.get('size', 7) * SCALE,
                                                       'style': 'SURVEY_KOR'}).set_placement(
                mm(op['x'], op['y'] - op.get('size', 7) * 0.3), align=align)
        elif kind == 'school_mark':
            for poly in _school_mark_shapes(op['x'],op['y'],op.get('variant'),op.get('size',6)):
                points=[mm(x,y) for x,y in poly]
                hatch=layout.add_hatch(color=7,dxfattribs={'layer':a['layer']})
                hatch.set_solid_fill(color=7)
                hatch.paths.add_polyline_path(points,is_closed=True)
        elif kind == 'circle':
            layout.add_circle(mm(op['x'], op['y']), op['r'] * SCALE, dxfattribs=a)
    # Ensure all paper space fills plot under boundaries, outlines and labels.
    ordered = sorted(list(layout), key=lambda ent: 0 if ent.dxftype() == 'HATCH' else 1)
    layout.set_redraw_order([(e.dxf.handle, f'{i+1:X}') for i, e in enumerate(ordered)])
    doc.header['$SORTENTS'] = int(doc.header.get('$SORTENTS', 0)) | 16
    return layout


def zoning_style(name):
    return style_for(name, "zoning")


def planning_context(features, group='zoning'):
    """Style the original PLAN feature identities, never invent missing subtype facts."""
    clean = []
    for feature in features:
        p = {str(k).lower(): v for k, v in (feature.get('properties') or {}).items()}
        name = str(p.get('uname') or p.get('name') or p.get('dgm_nm') or
                   p.get('fclty_nm') or '').strip() or '명칭 미확보'
        if feature.get('geometry'):
            clean.append((name, feature))
    classes = {name: (f'C{i+1:02d}', style_for(name, group))
               for i, name in enumerate(sorted({name for name, _ in clean}))}
    mapped = []
    for name, feature in clean:
        code, sym = classes[name]
        props = {'class_code': code, **sym, 'name': name, 'style_name': sym.get('name')}
        # Preserve verifiable source identity fields, but do not derive an ID.
        source_props = {str(k).lower(): v for k, v in
                        (feature.get('properties') or {}).items()}
        for field in ('unq_mnno', 'fclty_no', 'dgm_no'):
            if source_props.get(field) not in (None, ''):
                props['source_' + field] = source_props[field]
        mapped.append({'type': 'Feature', 'geometry': feature['geometry'],
                       'properties': props})
    legend = [{'class_code': code, **sym, 'name': name, 'style_name': sym.get('name')}
              for name, (code, sym) in classes.items()]
    return {'status': 'COMPLETE', 'features': mapped, 'legend': legend,
            'note': '도시·군관리계획수립지침 별첨 1의 분류 기호 적용. '
                    '원자료에 없는 세부유형·기호번호는 생성하지 않음. 고시 원도면과 별도 확인 필요.'}


def zoning_context(features):
    """Compatibility wrapper for 5-2, using the common symbol catalog."""
    return planning_context(features, 'zoning')
