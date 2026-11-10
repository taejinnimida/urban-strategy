from pathlib import Path
import re, subprocess, sys
ROOT=Path(__file__).resolve().parent
HTML=(ROOT/'app.html').read_text(encoding='utf-8')
PYAPP=(ROOT/'app.py').read_text(encoding='utf-8')
checks=[]
def ck(name, cond):
    ok=bool(cond); checks.append((name,ok)); print(('PASS ' if ok else 'FAIL ')+name); return ok

def section(start, end=None):
    i=HTML.index(start)
    j=HTML.index(end,i) if end else len(HTML)
    return HTML[i:j]

# 1. 접도: 재개발 6m / 주환개 4m+35m 막다른도로 예외 분리
roadfacts=section('function schemeFrontageEvidenceFacts','function roadRawFacts')
ck('R40 UI redevelopment frontage remains 6m', "mode:'width6_frontage'" in roadfacts and "주택재개발 접도율(6m 기준·40% 이하)" in roadfacts)
ck('R40 UI residential-environment frontage uses legal deadend mode', "mode:'width4_deadend35_width6'" in roadfacts and '35m 이상 막다른 도로는 폭 6m' in roadfacts)
style=section('function schemeRoadParcelEvidenceStyle','function schemeRoadEvidenceStyle')
ck('R40 UI residential-environment parcel map uses legal pass flag', "p._frontage_resenv_pass" in style and "p._frontage4_pass" not in style)
roadstyle=section('function schemeRoadEvidenceStyle','function renderSchemeRoadEvidence')
ck('R40 UI deadend exception has map styling branch', "rule.mode==='width4_deadend35_width6'" in roadstyle and '_long_dead_end_35' in roadstyle)
renderroad=section('function renderSchemeRoadEvidence','function ') if False else section('function renderSchemeRoadEvidence','function renderWidthRoadSchemeSummary') if 'function renderWidthRoadSchemeSummary' in HTML[HTML.index('function renderSchemeRoadEvidence'):] else HTML[HTML.index('function renderSchemeRoadEvidence'):HTML.index('function renderAgeSpatialStatus')]
# avoid depending on function order: narrow from marker to next clear marker
ri=HTML.index('function renderSchemeRoadEvidence')
rj=HTML.find('\nfunction ',ri+20)
renderroad=HTML[ri:rj]
ck('R40 UI road rule accepts PASS as legal pass', "['CONFIRMED','PASS'].includes(rule?.status)" in renderroad)
ck('R40 UI road legal result label explicit', '입안요건 충족' in renderroad and '입안요건 불충족' in renderroad)
ck('R40 UI road condition separated from legal result', '접도여건 불량' in renderroad and '공간 FACT와 사업 RULE을 분리 표시' in renderroad)

# 2. 역세권 요약 연결
station=section('function schemeStationRow','function stationSchemeStatusMeta')
ck('R40 UI station-complex uses distance row', "pick('역세권 거리')" in station)
ck('R40 UI station-complex uses block occupancy row', "pick('사업대상지 가로구역 점유')" in station)
ck('R40 UI station-complex stale missing row removed', "pick('역세권 가로구역')" not in station)
ck('R40 UI public-complex station reads commercial route directly', "res?.route_results?.commercial" in station and "r.item==='역세권 범위'" in station)
ck('R40 UI public-complex no selected-type NA shortcut', "f&&f.type!=='commercial'" not in station and '비역세권 유형 선택 · 역세권 경로 미사용' not in station)

# 3. 도심공공주택복합 3개 검토서
popup=section('function renderPublicComplexDetailPopup','function renderInnovationDetailPopup')
ck('R40 UI public-complex has exactly three independent route keys', "const routeOrder=['commercial','industrial','housing'];" in popup)
ck('R40 UI public-complex renders independent route sheets', 'public-complex-route-sheet' in popup and 'data-public-complex-route=' in popup)
ck('R40 UI public-complex renders all route sheets rather than selected tab', "routeOrder.map(routeSheet).join('')" in popup)
ck('R40 UI public-complex independence note', '다른 두 유형의 PASS/FAIL/REVIEW가 이 검토서에 전이되지 않습니다.' in popup)
ck('R40 UI public-complex housing five criteria shown', all(x in popup for x in ['과소토지 90㎡ 이하 30% 이상','호수밀도 50호/ha 이상','주택접도율 50% 이하','방재지구 면적 1/2 이상','지하층 주거사용 건축물 1/2 이상']))

