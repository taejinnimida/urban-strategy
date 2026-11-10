"""Regression of frozen A3 printing scales, measured SVG and editable DXF.

An A3 sheet's actual map area is smaller than the full page. Bbox 2x
context must fit BOTH directions; area by itself must never pick the scale.
"""
import io
import unittest
from shapely.geometry import box, mapping
from shapely.ops import transform
from pyproj import Transformer
import ezdxf

from development.print_scale import (
    decide_print_scale, MAP_PRINT_WIDTH_MM, MAP_PRINT_HEIGHT_MM,
    PLOT_MM_PER_UNIT,
)
from development.survey_sheet import make_sheet, svg_sheet, dxf_paper_sheet

TO_WGS = Transformer.from_crs('EPSG:5179','EPSG:4326',always_xy=True).transform


def geo(width,height):
    return mapping(transform(TO_WGS,box(953000,1950000,953000+width,1950000+height)))


class PrintScaleTest(unittest.TestCase):
    def test_site_bounds_pick_first_feasible_scale(self):
        for (w,h),expected in [
            ((90,90),1000),
            ((170,130),1000),
            ((173,90),3000),
            ((100,135),3000),
            ((500,380),3000),
            ((520,380),5000),
            ((800,600),5000),
            ((900,30),None),
            ((20,700),None),
        ]:
            with self.subTest(width=w,height=h):
                selected=decide_print_scale(geo(w,h))
                self.assertEqual(selected['denominator'],expected)
                self.assertEqual(selected['mode'],'NS' if expected is None else 'FIXED')
                self.assertEqual(selected['context_factor'],2.0)
                if expected:
                    self.assertGreaterEqual(MAP_PRINT_WIDTH_MM*expected/1000,2*w-0.002)
                    self.assertGreaterEqual(MAP_PRINT_HEIGHT_MM*expected/1000,2*h-0.002)

    def test_map_paper_size_is_not_the_entire_a3(self):
        self.assertLess(MAP_PRINT_WIDTH_MM,420)
        self.assertLess(MAP_PRINT_HEIGHT_MM,297)
        self.assertAlmostEqual(MAP_PRINT_WIDTH_MM,485*412/580)
        self.assertAlmostEqual(MAP_PRINT_HEIGHT_MM,376*412/580)

    def test_sheet_prints_exact_scale_in_svg_and_dxf(self):
        geometry=geo(160,110)
        scale=decide_print_scale(geometry)
        snapshot={
            'geometry':geometry,'print_scale':scale,
            '_cad_background':{'status':'COMPLETE','features':[
                {'type':'Feature','geometry':geometry,'properties':{'jibun':'100'}}]}
        }
        item={'id':'1-1','name':'대상지','features':[],'legend':[]}
        ops=make_sheet(item,snapshot)
        self.assertTrue(any(o.get('type')=='text' and o.get('text')=='1:1,000' for o in ops))
        self.assertIn(b'1:1,000',svg_sheet(ops))
        self.assertIn(b'width="420mm" height="297mm"',svg_sheet(ops))
        self.assertIn(b'<g transform="translate(4 ',svg_sheet(ops))
        # The plotted 160-m-wide site must be 160 mm wide at scale 1:1000.
        doc=ezdxf.new('R2010')
        dxf_paper_sheet(doc,ops)
        self.assertIn('SURVEY_A3',doc.layouts)
        self.assertTrue(any(e.dxftype()=='TEXT' and e.dxf.text=='1:1,000'
                            for e in doc.layouts.get('SURVEY_A3')))

    def test_nonscale_cannot_be_mislabeled_fixed(self):
        geometry=geo(1100,80)
        chosen=decide_print_scale(geometry)
        snapshot={'geometry':geometry,'print_scale':chosen,
                  '_cad_background':{'status':'COMPLETE','features':[]}}
        ops=make_sheet({'id':'1-1','features':[]},snapshot)
        self.assertEqual(chosen['label'],'N.S. (자동맞춤)')
        self.assertTrue(any(o.get('text')=='N.S. (자동맞춤)' for o in ops))
        self.assertNotIn(b'1:5,000',svg_sheet(ops))

    def test_reject_forged_fixed_scale_that_does_not_fit(self):
        geometry=geo(1000,1000)
        snapshot={'geometry':geometry,
                  'print_scale':{'mode':'FIXED','denominator':1000,'label':'1:1,000'},
                  '_cad_background':{'status':'COMPLETE','features':[]}}
        with self.assertRaisesRegex(ValueError,'does not contain'):
            make_sheet({'id':'1-1','features':[]},snapshot)

if __name__=='__main__':
    unittest.main()
