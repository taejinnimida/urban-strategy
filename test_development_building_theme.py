"""Regression tests: building ledger statistics are independent from GIS footprint maps."""
import unittest
from shapely.geometry import box, mapping, shape
from development_building_theme import attach_building_themes, _classify

PNU = '1111111111100000001'

def feature(geom, mgm=None, props=None):
    p = dict(props or {})
    if mgm is not None:
        p['mgmBldrgstPk'] = mgm
    return {'type': 'Feature', 'geometry': mapping(geom), 'properties': p}

def data():
    return {
        'as_of': '2026-11-12',
        'buildings': [
            {'id':'MG001','pnus':PNU,'use':'단독주택','structure':'벽돌구조',
             'floors':3,'age':25,'floor_area':150,'coverage':60,'far':170}
        ],
        'items':[{'id':f'3-{i}','name':str(i),'detail':''} for i in range(1,8)],
        'statistics':{f'3-{i}':{} for i in range(1,8)}
    }

def snapshot(features, status='COMPLETE', cadastral=None, address=None):
    layers={'LT_C_BLDGINFO':{'status':status,'features':features}}
    if cadastral is not None:
        layers['LP_PA_CBND_BUBUN']={'status':'COMPLETE','features':cadastral}
    if address is not None:
        layers['LT_C_SPBD']={'status':'COMPLETE','features':address}
    return {'geometry':mapping(box(0,0,10,10)),'_report_layers':layers}

def parcel(geom):
    return feature(geom, props={'pnu':PNU})

