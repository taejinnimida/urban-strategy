from pathlib import Path
import ast
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import subprocess
import sys
import threading
import types
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent
HTML = (ROOT / "app.html").read_text(encoding="utf-8")
PY = (ROOT / "app.py").read_text(encoding="utf-8")
checks = []


def check(name, condition, detail=""):
    checks.append((name, bool(condition), detail))
    print(("PASS" if condition else "FAIL"), name, detail)


def js_func(src, name):
    marker = f"function {name}("
    start = src.find(marker)
    if start < 0:
        return ""
    brace = src.find("{", start)
    depth = 0
    quote = None
    escape = False
    for i in range(brace, len(src)):
        ch = src[i]
        if quote:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                quote = None
            continue
        if ch in ("'", '"', "`"):
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    return ""


# Static integration assertions.
spec_block = HTML[HTML.index("const PLANNING_LAYER_SPECS="):HTML.index("const planningAnalysis=")]
layer_ids = []
for token in spec_block.split("{id:'")[1:]:
    layer_ids.append(token.split("'", 1)[0])
check("23 required planning layers", len(layer_ids) == 23 and len(set(layer_ids)) == 23, str(len(layer_ids)))
check("planning batch endpoint", '@app.post("/api/spatial/planning-layers")' in PY)
check("planning request max five", "layer_ids: List[str] = Field(..., min_length=1, max_length=5)" in PY)
check("global VWorld concurrency two", "_PLANNING_VWORLD_SEMAPHORE = threading.BoundedSemaphore(2)" in PY)
check("sequential frontend planning batches", "for(let i=0;i<specs.length;i+=5)" in HTML)
check("planning browser JSONP removed from planning path", "features=await fetchSpatialFeaturesBrowser(spec.id" not in js_func(HTML, "fetchPlanningSpec"))
check("planning statuses present", all(x in HTML for x in ["SUCCESS_DATA", "SUCCESS_EMPTY", "ERROR", "NOT_RUN", "planningComplete"]))
check("planning failed-only retry", "options.retryFailedOnly===true" in HTML and "orderedSpecs.filter(x=>planningAnalysis.layerStatus?.[x.id]?.status==='ERROR')" in HTML)
check("planning error cannot overwrite normal", "next.status==='ERROR'&&['SUCCESS_DATA','SUCCESS_EMPTY'].includes(prev?.status)" in js_func(HTML, "recordPlanningLayerResults"))
check("land official area gate", "official===pnus.length?small:''" in js_func(HTML, "analyzeLandLedger"))
check("land preliminary separated", "_preliminary_area_m2" in js_func(HTML, "analyzeLandLedger") and "preliminary_small_count" in js_func(HTML, "analyzeLandLedger"))
check("building spatial error rethrown", "buildingSpatialQueryState={loaded:true,status:'ERROR'" in js_func(HTML, "analyzeBuildings") and "throw e;" in js_func(HTML, "analyzeBuildings"))
check("hub per-PNU state", all(x in js_func(HTML, "analyzeBuildingHub") for x in ["successful_pnus", "empty_pnus", "failed_pnus", "by_pnu"]))
check("hub failed-only retry cache", "queryPnus=pnus.filter" in js_func(HTML, "analyzeBuildingHub") and "previousTitleStatus" in js_func(HTML, "analyzeBuildingHub"))
check("land failed-only retry cache", "reusableLand[pnu]?.status==='SUCCESS_DATA'" in js_func(HTML, "analyzeLandLedger"))
check("AI gated", "coreFactReadiness().ready" in js_func(HTML, "scheduleAiComprehensiveAnalysis"))
check("priority gated", "coreFactReadiness().ready" in js_func(HTML, "renderPriorityPreview"))
check("candidate gated", "coreFactReadiness().ready" in js_func(HTML, "updateCandidateSchemes"))
check("floor incompleteness retained", "floor_query_complete" in js_func(HTML, "runSchemeChecksWhenCoreReady") and "hubFloorQueryState.complete" in HTML)


