"""Official WMS vs WFS transport contracts, with all remote responses mocked."""
import io
import json
import struct
import unittest
from unittest.mock import patch
from shapely.geometry import mapping, box
from development_landcover_service import (
    fetch_landcover_wms, fetch_landcover_wfs, _bounds, LAYER,
    WMS_ENDPOINTS, WFS_ENDPOINT,
)

SITE = mapping(box(127.01, 37.50, 127.012, 37.501))


class MockResponse:
    status_code = 200

    def __init__(self, data, code=200):
        self.content = data
        self.status_code = code
        self.closed = False

    def close(self):
        self.closed = True


def png_signature(width=1024, height=768):
    # Transport-level header test only. Not a complete PNG stream.
    return b'\x89PNG\r\n\x1a\n' + b'\x00\x00\x00\x0dIHDR' + struct.pack('>II', width, height) + b'\x08\x06\x00\x00\x00'


class LandCoverServiceTest(unittest.TestCase):
    def test_bbox_remains_local_and_geo_reference_has_lat_lon_order(self):
        bbox, corners = _bounds(SITE)
        self.assertLess(bbox[0], bbox[2])
        self.assertLess(bbox[1], bbox[3])
        self.assertTrue(37 < corners[0][0] < 38)
        self.assertTrue(127 < corners[0][1] < 128)

    def test_wms_png_request_uses_fixed_official_layer(self):
        calls = []
        def get(url, params, timeout, allow_redirects, headers):
            calls.append((url, params))
            return MockResponse(png_signature())
        with patch('development_landcover_service.requests.get', side_effect=get):
            result = fetch_landcover_wms(SITE)
        self.assertEqual(result['status'], 'MAP_ONLY')
        self.assertEqual(result['layer'], LAYER)
        self.assertEqual(calls[0][0], WMS_ENDPOINTS[0])
        self.assertEqual(calls[0][1]['SRS'], 'EPSG:3857')
        self.assertNotIn('features', result)
        self.assertEqual(result['map_width'], 1024)

    def test_non_png_is_not_fake_map(self):
        with patch('development_landcover_service.requests.get',
                   return_value=MockResponse(b'<ServiceException>Access denied</ServiceException>')):
            result = fetch_landcover_wms(SITE)
        self.assertEqual(result['status'], 'ERROR')
        self.assertEqual(result['error_code'], 'NOT_PNG')

    def test_unadvertised_layer_never_causes_getfeature(self):
        xml = b'<WFS_Capabilities><FeatureTypeList><FeatureType><Name>EGIS:other</Name></FeatureType></FeatureTypeList></WFS_Capabilities>'
        called = []
        def get(url, params, timeout, allow_redirects, headers):
            called.append(params['request'])
            return MockResponse(xml)
        with patch('development_landcover_service.requests.get', side_effect=get):
            result = fetch_landcover_wfs(SITE)
        self.assertEqual(result['status'], 'WFS_NOT_ADVERTISED')
        self.assertEqual(called, ['GetCapabilities'])

    def test_wfs_featuretype_confirmed_then_geojson_bounded(self):
        xml = ('<WFS_Capabilities><FeatureTypeList><FeatureType><Name>'
               + LAYER + '</Name></FeatureType></FeatureTypeList></WFS_Capabilities>').encode()
        geo = json.dumps({'type': 'FeatureCollection', 'features': [
            {'type': 'Feature', 'geometry': SITE, 'properties': {'name': '활엽수림'}}
        ]}).encode()
        def get(url, params, timeout, allow_redirects, headers):
            return MockResponse(xml if params['request'] == 'GetCapabilities' else geo)
        with patch('development_landcover_service.requests.get', side_effect=get):
            result = fetch_landcover_wfs(SITE)
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['features'][0]['properties']['name'], '활엽수림')
        self.assertEqual(result['source_url'], 'https://aid.mcee.go.kr/api/land.do')

    def test_wfs_rejects_missing_classification(self):
        xml = ('<WFS_Capabilities><FeatureTypeList><FeatureType><Name>'
               + LAYER + '</Name></FeatureType></FeatureTypeList></WFS_Capabilities>').encode()
        geo = json.dumps({'type': 'FeatureCollection', 'features': [
            {'type': 'Feature', 'geometry': SITE, 'properties': {'unrelated': 'foo'}}
        ]}).encode()
        def get(url, params, timeout, allow_redirects, headers):
            return MockResponse(xml if params['request'] == 'GetCapabilities' else geo)
        with patch('development_landcover_service.requests.get', side_effect=get):
            result = fetch_landcover_wfs(SITE)
        self.assertEqual(result['status'], 'WFS_UNVERIFIED')
        self.assertEqual(result['error_code'], 'MISSING_CLASS')


if __name__ == '__main__':
    unittest.main()
