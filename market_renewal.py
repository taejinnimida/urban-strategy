"""R96 market facts and independent server-side screening.

Registry addresses and commercial-area representative points are discovery evidence,
never legal market boundaries. Vacancy is deliberately not a decision gate.
"""
import json
import re
import time
import threading
from datetime import date, datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor

import requests
from pyproj import Transformer
from shapely.geometry import shape, Point
from shapely.ops import transform

BASIS = '서울특별시 시장정비사업 추진계획 수립·운영기준(2025.6.25.)'
REGISTRY_URL = 'https://news.seoul.go.kr/economy/archives/574563'
POP_URL = 'https://data.seoul.go.kr/dataList/OA-15568/S/1/datasetView.do'
FACILITY_GROUPS = {
 '교통시설': ['도로','철도','항만','공항','주차장','자동차정류장','궤도','차량 검사 및 면허시설'],
 '공간시설': ['광장','공원','녹지','유원지','공공공지'],
 '유통·공급시설': ['유통업무설비','수도공급설비','전기공급설비','가스공급설비','열공급설비','방송·통신시설','공동구','시장','유류저장 및 송유설비'],
 '공공·문화체육시설': ['학교','공공청사','문화시설','공공필요성이 인정되는 체육시설','연구시설','사회복지시설','공공직업훈련시설','청소년수련시설'],
 '방재시설': ['하천','유수지','저수지','방화설비','방풍설비','방수설비','사방설비','방조설비'],
 '보건위생시설': ['장사시설','도축장','종합의료시설'],
 '환경기초시설': ['하수도','폐기물처리 및 재활용시설','빗물저장 및 이용시설','수질오염방지시설','폐차장'],
}
ALIASES = {'자동차정류장':['버스터미널','자동차정류장','공영차고지','물류터미널','환승센터'], '차량 검사 및 면허시설':['자동차검사시설','운전학원','면허시험장'], '공공필요성이 인정되는 체육시설':['체육시설'], '방송·통신시설':['방송시설','통신시설'], '유류저장 및 송유설비':['유류저장','송유설비'], '장사시설':['묘지','봉안','화장시설','장례식장'], '폐기물처리 및 재활용시설':['폐기물처리','재활용시설'], '빗물저장 및 이용시설':['빗물저장']}
LAYERS = dict(zip(['151','152','153','154','155','156','157','158'], ['교통시설','교통시설','공간시설','유통·공급시설','공공·문화체육시설','방재시설','보건위생시설','환경기초시설']))

def facility_type(layer, properties):
    """Classify present type fields first; do not mine historical remarks."""
    p = {str(k).lower(): str(v or '') for k,v in properties.items()}
    group = LAYERS.get(str(layer)[-3:], '기타 기반시설')
    for keys in [('fclty_ty_nm','fclty_type_nm','type_nm','kind_nm'), ('uname','fclty_nm','dgm_nm')]:
        text = ' '.join(p.get(k,'') for k in keys)
        hits = []
        for label in FACILITY_GROUPS.get(group, []):
            terms = [label]+ALIASES.get(label,[])
            # '시장' in an unrelated named facility is not sufficient outside supply layers.
            if any(term in text for term in terms): hits.append(label)
        if len(hits)==1: return {'group':group,'type':hits[0],'status':'IDENTIFIED'}
    if str(layer).endswith('151'): return {'group':group,'type':'도로','status':'IDENTIFIED'}
    return {'group':group,'type':'세부종류 미확인','status':'UNKNOWN'}

def norm(s): return re.sub(r'[\s()·ㆍ,\-]', '', str(s or ''))
def address_key(s):
    # Keep the lot-number boundary: 12 must never match 123 or 12-1.
    m = re.search(r'([가-힣0-9]+(?:동|가|리))\s*(산\s*)?(\d+)(?:-(\d+))?(?:\s|$|\()', str(s or ''))
    district=re.search(r'([가-힣]+구)(?:\s|$)', str(s or ''))
    return (district[1] if district else '',m[1],bool(m[2]),int(m[3]),int(m[4] or 0)) if m else None

