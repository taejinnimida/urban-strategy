from pathlib import Path
import hashlib,re,subprocess,sys,tempfile
ROOT=Path(__file__).resolve().parent
html=(ROOT/'app.html').read_text(encoding='utf-8')
checks=[]
def check(name,ok,detail=''):
    checks.append((name,bool(ok),detail)); print(('PASS' if ok else 'FAIL'),name,detail)
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

# UI / FACT separation
check('land card shows site average official price','id="ccLandPriceAvg"' in html and '사업구역 평균공시지가(대)' in html)
check('land card shows year and coverage',all(x in html for x in ('ccLandPriceYear','ccLandPriceArea','ccLandPriceCoverage')))
check('site land price FACT helper exists','function siteOfficialLandPriceFact' in html and "land_category:'대'" in html and "weighted_method:'면적가중평균'" in html)
ana=extract(html,'analyzeRenewalBusinessFeasibility') or ''
check('official price population remains land-category dae',"if(cat!=='대')continue;" in ana)
check('official price remains area-weighted','sumValue+=price*item.area' in ana and 'sumArea+=item.area' in ana and 'sumValue/sumArea' in ana)
check('boundary partial parcel keeps proportional official area','officialArea*' in ana and 'overlap_ratio' in ana)

# RULE separation / formulas
check('business coefficient rule registry exists','SEOUL_RENEWAL_BUSINESS_COEFFICIENT_RULES' in html)
check('redevelopment rule range and Seoul basis',"redevelopment:Object.freeze({min:1,max:2" in html and '서울시 재개발 평균공시지가 ÷ 사업구역 평균공시지가' in html)
check('reconstruction alpha helper exists','function reconstructionLandAreaAlpha' in html and 'area>=20000' in html and 'area<=10000?0.2' in html)
check('reconstruction beta helper uses available FAR','function reconstructionDensityBeta' in html and 'availableFarPct' in html and 'hh*110/area*100' in html)
check('reconstruction final formula staged','reconPrice+Number(alpha.value||0)+Number(beta.value||0)' in html and 'clampBusinessCoefficient' in html)
check('smallscale uses redevelopment Seoul average','seoul_basis:\'redevelopment\'' in html and "range:[1,1.5]" in html)
check('smallscale direct routes explicit',"applies_to:['autonomous','block','redevelopment']" in html)
check('small reconstruction not guessed',"small_reconstruction:{status:'REVIEW'" in html and '별도 시장기준' in html)
check('future allowed FAR formula staged','function businessCoefficientAdjustedAllowedFar' in html and '허용용적률 - 기준용적률' in html and 'future_far_application' in html)
check('density linkage deliberately deferred','density_connected:false' in html and '실제 허용용적률/상한/법적상한 밀도계산에는 아직 연결하지 않음' in html)

# UI exposes separate scheme rules.
for i in ('spRenewalFeasRedevCoefficient','spRenewalFeasReconPriceFactor','spRenewalFeasReconAlpha','spRenewalFeasReconBeta','spRenewalFeasReconCoefficient','spRenewalFeasSmallscaleCoefficient','spRenewalFeasSmallRecon'):
    check(f'coefficient UI field {i}',f'id="{i}"' in html)

# Pure formula runtime checks, no browser/network needed.
unit_names=['ceilBusinessCoefficient2','clampBusinessCoefficient','businessCoefficientAdjustedAllowedFar','reconstructionLandAreaAlpha','reconstructionDensityBeta']
js='\n'.join(extract(html,n) or '' for n in unit_names)+r'''
function assert(c,m){if(!c)throw new Error(m)}
assert(reconstructionLandAreaAlpha(10000).value===0.20,'alpha 10k');
assert(reconstructionLandAreaAlpha(16813.7).value===0.14,'alpha interpolation');
assert(reconstructionLandAreaAlpha(20000).value===0,'alpha >=20k excluded');
let b=reconstructionDensityBeta(261.92,16813.7,344);
assert(b.value===0&&b.applicable===false,'official beta not applicable example');
b=reconstructionDensityBeta(200,10000,300);
assert(b.value===0.20&&b.applicable===true,'beta low supply');
b=reconstructionDensityBeta(285,10000,300); // 95m2 per household
assert(b.value===0.15&&b.applicable===true,'beta linear mid');
b=reconstructionDensityBeta(null,10000,300);
assert(b.status==='REVIEW'&&b.required_far_pct!==null,'beta waits for available FAR');
assert(ceilBusinessCoefficient2(1.231)===1.24,'third decimal ceiling');
assert(clampBusinessCoefficient(2.23,1,2)===2,'redev/recon cap');
assert(clampBusinessCoefficient(1.71,1,1.5)===1.5,'smallscale cap');
let f=businessCoefficientAdjustedAllowedFar(210,230,1.5);
assert(f.status==='FORMULA_READY'&&f.adjusted_allowed_far_pct===240,'allowed FAR formula');
console.log('r11-formula-unit-pass');
'''
with tempfile.NamedTemporaryFile('w',suffix='.js',encoding='utf-8',delete=False) as f:
    f.write(js); unit=f.name
r=subprocess.run(['node',unit],capture_output=True,text=True)
check('business coefficient formula unit cases',r.returncode==0,r.stdout.strip() or r.stderr.strip()[:180])

# Scope protection: no density/rule core drift.
expected={
'buildSiteFactStore':'f0223a1e8a2ea50c9a107a838dcd80332bc383fd90c3f5897f98023f1692a2db',
'densityForScheme':'8200438b9fa05b114c025d275167eecba7f447fd7c9812e868562b662245f7a8',
'runAllSchemeChecks':'ea1389e3a751711a373b91db19c856521097697dc9e83eb715e6ab8dfbd8e1db',
'checkActivationFromFacts':'9656bb0b44a514efc40679cf536c34354695b7912056140227e86853581941a2',
'analyzeSchemeStreetBlocks':'452e520df0492f09772bfab97d0c9bf84620954d787f92472060e3853e14af23',
'analyzeRoadAccess':'747f5d4c0422cca81632ad9fe4f3d57d2e16c219f39f5506bfdf9b2ceaff646c',
'runAllAutoAnalyses':'d7e776b9b6b57b7ce6186186c62ab9b357d9734d5ea93af00cce407e462979ee'}
for fn,h in expected.items():
    body=extract(html,fn) or ''; got=hashlib.sha256(body.encode()).hexdigest()
    check(f'R10 core preserved: {fn}',got==h,got[:16])

# Browser syntax.
scripts=[]
for m in re.finditer(r'<script(?:\s[^>]*)?>(.*?)</script>',html,re.S|re.I):
    tag=html[m.start():html.find('>',m.start())+1]
    if not re.search(r'\bsrc\s*=',tag,re.I):scripts.append(m.group(1))
with tempfile.NamedTemporaryFile('w',suffix='.js',encoding='utf-8',delete=False) as f:
    f.write('\n;\n'.join(scripts)); browser=f.name
node=subprocess.run(['node','--check',browser],capture_output=True,text=True)
check('browser JavaScript syntax',node.returncode==0,node.stderr.strip()[:180])
py=subprocess.run([sys.executable,'-m','py_compile',str(ROOT/'app.py')],capture_output=True,text=True)
check('app.py compile',py.returncode==0,py.stderr.strip()[:180])

failed=[x for x in checks if not x[1]]
print(f'\nSUMMARY {len(checks)-len(failed)}/{len(checks)} PASS')
if failed:
    print('FAILED:',[x[0] for x in failed]);sys.exit(1)
