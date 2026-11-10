"""Synthetic official-source records: independent import, parsing and error semantics."""
import unittest
from unittest.mock import patch
from facts.vworld import FactTransportError
from facts.official_records import (
    OfficialRecordFacts, parse_ledger, parse_characteristics,
    parse_price, parse_building_page, pnu_to_building_params,
)

PNU='1111010100100010001'

def xml(text):
    return ('<?xml version="1.0" encoding="UTF-8"?><response>'
            +text+'</response>').encode('utf-8')


class OfficialRecordsTest(unittest.TestCase):
    def test_ledger_normalized_from_source_not_seoul(self):
        data=xml('<resultCode>00</resultCode><ladfrlVOList>'
                 f'<pnu>{PNU}</pnu><ldCodeNm>테스트동</ldCodeNm>'
                 '<lndcgrCodeNm>대</lndcgrCodeNm><lndpclAr>120.25</lndpclAr>'
                 '<posesnSeCodeNm>국유지</posesnSeCodeNm>'
                 '</ladfrlVOList>')
        rec=parse_ledger(data,PNU)
        self.assertEqual(rec['lndpclAr'],120.25)
        self.assertEqual(rec['posesnSeCodeNm'],'국유지')

    def test_price_rejects_wrong_year_and_pnu(self):
        data=xml('<resultCode>00</resultCode><fields><field>'
                 f'<pnu>{PNU}</pnu><stdrYear>2025</stdrYear>'
                 '<pblntfPclnd>80000</pblntfPclnd>'
                 '</field></fields>')
        self.assertIsNone(parse_price(data,PNU,2026))
        with self.assertRaises(FactTransportError):
            parse_price(data,'1111010100100019999',2025)

    def test_price_exact_year_normalization(self):
        data=xml('<resultCode>00</resultCode><fields><field>'
                 f'<pnu>{PNU}</pnu><stdrYear>2026</stdrYear><stdrMt>01</stdrMt>'
                 '<pblntfPclnd>1000000</pblntfPclnd>'
                 '</field></fields>')
        rec=parse_price(data,PNU,2026)
        self.assertEqual(rec['price_per_m2'],1000000.)
        self.assertEqual(rec['year'],2026)

    def test_characteristics_do_not_forge_ownership(self):
        data=xml('<resultCode>00</resultCode><fields><field>'
                 f'<pnu>{PNU}</pnu><lndpclAr>45</lndpclAr>'
                 '<lndcgrCodeNm>대</lndcgrCodeNm></field></fields>')
        rec=parse_characteristics(data,PNU)
        self.assertEqual(rec['lndpclAr'],45.)
        self.assertNotIn('posesnSeCodeNm',rec)

    def test_building_pnu_mapping(self):
        self.assertEqual(pnu_to_building_params(PNU),{
            'sigunguCd':'11110','bjdongCd':'10100','platGbCd':'0',
            'bun':'0001','ji':'0001'})

    def test_building_hub_json_xml_variants(self):
        data={'response':{'header':{'resultCode':'00'},
              'body':{'totalCount':1,'items':{'item':{'mgmBldrgstPk':'A'}}}}}
        self.assertEqual(parse_building_page(data),([{'mgmBldrgstPk':'A'}],1))
        xml_data=xml('<resultCode>00</resultCode><totalCount>1</totalCount>'
                     '<items><item><mgmBldrgstPk>B</mgmBldrgstPk>'
                     '</item></items>')
        self.assertEqual(parse_building_page(xml_data),([{'mgmBldrgstPk':'B'}],1))

    def test_official_ledger_source_no_seoul_snapshot(self):
        record=OfficialRecordFacts(vworld=None,building_key=lambda:'')
        fixture=xml('<resultCode>00</resultCode><ladfrlVOList>'
                    f'<pnu>{PNU}</pnu><lndpclAr>90</lndpclAr>'
                    '</ladfrlVOList>')
        with patch.object(record.vworld,'fetch',return_value=fixture):
            result=record.ledger(PNU)
        self.assertEqual(result['selected_source'],'vworld_ladfrl')
        self.assertEqual(result['record']['lndpclAr'],90.)

    def test_truncated_building_hub_fails_not_zero(self):
        record=OfficialRecordFacts(building_key=lambda:'dummy')
        incomplete=xml('<resultCode>00</resultCode><totalCount>12</totalCount>'
                       '<items><item><bldNm>one</bldNm></item></items>')
        with patch('facts.official_records._public_xml',return_value=incomplete):
            with self.assertRaises(FactTransportError) as err:
                record.building_titles(PNU)
        self.assertEqual(err.exception.code,'SOURCE_TRUNCATED')

    def test_provider_error_cannot_be_no_record(self):
        data=xml('<resultCode>99</resultCode><resultMsg>unauthorized</resultMsg>')
        with self.assertRaises(FactTransportError):
            parse_ledger(data,PNU)
        with self.assertRaises(FactTransportError):
            parse_building_page(data)

if __name__=='__main__':unittest.main()
