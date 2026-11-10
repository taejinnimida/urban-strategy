"""Nationwide public-data collectors. No Seoul eligibility rules or invented defaults."""
import copy, io, json, math, os, re, time, zipfile
from pathlib import Path
from datetime import datetime
from xml.etree import ElementTree as ET
from urllib.parse import urlsplit
from collections import defaultdict
from shapely.geometry import shape, mapping, box
from shapely.ops import unary_union, transform
from pyproj import Transformer
from fastapi import HTTPException
from fastapi.responses import Response
import requests

EXTRA_LAYERS={**{f'LT_C_UQ{k}':n for k,n in [(121,'경관지구'),(123,'고도지구'),(124,'방화지구'),(125,'방재지구'),(126,'보호지구'),(128,'취락지구'),(129,'개발진흥지구'),(130,'특정용도제한지구')]},'LT_C_UD801':'개발제한구역','LT_C_UO301':'국가유산보호구역','LT_C_UPISUQ161':'지구단위계획구역'}
VECTOR_ITEMS={'5-2':[f'LT_C_UQ11{i}' for i in range(1,5)],'5-3':[f'LT_C_UQ{i}' for i in (121,123,124,125,126,128,129,130)],'5-4':['LT_C_UD801'],'5-5':['LT_C_UPISUQ151','LT_C_UPISUQ152'],'5-6':['LT_C_UPISUQ153'],'5-7':[f'LT_C_UPISUQ15{i}' for i in range(4,10)],'5-8':['LT_C_UPISUQ161'],'6-4':['LT_C_UO301']}
# These patterns organize returned names, not legal applicability. Preserve relation fields.
NED_ITEMS={'1-2':r'도시개발|택지개발|공공주택|산업단지|정비구역','5-3':r'지구','5-4':r'구역','5-8':r'지구단위계획','6-1':r'농업진흥|농업보호|초지|농지','6-2':r'산지|보안림|산림|백두대간','6-3':r'하천|홍수|방재|재해|급경사|산사태','6-4':r'문화재|문화유산|국가유산|역사문화|자연유산|천연기념물','6-5':r'교육환경|학교환경|절대보호|상대보호','6-6':r'상수원|수변구역|수질보전','6-7':r'접도구역|도로구역|도로법','6-8':r'.+'}
FILE_ITEMS={'1-2':'주변 개발사업 경계·명칭·사업기간(대상지 5km)','2-5':'토지특성 형상분류','2-6':'세분류 토지피복도','4-3':'생태자연도 전체 등급 원도형','4-4':'국토환경성평가 등급 원도형','4-5':'임상도 임상·영급·수종·경급','4-6':'유역·수계·홍수 관련 원도형','4-7':'기상청 관측소·기간별 기상통계','5-1':'관할 시군 도시기본계획 토지이용계획도','7-1':'건물별 법정 노후불량 검토자료와 근거','7-2':'사업별 호수 산정자료와 근거','7-3':'건축물 출입구·유효도로·폭원·접도 검토자료','7-4':'사업별 과소·부정형 필지 검토자료'}
FILE_ITEMS.update({'5-3':'용도지구 공식 결정도형','5-4':'용도구역 공식 결정도형','5-8':'지구단위계획 공식 결정도형',**{f'6-{i}':v for i,v in enumerate(['농지·초지 규제 경계','산지구분·산지규제 경계','하천·방재구역 경계','국가유산·보호구역 경계','절대·상대 교육환경보호구역 경계','상수원보호·수변구역 경계','도로·접도구역 경계','기타 공적규제 경계'],1)}})
OFFICIAL_LINKS={'2-6':'https://aid.mcee.go.kr/req/intro.do','4-3':'https://www.nie-ecobank.kr/opn/file/list.do','4-4':'https://ecvam.neins.go.kr/api/apiGuide.do','4-5':'https://map.forest.go.kr/forest/','4-7':'https://data.kma.go.kr/','4-1':'https://map.ngii.go.kr/','4-2':'https://map.ngii.go.kr/','4-8':'https://map.ngii.go.kr/'}

# Only fixed categories are recorded. Never keep exception text, URLs or keys.
ERROR_LABELS={
 'SERVICE_NOT_READY':'공공 API 인증 설정 필요','PROVIDER_REJECTED':'제공기관 인증 또는 요청 거부',
 'RATE_LIMIT':'제공기관 호출 한도 도달','TEMPORARILY_PAUSED':'다른 조회 처리 후 재시도 필요',
 'CONNECT_TIMEOUT':'제공기관 연결 시간 초과','READ_TIMEOUT':'제공기관 응답 시간 초과',
 'TIMEOUT':'공공자료 조회 시간 초과','TLS_ERROR':'제공기관 보안 연결 실패',
 'PROXY_ERROR':'중계 연결 실패','CONNECTION_ERROR':'제공기관 연결 실패',
 'PROVIDER_REDIRECT':'제공기관 응답 주소 변경 확인 필요','PROVIDER_HTTP_ERROR':'제공기관 HTTP 오류',
 'PROVIDER_FORMAT':'제공기관 응답 형식 오류','PROVIDER_UNREACHABLE':'제공기관 연결 실패',
 'PROVIDER_LAYER_MISSING':'요청 자료의 제공 여부 확인 필요',
 'VALIDATION_FAILED':'공간자료·속성 검증 실패','SOURCE_FILE_MISSING':'등록된 원자료 파일 없음',
 'REQUEST_FAILED':'공공자료 조회 처리 실패'}
PROVIDER_CODES=frozenset(('INCORRECT_KEY','INVALID_KEY','EXPIRED_KEY','UNAUTHORIZED_KEY',
 'INVALID_DOMAIN','INCORRECT_DOMAIN','INVALID_REQUEST','INVALID_PARAMETER','INVALID_RANGE',
 'INVALID_DATATYPE','INVALID_DATA','INVALID_SERVICE','INVALID_VERSION','INVALID_FORMAT',
 'INVALID_CRS','INVALID_SIZE','INVALID_PAGE','OVER_QUERY_LIMIT','LIMIT_EXCEEDED','QUOTA_EXCEEDED',
 'TOO_MANY_REQUESTS','LIMIT_REQUEST','UNKNOWN_ERROR','SERVICE_UNAVAILABLE','OTHER'))
