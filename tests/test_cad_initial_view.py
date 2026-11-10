"""Regression: projected CAD opens at site rather than EPSG:5179 origin.

Model-space geometry remains unchanged; A3 paper-space is separate.
"""
import io
import unittest

import ezdxf
from shapely.geometry import box, mapping
from shapely.ops import transform
from pyproj import Transformer

from development.cad import _configure_initial_cad_view, _drawing_frame, dxf_bytes

TO_WGS=Transformer.from_crs('EPSG:5179','EPSG:4326',always_xy=True).transform
SITE=mapping(transform(TO_WGS,box(953000,1950000,953100,1950120)))
FRAME=_drawing_frame(__import__('shapely').geometry.shape(SITE))


class CADStartupViewTest(unittest.TestCase):
    def test_initial_active_model_view_and_limits_are_at_site(self):
        doc=ezdxf.new('R2010')
        opts=_configure_initial_cad_view(doc,FRAME)
        center=((FRAME.bounds[0]+FRAME.bounds[2])/2,
                (FRAME.bounds[1]+FRAME.bounds[3])/2)
        self.assertAlmostEqual(opts['center_epsg5179'][0],center[0],places=2)
        self.assertAlmostEqual(opts['center_epsg5179'][1],center[1],places=2)
        self.assertGreater(opts['view_height_m'],0)
        self.assertEqual(int(doc.header['$TILEMODE']),1)
        self.assertGreater(doc.modelspace().dxf_layout.dxf.limmin.x,900000)
        self.assertGreater(doc.modelspace().dxf_layout.dxf.limmin.y,1900000)
        vports=doc.viewports.get('*Active')
        if not isinstance(vports,(list,tuple)):vports=[vports]
        self.assertTrue(any(abs(v.dxf.center.x-center[0])<.01 and
                            abs(v.dxf.center.y-center[1])<.01 for v in vports))

    def test_dxf_reopen_stays_projected_and_initial_view_is_site_centered(self):
        snapshot={'geometry':SITE,'print_scale':{'mode':'FIXED','denominator':1000,'label':'1:1,000'},
                  '_cad_background':{'status':'COMPLETE','features':[{
                      'type':'Feature','geometry':SITE,
                      'properties':{'jibun':'100','pnu':'1111010100100010001'}}]}}
        meta={}
        raw=dxf_bytes({'id':'1-1','name':'위치 및 면적','features':[],'legend':[]},snapshot,meta)
        reopened=ezdxf.read(io.StringIO(raw.decode('utf-8')))
        vports=reopened.viewports.get('*Active')
        if not isinstance(vports,(list,tuple)):vports=[vports]
        model_frame=meta['model_companion_sheet']['model_frame_bbox']
        expected_x=(model_frame[0]+model_frame[2])/2
        expected_y=(model_frame[1]+model_frame[3])/2
        self.assertTrue(any(abs(v.dxf.center.x-expected_x)<1 and
                            abs(v.dxf.center.y-expected_y)<1 for v in vports))
        self.assertGreater(reopened.header['$LIMMAX'][0],model_frame[2])
        self.assertIn('initial_cad_view',meta)
        self.assertEqual(int(reopened.header['$TILEMODE']),0)
        self.assertEqual(reopened.layouts.active_layout().name,'SURVEY_A3')
        self.assertGreater(reopened.header['$LIMMIN'][0],900000)
        self.assertGreater(reopened.header['$LIMMIN'][1],1900000)
        self.assertIn('SURVEY_A3',reopened.layouts)
        layout=reopened.layouts.get('SURVEY_A3')
        self.assertGreater(len(list(layout.query('LWPOLYLINE'))),4)
        self.assertTrue(any('범' in entity.dxf.text for entity in layout.query('TEXT')))
        self.assertTrue(any('1:1,000' in entity.dxf.text for entity in layout.query('TEXT')))
        self.assertTrue(any('N' == entity.dxf.text for entity in layout.query('TEXT')))
        self.assertTrue(list(reopened.modelspace().query('LWPOLYLINE[layer=="SITE_BOUNDARY"]')))

if __name__=='__main__':
    unittest.main()