def registry_matches(addresses, facility_names, area_names):
    payload=json.loads(Path(__file__).with_name('market_registry.json').read_text())
    addresses={address_key(a) for a in addresses if address_key(a)}
    names={norm(n) for n in facility_names+area_names if n}
    out=[]
    for values in payload['rows']:
        r=dict(zip(payload['columns'],values))
        if r['type'] not in ('등록시장','인정시장'): continue
        by_address=address_key(r['jibun_address']) in addresses
        by_name=norm(r['name']) in names
        if by_address or by_name:
            out.append({k:r[k] for k in ('name','type','district','road_address','jibun_address','form')} | {'match':'대표지번 일치' if by_address else '시설·상권명 일치','boundary_status':'UNCONFIRMED'})
    return {'as_of':payload['as_of'],'source':REGISTRY_URL,'items':out,'status':'MATCHED' if out else 'NO_MATCH','note':'대표주소·명칭 대조 결과. 미검색은 시장 부존재 판정이 아니며 법정 등록·인정 경계와 인접지역을 구분해야 합니다.'}

_cache={}
_lock=threading.Lock()
def seoul_rows(key, service, period='', deadline=None):
    if service not in ('TbgisTrdarRelm','VwsmTrdarFlpopQq'): raise ValueError('Unsupported service')
    ck=(service,str(period))
    with _lock:
        entry=_cache.get(ck)
        if entry and time.monotonic()-entry[0]<21600: return entry[1]
    if not key: raise ValueError('서울 열린데이터 연결 설정 필요')
    result=[]
    for start in range(1,10001,1000):
        if deadline and time.monotonic()>deadline: raise ValueError('유동인구 조회시간 초과')
        url=f'http://openapi.seoul.go.kr:8088/{quote(key,safe="")}/json/{service}/{start}/{start+999}/{period}'
        try:
            resp=requests.get(url,timeout=(5,12)); resp.raise_for_status(); data=resp.json()
        except Exception:
            # requests exceptions include credential-bearing URLs. Never propagate them.
            raise ValueError('서울 열린데이터 응답 실패') from None
        block=data.get(service,{})
        code=(block.get('RESULT') or data.get('RESULT') or {}).get('CODE')
        if code=='INFO-200': break
        if code!='INFO-000': raise ValueError('서울 열린데이터 인증·서비스 응답 확인 필요')
        rows=block.get('row',[]); result.extend(rows)
        if len(result)>=int(block.get('list_total_count',0)): break
        if not rows: raise ValueError('서울 열린데이터 페이지 누락')
    else: raise ValueError('서울 열린데이터 조회 한도 초과')
    if result:
        with _lock: _cache[ck]=(time.monotonic(),result)
    return result

def population_facts(geometry,key):
    """Only representative-point proximity; no fabricated boundary polygon."""
    out={'status':'UNKNOWN','source':POP_URL,'items':[],'note':'상권 대표점 인근 참고 통계. 시장 경계 또는 대상지 인구로 환산하지 않으며 3년 감소율은 동분기 참고 비교입니다.'}
    if not key: return out | {'message':'서울 열린데이터 인증 설정 후 자동조회 가능'}
    deadline=time.monotonic()+65
    try:
        areas=seoul_rows(key,'TbgisTrdarRelm',deadline=deadline)
        project=Transformer.from_crs(4326,5181,always_xy=True).transform
        site=transform(project,shape(geometry)); nearby=[]
        for r in areas:
            try: point=Point(float(r['XCNTS_VALUE']),float(r['YDNTS_VALUE']))
            except (KeyError,TypeError,ValueError): continue
            dist=site.distance(point)
            if dist<=500: nearby.append((dist,r))
        nearby.sort(key=lambda x:x[0]); nearby=nearby[:8]
        if not nearby: return out | {'status':'NO_NEARBY_POINT','message':'500m 이내 상권 대표점 없음 · 상권 경계 미조회'}
        today=datetime.now(ZoneInfo('Asia/Seoul')).date(); quarter=(today.month-1)//3+1
        current=[]; latest=''
        for back in range(0,5):
            idx=today.year*4+quarter-1-back; y,q=divmod(idx,4); latest=f'{y}{q+1}'
            current=seoul_rows(key,'VwsmTrdarFlpopQq',latest,deadline)
            if current: break
        if not current: return out | {'message':'최근 5개 분기 자료 미확보'}
        periods=[str(int(latest)-30),str(int(latest)-20),str(int(latest)-10),latest]
        # Fetch 1/2/3-year same-quarter values; cache on the server across sites.
        with ThreadPoolExecutor(max_workers=3) as pool:
            prior=list(pool.map(lambda p:seoul_rows(key,'VwsmTrdarFlpopQq',p,deadline),periods[:-1]))
        lookup=[{str(r.get('TRDAR_CD')):r for r in rows} for rows in prior+[current]]
        for distance,r in nearby:
            code=str(r.get('TRDAR_CD')); series=[]
            for period,rows in zip(periods,lookup):
                row=rows.get(code); value=None
                if row and norm(row.get('TRDAR_CD_NM'))==norm(r.get('TRDAR_CD_NM')):
                    try: value=float(row['TOT_FLPOP_CO'])
                    except (KeyError,TypeError,ValueError): pass
                series.append({'period':period,'value':value})
            base,last=series[0]['value'],series[-1]['value']
            decline=(base-last)/base*100 if base is not None and base>0 and last is not None else None
            out['items'].append({'code':code,'name':r.get('TRDAR_CD_NM',''),'type':r.get('TRDAR_SE_CD_NM',''),'district':r.get('SIGNGU_CD_NM',''),'distance_m':round(distance),'series':series,'decline_pct':round(decline,2) if decline is not None else None,'comparison_status':'REFERENCE_ONLY','boundary_verified':False})
        out.update(status='AVAILABLE',message='대표점 500m 이내 상권 · 동분기 4개 시점 조회',periods=periods)
    except ValueError as e: out['message']=str(e)
    except Exception: out['message']='유동인구 자료 처리 실패 · 다음 조회에서 재시도'
    return out

