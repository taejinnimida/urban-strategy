"""Development-only inventories. No business eligibility rules are applied."""
import copy, io, math, re, threading, time, zipfile, json
from datetime import datetime
from zoneinfo import ZoneInfo
from xml.sax.saxutils import escape
from fastapi import HTTPException
from fastapi.responses import Response
from development_sources import safe_failure, ERROR_LABELS
from shapely.geometry import shape
from shapely.ops import unary_union

GROUPS = [
('사업의 개요','위치 및 면적|주변 개발사업 현황|대상지현황 종합'),
('토지이용 현황','소유자별|지목별|면적별|공시지가별|토지형상별|토지피복별'),
('건축물 현황','용도별|구조별|층수별|경과연도별|연면적별|건폐율별|용적률별'),
('자연환경 현황','표고분석|경사분석|생태자연도|국토환경성평가|임상도(임상·영급·수종·경급)|수리 수문|기상 기후|단면분석'),
('도시계획 현황','도시기본계획|용도지역|용도지구|용도구역|도시계획시설(교통)|도시계획시설(공간)|도시계획시설(기타)|지구단위계획구역'),
('공적규제','농지/초지|산지|하천/방재구역|문화재 등|교육환경보호구역|상수원보호/수변구역|도로/접도구역|기타 공적규제'),
('도시 및 주거환경현황','노후 불량 건축물|호수밀도|접도율|토지형상')]
LAND = [('parcel_source','지적자료 취득경로'),('parcel_assurance','지적자료 확인수준'),('pnu','PNU'),('address','소재지'),('jibun','지번'),('category','지목'),('official_area','공부면적(㎡)'),('overlap_area','편입 도형면적(㎡)'),('ownership','소유구분'),('zoning','중첩 용도지역'),('zoning_status','용도지역 조회상태'),('price','개별공시지가(원/㎡)'),('price_year','지가 기준연도'),('ledger_status','대장 조회상태'),('price_status','지가 조회상태'),('building_status','건축물 조회상태'),('ledger_source','대장 출처'),('price_source','지가 출처'),('updated','대장 갱신일'),('queried_at','조회시각'),('ledger_error_code','대장 오류구분'),('price_error_code','지가 오류구분'),('building_error_code','건축물 오류구분'),('missing_fields','미확보 속성'),('land_shape','토지형상 원문'),('road_frontage','도로접면 원문'),('land_characteristics_year','토지특성 연도')]
BUILDING = [('id','건축물대장 관리번호'),('pnus','관련 PNU'),('address','소재지'),('name','건물·동명'),('use','주용도'),('structure','구조'),('floors','지상층수'),('basements','지하층수'),('footprint','건축면적(㎡)'),('floor_area','연면적(㎡)'),('coverage','대장 건폐율(%)'),('far','대장 용적률(%)'),('approved','사용승인일'),('age','만 경과연수(년)'),('scope','편입 확인'),('source','출처'),('households','대장 세대수'),('families','대장 가구수'),('units','대장 호수'),('register_kind','대장 종류')]

def now(): return datetime.now(ZoneInfo('Asia/Seoul')).isoformat(timespec='seconds')
def number(v):
    try:
        n=float(v)
        return n if math.isfinite(n) and n>=0 else None
    except (TypeError,ValueError): return None

def age(v, day):
    try:
        d=datetime.strptime(str(v),'%Y%m%d').date()
        return day.year-d.year-((day.month,day.day)<(d.month,d.day)) if d<=day else None
    except ValueError:return None

def parcels(features, geometry, area):
    target=shape(geometry); grouped={}; props={}
    for f in features:
        p={str(k).lower():v for k,v in (f.get('properties') or {}).items()}
        pnu=str(p.get('pnu') or f.get('id') or '')
        if not re.fullmatch(r'\d{19}',pnu):raise HTTPException(422,'지적자료에 PNU가 없어 조서를 확정할 수 없습니다.')
        g=shape(f['geometry']).intersection(target)
        if area(g)<=0:continue
        grouped.setdefault(pnu,[]).append(g);props[pnu]=p
    out={}
    for pnu,geoms in grouped.items():
        p=props[pnu]
        jibun=p.get('jibun') or ('산 ' if pnu[10]=='2' else '')+str(int(pnu[11:15]))+('-'+str(int(pnu[15:])) if int(pnu[15:]) else '')
        out[pnu]={'pnu':pnu,'jibun':jibun,'overlap_area':area(unary_union(geoms)), 'ledger_status':'미조회','price_status':'미조회','building_status':'미조회'}
    return out

