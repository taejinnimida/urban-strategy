"""배포 전 핵심 회귀검사. 실행: python regression_checks.py"""

import os
import gc
import time
import hashlib
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

    assert "other_renewal" in available_types
    other = next(f for f in features if f["properties"]["renewal_type"] == "other_renewal")
    other_point = shape(other["geometry"]).representative_point()
    other_site = box(other_point.x - 0.00001, other_point.y - 0.00001, other_point.x + 0.00001, other_point.y + 0.00001)
    other_result = app.analyze_renewal_intersections(other_site.__geo_interface__)
    # 표시 전용 기타 정비 도형은 기존 재개발/재건축 판정값을 직접 만들지 않는다.
    assert other_result["renewal_area_type"] != "other_renewal"

    outside = box(127.49, 37.49, 127.50, 37.50)
    empty = app.analyze_renewal_intersections(outside.__geo_interface__)
    assert empty["status"] == "none"
    assert empty["renewal_area_type"] == "none"


def check_development_server_intersection() -> None:
    features = app._development_reference_data()["features"]
    assert len(features) >= 500
    available = {f["properties"]["development_kind"] for f in features}
    assert {"urban_development", "public_housing", "other_project"}.issubset(available)
    for kind in ("urban_development", "public_housing", "other_project"):
        feature = next(f for f in features if f["properties"]["development_kind"] == kind and shape(f["geometry"]).is_valid)
        point = shape(feature["geometry"]).representative_point()
        site = box(point.x - 0.00001, point.y - 0.00001, point.x + 0.00001, point.y + 0.00001)
        result = app.analyze_development_intersections(site.__geo_interface__)
        assert result["status"] == "matched"
        assert any(row["properties"]["development_kind"] == kind for row in result["overlaps"])
        assert result["context_features"]
        assert result["metadata"]["source"] == "서울 의제처리구역 위치정보(UQ181)"

    outside = box(127.49, 37.49, 127.50, 37.50)
    empty = app.analyze_development_intersections(outside.__geo_interface__)
    assert empty["status"] == "none"



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


def check_redevelopment_boolean_gate() -> None:
    """면적 AND 노후도 AND 추가요건 1개가 실제 최종판정을 지배해야 한다."""
    base = {
        "area_m2": 22716,
        "total_building_count": 53,
        "old_building_count": 33,
        "total_parcel_count": 79,
        "small_parcel_count": 18,
        "house_density_per_ha": 34.3,
        "total_floor_area_m2": 1000,
        "old_floor_area_m2": 515,
    }
    # 화면 사례: 확인된 추가요건은 모두 미달, 접도율만 미확인 -> PASS가 아니라 REVIEW.
    pending = app.evaluate_redevelopment(base)
    assert pending["physical_eligibility"]["status"] == "REVIEW"

    # 접도율도 미달로 확인되면 선택요건 전부 FAIL -> 주택재개발 신규입안 FAIL.
    failed = app.evaluate_redevelopment({
        **base,
        "road_basis_building_count": 53,
        "road_access_building_count_6m": 30,
    })
    assert failed["physical_eligibility"]["status"] == "FAIL"

    # 접도율 40% 이하가 공식 GIS AUTO로 확인되면 +1 충족 -> PASS.
    passed = app.evaluate_redevelopment({
        **base,
        "road_basis_building_count": 53,
        "road_access_building_count_6m": 20,
    })
    assert passed["physical_eligibility"]["status"] == "PASS"


def check_centerline_width_buffer() -> None:
    assert app._road_width_m({"road_bt": "8.0m"}) == 8.0
    line = LineString([(126.977, 37.565), (126.978, 37.565)])
    polygon = app._centerline_road_polygon(line, 8.0)
    assert polygon is not None
    assert polygon.geom_type in {"Polygon", "MultiPolygon"}
    assert polygon.area > 0


