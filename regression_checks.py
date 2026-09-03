from zoneinfo import ZoneInfo
from datetime import datetime
"""배포 전 핵심 회귀검사. 실행: python regression_checks.py"""

import os
import gc
import time
import hashlib
import json
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

import shapefile
from shapely.geometry import LineString, box, shape, mapping

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
    """주택재개발 면적 하드게이트는 단일 frontend Fact module에만 존재해야 한다."""
    html = Path(app.STATIC_DIR, "app.html").read_text(encoding="utf-8")
    block = html[html.index("function redevelopmentSpatialFacts(store)"):html.index("function reconstructionSpatialFacts(store)")]
    assert "area>=10000" in block
    assert "area>=5000" in block
    assert "areaStatus='PASS';areaConditional=!c.areaExceptionApproved" in block
    assert "위원회 인정 등 예외경로를 사업가능 경로로 반영" in block
    assert "else{areaStatus='FAIL'" in block
    # 이전 Python 중복 판정엔진은 제거되어야 한다.
    py = Path(app.BASE_DIR, "app.py").read_text(encoding="utf-8")
    assert "def evaluate_redevelopment(" not in py
    assert '/api/redevelopment/evaluate' not in py


def check_redevelopment_boolean_gate() -> None:
    """면적 AND 노후도 AND 추가요건(OR) 구조가 frontend 독립모듈에 고정되어야 한다."""
    html = Path(app.STATIC_DIR, "app.html").read_text(encoding="utf-8")
    block = html[html.index("function redevelopmentSpatialFacts(store)"):html.index("function reconstructionSpatialFacts(store)")]
    assert "smallStatus==='PASS'||densityStatus==='PASS'" in block
    assert "frontageThreshold" in block and "extraStatus='REVIEW'" in block
    assert "[smallStatus,densityStatus,frontageStatus].every(x=>x==='FAIL')" in block
    assert "노후·불량건축물 수 60% 이상" in block
    assert "과소필지 40% 이상 또는 주택접도율 40% 이하 또는 호수밀도 60호/ha 이상" in block


def check_centerline_width_buffer() -> None:
    """도로폭은 TL_SPRD_MANAGE ROAD_BT만 사용하고 프론트에서 중심선 버퍼를 만든다."""
    assert app._road_width_m({"road_bt": "8.0m"}) == 8.0
    html = Path(app.STATIC_DIR, "app.html").read_text(encoding="utf-8")
    assert "function roadPolygonsFromCenterlines(manageFeatures)" in html
    assert "turf.buffer(mf,w/2" in html
    assert "_width_source:'도로중심선 ROAD_BT 기반 개략범위'" in html


def check_bundled_road_dataset() -> None:
    """대용량 도로 폴리곤 번들은 제거되고 TL_SPRD_MANAGE 실시간 조회만 남아야 한다."""
    root = Path(app.BASE_DIR)
    html = (root / "app.html").read_text(encoding="utf-8")
    py = (root / "app.py").read_text(encoding="utf-8")
    assert not (root / "road_seoul.zip").exists()
    assert "/api/spatial/roads" not in html and "/api/spatial/roads" not in py
    assert "/api/spatial/road-data-status" not in html and "/api/spatial/road-data-status" not in py
    fetch = html[html.index("async function fetchRoadNetwork("):html.index("function roadDiagnosticModeLabel", html.index("async function fetchRoadNetwork("))]
    assert "trySpatialLayerCandidates(['TL_SPRD_MANAGE','LT_C_SPRD_MANAGE']" in fetch
    assert "road_polygons:derived" in fetch
    assert "ROAD_BT가 없거나 유효하지 않은 구간은 폭원을 추정하지 않고 REVIEW" in fetch


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
        "사업방식 적용판정",
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
    assert "function candidateDisplayState(name,st=safeCandidateState(name))" in html
    assert "function candidateChangeOpportunity(name,st)" in html
    assert "r.hardGate==='AREA'" in html
    assert "공개 GIS·입력자료 자동검토에서 충족 사실이 확인되지 않아 미충족 처리" in html
    assert "function baseReviewDisclaimerHtml(name)" in html
    assert "결정·고시된 도시관리계획" in html
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
    # buildSiteFactStore()가 buildingRawFacts()를 한 번 호출해 buildingRecords에 담고
    # site.building.records / site.factory_usage 양쪽이 그 결과를 공유 재사용한다
    # (예전엔 records:buildingRawFacts()를 인라인으로 두 번 부르던 자리였음 — 중복 호출 제거).
    store0 = html.index("function buildSiteFactStore()")
    store1 = html.index("function activationSpatialFacts(store)", store0)
    store_block = html[store0:store1]
    assert "const buildingRecords=buildingRawFacts();" in store_block
    assert "records:buildingRecords" in store_block
    assert "factory_usage:computeFactoryUsageFact(buildingRecords" in store_block
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
    assert "SCHEME_MODULE_API_VERSION='2026-09-02-r22-station-area-frontage-no-hierarchy'" in html
    assert "const SHELL_SCHEMES=new Set(['urban_innovation_zone','facility_complex_zone','mixed_use_zone'])" in html
    assert "현재 자동 활성화·추천·우선순위 미반영" in html
    assert "collectFacts:activationSpatialFacts" in html
    assert "function checkActivationFromFacts(store,f)" in html
    assert "역세권활성화사업 기초검토서" in html
    assert "선순위 사업 미리보기" in html
    assert "위치기반 매스 이미지" in html
    assert "innovation_growth:{id:'innovation_growth'" in html
    assert "innovation_housing:{id:'innovation_housing'" in html


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
    assert "승강장 250m 이내에 가로구역의 1/2 이상" in station
    assert "위원회 인정경로" in station
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
        "function innovationSpatialFacts(store,typ)", "function checkInnovationFromFacts(store,f)",
        "function urbanRedevelopmentSpatialFacts(store)", "function checkUrbanRedevelopmentFromFacts(store,f)",
        "collectFacts:longtermSpatialFacts", "collectFacts:publicComplexSpatialFacts",
        "collectFacts(store){return innovationSpatialFacts(store,'growth');}", "collectFacts(store){return innovationSpatialFacts(store,'housing');}", "collectFacts:urbanRedevelopmentSpatialFacts",
        "store.scheme_specific.longterm=fact", "store.scheme_specific.public_complex=fact",
        "store.scheme_specific[schemeKey]=fact", "store.scheme_specific.urban_redevelopment=fact",
        "function renderLongtermDetailPopup()", "function renderPublicComplexDetailPopup()",
        "function renderInnovationDetailPopup(name)", "function renderUrbanRedevelopmentDetailPopup()",
    )
    for text in required:
        assert text in html, text
    # 서울 도심공공주택복합: 350m / 노후도 20년 60%이어야 한다.
    assert "public_complex:chronologicalAgeAssessment(records,20,60" in html
    assert "승강장 경계 350m 이내" in html
    public_start=html.index("function publicComplexSpatialFacts(store)")
    public_end=html.index("function innovationSpatialFacts(store,typ)", public_start)
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
    innov_start=html.index("function innovationSpatialFacts(store,typ)")
    innov_end=html.index("function urbanRedevelopmentSpatialFacts(store)", innov_start)
    innov=html[innov_start:innov_end]
    assert "coverage350_pct" in innov and "coverage500_pct" in innov
    assert "owner_pct>=66.6667" in innov and "land_pct>=50" in innov
    assert "INNOVATION_RULE" in innov and "INNOVATION_ORD" in innov
    assert "innovationFactoryBuildingRatio" in html and "공장용도 건축물 수 ÷ 전체 건축물 수" in innov
    assert "innovationApartmentPrecheck" in html and "제4조제3호" in innov and "제5조제1항제4호" in innov
    assert "rows.push(schemeRow('접도'" in innov and 'f.road?.evidence' in innov
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
    assert "const order=names||Object.keys(schemeNames).filter(name=>name!=='urban_redevelopment').concat('urban_redevelopment')" in html
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
    for forbidden in ("longtermSpatialFacts(store)", "publicComplexSpatialFacts(store)", "innovationSpatialFacts(store,typ)", "urbanRedevelopmentSpatialFacts(store)"):
        assert forbidden not in pop


def check_dedicated_detail_popups() -> None:
    html=(Path(__file__).resolve().parent / "app.html").read_text(encoding="utf-8")
    funcs=[
        "renderGrowthPotentialDetailPopup","renderSafeHousingDetailPopup","renderSharedHousingDetailPopup",
        "renderStationComplexDetailPopup","renderLongtermDetailPopup","renderPublicComplexDetailPopup",
        "renderInnovationDetailPopup","renderUrbanRedevelopmentDetailPopup",
    ]
    for name in funcs:
        sig=f"function {name}(name)" if name=="renderInnovationDetailPopup" else f"function {name}()"
        start=html.index(sig)
        end=html.find("\nfunction ", start+20)
        block=html[start:end if end >= 0 else None]
        if name=="renderInnovationDetailPopup":
            assert "1. 자동분석 현황" in block and "2. 사업추진조건 판정" in block, name
            assert "핵심 추진조건" in block and "판정범위" in block, name
        else:
            assert "1. 현황" in block and "2. 검토결과" in block and "4. 추진일정" in block, name
            assert "판정구조" in block or "중복추천 배제" in block, name
        assert "schemeSpecificResultRows" in block, name
        assert "schemeSheetResultRows" not in block, name



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
    for marker in ("async function measureAndSync()", "async function lookupBoundaryAddresses()", "async function applyAddressPreviewAsBoundary()", "async function analyzeParcels()", "async function analyzeBuildings()", "async function analyzeBuildingHub(", "async function analyzeRoadAccess()"):
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
    assert 'spatial_evidence:{zoning:zoningSpatialEvidenceFacts(),roads:schemeRoadEvidenceFacts(c),frontage:schemeFrontageEvidenceFacts(c),street_block:streetBlockSpatialEvidenceFacts(),activation_arterial:activationLinearCommercialEvidence(),safe_medical:safeMedicalSpatialEvidenceFacts()}' in html
    assert 'function roadRawFacts(cArg=null)' in html
    assert 'has_20m_width_candidate' in html
    assert 'has_20m:c.has20' not in html
    assert 'arterialRoad' not in html and 'arterial_road' not in html
    assert 'store.site.spatial_evidence?.roads?.safe' in html
    assert 'store.site.spatial_evidence?.safe_medical' in html
    # 도로기준은 제도별로 분리한다. 하나의 generic arterial PASS를 쓰면 안 된다.
    for key in ("key:'activation'", "key:'safe'", "key:'growth'", "key:'longterm'", "key:'station_complex'", "key:'innovation_growth'", "key:'innovation_housing'", "key:'public_complex'"):
        assert key in html, key
    for fact_key in ('activationRoadFact','safeHousingRoadFact','growthPotential35mRoadFact','longtermArterialIntersectionFact','stationComplexRoadFact','innovationGrowthRoadFact','innovationHousingRoadFact','publicComplexRoadFact'):
        assert fact_key in html, fact_key
    assert "mode:'width6_frontage'" in html
    assert "threshold:20" in html and "threshold:35" in html
    assert "mode:'road4_8'" in html
    assert '도로 위계와 도시고속도로 제외 여부는 결과서 단서에 따라 재검토' in html
    assert "if(selected==='safe')" in html and "turf.buffer(f,50,{units:'meters'})" in html

    # 접도율/접면기준은 제도별 Fact로 분리하며 공통 frontage Boolean/ratio로 대체하지 않는다.
    assert 'function schemeFrontageEvidenceFacts(cArg=null)' in html
    for fact_key in ('redevelopmentFrontage6Fact','residentialEnvironmentFrontage6Fact','activationFrontageFact','safeHousingFrontageFact','growthPotentialFrontage35Fact','longtermFrontage20Fact','stationComplexFrontageFact','innovationGrowthFrontageFact','innovationHousingFrontageFact','publicComplexFrontageFact'):
        assert fact_key in html, fact_key
    for dom_id in ('spRoadFrontageLabel','spRoadFrontageValue','spRoadFrontageBasis','spRoadFrontageContact','spRoadFrontageStatus'):
        assert f'id="{dom_id}"' in html, dom_id
    assert '사업진입조건 확인을 위한 개략적 추정치로, 현장조서 및 도면검토를 통해 보완될 수 있음' in html
    assert "key:'redevelopment'" in html
    # 용도지역 지도는 용도지역별 실제 색상과 동일 색 범례를 제공한다.
    assert 'function zoningColorSpec(name)' in html
    assert 'zoning-legend-chip' in html

    # 장기전세 간선도로 교차지 경로는 거리·폭원·접면비를 모두 계산해 이진 판정한다.
    lt_start=html.index('function longtermSpatialFacts(store)')
    lt_end=html.index('function checkLongtermFromFacts', lt_start)
    lt=html[lt_start:lt_end]
    assert "const geometryOk=c.arterialIntersectionDist<=200&&c.has20Width===true&&c.road20Perimeter>=12.5" in lt
    assert "arterialStatus=geometryOk?'PASS':'FAIL'" in lt
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
    assert "사업방식 근거 기준일" in readme
    assert "분석번호" in readme
    assert "SEOUL_OPEN_DATA_KEY" in readme
    assert (root / "RULE_AUDIT_v2.5.0.md").exists()
    assert not list(root.glob("CHANGELOG_v2.5.0-r*.txt"))
    basic_zip = root / "basic_unit_seoul.zip"
    assert basic_zip.is_file() and basic_zip.stat().st_size > 10_000_000
    assert not (root / "road_seoul.zip").exists()
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
    gc.collect()


