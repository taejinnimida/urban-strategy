from pathlib import Path
import re, subprocess, json, tempfile

HTML = Path('app.html').read_text(encoding='utf-8')
PYCODE = Path('app.py').read_text(encoding='utf-8')
ALL = HTML + '\n' + PYCODE
checks=[]
def check(name, cond):
    if not cond:
        raise AssertionError(name)
    print('PASS', name); checks.append(name)

# Candidate recommendation state separation
check('secondary pass has alternative display state', "kind:'alternative'" in HTML and "label:'대안추천'" in HTML)
check('alternative is visually viable not candidate-off', "kind:'alternative',css:'candidate-on'" in HTML)
check('alternative included in live candidate ranking', "['available','alternative','adjustable'].includes(candidateDisplayState(n).kind)" in HTML)
check('alternative included in top3', "['available','alternative','adjustable'].includes(display.kind)" in HTML)
check('alternative receives PASS for AI explanation', "['available','alternative'].includes(display?.kind)" in HTML)
check('alternative no longer mapped to automatic noncompliance', "if(st.state==='mid')return {kind:'unavailable',css:'candidate-off',label:'자동판정 미충족'" not in HTML)
check('candidate summary reports alternatives separately', '대안추천 ${altCount}개' in HTML)

# Road fact UI separation
check('road estimate positive candidate can be known', "roadQuality==='ESTIMATE'&&candidates.length>0" in HTML)
check('road estimate negative candidate is guarded', "roadQuality==='AUTO'||(roadQuality==='ESTIMATE'&&candidates.length>0)" in HTML)
check('longterm road summary uses width and perimeter fact', "fact_key:'longtermArterialWidthFact'" in HTML and "폭20m 이상 도로 + 구역둘레 1/8 접면 공간기준" in HTML)
check('longterm intersection remains separate rule', '교차지 200m 및 특별시도 주·보조간선 여부는 장기전세 입지 Rule에서 별도 판정' in HTML)
check('innovation housing road summary does not require 6m block', "innovationHousingWidthOk" in HTML and "6m 이상 도로로 둘러싸인 일단의 지구 여부와 법정 도로위계는 주거중심형 독립 Rule에서 별도 판정" in HTML)
check('not applicable road rule has neutral display', "status==='NOT_APPLICABLE'||status==='INFO'" in HTML and "label:'비해당'" in HTML)
check('road summary caption separates spatial fact and legal hierarchy', '도로 폭원·접면 공간 Fact' in HTML and '이 표의 폭원·접면 결과를 확인필요로 되돌리지 않습니다' in HTML)
check('AI does not globally downgrade ESTIMATE road facts', "도로·접도 자료 ${analysisState.quality?.road||'REVIEW'} · 추가 확인 필요" not in HTML)
check('AI still flags actual NO_DATA road facts', "String(analysisState.quality?.road||'NO_DATA')==='NO_DATA'" in HTML)

# Preserve recent baseline work
for name, needle in [
    ('R26 area 10 percent filter retained','CANDIDATE_AREA_SHORTFALL_TOLERANCE_PCT=10'),
    ('R25 activation block relation retained','stationBlockRelation'),
    ('R24 parcel click UI retained','parcelFindMode'),
    ('R23 neighbor graph retained','_ensure_basic_unit_neighbors'),
    ('R22 prepared geometry retained','prepared_build_ms'),
    ('R21 4m frontage retained','frontage_ratio_4m'),
    ('R20 area prefilter retained','analysis_area_cut_m2:24000')
]:
    check(name, needle in ALL)

# Semantic smoke test for candidateDisplayState using the exact function source.
m = re.search(r"function candidateDisplayState\(name,st=safeCandidateState\(name\)\)\{(.*?)\n\}\nfunction ccContextFor", HTML, re.S)
check('candidateDisplayState source extractable', bool(m))
func = "function candidateDisplayState(name,st=safeCandidateState(name)){" + m.group(1) + "\n}"
js = f"""
const SHELL_SCHEMES=new Set();
function candidateChangeOpportunity(){{return null;}}
function safeCandidateState(){{return null;}}
{func}
const alt=candidateDisplayState('activation',{{ready:true,state:'mid',stage:'ALTERNATIVE',regulatory:'PASS',structural:'PASS',areaGate:'PASS'}});
const fail=candidateDisplayState('x',{{ready:true,state:'mid',stage:'REGULATION',regulatory:'FAIL',structural:'PASS',areaGate:'PASS',failureDetail:'법정기준 미달'}});
const rev=candidateDisplayState('x',{{ready:true,state:'review',stage:'REGULATION',regulatory:'REVIEW',structural:'PASS',areaGate:'PASS'}});
if(alt.kind!=='alternative'||alt.css!=='candidate-on') throw new Error('alternative semantic failure');
if(fail.kind!=='unavailable') throw new Error('regulation fail semantic failure');
if(rev.kind!=='pending') throw new Error('review semantic failure');
console.log('SEMANTIC PASS');
"""
Path('_r27_semantic.js').write_text(js,encoding='utf-8')
r=subprocess.run(['node','_r27_semantic.js'],capture_output=True,text=True)
check('candidate state semantic smoke', r.returncode==0 and 'SEMANTIC PASS' in r.stdout)
Path('_r27_semantic.js').unlink(missing_ok=True)

print(f'ALL R27 TARGETED CHECKS PASS ({len(checks)}/{len(checks)})')
