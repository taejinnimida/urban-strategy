"""Offline tests for shared DEM classes; never requires API credentials."""
import unittest
from development.nature.terrain_classes import classify,new_accumulator,add_cell,finish

class TerrainBandsTest(unittest.TestCase):
    def test_elevation_edges_including_negative(self):
        for n,c in [(-2,'C01'),(0,'C02'),(19.99,'C02'),(20,'C03'),(40,'C04'),(60,'C05'),(80,'C06')]:
            with self.subTest(value=n):
                self.assertEqual(classify(n,'elevation')[0],c)
    def test_slope_edges(self):
        for n,c in [(0,'C01'),(4.99,'C01'),(5,'C02'),(10,'C03'),(15,'C04'),(20,'C05')]:
            with self.subTest(value=n):
                self.assertEqual(classify(n,'slope')[0],c)
    def test_invalid_not_zero(self):
        self.assertIsNone(classify(None,'elevation'))
        self.assertIsNone(classify(float('nan'),'elevation'))
        self.assertIsNone(classify(-1,'slope'))
        self.assertIsNone(classify(True,'slope'))
    def test_partial_area_never_normalized(self):
        a=new_accumulator('elevation')
        add_cell(a,12,70)
        self.assertIsNone(add_cell(a,None,30))
        result=finish(a,100)
        self.assertEqual(result['valid_area_m2'],70.)
        self.assertEqual(result['coverage_pct'],70.)
        self.assertEqual(result['missing_area_m2'],30.)
        self.assertEqual(result['groups'][0]['pct'],70.)
    def test_disjoint_category_sum(self):
        a=new_accumulator('slope')
        for index in range(200):add_cell(a,5*(index%5),0.123456)
        result=finish(a,30)
        self.assertEqual(sum(x['count'] for x in result['groups']),200)
        self.assertEqual(result['valid_area_m2'],24.69)
        self.assertEqual(len(result['groups']),5)

if __name__=='__main__':unittest.main()
