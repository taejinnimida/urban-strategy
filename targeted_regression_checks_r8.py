from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
HTML = (ROOT / 'app.html').read_text(encoding='utf-8')
PY = (ROOT / 'app.py').read_text(encoding='utf-8')


def ok(cond, name):
    if not cond:
        raise AssertionError(name)
    print('PASS', name)

# 1) architecture: street block remains sequential and first, not background-parallelized.
site_m = re.search(r'async function runSiteReview\(\)\{(.*?)\n\}\nfunction resetMeasure', HTML, re.S)
ok(bool(site_m), 'runSiteReview found')
site = site_m.group(1)
ok('streetBlockPromise' not in site and 'streetBlockBackground' not in site, 'no street-block background promise')
ok("const streetBlockStep=await safeAnalysisStep('제도별 가로구역'" in site, 'street block awaited sequentially')
ok(site.find("제도별 가로구역") < site.find("연속지적"), 'street block before parcels')
ok(site.find("제도별 가로구역") < site.find("역세권 경계"), 'street block before station')

# 2) preflight: missing basic-unit short-circuits only the street-block analysis to REVIEW/PARTIAL.
ok('async function streetBlockRuntimePreflight(signal=null)' in HTML, 'street-block preflight present')
ok("setTimeout(()=>{try{controller.abort();}catch(_e){}},5000)" in HTML, 'preflight 5-second timeout')
ok("data.street_block_basic_unit_configured===true" in HTML, 'preflight reads health basic-unit status')
ok("markSchemeStreetBlocksUnavailable('SGIS 기초단위구 ZIP 미설치" in HTML, 'missing basic-unit short-circuit')
ok("st.status='review'" in HTML and "basic_unit_available:false" in HTML, 'missing basic-unit becomes REVIEW fact')
ok("status:missing?'partial'" in HTML, 'missing basic-unit progress is partial not rejected')

# analyzeSchemeStreetBlocks must perform preflight before road/VWorld work.
ana_m = re.search(r'async function analyzeSchemeStreetBlocks\(signal=null\)\{(.*?)\n\}\nfunction renderSchemeStreetBlockSpatialStatus', HTML, re.S)
ok(bool(ana_m), 'analyzeSchemeStreetBlocks found')
ana = ana_m.group(1)
ok(ana.find('streetBlockRuntimePreflight') < ana.find('fetchIndependentRoadFacts'), 'preflight before road facts')

# 3) selected-parcel boundary flow.
sel_m = re.search(r'async function applySelectedParcelsAsBoundary\(\)\{(.*?)\n\}\n\n', HTML, re.S)
ok(bool(sel_m), 'selected parcel boundary function found')
sel = sel_m.group(1)
ok('clearBoundaryAnalysisForNewGeometry({keepParcelSelection:true})' in sel, 'selected parcels preserved while stale facts reset')
ok('analyzeLandLedger' not in sel and 'analyzeBuildingHub' not in sel and 'analyzeRoadAccess' not in sel and 'analyzeSafeMedicalReference' not in sel, 'no heavy analysis before review click')
ok("setSiteReviewStatus('구역계 설정 완료(필지선택)" in sel, 'review button activation path')

# 4) health/reference diagnostics and road cache placement.
ok('def _reference_data_readiness()' in PY, 'reference readiness helper')
ok('"reference_data_missing"' in PY and '"analysis_reference_ready"' in PY, 'health reference diagnostics')
ok('"street_block_basic_unit_configured"' in PY, 'health basic-unit diagnostic')
road_path_pos = PY.find('def _road_zip_path()')
road_cache_pos = PY.find('@lru_cache(maxsize=1)\ndef _road_shape_zip_cache_dir()')
ok(road_path_pos >= 0 and road_cache_pos > road_path_pos, 'road extraction cache on expensive helper')
pre = PY[max(0, road_path_pos-80):road_path_pos]
ok('@lru_cache' not in pre, 'road path helper not unnecessarily cached')

# 5) no accidental change to intended serial heavy-data policy.
ok('대형 공간자료·외부 API는 순차 실행한다' in HTML, 'serial heavy-data policy retained')

print('ALL R8 TARGETED CHECKS PASSED')
