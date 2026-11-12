"""Map-only zoning must never replace site-scoped report FACTs."""
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
from shapely.geometry import box, mapping, shape
import development_review as d


class Response:
    def __init__(self, items):
        self.items = items

    def json(self):
        return {'response': {'status': 'OK', 'result': {'featureCollection': {'features': self.items}}}}

    def close(self):
        pass


class PlanningViewTests(unittest.TestCase):
    def setUp(self):
        self.geometry = mapping(box(126.97, 37.56, 126.971, 37.561))
        self.app = FastAPI()
        service = SimpleNamespace(_vworld_key=lambda: 'TEST',
                                  _vworld_domain=lambda: 'http://localhost')
        self.service = service
        d.register(self.app, service)
        self.client = TestClient(self.app)

    def request(self, layer='LT_C_UQ111'):
        return self.client.post('/api/development/planning-view',
                                json={'layer': layer, 'geometry': self.geometry})

    def test_viewport_scope_and_fact_isolation(self):
        feature = {'type': 'Feature', 'id': 'zoning-1',
                   'geometry': mapping(box(126.9695, 37.5595, 126.9715, 37.5615)),
                   'properties': {'uname': '제2종일반주거지역'}}
        with patch.object(d.VWorldChannel, 'request',
                          return_value=(Response([feature]), 'test')):
            snap = self.client.post('/api/development/snapshots',
                                    json={'geometry': self.geometry}).json()
            result = self.request().json()
            stored = self.client.get('/api/development/snapshots/' + snap['id'] + '/layers').json()
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertTrue(result['display_only'])
        self.assertEqual(result['count'], 1)
        self.assertEqual(shape(result['features'][0]['geometry']).bounds,
                         shape(self.geometry).bounds)
        self.assertEqual(stored['layers'], {})

    def test_reject_non_zoning_layer(self):
        self.assertEqual(self.request('LP_PA_CBND_BUBUN').status_code, 422)

    def test_reject_oversized_viewport(self):
        self.geometry = mapping(box(126.97, 37.56, 127.02, 37.61))
        self.assertEqual(self.request().status_code, 422)

    def test_provider_error_never_returns_empty_complete(self):
        with patch.object(d.VWorldChannel, 'request', side_effect=OSError('test')):
            result = self.request().json()
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertEqual(result['features'], [])


if __name__ == '__main__':
    unittest.main()