STAGES=frozenset(('TRANSPORT','PROVIDER_RESPONSE','JSON','WMS_IMAGE','SOURCE_COLLECTION',
 'LAND_LEDGER','LAND_PRICE','BUILDING_LEDGER','LAND_CHARACTERISTICS','LAND_USE',
 'ECVAM_AUTH','ECVAM_CAPABILITIES','ECVAM_MAP','WEATHER','LOCAL_SOURCE','ECOBANK_WFS'))

class PublicDataError(RuntimeError):
    def __init__(self,code,stage='SOURCE_COLLECTION',http_status=None,provider_code=None):
        self.code=code if code in ERROR_LABELS else 'REQUEST_FAILED'
        self.stage=stage if stage in STAGES else 'SOURCE_COLLECTION'
        self.http_status=http_status if isinstance(http_status,int) and 100<=http_status<=599 else None
        self.provider_code=provider_code if provider_code in PROVIDER_CODES else None
        super().__init__(self.code)

def safe_failure(exc,stage='SOURCE_COLLECTION'):
    from development.nature.ecobank import EcoBankError
    if isinstance(exc,EcoBankError):
        return {k:v for k,v in {'error_code':exc.code,'stage':'ECOBANK_WFS','http_status':exc.http_status}.items() if v is not None}
    if isinstance(exc,PublicDataError):
        return {k:v for k,v in {'error_code':exc.code,'stage':exc.stage,'http_status':exc.http_status,'provider_code':exc.provider_code}.items() if v is not None}
    status=getattr(getattr(exc,'response',None),'status_code',None)
    if isinstance(exc,HTTPException):status=exc.status_code
    code='REQUEST_FAILED'
    if isinstance(exc,requests.ConnectTimeout):code='CONNECT_TIMEOUT'
    elif isinstance(exc,requests.ReadTimeout):code='READ_TIMEOUT'
    elif isinstance(exc,requests.Timeout):code='TIMEOUT'
    elif isinstance(exc,requests.exceptions.SSLError):code='TLS_ERROR'
    elif isinstance(exc,requests.exceptions.ProxyError):code='PROXY_ERROR'
    elif isinstance(exc,requests.ConnectionError):code='CONNECTION_ERROR'
    elif isinstance(exc,(json.JSONDecodeError,ET.ParseError)):code='PROVIDER_FORMAT'
    elif isinstance(exc,FileNotFoundError):code='SOURCE_FILE_MISSING'
    elif isinstance(exc,(ValueError,KeyError,TypeError)):code='VALIDATION_FAILED'
    if status in (401,403):code='PROVIDER_REJECTED'
    elif status==429:code='RATE_LIMIT'
    elif isinstance(status,int) and 300<=status<400:code='PROVIDER_REDIRECT'
    elif isinstance(status,int) and status>=400:code='PROVIDER_HTTP_ERROR'
    return {k:v for k,v in {'error_code':code,'stage':stage if stage in STAGES else 'SOURCE_COLLECTION','http_status':status if isinstance(status,int) and 100<=status<=599 else None}.items() if v is not None}

def source_failure(exc,stage='SOURCE_COLLECTION'):
    diagnosis=safe_failure(exc,stage)
    return {'status':'ERROR','detail':ERROR_LABELS[diagnosis['error_code']]+' · 자료 없음으로 판단하지 않습니다.',**diagnosis}

def acquisition_status():
    """Admin only; configuration presence is never a successful connection."""
    root=source_root()
    return {'official_files_manifest_present':(root/'manifest.json').is_file(),
            'weather_station_config_present':(root/'weather.json').is_file(),
            'weather_key_present':bool(os.environ.get('KMA_PUBLIC_API_KEY','').strip()),
            'ecobank_key_present':bool(os.environ.get('ECOBANK_API_KEY','').strip()),
            'local_only_items':sorted(FILE_ITEMS), 'dem_acquisition':'LOCAL_OFFICIAL_GRID_ONLY'}

def finite(v):
    try:
        n=float(v)
        return n if math.isfinite(n) else None
    except (TypeError,ValueError):return None

def safe_json(path,limit=30_000_000):
    if path.stat().st_size>limit:raise ValueError('file size')
    return json.loads(path.read_text(encoding='utf-8-sig'))

def source_root():return Path(os.environ.get('NATIONAL_DATA_DIR') or Path(__file__).with_name('national_data')).resolve()
def local_path(root,name):
    p=(root/str(name)).resolve()
    if not p.is_relative_to(root):raise ValueError('path')
    return p

def manifest_entry(item):
    root=source_root();path=root/'manifest.json'
    if not path.is_file():return None,root
    entries=safe_json(path,1_000_000).get('datasets',{})
    entry=entries.get(item)
    if not entry:return None,root
    # Provenance and geographic coverage are mandatory, even for an empty source.
    if not all(entry.get(k) for k in ('provider','source_url','as_of','file','coverage')):raise ValueError('metadata')
    u=urlsplit(entry['source_url'])
    if u.scheme!='https' or u.username or u.password or u.query:raise ValueError('source URL must not contain credentials/query')
    return entry,root

def official_features(entry,root):
    p=local_path(root,entry['file'])
    if p.suffix.lower()=='.geojson':
        doc=safe_json(p)
        if doc.get('type')!='FeatureCollection':raise ValueError('format')
        return doc['features'],entry.get('crs','EPSG:4326')
    if p.suffix.lower()=='.zip':
        import shapefile
        if p.stat().st_size>100_000_000:raise ValueError('archive size')
        with zipfile.ZipFile(p) as z:
            if sum(i.file_size for i in z.infolist())>200_000_000:raise ValueError('archive size')
            stems=[n[:-4] for n in z.namelist() if n.lower().endswith('.shp')]
            if len(stems)!=1:raise ValueError('one SHP per source')
            stem=stems[0]
            with shapefile.Reader(shp=io.BytesIO(z.read(stem+'.shp')),dbf=io.BytesIO(z.read(stem+'.dbf')),shx=io.BytesIO(z.read(stem+'.shx')),encoding=entry.get('encoding','cp949')) as reader:
                if reader.numRecords>100000:raise ValueError('regional source required')
                fs=[{'type':'Feature','geometry':r.shape.__geo_interface__,'properties':dict(zip([v[0] for v in reader.fields[1:]],r.record))} for r in reader.iterShapeRecords()]
            crs=entry.get('crs') or z.read(stem+'.prj').decode('utf-8-sig')
            return fs,crs
    raise ValueError('unsupported format')

