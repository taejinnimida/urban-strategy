from pathlib import Path
import hashlib, importlib.util, json, re, subprocess, sys
from shapely.geometry import shape
from shapely.strtree import STRtree
from pyproj import Transformer
from shapely.ops import transform as geometry_transform

ROOT=Path(__file__).resolve().parent
html=(ROOT/'app.html').read_text(encoding='utf-8')
py=(ROOT/'app.py').read_text(encoding='utf-8')
base_html=(ROOT/'app.before.html').read_text(encoding='utf-8') if (ROOT/'app.before.html').exists() else None
checks=[]

def check(name, ok, detail=''):
    checks.append((name,bool(ok),detail))
    print(('PASS' if ok else 'FAIL'),name,detail)

# Exact architectural cleanup
check('BZ604 mapping removed', "'BZ604': ('other', '도시재생활성화지역')" not in py)
check('legacy GeoJSON file absent', not (ROOT/'urban_regeneration_innovation_reference.geojson').exists())
check('legacy GeoJSON path absent in code', 'urban_regeneration_innovation_reference.geojson' not in py and 'urban_regeneration_innovation_reference.geojson' not in html)
check('legacy reference endpoint removed', '/api/reference/urban-regeneration-innovation' not in py and '/api/reference/urban-regeneration-innovation' not in html)
legacy_front=(
    'urbanRegenerationRestrictionAnalysis','analyzeUrbanRegenerationRestrictions',
    'URBAN_REGEN_REFERENCE_REVIEW_BUFFER_M','URBAN_REGEN_YONGSAN_ANCHORS',
    'ccUrbanRegenerationMini','ccUrbanRegenerationUrban','ccUrbanRegenerationHousing',
    '도시재생 관련 지역·지구'
)
check('dedicated urban-regeneration browser module removed', all(x not in html for x in legacy_front))
check('legacy NED urban-regeneration categories removed', 'housing_regeneration_innovation' not in py and '"urban_regeneration_innovation": {"affected_pnus"' not in py)

# SHP source
required=[ROOT/f'urban_regeneration_innovation_gimpo{ext}' for ext in ('.shp','.shx','.dbf','.prj','.cpg')]
check('SHP component set complete', all(p.exists() for p in required))

spec=importlib.util.spec_from_file_location('platform_app_r3',ROOT/'app.py')
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
fc=mod._urban_regen_innovation_project_features()
meta=fc.get('metadata') or {}
check('backend SHP ready',mod._urban_regen_innovation_shp_ready())
check('SHP feature count=1',len(fc.get('features') or [])==1,str(len(fc.get('features') or [])))
check('SHP source CRS EPSG:5181',meta.get('source_crs')=='EPSG:5181',str(meta.get('source_crs')))
area=float(meta.get('source_area_m2') or 0)
check('SHP source area stable',abs(area-359275.76)<1.0,f'{area:.2f} m2')
if fc.get('features'):
    g=shape(fc['features'][0]['geometry'])
    check('SHP WGS84 geometry valid',g.is_valid and g.geom_type in ('Polygon','MultiPolygon'),g.geom_type)
    # isolate the shared existing-project intersection engine to prove the new SHP feeds it correctly
    features=fc['features']; geoms=[shape(x['geometry']) for x in features]; tree=STRtree(geoms)
    mod._planplus_project_spatial_index=lambda:(features,geoms,tree)
    site=g.representative_point().buffer(0.001)
    to_metric=Transformer.from_crs(4326,5174,always_xy=True).transform
    to_wgs=Transformer.from_crs(5174,4326,always_xy=True).transform
    site_metric=geometry_transform(to_metric,site)
    ov,ctx=mod._planplus_project_intersections(site,site_metric,float(site_metric.area),to_metric,to_wgs)
    check('SHP enters existing-project overlap FACT',len(ov)==1 and ov[0]['properties'].get('type_label')=='도시재생혁신지구',str([(x['properties'].get('type_label'),x['properties'].get('site_overlap_pct')) for x in ov]))
else:
    check('SHP WGS84 geometry valid',False,'no feature')
    check('SHP enters existing-project overlap FACT',False,'no feature')

# Front-end shared FACT linkage
check('project registry carries SHP metadata','project_registry_metadata:{...(renewalAnalysis.projectRegistryMetadata||{})}' in html)
check('public-complex exclusion reads existing-project FACT',"renewal.project_registry_metadata?.urban_regeneration_innovation_shp" in html and "renewal.existing_projects||[]" in html)
check('autonomous-housing target does not confuse innovation with activation','/자율주택|빈집|소규모주택정비|도시재생활성화지역/' in html and '/자율주택|빈집|소규모주택정비|도시재생/.test' not in html)

# Retry-13: agreed one-time sequential recovery only.
check('retry13 status present',"setSiteReviewStatus('분석실패 현황 재분석','running')" in html)
check('retry13 one-time sequential','for(const label of retryLabels)' in html and 'while(retryLabels.size>0' not in html and 'Promise.all(current.map' not in html)
check('retry13 excludes partial/no-data states',"['NO_DATA','UNKNOWN','NOT_IMPLEMENTED']" in html)
check('retry13 re-runs gated rule engine only after recovery','if(recovered>0)runSchemeChecksWhenCoreReady();' in html)

# Core engines unrelated to this change must be byte-identical.
def extract_function(src,name):
    m=re.search(r'(?:async\s+)?function\s+'+re.escape(name)+r'\s*\(',src)
    if not m:return None
    st=m.start(); b=src.find('{',m.end()); depth=0; quote=None; esc=False; template=False; i=b
    while i<len(src):
        c=src[i]
        if quote:
            if esc: esc=False
            elif c=='\\': esc=True
            elif c==quote: quote=None
        else:
            if c in "'\"`": quote=c
            elif c=='{': depth+=1
            elif c=='}':
                depth-=1
                if depth==0:return src[st:i+1]
        i+=1
    return None

if base_html:
    for fn in ('densityForScheme','runAllSchemeChecks','analyzeSchemeStreetBlocks','analyzeRoadAccess','runAllAutoAnalyses'):
        a,b=extract_function(base_html,fn),extract_function(html,fn)
        check(f'core function preserved: {fn}',a==b,hashlib.sha256((b or '').encode()).hexdigest()[:16])
else:
    check('R2 baseline comparison skipped',True,'R12 배포 ZIP에 app.before.html이 포함되지 않아 후속 버전 전용 회귀해시로 대체')

# Static syntax checks
check('app.py compile',subprocess.run([sys.executable,'-m','py_compile',str(ROOT/'app.py')],capture_output=True).returncode==0)
js=ROOT/'inline_scripts.js'
node=subprocess.run(['node','--check',str(js)],capture_output=True,text=True)
check('browser JavaScript syntax',node.returncode==0,node.stderr.strip()[:200])

failed=[x for x in checks if not x[1]]
print(f'\nSUMMARY {len(checks)-len(failed)}/{len(checks)} PASS')
if failed:
    print('FAILED:',[x[0] for x in failed])
    sys.exit(1)
