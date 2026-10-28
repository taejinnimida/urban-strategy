"""Isolated R105 development routes; no changes to the Seoul rule engine."""
import json, math, re, secrets, threading, time
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from shapely.geometry import shape, box
from shapely.geometry.polygon import orient
from shapely.ops import unary_union
from pyproj import Geod
GEOD=Geod(ellps='WGS84')
LAYERS={'LP_PA_CBND_BUBUN':'연속지적',**{f'LT_C_UQ11{i}':n for i,n in enumerate(['도시지역','관리지역','농림지역','자연환경보전지역'],1)},**{f'LT_C_UPISUQ15{i}':f'도시계획시설 UQ15{i}' for i in range(1,10)}}
class Site(BaseModel):
    geometry:dict
class LayerQuery(Site):
    layer:str
class Address(BaseModel):
    address:str=Field(min_length=5,max_length=200)
def area(g):
    if g.is_empty:return 0.0
    if g.geom_type=='Polygon':return round(abs(GEOD.geometry_area_perimeter(orient(g,1))[0]),2)
    return round(sum(area(v) for v in getattr(g,'geoms',[])),2)
def validate(value):
    try:
        if len(json.dumps(value))>1500000:raise ValueError()
        g=shape(value);a,b,c,d=g.bounds
        if g.geom_type not in ('Polygon','MultiPolygon') or g.is_empty or not g.is_valid:raise ValueError()
        if not all(math.isfinite(v) for v in g.bounds) or not (124<=a<c<=132 and 33<=b<d<=39.5):raise ValueError()
        if area(box(*g.bounds))>10000000:raise HTTPException(422,'조회 사각범위는 10㎢ 이하로 설정하세요. 대규모 사업지 분할조회는 아직 지원하지 않습니다.')
        return g
    except HTTPException:raise
    except Exception:raise HTTPException(422,'대한민국 WGS84 좌표의 유효한 Polygon/MultiPolygon이 필요합니다.')