def _run(label, fn) -> None:
    started = time.time()
    fn()
    print(f"PASS {label} ({time.time()-started:.1f}s)", flush=True)





def check_safe_medical_api_adapter() -> None:
    site = {
        "type": "Polygon",
        "coordinates": [[[126.998, 37.498], [127.002, 37.498], [127.002, 37.502], [126.998, 37.502], [126.998, 37.498]]],
    }
    test_parcel = {
        "type": "Feature", "properties": {"pnu": "1111010100100010000"},
        "geometry": {"type": "Polygon", "coordinates": [[[126.9997,37.4997],[127.0003,37.4997],[127.0003,37.5003],[126.9997,37.5003],[126.9997,37.4997]]]},
    }
    refs={
        "version":"test",
        "health_centers":[{"district":"테스트구","name":"테스트구보건소","address":"서울 테스트로 3"}],
        "municipal_hospitals":[{"name":"서울특별시서울의료원","aliases":["서울의료원"],"address":"서울 테스트로 2"}],
    }
    rows=[
        {"HPID":"A","DUTYDIVNAM":"종합병원","DUTYNAME":"테스트종합병원","DUTYADDR":"서울 테스트로 1","WGS84LON":"127.0","WGS84LAT":"37.5","WORK_DTTM":"2026-08-08"},
        {"HPID":"B","DUTYDIVNAM":"종합병원","DUTYNAME":"서울특별시서울의료원","DUTYADDR":"서울 테스트로 2","WGS84LON":"127.0","WGS84LAT":"37.5","WORK_DTTM":"2026-08-08"},
        {"HPID":"C","DUTYDIVNAM":"보건소","DUTYNAME":"테스트구보건소","DUTYADDR":"서울 테스트로 3","WGS84LON":"127.0","WGS84LAT":"37.5","WORK_DTTM":"2026-08-08"},
        {"HPID":"D","DUTYDIVNAM":"종합병원","DUTYNAME":"원거리종합병원","DUTYADDR":"서울 원거리로 1","WGS84LON":"126.0","WGS84LAT":"37.5","WORK_DTTM":"2026-08-08"},
        {"HPID":"E","DUTYDIVNAM":"병원","DUTYNAME":"서울의료원부속의원","DUTYADDR":"서울 테스트로 4","WGS84LON":"127.0","WGS84LAT":"37.5","WORK_DTTM":"2026-08-08"},
    ]
    parcel={"status":"resolved","feature":test_parcel,"pnu":"1111010100100010000"}
    with patch.object(app, "_safe_medical_reference_data", return_value=refs), \
         patch.object(app, "_tb_hospital_rows_live_or_snapshot", return_value=(rows,{"service":"TbHospitalInfo","mode":"live","rows":len(rows)})), \
         patch.object(app, "_tb_hospital_snapshot_rows", return_value=[]), \
         patch.object(app, "_seoul_open_data_key_info", return_value=("dummy","SEOUL_OPEN_DATA_KEY")), \
         patch.object(app, "_representative_parcel_for_facility", return_value=parcel) as parcel_lookup:
        result=app._safe_medical_reference(site)
    cats={row["category"] for row in result["items"]}
    assert {"general_hospital","municipal_hospital","public_health_center"}.issubset(cats)
    assert result["auto_pass_eligible"] is True
    assert result["nearby_counts"]["boundary_confirmed_350"] >= 1
    assert result["source_stats"]["hospital"]["service"] == "TbHospitalInfo"
    assert result["source_stats"]["medical_reference"]["official_health_centers"] == 1
    assert result["source_stats"]["medical_reference"]["public_health_center"] == 1
    assert result["source_stats"]["medical_reference"]["point_table_total"] == 4
    assert result["source_stats"]["medical_reference"]["point_screened_1500m"] == 3
    assert result["source_stats"]["medical_reference"]["parcel_lookup_calls"] == 3
    assert parcel_lookup.call_count == 3
    assert app._safe_medical_match_ref("서울의료원부속의원", refs["municipal_hospitals"]) is None
    assert all(x.get("boundary_basis") == "REPRESENTATIVE_CADASTRAL_PARCEL" for x in result["items"])

    html=(Path(__file__).resolve().parent / "app.html").read_text(encoding="utf-8")
    assert 'id="spSafeMedicalApiInfo"' in html
    assert "대표지번 1필지" in html
    data_dir=Path(__file__).resolve().parent / "data"
    reference=json.loads((data_dir / "safe_medical_reference.json").read_text(encoding="utf-8"))
    assert len(reference.get("health_centers") or []) == 25
    assert len(reference.get("municipal_hospitals") or []) == 10
    assert (data_dir / "TbHospitalInfo_snapshot_20260808.csv").is_file()
    snapshot=app._tb_hospital_snapshot_rows()
    assert sum(1 for row in snapshot if row.get("DUTYDIVNAM")=="보건소") == 25

    app._representative_parcel_cached.cache_clear()
    with patch.object(app, "_vworld_parcel_at_point", return_value=parcel) as point_lookup:
        app._representative_parcel_for_facility(lon=127.123456789,lat=37.555555555,address="서울 테스트로 9")
        app._representative_parcel_for_facility(lon=127.123456788,lat=37.555555554,address="서울  테스트로 9")
    assert point_lookup.call_count == 1
    app._representative_parcel_cached.cache_clear()

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
    # 결과 정교화 선택값은 추천 게이트로만 사용하고 기초검토 Rule Fact는 항상 생성한다.
    assert '<option value="housing">주거(일반)</option>' in html
    assert '<option value="housing_rental">주거(임대)</option>' in html
    assert 'onchange="runAllSchemeChecks()"' in html
    assert "if(name==='safe' && purpose!=='housing_rental')return {enabled:false" in html
    assert "const engineGate=purposeEngineGate(name);" in html
    assert "const result=evaluator(name,store);" in html
    assert "result.purpose_refinement_required=true" in html
    assert "delete store.scheme_specific[name]" not in html
    assert "function purposeDisabledSchemeResult" not in html
    assert "if(name==='safe' && purpose!=='housing_rental')return {state:'off'" in html
    assert "const HOUSING_PURPOSE_VALUES=new Set(['housing','housing_rental'])" in html
    # 주거(임대)는 다른 주거계열 분류에는 주거로 취급하되 안심주택만 별도 hard gate를 가진다.
    assert "if(purpose==='housing'||purpose==='housing_rental')return 'housing';" in html
    # 주택재개발은 독립모듈로 전환됐지만 접도율은 도로중심선 기반 개략 Fact로 유지한다.
    assert "const SHELL_SCHEMES=new Set(['urban_innovation_zone','facility_complex_zone','mixed_use_zone'])" in html
    assert "fact_key:'redevelopmentFrontage6Fact'" in html
    assert "trySpatialLayerCandidates(['TL_SPRD_MANAGE','LT_C_SPRD_MANAGE']" in html
    assert "사업진입조건 확인을 위한 개략적 추정치로, 현장조서 및 도면검토를 통해 보완될 수 있음" in html
    assert "const roadQuality='ESTIMATE'" in html
    assert "analysisState.quality.road=roadQuality" in html
    assert 'function checkRedevelopment(' not in html

def check_remaining_four_independent_modules_and_sources() -> None:
    """주택재개발·재건축·주거환경개선·일반주택건설은 독립모듈이며 모든 검토항목에 공식근거가 붙어야 한다."""
    root = Path(app.BASE_DIR)
    html = (root / "app.html").read_text(encoding="utf-8")
    py = (root / "app.py").read_text(encoding="utf-8")

    assert "SCHEME_MODULE_API_VERSION='2026-09-02-r22-station-area-frontage-no-hierarchy'" in html
    assert "const SHELL_SCHEMES=new Set(['urban_innovation_zone','facility_complex_zone','mixed_use_zone'])" in html
    required = (
        "function redevelopmentSpatialFacts(store)", "function checkRedevelopmentFromFacts(store,f)",
        "function reconstructionSpatialFacts(store)", "function checkReconstructionFromFacts(store,f)",
        "function residentialEnvironmentSpatialFacts(store)", "function checkResidentialEnvironmentFromFacts(store,f)",
        "function generalHousingSpatialFacts(store)", "function checkGeneralHousingFromFacts(store,f)",
        "collectFacts:redevelopmentSpatialFacts", "collectFacts:reconstructionSpatialFacts",
        "collectFacts:residentialEnvironmentSpatialFacts", "collectFacts:generalHousingSpatialFacts",
        "function renderRemainingSchemeDetailPopup(name)",
        "['redevelopment','reconstruction','residential_environment','general_housing','smallscale','prior_negotiation'].includes(name)",
    )
    for text in required:
        assert text in html, text

    module_block = html[html.index("const SCHEME_MODULES="):html.index("function evaluateSchemeModule", html.index("const SCHEME_MODULES="))]
    for key in ("redevelopment", "reconstruction", "residential_environment", "general_housing"):
        assert f"{key}:{{" in module_block or f"{key}: {{" in module_block
    assert "smallscale:{" in module_block or "smallscale: {" in module_block
    assert "prior_negotiation:{" in module_block or "prior_negotiation: {" in module_block
    for shell in ("urban_innovation_zone", "facility_complex_zone", "mixed_use_zone"):
        assert f"{shell}:{{" not in module_block and f"{shell}: {{" not in module_block

    # 신규 4개 사업의 모든 schemeRow는 sourceId + locator를 명시한다.
    blocks = [
        ("function checkRedevelopmentFromFacts(store,f)", "function reconstructionSpatialFacts(store)"),
        ("function checkReconstructionFromFacts(store,f)", "function residentialEnvironmentSpatialFacts(store)"),
        ("function checkResidentialEnvironmentFromFacts(store,f)", "function generalHousingSpatialFacts(store)"),
        ("function checkGeneralHousingFromFacts(store,f)", "const SCHEME_MODULES="),
    ]
    for start_text, end_text in blocks:
        block = html[html.index(start_text):html.index(end_text, html.index(start_text))]
        calls = [line for line in block.splitlines() if "schemeRow(" in line]
        assert calls, start_text
        for line in calls:
            assert "sourceId:" in line, f"sourceId missing: {line.strip()}"
            assert "locator:" in line, f"locator missing: {line.strip()}"

    # 화면의 결과/계획기준 표 모두 근거를 노출하고, 행이 없어도 default source를 붙인다.
    popup = html[html.index("function renderRemainingSchemeDetailPopup(name)"):html.index("function renderSchemeComparePopup()")]
    assert popup.count("<th>근거</th>") >= 2
    assert "검토표의 모든 항목은 근거유형·기준일·조문/장절을 함께 표시합니다." in popup
    assert "const resolved=ruleSourceFor(scheme,item)" in html
    assert "source=schemeSheetSourceCell({sourceType:src.type" in html

    # 독립 전용검토서의 계획기준 근거 셀도 단순 근거명 문자열이 아니라 공통 source renderer를 사용한다.
    assert "function schemeSheetSourceFor(scheme,item,sourceId='',locator='')" in html
    raw_source_cells = (
        '<td>안심주택 운영기준의 용도지역별 계획기준</td>',
        '<td>서울특별시 지구단위계획 수립기준</td>',
        '<td>역세권 장기전세주택 건립 운영기준</td>',
        '<td>운영기준 용도지역 변경기준</td>',
        '<td>도심복합개발법·서울시 조례/시행규칙</td>',
        '<td>2030 서울특별시 도시·주거환경정비기본계획</td>',
    )
    for raw in raw_source_cells:
        assert raw not in html, f"unstructured source cell remains: {raw}"

    # 일반 주택건설 30~49세대는 주택유형별 50세대 예외 여부를 확인하기 전 PASS하지 않는다.
    assert "else if(units>=30){approvalStatus='REVIEW'" in html
    assert "30~49세대 · 주택유형별 승인기준 확인" in html

    # 최신 서울 도시계획조례 재편 번호: 용적률은 제48조. 구 제55조를 신규 일반주택 기준에 쓰지 않는다.
    assert "locator:'제48조 용도지역 안에서의 용적률'" in html
    assert "서울특별시 도시계획 조례 제48조" in html
    gh = html[html.index("function generalHousingSpatialFacts(store)"):html.index("const SCHEME_MODULES=", html.index("function generalHousingSpatialFacts(store)"))]
    assert "도시계획 조례 제55조" not in gh

    # 두 사업은 같은 6m 도로·연속 4m 접촉 산식을 쓰되 적용 기준비율은 각각 20%/40%다.
    assert "fact_key:'residentialEnvironmentFrontage6Fact'" in html
    assert "fact_key:'redevelopmentFrontage6Fact'" in html
    assert "주택재개발 접도율(6m 기준·40% 이하)" in html
    assert "주거환경개선 주택접도율(6m 기준·20% 이하)" in html
    assert "const resenvAccess=Number.isFinite(Number(net.frontage_access_buildings_6m))" in html

    readme = (root / "README.md").read_text(encoding="utf-8")
    list_block = readme[readme.index("## v2.5.0 사업방식 구조 · 독립 검토모듈"):readme.index("## v2.4.3", readme.index("## v2.5.0 사업방식 구조 · 독립 검토모듈"))]
    for label in ("주택재개발", "재건축", "주거환경개선", "주택개발사업"):
        assert label in list_block
    assert "근거유형 / 공식 근거명 / 조문·장절 / 기준일 / 원문 링크" in list_block

    # 서버측 구형 재개발 판정엔진/중복 API는 제거한다.
    assert "def evaluate_redevelopment(" not in py
    assert '/api/redevelopment/evaluate' not in py
    assert '"scheme_module_api": "2026-09-02-r22-station-area-frontage-no-hierarchy"' in py
    assert '16 independent modules including smallscale 5-route family and prior_negotiation' in py