def official_vector(item,geometry,area):
    entry,root=manifest_entry(item)
    if not entry:return {'status':'MISSING_SOURCE','error_code':'AUTOMATIC_ACQUISITION_NOT_IMPLEMENTED','acquisition':'OFFICIAL_FILE','detail':FILE_ITEMS.get(item,'공식 원자료')+' 자동 취득 미구현 · 공식 파일자료 등록 필요','source':None,'features':[]}
    target=shape(geometry)
    if item=='1-2':
        to=Transformer.from_crs(4326,5179,always_xy=True).transform;back=Transformer.from_crs(5179,4326,always_xy=True).transform
        target=transform(back,transform(to,target).buffer(5000))
    coverage=shape(entry['coverage'])
    if not coverage.is_valid or not coverage.covers(target):return {'status':'OUTSIDE_COVERAGE','detail':'공식 원자료의 수록 범위가 조회 범위를 덮지 못합니다.','source':entry['provider'],'features':[]}
    fs,crs=official_features(entry,root);convert=Transformer.from_crs(crs,4326,always_xy=True).transform
    out=[];parts=[];rows=[];label=entry.get('label_field');fields=entry.get('fields',{})
    if not label:raise ValueError('label field required')
    missing=0
    for f in fs:
        g=transform(convert,shape(f['geometry']))
        if not g.is_valid:raise ValueError('invalid source geometry')
        if not g.intersects(target):continue
        clip=g.intersection(target)
        props=f.get('properties') or {};name=str(props.get(label) or '분류 미확보')[:250]
        if name=='분류 미확보':missing+=1
        values={str(k):str(props.get(v) or '')[:250] for k,v in fields.items()}
        if item in ('4-4','4-5','4-6'):
            values['source_layer']=str(props.get(entry.get('layer_field')) or entry.get('layer_name') or item)[:100]
        a=area(clip);parts.append(clip);rows.append({'name':name,'area_m2':a,**values})
        if len(out)>=5000:raise ValueError('feature limit')
        out.append({'type':'Feature','geometry':mapping(clip),'properties':{'name':name,**values}})
    return {'status':'PARTIAL' if missing else 'DATA','detail':f'공식 원자료 교차 {len(out)}건 · 분류 누락 {missing}건 · 법정 판정 미적용','source':entry['provider'],'source_url':entry['source_url'],'as_of':entry['as_of'],'features':out,'rows':rows,'overlap_area_m2':area(unary_union(parts)) if parts else 0,'scope':'5km' if item=='1-2' else 'fixed_site'}

def dem(geometry,area):
    from development.nature.terrain_classes import new_accumulator, add_cell, finish
    entry,root=manifest_entry('dem')
    if not entry:return {'status':'MISSING_SOURCE','error_code':'AUTOMATIC_ACQUISITION_NOT_IMPLEMENTED','acquisition':'OFFICIAL_FILE','detail':'DEM 자동 취득 미구현 · 공식 DEM 격자 파일 등록 필요','source':None}
    target=shape(geometry)
    if not shape(entry['coverage']).covers(target):return {'status':'OUTSIDE_COVERAGE','detail':'DEM 수록범위 밖'}
    doc=safe_json(local_path(root,entry['file']));values=doc['values'];dx=finite(doc['cell_size_m']);x0=finite(doc['x0']);y0=finite(doc['y0'])
    if not dx or dx<=0 or x0 is None or y0 is None or not values or len(values)*len(values[0])>100000:raise ValueError('DEM grid')
    crs=doc['crs'];to=Transformer.from_crs(4326,crs,always_xy=True);back=Transformer.from_crs(crs,4326,always_xy=True)
    if not to.target_crs.is_projected or any(a.unit_name!='metre' for a in to.target_crs.axis_info[:2]):raise ValueError('DEM needs metre CRS')
    metric=transform(to.transform,target);height=len(values);width=len(values[0]);nodata=doc.get('nodata')
    if any(len(row)!=width for row in values):raise ValueError('grid shape')
    frame=box(x0,y0,x0+width*dx,y0+height*dx)
    if not frame.covers(metric):return {'status':'OUTSIDE_COVERAGE','detail':'DEM 격자가 대상지를 덮지 못합니다.'}
    def val(r,c):
        if not (0<=r<height and 0<=c<width):return None
        v=values[r][c]
        return None if v is None or v==nodata else finite(v)
    elevations=[];slopes=[];weight=0;slope_weight=0;missing=0;features=[]
    elev_acc=new_accumulator('elevation')
    slope_acc=new_accumulator('slope')
    elev_features=[];slope_features=[]
    show_limit=5000
    mapped_cells=0

    for r in range(height):
        for c in range(width):
            cell=box(x0+c*dx,y0+r*dx,x0+(c+1)*dx,y0+(r+1)*dx);clip=cell.intersection(metric)
            if clip.area<=0:continue
            z=val(r,c);a=clip.area
            if z is None:missing+=1;continue
            elevations.append((z,a));weight+=a
            ec=add_cell(elev_acc,z,a)
            if ec is not None and mapped_cells<show_limit:
                elev_features.append({'type':'Feature','geometry':mapping(transform(back.transform,clip)),
                                      'properties':{'name':ec[1],'class_code':ec[0],'color':ec[2]}})
            mapped_cells+=1
            ns=[val(r+rr,c+cc) for rr in (-1,0,1) for cc in (-1,0,1)]
            slope=None
            if all(v is not None for v in ns):
                gx=((ns[2]+2*ns[5]+ns[8])-(ns[0]+2*ns[3]+ns[6]))/(8*dx);gy=((ns[6]+2*ns[7]+ns[8])-(ns[0]+2*ns[1]+ns[2]))/(8*dx)
                slope=math.degrees(math.atan(math.hypot(gx,gy)));slopes.append((slope,a));slope_weight+=a
                sc=add_cell(slope_acc,slope,a)
                if sc is not None and mapped_cells<=show_limit:
                    slope_features.append({'type':'Feature','geometry':mapping(transform(back.transform,clip)),
                                           'properties':{'name':sc[1],'class_code':sc[0],'color':sc[2]}})
            if len(features)<5000:features.append({'type':'Feature','geometry':mapping(transform(back.transform,clip)),'properties':{'name':f'{z:.2f}m','elevation_m':z,'slope_deg':slope}})
    def stats(seq):return {'min':min(v for v,a in seq),'max':max(v for v,a in seq),'mean':sum(v*a for v,a in seq)/sum(a for v,a in seq)} if seq else None
    center=metric.representative_point();xmin,ymin,xmax,ymax=metric.bounds;profiles=[]
    for axis in ('동서','남북'):
        pts=[]
        for i in range(101):
            x=xmin+(xmax-xmin)*i/100 if axis=='동서' else center.x;y=center.y if axis=='동서' else ymin+(ymax-ymin)*i/100
            from shapely.geometry import Point
            inside=metric.covers(Point(x,y));z=val(math.floor((y-y0)/dx),math.floor((x-x0)/dx)) if inside else None
            pts.append({'distance_m':(xmax-xmin if axis=='동서' else ymax-ymin)*i/100,'elevation_m':z})
        profiles.append({'axis':axis,'points':pts})
    return {'status':'DATA' if missing==0 and weight>0 and slope_weight>=metric.area*(1-1e-9) else 'PARTIAL','detail':'공식 DEM 격자 기반. 경사는 Horn 3×3 도수(°), 면적가중 평균. 단면은 내부 대표점을 지나는 동서·남북 자동선의 최근접 격자 참고값.','source':entry['provider'],'source_url':entry['source_url'],'as_of':entry['as_of'],'resolution_m':dx,'elevation':stats(elevations),'slope':stats(slopes),'elevation_coverage_pct':100*weight/metric.area,'slope_coverage_pct':100*slope_weight/metric.area,'profiles':profiles,'features':features,'map_truncated':len(elevations)>5000,
            'elevation_bands':finish(elev_acc,metric.area),
            'slope_bands':finish(slope_acc,metric.area),
            # Never publish the first 5000 cells as if they were the full map.
            'elevation_features':elev_features if mapped_cells<=show_limit else [],
            'slope_features':slope_features if mapped_cells<=show_limit else []}

