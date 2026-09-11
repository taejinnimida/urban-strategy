from pathlib import Path
import re

html = Path('app.html').read_text(encoding='utf-8')
checks = []
def ck(name, cond):
    checks.append((name, bool(cond)))
    print(('PASS' if cond else 'FAIL'), name)

# R28 browser main-thread guard
m = re.search(r"function schemeBlockComponentsAfterRemoval\([\s\S]*?\n}\nfunction schemeStreetBlockFact", html)
body = m.group(0) if m else ''
ck('streetblock postprocess function found', bool(m))
ck('global cutter union removed from streetblock postprocess', 'unionCutters' not in body and 'safeUnionPolygons(cutters)' not in body)
ck('bbox prefilter helper added', 'function schemeFastBboxOverlap' in html)
ck('only local cutters differenced', 'localCutters' in body and 'safeDifferencePolygons(refined,localCutters)' in body)
ck('scheme postprocess yields to browser before work', '도형 후처리 ${postIndex}/${activeEntries.length}' in html and 'await new Promise(resolve=>setTimeout(resolve,0));' in html)
ck('frontend postprocess timing recorded', 'frontend_postprocess_ms' in html)

# R28 duplicate road cutter guard
road = re.search(r"function schemeExistingRoadInputs\([\s\S]*?\n}\nasync function fetchSchemePlanningRoadBase", html)
roadbody = road.group(0) if road else ''
ck('road cutter function found', bool(road))
ck('matched road skip depends on actual surfaces', 'if(matched&&thresholdSurfaces.length)continue;' in roadbody)
ck('old duplicate empty-surface second loop removed', "if(!thresholdSurfaces.length){\n    for(const mf of thresholdManage)" not in roadbody)

# Preserve prior critical behaviors
ck('R27 alternative recommendation retained', "kind:'alternative'" in html or 'kind="alternative"' in html or "kind: 'alternative'" in html)
ck('R26 10 percent area filter retained', 'CANDIDATE_AREA_SHORTFALL_TOLERANCE_PCT=10' in html)
ck('R24 parcel click retained', "parcelFindMode==='click'" in html)
ck('R23 neighbor graph backend contract retained', '/api/spatial/street-block-batch' in html)
ck('R20 area prefilter retained', 'schemeStreetBlockAreaPrefilter' in html)

failed=[n for n,c in checks if not c]
if failed:
    raise SystemExit(f'FAILED {len(failed)} checks: {failed}')
print(f'ALL R28 TARGETED CHECKS PASS ({len(checks)}/{len(checks)})')
