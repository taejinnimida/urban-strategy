from pathlib import Path
import importlib.util
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent
HTML = (ROOT / "app.html").read_text(encoding="utf-8")
PY = (ROOT / "app.py").read_text(encoding="utf-8")


def check(name: str, ok: bool) -> None:
    if not ok:
        raise AssertionError(name)
    print("PASS", name)


head = HTML.split("</head>", 1)[0]
station = HTML.index('id="siteDetail_station"')
center = HTML.index('id="siteDetail_center"')
arterial = HTML.index('id="siteDetail_activationArterial"')

check("live UI has four-column site-analysis grid", ".site-detail-layout .spatial-module-grid{grid-template-columns:repeat(4" in head)
check("live UI has two-column wide cards", ".site-analysis-wide{grid-column:span 2}" in head)
check("station-center-arterial DOM order", station < center < arterial)
check("arterial uses two columns", 'site-analysis-context site-analysis-wide" id="siteDetail_activationArterial"' in HTML)
check("road condition uses two columns", 'site-analysis-diagnostic site-analysis-wide" id="siteDetail_roadCondition"' in HTML)
check("road diagnosis uses two columns", 'site-analysis-diagnostic site-analysis-wide" id="siteDetail_schemeRoad"' in HTML)

check("regulatory step registered", "'규제지역 3종·문화재 보강'" in HTML.split("const ANALYSIS_PROGRESS_ORDER=", 1)[1].split(";", 1)[0])
check("three regulatory progress cards", all(f"label:'{label}'" in HTML for label in ("자연환경 규제", "교통·안보 규제", "자연재해 규제")))
check("planning cards have independent kinds", all(f"planningKinds:['{kind}']" in HTML for kind in ("district", "facility", "heritage")))

check("NED endpoint exposes successful PNU", '"successful_pnus": sorted(success_set)' in PY)
check("NED endpoint exposes failed PNU", '"failed_pnus": sorted({row["pnu"] for row in errors})' in PY)
check("frontend retries failed NED PNU only", "previousNed?.failed_pnus||[]" in HTML)
check("frontend preserves successful disaster data", "const keepDisaster=retryFailedOnly" in HTML)
check("partial regulatory analysis enters isolated retry", "const regulatoryPartial=partial.find(step=>step.label==='규제지역 3종·문화재 보강')" in HTML)

# Runtime failure injection: one successful PNU must survive one failed PNU,
# and the failed PNU must be exposed for failed-only retry.
spec = importlib.util.spec_from_file_location("urban_strategy_r14", ROOT / "app.py")
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)
ok_pnu, bad_pnu = "1111010100100010000", "1111010100100020000"
original_rows, original_key = module._land_use_rows_for_pnu, module._vworld_key
try:
    module._vworld_key = lambda: "test-key"

    def injected_rows(pnu: str):
        if pnu == bad_pnu:
            raise RuntimeError("injected transient NED failure")
        return [{"prposAreaDstrcCodeNm": "자연공원구역", "prposAreaDstrcCode": "NATURE", "manageNo": "T1"}]

    module._land_use_rows_for_pnu = injected_rows
    result = module.regulatory_land_use_restrictions(SimpleNamespace(pnus=[ok_pnu, bad_pnu]))
    check("runtime NED partial is not empty", result["status"] == "partial" and result["success_parcels"] == 1)
    check("runtime NED preserves successful PNU", result["successful_pnus"] == [ok_pnu] and result["categories"]["natural_park"]["present"] is True)
    check("runtime NED exposes failed-only retry target", result["failed_pnus"] == [bad_pnu] and result["error_parcels"] == 1)
finally:
    module._land_use_rows_for_pnu, module._vworld_key = original_rows, original_key

print("SUMMARY 17/17 PASS")
