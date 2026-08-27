"""배포 전 핵심 회귀검사. 실행: python regression_checks.py"""

import os
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

import shapefile
from shapely.geometry import LineString, box, shape

import app


def check_measurement() -> None:
    geom = {
        "type": "Polygon",
        "coordinates": [[[126.977, 37.565], [126.978, 37.565], [126.978, 37.566], [126.977, 37.566], [126.977, 37.565]]],
    }
    measured = app.measure_geojson(geom)
    assert measured["area_m2"] > 0


def check_renewal_server_intersection() -> None:
    features = app._renewal_reference_data()["features"]
    assert len(features) >= 3000
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

    # 법정구역·사업/후보구역·촉진구역 유형이 서버 공간검색에서 실제로
    # 반환되는지 유형별 대표도형으로 확인한다.
    expected_types = {
        "housing_district", "urban_district", "reconstruction",
        "housing_planned", "urban_planned", "promotion",
    }
    available_types = {f["properties"]["renewal_type"] for f in features}
    assert expected_types.issubset(available_types)
    for renewal_type in sorted(expected_types):
        feature = next(
            f for f in features
            if f["properties"]["renewal_type"] == renewal_type
            and shape(f["geometry"]).is_valid
        )
        probe = shape(feature["geometry"]).representative_point()
        probe_site = box(probe.x - 0.00001, probe.y - 0.00001, probe.x + 0.00001, probe.y + 0.00001)
        probe_result = app.analyze_renewal_intersections(probe_site.__geo_interface__)
        assert any(
            row["properties"]["renewal_type"] == renewal_type
            and row["properties"]["name"] == feature["properties"]["name"]
            for row in probe_result["overlaps"]
        )

    outside = box(127.49, 37.49, 127.50, 37.50)
    empty = app.analyze_renewal_intersections(outside.__geo_interface__)
    assert empty["status"] == "none"
    assert empty["renewal_area_type"] == "none"


def check_area_gate() -> None:
    result = app.evaluate_redevelopment({"area_m2": 1399})
    area = next(row for row in result["checks"] if row["id"] == "AREA")
    assert area["status"] == "FAIL"
    assert result["physical_eligibility"]["status"] == "FAIL"

    pending = app.evaluate_redevelopment({"area_m2": 7000})
    pending_area = next(row for row in pending["checks"] if row["id"] == "AREA")
    assert pending_area["status"] == "UNKNOWN"

    approved = app.evaluate_redevelopment({"area_m2": 7000, "area_5000_exception_approved": True})
    approved_area = next(row for row in approved["checks"] if row["id"] == "AREA")
    assert approved_area["status"] == "PASS"


def check_centerline_width_buffer() -> None:
    assert app._road_width_m({"road_bt": "8.0m"}) == 8.0
    line = LineString([(126.977, 37.565), (126.978, 37.565)])
    polygon = app._centerline_road_polygon(line, 8.0)
    assert polygon is not None
    assert polygon.geom_type in {"Polygon", "MultiPolygon"}
    assert polygon.area > 0


def check_road_zip_pipeline() -> None:
    """공식 도로중심선+폭원 ZIP 설치 시 서버 자동공간분석이 작동해야 한다."""
    with tempfile.TemporaryDirectory() as tmp:
        stem = Path(tmp, "TL_SPRD_MANAGE")
        writer = shapefile.Writer(str(stem), shapeType=shapefile.POLYLINE)
        writer.field("ROAD_BT", "N", size=8, decimal=1)
        writer.line([[(126.9765, 37.5655), (126.9785, 37.5655)]])
        writer.record(8.0)
        writer.close()
        zip_path = Path(tmp, "road_seoul.zip")
        with zipfile.ZipFile(zip_path, "w") as archive:
            for suffix in ("shp", "shx", "dbf"):
                archive.write(stem.with_suffix(f".{suffix}"), f"TL_SPRD_MANAGE.{suffix}")

        previous_crs = os.environ.get("ROAD_DATA_CRS")
        os.environ["ROAD_DATA_CRS"] = "EPSG:4326"
        try:
            with patch.object(app, "_road_zip_path", return_value=str(zip_path)):
                app._road_spatial_layers.cache_clear()
                layers = app._road_spatial_layers()
                assert layers["available"] is True
                assert layers["road_mode"] == "centerline_width_buffer"
                site = box(126.9770, 37.5653, 126.9780, 37.5657)
                result = app.analyze_road_intersections(site.__geo_interface__)
                assert result["rw"]["features"]
                assert result["metadata"]["road_mode"] == "centerline_width_buffer"
        finally:
            app._road_spatial_layers.cache_clear()
            if previous_crs is None:
                os.environ.pop("ROAD_DATA_CRS", None)
            else:
                os.environ["ROAD_DATA_CRS"] = previous_crs


