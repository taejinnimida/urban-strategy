from pathlib import Path
import hashlib, importlib.util, json, re, subprocess, sys, zipfile
from shapely.geometry import shape

ROOT=Path(__file__).resolve().parent
html=(ROOT/'app.html').read_text(encoding='utf-8')
py=(ROOT/'app.py').read_text(encoding='utf-8')
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

# Required packaged references.
for fn in ('uq120_project.zip','uq181_legal.zip','centers.json'):
    check(f'packaged reference present: {fn}',(ROOT/fn).is_file() and (ROOT/fn).stat().st_size>0,str((ROOT/fn).stat().st_size if (ROOT/fn).exists() else 0))
with zipfile.ZipFile(ROOT/'uq120_project.zip') as zf:
    names=[n.lower() for n in zf.namelist()]
    check('UQ120 SHP bundled',any(n.endswith('upis_c_uq120.shp') for n in names))
centers=json.loads((ROOT/'centers.json').read_text(encoding='utf-8'))
check('center polygon reference usable',centers.get('type')=='FeatureCollection' and len(centers.get('features') or [])>=70,str(len(centers.get('features') or [])))

# New site-analysis cards/maps.
for token in ('siteDetail_urbanRedevelopment','ccUrbanRedevelopmentMiniMap','spUrbanRedevDistrictCount','spUrbanRedevPlannedCount','siteDetail_industrialPark','ccIndustrialParkMiniMap','spIndustrialParkCount','spIndustrialParkSource'):
    check(f'site-analysis UI present: {token}',token in html)
check('urban redevelopment map filters legal/project types',"['urban_district','urban_planned'].includes(f.properties?.renewal_type)" in html)
check('industrial park map uses dedicated DAMDAN result','developmentAnalysis.industrialParkContextFeatures' in html and 'developmentAnalysis.industrialParks' in html)
check('industrial unavailable not painted as zero',"parkMeta.available===true?`${parks.length}건`:'확인 필요'" in html and "parkMeta.available===true?'중첩 없음':'자료확인 필요'" in html)

# Center analysis: confirmed noncenter is no longer treated as unknown merely because label is blank.
check('center source load state tracked','const centerReferenceStatus={loaded:false,error:\'\'};' in html)
check('confirmed noncenter centerKnown fix',"c.centerAuto===true&&centerReferenceStatus.loaded===true" in html and "centerKnown=!!c.centerLabel" not in html)
check('urban redevelopment UQ120 fact row linked',"spatialFactRow('도시정비형 사업/예정구역'" in html and "renewal_type==='urban_planned'" in html)

# Public-complex industrial rule linkage.
check('public complex 2030 industrial route linked',"schemeRow('2030 준공업 사업기준'" in html and '공장비율 10% 이상' in html and '재생단위 3,000㎡ 이상' in html)
check('public complex industrial ratio fact linked',"schemeRow('종전 산업비율'" in html)
check('public complex 10k density exception linked',"schemeRow('1만㎡ 이상 용적률기준 예외'" in html and '역승강장 350m' in html and '폭 20m 이상 도로' in html)
check('old qualitative industrial-underdeveloped gate removed',"schemeRow('산업시설 정비필요'" not in html)
check('legal factory ratio not replaced by current proxy','현재 공장용도 추정치로 대체하지 않음' in html)

# Innovation housing road/APT/factory linkage.
check('innovation housing frontage quantitative row',"schemeRow('간선도로 폭원·접면'" in html and '15m / 3만 초과~6만㎡ 20m' in html)
check('innovation housing 6m enclosure separate row',"schemeRow('6m 도로 폐합'" in html)
check('old fixed frontage review removed',"schemeRow('추가 접도 적정성'" not in html)
check('innovation apartment precheck retained','innovationApartmentPrecheck' in html)
check('innovation legal factory hard gate retained',"법정 공장비율 10% 미만" in html)

# Industrial-park API failure must remain REVIEW, never false no-overlap.
check('industrial live failure not authoritative fallback','"fallback_authoritative": False' in py and 'UQ181_LOCAL_FALLBACK' not in py)
check('industrial live failure remains REVIEW',"return {status:'REVIEW',value:'산업단지 전용경계 확인필요'" in html)