def fetch_ned(service,pnu):
    rows=service._land_use_rows_for_pnu(pnu)
    if len(rows)>=1000:raise ValueError('NED truncation')
    keep=['pnu','cnflcAt','cnflcAtNm','prposAreaDstrcCode','prposAreaDstrcCodeNm','manageNo','lastUpdtDt'];out=[]
    for row in rows:
        if str(row.get('pnu') or '')!=pnu:raise ValueError('PNU mismatch')
        out.append({k:str(row.get(k) or '')[:300] for k in keep})
    return {'status':'ATTRIBUTE_ONLY','source':'VWorld 토지이용계획정보','rows':out,'detail':'필지 전체의 관계코드·관계명을 보존한 참고자료. 부분 편입구역 해당·비해당 또는 중첩면적을 확정하지 않습니다.'}

def characteristics(service,pnu):
    if not service._vworld_key():return {'status':'MISSING_CREDENTIAL','detail':'VWorld 인증 설정 필요'}
    response,_=service._vworld_get(service.VWORLD_LAND_URL,params={'format':'xml','key':service._vworld_key(),'domain':service._vworld_domain(),'pnu':pnu,'numOfRows':100,'pageNo':1},timeout=8)
    try:
        response.raise_for_status()
        if len(response.content)>2_000_000:raise ValueError('response size')
        root=ET.fromstring(response.content)
    finally:response.close()
    code=root.findtext('.//resultCode')
    if code and code not in ('0','00'):raise ValueError('API error')
    rows=[]
    for el in root.findall('.//field'):
        if el.findtext('pnu')!=pnu:raise ValueError('PNU mismatch')
        row={k:el.findtext(k) for k in ('pnu','stdrYear','stdrMt','lastUpdtDt','tpgrphFrmCodeNm','tpgrphHgCodeNm','roadSideCodeNm','lndUseCodeNm')}
        rows.append(row)
    if len(rows)>=100:raise ValueError('possible truncation')
    rows.sort(key=lambda r:(r.get('stdrYear') or '',r.get('stdrMt') or '',r.get('lastUpdtDt') or ''),reverse=True)
    return {'status':'ATTRIBUTE_ONLY' if rows else 'NO_RECORD','detail':'형상·고저·도로접면 원문. 최신 반환 연도를 표시하며 법정 접도율로 환산하지 않습니다.','source':'VWorld 토지특성정보','record':rows[0] if rows else None}

def weather(geometry,as_of):
    path=source_root()/'weather.json'
    if not path.is_file():return {'status':'MISSING_SOURCE','error_code':'STATION_NOT_CONFIGURED','detail':'기상 API 조회 전 대상지 대표 관측소·좌표·적용범위 설정 필요'}
    cfg=safe_json(path,100000);target=shape(geometry)
    if not shape(cfg['coverage']).covers(target):return {'status':'OUTSIDE_COVERAGE','detail':'기상 관측소 적용범위 밖'}
    station=str(cfg['station_id'])
    if not re.fullmatch(r'[0-9]{1,4}',station):raise ValueError('station ID')
    lon,lat=float(cfg['lon']),float(cfg['lat'])
    if not 124<=lon<=132 or not 33<=lat<=39.5:raise ValueError('station coordinates')
    key=os.environ.get('KMA_PUBLIC_API_KEY','').strip()
    if not key:return {'status':'MISSING_CREDENTIAL','detail':'기상청 ASOS 일자료 서비스 활용승인 인증키 설정 필요'}
    year=datetime.fromisoformat(as_of).year-1
    with requests.get('https://apis.data.go.kr/1360000/AsosDalyInfoService/getWthrDataList',params={'serviceKey':key,'dataType':'JSON','numOfRows':999,'pageNo':1,'dataCd':'ASOS','dateCd':'DAY','startDt':f'{year}0101','endDt':f'{year}1231','stnIds':station},timeout=(4,20),allow_redirects=False) as response:
        response.raise_for_status()
        if len(response.content)>3_000_000:raise ValueError('weather size')
        payload=response.json()['response']
    if str(payload['header']['resultCode']) not in ('00','0'):raise ValueError('weather API error')
    body=payload['body'];rows=(body.get('items') or {}).get('item') or []
    if not isinstance(rows,list) or int(body.get('totalCount',0))!=len(rows):raise ValueError('incomplete weather response')
    seen=set();temps=[];rain=[];days=[]
    for row in rows:
        d=datetime.strptime(row['tm'],'%Y-%m-%d').date()
        if d.year!=year or str(row['stnId'])!=station or d in seen:raise ValueError('weather scope')
        seen.add(d);t=finite(row.get('avgTa'));r=finite(row.get('sumRn'))
        if t is not None and -80<t<60:temps.append(t)
        else:t=None
        if r is not None and r>=0:rain.append(r)
        else:r=None
        days.append({'date':str(d),'mean_temperature_c':t,'precipitation_mm':r})
    from pyproj import Geod
    center=target.representative_point();distance=Geod(ellps='WGS84').inv(center.x,center.y,lon,lat)[2]
    expected=(datetime(year+1,1,1)-datetime(year,1,1)).days
    return {'status':'PARTIAL' if len(rows)<expected or len(temps)<expected or len(rain)<expected else 'DATA','detail':f'{year}년 ASOS 관측 일자료. 관측소 대표성 별도 검토. 30년 기후평년값이 아니며 빈 강수량을 0으로 바꾸지 않음.','source':'기상청 ASOS','station_id':station,'station_name':str(cfg['station_name'])[:100],'station_distance_m':distance,'year':year,'expected_days':expected,'received_days':len(rows),'temperature_days':len(temps),'rain_days':len(rain),'mean_temperature_c':sum(temps)/len(temps) if temps else None,'observed_precipitation_sum_mm':sum(rain) if rain else None,'days':days}

