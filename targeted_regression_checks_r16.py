from pathlib import Path
import re, sys

APP = Path('app_r16.html') if Path('app_r16.html').exists() else Path('app.html')
s = APP.read_text(encoding='utf-8')
checks=[]
def check(name, cond):
    checks.append((name,bool(cond)))
    print(('PASS' if cond else 'FAIL'), name)

# REVIEW != conditional
check('schemeSheetFeasibility REVIEW displays 확인필요', "return '확인필요';\n}" in s[s.index('function schemeSheetFeasibility'):s.index('function schemeSheetSourceCell')])
check('activation REVIEW displays 확인필요', "function activationFeasibilityLabel(status)" in s and "return '확인필요';" in s[s.index('function activationFeasibilityLabel'):s.index('function activationRowByItem')])
check('smallscale true conditional is explicit', "schemeSheetFeasibility(routeOverall,route.conditional===true)" in s)

# Smallscale street-block semantics
block_fn=s[s.index('function schemeBlockComponentsAfterRemoval'):s.index('function schemeStreetBlockFact')]
small_fn=s[s.index('function smallscaleSpatialFacts'):s.index('function checkSmallscaleFromFacts')]
check('street-block component selector supports coverage ranking', "rankMode==='coverage'" in block_fn and 'block_coverage_of_site_pct' in block_fn)
check('smallscale specifically uses coverage ranking', "key==='smallscale'?'coverage':'share'" in s)
check('smallscale no longer fails merely because multi_block is true', "scopeCoverage>=98&&scopeConnected" not in small_fn and 'scopeConnected=autoBlockKnown&&Number.isFinite(scopeCoverage)&&scopeCoverage>=98' in small_fn)
check('smallscale exposes site coverage and block occupancy separately', '검토구역 편입률' in small_fn and '가로구역 점유율' in small_fn)
check('98 percent documented as topology tolerance not legal number', '98%는 법정 수치가 아니라 GIS 경계·좌표 오차' in small_fn)

# Activation selected vs available route
check('activation evaluates available district and redevelopment routes', "facts.route.available={" in s and "redevelopment:activationRouteAlternativeStatus(store,rows,'redevelopment')" in s)
check('activation popup displays available routes', '가능 시행방식' in s[s.index('function renderActivationDetailPopup'):s.index('function renderPriorityPreview')])

# Urban redevelopment independent vs linked
urban_check=s[s.index('function checkUrbanRedevelopmentFromFacts'):s.index('// ---------- v2.5 remaining scheme modules')]
check('urban redevelopment has separate linked-policy row', "schemeRow('정책사업 연계추진'" in urban_check)
check('urban redevelopment has OR path summary row', "schemeRow('사업추진 경로 종합'" in urban_check and "independentStatus==='PASS'||linkedStatus==='PASS'" in urban_check)
check('urban redevelopment independent path is diagnostic not sole hard gate', "schemeRow('독립 추천 판단'" in urban_check and "independentAction,false" in urban_check)
check('possible-area diagnostic rows do not override linked path', "f.possible.boundary.note,false" in urban_check and "possibleRoute?ageFactNote(f.possible.age):'정비가능구역 포함 확인 후 적용',false" in urban_check)
check('old candidate hard-off override removed', '정책사업 하위/직접진입 미확인' not in s)
check('candidate display distinguishes linked route', "label:'연계추진 가능'" in s)
check('activation urban-redevelopment link uses available route not only selected route', 'activationRedevelopment?.status' in s and 'activationRedevelopment?.available===true' in s)
check('14-area boundary remains conservative until official GIS is connected', '14개 정비가능구역은 공식 경계 GIS가 연결되기 전까지 중심지명만으로 자동 PASS하지 않습니다.' in s)

failed=[n for n,v in checks if not v]
if failed:
    print(f'FAILED {len(failed)}/{len(checks)}')
    sys.exit(1)
print(f'ALL R16 TARGETED CHECKS PASS ({len(checks)})')