def check_scheme_family_separation() -> None:
    """주택정비 3종은 raw Fact만 공유하고 주택개발사업은 독립 family로 분리한다."""
    root = Path(app.BASE_DIR)
    html = (root / "app.html").read_text(encoding="utf-8")

    assert "const SCHEME_FAMILY_META={" in html
    assert "housing_renewal:{label:'주택정비사업'" in html
    assert "general_housing:{label:'민간주택개발'" in html
    assert "const HOUSING_RENEWAL_SCHEMES=new Set(SCHEME_FAMILY_META.housing_renewal.members)" in html
    assert "function housingRenewalFamilyFacts(store)" in html
    assert "family_specific:{},scheme_specific:{}" in html
    assert "공유하는 것은 공간·건축물 원자료 Fact뿐이며 입안요건 판정은 각 독립 Rule Module에서 수행" in html

    # 세 주택정비사업만 family raw fact를 읽는다.
    for fn in ("redevelopmentSpatialFacts", "reconstructionSpatialFacts", "residentialEnvironmentSpatialFacts"):
        start = html.index(f"function {fn}(store)")
        end = html.find("\nfunction ", start + 20)
        block = html[start:end if end != -1 else len(html)]
        assert "housingRenewalFamilyFacts(store)" in block, fn

    gh_start = html.index("function generalHousingSpatialFacts(store)")
    gh_end = html.index("function checkGeneralHousingFromFacts", gh_start)
    gh = html[gh_start:gh_end]
    assert "housingRenewalFamilyFacts(store)" not in gh
    assert "independent_from_renewal:true" in gh
    assert "주택정비사업 Family Fact/노후도/과소필지/주택접도율을 진입조건으로 사용하지 않는다" in gh

    # 일반주택은 재개발/재건축 실패의 자동 fallback으로 제시하지 않는다.
    alt = html[html.index("function schemeAlternativeText"):html.index("function ruleTrust", html.index("function schemeAlternativeText"))]
    assert "redevelopment:'주거환경개선·소규모주택정비(별도 검토)'" in alt
    assert "reconstruction:'소규모재건축·리모델링(별도 검토)'" in alt
    assert "redevelopment:'소규모주택정비·주거환경개선·일반주택건설'" not in alt
    assert "reconstruction:'소규모재건축·리모델링·일반주택건설'" not in alt

    # 첫 화면에서도 Family를 구분해 보여준다.
    assert 'data-family="housing_renewal">주택정비사업' in html
    assert 'data-family="smallscale_housing">소규모주택정비사업' in html
    assert 'data-family="general_housing">민간주택개발' in html
    assert 'data-family="special_housing">특례 주택사업' in html
    assert 'data-family="urban_renewal">도심정비사업' in html
    assert 'data-family="special_development">특례개발사업' in html
    assert "family_key:schemeFamilyKey(x.name),family_label:schemeFamilyLabel(x.name)" in html
    # 전체비교에서도 family 축을 잃지 않는다.
    assert "<th>사업 Family</th><th>사업방식</th>" in html
    assert "${escHtml(schemeFamilyLabel(name))}" in html

    # 일반주택의 도시계획 중첩은 정비구역 자료가 아니라 범용 도시계획 공간근거를 사용한다.
    assert "PLANNING_GIS:{type:'공간정보·고시',title:'서울도시계획포털·VWorld 도시계획 공식 공간자료'" in html
    gh_eval_start = html.index("function checkGeneralHousingFromFacts(store,f)")
    gh_eval_end = html.index("const SCHEME_MODULES=", gh_eval_start)
    gh_eval = html[gh_eval_start:gh_eval_end]
    assert "sourceId:'PLANNING_GIS'" in gh_eval
    assert "sourceId:'RENEWAL_GIS'" not in gh_eval

def check_r8_boundary_map_smallscale_prior() -> None:
    """r8: 구역계 준비/수동 검토, 지도대안, 소규모정비 5경로, 사전협상, 3개 shell을 고정한다."""
    root = Path(app.BASE_DIR)
    html = (root / "app.html").read_text(encoding="utf-8")
    py = (root / "app.py").read_text(encoding="utf-8")

    # 구역계 입력 수단: 기존 직접그리기·지번 입력을 유지하고 SHP를 추가한다.
    for marker in (
        'id="boundaryDrawTab"', 'id="boundaryAddressTab"', 'id="boundaryShpTab"',
        'function lookupBoundaryAddresses()', 'function applyAddressPreviewAsBoundary()',
        'function loadBoundaryShp()', 'normalizeShpGeojson', 'geometryLooksLikeSeoul',
    ):
        assert marker in html, marker

    # 지도는 일반/항공/항공+도시계획을 선택하고, 용도지역·도시계획시설을 반투명 오버레이한다.
    for marker in (
        '<option value="normal">일반지도</option>', '<option value="satellite">항공사진</option>',
        '<option value="satellite_planning">항공사진 + 도시계획</option>',
        'id="mapOverlayZoning"', 'id="mapOverlayFacility"', 'id="mapOverlayOpacity"',
        "const mapBaseSatellite=L.tileLayer", "const mainMapZoningWms=L.tileLayer.wms",
        "const mainMapFacilityWms=L.tileLayer.wms", "function setMapVisualMode(mode)",
        "function syncMapPlanningOverlay()",
    ):
        assert marker in html, marker
    assert "항공·도시계획 오버레이는 구역계 작성을 돕는 시각자료" in html

    # 회귀오류 방지: 도형 생성 직후 무거운 API를 자동 실행하지 않고 검토버튼을 활성화한다.
    m_start=html.index("async function measureAndSync()")
    m_end=html.index("async function runSiteReview()", m_start)
    prep=html[m_start:m_end]
    assert "clearBoundaryAnalysisForNewGeometry()" in prep
    assert "setBoundaryReferenceMetrics()" in prep
    assert "runAllAutoAnalyses()" not in prep
    assert "setSiteReviewStatus('구역계 설정 완료" in prep
    assert "btn.disabled=siteReviewRunning||!activeGeometry" in html
    # reset/사업엔진 오류가 생겨도 구역계 준비 흐름과 검토버튼 활성화를 막지 않는다.
    clear_start=html.index("function clearBoundaryAnalysisForNewGeometry()")
    clear_end=html.index("function setBoundaryReferenceMetrics()", clear_start)
    clear_block=html[clear_start:clear_end]
    assert "try{resetMeasure();}catch(e)" in clear_block
    reset_start=html.index("function resetMeasure()")
    reset_end=html.index("async function analyzeParcels()", reset_start)
    reset_block=html[reset_start:reset_end]
    assert "try{runAllSchemeChecks();}catch(e)" in reset_block

    review_start=html.index("async function runSiteReview()")
    review_end=html.index("function resetMeasure()", review_start)
    review=html[review_start:review_end]
    assert "runAllAutoAnalyses({skipParcels:true})" in review
    assert "safeAnalysisStep('연속지적',analyzeParcels" in review
    assert "finally{" in review and "siteReviewRunning=false" in review
    assert "safeAnalysisStep('연속지적'" in html
    assert "Promise.race" in html and "ANALYSIS_STEP_TIMEOUT_MS" in html
    # draw/address/SHP 모두 동일 geometry->prep 경로를 탄다.
    created=html[html.index("map.on(L.Draw.Event.CREATED"):html.index("map.on(L.Draw.Event.EDITED")]
    assert "await measureAndSync()" in created
    address=html[html.index("async function applyAddressPreviewAsBoundary()") : html.index("function normalizeShpGeojson")]
    assert "await measureAndSync()" in address
    shp=html[html.index("async function loadBoundaryShp()") : html.index("function clearBoundaryShp()")]
    assert "await measureAndSync()" in shp

    # 6개 Family UI와 소규모정비 5개 사용자 검토경로.
    families=(
        ('housing_renewal','주택정비사업'), ('smallscale_housing','소규모주택정비사업'),
        ('general_housing','민간주택개발'), ('special_housing','특례 주택사업'),
        ('urban_renewal','도심정비사업'), ('special_development','특례개발사업'),
    )
    for key,label in families:
        assert f'data-family="{key}">{label}' in html
    for route,label in (
        ('autonomous','자율주택정비'), ('block','가로주택정비'),
        ('reconstruction','소규모재건축'), ('redevelopment','소규모재개발'),
        ('moa','모아타운+모아주택'),
    ):
        assert f'data-smallscale-route="{route}"' in html and label in html
    assert "function smallscaleSpatialFacts(store)" in html
    assert "function checkSmallscaleFromFacts(store,f)" in html
    assert "별도 5번째 법정사업으로 판정하지 않고" in html
    assert "showSmallscaleRouteBasis" in html

    # 사전협상은 실제 독립모듈이고, 면적 5천㎡ 근거는 사전협상조례가 아니라 서울 도시계획조례 제17조로 연결한다.
    assert "function priorNegotiationSpatialFacts(store)" in html
    assert "function checkPriorNegotiationFromFacts(store,f)" in html
    assert "prior_negotiation:{id:'prior_negotiation'" in html
    assert "PRIOR_NEGOTIATION_AREA:{type:'조례',title:'서울특별시 도시계획 조례'" in html
    assert "locator:'제17조 · 사전협상 대상지 5천제곱미터 이상'" in html
    assert "sourceId:'PRIOR_NEGOTIATION_AREA'" in html
    assert "2026-06-29 제11차 개정" in html

    # 공간혁신 3종은 shell로만 남아 실제 모듈/추천에 진입하지 않는다.
    assert "const SHELL_SCHEMES=new Set(['urban_innovation_zone','facility_complex_zone','mixed_use_zone'])" in html
    module_block=html[html.index("const SCHEME_MODULES="):html.index("function evaluateSchemeModule", html.index("const SCHEME_MODULES="))]
    for shell in ('urban_innovation_zone','facility_complex_zone','mixed_use_zone'):
        assert f"{shell}:{{" not in module_block and f"{shell}: {{" not in module_block
    assert "현재 자동 활성화·추천·우선순위 미반영" in html
    assert "16 independent modules including smallscale 5-route family and prior_negotiation" in py
    assert '"engine": "site_fact_store_v2.5.0_r11"' in py
    assert "five user review routes: autonomous / block / small-scale reconstruction / small-scale redevelopment / Moa Town+Moa Housing policy route" in py
    assert "r22-station-area-frontage-no-hierarchy" in html and "r22-station-area-frontage-no-hierarchy" in py


def check_r9_refinement_placement() -> None:
    """r9: 결과 정교화 선택입력은 검토하기 버튼 바로 위에 배치한다."""
    root = Path(app.BASE_DIR)
    html = (root / "app.html").read_text(encoding="utf-8")
    # 서비스 우측 패널에서 제거되고, 위치도 내 review bar 직전에 동적으로 삽입되어야 한다.
    right_start=html.index('<div class="service-right-box">')
    right_end=html.index('</div>\n      </div>\n    </section>', right_start)
    right_block=html[right_start:right_end]
    assert 'service-condition-box' not in right_block
    assert "const reviewBar=location.querySelector('.boundary-review-bar');" in html
    assert "reviewBar.insertAdjacentElement('beforebegin',conditionBox);" in html
    assert '결과 정교화 — 선택사항' in html
    assert 'id="serviceConditionInputs"' in html



