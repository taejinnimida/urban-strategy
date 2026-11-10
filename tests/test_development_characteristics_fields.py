"""Offline regression for VWorld characteristics XML field spelling (2-5)."""
import unittest
from development_sources import characteristics

PNU='1111010100100010001'

class FakeResponse:
    def __init__(self, body):
        self.content=body.encode('utf-8')
        self.closed=False
    def raise_for_status(self):
        pass
    def close(self):
        self.closed=True

class Service:
    VWORLD_LAND_URL='https://api.vworld.kr/ned/data/getLandCharacteristics'
    def __init__(self, xml):
        self.response=FakeResponse(xml)
        self.calls=0
    def _vworld_key(self):
        return 'test-key'
    def _vworld_domain(self):
        return 'example.org'
    def _vworld_get(self, url, params, timeout):
        self.calls+=1
        assert url==self.VWORLD_LAND_URL
        assert params['pnu']==PNU
        assert params['format']=='xml'
        return self.response,'mock'

class VWorldCharacteristicTest(unittest.TestCase):
    def test_tpgrph_field_spelling_and_records(self):
        xml=f'<response><resultCode>0</resultCode><fields><field><pnu>{PNU}</pnu><stdrYear>2026</stdrYear><tpgrphFrmCodeNm>사다리형</tpgrphFrmCodeNm><tpgrphHgCodeNm>평지</tpgrphHgCodeNm></field></fields></response>'
        service=Service(xml)
        record=characteristics(service,PNU)
        self.assertEqual(record['status'],'ATTRIBUTE_ONLY')
        self.assertEqual(record['record']['tpgrphFrmCodeNm'],'사다리형')
        self.assertEqual(record['record']['tpgrphHgCodeNm'],'평지')
        self.assertNotIn('topgrphFrmCodeNm',record['record'])
        self.assertEqual(service.calls,1)
        self.assertTrue(service.response.closed)

    def test_no_record_never_assumed_flat_or_rectangular(self):
        result=characteristics(Service('<response><resultCode>0</resultCode><fields/></response>'),PNU)
        self.assertEqual(result['status'],'NO_RECORD')
        self.assertIsNone(result['record'])

    def test_response_pnu_mismatch_rejected(self):
        xml='<response><field><pnu>12345</pnu><tpgrphFrmCodeNm>정방형</tpgrphFrmCodeNm></field></response>'
        with self.assertRaisesRegex(ValueError,'PNU mismatch'):
            characteristics(Service(xml),PNU)

if __name__=='__main__':
    unittest.main()