def ecvam_map(service,geometry):
    # This collector requests the composite map only. It does not require six
    # unrelated Seoul law layers in the authentication bootstrap.
    key=service._ecvam_key()
    if not key:return {'status':'MISSING_CREDENTIAL','error_code':'SERVICE_NOT_READY','stage':'ECVAM_AUTH','detail':'국토환경성평가 API 인증 설정 필요'}
    referer=service._vworld_referer() if hasattr(service,'_vworld_referer') else service._vworld_domain().rstrip('/')+'/'
    headers={'Referer':referer,'User-Agent':'urban-strategy-development/105.8'}
    def get(url,params,stage):
        u=urlsplit(url)
        if u.scheme!='https' or u.hostname not in ('ecvam.neins.go.kr','webgis.neins.go.kr') or u.port not in (None,443) or u.username or u.password or u.path not in ('/apiConfirm.do','/apicall.do'):
            raise PublicDataError('VALIDATION_FAILED',stage)
        try:
            with requests.get(url,params=params,headers=headers,timeout=(4,15),allow_redirects=False,stream=True) as response:
                status=response.status_code
                if status!=200:
                    code='RATE_LIMIT' if status==429 else 'PROVIDER_REJECTED' if status in (401,403) else 'PROVIDER_REDIRECT' if 300<=status<400 else 'PROVIDER_HTTP_ERROR'
                    raise PublicDataError(code,stage,status)
                chunks=[];n=0
                for chunk in response.iter_content(65536):
                    n+=len(chunk)
                    if n>5_000_000:raise PublicDataError('PROVIDER_FORMAT',stage,200)
                    chunks.append(chunk)
                return b''.join(chunks)
        except requests.RequestException as exc:
            info=safe_failure(exc,stage);raise PublicDataError(info['error_code'],stage,info.get('http_status')) from None
    bootstrap=get('https://ecvam.neins.go.kr/apiConfirm.do',{'APIKEY':key},'ECVAM_AUTH').decode('utf-8',errors='replace')
    if 'ecvamLayerCreate' not in bootstrap:raise PublicDataError('PROVIDER_REJECTED','ECVAM_AUTH',200)
    wms_url,_=service._ecvam_wms_url_from_bootstrap(bootstrap)
    url,params=service._ecvam_request_target({'_wms_url':wms_url})
    try:caps=ET.fromstring(get(url,{**params,'SERVICE':'WMS','REQUEST':'GetCapabilities','VERSION':'1.1.1'},'ECVAM_CAPABILITIES'))
    except ET.ParseError:raise PublicDataError('PROVIDER_FORMAT','ECVAM_CAPABILITIES',200) from None
    names={e.text for e in caps.iter() if e.tag.split('}')[-1]=='Name'}
    layer=next((v for v in ('nem_ecvam','ecvam') if v in names),None)
    if not layer:raise PublicDataError('PROVIDER_LAYER_MISSING','ECVAM_CAPABILITIES',200)
    g=shape(geometry);bbox=g.bounds
    tf=Transformer.from_crs(4326,3857,always_xy=True)
    a,b=tf.transform(bbox[0],bbox[1]);c,d=tf.transform(bbox[2],bbox[3])
    image=get(url,{**params,'SERVICE':'WMS','REQUEST':'GetMap','VERSION':'1.1.1','SRS':'EPSG:3857','BBOX':','.join(map(str,(a,b,c,d))),'LAYERS':layer,'STYLES':'','WIDTH':800,'HEIGHT':600,'FORMAT':'image/png','TRANSPARENT':'TRUE'},'ECVAM_MAP')
    if not image.startswith(b'\x89PNG\r\n\x1a\n'):raise PublicDataError('PROVIDER_FORMAT','ECVAM_MAP',200)
    return {'status':'MAP_ONLY','detail':'국토환경성평가 공식 WMS 도면. 픽셀로 등급면적·법정 적합성을 산정하지 않습니다.','source':'한국환경연구원 국토환경성평가지도','source_url':OFFICIAL_LINKS['4-4'],'_image':image,'bounds':bbox}


def distribution(rows,field,area_field=None):
    groups=defaultdict(lambda:{'count':0,'area_m2':0.0});missing=0;nums=[]
    for row in rows:
        v=row.get(field)
        if v is None or v=='':missing+=1;continue
        key=str(v);groups[key]['count']+=1
        if area_field:
            a=finite(row.get(area_field))
            if a is not None:groups[key]['area_m2']+=a
        n=finite(v)
        if n is not None:nums.append(n)
    return {'field':field,'known':len(rows)-missing,'missing':missing,'total':len(rows),'groups':[{'name':k,**v} for k,v in sorted(groups.items())],'min':min(nums) if nums else None,'max':max(nums) if nums else None,'mean':sum(nums)/len(nums) if nums else None,'note':'원자료 값별 집계. 임의의 법정 구간·기준은 적용하지 않음.'}

