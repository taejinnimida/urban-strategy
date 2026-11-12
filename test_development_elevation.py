"""4-1 registered DEM regression: official-only, area-weighted FACT, maps and provenance."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from shapely.geometry import box, mapping, shape
from shapely.ops import transform
from pyproj import Transformer

from development_sources import dem, enrich

CRS='EPSG:5179'
X0,Y0=950000.,1950000.
TO_WGS=Transformer.from_crs(CRS,'EPSG:4326',always_xy=True).transform
TO_METRE=Transformer.from_crs('EPSG:4326',CRS,always_xy=True).transform

class ElevationSurveyTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.metric=box(X0+20,Y0+20,X0+60,Y0+60)
        self.target=mapping(transform(TO_WGS,self.metric))
        self.entry={'provider':'국토지리정보원 공식 DEM 시험자료',
                    'source_url':'https://map.ngii.go.kr/',
                    'as_of':'2026-11-12','file':'official.json',
                    'coverage':mapping(transform(TO_WGS,box(X0,Y0,X0+80,Y0+80)))}
        values=[[100.+r*2.+c*0.5 for c in range(8)] for r in range(8)]
        self.write_grid(values)
    def write_grid(self,values):
        self.grid={'values':values,'cell_size_m':10.,'x0':X0,'y0':Y0,
                   'crs':CRS,'nodata':-9999}
        (self.root/'official.json').write_text(json.dumps(self.grid),encoding='utf-8')
    def analysis(self):
        with patch('development_sources.manifest_entry',return_value=(self.entry,self.root)):
            return dem(self.target,lambda g:transform(TO_METRE,g).area)
    def test_no_official_dem_means_missing_not_zero(self):
        with patch('development_sources.manifest_entry',return_value=(None,self.root)):
            r=dem(self.target,lambda g:transform(TO_METRE,g).area)
        self.assertEqual(r['status'],'MISSING_SOURCE')
        self.assertNotIn('elevation',r)
    def test_elevation_completeness_and_area_distribution(self):
        r=self.analysis()
        self.assertEqual(r['elevation_status'],'DATA')
        self.assertAlmostEqual(r['elevation_coverage_pct'],100,places=2)
        self.assertTrue(r['elevation_map_complete'])
        self.assertEqual(len(r['elevation_features']),16)
        self.assertAlmostEqual(sum(x['area_m2'] for x in r['elevation_groups']),1600,places=3)
        self.assertAlmostEqual(r['elevation']['mean'],
                               sum(100.+row*2.+col*.5
                                   for row in range(2,6) for col in range(2,6))/16,places=5)
        self.assertTrue(all(f['properties']['class_code'].startswith('C')
                            and f['properties']['color'].startswith('#')
                            for f in r['elevation_features']))
        self.assertAlmostEqual(sum(f['properties']['area_m2'] for f in r['elevation_features']),1600,places=5)
    def test_nodata_does_not_create_missing_height_as_zero(self):
        self.grid['values'][3][3]=-9999
        self.write_grid(self.grid['values'])
        r=self.analysis()
        self.assertEqual(r['elevation_status'],'PARTIAL')
        self.assertAlmostEqual(r['elevation_coverage_pct'],93.75,places=2)
        self.assertEqual(len(r['elevation_features']),15)
    def test_enrich_reuses_exact_same_dem_fact_for_table_and_map(self):
        terrain=self.analysis()
        snapshot={'geometry':self.target,'_national_sources':{'dem':terrain},
                  '_report_layers':{},'_report':{'land':{}}}
        data={'area_m2':1600.,'land':[],'buildings':[],
              'items':[{'id':'4-1','name':'표고분석','detail':''}]}
        enriched=enrich(data,snapshot,lambda g:transform(TO_METRE,g).area)
        i=enriched['items'][0]
        self.assertEqual(i['thematic_type'],'elevation')
        self.assertEqual(i['status'],'공간자료 확보')
        self.assertEqual(i['survey_table']['status'],'COMPLETE')
        self.assertEqual(len(i['features']),len(terrain['elevation_features']))
        self.assertEqual(i['legend'],terrain['elevation_groups'])
        self.assertAlmostEqual(sum(x['area_m2'] for x in i['legend']),1600,places=2)
        self.assertEqual(i['statistics']['values'],terrain['elevation'])
        self.assertTrue(i['provider_verified'])
    def test_large_site_polygonized_into_class_vectors(self):
        # 8,000+ source grid cells should still produce a useful A3/DXF map
        # by dissolving adjacent cells with the same elevation class.
        self.grid['values']=[[100.+r*.1+c*.1 for c in range(100)]
                             for r in range(100)]
        self.write_grid(self.grid['values'])
        target_metric=box(X0+100,Y0+100,X0+900,Y0+900)
        self.target=mapping(transform(TO_WGS,target_metric))
        self.entry['coverage']=mapping(transform(TO_WGS,
            box(X0,Y0,X0+1000,Y0+1000)))
        r=self.analysis()
        self.assertEqual(r['elevation_status'],'DATA')
        self.assertTrue(r['elevation_map_complete'])
        self.assertLess(len(r['elevation_features']),300)
        self.assertGreater(len(r['elevation_features']),0)
        self.assertAlmostEqual(sum(x['area_m2'] for x in r['elevation_groups']),
                               640000.,places=2)
        mapped=sum(transform(TO_METRE,shape(f['geometry'])).area
                   for f in r['elevation_features'])
        self.assertAlmostEqual(mapped,640000.,delta=0.1)

    def test_registered_geotiff_reads_site_window(self):
        try:
            import numpy as np
            import rasterio
            from rasterio.transform import from_origin
        except ImportError:
            self.skipTest('rasterio not installed outside deployed environment')
        arr=np.array(self.grid['values'][::-1],dtype='float32')
        tif=self.root/'official.tif'
        with rasterio.open(tif,'w',driver='GTiff',width=8,height=8,count=1,
                           dtype='float32',crs=CRS,
                           transform=from_origin(X0,Y0+80,10,10),nodata=-9999) as ds:
            ds.write(arr,1)
        self.entry['file']='official.tif'
        r=self.analysis()
        self.assertEqual(r['elevation_status'],'DATA')
        self.assertTrue(r['elevation_map_complete'])
        self.assertEqual(len(r['elevation_features']),16)
        self.assertAlmostEqual(r['elevation']['mean'],self.grid['values'][3][3]+(2+.5)*.5,
                               delta=4.0)

if __name__=='__main__':
    unittest.main()
