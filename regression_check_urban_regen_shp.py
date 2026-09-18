from pathlib import Path
import ast, hashlib, importlib.util, json, re, subprocess, sys

ROOT=Path(__file__).resolve().parent
py=(ROOT/'app.py').read_text(encoding='utf-8')
html=(ROOT/'app.html').read_text(encoding='utf-8')
pre=(ROOT/'app.before_urban_regen_shp.html').read_text(encoding='utf-8')

checks=[]
def check(name, ok, detail=''):
    checks.append((name,bool(ok),detail))
    print(('PASS' if ok else 'FAIL'),name,detail)

# Source replacement checks
check('BZ604 removed from PLANPLUS registry', "'BZ604': ('other', '도시재생활성화지역')" not in py)
check('old GeoJSON path removed', 'urban_regeneration_innovation_reference.geojson' not in py and not (ROOT/'urban_regeneration_innovation_reference.geojson').exists())
check('SHP files complete', all((ROOT/f'urban_regeneration_innovation_gimpo{ext}').exists() for ext in ('.shp','.shx','.dbf','.prj','.cpg')))
check('old urban-regeneration NED categories removed', '"urban_regeneration_innovation": {"affected_pnus"' not in py and '"housing_regeneration_innovation": {"affected_pnus"' not in py)
check('old browser analysis removed', all(x not in html for x in ('urbanRegenerationRestrictionAnalysis','analyzeUrbanRegenerationRestrictions','URBAN_REGEN_REFERENCE_REVIEW_BUFFER_M','URBAN_REGEN_YONGSAN_ANCHORS','ccUrbanRegenerationHousing','ccUrbanRegenerationUrban')))
check('new SHP analysis connected', all(x in html for x in ('urbanRegenerationInnovationAnalysis','analyzeUrbanRegenerationInnovation','ccUrbanRegenerationSource','ccUrbanRegenerationOverlap')))
check('site card simplified', '중첩면적' in html and '중첩률' in html and '김포공항 도시재생혁신지구 SHP' in html)

# Retry-13 preservation
check('retry13 one-time status text', "setSiteReviewStatus('분석실패 현황 재분석','running')" in html)
check('old repeated retry loop absent', 'while(retryLabels.size>0' not in html and 'Promise.all(current.map' not in html)
check('partial/no-data exclusion preserved', "['NO_DATA','UNKNOWN','NOT_IMPLEMENTED']" in html)

# Core engine hash preservation except explicitly connected FactStore and runSiteReview retry section.
def extract_function(src,name):
    m=re.search(r'(?:async\s+)?function\s+'+re.escape(name)+r'\s*\(',src)
    if not m: return None
    st=m.start(); b=src.find('{',m.end()); depth=0; quote=None; esc=False; i=b
    while i<len(src):
        c=src[i]
        if quote:
            if esc: esc=False
            elif c=='\\': esc=True
            elif c==quote: quote=None
        else:
            if c in "'\"": quote=c
            elif c=='{': depth+=1
            elif c=='}':
                depth-=1
                if depth==0:return src[st:i+1]
        i+=1
    return None
for fn in ('densityForScheme','runAllSchemeChecks','analyzeSchemeStreetBlocks','analyzeRoadAccess','refreshMiniContextFeatures','safeAnalysisStep'):
    a,b=extract_function(pre,fn),extract_function(html,fn)
    check(f'core hash preserved: {fn}', a==b, hashlib.sha256((b or '').encode()).hexdigest()[:16])

# Backend import and SHP load
spec=importlib.util.spec_from_file_location('platform_app_regression',ROOT/'app.py')
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
data=mod._urban_regen_innovation_reference_data()
check('backend SHP ready',mod._urban_regen_innovation_shp_ready())
check('backend feature count=1',len(data.get('features',[]))==1,str(len(data.get('features',[]))))
area=float(data.get('metadata',{}).get('source_area_m2') or 0)
check('SHP area stable',abs(area-359275.76)<1.0,f'{area:.2f} m2')
check('source CRS=EPSG:5181',data.get('metadata',{}).get('source_crs')=='EPSG:5181')
check('endpoint JSON serializable',bool(json.dumps(mod.reference_urban_regeneration_innovation(),ensure_ascii=False)))
check('health readiness true',mod._reference_data_readiness().get('urban_regeneration_innovation_reference') is True)

failed=[x for x in checks if not x[1]]
print(f'\nSUMMARY {len(checks)-len(failed)}/{len(checks)} PASS')
if failed:
    sys.exit(1)
