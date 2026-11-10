import unittest
from fastapi.testclient import TestClient
from dev_app import app
import app as original
class Tests(unittest.TestCase):
 def test_routes(self):
  c=TestClient(app)
  for url in ['/', '/seoul', '/nonurban','/dev-assets/nonurban.js']:
   self.assertEqual(c.get(url).status_code,200)
  self.assertNotIn('개발 검토 초안 만들기',TestClient(original.app).get('/').text)
 def test_inputs(self):
  c=TestClient(app)
  r=c.post('/api/dev/nonurban/intake',json={'project':'development'})
  self.assertEqual(r.json()['status'],'NOT_ASSESSED')
  self.assertTrue(all(x['status']=='UNKNOWN' for x in r.json()['checks']))
  for d in [{'project':'bad'},{'project':'facility','area_m2':-1},{'project':'facility','geometry':{'type':'Point','coordinates':[127,37]}}]:
   self.assertEqual(c.post('/api/dev/nonurban/intake',json=d).status_code,422)
 def test_geometry(self):
  geo={'type':'Polygon','coordinates':[[[127,37],[127.001,37],[127.001,37.001],[127,37.001],[127,37]]]}
  r=TestClient(app).post('/api/dev/nonurban/intake',json={'project':'district','area_m2':1,'geometry':geo})
  self.assertEqual(r.status_code,200)
  self.assertTrue(9000<r.json()['effective_area_m2']<11000)
