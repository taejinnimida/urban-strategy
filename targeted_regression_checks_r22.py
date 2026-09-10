from pathlib import Path
import importlib.util
import sys

ROOT = Path(__file__).resolve().parent
APP = ROOT / 'app.py'
TEXT = APP.read_text(encoding='utf-8')

checks = []
def check(name, cond):
    if not cond:
        raise AssertionError(name)
    print('PASS', name)
    checks.append(name)

check('prepared geometry import', 'from shapely.prepared import prep' in TEXT)
check('shared barrier accepts prepared predicates', 'road_prepared: Any = None' in TEXT and 'strong_prepared: Any = None' in TEXT)
check('prepared predicate used for road', 'road_prepared.intersects(corridor)' in TEXT)
check('prepared predicate used for strong barrier', 'strong_prepared.intersects(corridor)' in TEXT)
check('road area still uses raw intersection', 'corridor.intersection(road_union).area' in TEXT)
check('strong area still uses raw intersection', 'corridor.intersection(strong_union).area' in TEXT)
check('prepared built once per component pass', 'road_prepared = prep(road_barrier_union)' in TEXT and 'strong_prepared = prep(strong_union)' in TEXT)
check('component performance diagnostics retained', "'component_pass_count': component_pass_count" in TEXT and "'component_pass_ms': component_pass_ms" in TEXT)
check('prepared build diagnostics retained', "'prepared_build_ms': prepared_build_ms" in TEXT)

spec = importlib.util.spec_from_file_location('urban_r22_app', APP)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

from shapely.geometry import LineString, box
from shapely.ops import unary_union
from shapely.prepared import prep

road = unary_union([
    box(4, -4, 8, 14),
    box(20, -4, 24, 14),
])
strong = box(40, -4, 42, 14)
road_p = prep(road)
strong_p = prep(strong)

cases = [
    LineString([(6, 0), (6, 10)]),   # road barrier
    LineString([(41, 0), (41, 10)]), # strong barrier
    LineString([(60, 0), (60, 10)]), # mergeable
    LineString([(0, 0), (0, 0.5)]),  # short touch
]
for i, shared in enumerate(cases, 1):
    raw = mod._shared_edge_barrier(shared, road, strong)
    fast = mod._shared_edge_barrier(shared, road, strong, road_p, strong_p)
    check(f'prepared/raw result identical case {i}', raw == fast)


print(f'ALL R22 TARGETED CHECKS PASS ({len(checks)}/{len(checks)})')
