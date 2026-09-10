from pathlib import Path
import sys

root=Path(sys.argv[1]) if len(sys.argv)>1 else Path('.')
html=(root/'app.html').read_text(encoding='utf-8')
py=(root/'app.py').read_text(encoding='utf-8')
checks=[]
def ck(name, cond):
    checks.append((name,bool(cond)))

ck('school step visible in progress order', "'학교 절대보호구역'" in html.split('const ANALYSIS_PROGRESS_ORDER=',1)[1].split(';',1)[0])
ck('activation zero roads is REVIEW', "if(roadCount===0)return {status:'REVIEW'" in html)
ck('activation zero zones is REVIEW', "if(zoneCount===0)return {status:'REVIEW'" in html)
ck('activation road fact local-first', "independentRoadManageCandidate(search,220)" in html)
ck('road network uses shared backend fact first', "fetchIndependentRoadFacts(radiusM)" in html[html.index('async function fetchRoadNetwork'):html.index('async function analyzeRoadAccess')])
ck('road network retains VWorld fallback', "trySpatialLayerCandidates(['TL_SPRD_MANAGE','LT_C_SPRD_MANAGE']" in html[html.index('async function fetchRoadNetwork'):html.index('async function analyzeRoadAccess')])
fitseg=html[html.index('function fitCompactMiniMap'):html.index('function compactParcelBaseFeatures')]
ck('compact mini map invalidates before delayed fit', 'try{mp.invalidateSize(false);}' in fitseg and 'setTimeout(fit,40);' in fitseg)
ck('conservation independent sources parallelized', 'Promise.allSettled([' in html[html.index('async function analyzeSharedConservation'):html.index('function renderSharedConservationSpatialStatus')])
ck('backend road mode manage-only', '"road_mode": "manage_only_centerline_width"' in py)
ck('backend explicitly stops RW use', '"rw_used": False' in py)
ck('backend road required files manage-only override', 'ROAD_SHAPE_REQUIRED = tuple(f"TL_SPRD_MANAGE{ext}"' in py)
ck('school uses source bbox reader', 'reader.iterShapeRecords(bbox=bbox)' in py[py.index('def _school_absolute_local_shape'):py.index('def analyze_local_road_facts') if 'def analyze_local_road_facts' in py[py.index('def _school_absolute_local_shape'):] else len(py)])
ck('medical uses official snapshot primary', '"mode": "official_snapshot_primary"' in py)
ck('medical parcel candidates resolved concurrently', 'ThreadPoolExecutor(max_workers=min(4, len(screened)))' in py)
ck('medical failures remain REVIEW', '"boundary_status":"REVIEW"' in py[py.index('def _safe_medical_reference'):py.index('app = FastAPI')])

failed=[n for n,v in checks if not v]
for n,v in checks: print(('PASS' if v else 'FAIL'), n)
if failed:
    print(f'{len(failed)} FAILED: '+', '.join(failed)); sys.exit(1)
print(f'ALL R18 TARGETED CHECKS PASS ({len(checks)}/{len(checks)})')
