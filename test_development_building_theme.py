"""Synthetic regression tests for independent building footprint survey maps."""
import unittest
from shapely.geometry import box, mapping
from development_building_theme import attach_building_themes, _classify


def feature(geom, mgm=None):
    properties = {'mgmBldrgstPk': mgm} if mgm else {}
    return {'type': 'Feature', 'geometry': mapping(geom), 'properties': properties}


def data():
    return {
        'as_of': '2026-11-10',
        'buildings': [{'id': 'MG001', 'pnus': '1111111111100000001',
                       'use': '단독주택', 'structure': '철근콘크리트구조',
                       'floors': 3, 'age': 25, 'floor_area': 100,
                       'coverage': 60, 'far': 150}],
        'items': [{'id': f'3-{i}', 'name': str(i), 'detail': ''} for i in range(1, 8)],
        'statistics': {f'3-{i}': {'field': 'test'} for i in range(1, 8)},
    }


def snapshot(features, status='COMPLETE'):
    return {
        'geometry': mapping(box(0, 0, 10, 10)),
        '_report_layers': {'LT_C_SPBD': {'status': status, 'features': features}},
    }


class BuildingSurveyTest(unittest.TestCase):
    def test_bins_follow_sample(self):
        self.assertEqual(_classify('3-3', 5), '5층 이상')
        self.assertEqual(_classify('3-4', 30), '30~40년')
        self.assertEqual(_classify('3-5', 85), '85~115㎡')
        self.assertEqual(_classify('3-6', 10), '20% 미만')
        self.assertEqual(_classify('3-7', 300), '300% 이상')
        self.assertEqual(_classify('3-5', 0), '기타(값 없음 등)')

    def test_verified_and_unmatched_both_drawn(self):
        d = data()
        attach_building_themes(d, snapshot([
            feature(box(1, 1, 3, 3), 'MG001'),
            feature(box(4, 4, 6, 6)),
        ]))
        for item in d['items']:
            self.assertEqual(len(item['features']), 2)
            self.assertEqual(item['survey_table']['rows'][0]['count'], 2)
            self.assertEqual(sum(e['count'] for e in item['legend']), 2)
            self.assertEqual(item['geometry_status'], 'PARTIAL')
        self.assertEqual(d['items'][0]['features'][0]['properties']['name'], '단독주택')
        self.assertEqual(d['items'][0]['features'][1]['properties']['name'], '대장 미확인')
        self.assertEqual(d['items'][2]['features'][0]['properties']['name'], '3층')

    def test_integrated_gis_attributes_classify_without_register_join(self):
        d = data()
        geom = feature(box(1, 1, 3, 3))
        geom['properties'] = {
            'mainPurpsCdNm': '근린생활시설',
            'grndFlrCnt': 4,
            'totArea': 110,
            'bcRat': 55,
            'vlRat': 205,
            'useAprDay': '20010101',
        }
        s = snapshot([], 'UNKNOWN')
        s['_report_layers']['LT_C_BLDGINFO'] = {
            'status': 'COMPLETE', 'features': [geom],
        }
        attach_building_themes(d, s)
        self.assertEqual(d['items'][0]['features'][0]['properties']['name'], '근린생활시설')
        self.assertEqual(d['items'][2]['features'][0]['properties']['name'], '4층')
        self.assertEqual(d['items'][4]['features'][0]['properties']['name'], '85~115㎡')
        self.assertEqual(d['items'][5]['features'][0]['properties']['name'], '40~70%')
        self.assertEqual(d['items'][6]['features'][0]['properties']['name'], '200~300%')
        self.assertEqual(d['items'][0]['survey_table']['rows'][0]['count'], 1)

    def test_fallback_to_address_building_footprint(self):
        d = data()
        s = snapshot([feature(box(1, 1, 3, 3))])
        s['_report_layers']['LT_C_BLDGINFO'] = {
            'status': 'UNKNOWN', 'features': [],
        }
        attach_building_themes(d, s)
        self.assertEqual(d['items'][0]['features'][0]['properties']['name'], '대장 미확인')
        self.assertIn('보조', d['items'][0]['source'])

    def test_zero_footprints_yields_real_blank_sheet_data(self):
        d = data()
        s = snapshot([])
        s['_report_layers']['LT_C_BLDGINFO'] = {'status': 'COMPLETE', 'features': []}
        attach_building_themes(d, s)
        for item in d['items']:
            self.assertEqual(item['features'], [])
            self.assertEqual(item['survey_table']['rows'][0]['count'], 0)
            self.assertTrue(item['provider_verified'])
            self.assertEqual(item['geometry_status'], 'COMPLETE')

    def test_partial_empty_is_not_verified_zero(self):
        d = data()
        attach_building_themes(d, snapshot([]))
        self.assertEqual(d['building_fact']['geometry_status'], 'PARTIAL')
        self.assertFalse(d['building_fact']['counts']['zero_confirmed'])
        self.assertEqual(d['items'][0]['survey_table']['rows'][0]['count'], 0)
        self.assertFalse(d['items'][0]['provider_verified'])

    def test_direct_join_conflict_is_not_silently_selected(self):
        d = data()
        geom = feature(box(1, 1, 3, 3), 'MG001')
        geom['properties']['mainPurpsCdNm'] = '근린생활시설'
        s = snapshot([], 'COMPLETE')
        s['_report_layers']['LT_C_BLDGINFO'] = {'status': 'COMPLETE', 'features': [geom]}
        attach_building_themes(d, s)
        self.assertEqual(d['items'][0]['features'][0]['properties']['name'], '자료 불일치(검토)')
        self.assertEqual(d['building_fact']['records'][0]['quality_grade'], 'E_CONFLICT')
        self.assertIsNone(d['building_fact']['records'][0]['attributes']['use']['value'])

    def test_spatial_overlap_not_fake_direct_register_match(self):
        d = data()
        attach_building_themes(d, snapshot([feature(box(1, 1, 3, 3))]))
        self.assertEqual(d['building_fact']['counts']['direct_register_matches'], 0)
        self.assertEqual(d['building_fact']['counts']['register_without_direct_geometry'], 1)
        self.assertEqual(d['items'][0]['features'][0]['properties']['name'], '대장 미확인')

    def test_duplicate_address_polygon_suppressed(self):
        d = data()
        geom = feature(box(1, 1, 3, 3))
        geom['properties'] = {'mainPurpsCdNm': '단독주택'}
        s = snapshot([feature(box(1, 1, 3, 3))])
        s['_report_layers']['LT_C_BLDGINFO'] = {'status': 'COMPLETE', 'features': [geom]}
        attach_building_themes(d, s)
        self.assertEqual(d['building_fact']['counts']['mapped_shapes'], 1)
        self.assertEqual(d['building_fact']['counts']['address_duplicates_suppressed'], 1)

    def test_disjoint_fallback_retained_with_gis(self):
        d = data()
        geom = feature(box(1, 1, 3, 3))
        geom['properties'] = {'mainPurpsCdNm': '근린생활시설'}
        s = snapshot([feature(box(6, 6, 8, 8))])
        s['_report_layers']['LT_C_BLDGINFO'] = {'status': 'COMPLETE', 'features': [geom]}
        attach_building_themes(d, s)
        self.assertEqual(d['building_fact']['counts']['mapped_shapes'], 2)
        self.assertEqual(d['items'][0]['features'][1]['properties']['name'], '대장 미확인')

    def _parcel(self, bounds, pnu='1111111111100000001'):
        return {'type': 'Feature', 'id': pnu, 'geometry': mapping(box(*bounds)),
                'properties': {'pnu': pnu}}

    def test_spatial_candidate_is_provisional_not_exact(self):
        d = data()
        geom = feature(box(1, 1, 3, 3))
        geom['properties'] = {'bcRat': 60, 'vlRat': 150}
        s = snapshot([])
        s['_report_layers']['LT_C_BLDGINFO'] = {
            'status': 'COMPLETE', 'features': [geom]}
        s['_report_layers']['LP_PA_CBND_BUBUN'] = {
            'status': 'COMPLETE', 'features': [self._parcel((0, 0, 4, 4))]}
        attach_building_themes(d, s)
        record = d['building_fact']['records'][0]
        self.assertEqual(record['join_status'], 'PARCEL_SPATIAL_REFERENCE')
        self.assertEqual(record['spatial_parcel_pnu'], '1111111111100000001')
        self.assertEqual(record['spatial_overlap_pct'], 100)
        self.assertEqual(record['attributes']['coverage']['assurance'], 'SPATIAL_AGREEMENT')
        self.assertEqual(record['attributes']['use']['assurance'], 'SPATIAL_REFERENCE')
        self.assertEqual(d['building_fact']['counts']['direct_register_matches'], 0)
        self.assertEqual(d['building_fact']['counts']['spatial_register_candidates'], 1)
        self.assertEqual(d['items'][0]['features'][0]['properties']['name'], '단독주택')
        self.assertIn('잠정', d['items'][0]['survey_table']['rows'][1]['note'])

    def test_no_spatial_candidate_without_complete_cadastral(self):
        d = data()
        s = snapshot([])
        s['_report_layers']['LT_C_BLDGINFO'] = {
            'status': 'COMPLETE', 'features': [feature(box(1, 1, 3, 3))]}
        s['_report_layers']['LP_PA_CBND_BUBUN'] = {
            'status': 'REFERENCE', 'features': [self._parcel((0, 0, 4, 4))]}
        attach_building_themes(d, s)
        self.assertEqual(d['building_fact']['counts']['spatial_register_candidates'], 0)

    def test_multi_footprints_same_register_rejected(self):
        d = data()
        s = snapshot([])
        s['_report_layers']['LT_C_BLDGINFO'] = {
            'status': 'COMPLETE',
            'features': [feature(box(1, 1, 2, 2)), feature(box(2, 2, 3, 3))]}
        s['_report_layers']['LP_PA_CBND_BUBUN'] = {
            'status': 'COMPLETE', 'features': [self._parcel((0, 0, 4, 4))]}
        attach_building_themes(d, s)
        self.assertEqual(d['building_fact']['counts']['spatial_register_candidates'], 0)

    def test_partial_parcel_overlap_not_silently_joined(self):
        d = data()
        s = snapshot([])
        s['_report_layers']['LT_C_BLDGINFO'] = {
            'status': 'COMPLETE', 'features': [feature(box(1, 1, 3, 3))]}
        s['_report_layers']['LP_PA_CBND_BUBUN'] = {
            'status': 'COMPLETE', 'features': [self._parcel((0, 0, 2, 4))]}
        attach_building_themes(d, s)
        self.assertEqual(d['building_fact']['counts']['spatial_register_candidates'], 0)

    def test_zero_ratio_is_not_taken_as_confirmed_gis_value(self):
        d = data()
        geom = feature(box(1, 1, 3, 3))
        geom['properties'] = {'bcRat': 0, 'vlRat': 0}
        s = snapshot([])
        s['_report_layers']['LT_C_BLDGINFO'] = {
            'status': 'COMPLETE', 'features': [geom]}
        s['_report_layers']['LP_PA_CBND_BUBUN'] = {
            'status': 'COMPLETE', 'features': [self._parcel((0, 0, 4, 4))]}
        attach_building_themes(d, s)
        record = d['building_fact']['records'][0]
        self.assertEqual(record['attributes']['coverage']['value'], 60)
        self.assertEqual(record['attributes']['coverage']['assurance'], 'SPATIAL_REFERENCE')

    def test_failed_layer_does_not_become_zero(self):
        d = data()
        attach_building_themes(d, snapshot([], 'UNKNOWN'))
        for item in d['items']:
            self.assertEqual(item['geometry_status'], 'UNAVAILABLE')
            self.assertFalse(item['provider_verified'])
            self.assertNotIn('survey_table', item)


if __name__ == '__main__':
    unittest.main()
