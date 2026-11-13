"""Deterministic checks for shared TIN elevation-band FACT (synthetic geometry)."""
import math
from shapely.geometry import Polygon
from development_terrain_bands import elevation_bands


def _fixture():
    site = Polygon([(0,0),(10,0),(10,10),(0,10)])
    tris = [Polygon([(0,0),(10,0),(10,10)]),
            Polygon([(0,0),(10,10),(0,10)])]
    return site, tris


def test_linear_plane_elevation_area_and_mean():
    site, tris = _fixture()
    z = {(0.,0.):100,(10.,0.):130,(10.,10.):130,(0.,10.):100}
    v = elevation_bands(tris,z,site,lambda x,y:(x,y))
    assert v['elevation_status']=='DATA'
    assert v['elevation_coverage_pct']==100
    assert math.isclose(v['elevation']['mean'],115,abs_tol=1e-9)
    assert math.isclose(v['elevation']['min'],100,abs_tol=1e-9)
    assert math.isclose(v['elevation']['max'],130,abs_tol=1e-9)
    assert math.isclose(sum(x['area_m2'] for x in v['elevation_groups']),100,abs_tol=.02)


def test_flat_plane_is_single_band():
    site, tris = _fixture()
    z = {(0.,0.):111,(10.,0.):111,(10.,10.):111,(0.,10.):111}
    v = elevation_bands(tris,z,site,lambda x,y:(x,y))
    assert len(v['elevation_groups'])==1
    assert math.isclose(v['elevation_groups'][0]['area_m2'],100)
    assert v['elevation']['min']==v['elevation']['max']==111


def test_partial_tin_does_not_claim_complete_coverage():
    site, tris = _fixture()
    z = {(0.,0.):100,(10.,0.):110,(10.,10.):120}
    v = elevation_bands(tris[:1],z,site,lambda x,y:(x,y))
    assert v['elevation_status']=='PARTIAL'
    assert math.isclose(v['elevation_coverage_pct'],50,abs_tol=1e-9)


def test_site_hole_excluded_from_area():
    site = Polygon([(0,0),(10,0),(10,10),(0,10)],
                   holes=[[(3,3),(7,3),(7,7),(3,7)]])
    _, tris = _fixture()
    z = {(0.,0.):100,(10.,0.):120,(10.,10.):120,(0.,10.):100}
    v = elevation_bands(tris,z,site,lambda x,y:(x,y))
    assert v['elevation_status']=='DATA'
    assert math.isclose(v['elevation_covered_area_m2'],84,abs_tol=1e-9)
    assert math.isclose(sum(x['area_m2'] for x in v['elevation_groups']),84,abs_tol=.03)
