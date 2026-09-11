from pathlib import Path
import re, subprocess, tempfile, textwrap

html = Path('app.html').read_text(encoding='utf-8')
checks=[]
def check(name, cond):
    if not cond:
        raise AssertionError(name)
    print('PASS', name)
    checks.append(name)

# 1) Global policy helpers and exact user-facing wording
check('global decision policy helper exists', 'function schemeDecisionPolicyRole' in html)
check('consent prerequisite exact label exists', "label:'사업추진 전제조건'" in html)
check('plan required exact label exists', "label:'계획반영필요'" in html)
check('schemeRow neutralizes prerequisite', "if(policyRole==='PREREQUISITE')" in html and "finalStatus='INFO';required=false;" in html)
check('schemeRow neutralizes planning criterion', "else if(policyRole==='PLAN')" in html and html.count("finalStatus='INFO';required=false;") >= 2)

# 2) Do not over-neutralize road FACT rules that were already automated in R27.
policy_match = re.search(r"function schemeDecisionPolicyRole\(.*?\n\}", html, re.S)
check('policy helper captured', policy_match is not None)
policy_text = policy_match.group(0)
check('road planning is not globally converted to plan variable', '|도로계획' not in policy_text)

# 3) Known plan-variable modules
check('general housing planned units are non-gating', "let approvalStatus='INFO'" in html and "'인허가 경로·세대수'" in html and "policyRole:'PLAN'" in html)
check('longterm planned units are plan-required', "schemeRow('계획 세대수'" in html and "100세대 이상은 사업계획 수립" in html)
check('longterm consent is prerequisite', "schemeRow('사업추진 동의'" in html and "policyRole:'PREREQUISITE'" in html)
check('shared housing sub-3000 unit path is plan-required', "부지면적 3,000㎡ 미만이면 공동주택 100세대 이상 계획" in html and "policyRole:'PLAN'" in html)
check('innovation housing planned floor share is plan-required', "schemeRow('주택 연면적'" in html and "planned_floor_pct" in html and "policyRole:'PLAN'" in html)
check('safe low-zone route no longer depends on entered elder supply', 'const locationExempt=specialLowZone;' in html)
check('safe elder supply is plan-required in fact and rule views', "계획반영필요 · 어르신 100% 공급" in html and "schemeRow('어르신 공급조건'" in html and "policyRole:'PLAN'" in html)

# 4) Existing factual housing counts remain statutory facts, not planning variables.
check('smallscale existing housing count remains a criterion', "smallscaleCriterion('기존주택수'" in html)
check('small reconstruction existing unit count remains a criterion', "smallscaleCriterion('기존주택 세대수','200세대 미만'" in html)

# 5) Rendering/AI keep the informational rows visible but out of gaps.
check('detailed sheet retains policy info rows', "r.status!=='INFO'||!!r.policyRole" in html and "r.status==='INFO'&&!r.policyRole" in html)
check('ai separates plan required items', 'planning_required=rows.filter' in html and "policyRole==='PLAN'" in html)
check('ai separates prerequisites', 'prerequisites=rows.filter' in html and "policyRole==='PREREQUISITE'" in html)

# 6) Previous baseline anchors retained.
check('R29 existing project registry retained', 'spExistingProjectLegend' in html and 'projectRegistryOverlaps' in html)
check('R28 frontend performance metric retained', 'frontend_postprocess_ms' in html)
check('R27 candidate alternative retained', '대안추천' in html)
check('R26 area shortfall tolerance retained', 'CANDIDATE_AREA_SHORTFALL_TOLERANCE_PCT' in html)
check('R24 parcel click retained', 'parcelFindMode' in html)

# 7) Semantic JS test: consent and plan rows can never depress overall; factual FAIL still does.
def extract_function(src, name):
    marker = f'function {name}('
    start = src.find(marker)
    if start < 0:
        raise AssertionError(f'missing function {name}')
    nxt = src.find('\nfunction ', start + len(marker))
    if nxt < 0:
        raise AssertionError(f'next function not found after {name}')
    return src[start:nxt].strip()

funcs='\n'.join(extract_function(html,n) for n in ['schemeDecisionPolicyRole','schemeRowDisplay','schemeRow','overallScheme'])
js = f"""
const RULE_SOURCE_CATALOG={{X:{{type:'공식',title:'테스트',effective:'2026-09-11',url:'',verified:true}}}};
const activeRuleScheme='test';
function ruleSourceFor(){{return {{id:'X',source:RULE_SOURCE_CATALOG.X}};}}
function ruleGapText(){{return 'gap';}}
function ruleActionText(){{return 'action';}}
function schemeAlternativeText(){{return '';}}
function ruleTrust(status,value,note,source){{return {{code:'AUTO_CONFIRMED',label:'자동확정'}};}}
function ruleLocatorFor(){{return '-';}}
{funcs}
const consent=schemeRow('토지등소유자 동의율','토지등소유자 2/3 이상','10%','FAIL','미달',true,{{sourceId:'X'}});
const plan=schemeRow('계획 세대수','100세대 이상','20세대','FAIL','미달',true,{{sourceId:'X'}});
const area=schemeRow('대상지 면적','5,000㎡ 이상','4,000㎡','FAIL','미달',true,{{sourceId:'X'}});
const road=schemeRow('간선도로변 도로계획','2면 접도 + 6m 이상','1면','FAIL','현황 미달',true,{{sourceId:'X'}});
function assert(c,m){{if(!c){{console.error(m);process.exit(1);}}}}
assert(consent.status==='INFO' && consent.required===false && consent.policyRole==='PREREQUISITE','consent policy failed');
assert(schemeRowDisplay(consent).label==='사업추진 전제조건','consent display failed');
assert(plan.status==='INFO' && plan.required===false && plan.policyRole==='PLAN','plan policy failed');
assert(schemeRowDisplay(plan).label==='계획반영필요','plan display failed');
assert(area.status==='FAIL' && area.required===true && !area.policyRole,'factual area must remain gate');
assert(road.status==='FAIL' && road.required===true && !road.policyRole,'road FACT must remain gate');
assert(overallScheme([consent,plan,schemeRow('용도지역','허용','허용','PASS','',true,{{sourceId:'X'}})])==='PASS','policy rows depressed overall');
assert(overallScheme([consent,plan,area])==='FAIL','factual fail did not control overall');
console.log('SEMANTIC PASS');
"""
with tempfile.NamedTemporaryFile('w', suffix='.js', encoding='utf-8', delete=False) as f:
    f.write(js); js_path=f.name
r=subprocess.run(['node',js_path],capture_output=True,text=True)
if r.returncode!=0:
    raise AssertionError('semantic JS policy test failed: '+r.stdout+r.stderr)
check('semantic consent/plan policy and factual gate', 'SEMANTIC PASS' in r.stdout)

print(f'ALL R30 TARGETED CHECKS PASS ({len(checks)}/{len(checks)})')
