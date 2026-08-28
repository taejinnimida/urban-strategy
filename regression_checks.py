from zoneinfo import ZoneInfo
from datetime import datetime
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
    assert app.ENGINE_AS_OF_DATE == datetime.now(ZoneInfo("Asia/Seoul")).date()
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
        "도로중심선·접도조건 개략분석", "RULE_SOURCE_CATALOG", "sourceLocator",
        "기준까지 차이", "다음 조치·대안", "자동확정", "공부·현장 확인",
        "analysis_id:analysisId", "장기전세 간선도로 교차지역 200m 판정",
        "ccLandMini", "ccBuildingMini", "ccPlanningMini", "ccStationMini", "ccCenterMini",
        "ccPlanningDistrictPlan", "ccPlanningRenewal", "smallParcelLayer", "oldParcelLayer",
        "safe_supply_type", "safe_adjacent_high_zone",
        "독립엔진 9개 사업을 자동 검토하고, 나머지 5개는 재설계 예정",
        "siteRoadNetworkStats", "scheme_road20_perimeter_ratio",
        "최신 공식 시행본 미확보 · 계획용적률 자동입력 금지",
        "시행령 별표 1 제2호·제4호 / 조례 제6조제1항제2·3호",
        "도로중심선 ROAD_BT 기반 개략범위",
        "정비사업 관련 현황도", "도시계획·개발사업 현황도",
        "공공주택지구", "기타 정비",
        "대중교통 중심지역 · 간선도로변", "의료시설 중심지역",
        "1-3-1 가목", "1-3-1 나목", "운영기준 1-3-2",
    ):
        assert text in html
    assert "/api/spatial/renewal-intersections" in html
    assert "/api/spatial/development-intersections" in html
    assert "TL_SPRD_MANAGE" in html
    assert "TL_SPRD_RW" not in html
    assert "st.areaGate==='FAIL'||st.structural==='FAIL'" in html
    assert "const disabled=st.state==='off'" in html
    assert "r.hardGate==='AREA'" in html
    assert "최신 공식 세부기준 원문 미확보로 자동 PASS 금지" in html
    assert "min_m2:specialLowZone?5000:1000" in html
    assert "safeSupplyType:schemeVal('safe_supply_type')||'standard'" in html
    assert "역세권은 지구단위계획구역이면서" not in html
    assert "safeArterialSpatialCandidate" in html and "safeMedicalPath" in html
    assert "SAFE_OP" in html and "verified:true" in html
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
    # 독립 사업모듈 영역에서 예전 공통 20/30년·OLD 판정을 직접 사용하면 안 된다.
    decision_start=html.index("function growthPotentialSpatialFacts")
    decision_end=html.index("const SCHEME_MODULES=", decision_start)
    decision=html[decision_start:decision_end]
    assert "c.age20" not in decision
    assert "c.age30" not in decision
    assert "c.oldFloorRatio" not in decision
    assert "analysisState.metrics.old_count" not in decision
    assert "age_status==='OLD'" not in decision
    assert "f.aging.assessment" in decision
    assert "schemeAgeFact(store,'safe')" in decision
    assert "schemeAgeFact(store,'longterm',route)" in decision
    assert "schemeAgeFact(store,'growth_potential',route)" in decision
    assert "schemeAgeFact(store,'urban_redevelopment')" in decision
    assert "const SCHEME_MODULES=" in html
    assert "SCHEME_MODULE_API_VERSION='2026-08-28-v5-nine-independent-five-shells'" in html
    assert "const SHELL_SCHEMES=new Set(['redevelopment','reconstruction','residential_environment','smallscale','general_housing'])" in html
    assert "기존 판정엔진 삭제 완료 · 현재 추천/우선순위 미반영" in html
    assert "collectFacts:activationSpatialFacts" in html
    assert "function checkActivationFromFacts(store,f)" in html
    assert "역세권활성화사업 기초검토서" in html
    assert "선순위 사업 미리보기" in html
    assert "위치기반 매스 이미지" in html
    assert "collectFacts:innovationSpatialFacts" in html