def register(app,service):
    router=APIRouter(prefix='/api/development');snapshots={};lock=threading.Lock()
    def jurisdiction(g):
        if not service._vworld_key():return 'UNKNOWN'
        try:
            r,_=service._vworld_get('https://api.vworld.kr/req/wfs',params={'key':service._vworld_key(),'domain':service._vworld_domain(),'service':'WFS','request':'GetFeature','version':'1.0.0','typename':'lt_c_adsido_info','srsname':'EPSG:4326','bbox':','.join(map(str,g.bounds)),'output':'application/json','maxfeatures':50},timeout=(4,20))
            r.raise_for_status();seoul=[];other=[]
            for f in r.json().get('features',[]):
                p={str(k).lower():str(v) for k,v in (f.get('properties') or {}).items()};v=shape(f['geometry'])
                if not v.is_valid:continue
                (seoul if p.get('ctp_kor_nm')=='서울특별시' or p.get('ctprvn_nm')=='서울특별시' or p.get('ctprvn_cd')=='11' else other).append(v)
            if seoul and unary_union(seoul).covers(g):return 'SEOUL'
            if other and unary_union(other+seoul).covers(g):return 'OUTSIDE_OR_MIXED'
        except Exception:pass
        return 'UNKNOWN'
    @router.get('/ui.js')
    def ui():return Response(Path(__file__).with_name('development-ui.js').read_text(),media_type='text/javascript',headers={'Cache-Control':'no-store'})
    @router.post('/snapshots')
    def create(data:Site):
        g=validate(data.geometry);p={'geometry':data.geometry,'area_m2':area(g),'region':jurisdiction(g),'created_at':time.time(),'expires_in':7200}
        with lock:
            for k in list(snapshots):
                if time.time()-snapshots[k]['created_at']>7200:del snapshots[k]
            if len(snapshots)>=100:raise HTTPException(503,'개발용 대상지 보관 한도입니다. 잠시 후 다시 시도하세요.')
            token=secrets.token_urlsafe(32);snapshots[token]=json.loads(json.dumps(p))
        return {'id':token,**p}
    @router.get('/snapshots/{token}')
    def get(token:str):
        with lock:
            p=snapshots.get(token)
            if not p or time.time()-p['created_at']>7200:raise HTTPException(404,'대상지가 만료되었습니다. 초기 화면에서 다시 확정하세요.')
            return json.loads(json.dumps(p))
    @router.post('/layer')
    def layer(data:LayerQuery):
        g=validate(data.geometry)
        if data.layer not in LAYERS:raise HTTPException(422,'지원하지 않는 레이어입니다.')
        base={'source':'VWorld','layer':data.layer,'name':LAYERS[data.layer],'queried_at':time.time()}
        try:
            fs,_=service._fetch_planning_layer_vworld(data.layer,data.geometry)
            intersections=[shape(f['geometry']).intersection(g) for f in fs if f.get('geometry')]
            return {**base,'status':'COMPLETE','features':fs,'overlap_area_m2':area(unary_union(intersections)) if intersections else 0}
        except Exception:return {**base,'status':'UNKNOWN','features':[],'message':'외부 공간자료 미확보. API 설정·서비스 상태 확인 후 재조회하세요.'}
    @router.post('/address')
    def address(data:Address):
        if not re.match(r'^(서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충청|충북|충남|전라|전북|전남|경상|경북|경남|제주)',data.address):raise HTTPException(422,'시·도부터 정식 지번주소를 입력하세요.')
        try:
            r,_=service._vworld_get(service.VWORLD_SEARCH_URL,params={'key':service._vworld_key(),'domain':service._vworld_domain(),'service':'search','version':'2.0','request':'search','format':'json','size':10,'page':1,'query':data.address,'type':'ADDRESS','category':'parcel','crs':'EPSG:4326'},timeout=(4,15))
            r.raise_for_status();p=r.json().get('response',{})
            if p.get('status') not in ('OK','NOT_FOUND'):raise ValueError()
            norm=lambda v:re.sub(r'\s+','',str(v or ''))
            hits=[it for it in (p.get('result') or {}).get('items',[]) if norm((it.get('address') or {}).get('parcel'))==norm(data.address)]
            if len(hits)!=1:return {'status':'not_found','feature':None,'message':'정확히 일치하는 주소가 없습니다. 정식 시·도명과 지번을 확인하세요.'}
            pnu=service._vworld_pnu_from_search_item(hits[0])
            if not pnu:raise ValueError()
            v=service._vworld_parcel_by_pnu(pnu,force_retry=True)
            return {'status':v.get('status'),'feature':v.get('feature'),'pnu':pnu,'address':data.address}
        except Exception:raise HTTPException(502,'주소자료 조회 실패. API 설정 또는 주소를 확인하세요.')
    @router.get('/wms/{kind}')
    def wms(kind:str,request:Request):
        groups={'cadastral':'lp_pa_cbnd_bubun','zoning':','.join(f'lt_c_uq11{i}' for i in range(1,5)),'facility':','.join(f'lt_c_upisuq15{i}' for i in range(1,10))}
        if kind not in groups:raise HTTPException(404)
        q={k.lower():v for k,v in request.query_params.items()}
        try:
            bounds=[float(v) for v in q['bbox'].split(',')];a,b,c,d=bounds;w=int(q.get('width',256));h=int(q.get('height',256))
            if not all(math.isfinite(v) for v in bounds) or not (13000000<=a<c<=15000000 and 3000000<=b<d<=5000000 and 1<=w<=1024 and 1<=h<=1024):raise ValueError()
        except Exception:raise HTTPException(422,'지도 요청범위를 확인하세요.')
        try:
            r,_=service._vworld_get('https://api.vworld.kr/req/wms',params={'key':service._vworld_key(),'domain':service._vworld_domain(),'service':'WMS','request':'GetMap','version':'1.1.1','srs':'EPSG:3857','bbox':','.join(map(str,bounds)),'width':w,'height':h,'layers':groups[kind],'styles':groups[kind],'format':'image/png','transparent':'TRUE'},timeout=(4,15))
            if r.status_code!=200 or not r.content.startswith(b'\x89PNG\r\n\x1a\n'):raise ValueError()
            return Response(r.content,media_type='image/png',headers={'Cache-Control':'private,maxage=120'})
        except Exception:raise HTTPException(503,'지도자료 미확보')
    app.include_router(router)
