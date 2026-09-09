from pathlib import Path
import re

ROOT=Path(__file__).resolve().parent
HTML=(ROOT/'app.html').read_text(encoding='utf-8')
PYTXT=(ROOT/'app.py').read_text(encoding='utf-8')

def ok(cond,name,detail=''):
    if not cond:
        raise AssertionError(f'{name}: {detail or "FAILED"}')
    print('PASS',name)

# 1) Wide desktop: four independent scheme cards; responsive tablet/mobile fallback.
ok('grid-template-columns:repeat(4,minmax(0,1fr))' in HTML,'desktop street-block UI is 4 columns')
ok('@media(max-width:1450px){.scheme-streetblock-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}' in HTML,'street-block UI falls back to 2 columns')
ok('@media(max-width:900px){.scheme-streetblock-grid{grid-template-columns:1fr}' in HTML,'street-block UI falls back to 1 column')

# 2) Small-scale legal block definition: ordinary/Building Act road uses 6m,
#    urban-planning facility road is a boundary regardless of width, and statutory facilities are active.
config_m=re.search(r'const SCHEME_STREET_BLOCK_CONFIG=\{(.*?)\n\};',HTML,re.S)
ok(bool(config_m),'street-block config found')
config=config_m.group(1)
small=re.search(r"smallscale:\{([^\n]+)\}",config)
ok(bool(small),'smallscale config found')
smalltxt=small.group(1)
ok("existing_min_m:6" in smalltxt,'smallscale ordinary road 6m rule retained')
ok("planning_mode:'all'" in smalltxt and "planning_min_m:null" in smalltxt,'smallscale all urban-planning facility roads are boundaries regardless of width')
ok("exclude_nonbuildable:true" in smalltxt,'smallscale statutory facility cutters enabled')
for k in ['parking','square','park','green','public_open','river','rail','school']:
    ok(f"'{k}'" in smalltxt,f'smallscale boundary facility includes {k}')
ok('도시계획도로 폭원무관' in HTML,'smallscale UI exposes current planning-road rule')
ok('도시계획시설 도로는 폭원과 무관하게 경계/제외' in HTML,'smallscale evidence note exposes current planning-road rule')

# 3) Facility/planning-road polygons participate in topology, not only post-difference.
ok('const topologyBarriers=[' in HTML,'scheme topology barrier collection present')
ok("_block_barrier_role:'planning_road'" in HTML,'planning roads tagged as topology barriers')
ok("_block_barrier_role:'facility'" in HTML,'statutory facilities tagged as topology barriers')
ok('barrier_features:topologyBarriers' in HTML,'topology barriers sent to backend street-block engine')
ok('barrier_candidates: List[tuple[Dict[str, Any], Any]]' in PYTXT,'backend accepts all scheme-filtered barrier candidates')
ok('_street_block_facility_effect(provisional, mm, frame_metric, barrier_base, site_metric)' in PYTXT,'backend filters effective traversing/closure barriers')
ok("props['_block_barrier_effect'] = reason" in PYTXT,'backend records why facility became a block barrier')

# 4) Base facility query includes all statutory types; scheme filtering remains explicit.
ok("const wanted=new Set(['rail','river','parking','park','green','public_open','square','school']);" in HTML,'facility base fetch includes parking and school')
ok('function schemeFacilityCutters(base,key)' in HTML,'scheme-specific facility filter present')

# 5) Growth-potential: street-block definition uses every road + listed facilities.
growth=re.search(r"growth_potential:\{([^\n]+)\}",config)
ok(bool(growth),'growth config found')
gtxt=growth.group(1)
ok('existing_min_m:0' in gtxt and 'all_roads_boundary:true' in gtxt,'growth street-block uses all roads regardless of width')
ok('requires_road_width_for_block:false' in gtxt,'growth block topology does not require width fact')
ok('exclude_nonbuildable:true' in gtxt,'growth facility cutters enabled')
for k in ['parking','square','park','green','public_open','river','rail','school']:
    ok(f"'{k}'" in gtxt,f'growth boundary facility includes {k}')
ok("planning_mode:'none'" in gtxt,'growth does not invent unopened planning-road boundary rule')
ok('road_min_width_m <= 0' in PYTXT and '폭원 미상 도로면도 topology 경계' in PYTXT,'growth backend keeps unknown-width roads as topology boundaries')

# 6) Growth 35m/6m entry requirements stay separate from street-block definition.
check_m=re.search(r'function checkGrowthPotentialFromFacts\(store,f\)\{(.*?)\n\}',HTML,re.S)
ok(bool(check_m),'growth rule function found')
check=check_m.group(1)
ok("f.arterial.road6_faces>=2?'PASS':'FAIL'" in check,'growth separate 6m two-face road rule retained')
ok("f.arterial.perimeter35_pct>=12.5?'PASS':'FAIL'" in check,'growth separate 35m one-eighth frontage rule retained')
ok('산정값 · 35m 접면' in HTML,'growth card shows 35m frontage result')

# 7) Diagnostic map keeps the selected site legible even if a candidate block is absurdly large.
ok("Number(st.blockAreaM2)>Math.max(200000,Number(st.siteAreaM2)*25)" in HTML,'growth oversized-candidate display clamp present')
ok("turf.buffer(turf.feature(activeGeometry),250" in HTML,'growth oversized candidate uses local 250m diagnostic view')

# 8) No CORS/order regressions.
ok('@app.post("/api/land/characteristics-one")' in PYTXT,'R9 land proxy backend retained')
ok('대형 공간자료·외부 API는 순차 실행한다' in HTML,'serial heavy-analysis policy retained')
ok("const streetBlockStep=await safeAnalysisStep('제도별 가로구역'" in HTML,'street-block remains first and awaited')

print('ALL R10 TARGETED CHECKS PASSED')