def check_four_independent_scheme_modules() -> None:
    """성장잠재권·안심주택·상생주택·역세권복합개발은 현황 Fact→독립엔진→전용팝업 구조여야 한다."""
    html = Path(app.STATIC_DIR, "app.html").read_text(encoding="utf-8")

    # 4개 독립 Fact collector / Rule engine / 전용 검토서가 모두 존재한다.
    required = (
        "function growthPotentialSpatialFacts(store)", "function checkGrowthPotentialFromFacts(store,f)",
        "function safeHousingSpatialFacts(store)", "function checkSafeFromFacts(store,f)",
        "function sharedHousingSpatialFacts(store)", "function checkSharedHousingFromFacts(store,f)",
        "function stationComplexSpatialFacts(store)", "function checkStationComplexFromFacts(store,f)",
        "function renderGrowthPotentialDetailPopup()", "function renderSafeHousingDetailPopup()",
        "function renderSharedHousingDetailPopup()", "function renderStationComplexDetailPopup()",
        "store.scheme_specific.growth_potential=fact", "store.scheme_specific.safe=fact",
        "store.scheme_specific.shared_housing=fact", "store.scheme_specific.station_complex=fact",
        "collectFacts:growthPotentialSpatialFacts", "collectFacts:safeHousingSpatialFacts",
        "collectFacts:sharedHousingSpatialFacts", "collectFacts:stationComplexSpatialFacts",
    )
    for text in required:
        assert text in html, text

    # 팝업은 현황 Fact collector를 다시 실행하지 않는다. 분석된 동일 Fact만 읽는다.
    popup_start = html.index("function renderGrowthPotentialDetailPopup()")
    popup_end = html.index("function renderSchemeDetailPopup(name)", popup_start)
    popup = html[popup_start:popup_end]
    for forbidden in (
        "growthPotentialSpatialFacts(store)", "safeHousingSpatialFacts(store)",
        "sharedHousingSpatialFacts(store)", "stationComplexSpatialFacts(store)",
    ):
        assert forbidden not in popup, forbidden
    assert "팝업에서는 새 현황을 계산하지 않습니다" in popup

    # 안심주택: 2026.08.03 기준에서 지구단위계획구역은 역세권 하드게이트가 아니다.
    safe_start = html.index("function safeHousingSpatialFacts(store)")
    safe_end = html.index("function sharedHousingSpatialFacts(store)", safe_start)
    safe = html[safe_start:safe_end]
    assert "지구단위계획구역으로서 승강장" not in safe
    assert "station.status==='PASS' && district" not in safe
    assert "지구단위계획구역(참고)" in safe
    assert "승강장 경계 250m 이내" in safe
    assert "통합심의로 350m 검토" in safe
    assert "의료시설 중심지역 350m" in safe

    # 상생주택: 저이용·유휴와 민간제안을 분리한 현황 Fact를 OR 판정한다.
    shared_start = html.index("function sharedHousingSpatialFacts(store)")
    shared_end = html.index("function stationComplexSpatialFacts(store)", shared_start)
    shared = html[shared_start:shared_end]
    assert "shared_housing_proposal" in html
    assert "sharedProposal:schemeYN('shared_housing_proposal')" in html
    assert "private_proposal:c.sharedProposal" in shared
    assert "spatialFactRow('민간제안 의사'" in shared
    assert "f.target.low_use===true||f.target.private_proposal===true" in shared
    assert "f.target.low_use===false&&f.target.private_proposal===false" in shared
    assert "scalePass?'PASS':'REVIEW'" in shared

    # 역세권복합개발: 기준에 없는 350m 숫자를 만들지 않는다. 250m 원칙 + 위원회 적용완화 REVIEW.
    station_start = html.index("function stationComplexSpatialFacts(store)")
    station_end = html.index("function moduleStrengthRisk", station_start)
    station = html[station_start:station_end]
    assert "250~350m" not in station
    assert "distance_m<=350" not in station
    assert "승강장 경계 반경 250m 이내 원칙" in station
    assert "도시·건축공동위원회" in station
    assert "STATION_COMPLEX" in html and "verified:false" in html

    # 성장잠재권: 35m 간선도로·둘레 1/8·6m 접면과 시행방식별 노후도 Fact를 사용한다.
    growth_start = html.index("function growthPotentialSpatialFacts(store)")
    growth_end = html.index("function safeHousingSpatialFacts(store)", growth_start)
    growth = html[growth_start:growth_end]
    assert "35m" in growth and "12.5" in growth
    assert "road6_faces" in growth
    assert "schemeAgeFact(store,'growth_potential'" in growth
    assert "지구단위계획형" in growth and "도시정비형" in growth


