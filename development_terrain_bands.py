"""Contour-derived elevation bands on a shared TIN (survey reference, not legal approval).

The surface is piecewise linear. Each triangle is sliced at elevation thresholds;
actual site-clipped polygon areas, rather than grid-center samples, are counted.
"""
import math
from collections import defaultdict
from shapely.geometry import Polygon, mapping
from shapely.ops import transform, unary_union

PALETTE = ('#075d22', '#197a28', '#68a900', '#cbd500',
           '#ffce24', '#fa8b19', '#df4720', '#a91516')


def _slice(vertices, level, above):
    """Clip a 3-D polygon against the plane z=level (Sutherland-Hodgman)."""
    if not vertices:
        return []
    output = []
    previous = vertices[-1]
    prev_inside = previous[2] >= level if above else previous[2] <= level
    for current in vertices:
        curr_inside = current[2] >= level if above else current[2] <= level
        if curr_inside != prev_inside:
            dz = current[2] - previous[2]
            if abs(dz) > 1e-12:
                t = (level - previous[2]) / dz
                output.append((previous[0] + t*(current[0]-previous[0]),
                               previous[1] + t*(current[1]-previous[1]), level))
        if curr_inside:
            output.append(current)
        previous, prev_inside = current, curr_inside
    return output


def _z_on_triangle(coords, z_values, x, y):
    (ax, ay), (bx, by), (cx, cy) = coords
    za, zb, zc = z_values
    ux, uy, vx, vy = bx-ax, by-ay, cx-ax, cy-ay
    det = ux*vy-uy*vx
    if abs(det) <= 1e-12:
        raise ValueError('degenerate TIN triangle')
    dzdx = ((zb-za)*vy-(zc-za)*uy)/det
    dzdy = (ux*(zc-za)-vx*(zb-za))/det
    return za + dzdx*(x-ax) + dzdy*(y-ay)


def _polygon_parts(geometry):
    if geometry.is_empty:
        return
    if geometry.geom_type == 'Polygon':
        yield geometry
    elif hasattr(geometry, 'geoms'):
        for child in geometry.geoms:
            yield from _polygon_parts(child)


def _elevation_breaks(minimum, maximum):
    if maximum-minimum <= 1e-8:
        return []
    for step in (5, 10, 20, 30, 50, 100, 200, 500, 1000):
        first = math.floor(minimum/step)*step + 2*step
        if first > maximum:
            return []
        edges = []
        while first <= maximum and len(edges) < 9:
            edges.append(first)
            first += step
        if len(edges) <= 7 and first > maximum:
            return edges
    return []


def elevation_bands(triangles, heights, site, inverse, max_vertices=100000):
    """Make exact planar-TIN elevation bands in projected metres.

    triangles: non-overlapping TIN triangles in a projected-metre CRS.
    heights: dict {(round(x,5),round(y,5)): source elevation_m}.
    site: target area polygon in the same CRS.
    inverse: projected CRS -> EPSG:4326 pyproj transformer function.
    """
    pieces = []
    covered, weighted = 0.0, 0.0
    minimum, maximum = float('inf'), -float('inf')
    for triangle in triangles:
        clipped = triangle.intersection(site)
        if clipped.is_empty or clipped.area <= 1e-8:
            continue
        coords = [tuple(xy[:2]) for xy in list(triangle.exterior.coords)[:3]]
        values = [float(heights[(round(x, 5), round(y, 5))]) for x,y in coords]
        for polygon in _polygon_parts(clipped):
            if polygon.area <= 1e-8:
                continue
            pts = list(polygon.exterior.coords)
            for ring in polygon.interiors:
                pts.extend(ring.coords)
            elevations = [_z_on_triangle(coords, values, x, y) for x,y in pts]
            minimum = min(minimum, min(elevations))
            maximum = max(maximum, max(elevations))
            covered += polygon.area
            weighted += polygon.area * _z_on_triangle(coords, values,
                                                       polygon.centroid.x, polygon.centroid.y)
        pieces.append((coords, values))
    if not pieces or covered <= 1e-8:
        return {'elevation_status': 'MISSING', 'elevation_coverage_pct': 0,
                'elevation_features': [], 'elevation_groups': [],
                'elevation_map_complete': False}
    edges = _elevation_breaks(minimum, maximum)
    colors = PALETTE
    classes = [None] + edges + [None]
    zones = defaultdict(list)
    area_by_class = defaultdict(float)
    for coords, values in pieces:
        vertices = [(x,y,z) for (x,y),z in zip(coords,values)]
        zlo, zhi = min(values), max(values)
        for index, (lo, hi) in enumerate(zip(classes[:-1],classes[1:])):
            if (lo is not None and zhi < lo) or (hi is not None and zlo > hi):
                continue
            polygon_vertices = vertices
            if lo is not None:
                polygon_vertices = _slice(polygon_vertices,lo,True)
            if hi is not None:
                polygon_vertices = _slice(polygon_vertices,hi,False)
            if len(polygon_vertices) < 3:
                continue
            polygon = Polygon([(x,y) for x,y,_ in polygon_vertices])
            if polygon.is_empty or not polygon.is_valid or polygon.area <= 1e-8:
                continue
            actual = polygon.intersection(site)
            if actual.is_empty or actual.area <= 1e-8:
                continue
            for part in _polygon_parts(actual):
                if part.area <= 1e-8:
                    continue
                zones[index].append(part)
                area_by_class[index] += part.area
    groups, features = [], []
    total_vertices = 0
    for i, (lo, hi) in enumerate(zip(classes[:-1],classes[1:])):
        area = area_by_class[i]
        if area <= 1e-8:
            continue
        if lo is None and hi is None:
            label = f'{minimum:.1f}~{maximum:.1f}m'
        elif lo is None:
            label = f'{hi:g}m 미만'
        elif hi is None:
            label = f'{lo:g}m 이상'
        else:
            label = f'{lo:g}~{hi:g}m'
        group = {'class_code': f'C{i+1:02d}', 'name': label,
                 'color': colors[min(i,len(colors)-1)],
                 'area_m2': round(area,2),
                 'pct': round(100*area/site.area,2),
                 'min_m': minimum if lo is None else lo,
                 'max_m': maximum if hi is None else hi,
                 'count': len(zones[i])}
        groups.append(group)
        merged = unary_union(zones[i])
        total_vertices += sum(len(poly.exterior.coords)+sum(len(r.coords) for r in poly.interiors)
                              for poly in _polygon_parts(merged))
        features.append({'type':'Feature',
                         'geometry':mapping(transform(inverse,merged)),
                         'properties':{'class_code':group['class_code'],
                                       'name':label,'color':group['color'],
                                       'area_m2':area}})
    map_complete = total_vertices <= max_vertices and bool(features)
    return {'elevation_status':'DATA' if covered/site.area >= .99999 else 'PARTIAL',
            'elevation_coverage_pct': min(100,100*covered/site.area),
            'elevation_site_area_m2': site.area,
            'elevation':{'min':minimum,'max':maximum,'mean':weighted/covered},
            'elevation_groups':groups,
            'elevation_features':features if map_complete else [],
            'elevation_map_complete':map_complete,
            'elevation_method':'TIN_TRIANGLE_HEIGHT_BANDS',
            'elevation_covered_area_m2':covered}
