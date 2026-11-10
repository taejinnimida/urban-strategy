from pathlib import Path
import re, hashlib, subprocess, sys

ROOT=Path(__file__).resolve().parent
html=(ROOT/'app.html').read_text(encoding='utf-8')
py=(ROOT/'app.py').read_text(encoding='utf-8')
checks=[]
def ok(name, cond):
    checks.append((name,bool(cond)))

def js_func(src,name):
    marker=f'function {name}('
    i=src.find(marker)
    if i<0:return None
    b=src.find('{',i)
    if b<0:return None
    depth=0; quote=None; esc=False; template_depth=0
    # brace count is enough for these functions because JS strings/templates may contain braces;
    # strip quoted content conservatively while scanning.
    j=b
    while j<len(src):
        ch=src[j]
        if quote:
            if esc: esc=False
            elif ch=='\\': esc=True
            elif ch==quote: quote=None
            j+=1; continue
        if ch in ('"',"'",'`'):
            quote=ch; j+=1; continue
        if ch=='{': depth+=1
        elif ch=='}':
            depth-=1
            if depth==0:return src[i:j+1]
        j+=1
    return None

def digest(x): return hashlib.sha256(x.encode('utf-8')).hexdigest() if x else None

# UI structure: old monolithic panel removed, exactly three new group panels/maps.
ok('old monolithic regulation panel removed', 'siteDetail_regulatoryConstraints' not in html)
for pid in ['siteDetail_naturalRegulations','siteDetail_transportSecurityRegulations','siteDetail_disasterRegulations']:
    ok(f'{pid} exists', f'id="{pid}"' in html)
for mid in ['ccNaturalRegulationMiniMap','ccTransportSecurityRegulationMiniMap','ccDisasterRegulationMiniMap']:
    ok(f'{mid} exists', f'id="{mid}"' in html)

# Dedicated modules remain the authority for planning facility and district-unit plan.
analyze=js_func(html,'analyzeRegulatoryConstraints') or ''
ok('planning facility removed from regulation item list', "regulatoryItem('planning_facility'" not in analyze)
ok('district unit plan removed from regulation item list', "regulatoryItem('district_plan'" not in analyze)
ok('CURRENT PLAN modal preserved', 'districtUnitPlanReviewModal' in html and 'openDistrictUnitPlanReviewModal' in html)
ok('planning facility dedicated module preserved', 'siteDetail_planningFacilities' in html or 'ccPlanningFacilityMiniMap' in html)

# Heritage environment merged to existing heritage panel.
ok('heritage environment UI merged', 'spHeritageEnvironment' in html and 'spHeritageEnvironmentStatus' in html)
ok('heritage environment classified in heritage group', "'HERITAGE','heritage'" in analyze and "'heritage_environment'" in analyze)
ok('existing heritage vector/WMS preserved', 'LT_C_UO301' in html and 'heritageWms' in html)

# Required group membership.
for token in ["'natural_park'","'ecological_landscape'","'wildlife_special'","'water_source'","'river_zone'"]:
    ok(f'natural group includes {token}', token in analyze)
for token in ["'railroad_protection'","'airport_obstacle'","'military_flight'"]:
    ok(f'transport group includes {token}', token in analyze)
for token in ["'disaster_risk'","'landslide_risk'","'steep_slope_risk'","'flood_management'","'flood_expected'","'flood_trace_2025'"]:
    ok(f'disaster group includes {token}', token in analyze)

# Server-side official Seoul flood data and positive-only NED disaster categories.
ok('disaster endpoint exists', '@app.post("/api/spatial/disaster-reference-intersections")' in py)
ok('Seoul flood expected source exists', 'OA-21172' in py and 'SEOUL_FLOOD_EXPECTED_URL' in py)
ok('Seoul flood trace source exists', 'OA-15636' in py and 'SEOUL_FLOOD_TRACE_2025_URL' in py)
for token in ['landslide_risk','steep_slope_risk','flood_management']:
    ok(f'NED server category {token}', token in py)

# Retry-on-failure and zoning safety must survive this refactor.
for token in ['analysisStepRegistry','RETRY_BUDGET_MS=150000','RETRY_WAIT_MS=15000','분석실패 현황 재분석',"safeAnalysisStep('용도지역 핵심 FACT'"]:
    ok(f'retry/zoning preserved: {token}', token in html)

# Core business-rule functions are byte-identical to the R11 basis hashes.
R11_CORE_HASHES={
    'densityForScheme':'8200438b9fa05b114c025d275167eecba7f447fd7c9812e868562b662245f7a8',
    'runAllSchemeChecks':'ea1389e3a751711a373b91db19c856521097697dc9e83eb715e6ab8dfbd8e1db',
    'checkActivationFromFacts':'9656bb0b44a514efc40679cf536c34354695b7912056140227e86853581941a2',
    'analyzeSchemeStreetBlocks':'5736dea83d7c1ddd23748b37e7178b533c09e78d9e202e35dfcae47367577a34',
    'analyzeRoadAccess':'b430e98d5ca3e26f6008c676456830f66bd8487ba88015160375c0d7e9a1db5a',
    'buildSiteFactStore':'f0223a1e8a2ea50c9a107a838dcd80332bc383fd90c3f5897f98023f1692a2db',
}
for fn,expected in R11_CORE_HASHES.items():
    after=js_func(html,fn)
    ok(f'core JS unchanged {fn}', after is not None and digest(after)==expected)

# Python core zoning business fact code remains present; new change is additive endpoint/category extension.
ok('zoning server endpoint preserved', '@app.post("/api/spatial/zoning")' in py)
ok('app internal version preserved', 'v2.5.0' in html or '2.5.0' in html)

failed=[n for n,v in checks if not v]
for n,v in checks: print(('PASS' if v else 'FAIL'),n)
print(f'RESULT {len(checks)-len(failed)}/{len(checks)} PASS')
if failed: sys.exit(1)