def check_next_four_independent_modules() -> None:
    """장기전세·도심공공주택복합·도심복합혁신·도시정비형은 공간현황 Fact→독립엔진→팝업 구조여야 한다."""
    html = Path(app.STATIC_DIR, "app.html").read_text(encoding="utf-8")
    required = (
        "function longtermSpatialFacts(store)", "function checkLongtermFromFacts(store,f)",
        "function publicComplexSpatialFacts(store)", "function checkPublicComplexFromFacts(store,f)",
        "function innovationSpatialFacts(store)", "function checkInnovationFromFacts(store,f)",
        "function urbanRedevelopmentSpatialFacts(store)", "function checkUrbanRedevelopmentFromFacts(store,f)",
        "collectFacts:longtermSpatialFacts", "collectFacts:publicComplexSpatialFacts",
        "collectFacts:innovationSpatialFacts", "collectFacts:urbanRedevelopmentSpatialFacts",
        "store.scheme_specific.longterm=fact", "store.scheme_specific.public_complex=fact",
        "store.scheme_specific.innovation=fact", "store.scheme_specific.urban_redevelopment=fact",
        "function renderLongtermDetailPopup()", "function renderPublicComplexDetailPopup()",
        "function renderInnovationDetailPopup()", "function renderUrbanRedevelopmentDetailPopup()",
    )
    for text in required:
        assert text in html, text
    # 서울 도심공공주택복합: 350m / 노후도 20년 60%이어야 한다.
    assert "public_complex:chronologicalAgeAssessment(records,20,60" in html
    assert "승강장 경계 350m 이내" in html
    public_start=html.index("function publicComplexSpatialFacts(store)")
    public_end=html.index("function innovationSpatialFacts(store)", public_start)
    public=html[public_start:public_end]
    assert "coverage350" in public and "coverage500" not in public
    assert "정비구역·도시개발구역 중첩" in public
    assert "도시재생사업 인허가" in public
    # 장기전세: 350/500 전체포함, 교차지역 200m, 20m 도로 둘레 1/8.
    long_start=html.index("function longtermSpatialFacts(store)")
    long_end=html.index("function publicComplexSpatialFacts(store)", long_start)
    longterm=html[long_start:long_end]
    for text in ("coverage350", "coverage500", "arterialIntersectionDist<=200", "road20Perimeter>=12.5", "schemeAgeFact(store,'longterm',route)", "700% 특례 입지후보"):
        assert text in longterm, text
    density_start=html.index("function longtermDensity(c)")
    density_end=html.index("function publicComplexDensity(c)", density_start)
    density=html[density_start:density_end]
    assert "Number(c.dist)<=250" in density
    assert "regionalOrHigher&&interchange&&Number(c.dist)<=350" in density
    assert "상한 500%${specialNote}" in density
    assert "법적상한 500%${specialNote}" in density
    assert "const first=c.dist!=null && c.dist<=350" not in density
    assert "2100000282274" in html
    # 도심복합개발: 2026 서울 조례·규칙, 유형별 Fact와 동의요건.
    innov_start=html.index("function innovationSpatialFacts(store)")
    innov_end=html.index("function urbanRedevelopmentSpatialFacts(store)", innov_start)
    innov=html[innov_start:innov_end]
    assert "coverage350_pct" in innov and "coverage500_pct" in innov
    assert "owner_pct>=66.6667" in innov and "land_pct>=50" in innov
    assert "INNOVATION_RULE" in innov and "INNOVATION_ORD" in innov
    # 도시정비형: 정책사업 의제는 독립 추천에 중복집계하지 않는다.
    urban_start=html.index("function urbanRedevelopmentSpatialFacts(store)")
    urban_end=html.index("const SCHEME_MODULES=", urban_start)
    urban=html[urban_start:urban_end]
    assert "정비가능구역 포함 여부" in urban
    assert "정비가능구역 용도지역" in urban
    assert "용도지역은 정비가능구역의 대체 진입경로가 아니라 충족조건" in urban
    assert "정비가능구역 공식 경계 GIS 미연결" in urban
    assert "정비구역·정비예정구역·정비가능구역 중 하나에 해당" in urban
    assert "정책사업 하위 시행방식" in urban
    assert "독립 추천 판단" in urban
    assert "시행주체별 추진방식" in urban
    assert "possibleConditionsPass" in urban
    assert "center_candidate&&f.possible.zoning_ok" not in urban
    assert "역세권활성화사업·역세권 장기전세주택 등 의제 추진사업" in html
    assert "evaluationOrder=Object.keys(schemeNames).filter(name=>name!=='urban_redevelopment').concat('urban_redevelopment')" in html
    assert "policyRedevelopmentPass" in urban
    assert "independentRow?.status==='FAIL'" in html
    # Candidate 계층은 독립모듈 결과만 사용하고 시행자·법정진입을 중복 판정하지 않는다.
    assert "공간현황 Fact → 독립 사업모듈의 단일 판정결과 사용" in html
    assert "function candidateExecutionFit(" not in html
    assert "function candidateStructuralGate(" not in html
    # 팝업은 collector를 직접 다시 호출하지 않는다.
    popup_start=html.index("function renderLongtermDetailPopup()")
    popup_end=html.index("function renderSchemeDetailPopup(name)", popup_start)
    pop=html[popup_start:popup_end]
    for forbidden in ("longtermSpatialFacts(store)", "publicComplexSpatialFacts(store)", "innovationSpatialFacts(store)", "urbanRedevelopmentSpatialFacts(store)"):
        assert forbidden not in pop


def check_dedicated_detail_popups() -> None:
    html=(Path(__file__).resolve().parent / "app.html").read_text(encoding="utf-8")
    funcs=[
        "renderGrowthPotentialDetailPopup","renderSafeHousingDetailPopup","renderSharedHousingDetailPopup",
        "renderStationComplexDetailPopup","renderLongtermDetailPopup","renderPublicComplexDetailPopup",
        "renderInnovationDetailPopup","renderUrbanRedevelopmentDetailPopup",
    ]
    for name in funcs:
        start=html.index(f"function {name}()")
        end=html.find("\nfunction ", start+20)
        block=html[start:end if end >= 0 else None]
        assert "1. 현황" in block and "2. 검토결과" in block and "4. 추진일정" in block, name
        assert "schemeSpecificResultRows" in block, name
        assert "schemeSheetResultRows" not in block, name
        assert "판정구조" in block or "중복추천 배제" in block, name



