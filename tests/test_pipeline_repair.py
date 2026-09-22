import io
import json
import zipfile
from pathlib import Path
from unittest.mock import patch
import pytest
import requests
import shapefile
from pyproj import CRS
from fastapi.testclient import TestClient
import app

SITE={'type':'Polygon','coordinates':[[[126.977,37.566],[126.978,37.566],[126.978,37.567],[126.977,37.567],[126.977,37.566]]]}

@pytest.fixture(autouse=True)
def isolated_transport():
    app._VWORLD_FAILURES.clear()
    yield
    app._VWORLD_FAILURES.clear()


def response(status=200, body=b'{"response":{"status":"OK"}}', content_type='application/json'):
    r=requests.Response();r.status_code=status;r._content=body;r._content_consumed=True;r.headers['content-type']=content_type
    return r


def test_bundled_endpoints_work_without_external_network():
    with patch.object(app.requests,'get',side_effect=AssertionError('External request forbidden')):
        c=TestClient(app.app)
        for path in ['regulatory-reference-intersections','local-disaster-reference-intersections','development-local-intersections','hill-intersections','renewal-intersections','biotope-intersections','forest-classification-intersections','school-absolute-protection-intersections','safe-downtown-exclusion']:
            r=c.post('/api/spatial/'+path,json={'geometry':SITE})
            assert r.status_code==200,(path,r.text)
            d=r.json()
            assert d.get('status') not in ['error','unavailable'],(path,d)
            assert not d.get('errors'),(path,d.get('errors'))
        d=c.post('/api/spatial/regulatory-reference-intersections',json={'geometry':SITE}).json()
        layers=[v for v in d.values() if isinstance(v,dict) and 'known' in v]
        assert len(layers)==13
        assert all(v['known'] is True for v in layers)
        assert c.get('/api/reference/ecvam-status').status_code==200


def test_industrial_route_uses_existing_analyzer():
    expected={'overlaps':[], 'metadata':{'available':True}}
    with patch.object(app,'analyze_industrial_park_intersections',return_value=expected) as fn:
        r=TestClient(app.app).post('/api/spatial/industrial-park-intersections',json={'geometry':SITE})
        assert r.status_code==200 and r.json()==expected
        fn.assert_called_once_with(SITE)


def fixture_zip(tmp_path,field='MNUM'):
    base=tmp_path/'source'
    with shapefile.Writer(str(base)) as w:
        w.field(field,'C');w.poly([[[126,37],[126,38],[127,38],[127,37],[126,37]]]);w.record('UJB100')
    base.with_suffix('.prj').write_text(CRS.from_epsg(4326).to_wkt())
    path=tmp_path/'source.zip'
    with zipfile.ZipFile(path,'w') as z:
        for p in tmp_path.glob('source.*'):
            if p.suffix!='.zip':z.write(p,p.name)
    return str(path)

@pytest.mark.parametrize('bounds,present,pct', [((126.2,37.2,126.3,37.3),True,100),((127.2,37.2,127.3,37.3),False,0),((126.9,37.2,127.1,37.3),True,50)])
def test_geometry_inside_outside_boundary(tmp_path,bounds,present,pct):
    from shapely.geometry import box,mapping
    d=app._analyze_local_polygon_zip(mapping(box(*bounds)),path=fixture_zip(tmp_path),source_label='test',include_codes=['UJB100'])
    assert d['known'] and d['present']==present
    assert d['overlap_pct']==pytest.approx(pct,abs=.2)


def test_missing_attribute_is_not_success_empty(tmp_path):
    with pytest.raises(RuntimeError,match='필수 속성'):
        app._analyze_local_polygon_zip(SITE,path=fixture_zip(tmp_path,'WRONG'),source_label='test',include_codes=['UJB100'])


def test_single_direct_success_has_no_fallback():
    with patch.object(app.requests,'get',return_value=response()) as get:
        _,route=app._vworld_get(app.VWORLD_DATA_URL,{'key':'secret'})
        assert route=='direct' and get.call_count==1


def test_direct_502_recovers_through_fallback():
    with patch.object(app.requests,'get',side_effect=[response(502),response()]) as get:
        _,route=app._vworld_get(app.VWORLD_DATA_URL,{'key':'secret'})
        assert route=='vworld_proxy' and get.call_count==2
        assert app._last_vworld_diagnostic()['direct_status']==502


def test_repeated_failures_open_cooldown_without_key_leak():
    with patch.object(app.requests,'get',side_effect=requests.ConnectionError('https://example.test?key=secret')) as get:
        for _ in range(3):
            with pytest.raises(app.VWorldTransportError) as exc:
                app._vworld_get(app.VWORLD_DATA_URL,{'key':'secret'})
            assert 'secret' not in str(exc.value)
        assert get.call_count==6
        with pytest.raises(app.VWorldTransportError,match='COOLDOWN'):
            app._vworld_get(app.VWORLD_DATA_URL,{'key':'secret'})
        assert get.call_count==6
        assert 'secret' not in json.dumps(app._last_vworld_diagnostic())
        app._VWORLD_FAILURES[app.urlparse(app.VWORLD_DATA_URL).netloc+app.urlparse(app.VWORLD_DATA_URL).path]['until']=0
        get.side_effect=None;get.return_value=response()
        assert app._vworld_get(app.VWORLD_DATA_URL,{})[1]=='direct'


def test_html_200_is_not_data():
    with patch.object(app.requests,'get',return_value=response(200,b'<html>Gateway error</html>','text/html')):
        with pytest.raises(app.VWorldTransportError,match='NON_DATA_HTML'):
            app._vworld_get(app.VWORLD_DATA_URL,{})


def test_404_not_retried_as_upstream_failure():
    with patch.object(app.requests,'get',return_value=response(404)) as get:
        res,_=app._vworld_get(app.VWORLD_DATA_URL,{})
        assert res.status_code==404 and get.call_count==1


def test_single_zoning_probe_safe_diagnostic():
    with patch.object(app,'_vworld_key',return_value='secret'),patch.object(app.requests,'get',return_value=response(502)):
        r=TestClient(app.app).get('/api/vworld/test?layer=LT_C_UQ111')
        assert r.status_code==502
        assert r.json()['detail']['transport']['direct_status']==502
        assert 'secret' not in r.text
