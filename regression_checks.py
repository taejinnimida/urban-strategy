"""v2.4.0 배포 전 핵심 회귀검사. 실행: python regression_checks.py"""

from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from shapely.geometry import LineString, box, shape

import app


ROOT = Path(__file__).resolve().parent
HTML = Path(app.STATIC_DIR, "app.html").read_text(encoding="utf-8")
SOURCE_HTML = Path(app.STATIC_DIR, "app.html")
SOURCE_DATA = Path(app.DATA_DIR)


def check_measurement() -> None:
    geom = {
        "type": "Polygon",
        "coordinates": [[[126.977, 37.565], [126.978, 37.565], [126.978, 37.566], [126.977, 37.566], [126.977, 37.565]]],
    }
    assert app.measure_geojson(geom)["area_m2"] > 0


def check_reference_data() -> None:
    assert len(app._station_reference_data()["features"]) == 349
    assert len(app._center_reference_data()["features"]) == 76
    assert len(app._renewal_reference_data()["features"]) == 3246


def check_renewal_server_intersection() -> None:
    features = app._renewal_reference_data()["features"]
    legal = next(
        feature for feature in features
        if feature["properties"]["source"] == "legal"
        and feature["properties"]["renewal_type"] != "promotion"
        and shape(feature["geometry"]).is_valid
    )
    point = shape(legal["geometry"]).representative_point()
    site = box(point.x - 0.000005, point.y - 0.000005, point.x + 0.000005, point.y + 0.000005)
    result = app.analyze_renewal_intersections(site.__geo_interface__)
    assert result["status"] == "matched"
    assert result["primary"]["properties"]["source"] == "legal"
    assert result["renewal_area_type"] != "none"
    assert result["overlaps"][0]["properties"]["site_overlap_pct"] > 0

    empty = app.analyze_renewal_intersections(box(127.49, 37.49, 127.50, 37.50).__geo_interface__)
    assert empty["status"] == "none"
    assert empty["renewal_area_type"] == "none"


def check_area_and_road() -> None:
    result = app.evaluate_redevelopment({"area_m2": 1399})
    area = next(row for row in result["checks"] if row["id"] == "AREA")
    assert area["status"] == "FAIL"
    assert result["physical_eligibility"]["status"] == "FAIL"

    assert app._road_width_m({"road_bt": "8.0m"}) == 8.0
    polygon = app._centerline_road_polygon(LineString([(126.977, 37.565), (126.978, 37.565)]), 8.0)
    assert polygon is not None and polygon.geom_type in {"Polygon", "MultiPolygon"} and polygon.area > 0


def check_feedback_analysis_id() -> None:
    feedback_id = app._store_feedback({
        "visitor_id": "regression-visitor",
        "session_id": "regression-session",
        "analysis_id": "11111111-1111-4111-8111-111111111111",
        "category": "data",
        "message": "회귀검사",
        "pnu_list": [],
        "recommendations": [],
    })
    row = next(row for row in app._feedback_rows() if row.get("id") == feedback_id)
    assert row["analysis_id"].startswith("11111111")
    assert app._set_feedback_status(feedback_id, "done")


def check_rule_ui() -> None:
    required = (
        "⚙ 관리자", "오류·개선의견", "analysis_id", "RULE_SOURCE_CATALOG",
        "AREA_HARD_GATE_ITEMS", "hardGate==='AREA'", "rowTrust", "numericGapText",
        "자동확정", "공부·현장 확인", "최대확보 시나리오", "최신 세부기준 확인 전 자동입력 보류",
        "간선도로 교차지", "200m 이내", "기존 사업구역 배타관계",
        "ccPlanningDistrictPlan", "ccPlanningRenewal", "실폭도로·접도율 공간분석",
        "st.areaGate!=='PASS'||st.structural!=='PASS'", "/api/spatial/renewal-intersections", "/api/spatial/roads",
    )
    for text in required:
        assert text in HTML, text

    names = re.search(r"const schemeNames=\{(.*?)\n\};", HTML, flags=re.S)
    assert names
    assert len(re.findall(r"^\s{2}[a-z_]+:", names.group(1), flags=re.M)) == 14

    assert "if(row.status==='PASS'&&(!source.current||!source.verified))" in HTML
    assert "zoneStatus='REVIEW'" in HTML and "주거지역 중 시장이 정하는 지역" in HTML
    assert "[source:r.properties.source" not in HTML


def check_javascript_syntax() -> None:
    node = shutil.which("node")
    if not node:
        return
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", HTML, flags=re.S | re.I)
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as fp:
        fp.write("\n".join(scripts))
        js_path = fp.name
    subprocess.run([node, "--check", js_path], check=True, capture_output=True, text=True)
    Path(js_path).unlink(missing_ok=True)


def check_flat_deployment() -> None:
    with tempfile.TemporaryDirectory(prefix="urban-flat-") as td:
        target = Path(td)
        shutil.copy2(ROOT / "app.py", target / "app.py")
        shutil.copy2(SOURCE_HTML, target / "app.html")
        for name in ("stations.json", "centers.json", "uq120_project.zip", "uq181_legal.zip"):
            shutil.copy2(SOURCE_DATA / name, target / name)
        code = (
            "import app; "
            "assert app.ASSET_LAYOUT=='flat-compatible'; "
            "assert app.health()['asset_files_ready']; "
            "assert '⚙ 관리자' in app.home(); "
            "assert len(app.reference_renewal_zones()['features'])==3246; "
            "print('flat-ok')"
        )
        run = subprocess.run([sys.executable, "-c", code], cwd=target, check=True, capture_output=True, text=True)
        assert "flat-ok" in run.stdout


def main() -> None:
    check_measurement()
    check_reference_data()
    check_renewal_server_intersection()
    check_area_and_road()
    check_feedback_analysis_id()
    check_rule_ui()
    check_javascript_syntax()
    check_flat_deployment()
    health = app.health()
    assert health["app"] == "seoul_urban_renewal_platform_v2.4.0"
    assert health["asset_files_ready"]
    print("v2.4.0 regression checks: PASS")


if __name__ == "__main__":
    main()