def check_startup_drawing_and_legacy_ui() -> None:
    html=(Path(__file__).resolve().parent / "app.html").read_text(encoding="utf-8")
    # drawing control + three lifecycle handlers must exist and startup rendering must not
    # execute between control construction and CREATED registration. That interval is where
    # a TDZ/initialization error previously killed all drawing events.
    draw=html.index("const drawControl=")
    created=html.index("map.on(L.Draw.Event.CREATED", draw)
    edited=html.index("map.on(L.Draw.Event.EDITED", created)
    deleted=html.index("map.on(L.Draw.Event.DELETED", edited)
    assert html.find("refreshCompactMiniMaps();", draw, created) == -1
    assert draw < created < edited < deleted
    assert "new L.Draw.Polygon(map" in html and "drawer.enable();" in html
    assert "function ccLabelOf(id)" in html
    for marker in ("async function measureAndSync()", "async function lookupBoundaryAddresses()", "async function applyAddressPreviewAsBoundary()", "async function analyzeParcels()", "async function analyzeBuildings()", "async function analyzeBuildingHub()", "async function analyzeRoadAccess()"):
        assert marker in html, marker
    created_block=html[created:edited]
    assert "drawnItems.addLayer(e.layer)" in created_block
    assert "activeGeometry=e.layer.toGeoJSON().geometry" in created_block
    assert "await measureAndSync()" in created_block
    # Initial evidence render is deferred until after all declarations and service layout setup.
    layout=html.index("buildServiceLayout();")
    final_refresh=html.find("refreshCompactMiniMaps();", layout)
    assert final_refresh > layout
    assert final_refresh > html.index("let planningFacilityConstraintCache=")
    # Old manual comparison sheet may remain as hidden engine-state DOM, but never user-visible.
    assert "역세권·도심복합 사업방식 비교 검토시트" not in html
    assert '<section class="panel scheme-panel legacy-engine-state" aria-hidden="true" style="display:none!important">' in html
    assert ".legacy-engine-state{display:none!important}" in html


def check_spatial_evidence_maps() -> None:
    html=(Path(__file__).resolve().parent / "app.html").read_text(encoding="utf-8")
    # 공간현황은 판정 근거데이터 화면이다. 필요한 주제도와 공통 지적 베이스가 존재해야 한다.
    for marker in (
        'id="ccZoningMiniMap"', 'id="ccSchemeRoadMiniMap"', 'id="ccSafeMedicalMiniMap"',
        'function compactParcelBaseFeatures()', 'function refreshCommonParcelBases()',
        'function renderZoningSpatialStatus()', 'function renderSchemeRoadEvidence()',
        'function renderSafeMedicalSpatialStatus()',
    ):
        assert marker in html, marker
    for layer in (
        'ccBuildingParcelBase','ccStationParcelBase','ccCenterParcelBase','ccRenewalParcelBase',
        'ccDevelopmentParcelBase','ccPlanningParcelBase','ccZoningParcelBase',
        'ccSchemeRoadParcelBase','ccSchemeRoadInfluence','ccSafeMedicalParcelBase',
    ):
        assert layer in html, layer
    # Fact Store가 도면과 사업엔진의 단일 근거가 된다. 공통 도로는 판정값이 아니라 raw fact로 보존한다.
    assert 'road_raw:roadRawFacts(c)' in html
    assert 'spatial_evidence:{zoning:zoningSpatialEvidenceFacts(),roads:schemeRoadEvidenceFacts(c),frontage:schemeFrontageEvidenceFacts(c),safe_medical:safeMedicalSpatialEvidenceFacts()}' in html
    assert 'function roadRawFacts(cArg=null)' in html
    assert 'has_20m_width_candidate' in html
    assert 'has_20m:c.has20' not in html
    assert 'arterialRoad' not in html and 'arterial_road' not in html
    assert 'store.site.spatial_evidence?.roads?.safe' in html
    assert 'store.site.spatial_evidence?.safe_medical' in html
    # 도로기준은 제도별로 분리한다. 하나의 generic arterial PASS를 쓰면 안 된다.
    for key in ("key:'activation'", "key:'safe'", "key:'growth'", "key:'longterm'", "key:'station_complex'", "key:'innovation'", "key:'public_complex'"):
        assert key in html, key
    for fact_key in ('activationRoadFact','safeHousingRoadFact','growthPotential35mRoadFact','longtermArterialIntersectionFact','stationComplexRoadFact','innovationDistrictRoadFact','publicComplexRoadFact'):
        assert fact_key in html, fact_key
    assert "mode:'width6_frontage'" in html
    assert "threshold:20" in html and "threshold:35" in html
    assert "mode:'road4_8'" in html
    assert '도로기능(특별시도·주/보조간선 등) 공식 속성 미연결' in html
    assert "if(selected==='safe')" in html and "turf.buffer(f,50,{units:'meters'})" in html

    # 접도율/접면기준은 제도별 Fact로 분리하며 공통 frontage Boolean/ratio로 대체하지 않는다.
    assert 'function schemeFrontageEvidenceFacts(cArg=null)' in html
    for fact_key in ('redevelopmentFrontage6Fact','activationFrontageFact','safeHousingFrontageFact','growthPotentialFrontage35Fact','longtermFrontage20Fact','stationComplexFrontageFact','innovationFrontageFact','publicComplexFrontageFact'):
        assert fact_key in html, fact_key
    for dom_id in ('spRoadFrontageLabel','spRoadFrontageValue','spRoadFrontageBasis','spRoadFrontageContact','spRoadFrontageStatus'):
        assert f'id="{dom_id}"' in html, dom_id
    assert '주택재개발 shell 판정엔진과는 분리된 공간현황 Fact' in html
    assert "key:'redevelopment'" in html
    # 용도지역 지도는 용도지역별 실제 색상과 동일 색 범례를 제공한다.
    assert 'function zoningColorSpec(name)' in html
    assert 'zoning-legend-chip' in html

    # A width candidate alone must never make the long-term arterial path PASS.
    lt_start=html.index('function longtermSpatialFacts(store)')
    lt_end=html.index('function checkLongtermFromFacts', lt_start)
    lt=html[lt_start:lt_end]
    assert "arterialFunctionConfirmed=roadEvidence?.function_confirmed===true" in lt
    assert "arterialFunctionConfirmed?'PASS':'REVIEW'" in lt
    assert "c.roadQuality==='AUTO'?'PASS'" not in lt
    # 안심주택 의료시설은 위치점 자체가 아니라 복원된 시설부지 경계 Fact만 PASS를 만들 수 있다.
    assert 'boundary_status:x.boundary_status' in html
    assert 'facility_boundary_geometry:x.facility_boundary_geometry' in html
    assert 'candidates=confirmed.filter' in html
    assert "x.auto_pass_eligible&&x.within_350===true" in html
    assert 'ccSafeMedicalFacilitySites' in html
    assert '부지경계 거리' in html and '350m 확정 후보' in html
    medical_start=html.index('function safeMedicalPath(')
    medical_end=html.find('\nfunction ', medical_start+20)
    medical=html[medical_start:medical_end if medical_end>=0 else None]
    assert "if(refs.length)" in medical and "status='PASS'" in medical
    assert 'reference_point_candidates_350' in medical
    assert "위치점" in medical and "REVIEW" in medical
    assert "spatialFactRow('의료시설 중심지역 350m',medical.value,medical.status,medical.note)" in html
    # 용도지역은 공간현황에서 별도 지도와 구성비를 확인한다.
    assert 'id="spZoningPrimary"' in html and 'id="spZoningPrimaryRatio"' in html
    assert "LT_C_UQ111" in html
    assert "area_m2:Number(r.area||0)" in html and "pct:r.pct==null?null:Number(r.pct)" in html
    assert "toLocaleString('ko-KR',{maximumFractionDigits:0})}㎡" in html
    # Every major spatial-status map must receive the common cadastral base.
    base_start=html.index('function refreshCommonParcelBases()')
    base_end=html.find('\nfunction ', base_start+20)
    base_block=html[base_start:base_end if base_end >= 0 else None]
    for layer in ('ccBuildingParcelBase','ccStationParcelBase','ccCenterParcelBase','ccRenewalParcelBase','ccDevelopmentParcelBase','ccPlanningParcelBase','ccZoningParcelBase','ccSchemeRoadParcelBase','ccSafeMedicalParcelBase'):
        assert layer in base_block, layer


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
    # 기준본에 포함된 공식 근거 PDF 8종이 배포 ZIP에서 누락되지 않도록 고정한다.
    assert len(list(root.glob("*.pdf"))) >= 8
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





