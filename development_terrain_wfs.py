"""On-demand official WFS contour/spot-height reader for preliminary terrain FACT.

Only administrators can configure an upstream official WFS endpoint and its
*verified* layer names. The server fetches a small site-plus-buffer bbox.
Never use WMS images, zero-height defaults, guessed type names, or a countrywide
SHP mirror. A capability mismatch and incomplete paging fail closed.
"""
import json
import math
import os
import re
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import requests
from pyproj import Transformer
from shapely.geometry import box, shape
from shapely.ops import transform

URL_ENV = 'TERRAIN_WFS_URL'
CONTOUR_ENV = 'TERRAIN_WFS_CONTOUR_TYPENAME'
SPOT_ENV = 'TERRAIN_WFS_SPOT_TYPENAME'
MAX_BYTES = 8_000_000
PAGE_SIZE = 1000
MAX_TOTAL = 10000
BUFFER_M = 150.0
MAX_BBOX_AREA_M2 = 5_000_000
NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_.:-]{0,99}$')


class TerrainServiceError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _config():
    raw = os.environ.get(URL_ENV, '').strip()
    contour = os.environ.get(CONTOUR_ENV, '').strip()
    spot = os.environ.get(SPOT_ENV, '').strip()
    if not raw or not contour or not spot:
        raise TerrainServiceError('MISSING_SOURCE', '공식 등고선·표고점 WFS URL과 레이어명이 등록되지 않음')
    url = urlsplit(raw)
    host = (url.hostname or '').lower()
    allowed = host == 'api.vworld.kr' or host.endswith('.go.kr')
    if (url.scheme != 'https' or not allowed or url.username or url.password or
            url.port not in (None, 443) or url.query or url.fragment):
        raise TerrainServiceError('MISSING_SOURCE', '인증된 공공 WFS 주소 설정 필요')
    if not all(NAME.fullmatch(x) for x in (contour, spot)) or contour == spot:
        raise TerrainServiceError('MISSING_SOURCE', '공식 등고선·표고점 레이어 식별자 설정 오류')
    key = (os.environ.get('TERRAIN_WFS_API_KEY') or
           (os.environ.get('VWORLD_API_KEY') if host == 'api.vworld.kr' else '') or '').strip()
    key_param = os.environ.get('TERRAIN_WFS_KEY_PARAM', 'KEY' if host == 'api.vworld.kr' else '').strip()
    if key_param and not NAME.fullmatch(key_param):
        raise TerrainServiceError('MISSING_SOURCE', 'WFS 인증 설정 오류')
    if host == 'api.vworld.kr' and not key:
        raise TerrainServiceError('MISSING_CREDENTIAL', '브이월드 서버 인증키 설정 필요')
    return {'url':raw, 'contour':contour, 'spot':spot, 'key':key,
            'key_param':key_param,
            'provider': os.environ.get('TERRAIN_WFS_PROVIDER','공식 지형 WFS')[:80],
            'as_of':os.environ.get('TERRAIN_WFS_AS_OF','기준일 미확인')[:32]}


def _get(config, params, *, max_bytes=MAX_BYTES):
    if config['key'] and config['key_param']:
        params = {**params, config['key_param']:config['key']}
    try:
        with requests.get(config['url'], params=params, timeout=(4, 20),
                          allow_redirects=False, stream=True,
                          headers={'Accept':'application/json, application/xml, text/xml'}) as response:
            if response.status_code != 200:
                raise TerrainServiceError('PROVIDER_REJECTED' if response.status_code in (401,403)
                    else 'PROVIDER_ERROR', '공식 지형 WFS 서버 응답 오류')
            data=bytearray()
            for chunk in response.iter_content(64*1024):
                data.extend(chunk)
                if len(data)>max_bytes:
                    raise TerrainServiceError('SOURCE_TOO_LARGE','대상지 WFS 응답 크기 한도 초과')
            return bytes(data)
    except requests.RequestException:
        raise TerrainServiceError('PROVIDER_UNREACHABLE','공식 지형 WFS 연결 실패') from None


def _capabilities(config):
    body=_get(config, {'SERVICE':'WFS','VERSION':'2.0.0','REQUEST':'GetCapabilities'},
              max_bytes=3_000_000)
    try:
        root=ET.fromstring(body)
    except ET.ParseError:
        raise TerrainServiceError('PROVIDER_FORMAT','WFS 서비스목록 형식 오류') from None
    types=set()
    for feature_type in root.iter():
        if feature_type.tag.split('}')[-1]!='FeatureType':
            continue
        for child in feature_type:
            if child.tag.split('}')[-1]=='Name' and child.text:
                types.add(child.text.strip())
    if not types:
        raise TerrainServiceError('PROVIDER_FORMAT','WFS GetCapabilities 피처 레이어 확인 불가')
    if config['contour'] not in types or config['spot'] not in types:
        raise TerrainServiceError('LAYER_UNAVAILABLE','WFS에서 등고선·표고점 원도형 레이어 제공 확인 불가')


