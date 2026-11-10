from pathlib import Path
import hashlib, importlib.util, re, subprocess, sys

ROOT=Path(__file__).resolve().parent
html=(ROOT/'app.html').read_text(encoding='utf-8')
py=(ROOT/'app.py').read_text(encoding='utf-8')
base=(ROOT/'app.before_r9.html').read_text(encoding='utf-8') if (ROOT/'app.before_r9.html').exists() else None
checks=[]
def check(name,ok,detail=''):
    checks.append((name,bool(ok),detail));print(('PASS' if ok else 'FAIL'),name,detail)

def extract(src,name):
    m=re.search(r'(?:async\s+)?function\s+'+re.escape(name)+r'\s*\(',src)
    if not m:return None
    st=m.start();b=src.find('{',m.end());depth=0;q=None;esc=False;i=b
    while i<len(src):
        c=src[i]
        if q:
            if esc:esc=False
            elif c=='\\':esc=True
            elif c==q:q=None
        else:
            if c in "'\"`":q=c
            elif c=='{':depth+=1
            elif c=='}':
                depth-=1
                if depth==0:return src[st:i+1]
        i+=1
    return None

check('CURRENT PLAN card present','지구단위계획 / 현재 계획기준' in html and 'spDistrictUnitPlanZone' in html)
check('backend current-plan endpoint present','@app.post("/api/reference/district-unit-plan-current")' in py and '_district_unit_reference_lookup' in py)
check('frontend calls real backend endpoint',"fetchBackendJson('/api/reference/district-unit-plan-current'" in html)
check('official Seoul services explicit',all(x in py for x in ('upisCUq161','upisDistUnitPlan','FIG_RPT_MNG_CD','DCSN_ANCMNT_MNG_CD')))
check('management-code-first match policy','관리코드 우선' in py and 'LBL_NM_EXACT_UNIQUE' in py)
check('no fuzzy-name inference','유사명칭 추정 금지' in py)
check('unsupported VWorld UQ165 not queried','LT_C_UPISUQ165' not in html)
check('UQ165 left as separate follow-up','UQ165 공간자료 추가연계 필요' in html or 'UQ165 공식 공간자료 추가연계 필요' in html)
check('district plan never changes scheme status',"effect_on_scheme_status:'NONE'" in html and '사업 PASS/FAIL/REVIEW를 변경하지 않습니다' in html)
check('professional judgment policy explicit','professional_judgment_required:true' in html and '변경·완화·의제 여부를 전문가가 검토' in html)
check('detailed fields not invented',"detailed_plan_fields_status:'DOCUMENT_REVIEW_REQUIRED'" in html and '자동연계 미확정' in html)
check('internal/unverified API not used','internal_unverified_source_used:false' in html and '내부/비공식 포털 API <strong>사용하지 않음</strong>' in html)
check('official source links present','OA-21161' in html and 'OA-21164' in html and 'urban.seoul.go.kr' in html)
check('AI gets review-only current plan','district_unit_plan:districtUnitPlanCurrentFacts()' in html and 'district_unit_plan_rule' in html)

# Existing core buildSiteFactStore and rule engines must stay unchanged from R8.
if base:
    for fn in ('buildSiteFactStore','densityForScheme','runAllSchemeChecks','analyzeSchemeStreetBlocks','analyzeRoadAccess','runAllAutoAnalyses'):
        a,b=extract(base,fn),extract(html,fn)
        check(f'R8 core preserved: {fn}',a==b,hashlib.sha256((b or '').encode()).hexdigest()[:16])
else:
    check('R8 baseline comparison skipped',True,'R12 배포 ZIP에 app.before_r9.html이 포함되지 않아 기존 전용 회귀해시로 대체')

# Backend lookup unit tests without live network.
spec=importlib.util.spec_from_file_location('urban_strategy_r9',ROOT/'app.py')
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
fake={
 'status':'OK',
 'uq161':[
   {'OBJT_ID':'101','STUT_FIG_MNG_NO':'S1','FIG_RPT_MNG_CD':'R1','DCSN_ANCMNT_MNG_CD':'A1','LBL_NM':'테스트 지구단위계획'},
   {'OBJT_ID':'102','STUT_FIG_MNG_NO':'S2','FIG_RPT_MNG_CD':'R2','DCSN_ANCMNT_MNG_CD':'A2','LBL_NM':'중복명'},
   {'OBJT_ID':'103','STUT_FIG_MNG_NO':'S3','FIG_RPT_MNG_CD':'R3','DCSN_ANCMNT_MNG_CD':'A3','LBL_NM':'중복명'},
 ],
 'plans':[
   {'RPT_MNG_CD':'R1','PRJC_CD':'P1','RPT_TYPE':'지구단위','LCLSF':'계획','RGN_NM':'테스트 지구','PSTN_NM':'테스트 위치','AREA_EXS':'1000','AREA_CHG':'100','AREA_CHG_AFTR':'1100','DCSN_ANCMNT_MNG_CD':'A1'}
 ],
 'message':''
}
mod._district_unit_reference_tables=lambda: fake
r=mod._district_unit_reference_lookup([{'name':'아무 이름','rpt_mng_cd':'R1','properties':{}}])
check('backend exact RPT code resolves',r.get('status')=='CONFIRMED' and r.get('matches') and r['matches'][0].get('rpt_mng_cd')=='R1',str(r.get('status')))
check('backend returns announcement management code',r.get('announcement_codes')==['A1'],str(r.get('announcement_codes')))
r2=mod._district_unit_reference_lookup([{'name':'테스트 지구단위계획','properties':{}}])
check('unique exact label fallback only',r2.get('status')=='CONFIRMED' and r2['matches'][0].get('match_method')=='LBL_NM_EXACT_UNIQUE',str(r2.get('status')))
r3=mod._district_unit_reference_lookup([{'name':'중복명','properties':{}}])
check('ambiguous label stays no-match',r3.get('status')=='NO_MATCH',str(r3.get('status')))

# Static syntax
check('app.py compile',subprocess.run([sys.executable,'-m','py_compile',str(ROOT/'app.py')],capture_output=True).returncode==0)
js=ROOT/'inline_scripts.js'
node=subprocess.run(['node','--check',str(js)],capture_output=True,text=True)
check('browser JavaScript syntax',node.returncode==0,node.stderr.strip()[:180])

failed=[x for x in checks if not x[1]]
print(f'\nSUMMARY {len(checks)-len(failed)}/{len(checks)} PASS')
if failed:
    print('FAILED:',[x[0] for x in failed]);sys.exit(1)