def check_safe_medical_api_adapter() -> None:
    # Render에서 실제 사용 중인 환경변수명(data.seoul.go.kr_KEY)도 인식해야 한다.
    with patch.dict(os.environ, {"SEOUL_OPEN_DATA_KEY": "", "data.seoul.go.kr_KEY": "render-test-key"}, clear=False):
        key, env_name = app._seoul_open_data_key_info()
        assert key == "render-test-key"
        assert env_name == "data.seoul.go.kr_KEY"

        # 인증 오류가 top-level RESULT로 와도 '0건'으로 삼키지 않고 명시 오류 처리한다.
        class FakeResponse:
            def raise_for_status(self):
                return None
            def json(self):
                return {"RESULT": {"CODE": "INFO-100", "MESSAGE": "인증키가 유효하지 않습니다."}}

        with patch.object(app.requests, "get", return_value=FakeResponse()):
            try:
                app._seoul_open_data_rows("LOCALDATA_010101", 5)
                raise AssertionError("top-level OpenAPI error was swallowed")
            except RuntimeError as exc:
                assert "INFO-100" in str(exc)

        to_metric = app.Transformer.from_crs(4326, 5174, always_xy=True)
        x, y = to_metric.transform(127.0000, 37.5000)
        site = {
            "type": "Polygon",
            "coordinates": [[[126.998, 37.498], [127.002, 37.498], [127.002, 37.502], [126.998, 37.502], [126.998, 37.498]]],
        }

        def fake_rows(service, limit=10000):
            if service == "LOCALDATA_010101":
                return [
                    {"TRDSTATENM": "영업/정상", "UPTAENM": "종합병원", "BPLCNM": "테스트종합병원", "X": x, "Y": y, "RDNWHLADDR": "서울 테스트로 1"},
                    {"TRDSTATENM": "영업/정상", "UPTAENM": "병원", "BPLCNM": "서울의료원", "X": x + 30, "Y": y + 30, "RDNWHLADDR": "서울 테스트로 2"},
                ]
            if service == "LOCALDATA_010102":
                return [
                    {"TRDSTATENM": "영업/정상", "UPTAENM": "보건소", "BPLCNM": "테스트구보건소", "X": x + 60, "Y": y + 60, "RDNWHLADDR": "서울 테스트로 3"},
                    {"TRDSTATENM": "영업/정상", "UPTAENM": "보건지소", "BPLCNM": "테스트보건지소", "X": x + 80, "Y": y + 80},
                ]
            if service == "tbEntranceItem":
                raise AssertionError("current health-center API succeeded, stale fallback must not run")
            return []

        def fake_boundary(item, site_wgs):
            return {
                "boundary_status": "CONFIRMED",
                "boundary_basis": "CADASTRAL_PARCEL_FROM_OFFICIAL_POINT" if item.get("category") == "public_health_center" else "URBAN_PLANNING_MEDICAL_FACILITY",
                "boundary_basis_label": "테스트 시설부지 경계",
                "facility_boundary_geometry": site,
                "buffer_350_geometry": site,
                "distance_boundary_m": 0.0,
                "within_350": True,
                "auto_pass_eligible": True,
            }
        with patch.object(app, "_seoul_open_data_rows", side_effect=fake_rows), patch.object(app, "_resolve_medical_facility_boundary", side_effect=fake_boundary):
            result = app._safe_medical_reference(site)
        cats = {row["category"] for row in result["items"]}
        assert {"general_hospital", "municipal_hospital", "public_health_center"}.issubset(cats)
        assert result["metadata"]["credential_env"] == "data.seoul.go.kr_KEY"
        assert result["auto_pass_eligible"] is True
        assert result["nearby_counts"]["boundary_confirmed_350"] >= 1
        assert result["nearby_counts"]["public_health_center"] == 1
        assert result["source_stats"]["health_center"]["service"] == "LOCALDATA_010102"

    html=(Path(__file__).resolve().parent / "app.html").read_text(encoding="utf-8")
    assert 'id="spSafeMedicalApiInfo"' in html
    assert "data.nearby_counts||{}" in html
    assert "data.source_stats||{}" in html
    assert "API 확인필요" in html

