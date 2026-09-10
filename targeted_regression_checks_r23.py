from __future__ import annotations
import importlib.util
import pathlib
import time
from shapely.geometry import box, GeometryCollection
from shapely.strtree import STRtree

HERE = pathlib.Path(__file__).resolve().parent
APP = HERE / 'app.py'
spec = importlib.util.spec_from_file_location('urban_r23_app', APP)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

passes=[]
def ck(name, cond):
    if not cond:
        raise AssertionError(name)
    passes.append(name)
    print('PASS', name)

# 1) static topology matches legacy candidate/shared-edge semantics.
geoms=[]
for y in range(12):
    for x in range(12):
        geoms.append(box(x*10.0, y*10.0, (x+1)*10.0, (y+1)*10.0))
tree=STRtree(geoms)
topo=mod._build_basic_unit_neighbor_topology(geoms, tree)
ck('neighbor topology built', topo['edge_count'] > 0 and len(topo['neighbor_order']) == len(geoms))
ck('neighbor shared geometry cached', len(topo['shared_edges']) == topo['edge_count'])
ck('neighbor topology query once per unit', topo['query_count'] == len(geoms))

# 2) graph BFS must reproduce legacy BFS exactly for multiple starts and cutoff.
empty=GeometryCollection()
for max_units in (50, 240):
    for start in (0, 5, 17, 66, 143):
        legacy=mod._basic_unit_component(start, geoms, tree, empty, empty, max_units=max_units)
        graph=mod._basic_unit_component_graph(
            start, topo['neighbor_order'], topo['shared_edges'], empty, empty,
            max_units=max_units, barrier_cache={}, stats={},
        )
        ck(f'graph equals legacy start={start} max={max_units}', graph == legacy)


# 2b) road/strong barrier가 있는 경우에도 legacy와 동일해야 한다.
from shapely.geometry import LineString
from shapely.ops import unary_union
road_union = unary_union([LineString([(60,-10),(60,130)]).buffer(3.2, cap_style=2), LineString([(90,-10),(90,130)]).buffer(3.2, cap_style=2)])
strong_union = LineString([(-10,70),(130,70)]).buffer(2.0, cap_style=2)
road_p = mod.prep(road_union)
strong_p = mod.prep(strong_union)
for start in (0, 5, 17, 66, 143):
    legacy=mod._basic_unit_component(start, geoms, tree, road_union, strong_union, max_units=240, road_prepared=road_p, strong_prepared=strong_p)
    graph=mod._basic_unit_component_graph(
        start, topo['neighbor_order'], topo['shared_edges'], road_union, strong_union,
        max_units=240, road_prepared=road_p, strong_prepared=strong_p, barrier_cache={}, stats={},
    )
    ck(f'barrier graph equals legacy start={start}', graph == legacy)

# 3) a component pass shares barrier decisions across seeds.
orig=mod._shared_edge_barrier
counts={'legacy':0,'graph':0}
def counted_legacy(*a, **kw):
    counts['legacy'] += 1
    return orig(*a, **kw)
mod._shared_edge_barrier=counted_legacy
legacy_results=[]
seeds=(0, 11, 66, 132, 143)
for start in seeds:
    legacy_results.append(mod._basic_unit_component(start, geoms, tree, empty, empty, max_units=240))

mod._shared_edge_barrier=orig
barrier_cache={}
stats={}
def counted_graph(*a, **kw):
    counts['graph'] += 1
    return orig(*a, **kw)
mod._shared_edge_barrier=counted_graph
graph_results=[]
for start in seeds:
    graph_results.append(mod._basic_unit_component_graph(
        start, topo['neighbor_order'], topo['shared_edges'], empty, empty,
        max_units=240, barrier_cache=barrier_cache, stats=stats,
    ))
mod._shared_edge_barrier=orig
ck('multi-seed graph results equal legacy', graph_results == legacy_results)
ck('barrier cache reduces repeated evaluations', counts['graph'] < counts['legacy'])
ck('barrier evaluations are unique-edge bounded', counts['graph'] <= topo['edge_count'])
ck('barrier cache records reuse', stats.get('barrier_cache_hits',0) > 0)

# 4) source-level integration markers.
text=APP.read_text(encoding='utf-8')
ck('common context stores lazy neighbor graph', "'neighbor_order': neighbor_order" in text and "neighbor_graph_mode':'lazy'" in text)
ck('component pass uses graph BFS', '_basic_unit_component_graph(' in text and 'barrier_cache=barrier_cache' in text)
ck('R22 prepared retained', 'road_prepared = prep(road_barrier_union)' in text)
ck('R21/R20 frontend retained', "analysis_area_cut_m2:24000" in (HERE/'app.html').read_text(encoding='utf-8') and "analysis_area_cut_m2:36000" in (HERE/'app.html').read_text(encoding='utf-8'))

print(f"BARRIER_CALLS legacy={counts['legacy']} graph={counts['graph']} unique_edges={topo['edge_count']} cache_hits={stats.get('barrier_cache_hits',0)}")
print(f'ALL R23 TARGETED CHECKS PASS ({len(passes)}/{len(passes)})')
