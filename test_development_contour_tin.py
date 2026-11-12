"""Offline contracts for contour-height SHP -> TIN -> 10 m grid slope."""
import math
import unittest
from unittest.mock import patch
from shapely.geometry import LineString, box, mapping
from shapely.ops import transform
from pyproj import Transformer

from development_contour_tin import analyze, _source_height
from development_sources import enrich

BACK=Transformer.from_crs("EPSG:5179","EPSG:4326",always_xy=True).transform
X=950000
Y=1950000
SITE=mapping(transform(BACK,box(X+10,Y+10,X+90,Y+90)))
COVERAGE=mapping(transform(BACK,box(X-200,Y-200,X+300,Y+300)))
ENTRY={"provider":"국토지리정보원 수치지형도 테스트",
       "source_url":"https://map.ngii.go.kr/","as_of":"2026-11-13",
       "file":"contour.zip","crs":"EPSG:5179",
       "coverage":COVERAGE,"elevation_field":"ELEV"}

def contour_lines(slope_degrees=10.0, omit=False):
    # Six parallel contours across one planar slope, surrounding 80m site.
    gradient=math.tan(math.radians(slope_degrees))
    elevations=[-20,0,20,40,60,80,100,120]
    return [{"type":"Feature","geometry":mapping(
            LineString([(X+x,Y-100),(X+x,Y+200)])),
            "properties":({} if omit and x==60 else {"ELEV":100+x*gradient})}
            for x in elevations]

class ContourSlopeTest(unittest.TestCase):
    def run_analysis(self,features=None,entry=None,site=SITE):
        e=ENTRY if entry is None else entry
        with patch("development_sources.manifest_entry",return_value=(e,None)),\
             patch("development_sources.official_features",
                   return_value=(features if features is not None else contour_lines(),
                                 "EPSG:5179")):
            return analyze(site)

    def test_10m_planar_tin_grid_matches_source_gradient(self):
        result=self.run_analysis()
        self.assertEqual(result["status"],"DATA")
        self.assertEqual(result["grid_size_m"],10)
        self.assertAlmostEqual(result["mean_slope_deg"],10.,delta=0.1)
        self.assertAlmostEqual(result["coverage_pct"],100,delta=.01)
        self.assertEqual(result["valid_grid_count"],64)
        self.assertAlmostEqual(sum(r["area_m2"] for r in result["grid_records"]),
                               result["covered_area_m2"],places=2)
        self.assertEqual(len(result["features"]),64)
        self.assertEqual(result["tin_method"],"UNCONSTRAINED_DELAUNAY_CONTOUR_VERTICES")
        self.assertFalse(result["legal_submission_ready"])

    def test_missing_contour_height_refused(self):
        with self.assertRaisesRegex(ValueError,"elevation"):
            self.run_analysis(contour_lines(omit=True))

    def test_absent_official_contour_avoids_fabricated_slope(self):
        r=self.run_analysis(entry=None)  # ordinary features path
        self.assertEqual(r["status"],"DATA")
        with patch("development_sources.manifest_entry",return_value=(None,None)):
            result=analyze(SITE)
        self.assertEqual(result["status"],"MISSING_SOURCE")
        self.assertNotIn("mean_slope_deg",result)

    def test_height_field_uses_explicit_source_attribute(self):
        line=LineString([(1,1),(2,2)])
        self.assertEqual(_source_height({"ELEV":125.4},line,"ELEV")[0],125.4)
        self.assertEqual(_source_height({"HEIGHT":130},line,"ELEV")[0],None)

    def test_report_prefers_contour_tin_to_dem_horn(self):
        result=self.run_analysis()
        snapshot={"geometry":SITE,"_national_sources":{"contour_tin":result},
                  "_report_layers":{},"_report":{"land":{}}}
        data={"land":[],"buildings":[],"area_m2":6400,
              "items":[{"id":"4-2","name":"경사분석","detail":""}]}
        report=enrich(data,snapshot,lambda g:g.area)
        item=report["items"][0]
        self.assertEqual(item["thematic_type"],"slope_tin")
        self.assertEqual(item["survey_table"]["title"],
                         "[표 4-2 경사도현황(TIN 10m 참고분석)]")
        self.assertEqual(len(item["features"]),64)
        self.assertEqual(item["statistics"]["grid_size_m"],10)
        self.assertFalse(item["provider_verified"])
        self.assertFalse(item["statistics"]["legal_submission_ready"])
        self.assertFalse(item["cad_exportable"])

if __name__=="__main__":
    unittest.main()
