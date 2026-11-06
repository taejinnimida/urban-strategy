"""Offline contract tests for survey CAD export; public API is deliberately mocked."""
import io, json, threading, zipfile
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pyproj import Transformer
from shapely.geometry import box, shape, mapping
from shapely.ops import transform
import ezdxf
import pytest
from development_cad import (_collect_surrounding_cadastre, _drawing_frame,
                             register_cad, CRS, MAX_TILE_AREA_M2)

PROJECT = Transformer.from_crs('EPSG:4326', CRS, always_xy=True).transform
INVERSE = Transformer.from_crs(CRS, 'EPSG:4326', always_xy=True).transform
P = transform(PROJECT, shape({'type':'Point','coordinates':[127.02,37.56]}))
CX, CY = P.x, P.y
SITE_XY = box(CX-100, CY-50, CX+100, CY+50)
SITE = mapping(transform(INVERSE,SITE_XY))
FRAME = _drawing_frame(shape(SITE))

def fixture_features():
    # 20 x 10 cadastral polygons cover the 400 x 200 m output frame, including tile seams.
    x0,y0,x1,y1=FRAME.bounds
    width=(x1-x0)/20; height=(y1-y0)/10
    features=[]
    for i in range(20):
      for j in range(10):
        geom=box(x0+i*width,y0+j*height,x0+(i+1)*width,y0+(j+1)*height)
        f={'type':'Feature','id':f'parcel_{i}_{j}', 'geometry':mapping(transform(INVERSE,geom)),
          'properties':{'pnu':f'1111100000{i:04d}{j:05d}','jibun':f'{i+1}-{j+1}'}}
        features.append(f)
    return features

FEATURES=fixture_features()

class LayerQuery:
    def __init__(self,**kwargs):self.__dict__.update(kwargs)

class FakeVworld:
    def __init__(self, mode=None):self.mode=mode;self.count=0;self.tile_bboxes=[]
    def __call__(self,request):
        self.count+=1
        assert request.layer=='LP_PA_CBND_BUBUN'
        tile=shape(request.geometry)
        self.tile_bboxes.append(transform(PROJECT,tile).bounds)
        assert tile.is_valid
        assert tile.area>0
        # First tile can mimic provider page truncation, making CAD retry smaller tiles.
        if self.mode=='truncated' and self.count==1:
            return {'status':'UNKNOWN','error_code':'PROVIDER_FORMAT','features':[]}
        if self.mode=='rate_limit' and self.count==1:
            return {'status':'UNKNOWN','error_code':'RATE_LIMIT','features':[]}
        if self.mode=='blank':
            return {'status':'COMPLETE','features':[]}
        res=[f for f in FEATURES if shape(f['geometry']).intersects(tile)]
        return {'status':'COMPLETE','features':res}

def make_app(vworld):
    fact={'status':'COMPLETE','fact_revision':'revision_A','features':FEATURES[80:105]}
    snapshot={'geometry':SITE,'area_m2':20000.0,'_report_layers':{'LP_PA_CBND_BUBUN':fact}}
    report={'items':[{'id':'1-1','name':'위치 및 면적','status':'기초자료 확보'},
                     {'id':'5-2','name':'용도지역','status':'공간자료 확보','provider_verified':True,
                      'features':[{'type':'Feature','geometry':SITE,'properties':{'name':'test'}}]},
                     {'id':'4-8','name':'단면','status':'자료 필요'}],
            'fact_revisions':{'LP_PA_CBND_BUBUN':'revision_A'},'as_of':'2026-11-06'}
    app=FastAPI();from fastapi import APIRouter
    router=APIRouter(prefix='/api/development')
    snapshots={'test-token':snapshot}
    register_cad(router, lambda token:report,snapshots,threading.Lock(),vworld,LayerQuery)
    app.include_router(router)
    return TestClient(app),snapshots

def load_dxf(content):
    doc=ezdxf.read(io.StringIO(content.decode('utf-8')))
    layers=set(layer.dxf.name for layer in doc.layers)
    assert {'DRAWING_FRAME','SITE_BOUNDARY','CADASTRE','PARCEL_LABEL'}<=layers
    assert doc.header['$INSUNITS']==6
    return doc