def _validated_features(data, kind, bounds):
    if not isinstance(data,dict) or data.get('type')!='FeatureCollection':
        raise TerrainServiceError('PROVIDER_FORMAT','WFS 원도형 GeoJSON이 아님')
    features=data.get('features')
    if not isinstance(features,list):
        raise TerrainServiceError('PROVIDER_FORMAT','WFS 피처 목록 형식 오류')
    out=[]
    for f in features:
        if not isinstance(f,dict) or not isinstance(f.get('properties'),dict):
            raise TerrainServiceError('PROVIDER_FORMAT','WFS 도형·속성 누락')
        try:
            g=shape(f['geometry'])
        except (KeyError,TypeError,ValueError):
            raise TerrainServiceError('PROVIDER_FORMAT','WFS 도형 해석 실패') from None
        valid_types=('LineString','MultiLineString') if kind=='contour' else ('Point',)
        if g.is_empty or not g.is_valid or g.geom_type not in valid_types:
            raise TerrainServiceError('PROVIDER_FORMAT','WFS 등고선·표고점 도형유형 오류')
        if any(not math.isfinite(v) for v in g.bounds) or not g.intersects(box(*bounds)):
            raise TerrainServiceError('CRS_MISMATCH','WFS 좌표계 또는 요청 범위 불일치')
        props={str(k).lower():v for k,v in f['properties'].items()}
        # The contour engine validates source numeric altitude, missing/zero
        # conflicts and interpolation; no fallback height is invented here.
        if not any(k in props for k in ('cont','elev','elevation','height','z',
                 'altitude','elev_m','alt','level','hgt','z_value','zvalue','표고','고도')) and not g.has_z:
            raise TerrainServiceError('MISSING_HEIGHT','WFS 원도형에 표고속성이 없음')
        out.append(f)
    return out


def _page(config, layer, bounds, kind):
    results=[]
    seen=set()
    for start in range(0,MAX_TOTAL,PAGE_SIZE):
        body=_get(config, {
            'SERVICE':'WFS','VERSION':'2.0.0','REQUEST':'GetFeature',
            'TYPENAME':layer,'SRSNAME':'EPSG:4326',
            'BBOX':','.join(f'{x:.9f}' for x in bounds)+',EPSG:4326',
            'OUTPUTFORMAT':'application/json',
            'COUNT':PAGE_SIZE,'STARTINDEX':start})
        try:
            page=json.loads(body)
        except (ValueError, UnicodeError):
            raise TerrainServiceError('PROVIDER_FORMAT','WFS GeoJSON 응답이 아님') from None
        if isinstance(page,dict) and page.get('exceptions'):
            raise TerrainServiceError('PROVIDER_ERROR','공식 지형 WFS 조회 오류')
        batch=_validated_features(page,kind,bounds)
        if not batch:
            break
        for f in batch:
            ident=f.get('id')
            identity=str(ident) if ident else json.dumps((f.get('geometry'),f.get('properties')),
                sort_keys=True,ensure_ascii=False,separators=(',',':'))
            if identity in seen:
                raise TerrainServiceError('PAGING_ERROR','WFS 중복·페이지 누락 가능성: 분석 중단')
            seen.add(identity)
            results.append(f)
        if len(batch)<PAGE_SIZE:
            break
    else:
        raise TerrainServiceError('SOURCE_TOO_LARGE','대상지 WFS 피처 건수 한도 초과')
    return results


def collect(geometry):
    """Return observed contour/spot GeoJSON over site + 150m, or an error status.

    Feature coordinates are WGS84 lon/lat; no durable nationwide source cache.
    """
    try:
        config=_config()
        site=shape(geometry)
        if site.is_empty or not site.is_valid or site.geom_type not in ('Polygon','MultiPolygon'):
            raise TerrainServiceError('INVALID_SITE','대상지 도형 오류')
        to=Transformer.from_crs('EPSG:4326','EPSG:5179',always_xy=True)
        back=Transformer.from_crs('EPSG:5179','EPSG:4326',always_xy=True)
        metric=transform(to.transform,site)
        extended=metric.buffer(BUFFER_M)
        if (extended.area>MAX_BBOX_AREA_M2 or
            not all(math.isfinite(v) for v in metric.bounds)):
            raise TerrainServiceError('SITE_TOO_LARGE','실시간 WFS 분석 허용범위 초과')
        search=transform(back.transform,extended).bounds
        _capabilities(config)
        lines=_page(config,config['contour'],search,'contour')
        spots=_page(config,config['spot'],search,'spot')
        if not lines or not spots:
            raise TerrainServiceError('INSUFFICIENT_SOURCE','등고선 또는 표고점 원도형 미수록')
        features=lines+spots
        if len(features)>MAX_TOTAL:
            raise TerrainServiceError('SOURCE_TOO_LARGE','대상지 WFS 피처 건수 한도 초과')
        return {'status':'DATA','source_mode':'LIVE_WFS',
                'features':features,'source_crs':'EPSG:4326',
                'source':config['provider'], 'source_url':config['url'],
                'as_of':config['as_of'], 'contour_count':len(lines),
                'spot_count':len(spots),
                'detail':'공식 WFS 등고선·표고점 원도형 대상지 주변범위 호출 완료 · 최신성·법정 적합성 별도 검증'}
    except TerrainServiceError as exc:
        return {'status':exc.code,'detail':str(exc), 'source_mode':'LIVE_WFS'}