def check_r10_scheme_fail_safe() -> None:
    """r10: 한 사업엔진/렌더러 오류가 나머지 사업추천과 미리보기를 중단하지 않는다."""
    root = Path(app.BASE_DIR)
    html = (root / "app.html").read_text(encoding="utf-8")
    py = (root / "app.py").read_text(encoding="utf-8")

    # 사업모듈은 개별 격리하고 오류 사업만 REVIEW 결과를 만든다.
    assert "function evaluateSchemeModulesSafely(store,names=null,evaluator=evaluateSchemeModule)" in html
    safe_block = html[html.index("function evaluateSchemeModulesSafely"):html.index("function runAllSchemeChecks", html.index("function evaluateSchemeModulesSafely"))]
    assert "for(const name of order)" in safe_block
    assert "catch(e)" in safe_block
    assert "schemeModuleReviewResult(name,e,store)" in safe_block
    assert "schemeResults[name]=fallback" in safe_block

    # Candidate/밀도 계산도 사업별 fail-safe이며, 순위 계산 실패 시 기본 후보순위를 남긴다.
    assert "function candidateEngineErrorState(name,error)" in html
    assert "function safeCandidateState(name)" in html
    assert "function safeDensityPotentialForScheme(name,st=null)" in html
    cand = html[html.index("function updateCandidateSchemes()"):html.index("const SMALLSCALE_ROUTE_UI=", html.index("function updateCandidateSchemes()"))]
    assert "try{st=ccContextFor(name);}" in cand and "candidateEngineErrorState(name,e)" in cand
    assert "analysisState.recommendations=ranked.filter" in cand

    # UI 렌더링은 개별 단계로 격리되어 priority preview가 앞 단계 오류에 묶이지 않는다.
    runall = html[html.index("function runAllSchemeChecks()"):html.index("// ---------- Boundary input", html.index("function runAllSchemeChecks()"))]
    for marker in (
        "safeSchemeUiStep('candidate'", "safeSchemeUiStep('comparison'",
        "safeSchemeUiStep('sheets'", "safeSchemeUiStep('priority-preview'"
    ):
        assert marker in runall, marker
    review = html[html.index("async function runSiteReview()"):html.index("function resetMeasure()", html.index("async function runSiteReview()"))]
    assert "현황 Fact Store 오류가 발생해 사업추천·시뮬레이션을 중단했습니다" in review
    assert "오류가 없는 독립 사업모듈만 계속 판정했습니다" in review
    assert "safeSchemeUiStep('candidate-final'" in review
    assert "safeSchemeUiStep('priority-final'" in review

    # 사전협상 공식 11차 개정 PDF 원본을 배포근거자료로 함께 보존한다.
    pdf = root / "도시계획변경 사전협상 운영지침(11차개정_2026.06.29).pdf"
    assert pdf.is_file() and pdf.stat().st_size > 500_000
    assert 'site_fact_store_v2.5.0_r11' in py
    assert 'r22-station-area-frontage-no-hierarchy' in html and 'r22-station-area-frontage-no-hierarchy' in py


def check_r11_popup_spatial_progress() -> None:
    """r11: 사업별 팝업·공간현황 도면·장시간 분석 진행표시가 회귀하지 않아야 한다."""
    root = Path(app.BASE_DIR)
    html = (root / "app.html").read_text(encoding="utf-8")
    py = (root / "app.py").read_text(encoding="utf-8")

    # 사업카드를 누르면 모달을 먼저 열고 렌더러 오류도 fallback으로 표시한다.
    assert "function openSchemeDetailSafely(name)" in html
    popup = html[html.index("function renderSchemePopupFallback"):html.index("function ccTopEntries", html.index("function renderSchemePopupFallback"))]
    assert "openReviewModal('schemeDetailModal')" in popup
    assert "renderSchemeDetailPopup(name)" in popup
    assert "renderSchemePopupFallback(name,e)" in popup
    assert "팝업 표시 자체는 유지" in popup
    assert "openSchemeDetailSafely(name);" in popup

    # 공간현황은 구역계만 남지 않도록 전체 미니맵을 DOM 이동/최종분석 후 재계산한다.
    assert "function invalidateAllSpatialMaps(refresh=true)" in html
    inv = html[html.index("function invalidateAllSpatialMaps"):html.index("function enforceLocationMapBoundaryOnly", html.index("function invalidateAllSpatialMaps"))]
    for map_name in ("ccLandMini","ccBuildingMini","ccZoningMini","ccStationMini","ccStreetBlockMini","ccCenterMini","ccRenewalStatusMini","ccDevelopmentStatusMini","ccSchemeRoadMini","ccSafeMedicalMini","ccPlanningMini"):
        assert map_name in inv, map_name
    for renderer in ("refreshCommonParcelBases()","renderIndependentSpatialStatusMaps()","renderZoningSpatialStatus()","renderStreetBlockSpatialStatus()","renderSchemeRoadEvidence()","renderSafeMedicalSpatialStatus()"):
        assert renderer in inv, renderer
    assert "setTimeout(()=>invalidateAllSpatialMaps(true),80);" in html
    assert "map.invalidateSize(false);invalidateAllSpatialMaps(true)" in html

    # 사용자가 장시간 분석을 기다릴 수 있도록 진행시간·단계상태를 표시하고 정확성 우선 timeout을 사용한다.
    for dom_id in ("siteAnalysisProgress","siteAnalysisElapsed","siteAnalysisProgressBar","siteAnalysisProgressSteps","siteAnalysisProgressNote"):
        assert f'id="{dom_id}"' in html, dom_id
    for fn in ("beginAnalysisProgress","markAnalysisProgress","finishAnalysisProgress","renderAnalysisProgress"):
        assert f"function {fn}" in html, fn
    assert "정확성 우선 모드" in html
    assert "analyzePlanningGIS,90000" in html
    assert "analyzeBuildingHub,120000" in html
    assert "analyzeRoadAccess,60000" in html
    assert "총 ${formatAnalysisElapsed" in html
    assert 'site_fact_store_v2.5.0_r11' in py
    assert 'r22-station-area-frontage-no-hierarchy' in html and 'r22-station-area-frontage-no-hierarchy' in py



def check_r11_data_recovery_fix1() -> None:
    """R11 실제 화면에서 발견된 Fact Store 연쇄오류·도로 fallback·가짜추천을 방지한다."""
    root = Path(app.BASE_DIR)
    html = (root / "app.html").read_text(encoding="utf-8")
    py = (root / "app.py").read_text(encoding="utf-8")

    road_block = html[html.index("function schemeRoadEvidenceFacts"):html.index("function schemeRoadEvidenceStyle", html.index("function schemeRoadEvidenceFacts"))]
    assert "const frontageFacts=schemeFrontageEvidenceFacts(c);" in road_block
    assert "const residentialEnvironment=frontageFacts.residential_environment;" in road_block
    assert "residential_environment:residentialEnvironment" in road_block

    fetch_block = html[html.index("async function fetchRoadNetwork("):html.index("async function analyzeRoadAccess()", html.index("async function fetchRoadNetwork("))]
    assert "TL_SPRD_MANAGE" in fetch_block and "ROAD_BT" in fetch_block
    # 도로 Fact는 VWorld TL_SPRD_MANAGE ROAD_BT 단일 경로를 사용한다.
    assert "trySpatialLayerCandidates(['TL_SPRD_MANAGE','LT_C_SPRD_MANAGE']" in fetch_block
    assert "road_polygons:derived" in fetch_block
    assert "ROAD_BT가 없거나 유효하지 않은 구간은 폭원을 추정하지 않고 REVIEW" in fetch_block

    ranking = html[html.index("function autoRecommendationTop3()"):html.index("function numOrNull", html.index("function autoRecommendationTop3()"))]
    assert "if(analysisState.fact_store_error)return [];" in ranking
    runall = html[html.index("function runAllSchemeChecks()"):html.index("// ---------- Boundary input", html.index("function runAllSchemeChecks()"))]
    assert "analysisState.fact_store_error=issue.message" in runall
    assert "analysisState.recommendations=[];analysisState.planning_alternatives=[];" in runall

    preview = html[html.index("function renderPriorityPreview()"):html.index("function schemeSheetFeasibility", html.index("function renderPriorityPreview()"))]
    assert "const area=store?.site?.area_m2??null;" in preview
    assert "numOrNull(document.getElementById('area_m2')?.value)" not in preview
    assert "Fact Store 오류로 선순위 산정을 중단" in preview

    assert 'VWorld TL_SPRD_MANAGE + ROAD_BT for cadastral/frontage calculations' in py
    assert '"scheme_module_api": "2026-09-02-r22-station-area-frontage-no-hierarchy"' in py



def check_r13_criterion_layer1() -> None:
    """R13: Fact 수집상태와 사업판정을 분리하고 역세권활성화/주택재개발 판정경로를 연결한다."""
    root = Path(app.BASE_DIR)
    html = (root / "app.html").read_text(encoding="utf-8")
    py = (root / "app.py").read_text(encoding="utf-8")

    assert "function schemeSpecificDecisionForRow(scheme,r)" in html
    assert "충족 · 단서" in html
    assert "사업별 기준판정" in html
    render_start=html.index("function schemeSpecificDecisionLabel")
    render = html[render_start:html.index("function ageFactValue", render_start)]
    assert "현황확인" not in render
    assert "불충족" in render and "확인필요" in render

    station = html[html.index("function activationStationCriterion"):html.index("function checkActivationFromFacts", html.index("function activationStationCriterion"))]
    assert "share>=50" in station
    assert "share>0" in station
    assert "block_committee" in station
    assert "위원회 심의 가능" in station
    assert "direct_inside_block_pending" in station

    activation = html[html.index("function checkActivationFromFacts"):html.index("function safeDistrictPlanOverlapState", html.index("function checkActivationFromFacts"))]
    for item in ("승강장 거리", "가로구역 포함", "역세권"):
        assert f"schemeRow('{item}'" in activation
    assert "conditional:stationDecision.overall.conditional" in activation
    assert "areaStatus='PASS';areaConditional=true" in activation

    redevelopment = html[html.index("function redevelopmentSpatialFacts"):html.index("function reconstructionSpatialFacts", html.index("function redevelopmentSpatialFacts"))]
    assert "5,000~10,000㎡ · 조례상 위원회 인정 등 예외경로를 사업가능 경로로 반영" in redevelopment
    assert "criterionStatus:smallStatus" in redevelopment
    assert "criterionStatus:densityStatus" in redevelopment
    assert "criterionStatus:age?.status||'REVIEW'" in redevelopment
    assert "conditional:f.area.conditional===true" in redevelopment

    assert "SCHEME_MODULE_API_VERSION='2026-09-02-r22-station-area-frontage-no-hierarchy'" in html
    assert '"scheme_module_api": "2026-09-02-r22-station-area-frontage-no-hierarchy"' in py

def check_r14_street_block_auto() -> None:
    """가로구역 후보는 기초단위구 + TL_SPRD_MANAGE ROAD_BT만 사용한다."""
    root = Path(app.BASE_DIR)
    html = (root / "app.html").read_text(encoding="utf-8")
    py = (root / "app.py").read_text(encoding="utf-8")
    assert 'id="ccStreetBlockMiniMap"' in html
    for dom_id in ("spStreetBlockState","spStreetBlockArea","spStreetBlockIntersection","spStreetBlockSiteShare","spStreetBlockCoverage","spStreetBlockRetainedRoad","spStreetBlockThroughRoad","spStreetBlockShare","spStreetBlockSiteStationShare","spStreetBlockRange","spStreetBlockPath"):
        assert f'id="{dom_id}"' in html
    assert "async function analyzeStreetBlock(" in html
    assert "/api/spatial/street-block" in html
    assert "road_features:roadFeatures" in html
    assert "TL_SPRD_MANAGE ROAD_BT만 사용" in html
    assert "deriveStreetBlockAnalysisScope" in html
    assert "retainedRoadFeatures" in html and "throughRoadCandidates" in html
    assert "_analyze_street_block_road_fallback" not in py


def check_r15_street_block_4m_conditional() -> None:
    """Current invariant: 4m is an engine merge threshold applied to ROAD_BT, not a legal width rule."""
    root = Path(app.BASE_DIR)
    html = (root / "app.html").read_text(encoding="utf-8")
    py = (root / "app.py").read_text(encoding="utf-8")
    assert "road_min_width_m = 4.0" in py
    assert "TL_SPRD_MANAGE ROAD_BT" in py
    assert "ROAD_BT의 4m는 자동후보 병합을 위한 엔진 운영기준일 뿐 법정 가로구역 도로요건과 별개" in html
    assert "철도|하천" in py
    assert "LT_C_UPISUQ151" not in html[html.index("function streetBlockBarrierSpec"):html.index("async function fetchStreetBlockFacilityBarriers")]

    from shapely.geometry import box, GeometryCollection
    from shapely.strtree import STRtree
    units = [box(0, 0, 50, 50), box(50, 0, 100, 50)]
    tree = STRtree(units)
    comp, limited = app._basic_unit_component(0, units, tree, GeometryCollection(), GeometryCollection(), max_units=20)
    assert limited is False and comp == {0, 1}
    road_corridor = box(48.5, -5, 51.5, 55)
    comp, limited = app._basic_unit_component(0, units, tree, road_corridor, GeometryCollection(), max_units=20)
    assert limited is False and comp == {0}