def check_bundled_road_dataset() -> None:
    """배포본의 서울 실폭도로 원본·좌표계·공간검색이 실제로 작동해야 한다."""
    road_zip = app._road_zip_path()
    assert road_zip and Path(road_zip).name == "road_seoul.zip"
    assert Path(road_zip).stat().st_size > 20_000_000
    app._road_spatial_layers.cache_clear()
    layers = app._road_spatial_layers()
    assert layers["available"] is True
    assert layers["road_mode"] == "real_width_polygon"
    assert layers["rw_count"] == 60534
    assert layers["manage_count"] == 0
    road = layers["rw"][0]["geometry"]
    point = road.representative_point()
    site = box(point.x - 0.00002, point.y - 0.00002, point.x + 0.00002, point.y + 0.00002)
    result = app.analyze_road_intersections(site.__geo_interface__)
    assert result["rw"]["features"]
    assert result["metadata"]["road_mode"] == "real_width_polygon"
    assert result["metadata"]["source"] == "서버 내장 공식 실폭도로 TL_SPRD_RW"


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



def check_age_annotation_reference_only() -> None:
    """서버 age_status는 지도 참고값일 뿐 법적 사업판정 근거가 아님을 고정한다."""
    assert app.ENGINE_AS_OF_DATE.isoformat() == "2026-08-27"
    known = app._age_annotation({
        "useAprDay": "19960826",
        "strctCdNm": "철근콘크리트구조",
        "mainPurpsCdNm": "업무시설",
    })
    assert known["age_basis"] == "REFERENCE_ONLY"
    assert known["age_threshold_years"] == 30
    unknown = app._age_annotation({"useAprDay": ""})
    assert unknown["age_status"] == "UNKNOWN"
    assert unknown["age_basis"] == "REFERENCE_ONLY"

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
        "function redevelopmentEntryGate(c)", "접도율 확인 전 보류",
        "시행령 별표 1 제2호·제4호 / 조례 제6조제1항제2·3호",
        "근거·조문·기준일", "VWorld 실폭도로·도로구간 API GIS AUTO",
        "TL_SPRD_RW 공식 실폭도형 산정",
        "정비사업 관련 현황도", "도시계획·개발사업 현황도",
        "공공주택지구", "기타 정비",
        "대중교통 중심지역 · 간선도로변", "의료시설 중심지역",
        "운영기준 1-3-1 가목", "운영기준 1-3-1 나목", "운영기준 1-3-2",
    ):
        assert text in html
    assert "/api/spatial/renewal-intersections" in html
    assert "/api/spatial/development-intersections" in html
    assert "/api/spatial/roads" in html
    assert "st.areaGate==='FAIL'||st.structural==='FAIL'" in html
    assert "n==='redevelopment'&&st.legalEntry!=='PASS'" in html
    assert "const disabled=st.state==='off'" in html
    assert "r.hardGate==='AREA'" in html
    assert "최신 공식 세부기준 원문 미확보로 자동 PASS 금지" in html
    assert "const safeMinArea=specialLowZone?5000:1000" in html
    assert "safeSupplyType:schemeVal('safe_supply_type')||'standard'" in html
    assert "역세권은 지구단위계획구역이면서" not in html
    assert "safeArterialSpatialCandidate" in html and "safeMedicalPath" in html
    assert "SAFE_OP" in html and "verified:true" in html
    assert "c.roadQuality==='ESTIMATE'?'REVIEW':'PASS'" in html
    assert "SCHEME_MODULE_API_VERSION='2026-08-27-v2-age-regimes'" in html
    assert "function buildSiteFactStore()" in html
    # 노후도는 공통 OLD 하나가 아니라 원자료 -> 제도별 파생현황 -> 사업별 링크 구조여야 한다.
    assert "function buildingRawFacts()" in html
    assert "function buildAgeDerivedFacts" in html
    assert "function urbanPlanningAgeAssessment" in html
    assert "function renewalAgeAssessment" in html
    assert "URBAN_PLANNING_AGE" in html and "RENEWAL_AGE" in html
    assert "records:buildingRawFacts()" in html
    assert "derived:{age:null}" in html
    assert "aging:{route:route.route,assessment:schemeAgeFact(store,'activation',route.route)}" in html
    assert "도시계획계 노후도" in html and "도정법계 노후도" in html
    # 사업별 판정에 쓰이는 노후도 파생현황은 공간현황 박스에서 모두 보여야 한다.
    assert "제도별 노후도 현황" in html
    assert 'id="spAgeFactList"' in html and "AGE_SPATIAL_FACT_META" in html
    assert "사업판정은 이 현황 Fact를 그대로 호출합니다" in html
    assert '사용승인일 확인</span><b id="ccBuildingOld"' in html
    assert '20년 경과(참고)</span><b id="ccBuildingOldRatio"' in html
    assert '노후건축물</span><b id="ccBuildingOld"' not in html
    # 독립 사업모듈의 추가현황은 Fact Store에 등록되고 공간현황 박스에 자동 노출되어야 한다.
    assert "사업별 추가 현황" in html and 'id="spSchemeFactList"' in html
    assert "spatial_status_rows" in html and "renderSchemeSpecificSpatialStatus(store)" in html
    assert "store.scheme_specific.activation=fact" in html
    assert "팝업에서 새 현황을 임의 생성하지 않습니다" in html
    # 사업판정 영역에서 예전 공통 20/30년·OLD 판정을 직접 사용하면 안 된다.
    decision_start=html.index("function longtermUrbanDeeming")
    decision_end=html.index("function calculateScheme", decision_start)
    decision=html[decision_start:decision_end]
    assert "c.age20" not in decision
    assert "c.age30" not in decision
    assert "c.oldFloorRatio" not in decision
    assert "analysisState.metrics.old_count" not in decision
    assert "age_status==='OLD'" not in decision
    assert "f.aging.assessment" in decision
    assert "schemeAgeFact(c,'redevelopment')" in decision
    assert "schemeAgeFact(c,'safe')" in decision
    assert "schemeAgeFact(c,'longterm',route)" in decision
    assert "schemeAgeFact(c,'growth_potential',typ)" in decision
    assert "const SCHEME_MODULES=" in html
    assert "collectFacts:activationSpatialFacts" in html
    assert "function checkActivationFromFacts(store,f)" in html
    assert "역세권활성화사업 기초검토서" in html
    assert "선순위 사업 미리보기" in html
    assert "위치기반 매스 이미지" in html
    assert html.count("function check") >= 14
    assert "redevelopment:checkRedevelopment" in html
    assert "innovation:checkInnovation" in html


