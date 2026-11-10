"""Offline EcoBank WFS regression tests; no API keys or provider requests."""
import unittest
from unittest.mock import patch
from xml.sax.saxutils import escape
from pyproj import Transformer
from shapely.geometry import mapping, box
from shapely.ops import transform
from development.nature.ecobank import collect_ecobank, EcoBankError, _parse, WFS_URL, _grade

BACK=Transformer.from_crs('EPSG:5186','EPSG:4326',always_xy=True).transform

def site():
    return mapping(transform(BACK,box(200000,500000,200100,500100)))

def gml_record(x0=200000,y0=500000,x1=200100,y1=500100,grade='1',fid='eco.1'):
    ring=f'{x0},{y0} {x1},{y0} {x1},{y1} {x0},{y1} {x0},{y0}'
    return ('<gml:featureMember><eco:tbl_opn_eczm fid="'+escape(fid)+'">'
            '<eco:eczmGr>'+escape(grade)+'</eco:eczmGr>'
            '<eco:geom><gml:Polygon srsName="EPSG:5186"><gml:outerBoundaryIs>'
            '<gml:LinearRing><gml:coordinates>'+ring+'</gml:coordinates>'
            '</gml:LinearRing></gml:outerBoundaryIs></gml:Polygon></eco:geom>'
            '</eco:tbl_opn_eczm></gml:featureMember>')

def xml(*records):
    return ('<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs" '
            'xmlns:gml="http://www.opengis.net/gml" xmlns:eco="http://sample.ecobank.test">'
            +''.join(records)+'</wfs:FeatureCollection>').encode()

class Response:
    status_code=200
    def __init__(self, payload, status=200): self.payload=payload;self.status_code=status
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def iter_content(self,chunk_size=65536):yield self.payload

class EcoBankTest(unittest.TestCase):
    def test_gml_grade_union_area(self):
        payload=xml(gml_record(200000,500000,200040,500100,'1','a'),
                    gml_record(200040,500000,200100,500100,'2','b'))
        captured=[]
        def get(url,**kwargs):
            self.assertEqual(url,WFS_URL)
            self.assertEqual(kwargs['params']['typeName'],'tbl_opn_eczm')
            self.assertEqual(kwargs['params']['maxFeatures'],500)
            self.assertIn('EPSG:5186',kwargs['params']['bbox'])
            self.assertEqual(kwargs['params']['serviceKey'],'secret-without-being-logged')
            captured.append(kwargs)
            return Response(payload)
        result=collect_ecobank(site(),api_key='secret-without-being-logged',get=get)
        self.assertEqual(result['status'],'DATA')
        self.assertEqual([x['name'] for x in result['legend']],['1등급','2등급'])
        self.assertAlmostEqual(result['overlap_area_m2'],10000,places=2)
        self.assertEqual(result['provider_calls'],1)
        self.assertNotIn('secret-without-being-logged',str(result))
        self.assertEqual(len(result['features']),2)
    def test_missing_grade_is_not_grade_three(self):
        result=collect_ecobank(site(),api_key='k',get=lambda *args,**kwargs:Response(xml(gml_record(grade='4'))))
        self.assertEqual(result['status'],'PARTIAL')
        self.assertEqual(result['legend'][0]['name'],'등급 미확보')
        self.assertEqual(result['unclassified_count'],1)
    def test_auth_error_fail_closed(self):
        with self.assertRaises(EcoBankError) as ctx:
            collect_ecobank(site(),api_key='k',get=lambda *args,**kwargs:Response(b'',status=403))
        self.assertEqual(ctx.exception.code,'PROVIDER_REJECTED')
    def test_full_page_must_be_subdivided(self):
        full=xml(*[gml_record(fid=f'{j}') for j in range(500)])
        one=xml(gml_record())
        calls=[]
        def get(*args,**kwargs):
            calls.append(1)
            return Response(full if len(calls)==1 else one)
        result=collect_ecobank(site(),api_key='k',get=get)
        self.assertEqual(result['provider_calls'],5)
        self.assertEqual(len(result['features']),1)
        self.assertEqual(result['status'],'DATA')
    def test_invalid_gml_does_not_become_no_data(self):
        with self.assertRaises(EcoBankError):
            _parse(b'<ServiceExceptionReport><ServiceException>wrong</ServiceException></ServiceExceptionReport>')
        with self.assertRaises(EcoBankError):
            _parse(b'<!DOCTYPE x [<!ENTITY x SYSTEM "file:///etc/passwd">]><x/>')
    def test_empty_is_not_no_legal_constraint(self):
        result=collect_ecobank(site(),api_key='k',get=lambda *args,**kwargs:Response(xml()))
        self.assertEqual(result['status'],'EMPTY')
        self.assertEqual(result['features'],[])
        self.assertIn('비해당 확정 불가',result['detail'])
    def test_approved_key_presence_is_not_online_success(self):
        self.assertEqual(collect_ecobank(site(),api_key='',get=lambda *a,**k:None)['status'],'MISSING_CREDENTIAL')

if __name__=='__main__':unittest.main()