def check_feedback_and_ui() -> None:
    analysis_id = "analysis-regression-0001"
    app._store_analytics_event({
        "analysis_id": analysis_id,
        "visitor_id": "regression-visitor",
        "event_type": "analysis_complete",
        "pnu_list": [],
        "recommendations": [],
        "result_summary": {},
    })
    assert any(row.get("analysis_id") == analysis_id for row in app._analytics_rows())
    feedback_id = app._store_feedback({
        "analysis_id": analysis_id,
        "visitor_id": "regression-visitor",
        "category": "data",
        "message": "회귀검사",
        "pnu_list": [],
        "recommendations": [],
    })
    assert any(row.get("id") == feedback_id and row.get("analysis_id") == analysis_id for row in app._feedback_rows())
    assert app._set_feedback_status(feedback_id, "done")

    html = Path(app.STATIC_DIR, "app.html").read_text(encoding="utf-8")
    for text in (
        "오류·개선의견", 'href="/admin"', "⚙ 관리자", "planningRenewalSummary",
        "실폭도로·접도율 공간분석", "RULE_SOURCE_CATALOG", "sourceLocator",
        "기준까지 차이", "다음 조치·대안", "자동확정", "공부·현장 확인",
        "analysis_id:analysisId", "장기전세 간선도로 교차지역 200m 판정",
        "ccLandMini", "ccBuildingMini", "ccPlanningMini", "ccStationMini", "ccCenterMini",
        "ccPlanningDistrictPlan", "ccPlanningRenewal", "smallParcelLayer", "oldParcelLayer",
        "safe_supply_type", "safe_adjacent_high_zone",
        "구역계만 그리면 14개 사업방식을 자동 검토",
        "siteRoadNetworkStats", "scheme_road20_perimeter_ratio",
        "최신 공식 시행본 미확보 · 계획용적률 자동입력 금지",
    ):
        assert text in html
    assert "/api/spatial/renewal-intersections" in html
    assert "/api/spatial/roads" in html
    assert "st.areaGate!=='PASS'||st.structural!=='PASS'" in html
    assert "r.hardGate==='AREA'" in html
    assert "최신 공식 세부기준 원문 미확보로 자동 PASS 금지" in html
    assert "const safeMinArea=specialLowZone?5000:1000" in html
    assert "safeSupplyType:schemeVal('safe_supply_type')||'standard'" in html
    assert "운영기준 4-2-1" in html
    assert "SAFE_OP" in html and "verified:true" in html
    assert "c.roadQuality==='ESTIMATE'?'REVIEW':'PASS'" in html
    assert html.count("function check") >= 14
    assert "redevelopment:checkRedevelopment" in html
    assert "innovation:checkInnovation" in html


def check_release_files() -> None:
    root = Path(app.BASE_DIR)
    assert app.app.version == "2.4.0"
    assert (root / "CHANGELOG_v2.4.0.txt").exists()
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert readme.startswith("# 서울 도시정비플랫폼 Web MVP v2.4.0")
    assert "14개 사업방식 근거 기준일" in readme
    assert "분석번호" in readme
    assert (root / "RULE_AUDIT_v2.4.0.md").exists()


def main() -> None:
    check_measurement()
    check_renewal_server_intersection()
    check_area_gate()
    check_centerline_width_buffer()
    check_road_zip_pipeline()
    check_feedback_and_ui()
    check_release_files()
    print("v2.4.0 regression checks: PASS")


if __name__ == "__main__":
    main()
