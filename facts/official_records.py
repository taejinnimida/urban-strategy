"""Independent official land, individual-price, and building-title FACT collectors.

No Seoul application imports, no scheme-specific RULES, no local Seoul snapshots.
Network calls are bounded, no redirection, server-key only. Failed collection is
an error/unknown, never a made-up zero or statutory non-applicability.
"""
from __future__ import annotations

import json
import math
import os
import re
from urllib.parse import unquote
from xml.etree import ElementTree as ET

import requests
from facts.vworld import VWorldFacts, FactTransportError

LAND_LEDGER_URL = 'https://api.vworld.kr/ned/data/ladfrlList'
LAND_PRICE_URL = 'https://api.vworld.kr/ned/data/getIndvdLandPriceAttr'
LAND_CHARACTERISTICS_URL = 'https://api.vworld.kr/ned/data/getLandCharacteristics'
BUILDING_HUB_URL = 'https://apis.data.go.kr/1613000/BldRgstHubService/getBrTitleInfo'
PUBLIC_LEDGER_URL = 'https://apis.data.go.kr/1611000/nsdi/eios/LadfrlService/ladfrlList.xml'

MAX_BUILDING_ROWS = 2000
MAX_PAGES = 20


def validate_pnu(pnu):
    if not isinstance(pnu,str) or re.fullmatch(r'\d{19}',pnu) is None:
        raise FactTransportError('INVALID_PNU')
    return pnu


def _xml(payload):
    if isinstance(payload,bytes):
        if len(payload)>3_000_000:raise FactTransportError('PROVIDER_FORMAT')
    elif not isinstance(payload,str):
        raise FactTransportError('PROVIDER_FORMAT')
    try:
        root=ET.fromstring(payload)
    except (ET.ParseError, UnicodeError, ValueError):
        raise FactTransportError('PROVIDER_FORMAT') from None
    code=(root.findtext('.//resultCode') or '').strip()
    if code and code not in ('00','0','0000'):
        raise FactTransportError('PROVIDER_REJECTED')
    if root.find('.//error') is not None:
        raise FactTransportError('PROVIDER_REJECTED')
    return root


def _field(row,name):
    return str(row.findtext(name) or '').strip()


def _best(rows,*keys):
    return max(rows,key=lambda row:tuple(str(row.get(k) or '') for k in keys),default=None)


def parse_ledger(payload,pnu):
    validate_pnu(pnu)
    root=_xml(payload)
    rows=[]
    for el in root.findall('.//ladfrlVOList'):
        supplied=_field(el,'pnu')
        if supplied!=pnu:
            raise FactTransportError('PROVIDER_FORMAT')
        value=_field(el,'lndpclAr')
        try:
            area=float(value) if value else None
        except (ValueError,TypeError):area=None
        if area is not None and (not math.isfinite(area) or area<=0):area=None
        rows.append({
            'pnu':pnu, 'ldCodeNm':_field(el,'ldCodeNm'),
            'mnnmSlno':_field(el,'mnnmSlno'),
            'regstrSeCodeNm':_field(el,'regstrSeCodeNm'),
            'lndcgrCodeNm':_field(el,'lndcgrCodeNm'),
            'lndpclAr':area,
            'posesnSeCodeNm':_field(el,'posesnSeCodeNm'),
            'cnrsPsnCo':_field(el,'cnrsPsnCo') or None,
            'ladFrtlScNm':_field(el,'ladFrtlScNm'),
            'lastUpdtDt':_field(el,'lastUpdtDt'),
        })
    if len(rows)>=100:
        raise FactTransportError('SOURCE_TRUNCATED')
    return _best(rows,'lastUpdtDt')


def parse_characteristics(payload,pnu):
    validate_pnu(pnu)
    root=_xml(payload)
    rows=[]
    for el in root.findall('.//field'):
        returned=_field(el,'pnu')
        if returned != pnu:raise FactTransportError('PROVIDER_FORMAT')
        try:area=float(_field(el,'lndpclAr'))
        except (TypeError,ValueError):continue
        if not math.isfinite(area) or area<=0:continue
        rows.append({'pnu':pnu,'lndpclAr':area,
            'lndcgrCodeNm':_field(el,'lndcgrCodeNm') or _field(el,'lndcgrCode'),
            'stdrYear':_field(el,'stdrYear'),'stdrMt':_field(el,'stdrMt'),
            'lastUpdtDt':_field(el,'lastUpdtDt')})
    if len(rows)>=100:raise FactTransportError('SOURCE_TRUNCATED')
    return _best(rows,'stdrYear','stdrMt','lastUpdtDt')


def parse_price(payload,pnu,year):
    validate_pnu(pnu)
    if not isinstance(year,int) or year<1900 or year>2200:
        raise FactTransportError('INVALID_YEAR')
    root=_xml(payload)
    rows=[]
    for el in root.findall('.//field'):
        returned=_field(el,'pnu')
        if returned!=pnu:raise FactTransportError('PROVIDER_FORMAT')
        if _field(el,'stdrYear')!=str(year):continue
        try:price=float(_field(el,'pblntfPclnd'))
        except (TypeError,ValueError):continue
        if not math.isfinite(price) or price<=0:continue
        rows.append({'pnu':pnu,'year':year,'price_per_m2':price,
            'month':_field(el,'stdrMt'),'announcement_date':_field(el,'pblntfDe'),
            'last_update':_field(el,'lastUpdtDt')})
    if len(rows)>=100:raise FactTransportError('SOURCE_TRUNCATED')
    return _best(rows,'month','last_update')


