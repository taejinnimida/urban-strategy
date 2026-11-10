"""Building GIS WFS -> current survey categories -> SVG/DXF integration.

Provider calls are mocked: proves code integration, not live GIS API data
coverage, credentials, GIS building standard fields, or CAD GUI display.
"""
import io
import unittest
from unittest.mock import patch
from shapely.geometry import box, mapping
from shapely.ops import transform
from pyproj import Transformer
from development.building.live import collect_gis_buildings, BuildingSourceError
from development.building.spatial import attach_building_spatial_themes
from development.cad import _exportable, dxf_bytes
from development.survey_sheet import make_sheet, svg_sheet
import ezdxf

TRANS=Transformer.from_crs(5179,4326,always_xy=True).transform
GEOM=mapping(transform(TRANS,box(953000,1950000,953100,1950120)))
BUILDING1=mapping(transform(TRANS,box(953010,1950020,953030,1950060)))
BUILDING2=mapping(transform(TRANS,box(953050,1950020,953080,1950060)))
AREA=Transformer.from_crs(4326,5179,always_xy=True).transform


class Provider:
    def __init__(self, results):
        self.results=list(results)
        self.calls=[]
    def configured(self):return True
    def fetch(self,url,params,expected='json'):
        self.calls.append((url,dict(params),expected))
        if not self.results:raise AssertionError('unexpected network call')
        value=self.results.pop(0)
        if isinstance(value,Exception):raise value
        return value


def feature(geom,i,use='공동주택',structure='철근콘크리트구조'):
    return {'type':'Feature','geometry':geom,
            'properties':{'A1':i,'A9':use,'A11':structure}}


class BuildingGISLiveTest(unittest.TestCase):
    def test_official_polygon_geometry_populates_two_cad_areas(self):
        provider=Provider([{'type':'FeatureCollection','features':[
            feature(BUILDING1,'GIS001','공동주택','철근콘크리트구조'),
            feature(BUILDING2,'GIS002','업무시설','일반철골구조')]}])
        fetched=collect_gis_buildings(GEOM,provider)
        self.assertEqual(fetched['status'],'DATA')
        self.assertEqual(len(fetched['features']),2)
        self.assertEqual(fetched['request_count'],1)
        self.assertEqual(provider.calls[0][1]['srsName'],'EPSG:4326')
        self.assertEqual(provider.calls[0][1]['typename'],'dt_d010')
        values=provider.calls[0][1]['bbox'].split(',')
        self.assertEqual(values[-1],'EPSG:4326')
        self.assertTrue(30<float(values[0])<45)
        self.assertTrue(120<float(values[1])<135)
        self.assertEqual(provider.calls[0][1]['maxFeatures'],'500')
        self.assertEqual(provider.calls[0][2],'json')
        snapshot={'geometry':GEOM,'_national_sources':{'building_gis':fetched},
                  '_cad_background':{'status':'COMPLETE','features':[{'type':'Feature','geometry':GEOM,'properties':{'jibun':'1-1'}}]}}
        data={'items':[{'id':'3-1','survey_table':{'title':'건축HUB 표제부 용도별 5건'}},
                       {'id':'3-2','survey_table':{'title':'건축HUB 표제부 구조별 5건'}}],
              'statistics':{}}
        attach_building_spatial_themes(data,snapshot,lambda g:transform(AREA,g).area)
        for item in data['items']:
            self.assertEqual(item['thematic_type'],'building_spatial')
            self.assertEqual(item['spatial_status'],'COMPLETE')
            self.assertEqual(item['survey_table']['total_count'],2)
            self.assertEqual(item['building_registry_table']['title'],
                             '건축HUB 표제부 '+('용도별' if item['id']=='3-1' else '구조별')+' 5건')
            self.assertEqual(item['registry_footprint_join_status'],'NOT_VERIFIED')
            self.assertTrue(_exportable(item,snapshot))
            self.assertIn(b'<svg',svg_sheet(make_sheet(item,snapshot)))
            raw=dxf_bytes(item,snapshot,{})
            doc=ezdxf.read(io.StringIO(raw.decode('utf-8')))
            self.assertIn('SURVEY_A3',doc.layouts)
            self.assertTrue(list(doc.modelspace().query('HATCH')))

    def test_missing_structure_does_not_invalidate_known_use(self):
        p=Provider([{'type':'FeatureCollection','features':[
            feature(BUILDING1,'GIS001','공동주택','')]}])
        fetched=collect_gis_buildings(GEOM,p)
        self.assertEqual(fetched['status'],'PARTIAL')
        snapshot={'geometry':GEOM,'_national_sources':{'building_gis':fetched}}
        data={'items':[{'id':'3-1'},{'id':'3-2'}],'statistics':{}}
        attach_building_spatial_themes(data,snapshot,lambda g:transform(AREA,g).area)
        self.assertEqual(data['items'][0]['spatial_status'],'COMPLETE')
        self.assertEqual(data['items'][1]['spatial_status'],'PARTIAL')
        self.assertTrue(_exportable(data['items'][0],snapshot))
        self.assertFalse(_exportable(data['items'][1],snapshot))

    def test_provider_failure_is_not_replaced_with_empty_building_site(self):
        provider=Provider([{'error':'AUTH_DENIED'}])
        with self.assertRaises(BuildingSourceError):
            collect_gis_buildings(GEOM,provider)

    def test_source_pagination_saturation_fails_closed(self):
        payload={'type':'FeatureCollection','features':[feature(BUILDING1,'G'+str(i))
                                                        for i in range(500)]}
        provider=Provider([payload]*90)
        with self.assertRaises(BuildingSourceError) as exc:
            collect_gis_buildings(GEOM,provider)
        self.assertEqual(exc.exception.code,'SOURCE_TRUNCATED')

    def test_duplicate_spatial_id_with_disparate_geometry_rejected(self):
        provider=Provider([{'type':'FeatureCollection','features':[
            feature(BUILDING1,'same'),feature(BUILDING2,'same')]}])
        with self.assertRaises(BuildingSourceError) as exc:
            collect_gis_buildings(GEOM,provider)
        self.assertEqual(exc.exception.code,'DUPLICATE_ID_CONFLICT')

    def test_missing_server_key_is_unknown_not_zero(self):
        class Unconfigured:
            def configured(self):return False
        state=collect_gis_buildings(GEOM,Unconfigured())
        self.assertEqual(state['status'],'MISSING_SOURCE')
        self.assertFalse(state['features'])

if __name__=='__main__':unittest.main()