def check_release_files() -> None:
    root = Path(app.BASE_DIR)
    assert app.app.version == "2.5.0"
    assert Path(app.STATIC_HTML_PATH).is_file()
    assert Path(app.DATA_DIR).is_dir()
    assert (root / "CHANGELOG_v2.5.0.txt").exists()
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert readme.startswith("# 서울 도시정비플랫폼 Web MVP v2.5.0")
    assert "GitHub 웹 업로드" in readme
    assert "14개 사업방식 근거 기준일" in readme
    assert "분석번호" in readme
    assert "SEOUL_OPEN_DATA_KEY" in readme
    assert (root / "RULE_AUDIT_v2.5.0.md").exists()
    structured = root / "static" / "app.html"
    root_html = root / "app.html"
    # 구조형 작업본에서는 두 HTML의 동일성을 검사하고, 평면 ZIP 검증본에서는
    # root app.html 자체를 검사한다. 평면 ZIP에는 최상위 하위폴더를 만들지 않는다.
    if structured.exists():
        assert hashlib.sha256(root_html.read_bytes()).hexdigest() == hashlib.sha256(structured.read_bytes()).hexdigest()
    assert root_html.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")


def _release_heavy_spatial_cache(kind: str) -> None:
    if kind == "renewal":
        app._renewal_spatial_index.cache_clear()
        app._renewal_reference_data.cache_clear()
    elif kind == "development":
        app._development_spatial_index.cache_clear()
        app._development_reference_data.cache_clear()
    elif kind == "road":
        app._road_spatial_layers.cache_clear()
    gc.collect()


def _run(label, fn) -> None:
    started = time.time()
    fn()
    print(f"PASS {label} ({time.time()-started:.1f}s)", flush=True)


def main() -> None:
    _run("measurement", check_measurement)
    _run("renewal spatial", check_renewal_server_intersection)
    _release_heavy_spatial_cache("renewal")
    _run("development spatial", check_development_server_intersection)
    _release_heavy_spatial_cache("development")
    _run("redevelopment area gate", check_area_gate)
    _run("redevelopment boolean gate", check_redevelopment_boolean_gate)
    _run("centerline width buffer", check_centerline_width_buffer)
    _run("bundled road", check_bundled_road_dataset)
    _release_heavy_spatial_cache("road")
    _run("road zip pipeline", check_road_zip_pipeline)
    _release_heavy_spatial_cache("road")
    _run("feedback + UI", check_feedback_and_ui)
    _run("release files", check_release_files)
    print("v2.5.0 regression checks: PASS")


if __name__ == "__main__":
    main()