def pnu_to_building_params(pnu):
    validate_pnu(pnu)
    if pnu[10] not in ('1','2'):
        raise FactTransportError('INVALID_PNU')
    return {'sigunguCd':pnu[:5], 'bjdongCd':pnu[5:10],
            'platGbCd':'0' if pnu[10]=='1' else '1',
            'bun':pnu[11:15],'ji':pnu[15:19]}


def parse_building_page(payload):
    """Normalized official building-HUB page: rows, declared totalCount."""
    if isinstance(payload,bytes):
        if len(payload)>3_000_000:raise FactTransportError('PROVIDER_FORMAT')
        try:
            if payload.lstrip().startswith(b'{'):
                payload=json.loads(payload.decode('utf-8'))
            else:
                payload=payload.decode('utf-8')
        except (UnicodeError,ValueError):
            raise FactTransportError('PROVIDER_FORMAT') from None
    if isinstance(payload,dict):
        response=payload.get('response') or {}
        header=response.get('header') or {}
        code=str(header.get('resultCode') or '')
        if code and code not in ('00','0000'):
            raise FactTransportError('PROVIDER_REJECTED')
        body=response.get('body') or {}
        try:total=int(body.get('totalCount') or 0)
        except (ValueError,TypeError):raise FactTransportError('PROVIDER_FORMAT') from None
        items=body.get('items') or {}
        entry=items.get('item') if isinstance(items,dict) else None
        rows=entry if isinstance(entry,list) else [entry] if isinstance(entry,dict) else []
    else:
        root=_xml(payload)
        try:total=int(root.findtext('.//totalCount') or 0)
        except ValueError:raise FactTransportError('PROVIDER_FORMAT') from None
        rows=[{part.tag.split('}')[-1]:(part.text or '').strip()
               for part in el} for el in root.findall('.//items/item')]
    if total<0 or total>100000 or len(rows)>MAX_BUILDING_ROWS or not all(isinstance(x,dict) for x in rows):
        raise FactTransportError('PROVIDER_FORMAT')
    return rows,total


def _public_xml(url,key,params,*,max_bytes=3_000_000):
    if not key:
        raise FactTransportError('SERVICE_NOT_READY')
    try:
        with requests.get(url,params={**params,'serviceKey':unquote(key)},
            timeout=(4,15),allow_redirects=False,stream=True,
            headers={'Accept':'application/xml,application/json'}) as response:
            if response.status_code!=200:
                raise FactTransportError('PROVIDER_REJECTED' if response.status_code in (401,403) else
                                         'RATE_LIMIT' if response.status_code==429 else
                                         'PROVIDER_HTTP_ERROR')
            blocks=[];n=0
            for block in response.iter_content(65536):
                n+=len(block)
                if n>max_bytes:raise FactTransportError('SOURCE_TRUNCATED')
                blocks.append(block)
            return b''.join(blocks)
    except requests.RequestException:
        raise FactTransportError('CONNECTION_ERROR') from None


class OfficialRecordFacts:
    def __init__(self, vworld=None, *, building_key=None):
        self.vworld = vworld if vworld is not None else VWorldFacts()
        self.building_key = building_key if building_key is not None else (
            lambda:os.getenv('BUILDING_HUB_API_KEY','').strip())

    def ledger(self,pnu):
        validate_pnu(pnu)
        errors=[]
        try:
            raw=self.vworld.fetch(LAND_LEDGER_URL,
                {'format':'xml','pnu':pnu},expected='xml')
            record=parse_ledger(raw,pnu)
            if record is not None:
                return {'record':record,'selected_source':'vworld_ladfrl'}
        except FactTransportError as exc:errors.append(exc.code)
        key=self.building_key()
        if key:
            try:
                raw=_public_xml(PUBLIC_LEDGER_URL,key,
                    {'pnu':pnu,'numOfRows':100})
                record=parse_ledger(raw,pnu)
                if record is not None:
                    return {'record':record,'selected_source':'data_go_ladfrl'}
            except FactTransportError as exc:errors.append(exc.code)
        # Characteristics are NOT full-coverage ledger; property title/ownership
        # must stay unknown, even when area and category were recovered.
        try:
            raw=self.vworld.fetch(LAND_CHARACTERISTICS_URL,
                {'format':'xml','pnu':pnu,'numOfRows':100,'pageNo':1},expected='xml')
            record=parse_characteristics(raw,pnu)
            if record is not None:
                return {'record':record,'selected_source':'vworld_land_characteristics'}
        except FactTransportError as exc:errors.append(exc.code)
        if errors:
            if all(x=='SERVICE_NOT_READY' for x in errors):
                raise FactTransportError('SERVICE_NOT_READY')
            raise FactTransportError('SOURCE_UNAVAILABLE')
        return {'record':None,'selected_source':None}

    def price(self,pnu,year):
        validate_pnu(pnu)
        raw=self.vworld.fetch(LAND_PRICE_URL,
            {'format':'xml','pnu':pnu,'stdrYear':str(year),'numOfRows':20},
            expected='xml')
        result=parse_price(raw,pnu,year)
        if result is not None:result['_source_type']='VWORLD_LIVE'
        return result

    def building_titles(self,pnu):
        base=pnu_to_building_params(pnu)
        key=self.building_key()
        if not key:
            raise FactTransportError('SERVICE_NOT_READY')
        rows=[]
        for page in range(1,MAX_PAGES+1):
            payload=_public_xml(BUILDING_HUB_URL,key,
                {**base,'numOfRows':100,'pageNo':page})
            batch,total=parse_building_page(payload)
            rows.extend(batch)
            if len(rows)>MAX_BUILDING_ROWS:
                raise FactTransportError('SOURCE_TRUNCATED')
            if len(rows)>=total:
                return rows
            if not batch:
                raise FactTransportError('SOURCE_TRUNCATED')
        raise FactTransportError('SOURCE_TRUNCATED')
