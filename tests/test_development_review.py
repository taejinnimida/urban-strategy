import unittest
from types import SimpleNamespace
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from shapely.geometry import box,mapping
import development_review as d
class R:
 status_code=200
 def __init__(self,data):self.data=data
 def json(self):return self.data
 def raise_for_status(self):pass
class Tests(unittest.TestCase):
 def setUp(self):
  self.g=mapping(box(126.97,37.56,126.971,37.561));self.regions=[{'geometry':mapping(box(126,37,128,38)),'properties':{'ctp_kor_nm':'서울특별시'}}]
  self.s=SimpleNamespace(_vworld_key=lambda:'TEST',_vworld_domain=lambda:'localhost',_vworld_get=lambda *a,**k:(R({'features':self.regions}),'mock'),_fetch_planning_layer_vworld=lambda *a:([{'type':'Feature','geometry':self.g,'properties':{}}]*2,'mock'),VWORLD_SEARCH_URL='mock')
  app=FastAPI();d.register(app,self.s);self.c=TestClient(app)
 def create(self):return self.c.post('/api/development/snapshots',json={'geometry':self.g})
 def test_snapshot_isolation(self):
  a=self.create().json();self.g=mapping(box(127,37.6,127.001,37.601));b=self.create().json();self.assertNotEqual(a['id'],b['id']);self.assertEqual(self.c.get('/api/development/snapshots/'+a['id']).json()['geometry'],a['geometry']);self.assertEqual(a['region'],'SEOUL')
 def test_outside(self):
  self.regions[0]['properties']={'ctp_kor_nm':'경기도'};self.assertEqual(self.create().json()['region'],'OUTSIDE_OR_MIXED')
 def test_unknown(self):
  self.regions=[];self.assertEqual(self.create().json()['region'],'UNKNOWN')
 def test_partial(self):
  self.regions[0]['geometry']=mapping(box(126.97,37.56,126.9705,37.561));self.assertEqual(self.create().json()['region'],'UNKNOWN')
 def test_bounds(self):
  self.g=mapping(box(0,0,1,1));self.assertEqual(self.create().status_code,422);self.g=mapping(box(126,37,127,38));self.assertEqual(self.create().status_code,422)
 def test_expiration(self):
  a=self.create().json()
  with patch.object(d.time,'time',return_value=a['created_at']+7201):self.assertEqual(self.c.get('/api/development/snapshots/'+a['id']).status_code,404)
 def test_union(self):
  v=self.c.post('/api/development/layer',json={'geometry':self.g,'layer':'LT_C_UPISUQ153'}).json();self.assertEqual(v['status'],'COMPLETE');self.assertEqual(v['overlap_area_m2'],d.area(box(126.97,37.56,126.971,37.561)))
 def test_failure(self):
  def fail(*a):raise ValueError('SECRET')
  self.s._fetch_planning_layer_vworld=fail;r=self.c.post('/api/development/layer',json={'geometry':self.g,'layer':'LT_C_UPISUQ153'});self.assertEqual(r.json()['status'],'UNKNOWN');self.assertNotIn('SECRET',r.text)
 def test_allowlist(self):self.assertEqual(self.c.post('/api/development/layer',json={'geometry':self.g,'layer':'invalid'}).status_code,422)
 def test_address(self):
  self.s._vworld_get=lambda *a,**k:(R({'response':{'status':'OK','result':{'items':[{'address':{'parcel':'다른 주소'}}]}}}),'mock');v=self.c.post('/api/development/address',json={'address':'경기도 화성시 봉담읍 수영리 1'}).json();self.assertEqual(v['status'],'not_found');self.assertIsNone(v['feature'])
 def test_wms(self):self.assertEqual(self.c.get('/api/development/wms/cadastral?bbox=0,0,1,1').status_code,422)
if __name__=='__main__':unittest.main(verbosity=2)
