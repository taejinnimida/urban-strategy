"""Seoul R37 terrain must populate 4-1 without inventing an official DEM."""
from array import array
from unittest.mock import patch
import unittest

from pyproj import Transformer
from shapely.geometry import box, mapping
from shapely.ops import transform

from development_seoul_elevation import seoul_elevation_reference
from development_sources import enrich

BASE_X, BASE_Y = 180000., 470000.
INV = Transformer.from_crs("EPSG:5174","EPSG:4326",always_xy=True).transform
SITE = mapping(transform(INV,box(BASE_X+20,BASE_Y-80,BASE_X+80,BASE_Y-20)))


def _sample():
    meta={
        "resolution_m":20., "origin_x_west_edge":BASE_X,
        "origin_y_north_edge":BASE_Y, "width":5,"height":5,
        "nodata_i16":-32768, "source_extract_date":"2025-03-18",
        "source_type":"PLATFORM_DERIVED_REFERENCE",
        "scales":{"elevation_per_meter":10.},
    }
    vals=array("h",[1000+r*50+c*10 for r in range(5) for c in range(5)])
    return meta,vals


class SeoulExistingElevationTest(unittest.TestCase):
    def test_source_has_actual_heights_and_area_weights(self):
        with patch("development_seoul_elevation._load_reference",return_value=_sample()):
            r=seoul_elevation_reference(SITE)
        self.assertEqual(r["status"],"REFERENCE")
        self.assertEqual(r["source_type"],"PLATFORM_DERIVED_REFERENCE")
        self.assertEqual(r["resolution_m"],20)
        self.assertTrue(r["elevation_features"])
        self.assertGreater(r["elevation"]["mean"],100)
        self.assertAlmostEqual(sum(g["area_m2"] for g in r["elevation_groups"]),
                               r["dem_site_area_m2"],delta=.01)
        self.assertFalse(r["legal_submission_ready"])
        self.assertGreater(r["elevation_coverage_pct"],99.9)

    def test_missing_cells_are_not_assumed_flat(self):
        meta,vals=_sample()
        vals[2*5+2]=-32768
        with patch("development_seoul_elevation._load_reference",
                   return_value=(meta,vals)):
            r=seoul_elevation_reference(SITE)
        self.assertEqual(r["status"],"REFERENCE")
        self.assertLess(r["elevation_coverage_pct"],100)
        self.assertEqual(r["elevation_status"],"PARTIAL")

    def test_missing_reference_does_not_fabricate_heights(self):
        with patch("development_seoul_elevation._load_reference",
                   return_value=(None,None)):
            r=seoul_elevation_reference(SITE)
        self.assertEqual(r["status"],"MISSING_SOURCE")
        self.assertNotIn("elevation",r)

    def test_4_1_survey_is_reference_not_official_dem(self):
        with patch("development_seoul_elevation._load_reference",return_value=_sample()):
            terrain=seoul_elevation_reference(SITE)
        snapshot={"geometry":SITE,"_national_sources":{"dem":terrain},
                  "_report_layers":{},"_report":{"land":{}}}
        data={"land":[],"buildings":[],"area_m2":3600,
              "items":[{"id":"4-1","name":"표고분석","detail":""}]}
        result=enrich(data,snapshot,lambda g:g.area)
        item=result["items"][0]
        self.assertEqual(item["status"],"참고 분석자료")
        self.assertEqual(item["thematic_type"],"elevation")
        self.assertEqual(item["source_type"],"PLATFORM_DERIVED_REFERENCE")
        self.assertTrue(item["features"])
        self.assertFalse(item["provider_verified"])
        self.assertFalse(item["cad_exportable"])
        self.assertEqual(item["survey_table"]["status"],"REFERENCE")
        self.assertEqual(item["legend"],terrain["elevation_groups"])

if __name__=="__main__":
    unittest.main()