def buildings(rows, day):
    result={}
    for pnu,items in sorted(rows.items()):
        for i,r in enumerate(items):
            ident=str(r.get('mgmBldrgstPk') or '')
            key=ident or f'미확인:{pnu}:{i}'
            if key in result:
                result[key]['pnus'] = ', '.join(sorted(set(result[key]['pnus'].split(', ')+[pnu])))
                continue
            result[key]={'id':ident or None,'pnus':pnu,'address':r.get('platPlc'),'name':' '.join(str(r.get(k) or '') for k in ('bldNm','dongNm')).strip(),'use':r.get('mainPurpsCdNm'),'structure':r.get('strctCdNm'),'approved':r.get('useAprDay'),'age':age(r.get('useAprDay'),day),'scope':'편입필지 관련 대장 · 건물 도형 편입 미확인'+(' · 관리번호 없어 중복확인 필요' if not ident else ''),'source':'국토교통부 건축HUB'}
            for dest,src in [('floors','grndFlrCnt'),('basements','ugrndFlrCnt'),('footprint','archArea'),('floor_area','totArea'),('coverage','bcRat'),('far','vlRat'),('households','hhldCnt'),('families','fmlyCnt'),('units','hoCnt')]: result[key][dest]=number(r.get(src))
            result[key]['register_kind']=r.get('regstrKindCdNm')
    return list(result.values())

def review(report, snapshot):
    land=list(report.get('land',{}).values()); b=buildings(report.get('buildings',{}),datetime.fromisoformat(report['as_of']).date()); layers=snapshot.get('_report_layers',{})
    out=[]
    for gi,(group,names) in enumerate(GROUPS,1):
        for ni,name in enumerate(names.split('|'),1):
            key=f'{gi}-{ni}'; status='자료 연결 필요'; detail='원본 공간자료·분석기준 미연결'; source='도시계획 현황 분석보고서 검토항목'
            if key=='1-1':status='기초자료 확보';detail=f"확정 구역계 {snapshot['area_m2']:,.2f}㎡ · 위치는 구역계 좌표 기준";source='확정 구역계'
            if gi==2 and ni in (1,2,3,4):
                field={1:'ownership',2:'category',3:'official_area',4:'price'}[ni]; known=sum(x.get(field) is not None and x.get(field)!='' for x in land)
                status='일부 확보' if known else '미확보';detail=f'{len(land)}필지 중 {known}필지 속성 확보'
                if land and known==len(land):status='기초자료 확보'
                if ni==1:detail+=' · 소유구분만 제공, 개인별 소유자 분석 아님'
                source='토지대장·개별공시지가'
            if gi==3:
                field=['use','structure','floors','age','floor_area','coverage','far'][ni-1]; known=sum(x.get(field) is not None and x.get(field)!='' for x in b)
                status='일부 확보' if known else '미확보';detail=f'관련 대장 {len(b)}건 중 {known}건 속성 확보 · 건물별 편입 미확인';source='건축HUB'
            ids={'5-2':[f'LT_C_UQ11{x}' for x in range(1,5)],'5-5':['LT_C_UPISUQ151'],'5-6':['LT_C_UPISUQ153'],'5-7':[f'LT_C_UPISUQ15{x}' for x in (2,4,5,6,7,8,9)]}.get(key)
            if ids:
                done=sum(layers.get(k,{}).get('status')=='COMPLETE' for k in ids)
                status='기초자료 확보' if done==len(ids) else ('일부 확보' if done else '미확보');detail=f'{done}/{len(ids)} 공간레이어 조회 완료 · 현행 결정 여부는 별도 확인';source='VWorld'
            if key=='7-1':detail='경과연수와 별개로 사업별 법정 노후·불량 기준 적용 필요'
            out.append({'id':key,'group':group,'name':name,'status':status,'detail':detail,'source':source})
    return out