def check_safe_medical_boundary_resolution() -> None:
    site = {
        "type": "Polygon",
        "coordinates": [[[126.999, 37.499], [127.001, 37.499], [127.001, 37.501], [126.999, 37.501], [126.999, 37.499]]],
    }
    site_shape = shape(site)
    facility_geom = {
        "type": "Polygon",
        "coordinates": [[[126.9995, 37.4995], [127.0002, 37.4995], [127.0002, 37.5002], [126.9995, 37.5002], [126.9995, 37.4995]]],
    }
    primary = {"type": "Feature", "geometry": facility_geom, "properties": {"pnu": "1111010100100010001"}}

    hospital = {"category": "general_hospital", "name": "테스트종합병원", "geometry": {"type": "Point", "coordinates": [127.0, 37.5]}}
    with patch.object(app, "_medical_planning_facility_boundary", return_value={"status": "resolved", "geometry": facility_geom, "features": []}):
        result = app._resolve_medical_facility_boundary(hospital, site_shape)
    assert result["boundary_status"] == "CONFIRMED"
    assert result["boundary_basis"] == "URBAN_PLANNING_MEDICAL_FACILITY"
    assert result["within_350"] is True
    assert result["auto_pass_eligible"] is True

    health = {"category": "public_health_center", "name": "테스트보건소", "geometry": {"type": "Point", "coordinates": [127.0, 37.5]}}
    with patch.object(app, "_vworld_parcel_at_point", return_value={"status": "resolved", "feature": primary, "pnu": primary["properties"]["pnu"]}):
        result = app._resolve_medical_facility_boundary(health, site_shape)
    assert result["boundary_status"] == "CONFIRMED"
    assert result["boundary_basis"] == "CADASTRAL_PARCEL_FROM_OFFICIAL_POINT"
    assert result["primary_pnu"] == primary["properties"]["pnu"]

    stale_health = {**health, "data_status": "stale_reference_point_2023"}
    with patch.object(app, "_vworld_parcel_at_point", return_value={"status": "resolved", "feature": primary, "pnu": primary["properties"]["pnu"]}):
        stale_result = app._resolve_medical_facility_boundary(stale_health, site_shape)
    assert stale_result["boundary_status"] == "REVIEW"
    assert stale_result["auto_pass_eligible"] is False
    assert stale_result["boundary_basis"] == "CADASTRAL_PARCEL_FROM_STALE_REFERENCE_POINT"

    with patch.object(app, "_medical_planning_facility_boundary", return_value={"status": "none", "geometry": None, "features": []}), \
         patch.object(app, "_vworld_parcel_at_point", return_value={"status": "resolved", "feature": primary, "pnu": primary["properties"]["pnu"]}), \
         patch.object(app, "_medical_building_site_boundary", return_value={"status": "resolved", "geometry": facility_geom, "related_pnus": [primary["properties"]["pnu"], "1111010100100010002"], "parcel_count": 2, "reason": "test"}):
        result = app._resolve_medical_facility_boundary(hospital, site_shape)
    assert result["boundary_status"] == "CONFIRMED"
    assert result["boundary_basis"] == "BUILDING_REGISTER_SITE_PARCELS"
    assert result["parcel_count"] == 2

    row = {
        "atchSigunguCd": "11110", "atchBjdongCd": "10100", "atchPlatGbCd": "0",
        "atchBun": "12", "atchJi": "3",
    }
    related_pnu = app._building_hub_attachment_pnu(row)
    assert related_pnu == "1111010100100120003"

    # 비도시계획시설 병원이 다수 필지에 걸친 경우 건축HUB 부속지번과
    # 연속지적을 실제로 Union해 1개 시설부지 경계를 만드는지 확인한다.
    primary_pnu = primary["properties"]["pnu"]
    related_feature = {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [[[127.0002, 37.4995], [127.0008, 37.4995], [127.0008, 37.5002], [127.0002, 37.5002], [127.0002, 37.4995]]]},
        "properties": {"pnu": related_pnu},
    }
    title_row = {
        "mgmBldrgstPk": "TEST-HOSPITAL-1", "mainPurpsCdNm": "의료시설",
        "bldNm": "테스트종합병원", "platArea": "12000", "bylotCnt": "1",
        "sigunguCd": "11110", "bjdongCd": "10100", "platGbCd": "0", "bun": "1", "ji": "1",
    }
    attachment_row = {**row, "mgmBldrgstPk": "TEST-HOSPITAL-1"}
    with patch.object(app, "building_hub_ready", return_value=True), \
         patch.object(app, "_query_building_hub_recap_title", return_value=[]), \
         patch.object(app, "_query_building_hub_title", return_value=[title_row]), \
         patch.object(app, "_query_building_hub_atch_jibun", return_value=[attachment_row]), \
         patch.object(app, "_fetch_vworld_parcels_for_pnus", return_value={primary_pnu: primary, related_pnu: related_feature}):
        multi = app._medical_building_site_boundary(primary_pnu, primary, "테스트종합병원")
    assert multi["status"] == "resolved"
    assert multi["parcel_count"] == 2
    assert set(multi["related_pnus"]) == {primary_pnu, related_pnu}
    assert shape(multi["geometry"]).area > shape(primary["geometry"]).area

    address_primary = {"type": "Feature", "geometry": facility_geom, "properties": {"pnu": "1111010100100990001"}}
    hospital_with_addr = {**hospital, "parcel_address": "서울특별시 종로구 테스트동 99-1"}
    with patch.object(app, "_medical_planning_facility_boundary", return_value={"status": "none", "geometry": None, "features": []}), \
         patch.object(app, "_vworld_parcel_at_point", return_value={"status": "not_found", "feature": None, "pnu": None}), \
         patch.object(app, "_vworld_parcel_by_address", return_value={"status": "resolved", "feature": address_primary, "pnu": address_primary["properties"]["pnu"]}), \
         patch.object(app, "_medical_building_site_boundary", return_value={"status": "resolved", "geometry": facility_geom, "related_pnus": [address_primary["properties"]["pnu"]], "parcel_count": 1, "title": {"ledger_kind": "recap"}}):
        addr_result = app._resolve_medical_facility_boundary(hospital_with_addr, site_shape)
    assert addr_result["boundary_status"] == "CONFIRMED"
    assert addr_result["boundary_basis"] == "BUILDING_REGISTER_SITE_PARCELS"
    assert addr_result["parcel_candidate_basis"] == "official_license_address"

    recap_row = {**title_row, "regstrKindCdNm": "총괄표제부"}
    with patch.object(app, "building_hub_ready", return_value=True), \
         patch.object(app, "_query_building_hub_recap_title", return_value=[recap_row]), \
         patch.object(app, "_query_building_hub_title") as title_mock, \
         patch.object(app, "_query_building_hub_atch_jibun", return_value=[attachment_row]), \
         patch.object(app, "_fetch_vworld_parcels_for_pnus", return_value={primary_pnu: primary, related_pnu: related_feature}):
        recap_multi = app._medical_building_site_boundary(primary_pnu, primary, "테스트종합병원")
    assert recap_multi["status"] == "resolved"
    assert recap_multi["title"]["ledger_kind"] == "recap"
    title_mock.assert_not_called()

    html=(Path(__file__).resolve().parent / "app.html").read_text(encoding="utf-8")
    for marker in (
        "URBAN_PLANNING_MEDICAL_FACILITY", "BUILDING_REGISTER_SITE_PARCELS",
        "CADASTRAL_PARCEL_FROM_OFFICIAL_POINT", "CADASTRAL_PARCEL_FROM_STALE_REFERENCE_POINT",
        "official_license_address", "getBrRecapTitleInfo", "ccSafeMedicalFacilitySites",
        "buffer_350_geometry", "distance_boundary_m",
    ):
        assert marker in html or marker in (Path(__file__).resolve().parent / "app.py").read_text(encoding="utf-8"), marker


