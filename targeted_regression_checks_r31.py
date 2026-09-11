from pathlib import Path
import re, subprocess, tempfile

html=Path('app.html').read_text(encoding='utf-8')
checks=[]
def check(name, cond):
    if not cond: raise AssertionError(name)
    print('PASS',name); checks.append(name)

# 1. Site-analysis card: three concepts are separate and visible.
check('industrial-factory card renamed', '<b>산업 및 공장비율</b>' in html)
check('factory legal ratio metric exists', 'id="spFactoryRatio"' in html)
check('industrial ratio metric exists', 'id="spIndustrialRatio"' in html)
check('factory building ratio metric exists', 'id="spFactoryBuildingRatio"' in html)
check('three metrics non-substitution warning', '세 지표는 서로 대체하지 않습니다' in html)
check('factory legal manual input dated', '공장비율(2008.1.31, %)' in html)
check('industrial ratio manual input exists', 'id="scheme_industrial_ratio"' in html)

# 2. Distinct calculations / legal gate separation.
check('factory current proxy separated', 'factory_ratio_current_proxy_pct' in html and 'factory_ratio_reference_date' in html)
check('factory legal ratio uses manual only', 'factory_ratio_pct:manual' in html and "factory_ratio_status:manual!=null?'CONFIRMED':'REVIEW'" in html)
check('factory building ratio independent', 'factory_building_ratio_pct:buildingPct' in html)
check('industrial ratio calculator exists', 'function computeIndustrialUsageFact' in html and 'lower_pct:lower' in html and 'upper_pct:upper' in html)
check('industrial mixed-building 50 percent whole-parcel rule encoded', 'knownShare>=0.5?info.area' in html and 'possibleShare>=0.5?info.area' in html)
check('scheme fact uses legal factory ratio', 'pct:fu.factory_ratio_pct??null' in html)
check('innovation housing displays legal and building ratios separately', "spatialFactRow('법정 공장비율'" in html and "spatialFactRow('공장용도 건축물 비율(참고)'" in html)

# 3. Old 2030 link is not a REVIEW gate anymore.
check('old 2030 semiindustrial text removed', '2030 준공업지역 종합발전계획' not in html and '준공업 종합발전계획' not in html)
check('public complex links 2040 info only', "schemeRow('공업지역기본계획 연계'" in html and "2040 서울 공업지역기본계획" in html)
check('public complex 2040 row nonmandatory info', re.search(r"schemeRow\('공업지역기본계획 연계'.*?'INFO'.*?false", html, re.S) is not None)

# 4. Dedicated semiindustrial branch and industrial-law schemes.
check('semiindustrial panel exists', 'id="semiindustrialReviewPanel"' in html and 'id="semiindustrialReviewGrid"' in html)
check('semiindustrial fact builder exists', 'function buildSemiindustrialDevelopmentFacts' in html)
check('industrial park applicability gate exists', 'function semiindustrialBasicPlanApplicability' in html and 'LT_C_DAMDAN' in html and "==='UQ1400'" in html and "==='UQ5700'" in html and 'NOT_APPLICABLE' in html)
app_py=Path('app.py').read_text(encoding='utf-8')
check('vworld industrial park boundary layer wired', 'VWORLD_LAYER_INDUSTRIAL_PARK = "LT_C_DAMDAN"' in app_py and 'def analyze_industrial_park_intersections' in app_py and '"industrial_parks": industrial_parks' in app_py)
check('latest UQ181 reference month wired', '"reference_month": "2026-09"' in app_py)
check('development facts preserve industrial park code and label', "code:p.code||''" in html and "type_label:p.type_label||''" in html)
check('industrial innovation scheme exists', "title:'산업혁신구역'" in html and 'openSemiindustrialSchemeDetail(\'innovation\')' in html)
check('industrial renewal scheme exists', "title:'산업정비구역'" in html and 'openSemiindustrialSchemeDetail(\'renewal\')' in html)
check('innovation 5000 and single parcel exception encoded', "area>=5000" in html and "parcelCount===1" in html and '단일필지 예외' in html)
check('industrial renewal 5000 encoded', "area>=5000?'PASS':'FAIL'" in html and '산업정비구역 5,000㎡ 이상' in html)
check('industrial ratio 10 route encoded', "semiindustrialRatioThresholdFact(iu,10,'gte')" in html)
check('management type hidden as internal fact', '관리유형은 내부 참고' in html and 'managementHint' in html)
check('semiindustrial panel rendered after candidate', html.find("safeSchemeUiStep('candidate'") < html.find("safeSchemeUiStep('semiindustrial-review'"))
check('semiindustrial fact exposed to analysis state', 'analysisState.semiindustrial_review=store.family_specific.semiindustrial' in html)

