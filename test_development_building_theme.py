"""Building survey invariants: building register authority, PNU grouping,
multiple titles/footprints, and no parcel-shape substitution."""
import unittest
from shapely.geometry import box, mapping, shape
from development_building_theme import attach_building_themes, _classify

PNU='1111111111100000001'
PNU2='1111111111100000002'

def feature(geom,mgm=None,props=None):
    properties=dict(props or {})
    if mgm is not None:properties['mgmBldrgstPk']=mgm
    return {'type':'Feature','geometry':mapping(geom),'properties':properties}

def ledger(key='MG001',pnu=PNU,**changes):
    result={'id':key,'pnus':pnu,'use':'단독주택','structure':'벽돌구조',
            'floors':3,'age':25,'floor_area':150,'footprint':80,
            'coverage':60,'far':170}
    result.update(changes)
    return result

def data(rows=None):
    return {'as_of':'2026-11-12','buildings':rows if rows is not None else [ledger()],
            'items':[{'id':f'3-{i}','name':str(i),'detail':''} for i in range(1,8)],
            'statistics':{f'3-{i}':{} for i in range(1,8)}}

def parcel(geom,pnu=PNU):
    return feature(geom,props={'pnu':pnu})

def snapshot(features,status='COMPLETE',cadastral=None,address=None):
    layers={'LT_C_BLDGINFO':{'status':status,'features':features}}
    if cadastral is not None:layers['LP_PA_CBND_BUBUN']={'status':'COMPLETE','features':cadastral}
    if address is not None:layers['LT_C_SPBD']={'status':'COMPLETE','features':address}
    return {'geometry':mapping(box(0,0,10,10)),'_report_layers':layers}

