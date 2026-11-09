"""Offline regression: 2-5 land shape and 2-6 cover; NO real API requests."""
import io
import unittest
from shapely.geometry import box, mapping
from shapely.ops import transform
from pyproj import Transformer
import ezdxf

from development_land_shape import attach_land_shape_theme
from development_landcover import attach_landcover_theme, WMS_LAYER
from development_cad import _exportable
from development_survey_sheet import make_sheet, svg_sheet, dxf_paper_sheet

BACK=Transformer.from_crs('EPSG:5179','EPSG:4326',always_xy=True).transform

def polygon(x0,y0,x1,y1):
    return mapping(transform(BACK,box(x0,y0,x1,y1)))

class LandThemeTest(unittest.TestCase):
    def setUp(self):
        self.site=polygon(953000,1950000,953020,1950010)
        self.features=[
            {'type':'Feature','geometry':polygon(953000,1950000,953010,1950010),
             'properties':{'pnu':'p1','jibun':'1'}},
            {'type':'Feature','geometry':polygon(953010,1950000,953020,1950010),
             'properties':{'pnu':'p2','jibun':'2'}}]
        self.snapshot={'geometry':self.site,
                       '_report_layers':{'LP_PA_CBND_BUBUN':{'status':'COMPLETE','features':self.features}},
                       '_cad_background':{'status':'COMPLETE','features':self.features},
                       '_national_sources':{
                           'characteristics:p1':{'status':'ATTRIBUTE_ONLY',
                             'record':{'pnu':'p1','topgrphFrmCodeNm':'사다리형'}},
                           'characteristics:p2':{'status':'ATTRIBUTE_ONLY',
                             'record':{'pnu':'p2','topgrphFrmCodeNm':'부정형'}}}}
        self.data={'items':[{'id':'2-5','name':'토지형상별'},
                            {'id':'2-6','name':'토지피복별'}],
                   'land':[{'pnu':'p1'},{'pnu':'p2'}],
                   'statistics':{}}
        self.area=lambda g:transform(Transformer.from_crs('EPSG:4326','EPSG:5179',always_xy=True).transform,g).area

    def render(self,item):
        ops=make_sheet(item,self.snapshot)
        self.assertTrue(any(op.get('layer')=='SHEET_SITE' for op in ops))
        self.assertTrue(any(op.get('text')=='범 례' for op in ops))
        self.assertIn('SHEET_THEME_C01',{op.get('layer') for op in ops})
        self.assertTrue(svg_sheet(ops).startswith(b'<svg'))
        doc=ezdxf.new('R2010')
        dxf_paper_sheet(doc,ops)
        raw=io.StringIO()
        doc.write(raw)
        reread=ezdxf.read(io.StringIO(raw.getvalue()))
        self.assertIn('SURVEY_A3',reread.layouts)
        self.assertGreater(len(list(reread.layouts.get('SURVEY_A3').query('HATCH'))),0)

    def test_shape_full_source_has_editable_sheet(self):
        attach_land_shape_theme(self.data,self.snapshot,self.area)
        item=self.data['items'][0]
        self.assertTrue(_exportable(item,self.snapshot))
        self.assertEqual(item['status'],'공간자료 확보')
        self.assertEqual(sum(x['area_m2'] for x in item['legend']),200)
        self.render(item)

    def test_shape_missing_value_is_gray_not_inferred(self):
        self.snapshot['_national_sources'].pop('characteristics:p2')
        attach_land_shape_theme(self.data,self.snapshot,self.area)
        item=self.data['items'][0]
        self.assertEqual(item['status'],'일부 확보')
        self.assertIn('형상 미확보',[x['name'] for x in item['legend']])

    def test_shape_browser_reference_is_not_official_cad(self):
        self.snapshot['_report_layers']['LP_PA_CBND_BUBUN']['status']='REFERENCE'
        attach_land_shape_theme(self.data,self.snapshot,self.area)
        self.assertFalse(_exportable(self.data['items'][0],self.snapshot))

    def test_wms_only_never_enables_cad_or_fake_area(self):
        attach_landcover_theme(self.data,self.snapshot,self.area)
        item=self.data['items'][1]
        self.assertEqual(item['wms_layer'],WMS_LAYER)
        self.assertFalse(_exportable(item,self.snapshot))
        self.assertNotIn('features',item)
        self.assertNotIn('2-6',self.data['statistics'])

    def test_complete_vector_is_classified_and_editable(self):
        fs=[{'type':'Feature','geometry':f['geometry'],
             'properties':{'name':'활엽수림' if i==0 else '침엽수림'}}
            for i,f in enumerate(self.features)]
        self.snapshot['_national_sources']['file:2-6']={
            'status':'DATA','features':fs,'source':'official vector','as_of':'2025'}
        attach_landcover_theme(self.data,self.snapshot,self.area)
        item=self.data['items'][1]
        self.assertTrue(_exportable(item,self.snapshot))
        self.assertEqual(sum(x['area_m2'] for x in item['legend']),200)
        self.render(item)

    def test_incomplete_vector_cannot_export_even_with_official_partial_source(self):
        self.snapshot['_national_sources']['file:2-6']={
            'status':'PARTIAL','features':[{'geometry':self.features[0]['geometry'],
                       'properties':{'name':'활엽수림'}}]}
        attach_landcover_theme(self.data,self.snapshot,self.area)
        self.assertFalse(_exportable(self.data['items'][1],self.snapshot))

if __name__=='__main__':
    unittest.main()
