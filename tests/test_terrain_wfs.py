"""Mocked tests for terrain WFS acquisition: never use unverified layers as terrain FACT."""
import json
import os
from unittest.mock import patch

import development_terrain_wfs as terrain


CAP = (b'<wfs:WFS_Capabilities xmlns:wfs="http://www.opengis.net/wfs/2.0">'
       b'<wfs:FeatureTypeList><wfs:FeatureType>'
       b'<wfs:Name>test:contours</wfs:Name></wfs:FeatureType>'
       b'<wfs:FeatureType><wfs:Name>test:spots</wfs:Name>'
       b'</wfs:FeatureType></wfs:FeatureTypeList></wfs:WFS_Capabilities')
CONTOUR = {'type':'FeatureCollection','features':[{
    'type':'Feature','id':'c1',
    'geometry':{'type':'LineString','coordinates':[[127.0,37.0],[127.00001,37.00001]]},
    'properties':{'CONT':120}}]}
SPOTS = {'type':'FeatureCollection','features':[{
    'type':'Feature','id':'s1',
    'geometry':{'type':'Point','coordinates':[127.0,37.0]},
    'properties':{'height':125}}]}
SITE = {'type':'Polygon','coordinates':[[
    [126.9999,36.9999],[127.0002,36.9999],
    [127.0002,37.0002],[126.9999,37.0002],
    [126.9999,36.9999]]]}


class Response:
    status_code = 200
    def __init__(self, body):self.body=body
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def iter_content(self,size):yield self.body


def _mock_get(url, params, **kwargs):
    assert kwargs['allow_redirects'] is False
    if params['REQUEST']=='GetCapabilities':return Response(CAP)
    payload=CONTOUR if params['TYPENAME']=='test:contours' else SPOTS
    return Response(json.dumps(payload).encode())


def _env():
    return patch.dict(os.environ,{
        'TERRAIN_WFS_URL':'https://api.vworld.kr/req/wfs',
        'TERRAIN_WFS_CONTOUR_TYPENAME':'test:contours',
        'TERRAIN_WFS_SPOT_TYPENAME':'test:spots',
        'VWORLD_API_KEY':'MOCK-KEY'},clear=True)


def test_bounded_wfs_terrain_features_and_credentials_hidden():
    with _env(),patch.object(terrain.requests,'get',side_effect=_mock_get):
        result=terrain.collect(SITE)
    assert result['status']=='DATA'
    assert result['contour_count']==1
    assert result['spot_count']==1
    assert result['source_crs']=='EPSG:4326'
    assert 'MOCK-KEY' not in str(result)


def test_reject_missing_wfs_layer():
    with _env(),patch.object(terrain.requests,'get',
        return_value=Response(CAP.replace(b'test:spots',b'test:absent'))):
        result=terrain.collect(SITE)
    assert result['status']=='LAYER_UNAVAILABLE'


def test_reject_no_official_wfs_configuration():
    with patch.dict(os.environ,{},clear=True):
        result=terrain.collect(SITE)
    assert result['status']=='MISSING_SOURCE'


def test_reject_source_without_observed_height():
    broken=json.loads(json.dumps(CONTOUR))
    broken['features'][0]['properties']={}
    def get(url,params,**kwargs):
        if params['REQUEST']=='GetCapabilities':return Response(CAP)
        return Response(json.dumps(
            broken if params['TYPENAME']=='test:contours' else SPOTS).encode())
    with _env(),patch.object(terrain.requests,'get',side_effect=get):
        result=terrain.collect(SITE)
    assert result['status']=='MISSING_HEIGHT'


def test_reject_non_official_host():
    with patch.dict(os.environ,{
        'TERRAIN_WFS_URL':'https://untrusted.example.com/wfs',
        'TERRAIN_WFS_CONTOUR_TYPENAME':'test:contours',
        'TERRAIN_WFS_SPOT_TYPENAME':'test:spots'},clear=True):
        result=terrain.collect(SITE)
    assert result['status']=='MISSING_SOURCE'
