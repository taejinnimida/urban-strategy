"""Official plan-drawing symbols, isolated from Seoul RULE and FACT.

Source: supplied [별첨 1] 도시·군관리계획 조서 및 도면 작성지침,
section 6, pp. 23–27 (1:5,000). Missing subtypes are UNKNOWN, not guessed.
The ACI index, not an arbitrary web color, is the authoritative color identifier.
"""
from functools import lru_cache
from pathlib import Path
import json
import re


@lru_cache(maxsize=1)
def catalog():
    return json.loads(Path(__file__).with_name("development_planning_symbols.json").read_text(encoding="utf-8"))


def color_aci(name):
    return {"red": 1, "green": 3, "blue": 5, "light_orange": 31,
            "dark_purple": 204, "gray": 254, "light_blue": 131, "black": 7}.get(name, 7)


def rgb_for_aci(aci):
    return catalog()["aci_rgb"].get(str(aci), "#ffffff")


def style_for(name, group="zoning"):
    """Use named official subtype where present, without imputing unspecified types.

    Prefix layer GROUP is a data *source*, not a subtype. The name itself
    must match a known classification; ambiguous generic features stay unknown.
    """
    key = re.sub(r"\s+", "", str(name or "")).replace("ㆍ", "·")
    types = [s for s in catalog()["types"] if s["group"] == group]
    indexed = []
    for spec in types:
        for text in [spec["name"], *(spec.get("aliases") or [])]:
            text = re.sub(r"\s+", "", text)
            if text and text in key:
                indexed.append((len(text), spec))
    if not indexed:
        return {"name": name or "명칭 미확보", "group": group, "verified_style": False,
                "color": "#ced5d9", "aci": None, "pattern": None,
                "fill_mode": "none", "note": "세부분류를 확인할 수 없어 지침기호 미확정"}
    spec = max(indexed, key=lambda v: v[0])[1]
    fill_aci = spec.get("fill_aci")
    fill_mode = spec.get("fill_mode", "none")
    return {
        "name": spec["name"], "group": group,
        "verified_style": True,
        "color": rgb_for_aci(fill_aci) if fill_aci is not None else "#ffffff",
        "aci": fill_aci, "fill_mode": fill_mode,
        "pattern": spec.get("pattern_type"),
        "pattern_aci": spec.get("pattern_aci") or color_aci(spec.get("pattern_color")),
        "pattern_color": spec.get("pattern_color"),
        "spacing_mm": spec.get("spacing_mm"),
        "diameter_mm": spec.get("diameter_mm"),
        "angle_deg": spec.get("angle_deg", 45),
        "boundary_color": spec.get("boundary_color"),
        "boundary_pattern": spec.get("boundary_pattern"),
        "boundary_diameter_mm": spec.get("boundary_diameter_mm"),
        "boundary_spacing_mm": spec.get("boundary_spacing_mm"),
        "facility_index": spec.get("facility_index"),  # catalog example only; not an actual facility ID
        "line_color": spec.get("line_color"),
        "marker_color": spec.get("marker_color"),
        "label_color": spec.get("label_color"),
        "guideline_page": spec.get("guideline_page"),
    }


def style_from_properties(properties, group="zoning"):
    p = {str(k).lower(): v for k, v in (properties or {}).items()}
    name = str(p.get("uname") or p.get("name") or p.get("dgm_nm")
               or p.get("fclty_nm") or "").strip()
    return style_for(name, group)


def hatch_pattern(model, polygon, layer, style, drawing_units_per_mm, max_circles=5000):
    """Create independently editable ACI HATCH and circle outlines.

    ACI solid/base fill and non-solid foreground are *separate* entities.
    1:5000 model-space geometry uses 5 m drawing units per paper millimeter;
    A3 paper sheet uses sheet units scaled to the actual paper mm.
    """
    from shapely.geometry import Point
    if polygon.is_empty or polygon.area <= 0:
        return 0
    a = style.get("aci")
    mode = style.get("fill_mode")
    count = 0

    def attach(h):
        h.paths.add_polyline_path(list(polygon.exterior.coords)[:-1], is_closed=True, flags=1)
        for hole in polygon.interiors:
            h.paths.add_polyline_path(list(hole.coords)[:-1], is_closed=True, flags=0)

    if mode == "solid" and a is not None:
        h = model.add_hatch(color=int(a), dxfattribs={"layer": layer})
        h.set_solid_fill(color=int(a))
        attach(h)
        count += 1

    pattern = style.get("pattern")
    if pattern in ("diagonal", "vertical"):
        distance = float(style.get("spacing_mm") or 3) * drawing_units_per_mm
        if distance > 0:
            h = model.add_hatch(color=int(style.get("pattern_aci") or 1),
                                dxfattribs={"layer": layer})
            # Use explicit millimeter-in-scale spacing, independent of hatch defaults.
            angle = 45 if pattern == "diagonal" else 90
            offset = (-distance / (2**0.5), distance / (2**0.5)) if angle == 45 else (-distance, 0)
            h.set_pattern_fill("USER", color=int(style.get("pattern_aci") or 1),
                               pattern_type=0,
                               definition=[[angle, (0, 0), offset, []]])
            attach(h)
            count += 1
    elif pattern == "circles":
        distance = float(style.get("spacing_mm") or 5) * drawing_units_per_mm
        radius = float(style.get("diameter_mm") or 2) * drawing_units_per_mm / 2
        if distance <= 0 or radius <= 0:
            return count
        import math
        from shapely.prepared import prep
        xmin, ymin, xmax, ymax = polygon.bounds
        start_x = math.floor(xmin / distance) * distance
        start_y = math.floor(ymin / distance) * distance
        cols = math.ceil((xmax - start_x) / distance) + 1
        rows = math.ceil((ymax - start_y) / distance) + 1
        if cols * rows > max_circles:
            # Never silently render the wrong-sized circle pattern.
            raise ValueError("circle pattern would exceed drawing safety limit")
        inside = prep(polygon)
        for i in range(cols):
            for j in range(rows):
                x, y = start_x + i * distance, start_y + j * distance
                if inside.covers(Point(x, y)):
                    model.add_circle((x, y), radius,
                                     dxfattribs={"layer": layer, "color": int(style.get("pattern_aci") or 3)})
                    count += 1
    # 1.5 mm dots at 3 mm on the regulated boundary, not within the fill.
    if style.get("boundary_pattern") == "dots":
        count += boundary_dots(model, polygon, layer, style, drawing_units_per_mm)
    # Edge ticks are prescribed for height/fire/disaster districts.
    if pattern == "edge_ticks":
        count += boundary_ticks(model, polygon, layer, style, drawing_units_per_mm)
    return count


