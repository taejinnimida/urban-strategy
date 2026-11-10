"""Print scale is determined once per frozen survey boundary.

Survey map plot area is A3 paper-space, not the whole sheet. A 2x site
bounding box is a *context coverage preference*; it is not itself a scale.
1:3,000 is a platform working scale, not an assertion of a mandatory statutory
urban-management planning scale. N.S. is a fitted working preview.

All dimensions here are projected EPSG:5179 metres and A3 paper millimetres.
The DXF model-space remains full-size metric EPSG:5179.
"""
import math
from shapely.geometry import shape
from shapely.ops import transform
from pyproj import Transformer

PAPER_WIDTH_MM = 420.0
PAPER_HEIGHT_MM = 297.0
SHEET_WIDTH = 580.0
SHEET_HEIGHT = 380.0
MAP_WIDTH = 485.0
MAP_HEIGHT = 376.0
PLOT_MM_PER_UNIT = (PAPER_WIDTH_MM - 8.0) / SHEET_WIDTH
MAP_PRINT_WIDTH_MM = MAP_WIDTH * PLOT_MM_PER_UNIT
MAP_PRINT_HEIGHT_MM = MAP_HEIGHT * PLOT_MM_PER_UNIT
CONTEXT_FACTOR = 2.0
SCALE_CHOICES = (1000, 3000, 5000)
PROJECT = Transformer.from_crs('EPSG:4326', 'EPSG:5179', always_xy=True).transform


def decide_print_scale(geometry):
    """Select the first scale that fits 2x the site bbox in BOTH plot directions.

    Return explicit N.S. only if not even 1:5,000 fits; the A3 layout still
    retains a geometrically correct measured graphical scale bar in that case.
    """
    site = transform(PROJECT, shape(geometry))
    if site.is_empty or not site.is_valid or site.area <= 0:
        raise ValueError('invalid survey boundary')
    minx, miny, maxx, maxy = site.bounds
    width_m, height_m = maxx-minx, maxy-miny
    if not all(math.isfinite(v) and v > 0 for v in (width_m, height_m)):
        raise ValueError('invalid survey boundary extents')
    desired_w, desired_h = width_m*CONTEXT_FACTOR, height_m*CONTEXT_FACTOR
    for denominator in SCALE_CHOICES:
        extent_w = MAP_PRINT_WIDTH_MM * denominator/1000
        extent_h = MAP_PRINT_HEIGHT_MM * denominator/1000
        if desired_w <= extent_w + 1e-8 and desired_h <= extent_h + 1e-8:
            return {'mode':'FIXED','denominator':denominator,
                    'label':f'1:{denominator:,}', 'paper_size':'A3_420x297mm',
                    'map_width_mm':round(MAP_PRINT_WIDTH_MM,5),
                    'map_height_mm':round(MAP_PRINT_HEIGHT_MM,5),
                    'context_factor':CONTEXT_FACTOR,
                    'requested_context_width_m':round(desired_w,3),
                    'requested_context_height_m':round(desired_h,3),
                    'map_extent_width_m':round(extent_w,3),
                    'map_extent_height_m':round(extent_h,3)}
    return {'mode':'NS','denominator':None,'label':'N.S. (자동맞춤)',
            'paper_size':'A3_420x297mm',
            'map_width_mm':round(MAP_PRINT_WIDTH_MM,5),
            'map_height_mm':round(MAP_PRINT_HEIGHT_MM,5),
            'context_factor':CONTEXT_FACTOR,
            'requested_context_width_m':round(desired_w,3),
            'requested_context_height_m':round(desired_h,3),
            'map_extent_width_m':None,'map_extent_height_m':None}
