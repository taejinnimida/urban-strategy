from pathlib import Path
import re, subprocess, json
ROOT=Path(__file__).resolve().parent
HTML=(ROOT/'app.html').read_text(encoding='utf-8')
PYTXT=(ROOT/'app.py').read_text(encoding='utf-8')

def ok(cond,name,detail=''):
    if not cond:
        raise AssertionError(f'{name}: {detail or "FAILED"}')
    print('PASS',name)

# R11 structure preserved
for key in ['autonomous','block','reconstruction','redevelopment']:
    ok(re.search(rf"{key}:\{{label:.*?criteria:",HTML,re.S) is not None,f'{key} has independent criteria')
ok('function renderSmallscaleSchemeDetailPopup()' in HTML,'custom smallscale popup renderer present')
ok("const legalKeys=['autonomous','block','reconstruction','redevelopment'];" in HTML,'popup has four statutory tabs')
ok("const statutoryStatuses=['autonomous','block','reconstruction','redevelopment'].map" in HTML,'Moa excluded from statutory aggregate')

# Current-law area architecture
ok("const blockAreaBasicLimit=moa==='designated'?40000:13000;" in HTML,'street-block basic limit: Seoul 13k / management 40k')
ok("const blockAreaConditionalLimit=moa==='designated'?40000:20000;" in HTML,'street-block conditional general route up to 20k')
ok("const blockSiteAreaBasicLimit=moa==='designated'?20000:10000;" in HTML,'project-area basic limit: general 10k / management 20k')
ok("const blockSiteAreaConditionalLimit=moa==='designated'?40000:20000;" in HTML,'project-area conditional limit: general 20k / management 40k')
ok('공공시행+공공임대 10%' in HTML,'conditional public-implementation/public-rental rule exposed')
ok('지방도시계획위원회 심의 시 20,000㎡ 미만 가능' in HTML,'street-block 13k-to-20k committee route exposed')

# Conditional status is not mislabelled as generic data REVIEW in popup
ok("r.conditional?'조건부':'확인필요'" in HTML,'criterion conditional label present')
ok("rr.conditional?'조건부':'확인필요'" in HTML,'route conditional label present')
ok("conditional:(blockAreaConditional||blockSiteAreaConditional)&&blockStatus==='REVIEW'" in HTML,'block route carries conditional metadata')

# R9/R10 backend compatibility bundled in this patch
ok('@app.post("/api/land/ledger-one")' in PYTXT,'ledger proxy backend present')
ok('@app.post("/api/land/characteristics-one")' in PYTXT,'land-characteristics proxy backend present')
ok('barrier_candidates: List[tuple[Dict[str, Any], Any]]' in PYTXT,'R10 street-block barrier backend retained')
ok('def _reference_data_readiness()' in PYTXT,'health reference-data diagnostics retained')

# Heavy spatial analysis remains serial and street block stays first
run=re.search(r'async function runSiteReview\(\)\{.*?\n\}',HTML,re.S)
ok(bool(run),'runSiteReview found')
blk=run.group(0)
ok('streetBlockPromise' not in blk and 'streetBlockBackground' not in blk,'no street-block background parallelization')
ok("await safeAnalysisStep('제도별 가로구역'" in blk,'street-block remains awaited')
ok(blk.find("'제도별 가로구역'") < blk.find("'연속지적'"),'street-block before parcel analysis')

# Dynamic threshold smoke test (mirrors the explicit expressions in app.html)
js=r'''
function calc(moa,blockArea,siteArea){
 const blockAreaBasicLimit=moa==='designated'?40000:13000;
 const blockAreaConditionalLimit=moa==='designated'?40000:20000;
 const blockAreaConditional=blockArea!=null&&moa!=='designated'&&blockArea>=blockAreaBasicLimit&&blockArea<blockAreaConditionalLimit;
 const blockAreaStatus=blockArea==null?'REVIEW':blockArea<blockAreaBasicLimit?'PASS':blockArea<blockAreaConditionalLimit?'REVIEW':'FAIL';
 const blockSiteAreaBasicLimit=moa==='designated'?20000:10000;
 const blockSiteAreaConditionalLimit=moa==='designated'?40000:20000;
 const blockSiteAreaConditional=siteArea!=null&&siteArea>=blockSiteAreaBasicLimit&&siteArea<blockSiteAreaConditionalLimit;
 const blockSiteAreaStatus=siteArea==null?'REVIEW':siteArea<blockSiteAreaBasicLimit?'PASS':siteArea<blockSiteAreaConditionalLimit?'REVIEW':'FAIL';
 return {blockAreaStatus,blockAreaConditional,blockSiteAreaStatus,blockSiteAreaConditional};
}
console.log(JSON.stringify({
 g12:calc('none',12000,9000),g15:calc('none',15000,15000),g21:calc('none',21000,21000),
 m35:calc('designated',35000,15000),m30:calc('designated',35000,30000),m41:calc('designated',35000,41000)
}));
'''
proc=subprocess.run(['node','-e',js],capture_output=True,text=True,check=True)
d=json.loads(proc.stdout.strip())
ok(d['g12']['blockAreaStatus']=='PASS' and d['g12']['blockSiteAreaStatus']=='PASS','general basic route dynamic')
ok(d['g15']['blockAreaStatus']=='REVIEW' and d['g15']['blockAreaConditional'] and d['g15']['blockSiteAreaConditional'],'general 13-20k / 10-20k conditional dynamic')
ok(d['g21']['blockAreaStatus']=='FAIL' and d['g21']['blockSiteAreaStatus']=='FAIL','general above 20k fails dynamic')
ok(d['m35']['blockAreaStatus']=='PASS' and d['m35']['blockSiteAreaStatus']=='PASS','management block 35k + site 15k basic dynamic')
ok(d['m30']['blockAreaStatus']=='PASS' and d['m30']['blockSiteAreaStatus']=='REVIEW' and d['m30']['blockSiteAreaConditional'],'management 20-40k site conditional dynamic')
ok(d['m41']['blockSiteAreaStatus']=='FAIL','management site >=40k fails dynamic')

print('ALL R12 TARGETED CHECKS PASSED')