# A small standards-based XLSX serializer for the deployed Python service.
# Explicit string cells protect 19-digit IDs and prevent formula execution.
def xlsx(sheets):
    buf=io.BytesIO(); ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    def clean(v):return escape(re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]','',str(v))[:32767])
    def col(n):
        s=''
        while n:n,r=divmod(n-1,26);s=chr(65+r)+s
        return s
    with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
        ct='<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        wb=f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'; rel='<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        for idx,(name,rows) in enumerate(sheets,1):
            ct+=f'<Override PartName="/xl/worksheets/sheet{idx}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            wb+=f'<sheet name="{clean(name)}" sheetId="{idx}" r:id="rId{idx}"/>';rel+=f'<Relationship Id="rId{idx}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{idx}.xml"/>'
            xml=f'<worksheet xmlns="{ns}"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><cols><col min="1" max="{len(rows[0])}" width="24" customWidth="1"/></cols><sheetData>'
            for ri,row in enumerate(rows,1):
                xml+=f'<row r="{ri}" ht="30" customHeight="1">'
                for ci,v in enumerate(row,1):
                    if v is None:continue
                    ref=f'{col(ci)}{ri}';s=1 if ri==1 else (2 if isinstance(v,(int,float)) else 0)
                    if isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v):xml+=f'<c r="{ref}" s="{s}"><v>{v}</v></c>'
                    else:xml+=f'<c r="{ref}" s="{s}" t="inlineStr"><is><t xml:space="preserve">{clean(v)}</t></is></c>'
                xml+='</row>'
            xml+=f'</sheetData><autoFilter ref="A1:{col(len(rows[0]))}{max(1,len(rows))}"/></worksheet>';z.writestr(f'xl/worksheets/sheet{idx}.xml',xml)
        z.writestr('[Content_Types].xml',ct+'</Types>');z.writestr('xl/workbook.xml',wb+'</sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels',rel+'<Relationship Id="styles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        z.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/styles.xml',f'<styleSheet xmlns="{ns}"><fonts count="2"><font><sz val="11"/><name val="맑은 고딕"/></font><font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="맑은 고딕"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF16364A"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="3"><xf fontId="0" fillId="0" borderId="0" xfId="0"/><xf fontId="1" fillId="2" borderId="0" xfId="0" applyFill="1" applyFont="1"/><xf fontId="0" fillId="0" borderId="0" numFmtId="4" xfId="0" applyNumberFormat="1"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
    return buf.getvalue()

def tables(data):
    def table(cols,rows):return [[label for _,label in cols]]+[[r.get(k) for k,_ in cols] for r in rows]
    sheets=[('요약',[['항목','내용'],['작성 기준시각',data['as_of']],['확정 구역계 면적(㎡)',data['area_m2']],['지적 확보 상태',data.get('parcel_status','UNAVAILABLE')],['편입 필지 수',len(data['land']) if data.get('parcel_status')=='COMPLETE' else '미확보'],['보조 지적 참고 필지 수',len(data['land']) if data.get('parcel_status')=='REFERENCE' else '해당 없음'],['지적자료 한계',data.get('parcel_note') or '제공기관 응답 기준'],['관련 건축물대장 수',len(data['buildings'])],['건축물 범위','편입필지 관련 대장. 건물 도형 편입 여부 미확인'],['미확보 처리','빈 셀은 미확보. 0 또는 해당 없음으로 해석 금지'],['노후도','경과연수는 법정 노후·불량 판정 아님'],['검토 범위','44개 항목 등록. 자료 연결과 법정 분석 완료는 별개']]),('토지조서',table(LAND,data['land'])),('건축물조서',table(BUILDING,data['buildings'])),('검토항목',table([('id','번호'),('group','분류'),('name','검토항목'),('status','자료 상태'),('detail','내용·한계'),('source','출처'),('queried_at','조회시각')],data['items'])),('출처와 미확보',[['PNU','토지대장','공시지가','건축물대장','조회시각','미확보 속성']]+[[r['pnu'],r['ledger_status'],r['price_status'],r['building_status'],r.get('queried_at'),r.get('missing_fields')] for r in data['land']])]
    stats=[['검토항목','분류','건수','면적(㎡)','요약']]
    for key,value in data.get('statistics',{}).items():
        groups=value.get('groups') or []
        for row in groups:stats.append([key,row.get('name'),row.get('count'),row.get('area_m2'),json.dumps({k:v for k,v in row.items() if k not in ('name','count','area_m2')},ensure_ascii=False)])
        stats.append([key,'전체',value.get('known'),value.get('overlap_area_m2'),json.dumps({k:v for k,v in value.items() if k!='groups'},ensure_ascii=False)])
    sheets.append(('분류별 통계',stats))
    # Thematic map/CAD/XLSX share the exact same classified area/count FACT.
    # Raw distribution remains available separately above.
    theme_rows=[['검토항목','등급코드','분류명','필지수','대상지 편입 도형면적(㎡)','표시색상']]
    for item in data['items']:
        if item.get('thematic_type') not in ('jimok','land_numeric'):
            continue
        for cls in item.get('legend') or []:
            theme_rows.append([item['id'],cls.get('class_code'),cls.get('name'),
                               cls.get('count'),cls.get('area_m2'),cls.get('color')])
    if len(theme_rows)>1:
        sheets.append(('현황도 분류별 통계',theme_rows))
    land_table_rows=[['검토항목','자료상태','행구분','구분','면적(㎡)','구성비(%)','필지·도형수','비고','자료출처','자료기준일']]
    for item in data['items']:
        t=item.get('survey_table')
        if not t:
            continue
        if not t.get('rows'):
            land_table_rows.append([item['id'],t['status'],'미확보','현황표 미확보',None,None,None,
                                    ' / '.join(t.get('notes') or []),t.get('source'),t.get('as_of')])
        for row in t.get('rows') or []:
            land_table_rows.append([item['id'],t['status'],row.get('kind'),row.get('label'),
                                    row.get('area_m2'),row.get('pct'),row.get('count'),
                                    row.get('note'),t.get('source'),t.get('as_of')])
    sheets.append(('토지현황표',land_table_rows))
    building_table_rows=[['검토항목','자료상태','행구분','구분','대장 건수(건)','구성비(%)','비고','분석문','자료출처','자료기준일']]
    for item in data['items']:
        if not re.fullmatch(r'3-[1-7]', str(item.get('id', ''))):
            continue
        t=item.get('survey_table')
        if not t:
            continue
        if not t.get('rows'):
            building_table_rows.append([item['id'],t['status'],'미확보','건축물현황표 미확보',None,None,
                                        ' / '.join(t.get('notes') or []),None,t.get('source'),t.get('as_of')])
        for row in t.get('rows') or []:
            building_table_rows.append([item['id'],t['status'],row.get('kind'),row.get('label'),
                                        row.get('count'),row.get('pct'),row.get('note'),
                                        t.get('analysis_text') if row.get('kind')=='total' else None,
                                        t.get('source'),t.get('as_of')])
    sheets.append(('건축물현황표',building_table_rows))
    nature_table_rows=[['검토항목','자료상태','행구분','구분','값','단위','면적(㎡)','구역계 대비(%)','도형수·표본수','최저표고(m)','최고표고(m)','비고','자료출처','원자료 기준일','작성시각','분석문']]
    for item in data['items']:
        if not re.fullmatch(r'4-[1-8]',str(item.get('id',''))):
            continue
        t=item.get('survey_table') or {}
        rows=t.get('rows') or []
        if not rows:
            nature_table_rows.append([item['id'],t.get('status','MISSING'),'미확보','자료 미확보',None,None,None,None,None,None,None,
                                      ' / '.join(t.get('notes') or []),t.get('source'),t.get('source_as_of'),t.get('prepared_at'),None])
        for row in rows:
            nature_table_rows.append([item['id'],t.get('status'),row.get('kind'),row.get('label'),row.get('value'),
                                      row.get('unit'),row.get('area_m2'),row.get('pct'),row.get('count'),
                                      row.get('min_m'),row.get('max_m'),row.get('note'),t.get('source'),
                                      t.get('source_as_of'),t.get('prepared_at'),
                                      t.get('analysis_text') if row is rows[0] else None])
    sheets.append(('자연환경현황표',nature_table_rows))
    regulations=[['PNU','검토항목','규제명','관계코드','관계명','관리번호','갱신일']]
    for item in data['items']:
        for row in item.get('regulation_rows',[]):regulations.append([row.get('pnu'),item['id'],row.get('prposAreaDstrcCodeNm'),row.get('cnflcAt'),row.get('cnflcAtNm'),row.get('manageNo'),row.get('lastUpdtDt')])
    sheets.append(('규제 참고행',regulations))
    sheets.append(('자료 연결결과',[['조회 항목','상태','내용','출처','조회시각','오류구분','실패단계']]+[[v.get(k) for k in ('id','status','detail','source','queried_at','error_code','stage')] for v in data.get('source_jobs',[])]))
    weather=data.get('statistics',{}).get('4-7',{}).get('days',[])
    if weather:sheets.append(('기상 일자료',[['일자','평균기온(℃)','일강수량(mm)']]+[[r.get(k) for k in ('date','mean_temperature_c','precipitation_mm')] for r in weather]))
    profiles=data.get('statistics',{}).get('4-8',{}).get('values',[])
    if profiles:sheets.append(('참고 단면',[['자동 단면방향','거리(m)','표고(m)']]+[[r['axis'],p['distance_m'],p['elevation_m']] for r in profiles for p in r['points']]))
    return sheets

def register_report(router, service, snapshots, lock, get_snapshot, fetch_layer, layer_type, area):
    gate=threading.Lock() # One inventory request per server; other map channels remain independent.
    def sync_parcels(s):
        fact=s.get('_report_layers',{}).get('LP_PA_CBND_BUBUN',{})
        report=s.get('_report')
        if report is None or fact.get('status') not in ('COMPLETE','REFERENCE'):return
        revision=fact.get('fact_revision')
        if revision and report.get('parcel_fact_revision')==revision:return
        rows=parcels(fact.get('features',[]),s['geometry'],area)
        if len(rows)>1000:raise HTTPException(422,'개발용 조서는 1,000필지 이하 구역을 지원합니다.')
        source='VWorld 서버 직접 수신' if fact['status']=='COMPLETE' else 'VWorld 브라우저 보조 수신'
        assurance='제공기관 응답 확보' if fact['status']=='COMPLETE' else '참고자료 · 원문 진위·전체 수록 여부 미검증'
        old=report.get('land',{})
        for pnu,row in rows.items():
            rows[pnu]={**row,**old.get(pnu,{}),'pnu':pnu,'jibun':row['jibun'],'overlap_area':row['overlap_area'],
                       'parcel_source':source,'parcel_assurance':assurance}
        report.update(land=rows,buildings={p:v for p,v in report.get('buildings',{}).items() if p in rows},
                      parcel_status=fact['status'],parcel_error_code=None,parcel_fact_revision=revision)
        for job in list(s.get('_national_sources',{})):
            if job.startswith(('ned:','characteristics:')) and job.split(':',1)[1] not in rows:del s['_national_sources'][job]
    def state(token):
        get_snapshot(token)
        with lock:
            s=state_unlocked(token);sync_parcels(s);return s
    def output(token):
        with lock:
            live=state_unlocked(token);sync_parcels(live);s=copy.deepcopy(live);r=s.get('_report')
        if not r:raise HTTPException(409,'현황조서 조회를 먼저 실행하세요.')
        layers=s.get('_report_layers',{}); zoneids=[f'LT_C_UQ11{i}' for i in range(1,5)]; complete=all(layers.get(k,{}).get('status')=='COMPLETE' for k in zoneids)
        geo={}
        for f in layers.get('LP_PA_CBND_BUBUN',{}).get('features',[]):
            props={str(k).lower():v for k,v in (f.get('properties') or {}).items()};pnu=str(props.get('pnu') or f.get('id') or '')
            geo.setdefault(pnu,[]).append(shape(f['geometry']).intersection(shape(s['geometry'])))
        for pnu,row in r['land'].items():
            names=set();parcel_geom=unary_union(geo.get(pnu,[]))
            for k in zoneids:
                layer=layers.get(k,{})
                if layer.get('status') not in ('COMPLETE','REFERENCE'):continue
                for f in layer.get('features',[]):
                    if area(shape(f['geometry']).intersection(parcel_geom))>0:
                        props={str(k).lower():v for k,v in (f.get('properties') or {}).items()};names.add(str(props.get('uname') or props.get('name') or props.get('dgm_nm') or '명칭 미확보'))
            row['zoning']=' · '.join(sorted(names)) or None;row['zoning_status']='조회 완료' if complete else '보조 조회 참고자료 포함' if any(layers.get(k,{}).get('status')=='REFERENCE' for k in zoneids) else '일부 또는 미확보'
        for row in r['land'].values():
            row['missing_fields']=' · '.join(label for field,label in [('address','소재지'),('category','지목'),('official_area','공부면적'),('ownership','소유구분'),('price','공시지가'),('zoning','용도지역')] if row.get(field) in (None,''))
        from development_sources import enrich
        data={'parcel_status':r.get('parcel_status','UNAVAILABLE'),'parcel_error_code':r.get('parcel_error_code'),'as_of':r['as_of'],'area_m2':s['area_m2'],'land':list(r['land'].values()),'buildings':buildings(r['buildings'],datetime.fromisoformat(r['as_of']).date()),'items':review(r,s)}
        data['parcel_note']='브라우저 보조 수신 지적에 따른 참고 필지목록입니다. 원문 진위·전체 필지 수록 여부는 미검증이며 대장 속성은 공공 API에서 별도 조회합니다.' if r.get('parcel_status')=='REFERENCE' else ''
        if data['parcel_note']:
            for row in data['buildings']:row['scope']='보조 지적 필지목록 기반 · '+row['scope']
        data['fact_revisions']={k:v.get('fact_revision') for k,v in layers.items()}
        data=enrich(data,s,area)
        from development_jimok import attach_jimok_theme
        attach_jimok_theme(data,s,area)
        from development_ownership import attach_ownership_theme
        attach_ownership_theme(data,s,area)
        from development_land_numeric import attach_numeric_land_themes
        attach_numeric_land_themes(data,s,area)
        from development_land_shape import attach_land_shape_theme
        attach_land_shape_theme(data,s,area)
        from development_landcover import attach_landcover_theme
        attach_landcover_theme(data,s,area)
        from development_land_tables import attach_land_survey_tables
        attach_land_survey_tables(data,s,area)
        from development_building_tables import attach_building_survey_tables
        attach_building_survey_tables(data)
        from development_nature_tables import attach_nature_survey_tables
        from development_nature_vector import attach_nature_vector_themes
        attach_nature_vector_themes(data,s)
        attach_nature_survey_tables(data,s)
        return data
    def state_unlocked(token):
        s=snapshots.get(token)
        if not s or time.time()-s['created_at']>7200:raise HTTPException(404,'대상지가 만료되었습니다.')
        return s
    @router.post('/reports/{token}/prepare')
    def prepare(token:str):
        s=state(token)
        if s.get('_report',{}).get('parcel_status') in ('COMPLETE','REFERENCE'):return output(token)
        if not gate.acquire(False):raise HTTPException(429,'다른 조서를 조회 중입니다. 잠시 후 다시 시도하세요.')
        try:
            fact=s.get('_report_layers',{}).get('LP_PA_CBND_BUBUN')
            if not fact or fact.get('status') not in ('COMPLETE','REFERENCE'):fact=fetch_layer(layer_type(geometry=s['geometry'],layer='LP_PA_CBND_BUBUN',snapshot_id=token))
            if fact.get('status') not in ('COMPLETE','REFERENCE'):
                with lock:
                    r=s.setdefault('_report',{'as_of':now(),'land':{},'buildings':{}})
                    r['parcel_status']='UNAVAILABLE'
                    r['parcel_error_code']=fact.get('error_code','REQUEST_FAILED')
                return output(token)
            with lock:
                current=state_unlocked(token)
                current.setdefault('_report',{'as_of':now(),'land':{},'buildings':{}})
                sync_parcels(current)
            return output(token)
        finally:gate.release()
    @router.get('/reports/{token}')
    def report(token:str):return output(token)
    @router.post('/reports/{token}/parcel/{pnu}')
    def parcel(token:str,pnu:str):
        s=state(token);r=s.get('_report',{});row=r.get('land',{}).get(pnu)
        if row is None:raise HTTPException(404,'확정 구역계의 조서 대상 필지가 아닙니다.')
        if not gate.acquire(False):raise HTTPException(429,'조서 조회 중입니다. 잠시 후 다시 시도하세요.')
        try:
            item=copy.deepcopy(row);new_buildings=None;year=datetime.fromisoformat(r['as_of']).year;revision=r.get('parcel_fact_revision')
            if item['ledger_status']!='확보' or any(item.get(k) in (None,'') for k in ('official_area','ownership','category')):
                try:
                    result=service._resolve_land_ledger(pnu);rec=result.get('record') or {}
                    if rec and str(rec.get('pnu') or pnu)!=pnu:raise ValueError('PNU mismatch')
                    item.update(address=rec.get('ldCodeNm'),category=rec.get('lndcgrCodeNm'),official_area=number(rec.get('lndpclAr')),ownership=rec.get('posesnSeCodeNm'),updated=rec.get('lastUpdtDt'),ledger_source={'vworld_ladfrl':'VWorld 토지대장','data_go_ladfrl':'공공데이터포털 토지대장','vworld_land_characteristics':'VWorld 토지특성','local_AL_D003_snapshot':'서울 보유 공공자료'}.get(result.get('selected_source'),'공공 토지자료'),ledger_status='확보' if rec else '미확보')
                    if rec.get('mnnmSlno'):item['jibun']=rec['mnnmSlno']
                except Exception as exc:item.update(ledger_status='조회 실패',ledger_error_code=safe_failure(exc,'LAND_LEDGER')['error_code'])
            if item['price_status']!='확보':
                try:
                    rec=service._resolve_individual_land_price(pnu,year,8) or {}
                    valid=str(rec.get('pnu'))==pnu and rec.get('year')==year and number(rec.get('price_per_m2')) is not None
                    item.update(price=number(rec.get('price_per_m2')) if valid else None,price_year=year,price_status='확보' if valid else '미확보',price_source={'VWORLD_LIVE':'VWorld 실시간','LOCAL_SNAPSHOT':'서울 보유 공공자료'}.get(rec.get('_source_type'),'공공 지가자료') if valid else None)
                except Exception as exc:item.update(price_year=year,price_status='조회 실패',price_error_code=safe_failure(exc,'LAND_PRICE')['error_code'])
            if item['building_status'] not in ('확보','대장 응답 0건'):
                try:
                    raw=service._query_building_hub_title(pnu)
                    if len(raw)>=2000:raise ValueError('possible truncation')
                    keep=['mgmBldrgstPk','platPlc','bldNm','dongNm','mainPurpsCdNm','strctCdNm','useAprDay','grndFlrCnt','ugrndFlrCnt','archArea','totArea','bcRat','vlRat','hhldCnt','fmlyCnt','hoCnt','regstrKindCdNm']
                    new_buildings=[{k:v.get(k) for k in keep} for v in raw];item['building_status']='확보' if raw else '대장 응답 0건'
                except Exception as exc:item.update(building_status='조회 실패',building_error_code=safe_failure(exc,'BUILDING_LEDGER')['error_code'])
            for kind in ('ledger','price','building'):
                if item.get(kind+'_status') in ('확보','대장 응답 0건'):item.pop(kind+'_error_code',None)
            item['queried_at']=now()
            with lock:
                live=state_unlocked(token);sync_parcels(live);current=live['_report']
                if revision!=current.get('parcel_fact_revision') or pnu not in current['land']:raise HTTPException(409,'지적자료가 갱신되었습니다. 조서를 다시 조회하세요.')
                current['land'][pnu]=item
                if new_buildings is not None:current['buildings'][pnu]=new_buildings
            return {'pnu':pnu,'ledger_status':item['ledger_status'],'price_status':item['price_status'],'building_status':item['building_status'],**{k:v for k,v in item.items() if k.endswith('_error_code')}}
        finally:gate.release()
    @router.get('/reports/{token}/xlsx')
    def download(token:str):
        return Response(xlsx(tables(output(token))),media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename="land-building-report.xlsx"','Cache-Control':'no-store'})

    from development_cad import register_cad
    register_cad(router,output,snapshots,lock,fetch_layer,layer_type)

    from development_sources import register_sources
    register_sources(router,service,snapshots,lock,output,get_snapshot,fetch_layer,layer_type,area,gate)
