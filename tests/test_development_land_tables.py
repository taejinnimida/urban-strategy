"""Regression checks for report tables (no provider network required)."""
import unittest
from shapely.geometry import box, mapping
from development.land.tables import attach_land_survey_tables

class LandTableTest(unittest.TestCase):
    def setUp(self):
        self.site=box(0,0,10,10)
        self.snapshot={'geometry':mapping(self.site)}
        self.data={'area_m2':100,'as_of':'2026-11-09','parcel_status':'COMPLETE',
                   'land':[{'pnu':'1'},{'pnu':'2'}],
                   'items':[{'id':'2-2','name':'지목별','source':'VWorld',
                             'provider_verified':True,'features':[
                              {'type':'Feature','geometry':mapping(box(0,0,5,10)),'properties':{'pnu':'1'}},
                              {'type':'Feature','geometry':mapping(box(5,0,10,10)),'properties':{'pnu':'2'}}],
                             'legend':[{'name':'대','area_m2':50,'count':1,'color':'#aaa','class_code':'C01'},
                                       {'name':'답','area_m2':50,'count':1,'color':'#bbb','class_code':'C02'}]}]}
    def test_complete_table_and_consistent_area(self):
        attach_land_survey_tables(self.data,self.snapshot,lambda g:g.area)
        t=self.data['items'][0]['survey_table']
        self.assertEqual(t['status'],'COMPLETE')
        self.assertEqual(t['rows'][0]['area_m2'],100)
        self.assertEqual(t['rows'][1]['area_m2'],0)
        self.assertEqual(t['rows'][2]['count'],2)
        self.assertEqual(t['rows'][3]['pct'],50)
    def test_partial_missing_area_not_silently_normalized(self):
        self.data['items'][0]['features']=self.data['items'][0]['features'][:1]
        self.data['items'][0]['legend']=self.data['items'][0]['legend'][:1]
        attach_land_survey_tables(self.data,self.snapshot,lambda g:g.area)
        t=self.data['items'][0]['survey_table']
        self.assertEqual(t['status'],'PARTIAL')
        self.assertEqual(t['rows'][1]['area_m2'],50)
        self.assertEqual(t['rows'][2]['pct'],50)
    def test_wms_only_is_missing_not_zero(self):
        self.data['items'][0]={'id':'2-6','name':'토지피복별','landcover_map_available':True}
        attach_land_survey_tables(self.data,self.snapshot,lambda g:g.area)
        t=self.data['items'][0]['survey_table']
        self.assertEqual(t['status'],'MISSING')
        self.assertEqual(t['rows'],[])
    def test_overlap_not_double_counted(self):
        self.data['items'][0]['features']=[{'type':'Feature','geometry':mapping(self.site),'properties':{'pnu':'1'}},
                                           {'type':'Feature','geometry':mapping(self.site),'properties':{'pnu':'2'}}]
        self.data['items'][0]['legend'][0]['area_m2']=100
        self.data['items'][0]['legend'][1]['area_m2']=100
        attach_land_survey_tables(self.data,self.snapshot,lambda g:g.area)
        t=self.data['items'][0]['survey_table']
        self.assertEqual(t['status'],'PARTIAL')
        self.assertEqual(t['rows'][2]['area_m2'],100)
        self.assertEqual(t['rows'][3]['kind'],'overlap')
        self.assertEqual(t['rows'][3]['area_m2'],100)
if __name__=='__main__':unittest.main()