# Existing unrelated engines preserved against R7 known-good hashes.
expected_hashes={
 'densityForScheme':'8200438b9fa05b114c025d275167eecba7f447fd7c9812e868562b662245f7a8',
 'checkActivationFromFacts':'9656bb0b44a514efc40679cf536c34354695b7912056140227e86853581941a2',
 'checkPriorNegotiationFromFacts':'fb89d91796ef82ef62876721a28db4a59533a8869fad2e3a25fadc6378684de0',
 'analyzeSchemeStreetBlocks':'452e520df0492f09772bfab97d0c9bf84620954d787f92472060e3853e14af23',
 'analyzeRoadAccess':'747f5d4c0422cca81632ad9fe4f3d57d2e16c219f39f5506bfdf9b2ceaff646c',
 'runAllSchemeChecks':'ea1389e3a751711a373b91db19c856521097697dc9e83eb715e6ab8dfbd8e1db',
 'runAllAutoAnalyses':'d7e776b9b6b57b7ce6186186c62ab9b357d9734d5ea93af00cce407e462979ee',
}
for fn,expected in expected_hashes.items():
    b=extract(html,fn); actual=hashlib.sha256((b or '').encode()).hexdigest()
    check(f'unrelated core preserved: {fn}',actual==expected,actual[:16])

# Runtime reference loading.
spec=importlib.util.spec_from_file_location('platform_app_r8',ROOT/'app.py')
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
renew=mod._renewal_reference_data(); rfs=renew.get('features') or []
check('UQ120 BZ102 loaded into renewal facts',any((f.get('properties') or {}).get('code')=='BZ102' and (f.get('properties') or {}).get('renewal_type')=='urban_planned' for f in rfs),str(sum(1 for f in rfs if (f.get('properties') or {}).get('code')=='BZ102')))
check('UQ181 urban district loaded',any((f.get('properties') or {}).get('renewal_type')=='urban_district' for f in rfs),str(sum(1 for f in rfs if (f.get('properties') or {}).get('renewal_type')=='urban_district')))
center_fc=mod._center_reference_data()
check('server center reference endpoint source ready',len(center_fc.get('features') or [])>=70,str(len(center_fc.get('features') or [])))

# On live industrial boundary failure, no local non-overlap is invented.
orig=mod.analyze_industrial_park_intersections
mod.analyze_industrial_park_intersections=lambda geometry: (_ for _ in ()).throw(RuntimeError('VWorld LT_C_DAMDAN HTTP 502'))
# use a real development feature geometry so base intersection engine has valid input
sample_dev=(mod._development_reference_data().get('features') or [None])[0]
if sample_dev:
    out=mod.analyze_development_intersections(sample_dev['geometry'])
    meta=out.get('industrial_park_metadata') or {}
    check('backend DAMDAN failure classified unavailable',meta.get('available') is False and meta.get('fallback_authoritative') is False and out.get('industrial_parks')==[],str(meta.get('source_type')))
else:
    check('backend DAMDAN failure classified unavailable',False,'no development fixture')
mod.analyze_industrial_park_intersections=orig

# Syntax / compile.
scripts=re.findall(r'<script(?:\s[^>]*)?>(.*?)</script>',html,re.S|re.I)
tmp=ROOT/'_semiindustrial_r8_tmp.js';tmp.write_text('\n'.join(scripts),encoding='utf-8')
rjs=subprocess.run(['node','--check',str(tmp)],capture_output=True,text=True);tmp.unlink(missing_ok=True)
check('browser JavaScript syntax',rjs.returncode==0,rjs.stderr.strip()[:200])
rpy=subprocess.run([sys.executable,'-m','py_compile',str(ROOT/'app.py')],capture_output=True,text=True)
check('app.py compile',rpy.returncode==0,rpy.stderr.strip()[:200])

failed=[x for x in checks if not x[1]]
print(f'\nSUMMARY {len(checks)-len(failed)}/{len(checks)} PASS')
if failed:
    print('FAILED:',[x[0] for x in failed]);sys.exit(1)
