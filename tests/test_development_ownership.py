import copy
import unittest
from shapely.geometry import box, mapping
from development.land.ownership import attach_ownership_theme, ownership_class

class OwnershipTest(unittest.TestCase):
    def fixture(self, assure='COMPLETE'):
        fs = [dict(type='Feature', properties={'pnu': p}, geometry=mapping(box(i, 0, i+1, 1)))
              for i,p in enumerate(('p1','p2','p3'))]
        land = [dict(pnu='p1',ownership='국유지',ledger_status='확보'),
                dict(pnu='p2',ownership='개인',ledger_status='확보'),
                dict(pnu='p3',ownership='값누락',ledger_status='확보')]
        data={'land':land, 'items':[dict(id='2-1')], 'statistics':{}}
        snap={'geometry':mapping(box(0,0,3,1)), '_report_layers':{'LP_PA_CBND_BUBUN':{'status':assure,'features':fs}}}
        return data,snap
    def test_unknown_not_assumed_private(self):
        self.assertIsNone(ownership_class('값누락'))
        self.assertIsNone(ownership_class('김OO'))
        self.assertEqual(ownership_class('구유지'), '공유')
        self.assertEqual(ownership_class('법인'), '법인')
    def test_color_area_counts_and_missing(self):
        d,s=self.fixture()
        attach_ownership_theme(d,s,lambda g:g.area)
        item=d['items'][0]
        self.assertEqual(item['status'],'일부 확보')
        self.assertEqual(item['thematic_type'],'ownership')
        self.assertTrue(item['provider_verified'])
        self.assertEqual(len(item['features']),3)
        self.assertEqual(sum(x['area_m2'] for x in item['legend']),3)
        self.assertEqual(sum(x['count'] for x in item['legend']),3)
        self.assertEqual([x['name'] for x in item['legend']], ['국유','개인','소유구분 미확보'])
        self.assertEqual(d['statistics']['2-1']['classified_parcel_count'],2)
        self.assertEqual(d['statistics']['2-1']['unclassified_parcel_count'],1)
    def test_reference_never_official(self):
        d,s=self.fixture('REFERENCE')
        attach_ownership_theme(d,s,lambda g:g.area)
        self.assertEqual(d['items'][0]['status'],'보조 조회 참고자료')
        self.assertFalse(d['items'][0]['provider_verified'])
    def test_unverified_ledger_wont_classify(self):
        d,s=self.fixture()
        d['land'][0]['ledger_status']='조회 실패'
        attach_ownership_theme(d,s,lambda g:g.area)
        self.assertEqual(d['statistics']['2-1']['classified_parcel_count'],1)
    def test_no_verified_owners_no_fake_map(self):
        d,s=self.fixture()
        for row in d['land']: row['ledger_status']='조회 실패'
        attach_ownership_theme(d,s,lambda g:g.area)
        self.assertNotIn('thematic_type',d['items'][0])

if __name__ == '__main__':unittest.main()