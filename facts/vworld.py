"""Independent read-only VWorld factual transport.

Does not import Seoul app, development RULE, or their shared circuit breaker.
This adapter is opt-in until existing nationwide callers and regression tests
are migrated; it must not silently replace the operating Seoul data path.
"""
import json
import os
import re
from urllib.parse import urlparse
import requests

ALLOWED = {
    ('api.vworld.kr', '/req/data'),
    ('api.vworld.kr', '/req/search'),
    ('api.vworld.kr', '/req/wfs'),
    ('api.vworld.kr', '/req/wms'),
    ('api.vworld.kr', '/ned/data/getLandCharacteristics'),
    ('api.vworld.kr', '/ned/data/ladfrlList'),
    ('api.vworld.kr', '/ned/data/getIndvdLandPriceAttr'),
    ('api.vworld.kr', '/ned/data/getLandUseAttr'),
}

class FactTransportError(RuntimeError):
    def __init__(self, code: str):
        self.code=code
        super().__init__(code)  # Never embed URLs, keys or response bodies.


def vworld_key() -> str:
    return os.environ.get('VWORLD_API_KEY','').strip()


def vworld_domain() -> str:
    raw=(os.environ.get('VWORLD_DOMAIN') or os.environ.get('RENDER_EXTERNAL_HOSTNAME') or 'localhost').strip()
    parsed=urlparse(raw if '://' in raw else 'https://'+raw)
    host=parsed.hostname or 'localhost'
    if not re.fullmatch(r'[A-Za-z0-9.-]+',host):
        raise FactTransportError('INVALID_DOMAIN')
    return host.lower()


class VWorldFacts:
    """Only observations and raw attributes; never legal eligibility."""
    def __init__(self, *, key_provider=vworld_key, domain_provider=vworld_domain):
        self._key_provider=key_provider
        self._domain_provider=domain_provider

    def configured(self) -> bool:
        return bool(self._key_provider())

    def fetch(self, url: str, params: dict, *, expected: str='json') -> dict | bytes:
        parsed=urlparse(url)
        if (parsed.scheme!='https' or (parsed.hostname,parsed.path) not in ALLOWED or
            parsed.username or parsed.password or parsed.port not in (None,443) or
            parsed.query or parsed.fragment):
            raise FactTransportError('INVALID_ENDPOINT')
        key=self._key_provider()
        if not key:
            raise FactTransportError('SERVICE_NOT_READY')
        query={str(k):str(v) for k,v in params.items() if k not in ('key','domain')}
        query.update({'key':key,'domain':self._domain_provider()})
        try:
            with requests.get(url,params=query,headers={
                'Referer':'https://'+self._domain_provider()+'/',
                'Accept':'application/json' if expected=='json' else 'application/xml',
                'User-Agent':'urban-facts/1.0',
            },timeout=(4,12),allow_redirects=False,stream=True) as response:
                if response.status_code!=200:
                    if response.status_code==429:code='RATE_LIMIT'
                    elif response.status_code in (401,403):code='PROVIDER_REJECTED'
                    else:code='PROVIDER_HTTP_ERROR'
                    raise FactTransportError(code)
                limit=3_000_000
                parts=[];size=0
                for chunk in response.iter_content(65536):
                    size+=len(chunk)
                    if size>limit:raise FactTransportError('PROVIDER_FORMAT')
                    parts.append(chunk)
                payload=b''.join(parts)
        except requests.RequestException:
            raise FactTransportError('CONNECTION_ERROR') from None
        if expected=='xml':
            return payload
        if expected!='json':
            raise FactTransportError('INVALID_FORMAT')
        try:
            data=json.loads(payload.decode('utf-8'))
        except (UnicodeError, ValueError):
            raise FactTransportError('PROVIDER_FORMAT') from None
        if not isinstance(data,dict):
            raise FactTransportError('PROVIDER_FORMAT')
        return data

    def regions(self, bbox) -> list:
        """Public WFS municipality boundaries. Classification is a FACT only."""
        if len(bbox)!=4 or not all(isinstance(v,(int,float)) for v in bbox):
            raise FactTransportError('INVALID_BBOX')
        result=self.fetch('https://api.vworld.kr/req/wfs',{
            'service':'WFS','request':'GetFeature','version':'1.0.0',
            'typename':'lt_c_adsido_info','srsname':'EPSG:4326',
            'bbox':','.join(map(str,bbox)),
            'output':'application/json','maxfeatures':50})
        features=result.get('features')
        if not isinstance(features,list) or len(features)>50:
            raise FactTransportError('PROVIDER_FORMAT')
        return features

    def search_parcel_address(self, address: str) -> dict:
        return self.fetch('https://api.vworld.kr/req/search', {
            'service':'search','request':'search','version':'2.0','format':'json',
            'size':10,'page':1,'query':address,'type':'ADDRESS',
            'category':'parcel','crs':'EPSG:4326'})

    def parcel_by_pnu(self, pnu: str) -> dict:
        if not re.fullmatch(r'\\d{19}',pnu):
            raise FactTransportError('INVALID_PNU')
        return self.fetch('https://api.vworld.kr/req/data',{
            'service':'data','request':'getfeature','version':'2.0',
            'format':'json','size':10,'page':1,'geometry':'true',
            'attribute':'true','crs':'EPSG:4326','data':'LP_PA_CBND_BUBUN',
            'attrfilter':f'pnu:=:{pnu}'})