def test_2x_drawing_frame_and_projected_coordinates():
    b=transform(PROJECT,shape(SITE)).bounds;f=FRAME.bounds
    assert (f[2]-f[0])==pytest.approx(2*(b[2]-b[0]),abs=1e-6)
    assert (f[3]-f[1])==pytest.approx(2*(b[3]-b[1]),abs=1e-6)
    assert ((f[0]+f[2])/2,(f[1]+f[3])/2)==pytest.approx(((b[0]+b[2])/2,(b[1]+b[3])/2),abs=1e-6)


def test_tiling_deduplicates_at_tile_seams():
    provider=FakeVworld()
    out=_collect_surrounding_cadastre(FRAME,provider,LayerQuery)
    assert out['tile_count']==4
    assert out['request_count']==4
    assert len(out['features'])==200
    assert out['duplicates_removed']>0
    assert provider.count==4


def test_page_cap_recovery_by_subdivision():
    provider=FakeVworld('truncated')
    out=_collect_surrounding_cadastre(FRAME,provider,LayerQuery)
    assert out['request_count']==8  # one failed tile + four retries + three normal tiles
    assert len(out['features'])==200


def test_provider_error_never_generates_partial_cad():
    client,_=make_app(FakeVworld('rate_limit'))
    response=client.get('/api/development/reports/test-token/dxf/1-1')
    assert response.status_code==503
    assert 'RATE_LIMIT' in response.text


def test_oversize_frame_subdivides_by_area():
    large=box(CX-2200,CY-2200,CX+2200,CY+2200)
    provider=FakeVworld()
    out=_collect_surrounding_cadastre(large,provider,LayerQuery)
    assert out['request_count']==16
    assert len(out['features'])==200


def test_real_dxf_and_bulk_zip_reopen_cad_layers():
    provider=FakeVworld();client,snapshots=make_app(provider)
    response=client.get('/api/development/reports/test-token/dxf/1-1')
    assert response.status_code==200,response.text[:200]
    doc=load_dxf(response.content)
    spaces=doc.modelspace()
    frame_polys=list(spaces.query('LWPOLYLINE[layer=="DRAWING_FRAME"]'))
    assert len(frame_polys)==1
    points=[(row[0],row[1]) for row in frame_polys[0].get_points('xy')]
    assert min(x for x,y in points)==pytest.approx(FRAME.bounds[0],abs=.001)
    assert max(y for x,y in points)==pytest.approx(FRAME.bounds[3],abs=.001)
    assert len(list(spaces.query('LWPOLYLINE[layer=="CADASTRE"]'))) == 200
    assert len(list(spaces.query('TEXT[layer=="PARCEL_LABEL"]'))) == 200
    assert len(list(spaces.query('LWPOLYLINE[layer=="SITE_BOUNDARY"]'))) == 1
    assert provider.count==4
    zip_resp=client.get('/api/development/reports/test-token/dxf.zip')
    assert zip_resp.status_code==200
    assert provider.count==4 # cached: no re-requests
    with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as z:
        assert {'survey_1_1.dxf','survey_5_2.dxf','survey_manifest.json','README.txt'} <= set(z.namelist())
        assert 'survey_4_8.dxf' not in z.namelist()
        manifest=json.loads(z.read('survey_manifest.json'))
        assert manifest['frame_width_height_multiplier']==2.0
        assert manifest['cadastre_request_count']==4
        assert manifest['cadastre_duplicates_removed']>0
        for name in ('survey_1_1.dxf','survey_5_2.dxf'):load_dxf(z.read(name))


def test_no_fake_success_with_blank_cadastre():
    client,_=make_app(FakeVworld('blank'))
    r=client.get('/api/development/reports/test-token/dxf/1-1')
    assert r.status_code==503


def test_unavailable_item_rejected():
    client,_=make_app(FakeVworld())
    r=client.get('/api/development/reports/test-token/dxf/4-8')
    assert r.status_code==409
