"""WMS-only 2-6 preview: clip imagery to the site and render a labelled reference sheet.

This does not classify pixels or authorize numeric land-cover areas / editable DXF.
EPSG:3857 WMS extents are transformed consistently with the original GetMap.
"""
import base64
import io
import math
from xml.sax.saxutils import escape

from PIL import Image, ImageChops, ImageDraw
from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform

PROJECT = Transformer.from_crs('EPSG:4326', 'EPSG:3857', always_xy=True).transform
MAX_PIXELS = 3_000_000


def _parts(geometry):
    if geometry.geom_type == 'Polygon':
        yield geometry
    elif geometry.geom_type == 'MultiPolygon':
        yield from geometry.geoms


def _image_extent(geometry, width, height):
    from development_landcover_service import _bounds
    bbox, _ = _bounds(geometry)
    xmin, ymin, xmax, ymax = bbox
    if xmax <= xmin or ymax <= ymin:
        raise ValueError('invalid projected WMS bbox')
    return xmin, ymin, xmax, ymax


def site_mask(width, height, geometry):
    """Exact WMS pixel-grid footprint (including multipolygons and holes)."""
    if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
        raise ValueError('invalid WMS image size')
    target = shape(geometry)
    if target.is_empty or not target.is_valid or target.geom_type not in ('Polygon', 'MultiPolygon'):
        raise ValueError('invalid site boundary')
    xmin, ymin, xmax, ymax = _image_extent(geometry, width, height)
    mask = Image.new('L', (width, height), 0)
    brush = ImageDraw.Draw(mask)
    def pixel(pt):
        x, y = PROJECT(*pt[:2])
        return ((x-xmin)/(xmax-xmin)*width, (ymax-y)/(ymax-ymin)*height)
    for poly in _parts(target):
        exterior = [pixel(p) for p in poly.exterior.coords]
        brush.polygon(exterior, fill=255)
        for hole in poly.interiors:
            brush.polygon([pixel(p) for p in hole.coords], fill=0)
    return mask


def clipped_png(image_bytes, geometry):
    """Preserve original WMS colors; transparent outside the definite site."""
    with Image.open(io.BytesIO(image_bytes)) as image:
        image.load()
        if image.format != 'PNG' or image.width * image.height > MAX_PIXELS:
            raise ValueError('not a bounded PNG')
        result = image.convert('RGBA')
    mask = site_mask(result.width, result.height, geometry)
    result.putalpha(ImageChops.multiply(result.getchannel('A'), mask))
    buffer = io.BytesIO()
    result.save(buffer, format='PNG', optimize=True)
    return buffer.getvalue()