def age_fact(records,summary,as_of=None):
    today=as_of or datetime.now(ZoneInfo('Asia/Seoul')).date()
    try: cutoff=today.replace(year=today.year-30)
    except ValueError: cutoff=today.replace(year=today.year-30,day=28)
    seen=set(); old=0; unknown=0; clean=[]
    for r in records:
        if '전유' in str(r.get('register_kind','')) or '총괄' in str(r.get('register_kind','')): continue
        pk=r.get('mgm_bldrgst_pk') or r.get('id')
        if pk and pk in seen: continue
        if pk: seen.add(pk)
        clean.append(r)
        raw=re.sub(r'\D','',str(r.get('approval_date') or ''))
        try:
            if len(raw)!=8: raise ValueError()
            dt=datetime.strptime(raw,'%Y%m%d').date()
            if dt>today: raise ValueError()
            old+=int(dt<=cutoff)
        except ValueError: unknown+=1
    total=max(len(clean),int(summary.get('spatial_building_count') or 0))
    unknown+=total-len(clean)
    lower=100*old/total if total else None
    upper=100*(old+unknown)/total if total else None
    complete=summary.get('query_complete') is True and not summary.get('failed_pnu_count')
    status='PASS' if complete and lower is not None and lower>=60 else 'FAIL' if complete and upper is not None and upper<60 else 'REVIEW'
    value=f'{old}/{total}동 · {lower:.1f}~{upper:.1f}%' if total else '건축물자료 미확보'
    return {'status':status,'value':value,'count':old,'total':total,'unknown':unknown,'calculated_lower_pct':lower,'calculated_upper_pct':upper,'threshold_pct':60,'as_of':today.isoformat(),'label':'시장정비 30년 이상 건축물','source_document':BASIS,'source_type':'서울시 운영기준','source_effective':'2025-06-25','source_locator':'2-2-1','scope':'선택 사업구역','legal_market_scope_verified':False,'note':'선택구역 참고 산정. 시장 법정 경계 내 건축물과 인접지역 분리 확인 필요. 안전등급 D등급 이상(운영기준 문구)의 대체경로는 별도 진단자료 확인.'}

PLAN_ROWS=[
 ['일반주거·준공업 용적률','400% 이하','3-5-1'],
 ['준주거 용적률','450% 이하 / 심의 시 500% 이하','3-5-1'],
 ['상업지역 용적률','용도지역·해당 지구단위계획 및 상한용적률 항목 적용','3-5-3'],
 ['인접지역','전통시장 부분에만 특례 적용. 인접지역은 해당 용적률 적용 후 가중평균','3-5-2'],
 ['용도지역 상향 공공기여','서울시 지구단위계획 수립기준 준용 · 변경 전후 용도지역에 따라 산정','3-9-1'],
 ['시설 폐지 공공기여','폐지 시설면적 3천㎡ 미만 5% / 3천~5천㎡ 미만 10% / 5천㎡ 이상 20~35%(사전협상기준). 관리부서 현행기준 연동 여부 확인','3-9-2'],
 ['시설 폐지 기여와 인센티브','시설 폐지 공공기여는 상한용적률 산정에서 제외. 종상향 기여와 기계적 합산 금지','3-9-2'],
 ['매장 확보','구역 3천㎡ 이상: 매장 3천㎡ 이상. 3천㎡ 미만 주거·공업지역: 1천~3천㎡ 미만 범위에서 구역면적 이상','3-8-3'],
 ['상인 보호','임시시장 또는 영업손실 보전, 재입점·우선분양·임대료 할인 등 보호대책','3-7'],
 ['계획 확정','용적률은 자동 부여값이 아님. 현행 조례·지구단위계획·연동기준 및 심의 확인','3-2, 3-5, 3-9'],
]