# 5. Existing applicable schemes are explicitly grouped in the semiindustrial panel.
for label in ['재건축','주택재개발','도시정비형 재개발','소규모재개발','안심주택','역세권 장기전세','도심공공주택복합 · 주거산업융합','도심복합 주거중심형','사전협상(선택)']:
    check(f'semiindustrial panel includes {label}', label in html)
check('prior negotiation stays optional', "prior.status='INFO';prior.label='선택검토'" in html and '다른 사업을 배제하지 않음' in html)

# 6. Previous baseline anchors retained.
check('R30 policy labels retained', '사업추진 전제조건' in html and '계획반영필요' in html)
check('R29 project registry retained', 'spExistingProjectLegend' in html and 'projectRegistryOverlaps' in html)
check('R28 frontend performance retained', 'frontend_postprocess_ms' in html)
check('R27 alternative retained', '대안추천' in html)
check('R26 area tolerance retained', 'CANDIDATE_AREA_SHORTFALL_TOLERANCE_PCT' in html)
check('R24 parcel click retained', 'parcelFindMode' in html)

# 7. Semantic smoke test for semiindustrial thresholds/size branches.
def extract_function(src,name):
    marker=f'function {name}('
    start=src.find(marker)
    if start<0: raise AssertionError('missing '+name)
    nxt=src.find('\nfunction ',start+len(marker))
    if nxt<0: raise AssertionError('next function missing after '+name)
    return src[start:nxt].strip()