# 4. 제도별 노후도 REVIEW 원인 표시
age=section('function ageUncertaintyNote','function schemeSpecificDecisionForRow')
ck('R40 UI age review reason helper exists', 'function ageUncertaintyNote' in age)
ck('R40 UI age review identifies unknown buildings', '미확인 ${u}/${t}동' in age)
ck('R40 UI age review explains threshold cannot be finalized', '범위라 기준 확정 불가' in age)
ck('R40 UI age card uses uncertainty explanation', 'ageUncertaintyNote(a)' in section('function renderAgeSpatialStatus','function schemeSpecificDecisionForRow'))

# 5. 사업별 추가현황 재정리: 공통 FACT는 기존 사이트박스, 고유조건만 잔존
extra=section('const SCHEME_SPECIFIC_KEEP_ROWS','function ageFactValue')
for token in [
  'activation|사전협상 대상요건','growth_potential|역세권활성화 배타관계','safe|서울도심 배제범위',
  'shared_housing|상생주택 사업대상 후보','station_complex|저층주거지 인접','station_complex|역사연결 편의시설 특례',
  'longterm|700% 특례 입지후보','smallscale|자율주택 사업대상지','prior_negotiation|부지성격',
  'prior_negotiation|도시계획변경 필요성','prior_negotiation|상생발전형 자치구']:
    ck(f'R40 UI extra keeps scheme-specific row: {token}', token in extra)
ck('R40 UI extra moves age rows to age box', "return '노후도'" in extra)
ck('R40 UI extra moves frontage rows to road box', "return '접도진단'" in extra)
ck('R40 UI extra moves station rows to station box', "return '역세권'" in extra)
ck('R40 UI extra moves public-complex small parcel to land box', '과소토지' in extra and "return '토지'" in extra)
ck('R40 UI extra moves public-complex basement housing to building-use box', '지하층 주거사용' in extra and "return '건축물 용도'" in extra)
ck('R40 UI extra moves public-complex disaster district to district box', '방재지구' in extra and "return '용도지구'" in extra)
ck('R40 UI extra keeps procedure variables in scheme sheet only', "new Set(['사업유형','재건축진단','주민동의·조합 등'])" in extra)
ck('R40 UI extra renderer reports moved/common/popup counts', '공통현황 ${moved}개 기존박스' in extra and '절차/계획 ${popupOnly}개 검토서' in extra)

# 6. Baseline preservation / version / syntax
ck('R40 UI R35 route-commercial preserved', 'MODEL_REFERENCE' in HTML and 'activation_arterial' in HTML)
ck('R40 UI R39 hillside preserved', 'function analyzeHillZones' in HTML and '40m+10°' in HTML)
ck('R40 UI app version remains v2.5.0', 'seoul_urban_renewal_platform_v2.5.0' in PYAPP)
subprocess.run([sys.executable,'-m','py_compile',str(ROOT/'app.py')],check=True)
ck('R40 UI backend python syntax', True)
parts=[m.group(2) for m in re.finditer(r'<script([^>]*)>(.*?)</script>',HTML,re.S|re.I) if 'src=' not in m.group(1).lower()]
js=ROOT/'_r40_ui_inline_check.js'; js.write_text('\n'.join(parts),encoding='utf-8')
r=subprocess.run(['node','--check',str(js)],capture_output=True,text=True)
if r.returncode!=0: print(r.stderr)
ck('R40 UI inline javascript syntax', r.returncode==0)
try: js.unlink()
except: pass

passed=sum(ok for _,ok in checks); total=len(checks)
if passed!=total:
    print(f'R40 UI FINAL CHECKS FAILED ({passed}/{total})'); sys.exit(1)
print(f'ALL R40 UI FINAL CHECKS PASS ({passed}/{total})')