def evaluate(payload,key='',population=None):
    age=age_fact(payload.get('buildings',[]),payload.get('building_summary',{}))
    facilities=[]
    for f in payload.get('facilities',[]):
        typ=facility_type(f.get('layer',''),f.get('properties',{}))
        if typ['type']=='시장' and float(f.get('area_m2') or 0)>0.5:
            facilities.append({'name':f.get('name') or '시장','area_m2':f.get('area_m2'),'type':typ['type']})
    pop=population if population is not None else population_facts(payload['geometry'],key)
    registry=registry_matches(payload.get('addresses',[]),[f['name'] for f in facilities],[r['name'] for r in pop.get('items',[]) if r['distance_m']==0 and r.get('type')=='전통시장'])
    plans=[p for p in payload.get('plans',[]) if float(p.get('area_m2') or 0)>0.5]
    market=bool(registry['items']); hints=market or bool(facilities)
    if market and age['status']=='PASS':
        overall='CONDITIONAL'; label='추진 검토 가능'
        route='도시관리계획 구역 완화 검토' if plans else '일반 경로 · 공실 확인 전제'
        note='시장 법정 경계·대상 건축물 범위, 구청장 필요인정 및 제외사유 확인' if plans else '공실률 30% 이상 확인 전제 · 시장 법정 경계·제외사유 확인'
    else:
        overall='REVIEW'; label='시장 자격·자료 확인' if hints else '시장 해당 여부 확인'
        route='안전등급·재해훼손 등 별도 경로 확인' if age['status']=='FAIL' else '시장 자격·노후도 확인'
        note='공실자료 미확보는 이 상태의 원인이 아닙니다. 등록·인정 경계, 노후도 또는 안전·재해 자료가 필요합니다.'
    def row(item,rule,value,status='INFO',required=False):
        return {'item':item,'rule':rule,'value':value,'status':status,'required':required,'sourceType':'서울시 운영기준','sourceTitle':BASIS,'sourceEffective':'2025-06-25','sourceLocator':'2-2-1','sourceVerified':True,'trustLabel':'공식기준·초기검토','note':''}
    rows=[row('시장 자격','전통시장법 제31조 및 운영기준 2-2-3',' / '.join(r['name']+'('+r['type']+')' for r in registry['items']) or '명부 일치시장 미확인','INFO'),row('노후도','준공 30년 이상 건축물 60% 이상 또는 안전등급 D등급 이상(원문)',age['value']+' · 선택구역 참고',age['status']),row('일반 경로','노후·안전 / 유동인구 감소 / 공실 중 2항목','유동인구는 상권 참고값, 공실은 발주처 확인'),row('완화 경로','도시관리계획 결정구역 내 시장 + 노후·안전 + 구청장 필요인정',' / '.join(p['name'] for p in plans) or '지단·재촉 중첩 미확인'),row('공실률','점포수 대비 공실 점포수 30% 이상','발주처 확인 · 자동판정 집계 제외'),row('재해훼손 경로','시장 기능 수행·수리 회복 곤란','재해·훼손 증빙에 따라 별도 검토'),row('제외·추진 전제','운영기준 2-2-2, 2-4','기존 인허가·구청장 제한 판단·동의 확보 등 확인')]
    rows[4].update(policyRole='CLIENT_CHECK',decision_excluded=True)
    zone=payload.get('zoning','')
    far='400% 이하' if ('일반주거' in zone or '준공업' in zone) else '450% 이하 · 심의 500% 이하' if '준주거' in zone else '해당 용도지역·지구단위계획 확인'
    return {'module':'market_renewal','overall':overall,'label':label,'candidate_possible':overall=='CONDITIONAL','legal_eligibility':'UNCONFIRMED','route':route,'note':note,'facts':{'registry':registry,'facilities':facilities,'facilities_complete':payload.get('facilities_complete',False),'age':age,'population':pop,'plans':plans,'plans_complete':payload.get('plans_complete',False)},'rows':rows,'density':{'zone':zone or '확인 필요','far':far,'contribution':'종상향·시설폐지 구분 / 현행 연동기준 확인'},'plan_rows':PLAN_ROWS,'basis':BASIS,'strengths':[route] if hints else [],'risks':[note],'followups':['공실 확인은 종합판정 REVIEW 사유에서 제외','시장 법정 경계 확인','공공기여 연동기준 확인']}