class BuildingSurveyTest(unittest.TestCase):
    def test_classification_boundaries(self):
        self.assertEqual(_classify('3-3',5),'5층 이상')
        self.assertEqual(_classify('3-4',30),'30~40년')
        self.assertEqual(_classify('3-5',85),'85~115㎡')
        self.assertEqual(_classify('3-6',10),'20% 미만')
        self.assertEqual(_classify('3-7',300),'300% 이상')
        self.assertEqual(_classify('3-5',0),'기타(값 없음 등)')

    def assert_only_gis_footprints(self, d):
        for item in d['items']:
            for feat in item['features']:
                p=feat['properties']
                self.assertEqual(p['geometry_basis'],'BUILDING_FOOTPRINT')
                self.assertEqual(p['geometry_source'],'LT_C_BLDGINFO')
                self.assertNotEqual(p['join_status'],'PNU_PARCEL_REGISTER')

    def test_exact_id_uses_ledger_attribute_on_true_footprint(self):
        d=data()
        s=snapshot([feature(box(1,1,3,3),'MG001',
                            {'mainPurpsCdNm':'근린생활시설'})])
        attach_building_themes(d,s)
        self.assertEqual(d['items'][0]['survey_table']['rows'][0]['count'],1)
        self.assertEqual(len(d['items'][0]['features']),1)
        p=d['items'][0]['features'][0]['properties']
        self.assertEqual(p['join_status'],'EXACT_ID')
        self.assertEqual(p['name'],'단독주택')
        self.assertEqual(d['building_fact']['records'][0]['quality_grade'],'E_CONFLICT')
        self.assertTrue(d['items'][0]['cad_exportable'])
        self.assert_only_gis_footprints(d)

    def test_gis_attribute_when_no_ledger_join(self):
        d=data()
        s=snapshot([feature(box(1,1,3,3),props={'mainPurpsCdNm':'근린생활시설','grndFlrCnt':5})])
        attach_building_themes(d,s)
        self.assertEqual(d['items'][0]['survey_table']['rows'][0]['count'],1)
        self.assertEqual(d['items'][0]['features'][0]['properties']['name'],'근린생활시설')
        self.assertEqual(d['items'][0]['features'][0]['properties']['join_status'],'GIS_ONLY')
        self.assertEqual(d['items'][2]['features'][0]['properties']['name'],'5층 이상')
        self.assertEqual(d['building_fact']['counts']['direct_register_matches'],0)
        self.assert_only_gis_footprints(d)

    def test_gis_unknown_attributes_show_grey_not_parcel(self):
        d=data()
        s=snapshot([feature(box(1,1,3,3))])
        attach_building_themes(d,s)
        p=d['items'][0]['features'][0]['properties']
        self.assertEqual(p['join_status'],'GIS_ONLY')
        self.assertEqual(p['name'],'대장·도형 미연계')
        self.assertEqual(p['class_code'],'C99')
        self.assertEqual(d['items'][0]['survey_table']['rows'][0]['count'],1)
        self.assert_only_gis_footprints(d)

    def test_many_gis_polygons_not_ledger_building_count(self):
        d=data()
        features=[feature(box(1+i*.03,1,4+i*.03,4),props={'grndFlrCnt':2})
                  for i in range(20)]
        attach_building_themes(d,snapshot(features))
        self.assertEqual(d['building_fact']['counts']['mapped_shapes'],20)
        for item in d['items']:
            self.assertEqual(item['survey_table']['rows'][0]['count'],1)
            self.assertEqual(len(item['features']),20)
        self.assertEqual(d['statistics']['3-3']['displayed_gis_footprints'],20)
        self.assertIn('멸실 여부',d['building_fact']['display_exclusion']['note'])

    def test_parcel_never_used_even_if_it_exactly_matches_ledger_pnu(self):
        d=data()
        attach_building_themes(d,snapshot([],cadastral=[parcel(box(0,0,4,4))]))
        self.assertEqual(d['building_fact']['counts']['register_with_cadastral_pnu'],1)
        for item in d['items']:
            self.assertEqual(item['features'],[])
            self.assertEqual(item['survey_table']['rows'][0]['count'],1)
            self.assertFalse(item['cad_exportable'])
        self.assertEqual(d['statistics']['3-5']['cadastral_parcels_excluded_from_theme'],1)

    def test_65_percent_primary_parcel_with_competing_overlap_is_provisional(self):
        d=data()
        # Building spans two cadastral parcels: 70% in the ledger parcel,
        # 30% in another parcel. Competing overlap must not veto the match.
        building=feature(box(0,0,10,1),props={'mainPurpsCdNm':'근린생활시설'})
        target=parcel(box(0,0,7,1))
        other=feature(box(7,0,10,1),props={'pnu':'1111111111100000002'})
        attach_building_themes(d,snapshot([building],cadastral=[target,other]))
        p=d['items'][0]['features'][0]['properties']
        self.assertEqual(p['join_status'],'PARCEL_SPATIAL_REFERENCE')
        self.assertEqual(p['name'],'단독주택')
        self.assertEqual(d['building_fact']['records'][0]['spatial_overlap_pct'],70.0)
        self.assertEqual(d['items'][0]['survey_table']['rows'][0]['count'],1)
        self.assertEqual(shape(d['items'][0]['features'][0]['geometry']).area,10)
        self.assert_only_gis_footprints(d)

    def test_65_percent_boundary_inclusive_and_just_below_excluded(self):
        target=parcel(box(0,0,6.5,1))
        other=feature(box(6.5,0,10,1),props={'pnu':'1111111111100000002'})
        building=feature(box(0,0,10,1))
        d=data()
        attach_building_themes(d,snapshot([building],cadastral=[target,other]))
        self.assertEqual(d['items'][0]['features'][0]['properties']['join_status'],'PARCEL_SPATIAL_REFERENCE')
        below=parcel(box(0,0,6.49,1))
        d=data()
        attach_building_themes(d,snapshot([building],cadastral=[below,other]))
        self.assertEqual(d['items'][0]['features'][0]['properties']['join_status'],'GIS_ONLY')

    def test_parcel_not_substituted_when_gis_pnu_provisional(self):
        d=data()
        geom=feature(box(1,1,3,3),props={'pnu':PNU})
        attach_building_themes(d,snapshot([geom],cadastral=[parcel(box(0,0,4,4))]))
        p=d['items'][0]['features'][0]['properties']
        self.assertEqual(p['join_status'],'PARCEL_SPATIAL_REFERENCE')
        self.assertEqual(p['name'],'단독주택')
        self.assertEqual(p['attribute_assurance'],'PROVISIONAL_PNU')
        self.assertEqual(shape(d['items'][0]['features'][0]['geometry']).area,4)
        self.assert_only_gis_footprints(d)

    def test_source_only_when_mismatched_gis_and_ledger(self):
        d=data()
        attach_building_themes(d,snapshot([
            feature(box(1,1,3,3),'OTHER',{'mainPurpsCdNm':'제2종근린생활시설'}),
        ]))
        p=d['items'][0]['features'][0]['properties']
        self.assertEqual(p['join_status'],'GIS_ONLY')
        self.assertEqual(p['name'],'제2종근린생활시설')
        self.assertEqual(d['items'][0]['survey_table']['rows'][1]['count'],1)
        self.assertNotEqual(p['register_id'],'MG001')

    def test_road_address_shapes_are_ignored_entirely(self):
        d=data()
        attach_building_themes(d,snapshot(
            [feature(box(1,1,3,3),'MG001')],
            address=[feature(box(5,5,8,8),'MG002')]))
        self.assertEqual(len(d['items'][0]['features']),1)
        self.assertEqual(d['building_fact']['counts']['address_shapes'],0)
        self.assertNotIn('LT_C_SPBD',d['building_fact']['source_info'])

    def test_absent_gis_does_not_erase_ledger_statistics(self):
        d=data()
        attach_building_themes(d,snapshot([],status='UNKNOWN'))
        for item in d['items']:
            self.assertEqual(item['survey_table']['rows'][0]['count'],1)
            self.assertFalse(item['cad_exportable'])
            self.assertEqual(item['features'],[])
        self.assertFalse(d['building_fact']['provider_verified'])

    def test_gis_complete_but_no_footprints_not_a_complete_map(self):
        d=data()
        attach_building_themes(d,snapshot([],status='COMPLETE'))
        self.assertEqual(d['items'][0]['geometry_status'],'UNAVAILABLE')
        self.assertEqual(d['items'][0]['survey_table']['rows'][0]['count'],1)

    def test_22_register_rows_and_43_unjoined_gis_footprints_still_draw(self):
        d=data()
        d['buildings']=[{**d['buildings'][0],'id':f'MG{i:03d}'}
                        for i in range(22)]
        polygons=[feature(box(0.2+(i%9)*0.9,0.2+(i//9)*0.9,
                              0.65+(i%9)*0.9,0.65+(i//9)*0.9),
                          props={'mainPurpsCdNm':'단독주택', 'grndFlrCnt':2})
                  for i in range(43)]
        snap=snapshot(polygons,cadastral=[parcel(box(0,0,10,10))])
        attach_building_themes(d,snap)
        for item in d['items']:
            self.assertEqual(item['survey_table']['rows'][0]['count'],22)
            self.assertEqual(len(item['features']),43)
            self.assertEqual(sum(v['count'] for v in item['legend']),43)
            self.assertTrue(item['cad_exportable'])
            self.assertTrue(all(f['properties']['geometry_source']=='LT_C_BLDGINFO'
                                for f in item['features']))
            self.assertTrue(all(f['properties']['geometry_basis']=='BUILDING_FOOTPRINT'
                                for f in item['features']))
        self.assertEqual(d['statistics']['3-1']['survey_total'],22)
        self.assertEqual(d['statistics']['3-1']['mapped_total'],43)
        self.assertEqual(d['statistics']['3-1']['verified_ledger_joins'],0)

    def test_register_count_and_gis_count_are_separate(self):
        d=data()
        d['buildings'].append({**d['buildings'][0],'id':'MG002','use':'공동주택'})
        s=snapshot([feature(box(1,1,3,3),'MG001'),
                    feature(box(4,4,6,6),props={'mainPurpsCdNm':'단독주택'}),
                    feature(box(6,6,8,8),props={'mainPurpsCdNm':'근린생활시설'})])
        attach_building_themes(d,s)
        item=d['items'][0]
        self.assertEqual(item['survey_table']['rows'][0]['count'],2)
        self.assertEqual(len(item['features']),3)
        self.assertEqual(sum(x['count'] for x in item['legend']),3)
        self.assertEqual(d['statistics']['3-1']['survey_total'],2)
        self.assertEqual(d['statistics']['3-1']['mapped_total'],3)
        self.assert_only_gis_footprints(d)

if __name__=='__main__':
    unittest.main()