def check_r16_basic_unit_street_block() -> None:
    """SGIS 기초단위구가 후보 geometry이고 도로는 TL_SPRD_MANAGE ROAD_BT만 쓴다."""
    root = Path(app.BASE_DIR)
    html = (root / "app.html").read_text(encoding="utf-8")
    py = (root / "app.py").read_text(encoding="utf-8")
    for token in (
        "def _basic_unit_zip_path()", "def _basic_unit_spatial_layers()",
        "def _shared_edge_barrier", "def _basic_unit_component",
        "def _street_block_from_basic_units", "sgis_basic_unit_roadbt_merge",
        "basic_unit_is_legal_street_block", "street_block_basic_unit_configured",
    ):
        assert token in py, token
    assert "basicUnitContext" in html and "ccStreetBlockUnitContext" in html
    assert "SGIS 기초단위구 후보경계" in html
    assert "AUTO · 기초단위구+ROAD_BT" in html
    assert "도로 fallback" not in html[html.index("function renderStreetBlockSpatialStatus"):html.index("async function refreshMiniContextFeatures", html.index("function renderStreetBlockSpatialStatus"))]
    assert "SCHEME_MODULE_API_VERSION='2026-09-02-r22-station-area-frontage-no-hierarchy'" in html
    assert '"scheme_module_api": "2026-09-02-r22-station-area-frontage-no-hierarchy"' in py

    lon, lat, d = 127.0152, 37.5586, 0.00008
    geom = {"type":"Polygon","coordinates":[[[lon-d,lat-d],[lon+d,lat-d],[lon+d,lat+d],[lon-d,lat+d],[lon-d,lat-d]]]}
    result = app.analyze_street_block(geom, [], [], 500)
    md = result.get("metadata") or {}
    if not app._basic_unit_spatial_layers().get("available"):
        assert result["status"] == "unavailable", result
        assert md.get("fallback_used") is False


def check_r17_spatial_relation_road_facts() -> None:
    """R17: spatial facts are separated from scheme rules; block/site/station ratios have explicit denominators."""
    root = Path(app.BASE_DIR)
    html = (root / "app.html").read_text(encoding="utf-8")
    py = (root / "app.py").read_text(encoding="utf-8")
    for token in (
        "siteShareOfBlockPct","blockCoverageOfSitePct","siteStationSharePct","blockRelations",
        "site_share_of_block_pct","block_coverage_of_site_pct","station_share_of_block_pct","station_share_of_site_pct",
    ):
        assert token in html, token
    for label in (
        "분석범위 중 대상지 점유율","대상지의 분석범위 포함률","가로구역의 역세권 편입률","대상지의 역세권 편입률",
    ):
        assert label in html, label
    assert "frontage_ratio_pct:Number.isFinite(Number(x.boundary_share_pct))" in html
    assert "boundary_share_pct:pct(x.contact)" in html
    assert "도로명(없으면 RN_CD/관리번호) 단위로 묶는다" in html
    assert "faceCountAt(4)" in html and "faceCountAt(35)" in html
    assert "road4Perimeter" in html and "road6Perimeter" in html and "road8Perimeter" in html
    assert "ROAD_BT 폭원 · 도로위계 판정 비활성(사용자 제공자료 연결 대기)" in html
    assert "공간 Fact → 사업모듈 판정 입력" in html
    # station-complex uses site/block occupancy, while activation uses station/block coverage.
    station_block = html[html.index("function stationComplexSpatialFacts"):html.index("function checkStationComplexFromFacts")]
    assert "c.siteShareOfBlock??c.blockShare" in station_block
    activation_block = html[html.index("function activationSpatialFacts"):html.index("function spatialFactRow", html.index("function activationSpatialFacts"))]
    assert "activationBlockShare" in activation_block
    assert "stationBlock" in activation_block
    # explicit committee path is PASS + caveat, not data-unknown REVIEW.
    station_check = html[html.index("function checkStationComplexFromFacts"):html.index("function longtermSpatialFacts", html.index("function checkStationComplexFromFacts"))]
    assert "blockStatus='PASS';blockConditional=true" in station_check
    # street-block API accepts TL_SPRD_MANAGE centerline features.
    assert "road_features: List[Dict[str, Any]]" in py
    assert "도로폭은 TL_SPRD_MANAGE의 ROAD_BT" in py
    assert "_road_spatial_layers()" not in py[py.index("def _street_block_from_basic_units"):py.index("def analyze_street_block")]
    smallscale = html[html.index("function smallscaleSpatialFacts(store)"):html.index("function checkSmallscaleFromFacts")]
    assert "analysis_scope_m2" in smallscale and "retained_road_area_m2" in smallscale
    assert "through_road_candidate_count" in smallscale and "blockThroughRoadStatus" in smallscale
    assert "사용자 구역계는 고정" in html and "존치기반시설" in html
    assert "사업방식의 '대상면적'은 사용자가 확정한 구역계 면적" in html
    assert "area:Number.isFinite(Number(businessBoundaryArea))" in html
    assert "retained_facility_area_m2" in smallscale and "analysis_scope_connected" in smallscale
    derive = html[html.index("function deriveStreetBlockAnalysisScope"):html.index("function streetBlockApplicableStationBuffer")]
    assert "planningAnalysis.facilities" in derive and "_retained_kind:'facility'" in derive
    assert "activeGeometry=" not in derive, "analysis-scope derivation must not mutate user boundary"


def check_r18_bundled_basic_unit_and_frontage_caveat() -> None:
    """서울 기초단위구 번들과 TL_SPRD_MANAGE 기반 접도 단서를 검증한다."""
    root = Path(app.BASE_DIR)
    basic_zip = root / "basic_unit_seoul.zip"
    assert basic_zip.is_file() and basic_zip.stat().st_size > 10_000_000
    app._basic_unit_spatial_layers.cache_clear()
    layers = app._basic_unit_spatial_layers()
    assert layers.get("available") is True, layers
    assert layers.get("feature_count") == 72307, layers.get("feature_count")
    assert layers.get("base_date") == "20250630", layers.get("base_date")
    assert layers.get("source_crs_note") == "SGIS 제공기준 EPSG:5179 · PRJ 우선"
    # End-to-end: use one real bundled basic-unit polygon and a synthetic ROAD_BT centerline
    # exactly along its outer boundary. The result must resolve to that seed with ROAD_BT centerline input.
    unit = layers["rows"][1234]["geometry"]
    poly = unit if unit.geom_type == "Polygon" else max(unit.geoms, key=lambda g: g.area)
    point = poly.representative_point()
    site = point.buffer(0.00002).envelope
    boundary_road = {
        "type": "Feature",
        "geometry": mapping(LineString(list(poly.exterior.coords))),
        "properties": {"ROAD_BT": 8.0, "ROAD_NM": "R18 regression boundary"},
    }
    result = app.analyze_street_block(mapping(site), [], [boundary_road], 500)
    assert result["status"] in {"resolved", "partial"}, result
    assert result["metadata"].get("merged_basic_unit_count") == 1
    html = (root / "app.html").read_text(encoding="utf-8")
    assert "접도 분석 단서" in html
    assert "지적측량성과도·도로대장·결정도서 및 현장조사" in html
    assert "analysis_caveat" in html
    assert "도로폭은 TL_SPRD_MANAGE ROAD_BT만 사용합니다" in html
    changelog = (root / "CHANGELOG_v2.5.0.txt").read_text(encoding="utf-8")
    assert "[v2.5.0-r18" in changelog
    assert not list(root.glob("CHANGELOG_v2.5.0-r*.txt"))

def check_r19_activation_arterial_linear_commercial() -> None:
    html=(Path(__file__).resolve().parent / "app.html").read_text(encoding="utf-8")
    for marker in (
        'id="ccActivationArterialMiniMap"',
        '간선도로 검토',
        'id="widthRoadSchemeSummary"',
        "const WIDTH_ROAD_SCHEME_KEYS=['safe','growth','longterm','innovation_growth','innovation_housing']",
        'function analyzeActivationArterial()',
        'function updateActivationArterialBlockLink()',
        'function activationLinearCommercialEvidence()',
        'function classifyLinearCommercialShape(',
        'function activationRoadSegmentKey(',
        'function roadSideOfPoint(',
        'function linearCommercialRoadSearchBufferM(',
        'nearby_roads_plus_uq111_shape_aspect_local_bilateral',
        'activation_arterial:activationLinearCommercialEvidence()',
        "safeAnalysisStep('노선형 상업지역',async()=>{await analyzeActivationArterial();",
        "공개 용도지역 도형의 면적·둘레로 띠 폭을 역산",
    ):
        assert marker in html, marker
    # 노선형 상업지역·가로구역은 TL_SPRD_MANAGE와 용도지역/기초단위구 Fact로 분석한다.
    block=html[html.index('async function analyzeActivationArterial()'):html.index('async function analyzeStreetBlock(')]
    assert "fetchSpatialFeaturesBrowser('LT_C_UQ111'" in block
    assert "trySpatialLayerCandidates(['TL_SPRD_MANAGE','LT_C_SPRD_MANAGE']" in block
    assert 'ACTIVATION_LINEAR_COMMERCIAL_ROADS' not in html
    assert 'LINEAR_COMMERCIAL_MIN_WIDTH_M=8' in html
    assert 'LINEAR_COMMERCIAL_MAX_WIDTH_M=18' in html
    assert 'LINEAR_COMMERCIAL_MIN_ASPECT=3' in html
    changelog=(Path(__file__).resolve().parent / 'CHANGELOG_v2.5.0.txt').read_text(encoding='utf-8')
    assert '[v2.5.0-r19' in changelog



def check_r20_progress_truth_and_wide_scheme_facts() -> None:
    html=(Path(__file__).resolve().parent / "app.html").read_text(encoding="utf-8")
    assert "#siteDetail_schemeSpecific{grid-column:1/-1}" in html
    assert "#siteDetail_schemeSpecific #spSchemeFactList{display:grid;grid-template-columns:repeat(3" in html
    assert "가로구역 polygon/면적 미확보" in html
    assert "사용승인일 0동" in html
    assert "주변 도로 0건 · 후보 없음으로 확정하지 않음" in html
    assert "도로 인접관계·대상지 주변 동일 도로구간의 양측 후보" in html
    assert "const partial=steps.filter(x=>x.status==='partial')" in html



def check_r21_single_boundary_sequential_diagnostics() -> None:
    base=Path(__file__).resolve().parent
    html=(base / "app.html").read_text(encoding="utf-8")
    py=(base / "app.py").read_text(encoding="utf-8")
    # R22 correction: user-confirmed geometry is authoritative. Parcel union is reference-only.
    for marker in (
        "let analysisGeometry=null",
        "let boundaryReferenceGeometry=null",
        "function finalizeAnalysisGeometryFromSelectedParcels()",
        "function currentSpatialGeometry(){return activeGeometry;}",
        "source:'user_boundary'",
        "parcel_union_area_m2",
        "사용자 구역계는 변경하지 않습니다",
        "runAllAutoAnalyses({skipParcels:true})",
        "async function fetchBackendJson",
        "JSON 아님",
        "선행 ROAD_BT 미확보 · 분석 미실행",
        "ROAD_BT가 없거나 유효하지 않은 구간은 폭원을 추정하지 않고 REVIEW",
    ):
        assert marker in html, marker
    finalize=html[html.index("function finalizeAnalysisGeometryFromSelectedParcels()"):html.index("const parcelFeatureMap", html.index("function finalizeAnalysisGeometryFromSelectedParcels()"))]
    assert "activeGeometry=analysisGeometry" not in finalize
    assert "drawnItems.clearLayers()" not in finalize
    # Explicit user action remains the only parcel-union boundary replacement path.
    manual=html[html.index("function applySelectedParcelsAsBoundary()") : html.index("function ", html.index("function applySelectedParcelsAsBoundary()")+20)]
    assert "activeGeometry=merged.geometry" in manual
    # 대형 GIS는 같은 Promise.all 묶음에 넣지 않고 순차 await한다.
    auto=html[html.index("async function runAllAutoAnalyses(options={})"):html.index("// 구역계 입력 직후", html.index("async function runAllAutoAnalyses(options={})"))]
    assert "Promise.all([" in auto  # 토지대장/건축공간만 병렬
    assert "safeAnalysisStep('정비사업 GIS'" in auto
    assert "safeAnalysisStep('개발사업 GIS'" in auto
    assert auto.index("safeAnalysisStep('정비사업 GIS'") < auto.index("safeAnalysisStep('개발사업 GIS'")
    for marker in (
        "def _prototype_low_memory_mode()",
        "def _release_heavy_analysis_cache(kind: str)",
        '_release_heavy_analysis_cache("renewal")',
        '_release_heavy_analysis_cache("development")',
    ):
        assert marker in py, marker
    block=py[py.index('def analyze_street_block('):py.index('@app.post("/api/spatial/street-block")')] if 'def analyze_street_block(' in py else py
    assert "TL_SPRD_MANAGE ROAD_BT" in block



def check_r22_shared_conservation_and_collapsible_ui() -> None:
    base=Path(__file__).resolve().parent
    html=(base / "app.html").read_text(encoding="utf-8")
    py=(base / "app.py").read_text(encoding="utf-8")
    for marker in (
        'id="siteDetail_sharedConservation"',
        'id="ccSharedConservationMiniMap"',
        'id="siteDetail_planningFacility"',
        'id="ccPlanningFacilityMiniMap"',
        "async function analyzeSharedConservation()",
        "VWorld NED 토지이용계획 getLandUseAttr",
        "UFM120 도형으로 실제 교차면적·비율을 계산합니다",
        "function enableSpatialModuleCollapsing()",
        "function setAllSpatialModulesCollapsed(collapsed)",
        "status.classList.add('engine-data-panel')",
        "상생주택 보전환경",
    ):
        assert marker in html, marker
    # 상단 중복 카드 제거: 서비스 템플릿 구간에 카드 제목이 없어야 한다.
    block=html[html.index("main.innerHTML=`"):html.index("main.insertAdjacentHTML('beforeend'", html.index("main.innerHTML=`"))]
    assert '<b>도시계획 GIS</b>' not in block
    assert '<b>종합현황</b>' not in block
    assert 'repeat(4,minmax(0,1fr))' in html
    for marker in (
        'VWORLD_LAND_USE_URL = "https://api.vworld.kr/ned/data/getLandUseAttr"',
        'def _parse_land_use_xml',
        '@app.post("/api/spatial/land-use-restrictions")',
        '"geometry_basis": "parcel_attribute_only"',
        '"biotope_grade1"',
        '"public_interest_forest"',
    ):
        assert marker in py, marker



