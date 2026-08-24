from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from pyproj import Geod
from shapely.geometry import shape, mapping
from shapely.validation import explain_validity

# ============================================================
# 도시검토 플랫폼 Web MVP v0.3.2
# - GitHub 업로드 실수를 줄이기 위한 "단일 app.py" 배포판
# - 서울 주택정비형 재개발 1차 Rule Engine
# - 웹 지도 Polygon 면적 자동계산
# 기준일: 2026-08-24
# ============================================================

RULES = {'rule_set_id': 'seoul_housing_redevelopment_2026_08_v02', 'title': '서울 주택정비형 재개발 1차 입안대상 판정', 'scope': '서울특별시 주택정비형 재개발사업 정비계획 입안대상지역 1차 스크리닝', 'as_of': '2026-08-24', 'thresholds': {'area_normal_m2': 10000, 'area_exception_m2': 5000, 'old_building_count_ratio': 0.6, 'old_building_count_ratio_promotion_district': 0.5, 'old_building_count_deemed_selection_ratio': 0.75, 'small_parcel_ratio': 0.4, 'housing_road_access_ratio': 0.4, 'house_density_per_ha': 60, 'old_floor_area_ratio': 0.6, 'old_floor_area_ratio_promotion_district': 0.5, 'request_owner_consent_ratio': 0.3, 'proposal_owner_consent_ratio': 0.6, 'proposal_land_area_consent_ratio': 0.5}, 'policy_watch': [{'id': 'OLD_COUNT_DEEMED_70_WATCH', 'status': 'UNVERIFIED_NOT_ACTIVE', 'current': '노후·불량건축물 수 75% 이상이면 조례상 추가요건을 갖춘 것으로 보는 간주규정', 'possible_future': '70% 완화 가능성 언급이 있어 향후 시행령 개정 여부 추적 필요', 'engine_behavior': '현행 75%만 적용. 법령 공포·시행 전에는 70%를 판정에 사용하지 않음'}], 'sources': [{'id': 'ENFORCEMENT_DECREE_APPENDIX1', 'title': '도시 및 주거환경정비법 시행령 제7조제1항 별표 1', 'url': 'https://www.law.go.kr/lsInfoP.do?lsId=009521', 'note': '재개발 정비계획 입안대상지역 기본요건 및 노후·불량건축물 75% 간주규정'}, {'id': 'SEOUL_ORDINANCE_ART2_5', 'title': '서울특별시 도시 및 주거환경정비 조례 제2조제5호', 'url': 'https://law.go.kr/LSW/ordinInfoP.do?ordinSeq=2130189', 'note': '호수밀도 정의 및 유형별 산정기준'}, {'id': 'SEOUL_ORDINANCE_ART6', 'title': '서울특별시 도시 및 주거환경정비 조례 제6조', 'url': 'https://law.go.kr/LSW/ordinInfoP.do?ordinSeq=2130189', 'note': '주택정비형 재개발 면적·노후도·과소필지·주택접도율·호수밀도 요건'}, {'id': 'SEOUL_ORDINANCE_ART9_2', 'title': '서울특별시 도시 및 주거환경정비 조례 제9조의2', 'url': 'https://law.go.kr/LSW/ordinInfoP.do?ordinSeq=2130189', 'note': '정비계획 입안요청 동의비율'}, {'id': 'SEOUL_ORDINANCE_ART10', 'title': '서울특별시 도시 및 주거환경정비 조례 제10조', 'url': 'https://law.go.kr/LSW/ordinInfoP.do?ordinSeq=2130189', 'note': '정비계획 입안제안 동의요건'}]}

@dataclass
class Check:
    id: str
    group: str
    label: str
    requirement: str
    actual: Optional[str]
    status: str  # PASS, FAIL, UNKNOWN, INFO
    source_ids: List[str]
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _load_rules() -> Dict[str, Any]:
    # 규칙은 단일 파일 배포를 위해 코드에 내장되어 있습니다.
    return RULES


