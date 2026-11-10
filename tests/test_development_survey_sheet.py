"""Offline regression tests for the nationwide survey SVG/DXF layout.

All geometry is synthetic. No VWorld/API key or production services required.
Run: python -m unittest test_development_survey_sheet.py
"""
import io
import unittest
import xml.etree.ElementTree as ET
from collections import Counter
from shapely.geometry import box, mapping
from shapely.ops import transform
from pyproj import Transformer
import ezdxf
from development.survey_sheet import make_sheet, svg_sheet, dxf_paper_sheet, zoning_context

BACK = Transformer.from_crs('EPSG:5179', 'EPSG:4326', always_xy=True).transform


def polygon(x1, y1, x2, y2):
    return mapping(transform(BACK, box(x1, y1, x2, y2)))


class SurveySheetTest(unittest.TestCase):
    def setUp(self):
        self.site = polygon(953000, 1950000, 953110, 1950110)
        self.parcels = [
            {'type': 'Feature', 'geometry': polygon(952880 + 45*i, 1949900 + 45*j,
                                                    952920 + 45*i, 1949940 + 45*j),
             'properties': {'jibun': f'{i}-{j}'}}
            for i in range(7) for j in range(7)
        ]
        self.snapshot = {'geometry': self.site, '_cad_background':
                         {'status': 'COMPLETE', 'features': self.parcels}}

    def assert_editable(self, ops):
        svg = svg_sheet(ops)
        ET.fromstring(svg)
        doc = ezdxf.new('R2010')
        doc.modelspace().add_lwpolyline([(953000, 1950000), (953110, 1950000)])
        dxf_paper_sheet(doc, ops)
        writer = io.StringIO()
        doc.write(writer)
        loaded = ezdxf.read(io.StringIO(writer.getvalue()))
        self.assertIn('SURVEY_A3', loaded.layouts)
        self.assertTrue(len(loaded.layouts.get('SURVEY_A3')) > 30)
        self.assertTrue(Counter(e.dxftype() for e in loaded.layouts.get('SURVEY_A3'))['HATCH'] > 0)
        self.assertTrue(len(loaded.modelspace()) > 0)
        return svg

    def test_jimok_same_class_colors(self):
        theme = [
            {'type': 'Feature', 'geometry': polygon(953000, 1950000, 953055, 1950110),
             'properties': {'name': '대', 'color': '#a9bdf1', 'class_code': 'C01'}},
            {'type': 'Feature', 'geometry': polygon(953055, 1950000, 953110, 1950110),
             'properties': {'name': '도로', 'color': '#fbef97', 'class_code': 'C02'}}]
        item = {'id': '2-2', 'name': '지목별', 'features': theme,
                'legend': [{'class_code': 'C01', 'name': '대', 'color': '#a9bdf1'},
                           {'class_code': 'C02', 'name': '도로', 'color': '#fbef97'}]}
        ops = make_sheet(item, self.snapshot)
        self.assertEqual({o['fill'] for o in ops if o.get('layer', '').startswith('SHEET_THEME')},
                         {'#a9bdf1', '#fbef97'})
        self.assertTrue(any(o.get('layer') == 'SHEET_SITE' for o in ops))
        self.assert_editable(ops)

    def test_ownership_area_price_share_jimok_sheet_geometry_and_editable_cad(self):
        # No separately invented sheet for other land themes: all use the
        # same source classes, legend, red boundary and A3 CAD primitives.
        for item_id, label, class_name, color in (
            ('2-1', '소유자별', '국유', '#7fa9d4'),
            ('2-3', '면적별', '100㎡ 이하', '#c5e0ef'),
            ('2-4', '공시지가별', '1,000,000원/㎡ 이하', '#8bc6e0')):
            with self.subTest(item=item_id):
                item = {'id': item_id, 'name': label,
                        'features': [{'type': 'Feature',
                                      'geometry': polygon(953000,1950000,953110,1950110),
                                      'properties': {'name': class_name, 'color': color,
                                                     'class_code': 'C01'}}],
                        'legend': [{'name': class_name, 'class_code': 'C01', 'color': color,
                                    'count': 1, 'area_m2': 12100}]}
                ops = make_sheet(item, self.snapshot)
                fills = {op['fill'] for op in ops if op.get('layer') == 'SHEET_THEME_C01'}
                self.assertEqual(fills, {color})
                self.assertTrue(any(op.get('layer') == 'SHEET_SITE' for op in ops))
                self.assertTrue(any(op.get('text') == '범 례' for op in ops))
                svg = self.assert_editable(ops)
                self.assertIn(class_name.encode('utf-8'), svg)

    def test_zoning_surrounding_context(self):
        zone = zoning_context([
            {'geometry': polygon(952800, 1949800, 953055, 1950300),
             'properties': {'uname': '제2종일반주거지역'}},
            {'geometry': polygon(953055, 1949800, 953300, 1950300),
             'properties': {'uname': '제3종일반주거지역'}}])
        self.snapshot['_cad_zone_background'] = zone
        item = {'id': '5-2', 'name': '용도지역', 'features': [], 'legend': zone['legend']}
        ops = make_sheet(item, self.snapshot)
        self.assertEqual(len([o for o in ops if o.get('layer', '').startswith('SHEET_THEME')]), 2)
        self.assertTrue(any(o.get('layer') == 'SHEET_PARCEL_LABEL' for o in ops))
        self.assert_editable(ops)

    def test_missing_background_is_not_fake_completion(self):
        item = {'id': '2-2', 'features': [], 'legend': []}
        with self.assertRaisesRegex(ValueError, 'not acquired'):
            make_sheet(item, {'geometry': self.site, '_cad_background': {'status': 'UNKNOWN'}})


if __name__ == '__main__':
    unittest.main()