def check_purpose_filter_and_frontage_facts() -> None:
    html=(Path(__file__).resolve().parent / "app.html").read_text(encoding="utf-8")
    # 안심주택은 단순 주거가 아니라 주거(임대) 목적에서만 실제 Rule Module을 실행한다.
    assert '<option value="housing">주거(일반)</option>' in html
    assert '<option value="housing_rental">주거(임대)</option>' in html
    assert 'onchange="runAllSchemeChecks()"' in html
    assert "if(name==='safe' && purpose!=='housing_rental')return {enabled:false" in html
    assert "const engineGate=purposeEngineGate(name);" in html
    assert "engineGate.enabled?evaluateSchemeModule(name,store):purposeDisabledSchemeResult(name,engineGate.reason)" in html
    assert "if(!engineGate.enabled)delete store.scheme_specific[name]" in html
    assert "if(name==='safe' && purpose!=='housing_rental')return {state:'off'" in html
    assert "const HOUSING_PURPOSE_VALUES=new Set(['housing','housing_rental'])" in html
    # 주거(임대)는 다른 주거계열 분류에는 주거로 취급하되 안심주택만 별도 hard gate를 가진다.
    assert "if(purpose==='housing'||purpose==='housing_rental')return 'housing';" in html
    # shell-only 주택재개발은 접도 Fact가 생겨도 판정엔진으로 부활하면 안 된다.
    assert "const SHELL_SCHEMES=new Set(['redevelopment','reconstruction','residential_environment','smallscale','general_housing'])" in html
    assert "fact_key:'redevelopmentFrontage6Fact'" in html
    assert "trySpatialLayerCandidates(['TL_SPRD_MANAGE','LT_C_SPRD_MANAGE']" in html
    assert "trySpatialLayerCandidates(['TL_SPRD_RW'" not in html
    assert "사업진입조건 확인을 위한 개략적 추정치로, 현장조서 및 도면검토를 통해 보완될 수 있음" in html
    assert "analysisState.quality.road='ESTIMATE'" in html
    assert 'function checkRedevelopment(' not in html