class BuildingSurveyTest(unittest.TestCase):
    def assert_footprints_only(self,d):
        for item in d['items']:
            for f in item['features']:
                p=f['properties']
                self.assertEqual(p['geometry_basis'],'BUILDING_FOOTPRINT')
                self.assertEqual(p['geometry_source'],'LT_C_BLDGINFO')

    def test_bins(self):
        self.assertEqual(_classify('3-3',5),'5층 이상')
        self.assertEqual(_classify('3-4',30),'30~40년')
        self.assertEqual(_classify('3-5',85),'85~115㎡')
        self.assertEqual(_classify('3-6',10),'20% 미만')

    def test_single_title_one_building_direct(self):
        d=data()
        attach_building_themes(d,snapshot([feature(box(1,1,3,3),'MG001')]))
        p=d['items'][0]['features'][0]['properties']
        self.assertEqual(p['join_status'],'EXACT_ID')
        self.assertEqual(p['building_role'],'MAIN')
        self.assertEqual(p['name'],'단독주택')
        self.assertEqual(d['items'][0]['survey_table']['rows'][0]['count'],1)
        self.assertTrue(d['items'][0]['cad_exportable'])
        self.assert_footprints_only(d)

    def test_one_title_multiple_buildings_largest_main_others_annexes(self):
        d=data()
        gis=[feature(box(1,1,4,4),props={'pnu':PNU}),
             feature(box(5,1,6,2),props={'pnu':PNU}),
             feature(box(5,3,6,4),props={'pnu':PNU,'A26':2,'A11':'철근콘크리트구조'})]
        attach_building_themes(d,snapshot(gis))
        rows=d['items'][2]['features']
        self.assertEqual(len(rows),3)
        self.assertEqual(sum(f['properties']['building_role']=='MAIN' for f in rows),1)
        self.assertEqual(sum(f['properties']['building_role']=='AUXILIARY' for f in rows),2)
        self.assertEqual([f for f in rows if f['properties']['building_role']=='MAIN'][0]['properties']['name'],'3층')
        self.assertEqual(sum(f['properties']['name']=='2층' for f in rows),1)
        self.assertEqual(d['items'][2]['survey_table']['rows'][0]['count'],1)
        self.assert_footprints_only(d)

    def test_many_titles_one_pnu_preserved_with_individual_gis_attributes(self):
        d=data([ledger('MG001',floors=2,use='단독주택'),
                ledger('MG002',floors=5,use='공동주택')])
        polys=[feature(box(1,1,4,4),props={'pnu':PNU,'A9':'단독주택','A26':2}),
               feature(box(5,1,8,4),props={'pnu':PNU,'A9':'공동주택','A26':5})]
        attach_building_themes(d,snapshot(polys))
        feats=d['items'][0]['features']
        self.assertEqual(len(feats),2)
        self.assertEqual({v['properties']['name'] for v in feats},{'단독주택','공동주택'})
        self.assertEqual({v['properties']['register_id'] for v in feats},{'MG001','MG002'})
        self.assertEqual(d['items'][0]['survey_table']['rows'][0]['count'],2)
        self.assert_footprints_only(d)

    def test_many_titles_ambiguous_keeps_group_not_fake_individual_id(self):
        d=data([ledger('MG001',use='단독주택'),ledger('MG002',use='공동주택')])
        gis=[feature(box(1,1,3,3),props={'pnu':PNU}),
             feature(box(4,4,6,6),props={'pnu':PNU})]
        attach_building_themes(d,snapshot(gis))
        values=d['items'][0]['features']
        self.assertEqual(len(values),2)
        self.assertTrue(all(f['properties']['join_status']=='PNU_GROUP_REFERENCE' for f in values))
        self.assertTrue(all(f['properties']['register_id'] is None for f in values))
        self.assertEqual(d['items'][0]['survey_table']['rows'][0]['count'],2)

    def test_multiple_titles_one_footprint_keeps_pnu_group(self):
        d=data([ledger('MG001'),ledger('MG002',use='공동주택')])
        attach_building_themes(d,snapshot([feature(box(1,1,3,3),props={'pnu':PNU})]))
        features=d['items'][0]['features']
        self.assertEqual(len(features),1)
        self.assertEqual(features[0]['properties']['join_status'],'PNU_GROUP_REFERENCE')
        self.assertEqual(d['items'][0]['survey_table']['rows'][0]['count'],2)

    def test_unrelated_gis_shapes_removed_not_counted(self):
        d=data()
        shapes=[feature(box(1,1,3,3),props={'pnu':PNU}),
                feature(box(7,7,9,9),props={'pnu':PNU2,'A9':'근린생활시설'})]
        attach_building_themes(d,snapshot(shapes))
        self.assertEqual(len(d['items'][0]['features']),1)
        self.assertEqual(d['building_fact']['counts']['source_footprints_excluded_from_survey'],1)
        self.assertEqual(d['items'][0]['survey_table']['rows'][0]['count'],1)

    def test_provider_pnu_vs_overlay_conflict_not_rejected(self):
        d=data()
        building=feature(box(0,0,10,1),props={'A2':PNU2,'A9':'단독주택'})
        target=parcel(box(0,0,7,1),PNU)
        other=parcel(box(7,0,10,1),PNU2)
        attach_building_themes(d,snapshot([building],cadastral=[target,other]))
        rec=d['building_fact']['records'][0]
        self.assertTrue(rec['provider_pnu_conflict'])
        self.assertEqual(rec['spatial_parcel_pnu'],PNU)
        self.assertEqual(len(d['items'][0]['features']),1)

    def test_65_percent_inclusive_other_parcel_not_veto(self):
        d=data()
        attach_building_themes(d,snapshot([feature(box(0,0,10,1))],
            cadastral=[parcel(box(0,0,6.5,1)),parcel(box(6.5,0,10,1),PNU2)]))
        self.assertEqual(d['building_fact']['records'][0]['spatial_overlap_pct'],65)
        self.assertEqual(len(d['items'][0]['features']),1)

    def test_below_65_percent_no_pnu_is_not_assigned(self):
        d=data()
        attach_building_themes(d,snapshot([feature(box(0,0,10,1))],
            cadastral=[parcel(box(0,0,6.49,1)),parcel(box(6.49,0,10,1),PNU2)]))
        self.assertEqual(d['items'][0]['features'],[])
        self.assertEqual(d['items'][0]['survey_table']['rows'][0]['count'],1)

    def test_no_actual_building_footprint_does_not_shade_parcel(self):
        d=data()
        attach_building_themes(d,snapshot([],cadastral=[parcel(box(0,0,4,4))]))
        self.assertEqual(d['items'][0]['features'],[])
        self.assertFalse(d['items'][0]['cad_exportable'])
        self.assertEqual(d['items'][0]['survey_table']['rows'][0]['count'],1)

    def test_land_without_register_or_gis_is_vacant_if_complete(self):
        d=data()
        attach_building_themes(d,snapshot([],status='COMPLETE',
             cadastral=[parcel(box(0,0,3,3),PNU2),
                        parcel(box(4,4,6,6),PNU)]))
        ps=d['building_fact']['parcel_building_status']
        self.assertEqual(ps[PNU2],'VACANT_BY_VERIFIED_SOURCE')
        self.assertEqual(ps[PNU],'BUILDING_REGISTERED')
        self.assertEqual(d['building_fact']['counts']['source_verified_vacant_parcels'],1)
        self.assertEqual(d['building_fact']['counts']['parcels_with_register_no_footprint'],1)

    def test_failed_gis_or_unregistered_footprint_never_vacant(self):
        d=data()
        attach_building_themes(d,snapshot([],status='UNKNOWN',
             cadastral=[parcel(box(0,0,3,3),PNU2)]))
        self.assertEqual(d['building_fact']['parcel_building_status'][PNU2],'UNCONFIRMED')
        d=data()
        attach_building_themes(d,snapshot([feature(box(0,0,2,2),props={'A2':PNU2})],
             cadastral=[parcel(box(0,0,3,3),PNU2)]))
        self.assertEqual(d['building_fact']['parcel_building_status'][PNU2],
                         'GIS_FOOTPRINT_WITHOUT_REGISTER')
        self.assertEqual(d['items'][0]['features'],[])

    def test_failed_gis_is_not_vacant(self):
        d=data()
        attach_building_themes(d,snapshot([],status='UNKNOWN'))
        self.assertEqual(d['items'][0]['features'],[])
        self.assertEqual(d['items'][0]['survey_table']['rows'][0]['count'],1)

    def test_road_address_source_not_used(self):
        d=data()
        attach_building_themes(d,snapshot([feature(box(1,1,3,3),props={'pnu':PNU})],
            address=[feature(box(4,4,8,8),'MG002')]))
        self.assertEqual(len(d['items'][0]['features']),1)
        self.assertEqual(d['building_fact']['counts']['address_shapes'],0)

    def test_gis_a2_without_parcel_polygon_joins_pnu(self):
        d=data()
        attach_building_themes(d,snapshot([feature(box(1,1,3,3),props={'A2':PNU,'A9':'단독주택'})]))
        self.assertEqual(d['building_fact']['records'][0]['provider_pnu'],PNU)
        self.assertEqual(d['building_fact']['counts']['gis_source_a2_pnu_available'],1)
        self.assertEqual(len(d['items'][0]['features']),1)

if __name__=='__main__':unittest.main()