def check_r22_verified_cultural_layers_and_hill_disabled() -> None:
    base=Path(__file__).resolve().parent
    html=(base / "app.html").read_text(encoding="utf-8")
    py=(base / "app.py").read_text(encoding="utf-8")
    # Only verified public/current spatial sources participate in automatic long-term-jeonse checks.
    for marker in (
        "LT_C_UQ111", "LT_C_UQ121", "LT_C_UO301",
        "제1종전용주거", "제2종전용주거", "제1종일반주거",
        "역사문화특화경관지구", "국가유산보호구역",
        "구릉지 공개 SHP는 미확보이므로 자동판정하지 않음",
    ):
        assert marker in html, marker
    # Hill legacy adapter may remain for compatibility but must be disabled/not presented as a found source.
    assert '"hill_official_gis": "disabled_public_shp_not_found"' in py
    assert '"hill_official_file": None' in py
    assert "구릉지 원도형','정비사업 GIS" not in html
    import app
    app._hill_reference_data.cache_clear(); app._hill_spatial_index.cache_clear()
    status=app._hill_reference_data()['metadata']
    if not app._hill_zip_path():
        assert status['available'] is False
        sample={"type":"Polygon","coordinates":[[[126.97,37.56],[126.971,37.56],[126.971,37.561],[126.97,37.561],[126.97,37.56]]]}
        out=app.analyze_hill_intersections(sample)
        assert out['status']=='unavailable' and out['intersects'] is None



def check_r22_downtown_complex_type_split() -> None:
    base=Path(__file__).resolve().parent
    html=(base / "app.html").read_text(encoding="utf-8")
    # The two statutory types remain independent modules.
    assert 'data-scheme="innovation_growth"' in html
    assert 'data-scheme="innovation_housing"' in html
    assert 'data-scheme="innovation"' not in html
    assert 'innovation_type' not in html
    assert "innovation_growth:{id:'innovation_growth'" in html
    assert "innovation_housing:{id:'innovation_housing'" in html
    assert "SCHEME_MODULES.innovation" not in html
    # Growth core gates: location + 5,000m2 + apartment-complex limitation.
    for text in (
        '[중심지 + 폭20m 이상 간선도로] OR [비중심지 + 2개 이상 철도노선 환승역 500m 이내]',
        '제4조제1호', '제4조제2호', '제4조제3호',
        '중심지역 + 공개 GIS 접도기준 자동판정',
    ):
        assert text in html, text
    # Housing core gates: station coverage + age + area + apartment-complex limitation.
    for text in (
        '일반지역: 사업면적 과반 역세권 / 준공업: 사업면적 전체 역세권 + 공장건축물 비율 10% 미만',
        '20년 이상 건축물 60% 이상',
        '20,000㎡ 이상 60,000㎡ 이하',
        '제5조제1항제1호', '제5조제1항제2호', '제5조제1항제3호', '제5조제1항제4호',
    ):
        assert text in html, text
    # Factory ratio is an explicit platform precheck from building-register counts.
    assert 'function innovationFactoryBuildingRatio' in html
    assert '공장용도 건축물 수 ÷ 전체 건축물 수 × 100' in html
    assert '공장건축물 비율(초기검토)' in html
    # 350~500m is not an automatic pass in Seoul; mayoral recognition is still required.
    assert '350~500m 구간은 시장 인정 필요' in html
    # Common apartment-complex limitation is a mandatory core gate for both types.
    assert '각 공동주택단지 10,000㎡ 이하 AND 해당 단지면적이 사업구역 면적의 30% 이하' in html
    assert 'function innovationApartmentPrecheck' in html
    # Additional enforcement-rule suitability checks are separated from the core result.
    assert '핵심 사업추진조건 결과에는 미반영' in html
    assert '핵심 추진조건' in html and '사업추진조건 판정' in html

def check_r22_multi_station_fact_engine():
    html = Path(app.BASE_DIR, "app.html").read_text(encoding="utf-8")
    py = Path(app.BASE_DIR, "app.py").read_text(encoding="utf-8")
    for token in (
        "const STATION_SEARCH_RADIUS_M=1000",
        "const STATION_SAME_NAME_CLUSTER_M=400",
        "nearbyStations:[]",
        "function clusterStationFeatures(features,maxGapM=STATION_SAME_NAME_CLUSTER_M)",
        "same_name_cluster_count",
        "line_data_complete:lineDataComplete",
        "function activationStationCandidates()",
        "function stationBlockRelation(station,threshold)",
        "function streetBlockIsAuthoritative()",
        "function bestLongtermStationFact(c)",
        "bestStationByCoverage(350)",
        "transferCandidate=stationAnalysis.loaded?bestConfirmedTransferStation():null",
        "역세권활성화 공간대상에 포함되면 성장잠재권 활성화구역은 비활성화",
        "현재 내장 기초단위구/ROAD_BT 형상은 법정 가로구역이 아니므로 행안부/공식 가로구역 데이터 연결 전 자동 PASS·FAIL 금지",
        "future_function_interface:{field:'road_hierarchy'",
    ):
        assert token in html, token
    # 후보검색 1km는 판정기준이 아니라 수집범위이며, 개별 역의 250/350/500m 면적관계를 보존한다.
    assert "coverage250:m250.coverage_pct" in html and "coverage350:m350.coverage_pct" in html and "coverage500:m500.coverage_pct" in html
    assert "overlap250M2:m250.overlap_m2" in html and "full500:m500.full_containment" in html
    # 현 SGIS 기초단위구 자동추정은 법정 가로구역으로 승격하지 않는다. 향후 공식 데이터 인터페이스만 열어둔다.
    assert "'authoritative_street_block':False" in py
    assert "'future_street_block_interface':'MOIS_BASIC_UNIT_OR_VERIFIED_PLANNING_ROAD_BLOCK'" in py
    # 역세권활성화 간선가로형은 공식 가로구역 유무와 별개로 공개 GIS의 띠형 상업지역을 이진 판정한다.
    assert "shareKnown&&!blockAuthoritative" in html
    assert "arterial.linear_commercial===true?'PASS':'FAIL'" in html
    # 동명 이격역을 단순 역명으로 합쳐 거짓 환승역을 만들지 않는다.
    assert "function stationNameOnlyLineFactAllowed(group){return Number(group?.same_name_cluster_count||1)<=1;}" in html
    assert "const sameNameAmbiguous=!stationNameOnlyLineFactAllowed(group);" in html
    assert "let official=sameNameAmbiguous?null:" in html


def check_r22_station_rule_engine_v4():
    html = Path(app.BASE_DIR, "app.html").read_text(encoding="utf-8")
    py = Path(app.BASE_DIR, "app.py").read_text(encoding="utf-8")

    # 공통 후보역 엔진: 대상지 중심 1km 내 전 역 + 역별 거리/250/350/500 면적관계.
    for token in (
        "const STATION_SEARCH_RADIUS_M=1000",
        "stationCandidateRowsHtml",
        "판정역",
        "coverage250:m250.coverage_pct",
        "coverage350:m350.coverage_pct",
        "coverage500:m500.coverage_pct",
    ):
        assert token in html, token

    # 노선/환승 Fact: 서울 열린데이터광장 통합 참조 + 부역명 정규화.
    assert '/api/reference/station-lines' in py
    assert 'CardSubwayStatsNew' in py and 'SearchSTNBySubwayLineInfo' in py
    assert 'def _normalize_station_public_name' in py
    assert 'wangsimni_probe' in py
    assert "const lineDataComplete=!sameNameAmbiguous&&transferConfirmed" in html
    assert "const transferConfirmed=official?.transfer===true" in html

    # 역세권활성화: 거리 하나가 아니라 역별 적용반경 + 가로구역. 비공식 블록은 자동 PASS 금지.
    a0=html.index('function activationSpatialFacts(store)')
    a1=html.index('function growthPotentialSpatialFacts(store)',a0)
    activation=html[a0:a1]
    assert 'activationStationCandidates()' in activation
    assert 'block_authoritative' in activation
    assert '가로구역' in activation

    # 성장잠재권: 역세권활성화 공간대상 선행 배타 Gate.
    g0=html.index('function checkGrowthPotentialFromFacts(store,f)')
    g1=html.index('function safeHousingSpatialFacts(store)',g0)
    growth=html[g0:g1]
    assert '역세권활성화 공간대상에 포함되면 성장잠재권 활성화구역은 비활성화' in growth

    # 안심주택: 최근접 거리만으로 PASS 금지. 역별 대상지 면적 50% + 250/350 경로.
    # 350m 예외경로는 안심주택 전용 승강장+출입구 통합범위(safe350)를 쓴다.
    s0=html.index('function safeStationPath(c)')
    s1=html.index('function safeArterialPath',s0)
    safe=html[s0:s1]
    assert 'Number(x.coverage250)>=50' in safe
    assert 'Number(x.safe350_coverage)>=50' in safe
    assert 'bestSafe350(' in safe
    assert '50% 미만' in safe
    assert "distance_m<=250" not in safe

    # 역세권복합: 250m가 '가로구역'을 얼마나 포함하는지와 대상지의 가로구역 점유율을 별도 판정.
    c0=html.index('function stationComplexSpatialFacts(store)')
    c1=html.index('function moduleStrengthRisk',c0)
    complex_text=html[c0:c1]
    assert 'catchment_block_share_pct' in complex_text
    assert '사업대상지 가로구역 점유' in complex_text
    assert 'catchment_block_authoritative' in complex_text

    # 장기전세/도심공공주택복합/도심복합 주거중심형: 단일역별 포함률 사용.
    assert 'function bestLongtermStationFact(c)' in html
    assert 'bestStationByCoverage(350)' in html
    assert 'coverage350_pct>=50' in html
    assert 'coverage500_pct>=50' in html
    assert 'coverage350_pct>50' not in html
    assert 'coverage500_pct>50' not in html

    # 성장거점형: 1km 전 역에서 2개 이상 철도노선 환승역을 골라 500m 판정.
    assert 'bestConfirmedTransferStation(500)' in html
    assert 'function bestConfirmedTransferStation(maxDistanceM=null)' in html
    assert 'transfer_candidate' in html
    assert '환승결절 판정역' in html

    # 소규모재개발/정비사업 역세권 특례도 다중역 Fact를 사용하고, c.dist는 fallback에만 남긴다.
    sm0=html.index('function smallscaleSpatialFacts(store)')
    sm1=html.index('function checkSmallscaleFromFacts',sm0)
    small=html[sm0:sm1]
    assert 'smallRedevelopmentStation=stationAnalysis.loaded?bestStationByDistance' in small
    up0=html.index('function stationRenewalUpzone(c,name)')
    up1=html.index('function activationContribution',up0)
    up=html[up0:up1]
    assert 'candidate=stationAnalysis.loaded?bestStationByDistance()' in up


