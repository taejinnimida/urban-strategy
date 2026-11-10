"""Non-network tests: independent FACT adapter without Seoul or national RULE imports."""
import ast
from pathlib import Path
import unittest
from unittest.mock import patch
from dataclasses import FrozenInstanceError
from facts.contract import FactEnvelope, fact_revision, unavailable
from facts.vworld import FactTransportError, VWorldFacts


class Response:
    def __init__(self, data=b'{"response":{"status":"OK","result":{"items":[]}}}', status=200):
        self.data = data
        self.status_code = status
    def __enter__(self):return self
    def __exit__(self,*exc):return False
    def iter_content(self,size):yield self.data


class FactBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.adapter=VWorldFacts(key_provider=lambda:'PRIVATE_KEY',domain_provider=lambda:'example.org')

    def test_common_facts_cannot_import_seoul_or_nationwide(self):
        fact_root=Path(__file__).resolve().parents[1] / 'facts'
        forbidden=('app','dev_app','market_renewal','seoul_facilities','development','ui_editor_backend')
        for source in fact_root.glob('*.py'):
            parsed=ast.parse(source.read_text(encoding='utf-8'))
            imports=[]
            for node in ast.walk(parsed):
                if isinstance(node,ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node,ast.ImportFrom) and node.module:
                    imports.append(node.module)
            for dependency in imports:
                self.assertFalse(any(dependency==f or dependency.startswith(f+'.') for f in forbidden),
                                 f'{source.name}: illegal dependency {dependency}')

    def test_immutability_and_detached_copy(self):
        x=FactEnvelope(kind='land',status='COMPLETE',source='test',
                       as_of='2026-11-10',geometry_crs='EPSG:4326',
                       coverage='VERIFIED',facts={'values':[1]})
        with self.assertRaises(FrozenInstanceError):
            x.status='ERROR'
        detached=x.serialize()
        detached['facts']['values'][0]=999
        self.assertEqual(x.facts['values'][0],1)

    def test_missing_never_zero(self):
        x=unavailable('building').serialize()
        self.assertEqual(x['status'],'UNAVAILABLE')
        self.assertIsNone(x['facts'])

    def test_fact_revision_stable_and_source_sensitive(self):
        a={'type':'Polygon','coordinates':[[[127,37],[128,37],[127,38],[127,37]]]}
        self.assertEqual(fact_revision(a,'building','src1'),fact_revision(a,'building','src1'))
        self.assertNotEqual(fact_revision(a,'building','src1'),fact_revision(a,'building','src2'))

    def test_no_key_leak_through_errors(self):
        with patch('facts.vworld.requests.get',return_value=Response(status=403)):
            with self.assertRaises(FactTransportError) as e:
                self.adapter.search_parcel_address('서울 종로구')
        self.assertNotIn('PRIVATE_KEY',str(e.exception))
        self.assertEqual(str(e.exception),'PROVIDER_REJECTED')

    def test_no_arbitrary_network_endpoints(self):
        with self.assertRaises(FactTransportError) as e:
            self.adapter.fetch('http://api.vworld.kr/req/data',{})
        self.assertEqual(str(e.exception),'INVALID_ENDPOINT')
        with self.assertRaises(FactTransportError):
            self.adapter.fetch('https://example.com/req/data',{})

    def test_pnu_guard_before_network(self):
        with self.assertRaises(FactTransportError):
            self.adapter.parcel_by_pnu('1234')

    def test_independent_region_boundaries(self):
        data='{\"type\":\"FeatureCollection\",\"features\":[{\"type\":\"Feature\",\"properties\":{\"ctp_kor_nm\":\"서울특별시\"},\"geometry\":{\"type\":\"Polygon\",\"coordinates\":[]}}]}'.encode('utf-8')
        with patch('facts.vworld.requests.get',return_value=Response(data=data)) as request:
            result=self.adapter.regions((126,37,128,38))
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['properties']['ctp_kor_nm'],'서울특별시')
        self.assertEqual(request.call_args.kwargs['params']['typename'],'lt_c_adsido_info')

    def test_source_request_has_server_credentials_and_no_redirects(self):
        with patch('facts.vworld.requests.get',return_value=Response()) as request:
            result=self.adapter.search_parcel_address('서울 종로구')
        self.assertEqual(result['response']['status'],'OK')
        args,kwargs=request.call_args
        self.assertTrue(args[0].startswith('https://api.vworld.kr/'))
        self.assertEqual(kwargs['params']['key'],'PRIVATE_KEY')
        self.assertEqual(kwargs['params']['domain'],'example.org')
        self.assertFalse(kwargs['allow_redirects'])
        self.assertTrue(kwargs['stream'])

    def test_reject_html_200(self):
        with patch('facts.vworld.requests.get',return_value=Response(data=b'<html>bad</html>')):
            with self.assertRaises(FactTransportError) as e:
                self.adapter.search_parcel_address('서울 종로구')
        self.assertEqual(str(e.exception),'PROVIDER_FORMAT')


if __name__=='__main__':
    unittest.main()