def enrich(data,snapshot,area):
    facts=snapshot.get('_national_sources',{});layers=snapshot.get('_report_layers',{});results={};statistics={}
    land=data['land'];buildings=data['buildings'];land_fields={'2-1':'ownership','2-2':'category','2-3':'official_area','2-4':'price'}
    building_fields={f'3-{i}':v for i,v in enumerate(('use','structure','floors','age','floor_area','coverage','far'),1)}
    for item,field in {**land_fields,**building_fields}.items():
        st=distribution(land if item in land_fields else buildings,field,'overlap_area' if item in land_fields else None);statistics[item]=st
        results[item]={'status':('일부 집계자료' if st['missing'] else '집계자료 확보') if st['known'] else '미확보','detail':f"{st['known']}/{st['total']}건 확보 · 미확보 {st['missing']}건 · 원자료 값별 집계",'source':'토지대장·공시지가' if item in land_fields else '건축HUB'}
        if item=='2-1':results[item]['detail']+=' · 개인별 소유자 분석이 아닌 소유구분'
        if item in building_fields:results[item]['detail']+=' · 편입필지 관련 대장, 건물별 편입 미확인'
        if data.get('parcel_status')=='REFERENCE':
            results[item]['detail']+=' · 보조 지적 참고목록 기준이며 대상지 전체 분포를 확정하지 않음'
            if st['known']:results[item]['status']='참고목록 집계자료'
            st['scope']='REFERENCE_PARCELS'
    for item,ids in VECTOR_ITEMS.items():
        done=[layers[k] for k in ids if layers.get(k,{}).get('status') in ('COMPLETE','REFERENCE')];verified=sum(v.get('status')=='COMPLETE' for v in done);reference=len(done)-verified;parts=[];names=defaultdict(list);fs=[]
        for layer in done:
            for f in layer.get('features',[]):
                clip=shape(f['geometry']).intersection(shape(snapshot['geometry']))
                if area(clip)<=0:continue
                props={str(k).lower():v for k,v in (f.get('properties') or {}).items()};name=str(props.get('uname') or props.get('fclty_nm') or props.get('dgm_nm') or '명칭 미확보');parts.append(clip);names[name].append(clip);fs.append({'type':'Feature','geometry':mapping(clip),'properties':{'name':name}})
        a=area(unary_union(parts)) if parts else (0 if len(done)==len(ids) else None)
        statistics[item]={'overlap_area_m2':a,'overlap_pct':100*a/data['area_m2'] if a is not None and data['area_m2'] else None,'groups':[{'name':k,'area_m2':area(unary_union(v))} for k,v in names.items()],'note':'서로 다른 분류 간 중첩이 있어 분류별 면적 합계와 전체 union 면적은 다를 수 있음'}
        results[item]={'status':'보조 조회 참고자료' if reference else '공간자료 확보' if len(done)==len(ids) else '일부 확보' if done else '미확보','detail':f'{len(done)}/{len(ids)} 레이어 · 확정구역 교차면적 '+(f'{a:,.2f}㎡' if a is not None else '미확보'),'source':'VWorld 브라우저 보조 수신 포함(출처 재확인 필요)' if reference else 'VWorld','features':fs,'provider_verified':reference==0 and verified==len(ids)}
        if reference:results[item]['detail']+=f' · 서버 직접 {verified}개/보조 {reference}개 · 보조자료 원문 진위·수록 완전성 미검증, 법정 판정 미적용'
        if item in ('5-3','5-4','6-4'):results[item]['detail']+=' · 연결된 종류만 조회, 모든 지구·구역·유산 규제 포괄 아님'
    ned=[(p,facts.get('ned:'+p,{})) for p in snapshot.get('_report',{}).get('land',{})]
    for item,pattern in NED_ITEMS.items():
        checked=sum(v.get('status')=='ATTRIBUTE_ONLY' for p,v in ned);rows=[]
        for p,v in ned:
            for row in v.get('rows',[]):
                if re.search(pattern,row.get('prposAreaDstrcCodeNm','')):rows.append(row)
        existing=results.get(item,{})
        if checked:
            existing={**existing,'status':existing.get('status','필지 참고자료'),'detail':existing.get('detail','')+f' · 토지이용계획 {checked}/{len(ned)}필지 조회, 관련 행 {len(rows)}건. 관계코드 확인 필요; 음성으로 비해당 확정 금지.','source':existing.get('source','VWorld 토지이용계획정보'),'regulation_rows':rows}
            if item=='1-2':existing['detail']+=' 대상필지 속성이며 주변 5km 사업현황은 별도 원도형 필요.'
            results[item]=existing
    for item in FILE_ITEMS:
        f=facts.get('file:'+item)
        if not f:continue
        if f.get('status') in ('DATA','PARTIAL'):
            results[item]={'status':'공식 파일자료 확보' if f['status']=='DATA' else '일부 확보','detail':f['detail'],'source':f['source'],'features':f.get('features',[]),'as_of':f.get('as_of'),'source_url':f.get('source_url')};statistics[item]={'groups':f.get('rows',[]),'overlap_area_m2':f.get('overlap_area_m2')}
            if item.startswith('7-'):results[item]['status']='검토자료 확보';results[item]['detail']+=' · 자동 법정 판정은 적용하지 않음'
        elif item not in results:results[item]={'status':'자료 필요' if f.get('status')=='MISSING_SOURCE' else '미확보','detail':f.get('detail','조회 실패'),'source':f.get('source') or '공공 원자료','source_url':OFFICIAL_LINKS.get(item)}
    # API WFS is preferred for 4-3; registered source files remain a
    # fallback only when live API is not verified. Never replace an existing
    # complete official file with a failed or empty API response.
    eco=facts.get('ecobank',{})
    registered=facts.get('file:4-3',{})
    if eco.get('status') in ('DATA','PARTIAL'):
        eco_full=eco['status']=='DATA'
        results['4-3']={
            'status':'공간자료 확보' if eco_full else '일부 확보',
            'detail':eco['detail'],
            'source':eco['source'],
            'source_url':eco.get('source_url'),
            'features':eco.get('features',[]),
            'legend':eco.get('legend',[]),
            'thematic_type':'ecobank',
            'provider_verified':eco_full,
            'queried_at':eco.get('queried_at'),
            'as_of':eco.get('as_of'),
        }
        statistics['4-3']={
            'groups':eco.get('legend',[]),
            'mapped_groups':eco.get('legend',[]),
            'overlap_area_m2':eco.get('overlap_area_m2'),
            'mapped_area_m2':eco.get('overlap_area_m2'),
            'site_area_m2_epsg5186':eco.get('site_area_m2_epsg5186'),
            'unclassified_count':eco.get('unclassified_count'),
            'overlap_warning_m2':eco.get('overlap_warning_m2'),
            'source_layer':eco.get('source_layer'),
            'provider_calls':eco.get('provider_calls'),
            'note':'WFS 공간등급 참고자료. 최신 생태·자연도 고시 원본과의 일치 여부 별도 검증 필요.',
        }
    elif registered is None or registered.get('status') not in ('DATA','PARTIAL'):
        if eco:
            code=eco.get('status')
            results['4-3']={
                'status':'자료 필요' if code in ('MISSING_CREDENTIAL','EMPTY') else '미확보',
                'detail':eco.get('detail','생태·자연도 WFS 원자료 미확보'),
                'source':'국립생태원 EcoBank',
                'source_url':eco.get('source_url') or OFFICIAL_LINKS['4-3'],
                'error_code':eco.get('error_code'),
                'provider_verified':False,
            }
    terrain=facts.get('dem',{})
    for item,key in [('4-1','elevation'),('4-2','slope'),('4-8','profiles')]:
        got=terrain.get(key);results[item]={'status':'참고 분석자료' if got else '자료 필요','detail':terrain.get('detail','공식 DEM 자료 미조회'),'source':terrain.get('source') or '국토지리정보원 등 공공 DEM','source_url':OFFICIAL_LINKS[item]}
        if got:
            statistics[item]={'values':got,'resolution_m':terrain.get('resolution_m'),
                              'elevation_coverage_pct':terrain.get('elevation_coverage_pct'),
                              'slope_coverage_pct':terrain.get('slope_coverage_pct')}
            if item in ('4-1','4-2'):
                band=terrain.get('elevation_bands' if item=='4-1' else 'slope_bands') or {}
                features=terrain.get('elevation_features' if item=='4-1' else 'slope_features') or []
                statistics[item]['bands']=band
                results[item]['features']=features
                results[item]['legend']=band.get('groups') or []
                results[item]['thematic_type']='nature_dem'
                results[item]['provider_verified']=bool(terrain.get('status') in ('DATA','PARTIAL') and not terrain.get('map_truncated') and features and len(features)<=5000 and band.get('coverage_pct',0)>0)
                if terrain.get('status')=='PARTIAL':
                    results[item]['detail']+=' · 부분 확보 DEM: 결측면적은 별도 표기하며 전체 구역계 분포로 확정하지 않음'
                if terrain.get('map_truncated'):
                    results[item]['detail']+=' · 등급별 전체 통계만 확보, 지도용 DEM 셀 5,000개 상한 초과로 주제도 보류'
            else:
                statistics[item]['profile_data_source']='DEM'
                results[item]['features']=[]
    e=facts.get('ecvam',{})
    if e.get('status')=='MAP_ONLY' and results.get('4-4',{}).get('status') not in ('공식 파일자료 확보','일부 확보'):results['4-4']={'status':'도면 확보','detail':e['detail'],'source':e['source'],'map_available':True}
    elif e.get('status')=='ERROR' and results.get('4-4',{}).get('status') not in ('공식 파일자료 확보','일부 확보'):results['4-4']={'status':'미확보','detail':e.get('detail','공식 WMS 연결 실패')+' · 등급별 면적은 원도형 별도 필요','source':'국토환경성평가지도'}
    characteristic_rows=[]
    for row in land:
        rec=facts.get('characteristics:'+row['pnu'],{}).get('record') or {}
        row['land_shape']=rec.get('tpgrphFrmCodeNm') or rec.get('topgrphFrmCodeNm');row['road_frontage']=rec.get('roadSideCodeNm');row['land_characteristics_year']=rec.get('stdrYear')
        if rec:characteristic_rows.append({'pnu':row['pnu'],'shape':row['land_shape'],'overlap_area':row['overlap_area']})
    if characteristic_rows and results.get('2-5',{}).get('status') not in ('공식 파일자료 확보','일부 확보'):
        st=distribution(characteristic_rows,'shape','overlap_area');statistics['2-5']=st;results['2-5']={'status':'필지 속성자료' if st['known'] else '미확보','detail':f"형상명 {st['known']}/{len(land)}필지 확보 · 공시지가 조사용 형상 원문, 사업별 부정형 판정 미적용",'source':'VWorld 토지특성정보'}
        for item in ('7-3','7-4'):
            if results.get(item,{}).get('status')!='검토자료 확보':results[item]={'status':'기초 속성자료','detail':'토지특성 형상·도로접면 원문을 토지조서에 연결. 실제 유효도로 폭원·출입구 및 사업별 법정 산정기준은 별도 필요.','source':'VWorld 토지특성정보'}
    w=facts.get('weather',{})
    if w.get('status') in ('DATA','PARTIAL'):
        results['4-7']={'status':'관측자료 확보' if w['status']=='DATA' else '일부 관측자료','detail':w['detail'],'source':w['source']};statistics['4-7']={k:v for k,v in w.items() if k not in ('detail','source')}
    elif w and results.get('4-7',{}).get('status')!='공식 파일자료 확보':results['4-7']={'status':'자료 필요','detail':w.get('detail','기상자료 연결 실패'),'source':'기상청'}
    if buildings:
        if results.get('7-1',{}).get('status')!='검토자료 확보':results['7-1']={'status':'기초 속성자료','detail':'사용승인일·만 경과연수·구조를 연결. 사업방식과 관할 기준 미선택으로 법정 노후불량 비율은 미산정.','source':'건축HUB'};statistics['7-1']=distribution(buildings,'age')
        if results.get('7-2',{}).get('status')!='검토자료 확보':results['7-2']={'status':'기초 속성자료','detail':'대장 세대수·가구수·호수와 대장 종류를 건축물조서에 연결. 서로 더하거나 법정 호수밀도로 자동 환산하지 않음.','source':'건축HUB'}
    results['1-3']={'status':'부분 종합','detail':'공간·대장·공식 파일의 확보 결과를 종합. 미확보 항목을 제외하거나 없음 처리하지 않음.','source':'동일 확정 구역계 FACT'}
    for row in data['items']:
        row.update(results.get(row['id'],{}));row['statistics']=statistics.get(row['id']);row['queried_at']=max((v.get('queried_at','') for v in facts.values()),default='')
    data['statistics']=statistics;data['source_jobs']=[{'id':k,'status':v.get('status'),'detail':v.get('detail'),'source':v.get('source'),'queried_at':v.get('queried_at'),'error_code':v.get('error_code'),'stage':v.get('stage')} for k,v in facts.items()]
    return data


