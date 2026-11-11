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
        self.assertEqual(d['items'][0]['features'][1]['properties']['name'], '대장·도형 미연계')
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
        self.assertEqual(d['items'][0]['features'][0]['properties']['name'], '대장·도형 미연계')
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
        self.assertEqual(d['items'][0]['features'][0]['properties']['name'], '단독주택')
        self.assertEqual(d['building_fact']['records'][0]['quality_grade'], 'E_CONFLICT')
        self.assertEqual(d['building_fact']['records'][0]['attributes']['use']['value'], '단독주택')
        self.assertEqual(d['building_fact']['records'][0]['attributes']['use']['gis_value'], '근린생활시설')
        self.assertEqual(d['building_fact']['records'][0]['attributes']['use']['assurance'], 'LEDGER_GIS_CONFLICT')

    def test_spatial_overlap_not_fake_direct_register_match(self):
        d = data()
        attach_building_themes(d, snapshot([feature(box(1, 1, 3, 3))]))
        self.assertEqual(d['building_fact']['counts']['direct_register_matches'], 0)
        self.assertEqual(d['building_fact']['counts']['register_without_direct_geometry'], 1)
        self.assertEqual(d['items'][0]['features'][0]['properties']['name'], '대장·도형 미연계')

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
        self.assertEqual(d['items'][0]['features'][1]['properties']['name'], '대장·도형 미연계')

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

    def test_reference_cadastre_may_support_labelled_provisional_candidate(self):
        d = data()
        s = snapshot([])
        s['_report_layers']['LT_C_BLDGINFO'] = {
            'status': 'COMPLETE', 'features': [feature(box(1, 1, 3, 3))]}
        s['_report_layers']['LP_PA_CBND_BUBUN'] = {
            'status': 'REFERENCE', 'features': [self._parcel((0, 0, 4, 4))]}
        attach_building_themes(d, s)
        self.assertEqual(d['building_fact']['counts']['direct_register_matches'], 0)
        self.assertEqual(d['building_fact']['counts']['spatial_register_candidates'], 1)
        self.assertEqual(d['building_fact']['counts']['spatial_candidates_from_reference_cadastre'], 1)
        self.assertEqual(d['building_fact']['records'][0]['match_basis'], 'REFERENCE_PARCEL_OVERLAY_PROVISIONAL')
        self.assertEqual(d['items'][2]['features'][0]['properties']['name'], '3층')

    def test_provider_pnu_provisional_candidate_without_cadastral_polygon(self):
        d = data()
        s = snapshot([])
        geom = feature(box(1, 1, 3, 3))
        geom['properties']['pnu'] = '1111111111100000001'
        s['_report_layers']['LT_C_BLDGINFO'] = {'status': 'COMPLETE', 'features': [geom]}
        attach_building_themes(d, s)
        self.assertEqual(d['building_fact']['counts']['spatial_candidates_from_provider_pnu'], 1)
        self.assertEqual(d['building_fact']['records'][0]['join_status'], 'PARCEL_SPATIAL_REFERENCE')
        self.assertEqual(d['items'][2]['features'][0]['properties']['name'], '3층')

    def test_unkeyed_ledger_is_available_for_unique_provisional_parcel(self):
        d = data()
        d['buildings'][0]['id'] = None
        s = snapshot([])
        s['_report_layers']['LT_C_BLDGINFO'] = {'status': 'COMPLETE',
            'features': [feature(box(1, 1, 3, 3))]}
        s['_report_layers']['LP_PA_CBND_BUBUN'] = {'status': 'COMPLETE',
            'features': [self._parcel((0, 0, 4, 4))]}
        attach_building_themes(d, s)
        self.assertEqual(d['building_fact']['counts']['unkeyed_register_rows'], 1)
        self.assertEqual(d['building_fact']['counts']['spatial_register_candidates'], 1)
        self.assertEqual(d['building_fact']['counts']['direct_register_matches'], 0)
        self.assertEqual(d['items'][2]['features'][0]['properties']['name'], '3층')

    def test_pnu_register_parcel_theme_without_any_building_footprint(self):
        d = data()
        s = snapshot([], 'UNKNOWN')
        s['_report_layers']['LP_PA_CBND_BUBUN'] = {
            'status': 'COMPLETE', 'features': [self._parcel((0, 0, 4, 4))]}
        attach_building_themes(d, s)
        self.assertEqual(d['building_fact']['counts']['mapped_shapes'], 0)
        self.assertEqual(d['building_fact']['counts']['register_with_cadastral_pnu'], 1)
        for item in d['items']:
            self.assertEqual(item['thematic_type'], 'building')
            self.assertEqual(len(item['features']), 1)
            self.assertEqual(item['features'][0]['properties']['geometry_basis'], 'CADASTRAL_PNU')
            self.assertEqual(item['features'][0]['properties']['join_status'], 'PNU_PARCEL_REGISTER')
            self.assertEqual(item['survey_table']['rows'][0]['count'], 1)
            self.assertIn('건물도형 0개', item['survey_table']['rows'][0]['note'])
        self.assertEqual(d['items'][2]['features'][0]['properties']['name'], '3층')
        self.assertEqual(d['statistics']['3-3']['footprint_count'], 0)
        self.assertEqual(d['statistics']['3-3']['pnu_register_parcel_features'], 1)

    def test_pnu_register_parcel_not_double_counted_when_footprint_already_joined(self):
        d = data()
        s = snapshot([feature(box(1, 1, 3, 3), 'MG001')])
        s['_report_layers']['LP_PA_CBND_BUBUN'] = {
            'status': 'COMPLETE', 'features': [self._parcel((0, 0, 4, 4))]}
        attach_building_themes(d, s)
        self.assertEqual(d['building_fact']['counts']['register_with_cadastral_pnu'], 0)
        self.assertEqual(len(d['items'][2]['features']), 1)
        self.assertEqual(d['items'][2]['features'][0]['properties']['join_status'], 'EXACT_ID')

    def test_pnu_register_multiple_titles_on_one_parcel_not_misattributed(self):
        d = data()
        other = dict(d['buildings'][0], id='MG002', floors=5)
        d['buildings'].append(other)
        s = snapshot([], 'UNKNOWN')
        s['_report_layers']['LP_PA_CBND_BUBUN'] = {
            'status': 'COMPLETE', 'features': [self._parcel((0, 0, 4, 4))]}
        attach_building_themes(d, s)
        self.assertEqual(d['building_fact']['counts']['register_with_cadastral_pnu'], 0)
        self.assertEqual(len(d['items'][2]['features']), 0)

    def test_pnu_register_multi_parcel_title_not_repeated(self):
        d = data()
        d['buildings'][0]['pnus'] += ', 1111111111100000002'
        s = snapshot([], 'UNKNOWN')
        s['_report_layers']['LP_PA_CBND_BUBUN'] = {
            'status': 'COMPLETE', 'features': [
                self._parcel((0, 0, 4, 4)),
                self._parcel((5, 5, 9, 9), '1111111111100000002')]}
        attach_building_themes(d, s)
        self.assertEqual(d['building_fact']['counts']['register_with_cadastral_pnu'], 0)
        self.assertEqual(d['items'][2]['features'], [])

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