def main() -> None:
    _run("measurement", check_measurement)
    _run("renewal spatial", check_renewal_server_intersection)
    _release_heavy_spatial_cache("renewal")
    _run("development spatial", check_development_server_intersection)
    _release_heavy_spatial_cache("development")
    _run("redevelopment area gate", check_area_gate)
    _run("redevelopment boolean gate", check_redevelopment_boolean_gate)
    _run("centerline width buffer", check_centerline_width_buffer)
    _run("age annotation reference", check_age_annotation_reference_only)
    _run("feedback + UI", check_feedback_and_ui)
    _run("four independent modules", check_four_independent_scheme_modules)
    _run("next four independent modules", check_next_four_independent_modules)
    _run("legacy engine purge", test_migrated_scheme_legacy_engines_removed)
    _run("dedicated detail popups", check_dedicated_detail_popups)
    _run("startup drawing + legacy UI", check_startup_drawing_and_legacy_ui)
    _run("spatial evidence maps", check_spatial_evidence_maps)
    _run("safe medical api adapter", check_safe_medical_api_adapter)
    _run("safe medical boundary resolution", check_safe_medical_boundary_resolution)
    _run("purpose filter + frontage facts", check_purpose_filter_and_frontage_facts)
    _run("release files", check_release_files)
    print("v2.5.0 regression checks: PASS")



def test_migrated_scheme_legacy_engines_removed():
    html = Path(app.BASE_DIR, "app.html").read_text(encoding="utf-8")
    # 모든 구형 사업 판정엔진은 삭제한다. 9개는 독립모듈, 5개는 shell-only다.
    deprecated_functions = [
        "checkActivation", "checkSafe", "checkStationComplex", "checkLongterm", "checkPublicComplex",
        "checkInnovation", "checkGrowthPotential", "checkSharedHousing", "checkUrbanRedevelopment",
        "checkRedevelopment", "checkReconstruction", "checkResidentialEnvironment", "checkSmallScale", "checkGeneralHousing",
        "calculateScheme", "redevelopmentEntryGate", "candidateExecutionFit", "candidateStructuralGate", "reconstructionEvidence",
        "activationUrbanDeeming", "longtermUrbanDeeming", "urbanRedevelopmentAccess",
        "activationDistrictAgingAssessment", "activationRedevelopmentAgingAssessment",
    ]
    for name in deprecated_functions:
        assert f"function {name}(" not in html, f"deprecated engine/helper {name} remains"
    assert "legacy-adapter" not in html
    assert "const SHELL_SCHEMES=new Set(['redevelopment','reconstruction','residential_environment','smallscale','general_housing'])" in html
    assert "if(SHELL_SCHEMES.has(name))" in html
    assert "state:'neutral',label:'재설계 예정',rank:0,stage:'SHELL'" in html
    assert "기존 판정엔진 삭제 완료 · 현재 추천/우선순위 미반영" in html
    assert "독립모듈 9개만 실제 판정한다. 미전환 5개는 shell 결과만 반환" in html
    for key in ["activation", "growth_potential", "safe", "shared_housing", "station_complex", "longterm", "public_complex", "innovation", "urban_redevelopment"]:
        assert f"{key}:{{" in html or f"{key}: {{" in html, f"independent module {key} missing"

if __name__ == "__main__":
    main()