# Runtime JavaScript synthetic scenarios against the real helper functions.
module = "\n".join(js_func(HTML, name) for name in [
    "updatePlanningCompleteness", "planningLayerDisplayStatus", "recordPlanningLayerResults",
    "officialSmallParcelRatio", "thresholdStatus", "coreFactReadiness",
    "renderCoreFactPending", "runSchemeChecksWhenCoreReady",
])
node_test = f"""
const vm=require('vm');
const ids={json.dumps(layer_ids)};
const context={{console,Set,Map,Number,Array,JSON,Math,
  PLANNING_LAYER_SPECS:ids.map(id=>({{id}})),
  planningAnalysis:{{layerStatus:{{}},failedLayerIds:[],notRunLayerIds:[],planningComplete:false}},
  analysisState:{{quality:{{small:'MIXED'}},recommendations:[],planning_alternatives:[]}},
  selectedParcelPnus:new Set(['1'.repeat(19)]),
  parcelSpatialQueryState:{{status:'SUCCESS_DATA',error:'',selected_count:1}},
  landLedgerQueryState:{{complete:true,official_area_count:1,failed_pnus:[]}},
  buildingSpatialQueryState:{{status:'SUCCESS_EMPTY',error:'',feature_count:0}},
  hubTitleQueryState:{{complete:true,requested_pnus:['1'.repeat(19)],failed_pnus:[],error_count:0}},
  hubFloorQueryState:{{complete:false,error_count:1}},
  latestSiteFactStore:null,latestSchemeModuleResults:{{}},schemeResults:{{}},coreFactGateState:{{}},
  document:{{getElementById:()=>null}},escHtml:String,markAnalysisProgress:()=>{{}},resetAiComprehensiveAnalysis:()=>{{}},rawFactDate:r=>r.approval_date||null,
  engineCalls:0,runAllSchemeChecksEngine:()=>{{context.engineCalls++;return {{store:{{site:{{building:{{records:[]}}}}}}}};}}
}};
vm.createContext(context);vm.runInContext({json.dumps(module)},context);
function assert(x,n){{if(!x)throw new Error(n);}}
for(const id of ids)context.planningAnalysis.layerStatus[id]={{status:'SUCCESS_EMPTY',count:0}};
context.updatePlanningCompleteness();
assert(context.planningAnalysis.planningComplete===true,'all complete');
assert(context.planningAnalysis.successEmptyLayerCount===23,'empty count');
context.planningAnalysis.layerStatus[ids[3]]={{status:'ERROR',count:0,error:'timeout'}};context.updatePlanningCompleteness();
assert(context.planningAnalysis.planningComplete===false,'one error');
assert(context.planningLayerDisplayStatus(ids[3]).label.includes('자료 미확보'),'error display');
context.planningAnalysis.layerStatus[ids[3]]={{status:'NOT_RUN',count:0}};context.updatePlanningCompleteness();
assert(context.planningAnalysis.planningComplete===false&&context.planningAnalysis.notRunLayerIds.length===1,'one not run');
context.planningAnalysis.layerStatus[ids[3]]={{status:'SUCCESS_EMPTY',count:0}};context.updatePlanningCompleteness();
assert(context.planningLayerDisplayStatus(ids[3]).label==='중첩 없음','empty display');
context.recordPlanningLayerResults([{{spec:{{id:ids[3]}},status:'ERROR',features:[],error:'late'}}]);
assert(context.planningAnalysis.layerStatus[ids[3]].status==='SUCCESS_EMPTY','normal preserved');
assert(context.officialSmallParcelRatio(100,40,'MIXED')===null,'mixed small review');
assert(context.officialSmallParcelRatio(100,40,'OFFICIAL')===40,'official small');
let stat={{lower:70,upper:80}};context.hubTitleQueryState.complete=false;assert(context.thresholdStatus(stat,66.7)==='REVIEW','population gate');
context.hubTitleQueryState.complete=true;assert(context.thresholdStatus(stat,66.7)==='PASS','lower pass');
assert(context.thresholdStatus({{lower:40,upper:60}},66.7)==='FAIL','upper fail');
assert(context.thresholdStatus({{lower:60,upper:80}},66.7)==='REVIEW','bounded review');
context.planningAnalysis.planningComplete=false;let r=context.coreFactReadiness();assert(!r.ready&&r.incomplete_groups.includes('도시관리계획'),'gate blocks');
context.runSchemeChecksWhenCoreReady();assert(context.engineCalls===0,'engine blocked');
context.planningAnalysis.planningComplete=true;r=context.coreFactReadiness();assert(r.ready,'gate ready');
context.runSchemeChecksWhenCoreReady();assert(context.engineCalls===1,'engine once');
console.log('core-fact-js-unit-pass');
"""
tmp = ROOT / "_core_fact_gate_unit_tmp.js"
tmp.write_text(node_test, encoding="utf-8")
try:
    run = subprocess.run(["node", str(tmp)], capture_output=True, text=True)
finally:
    tmp.unlink(missing_ok=True)
check("runtime planning/gate/small/age scenarios", run.returncode == 0, (run.stdout + run.stderr).strip()[:500])


