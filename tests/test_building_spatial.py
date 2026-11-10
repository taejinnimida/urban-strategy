"""Regression: official GIS building footprints are not cadastral parcels."""
import unittest
from shapely.geometry import box, mapping
from shapely.ops import transform
from pyproj import Transformer
from development.building.spatial import attach_building_spatial_themes
from development.cad import _exportable, dxf_bytes
from development.survey_sheet import make_sheet, svg_sheet

TO_GEO=Transformer.from_crs(5179,4326,always_xy=True).transform
TO_METRIC=Transformer.from_crs(4326,5179,always_xy=True).transform


def poly(a,b,c,d):
    return mapping(transform(TO_GEO,box(a,b,c,d)))

class BuildingSpatialTest(unittest.TestCase):
    def setUp(self):
        self.site=poly(953000,1950000,953100,1950100)
        self.base={'geometry':self.site,
            '_cad_background':{'status':'COMPLETE','features':[
                {'type':'Feature','geometry':self.site,'properties':{'jibun':'1'}}]},
            '_national_sources':{}}
        self.data={'items':[
            {'id':'3-1','survey_table':{'title':'건축HUB 표제부 용도별 5건'}},
            {'id':'3-2','survey_table':{'title':'건축HUB 표제부 구조별 5건'}}],
            'buildings':[{'id':str(i)} for i in range(5)],'statistics':{}}
        self.area=lambda g:transform(TO_METRIC,g).area

    def test_ledger_only_never_paints_parcel_as_building(self):
        attach_building_spatial_themes(self.data,self.base,self.area)
        self.assertEqual(self.data['items'][0]['spatial_status'],'MISSING')
        self.assertNotIn('features',self.data['items'][0])
        self.assertFalse(_exportable(self.data['items'][0],self.base))
        self.assertEqual(self.data['items'][0]['survey_table']['title'],
                         '건축HUB 표제부 용도별 5건')

    def test_two_official_buildings_are_independent_of_five_hub_titles(self):
        f=[]
        for i,(name,structure) in enumerate([('공동주택','철근콘크리트구조'),
                                              ('업무시설','일반철골구조')]):
            f.append({'type':'Feature','geometry':poly(953010+i*30,1950020,953025+i*30,1950040),
                      'properties':{'name':str(i),'building_id':f'GIS-{i}',
                                    'use':name,'structure':structure}})
        self.base['_national_sources']['file:3-spatial']={
            'status':'DATA','source':'국토교통부 GIS건물통합정보','features':f}
        attach_building_spatial_themes(self.data,self.base,self.area)
        for item in self.data['items']:
            self.assertTrue(_exportable(item,self.base))
            self.assertEqual(item['spatial_status'],'COMPLETE')
            self.assertEqual(len(item['features']),2)
            self.assertEqual(item['survey_table']['total_count'],2)
            self.assertEqual(item['building_registry_table']['title'],
                             '건축HUB 표제부 '+('용도별' if item['id']=='3-1' else '구조별')+' 5건')
            self.assertGreater(item['statistics']['mapped_footprint_area_m2'],0)
            ops=make_sheet(item,self.base)
            self.assertIn(b'<svg',svg_sheet(ops))
            raw=dxf_bytes(item,self.base,{})
            self.assertIn(('SURVEY_'+item['id'].replace('-','_')).encode(),raw)

    def test_missing_class_is_not_made_up(self):
        self.base['_national_sources']['file:3-spatial']={
            'status':'DATA','source':'official source','features':[{
                'type':'Feature','geometry':poly(953010,1950020,953030,1950040),
                'properties':{'building_id':'A','use':'','structure':'철근콘크리트'}}]}
        attach_building_spatial_themes(self.data,self.base,self.area)
        self.assertEqual(self.data['items'][0]['spatial_status'],'PARTIAL')
        self.assertFalse(_exportable(self.data['items'][0],self.base))
        self.assertIn('분류 미확보',[v['name'] for v in self.data['items'][0]['legend']])
        self.assertEqual(self.data['items'][1]['spatial_status'],'COMPLETE')

    def test_invalid_or_duplicate_geometry_fails_closed(self):
        p=poly(953010,1950020,953030,1950040)
        self.base['_national_sources']['file:3-spatial']={
            'status':'DATA','features':[
                {'type':'Feature','geometry':p,'properties':{'building_id':'A','use':'주택','structure':'철골'}},
                {'type':'Feature','geometry':p,'properties':{'building_id':'A','use':'주택','structure':'철골'}}]}
        attach_building_spatial_themes(self.data,self.base,self.area)
        self.assertEqual(self.data['items'][0]['spatial_status'],'ERROR')
        self.assertNotIn('features',self.data['items'][0])
        self.assertFalse(_exportable(self.data['items'][0],self.base))

if __name__=='__main__': unittest.main()
