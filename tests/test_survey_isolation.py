"""Ensure survey and Seoul applications do not depend on each other's runtime.

No external API calls. Source provider availability and result uncertainty remain
explicit when public sources have not been implemented.
"""
from unittest.mock import patch
import unittest
from fastapi.testclient import TestClient
from shapely.geometry import box, mapping

from dev_app import app as survey_app
from survey_sources import SurveySources, SurveySourceUnavailable


class StandaloneSurveyTest(unittest.TestCase):
    def setUp(self):
        self.client=TestClient(survey_app)
        self.geometry=mapping(box(127.0,37.5,127.001,37.501))

    def test_root_is_survey_not_seoul(self):
        page=self.client.get('/')
        self.assertEqual(page.status_code,200)
        self.assertIn('도시현황 FACT 조사',page.text)
        self.assertNotIn('서울시 정비사업 검토',page.text)
        self.assertEqual(self.client.get('/survey-ui.js').status_code,200)
        self.assertEqual(self.client.get('/seoul').status_code,404)
        self.assertEqual(self.client.get('/nonurban').status_code,404)

    def test_seoul_routes_not_mounted_here(self):
        routes={getattr(r,'path','') for r in survey_app.routes}
        self.assertNotIn('/api/rules/market-renewal',routes)
        # The same route can be exercised even when the framework keeps
        # include_router internals in a mounted router representation.
        with patch('facts.vworld.VWorldFacts.regions',return_value=[]):
            resp=self.client.post('/api/development/snapshots',json={'geometry':self.geometry})
        self.assertEqual(resp.status_code,200)
        token=resp.json()['id']
        self.assertEqual(self.client.get('/api/development/snapshots/'+token).status_code,200)
        self.assertFalse(any('/seoul' in r for r in routes))

    def test_boundary_fact_can_be_created_without_seoul_app(self):
        with patch('facts.vworld.VWorldFacts.regions',return_value=[]):
            result=self.client.post('/api/development/snapshots',json={'geometry':self.geometry})
        self.assertEqual(result.status_code,200)
        self.assertGreater(result.json()['area_m2'],0)
        self.assertIn('id',result.json())

    def test_official_sources_are_independent_and_mockable(self):
        source=SurveySources()
        with patch.object(source.records,'ledger',return_value={
                 'record':{'pnu':'1234567890123456789','lndpclAr':45.},
                 'selected_source':'vworld_ladfrl'}):
            result=source._resolve_land_ledger('1234567890123456789')
        self.assertEqual(result['record']['lndpclAr'],45.)
        with patch.object(source.records,'price',return_value={
                 'pnu':'1234567890123456789','year':2026,
                 'price_per_m2':1300000.}):
            result=source._resolve_individual_land_price('1234567890123456789',2026)
        self.assertEqual(result['price_per_m2'],1300000.)
        with patch.object(source.records,'building_titles',return_value=[{
                 'mgmBldrgstPk':'fixture-id'}]):
            rows=source._query_building_hub_title('1234567890123456789')
        self.assertEqual(len(rows),1)

    def test_live_provider_has_no_seoul_runtime_dependency(self):
        import sys
        import survey_sources
        self.assertNotIn('app',survey_sources.__dict__)
        self.assertNotIn('seoul',survey_sources.__dict__)
        self.assertFalse(any(s.startswith('app.') for s in sys.modules if s.startswith('survey_sources')))


if __name__=='__main__':unittest.main()