# Runtime Python endpoint/cache scenarios without external network calls.
try:
    tree = ast.parse(PY)
    def source_def(name):
        node = next(x for x in tree.body if isinstance(x, (ast.FunctionDef, ast.ClassDef)) and x.name == name)
        lines = PY.splitlines()
        return "\n".join(lines[node.lineno - 1:node.end_lineno])

    class TimeProxy:
        def __init__(self): self.value = 1000.0
        def time(self): return self.value
        def monotonic(self): return self.value
        def sleep(self, seconds): self.value += float(seconds)

    clock = TimeProxy()
    ns = {
        "json": json, "hashlib": hashlib, "threading": threading, "time": clock,
        "Dict": Dict, "List": List, "Optional": Optional, "Any": Any,
        "_PLANNING_LAYER_CACHE": {}, "_PLANNING_LAYER_CACHE_LOCK": threading.Lock(),
        "_PLANNING_LAYER_CACHE_TTL_SEC": 600,
    }
    exec("from __future__ import annotations\n" + "\n\n".join(source_def(x) for x in [
        "PlanningLayerFetchError", "_planning_geometry_signature", "_planning_cache_get",
        "_planning_cache_put", "_planning_layer_result",
    ]), ns)
    geom1 = {"type": "Polygon", "coordinates": [[[126.9, 37.5], [126.91, 37.5], [126.91, 37.51], [126.9, 37.5]]]}
    geom2 = {"type": "Polygon", "coordinates": [[[126.92, 37.5], [126.93, 37.5], [126.93, 37.51], [126.92, 37.5]]]}
    calls = []
    ns["_fetch_planning_layer_once"] = lambda layer, geom: (calls.append((layer, ns["_planning_geometry_signature"](geom))) or ([], "direct"))
    a = ns["_planning_layer_result"](layer_ids[0], geom1)
    b = ns["_planning_layer_result"](layer_ids[0], geom1)
    c = ns["_planning_layer_result"](layer_ids[0], geom2)
    check("SUCCESS_EMPTY is normal", a["status"] == "SUCCESS_EMPTY" and a["feature_count"] == 0)
    check("same geometry normal cache reused", len(calls) == 2 and b.get("cache_hit") is True)
    check("different geometry cache isolated", c.get("cache_hit") is False and calls[0][1] != calls[1][1])

    attempts = {"n": 0}
    def fail_fetch(layer, geom):
        attempts["n"] += 1
        raise ns["PlanningLayerFetchError"]("timeout", retryable=True)
    ns["_PLANNING_LAYER_CACHE"].clear()
    ns["_fetch_planning_layer_once"] = fail_fetch
    d = ns["_planning_layer_result"](layer_ids[1], geom1)
    check("retryable planning error retried twice", d["status"] == "ERROR" and d["attempts"] == 3 and attempts["n"] == 3)
    check("ERROR not cached", not ns["_PLANNING_LAYER_CACHE"])

    pnus = ["1111111111100000001", "1111111111100000002", "1111111111100000003"]
    def title_rows(pnu):
        if pnu.endswith("1"):
            return [{"mgmBldrgstPk": "A", "useAprDay": "20000101"}]
        if pnu.endswith("2"):
            return []
        raise RuntimeError("hub timeout")
    class Logger:
        def warning(self, *args, **kwargs): pass
    hub_ns = {
        "List": List, "Dict": Dict, "Any": Any, "ThreadPoolExecutor": ThreadPoolExecutor,
        "as_completed": as_completed, "building_hub_ready": lambda: True,
        "_query_building_hub_title": title_rows, "_normalize_building_title": lambda item, pnu: {**item, "pnu": pnu},
        "logger": Logger(), "ENGINE_AS_OF_DATE": types.SimpleNamespace(isoformat=lambda: "2026-09-21"),
        "HTTPException": RuntimeError,
    }
    exec("from __future__ import annotations\n" + source_def("building_hub_title_batch"), hub_ns)
    hub = hub_ns["building_hub_title_batch"](types.SimpleNamespace(pnus=pnus))
    states = {x["pnu"]: x["status"] for x in hub["pnu_status"]}
    check("HUB SUCCESS_DATA distinguished", states[pnus[0]] == "SUCCESS_DATA")
    check("HUB SUCCESS_EMPTY distinguished", states[pnus[1]] == "SUCCESS_EMPTY")
    check("HUB ERROR distinguished", states[pnus[2]] == "ERROR" and hub["complete"] is False)
except Exception as exc:
    check("runtime Python endpoint/cache scenarios", False, repr(exc))
else:
    check("runtime Python endpoint/cache scenarios", True)


# Syntax and legacy regressions.
js = ROOT / "_r13_inline_tmp.js"
js.write_text(HTML[HTML.index("<script>") + len("<script>"):HTML.rindex("</script>")], encoding="utf-8")
try:
    rjs = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
finally:
    js.unlink(missing_ok=True)
check("browser JavaScript syntax", rjs.returncode == 0, rjs.stderr.strip()[:300])
rpy = subprocess.run([sys.executable, "-m", "py_compile", str(ROOT / "app.py")], capture_output=True, text=True)
check("app.py compile", rpy.returncode == 0, rpy.stderr.strip()[:300])

failed = [name for name, passed, _ in checks if not passed]
print(f"\nSUMMARY {len(checks) - len(failed)}/{len(checks)} PASS")
if failed:
    print("FAILED:", failed)
    raise SystemExit(1)
