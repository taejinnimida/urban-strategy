"""Independent data-only provider for the urban survey application.

Do NOT import the Seoul redevelopment app or its internal data functions.
Unsupported sources fail closed as NOT_IMPLEMENTED. They are never inferred
from Seoul analysis or converted into zero/absence.
"""
import os
import re
from urllib.parse import urljoin, urlparse, parse_qsl, urlunparse
from xml.etree import ElementTree as ET
from facts.vworld import VWorldFacts, vworld_key, vworld_domain, FactTransportError


class SurveySourceUnavailable(RuntimeError):
    def __init__(self, source):
        self.code='SOURCE_NOT_IMPLEMENTED'
        super().__init__('SURVEY_SOURCE_NOT_IMPLEMENTED: '+source)


class _FetchedResponse:
    """Adapter matching the existing survey-only collectors, not the Seoul app."""
    status_code=200

    def __init__(self, payload):
        self.payload=payload
        self.content=payload if isinstance(payload, bytes) else b''
    def json(self):
        if isinstance(self.payload,bytes):
            raise ValueError('not JSON')
        return self.payload
    def raise_for_status(self):
        pass
    def close(self):
        pass


class SurveySources:
    """Survey service provider: facts only, no business-rule or Seoul app state."""
    VWORLD_SEARCH_URL='https://api.vworld.kr/req/search'
    VWORLD_LAND_URL='https://api.vworld.kr/ned/data/getLandCharacteristics'
    VWORLD_LAND_USE_URL='https://api.vworld.kr/ned/data/getLandUseAttr'
    ECVAM_WMS_URL='https://ecvam.neins.go.kr/apicall.do'

    def __init__(self, facts=None):
        self.facts=facts if facts is not None else VWorldFacts()

    def _vworld_key(self):
        return vworld_key()

    def _vworld_domain(self):
        return vworld_domain()

    def _vworld_referer(self):
        return 'https://'+self._vworld_domain()+'/'

    def _vworld_get(self, url, params, timeout=None, force_retry=False, **kwargs):
        if force_retry:
            # Independent survey transport does not share Seoul circuit-breaker
            # state. A force request is just a normal, bounded independent read.
            pass
        fmt='xml' if str(params.get('format','')).lower()=='xml' else 'json'
        payload=self.facts.fetch(url,params,expected=fmt)
        return _FetchedResponse(payload),'survey_direct'

    @staticmethod
    def _vworld_pnu_from_search_item(item):
        for raw in (item.get('id'),item.get('pnu')):
            m=re.search(r'(?<!\d)(\d{19})(?!\d)',str(raw or ''))
            if m:return m.group(1)
        return None

    def _vworld_parcel_by_pnu(self,pnu,force_retry=False):
        result=self.facts.parcel_by_pnu(pnu)
        response=result.get('response') or {}
        if response.get('status')=='NOT_FOUND':
            return {'status':'not_found','feature':None,'pnu':pnu}
        if response.get('status')!='OK':
            raise FactTransportError('PROVIDER_REJECTED')
        features=(((response.get('result') or {}).get('featureCollection') or {}).get('features') or [])
        if not isinstance(features,list) or len(features)>=10:
            raise FactTransportError('PROVIDER_FORMAT')
        for feature in features:
            if not feature or not feature.get('geometry'):
                continue
            props=dict(feature.get('properties') or {})
            returned=str(props.get('pnu') or props.get('PNU') or '')
            if returned and returned!=pnu:
                continue
            if not returned:
                props['pnu']=pnu
                feature={**feature,'properties':props}
            return {'status':'resolved','feature':feature,'pnu':pnu}
        return {'status':'not_found','feature':None,'pnu':pnu}

    def _land_use_rows_for_pnu(self,pnu):
        if not re.fullmatch(r'\d{19}',str(pnu)):
            raise FactTransportError('INVALID_PNU')
        payload=self.facts.fetch(self.VWORLD_LAND_USE_URL,{
            'format':'xml','pnu':pnu,'numOfRows':1000},expected='xml')
        try:
            root=ET.fromstring(payload)
        except ET.ParseError:
            raise FactTransportError('PROVIDER_FORMAT') from None
        code=(root.findtext('.//resultCode') or '').strip()
        if code and code not in ('0','00'):
            raise FactTransportError('PROVIDER_REJECTED')
        result=[]
        for field in root.findall('.//field'):
            row={el.tag.split('}')[-1]:el.text for el in field}
            if str(row.get('pnu') or '')!=pnu:
                raise FactTransportError('PROVIDER_FORMAT')
            result.append(row)
        if len(result)>=1000:
            raise FactTransportError('SOURCE_TRUNCATED')
        return result

    # Each source is built and verified independently in subsequent survey work.
    # Fail closed rather than borrow Seoul operational query functions.
    def _resolve_land_ledger(self,pnu):
        raise SurveySourceUnavailable('land_ledger')
    def _resolve_individual_land_price(self,pnu,year,timeout=8):
        raise SurveySourceUnavailable('individual_land_price')
    def _query_building_hub_title(self,pnu):
        raise SurveySourceUnavailable('building_hub_title')

    def _ecvam_key(self):
        return os.getenv('ECVAM_API_KEY','').strip()

    @staticmethod
    def _ecvam_wms_url_from_bootstrap(source):
        for raw in re.findall(r'["\x27]([^"\x27]*apicall\.do[^"\x27]*)["\x27]',str(source).replace('\\/','/'),flags=re.I):
            candidate=urljoin('https://ecvam.neins.go.kr/',raw)
            parsed=urlparse(candidate)
            if (parsed.scheme=='https' and parsed.hostname=='ecvam.neins.go.kr'
                    and parsed.path.endswith('/apicall.do')):
                return candidate,'bootstrap'
        raise FactTransportError('PROVIDER_FORMAT')

    def _ecvam_request_target(self,ready):
        raw=str(ready.get('_wms_url') or self.ECVAM_WMS_URL)
        parsed=urlparse(raw)
        if parsed.scheme!='https' or parsed.hostname!='ecvam.neins.go.kr' or parsed.path!='/apicall.do':
            raise FactTransportError('INVALID_ENDPOINT')
        parameters={str(k):str(v) for k,v in parse_qsl(parsed.query,keep_blank_values=True)}
        parameters.setdefault('APIKEY',self._ecvam_key())
        parameters.setdefault('DOMAIN',self._vworld_domain())
        return urlunparse((parsed.scheme,parsed.netloc,parsed.path,'','','')),parameters