fnames=['semiindustrialRatioThresholdFact','semiindustrialBuildingAgeFact','semiindustrialFacilityAgeFact','semiindustrialBasicPlanApplicability','buildSemiindustrialDevelopmentFacts']
funcs='\n'.join(extract_function(html,n) for n in fnames)
js=f"""
function industrialUsageDisplay(iu){{if(iu.industrial_ratio_pct!=null)return iu.industrial_ratio_pct.toFixed(1)+'%';return iu.lower_pct.toFixed(1)+'~'+iu.upper_pct.toFixed(1)+'%';}}
function boundedRatio(records,classifier){{let total=0,old=0,unknown=0;for(const r of records){{total++;const s=classifier(r);if(s==='OLD')old++;else if(s!=='NOT_OLD')unknown++;}}if(!total)return {{total:0,old:0,unknown:0,lower:null,upper:null}};return {{total,old,unknown,known:total-unknown,ratio:unknown?null:old/total*100,lower:old/total*100,upper:(old+unknown)/total*100}};}}
function thresholdStatus(stat,t){{if(stat.lower==null||stat.upper==null)return 'REVIEW';if(stat.lower>=t)return 'PASS';if(stat.upper<t)return 'FAIL';return 'REVIEW';}}
function rawFactAgeState(r,y){{return r.age>=y?'OLD':'NOT_OLD';}}
function industrialRecordClass(r){{return r.ind?'INDUSTRIAL':'NONINDUSTRIAL';}}
function fmtSchemeArea(v){{return v==null?'-':String(v)+'㎡';}}
{funcs}
function assert(c,m){{if(!c){{console.error(m);process.exit(1);}}}}
let store={{common:{{zoning:'준공업',area:6000}},site:{{development:{{loaded:true,overlaps:[],industrial_parks:[],industrial_park_metadata:{{available:true}}}},parcel_count:2,industrial_usage:{{industrial_ratio_pct:12,lower_pct:12,upper_pct:12}},factory_usage:{{factory_ratio_current_proxy_pct:80}},building:{{records:[{{age:25,ind:true}},{{age:22,ind:true}}]}}}}}};
let f=buildSemiindustrialDevelopmentFacts(store);
assert(f.active===true,'not active');
assert(f.innovation.area.status==='PASS','innovation size expected pass');
assert(f.innovation.target.status==='PASS','innovation target expected pass');
assert(f.innovation.status==='PASS','innovation should pass when official industrial-park layer is available and no overlap');
assert(f.renewal.subtypes.industrial_residential.status==='PASS','mixed route expected pass');
assert(f.renewal.status==='PASS','renewal should pass when official industrial-park layer is available and subtype passes');
store={{common:{{zoning:'준공업',area:4000}},site:{{development:{{loaded:true,overlaps:[],industrial_parks:[],industrial_park_metadata:{{available:true}}}},parcel_count:2,industrial_usage:{{industrial_ratio_pct:8,lower_pct:8,upper_pct:8}},factory_usage:{{factory_ratio_current_proxy_pct:80}},building:{{records:[{{age:25,ind:true}}]}}}}}};
f=buildSemiindustrialDevelopmentFacts(store);
assert(f.innovation.status==='FAIL','multi parcel under 5000 innovation fail');
assert(f.renewal.status==='FAIL','renewal under 5000 fail');
assert(f.management_hint.value==='주거정비형 가능성','under 10 hint');
store.site.parcel_count=1;
f=buildSemiindustrialDevelopmentFacts(store);
assert(f.innovation.area.status==='PASS','single parcel exception not applied');
assert(f.renewal.area.status==='FAIL','single parcel must not waive renewal 5000');
store={{common:{{zoning:'준공업',area:6000}},site:{{development:{{loaded:true,overlaps:[],industrial_parks:[{{name:'테스트산업단지',type_label:'일반산업단지',overlap_area_m2:6000,overlap_pct:100}}],industrial_park_metadata:{{available:true,source_layer:'LT_C_DAMDAN'}}}},parcel_count:1,industrial_usage:{{industrial_ratio_pct:20,lower_pct:20,upper_pct:20}},factory_usage:{{factory_ratio_current_proxy_pct:100}},building:{{records:[{{age:25,ind:true}}]}}}}}};
f=buildSemiindustrialDevelopmentFacts(store);
assert(f.basic_plan_applicability.status==='NOT_APPLICABLE','industrial park should be excluded from basic plan');
assert(f.innovation.status==='NOT_APPLICABLE','industrial park innovation should be N/A');
assert(f.renewal.status==='NOT_APPLICABLE','industrial park renewal should be N/A');
store.site.development.industrial_parks[0].overlap_pct=30;store.site.development.industrial_parks[0].overlap_area_m2=1800;
f=buildSemiindustrialDevelopmentFacts(store);
assert(f.basic_plan_applicability.status==='REVIEW','partial industrial park overlap should require split review');
assert(f.innovation.status==='REVIEW','partial overlap innovation should review');
console.log('SEMANTIC PASS');
"""
with tempfile.NamedTemporaryFile('w',suffix='.js',encoding='utf-8',delete=False) as tf:
    tf.write(js); path=tf.name
r=subprocess.run(['node',path],capture_output=True,text=True)
if r.returncode!=0: raise AssertionError('semantic R31 failed: '+r.stdout+r.stderr)
check('semantic semiindustrial thresholds and area branches', 'SEMANTIC PASS' in r.stdout)

print(f'ALL R31 TARGETED CHECKS PASS ({len(checks)}/{len(checks)})')