def reference_svg(geometry, wms, cadastre=None):
    """A self-contained WMS reference sheet, NOT a vector / classified survey.

    Same fixed sheet dimensions as the 2-3 printed drawing; raster is aligned
    in the exact native GetMap EPSG:3857 plane and clipped to the project site.
    """
    if wms.get('status') != 'MAP_ONLY' or not wms.get('_image'):
        raise ValueError('WMS reference unavailable')
    target = shape(geometry)
    if target.is_empty or not target.is_valid:
        raise ValueError('invalid geometry')
    target_m = transform(PROJECT, target)
    x1, y1, x2, y2 = target_m.bounds
    cx, cy = (x1+x2)/2, (y1+y2)/2
    scale = max(2*(x2-x1)/485, 2*(y2-y1)/376, 0.000001)
    def xy(x,y):
        return (2+485/2+(x-cx)/scale, 2+376/2-(y-cy)/scale)
    def path(g):
        parts = []
        for poly in _parts(g):
            for ring in [poly.exterior, *poly.interiors]:
                coords=[xy(x,y) for x,y in ring.coords]
                if coords:
                    parts.append('M'+' L'.join(f'{a:.3f},{b:.3f}' for a,b in coords)+' Z')
        return ' '.join(parts)
    with Image.open(io.BytesIO(wms['_image'])) as img:
        width, height=img.size
    png=clipped_png(wms['_image'], geometry)
    xm1,ym1,xm2,ym2=_image_extent(geometry,width,height)
    left,top=xy(xm1,ym2)
    right,bottom=xy(xm2,ym1)
    raw=base64.b64encode(png).decode('ascii')
    s=[
        '<svg xmlns="http://www.w3.org/2000/svg" width="1160" height="760" viewBox="0 0 580 380">',
        '<defs><clipPath id="mapview"><rect x="2" y="2" width="485" height="376"/></clipPath></defs>',
        '<rect width="580" height="380" fill="#fff"/>',
        '<rect x=".5" y=".5" width="579" height="379" fill="none" stroke="#999"/>',
    ]
    if cadastre and cadastre.get('status') == 'COMPLETE':
        for f in (cadastre.get('features') or [])[:3000]:
            try:
                poly=transform(PROJECT,shape(f['geometry']))
                if not poly.is_valid or not poly.intersects(target_m.envelope.buffer(max(x2-x1,y2-y1))):
                    continue
                outline=path(poly)
                if outline:
                    s.append(f'<path d="{outline}" fill="none" stroke="#c9c9c9" stroke-width=".55" clip-path="url(#mapview)"/>')
            except (KeyError, TypeError, ValueError):
                continue
    s.extend([
        f'<image x="{left:.3f}" y="{top:.3f}" width="{right-left:.3f}" height="{bottom-top:.3f}" '
        f'href="data:image/png;base64,{raw}" clip-path="url(#mapview)"/>',
        f'<path d="{path(target_m)}" fill="none" stroke="#ec2626" stroke-width="2.1" stroke-dasharray="7 4" clip-path="url(#mapview)"/>',
        '<rect x="2" y="2" width="485" height="376" fill="none" stroke="#888" stroke-width=".7"/>',
        '<rect x="491" y="5" width="82" height="28" fill="#fff" stroke="#999"/>',
        '<text x="532" y="22" font-size="10.3" text-anchor="middle" font-weight="bold">토지피복별 현황</text>',
        '<rect x="491" y="39" width="82" height="280" fill="#fff" stroke="#999"/>',
        '<text x="532" y="53" font-size="9.5" font-weight="bold" text-anchor="middle">범 례</text>',
        '<path d="M497,67 L506,67" stroke="#ec2626" stroke-width="2" stroke-dasharray="5 3"/>',
        '<text x="508" y="69" font-size="7">확정 대상지</text>',
        '<text x="497" y="83" font-size="6.9">2025 세분류 토지피복</text>',
        '<text x="497" y="93" font-size="6.5">WMS 원영상 참고도</text>',
        '<rect x="491" y="322" width="82" height="54" fill="#fff" stroke="#999"/>',
        '<text x="505" y="331" font-size="9" font-weight="bold">N</text>',
        '<path d="M505,334 L501,347 L505,342 L509,347 Z" fill="#333"/>',
    ])
    legend=wms.get('_legend_image')
    if legend and legend.startswith(b'\x89PNG\r\n\x1a\n'):
        try:
            with Image.open(io.BytesIO(legend)) as img:
                iw,ih=img.size
            if 0<iw<=1500 and 0<ih<=3000:
                fit=min(75/max(iw,1),205/max(ih,1))
                width_l=iw*fit
                height_l=ih*fit
                s.append(f'<image x="494" y="98" width="{width_l:.3f}" height="{height_l:.3f}" '
                         f'href="data:image/png;base64,{base64.b64encode(legend).decode("ascii")}"/>')
        except (OSError, ValueError):
            legend=None
    if not legend:
        s.extend(['<text x="497" y="110" font-size="6.3">공식 색상 범례 미수신</text>',
                  '<text x="497" y="121" font-size="6.3">공간분류는 원자료 확인</text>'])
    s.extend([
        '<text x="497" y="306" font-size="5.9">면적·구성비: 미산정</text>',
        '<text x="497" y="314" font-size="5.7">CAD 원도형과 구별</text>',
        '<text x="497" y="359" font-size="6.2">WMS 참고도</text>',
        '<text x="497" y="369" font-size="6.1">수치·CAD 검증 별도</text>',
        '</svg>'
    ])
    return ''.join(s).encode('utf-8')
