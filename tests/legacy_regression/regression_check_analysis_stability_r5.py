from pathlib import Path
import re, subprocess, sys, json
ROOT=Path(__file__).resolve().parent
html=(ROOT/'app.html').read_text(encoding='utf-8')
py=(ROOT/'app.py').read_text(encoding='utf-8')
checks=[]
def check(name,ok,detail=''):
    checks.append((name,bool(ok),detail)); print(('PASS' if ok else 'FAIL'),name,detail)

check('same-site step cache present','analysisFactStepCache' in html and 'ANALYSIS_FACT_CACHE_TTL_MS=10*60*1000' in html)
check('partial cache short TTL','ANALYSIS_PARTIAL_CACHE_TTL_MS=60*1000' in html)
check('cache cleared on geometry reset','resetAnalysisFactStepCache();' in html[html.index('function clearBoundaryAnalysisForNewGeometry'):html.index('function setBoundaryReferenceMetrics')])
check('fulfilled/partial cache reuse',"['fulfilled','partial'].includes(row.result?.status)" in html and '직전 성공 Fact 재사용' in html and '직전 확보 Fact 재사용' in html)
check('run id superseded guard','currentAnalysisRunId=++analysisRunSequence' in html and "error_kind:'SUPERSEDED'" in html)
check('429 classified separately',"err.code='RATE_LIMIT'" in html and "errorKind=e?.code==='RATE_LIMIT'" in html)
check('429 excluded from retry13',"errorKind!=='RATE_LIMIT'" in html and "dataState!=='RATE_LIMIT'" in html)
check('retry13 still one pass','for(const label of retryLabels)' in html and 'while(retryLabels.size>0' not in html)
check('backend API paced','BACKEND_API_MIN_GAP_MS=300' in html and 'waitBackendApiSlot(url)' in html)
check('non-json API response normalized','NON_JSON_RESPONSE' in html and 'JSON 응답 아님' in html)
check('frontend network mapLimit capped at 2','effectiveLimit=Math.min(Math.max(1,Number(limit)||1),2' in html)
check('land/building top-level sequential',"results.push(await safeAnalysisStep('토지대장'" in html and "results.push(await safeAnalysisStep('건축물 공간'" in html and "Promise.all([\n    safeAnalysisStep('토지대장'" not in html)
check('key APIs use checked JSON helper', all(x in html for x in [
    "fetchBackendJson('/api/spatial/road-facts'",
    "fetchBackendJson('/api/building-hub/title-batch'",
    "fetchBackendJson('/api/building-hub/floor-batch'",
    "fetchBackendJson('/api/land/ledger-one'",
    "fetchBackendJson('/api/land/characteristics-one'",
    "fetchBackendJson('/api/land/official-price-batch'",
]))
check('building hub rate-limit stops batch storm', html.count("if(e?.code==='RATE_LIMIT'||e?.http_status===429)throw e;")>=4)
check('backend building hub workers capped', 'ThreadPoolExecutor(max_workers=min(2, len(pnus)))' in py and 'ThreadPoolExecutor(max_workers=min(2, len(pnus) or 1))' in py)
check('backend official price workers capped','max_workers = min(2, len(pnus))' in py)

# SafeAnalysisStep runtime behavior in a minimal Node context.
start=html.index('const analysisStepRegistry=new Map();')
end=html.index('function clearBoundaryAnalysisForNewGeometry',start)
module=html[start:end]
unit=f"""
const vm=require('vm');
const context={{
  Map,Date,JSON,String,Number,Math,Promise,AbortController,setTimeout,clearTimeout,console,
  ANALYSIS_STEP_TIMEOUT_MS:1000,
  boundaryReferenceGeometry:{{type:'Polygon',coordinates:[[[0,0],[1,0],[1,1],[0,0]]]}},activeGeometry:null,boundaryReferenceMetrics:{{area_m2:1}},
  marks:[],markAnalysisProgress:(...x)=>context.marks.push(x)
}};
vm.createContext(context);vm.runInContext({json.dumps(module)},context);
(async()=>{{
 context.currentAnalysisRunId=1;
 let calls=0;
 const fn=async()=>{{calls++;return {{ok:true}};}};
 let a=await context.safeAnalysisStep('X',fn,500,{{classify:v=>({{status:'fulfilled',detail:'ok'}})}});
 let b=await context.safeAnalysisStep('X',fn,500,{{classify:v=>({{status:'fulfilled',detail:'ok'}})}});
 if(calls!==1||!b.cached)process.exit(11);
 context.boundaryReferenceGeometry={{type:'Polygon',coordinates:[[[0,0],[2,0],[2,2],[0,0]]]}};
 let c=await context.safeAnalysisStep('X',fn,500,{{classify:v=>({{status:'fulfilled',detail:'ok'}})}});
 if(calls!==2||c.cached)process.exit(12);
 const rate=async()=>{{const e=new Error('HTTP 429 Too Many Requests');e.code='RATE_LIMIT';e.http_status=429;throw e;}};
 let d=await context.safeAnalysisStep('Y',rate,500,{{}});
 if(d.status!=='rejected'||d.error_kind!=='RATE_LIMIT'||d.data_status!=='RATE_LIMIT')process.exit(13);
 let e=await context.safeAnalysisStep('Z',async()=>({{errors:['HTTP 429 · Just a moment']}}),500,{{classify:v=>({{status:'rejected',detail:v.errors[0]}})}});
 if(e.error_kind!=='RATE_LIMIT')process.exit(14);
 console.log('stability-unit-pass');
}})().catch(e=>{{console.error(e);process.exit(99);}});
"""
tmp=ROOT/'_stability_unit_tmp.js';tmp.write_text(unit,encoding='utf-8')
r=subprocess.run(['node',str(tmp)],capture_output=True,text=True);tmp.unlink(missing_ok=True)
check('same-site cache and 429 runtime unit',r.returncode==0,(r.stdout+r.stderr).strip()[:300])

# Syntax / compile
s=html; st=s.index('<script>')+len('<script>'); en=s.rindex('</script>'); js=ROOT/'_stability_inline_tmp.js';js.write_text(s[st:en],encoding='utf-8')
rjs=subprocess.run(['node','--check',str(js)],capture_output=True,text=True);js.unlink(missing_ok=True)
check('browser JavaScript syntax',rjs.returncode==0,rjs.stderr.strip()[:200])
rpy=subprocess.run([sys.executable,'-m','py_compile',str(ROOT/'app.py')],capture_output=True,text=True)
check('app.py compile',rpy.returncode==0,rpy.stderr.strip()[:200])

failed=[x for x in checks if not x[1]]
print(f'\nSUMMARY {len(checks)-len(failed)}/{len(checks)} PASS')
if failed:
    print('FAILED:',[x[0] for x in failed]);sys.exit(1)
