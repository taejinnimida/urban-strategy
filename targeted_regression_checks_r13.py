from pathlib import Path
import re

ROOT=Path(__file__).resolve().parent
html=(ROOT/'app.html').read_text(encoding='utf-8')
py=(ROOT/'app.py').read_text(encoding='utf-8')

def ok(cond,msg):
    if not cond: raise AssertionError(msg)
    print('PASS',msg)

ok("requestStreetBlock(320)" in html,'adaptive street-block starts at 320m')
ok("requestStreetBlock(700)" in html,'adaptive street-block retries legacy 700m')
ok("frame_boundary_touched" in html,'wide retry checks frame boundary')
ok("merge_limit_reached" in html,'wide retry checks merge limit')
ok("analysis_radius_m" in py,'backend records analysis radius')
ok("frame_boundary_touched" in py,'backend records frame boundary touch')
ok("frame_metric.buffer(-5.0).contains(primary)" in py,'frame touch uses conservative inner-frame containment')
ok("sameSchemeStreetBlockSpatialRule" in html,'shared-rule guard exists')
ok("reused_from:'activation'" in html,'station-complex can reuse activation block')
ok("activationComplexSameRule?['smallscale','activation','growth_potential']" in html,'duplicate 4m calculation removed only when rules match')
ok('streetBlockPromise' not in html,'no background street-block parallelization')
ok("safeAnalysisStep('제도별 가로구역',analyzeSchemeStreetBlocks,300000" in html,'street-block remains first awaited analysis step')
ok("[가로구역 성능] 도로 FACT" in html,'road fact timing log exists')
ok("[가로구역 성능] 계획도로·시설" in html,'facility timing log exists')
ok("[가로구역 성능] 전체" in html,'total timing log exists')
# Current activation/station-complex configs must really match spatially.
pat=r"activation:\{([^\n]+)\}\s*,\s*station_complex:\{([^\n]+)\}"
m=re.search(pat,html)
ok(bool(m),'activation and station-complex configs found')
for token in ["existing_min_m:4","planning_mode:'gt_width'","planning_min_m:4","exclude_nonbuildable:true","requires_road_width_for_block:true"]:
    ok(token in m.group(1) and token in m.group(2),f'shared 4m rule matches: {token}')
print('ALL R13 TARGETED CHECKS PASSED')