def _ring_line_parts(polygon):
    from shapely.geometry import LineString
    for ring in (polygon.exterior, *polygon.interiors):
        yield LineString(ring.coords)


def boundary_dots(model, polygon, layer, style, drawing_units_per_mm, max_dots=5000):
    """Dotted red decision boundary, with actual 1.5 mm diameter / 3 mm pitch."""
    from shapely.geometry import Point
    diameter = float(style.get("boundary_diameter_mm") or 1.5) * drawing_units_per_mm
    step = float(style.get("boundary_spacing_mm") or 3) * drawing_units_per_mm
    if step <= 0:
        raise ValueError("invalid decision boundary dot spacing")
    count = 0
    color = color_aci(style.get("boundary_color") or "red")
    for line in _ring_line_parts(polygon):
        n = int(line.length / step)
        if n + count > max_dots:
            raise ValueError("boundary dot count exceeds safety limit")
        for i in range(n + 1):
            point = line.interpolate(i * step)
            model.add_circle((point.x, point.y), diameter / 2,
                             dxfattribs={"layer": layer, "color": color})
            count += 1
    return count


def boundary_ticks(model, polygon, layer, style, drawing_units_per_mm, max_ticks=5000):
    """4 mm boundary tick rhythm for height/fire/disaster districts."""
    from shapely.geometry import Point
    import math
    step = float(style.get("spacing_mm") or 4) * drawing_units_per_mm
    tick = drawing_units_per_mm
    color = color_aci(style.get("pattern_color") or "red")
    count = 0
    for line in _ring_line_parts(polygon):
        n = int(line.length / step)
        if count + n > max_ticks:
            raise ValueError("boundary tick count exceeds safety limit")
        for i in range(n + 1):
            distance = i * step
            c = line.interpolate(distance)
            prev = line.interpolate(max(0, distance - tick * .2))
            nxt = line.interpolate(min(line.length, distance + tick * .2))
            dx, dy = nxt.x - prev.x, nxt.y - prev.y
            size = math.hypot(dx, dy)
            if not size:
                continue
            nx, ny = -dy / size, dx / size
            a = Point(c.x + nx * tick * .7, c.y + ny * tick * .7)
            b = Point(c.x - nx * tick * .7, c.y - ny * tick * .7)
            end = a if polygon.covers(a) else b
            model.add_line((c.x, c.y), (end.x, end.y),
                           dxfattribs={"layer": layer, "color": color})
            count += 1
    return count


def stroke_line_symbol(model, geometry, layer, style, drawing_units_per_mm, max_dots=5000):
    """Editable linework with source-classified color and optional 1cm pipe dots."""
    from shapely.geometry import LineString
    color = color_aci(style.get("line_color") or style.get("boundary_color") or "red")
    lines = [geometry] if geometry.geom_type == "LineString" else [
        v for v in getattr(geometry, "geoms", ()) if v.geom_type == "LineString"]
    count = 0
    for line in lines:
        if line.is_empty or line.length <= 0:
            continue
        model.add_lwpolyline(list(line.coords), dxfattribs={"layer": layer, "color": color})
        count += 1
        if style.get("pattern") != "line_dots":
            continue
        step = float(style.get("spacing_mm") or 10) * drawing_units_per_mm
        radius = float(style.get("diameter_mm") or 1) * drawing_units_per_mm / 2
        n = int(line.length / step)
        if count + n > max_dots:
            raise ValueError("pipeline dot count exceeds safety limit")
        for i in range(n + 1):
            p = line.interpolate(i * step)
            model.add_circle((p.x, p.y), radius,
                             dxfattribs={"layer": layer, "color": color})
            count += 1
    return count
