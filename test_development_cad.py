"""Offline contract tests for survey CAD export; public API is deliberately mocked."""
import io, json, threading, zipfile
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pyproj import Transformer
from shapely.geometry import box, shape, mapping, LineString
from shapely.ops import transform, unary_union
import ezdxf
import pytest
from development_cad import (_collect_surrounding_cadastre, _drawing_frame, dxf_bytes,
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


def prepare_background(client, expected_requests=4):
    url='/api/development/reports/test-token/cad-background'
    initial=client.get(url+'/status')
    assert initial.status_code==200
    assert initial.json()['ready'] is False
    assert client.get('/api/development/reports/test-token/dxf/1-1').status_code==409
    for _ in range(64):
        response=client.post(url+'/next')
        assert response.status_code==200,response.text[:240]
        if response.json()['ready']:
            break
    else:
        pytest.fail('surrounding cadastre never completed')
    state=client.get(url+'/status').json()
    assert state['ready'] is True
    assert state['request_count']==expected_requests
    return state


def test_provider_error_never_generates_partial_cad():
    client,_=make_app(FakeVworld('rate_limit'))
    response=client.post('/api/development/reports/test-token/cad-background/next')
    assert response.status_code==503
    assert 'RATE_LIMIT' in response.text
    assert client.get('/api/development/reports/test-token/dxf/1-1').status_code==409
    state=client.get('/api/development/reports/test-token/cad-background/status').json()
    assert state['ready'] is False and state['done']==0


def test_staged_provider_truncation_recovers_without_discarding_tiles():
    provider=FakeVworld('truncated')
    client,_=make_app(provider)
    state=prepare_background(client,expected_requests=8)
    assert state['done']==7  # four first-level tiles, first divided into four.
    assert provider.count==8


def test_oversize_frame_subdivides_by_area():
    large=box(CX-2200,CY-2200,CX+2200,CY+2200)
    provider=FakeVworld()
    out=_collect_surrounding_cadastre(large,provider,LayerQuery)
    assert out['request_count']==16
    assert len(out['features'])==200


def test_real_dxf_and_bulk_zip_reopen_cad_layers():
    provider=FakeVworld();client,snapshots=make_app(provider)
    prepare_background(client)
    response=client.get('/api/development/reports/test-token/dxf/1-1')
    assert response.status_code==200,response.text[:200]
    doc=load_dxf(response.content)
    assert 'SURVEY_A3' in doc.layouts
    layout=doc.layouts.get('SURVEY_A3')
    assert len(list(layout.query('TEXT'))) > 1
    spaces=doc.modelspace()
    frame_polys=list(spaces.query('LWPOLYLINE[layer=="DRAWING_FRAME"]'))
    assert len(frame_polys)==1
    points=[(row[0],row[1]) for row in frame_polys[0].get_points('xy')]
    assert min(x for x,y in points)==pytest.approx(FRAME.bounds[0],abs=.001)
    assert max(y for x,y in points)==pytest.approx(FRAME.bounds[3],abs=.001)
    cadastre=list(spaces.query('LWPOLYLINE[layer=="CADASTRE"]'))
    linework=[LineString([(p[0],p[1]) for p in ent.get_points('xy')]) for ent in cadastre]
    # One clean parcel edge, not duplicate shared edges from neighboring polygons.
    assert sum(edge.length for edge in linework)==pytest.approx(8600.0, abs=0.01)
    assert sum(edge.length for edge in linework)==pytest.approx(unary_union(linework).length, abs=0.01)
    assert all(FRAME.bounds[0]-.01 <= p[0] <= FRAME.bounds[2]+.01 and
               FRAME.bounds[1]-.01 <= p[1] <= FRAME.bounds[3]+.01
               for ent in cadastre for p in ent.get_points('xy'))
    labels=list(spaces.query('TEXT[layer=="PARCEL_LABEL"]'))
    assert len(labels)==200
    assert all(label.dxf.style=='KOREAN' and label.dxf.height>0 for label in labels)
    assert len(list(spaces.query('LWPOLYLINE[layer=="SITE_BOUNDARY"]'))) == 1
    assert provider.count==4
    zip_resp=client.get('/api/development/reports/test-token/dxf.zip')
    assert zip_resp.status_code==200
    assert provider.count==5 # four cached CAD tiles + one zoning-context fetch for 5-2; no new cadastral tile requests
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
    for _ in range(3):
        assert client.post('/api/development/reports/test-token/cad-background/next').status_code==200
    r=client.post('/api/development/reports/test-token/cad-background/next')
    assert r.status_code==503
    assert client.get('/api/development/reports/test-token/dxf/1-1').status_code==409


def test_partial_building_cad_export_has_layers_and_a3_layout():
    # Even a PARTIAL report can export proven GIS building footprints,
    # but cannot shade a provisional parcel intersection as a building.
    provider = FakeVworld()
    client, snapshots = make_app(provider)
    prepare_background(client)
    item = {
        'id': '3-3', 'name': '층수별 현황',
        'status': '일부 공간자료 확보', 'thematic_type': 'building',
        'cad_exportable': True, 'geometry_status': 'PARTIAL',
        'provider_verified': False,
        'features': [{'type': 'Feature',
                      'geometry': mapping(transform(INVERSE, box(CX-20, CY-10, CX+20, CY+10))),
                      'properties': {'name': '3층', 'class_code': 'C03', 'color': '#228833',
                                     'join_status': 'EXACT_ID', 'geometry_basis': 'BUILDING_FOOTPRINT'}}],
        'legend': [{'name': '3층', 'class_code': 'C03', 'color': '#228833',
                    'count': 1, 'pct': 100.0}],
    }
    response = client.get('/api/development/reports/test-token/dxf/3-3')
    assert response.status_code == 404  # No item in the fixture report.
    doc = load_dxf(dxf_bytes(item, snapshots['test-token'] | {
        '_cad_background': snapshots['test-token']['_cad_background']}, {}))
    assert 'SURVEY_A3' in doc.layouts
    assert len(list(doc.modelspace().query('HATCH[layer=="SURVEY_3_3_C03"]'))) > 0
    assert len(list(doc.modelspace().query('LWPOLYLINE[layer=="SURVEY_3_3_C03"]'))) > 0
    # Old PNU-substitute data must fail rather than silently export land as buildings.
    item['features'][0]['properties'] = {**item['features'][0]['properties'],
        'join_status': 'PNU_PARCEL_REGISTER', 'geometry_basis': 'CADASTRAL_PNU'}
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        dxf_bytes(item, snapshots['test-token'], {})
    assert exc.value.status_code == 409


def test_unavailable_item_rejected():
    client,_=make_app(FakeVworld())
    r=client.get('/api/development/reports/test-token/dxf/4-8')
    assert r.status_code==409



def test_small_parcel_label_is_not_forced_outside_geometry():
    # A tiny parcel cannot accommodate the former fixed 2.4 m text; suppress it.
    big=box(CX-100,CY-30,CX-55,CY+15)
    tiny=box(CX+100,CY+25,CX+100.5,CY+25.5)
    def feature(g,label):
        return {'geometry':mapping(transform(INVERSE,g)),'properties':{'jibun':label}}
    snap={'geometry':SITE,'_cad_background':{'status':'COMPLETE','features':[
        feature(big,'산 123-4'), feature(tiny,'1-2')]}}
    metadata={}
    doc=load_dxf(dxf_bytes({'id':'1-1','features':[]},snap,metadata))
    texts=list(doc.modelspace().query('TEXT[layer=="PARCEL_LABEL"]'))
    assert len(texts)==1
    assert texts[0].dxf.text=='산 123-4'
    assert texts[0].dxf.style=='KOREAN'
    assert metadata['labels']==1 and metadata['labels_omitted']==1
    assert metadata['cadastre_boundary_style']=='shared_edges_merged'


def test_duplicate_polygon_boundaries_are_not_doubled():
    # Even duplicate provider geometry must not produce overlapped cadastral edges.
    polygon=box(CX-35,CY-20,CX+35,CY+20)
    feature={'geometry':mapping(transform(INVERSE,polygon)), 'properties':{'jibun':'123'}}
    snap={'geometry':SITE,'_cad_background':{'status':'COMPLETE','features':[feature,feature]}}
    doc=load_dxf(dxf_bytes({'id':'1-1','features':[]},snap,{}))
    entities=list(doc.modelspace().query('LWPOLYLINE[layer=="CADASTRE"]'))
    total=sum(LineString([(p[0],p[1]) for p in e.get_points('xy')]+
             ([(e.get_points('xy')[0][0],e.get_points('xy')[0][1])] if e.closed else [])).length for e in entities)
    assert total == pytest.approx(polygon.length, abs=.001)


def test_jibun_labels_clear_highlighted_site_boundary():
    provider=FakeVworld();client,_=make_app(provider)
    prepare_background(client)
    response=client.get('/api/development/reports/test-token/dxf/1-1')
    assert response.status_code==200
    doc=load_dxf(response.content)
    site_line=transform(PROJECT,shape(SITE)).boundary
    for t in doc.modelspace().query('TEXT[layer=="PARCEL_LABEL"]'):
        x,y=t.dxf.align_point.x,t.dxf.align_point.y
        ems=sum(1.0 if ord(c)>127 else .68 for c in t.dxf.text)
        width=ems*t.dxf.height/2*1.06
        height=t.dxf.height*.62
        from shapely.geometry import box
        assert box(x-width,y-height,x+width,y+height).distance(site_line)>=max(.5,t.dxf.height*.35)-.01
    layers={layer.dxf.name:layer for layer in doc.layers}
    assert layers['CADASTRE'].dxf.lineweight==13
    assert layers['SITE_BOUNDARY'].dxf.lineweight==50
    assert layers['DRAWING_FRAME'].dxf.lineweight==25