"""Offline regression tests for 4-3 to 4-6 natural-area vectors.

Run: python -m unittest tests.test_development_nature_vector
Uses synthetic GeoJSON + injected official-source snapshot, never a live API.
"""
import unittest
from shapely.geometry import box, mapping, LineString, Point
from development.nature.vector import attach_nature_vector_themes


def feature(g, name, source_layer=None):
    p = {'name': name}
    if source_layer:
        p['source_layer'] = source_layer
    return {'type': 'Feature', 'geometry': mapping(g), 'properties': p}


def scenario(key, status, features, site=None):
    site = site or box(127, 37, 127.01, 37.01)
    fact = {'status': status, 'source': '합성 테스트', 'as_of': '2026-10-01', 'features': features}
    data = {'items': [{'id': key, 'source': '테스트'}], 'statistics': {}}
    snapshot = {'geometry': mapping(site), '_national_sources': {'file:' + key: fact}}
    return data, snapshot


class NatureVectorTest(unittest.TestCase):
    def test_wms_only_is_not_vector(self):
        data, snapshot = scenario('4-4', 'MAP_ONLY', [])
        snapshot['_national_sources']['ecvam'] = {'status': 'MAP_ONLY', '_image': b'png'}
        attach_nature_vector_themes(data, snapshot)
        x = data['items'][0]
        self.assertFalse(x['provider_verified'])
        self.assertFalse(x['table_available'])
        self.assertEqual(x['features'], [])

    def test_overlap_does_not_double_count_total_area(self):
        g1 = box(127.001, 37.001, 127.005, 37.005)
        g2 = box(127.003, 37.003, 127.008, 37.007)
        data, snapshot = scenario('4-5', 'DATA', [
            feature(g1, '침엽수림'), feature(g2, '활엽수림')])
        attach_nature_vector_themes(data, snapshot)
        x = data['items'][0]
        self.assertTrue(x['table_available'])
        self.assertFalse(x['provider_verified'])
        self.assertEqual(len(x['legend']), 2)
        self.assertGreater(data['statistics']['4-5']['overlap_warning_m2'], 0)
        self.assertTrue(any(r['kind'] == 'warning' for r in x['nature_vector_summary']['rows']))

    def test_4_6_mixed_dimensions_do_not_invent_areas(self):
        data, snapshot = scenario('4-6', 'DATA', [
            feature(box(127.001, 37.001, 127.004, 37.004), '하천구역', 'zone'),
            feature(LineString([(127.001, 37.001), (127.009, 37.009)]), '하천중심선', 'line'),
            feature(Point(127.005,37.005), '관측지점', 'point')])
        attach_nature_vector_themes(data, snapshot)
        x = data['items'][0]
        self.assertTrue(x['table_available'])
        dims = {v['dimension']:v for v in x['legend']}
        self.assertEqual(set(dims), {'면','선','점'})
        self.assertIsNone(dims['선']['area_m2'])
        self.assertIsNone(dims['점']['area_m2'])
        self.assertGreater(dims['선']['length_m'], 0)
        self.assertEqual(dims['점']['count'], 1)
        self.assertEqual(len(x['features']), 3)

    def test_source_class_names_preserved(self):
        g = box(127.001,37.001,127.002,37.002)
        data, snapshot = scenario('4-5','DATA',[feature(g,'혼효림: 3영급')])
        attach_nature_vector_themes(data,snapshot)
        self.assertEqual(data['items'][0]['legend'][0]['name'], '혼효림: 3영급')
        self.assertEqual(data['items'][0]['features'][0]['properties']['name'], '혼효림: 3영급')

    def test_missing_name_remains_unknown(self):
        g = box(127.001,37.001,127.002,37.002)
        data, snapshot = scenario('4-4','PARTIAL',[feature(g,'분류 미확보')])
        attach_nature_vector_themes(data,snapshot)
        self.assertTrue(data['items'][0]['table_available'])
        self.assertFalse(data['items'][0]['provider_verified'])
        self.assertIn('분류 미확보',data['items'][0]['legend'][0]['name'])

    def test_invalid_geometry_fails_closed(self):
        bow = {'type':'Polygon','coordinates':[[[127,37],[127.01,37.01],[127,37.01],[127.01,37],[127,37]]]}
        data,snapshot=scenario('4-4','DATA',[{'type':'Feature','geometry':bow,'properties':{'name':'1등급'}}])
        attach_nature_vector_themes(data,snapshot)
        self.assertFalse(data['items'][0]['table_available'])
        self.assertFalse(data['items'][0]['provider_verified'])

    def test_no_registered_source_is_not_zero(self):
        data,snapshot=scenario('4-6','MISSING_SOURCE',[])
        attach_nature_vector_themes(data,snapshot)
        self.assertNotIn('nature_vector_summary',data['items'][0])
        self.assertFalse(data['items'][0]['table_available'])


    def test_complete_vector_produces_real_dxf_and_svg(self):
        from development.cad import _exportable, dxf_bytes
        from development.survey_sheet import make_sheet, svg_sheet
        site = box(127, 37, 127.01, 37.01)
        data, snapshot = scenario('4-6', 'DATA', [
            feature(box(127.002,37.002,127.005,37.005), '하천구역', 'zone'),
            feature(LineString([(127.001,37.001),(127.009,37.009)]), '하천중심선', 'line'),
            feature(Point(127.006,37.006), '관측지점', 'point'),
        ], site=site)
        attach_nature_vector_themes(data, snapshot)
        item=data['items'][0]
        self.assertTrue(_exportable(item, snapshot))
        snapshot['_cad_background'] = {
            'status':'COMPLETE','features':[feature(site, '지적')],
            'request_count':1,'coverage_assurance':'PROVIDER_RESPONSE_ONLY',
        }
        payload=dxf_bytes(item, snapshot, {})
        self.assertIn(b'SURVEY_4_6',payload)
        svg=svg_sheet(make_sheet(item,snapshot))
        self.assertIn(b'<svg',svg)
        self.assertIn(b'<circle',svg)

    def test_wms_only_not_exportable(self):
        from development.cad import _exportable
        data, snapshot = scenario('4-4', 'MAP_ONLY', [])
        attach_nature_vector_themes(data, snapshot)
        self.assertFalse(_exportable(data['items'][0], snapshot))


if __name__ == '__main__':
    unittest.main()
