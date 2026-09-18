from pathlib import Path
import hashlib, importlib.util, re, subprocess, sys
from shapely.geometry import Polygon, mapping

ROOT=Path(__file__).resolve().parent
html=(ROOT/'app.html').read_text(encoding='utf-8')
py=(ROOT/'app.py').read_text(encoding='utf-8')
base=(ROOT/'app.before_zoning_server_r7.html').read_text(encoding='utf-8')
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

check('server zoning endpoint present','@app.post("/api/spatial/zoning")' in py and 'analyze_zoning_features(inp.geometry)' in py)
check('UQ111 browser planning path uses backend',"if(spec.id==='LT_C_UQ111')" in html and "fetchBackendJson('/api/spatial/zoning'" in html)
check('UQ111 excluded from generic planning browser fanout',"x.kind!=='facility'&&x.id!=='LT_C_UQ111'" in html)
check('zoning fetched before other planning layers',html.index("zoningResult=await fetchPlanningSpec(zoningSpec,activeGeometry)") < html.index('const facilityPromise=mapLimit(facilitySpecs'))
check('zoning cache present','_ZONING_FACT_CACHE_TTL_SEC = 600' in py and '_ZONING_FACT_CACHE_STALE_SEC = 3600' in py)
check('zero zoning is not cached','if not features:' in py and 'LT_C_UQ111 조회 결과가 0건입니다. 핵심 FACT를 확정하지 않습니다.' in py)
check('stale cache fallback present','VWorld UQ111 일시장애 · 최근 정상 FACT 재사용' in py and 'server_stale_cache' in py)
check('429 propagated for frontend cooldown','raise HTTPException(status_code=429' in py and '용도지역 LT_C_UQ111 일시 호출제한' in py)
check('zoning source label server proxy',"source:'서버 프록시 · VWorld LT_C_UQ111'" in html)

# Unrelated core engines must remain byte-identical to R6.
for fn in ('densityForScheme','checkActivationFromFacts','checkPriorNegotiationFromFacts','analyzeSchemeStreetBlocks','analyzeRoadAccess','runAllSchemeChecks','runAllAutoAnalyses'):
    a,b=extract(base,fn),extract(html,fn)
    check(f'unrelated core preserved: {fn}',a==b,hashlib.sha256((b or '').encode()).hexdigest()[:16])

# Runtime server-cache behavior with network function isolated.
spec=importlib.util.spec_from_file_location('platform_app_r7',ROOT/'app.py')
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
site=mapping(Polygon([(126.9,37.5),(126.901,37.5),(126.901,37.501),(126.9,37.501),(126.9,37.5)]))
feature={"type":"Feature","id":"z1","geometry":site,"properties":{"uname":"제3종일반주거지역"}}
calls={'n':0}
def fake(layer,target_geom,size=1000,max_pages=5):
    calls['n']+=1
    return [feature]
mod._vworld_features_in_bbox=fake
mod._ZONING_FACT_CACHE.clear()
a=mod.analyze_zoning_features(site)
b=mod.analyze_zoning_features(site)
check('server zoning successful fetch cached',a.get('known') is True and a.get('cache_hit') is False and b.get('cache_hit') is True and calls['n']==1,str((a.get('status'),b.get('status'),calls['n'])))

# Failed refresh can reuse recent successful FACT.
key=mod._zoning_cache_key(site)
with mod._ZONING_FACT_CACHE_LOCK:
    mod._ZONING_FACT_CACHE[key]['saved_at']-=mod._ZONING_FACT_CACHE_TTL_SEC+1
def fail(layer,target_geom,size=1000,max_pages=5):
    raise RuntimeError('VWorld LT_C_UQ111 HTTP 502')
mod._vworld_features_in_bbox=fail
c=mod.analyze_zoning_features(site)
check('server zoning stale fallback on transient error',c.get('known') is True and c.get('status')=='stale_cache' and len(c.get('features') or [])==1,str(c.get('status')))

# Syntax / compile
st=html.index('<script>')+len('<script>');en=html.rindex('</script>')
tmp=ROOT/'_zoning_server_r7_tmp.js';tmp.write_text(html[st:en],encoding='utf-8')
rjs=subprocess.run(['node','--check',str(tmp)],capture_output=True,text=True);tmp.unlink(missing_ok=True)
check('browser JavaScript syntax',rjs.returncode==0,rjs.stderr.strip()[:200])
rpy=subprocess.run([sys.executable,'-m','py_compile',str(ROOT/'app.py')],capture_output=True,text=True)
check('app.py compile',rpy.returncode==0,rpy.stderr.strip()[:200])

failed=[x for x in checks if not x[1]]
print(f'\nSUMMARY {len(checks)-len(failed)}/{len(checks)} PASS')
if failed:
    print('FAILED:',[x[0] for x in failed]);sys.exit(1)