def register_sources(router,service,snapshots,lock,output,get_snapshot,fetch_layer,layer_type,area,gate):
    def fixed(token):get_snapshot(token);return snapshots[token]
    @router.get('/reports/{token}/source-jobs')
    def jobs(token:str):
        s=fixed(token)
        if '_report' not in s:raise HTTPException(409,'조서 조회를 먼저 실행하세요.')
        return {'jobs':[{'id':'vector:'+k,'label':v} for k,v in {**{f'LT_C_UQ11{i}':'용도지역' for i in range(1,5)},**{f'LT_C_UPISUQ15{i}':'도시계획시설' for i in range(1,10)},**EXTRA_LAYERS}.items()]+[{'id':'ned:'+p,'label':'필지 토지이용계획 '+p} for p in s['_report']['land']]+[{'id':'characteristics:'+p,'label':'토지특성 '+p} for p in s['_report']['land']]+[{'id':'file:'+k,'label':v} for k,v in FILE_ITEMS.items() if k!='4-3']+[{'id':'landcover_wms','label':'2025년 세분류 토지피복 WMS 지도'},{'id':'landcover_wfs','label':'세분류 토지피복 원도형 WFS 제공 여부 확인'},{'id':'ecobank','label':'생태·자연도 WFS 원도형'},{'id':'weather','label':'기상청 관측 일자료'},{'id':'dem','label':'표고·경사·단면'},{'id':'ecvam','label':'국토환경성평가 WMS'}]}
    @router.post('/reports/{token}/source-jobs/{job}')
    def collect(token:str,job:str):
        s=fixed(token);allowed={v['id'] for v in jobs(token)['jobs']}
        if job not in allowed:raise HTTPException(404,'허용되지 않은 조회입니다.')
        if not gate.acquire(False):raise HTTPException(429,'조서 조회 중입니다. 잠시 후 다시 시도하세요.')
        try:
            old=s.get('_national_sources',{}).get(job)
            # Prior 2-5 releases cached an incorrectly spelled topgrph* field.
            # Refetch the legacy ATTRIBUTE_ONLY response once, instead of
            # silently displaying 0/N when a user retries the survey.
            stale_characteristics = (
                job.startswith('characteristics:') and
                old and old.get('status') == 'ATTRIBUTE_ONLY' and
                'tpgrphFrmCodeNm' not in (old.get('record') or {})
            )
            if old and old.get('status') in ('DATA','ATTRIBUTE_ONLY','MAP_ONLY') and not stale_characteristics:
                return {k:v for k,v in old.items() if k not in ('_image','features','rows','profiles')}
            try:
                if job.startswith('vector:'):
                    k=job.split(':',1)[1];f=s.get('_report_layers',{}).get(k)
                    if not f or f.get('status') not in ('COMPLETE','REFERENCE'):f=fetch_layer(layer_type(geometry=s['geometry'],layer=k,snapshot_id=token))
                    result={'status':'DATA' if f.get('status')=='COMPLETE' else 'REFERENCE' if f.get('status')=='REFERENCE' else 'ERROR','detail':'공간레이어 조회 완료' if f.get('status')=='COMPLETE' else '보조 조회 참고자료 · 원문 진위·수록 완전성 미검증' if f.get('status')=='REFERENCE' else ERROR_LABELS.get(f.get('error_code'),'공간레이어 조회 실패'),'source':'VWorld','error_code':f.get('error_code')}
                elif job.startswith('ned:'):result=fetch_ned(service,job.split(':',1)[1])
                elif job.startswith('characteristics:'):result=characteristics(service,job.split(':',1)[1])
                elif job=='weather':result=weather(s['geometry'],s['_report']['as_of'])
                elif job=='ecobank':
                    from development.nature.ecobank import collect_ecobank
                    result=collect_ecobank(s['geometry'])
                elif job=='landcover_wms':
                    from development.land.landcover_service import fetch_landcover_wms
                    result=fetch_landcover_wms(s['geometry'])
                elif job=='landcover_wfs':
                    from development.land.landcover_service import fetch_landcover_wfs
                    result=fetch_landcover_wfs(s['geometry'])
                elif job.startswith('file:'):result=official_vector(job.split(':',1)[1],s['geometry'],area)
                elif job=='dem':result=dem(s['geometry'],area)
                else:result=ecvam_map(service,s['geometry'])
            except Exception as exc:
                stage='LAND_USE' if job.startswith('ned:') else 'LAND_CHARACTERISTICS' if job.startswith('characteristics:') else 'WEATHER' if job=='weather' else 'ECOBANK_WFS' if job=='ecobank' else 'LOCAL_SOURCE' if job=='dem' or job.startswith('file:') else 'SOURCE_COLLECTION'
                result=source_failure(exc,stage)
            result['queried_at']=datetime.now(__import__('zoneinfo').ZoneInfo('Asia/Seoul')).isoformat(timespec='seconds')
            with lock:s.setdefault('_national_sources',{})[job]=result
            return {k:v for k,v in result.items() if k not in ('_image','features','rows','profiles')}
        finally:gate.release()
    @router.get('/reports/{token}/landcover.png')
    def landcover_image(token:str):
        data=fixed(token).get('_national_sources',{}).get('landcover_wms',{})
        if data.get('status')!='MAP_ONLY' or not data.get('_image'):
            raise HTTPException(404,'토지피복 WMS 이미지가 확보되지 않았습니다.')
        return Response(data['_image'],media_type='image/png',
                        headers={'Cache-Control':'private,max-age=120',
                                 'X-Content-Type-Options':'nosniff'})

    @router.get('/reports/{token}/ecvam.png')
    def image(token:str):
        data=fixed(token).get('_national_sources',{}).get('ecvam',{})
        if data.get('status')!='MAP_ONLY':raise HTTPException(404,'확보된 도면이 없습니다.')
        return Response(data['_image'],media_type='image/png',headers={'Cache-Control':'no-store'})