def check_factory_usage_common_fact() -> None:
    """공장용도 현황: 공통 Fact(store.site.factory_usage) → 역세권 장기전세·도심복합 주거중심형이
    공유 참조하는 구조. 산식은 '공장용도 건축물 수 ÷ 전체 건축물 수 × 100'(동수 기준) 고정이며,
    공간현황 모듈 자체는 10% 충족/미충족 같은 사업판정을 하지 않는다.
    """
    html = Path(app.BASE_DIR, "app.html").read_text(encoding="utf-8")

    # main_use/other_use 기준 공장 판정 정규식은 isFactoryBuildingRecord() 하나만 있어야 한다.
    # (다른 곳의 /공장/.test(text) 는 무관한 용도의 항목명→근거ID 매핑이라 여기서는 안 본다.)
    assert html.count("/공장/.test(`${r") == 1, "isFactoryBuildingRecord 밖에서 main_use/other_use 공장 판정 정규식이 중복 작성됨"
    assert "function isFactoryBuildingRecord(r)" in html

    # 공통 Fact 산식 확인 — 동수 기준 고정, 연면적/대지면적 비율로 바뀌지 않았는지.
    fu0 = html.index("function computeFactoryUsageFact(records,buildingFeatures,manualPct)")
    fu1 = html.index("function factoryUsageAsSchemeFact", fu0)
    fu_block = html[fu0:fu1]
    assert "isFactoryBuildingRecord" in fu_block
    assert "factoryCount/total*100" in fu_block
    assert "total_floor_area_m2" not in fu_block
    assert "plat_area_m2" not in fu_block
    assert "calculation_basis:'BUILDING_COUNT'" in fu_block

    # polygon 매칭: 관리번호 확정 매칭 또는 필지 내 1:1일 때만 매칭, 그 외엔 unmatched/ambiguous로 남긴다
    # (특정 건물을 임의로 공장이라 확정하지 않는다).
    mt0 = html.index("function matchFactoryBuildingFeatures(factoryRecords,buildingFeatures)")
    mt1 = html.index("function computeFactoryUsageFact", mt0)
    match_block = html[mt0:mt1]
    assert "MGM_BLDRGST_PK" in match_block
    assert "PARCEL_SINGLE_BUILDING" in match_block
    assert "unmatched.push(r.id)" in match_block
    assert "ambiguousParcels.add(pnu)" in match_block

    # buildSiteFactStore가 공통 Fact를 만들어 site.factory_usage에 싣는다.
    store0 = html.index("function buildSiteFactStore()")
    store1 = html.index("function activationSpatialFacts(store)", store0)
    store_block = html[store0:store1]
    assert "factory_usage:computeFactoryUsageFact(buildingRecords,currentBuildingFeatures,c.factory)" in store_block

    # 역세권 장기전세: c.factory 단독이 아니라 store.site.factory_usage를 우선 참조.
    lt0 = html.index("function longtermSpatialFacts(store)")
    lt1 = html.index("function checkLongtermFromFacts", lt0)
    longterm_block = html[lt0:lt1]
    assert "store.site.factory_usage" in longterm_block
    assert "factoryUsage?.factory_ratio_pct" in longterm_block
    assert "factoryUsage?.mismatch" in longterm_block
    assert "factoryPct<10" in longterm_block

    # 도심복합개발 주거중심형: innovationFactoryBuildingRatio()를 별도로 다시 계산하지 않고
    # 동일한 factoryUsageAsSchemeFact(store.site.factory_usage) 어댑터를 쓴다.
    inv0 = html.index("function innovationSpatialFacts(store,typ)")
    inv1 = html.index("function checkInnovationFromFacts", inv0)
    inv_block = html[inv0:inv1]
    assert "factoryUsageAsSchemeFact(store.site.factory_usage)" in inv_block
    assert "innovationFactoryBuildingRatio(records" not in inv_block

    chk0 = html.index("function checkInnovationFromFacts(store,f)")
    chk1 = html.index("function urbanRedevelopmentSpatialFacts", chk0)
    chk_block = html[chk0:chk1]
    assert "f.zoning.factory_pct<10" in chk_block
    assert "factoryMismatch" in chk_block

    # 성장거점형(growth) 분기에는 공장비율 조건을 억지로 추가하지 않는다.
    growth_branch_start = chk_block.index("if(f.type==='growth'){")
    growth_branch_end = chk_block.index("}else{", growth_branch_start)
    growth_branch = chk_block[growth_branch_start:growth_branch_end]
    assert "factory" not in growth_branch.lower()

    # 공간현황 모듈 자체는 10% 판정을 하지 않는다 — Fact(비율·건수)만 표시.
    render0 = html.index("function renderFactoryUsageSpatialStatus()")
    render1 = html.index("function renderZoningSpatialStatus()", render0)
    render_block = html[render0:render1]
    assert "<10" not in render_block and ">=10" not in render_block and "FAIL" not in render_block

    # 수기입력(scheme_factory_ratio)은 삭제하지 않고 보정용으로 남긴다.
    assert 'id="scheme_factory_ratio"' in html



def check_safe_housing_popup_always_opens() -> None:
    """선택사항과 무관하게 안심주택도 공통 경로로 기초결과검토서를 표시한다."""
    html = Path(app.BASE_DIR, "app.html").read_text(encoding="utf-8")
    assert "function openSafeHousingDetailSafely()" not in html
    show0 = html.index("function showCandidateBasis(name)")
    show1 = html.index("function scrollToSchemeDetail", show0)
    show = html[show0:show1]
    assert "openSafeHousingDetailSafely" not in show
    assert "openSchemeDetailSafely(name);" in show
    opener0 = html.index("function openSchemeDetailSafely(name)")
    opener1 = html.index("function showSmallscaleRouteBasis", opener0)
    opener = html[opener0:opener1]
    assert "openReviewModal('schemeDetailModal')" in opener
    assert "try{renderSchemeDetailPopup(name);}" in opener
    assert "renderSchemePopupFallback(name,e)" in opener
    router0 = html.index("function renderSchemeDetailPopup(name)")
    router1 = html.index("function renderSchemeComparePopup()", router0)
    assert "if(name==='safe'){renderSafeHousingDetailPopup();appendBaseReviewDisclaimer(name);return;}" in html[router0:router1]
    safe0 = html.index("function renderSafeHousingDetailPopup()")
    safe1 = html.index("function renderSharedHousingDetailPopup()", safe0)
    safe = html[safe0:safe1]
    assert "latestSiteFactStore||analysisState.fact_store||null" in safe
    assert "buildSiteFactStore()" not in safe
    assert "현황분석 필요" in safe
    assert "결과 정교화 선택사항" in safe
    assert "현재 선택값과 무관하게 아래 기초결과검토서는 정상 표시합니다." in safe
    assert "if(res?.purpose_disabled)" not in safe

def check_safe_housing_entrance_350() -> None:
    """안심주택 350m 예외경로 전용 승강장+출입구 통합범위 판정.

    250m 일반경로는 손대지 않았고, 다른 사업방식(역세권활성화·장기전세·역세권복합·
    도심공공주택복합·도심복합개발 등)의 station 판정 로직은 이 기능과 완전히
    분리되어 있어야 한다(같은 안심주택 파일 안에서만 safeStation350Geometry /
    safeEntranceFeaturesForStation / bestSafe350을 참조해야 한다).
    """
    html = Path(app.BASE_DIR, "app.html").read_text(encoding="utf-8")
    py = Path(app.BASE_DIR, "app.py").read_text(encoding="utf-8")

    # 신규 함수 존재 확인
    for token in (
        "function safeEntranceFeaturesForStation(stationFact)",
        "function safeStation350Geometry(stationFact)",
        "function coverageOfSite(siteGeometry,unionGeometry)",
        "function safeStation350Facts()",
        "function bestSafe350(predicate=()=>true)",
        "let stationEntranceMap=new Map()",
        "async function loadStationEntrances(force=false)",
    ):
        assert token in html, token
    assert "await loadStationEntrances(force)" in html
    assert "let stationEntranceReferenceStatus=" in html

    # 출입구는 반드시 해당 역과 "공식 연결"된 것만 쓴다 — 역명 재정규화로 재매칭하지 않는다.
    ent0 = html.index("function safeEntranceFeaturesForStation(stationFact)")
    ent1 = html.index("function safeStation350Geometry", ent0)
    ent_block = html[ent0:ent1]
    assert "stationEntranceMap.get(stationFact?.name)" in ent_block
    assert "stationNameKey" not in ent_block  # 이름 정규화 재매칭 금지

    # 350m geometry는 승강장 경계 buffers[350] ∪ 연결된 출입구 350m 버퍼.
    geo0 = html.index("function safeStation350Geometry(stationFact)")
    geo1 = html.index("function coverageOfSite", geo0)
    geo_block = html[geo0:geo1]
    assert "stationFact?.buffers?.[350]" in geo_block
    assert "safeEntranceFeaturesForStation(stationFact)" in geo_block
    assert "turf.buffer(e,350" in geo_block

    # 250m 일반경로는 원래 함수·우선순위 그대로. 350m만 안심주택 전용 안전판정으로 교체.
    s0 = html.index("function safeStationPath(c)")
    s1 = html.index("function safeArterialPath", s0)
    safe = html[s0:s1]
    assert safe.index("eligible250") < safe.index("eligible350")
    assert safe.index("eligible350") < safe.index("overlap350")
    assert "eligible250||overlap250||eligible350||overlap350||bestStationByDistance()" in safe
    # 350m에서 출입구가 없어도(연결된 출입구 0개) 승강장 경계만으로는 REVIEW로 남기지,
    # 이 함수 안에서 FAIL 반환 경로를 새로 늘리지 않는다(원래도 최종 fallback 하나뿐).
    assert safe.count("status:'FAIL'") == 2  # 'status:'FAIL'' 은 'coverage_status:'FAIL'' 안에도 부분일치하므로 2
    assert "!stationEntranceReferenceStatus.loaded||!stationEntranceReferenceStatus.linkage_complete" in safe
    assert "누락된 해당 역 출입구 가능성이 있어 FAIL 확정 금지" in safe

    # 다른 사업방식(다른 station rule)은 안심주택 전용 함수를 절대 참조하지 않는다 — 완전 분리 확인.
    other_module_markers = [
        ("function activationSpatialFacts(store)", "function growthPotentialSpatialFacts(store)"),
        ("function stationComplexSpatialFacts(store)", "function moduleStrengthRisk"),
        ("function longtermSpatialFacts(store)", "function checkLongtermFromFacts"),
        ("function publicComplexSpatialFacts(store)", "function checkPublicComplexFromFacts"),
        ("function innovationSpatialFacts(store,typ)", "function checkInnovationFromFacts"),
    ]
    for start_marker, end_marker in other_module_markers:
        if start_marker not in html:
            continue
        b0 = html.index(start_marker)
        b1 = html.index(end_marker, b0)
        block = html[b0:b1]
        assert "safeStation350Geometry" not in block, start_marker
        assert "safeEntranceFeaturesForStation" not in block, start_marker
        assert "bestSafe350(" not in block, start_marker

    # 공간현황 UI: 350m 예외경로 상세(출입구 연결 개수 포함) row가 별도로 있어야 한다.
    fact0 = html.index("function safeHousingSpatialFacts(store)")
    fact1 = html.index("function checkSafeFromFacts(store,f)", fact0)
    fact_block = html[fact0:fact1]
    assert "역세권 350m 예외경로 상세" in fact_block
    assert "station.entrance_count" in fact_block
    for label in (
        "안심주택 250m 기준 geometry", "안심주택 250m 포함률", "안심주택 350m 예외범위",
        "안심주택 출입구 연결", "안심주택 350m 예외 포함률", "안심주택 역세권 판정",
    ):
        assert label in fact_block

    # 백엔드: 출입구 참조 데이터 로더 + 엔드포인트.
    assert "def _station_entrance_reference_data()" in py
    assert '@app.get("/api/reference/station-entrances")' in py
    endpoint = app.reference_station_entrances()
    assert endpoint["metadata"]["linkage_complete"] is False
    assert endpoint["metadata"]["official_relation_key"] is False
    assert endpoint["metadata"]["matched_entrance_count"] > 0
    assert isinstance(endpoint["stations"], dict) and endpoint["stations"]

    # 데이터 품질: 서버 전처리 결과(station_entrances.json)가 실제로 존재하고,
    # 같은 출입구가 두 역에 동시에 배정되지 않았는지(= 다른 역 소속 출입구를 섞어쓰지 않았는지),
    # 그리고 애매한 출입구는 실제로 제외되어 매칭 개수가 원본보다 적은지 확인한다.
    entrance_path = Path(app.BASE_DIR, "station_entrances.json")
    assert entrance_path.is_file()
    with entrance_path.open(encoding="utf-8") as fp:
        entrance_data = json.load(fp)
    assert isinstance(entrance_data, dict) and len(entrance_data) > 0
    seen_sub_ent_sn = set()
    total_matched = 0
    for station_name, entries in entrance_data.items():
        assert isinstance(entries, list) and len(entries) > 0
        for e in entries:
            assert "lon" in e and "lat" in e
            key = e.get("entrance_id")
            assert key, "entrance_id missing"
            assert key not in seen_sub_ent_sn, f"entrance {key} assigned to multiple stations"
            seen_sub_ent_sn.add(key)
            total_matched += 1
    assert total_matched < 1743  # 원본 출입구 1743건 중 애매한 것들은 실제로 빠졌어야 한다



def check_r22_growth_frontage_engine():
    html = Path(app.BASE_DIR, "app.html").read_text(encoding="utf-8")
    py = Path(app.BASE_DIR, "app.py").read_text(encoding="utf-8")
    # Growth-node area remains an exact project-boundary fact; road hierarchy is deliberately deferred.
    for token in (
        "function growthInnovationAreaFact(areaM2)",
        "status:n>=5000?'PASS':'FAIL'",
        "source:'USER_CONFIRMED_BOUNDARY'",
        "function growthInnovationRoadFactFromRecords(records,siteSummary={})",
        "const touchingSegments=[];",
        "const segContact=frontageStats(site,[x.road]);",
        "hierarchy_verification:'DEFERRED_USER_DATA'",
        "const ordered20=rs.filter(r=>Number(r.width_m)>=20",
        "const second=selected20?(rs.filter(r=>r.road_id!==selected20.road_id&&Number(r.width_m)>=8",
        "hierarchy_status:'WAITING_USER_DATA'",
        "selected_20m:selected20",
        "간선도로 위계·특별시도 여부는 사용자 제공자료 연결 전 판정하지 않음",
        "future_function_interface:{field:'road_hierarchy'",
        "2026-09-02-r22-station-area-frontage-no-hierarchy",
    ):
        assert token in html, token
    # Automatic interpretation of address-road hierarchy codes must be disabled until user-provided hierarchy data is connected.
    road_attr = html[html.index("function roadAddressAttributeFacts(props)"):html.index("function roadFunctionIsArterial", html.index("function roadAddressAttributeFacts(props)"))]
    assert "functional_arterial_confirmed:false" in road_attr
    assert "special_city_road_explicit:false" in road_attr
    assert "ROA_CLS_SE/WDR_RD_CD 등은 판정에 사용하지 않는다" in road_attr
    assert "normalizeRoadHierarchyCode" not in html
    assert "normalizeWideRoadCode" not in html
    assert "explicitSpecialCityRoadValue" not in html
    # 성장거점형은 공개 GIS 폭원·접도 결과를 이진 판정하고 도로위계는 보고서 단서로 남긴다.
    c0=html.index("function checkInnovationFromFacts(store,f)")
    c1=html.index("function urbanRedevelopmentSpatialFacts(store)", c0)
    block=html[c0:c1]
    assert "rows.push(schemeRow('접도'" in block
    assert "loc=gr?.status==='CONFIRMED'?'PASS':'FAIL'" in block
    assert "centerRoadRequired?(gr.status==='CONFIRMED'?'PASS':'FAIL'):'INFO'" in block
    assert "비중심지 환승결절 500m 경로에는 중심지 접도기준을 중복 적용하지 않음" in block
    assert 'APP_BUILD_MARKER = "R31_SAFE_MEDICAL_PERFORMANCE_MERGED_20260903"' in py
    assert '"scheme_module_api": "2026-09-02-r22-station-area-frontage-no-hierarchy"' in py


