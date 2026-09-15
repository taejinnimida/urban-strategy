from pathlib import Path
import json, re, subprocess, sys, importlib.util
ROOT=Path(__file__).resolve().parent
HTML=(ROOT/'app.html').read_text(encoding='utf-8')
PYAPP=(ROOT/'app.py').read_text(encoding='utf-8')
checks=[]
def ck(name, cond):
    ok=bool(cond); checks.append((name,ok)); print(('PASS ' if ok else 'FAIL ')+name); return ok
# Backend/data additions
ck('R40 backend floor-batch endpoint', '@app.post("/api/building-hub/floor-batch")' in PYAPP)
ck('R40 backend floor outline operation', 'getBrFlrOulnInfo' in PYAPP)
ck('R40 backend innovation land-use categories', 'urban_regeneration_innovation' in PYAPP and 'housing_regeneration_innovation' in PYAPP)
ck('R40 backend safe downtown endpoint', '@app.post("/api/spatial/safe-downtown-exclusion")' in PYAPP)
# Runtime import helpers
spec=importlib.util.spec_from_file_location('r40app', ROOT/'app.py'); mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
ck('R40 land-use category urban innovation', mod._land_use_category('도시재생혁신지구')=='urban_regeneration_innovation')
ck('R40 land-use category housing innovation', mod._land_use_category('주거재생혁신지구')=='housing_regeneration_innovation')
ref=mod._safe_downtown_exclusion_reference_data(); meta=ref.get('metadata',{})
ck('R40 safe downtown reference available', meta.get('available') is True and len(ref.get('features') or [])==2)
ck('R40 safe downtown explicitly non-legal', meta.get('legal_source') is False and meta.get('source_type')=='USER_CURATED_MODEL_REFERENCE')
# Core frontend FACT pipeline
ck('R40 urban regeneration analysis state', 'const urbanRegenerationRestrictionAnalysis=' in HTML)
ck('R40 urban regeneration NED analyzer', 'async function analyzeUrbanRegenerationRestrictions()' in HTML and '/api/spatial/land-use-restrictions' in HTML)
ck('R40 urban regeneration pipeline step', "safeAnalysisStep('도시재생 혁신지구'" in HTML)
ck('R40 urban regeneration fact store', 'urban_regeneration:urbanRegenerationRestrictionSpatialEvidenceFacts()' in HTML)
# Public complex final scope
fn=HTML[HTML.index('function publicComplexUrbanRegenerationFact'):HTML.index('function publicComplexSpatialFacts')]
ck('R40 public complex only two innovation types', '도시재생혁신지구|주거재생혁신지구' in fn)
ck('R40 public complex recognition project removed from gate', '인정사업' not in fn)
ck('R40 public complex activation area not hard fail', '도시재생활성화지역' in fn and '단순 중첩은 배제하지 않음' in fn)
ck('R40 public complex five separate criteria', all(x in HTML for x in ['과소토지 90㎡ 이하 30% 이상','호수밀도 50호/ha 이상','주택접도율 50% 이하','방재지구 면적 1/2 이상','지하층 주거사용 건축물 1/2 이상']))
ck('R40 public complex urban regen label cleaned', '도시재생 인정사업·혁신지구' not in HTML and "schemeRow('도시재생 혁신지구 배제'" in HTML)
# Housing / shared / safe / longterm
ck('R40 general housing district-plan aging', "schemeRow('지구단위계획 노후도'" in HTML and "resolvedSchemeAgeFact(store,'general_housing')" in HTML)
ck('R40 general housing semiindustrial zoning passes route', "'준공업'" in HTML[HTML.index('function generalHousingSpatialFacts'):HTML.index('function checkGeneralHousingFromFacts')])
ck('R40 shared candidate model', 'function sharedHousingAutomaticTargetFacts' in HTML and 'Number(far.pct)<=200' in HTML)
ck('R40 shared 200pct explicitly platform reference', '200%는 법정 진입요건이 아닌 플랫폼 저이용 후보참조' in HTML)
ck('R40 safe medical 50pct boundary logic', 'coverage_350_pct>=50' in HTML and 'negative_complete===true' in HTML)
ck('R40 safe downtown DXF linked', 'USER_CURATED_MODEL_REFERENCE' in HTML and "schemeRow('서울도심 배제범위'" in HTML)
ck('R40 safe concentration hard gate absent', "schemeRow('사업집중도'" not in HTML)
ck('R40 longterm zoning/factory split', "schemeRow('용도지역'" in HTML and "schemeRow('준공업 법정 공장비율'" in HTML)
# Frontage and plan variables
ck('R40 residential environment deadend exception', 'width4_deadend35_width6' in HTML and '35m 이상 막다른도로' in HTML)
resfn=HTML[HTML.index('function residentialEnvironmentSpatialFacts'):HTML.index('function checkResidentialEnvironmentFromFacts')]
ck('R40 residential method removed from spatial hard rows', "spatialFactRow('주거환경개선 시행방법'" not in resfn)
ck('R40 residential method is future review info', "'추후 검토','INFO'" in HTML[HTML.index('function checkResidentialEnvironmentFromFacts'):HTML.index('function generalHousingSpatialFacts')])
redfn=HTML[HTML.index('function redevelopmentSpatialFacts'):HTML.index('function checkRedevelopmentFromFacts')]
ck('R40 redevelopment frontage criterion connected', "criterionStatus:frontageStatus" in redfn and '주택접도율(6m 기준)' in redfn)
# Autonomous / urban redevelopment
smallfn=HTML[HTML.index('function smallscaleAutonomousTargetFacts'):HTML.index('function smallscaleSpatialFacts')]
ck('R40 autonomous known negative becomes fail', "return {status:knownBase?'FAIL':'REVIEW'" in smallfn)
urbanfn=HTML[HTML.index('function urbanRedevelopmentSpatialFacts'):HTML.index('function checkUrbanRedevelopmentFromFacts')]
ck('R40 urban redevelopment reuses center fact', 'isUrbanRedevelopmentPossibleCenter(c)' in urbanfn and "source:'centers.json'" in urbanfn)
ck('R40 urban redevelopment zoning 2000 age gates', "['준주거','준공업','근린상업','일반상업','중심상업']" in urbanfn and 'Number(c.area)>=2000' in urbanfn and "age?.status==='PASS'" in urbanfn)
ck('R40 selected center list includes all grouped centers', all(x in HTML for x in ['영등포','강남','가산','대림','용산','청량리','왕십리','잠실','창동','상계','신촌','연신내','불광','사당','이수','성수','봉천','천호','길동','동대문']))
# Fake review / info renderer
ck('R40 INFO preserved in spatial fact row', "state:state==='CONFIRMED'?'CONFIRMED':state==='INFO'?'INFO':'REVIEW'" in HTML)
ck('R40 INFO renderer label', "if(d.status==='INFO')return '참고'" in HTML)
ck('R40 INFO css decision class path', "d.status==='INFO'?'INFO':'REVIEW'" in HTML)
# Syntax
subprocess.run([sys.executable,'-m','py_compile',str(ROOT/'app.py')],check=True)
ck('R40 backend python syntax', True)
js=ROOT/'_r40_inline_check.js'
parts=[m.group(2) for m in re.finditer(r'<script([^>]*)>(.*?)</script>',HTML,re.S|re.I) if 'src=' not in m.group(1).lower()]
js.write_text('\n'.join(parts),encoding='utf-8')
r=subprocess.run(['node','--check',str(js)],capture_output=True,text=True)
ck('R40 inline javascript syntax', r.returncode==0)
try: js.unlink()
except: pass
passed=sum(ok for _,ok in checks); total=len(checks)
if passed!=total:
    print(f'R40 TARGETED CHECKS FAILED ({passed}/{total})'); sys.exit(1)
print(f'ALL R40 TARGETED CHECKS PASS ({passed}/{total})')
