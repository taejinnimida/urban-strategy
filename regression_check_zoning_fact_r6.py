from pathlib import Path
import hashlib, re, subprocess, sys, json
ROOT=Path(__file__).resolve().parent
html=(ROOT/'app.html').read_text(encoding='utf-8')
base=Path('/mnt/data/r5_buginspect/app.html').read_text(encoding='utf-8')
checks=[]
def check(name,ok,detail=''):
    checks.append((name,bool(ok),detail));print(('PASS' if ok else 'FAIL'),name,detail)

check('zoning critical progress step added',"'용도지역 핵심 FACT'" in html and "{label:'용도지역',sources:['용도지역 핵심 FACT']}" in html)
check('bulk planning no longer claims zoning success by layer count alone','응답 ${v.ok}/${v.queried} 레이어 · 용도지역 ${Number(v?.zoning_count||0)}건' in html)
check('critical zoning dedicated one-layer recheck',"PLANNING_LAYER_SPECS.find(x=>x.id==='LT_C_UQ111')" in html and 'fetchPlanningSpec(spec,activeGeometry)' in html)
check('critical zoning retry is registered with safeAnalysisStep',"safeAnalysisStep('용도지역 핵심 FACT',analyzePlanningZoningCritical,60000" in html)
check('zoning zero is not confirmed',"const known=!!planningAnalysis.loaded&&!uq111Error&&rows.length>0&&!!primary;" in html and "status:known?'CONFIRMED':'REVIEW'" in html)
check('zoning UI says core fact missing', '용도지역 핵심 Fact 미확보 · 사업판정 보류' in html and "'핵심 Fact 미확보'" in html)
check('planning quality partial when zoning empty',"planningAnalysis.errors.length||zoneRows.length===0||planningAnalysis.zoningOverlapArea>0.5" in html)
check('scheme progress not green when zoning missing',"schemeProgressStatus=schemeRun?.moduleErrors?.length?'rejected':zoningCoreKnown?'fulfilled':'partial'" in html)
check('candidate summary explains zoning hold','핵심 FACT 미확보: 용도지역 · 사업방식 판정은 보류' in html)
check('retry13 remains rejected-only',"const retryableFailed=failed.filter" in html and "partial 및 명시적 NO_DATA / UNKNOWN / NOT_IMPLEMENTED는 재분석하지 않는다" in html)

# Ensure unrelated core legal / spatial engines are unchanged from R5.
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
for fn in ('densityForScheme','checkActivationFromFacts','checkPriorNegotiationFromFacts','analyzeSchemeStreetBlocks','analyzeRoadAccess','runAllSchemeChecks'):
    a,b=extract(base,fn),extract(html,fn)
    check(f'unrelated core preserved: {fn}',a==b,hashlib.sha256((b or '').encode()).hexdigest()[:16])

# Runtime semantics of zoningSpatialEvidenceFacts: loaded+0 rows must remain REVIEW/known=false.
fn=extract(html,'zoningSpatialEvidenceFacts')
unit=f"""
const planningAnalysis={{loaded:true,errors:[],primaryZoning:'',primaryRatio:null,zoningMixed:false,zoningOverlapArea:0}};
const document={{getElementById:(id)=>({{value:'5093'}})}};
const planningZoningRows=(total)=>[];
{fn}
const x=zoningSpatialEvidenceFacts();
if(x.known!==false||x.status!=='REVIEW'||x.rows.length!==0){{console.error(x);process.exit(13);}}
console.log('zoning-zero-review-pass');
"""
tmp=ROOT/'_zoning_unit_tmp.js';tmp.write_text(unit,encoding='utf-8')
r=subprocess.run(['node',str(tmp)],capture_output=True,text=True);tmp.unlink(missing_ok=True)
check('zoning zero runtime -> REVIEW',r.returncode==0,(r.stdout+r.stderr).strip()[:300])

# Syntax/compile
st=html.index('<script>')+len('<script>');en=html.rindex('</script>');js=ROOT/'_zoning_inline_tmp.js';js.write_text(html[st:en],encoding='utf-8')
rjs=subprocess.run(['node','--check',str(js)],capture_output=True,text=True);js.unlink(missing_ok=True)
check('browser JavaScript syntax',rjs.returncode==0,rjs.stderr.strip()[:200])
rpy=subprocess.run([sys.executable,'-m','py_compile',str(ROOT/'app.py')],capture_output=True,text=True)
check('app.py compile',rpy.returncode==0,rpy.stderr.strip()[:200])
failed=[x for x in checks if not x[1]]
print(f'\nSUMMARY {len(checks)-len(failed)}/{len(checks)} PASS')
if failed:
    print('FAILED:',[x[0] for x in failed]);sys.exit(1)