def check_biotope_bundled_exact_fact() -> None:
    """비오톱1등급은 내장 SHP 실제 교차 Fact이며 PNU/NED 실패와 독립되어야 한다."""
    root=Path(app.BASE_DIR)
    html=Path(app.STATIC_HTML_PATH).read_text(encoding="utf-8")
    py=Path(root,"app.py").read_text(encoding="utf-8")
    assert (root / "biotope_seoul.zip").is_file() and (root / "biotope_seoul.zip").stat().st_size > 1_000_000
    for marker in (
        "function computeBiotopeFactFromBundled(payload)",
        "'/api/spatial/biotope-intersections'",
        "서울시 개별비오톱(2025 기준) 원본 SHP",
        "geometry_basis:'EXACT_SHP'",
        "선택필지 PNU 미확보 · 실패한 원도형의 필지조회 fallback 생략",
        "if(!biotopeExact)",
        "if(a.biotope?.features?.length)ccSharedBiotopeParcels.addData",
    ):
        assert marker in html, marker
    conserve=html[html.index("async function analyzeSharedConservation()") : html.index("function renderPlanningFacilitySpatialStatus()")]
    assert conserve.index("biotope-intersections") < conserve.index("land-use-restrictions")
    assert "throw new Error('선택필지 PNU 미확보')" not in conserve
    assert "sharedConservationAnalysis.biotope={known:false" in conserve
    for marker in (
        "def _biotope_zip_path()", "def _biotope_spatial_layers()", "def analyze_biotope_intersections(",
        '@app.get("/api/spatial/biotope-data-status")', '@app.post("/api/spatial/biotope-intersections")',
        '"status": "matched" if clipped_rows else "none"',
        '"grade_basis": "유형평가 또는 개별평가 중 하나라도 1등급"',
    ):
        assert marker in py, marker


def check_public_forest_bundled_exact_fact() -> None:
    """UF801은 UFM120/110을 분리하며 공익용산지를 구역계와 실제 교차해야 한다."""
    root=Path(app.BASE_DIR)
    html=Path(app.STATIC_HTML_PATH).read_text(encoding="utf-8")
    py=Path(root,"app.py").read_text(encoding="utf-8")
    forest_zip=root / "forest_classification_seoul_202608.zip"
    assert forest_zip.is_file() and forest_zip.stat().st_size > 1_000_000
    for marker in (
        "function computePublicForestFactFromBundled(payload)",
        "'/api/spatial/forest-classification-intersections'",
        "UFM120 도형으로 실제 교차면적·비율",
        "if(a.publicForest?.features?.length)ccSharedForestParcels.addData",
        "forest.area_m2!=null",
    ):
        assert marker in html, marker
    conserve=html[html.index("async function analyzeSharedConservation()") : html.index("function renderPlanningFacilitySpatialStatus()")]
    assert conserve.index("forest-classification-intersections") < conserve.index("land-use-restrictions")
    assert "if(!forestExact)" in conserve
    for marker in (
        "def _forest_classification_zip_path()", "def _forest_class_from_properties(",
        "def _forest_classification_spatial_layers()", "def analyze_forest_classification_intersections(",
        '@app.get("/api/spatial/forest-classification-data-status")',
        '@app.post("/api/spatial/forest-classification-intersections")',
        '"120": "public_interest_forest"', '"110": "forestry_forest"',
        '"classification_basis": "MNUM UFM120=공익용산지, UFM110=임업용산지"',
    ):
        assert marker in py, marker


def check_r24_candidate_feasibility_density_ui() -> None:
    """사업카드 색은 추진가능성, 명도는 계획가능용적률만 표현해야 한다."""
    html=Path(app.STATIC_HTML_PATH).read_text(encoding="utf-8")
    for marker in (
        "현재 추진 가능", "조건변경 후 가능", "색이 진할수록 계획가능용적률이 높음",
        "function candidateDisplayState(name,st=safeCandidateState(name))",
        "function candidateChangeOpportunity(name,st)",
        "function candidateOneYearAgeOpportunity(name)",
        "사업시행자·토지확보 구조 변경", "인접 노후건축물 편입 검토",
        "display.kind==='available'?'현재 추진 가능'",
        "display_state:x.display?.kind||'pending'",
        "available:decisions.filter(x=>x.display_state==='available').length",
    ):
        assert marker in html, marker
    density=html[html.index("function densityPotentialForScheme(name,st=null)"):html.index("function densityTierLabel(tier)")]
    assert "maxFar>=600?4:maxFar>=400?3:maxFar>=250?2:1" in density
    assert "색상 농도에는 역세권·공공기여를 반영하지 않음" in density
    assert "stationInfluenceForScheme" not in density
    compare=html[html.index("function compareCandidateNames(a,b)"):html.index("function rankedSchemeNames()")]
    assert "['finalRank','densityTier','purposeRank'" in compare
    opportunity=html[html.index("function candidateChangeOpportunity(name,st)"):html.index("function candidateDisplayState(name,st=safeCandidateState(name))")]
    assert "st.purposeGate==='off'||st.structural==='FAIL'||st.stage==='LEGAL_ENTRY'" in opportunity
    assert "개발제한|공익용산지|비오톱|문화재|군사" in opportunity

def check_three_legal_road_groups() -> None:
    html = Path(app.BASE_DIR, "app.html").read_text(encoding="utf-8")
    for marker in (
        'id="urbanRenewalFrontageSummary"', 'id="smallscaleBlockSummary"',
        'id="frontageSchemeSummary"', 'id="widthRoadSchemeSummary"',
        "const URBAN_RENEWAL_FRONTAGE_SCHEME_KEYS=['redevelopment','residential_environment']",
        "const STATION_SPECIAL_FRONTAGE_SCHEME_KEYS=['activation','station_complex','public_complex']",
        "const WIDTH_ROAD_SCHEME_KEYS=['safe','growth','longterm','innovation_growth','innovation_housing']",
    ):
        assert marker in html, marker
    # 소정법 현황표는 면적·노후도·주택수까지 합친 routes.block 전체판정을 쓰지 않는다.
    start=html.index("function renderSmallscaleBlockSummary()")
    end=html.index("function renderWidthRoadSchemeSummary()",start)
    block=html[start:end]
    assert ".street_block" in block
    assert "street_status" in block and "through_road_status" in block
    assert "routes?.block" not in block and "routes.block" not in block
    assert "통과도로 요건 적용 제외" in block
    # 도정법 두 사업은 동일한 6m 산식 입력을 사용하고 기준비율만 40%/20%로 분리한다.
    frontage=html[html.index("function schemeFrontageEvidenceFacts(cArg=null)"):html.index("function schemeRoadEvidenceFacts(")]
    assert "frontage_access_buildings_6m" in frontage
    assert "frontage_access_buildings_4m))?Number(net.frontage_access_buildings_4m)" not in frontage
    assert "40% 이하" in frontage and "20% 이하" in frontage


def main() -> None:
    _run("measurement", check_measurement)
    _run("renewal spatial", check_renewal_server_intersection)
    _release_heavy_spatial_cache("renewal")
    _run("development spatial", check_development_server_intersection)
    _release_heavy_spatial_cache("development")
    _run("redevelopment area gate", check_area_gate)
    _run("redevelopment boolean gate", check_redevelopment_boolean_gate)
    _run("centerline width buffer", check_centerline_width_buffer)
    _run("bundled road dataset", check_bundled_road_dataset)
    _run("age annotation reference", check_age_annotation_reference_only)
    _run("feedback + UI", check_feedback_and_ui)
    _run("four independent modules", check_four_independent_scheme_modules)
    _run("next four independent modules", check_next_four_independent_modules)
    _run("legacy engine purge", test_migrated_scheme_legacy_engines_removed)
    _run("dedicated detail popups", check_dedicated_detail_popups)
    _run("startup drawing + legacy UI", check_startup_drawing_and_legacy_ui)
    _run("progress truth + wide scheme facts", check_r20_progress_truth_and_wide_scheme_facts)
    _run("r21 single boundary + sequential diagnostics", check_r21_single_boundary_sequential_diagnostics)
    _run("r22 shared conservation + collapsible ui", check_r22_shared_conservation_and_collapsible_ui)
    _run("r22 verified cultural layers + hill disabled", check_r22_verified_cultural_layers_and_hill_disabled)
    _run("r22 downtown complex type split", check_r22_downtown_complex_type_split)
    _run("spatial evidence maps", check_spatial_evidence_maps)
    _run("safe medical api adapter", check_safe_medical_api_adapter)
    _run("safe medical boundary resolution", check_safe_medical_boundary_resolution)
    _run("purpose filter + frontage facts", check_purpose_filter_and_frontage_facts)
    _run("remaining four + sources", check_remaining_four_independent_modules_and_sources)
    _run("scheme family separation", check_scheme_family_separation)
    _run("r8 boundary + map + smallscale + prior", check_r8_boundary_map_smallscale_prior)
    _run("r9 refinement placement", check_r9_refinement_placement)
    _run("r10 scheme fail-safe", check_r10_scheme_fail_safe)
    _run("r11 popup + spatial + progress", check_r11_popup_spatial_progress)
    _run("r11 data recovery fix1", check_r11_data_recovery_fix1)
    _run("r13 criterion layer1", check_r13_criterion_layer1)
    _run("r14 street block auto", check_r14_street_block_auto)
    _run("r15 street block 4m conditional", check_r15_street_block_4m_conditional)
    _run("r16 basic unit street block", check_r16_basic_unit_street_block)
    _run("r17 spatial relation road facts", check_r17_spatial_relation_road_facts)
    _run("r18 bundled basic unit + frontage caveat", check_r18_bundled_basic_unit_and_frontage_caveat)
    _run("r19 activation arterial linear commercial", check_r19_activation_arterial_linear_commercial)
    _run("r22 multi-station fact engine", check_r22_multi_station_fact_engine)
    _run("r22 station rule engine v4", check_r22_station_rule_engine_v4)
    _run("safe housing popup always opens", check_safe_housing_popup_always_opens)
    _run("safe housing entrance 350m", check_safe_housing_entrance_350)
    _run("factory usage common fact", check_factory_usage_common_fact)
    _run("biotope bundled exact fact", check_biotope_bundled_exact_fact)
    _run("public forest bundled exact fact", check_public_forest_bundled_exact_fact)
    _run("r24 candidate feasibility density ui", check_r24_candidate_feasibility_density_ui)
    _run("r22 growth frontage engine", check_r22_growth_frontage_engine)
    _run("three legal road groups", check_three_legal_road_groups)
    _run("release files", check_release_files)
    print("v2.5.0 regression checks: PASS")



def test_migrated_scheme_legacy_engines_removed():
    html = Path(app.BASE_DIR, "app.html").read_text(encoding="utf-8")
    # 모든 구형 사업 판정엔진은 삭제한다. 16개는 독립모듈, 공간혁신 3종만 shell-only다.
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
    assert "const SHELL_SCHEMES=new Set(['urban_innovation_zone','facility_complex_zone','mixed_use_zone'])" in html
    assert "if(SHELL_SCHEMES.has(name))" in html
    assert "state:'neutral',label:'추후보완예정',rank:0,stage:'SHELL'" in html
    assert "현재 자동 활성화·추천·우선순위 미반영" in html
    assert "독립모듈 16개를 실제 판정한다. 소규모주택정비는 5개 사용자 검토경로를 1개 Family 모듈에서 비교하고, 공간혁신 3종은 shell로 유지한다." in html
    for key in ["redevelopment", "reconstruction", "residential_environment", "general_housing", "smallscale", "prior_negotiation", "activation", "growth_potential", "safe", "shared_housing", "station_complex", "longterm", "public_complex", "innovation_growth", "innovation_housing", "urban_redevelopment"]:
        assert f"{key}:{{" in html or f"{key}: {{" in html, f"independent module {key} missing"

if __name__ == "__main__":
    main()