def _ratio(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _pct(v: Optional[float]) -> Optional[str]:
    return None if v is None else f"{v * 100:.1f}%"


def _check_ge(check_id: str, group: str, label: str, actual: Optional[float], threshold: float,
              unit: str, source_ids: List[str], note: str = "") -> Check:
    if actual is None:
        return Check(check_id, group, label, f">= {threshold:g}{unit}", None, "UNKNOWN", source_ids, note)
    return Check(check_id, group, label, f">= {threshold:g}{unit}", f"{actual:g}{unit}",
                 "PASS" if actual >= threshold else "FAIL", source_ids, note)


def calculate_house_density(detail: Dict[str, Any]) -> Dict[str, Any]:
    """서울시 조례 제2조제5호에 따른 호수밀도 계산 보조엔진.

    건축물 레코드 type:
      - single_house: 일반 단독주택, count만큼 1동씩
      - multiunit_or_multifamily: 공동주택/다가구. 건축물대장상 세대(가구)수가 가장 많은 층의 세대수를 동수로 환산
      - specific_unauthorized: 특정무허가건축물, 포함
      - new_unauthorized: 신발생무허가건축물, 제외
      - converted_single_to_multi: 준공 후 단독→다세대/다가구 변경, 변경 전 동수(통상 1동)로 산정
      - non_residential: 비주거용건축물, 건축면적 90㎡당 1동(소수점 버림)
      - factory: 준공업지역에서 재배치 필요로 exclude_for_relocation=true면 건축물 동수에서 제외

    면적 분모에서는 존치공원, 사업완료공원, 존치학교와 재배치 대상 공장용지를 제외한다.
    """
    area_m2 = float(detail.get("area_m2") or 0)
    retained_park = float(detail.get("retained_park_area_m2") or 0)
    completed_park = float(detail.get("completed_park_area_m2") or 0)
    retained_school = float(detail.get("retained_school_area_m2") or 0)
    excluded_factory_land = float(detail.get("excluded_factory_land_area_m2") or 0)

    effective_area = area_m2 - retained_park - completed_park - retained_school - excluded_factory_land
    if effective_area <= 0:
        return {
            "status": "ERROR",
            "message": "호수밀도 산정 유효면적이 0 이하입니다.",
            "effective_area_m2": effective_area,
            "equivalent_building_count": None,
            "house_density_per_ha": None,
            "breakdown": []
        }

    equivalent = 0
    breakdown: List[Dict[str, Any]] = []

    for idx, b in enumerate(detail.get("buildings") or [], start=1):
        btype = b.get("type")
        count = int(b.get("count") or 1)
        added = 0
        rule = ""

        if btype == "single_house":
            added = count
            rule = "단독주택: 건축물 1동을 1동으로 산정"
        elif btype == "multiunit_or_multifamily":
            max_households = int(b.get("max_households_on_any_floor") or 0)
            added = max_households * count
            rule = "공동주택·다가구: 세대(가구)수가 가장 많은 층의 세대수를 동수로 환산"
        elif btype == "specific_unauthorized":
            added = count
            rule = "특정무허가건축물: 포함"
        elif btype == "new_unauthorized":
            added = 0
            rule = "신발생무허가건축물: 제외"
        elif btype == "converted_single_to_multi":
            pre_count = int(b.get("pre_conversion_building_count") or 1)
            added = pre_count * count
            rule = "단독→다세대/다가구 변경: 변경 전 건축물 동수 적용"
        elif btype == "non_residential":
            building_area = float(b.get("building_area_m2") or 0)
            added = math.floor(building_area / 90.0) * count
            rule = "비주거용: 건축면적 90㎡당 1동, 소수점 버림"
        elif btype == "factory":
            if bool(b.get("exclude_for_relocation", False)):
                added = 0
                rule = "준공업지역 재배치 대상 공장: 건축물 동수에서 제외"
            else:
                building_area = float(b.get("building_area_m2") or 0)
                added = math.floor(building_area / 90.0) * count
                rule = "재배치 제외대상이 아닌 공장: 비주거용 환산 적용"
        else:
            breakdown.append({
                "index": idx, "type": btype, "count": count, "added": None,
                "status": "UNKNOWN_TYPE", "rule": "지원하지 않는 건축물 유형"
            })
            continue

        equivalent += added
        breakdown.append({
            "index": idx, "type": btype, "count": count, "added": added,
            "status": "OK", "rule": rule
        })

    density = equivalent / (effective_area / 10000.0)
    return {
        "status": "OK",
        "message": "서울시 조례 제2조제5호 방식으로 계산한 호수밀도입니다.",
        "effective_area_m2": effective_area,
        "equivalent_building_count": equivalent,
        "house_density_per_ha": density,
        "breakdown": breakdown,
        "source": "서울특별시 도시 및 주거환경정비 조례 제2조제5호"
    }


def evaluate_redevelopment(payload: Dict[str, Any]) -> Dict[str, Any]:
    """서울 주택정비형 재개발 정비계획 입안대상지역 1차 스크리닝."""
    rules = _load_rules()
    t = rules["thresholds"]

    promotion = bool(payload.get("promotion_district", False))
    committee_exception = bool(payload.get("area_5000_exception_approved", False))

    area = payload.get("area_m2")
    total_buildings = payload.get("total_building_count")
    old_buildings = payload.get("old_building_count")
    total_parcels = payload.get("total_parcel_count")
    small_parcels = payload.get("small_parcel_count")
    total_road_basis_buildings = payload.get("road_basis_building_count")
    road_access_buildings = payload.get("road_access_building_count_6m")
    house_density = payload.get("house_density_per_ha")
    total_floor_area = payload.get("total_floor_area_m2")
    old_floor_area = payload.get("old_floor_area_m2")

    # 상세 호수밀도 입력이 있고 직접입력값이 없으면 자동 계산
    density_calc = None
    if house_density is None and payload.get("house_density_detail"):
        density_calc = calculate_house_density(payload["house_density_detail"])
        if density_calc.get("status") == "OK":
            house_density = density_calc["house_density_per_ha"]

    owner_request_consent = payload.get("request_owner_consent_ratio")
    owner_proposal_consent = payload.get("proposal_owner_consent_ratio")
    land_proposal_consent = payload.get("proposal_land_area_consent_ratio")

    old_count_ratio = _ratio(old_buildings, total_buildings)
    small_parcel_ratio = _ratio(small_parcels, total_parcels)
    road_access_ratio = _ratio(road_access_buildings, total_road_basis_buildings)
    old_floor_ratio = _ratio(old_floor_area, total_floor_area)

    checks: List[Check] = []

    # 면적요건: 현행 서울 조례 문언에 따라 일반지역은 서울시 도시계획위원회,
    # 재정비촉진지구는 도시재정비위원회 심의 인정 시 5천㎡ 이상 가능.
    if area is None:
        checks.append(Check("AREA", "mandatory", "구역면적",
                            "10,000㎡ 이상; 관련 위원회 심의 인정 시 5,000㎡ 이상",
                            None, "UNKNOWN", ["SEOUL_ORDINANCE_ART6"]))
    else:
        if area >= t["area_normal_m2"]:
            status, note = "PASS", "통상 면적기준 충족"
        elif area >= t["area_exception_m2"] and committee_exception:
            status = "PASS"
            note = ("재정비촉진지구 도시재정비위원회 심의 인정 입력" if promotion
                    else "서울특별시 도시계획위원회 심의 인정 입력")
        elif area >= t["area_exception_m2"]:
            status = "UNKNOWN"
            note = ("5,000~10,000㎡: 재정비촉진지구 도시재정비위원회 심의 인정 확인 필요" if promotion
                    else "5,000~10,000㎡: 서울특별시 도시계획위원회 심의 인정 확인 필요")
        else:
            status, note = "FAIL", "5,000㎡ 미만"
        checks.append(Check("AREA", "mandatory", "구역면적",
                            "10,000㎡ 이상; 관련 위원회 심의 인정 시 5,000㎡ 이상",
                            f"{area:,.0f}㎡", status, ["SEOUL_ORDINANCE_ART6"], note))

    old_count_threshold = (t["old_building_count_ratio_promotion_district"]
                           if promotion else t["old_building_count_ratio"])
    if old_count_ratio is None:
        checks.append(Check("OLD_COUNT", "mandatory", "노후·불량건축물 수 비율",
                            f">= {old_count_threshold*100:.0f}%", None, "UNKNOWN",
                            ["ENFORCEMENT_DECREE_APPENDIX1", "SEOUL_ORDINANCE_ART6"]))
    else:
        checks.append(Check("OLD_COUNT", "mandatory", "노후·불량건축물 수 비율",
                            f">= {old_count_threshold*100:.0f}%", _pct(old_count_ratio),
                            "PASS" if old_count_ratio >= old_count_threshold else "FAIL",
                            ["ENFORCEMENT_DECREE_APPENDIX1", "SEOUL_ORDINANCE_ART6"],
                            "재정비촉진지구 50% 기준" if promotion else "서울 일반지역 60% 기준"))

    # 시행령 별표 1 간주규정: 노후·불량건축물 수 75% 이상이면 조례가 따로 정한 추가요건을 갖춘 것으로 봄
    deemed_threshold = t["old_building_count_deemed_selection_ratio"]
    if old_count_ratio is None:
        checks.append(Check("DEEMED_SELECTION_BY_OLD_COUNT", "selection", "노후도 간주규정",
                            f">= {deemed_threshold*100:.0f}%", None, "UNKNOWN",
                            ["ENFORCEMENT_DECREE_APPENDIX1"],
                            "충족 시 과소필지·접도·호수밀도 등 조례상 추가요건 충족으로 간주"))
    else:
        checks.append(Check("DEEMED_SELECTION_BY_OLD_COUNT", "selection", "노후도 간주규정",
                            f">= {deemed_threshold*100:.0f}%", _pct(old_count_ratio),
                            "PASS" if old_count_ratio >= deemed_threshold else "FAIL",
                            ["ENFORCEMENT_DECREE_APPENDIX1"],
                            "현행 기준 75%. 법령 개정 전까지 70%를 적용하지 않음"))

    if small_parcel_ratio is None:
        checks.append(Check("SMALL_PARCEL", "selection", "과소필지 비율", ">= 40%", None, "UNKNOWN",
                            ["SEOUL_ORDINANCE_ART6"], "과소필지=90㎡ 미만 토지"))
    else:
        checks.append(Check("SMALL_PARCEL", "selection", "과소필지 비율", ">= 40%", _pct(small_parcel_ratio),
                            "PASS" if small_parcel_ratio >= t["small_parcel_ratio"] else "FAIL",
                            ["SEOUL_ORDINANCE_ART6"], "과소필지=90㎡ 미만 토지"))

    if road_access_ratio is None:
        checks.append(Check("ROAD_ACCESS", "selection", "주택접도율", "<= 40%", None, "UNKNOWN",
                            ["SEOUL_ORDINANCE_ART6"], "재개발은 폭 6m 이상 도로 기준"))
    else:
        checks.append(Check("ROAD_ACCESS", "selection", "주택접도율", "<= 40%", _pct(road_access_ratio),
                            "PASS" if road_access_ratio <= t["housing_road_access_ratio"] else "FAIL",
                            ["SEOUL_ORDINANCE_ART6"], "재개발은 폭 6m 이상 도로 기준"))

    density_note = "서울시 조례 제2조제5호 유형별 산정기준"
    if density_calc and density_calc.get("status") == "OK":
        density_note += f"; 상세입력에서 자동계산(환산동수 {density_calc['equivalent_building_count']}동)"
    checks.append(_check_ge("HOUSE_DENSITY", "selection", "호수밀도", house_density,
                            t["house_density_per_ha"], "호/ha", ["SEOUL_ORDINANCE_ART2_5", "SEOUL_ORDINANCE_ART6"],
                            density_note))

    old_floor_threshold = (t["old_floor_area_ratio_promotion_district"]
                           if promotion else t["old_floor_area_ratio"])
    if old_floor_ratio is None:
        checks.append(Check("OLD_FLOOR_AREA", "selection", "노후·불량건축물 연면적 비율",
                            f">= {old_floor_threshold*100:.0f}%", None, "UNKNOWN",
                            ["ENFORCEMENT_DECREE_APPENDIX1"]))
    else:
        checks.append(Check("OLD_FLOOR_AREA", "selection", "노후·불량건축물 연면적 비율",
                            f">= {old_floor_threshold*100:.0f}%", _pct(old_floor_ratio),
                            "PASS" if old_floor_ratio >= old_floor_threshold else "FAIL",
                            ["ENFORCEMENT_DECREE_APPENDIX1"]))

    def consent_check(cid: str, label: str, actual: Optional[float], threshold: float, source: List[str]) -> Check:
        if actual is None:
            return Check(cid, "consent", label, f">= {threshold*100:.0f}%", None, "UNKNOWN", source)
        return Check(cid, "consent", label, f">= {threshold*100:.0f}%", _pct(actual),
                     "PASS" if actual >= threshold else "FAIL", source)

    checks.append(consent_check("REQUEST_OWNER_CONSENT", "입안요청 토지등소유자 동의율",
                                owner_request_consent, t["request_owner_consent_ratio"], ["SEOUL_ORDINANCE_ART9_2"]))
    checks.append(consent_check("PROPOSAL_OWNER_CONSENT", "입안제안 토지등소유자 동의율",
                                owner_proposal_consent, t["proposal_owner_consent_ratio"], ["SEOUL_ORDINANCE_ART10"]))
    checks.append(consent_check("PROPOSAL_LAND_CONSENT", "입안제안 토지면적 동의율",
                                land_proposal_consent, t["proposal_land_area_consent_ratio"], ["SEOUL_ORDINANCE_ART10"]))

    mandatory = [c for c in checks if c.group == "mandatory"]
    selection = [c for c in checks if c.group == "selection"]

    if any(c.status == "FAIL" for c in mandatory):
        physical_status = "FAIL"
        physical_message = "필수요건 중 미충족 항목이 있어 현재 입력값 기준으로 입안대상 1차 요건을 충족하지 못합니다."
    elif any(c.status == "UNKNOWN" for c in mandatory):
        physical_status = "REVIEW"
        physical_message = "필수요건에 확인되지 않은 값이 있어 판정을 보류합니다."
    elif any(c.status == "PASS" for c in selection):
        physical_status = "PASS"
        if any(c.id == "DEEMED_SELECTION_BY_OLD_COUNT" and c.status == "PASS" for c in selection):
            physical_message = "필수요건을 충족하고 노후·불량건축물 수 75% 이상 간주규정이 적용되어 추가요건을 충족한 것으로 판정합니다."
        else:
            physical_message = "필수요건과 선택요건(1개 이상)을 충족한 것으로 입력되어 정비계획 입안대상지역 1차 요건을 충족합니다."
    elif all(c.status == "FAIL" for c in selection):
        physical_status = "FAIL"
        physical_message = "필수요건은 충족하지만 간주규정 및 선택요건이 모두 미충족입니다."
    else:
        physical_status = "REVIEW"
        physical_message = "필수요건은 충족했으나 선택요건 중 확인되지 않은 값이 있어 추가 데이터가 필요합니다."

    special_notes: List[str] = [
        "노후·불량건축물 75% 간주규정은 현행 시행령 기준으로 판정에 직접 반영합니다.",
        "서울시 조례 제6조에 따라 입안대상 정량요건 외에도 도시·주거환경정비기본계획 적합성 검토가 별도로 필요합니다.",
        "향후 법령이 개정되면 룰셋 버전을 새로 만들어 기준일과 시행일을 분리하여 적용해야 합니다.",
        "이 결과는 정비구역 지정, 도시계획위원회 심의, 사업성 또는 조합설립 가능성을 확정하지 않습니다."
    ]

    sources = {s["id"]: s for s in rules["sources"]}

    return {
        "engine": {
            "id": rules["rule_set_id"],
            "title": rules["title"],
            "scope": rules["scope"],
            "as_of": rules["as_of"]
        },
        "derived": {
            "old_building_count_ratio": old_count_ratio,
            "small_parcel_ratio": small_parcel_ratio,
            "housing_road_access_ratio": road_access_ratio,
            "old_floor_area_ratio": old_floor_ratio,
            "house_density_calculation": density_calc
        },
        "physical_eligibility": {
            "status": physical_status,
            "message": physical_message,
            "meaning": "정비계획 입안대상지역 정량요건 1차 스크리닝"
        },
        "checks": [c.to_dict() for c in checks],
        "special_notes": special_notes,
        "policy_watch": rules.get("policy_watch", []),
        "sources": sources,
        "input_data_map": {
            "area_m2": "GIS polygon 자동계산 가능",
            "total_building_count / old_building_count": "건물통합정보+건축HUB 자동화 예정",
            "total_parcel_count / small_parcel_count": "연속지적 기반 자동화 가능(90㎡ 미만)",
            "road_basis_building_count / road_access_building_count_6m": "도로폭원+대지접도 공간연산 필요",
            "house_density_per_ha": "직접입력 또는 조례 제2조제5호 상세산정 엔진 사용",
            "house_density_detail": "건축물 유형·면적을 넣으면 조례식으로 계산",
            "total_floor_area_m2 / old_floor_area_m2": "건축물대장 연면적 기반 자동화 예정",
            "consent ratios": "공공 API 자동취득 곤란; 사용자 입력"
        }
    }


GEOD = Geod(ellps="WGS84")


def measure_geojson(geometry: Dict[str, Any]) -> Dict[str, Any]:
    """Measure a WGS84 GeoJSON Polygon/MultiPolygon geodesically.

    Returns absolute geodesic area and perimeter. The geometry is not projected,
    so the result is stable for ordinary project-area screening anywhere in Seoul.
    """
    geom = shape(geometry)
    if geom.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError("Polygon 또는 MultiPolygon만 지원합니다.")
    if geom.is_empty:
        raise ValueError("빈 도형은 분석할 수 없습니다.")
    if not geom.is_valid:
        raise ValueError(f"유효하지 않은 도형입니다: {explain_validity(geom)}")

    area_m2, perimeter_m = GEOD.geometry_area_perimeter(geom)
    area_m2 = abs(float(area_m2))
    perimeter_m = abs(float(perimeter_m))

    minx, miny, maxx, maxy = geom.bounds
    centroid = geom.centroid
    return {
        "geometry": mapping(geom),
        "area_m2": area_m2,
        "area_ha": area_m2 / 10000.0,
        "perimeter_m": perimeter_m,
        "centroid": {"lng": centroid.x, "lat": centroid.y},
        "bbox": [minx, miny, maxx, maxy],
    }


INDEX_HTML = '<!doctype html>\n<html lang="ko">\n<head>\n  <meta charset="utf-8" />\n  <meta name="viewport" content="width=device-width, initial-scale=1" />\n  <title>도시검토 플랫폼 | 서울 재개발 웹 MVP</title>\n  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" crossorigin="" />\n  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet.draw/1.0.4/leaflet.draw.css" />\n  <style>\n    :root{--bg:#f3f5f7;--card:#fff;--line:#dfe3e8;--text:#16181d;--muted:#667085;--dark:#111827;--green:#067647;--red:#b42318;--amber:#b54708;--blue:#175cd3}\n    *{box-sizing:border-box}body{margin:0;font-family:system-ui,-apple-system,"Noto Sans KR",sans-serif;background:var(--bg);color:var(--text)}\n    header{padding:18px 24px;background:#fff;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:20px;align-items:center}\n    header h1{font-size:22px;margin:0 0 4px}header .sub{font-size:12px;color:var(--muted)}\n    .shell{display:grid;grid-template-columns:minmax(520px,1.35fr) minmax(420px,.95fr);gap:16px;padding:16px;min-height:calc(100vh - 78px)}\n    .panel{background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden}.panel-head{padding:14px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:10px}.panel-head h2{font-size:16px;margin:0}\n    #map{height:520px;width:100%;background:#e8eaed}.map-foot{padding:12px 16px;border-top:1px solid var(--line);display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.metric{padding:10px;border:1px solid var(--line);border-radius:10px}.metric .k{font-size:11px;color:var(--muted)}.metric .v{font-weight:800;font-size:17px;margin-top:2px}\n    .form-wrap{padding:14px 16px}.section-title{font-size:13px;font-weight:800;margin:4px 0 10px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.field label{display:block;font-size:11px;color:#475467;font-weight:700;margin-bottom:4px}.field input{width:100%;padding:9px 10px;border:1px solid #cfd5dc;border-radius:8px;font-size:14px;background:#fff}.field input:focus{outline:2px solid #c7d7fe;border-color:#84adff}.field small{color:var(--muted);font-size:10px}\n    .checkline{display:flex;gap:8px;align-items:center;font-size:12px;margin-top:9px}.checkline input{width:auto}.actions{display:flex;gap:8px;margin-top:14px}.btn{padding:11px 13px;border-radius:9px;border:1px solid var(--line);font-weight:800;cursor:pointer;background:white}.btn.primary{background:var(--dark);color:white;border-color:var(--dark);flex:1}.btn:disabled{opacity:.45;cursor:not-allowed}\n    .hint{margin-top:10px;padding:9px 10px;border-radius:8px;background:#f8fafc;border:1px dashed #d0d5dd;font-size:11px;color:#475467;line-height:1.5}\n    .result{padding:14px 16px}.statusbox{padding:14px;border-radius:12px;border:1px solid var(--line);background:#fbfcfd}.status{font-size:24px;font-weight:900}.PASS{color:var(--green)}.FAIL{color:var(--red)}.REVIEW,.UNKNOWN{color:var(--amber)}.INFO{color:#475467}.statusmsg{font-size:13px;line-height:1.5;margin-top:6px}.tiny{font-size:10px;color:var(--muted)}\n    table{border-collapse:collapse;width:100%;font-size:11px;margin-top:12px}th,td{padding:8px 6px;border-bottom:1px solid #edf0f2;text-align:left;vertical-align:top}th{font-size:10px;color:#475467;background:#fafafa;position:sticky;top:0}.pill{font-size:10px;font-weight:900;padding:2px 6px;border-radius:999px;background:#f2f4f7;white-space:nowrap}.source-link{color:var(--blue);text-decoration:none}.source-link:hover{text-decoration:underline}\n    details{margin-top:12px;border:1px solid var(--line);border-radius:10px;background:#fff}summary{cursor:pointer;font-size:12px;font-weight:800;padding:10px 12px}.details-body{padding:0 12px 12px}.note-list{font-size:11px;color:#475467;line-height:1.55;padding-left:18px}.empty{color:var(--muted);font-size:13px;padding:24px 0;text-align:center}.badge{font-size:10px;padding:3px 7px;border-radius:999px;background:#eef4ff;color:#3538cd;font-weight:800}\n    .connection{display:flex;gap:6px;flex-wrap:wrap}.conn{font-size:10px;border:1px solid var(--line);border-radius:999px;padding:4px 7px;background:#fff}.auto{color:var(--green);border-color:#abefc6;background:#ecfdf3}.manual{color:#475467}.planned{color:#b54708;background:#fffaeb;border-color:#fedf89}\n    @media(max-width:1050px){.shell{grid-template-columns:1fr}.panel{overflow:visible}#map{height:470px}}\n    @media(max-width:640px){header{align-items:flex-start;flex-direction:column}.shell{padding:8px}.grid{grid-template-columns:1fr}.map-foot{grid-template-columns:1fr 1fr}#map{height:420px}}\n  </style>\n</head>\n<body>\n<header>\n  <div><h1>도시검토 플랫폼</h1><div class="sub">서울 주택정비형 재개발 · 웹 지도 + Rule Engine v0.3 · 기준일 2026-08-24</div></div>\n  <div class="connection"><span class="conn auto">구역면적 AUTO</span><span class="conn manual">노후도 MANUAL</span><span class="conn manual">과소필지 MANUAL</span><span class="conn planned">공공데이터 연결 NEXT</span></div>\n</header>\n<div class="shell">\n  <section class="panel">\n    <div class="panel-head"><h2>1. 지도에서 사업구역 그리기</h2><span class="badge">서울 중심</span></div>\n    <div id="map"></div>\n    <div class="map-foot">\n      <div class="metric"><div class="k">구역면적</div><div class="v" id="mArea">-</div></div>\n      <div class="metric"><div class="k">면적(ha)</div><div class="v" id="mHa">-</div></div>\n      <div class="metric"><div class="k">둘레</div><div class="v" id="mPerimeter">-</div></div>\n    </div>\n    <div class="form-wrap">\n      <div class="section-title">2. 현재 자동취득되지 않는 정비지표 입력</div>\n      <div class="grid">\n        <div class="field"><label>구역면적(㎡) <span class="PASS">AUTO</span></label><input id="area_m2" type="number" placeholder="지도를 그리면 자동입력" readonly></div>\n        <div class="field"><label>전체 건축물 수</label><input id="total_building_count" type="number" placeholder="예: 100"></div>\n        <div class="field"><label>노후·불량건축물 수</label><input id="old_building_count" type="number" placeholder="예: 75"><small>현행 간주기준 75%</small></div>\n        <div class="field"><label>전체 필지 수</label><input id="total_parcel_count" type="number" placeholder="예: 80"></div>\n        <div class="field"><label>90㎡ 미만 필지 수</label><input id="small_parcel_count" type="number" placeholder="예: 35"></div>\n        <div class="field"><label>접도율 산정 건축물 수</label><input id="road_basis_building_count" type="number" placeholder="예: 100"></div>\n        <div class="field"><label>6m 이상 도로 접도 건축물 수</label><input id="road_access_building_count_6m" type="number" placeholder="예: 32"><small>재개발 주택접도율 6m 기준</small></div>\n        <div class="field"><label>호수밀도(호/ha)</label><input id="house_density_per_ha" type="number" step="0.01" placeholder="예: 62"></div>\n        <div class="field"><label>전체 건축물 연면적(㎡)</label><input id="total_floor_area_m2" type="number" placeholder="선택 입력"></div>\n        <div class="field"><label>노후·불량건축물 연면적(㎡)</label><input id="old_floor_area_m2" type="number" placeholder="선택 입력"></div>\n        <div class="field"><label>입안요청 토지등소유자 동의율(%)</label><input id="request_owner_consent_ratio" type="number" min="0" max="100" placeholder="선택 입력"></div>\n        <div class="field"><label>입안제안 토지등소유자 동의율(%)</label><input id="proposal_owner_consent_ratio" type="number" min="0" max="100" placeholder="선택 입력"></div>\n        <div class="field"><label>입안제안 토지면적 동의율(%)</label><input id="proposal_land_area_consent_ratio" type="number" min="0" max="100" placeholder="선택 입력"></div>\n      </div>\n      <label class="checkline"><input id="promotion_district" type="checkbox"> 재정비촉진지구</label>\n      <label class="checkline"><input id="area_5000_exception_approved" type="checkbox"> 5,000~10,000㎡ 관련 위원회 심의 인정 확인</label>\n      <div class="actions"><button class="btn" onclick="loadSample()">샘플값</button><button class="btn" onclick="clearInputs()">초기화</button><button id="runBtn" class="btn primary" onclick="runEvaluation()" disabled>재개발 검토 실행</button></div>\n      <div class="hint">지금 버전은 <b>구역면적만 지도에서 자동계산</b>한다. 노후도·필지·접도·호수밀도는 엔진 정확성 검증을 위해 직접 입력한다. 다음 버전에서 연속지적·건축물·도로 데이터를 연결해 이 입력칸을 차례로 없앤다.</div>\n    </div>\n  </section>\n\n  <section class="panel">\n    <div class="panel-head"><h2>3. 재개발 검토 결과</h2><span class="badge">정량요건 1차 스크리닝</span></div>\n    <div class="result" id="result"><div class="empty">왼쪽 지도에서 대상구역을 먼저 그리세요.</div></div>\n  </section>\n</div>\n<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" crossorigin=""></script>\n<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet.draw/1.0.4/leaflet.draw.js"></script>\n<script>\nconst map=L.map(\'map\',{zoomControl:true}).setView([37.5665,126.9780],13);\nL.tileLayer(\'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png\',{maxZoom:20,attribution:\'© OpenStreetMap contributors\'}).addTo(map);\nconst drawnItems=new L.FeatureGroup().addTo(map);\nlet activeGeometry=null;\nconst drawControl=new L.Control.Draw({position:\'topright\',draw:{polygon:{allowIntersection:false,showArea:false,shapeOptions:{weight:3}},rectangle:{shapeOptions:{weight:3}},polyline:false,circle:false,circlemarker:false,marker:false},edit:{featureGroup:drawnItems,remove:true}});\nmap.addControl(drawControl);\n\nmap.on(L.Draw.Event.CREATED, async e=>{drawnItems.clearLayers(); drawnItems.addLayer(e.layer); activeGeometry=e.layer.toGeoJSON().geometry; await measureAndSync();});\nmap.on(L.Draw.Event.EDITED, async e=>{e.layers.eachLayer(layer=>activeGeometry=layer.toGeoJSON().geometry); await measureAndSync();});\nmap.on(L.Draw.Event.DELETED, ()=>{activeGeometry=null; resetMeasure(); document.getElementById(\'result\').innerHTML=\'<div class="empty">왼쪽 지도에서 대상구역을 먼저 그리세요.</div>\';});\n\nfunction num(id){const v=document.getElementById(id).value.trim(); return v===\'\'?null:Number(v)}\nfunction ratio(id){const v=num(id); return v===null?null:v/100}\nfunction fmt(n,d=0){return n==null?\'-\':Number(n).toLocaleString(\'ko-KR\',{maximumFractionDigits:d})}\n\nasync function measureAndSync(){\n  if(!activeGeometry)return;\n  const r=await fetch(\'/api/spatial/measure\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({geometry:activeGeometry})});\n  const d=await r.json();\n  if(!r.ok){alert(d.detail||\'면적 계산 실패\');return;}\n  document.getElementById(\'area_m2\').value=d.area_m2.toFixed(2);\n  document.getElementById(\'mArea\').textContent=fmt(d.area_m2,0)+\' ㎡\';\n  document.getElementById(\'mHa\').textContent=fmt(d.area_ha,3)+\' ha\';\n  document.getElementById(\'mPerimeter\').textContent=fmt(d.perimeter_m,0)+\' m\';\n  document.getElementById(\'runBtn\').disabled=false;\n  document.getElementById(\'result\').innerHTML=\'<div class="empty">구역면적을 계산했습니다. 정비지표를 입력하고 검토를 실행하세요.</div>\';\n}\nfunction resetMeasure(){document.getElementById(\'area_m2\').value=\'\';document.getElementById(\'mArea\').textContent=\'-\';document.getElementById(\'mHa\').textContent=\'-\';document.getElementById(\'mPerimeter\').textContent=\'-\';document.getElementById(\'runBtn\').disabled=true;}\n\nfunction loadSample(){\n  const vals={total_building_count:100,old_building_count:75,total_parcel_count:100,small_parcel_count:32,road_basis_building_count:100,road_access_building_count_6m:55,house_density_per_ha:42,total_floor_area_m2:30000,old_floor_area_m2:14000,request_owner_consent_ratio:32};\n  Object.entries(vals).forEach(([k,v])=>document.getElementById(k).value=v);\n}\nfunction clearInputs(){\n  [\'total_building_count\',\'old_building_count\',\'total_parcel_count\',\'small_parcel_count\',\'road_basis_building_count\',\'road_access_building_count_6m\',\'house_density_per_ha\',\'total_floor_area_m2\',\'old_floor_area_m2\',\'request_owner_consent_ratio\',\'proposal_owner_consent_ratio\',\'proposal_land_area_consent_ratio\'].forEach(id=>document.getElementById(id).value=\'\');\n  document.getElementById(\'promotion_district\').checked=false; document.getElementById(\'area_5000_exception_approved\').checked=false;\n}\n\nasync function runEvaluation(){\n  const body={\n    area_m2:num(\'area_m2\'), total_building_count:num(\'total_building_count\'), old_building_count:num(\'old_building_count\'),\n    total_parcel_count:num(\'total_parcel_count\'), small_parcel_count:num(\'small_parcel_count\'),\n    road_basis_building_count:num(\'road_basis_building_count\'), road_access_building_count_6m:num(\'road_access_building_count_6m\'),\n    house_density_per_ha:num(\'house_density_per_ha\'), total_floor_area_m2:num(\'total_floor_area_m2\'), old_floor_area_m2:num(\'old_floor_area_m2\'),\n    promotion_district:document.getElementById(\'promotion_district\').checked,\n    area_5000_exception_approved:document.getElementById(\'area_5000_exception_approved\').checked,\n    request_owner_consent_ratio:ratio(\'request_owner_consent_ratio\'), proposal_owner_consent_ratio:ratio(\'proposal_owner_consent_ratio\'),\n    proposal_land_area_consent_ratio:ratio(\'proposal_land_area_consent_ratio\')\n  };\n  const r=await fetch(\'/api/redevelopment/evaluate\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify(body)});\n  const d=await r.json();\n  if(!r.ok){document.getElementById(\'result\').innerHTML=\'<div class="statusbox"><div class="FAIL">오류</div><div class="statusmsg">\'+(d.detail||\'판정 실패\')+\'</div></div>\';return;}\n  renderResult(d);\n}\nfunction renderResult(d){\n  const s=d.physical_eligibility;\n  const rows=d.checks.map(c=>{\n    const src=(c.source_ids||[]).map(id=>{const x=d.sources[id];return x?`<a class="source-link" href="${x.url}" target="_blank">${id}</a>`:id}).join(\'<br>\');\n    return `<tr><td>${groupKo(c.group)}</td><td><b>${c.label}</b><div class="tiny">${c.note||\'\'}</div></td><td>${c.requirement}</td><td>${c.actual??\'-\'}</td><td><span class="pill ${c.status}">${c.status}</span></td><td>${src}</td></tr>`;\n  }).join(\'\');\n  const policy=(d.policy_watch||[]).map(x=>`<li><b>${x.status}</b> · ${x.current}<br>${x.engine_behavior}</li>`).join(\'\');\n  document.getElementById(\'result\').innerHTML=`\n    <div class="statusbox"><div class="status ${s.status}">${s.status}</div><div class="statusmsg">${s.message}</div><div class="tiny" style="margin-top:8px">${s.meaning} · 룰셋 ${d.engine.id}</div></div>\n    <table><thead><tr><th>구분</th><th>항목</th><th>기준</th><th>대상지</th><th>판정</th><th>근거</th></tr></thead><tbody>${rows}</tbody></table>\n    <details open><summary>판정 해석 및 한계</summary><div class="details-body"><ul class="note-list">${d.special_notes.map(x=>`<li>${x}</li>`).join(\'\')}</ul></div></details>\n    <details><summary>정책 변경 추적</summary><div class="details-body"><ul class="note-list">${policy||\'<li>없음</li>\'}</ul></div></details>`;\n}\nfunction groupKo(g){return {mandatory:\'필수\',selection:\'선택/간주\',consent:\'주민절차\'}[g]||g}\n</script>\n</body>\n</html>\n'

app = FastAPI(
    title="도시검토 플랫폼 - 서울 재개발 웹 MVP",
    version="0.3.2",
    description="웹 지도에서 사업구역을 그리고 서울 주택정비형 재개발 1차 요건을 판정하는 MVP",
)


class RedevelopmentInput(BaseModel):
    area_m2: Optional[float] = Field(None, ge=0)
    total_building_count: Optional[float] = Field(None, ge=0)
    old_building_count: Optional[float] = Field(None, ge=0)
    total_parcel_count: Optional[float] = Field(None, ge=0)
    small_parcel_count: Optional[float] = Field(None, ge=0)
    road_basis_building_count: Optional[float] = Field(None, ge=0)
    road_access_building_count_6m: Optional[float] = Field(None, ge=0)
    house_density_per_ha: Optional[float] = Field(None, ge=0)
    house_density_detail: Optional[Dict[str, Any]] = None
    total_floor_area_m2: Optional[float] = Field(None, ge=0)
    old_floor_area_m2: Optional[float] = Field(None, ge=0)
    promotion_district: bool = False
    area_5000_exception_approved: bool = False
    request_owner_consent_ratio: Optional[float] = Field(None, ge=0, le=1)
    proposal_owner_consent_ratio: Optional[float] = Field(None, ge=0, le=1)
    proposal_land_area_consent_ratio: Optional[float] = Field(None, ge=0, le=1)


class GeometryInput(BaseModel):
    geometry: Dict[str, Any]


@app.get("/", response_class=HTMLResponse)
def home():
    return INDEX_HTML


@app.get("/health")
def health():
    return {
        "ok": True,
        "app": "urban_strategy_web_v0.3.2",
        "engine": RULES["rule_set_id"],
        "map": "leaflet-draw",
    }


@app.post("/api/spatial/measure")
def spatial_measure(inp: GeometryInput):
    try:
        return measure_geojson(inp.geometry)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/redevelopment/evaluate")
def redevelopment_evaluate(inp: RedevelopmentInput):
    return evaluate_redevelopment(inp.model_dump())


@app.post("/api/redevelopment/house-density")
def house_density(detail: Dict[str, Any]):
    return calculate_house_density(detail)
