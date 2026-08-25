from __future__ import annotations

import math
import os
import logging
import xml.etree.ElementTree as ET
from datetime import date, datetime
from urllib.parse import quote, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache

import requests
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
RULES['sources'].append({'id':'SEOUL_ORDINANCE_ART2_10','title':'서울특별시 도시 및 주거환경정비 조례 제2조제10호','url':'https://law.go.kr/LSW/ordinInfoP.do?ordinSeq=2130189','note':'주택접도율 정의: 도로 접도길이 4m 이상. 제6조에서 주택정비형 재개발은 도로폭 6m 이상 적용'})


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
                            ["SEOUL_ORDINANCE_ART2_10", "SEOUL_ORDINANCE_ART6"], "폭 6m 이상 도로에 대지가 길이 4m 이상 접한 경우"))
    else:
        checks.append(Check("ROAD_ACCESS", "selection", "주택접도율", "<= 40%", _pct(road_access_ratio),
                            "PASS" if road_access_ratio <= t["housing_road_access_ratio"] else "FAIL",
                            ["SEOUL_ORDINANCE_ART2_10", "SEOUL_ORDINANCE_ART6"], "폭 6m 이상 도로에 대지가 길이 4m 이상 접한 경우"))

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



VWORLD_DATA_URL = "https://api.vworld.kr/req/data"
VWORLD_LAND_URL = "https://api.vworld.kr/ned/data/getLandCharacteristics"
VWORLD_PROXY_URL = "https://map.vworld.kr/proxy.do?url="
VWORLD_LAYER_PARCEL = "LP_PA_CBND_BUBUN"
SMALL_PARCEL_THRESHOLD_M2 = 90.0

logger = logging.getLogger("urban_strategy.vworld")
logging.basicConfig(level=logging.INFO)


def _vworld_key() -> str:
    return (os.getenv("VWORLD_API_KEY") or "").strip()


def _vworld_domain() -> str:
    raw = (
        (os.getenv("VWORLD_DOMAIN") or "").strip()
        or (os.getenv("RENDER_EXTERNAL_HOSTNAME") or "").strip()
        or "localhost"
    )
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw.rstrip("/")
    if raw == "localhost" or raw.startswith("localhost:"):
        return "http://" + raw.rstrip("/")
    return "https://" + raw.rstrip("/")


def _vworld_referer() -> str:
    return _vworld_domain() + "/"


def _vworld_headers() -> Dict[str, str]:
    return {
        "Referer": _vworld_referer(),
        "User-Agent": "urban-strategy/0.4.2",
        "Accept": "application/json, text/xml;q=0.9, */*;q=0.8",
    }


def _vworld_get(url: str, params: Dict[str, Any], timeout: int = 20):
    """VWorld direct call first, then VWorld's own proxy on transport/5xx failure.

    The proxy path is used by VWorld's published utilization examples.
    API keys remain server-side because this function runs only in FastAPI.
    Returns (response, route) where route is "direct" or "vworld_proxy".
    """
    direct_error = None
    try:
        resp = requests.get(url, params=params, headers=_vworld_headers(), timeout=timeout)
        if resp.status_code < 500:
            return resp, "direct"
        direct_error = f"HTTP {resp.status_code}"
        logger.warning("VWorld direct call failed with %s; trying official proxy", direct_error)
    except requests.RequestException as exc:
        direct_error = repr(exc)
        logger.warning("VWorld direct connection failed (%s); trying official proxy", direct_error)

    # Build the exact inner VWorld URL, then ask VWorld's own proxy to fetch it.
    inner_url = requests.Request("GET", url, params=params).prepare().url
    proxy_url = VWORLD_PROXY_URL + quote(inner_url, safe="")
    try:
        resp = requests.get(
            proxy_url,
            headers={
                "Referer": _vworld_referer(),
                "User-Agent": "urban-strategy/0.4.2",
                "Accept": "application/json, text/xml;q=0.9, */*;q=0.8",
            },
            timeout=timeout + 10,
        )
        logger.info(
            "VWorld proxy fallback route status=%s (direct_error=%s)",
            resp.status_code,
            direct_error,
        )
        return resp, "vworld_proxy"
    except requests.RequestException as exc:
        logger.error(
            "VWorld direct and proxy both failed direct=%s proxy=%r",
            direct_error,
            exc,
        )
        raise RuntimeError(
            f"VWorld 직접연결과 공식 프록시 연결이 모두 실패했습니다. direct={direct_error}; proxy={exc}"
        ) from exc


def vworld_ready() -> bool:
    return bool(_vworld_key())


def _response_error_message(payload: Dict[str, Any]) -> str:
    response = payload.get("response") or {}
    error = response.get("error") or {}
    return str(
        error.get("text")
        or error.get("message")
        or response.get("status")
        or "VWorld 응답 오류"
    )


def _fetch_vworld_parcel_candidates(target_geom) -> List[Dict[str, Any]]:
    """Fetch cadastral features in the target bbox, then exact-filter locally.

    VWorld Data API 2.0 uses LP_PA_CBND_BUBUN with geomFilter=BOX(...).
    Boundary-only touches are excluded by requiring positive geodesic
    intersection area, so parcels merely touching the drawn line are not counted.
    """
    key = _vworld_key()
    if not key:
        raise RuntimeError("VWorld API 키가 설정되지 않았습니다.")

    minx, miny, maxx, maxy = target_geom.bounds
    bbox_poly = shape({
        "type": "Polygon",
        "coordinates": [[
            [minx, miny], [maxx, miny], [maxx, maxy], [minx, maxy], [minx, miny]
        ]]
    })
    bbox_area, _ = GEOD.geometry_area_perimeter(bbox_poly)
    if abs(float(bbox_area)) > 10_000_000:
        raise RuntimeError("VWorld 필지조회 범위가 10㎢를 넘습니다. 대상구역을 더 작게 나눠 주세요.")

    all_features: List[Dict[str, Any]] = []
    seen_api_ids = set()
    size = 1000

    for page in range(1, 11):
        params = {
            "key": key,
            "domain": _vworld_domain(),
            "service": "data",
            "version": "2.0",
            "request": "getfeature",
            "format": "json",
            "size": size,
            "page": page,
            "geometry": "true",
            "attribute": "true",
            "crs": "EPSG:4326",
            "data": VWORLD_LAYER_PARCEL,
            "geomfilter": f"BOX({minx},{miny},{maxx},{maxy})",
        }
        safe_params = {k: ("***" if k == "key" else v) for k, v in params.items()}
        logger.info("VWorld parcel request page=%s domain=%s params=%s", page, _vworld_domain(), safe_params)
        resp, route = _vworld_get(VWORLD_DATA_URL, params=params, timeout=20)
        logger.info("VWorld parcel response route=%s status=%s", route, resp.status_code)
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            logger.error("VWorld HTTP error status=%s body=%s", resp.status_code, resp.text[:1000])
            raise RuntimeError(f"VWorld HTTP {resp.status_code}: {resp.text[:300]}") from exc
        try:
            payload = resp.json()
        except Exception as exc:
            logger.error("VWorld non-JSON response status=%s body=%s", resp.status_code, resp.text[:1000])
            raise RuntimeError(f"VWorld 비정상 응답: {resp.text[:300]}") from exc
        status = str((payload.get("response") or {}).get("status") or "").upper()
        if status not in {"OK", "NOT_FOUND"}:
            msg = _response_error_message(payload)
            logger.error("VWorld API error status=%s detail=%s payload=%s", status, msg, str(payload)[:1500])
            raise RuntimeError(msg)
        if status == "NOT_FOUND":
            break

        fc = (((payload.get("response") or {}).get("result") or {}).get("featureCollection") or {})
        feats = fc.get("features") or []
        for f in feats:
            fid = f.get("id") or ((f.get("properties") or {}).get("pnu"))
            if fid and fid in seen_api_ids:
                continue
            if fid:
                seen_api_ids.add(fid)
            all_features.append(f)
        if len(feats) < size:
            break
    else:
        raise RuntimeError("필지 후보가 10,000건을 넘어 조회를 중단했습니다.")

    exact: List[Dict[str, Any]] = []
    seen_pnu = set()
    for f in all_features:
        gj = f.get("geometry")
        if not gj:
            continue
        try:
            pg = shape(gj)
            inter = target_geom.intersection(pg)
            if inter.is_empty:
                continue
            ia, _ = GEOD.geometry_area_perimeter(inter)
            if abs(float(ia)) < 0.01:
                continue
        except Exception:
            continue
        props = dict(f.get("properties") or {})
        pnu = str(props.get("pnu") or "").strip()
        if not pnu or pnu in seen_pnu:
            continue
        seen_pnu.add(pnu)
        exact.append({"type": "Feature", "id": f.get("id"), "geometry": gj, "properties": props})
    return exact


@lru_cache(maxsize=20000)
def _vworld_official_land_area(pnu: str) -> Optional[float]:
    """Read official parcel area (lndpclAr) from VWorld land-characteristics API.

    Multiple historical <field> rows can be returned. The newest year with a
    positive lndpclAr is selected. Geometry area is deliberately NOT substituted
    when the official field is missing.
    """
    key = _vworld_key()
    if not key:
        return None
    params = {
        "pnu": pnu,
        "format": "xml",
        "key": key,
        "numOfRows": 50,
    }
    try:
        resp, route = _vworld_get(VWORLD_LAND_URL, params=params, timeout=15)
        logger.debug("VWorld land-area route=%s pnu=%s status=%s", route, pnu, resp.status_code)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
    except Exception as exc:
        logger.warning("VWorld land-area lookup failed pnu=%s error=%s", pnu, exc)
        return None

    candidates = []
    for field in root.findall(".//field"):
        area_node = field.find("lndpclAr")
        if area_node is None or not (area_node.text or "").strip():
            continue
        try:
            area = float(area_node.text.strip())
        except ValueError:
            continue
        if area <= 0:
            continue
        year_node = field.find("stdrYear")
        try:
            year = int((year_node.text or "0").strip()) if year_node is not None else 0
        except ValueError:
            year = 0
        candidates.append((year, area))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]

    # Fallback for response shapes without <field>.
    node = root.find(".//lndpclAr")
    if node is not None and (node.text or "").strip():
        try:
            area = float(node.text.strip())
            return area if area > 0 else None
        except ValueError:
            pass
    return None


def analyze_parcels_for_geometry(geometry: Dict[str, Any]) -> Dict[str, Any]:
    target = shape(geometry)
    if target.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError("Polygon 또는 MultiPolygon만 지원합니다.")
    if target.is_empty or not target.is_valid:
        raise ValueError("유효한 대상구역 도형이 필요합니다.")

    features = _fetch_vworld_parcel_candidates(target)
    if not features:
        return {
            "total_parcel_count": 0,
            "official_area_count": 0,
            "missing_official_area_count": 0,
            "known_small_parcel_count": 0,
            "small_parcel_count": 0,
            "complete_official_area": True,
            "feature_collection": {"type": "FeatureCollection", "features": []},
            "source": {
                "parcel_boundary": "VWorld LP_PA_CBND_BUBUN",
                "official_area": "VWorld getLandCharacteristics.lndpclAr",
            },
        }

    pnus = [str((f.get("properties") or {}).get("pnu") or "") for f in features]
    area_map: Dict[str, Optional[float]] = {}
    # Modest concurrency: I/O-bound calls, conservative for a free Render instance/API.
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(pnus)))) as ex:
        futs = {ex.submit(_vworld_official_land_area, pnu): pnu for pnu in pnus}
        for fut in as_completed(futs):
            pnu = futs[fut]
            try:
                area_map[pnu] = fut.result()
            except Exception:
                area_map[pnu] = None

    official_count = 0
    known_small = 0
    out_features = []
    for f in features:
        props = dict(f.get("properties") or {})
        pnu = str(props.get("pnu") or "")
        area = area_map.get(pnu)
        if area is not None:
            official_count += 1
            if area < SMALL_PARCEL_THRESHOLD_M2:
                known_small += 1
        props["official_area_m2"] = area
        props["is_small"] = (area < SMALL_PARCEL_THRESHOLD_M2) if area is not None else None

        # Geometry area is shown only as diagnostic metadata, never as the legal area.
        try:
            ga, _ = GEOD.geometry_area_perimeter(shape(f["geometry"]))
            props["geometry_area_m2"] = abs(float(ga))
        except Exception:
            props["geometry_area_m2"] = None

        out_features.append({
            "type": "Feature",
            "id": f.get("id"),
            "geometry": f.get("geometry"),
            "properties": props,
        })

    total = len(out_features)
    complete = official_count == total
    return {
        "total_parcel_count": total,
        "official_area_count": official_count,
        "missing_official_area_count": total - official_count,
        "known_small_parcel_count": known_small,
        # Strict behavior: only feed the legal small-parcel count when every parcel
        # has official lndpclAr. Otherwise the engine must remain REVIEW/manual.
        "small_parcel_count": known_small if complete else None,
        "complete_official_area": complete,
        "feature_collection": {"type": "FeatureCollection", "features": out_features},
        "source": {
            "parcel_boundary": "VWorld LP_PA_CBND_BUBUN",
            "official_area": "VWorld getLandCharacteristics.lndpclAr",
            "small_parcel_rule": "< 90㎡",
        },
        "note": "경계만 접하는 필지는 제외하고, 대상구역과 양(+)의 면적으로 겹치는 필지만 집계합니다. 법정 과소필지 판정에는 지적도 도형면적이 아니라 토지특성 lndpclAr을 사용합니다.",
    }


BUILDING_HUB_BASE_URL = "https://apis.data.go.kr/1613000/BldRgstHubService"
BUILDING_HUB_TITLE_URL = BUILDING_HUB_BASE_URL + "/getBrTitleInfo"
ENGINE_AS_OF_DATE = date(2026, 8, 24)


def _building_hub_key() -> str:
    # Public Data Portal may display the key URL-encoded. requests will encode params,
    # so decode once before passing it as serviceKey.
    raw = (os.getenv("BUILDING_HUB_API_KEY") or "").strip()
    return unquote(raw) if raw else ""


def building_hub_ready() -> bool:
    return bool(_building_hub_key())


def _pnu_to_bld_params(pnu: str) -> Dict[str, str]:
    pnu = str(pnu or "").strip()
    if len(pnu) != 19 or not pnu.isdigit():
        raise ValueError("PNU는 19자리 숫자여야 합니다.")
    land_code = pnu[10]
    if land_code == "1":
        plat_gb = "0"  # 일반 대지
    elif land_code == "2":
        plat_gb = "1"  # 산
    else:
        raise ValueError(f"지원하지 않는 PNU 토지구분코드: {land_code}")
    return {
        "sigunguCd": pnu[0:5],
        "bjdongCd": pnu[5:10],
        "platGbCd": plat_gb,
        "bun": pnu[11:15],
        "ji": pnu[15:19],
    }


def _items_from_data_go_kr(payload: Dict[str, Any]) -> tuple[List[Dict[str, Any]], int]:
    response = payload.get("response") or {}
    header = response.get("header") or {}
    code = str(header.get("resultCode") or "")
    msg = str(header.get("resultMsg") or "")
    if code and code not in {"00", "0000"}:
        raise RuntimeError(f"건축HUB {code}: {msg or 'API 오류'}")
    body = response.get("body") or {}
    total = int(body.get("totalCount") or 0)
    items = body.get("items") or {}
    item = items.get("item") if isinstance(items, dict) else None
    if item is None:
        return [], total
    if isinstance(item, list):
        return item, total
    if isinstance(item, dict):
        return [item], total
    return [], total



def _items_from_data_go_kr_xml(text: str) -> tuple[List[Dict[str, Any]], int]:
    root = ET.fromstring(text)
    code = (root.findtext('.//resultCode') or '').strip()
    msg = (root.findtext('.//resultMsg') or '').strip()
    if code and code not in {'00','0000'}:
        raise RuntimeError(f"건축HUB {code}: {msg or 'API 오류'}")
    total_txt=(root.findtext('.//totalCount') or '0').strip()
    try: total=int(float(total_txt))
    except Exception: total=0
    items=[]
    for node in root.findall('.//items/item'):
        row={}
        for c in list(node): row[c.tag]=(c.text or '').strip()
        items.append(row)
    return items,total

def _query_building_hub_title(pnu: str) -> List[Dict[str, Any]]:
    key = _building_hub_key()
    if not key:
        raise RuntimeError("BUILDING_HUB_API_KEY가 설정되지 않았습니다.")
    base=_pnu_to_bld_params(pnu); page=1; size=100; out=[]
    while page<=20:
        params={"serviceKey":key,**base,"numOfRows":size,"pageNo":page}
        resp=requests.get(BUILDING_HUB_TITLE_URL,params=params,timeout=25)
        if resp.status_code>=400: raise RuntimeError(f"건축HUB HTTP {resp.status_code}: {resp.text[:240]}")
        ctype=(resp.headers.get('content-type') or '').lower(); text=resp.text
        try:
            if 'json' in ctype or text.lstrip().startswith('{'):
                items,total=_items_from_data_go_kr(resp.json())
            else:
                items,total=_items_from_data_go_kr_xml(text)
        except Exception as exc:
            raise RuntimeError(f"건축HUB 응답 파싱 실패: {text[:300].replace(chr(10),' ')}") from exc
        out.extend(items)
        if len(out)>=total or len(items)<size: break
        page+=1
    return out


def _parse_yyyymmdd(value: Any) -> Optional[date]:
    s = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(s) != 8:
        return None
    try:
        return datetime.strptime(s, "%Y%m%d").date()
    except ValueError:
        return None


def _is_long_life_structure(structure_name: str, purpose_name: str) -> bool:
    st = str(structure_name or "")
    purp = str(purpose_name or "")
    # Seoul Ordinance Art. 4: RC / steel concrete / SRC / steel structure,
    # except detached-house use, uses the 30-year group; others use 20 years.
    structural_tokens = (
        "철근콘크리트",
        "철골철근콘크리트",
        "철골콘크리트",
        "강구조",
        "철골구조",
        "일반철골",
    )
    return ("단독주택" not in purp) and any(tok in st for tok in structural_tokens)


def _age_annotation(item: Dict[str, Any]) -> Dict[str, Any]:
    approved = _parse_yyyymmdd(item.get("useAprDay"))
    if approved is None:
        return {
            "age_status": "UNKNOWN",
            "age_threshold_years": None,
            "age_years": None,
        }

    threshold = 30 if _is_long_life_structure(
        str(item.get("strctCdNm") or ""),
        str(item.get("mainPurpsCdNm") or ""),
    ) else 20

    # Exact anniversary comparison at engine as-of date.
    try:
        anniversary = approved.replace(year=approved.year + threshold)
    except ValueError:
        anniversary = approved.replace(month=2, day=28, year=approved.year + threshold)

    age_years = ENGINE_AS_OF_DATE.year - approved.year - (
        (ENGINE_AS_OF_DATE.month, ENGINE_AS_OF_DATE.day) < (approved.month, approved.day)
    )
    return {
        "age_status": "OLD" if ENGINE_AS_OF_DATE >= anniversary else "NOT_OLD",
        "age_threshold_years": threshold,
        "age_years": age_years,
    }


def _normalize_building_title(item: Dict[str, Any], pnu: str) -> Dict[str, Any]:
    keep = [
        "mgmBldrgstPk", "platPlc", "newPlatPlc", "bldNm", "dongNm",
        "useAprDay", "strctCd", "strctCdNm", "mainPurpsCd", "mainPurpsCdNm",
        "mainAtchGbCd", "mainAtchGbCdNm", "totArea", "platArea", "archArea",
        "bcRat", "vlRat", "vlRatEstmTotArea", "etcPurps",
        "grndFlrCnt", "ugrndFlrCnt", "hhldCnt", "fmlyCnt", "hoCnt",
        "regstrGbCd", "regstrGbCdNm", "regstrKindCd", "regstrKindCdNm",
        "crtnDay", "sigunguCd", "bjdongCd", "platGbCd", "bun", "ji",
    ]
    result = {k: item.get(k) for k in keep}
    result["pnu"] = pnu
    result.update(_age_annotation(item))
    return result


VWORLD_LAND_LEDGER_URL = "https://api.vworld.kr/ned/data/ladfrlList"
LEGACY_LAND_LEDGER_URLS = [
    "https://apis.data.go.kr/1611000/nsdi/eios/LadfrlService/ladfrlList.xml",
    "http://apis.data.go.kr/1611000/nsdi/eios/LadfrlService/ladfrlList.xml",
]


def _parse_land_ledger_xml(text: str) -> Optional[Dict[str, Any]]:
    root = ET.fromstring(text)
    err = root.find(".//error")
    if err is not None and (err.text or "").strip():
        raise RuntimeError(f"토지대장 API 오류: {(err.text or '').strip()}")

    rows = root.findall(".//ladfrlVOList")
    if not rows:
        return None

    def val(row, name: str) -> str:
        n = row.find(name)
        return (n.text or "").strip() if n is not None else ""

    parsed = []
    for row in rows:
        area_raw = val(row, "lndpclAr")
        try:
            area = float(area_raw) if area_raw else None
        except ValueError:
            area = None
        try:
            cnrs = int(float(val(row, "cnrsPsnCo") or 0))
        except ValueError:
            cnrs = 0
        parsed.append({
            "pnu": val(row, "pnu"),
            "ldCodeNm": val(row, "ldCodeNm"),
            "mnnmSlno": val(row, "mnnmSlno"),
            "regstrSeCodeNm": val(row, "regstrSeCodeNm"),
            "lndcgrCodeNm": val(row, "lndcgrCodeNm"),
            "lndpclAr": area,
            "posesnSeCodeNm": val(row, "posesnSeCodeNm"),
            "cnrsPsnCo": cnrs,
            "ladFrtlScNm": val(row, "ladFrtlScNm"),
            "lastUpdtDt": val(row, "lastUpdtDt"),
        })
    parsed.sort(key=lambda r: str(r.get("lastUpdtDt") or ""), reverse=True)
    return parsed[0]


def _server_land_ledger_vworld(pnu: str) -> Optional[Dict[str, Any]]:
    if not _vworld_key():
        return None
    params = {
        "format": "xml",
        "key": _vworld_key(),
        "domain": _vworld_domain(),
        "pnu": pnu,
    }
    try:
        resp, route = _vworld_get(VWORLD_LAND_LEDGER_URL, params=params, timeout=15)
        if resp.status_code >= 400:
            return None
        record = _parse_land_ledger_xml(resp.text)
        if record:
            record["_route"] = f"server_{route}"
        return record
    except Exception as exc:
        logger.info("server VWorld land ledger failed pnu=%s err=%s", pnu, exc)
        return None


def _server_land_ledger_legacy_data_go(pnu: str) -> Optional[Dict[str, Any]]:
    # data.go.kr account keys are often shared across approved APIs. This is only
    # a fallback; authorization failure is silently ignored.
    key = _building_hub_key()
    if not key:
        return None
    params = {"serviceKey": key, "pnu": pnu, "numOfRows": 100}
    for url in LEGACY_LAND_LEDGER_URLS:
        try:
            resp = requests.get(url, params=params, timeout=15, allow_redirects=True)
            if resp.status_code >= 400:
                continue
            record = _parse_land_ledger_xml(resp.text)
            if record:
                record["_route"] = "legacy_data_go"
                return record
        except Exception:
            continue
    return None

INDEX_HTML = '<!doctype html>\n<html lang="ko">\n<head>\n  <meta charset="utf-8" />\n  <meta name="viewport" content="width=device-width, initial-scale=1" />\n  <title>컴팩트시티플랫폼</title>\n  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" crossorigin="" />\n  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet.draw/1.0.4/leaflet.draw.css" />\n  <style>\n    :root{--bg:#f3f5f7;--card:#fff;--line:#dfe3e8;--text:#16181d;--muted:#667085;--dark:#111827;--green:#067647;--red:#b42318;--amber:#b54708;--blue:#175cd3}\n    *{box-sizing:border-box}body{margin:0;font-family:system-ui,-apple-system,"Noto Sans KR",sans-serif;background:var(--bg);color:var(--text)}\n    header{padding:18px 24px;background:#fff;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:20px;align-items:center}\n    header h1{font-size:22px;margin:0 0 4px}header .sub{font-size:12px;color:var(--muted)}\n    .shell{display:grid;grid-template-columns:minmax(520px,1.35fr) minmax(420px,.95fr);gap:16px;padding:16px;min-height:calc(100vh - 78px)}\n    .panel{background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden}.panel-head{padding:14px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:10px}.panel-head h2{font-size:16px;margin:0}\n    #map{height:520px;width:100%;background:#e8eaed}.map-foot{padding:12px 16px;border-top:1px solid var(--line);display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.metric{padding:10px;border:1px solid var(--line);border-radius:10px}.metric .k{font-size:11px;color:var(--muted)}.metric .v{font-weight:800;font-size:17px;margin-top:2px}\n    .form-wrap{padding:14px 16px}.section-title{font-size:13px;font-weight:800;margin:4px 0 10px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.field label{display:block;font-size:11px;color:#475467;font-weight:700;margin-bottom:4px}.field input{width:100%;padding:9px 10px;border:1px solid #cfd5dc;border-radius:8px;font-size:14px;background:#fff}.field input:focus{outline:2px solid #c7d7fe;border-color:#84adff}.field small{color:var(--muted);font-size:10px}\n    .checkline{display:flex;gap:8px;align-items:center;font-size:12px;margin-top:9px}.checkline input{width:auto}.actions{display:flex;gap:8px;margin-top:14px}.btn{padding:11px 13px;border-radius:9px;border:1px solid var(--line);font-weight:800;cursor:pointer;background:white}.btn.primary{background:var(--dark);color:white;border-color:var(--dark);flex:1}.btn:disabled{opacity:.45;cursor:not-allowed}\n    .hint{margin-top:10px;padding:9px 10px;border-radius:8px;background:#f8fafc;border:1px dashed #d0d5dd;font-size:11px;color:#475467;line-height:1.5}\n    .result{padding:14px 16px}.statusbox{padding:14px;border-radius:12px;border:1px solid var(--line);background:#fbfcfd}.status{font-size:24px;font-weight:900}.PASS{color:var(--green)}.FAIL{color:var(--red)}.REVIEW,.UNKNOWN{color:var(--amber)}.INFO{color:#475467}.statusmsg{font-size:13px;line-height:1.5;margin-top:6px}.tiny{font-size:10px;color:var(--muted)}\n    table{border-collapse:collapse;width:100%;font-size:11px;margin-top:12px}th,td{padding:8px 6px;border-bottom:1px solid #edf0f2;text-align:left;vertical-align:top}th{font-size:10px;color:#475467;background:#fafafa;position:sticky;top:0}.pill{font-size:10px;font-weight:900;padding:2px 6px;border-radius:999px;background:#f2f4f7;white-space:nowrap}.source-link{color:var(--blue);text-decoration:none}.source-link:hover{text-decoration:underline}\n    details{margin-top:12px;border:1px solid var(--line);border-radius:10px;background:#fff}summary{cursor:pointer;font-size:12px;font-weight:800;padding:10px 12px}.details-body{padding:0 12px 12px}.note-list{font-size:11px;color:#475467;line-height:1.55;padding-left:18px}.empty{color:var(--muted);font-size:13px;padding:24px 0;text-align:center}.badge{font-size:10px;padding:3px 7px;border-radius:999px;background:#eef4ff;color:#3538cd;font-weight:800}\n    .connection{display:flex;gap:6px;flex-wrap:wrap}.conn{font-size:10px;border:1px solid var(--line);border-radius:999px;padding:4px 7px;background:#fff}.auto{color:var(--green);border-color:#abefc6;background:#ecfdf3}.manual{color:#475467}.planned{color:#b54708;background:#fffaeb;border-color:#fedf89}\n    @media(max-width:1050px){.shell{grid-template-columns:1fr}.panel{overflow:visible}#map{height:470px}}\n    @media(max-width:640px){header{align-items:flex-start;flex-direction:column}.shell{padding:8px}.grid{grid-template-columns:1fr}.map-foot{grid-template-columns:1fr 1fr}#map{height:420px}}\n  \n.boundary-box{margin:14px 18px 0;padding:14px;border:1px solid var(--line);border-radius:12px;background:#fbfcfe}\n.boundary-box .toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:flex-start}\n.boundary-box textarea{flex:1;min-width:280px;min-height:72px;border:1px solid #d0d5dd;border-radius:8px;padding:9px;font:inherit;resize:vertical}\n.parcel-summary{margin-top:10px;font-size:12px;color:#475467}\n.parcel-list{margin-top:8px;max-height:210px;overflow:auto;border:1px solid #eaecf0;border-radius:8px;background:#fff}\n.parcel-row{display:grid;grid-template-columns:34px 1fr 130px;gap:8px;align-items:center;padding:7px 9px;border-bottom:1px solid #f2f4f7;font-size:12px}\n.parcel-row:last-child{border-bottom:0}\n.parcel-row .mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;color:#475467}\n.parcel-row.excluded{opacity:.55;background:#f9fafb}\n.source-box{margin:12px 18px 18px;border:1px solid var(--line);border-radius:12px;overflow:hidden;background:#fff}\n.source-box .source-title{padding:11px 13px;font-weight:800;background:#f9fafb;border-bottom:1px solid var(--line)}\n.source-grid{display:grid;grid-template-columns:150px 1fr 150px 1fr;font-size:12px}\n.source-grid>div{padding:9px 11px;border-bottom:1px solid #f2f4f7}\n.source-grid .k{font-weight:700;background:#fcfcfd;color:#344054}\n@media(max-width:900px){.source-grid{grid-template-columns:120px 1fr}.source-grid .wide-k{grid-column:auto}}\n\n\n.hub-box{margin:12px 0;border:1px solid #d0d5dd;border-radius:10px;background:#fff;overflow:hidden}\n.hub-head{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:10px 12px;background:#f9fafb;border-bottom:1px solid #eaecf0;font-size:12px}\n.hub-head b{font-size:13px}\n.hub-table-wrap{max-height:280px;overflow:auto}\n.hub-table{width:100%;border-collapse:collapse;font-size:11px}\n.hub-table th,.hub-table td{padding:7px 8px;border-bottom:1px solid #f2f4f7;text-align:left;white-space:nowrap}\n.hub-table th{position:sticky;top:0;background:#f9fafb;z-index:1}\n.hub-warn{padding:9px 11px;background:#fff8e6;color:#7a2e0e;font-size:11px;border-top:1px solid #fedf89}\n\n\n.land-box{margin:12px 0;border:1px solid #d0d5dd;border-radius:10px;background:#fff;overflow:hidden}\n.land-head{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:10px 12px;background:#f9fafb;border-bottom:1px solid #eaecf0;font-size:12px}\n.land-head b{font-size:13px}\n.land-table-wrap{max-height:300px;overflow:auto}\n.land-table{width:100%;border-collapse:collapse;font-size:11px}\n.land-table th,.land-table td{padding:7px 8px;border-bottom:1px solid #f2f4f7;text-align:left;white-space:nowrap}\n.land-table th{position:sticky;top:0;background:#f9fafb;z-index:1}\n.land-note{padding:9px 11px;background:#f8fafc;color:#475467;font-size:11px;border-top:1px solid #eaecf0}\n.small-official{color:#b42318;font-weight:800}\n.small-estimate{color:#b54708;font-weight:700}\n\n\n.road-box{margin:12px 0;border:1px solid #d0d5dd;border-radius:10px;background:#fff;overflow:hidden}\n.road-head{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:10px 12px;background:#f9fafb;border-bottom:1px solid #eaecf0;font-size:12px}\n.road-head b{font-size:13px}.road-table-wrap{max-height:300px;overflow:auto}.road-table{width:100%;border-collapse:collapse;font-size:11px}.road-table th,.road-table td{padding:7px 8px;border-bottom:1px solid #f2f4f7;text-align:left;white-space:nowrap}.road-table th{position:sticky;top:0;background:#f9fafb;z-index:1}.road-note{padding:9px 11px;background:#fff8e6;color:#7a2e0e;font-size:11px;border-top:1px solid #fedf89}.road-tools{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.road-tools input[type=file]{font-size:11px;max-width:260px}\n\n\n.analysis-layer-box{margin:10px 18px 0;padding:10px 12px;border:1px solid #d0d5dd;border-radius:10px;background:#fff;display:flex;gap:12px;align-items:center;flex-wrap:wrap;font-size:11px}\n.analysis-layer-box .section-title{margin:0 6px 0 0}\n.analysis-layer-box label{display:flex;gap:5px;align-items:center;font-weight:700;color:#344054}\n.analysis-layer-summary{flex-basis:100%;color:#667085;font-size:10px;padding-top:2px}\n.density-box{margin:12px 0;border:1px solid #d0d5dd;border-radius:10px;background:#fff;overflow:hidden}\n.density-head{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:10px 12px;background:#f9fafb;border-bottom:1px solid #eaecf0;font-size:12px}\n.density-head b{font-size:13px}\n.density-table-wrap{max-height:280px;overflow:auto}\n.density-table{width:100%;border-collapse:collapse;font-size:11px}\n.density-table th,.density-table td{padding:7px 8px;border-bottom:1px solid #f2f4f7;text-align:left;white-space:nowrap}\n.density-table th{position:sticky;top:0;background:#f9fafb;z-index:1}\n.density-note{padding:9px 11px;background:#fff8e6;color:#7a2e0e;font-size:11px;border-top:1px solid #fedf89}\n\n\n.hub-stats{display:grid;grid-template-columns:repeat(6,minmax(90px,1fr));gap:0;border-bottom:1px solid #eaecf0;background:#fff}\n.hub-stats>div{padding:8px 10px;border-right:1px solid #f2f4f7}\n.hub-stats>div:last-child{border-right:0}\n.hub-stats span{display:block;font-size:10px;color:#667085;margin-bottom:2px}\n.hub-stats b{font-size:14px;color:#101828}\n.road-actions{display:flex;gap:6px;align-items:center;flex-wrap:wrap}\n.road-link,.road-upload-label{display:inline-flex;align-items:center;padding:6px 9px;border:1px solid #d0d5dd;border-radius:7px;background:white;color:#344054;font-size:10px;font-weight:800;text-decoration:none;cursor:pointer}\n.road-upload-label input{display:none}\n@media(max-width:900px){.hub-stats{grid-template-columns:repeat(3,1fr)}}\n\n\n.strategy-panel{margin-top:14px}\n.strategy-screening{background:#fff}\n.strategy-screening-head{display:flex;justify-content:space-between;gap:12px;align-items:center;padding:14px 16px;background:#f9fafb;border-bottom:1px solid #eaecf0}\n.strategy-screening-head>div{display:flex;flex-direction:column;gap:3px}\n.strategy-screening-head b{font-size:14px}\n.strategy-screening-head span{font-size:10px;color:#667085}\n.strategy-screening-head strong{font-size:18px}\n.strategy-grid{display:grid;grid-template-columns:repeat(4,1fr);border-bottom:1px solid #eaecf0}.strategy-grid-6{grid-template-columns:repeat(6,1fr)}\n.strategy-grid>div{padding:12px 14px;border-right:1px solid #f2f4f7}\n.strategy-grid>div:last-child{border-right:0}\n.strategy-grid span{display:block;font-size:10px;color:#667085;margin-bottom:4px}\n.strategy-grid b{font-size:16px;color:#101828}\n.strategy-reason{padding:11px 14px;font-size:11px;color:#344054}\n.strategy-caveat{padding:9px 14px;background:#fff8e6;border-top:1px solid #fedf89;color:#7a2e0e;font-size:10px}\n@media(max-width:1100px){.strategy-grid-6{grid-template-columns:repeat(3,1fr)}}@media(max-width:900px){.strategy-grid{grid-template-columns:repeat(2,1fr)}.strategy-grid-6{grid-template-columns:repeat(2,1fr)}}\n\n\n.scheme-panel{grid-column:1/-1}\n.scheme-body{padding:14px 16px 18px}\n.scheme-auto-strip{display:grid;grid-template-columns:repeat(5,1fr);border:1px solid var(--line);border-radius:12px;overflow:hidden;background:#fff;margin-bottom:12px}\n.scheme-auto-strip>div{padding:10px 12px;border-right:1px solid #f2f4f7}\n.scheme-auto-strip>div:last-child{border-right:0}\n.scheme-auto-strip span{display:block;font-size:10px;color:#667085;margin-bottom:3px}\n.scheme-auto-strip b{font-size:15px}\n.scheme-input-details{margin:0 0 12px}\n.scheme-input-inner{padding:0 12px 12px}\n.scheme-input-grid{display:grid;grid-template-columns:repeat(6,minmax(130px,1fr));gap:9px}\n.scheme-specific-grid{display:grid;grid-template-columns:repeat(6,minmax(130px,1fr));gap:9px;margin-top:10px;padding-top:10px;border-top:1px solid #eaecf0}\n.scheme-field label{display:block;font-size:10px;color:#475467;font-weight:800;margin-bottom:4px}\n.scheme-field input,.scheme-field select{width:100%;padding:8px 9px;border:1px solid #cfd5dc;border-radius:8px;background:#fff;font-size:12px}\n.scheme-field small{display:block;color:#667085;font-size:9px;margin-top:2px}\n.scheme-check-grid{display:flex;gap:16px;flex-wrap:wrap;padding:10px 2px 0;font-size:11px;color:#344054}\n.scheme-check-grid label{display:flex;align-items:center;gap:5px;font-weight:700}\n.scheme-card-grid{display:grid;grid-template-columns:repeat(6,1fr);gap:8px;margin:12px 0}.scheme-card-grid-8{grid-template-columns:repeat(4,1fr)}\n.scheme-card{border:1px solid #d0d5dd;border-radius:11px;background:#fff;padding:11px;text-align:left;cursor:pointer;min-height:90px}\n.scheme-card.active{outline:2px solid #84adff;border-color:#84adff;background:#f5f8ff}\n.scheme-card span{display:block;font-size:11px;font-weight:850;min-height:30px;color:#344054}\n.scheme-card b{display:block;font-size:15px;margin:4px 0}\n.scheme-card small{display:block;font-size:9px;color:#667085;line-height:1.35}\n.scheme-card[data-state="PASS"] b{color:var(--green)}\n.scheme-card[data-state="FAIL"] b{color:var(--red)}\n.scheme-card[data-state="REVIEW"] b{color:var(--amber)}\n.scheme-sheet{border:1px solid #d0d5dd;border-radius:12px;overflow:hidden}\n.scheme-sheet-head{display:flex;justify-content:space-between;gap:12px;align-items:center;padding:12px 14px;background:#f9fafb;border-bottom:1px solid #eaecf0}\n.scheme-sheet-head h3{margin:0 0 3px;font-size:15px}\n.scheme-basis{font-size:10px;color:#667085}\n.scheme-overall{font-size:13px;font-weight:900;border:1px solid #d0d5dd;border-radius:999px;padding:6px 10px;background:#fff}\n.scheme-table-wrap{overflow:auto;max-height:480px}\n.scheme-table{margin:0;font-size:11px}\n.scheme-table th{top:0;z-index:2}\n.scheme-table td:nth-child(1){font-weight:800;min-width:125px}\n.scheme-table td:nth-child(2){min-width:220px}\n.scheme-table td:nth-child(3){min-width:135px}\n.scheme-table td:nth-child(5){min-width:240px;color:#667085}\n.scheme-caveat{padding:10px 12px;background:#fff8e6;border-top:1px solid #fedf89;color:#7a2e0e;font-size:10px}\n.scheme-tag{display:inline-block;font-size:9px;font-weight:900;border-radius:999px;padding:3px 7px;background:#f2f4f7}\n.scheme-tag.PASS{background:#ecfdf3;color:#067647}.scheme-tag.FAIL{background:#fef3f2;color:#b42318}.scheme-tag.REVIEW{background:#fffaeb;color:#b54708}.scheme-tag.INFO{background:#f2f4f7;color:#475467}\n@media(max-width:1350px){.scheme-input-grid,.scheme-specific-grid{grid-template-columns:repeat(4,1fr)}.scheme-card-grid{grid-template-columns:repeat(3,1fr)}}\n@media(max-width:900px){.scheme-input-grid,.scheme-specific-grid{grid-template-columns:repeat(2,1fr)}.scheme-auto-strip{grid-template-columns:repeat(2,1fr)}.scheme-card-grid{grid-template-columns:repeat(2,1fr)}}\n@media(max-width:560px){.scheme-input-grid,.scheme-specific-grid,.scheme-card-grid{grid-template-columns:1fr}}\n\n\n.scheme-density-strip{display:grid;grid-template-columns:1fr 1.2fr 1.8fr;border-bottom:1px solid #eaecf0;background:#fff}\n.scheme-density-strip>div{padding:10px 13px;border-right:1px solid #f2f4f7}\n.scheme-density-strip>div:last-child{border-right:0}\n.scheme-density-strip span{display:block;font-size:9px;color:#667085;margin-bottom:3px;font-weight:800}\n.scheme-density-strip b{display:block;font-size:12px;line-height:1.45;color:#101828}\n@media(max-width:900px){.scheme-density-strip{grid-template-columns:1fr}.scheme-density-strip>div{border-right:0;border-bottom:1px solid #f2f4f7}.scheme-card-grid-8{grid-template-columns:repeat(2,1fr)}}\n\n\n.scheme-compare-title{display:flex;justify-content:space-between;align-items:center;gap:12px;margin:14px 0 8px}\n.scheme-compare-title h3{margin:0;font-size:15px}\n.scheme-compare-title span{display:block;margin-top:3px;font-size:10px;color:#667085}\n.scheme-compare-wrap{overflow:auto;border:1px solid #d0d5dd;border-radius:12px;background:#fff;margin-bottom:14px}\n.scheme-compare-table{width:100%;border-collapse:collapse;font-size:10px;min-width:1250px}\n.scheme-compare-table th,.scheme-compare-table td{padding:9px 10px;border-bottom:1px solid #f2f4f7;border-right:1px solid #f2f4f7;text-align:left;vertical-align:top}\n.scheme-compare-table th{position:sticky;top:0;background:#f9fafb;z-index:2}\n.scheme-compare-table th:last-child,.scheme-compare-table td:last-child{border-right:0}\n.scheme-compare-table td:first-child{font-weight:900;min-width:145px}\n.scheme-compare-table td:nth-child(4){min-width:180px}\n.scheme-compare-table td:nth-child(5){min-width:260px}\n.scheme-compare-table td:nth-child(6){min-width:330px}\n.scheme-compare-status{display:inline-block;border-radius:999px;padding:4px 8px;font-size:10px;font-weight:900}\n.scheme-compare-status.PASS{background:#ecfdf3;color:#067647}\n.scheme-compare-status.FAIL{background:#fef3f2;color:#b42318}\n.scheme-compare-status.REVIEW{background:#fffaeb;color:#b54708}\n.all-scheme-sheets{display:grid;grid-template-columns:1fr;gap:12px}\n.all-scheme-sheet{border:1px solid #d0d5dd;border-radius:12px;overflow:hidden;background:#fff}\n.all-scheme-sheet-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;padding:12px 14px;background:#f9fafb;border-bottom:1px solid #eaecf0}\n.all-scheme-sheet-head h3{margin:0 0 3px;font-size:14px}\n.all-scheme-sheet-head .basis{font-size:9px;color:#667085}\n.all-scheme-density{display:grid;grid-template-columns:1fr 1.2fr 1.8fr;border-bottom:1px solid #eaecf0}\n.all-scheme-density>div{padding:9px 12px;border-right:1px solid #f2f4f7}\n.all-scheme-density>div:last-child{border-right:0}\n.all-scheme-density span{display:block;font-size:9px;color:#667085;font-weight:800;margin-bottom:3px}\n.all-scheme-density b{font-size:11px;line-height:1.45}\n.all-scheme-table-wrap{overflow:auto;max-height:360px}\n.all-scheme-table{width:100%;border-collapse:collapse;font-size:10px}\n.all-scheme-table th,.all-scheme-table td{padding:7px 8px;border-bottom:1px solid #f2f4f7;text-align:left;vertical-align:top}\n.all-scheme-table th{background:#f9fafb;position:sticky;top:0;z-index:1}\n.all-scheme-table td:nth-child(1){font-weight:800;min-width:120px}\n.all-scheme-table td:nth-child(2){min-width:200px}\n.all-scheme-table td:nth-child(3){min-width:130px}\n.all-scheme-table td:nth-child(5){min-width:220px;color:#667085}\n.all-scheme-loading{padding:18px;text-align:center;color:#667085}\n@media(max-width:900px){\n  .all-scheme-density{grid-template-columns:1fr}\n  .all-scheme-density>div{border-right:0;border-bottom:1px solid #f2f4f7}\n  .scheme-compare-title{align-items:flex-start;flex-direction:column}\n}\n\n\n.station-gis-box{margin:0 0 12px;border:1px solid #b2ccff;border-radius:12px;background:#f8faff;overflow:hidden}\n.station-gis-head{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:11px 13px;border-bottom:1px solid #d1e0ff}\n.station-gis-head>div:first-child{display:flex;flex-direction:column;gap:3px}\n.station-gis-head b{font-size:13px}.station-gis-head span{font-size:9px;color:#475467}\n.station-gis-actions{display:flex;gap:6px;align-items:center;flex-wrap:wrap}\n.station-file-label{display:inline-flex;align-items:center;padding:7px 9px;border:1px solid #84adff;border-radius:8px;background:#fff;color:#175cd3;font-size:10px;font-weight:900;cursor:pointer}\n.station-file-label input{display:none}\n.station-mini-btn{padding:7px 9px!important;font-size:10px}\n.station-gis-stats{display:grid;grid-template-columns:repeat(6,1fr);background:#fff}\n.station-gis-stats>div{padding:9px 10px;border-right:1px solid #f2f4f7}\n.station-gis-stats>div:last-child{border-right:0}\n.station-gis-stats span{display:block;font-size:9px;color:#667085;margin-bottom:3px}\n.station-gis-stats b{display:block;font-size:12px;color:#101828}\n.station-gis-note{padding:8px 11px;font-size:9px;color:#475467;border-top:1px solid #eaecf0;background:#fcfcfd}\n@media(max-width:1100px){.station-gis-stats{grid-template-columns:repeat(3,1fr)}}\n@media(max-width:640px){.station-gis-head{align-items:flex-start;flex-direction:column}.station-gis-stats{grid-template-columns:repeat(2,1fr)}}\n\n\n/* v1.5 first-screen information architecture */\nbody{background:#f4f5f7}\n.cc-header{padding:16px 20px 11px;background:#fff;border-bottom:1px solid #e4e7ec}\n.cc-header h1{font-size:22px;letter-spacing:-.4px;color:#d92d20}\n.cc-header .sub{font-size:10px;color:#d92d20;font-weight:800}\n.cc-decision{margin:12px 12px 10px;display:grid;grid-template-columns:180px 210px 210px minmax(520px,1fr);gap:8px;padding:10px;background:#eaecf0;border:1px solid #e4e7ec}\n.cc-decision-input{background:#fff;padding:10px 12px;min-height:116px}\n.cc-decision-label{font-size:16px;font-weight:900;color:#344054;margin-bottom:9px}\n.cc-decision-input select{width:100%;border:1px solid #d0d5dd;border-radius:5px;padding:8px;font-size:12px;background:#fff}\n.cc-decision-input small{display:block;margin-top:7px;color:#d92d20;font-size:9px;font-weight:750;line-height:1.35}\n.cc-candidate-panel{background:#fff;padding:8px 10px;min-width:0}\n.cc-candidate-head{display:flex;justify-content:space-between;align-items:flex-end;gap:10px;padding:1px 2px 7px;border-bottom:1px solid #eaecf0}\n.cc-candidate-head b{font-size:16px;color:#344054}\n.cc-candidate-head span{font-size:9px;color:#667085;text-align:right}\n.cc-scheme-icons{display:grid;grid-template-columns:repeat(4,minmax(90px,1fr));gap:5px;padding:7px 0}\n.cc-scheme-icons button{appearance:none;border:1px solid #d0d5dd;background:#f2f4f7;color:#98a2b3;min-height:50px;padding:5px 6px;cursor:pointer;display:grid;grid-template-columns:22px 1fr;grid-template-rows:1fr 1fr;align-items:center;text-align:left;transition:.12s}\n.cc-scheme-icons button i{grid-row:1/3;font-style:normal;font-size:17px;text-align:center;filter:grayscale(1);opacity:.55}\n.cc-scheme-icons button span{font-size:10px;font-weight:900;line-height:1.15}\n.cc-scheme-icons button em{font-style:normal;font-size:8px;font-weight:800}\n.cc-scheme-icons button.candidate-on{background:#175cd3;border-color:#175cd3;color:#fff}\n.cc-scheme-icons button.candidate-on i{filter:none;opacity:1}\n.cc-scheme-icons button.candidate-mid{background:#d1e9ff;border-color:#84adff;color:#1849a9}\n.cc-scheme-icons button.candidate-mid i{filter:none;opacity:.9}\n.cc-scheme-icons button.candidate-off{background:#f2f4f7;border-color:#e4e7ec;color:#98a2b3;opacity:.62}\n.cc-scheme-icons button.candidate-selected{outline:2px solid #101828;outline-offset:1px}\n.cc-scheme-basis{min-height:38px;border-top:1px solid #eaecf0;padding:7px 2px 0;display:flex;gap:8px;align-items:flex-start;font-size:9px;line-height:1.4;color:#475467}\n.cc-scheme-basis b{white-space:nowrap;color:#101828}\n.cc-scheme-basis .cc-basis-actions{margin-left:auto;white-space:nowrap}\n.cc-scheme-basis button{font-size:8px;padding:4px 7px;border:1px solid #d0d5dd;background:#fff;cursor:pointer}\n\n.shell{padding:0 12px 16px}\n.cc-map-workspace{overflow:visible}\n.cc-map-head-right{display:flex;gap:6px}\n.cc-workgrid{display:grid;grid-template-columns:minmax(0,2.25fr) minmax(300px,.95fr);gap:8px;padding:8px;background:#f2f4f7}\n.cc-map-column{min-width:0;background:#fff}\n#map{height:555px!important;width:100%}\n.cc-map-instruction{padding:6px 8px;font-size:10px;color:#d92d20;font-weight:800;border-top:1px solid #eaecf0}\n.cc-info-rail{display:grid;grid-template-rows:1fr 1fr;gap:8px;min-height:555px}\n.cc-info-card{background:#fff;border:1px solid #eaecf0;min-height:0;overflow:hidden}\n.cc-info-head{display:flex;justify-content:space-between;align-items:center;padding:10px 12px;background:#f9fafb;border-bottom:1px solid #eaecf0}\n.cc-info-head b{font-size:18px;color:#101828}\n.cc-info-head span{font-size:9px;color:#667085}\n.cc-info-metrics{display:grid;grid-template-columns:1fr 1fr}\n.cc-info-metrics>div{padding:10px 11px;border-right:1px solid #f2f4f7;border-bottom:1px solid #f2f4f7}\n.cc-info-metrics>div:nth-child(2n){border-right:0}\n.cc-info-metrics span{display:block;font-size:9px;color:#667085;margin-bottom:3px}\n.cc-info-metrics b{font-size:15px;color:#101828}\n.cc-info-section-title{font-size:10px;font-weight:900;color:#475467;padding:8px 10px 4px}\n.cc-legend{padding:4px 9px 9px;display:flex;flex-wrap:wrap;gap:5px;align-content:flex-start}\n.cc-legend>span{display:inline-flex;gap:4px;align-items:center;padding:4px 6px;background:#f2f4f7;border:1px solid #eaecf0;font-size:9px;color:#475467}\n.cc-legend>span b{font-size:9px;color:#101828}\n.cc-map-tools,.cc-data-tools{margin:7px 8px;background:#fff;border:1px solid #e4e7ec}\n.cc-map-tools summary,.cc-data-tools summary{cursor:pointer;padding:9px 11px;font-size:10px;font-weight:900;color:#344054;background:#f9fafb}\n.cc-detail-pad{padding:8px}\n.cc-data-tools .form-wrap{padding:0}\n.cc-data-tools .section-title:first-child{margin-top:0}\n.leaflet-top.leaflet-right .leaflet-draw{transform:scale(1.2);transform-origin:top right;margin-top:14px;margin-right:14px}\n.leaflet-draw-toolbar a{border-radius:3px}\n@media(max-width:1250px){\n  .cc-decision{grid-template-columns:160px 180px 180px 1fr}\n  .cc-scheme-icons{grid-template-columns:repeat(4,minmax(78px,1fr))}\n}\n@media(max-width:1000px){\n  .cc-decision{grid-template-columns:repeat(3,1fr)}\n  .cc-candidate-panel{grid-column:1/-1}\n  .cc-workgrid{grid-template-columns:1fr}\n  .cc-info-rail{grid-template-columns:1fr 1fr;grid-template-rows:auto;min-height:0}\n}\n@media(max-width:650px){\n  .cc-decision{grid-template-columns:1fr}\n  .cc-candidate-panel{grid-column:auto}\n  .cc-scheme-icons{grid-template-columns:repeat(2,1fr)}\n  .cc-info-rail{grid-template-columns:1fr}\n}\n\n</style>\n</head>\n<body>\n<header class="cc-header">\n  <div>\n    <h1>컴팩트시티플랫폼</h1>\n    <div class="sub">역세권 정비제도 · 8개 사업방식 통합검토 · Strategy Engine v1.5.0</div>\n  </div>\n  <div class="connection">\n    <span class="conn auto">구역면적 AUTO</span>\n    <span id="hubConn" class="conn planned">건축HUB 준비</span>\n    <span id="parcelConn" class="conn planned">필지공간 AUTO</span>\n    <span id="landConn" class="conn planned">토지대장 준비</span>\n    <span id="buildingConn" class="conn planned">건축물 AUTO 준비</span>\n  </div>\n</header>\n\n<section class="cc-decision">\n  <div class="cc-decision-input">\n    <div class="cc-decision-label">목적사업</div>\n    <select id="ccPurpose" onchange="updateCandidateSchemes()">\n      <option value="">선택</option>\n      <option value="housing">주거</option>\n      <option value="commercial">상업</option>\n      <option value="office">업무</option>\n      <option value="lodging">숙박</option>\n      <option value="mixed">주거·상업·업무 복합</option>\n    </select>\n    <small>주된 개발기능</small>\n  </div>\n\n  <div class="cc-decision-input">\n    <div class="cc-decision-label">토지확보여부</div>\n    <select id="ccLandControl" onchange="updateCandidateSchemes()">\n      <option value="">선택</option>\n      <option value="secured">대부분 확보</option>\n      <option value="partial">일부 확보·매입진행</option>\n      <option value="unsecured">미확보</option>\n      <option value="owners">토지등소유자 공동추진</option>\n    </select>\n    <small>매입형·정비사업형 구분</small>\n  </div>\n\n  <div class="cc-decision-input">\n    <div class="cc-decision-label">의뢰·추진주체</div>\n    <select id="ccActor" onchange="updateCandidateSchemes()">\n      <option value="">선택</option>\n      <option value="private_owner">토지소유자</option>\n      <option value="private_developer">민간사업자</option>\n      <option value="owners_association">토지등소유자·조합</option>\n      <option value="public">공공</option>\n      <option value="public_private">공공+민간</option>\n    </select>\n    <small>사업방식 1차 선별용</small>\n  </div>\n\n  <div class="cc-candidate-panel">\n    <div class="cc-candidate-head">\n      <b>검토가능 사업방식</b>\n      <span id="ccCandidateSummary">목적사업 · 토지확보 · 의뢰주체를 선택하면 우선 검토방식이 표시됩니다.</span>\n    </div>\n    <div class="cc-scheme-icons" id="ccSchemeIcons">\n      <button data-scheme="redevelopment" onclick="showCandidateBasis(\'redevelopment\')"><i>🏘</i><span>주택재개발</span><em>입력필요</em></button>\n      <button data-scheme="reconstruction" onclick="showCandidateBasis(\'reconstruction\')"><i>▥</i><span>주택재건축</span><em>입력필요</em></button>\n      <button data-scheme="activation" onclick="showCandidateBasis(\'activation\')"><i>🚉</i><span>역세권활성화</span><em>입력필요</em></button>\n      <button data-scheme="station_complex" onclick="showCandidateBasis(\'station_complex\')"><i>▦</i><span>역세권복합지단</span><em>입력필요</em></button>\n      <button data-scheme="safe" onclick="showCandidateBasis(\'safe\')"><i>⌂</i><span>안심주택</span><em>입력필요</em></button>\n      <button data-scheme="longterm" onclick="showCandidateBasis(\'longterm\')"><i>∞</i><span>역세권장기전세</span><em>입력필요</em></button>\n      <button data-scheme="public_complex" onclick="showCandidateBasis(\'public_complex\')"><i>▧</i><span>도심공공주택</span><em>입력필요</em></button>\n      <button data-scheme="innovation" onclick="showCandidateBasis(\'innovation\')"><i>◆</i><span>도심복합개발</span><em>입력필요</em></button>\n    </div>\n    <div id="ccSchemeBasis" class="cc-scheme-basis">\n      <b>사업방식 선택근거</b>\n      <span>위 아이콘을 클릭하면 해당 제도의 적용근거·현재 판정·용적률·공공기여를 표시합니다.</span>\n    </div>\n  </div>\n</section>\n\n<div class="shell">\n  \n<section class="panel cc-map-workspace">\n  <div class="panel-head cc-map-head">\n    <h2>1. 사업구역 / 위치도</h2>\n    <div class="cc-map-head-right">\n      <span id="ccMapRole" class="badge">사업지 범위를 그리시오</span>\n      <span class="badge">서울 중심</span>\n    </div>\n  </div>\n\n  <div class="cc-workgrid">\n    <div class="cc-map-column">\n      <div id="map"></div>\n      <div class="cc-map-instruction" id="ccMapInstruction">우측 상단의 그리기 아이콘으로 사업구역을 설정하세요. 구역 확정 후 이 지도는 위치도로 계속 사용됩니다.</div>\n      <div class="map-foot">\n<div class="metric"><div class="k">구역면적</div><div class="v" id="mArea">-</div></div>\n<div class="metric"><div class="k">면적(ha)</div><div class="v" id="mHa">-</div></div>\n<div class="metric"><div class="k">둘레</div><div class="v" id="mPerimeter">-</div></div>\n</div>\n    </div>\n\n    <aside class="cc-info-rail">\n      <section class="cc-info-card">\n        <div class="cc-info-head">\n          <b>토지</b>\n          <span id="ccLandState">구역 설정 전</span>\n        </div>\n        <div class="cc-info-metrics">\n          <div><span>구역면적</span><b id="ccLandArea">-</b></div>\n          <div><span>필지수</span><b id="ccLandParcels">-</b></div>\n          <div><span>90㎡ 미만</span><b id="ccLandSmall">-</b></div>\n          <div><span>공식면적 확보</span><b id="ccLandOfficial">-</b></div>\n        </div>\n        <div class="cc-info-section-title">지목 구성</div>\n        <div id="ccLandLegend" class="cc-legend"><span>토지대장 조회 후 표시</span></div>\n      </section>\n\n      <section class="cc-info-card">\n        <div class="cc-info-head">\n          <b>건축물</b>\n          <span id="ccBuildingState">구역 설정 전</span>\n        </div>\n        <div class="cc-info-metrics">\n          <div><span>건축물</span><b id="ccBuildingTotal">-</b></div>\n          <div><span>노후건축물</span><b id="ccBuildingOld">-</b></div>\n          <div><span>노후도</span><b id="ccBuildingOldRatio">-</b></div>\n          <div><span>총연면적</span><b id="ccBuildingFloorArea">-</b></div>\n        </div>\n        <div class="cc-info-section-title">주용도 구성</div>\n        <div id="ccBuildingLegend" class="cc-legend"><span>건축HUB 조회 후 표시</span></div>\n      </section>\n    </aside>\n  </div>\n\n  <details class="cc-map-tools">\n    <summary>필지 편집 · 지도 분석레이어</summary>\n    <div class="cc-detail-pad">\n      <div class="analysis-layer-box">\n<div class="section-title">지도 분석레이어</div>\n<label><input checked="" id="lyAllParcels" onchange="refreshAnalysisLayers()" type="checkbox"/> 전체필지</label>\n<label><input checked="" id="lySmall" onchange="refreshAnalysisLayers()" type="checkbox"/> 90㎡ 미만 필지</label>\n<label><input checked="" id="lyOld" onchange="refreshAnalysisLayers()" type="checkbox"/> 노후건축물 소재필지</label>\n<div class="analysis-layer-summary" id="analysisLayerSummary">필지·건축물 원장은 향후 역세권·지구단위·복합개발 검토에 재사용합니다.</div>\n</div>\n      <div class="boundary-box">\n<div class="section-title">1-1. 필지 기반 구역계 편집</div>\n<div class="toolbar">\n<textarea id="parcelListInput" placeholder="PNU 19자리 목록을 줄바꿈·쉼표로 붙여넣기\n예) 1111010100100010000"></textarea>\n<div style="display:flex;gap:6px;flex-wrap:wrap;max-width:360px">\n<button class="btn" onclick="addParcelsFromList()">PNU 목록 추가</button>\n<button class="btn" onclick="loadNearbyParcels()">주변필지 불러오기</button>\n<button class="btn" onclick="selectAllCurrentParcels()">현재필지 전체포함</button>\n<button class="btn" onclick="clearParcelSelection()">선택 해제</button>\n<button class="btn primary" onclick="applySelectedParcelsAsBoundary()">선택필지로 구역계 갱신</button>\n</div>\n</div>\n<div class="parcel-summary" id="parcelSelectionSummary">구역을 그리면 교차필지가 목록으로 들어옵니다. 지도에서 필지를 클릭하면 포함/제외를 바꿀 수 있습니다.</div>\n<div class="parcel-list" id="parcelSelectionList"></div>\n</div>\n    </div>\n  </details>\n\n  <details class="cc-data-tools">\n    <summary>상세 토지 · 건축물 원장 및 재조회</summary>\n    <div class="cc-detail-pad">\n      <div class="form-wrap">\n<div class="section-title">2. 대상지 기초정보 · 재개발 노후도</div>\n<div class="grid">\n<div class="field"><label>구역면적(㎡) <span class="PASS">AUTO</span></label><input id="area_m2" placeholder="지도를 그리면 자동입력" readonly="" type="number"/></div>\n<div class="field"><label>전체 건축물 수 <span class="PASS">AUTO(1차)</span></label><input id="total_building_count" placeholder="VWorld 건물공간 자동 / 수기 보완 가능" type="number"/><small>LT_C_SPBD 건물관리번호 기준 · 법정 산정은 건축HUB 보정 예정</small></div>\n<div class="field"><label>노후건축물 수 <span class="PASS">건축HUB AUTO</span></label><input id="old_building_count" placeholder="건축HUB 자동 / 수기 보완 가능" type="number"/><small>사용승인일·구조·주용도 기반 연령판정 · 민간 초기 사업검토용</small></div>\n<div class="field"><label>전체 필지 수 <span class="PASS">AUTO</span></label><input id="total_parcel_count" placeholder="VWorld 자동 / 수기 보완 가능" type="number"/><small>연속지적 LP_PA_CBND_BUBUN</small></div>\n<div class="field"><label>90㎡ 미만 필지 수 <span class="PASS">AUTO</span> <span class="PASS">AUTO</span></label><input id="small_parcel_count" placeholder="VWorld 자동 / 수기 보완 가능" type="number"/><small>토지특성 공식면적(lndpclAr) 기준</small></div>\n<div class="field secondary-criterion" style="display:none"><label>접도율 산정 건축물 수 <span class="PASS">AUTO</span></label><input id="road_basis_building_count" placeholder="건축HUB/건물공간 자동 · 수기 보완 가능" type="number"/><small>선택필지 내 건축물 수</small></div>\n<div class="field secondary-criterion" style="display:none"><label>6m 이상 도로 접도 건축물 수 <span class="PASS">AUTO</span></label><input id="road_access_building_count_6m" placeholder="실폭도로 공간연산 자동 · 수기 보완 가능" type="number"/><small>폭 6m 이상 + 대지 접도길이 4m 이상</small></div>\n<div class="field secondary-criterion" style="display:none"><label>호수밀도(호/ha) <span class="REVIEW" id="densityBadge">AUTO(초기검토)</span></label><input id="house_density_per_ha" placeholder="건축HUB 자동추정 / 수기 보완" step="0.01" type="number"/><small>공동·다가구는 세대/가구수÷지상층수 추정, 비주거는 건축면적÷90㎡, 공원·학교용지는 예비 제외</small></div>\n<div class="field secondary-criterion" style="display:none"><label>전체 건축물 연면적(㎡) <span class="PASS">HUB AUTO</span></label><input id="total_floor_area_m2" placeholder="건축HUB 자동 / 수기 보완 가능" type="number"/></div>\n<div class="field secondary-criterion" style="display:none"><label>노후·불량건축물 연면적(㎡) <span class="PASS">HUB AUTO(연령)</span></label><input id="old_floor_area_m2" placeholder="건축HUB 자동 / 수기 보완 가능" type="number"/></div>\n<div class="field secondary-criterion" style="display:none"><label>입안요청 토지등소유자 동의율(%)</label><input id="request_owner_consent_ratio" max="100" min="0" placeholder="선택 입력" type="number"/></div>\n<div class="field secondary-criterion" style="display:none"><label>입안제안 토지등소유자 동의율(%)</label><input id="proposal_owner_consent_ratio" max="100" min="0" placeholder="선택 입력" type="number"/></div>\n<div class="field secondary-criterion" style="display:none"><label>입안제안 토지면적 동의율(%)</label><input id="proposal_land_area_consent_ratio" max="100" min="0" placeholder="선택 입력" type="number"/></div>\n</div>\n<span style="display:none"><label class="checkline"><input id="promotion_district" type="checkbox"/> 재정비촉진지구</label></span>\n<span style="display:none"><label class="checkline"><input id="area_5000_exception_approved" type="checkbox"/> 5,000~10,000㎡ 관련 위원회 심의 인정 확인</label></span>\n<div class="actions"><button class="btn" onclick="loadSample()">샘플값</button><button class="btn" onclick="clearInputs()">초기화</button><button class="btn" onclick="analyzeParcels()">필지공간 재조회</button><button class="btn" onclick="analyzeLandLedger()">토지대장 조회</button><button class="btn" onclick="analyzeBuildings()">건축물공간 재조회</button><button class="btn" onclick="analyzeBuildingHub()">건축HUB 대장조회</button><button class="btn primary" disabled="" id="runBtn" onclick="runEvaluation()">재개발 가능성 검토</button></div>\n<div class="hint" id="parcelStatus"><b>과소필지 AUTO:</b> 구역을 그리면 브라우저가 VWorld 연속지적을 직접 조회해 교차필지를 찾고, 각 PNU의 토지특성 공식 면적(lndpclAr)을 조회해 90㎡ 미만 필지를 자동집계한다. VWorld API 키가 없거나 일부 필지의 공식면적을 확인하지 못하면 자동 판정을 강행하지 않고 수기 보완 상태로 남긴다.</div>\n<div class="land-box">\n<div class="land-head">\n<b>토지임야대장 속성정보</b>\n<span id="landSummary">필지 선택 후 ‘토지대장 조회’를 실행하세요.</span>\n</div>\n<div class="land-table-wrap" id="landTableWrap" style="display:none">\n<table class="land-table">\n<thead><tr>\n<th>지번</th><th>PNU</th><th>지목</th><th>토지대장 공식면적㎡</th><th>연속지적 계산면적㎡</th><th>건축HUB 대지면적㎡(보조)</th>\n<th>소유구분</th><th>공유인수</th><th>축척</th><th>데이터기준일</th><th>과소필지</th>\n</tr></thead>\n<tbody id="landTableBody"></tbody>\n</table>\n</div>\n<div class="land-note">필지 원장은 토지대장 공식면적을 우선 표시하고, 미응답 시 연속지적 경계의 계산면적을 보조값으로 표시합니다. 건축HUB 대지면적은 건축물 소재 대지의 교차확인용으로만 사용합니다.</div>\n</div>\n<div class="hint" id="buildingStatus"><b>건축물 AUTO:</b> VWorld 도로명주소 건물(LT_C_SPBD)을 대상구역과 교차시켜 건물 폴리곤과 1차 건물 수를 자동 산정한다. 이 값은 공간상 후보 건물 수이며, 재개발 법정 건축물 동수와 노후·불량 판정은 건축HUB 건축물대장 연결 후 보정한다.</div>\n<div class="hub-box">\n<div class="hub-head">\n<b>건축HUB 건축물대장 표제부</b>\n<span id="hubSummary">선택필지 확정 후 ‘건축HUB 대장조회’를 실행하세요.</span>\n</div>\n<div class="hub-stats" id="hubStats">\n<div><span>전체 건축물</span><b id="hubStatTotal">-</b></div>\n<div><span>노후</span><b id="hubStatOld">-</b></div>\n<div><span>비노후</span><b id="hubStatNotOld">-</b></div>\n<div><span>미판정</span><b id="hubStatUnknown">-</b></div>\n<div><span>노후도</span><b id="hubStatRatio">-</b></div>\n<div><span>노후연면적률</span><b id="hubStatFloorRatio">-</b></div>\n</div>\n<div class="hub-table-wrap" id="hubTableWrap" style="display:none">\n<table class="hub-table">\n<thead><tr>\n<th>대지위치</th><th>동/건물명</th><th>사용승인일</th><th>구조</th><th>주용도</th>\n<th>지상층</th><th>연면적㎡</th><th>대지면적㎡</th><th>세대</th><th>가구</th><th>연령판정</th><th>대장 생성일</th>\n</tr></thead>\n<tbody id="hubTableBody"></tbody>\n</table>\n</div>\n<div class="hub-warn">노후·불량건축물 자동값은 <b>사용승인일+구조+주용도에 따른 연령기준</b>만 반영합니다. 특정무허가건축물, 미사용승인, 구조적 불량 등은 별도 확인이 필요합니다.</div>\n</div>\n<div class="density-box" style="display:none">\n<div class="density-head">\n<b>호수밀도 초기검토 자동산정</b>\n<span id="densitySummary">건축HUB 조회 후 자동산정합니다.</span>\n</div>\n<div class="density-table-wrap" id="densityTableWrap" style="display:none">\n<table class="density-table">\n<thead><tr><th>대지위치</th><th>주용도</th><th>세대/가구</th><th>지상층</th><th>건축면적㎡</th><th>산정동수</th><th>방법</th></tr></thead>\n<tbody id="densityTableBody"></tbody>\n</table>\n</div>\n<div class="density-note"><b>초기검토 단서:</b> 본 자동분석은 공공데이터 기반 예비검토이며 무허가건축물, 단독→다세대·다가구 변경이력, 존치 공원·학교, 준공업지역 공장 재배치 등은 정비계획 입안 단계에서 공부·현황조사로 재확인해야 합니다.</div>\n</div>\n<div class="source-box">\n<div class="source-title">데이터 출처 · 공공데이터 갱신정보</div>\n<div class="source-grid">\n<div class="k">승강장 경계</div><div>행정안전부 주소기반산업지원서비스 기타자료 · <b>TL_SPSB_STATN 지하철역사</b> SHP · 경계 최단거리/250·350·500m 포함률</div>\n<div class="k">저장방식</div><div>ZIP 1회 연결 → 브라우저 IndexedDB 저장 → 대상구역 변경 시 자동 재분석</div>\n<div class="k">토지/필지</div>\n<div>국토교통부 VWorld 2D Data API · 연속지적 <b>LP_PA_CBND_BUBUN</b></div>\n<div class="k">갱신주기</div>\n<div><b>일간</b> · 공공데이터포털 「국토교통부_연속지적_전국」 기준</div>\n<div class="k">필지면적/토지특성</div>\n<div>국토교통부 VWorld 토지임야정보 <b>ladfrlList</b> 공식면적 우선 · 미응답 시 연속지적 <b>LP_PA_CBND_BUBUN</b> 경계의 계산면적을 초기검토용으로 사용</div>\n<div class="k">갱신주기</div>\n<div>개별 필지 응답의 <b>lastUpdtDt(데이터기준일)</b> 표시 · 공공데이터포털 「국토교통부_토지임야정보(속성정보)」 수정일 2025-07-01</div>\n<div class="k">건축물(현재 1차)</div>\n<div>국토교통부 VWorld 2D Data API · 도로명주소 건물 <b>LT_C_SPBD</b></div>\n<div class="k">갱신정보</div>\n<div>현재 사용 중인 VWorld 레이어의 개별 갱신일은 API 응답에서 별도 제공되지 않음</div>\n<div class="k">건축물(후속)</div>\n<div>국토교통부 건축HUB 건축물대장정보 · 표제부 <b>getBrTitleInfo</b></div>\n<div class="k">갱신주기</div>\n<div>공공데이터포털 서비스 <b>수정일 2026-07-10</b> · 데이터 갱신주기는 상세페이지에 별도 미표기 · 각 대장행의 생성일자(crtnDay)는 별도 표시</div>\n<div class="k">도로/접도</div><div>현재 민간 초기검토의 주 판단에서 제외 · 실제 정비계획 입안이 필요한 경우 별도 검토</div><div class="k">확장방향</div><div>필지·건축물 공통원장에 역세권·용도지역·도시계획시설 정보를 추가하여 사업방식 비교에 활용</div><div class="k">판정기준</div>\n<div>서울특별시 도시 및 주거환경정비 조례 시행 <b>2026-05-18</b></div>\n<div class="k">표시원칙</div>\n<div>플랫폼 조회시각은 표시하지 않으며, 공공데이터 제공기관이 공개한 <b>갱신주기·기준일·최종갱신일</b>만 표시</div>\n</div>\n</div>\n</div>\n    </div>\n  </details>\n</section>\n\n  \n  <section class="panel strategy-panel">\n    <div class="panel-head"><h2>3. 재개발 초기 가능성</h2><span class="badge">노후도 중심 민간 스크리닝</span></div>\n    <div class="strategy-screening">\n      <div class="strategy-screening-head">\n        <div><b>노후도 기반 1차 판단</b><span>정비구역 지정요건 확정판정이 아니라 사업방식 비교를 위한 초기 신호</span></div>\n        <strong id="strategySignal">건축HUB 조회 필요</strong>\n      </div>\n      <div class="strategy-grid strategy-grid-6">\n        <div><span>건축물대장</span><b id="strategyTotalBuildings">-</b></div>\n        <div><span>노후건축물</span><b id="strategyOldBuildings">-</b></div>\n        <div><span>노후도</span><b id="strategyOldRatio">-</b></div>\n        <div><span>90㎡ 미만 필지</span><b id="strategySmallCount">-</b></div>\n        <div><span>과소필지율</span><b id="strategySmallRatio">-</b></div>\n        <div><span>초기 판단</span><b id="strategyAgeJudgement">-</b></div>\n      </div>\n      <div id="strategyReason" class="strategy-reason">건축HUB에서 건축물대장을 불러오면 자동판정합니다.</div>\n      <div class="strategy-caveat">본 결과는 공공데이터를 이용한 사업 초기검토용입니다. 재개발의 1차 판단은 노후도를 중심으로 보되, 90㎡ 미만 과소필지 수·비율은 함께 확인합니다. 접도율·호수밀도·무허가 여부 등은 실제 입안이 필요한 경우 별도 확인합니다.</div>\n    </div>\n  </section>\n\n<section class="panel scheme-panel">\n  <div class="panel-head">\n    <h2>4. 역세권·도심복합 사업방식 비교 검토시트</h2>\n    <span class="badge">8개 제도 · 사업가능성+밀도+공공기여</span>\n  </div>\n\n  <div class="scheme-body">\n    <div class="scheme-auto-strip">\n      <div><span>대상지 면적</span><b id="schemeAutoArea">-</b></div>\n      <div><span>건축HUB</span><b id="schemeAutoBuildings">-</b></div>\n      <div><span>20년 이상</span><b id="schemeAutoAge20">-</b></div>\n      <div><span>30년 이상</span><b id="schemeAutoAge30">-</b></div>\n      <div><span>90㎡ 미만</span><b id="schemeAutoSmall">-</b></div>\n    </div>\n\n    \n    <div class="station-gis-box">\n      <div class="station-gis-head">\n        <div>\n          <b>승강장 경계 GIS <span class="PASS">AUTO</span></b>\n          <span>행정안전부 주소기반산업지원서비스 · 기타자료 · TL_SPSB_STATN(지하철역사)</span>\n        </div>\n        <div class="station-gis-actions">\n          <label class="station-file-label">지하철역사 ZIP 연결\n            <input id="stationShpInput" type="file" accept=".zip" onchange="loadStationShpZip(this.files[0])">\n          </label>\n          <button class="btn station-mini-btn" onclick="clearStoredStationData()">저장자료 삭제</button>\n        </div>\n      </div>\n      <div class="station-gis-stats">\n        <div><span>데이터</span><b id="stationDataStatus">미연결</b></div>\n        <div><span>가장 가까운 역사</span><b id="stationNearestName">-</b></div>\n        <div><span>경계 최단거리</span><b id="stationNearestDistance">-</b></div>\n        <div><span>250m 포함률</span><b id="stationCoverage250">-</b></div>\n        <div><span>350m 포함률</span><b id="stationCoverage350">-</b></div>\n        <div><span>500m 포함률</span><b id="stationCoverage500">-</b></div>\n      </div>\n      <div id="stationGisNote" class="station-gis-note">\n        ZIP을 한 번 연결하면 이 브라우저에 저장합니다. 이후 구역을 다시 그려도 자동으로 승강장 경계를 분석합니다.\n      </div>\n    </div>\n\n<details class="scheme-input-details" open>\n      <summary>공통 입력값 — 향후 GIS 자동화 대상</summary>\n      <div class="scheme-input-inner">\n        <div class="scheme-input-grid">\n          <div class="scheme-field"><label>승강장 경계 최단거리(m)</label><input id="scheme_station_distance" type="number" min="0" placeholder="GIS 미연결 시 수기" oninput="runAllSchemeChecks()"><small id="stationDistanceInputNote">지하철역사 GIS 미연결 시 수기 입력</small></div>\n          <div class="scheme-field"><label>철도 노선수</label><select id="scheme_station_lines" onchange="runAllSchemeChecks()"><option value="">미입력</option><option value="1">1개 노선</option><option value="2">2개 이상(환승)</option></select></div>\n          <div class="scheme-field"><label>중심지 체계</label><select id="scheme_center" onchange="runAllSchemeChecks()"><option value="">미입력</option><option value="downtown">도심</option><option value="metro">광역중심</option><option value="regional">지역중심</option><option value="district">지구중심</option><option value="noncenter">비중심지</option></select></div>\n          <div class="scheme-field"><label>현재 용도지역</label><select id="scheme_zoning" onchange="runAllSchemeChecks()">\n            <option value="">미입력</option><option>제1종일반주거</option><option>제2종일반주거(7층)</option><option>제2종일반주거</option><option>제3종일반주거</option><option>준주거</option><option>준공업</option><option>근린상업</option><option>일반상업</option><option>중심상업</option><option>자연녹지</option><option>기타</option>\n          </select></div>\n          <div class="scheme-field"><label>검토 목표 용도지역</label><select id="scheme_target_zoning" onchange="runAllSchemeChecks()">\n            <option value="">자동추천/미선택</option><option>제2종일반주거</option><option>제3종일반주거</option><option>준주거</option><option>준공업</option><option>근린상업</option><option>일반상업</option><option>중심상업</option>\n          </select><small>종상향·용적률·공공기여 계산용</small></div>\n          <div class="scheme-field"><label>역세권 범위 포함비율(%)</label><input id="scheme_station_coverage" type="number" min="0" max="100" placeholder="GIS 미연결 시 수기" oninput="runAllSchemeChecks()"><small>GIS 연결 시 제도별 250·350·500m 포함률 자동 사용</small></div>\n          <div class="scheme-field"><label>가로구역 포함비율(%)</label><input id="scheme_block_share" type="number" min="0" max="100" placeholder="예: 70" oninput="runAllSchemeChecks()"></div>\n\n          <div class="scheme-field"><label>도로 접면 수</label><input id="scheme_road_faces" type="number" min="0" placeholder="예: 2" oninput="runAllSchemeChecks()"></div>\n          <div class="scheme-field"><label>최대 접면도로 폭(m)</label><input id="scheme_max_road_width" type="number" min="0" placeholder="예: 20" oninput="runAllSchemeChecks()"></div>\n          <div class="scheme-field"><label>8m 이상 도로 별도 접면</label><select id="scheme_has_8m_road" onchange="runAllSchemeChecks()"><option value="">미입력</option><option value="yes">예</option><option value="no">아니오</option></select></div>\n          <div class="scheme-field"><label>20m 이상 간선도로 접면</label><select id="scheme_has_20m_arterial" onchange="runAllSchemeChecks()"><option value="">미입력</option><option value="yes">예</option><option value="no">아니오</option></select></div>\n          <div class="scheme-field"><label>6m 이상 도로로 둘러싸인 1개 블록</label><select id="scheme_enclosed_by6m" onchange="runAllSchemeChecks()"><option value="">미입력</option><option value="yes">예</option><option value="no">아니오</option></select></div>\n          <div class="scheme-field"><label>20m 도로 접면 구간/둘레 비율(%)</label><input id="scheme_road20_perimeter_ratio" type="number" min="0" max="100" placeholder="예: 15" oninput="runAllSchemeChecks()"></div>\n\n          <div class="scheme-field"><label>간선도로 경계 거리(m)</label><input id="scheme_arterial_distance" type="number" min="0" placeholder="안심주택용" oninput="runAllSchemeChecks()"></div>\n          <div class="scheme-field"><label>의료시설 경계 거리(m)</label><input id="scheme_medical_distance" type="number" min="0" placeholder="안심주택용" oninput="runAllSchemeChecks()"></div>\n          <div class="scheme-field"><label>계획 세대수</label><input id="scheme_planned_units" type="number" min="0" placeholder="예: 350" oninput="runAllSchemeChecks()"></div>\n          <div class="scheme-field"><label>공장비율(%)</label><input id="scheme_factory_ratio" type="number" min="0" max="100" placeholder="준공업지역" oninput="runAllSchemeChecks()"></div>\n          <div class="scheme-field"><label>최대 공동주택단지 면적(㎡)</label><input id="scheme_apartment_max_area" type="number" min="0" placeholder="예: 8500" oninput="runAllSchemeChecks()"></div>\n          <div class="scheme-field"><label>최대 공동주택단지 비율(%)</label><input id="scheme_apartment_max_ratio" type="number" min="0" max="100" placeholder="예: 25" oninput="runAllSchemeChecks()"></div>\n\n          <div class="scheme-field"><label>토지등소유자 동의율(%)</label><input id="scheme_owner_consent" type="number" min="0" max="100" placeholder="예: 70" oninput="runAllSchemeChecks()"></div>\n          <div class="scheme-field"><label>토지면적 동의율(%)</label><input id="scheme_land_consent" type="number" min="0" max="100" placeholder="예: 55" oninput="runAllSchemeChecks()"></div>\n          <div class="scheme-field"><label>20m 도로접면 필지 소유자 동의율(%)</label><input id="scheme_road20_owner_consent" type="number" min="0" max="100" placeholder="장기전세 정비계획" oninput="runAllSchemeChecks()"></div>\n          <div class="scheme-field"><label>20m 도로접면 필지 면적 동의율(%)</label><input id="scheme_road20_land_consent" type="number" min="0" max="100" placeholder="장기전세 정비계획" oninput="runAllSchemeChecks()"></div>\n          <div class="scheme-field"><label>계획 주택 연면적 비율(%)</label><input id="scheme_planned_housing_floor_ratio" type="number" min="0" max="100" placeholder="도심복합혁신지구" oninput="runAllSchemeChecks()"></div>\n          <div class="scheme-field"><label>사전협상 대상지 요건 해당</label><select id="scheme_prior_negotiation" onchange="runAllSchemeChecks()"><option value="">미입력</option><option value="no">아니오</option><option value="yes">예</option></select></div>\n          <div class="scheme-field"><label>정책상 제외지역 해당</label><select id="scheme_site_excluded" onchange="runAllSchemeChecks()"><option value="">미입력</option><option value="no">아니오</option><option value="yes">예</option></select></div>\n          <div class="scheme-field"><label>저개발·개발저조 지역</label><select id="scheme_underdeveloped" onchange="runAllSchemeChecks()"><option value="">미입력</option><option value="yes">예</option><option value="no">아니오</option></select></div>\n        </div>\n\n        <div class="scheme-check-grid">\n          <label><input id="scheme_existing_nonresidential_only" type="checkbox" onchange="runAllSchemeChecks()"> 기존 건축물이 전부 비주거</label>\n          <label><input id="scheme_lowrise_bad_infra" type="checkbox" onchange="runAllSchemeChecks()"> 저층 노후주거지·기반시설 열악</label>\n          <label><input id="scheme_special_planning" type="checkbox" onchange="runAllSchemeChecks()"> 특별계획구역(장기전세 면적특례 검토)</label>\n        </div>\n\n        <div class="scheme-check-grid scheme-redev-extra">\n          <label><input id="scheme_infra_good" type="checkbox" onchange="runAllSchemeChecks()"> 역세권 기반시설 양호</label>\n          <label><input id="scheme_policy_facility" type="checkbox" onchange="runAllSchemeChecks()"> 시 정책 연계 전략시설 도입</label>\n          <label><input id="scheme_reconstruction_apartment" type="checkbox" onchange="runAllSchemeChecks()"> 공동주택 재건축 대상</label>\n          <label><input id="scheme_reconstruction_diagnosis" type="checkbox" onchange="runAllSchemeChecks()"> 재건축진단 추진요건 확인</label>\n        </div>\n\n        <div class="scheme-specific-grid">\n          <div class="scheme-field"><label>역세권활성화 사업방식</label><select id="activation_route" onchange="runAllSchemeChecks()"><option value="district">지구단위계획</option><option value="redevelopment">도시정비형 재개발</option></select></div>\n          <div class="scheme-field"><label>안심주택 입지유형</label><select id="safe_location_type" onchange="runAllSchemeChecks()"><option value="station">역세권</option><option value="arterial">간선도로변</option><option value="medical">의료시설 중심지역</option></select></div>\n          <div class="scheme-field"><label>장기전세 사업계획</label><select id="longterm_route" onchange="runAllSchemeChecks()"><option value="district">지구단위계획</option><option value="redevelopment">정비계획</option></select></div>\n          <div class="scheme-field"><label>장기전세 입지유형</label><select id="longterm_location_type" onchange="runAllSchemeChecks()"><option value="station">역세권</option><option value="arterial_intersection">간선도로 교차지</option></select></div>\n          <div class="scheme-field"><label>도심공공주택복합 유형</label><select id="public_complex_type" onchange="runAllSchemeChecks()"><option value="commercial">주거상업고밀지구</option><option value="industrial">주거산업융합지구</option><option value="housing">주택공급활성화지구</option></select></div>\n          <div class="scheme-field"><label>도심복합혁신지구 유형</label><select id="innovation_type" onchange="runAllSchemeChecks()"><option value="growth">성장거점형</option><option value="housing">주거중심형</option></select></div>\n        </div>\n      </div>\n    </details>\n\n    \n    <div class="scheme-compare-title">\n      <div>\n        <h3>8개 제도 동시 비교</h3>\n        <span>사업가능성 · 용도지역 상향 · 제도상 용적률 · 공공기여를 한 화면에서 비교</span>\n      </div>\n      <button class="btn" onclick="runAllSchemeChecks()">8개 제도 다시 계산</button>\n    </div>\n\n    <div class="scheme-compare-wrap">\n      <table class="scheme-compare-table">\n        <thead>\n          <tr>\n            <th>제도</th><th>사업가능성</th><th>충족/미달/확인</th>\n            <th>용도지역 상향</th><th>제도상 용적률</th><th>공공기여·기부채납</th>\n          </tr>\n        </thead>\n        <tbody id="schemeCompareBody"></tbody>\n      </table>\n    </div>\n\n    <div id="allSchemeSheets" class="all-scheme-sheets">\n      <div class="all-scheme-loading">8개 제도를 계산 중입니다.</div>\n    </div>\n\n    <div class="scheme-caveat">공공데이터와 입력값 기반 초기검토입니다. 8개 제도를 동일 대상지에서 동시에 비교하며 사업가능성, 용도지역·용적률 상향, 공공기여를 함께 표시합니다. 위원회 인정, 제외지역, 구체 기부채납량, 도시관리계획은 사업제안 전 재확인합니다.</div>\n  </div>\n</section>\n\n<section class="panel" style="display:none">\n    <div class="panel-head"><h2>기존 정량요건 상세</h2><span class="badge">비표시</span></div>\n    <div class="result" id="result"><div class="empty">왼쪽 지도에서 대상구역을 먼저 그리세요.</div></div>\n  </section>\n</div>\n<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" crossorigin=""></script>\n<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet.draw/1.0.4/leaflet.draw.js"></script>\n<script src="https://cdn.jsdelivr.net/npm/@turf/turf@6.5.0/turf.min.js"></script>\n<script src="https://unpkg.com/shpjs@latest/dist/shp.js"></script>\n<script>\nconst VWORLD_CLIENT_KEY="__VWORLD_CLIENT_KEY__";\nconst VWORLD_CLIENT_DOMAIN=window.location.origin;\nconst VWORLD_DATA_URL="https://api.vworld.kr/req/data";\nconst VWORLD_LAND_URL="https://api.vworld.kr/ned/data/getLandCharacteristics";\n\nfunction vworldJsonp(baseUrl, params, timeoutMs=20000){\n  return new Promise((resolve,reject)=>{\n    const cb=\'vwcb_\'+Date.now()+\'_\'+Math.random().toString(36).slice(2);\n    const script=document.createElement(\'script\');\n    let finished=false;\n    const cleanup=()=>{\n      if(finished)return;\n      finished=true;\n      clearTimeout(timer);\n      try{delete window[cb];}catch(e){window[cb]=undefined;}\n      if(script.parentNode)script.parentNode.removeChild(script);\n    };\n    const timer=setTimeout(()=>{\n      cleanup();\n      reject(new Error(\'VWorld JSONP timeout\'));\n    },timeoutMs);\n    window[cb]=(data)=>{cleanup();resolve(data);};\n    const usp=new URLSearchParams(params);\n    usp.set(\'callback\',cb);\n    script.src=baseUrl+\'?\'+usp.toString();\n    script.onerror=()=>{cleanup();reject(new Error(\'VWorld JSONP network error\'));};\n    document.head.appendChild(script);\n  });\n}\n\nasync function fetchParcelCandidatesBrowser(geometry){\n  if(!VWORLD_CLIENT_KEY) throw new Error(\'VWorld API 키가 페이지에 주입되지 않았습니다.\');\n  const target=turf.feature(geometry);\n  const [minx,miny,maxx,maxy]=turf.bbox(target);\n  const collected=[];\n  const seen=new Set();\n\n  for(let page=1;page<=10;page++){\n    const data=await vworldJsonp(VWORLD_DATA_URL,{\n      key:VWORLD_CLIENT_KEY,\n      domain:VWORLD_CLIENT_DOMAIN,\n      service:\'data\',\n      version:\'2.0\',\n      request:\'getfeature\',\n      format:\'json\',\n      size:\'1000\',\n      page:String(page),\n      geometry:\'true\',\n      attribute:\'true\',\n      crs:\'EPSG:4326\',\n      data:\'LP_PA_CBND_BUBUN\',\n      geomfilter:`BOX(${minx},${miny},${maxx},${maxy})`\n    });\n\n    const rsp=data&&data.response;\n    if(!rsp) throw new Error(\'VWorld 응답 형식 오류\');\n    if(rsp.status===\'NOT_FOUND\') break;\n    if(rsp.status!==\'OK\'){\n      const er=rsp.error||{};\n      throw new Error(er.text||er.message||rsp.status||\'VWorld Data API 오류\');\n    }\n\n    const features=((((rsp||{}).result||{}).featureCollection||{}).features)||[];\n    for(const f of features){\n      const props=f.properties||{};\n      const pnu=String(props.pnu||\'\');\n      if(!pnu||seen.has(pnu)||!f.geometry) continue;\n      try{\n        const parcel=turf.feature(f.geometry,props);\n        if(!turf.booleanIntersects(target,parcel)) continue;\n        let inter=null;\n        try{inter=turf.intersect(target,parcel);}catch(e){}\n        if(inter && turf.area(inter)<0.01) continue;\n      }catch(e){\n        continue;\n      }\n      seen.add(pnu);\n      collected.push(f);\n    }\n    if(features.length<1000) break;\n  }\n  return collected;\n}\n\n\nasync function fetchSpatialFeaturesBrowser(layerId, geometry, uniqueKeys=[]){\n  if(!VWORLD_CLIENT_KEY) throw new Error(\'VWorld API 키가 페이지에 주입되지 않았습니다.\');\n  const target=turf.feature(geometry);\n  const [minx,miny,maxx,maxy]=turf.bbox(target);\n  const collected=[];\n  const seen=new Set();\n\n  for(let page=1;page<=10;page++){\n    const data=await vworldJsonp(VWORLD_DATA_URL,{\n      key:VWORLD_CLIENT_KEY,\n      domain:VWORLD_CLIENT_DOMAIN,\n      service:\'data\',\n      version:\'2.0\',\n      request:\'getfeature\',\n      format:\'json\',\n      size:\'1000\',\n      page:String(page),\n      geometry:\'true\',\n      attribute:\'true\',\n      crs:\'EPSG:4326\',\n      data:layerId,\n      geomfilter:`BOX(${minx},${miny},${maxx},${maxy})`\n    });\n\n    const rsp=data&&data.response;\n    if(!rsp) throw new Error(\'VWorld 응답 형식 오류\');\n    if(rsp.status===\'NOT_FOUND\') break;\n    if(rsp.status!==\'OK\'){\n      const er=rsp.error||{};\n      throw new Error(er.text||er.message||rsp.status||\'VWorld Data API 오류\');\n    }\n\n    const features=((((rsp||{}).result||{}).featureCollection||{}).features)||[];\n    for(const f of features){\n      if(!f.geometry)continue;\n      const props=f.properties||{};\n      let key=\'\';\n      for(const k of uniqueKeys){\n        const v=props[k];\n        if(v!==undefined && v!==null && String(v)!==\'\'){key=String(v);break;}\n      }\n      if(!key)key=String(f.id||JSON.stringify(f.geometry).slice(0,120));\n      if(seen.has(key))continue;\n\n      try{\n        const obj=turf.feature(f.geometry,props);\n        if(!turf.booleanIntersects(target,obj))continue;\n        let inter=null;\n        try{inter=turf.intersect(target,obj);}catch(e){}\n        if(inter && turf.area(inter)<0.01)continue;\n      }catch(e){\n        continue;\n      }\n\n      seen.add(key);\n      collected.push(f);\n    }\n    if(features.length<1000)break;\n  }\n  return collected;\n}\n\nasync function fetchOfficialAreaBrowser(pnu){\n  const u=new URL(VWORLD_LAND_URL);\n  u.searchParams.set(\'pnu\',pnu);\n  u.searchParams.set(\'format\',\'xml\');\n  u.searchParams.set(\'key\',VWORLD_CLIENT_KEY);\n  u.searchParams.set(\'domain\',VWORLD_CLIENT_DOMAIN);\n  u.searchParams.set(\'numOfRows\',\'50\');\n\n  const r=await fetch(u.toString(),{method:\'GET\',mode:\'cors\'});\n  if(!r.ok) throw new Error(\'토지특성 HTTP \'+r.status);\n  const xml=await r.text();\n  const doc=new DOMParser().parseFromString(xml,\'application/xml\');\n  if(doc.querySelector(\'parsererror\')) throw new Error(\'토지특성 XML 파싱 오류\');\n\n  const vals=[];\n  for(const row of [...doc.querySelectorAll(\'field\')]){\n    const an=row.querySelector(\'lndpclAr\');\n    if(!an||!an.textContent.trim()) continue;\n    const area=Number(an.textContent.trim());\n    if(!Number.isFinite(area)||area<=0) continue;\n    const yn=row.querySelector(\'stdrYear\');\n    const year=yn?Number(yn.textContent.trim())||0:0;\n    vals.push({year,area});\n  }\n  if(vals.length){\n    vals.sort((a,b)=>b.year-a.year);\n    return vals[0].area;\n  }\n  const any=doc.querySelector(\'lndpclAr\');\n  if(any){\n    const area=Number(any.textContent.trim());\n    if(Number.isFinite(area)&&area>0)return area;\n  }\n  return null;\n}\n\nasync function mapLimit(items,limit,fn){\n  const results=new Array(items.length);\n  let cursor=0;\n  async function worker(){\n    while(true){\n      const i=cursor++;\n      if(i>=items.length)return;\n      try{results[i]=await fn(items[i],i);}\n      catch(e){results[i]=null;}\n    }\n  }\n  await Promise.all(Array.from({length:Math.min(limit,Math.max(1,items.length))},worker));\n  return results;\n}\n\nconst map=L.map(\'map\',{zoomControl:true}).setView([37.5665,126.9780],13);\nL.tileLayer(\'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png\',{maxZoom:20,attribution:\'© OpenStreetMap contributors\'}).addTo(map);\nconst drawnItems=new L.FeatureGroup().addTo(map);\nconst parcelLayer=L.geoJSON(null,{\n  style:f=>{\n    const p=f.properties||{};\n    const included=p._included!==false;\n    if(!included)return {weight:1,color:\'#98a2b3\',fillColor:\'#eaecf0\',fillOpacity:.12,dashArray:\'4,4\'};\n    if(p.is_small===true)return {weight:1.7,color:\'#b42318\',fillColor:\'#fecdca\',fillOpacity:.28};\n    return {weight:1.7,color:\'#175cd3\',fillColor:\'#d1e9ff\',fillOpacity:.22};\n  },\n  onEachFeature:(f,l)=>{\n    const p=f.properties||{};\n    const a=p.official_area_m2!=null?\'공식 \'+Number(p.official_area_m2).toLocaleString(\'ko-KR\',{maximumFractionDigits:2})+\'㎡\':(p.geometry_area_m2!=null?\'도형 \'+Number(p.geometry_area_m2).toLocaleString(\'ko-KR\',{maximumFractionDigits:2})+\'㎡\':\'공식면적 미확인\');\n    l.bindTooltip(`${p.jibun||p.pnu||\'필지\'} · ${a}`);\n    l.on(\'click\',()=>toggleParcelSelection(String((f.properties||{}).pnu||f.id||\'\')));\n  }\n}).addTo(map);\nconst buildingLayer=L.geoJSON(null,{\n  style:f=>{\n    const p=f.properties||{};\n    if(p._age_status===\'OLD\')return {weight:1.4,color:\'#b42318\',fillColor:\'#f04438\',fillOpacity:.48};\n    if(p._age_status===\'NOT_OLD\')return {weight:1.1,color:\'#067647\',fillColor:\'#75e0a7\',fillOpacity:.26};\n    if(p._parcel_old_any===true)return {weight:1.2,color:\'#b54708\',fillColor:\'#fec84b\',fillOpacity:.34};\n    return {weight:1.1,color:\'#7a5af8\',fillColor:\'#bdb4fe\',fillOpacity:.28};\n  },\n  onEachFeature:(f,l)=>{\n    const p=f.properties||{};\n    const nm=p.buld_nm||p.buld_nm_dc||p.pos_bul_nm||\'건물\';\n    const floors=p.gro_flo_co==null?\'?\':p.gro_flo_co;\n    const age=p._age_status===\'OLD\'?\'노후\':p._age_status===\'NOT_OLD\'?\'비노후\':p._parcel_old_any===true?\'노후건축물 소재필지(개별동 매칭불가)\':\'노후도 미확인\';\n    const pnu=p._parcel_pnu||p.pnu||\'PNU 미확인\';\n    l.bindTooltip(`${nm} · ${age} · 지상 ${floors}층 · ${pnu}`);\n  }\n}).addTo(map);\n\n\nconst stationBoundaryLayer=L.geoJSON(null,{\n  style:{weight:3,color:\'#175cd3\',fillColor:\'#84adff\',fillOpacity:.18},\n  onEachFeature:(f,l)=>l.bindTooltip(`지하철역사 · ${(f.properties||{}).KOR_SUB_NM||(f.properties||{}).kor_sub_nm||(f.properties||{}).name||\'-\'}`)\n}).addTo(map);\nconst stationBuffer500Layer=L.geoJSON(null,{style:{weight:1,color:\'#98a2b3\',fillColor:\'#d0d5dd\',fillOpacity:.05,dashArray:\'7,5\'}}).addTo(map);\nconst stationBuffer350Layer=L.geoJSON(null,{style:{weight:1.3,color:\'#b54708\',fillColor:\'#fedf89\',fillOpacity:.06,dashArray:\'5,4\'}}).addTo(map);\nconst stationBuffer250Layer=L.geoJSON(null,{style:{weight:1.5,color:\'#067647\',fillColor:\'#75e0a7\',fillOpacity:.08,dashArray:\'3,3\'}}).addTo(map);\n\nconst smallParcelLayer=L.geoJSON(null,{style:{weight:2.4,color:\'#b42318\',fillColor:\'#f97066\',fillOpacity:.42},onEachFeature:(f,l)=>{const p=f.properties||{};l.bindTooltip(`과소필지 · ${p.jibun||p.pnu||\'\'} · ${fmt(p._analysis_area_m2,2)}㎡ (${p._small_source||\'\'})`);}}).addTo(map);\nconst oldParcelLayer=L.geoJSON(null,{style:{weight:2.2,color:\'#b54708\',fillColor:\'#fec84b\',fillOpacity:.30},onEachFeature:(f,l)=>{const p=f.properties||{};l.bindTooltip(`노후건축물 소재필지 · ${p.jibun||p.pnu||\'\'} · 노후 ${p._old_building_count||0}/${p._hub_building_count||0}동`);}}).addTo(map);\nconst frontagePassLayer=L.geoJSON(null,{style:{weight:2.4,color:\'#067647\',fillColor:\'#75e0a7\',fillOpacity:.22},onEachFeature:(f,l)=>{const p=f.properties||{};l.bindTooltip(`접도충족 · 접면 ${fmt(p._frontage_max_m,1)}m`);}}).addTo(map);\nconst frontageFailLayer=L.geoJSON(null,{style:{weight:2.2,color:\'#b42318\',fillColor:\'#fecdca\',fillOpacity:.24,dashArray:\'4,3\'},onEachFeature:(f,l)=>{const p=f.properties||{};l.bindTooltip(`접도미충족 · 접면 ${fmt(p._frontage_max_m,1)}m`);}});\n\nconst roadLayer=L.geoJSON(null,{style:f=>{const p=f.properties||{};const q=p._qualifies6===true;return {weight:q?2:1,color:q?\'#7a5af8\':\'#98a2b3\',fillColor:q?\'#d9d6fe\':\'#eaecf0\',fillOpacity:q?.28:.10};},onEachFeature:(f,l)=>{const p=f.properties||{};const w=p._width_m==null?\'폭원 미확인\':Number(p._width_m).toFixed(1)+\'m\';l.bindTooltip(`실폭도로 · ${w} · ${p._width_source||\'\'}`);}}).addTo(map);\n\nlet activeGeometry=null;\nconst parcelFeatureMap=new Map();\nconst selectedParcelPnus=new Set();\nlet currentBuildingFeatures=[];\nconst hubRecordsByPnu=new Map();\nlet currentRoadWidthFeatures=[];\nlet currentRoadManageFeatures=[];\nlet uploadedRoadWidthFeatures=[];\nlet uploadedRoadManageFeatures=[];\n\nlet stationFeatures=[];\nlet stationSourceName=\'\';\nconst stationAnalysis={\n  loaded:false,\n  nearestName:\'\',\n  nearestDistance:null,\n  coverage250:null,\n  coverage350:null,\n  coverage500:null,\n  nearestFeatures:[],\n  sourceDate:\'\'\n};\nconst STATION_DB_NAME=\'urban_strategy_gis_v1\';\nconst STATION_DB_STORE=\'datasets\';\nconst STATION_DB_KEY=\'juso_TL_SPSB_STATN\';\n\n\n\n// Common analysis objects. Later station-area / zoning rules reuse the same objects.\nconst analysisState={\n  parcels:new Map(),\n  buildings:[],\n  roads:[],\n  metrics:{},\n  quality:{small:\'NONE\',old:\'NONE\',road:\'NONE\',density:\'NONE\'}\n};\n\nconst drawControl=new L.Control.Draw({position:\'topright\',draw:{polygon:{allowIntersection:false,showArea:false,shapeOptions:{weight:3}},rectangle:{shapeOptions:{weight:3}},polyline:false,circle:false,circlemarker:false,marker:false},edit:{featureGroup:drawnItems,remove:true}});\nmap.addControl(drawControl);\n\nmap.on(L.Draw.Event.CREATED, async e=>{drawnItems.clearLayers(); drawnItems.addLayer(e.layer); activeGeometry=e.layer.toGeoJSON().geometry; updateCompactMapRole(); await measureAndSync(); updateCompactInfoRail(); updateCandidateSchemes();});\nmap.on(L.Draw.Event.EDITED, async e=>{e.layers.eachLayer(layer=>activeGeometry=layer.toGeoJSON().geometry); updateCompactMapRole(); await measureAndSync(); updateCompactInfoRail(); updateCandidateSchemes();});\nmap.on(L.Draw.Event.DELETED, ()=>{activeGeometry=null; parcelLayer.clearLayers(); buildingLayer.clearLayers(); roadLayer.clearLayers(); smallParcelLayer.clearLayers(); oldParcelLayer.clearLayers(); frontagePassLayer.clearLayers(); frontageFailLayer.clearLayers(); stationBoundaryLayer.clearLayers();stationBuffer250Layer.clearLayers();stationBuffer350Layer.clearLayers();stationBuffer500Layer.clearLayers(); parcelFeatureMap.clear(); selectedParcelPnus.clear(); renderParcelSelectionList(); resetMeasure(); document.getElementById(\'result\').innerHTML=\'<div class="empty">왼쪽 지도에서 대상구역을 먼저 그리세요.</div>\'; updateCompactMapRole(); updateCompactInfoRail(); updateCandidateSchemes();});\n\nfunction num(id){const v=document.getElementById(id).value.trim(); return v===\'\'?null:Number(v)}\nfunction ratio(id){const v=num(id); return v===null?null:v/100}\nfunction fmt(n,d=0){return n==null?\'-\':Number(n).toLocaleString(\'ko-KR\',{maximumFractionDigits:d})}\n\n\n\n\nfunction validPositiveNumber(v){\n  if(v===null || v===undefined || v===\'\')return null;\n  const n=Number(v);\n  return Number.isFinite(n) && n>0 ? n : null;\n}\nfunction hubPlatAreaForPnu(pnu){\n  const rows=hubRecordsByPnu.get(String(pnu))||[];\n  const vals=rows.map(r=>validPositiveNumber(r.platArea)).filter(v=>v!=null);\n  if(!vals.length)return null;\n  // 건축물대장의 대지면적은 동일 대지에서 반복될 수 있으므로 합계가 아니라 대표 최대값 표시.\n  return Math.max(...vals);\n}\nfunction cloneFeature(f){\n  return {type:\'Feature\',id:f.id,geometry:f.geometry,properties:Object.assign({},f.properties||{})};\n}\nfunction updateAnalysisState(){\n  analysisState.parcels.clear();\n  for(const [pnu,f] of parcelFeatureMap.entries()){\n    if(!selectedParcelPnus.has(String(pnu)))continue;\n    const p=f.properties||{};\n    analysisState.parcels.set(String(pnu),{\n      pnu:String(pnu),jibun:p.jibun||\'\',area_m2:p._analysis_area_m2??p.official_area_m2??p.geometry_area_m2??null,\n      area_source:p._small_source||\'\',small:p.is_small===true,\n      hub_buildings:p._hub_building_count||0,old_buildings:p._old_building_count||0,\n      old_any:p._old_building_count>0,frontage_pass:p._frontage_pass===true,\n      frontage_max_m:p._frontage_max_m??null,frontage_total_m:p._frontage_total_m??null\n    });\n  }\n  analysisState.buildings=currentBuildingFeatures.map(f=>({\n    key:String((f.properties||{})._building_key||f.id||\'\'),\n    pnu:String((f.properties||{})._parcel_pnu||(f.properties||{}).pnu||\'\'),\n    age_status:(f.properties||{})._age_status||\'UNKNOWN\'\n  }));\n  analysisState.roads=currentRoadWidthFeatures.map(f=>({\n    width_m:(f.properties||{})._width_m??null,qualifies6:(f.properties||{})._qualifies6===true,\n    source:(f.properties||{})._width_source||\'\'\n  }));\n}\nfunction refreshAnalysisLayers(){\n  smallParcelLayer.clearLayers();\n  oldParcelLayer.clearLayers();\n  frontagePassLayer.clearLayers();\n  frontageFailLayer.clearLayers();\n\n  const small=[],old=[],pass=[],fail=[];\n  for(const pnu of selectedParcelPnus){\n    const f=parcelFeatureMap.get(String(pnu)); if(!f)continue;\n    const p=f.properties||{};\n    if(p.is_small===true)small.push(cloneFeature(f));\n    if((p._old_building_count||0)>0)old.push(cloneFeature(f));\n    if(p._frontage_pass===true)pass.push(cloneFeature(f));\n    if(p._frontage_pass===false)fail.push(cloneFeature(f));\n  }\n\n  if(document.getElementById(\'lySmall\')?.checked)smallParcelLayer.addData({type:\'FeatureCollection\',features:small});\n  if(document.getElementById(\'lyOld\')?.checked)oldParcelLayer.addData({type:\'FeatureCollection\',features:old});\n  if(document.getElementById(\'lyFrontagePass\')?.checked)frontagePassLayer.addData({type:\'FeatureCollection\',features:pass});\n  if(document.getElementById(\'lyFrontageFail\')?.checked)frontageFailLayer.addData({type:\'FeatureCollection\',features:fail});\n\n  if(document.getElementById(\'lyAllParcels\')?.checked){\n    if(!map.hasLayer(parcelLayer))parcelLayer.addTo(map);\n  }else if(map.hasLayer(parcelLayer))map.removeLayer(parcelLayer);\n\n  if(document.getElementById(\'lyRoad6\')?.checked){\n    if(!map.hasLayer(roadLayer))roadLayer.addTo(map);\n  }else if(map.hasLayer(roadLayer))map.removeLayer(roadLayer);\n\n  const s=document.getElementById(\'analysisLayerSummary\');\n  if(s)s.textContent=`선택필지 ${selectedParcelPnus.size} · 90㎡ 미만 ${small.length}필지 · 노후건축물 소재필지 ${old.length} · 필지·건축물 원장은 향후 사업방식 비교에 재사용`;\n  updateAnalysisState();\n  updateRedevelopmentStrategySignal();\n}\nfunction assignBuildingsToParcels(){\n  for(const b of currentBuildingFeatures){\n    const bp=b.properties||{};\n    let pnu=String(bp.pnu||\'\');\n    if(pnu && parcelFeatureMap.has(pnu)){bp._parcel_pnu=pnu;continue;}\n    try{\n      const pt=turf.centroid(b);\n      for(const spnu of selectedParcelPnus){\n        const pf=parcelFeatureMap.get(String(spnu)); if(!pf)continue;\n        if(turf.booleanPointInPolygon(pt,pf)){bp._parcel_pnu=String(spnu);break;}\n      }\n    }catch(e){}\n  }\n}\nfunction refreshBuildingAgeClassification(){\n  assignBuildingsToParcels();\n  const geomByPnu=new Map();\n  for(const b of currentBuildingFeatures){\n    const pnu=String((b.properties||{})._parcel_pnu||\'\');\n    if(!pnu)continue;\n    if(!geomByPnu.has(pnu))geomByPnu.set(pnu,[]);\n    geomByPnu.get(pnu).push(b);\n  }\n\n  for(const pnu of selectedParcelPnus){\n    const pf=parcelFeatureMap.get(String(pnu)); if(!pf)continue;\n    const rows=hubRecordsByPnu.get(String(pnu))||[];\n    const oldRows=rows.filter(r=>r.age_status===\'OLD\');\n    const knownRows=rows.filter(r=>r.age_status===\'OLD\'||r.age_status===\'NOT_OLD\');\n    const pp=pf.properties||{};\n    pp._hub_building_count=rows.length;\n    pp._old_building_count=oldRows.length;\n    pp._known_age_count=knownRows.length;\n    pf.properties=pp;\n\n    const geoms=geomByPnu.get(String(pnu))||[];\n    // Exact 1:1 is uncommon because LT_C_SPBD and building register IDs differ.\n    // For one title/one geometry we can classify exactly; otherwise mark parcel-level condition.\n    if(rows.length===1 && geoms.length===1){\n      geoms[0].properties._age_status=rows[0].age_status||\'UNKNOWN\';\n      geoms[0].properties._parcel_old_any=oldRows.length>0;\n    }else{\n      for(const g of geoms){\n        g.properties._age_status=\'UNKNOWN\';\n        g.properties._parcel_old_any=oldRows.length>0;\n      }\n    }\n  }\n  buildingLayer.clearLayers();\n  buildingLayer.addData({type:\'FeatureCollection\',features:currentBuildingFeatures});\n  refreshAnalysisLayers();\n}\n\nfunction parcelKey(f){\n  const p=f.properties||{};\n  return String(p.pnu||f.id||\'\');\n}\nfunction syncParcelLayerFromState(){\n  parcelLayer.clearLayers();\n  const arr=[];\n  for(const [key,f] of parcelFeatureMap.entries()){\n    const copy={type:\'Feature\',id:f.id,geometry:f.geometry,properties:Object.assign({},f.properties||{})};\n    copy.properties._included=selectedParcelPnus.has(key);\n    arr.push(copy);\n  }\n  parcelLayer.addData({type:\'FeatureCollection\',features:arr});\n  renderParcelSelectionList();\n  recalcSelectedParcelStats();\n  refreshAnalysisLayers();\n}\nfunction renderParcelSelectionList(){\n  const box=document.getElementById(\'parcelSelectionList\');\n  const summary=document.getElementById(\'parcelSelectionSummary\');\n  if(!box||!summary)return;\n  const arr=[...parcelFeatureMap.entries()];\n  const included=arr.filter(([k])=>selectedParcelPnus.has(k));\n  summary.textContent=`불러온 필지 ${arr.length}개 · 포함 ${included.length}개 · 제외 ${arr.length-included.length}개. 지도 필지를 클릭하거나 목록 체크로 포함/제외할 수 있습니다.`;\n  if(!arr.length){box.innerHTML=\'\';return;}\n  const rows=arr.slice(0,300).map(([k,f])=>{\n    const p=f.properties||{};\n    const on=selectedParcelPnus.has(k);\n    const label=p.jibun||p.jibun_nm||p.pnu||k;\n    return `<div class="parcel-row ${on?\'\':\'excluded\'}">\n      <input type="checkbox" ${on?\'checked\':\'\'} onchange="setParcelIncluded(\'${String(k).replaceAll("\'","")} \',this.checked)" style="width:18px;height:18px">\n      <div><b>${String(label)}</b><div class="mono">${String(p.pnu||k)}</div></div>\n      <div>${p.official_area_m2!=null?fmt(p.official_area_m2,2)+\'㎡\':(p.geometry_area_m2!=null?\'도형 \'+fmt(p.geometry_area_m2,2)+\'㎡\':\'면적 미확인\')}</div>\n    </div>`;\n  }).join(\'\');\n  box.innerHTML=rows+(arr.length>300?`<div class="parcel-row"><div></div><div>외 ${arr.length-300}개</div><div></div></div>`:\'\');\n}\nfunction setParcelIncluded(key,checked){\n  key=String(key||\'\').trim();\n  if(!key)return;\n  if(checked)selectedParcelPnus.add(key); else selectedParcelPnus.delete(key);\n  syncParcelLayerFromState();\n}\nfunction toggleParcelSelection(key){\n  key=String(key||\'\').trim();\n  if(!key)return;\n  if(selectedParcelPnus.has(key))selectedParcelPnus.delete(key); else selectedParcelPnus.add(key);\n  syncParcelLayerFromState();\n}\nfunction selectAllCurrentParcels(){\n  for(const k of parcelFeatureMap.keys())selectedParcelPnus.add(k);\n  syncParcelLayerFromState();\n}\nfunction clearParcelSelection(){\n  selectedParcelPnus.clear();\n  syncParcelLayerFromState();\n}\nfunction recalcSelectedParcelStats(){\n  const hubSummary=document.getElementById(\'hubSummary\');\n  if(hubSummary && !hubSummary.textContent.includes(\'대장조회\')) hubSummary.textContent=\'필지선택이 변경되었습니다. 건축HUB 대장조회를 다시 실행하세요.\';\n  const sel=[...selectedParcelPnus].map(k=>parcelFeatureMap.get(k)).filter(Boolean);\n  document.getElementById(\'total_parcel_count\').value=sel.length;\n  let small=0,known=0;\n  for(const f of sel){\n    const a=(f.properties||{})._analysis_area_m2 ?? (f.properties||{}).official_area_m2 ?? (f.properties||{}).geometry_area_m2;\n    if(a!==null && a!==undefined && Number.isFinite(Number(a))){\n      known++;\n      if(Number(a)<90)small++;\n    }\n  }\n  document.getElementById(\'small_parcel_count\').value=(sel.length>0 && known>0)?small:\'\';\n  runAllSchemeChecks();\n}\nasync function fetchParcelByPnuBrowser(pnu){\n  const data=await vworldJsonp(VWORLD_DATA_URL,{\n    key:VWORLD_CLIENT_KEY,\n    domain:VWORLD_CLIENT_DOMAIN,\n    service:\'data\',\n    version:\'2.0\',\n    request:\'getfeature\',\n    format:\'json\',\n    size:\'10\',\n    page:\'1\',\n    geometry:\'true\',\n    attribute:\'true\',\n    crs:\'EPSG:4326\',\n    data:\'LP_PA_CBND_BUBUN\',\n    attrfilter:`pnu:=:${pnu}`\n  });\n  const rsp=data&&data.response;\n  if(!rsp||rsp.status===\'NOT_FOUND\')return null;\n  if(rsp.status!==\'OK\'){\n    const er=rsp.error||{};\n    throw new Error(er.text||er.message||rsp.status||\'PNU 필지조회 오류\');\n  }\n  const fs=((((rsp||{}).result||{}).featureCollection||{}).features)||[];\n  return fs[0]||null;\n}\nasync function addParcelsFromList(){\n  const txt=document.getElementById(\'parcelListInput\').value||\'\';\n  const pnus=[...new Set(txt.split(/[\\s,;]+/).map(s=>s.trim()).filter(s=>/^\\d{19}$/.test(s)))];\n  if(!pnus.length){alert(\'현재 1차 버전은 PNU 19자리 목록을 지원합니다.\');return;}\n  const status=document.getElementById(\'parcelSelectionSummary\');\n  status.textContent=`PNU ${pnus.length}개 조회 중...`;\n  let added=0,failed=0;\n  for(const pnu of pnus){\n    try{\n      const f=await fetchParcelByPnuBrowser(pnu);\n      if(!f){failed++;continue;}\n      const key=parcelKey(f);\n      parcelFeatureMap.set(key,f);\n      selectedParcelPnus.add(key);\n      added++;\n    }catch(e){failed++;}\n  }\n  syncParcelLayerFromState();\n  status.textContent=`PNU 목록 추가 완료: ${added}개 추가 · ${failed}개 미확인`;\n}\nasync function loadNearbyParcels(){\n  if(!activeGeometry)return;\n  const status=document.getElementById(\'parcelSelectionSummary\');\n  status.textContent=\'주변필지(약 80m) 불러오는 중...\';\n  try{\n    const buffered=turf.buffer(turf.feature(activeGeometry),80,{units:\'meters\'});\n    const fs=await fetchParcelCandidatesBrowser(buffered.geometry);\n    let add=0;\n    for(const f of fs){\n      const key=parcelKey(f);\n      if(!parcelFeatureMap.has(key)){\n        parcelFeatureMap.set(key,f);\n        add++;\n      }\n    }\n    syncParcelLayerFromState();\n    status.textContent=`주변필지 ${add}개 추가. 회색 필지를 클릭하면 구역에 포함할 수 있습니다.`;\n  }catch(e){\n    status.textContent=\'주변필지 조회 실패: \'+String(e.message||e);\n  }\n}\nasync function updateMeasurementOnly(){\n  if(!activeGeometry)return;\n  const r=await fetch(\'/api/spatial/measure\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({geometry:activeGeometry})});\n  const d=await r.json();\n  if(!r.ok)throw new Error(d.detail||\'면적계산 실패\');\n  document.getElementById(\'area_m2\').value=d.area_m2;\n  document.getElementById(\'mArea\').textContent=fmt(d.area_m2)+\' ㎡\';\n  document.getElementById(\'mHa\').textContent=fmt(d.area_ha,3)+\' ha\';\n  document.getElementById(\'mPerimeter\').textContent=fmt(d.perimeter_m)+\' m\';\n}\nasync function applySelectedParcelsAsBoundary(){\n  const fs=[...selectedParcelPnus].map(k=>parcelFeatureMap.get(k)).filter(Boolean);\n  if(!fs.length){alert(\'포함할 필지를 하나 이상 선택하세요.\');return;}\n  let merged=turf.feature(fs[0].geometry,fs[0].properties||{});\n  for(let i=1;i<fs.length;i++){\n    const next=turf.feature(fs[i].geometry,fs[i].properties||{});\n    try{\n      const u=turf.union(merged,next);\n      if(u)merged=u;\n    }catch(e){\n      // 서로 떨어진 필지는 MultiPolygon으로 결합\n      const coords=[];\n      const pushGeom=g=>{\n        if(g.type===\'Polygon\')coords.push(g.coordinates);\n        else if(g.type===\'MultiPolygon\')coords.push(...g.coordinates);\n      };\n      pushGeom(merged.geometry);pushGeom(next.geometry);\n      merged=turf.multiPolygon(coords);\n    }\n  }\n  activeGeometry=merged.geometry;\n  drawnItems.clearLayers();\n  L.geoJSON(merged,{style:{weight:3}}).eachLayer(l=>drawnItems.addLayer(l));\n  await updateMeasurementOnly();\n  recalcSelectedParcelStats();\n  await Promise.allSettled([analyzeLandLedger(),analyzeBuildings()]);\n  await analyzeBuildingHub();\n  await analyzeRoadAccess();\n  document.getElementById(\'result\').innerHTML=\'<div class="empty">선택필지로 구역계를 갱신했습니다. 자동·수기 지표를 확인한 뒤 재개발 검토를 실행하세요.</div>\';\n}\n\n\nfunction pickPropCI(obj,names){\n  if(!obj)return undefined;\n  const map={}; for(const [k,v] of Object.entries(obj))map[String(k).toLowerCase()]=v;\n  for(const n of names){const v=map[String(n).toLowerCase()];if(v!==undefined&&v!==null&&String(v)!==\'\')return v;}\n  return undefined;\n}\nfunction cleanNum(v){const n=Number(v);return Number.isFinite(n)?n:null;}\nfunction parseJibunLandCategory(jibun){\n  const s=String(jibun||\'\').trim(); const m=s.match(/\\s([^\\d\\s]+)$/); return m?m[1]:\'\';\n}\nasync function trySpatialLayerCandidates(ids,geometry,keys=[]){\n  const errs=[];\n  for(const id of ids){\n    try{const fs=await fetchSpatialFeaturesBrowser(id,geometry,keys); if(fs.length)return {id,features:fs}; errs.push(id+\':0건\');}\n    catch(e){errs.push(id+\':\'+String(e.message||e));}\n  }\n  return {id:null,features:[],errors:errs};\n}\nfunction estimateRoadPolygonWidthMeters(f){\n  try{\n    const A=turf.area(f); if(!(A>0))return null;\n    const line=turf.polygonToLine(f); let P=0;\n    const lines=line.type===\'FeatureCollection\'?line.features:[line];\n    for(const ln of lines)P+=turf.length(ln,{units:\'kilometers\'})*1000;\n    const s=P/2; const disc=s*s-4*A;\n    if(disc>0){const w=(s-Math.sqrt(disc))/2;if(w>0.5&&w<100)return w;}\n    const bb=turf.bbox(f); const a=turf.distance([bb[0],bb[1]],[bb[2],bb[1]],{units:\'kilometers\'})*1000; const b=turf.distance([bb[0],bb[1]],[bb[0],bb[3]],{units:\'kilometers\'})*1000; const w=Math.min(a,b); return (w>0.5&&w<100)?w:null;\n  }catch(e){return null;}\n}\nfunction roadWidthFromManage(rw,manage){\n  let best=null;\n  for(const mf of manage){\n    try{\n      if(!turf.booleanIntersects(rw,mf))continue;\n      let w=cleanNum(pickPropCI(mf.properties,[\'ROAD_BT\',\'road_bt\',\'roadBt\']));\n      if(w!=null && w>=1 && w<=100){ if(best==null||w>best)best=w; }\n    }catch(e){}\n  }\n  return best;\n}\nfunction annotateRoadWidths(rwFeatures,manageFeatures){\n  return rwFeatures.map(f=>{\n    const p=Object.assign({},f.properties||{}); let w=cleanNum(pickPropCI(p,[\'ROAD_BT\',\'road_bt\'])); let src=\'\';\n    if(!(w>=1&&w<=100)){w=roadWidthFromManage(f,manageFeatures); if(w!=null)src=\'TL_SPRD_MANAGE ROAD_BT\';} else src=\'실폭도로 ROAD_BT\';\n    if(w==null){w=estimateRoadPolygonWidthMeters(f); if(w!=null)src=\'실폭도로 도형추정(예비)\';}\n    p._width_m=w; p._width_source=src||\'미확인\'; p._qualifies6=(w!=null&&w>=6); return {type:\'Feature\',id:f.id,geometry:f.geometry,properties:p};\n  });\n}\nfunction boundaryLines(poly){\n  try{const x=turf.polygonToLine(poly);return x.type===\'FeatureCollection\'?x.features:[x];}catch(e){return [];}\n}\nfunction frontageStats(parcel,qualifiedRoads){\n  const buffers=qualifiedRoads.map(f=>{try{return turf.buffer(f,0.45,{units:\'meters\'});}catch(e){return f;}});\n  let total=0,best=0; const step=0.5;\n  for(const line of boundaryLines(parcel)){\n    const len=turf.length(line,{units:\'kilometers\'})*1000; let run=0;\n    for(let d=0;d<len;d+=step){\n      const seg=Math.min(step,len-d); const pt=turf.along(line,(d+seg/2)/1000,{units:\'kilometers\'}); let hit=false;\n      for(const r of buffers){try{if(turf.booleanPointInPolygon(pt,r)){hit=true;break;}}catch(e){}}\n      if(hit){total+=seg;run+=seg;best=Math.max(best,run);}else run=0;\n    }\n  }\n  return {total_m:total,max_contiguous_m:best};\n}\nfunction spatialBuildingCountsByParcel(){\n  const m=new Map(); for(const pnu of selectedParcelPnus)m.set(String(pnu),0);\n  for(const b of currentBuildingFeatures){\n    let pt=null;try{pt=turf.centroid(b);}catch(e){continue;}\n    for(const pnu of selectedParcelPnus){const pf=parcelFeatureMap.get(String(pnu));if(!pf)continue;try{if(turf.booleanPointInPolygon(pt,pf)){m.set(String(pnu),(m.get(String(pnu))||0)+1);break;}}catch(e){}}\n  } return m;\n}\nfunction buildingCountsByParcel(){\n  let hubTotal=0; const out=new Map();\n  for(const pnu of selectedParcelPnus){const rows=hubRecordsByPnu.get(String(pnu))||[];const uniq=new Set(rows.map(r=>String(r.mgmBldrgstPk||`${r.dongNm||\'\'}|${r.bldNm||\'\'}|${r.useAprDay||\'\'}`)));out.set(String(pnu),uniq.size);hubTotal+=uniq.size;}\n  if(hubTotal>0)return {source:\'건축HUB 표제부\',counts:out};\n  return {source:\'VWorld LT_C_SPBD 공간건물(1차)\',counts:spatialBuildingCountsByParcel()};\n}\n\nconst VWORLD_RW_FILE_BY_SIDO={\n  \'11\':{name:\'서울\',fileNo:63}, \'26\':{name:\'부산\',fileNo:65}, \'27\':{name:\'대구\',fileNo:47},\n  \'28\':{name:\'인천\',fileNo:59}, \'29\':{name:\'광주\',fileNo:41}, \'30\':{name:\'대전\',fileNo:45},\n  \'31\':{name:\'울산\',fileNo:71}, \'36\':{name:\'세종\',fileNo:69}, \'41\':{name:\'경기\',fileNo:53},\n  \'43\':{name:\'충북\',fileNo:49}, \'44\':{name:\'충남\',fileNo:61}, \'46\':{name:\'전남\',fileNo:55},\n  \'47\':{name:\'경북\',fileNo:57}, \'48\':{name:\'경남\',fileNo:39}, \'50\':{name:\'제주\',fileNo:43},\n  \'51\':{name:\'강원특별자치도\',fileNo:51}, \'52\':{name:\'전북특별자치도\',fileNo:89}\n};\nfunction updateRoadDownloadLink(){\n  const el=document.getElementById(\'roadVworldDownload\');if(!el)return;\n  const first=[...selectedParcelPnus].find(p=>/^\\d{19}$/.test(String(p)));\n  const code=first?String(first).slice(0,2):\'\';\n  const info=VWORLD_RW_FILE_BY_SIDO[code];\n  if(info){\n    el.href=`https://www.vworld.kr/dtmk/downloadResourceFile.do?ds_id=30057&fileNo=${info.fileNo}`;\n    el.textContent=`VWorld ${info.name} 실폭도로 SHP 받기`;\n  }else{\n    el.href=\'https://www.vworld.kr/dtmk/dtmk_ntads_s002.do?svcCde=MK&dsId=30057\';\n    el.textContent=\'VWorld 실폭도로 SHP\';\n  }\n}\n\nasync function loadRoadShpZip(file){\n  if(!file)return; const s=document.getElementById(\'roadSummary\');s.textContent=\'실폭도로 ZIP 읽는 중...\';\n  try{\n    const parsed=await shp(await file.arrayBuffer()); const layers=Array.isArray(parsed)?parsed:[parsed]; let rw=[],mg=[];\n    for(const g of layers){const nm=String(g.fileName||\'\').toUpperCase();const fs=(g.features||[]);const geom=fs[0]?.geometry?.type||\'\';if(nm.includes(\'SPRD_RW\')||geom.includes(\'Polygon\'))rw.push(...fs);if(nm.includes(\'SPRD_MANAGE\')||geom.includes(\'Line\'))mg.push(...fs);}\n    uploadedRoadWidthFeatures=rw;uploadedRoadManageFeatures=mg;s.textContent=`ZIP 로드: 실폭도로 ${rw.length}건 · 도로구간 ${mg.length}건`;await analyzeRoadAccess();\n  }catch(e){s.textContent=\'ZIP 읽기 실패: \'+String(e.message||e);}\n}\nasync function fetchRoadNetwork(){\n  if(!activeGeometry)return {rw:[],manage:[],source:\'none\'};\n  const zone=turf.buffer(turf.feature(activeGeometry),60,{units:\'meters\'}).geometry;\n  const rwRes=await trySpatialLayerCandidates([\'TL_SPRD_RW\',\'LT_C_SPRD_RW\'],zone,[\'rw_sn\',\'RW_SN\']);\n  const mgRes=await trySpatialLayerCandidates([\'TL_SPRD_MANAGE\',\'LT_C_SPRD_MANAGE\'],zone,[\'rds_man_no\',\'RDS_MAN_NO\']);\n  let rw=rwRes.features,mg=mgRes.features,source=\'VWorld 2D Data API\';\n  if(!rw.length && uploadedRoadWidthFeatures.length){rw=uploadedRoadWidthFeatures;mg=uploadedRoadManageFeatures;source=\'업로드 VWorld 도로명주소 전자지도 SHP\';}\n  return {rw,manage:mg,source,errors:[...(rwRes.errors||[]),...(mgRes.errors||[])]};\n}\nasync function analyzeRoadAccess(){\n  if(!activeGeometry||!selectedParcelPnus.size)return;\n  const summary=document.getElementById(\'roadSummary\'),conn=document.getElementById(\'roadConn\'),tbody=document.getElementById(\'roadTableBody\'),wrap=document.getElementById(\'roadTableWrap\');\n  conn.textContent=\'접도율 계산 중\';conn.className=\'conn planned\';summary.textContent=\'실폭도로·도로구간 조회 및 공간연산 중...\';tbody.innerHTML=\'\';wrap.style.display=\'none\';\n  try{\n    const net=await fetchRoadNetwork(); if(!net.rw.length){document.getElementById(\'road_basis_building_count\').value=\'\';document.getElementById(\'road_access_building_count_6m\').value=\'\';summary.textContent=\'VWorld 2D Data API에는 실폭도로가 열리지 않았습니다. 위의 해당 시도 VWorld SHP를 받아 ‘다운받은 ZIP 분석’에 넣으면 접도율을 계산합니다.\';conn.textContent=\'접도율 SHP 필요\';conn.className=\'conn planned\';analysisState.quality.road=\'NO_DATA\';refreshAnalysisLayers();return;}\n    currentRoadManageFeatures=net.manage; currentRoadWidthFeatures=annotateRoadWidths(net.rw,net.manage); roadLayer.clearLayers();roadLayer.addData({type:\'FeatureCollection\',features:currentRoadWidthFeatures});\n    const qualified=currentRoadWidthFeatures.filter(f=>(f.properties||{})._qualifies6===true); const counts=buildingCountsByParcel();let basis=0,access=0;const rows=[];let estimateUsed=false;\n    for(const pnu of selectedParcelPnus){\n      const pf=parcelFeatureMap.get(String(pnu));if(!pf)continue;\n      const bc=counts.counts.get(String(pnu))||0;basis+=bc;\n      const st=frontageStats(pf,qualified);\n      const ok=st.max_contiguous_m>=4;\n      if(ok)access+=bc;\n      let srcs=new Set();\n      for(const r of qualified){\n        try{if(turf.booleanIntersects(pf,turf.buffer(r,0.5,{units:\'meters\'})))srcs.add((r.properties||{})._width_source||\'\');}catch(e){}\n      }\n      const src=[...srcs].filter(Boolean).join(\', \')||\'-\';\n      if(src.includes(\'추정\'))estimateUsed=true;\n      const pp=pf.properties||{};\n      pp._frontage_pass=ok;\n      pp._frontage_total_m=st.total_m;\n      pp._frontage_max_m=st.max_contiguous_m;\n      pp._frontage_building_count=bc;\n      pp._frontage_source=src;\n      pf.properties=pp;\n      rows.push({pnu,jibun:pp.jibun||\'\',bc,total:st.total_m,max:st.max_contiguous_m,ok,src});\n    }\n    document.getElementById(\'road_basis_building_count\').value=basis;document.getElementById(\'road_access_building_count_6m\').value=access;const ratio=basis?access/basis*100:0;\n    tbody.innerHTML=rows.map(r=>`<tr><td>${escHtml(r.jibun)}</td><td>${escHtml(r.pnu)}</td><td>${r.bc}</td><td>${r.total.toFixed(1)}m</td><td>${r.max.toFixed(1)}m</td><td>${r.ok?\'<b class="PASS">접도</b>\':\'-\'}</td><td>${escHtml(r.src)}</td></tr>`).join(\'\');wrap.style.display=\'block\';\n    const dates=currentRoadWidthFeatures.map(f=>String(pickPropCI(f.properties,[\'OPERT_DE\',\'opert_de\'])||\'\')).filter(Boolean).sort();const dr=dates.length?`${hubFmtDay(dates[0].slice(0,8))} ~ ${hubFmtDay(dates[dates.length-1].slice(0,8))}`:\'미제공\';summary.innerHTML=`${net.source} · 실폭도로 ${currentRoadWidthFeatures.length}건(6m+ ${qualified.length}건) · ${counts.source} 기준 ${basis}동 중 접도 ${access}동 = <b>${ratio.toFixed(1)}%</b> · OPERT_DE ${dr}${estimateUsed?\' · <b>폭원 도형추정 포함(예비)</b>\':\'\'}`;conn.textContent=estimateUsed?\'접도율 AUTO(예비)\':\'접도율 AUTO\';conn.className=estimateUsed?\'conn planned\':\'conn auto\';analysisState.quality.road=estimateUsed?\'ESTIMATE\':\'AUTO\';refreshAnalysisLayers();\n  }catch(e){summary.textContent=\'접도율 계산 실패: \'+String(e.message||e);conn.textContent=\'접도율 REVIEW\';conn.className=\'conn planned\';}\n}\nasync function runAllAutoAnalyses(){\n  await analyzeParcels();\n  await Promise.allSettled([analyzeLandLedger(),analyzeBuildings()]);\n  await analyzeBuildingHub();\n  runAllSchemeChecks();\n}\nasync function measureAndSync(){\n  if(!activeGeometry)return;\n  const r=await fetch(\'/api/spatial/measure\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({geometry:activeGeometry})});\n  const d=await r.json();\n  if(!r.ok){alert(d.detail||\'면적 계산 실패\');return;}\n  document.getElementById(\'area_m2\').value=d.area_m2.toFixed(2);\n  document.getElementById(\'mArea\').textContent=fmt(d.area_m2,0)+\' ㎡\';\n  document.getElementById(\'mHa\').textContent=fmt(d.area_ha,3)+\' ha\';\n  document.getElementById(\'mPerimeter\').textContent=fmt(d.perimeter_m,0)+\' m\';\n  document.getElementById(\'runBtn\').disabled=false;\n  if(stationAnalysis.loaded) await analyzeStationBoundary();\n  runAllSchemeChecks();\n  document.getElementById(\'result\').innerHTML=\'<div class="empty">구역면적을 계산했습니다. 필지·건축물 원장을 자동 조회 중입니다.</div>\';\n  await runAllAutoAnalyses();\n}\nfunction resetMeasure(){\n  document.getElementById(\'area_m2\').value=\'\';\n  document.getElementById(\'mArea\').textContent=\'-\';\n  document.getElementById(\'mHa\').textContent=\'-\';\n  document.getElementById(\'mPerimeter\').textContent=\'-\';\n  document.getElementById(\'total_parcel_count\').value=\'\';\n  document.getElementById(\'small_parcel_count\').value=\'\';\n  document.getElementById(\'total_building_count\').value=\'\';\n  buildingLayer.clearLayers();smallParcelLayer.clearLayers();oldParcelLayer.clearLayers();frontagePassLayer.clearLayers();frontageFailLayer.clearLayers();roadLayer.clearLayers();stationBoundaryLayer.clearLayers();stationBuffer250Layer.clearLayers();stationBuffer350Layer.clearLayers();stationBuffer500Layer.clearLayers();\n  hubRecordsByPnu.clear();analysisState.parcels.clear();analysisState.buildings=[];analysisState.roads=[];analysisState.metrics={};\n  const bs=document.getElementById(\'buildingStatus\');\n  if(bs)bs.innerHTML=\'<b>건축물 AUTO:</b> 구역을 그리면 자동 조회합니다.\';\n  document.getElementById(\'parcelStatus\').innerHTML=\'<b>과소필지 AUTO:</b> 구역을 그리면 자동 조회합니다.\';\n  document.getElementById(\'runBtn\').disabled=true;\n  runAllSchemeChecks();\n}\n\nasync function analyzeParcels(){\n  if(!activeGeometry)return;\n  const status=document.getElementById(\'parcelStatus\'),conn=document.getElementById(\'parcelConn\');status.innerHTML=\'<b>필지공간 AUTO:</b> VWorld 연속지적 조회 중...\';conn.textContent=\'필지공간 조회 중\';conn.className=\'conn planned\';parcelLayer.clearLayers();\n  try{\n    const features=await fetchParcelCandidatesBrowser(activeGeometry);parcelFeatureMap.clear();selectedParcelPnus.clear();\n    for(const f of features){const props=Object.assign({},f.properties||{});props.geometry_area_m2=parcelGeometryArea(f);props.official_area_m2=null;props.is_small=null;props.land_ledger=null;const nf={type:\'Feature\',id:f.id,geometry:f.geometry,properties:props};const key=parcelKey(nf);parcelFeatureMap.set(key,nf);selectedParcelPnus.add(key);}\n    syncParcelLayerFromState();updateRoadDownloadLink();document.getElementById(\'total_parcel_count\').value=features.length;document.getElementById(\'small_parcel_count\').value=\'\';status.innerHTML=`<b>필지공간 AUTO 완료:</b> 연속지적 ${features.length}필지 · PNU/지번/도형면적 확보. 공식면적은 토지대장·토지특성으로 추가조회합니다.`;conn.textContent=\'필지공간 AUTO\';conn.className=\'conn auto\';\n  }catch(e){document.getElementById(\'total_parcel_count\').value=\'\';document.getElementById(\'small_parcel_count\').value=\'\';status.innerHTML=\'<b>필지공간 AUTO 실패:</b> \'+String(e.message||e);conn.textContent=\'필지공간 MANUAL\';conn.className=\'conn manual\';}\n}\n\nasync function analyzeBuildings(){\n  if(!activeGeometry)return;\n  const status=document.getElementById(\'buildingStatus\');\n  const conn=document.getElementById(\'buildingConn\');\n\n  status.innerHTML=\'<b>건축물 AUTO:</b> 브라우저에서 VWorld 도로명주소 건물을 조회 중입니다...\';\n  conn.textContent=\'건축물 조회 중\';\n  conn.className=\'conn planned\';\n  buildingLayer.clearLayers();\n\n  try{\n    const features=await fetchSpatialFeaturesBrowser(\n      \'LT_C_SPBD\',\n      activeGeometry,\n      [\'bd_mgt_sn\',\'bul_man_no\',\'pk\']\n    );\n\n    const normalized=features.map(f=>{\n      const p=Object.assign({},f.properties||{});\n      // VWorld LT_C_SPBD commonly exposes pnu, bd_mgt_sn, gro_flo_co.\n      p._building_key=String(p.bd_mgt_sn||p.bul_man_no||p.pk||f.id||\'\');\n      return {type:\'Feature\',id:f.id,geometry:f.geometry,properties:p};\n    });\n\n    currentBuildingFeatures=normalized;\n    assignBuildingsToParcels();\n    buildingLayer.addData({type:\'FeatureCollection\',features:normalized});\n    document.getElementById(\'total_building_count\').value=normalized.length;\nconst pnuCount=new Set(normalized.map(f=>String((f.properties||{}).pnu||\'\')).filter(Boolean)).size;\n    const floorKnown=normalized.filter(f=>{\n      const v=Number((f.properties||{}).gro_flo_co);\n      return Number.isFinite(v);\n    }).length;\n\n    status.innerHTML=`<b>건축물 AUTO 완료:</b> VWorld LT_C_SPBD · 공간건물 ${fmt(normalized.length)}동 · PNU 확인 ${fmt(pnuCount)}개 · 지상층수 확인 ${fmt(floorKnown)}/${fmt(normalized.length)}동. <b>초기검토:</b> 공간건물은 위치 확인용이며 건축물 동수·노후도는 건축HUB 값으로 보정한다.`;\n    conn.textContent=\'건축물공간 AUTO\';\n    conn.className=\'conn auto\';\n  \n  }catch(e){\n    document.getElementById(\'total_building_count\').value=\'\';\n    buildingLayer.clearLayers();\n    status.innerHTML=\'<b>건축물 AUTO 실패:</b> \'+String(e.message||e)+\' · 수기 입력은 계속 사용할 수 있습니다.\';\n    conn.textContent=\'건축물 MANUAL\';\n    conn.className=\'conn manual\';\n  }\n}\n\n\nfunction hubFmtDay(v){\n  const s=String(v||\'\').replace(/\\D/g,\'\');\n  if(s.length!==8)return v||\'-\';\n  return `${s.slice(0,4)}-${s.slice(4,6)}-${s.slice(6,8)}`;\n}\nfunction escHtml(v){\n  return String(v??\'\').replace(/[&<>"\']/g,ch=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\',"\'":\'&#039;\'}[ch]));\n}\nfunction chunkArray(arr,n){\n  const out=[];\n  for(let i=0;i<arr.length;i+=n)out.push(arr.slice(i,i+n));\n  return out;\n}\n\nfunction isResidentialPurpose(name){\n  const s=String(name||\'\');\n  return /단독주택|공동주택|다가구주택|다세대주택|연립주택|아파트/.test(s);\n}\n\n\n\n// ---------- JUSO subway-station boundary GIS ----------\nfunction openStationDb(){\n  return new Promise((resolve,reject)=>{\n    const req=indexedDB.open(STATION_DB_NAME,1);\n    req.onupgradeneeded=()=>{const db=req.result;if(!db.objectStoreNames.contains(STATION_DB_STORE))db.createObjectStore(STATION_DB_STORE);};\n    req.onsuccess=()=>resolve(req.result);\n    req.onerror=()=>reject(req.error);\n  });\n}\nasync function saveStationDataset(payload){\n  try{\n    const db=await openStationDb();\n    await new Promise((resolve,reject)=>{\n      const tx=db.transaction(STATION_DB_STORE,\'readwrite\');\n      tx.objectStore(STATION_DB_STORE).put(payload,STATION_DB_KEY);\n      tx.oncomplete=resolve;tx.onerror=()=>reject(tx.error);\n    });\n    db.close();\n  }catch(e){console.warn(\'station IndexedDB save failed\',e);}\n}\nasync function loadStoredStationDataset(){\n  try{\n    const db=await openStationDb();\n    const data=await new Promise((resolve,reject)=>{\n      const tx=db.transaction(STATION_DB_STORE,\'readonly\');\n      const req=tx.objectStore(STATION_DB_STORE).get(STATION_DB_KEY);\n      req.onsuccess=()=>resolve(req.result||null);req.onerror=()=>reject(req.error);\n    });\n    db.close();\n    if(data?.features?.length){\n      stationFeatures=data.features;\n      stationSourceName=data.sourceName||\'저장된 TL_SPSB_STATN\';\n      stationAnalysis.loaded=true;\n      stationAnalysis.sourceDate=data.savedAt||\'\';\n      updateStationDataStatus();\n      if(activeGeometry)analyzeStationBoundary();\n    }\n  }catch(e){console.warn(\'station IndexedDB load failed\',e);}\n}\nasync function clearStoredStationData(){\n  try{\n    const db=await openStationDb();\n    await new Promise((resolve,reject)=>{\n      const tx=db.transaction(STATION_DB_STORE,\'readwrite\');\n      tx.objectStore(STATION_DB_STORE).delete(STATION_DB_KEY);\n      tx.oncomplete=resolve;tx.onerror=()=>reject(tx.error);\n    });\n    db.close();\n  }catch(e){}\n  stationFeatures=[];stationSourceName=\'\';\n  stationAnalysis.loaded=false;stationAnalysis.nearestName=\'\';stationAnalysis.nearestDistance=null;\n  stationAnalysis.coverage250=null;stationAnalysis.coverage350=null;stationAnalysis.coverage500=null;stationAnalysis.nearestFeatures=[];\n  stationBoundaryLayer.clearLayers();stationBuffer250Layer.clearLayers();stationBuffer350Layer.clearLayers();stationBuffer500Layer.clearLayers();\n  const dist=document.getElementById(\'scheme_station_distance\');if(dist){dist.readOnly=false;dist.dataset.auto=\'\';}\n  updateStationDataStatus();\n  runAllSchemeChecks();\n}\nfunction stationFeatureName(f){\n  const p=f?.properties||{};\n  return String(p.KOR_SUB_NM||p.kor_sub_nm||p.SUB_NM||p.sub_nm||p.STATN_NM||p.station_nm||p.name||\'역사\').trim();\n}\nfunction isStationHistoryFeatureCollection(g){\n  const nm=String(g?.fileName||g?.name||\'\').toUpperCase();\n  if(nm.includes(\'TL_SPSB_STATN\')||nm.includes(\'SPSB_STATN\'))return true;\n  const f=(g?.features||[])[0],p=f?.properties||{};\n  return (\'KOR_SUB_NM\' in p || \'SUB_STA_SN\' in p) && [\'Polygon\',\'MultiPolygon\'].includes(f?.geometry?.type);\n}\nasync function loadStationShpZip(file){\n  if(!file)return;\n  const status=document.getElementById(\'stationDataStatus\');\n  const note=document.getElementById(\'stationGisNote\');\n  status.textContent=\'ZIP 분석 중\';\n  note.textContent=\'TL_SPSB_STATN(지하철역사) 레이어를 찾고 있습니다.\';\n  try{\n    const parsed=await shp(await file.arrayBuffer());\n    const layers=Array.isArray(parsed)?parsed:[parsed];\n    let features=[];\n    for(const g of layers){\n      if(!g?.features?.length)continue;\n      const nm=String(g.fileName||\'\').toUpperCase();\n      const stationNamed=nm.includes(\'TL_SPSB_STATN\')||nm.includes(\'SPSB_STATN\');\n      const stationSchema=isStationHistoryFeatureCollection(g);\n      if(stationNamed||stationSchema){\n        features.push(...g.features.filter(f=>[\'Polygon\',\'MultiPolygon\'].includes(f?.geometry?.type)));\n      }\n    }\n    // Some shpjs builds return one FeatureCollection without fileName.\n    if(!features.length && layers.length===1){\n      const fs=layers[0]?.features||[];\n      features=fs.filter(f=>{\n        const p=f.properties||{};\n        return [\'Polygon\',\'MultiPolygon\'].includes(f?.geometry?.type) && (\'KOR_SUB_NM\' in p || \'SUB_STA_SN\' in p);\n      });\n    }\n    if(!features.length)throw new Error(\'TL_SPSB_STATN 폴리곤을 찾지 못했습니다. 주소기반 기타자료 전체분 ZIP인지 확인하세요.\');\n    stationFeatures=features;\n    stationSourceName=file.name;\n    stationAnalysis.loaded=true;\n    stationAnalysis.sourceDate=new Date().toISOString();\n    await saveStationDataset({features:stationFeatures,sourceName:stationSourceName,savedAt:stationAnalysis.sourceDate});\n    updateStationDataStatus();\n    if(activeGeometry)await analyzeStationBoundary();\n    else note.textContent=`지하철역사 ${stationFeatures.length}개 도형 저장 완료. 대상구역을 그리면 자동 계산합니다.`;\n  }catch(e){\n    status.textContent=\'연결 실패\';\n    note.textContent=\'지하철역사 ZIP 읽기 실패: \'+String(e.message||e);\n  }finally{\n    const inp=document.getElementById(\'stationShpInput\');if(inp)inp.value=\'\';\n  }\n}\nfunction updateStationDataStatus(){\n  const status=document.getElementById(\'stationDataStatus\');\n  const note=document.getElementById(\'stationGisNote\');\n  if(!status)return;\n  if(stationAnalysis.loaded && stationFeatures.length){\n    status.textContent=`${stationFeatures.length}개 역사`;\n    note.textContent=`${stationSourceName||\'TL_SPSB_STATN\'} · 브라우저 저장됨 · 대상구역 변경 시 자동 재분석`;\n  }else{\n    status.textContent=\'미연결\';\n    note.textContent=\'주소기반산업지원서비스 기타자료의 TL_SPSB_STATN(지하철역사) ZIP을 한 번 연결하면 이 브라우저에 저장합니다.\';\n  }\n}\nfunction polygonBoundaryLines(feature){\n  try{\n    const r=turf.polygonToLine(feature);\n    if(r?.type===\'FeatureCollection\')return r.features||[];\n    if(r?.type===\'Feature\')return [r];\n  }catch(e){}\n  return [];\n}\nfunction pointToAnyLineDistanceMeters(coord,lines){\n  let best=Infinity;\n  const pt=turf.point(coord);\n  for(const ln of lines){\n    try{\n      const d=turf.pointToLineDistance(pt,ln,{units:\'meters\'});\n      if(Number.isFinite(d)&&d<best)best=d;\n    }catch(e){}\n  }\n  return best;\n}\nfunction geometryBoundaryDistanceMeters(a,b){\n  try{if(turf.booleanIntersects(a,b))return 0;}catch(e){}\n  const la=polygonBoundaryLines(a),lb=polygonBoundaryLines(b);\n  if(!la.length||!lb.length)return null;\n  let best=Infinity;\n  const ca=turf.coordAll(a),cb=turf.coordAll(b);\n  for(const c of ca){const d=pointToAnyLineDistanceMeters(c,lb);if(d<best)best=d;}\n  for(const c of cb){const d=pointToAnyLineDistanceMeters(c,la);if(d<best)best=d;}\n  return Number.isFinite(best)?best:null;\n}\nfunction safeUnionPolygons(features){\n  let out=null;\n  for(const f of features){\n    if(!f)continue;\n    if(!out){out=f;continue;}\n    try{out=turf.union(out,f)||out;}catch(e){}\n  }\n  return out;\n}\nfunction coverageWithinStationBuffers(zoneFeature,features,meters){\n  if(!features.length)return null;\n  const buffs=[];\n  for(const f of features){\n    try{const b=turf.buffer(f,meters,{units:\'meters\'});if(b)buffs.push(b);}catch(e){}\n  }\n  const union=safeUnionPolygons(buffs);\n  if(!union)return null;\n  try{\n    const inter=turf.intersect(zoneFeature,union);\n    if(!inter)return 0;\n    const za=turf.area(zoneFeature);\n    return za>0?Math.min(100,turf.area(inter)/za*100):null;\n  }catch(e){return null;}\n}\nfunction candidateStationFeatures(zoneFeature,maxDistance=1200){\n  let search=null;\n  try{search=turf.buffer(zoneFeature,maxDistance,{units:\'meters\'});}catch(e){}\n  if(!search)return stationFeatures;\n  const bb=turf.bbox(search);\n  return stationFeatures.filter(f=>{\n    try{\n      const fb=turf.bbox(f);\n      if(fb[2]<bb[0]||fb[0]>bb[2]||fb[3]<bb[1]||fb[1]>bb[3])return false;\n      return true;\n    }catch(e){return false;}\n  });\n}\nasync function analyzeStationBoundary(){\n  updateStationDataStatus();\n  if(!stationAnalysis.loaded||!stationFeatures.length||!activeGeometry){\n    runAllSchemeChecks();return;\n  }\n  const zone=turf.feature(activeGeometry);\n  const candidates=candidateStationFeatures(zone,1200);\n  let best=null,bestDist=Infinity;\n  for(const f of candidates){\n    const d=geometryBoundaryDistanceMeters(zone,f);\n    if(d!=null && d<bestDist){bestDist=d;best=f;}\n  }\n  if(!best){\n    stationAnalysis.nearestName=\'\';stationAnalysis.nearestDistance=null;\n    document.getElementById(\'stationGisNote\').textContent=\'대상지 주변에서 지하철역사 도형을 찾지 못했습니다.\';\n    runAllSchemeChecks();return;\n  }\n  const nearestName=stationFeatureName(best);\n  const sameStation=candidates.filter(f=>stationFeatureName(f)===nearestName);\n  // Coverage uses all station history polygons near the site: a site can fall within buffers of more than one station.\n  const relevantForBuffers=candidates;\n  const cov250=coverageWithinStationBuffers(zone,relevantForBuffers,250);\n  const cov350=coverageWithinStationBuffers(zone,relevantForBuffers,350);\n  const cov500=coverageWithinStationBuffers(zone,relevantForBuffers,500);\n\n  stationAnalysis.nearestName=nearestName;\n  stationAnalysis.nearestDistance=bestDist;\n  stationAnalysis.coverage250=cov250;\n  stationAnalysis.coverage350=cov350;\n  stationAnalysis.coverage500=cov500;\n  stationAnalysis.nearestFeatures=sameStation;\n\n  document.getElementById(\'stationNearestName\').textContent=nearestName||\'-\';\n  document.getElementById(\'stationNearestDistance\').textContent=bestDist==null?\'-\':`${bestDist.toFixed(1)}m`;\n  document.getElementById(\'stationCoverage250\').textContent=cov250==null?\'-\':`${cov250.toFixed(1)}%`;\n  document.getElementById(\'stationCoverage350\').textContent=cov350==null?\'-\':`${cov350.toFixed(1)}%`;\n  document.getElementById(\'stationCoverage500\').textContent=cov500==null?\'-\':`${cov500.toFixed(1)}%`;\n\n  const dist=document.getElementById(\'scheme_station_distance\');\n  if(dist){dist.value=bestDist.toFixed(1);dist.readOnly=true;dist.dataset.auto=\'station\';}\n  const note=document.getElementById(\'stationDistanceInputNote\');\n  if(note)note.textContent=`AUTO · ${nearestName} 역사 경계 기준`;\n\n  stationBoundaryLayer.clearLayers();stationBoundaryLayer.addData(sameStation);\n  stationBuffer250Layer.clearLayers();stationBuffer350Layer.clearLayers();stationBuffer500Layer.clearLayers();\n  const b250=safeUnionPolygons(sameStation.map(f=>{try{return turf.buffer(f,250,{units:\'meters\'});}catch(e){return null;}}).filter(Boolean));\n  const b350=safeUnionPolygons(sameStation.map(f=>{try{return turf.buffer(f,350,{units:\'meters\'});}catch(e){return null;}}).filter(Boolean));\n  const b500=safeUnionPolygons(sameStation.map(f=>{try{return turf.buffer(f,500,{units:\'meters\'});}catch(e){return null;}}).filter(Boolean));\n  if(b250)stationBuffer250Layer.addData(b250);\n  if(b350)stationBuffer350Layer.addData(b350);\n  if(b500)stationBuffer500Layer.addData(b500);\n\n  document.getElementById(\'stationGisNote\').textContent=\n    `${nearestName} 경계 최단거리 ${bestDist.toFixed(1)}m · 대상지 포함률 250m ${cov250==null?\'-\':cov250.toFixed(1)+\'%\'} / 350m ${cov350==null?\'-\':cov350.toFixed(1)+\'%\'} / 500m ${cov500==null?\'-\':cov500.toFixed(1)+\'%\'}`;\n  runAllSchemeChecks();\n}\n\n\n// ---------- Six-scheme initial review engine ----------\nconst SCHEME_AS_OF = new Date(\'2026-08-24T00:00:00+09:00\');\nlet activeScheme=\'redevelopment\';\nlet schemeResults={};\n\nconst schemeNames={\n  redevelopment:\'주택재개발\',\n  reconstruction:\'재건축\',\n  activation:\'역세권활성화사업\',\n  safe:\'안심주택\',\n  station_complex:\'역세권복합개발 지구단위계획\',\n  longterm:\'역세권 장기전세주택\',\n  public_complex:\'도심 공공주택 복합사업\',\n  innovation:\'도심복합개발혁신지구\'\n};\nconst schemeBasis={\n  redevelopment:\'서울특별시 도시·주거환경정비 기본계획 + 도시정비법·서울시 도시 및 주거환경정비 조례\',\n  reconstruction:\'서울특별시 도시·주거환경정비 기본계획 + 도시정비법·재건축진단 관련 기준\',\n  activation:\'서울특별시 역세권 활성화사업 운영기준(2026.04.30.)\',\n  safe:\'서울특별시 안심주택 공급 지원에 관한 조례(2026.01.05.) + 건립·운영기준(2025.08.13.)\',\n  station_complex:\'서울시 「역세권 복합개발(고밀주거) 계획수립기준」 기반 — 최신 사업제안 전 재확인\',\n  longterm:\'서울특별시 역세권 장기전세주택 건립 운영기준(2026.03.06.)\',\n  public_complex:\'공공주택 특별법 시행령 별표4의2 + 공공주택 업무처리지침\',\n  innovation:\'도심 복합개발 지원에 관한 법률·시행령 + 서울특별시 조례·시행규칙(2026)\'\n};\n\nfunction schemeNum(id){\n  const el=document.getElementById(id); if(!el)return null;\n  const t=String(el.value??\'\').trim(); if(t===\'\')return null;\n  const n=Number(t); return Number.isFinite(n)?n:null;\n}\nfunction schemeVal(id){const el=document.getElementById(id);return el?String(el.value||\'\'):\'\';}\nfunction schemeYN(id){\n  const v=schemeVal(id); return v===\'yes\'?true:v===\'no\'?false:null;\n}\nfunction schemeChecked(id){return !!document.getElementById(id)?.checked}\nfunction schemeStatus(cond){\n  if(cond===null || cond===undefined)return \'REVIEW\';\n  return cond?\'PASS\':\'FAIL\';\n}\nfunction schemeRow(item,rule,value,status,note=\'\',required=true){\n  return {item,rule,value:value??\'-\',status,note,required};\n}\nfunction selectedHubRecordsForSchemes(){\n  const mp=new Map();\n  for(const pnu of selectedParcelPnus){\n    for(const r of (hubRecordsByPnu.get(String(pnu))||[])){\n      const k=String(r.mgmBldrgstPk||`${r.pnu||\'\'}|${r.dongNm||\'\'}|${r.bldNm||\'\'}|${r.useAprDay||\'\'}`);\n      if(!mp.has(k))mp.set(k,r);\n    }\n  }\n  return [...mp.values()];\n}\nfunction parseHubDate(v){\n  const s=String(v||\'\').replace(/\\D/g,\'\');\n  if(s.length!==8)return null;\n  const y=Number(s.slice(0,4)),m=Number(s.slice(4,6)),d=Number(s.slice(6,8));\n  if(!y||!m||!d)return null;\n  const dt=new Date(`${s.slice(0,4)}-${s.slice(4,6)}-${s.slice(6,8)}T00:00:00+09:00`);\n  return Number.isNaN(dt.getTime())?null:dt;\n}\nfunction olderThanYears(dt,years){\n  if(!dt)return false;\n  const anniv=new Date(dt.getTime());\n  anniv.setFullYear(anniv.getFullYear()+years);\n  return SCHEME_AS_OF>=anniv;\n}\nfunction schemeAgeStat(years){\n  const rs=selectedHubRecordsForSchemes();\n  const known=rs.filter(r=>parseHubDate(r.useAprDay));\n  const old=rs.filter(r=>olderThanYears(parseHubDate(r.useAprDay),years));\n  const ratio=rs.length?old.length/rs.length*100:null; // unknown approval dates remain in denominator (conservative)\n  return {total:rs.length,known:known.length,count:old.length,ratio};\n}\nfunction fmtSchemePct(v){return v==null?\'-\':`${Number(v).toFixed(1)}%`}\nfunction fmtSchemeArea(v){return v==null?\'-\':`${Number(v).toLocaleString(\'ko-KR\',{maximumFractionDigits:0})}㎡`}\nfunction commonSchemeData(){\n  return {\n    area:schemeNum(\'area_m2\'),\n    dist:(stationAnalysis.loaded && stationAnalysis.nearestDistance!=null)?stationAnalysis.nearestDistance:schemeNum(\'scheme_station_distance\'),\n    stationAuto:stationAnalysis.loaded && stationAnalysis.nearestDistance!=null,\n    stationName:stationAnalysis.nearestName||\'\',\n    coverage250:stationAnalysis.loaded?stationAnalysis.coverage250:null,\n    coverage350:stationAnalysis.loaded?stationAnalysis.coverage350:null,\n    coverage500:stationAnalysis.loaded?stationAnalysis.coverage500:null,\n    lines:schemeNum(\'scheme_station_lines\'),\n    center:schemeVal(\'scheme_center\'),\n    zoning:schemeVal(\'scheme_zoning\'),\n    coverage:schemeNum(\'scheme_station_coverage\'),\n    blockShare:schemeNum(\'scheme_block_share\'),\n    roadFaces:schemeNum(\'scheme_road_faces\'),\n    maxRoad:schemeNum(\'scheme_max_road_width\'),\n    has8:schemeYN(\'scheme_has_8m_road\'),\n    has20:schemeYN(\'scheme_has_20m_arterial\'),\n    enclosed6:schemeYN(\'scheme_enclosed_by6m\'),\n    road20Perimeter:schemeNum(\'scheme_road20_perimeter_ratio\'),\n    arterialDist:schemeNum(\'scheme_arterial_distance\'),\n    medicalDist:schemeNum(\'scheme_medical_distance\'),\n    units:schemeNum(\'scheme_planned_units\'),\n    factory:schemeNum(\'scheme_factory_ratio\'),\n    aptMaxArea:schemeNum(\'scheme_apartment_max_area\'),\n    aptMaxRatio:schemeNum(\'scheme_apartment_max_ratio\'),\n    ownerConsent:schemeNum(\'scheme_owner_consent\'),\n    landConsent:schemeNum(\'scheme_land_consent\'),\n    road20OwnerConsent:schemeNum(\'scheme_road20_owner_consent\'),\n    road20LandConsent:schemeNum(\'scheme_road20_land_consent\'),\n    plannedHousingFloor:schemeNum(\'scheme_planned_housing_floor_ratio\'),\n    priorNegotiation:schemeYN(\'scheme_prior_negotiation\'),\n    excluded:schemeYN(\'scheme_site_excluded\'),\n    underdeveloped:schemeYN(\'scheme_underdeveloped\'),\n    allNonResidential:schemeChecked(\'scheme_existing_nonresidential_only\'),\n    lowriseBad:schemeChecked(\'scheme_lowrise_bad_infra\'),\n    specialPlanning:schemeChecked(\'scheme_special_planning\'),\n    targetZoning:schemeVal(\'scheme_target_zoning\'),\n    infraGood:schemeChecked(\'scheme_infra_good\'),\n    policyFacility:schemeChecked(\'scheme_policy_facility\'),\n    reconstructionApartment:schemeChecked(\'scheme_reconstruction_apartment\'),\n    reconstructionDiagnosis:schemeChecked(\'scheme_reconstruction_diagnosis\'),\n    age20:schemeAgeStat(20),\n    age30:schemeAgeStat(30)\n  };\n}\nfunction zoneIn(z,arr){return z?arr.includes(z):null}\nfunction passYesNo(v){return v===null?\'REVIEW\':(v?\'PASS\':\'FAIL\')}\nfunction overallScheme(rows){\n  const mandatory=rows.filter(r=>r.required!==false);\n  if(mandatory.some(r=>r.status===\'FAIL\'))return \'FAIL\';\n  if(mandatory.some(r=>r.status===\'REVIEW\'))return \'REVIEW\';\n  return \'PASS\';\n}\nfunction overallLabel(s){return s===\'PASS\'?\'우선검토\':s===\'FAIL\'?\'조건미달\':\'추가입력\'}\nfunction passMeta(rows){\n  const mandatory=rows.filter(r=>r.required!==false);\n  const p=mandatory.filter(r=>r.status===\'PASS\').length;\n  const f=mandatory.filter(r=>r.status===\'FAIL\').length;\n  const r=mandatory.filter(r=>r.status===\'REVIEW\').length;\n  return `${p} 충족 · ${f} 미달 · ${r} 확인`;\n}\n\n\nfunction targetOrSuggested(c,name){\n  if(c.targetZoning)return c.targetZoning;\n  const z=c.zoning||\'\';\n  if(name===\'redevelopment\'||name===\'reconstruction\'){\n    if(z===\'제1종일반주거\')return \'제2종일반주거\';\n    if(z===\'제2종일반주거(7층)\')return \'제3종일반주거\';\n    if(z===\'제2종일반주거\')return \'제3종일반주거\';\n    if(z===\'제3종일반주거\')return \'준주거\';\n    return z;\n  }\n  if(name===\'activation\'){\n    if([\'제2종일반주거(7층)\',\'제2종일반주거\',\'제3종일반주거\'].includes(z))return \'준주거\';\n    if(z===\'준주거\')return \'일반상업\';\n    if(z===\'근린상업\')return \'일반상업\';\n    return z;\n  }\n  if(name===\'safe\'){\n    if(z===\'자연녹지\'||z===\'제1종일반주거\')return \'제2종일반주거\';\n    if([\'제2종일반주거(7층)\',\'제2종일반주거\',\'제3종일반주거\'].includes(z))return \'준주거\';\n    return z;\n  }\n  if(name===\'station_complex\'){\n    if([\'제2종일반주거(7층)\',\'제2종일반주거\',\'제3종일반주거\'].includes(z))return \'준주거\';\n    return z;\n  }\n  if(name===\'longterm\'){\n    if([\'제2종일반주거(7층)\',\'제2종일반주거\'].includes(z))return \'제3종일반주거\';\n    if(z===\'제3종일반주거\')return \'준주거\';\n    return z;\n  }\n  return z;\n}\n\nfunction redevelopmentFarProfile(current,target){\n  const key=`${current}->${target}`;\n  const map={\n    \'제1종일반주거->제2종일반주거\':{base:150,allow:170,cap:200,legal:250},\n    \'제1종일반주거->제3종일반주거\':{base:150,allow:170,cap:250,legal:300},\n    \'제2종일반주거(7층)->제2종일반주거\':{base:190,allow:210,cap:250,legal:250},\n    \'제2종일반주거(7층)->제3종일반주거\':{base:190,allow:210,cap:250,legal:300},\n    \'제2종일반주거->제3종일반주거\':{base:190,allow:210,cap:250,legal:300},\n    \'제3종일반주거->준주거\':{base:210,allow:230,cap:400,legal:500}\n  };\n  return map[key]||null;\n}\nfunction stationRenewalUpzone(c,name){\n  const target=targetOrSuggested(c,name);\n  const profile=redevelopmentFarProfile(c.zoning,target);\n  let distanceStatus=\'REVIEW\',distanceText=\'역거리 미입력\';\n  if(c.dist!=null){\n    if(c.dist<=250){distanceStatus=\'PASS\';distanceText=\'250m 이내\';}\n    else if(c.dist<=350){distanceStatus=\'REVIEW\';distanceText=\'250~350m 위원회 심의 완화구간\';}\n    else {distanceStatus=\'FAIL\';distanceText=\'350m 초과\';}\n  }\n  const policyOk=c.infraGood && c.policyFacility;\n  const eligible=(distanceStatus===\'PASS\' && policyOk && profile)?\'PASS\':\n                 (distanceStatus===\'FAIL\'||(c.zoning&&target&&!profile))?\'FAIL\':\'REVIEW\';\n  const far=profile?`기준 ${profile.base}% · 허용 ${profile.allow}% · 상한 ${profile.cap}% · 법적상한 ${profile.legal}%`:\'상향경로 선택/확인\';\n  return {\n    target,profile,eligible,distanceStatus,distanceText,\n    zone:`${c.zoning||\'-\'} → ${target||\'-\'}`,\n    far,\n    contribution:\'기존 정률 공공기여 완화 + 임대주택·노인시설·공원 등 시 정책 연계 전략시설 도입 전제(구체량 위원회 결정)\'\n  };\n}\n\nfunction activationContribution(current,target){\n  const map={\n    \'제2종일반주거(7층)->제3종일반주거\':10,\n    \'제2종일반주거->제3종일반주거\':10,\n    \'제2종일반주거(7층)->준주거\':25,\n    \'제2종일반주거->준주거\':25,\n    \'제3종일반주거->준주거\':19,\n    \'제2종일반주거(7층)->근린상업\':33,\n    \'제2종일반주거->근린상업\':33,\n    \'제3종일반주거->근린상업\':29,\n    \'준주거->근린상업\':17,\n    \'제2종일반주거(7층)->일반상업\':38,\n    \'제2종일반주거->일반상업\':38,\n    \'제3종일반주거->일반상업\':34,\n    \'준주거->일반상업\':25,\n    \'근린상업->일반상업\':13,\n    \'제3종일반주거->중심상업\':38,\n    \'준주거->중심상업\':30,\n    \'근린상업->중심상업\':20,\n    \'일반상업->중심상업\':10\n  };\n  return map[`${current}->${target}`]??null;\n}\nfunction ordinanceFarForZone(z){\n  const map={\'제2종일반주거\':200,\'제3종일반주거\':250,\'준주거\':400,\'근린상업\':600,\'일반상업\':800,\'중심상업\':1000,\'준공업\':400};\n  return map[z]??null;\n}\nfunction activationDensity(c){\n  const target=targetOrSuggested(c,\'activation\');\n  const pc=activationContribution(c.zoning,target);\n  const far=ordinanceFarForZone(target);\n  return {\n    zone:`${c.zoning||\'-\'} → ${target||\'-\'}`,\n    far:far?`상한용적률 ${far}% 이내(변경 후 용도지역 조례용적률, 계획용적률은 위원회 결정)`:\'목표 용도지역 선택 필요\',\n    contribution:pc!=null?`부지면적 기준 ${pc}% 이상 · 증가용적률의 50% 환산 원칙`:\'종상향 경로 선택 시 공공기여율 자동표시\'\n  };\n}\nfunction safeDensity(c){\n  const target=targetOrSuggested(c,\'safe\');\n  const key=`${c.zoning}->${target}`;\n  const change={\n    \'자연녹지->제2종일반주거\':{base:200,cap:250,pc:30},\n    \'제1종일반주거->제2종일반주거\':{base:200,cap:250,pc:10},\n    \'제2종일반주거(7층)->준주거\':{base:400,cap:500,pc:15},\n    \'제2종일반주거->준주거\':{base:400,cap:500,pc:15},\n    \'제3종일반주거->준주거\':{base:400,cap:500,pc:10},\n    \'제2종일반주거(7층)->근린상업\':{base:540,cap:900,pc:25},\n    \'제2종일반주거->근린상업\':{base:540,cap:900,pc:25},\n    \'제3종일반주거->근린상업\':{base:540,cap:900,pc:20},\n    \'준주거->근린상업\':{base:540,cap:900,pc:10},\n    \'제2종일반주거(7층)->일반상업\':{base:680,cap:1300,pc:30},\n    \'제2종일반주거->일반상업\':{base:680,cap:1300,pc:30},\n    \'제3종일반주거->일반상업\':{base:680,cap:1300,pc:25},\n    \'준주거->일반상업\':{base:680,cap:1300,pc:20},\n    \'근린상업->일반상업\':{base:700,cap:1300,pc:10}\n  }[key];\n  if(change)return {zone:`${c.zoning} → ${target}`,far:`기본 ${change.base}% · 상한 ${change.cap}%`,contribution:`공공기여율 ${change.pc}% 이상 · 공공임대주택 우선`};\n\n  if(c.zoning===target || !target){\n    if(c.zoning===\'준주거\')return {zone:\'용도지역 변경 없음\',far:\'기본 500% (서울도심 400%)\',contribution:\'공공기여 6% 이상 (서울도심 4%)\'};\n    if(c.zoning===\'준공업\'){\n      const pc=c.factory==null?\'10~15\':c.factory<10?\'10\':\'15\';\n      return {zone:\'용도지역 변경 없음\',far:\'기본 400%\',contribution:`공공기여 ${pc}% 이상${c.factory!=null&&c.factory>=10?\' · 1/2 공공임대 + 1/2 임대산업시설\':\'\'}`};\n    }\n    if(c.zoning===\'근린상업\')return {zone:\'용도지역 변경 없음\',far:\'기본 600~800%\',contribution:\'공공기여 8~12% 이상(선택 기본용적률에 연동)\'};\n    if(c.zoning===\'일반상업\')return {zone:\'용도지역 변경 없음\',far:\'기본 800~1,000%\',contribution:\'공공기여 10~14% 이상(서울도심 별도)\'};\n  }\n  return {zone:`${c.zoning||\'-\'} → ${target||\'-\'}`,far:\'세부 경로 확인\',contribution:\'세부 경로 확인\'};\n}\nfunction stationComplexDensity(c){\n  const center=[\'downtown\',\'metro\',\'regional\',\'district\'].includes(c.center);\n  let baseRange=center?\'500~600\':\'400~500\';\n  let extra=[];\n  if(c.has20===true)extra.push(\'간선변 최대 +70%p\');\n  if(c.maxRoad!=null && c.maxRoad>=10)extra.push(\'10m+ 진출입 최대 +40%p\');\n  return {\n    zone:`${c.zoning||\'-\'} → 준주거 원칙`,\n    far:`상한 ${baseRange}%${extra.length?\' · \'+extra.join(\' · \'):\'\'} · 조건 충족 시 최대 700/600% 검토`,\n    contribution:\'완화용적률(상한-허용)의 1/2을 부지면적으로 환산 · 공공기여의 70% 이상 공공임대, 30% 이하 공공시설\'\n  };\n}\nfunction longtermDensity(c){\n  const target=targetOrSuggested(c,\'longterm\');\n  const route=schemeVal(\'longterm_route\');\n  const first=c.dist!=null && c.dist<=350;\n  let far=\'용도지역·사업계획 방식 입력 필요\';\n  if(route===\'district\'){\n    if([\'제2종일반주거(7층)\',\'제2종일반주거\'].includes(c.zoning)&&target===\'제3종일반주거\')far=\'기준 200% · 상한 300%\';\n    else if(c.zoning===\'제3종일반주거\'&&target===\'준주거\')far=`기준 250% · 상한 ${first?\'500~700\':\'500\'}%`;\n  }else{\n    if([\'제2종일반주거(7층)\',\'제2종일반주거\'].includes(c.zoning)&&target===\'제3종일반주거\')far=\'기준 190%(완화 210%) · 상한 250% · 법적상한 300%\';\n    else if(c.zoning===\'제3종일반주거\'&&target===\'준주거\')far=`기준 210%(완화 230%) · 상한 250% · 법적상한 ${first?\'500~700\':\'500\'}%`;\n  }\n  return {\n    zone:`${c.zoning||\'-\'} → ${target||\'-\'}`,\n    far,\n    contribution:\'용도지역 변경 등 완화용적률의 1/2 이상을 부지면적으로 환산 · 장기전세주택 우선 제공\'\n  };\n}\nfunction publicComplexDensity(c){\n  const typ=schemeVal(\'public_complex_type\');\n  const rental=typ===\'commercial\'?\'공공임대 15% 이상\':\'공공임대 10% 이상\';\n  return {\n    zone:\'복합지구계획에서 용도지역·밀도 특례 검토\',\n    far:\'공공주택특별법상 지구계획을 통한 용적률 완화 가능 · 구체 용적률은 지구계획/업무처리지침 산정\',\n    contribution:`공공성 구조: ${rental} + 공공분양 등 공공주택 의무공급 구성`\n  };\n}\nfunction innovationDensity(c){\n  const typ=schemeVal(\'innovation_type\');\n  return {\n    zone:typ===\'growth\'?\'도시혁신구역 지정 및 복합밀도 특례 검토\':\'현 용도지역 기반 복합개발 특례 검토\',\n    far:\'국토계획법 시행령 최대한도까지 · 주거지역은 해당 최대한도의 120%, 준주거는 140%까지 완화 가능\',\n    contribution:\'규제특례 적용 시 기반시설·생활인프라·공공시설·국민주택규모 주택 등 공공기여 · 서울은 필수 기반시설 우선, 일부 현금납부 가능\'\n  };\n}\nfunction densityForScheme(name,c){\n  if(name===\'redevelopment\'||name===\'reconstruction\')return stationRenewalUpzone(c,name);\n  if(name===\'activation\')return activationDensity(c);\n  if(name===\'safe\')return safeDensity(c);\n  if(name===\'station_complex\')return stationComplexDensity(c);\n  if(name===\'longterm\')return longtermDensity(c);\n  if(name===\'public_complex\')return publicComplexDensity(c);\n  if(name===\'innovation\')return innovationDensity(c);\n  return {zone:\'-\',far:\'-\',contribution:\'-\'};\n}\n\nfunction checkRedevelopment(c){\n  const rows=[];\n  const total=Number(analysisState.metrics.total_buildings)||0;\n  const old=Number(analysisState.metrics.old_count)||0;\n  const oldRatio=total?old/total*100:null;\n  const totalParcels=Number(document.getElementById(\'total_parcel_count\')?.value)||selectedParcelPnus.size||0;\n  const small=Number(document.getElementById(\'small_parcel_count\')?.value)||0;\n  const smallRatio=totalParcels?small/totalParcels*100:null;\n\n  rows.push(schemeRow(\'구역면적\',\'주택정비형 재개발 초기검토 면적\',fmtSchemeArea(c.area),c.area==null?\'REVIEW\':schemeStatus(c.area>=10000),\'현재 플랫폼 기본 10,000㎡ 기준\'));\n  rows.push(schemeRow(\'노후도\',\'노후·불량건축물 수 비율 60% 이상\',oldRatio==null?\'-\':`${old}/${total}동 (${oldRatio.toFixed(1)}%)`,oldRatio==null?\'REVIEW\':schemeStatus(oldRatio>=60),\'건축HUB 연령판정 기반 초기검토\'));\n  rows.push(schemeRow(\'과소필지\',\'90㎡ 미만 필지 수·비율 확인\',smallRatio==null?\'-\':`${small}/${totalParcels}필지 (${smallRatio.toFixed(1)}%)`,\'INFO\',\'물리적 정비여건 보조지표\',false));\n\n  const up=stationRenewalUpzone(c,\'redevelopment\');\n  rows.push(schemeRow(\'역세권 용도지역 상향\',\'승강장 250m 이내 원칙, 위원회 심의 최대 350m + 기반시설 양호 + 정책연계 전략시설\',\n    `${up.distanceText} / 기반시설 ${c.infraGood?\'O\':\'-\'} / 전략시설 ${c.policyFacility?\'O\':\'-\'}`,up.eligible,\'도시·주거환경정비 기본계획 역세권 고밀복합개발\'));\n  rows.push(schemeRow(\'용적률 상향\',up.zone,up.far,up.profile?\'PASS\':\'REVIEW\',\'역세권 정비사업 용도지역 상향표\',false));\n  rows.push(schemeRow(\'공공기여\',\'종상향과 연계한 전략시설 도입\',up.contribution,\'INFO\',\'정률만으로 확정하지 않고 정책시설·위원회 계획 반영\',false));\n  return rows;\n}\n\nfunction checkReconstruction(c){\n  const rows=[];\n  rows.push(schemeRow(\'사업대상\',\'공동주택 재건축 대상\',c.reconstructionApartment?\'체크\':\'미체크\',c.reconstructionApartment?\'PASS\':\'REVIEW\',\'단독/저층 재개발과 구분\'));\n  rows.push(schemeRow(\'재건축진단\',\'재건축진단 통과·면제 등 추진요건 확인\',c.reconstructionDiagnosis?\'확인\':\'미확인\',c.reconstructionDiagnosis?\'PASS\':\'REVIEW\',\'구체 절차는 정비계획 추진단계 재확인\'));\n  const up=stationRenewalUpzone(c,\'reconstruction\');\n  rows.push(schemeRow(\'역세권 용도지역 상향\',\'승강장 250m 이내 원칙, 위원회 심의 최대 350m + 기반시설 양호 + 정책연계 전략시설\',\n    `${up.distanceText} / 기반시설 ${c.infraGood?\'O\':\'-\'} / 전략시설 ${c.policyFacility?\'O\':\'-\'}`,up.eligible,\'도시·주거환경정비 기본계획\'));\n  rows.push(schemeRow(\'용적률 상향\',up.zone,up.far,up.profile?\'PASS\':\'REVIEW\',\'역세권 정비사업 용도지역 상향표\',false));\n  rows.push(schemeRow(\'공공기여\',\'종상향과 연계한 전략시설 도입\',up.contribution,\'INFO\',\'임대주택·노인시설·공원 등 정책연계 시설\',false));\n  return rows;\n}\n\nfunction checkActivation(c){\n  const rows=[];\n  const route=schemeVal(\'activation_route\');\n  const center350=[\'downtown\',\'metro\',\'regional\'].includes(c.center);\n  const threshold=(center350 || c.lines>=2)?350:250;\n\n  rows.push(schemeRow(\'역세권 범위\',`승강장 경계 ${threshold}m 이내`,c.dist==null?\'-\':`${Number(c.dist).toFixed(1)}m${c.stationAuto&&c.stationName?\' · \'+c.stationName:\'\'}`,\n    c.dist==null?\'REVIEW\':schemeStatus(c.dist<=threshold),\n    `도심·광역·지역중심 또는 2개 노선 이상 환승역 350m / 지구중심·비중심 단일역 250m`));\n\n  let blockStatus=\'REVIEW\',blockNote=\'가로구역의 1/2 이상이면 원칙 대상\';\n  if(c.blockShare!=null){\n    if(c.blockShare>=50)blockStatus=\'PASS\';\n    else {\n      blockStatus=\'REVIEW\';\n      blockNote=(c.maxRoad!=null && c.maxRoad>=20)?\'1/2 미만이나 20m 이상 간선가로 접면: 위원회 심의 가능\':\'1/2 미만: 구역 정형화 등 위원회 인정 여부 확인\';\n    }\n  }\n  rows.push(schemeRow(\'가로구역\',`역세권 범위가 가로구역 1/2 이상`,c.blockShare==null?\'-\':`${c.blockShare}%`,blockStatus,blockNote));\n\n  let areaStatus=\'REVIEW\',areaNote=\'기본 1,500~10,000㎡\';\n  if(c.area!=null){\n    if(c.area<1500)areaStatus=\'FAIL\';\n    else if(c.area<=10000)areaStatus=\'PASS\';\n    else if(route===\'redevelopment\' && c.area<=30000){areaStatus=\'REVIEW\';areaNote=\'도시정비형 재개발 30,000㎡ 이하: 지원 자문단 인정 필요\';}\n    else areaStatus=\'FAIL\';\n  }\n  rows.push(schemeRow(\'대상지 면적\',route===\'redevelopment\'?\'1,500~10,000㎡ 원칙 / 재개발 30,000㎡ 이하 자문 인정\':\'1,500~10,000㎡\',fmtSchemeArea(c.area),areaStatus,areaNote));\n\n  const allowed=[\'제2종일반주거(7층)\',\'제2종일반주거\',\'제3종일반주거\',\'준주거\',\'근린상업\',\'일반상업\'];\n  rows.push(schemeRow(\'용도지역\',\'2종(7층 포함)·3종·준주거·근린상업·일반상업\',c.zoning||\'-\',\n    c.zoning?schemeStatus(allowed.includes(c.zoning)):\'REVIEW\',\'운영기준 대상 용도지역\'));\n\n  let roadStatus=\'REVIEW\';\n  if(c.roadFaces!=null && c.has8!==null){\n    roadStatus=schemeStatus(c.roadFaces>=2 && c.has8===true);\n  }\n  rows.push(schemeRow(\'도로\',\'2면 이상 도로 접면 + 최소 1면 폭 8m 이상\',\n    `${c.roadFaces==null?\'-\':c.roadFaces+\'면\'} / 8m ${c.has8===null?\'-\':c.has8?\'접함\':\'미접함\'}`,\n    roadStatus,\'원활한 차량 진출입 가능한 도로\'));\n\n  rows.push(schemeRow(\'사전협상 제외\',\'5,000㎡ 이상 사전협상 대상지요건에 해당하면 제외\',\n    c.priorNegotiation===null?\'-\':c.priorNegotiation?\'해당\':\'비해당\',\n    c.priorNegotiation===null?\'REVIEW\':schemeStatus(!c.priorNegotiation),\'면적만으로 확정하지 않고 대상지요건 여부 입력\'));\n\n  const ageInfo=route===\'redevelopment\'\n    ? `재개발 연령판정 ${analysisState.metrics.old_count??\'-\'}/${analysisState.metrics.total_buildings??\'-\'}동`\n    : `20년 이상 ${c.age20.count}/${c.age20.total}동 (${fmtSchemePct(c.age20.ratio)})`;\n  rows.push(schemeRow(\'노후도\',\'사업유형별 노후건축물 기준 적용\',ageInfo,\'INFO\',\'지구단위계획·도시정비형 재개발 각각의 노후도 기준 적용\',false));\n\n  if(route===\'district\'){\n    rows.push(schemeRow(\'대상지 선정 동의\',\'대상 토지면적의 2/3 이상 토지소유자 동의\',\n      c.landConsent==null?\'-\':`${c.landConsent}%`,\n      c.landConsent==null?\'REVIEW\':schemeStatus(c.landConsent>=66.6667),\'지구단위계획 방식\'));\n  }else{\n    const st=(c.ownerConsent==null||c.landConsent==null)?\'REVIEW\':schemeStatus(c.ownerConsent>=50 && c.landConsent>=40);\n    rows.push(schemeRow(\'대상지 선정 동의\',\'토지등소유자 50% 이상 + 토지면적 40% 이상\',\n      `소유자 ${c.ownerConsent==null?\'-\':c.ownerConsent+\'%\'} / 면적 ${c.landConsent==null?\'-\':c.landConsent+\'%\'}`,st,\'도시정비형 재개발 방식\'));\n  }\n  const den=activationDensity(c);\n  rows.push(schemeRow(\'제도적 용적률\',den.zone,den.far,\'INFO\',\'용도지역 변경·공공기여 전제\',false));\n  rows.push(schemeRow(\'공공기여 기준\',\'종상향 증가용적률의 50% 환산\',den.contribution,\'INFO\',\'운영기준 제4장 공공기여율 표 적용\',false));\n  return rows;\n}\n\nfunction checkSafe(c){\n  const rows=[];\n  const typ=schemeVal(\'safe_location_type\');\n  if(typ===\'station\'){\n    let st=\'REVIEW\',note=\'250m 이내 원칙\';\n    if(c.dist!=null){\n      if(c.dist<=250)st=\'PASS\';\n      else if(c.dist<=350){st=\'REVIEW\';note=\'250~350m: 통합심의위원회 인정 가능구간\';}\n      else st=\'FAIL\';\n    }\n    rows.push(schemeRow(\'입지\',\'역세권 승강장 250m / 예외 350m\',c.dist==null?\'-\':`${Number(c.dist).toFixed(1)}m${c.stationAuto&&c.stationName?\' · \'+c.stationName:\'\'}`,st,note));\n  }else if(typ===\'arterial\'){\n    rows.push(schemeRow(\'입지\',\'주·보조간선도로 경계 50m 이내\',c.arterialDist==null?\'-\':`${c.arterialDist}m`,\n      c.arterialDist==null?\'REVIEW\':schemeStatus(c.arterialDist<=50),\'간선도로변\'));\n  }else{\n    rows.push(schemeRow(\'입지\',\'종합병원·시립병원·보건소 경계 350m 이내\',c.medicalDist==null?\'-\':`${c.medicalDist}m`,\n      c.medicalDist==null?\'REVIEW\':schemeStatus(c.medicalDist<=350),\'의료시설 중심지역\'));\n  }\n\n  const safeCoverage=c.stationAuto ? ((c.dist!=null&&c.dist<=250)?c.coverage250:c.coverage350) : c.coverage;\n  let cov=\'REVIEW\',covNote=\'사업대상지 과반이 중심지역에 포함\';\n  if(safeCoverage!=null){\n    if(safeCoverage>50)cov=\'PASS\';\n    else {cov=\'REVIEW\';covNote=\'과반 미만: 토지 효율이용·구역 정형화 필요 시 통합심의위원회 예외 가능\';}\n  }\n  rows.push(schemeRow(\'중심지역 포함\',\'사업대상지 과반 포함\',safeCoverage==null?\'-\':`${safeCoverage.toFixed? safeCoverage.toFixed(1):safeCoverage}%`,cov,covNote+(c.stationAuto?\' · 지하철역사 GIS AUTO\':\'\')));\n\n  let zoneStatus=\'REVIEW\',zoneNote=\'주거지역은 시장이 정하는 세부지역 확인\';\n  if(c.zoning){\n    if([\'준공업\',\'근린상업\',\'일반상업\'].includes(c.zoning))zoneStatus=\'PASS\';\n    else if([\'제1종일반주거\',\'제2종일반주거(7층)\',\'제2종일반주거\',\'제3종일반주거\',\'준주거\'].includes(c.zoning))zoneStatus=\'PASS\';\n    else if(c.zoning===\'자연녹지\'){zoneStatus=\'REVIEW\';zoneNote=\'자연녹지는 어르신 대상 등 세부기준 충족 시\';}\n    else zoneStatus=\'FAIL\';\n  }\n  rows.push(schemeRow(\'용도지역\',\'시장 지정 주거지역·준공업·근린/일반상업·일부 자연녹지\',c.zoning||\'-\',zoneStatus,zoneNote));\n\n  const agePass=c.allNonResidential ? true : (c.age20.ratio==null?null:c.age20.ratio>=50);\n  rows.push(schemeRow(\'노후도\',\'20년 이상 건축물 50% 이상 / 기존 건축물이 전부 비주거면 충족 간주\',\n    c.allNonResidential?\'비주거 전체 → 간주충족\':`${c.age20.count}/${c.age20.total}동 (${fmtSchemePct(c.age20.ratio)})`,\n    agePass===null?\'REVIEW\':schemeStatus(agePass),`사용승인일 확인 ${c.age20.known}/${c.age20.total}동`));\n\n  rows.push(schemeRow(\'제외지역\',\'정책상 보전·양호 저층주거·타 법령 추진지역 등 제외\',\n    c.excluded===null?\'-\':c.excluded?\'해당\':\'비해당\',\n    c.excluded===null?\'REVIEW\':schemeStatus(!c.excluded),\'공통 제외지역 확인\'));\n  const den=safeDensity(c);\n  rows.push(schemeRow(\'제도적 용적률\',den.zone,den.far,\'INFO\',\'안심주택 기본·상한용적률 체계\',false));\n  rows.push(schemeRow(\'공공기여 기준\',\'기본용적률·종상향 경로별 공공기여율\',den.contribution,\'INFO\',\'공공임대주택 공급 우선\',false));\n  return rows;\n}\n\nfunction checkStationComplex(c){\n  const rows=[];\n  rows.push(schemeRow(\'역세권\',\'승강장 경계 반경 250m 이내\',c.dist==null?\'-\':`${Number(c.dist).toFixed(1)}m${c.stationAuto&&c.stationName?\' · \'+c.stationName:\'\'}`,\n    c.dist==null?\'REVIEW\':schemeStatus(c.dist<=250),\'2021 서울시 공개 수립기준\'));\n  rows.push(schemeRow(\'면적\',\'1,500~5,000㎡\',fmtSchemeArea(c.area),\n    c.area==null?\'REVIEW\':schemeStatus(c.area>=1500 && c.area<=5000),\'\'));\n  rows.push(schemeRow(\'가로구역\',\'가로구역의 1/2 이상\',c.blockShare==null?\'-\':`${c.blockShare}%`,\n    c.blockShare==null?\'REVIEW\':schemeStatus(c.blockShare>=50),\'\'));\n  rows.push(schemeRow(\'도로\',\'2면 이상 도로 접면\',c.roadFaces==null?\'-\':`${c.roadFaces}면`,\n    c.roadFaces==null?\'REVIEW\':schemeStatus(c.roadFaces>=2),\'\'));\n\n  let zst=\'REVIEW\',zn=\'준주거지역이 기본\';\n  if(c.zoning){\n    if(c.zoning===\'준주거\')zst=\'PASS\';\n    else if([\'제2종일반주거(7층)\',\'제2종일반주거\',\'제3종일반주거\'].includes(c.zoning)){zst=\'REVIEW\';zn=\'기준 충족 시 준주거지역 변경 전제\';}\n    else zst=\'FAIL\';\n  }\n  rows.push(schemeRow(\'용도지역\',\'준주거 / 2·3종 일반주거는 준주거 변경 검토\',c.zoning||\'-\',zst,zn));\n  rows.push(schemeRow(\'노후도\',\'노후도 요건 충족 필요\',`${c.age20.count}/${c.age20.total}동 20년+ (${fmtSchemePct(c.age20.ratio)})`,\n    \'INFO\',\'2021 공개기준은 존재하나 2026 사업제안 전 최신 지구단위계획 기준 재확인\',false));\n  rows.push(schemeRow(\'제외지역\',\'역사도심 특성관리·구릉지·정비(예정)구역 등 제외\',\n    c.excluded===null?\'-\':c.excluded?\'해당\':\'비해당\',c.excluded===null?\'REVIEW\':schemeStatus(!c.excluded),\'2021 공개기준 기반\'));\n  const den=stationComplexDensity(c);\n  rows.push(schemeRow(\'제도적 용적률\',den.zone,den.far,\'INFO\',\'중심지·간선변·진출입도로별 차등\',false));\n  rows.push(schemeRow(\'공공기여 기준\',\'완화용적률의 1/2\',den.contribution,\'INFO\',\'공공임대 70% 이상 / 공공시설 30% 이하\',false));\n  return rows;\n}\n\nfunction checkLongterm(c){\n  const rows=[];\n  const loc=schemeVal(\'longterm_location_type\');\n  const route=schemeVal(\'longterm_route\');\n\n  if(loc===\'station\'){\n    rows.push(schemeRow(\'입지\',\'승강장 경계 500m 이내\',c.dist==null?\'-\':`${Number(c.dist).toFixed(1)}m${c.stationAuto&&c.stationName?\' · \'+c.stationName:\'\'}`,\n      c.dist==null?\'REVIEW\':schemeStatus(c.dist<=500),\'1차 350m 이내 / 2차 350~500m\'));\n    const longtermCoverage=c.stationAuto?c.coverage500:c.coverage;\n    rows.push(schemeRow(\'대상지 포함\',\'대상지 전체가 해당 역세권 범위 안\',longtermCoverage==null?\'-\':`${Number(longtermCoverage).toFixed(1)}%`,\n      longtermCoverage==null?\'REVIEW\':schemeStatus(longtermCoverage>=99.999),\'경계 전체 포함 원칙\'+(c.stationAuto?\' · 지하철역사 GIS AUTO\':\'\')));\n  }else{\n    const st=(c.has20===null||c.road20Perimeter==null)?\'REVIEW\':schemeStatus(c.has20 && c.road20Perimeter>=12.5);\n    rows.push(schemeRow(\'간선도로 교차지\',\'20m 이상 간선도로 교차지 + 구역둘레 1/8 이상 20m 도로 접면\',\n      `20m ${c.has20===null?\'-\':c.has20?\'접함\':\'미접함\'} / 둘레 ${c.road20Perimeter==null?\'-\':c.road20Perimeter+\'%\'}`,st,\'2026 기준 신규 확대\'));\n  }\n\n  let ast=\'REVIEW\',anote=\'3,000~20,000㎡\';\n  if(c.area!=null){\n    const min=c.specialPlanning?2400:3000;\n    if(c.area<min)ast=\'FAIL\';\n    else if(c.area<=20000)ast=\'PASS\';\n    else if(c.area<=30000){ast=\'REVIEW\';anote=\'20,000㎡ 초과~30,000㎡: 관련 위원회 인정 필요\';}\n    else ast=\'FAIL\';\n  }\n  rows.push(schemeRow(\'면적\',c.specialPlanning?\'2,400㎡ 이상(특별계획구역) ~20,000㎡ / 30,000㎡ 위원회 인정\':\'3,000~20,000㎡ / 30,000㎡ 위원회 인정\',fmtSchemeArea(c.area),ast,anote));\n\n  let zst=\'REVIEW\',zn=\'\';\n  if(c.zoning){\n    if([\'제2종일반주거(7층)\',\'제2종일반주거\',\'제3종일반주거\',\'준주거\'].includes(c.zoning))zst=\'PASS\';\n    else if(c.zoning===\'준공업\'){\n      if(c.factory==null){zst=\'REVIEW\';zn=\'준공업은 공장비율 입력 필요\';}\n      else {zst=schemeStatus(c.factory<10);zn=\'준공업 공장비율 10% 미만\';}\n    }else zst=\'FAIL\';\n  }\n  rows.push(schemeRow(\'용도지역\',\'2종(7층 포함)·3종·준주거 / 준공업은 공장비율 10% 미만\',c.zoning||\'-\',zst,zn));\n\n  rows.push(schemeRow(\'계획 세대수\',\'100세대 이상\',c.units==null?\'-\':`${c.units}세대`,\n    c.units==null?\'REVIEW\':schemeStatus(c.units>=100),\'\'));\n\n  const a=route===\'district\'?c.age20:c.age30;\n  const threshold=route===\'district\'?50:60;\n  rows.push(schemeRow(\'노후도\',route===\'district\'?\'20년 이상 50% 이상\':\'30년 이상 60% 이상\',\n    `${a.count}/${a.total}동 (${fmtSchemePct(a.ratio)})`,\n    a.ratio==null?\'REVIEW\':schemeStatus(a.ratio>=threshold),`사용승인일 확인 ${a.known}/${a.total}동`));\n\n  if(route===\'district\'){\n    rows.push(schemeRow(\'사전검토 동의\',\'토지면적 50% 이상\',c.landConsent==null?\'-\':`${c.landConsent}%`,\n      c.landConsent==null?\'REVIEW\':schemeStatus(c.landConsent>=50),\'지구단위계획\'));\n  }else{\n    const vals=[c.ownerConsent,c.landConsent,c.road20OwnerConsent,c.road20LandConsent];\n    const st=vals.some(v=>v==null)?\'REVIEW\':schemeStatus(c.ownerConsent>=40 && c.landConsent>=40 && c.road20OwnerConsent>=50 && c.road20LandConsent>=50);\n    rows.push(schemeRow(\'사전검토 동의\',\'소유자 40% + 토지 40% + 20m 도로접면 필지 소유자·면적 각 50%\',\n      `소유 ${c.ownerConsent??\'-\'} / 면적 ${c.landConsent??\'-\'} / 도로소유 ${c.road20OwnerConsent??\'-\'} / 도로면적 ${c.road20LandConsent??\'-\'}%`,st,\'정비계획\'));\n  }\n  const den=longtermDensity(c);\n  rows.push(schemeRow(\'제도적 용적률\',den.zone,den.far,\'INFO\',\'1차·2차 역세권, 지구단위/정비계획별 체계\',false));\n  rows.push(schemeRow(\'공공기여 기준\',\'완화용적률의 1/2 이상\',den.contribution,\'INFO\',\'장기전세주택 우선\',false));\n  return rows;\n}\n\nfunction checkPublicComplex(c){\n  const rows=[];\n  const typ=schemeVal(\'public_complex_type\');\n  if(typ===\'commercial\'){\n    rows.push(schemeRow(\'입지\',\'역세권 등 접근성 양호·개발 저조지역 / 승강장 500m 범위\',c.dist==null?\'-\':`${Number(c.dist).toFixed(1)}m${c.stationAuto&&c.stationName?\' · \'+c.stationName:\'\'}`,\n      c.dist==null?\'REVIEW\':schemeStatus(c.dist<=500),\'주거상업고밀지구\'));\n    const publicCoverage=c.stationAuto?c.coverage500:c.coverage;\n    rows.push(schemeRow(\'역세권 포함\',\'복합사업 면적 과반이 지정범위 안이면 전체 포함 가능\',publicCoverage==null?\'-\':`${Number(publicCoverage).toFixed(1)}%`,\n      publicCoverage==null?\'REVIEW\':schemeStatus(publicCoverage>50),c.stationAuto?\'지하철역사 GIS 500m AUTO\':\'\'));\n    rows.push(schemeRow(\'저개발성\',\'개발이 이루어지지 않거나 저조한 지역\',c.underdeveloped===null?\'-\':c.underdeveloped?\'해당\':\'비해당\',\n      c.underdeveloped===null?\'REVIEW\':schemeStatus(c.underdeveloped),\'\'));\n    rows.push(schemeRow(\'면적\',\'5,000㎡ 이상\',fmtSchemeArea(c.area),c.area==null?\'REVIEW\':schemeStatus(c.area>=5000),\'\'));\n  }else if(typ===\'industrial\'){\n    rows.push(schemeRow(\'용도지역\',\'준공업지역\',c.zoning||\'-\',c.zoning?schemeStatus(c.zoning===\'준공업\'):\'REVIEW\',\'주거산업융합지구\'));\n    rows.push(schemeRow(\'면적\',\'5,000㎡ 이상\',fmtSchemeArea(c.area),c.area==null?\'REVIEW\':schemeStatus(c.area>=5000),\'\'));\n    rows.push(schemeRow(\'정비 필요\',\'공장·산업시설 낙후 또는 주거지 인근 정비 필요\',c.underdeveloped===null?\'-\':c.underdeveloped?\'해당\':\'비해당\',\n      c.underdeveloped===null?\'REVIEW\':schemeStatus(c.underdeveloped),\'\'));\n  }else{\n    rows.push(schemeRow(\'저층·기반시설\',\'20년 이상 저층 노후주거지 비율 높고 기반시설 열악\',c.lowriseBad?\'해당\':\'미체크\',\n      c.lowriseBad?\'PASS\':\'REVIEW\',\'주택공급활성화지구\'));\n    rows.push(schemeRow(\'면적\',\'10,000㎡ 이상\',fmtSchemeArea(c.area),c.area==null?\'REVIEW\':schemeStatus(c.area>=10000),\'\'));\n  }\n\n  rows.push(schemeRow(\'노후도\',\'20년 이상 노후건축물 40% 이상(시행령 최소기준)\',`${c.age20.count}/${c.age20.total}동 (${fmtSchemePct(c.age20.ratio)})`,\n    c.age20.ratio==null?\'REVIEW\':schemeStatus(c.age20.ratio>=40),`사용승인일 확인 ${c.age20.known}/${c.age20.total}동`));\n  rows.push(schemeRow(\'기타 지정기준\',\'용도지역·호수밀도 등 국토교통부 고시 세부요건\', \'후속확인\',\'INFO\',\'공공주택 업무처리지침·지정권자 검토사항\',false));\n  rows.push(schemeRow(\'사업 성격\',\'공공주택사업자 중심 공공주도 복합사업\',\'공공사업 후보검토\',\'INFO\',\'민간 단독 사업방식과 구분\',false));\n  const den=publicComplexDensity(c);\n  rows.push(schemeRow(\'제도적 용적률\',\'복합지구계획 규제특례\',den.far,\'INFO\',\'공공주택특별법 체계\',false));\n  rows.push(schemeRow(\'공공기여·공공성\',\'공공주택 의무공급\',den.contribution,\'INFO\',\'일반 민간사업의 기부채납률과 다른 구조\',false));\n  return rows;\n}\n\nfunction checkInnovation(c){\n  const rows=[];\n  const typ=schemeVal(\'innovation_type\');\n  if(typ===\'growth\'){\n    const isCore=[\'downtown\',\'metro\'].includes(c.center);\n    const transitNode=(c.lines!=null && c.lines>=2 && c.dist!=null && c.dist<=500);\n    let locStatus=\'REVIEW\',locValue=\'-\';\n    if(c.center && c.has20!==null && c.lines!=null && c.dist!=null){\n      const ok=(isCore && c.has20===true) || (!isCore && transitNode);\n      locStatus=schemeStatus(ok);\n      locValue=isCore?`중심지역 / 20m간선 ${c.has20?\'접함\':\'미접함\'}`:`${c.lines>=2?\'환승\':\'단일\'} / ${c.dist}m`;\n    }\n    rows.push(schemeRow(\'입지\',\'도심·광역중심 + 20m 간선도로 접면 OR 비중심 2개 노선 결절지 500m\',locValue,locStatus,\'서울 조례 성장거점형\'));\n    rows.push(schemeRow(\'면적\',\'5,000㎡ 이상\',fmtSchemeArea(c.area),c.area==null?\'REVIEW\':schemeStatus(c.area>=5000),\'\'));\n\n    const aptSt=(c.aptMaxArea==null||c.aptMaxRatio==null)?\'REVIEW\':schemeStatus(c.aptMaxArea<=10000 && c.aptMaxRatio<=30);\n    rows.push(schemeRow(\'공동주택단지\',\'각 단지 10,000㎡ 이하 + 사업면적의 30% 이하\',\n      `최대 ${fmtSchemeArea(c.aptMaxArea)} / ${c.aptMaxRatio==null?\'-\':c.aptMaxRatio+\'%\'}`,aptSt,\'서울 조례\'));\n\n    const allowed=[\'제2종일반주거(7층)\',\'제2종일반주거\',\'제3종일반주거\',\'준주거\',\'중심상업\',\'근린상업\',\'일반상업\'];\n    rows.push(schemeRow(\'용도지역\',\'2종·3종·준주거·중심/근린/일반상업\',c.zoning||\'-\',\n      c.zoning?schemeStatus(allowed.includes(c.zoning)):\'REVIEW\',\'서울 시행규칙\'));\n\n    const roadSt=(c.roadFaces==null||c.has20===null||c.has8===null)?\'REVIEW\':schemeStatus(c.roadFaces>=2 && c.has20 && c.has8);\n    rows.push(schemeRow(\'접도\',\'2면 이상 + 한 면 20m 간선도로 + 다른 한 면 8m 이상\',\n      `${c.roadFaces??\'-\'}면 / 20m ${c.has20===null?\'-\':c.has20?\'O\':\'X\'} / 8m ${c.has8===null?\'-\':c.has8?\'O\':\'X\'}`,roadSt,\'서울 시행규칙\'));\n  }else{\n    let locSt=\'REVIEW\',locNote=\'승강장 350m 역세권 / 350~500m 시장 인정 가능\';\n    const innovationCoverage=c.stationAuto ? ((c.dist!=null&&c.dist<=350)?c.coverage350:c.coverage500) : c.coverage;\n    if(c.dist!=null && innovationCoverage!=null){\n      if(c.zoning===\'준공업\'){\n        if(c.factory==null)locSt=\'REVIEW\';\n        else if(c.dist<=350)locSt=schemeStatus(innovationCoverage>=99.999 && c.factory<10);\n        else if(c.dist<=500 && innovationCoverage>=99.999 && c.factory<10){locSt=\'REVIEW\';locNote=\'350~500m 시장 인정 필요\';}\n        else locSt=\'FAIL\';\n      }else{\n        if(c.dist<=350)locSt=schemeStatus(innovationCoverage>50);\n        else if(c.dist<=500 && innovationCoverage>50){locSt=\'REVIEW\';locNote=\'350~500m 시장 인정 필요\';}\n        else locSt=\'FAIL\';\n      }\n    }\n    rows.push(schemeRow(\'입지\',\'사업면적 과반 역세권 / 준공업은 전부 역세권+공장비율 10% 미만\',\n      `${c.dist==null?\'-\':Number(c.dist).toFixed(1)+\'m\'} / 포함 ${innovationCoverage==null?\'-\':Number(innovationCoverage).toFixed(1)+\'%\'}${c.zoning===\'준공업\'?` / 공장 ${c.factory==null?\'-\':c.factory+\'%\'}`:\'\'}`,locSt,locNote+(c.stationAuto?\' · 지하철역사 GIS AUTO\':\'\')));\n\n    rows.push(schemeRow(\'노후도\',\'20년 이상 60% 이상\',`${c.age20.count}/${c.age20.total}동 (${fmtSchemePct(c.age20.ratio)})`,\n      c.age20.ratio==null?\'REVIEW\':schemeStatus(c.age20.ratio>=60),`사용승인일 확인 ${c.age20.known}/${c.age20.total}동`));\n    rows.push(schemeRow(\'면적\',\'20,000~60,000㎡\',fmtSchemeArea(c.area),\n      c.area==null?\'REVIEW\':schemeStatus(c.area>=20000 && c.area<=60000),\'서울 조례\'));\n\n    const aptSt=(c.aptMaxArea==null||c.aptMaxRatio==null)?\'REVIEW\':schemeStatus(c.aptMaxArea<=10000 && c.aptMaxRatio<=30);\n    rows.push(schemeRow(\'공동주택단지\',\'각 단지 10,000㎡ 이하 + 사업면적의 30% 이하\',\n      `최대 ${fmtSchemeArea(c.aptMaxArea)} / ${c.aptMaxRatio==null?\'-\':c.aptMaxRatio+\'%\'}`,aptSt,\'서울 조례\'));\n\n    const allowed=[\'제2종일반주거(7층)\',\'제2종일반주거\',\'제3종일반주거\',\'준주거\',\'준공업\'];\n    rows.push(schemeRow(\'용도지역\',\'2종(7층 포함)·3종·준주거·준공업\',c.zoning||\'-\',\n      c.zoning?schemeStatus(allowed.includes(c.zoning)):\'REVIEW\',\'서울 시행규칙\'));\n\n    let requiredWidth=null;\n    if(c.area!=null){\n      if(c.area>=20000 && c.area<=30000)requiredWidth=15;\n      else if(c.area>30000 && c.area<=60000)requiredWidth=20;\n    }\n    const roadSt=(requiredWidth==null||c.maxRoad==null||c.enclosed6===null)?\'REVIEW\':schemeStatus(c.maxRoad>=requiredWidth && c.enclosed6);\n    rows.push(schemeRow(\'도로\',requiredWidth?`${requiredWidth}m 이상 간선도로 접면 + 6m 이상 도로로 둘러싸인 1개 블록`:\'면적별 15m/20m 간선도로 + 6m 이상 도로 블록\',\n      `최대 ${c.maxRoad==null?\'-\':c.maxRoad+\'m\'} / 6m블록 ${c.enclosed6===null?\'-\':c.enclosed6?\'O\':\'X\'}`,roadSt,\'서울 시행규칙\'));\n\n    rows.push(schemeRow(\'주택 비율\',\'사업 후 전체 건축물 연면적의 주택 50% 이상\',c.plannedHousingFloor==null?\'-\':`${c.plannedHousingFloor}%`,\n      c.plannedHousingFloor==null?\'REVIEW\':schemeStatus(c.plannedHousingFloor>=50),\'도심복합개발법 시행령\'));\n  }\n\n  const consentSt=(c.ownerConsent==null||c.landConsent==null)?\'REVIEW\':schemeStatus(c.ownerConsent>=66.6667 && c.landConsent>=50);\n  rows.push(schemeRow(\'지정신청 동의\',\'토지등소유자 2/3 이상 + 토지면적 1/2 이상\',\n    `소유자 ${c.ownerConsent==null?\'-\':c.ownerConsent+\'%\'} / 면적 ${c.landConsent==null?\'-\':c.landConsent+\'%\'}`,consentSt,\'도심복합개발법 제8조\'));\n\n  rows.push(schemeRow(\'제외지역\',\'서울시 운영기준상 제외지역 여부\',c.excluded===null?\'-\':c.excluded?\'해당\':\'비해당\',\n    c.excluded===null?\'REVIEW\':schemeStatus(!c.excluded),\'세부 제외지역은 운영기준 확인\'));\n  const den=innovationDensity(c);\n  rows.push(schemeRow(\'제도적 용적률\',\'도심복합개발 규제특례\',den.far,\'INFO\',\'법 제37조·시행령 제27조\',false));\n  rows.push(schemeRow(\'공공기여 기준\',\'규제특례 적용 시 공공기여\',den.contribution,\'INFO\',\'기반시설 우선·일부 현금납부 가능\',false));\n  return rows;\n}\n\nfunction calculateScheme(name,c){\n  if(name===\'redevelopment\')return checkRedevelopment(c);\n  if(name===\'reconstruction\')return checkReconstruction(c);\n  if(name===\'activation\')return checkActivation(c);\n  if(name===\'safe\')return checkSafe(c);\n  if(name===\'station_complex\')return checkStationComplex(c);\n  if(name===\'longterm\')return checkLongterm(c);\n  if(name===\'public_complex\')return checkPublicComplex(c);\n  if(name===\'innovation\')return checkInnovation(c);\n  return [];\n}\nfunction updateSchemeAutoStrip(c){\n  const totalParcels=Number(document.getElementById(\'total_parcel_count\')?.value)||selectedParcelPnus.size||0;\n  const small=Number(document.getElementById(\'small_parcel_count\')?.value)||0;\n  document.getElementById(\'schemeAutoArea\').textContent=fmtSchemeArea(c.area);\n  document.getElementById(\'schemeAutoBuildings\').textContent=c.age20.total?`${c.age20.total}동`:\'-\';\n  document.getElementById(\'schemeAutoAge20\').textContent=c.age20.total?`${c.age20.count}/${c.age20.total} (${fmtSchemePct(c.age20.ratio)})`:\'-\';\n  document.getElementById(\'schemeAutoAge30\').textContent=c.age30.total?`${c.age30.count}/${c.age30.total} (${fmtSchemePct(c.age30.ratio)})`:\'-\';\n  document.getElementById(\'schemeAutoSmall\').textContent=totalParcels?`${small}/${totalParcels}필지`:\'-\';\n}\n\nfunction schemeResultCounts(rows){\n  const mandatory=rows.filter(r=>r.required!==false);\n  return {\n    pass:mandatory.filter(r=>r.status===\'PASS\').length,\n    fail:mandatory.filter(r=>r.status===\'FAIL\').length,\n    review:mandatory.filter(r=>r.status===\'REVIEW\').length\n  };\n}\nfunction renderSchemeComparison(){\n  const body=document.getElementById(\'schemeCompareBody\');\n  if(!body)return;\n  body.innerHTML=Object.keys(schemeNames).map(name=>{\n    const res=schemeResults[name]||{rows:[],overall:\'REVIEW\',density:{zone:\'-\',far:\'-\',contribution:\'-\'}};\n    const den=res.density||{zone:\'-\',far:\'-\',contribution:\'-\'};\n    const cnt=schemeResultCounts(res.rows||[]);\n    return `<tr>\n      <td>${escHtml(schemeNames[name])}</td>\n      <td><span class="scheme-compare-status ${res.overall}">${overallLabel(res.overall)}</span></td>\n      <td>충족 ${cnt.pass} · 미달 ${cnt.fail} · 확인 ${cnt.review}</td>\n      <td>${escHtml(den.zone||\'-\')}</td>\n      <td>${escHtml(den.far||\'-\')}</td>\n      <td>${escHtml(den.contribution||\'-\')}</td>\n    </tr>`;\n  }).join(\'\');\n}\nfunction renderAllSchemeSheets(){\n  const root=document.getElementById(\'allSchemeSheets\');\n  if(!root)return;\n  root.innerHTML=Object.keys(schemeNames).map(name=>{\n    const res=schemeResults[name]||{rows:[],overall:\'REVIEW\',density:{zone:\'-\',far:\'-\',contribution:\'-\'}};\n    const den=res.density||{zone:\'-\',far:\'-\',contribution:\'-\'};\n    const rows=(res.rows||[]).map(r=>`<tr>\n      <td>${escHtml(r.item)}</td>\n      <td>${escHtml(r.rule)}</td>\n      <td>${escHtml(r.value)}</td>\n      <td><span class="scheme-tag ${r.status}">${r.status===\'PASS\'?\'충족\':r.status===\'FAIL\'?\'미달\':r.status===\'REVIEW\'?\'확인\':\'정보\'}</span></td>\n      <td>${escHtml(r.note||\'\')}</td>\n    </tr>`).join(\'\');\n    return `<section class="all-scheme-sheet" id="schemeSheet_${name}">\n      <div class="all-scheme-sheet-head">\n        <div>\n          <h3>${escHtml(schemeNames[name])}</h3>\n          <div class="basis">${escHtml(schemeBasis[name])}</div>\n        </div>\n        <span class="scheme-overall ${res.overall}">${overallLabel(res.overall)}</span>\n      </div>\n      <div class="all-scheme-density">\n        <div><span>용도지역 상향</span><b>${escHtml(den.zone||\'-\')}</b></div>\n        <div><span>제도상 용적률</span><b>${escHtml(den.far||\'-\')}</b></div>\n        <div><span>공공기여·기부채납</span><b>${escHtml(den.contribution||\'-\')}</b></div>\n      </div>\n      <div class="all-scheme-table-wrap">\n        <table class="all-scheme-table">\n          <thead><tr><th>검토항목</th><th>기준</th><th>대상지 값</th><th>판정</th><th>출처·비고</th></tr></thead>\n          <tbody>${rows}</tbody>\n        </table>\n      </div>\n    </section>`;\n  }).join(\'\');\n}\nfunction renderSchemeSheet(){\n  renderSchemeComparison();\n  renderAllSchemeSheets();\n}\nfunction setActiveScheme(name){\n  activeScheme=name;\n  const el=document.getElementById(`schemeSheet_${name}`);\n  if(el)el.scrollIntoView({behavior:\'smooth\',block:\'start\'});\n}\nfunction runAllSchemeChecks(){\n  const panel=document.querySelector(\'.scheme-panel\');\n  if(!panel)return;\n  const c=commonSchemeData();\n  updateSchemeAutoStrip(c);\n  for(const name of Object.keys(schemeNames)){\n    const rows=calculateScheme(name,c);\n    const overall=overallScheme(rows);\n    const density=densityForScheme(name,c);\n    schemeResults[name]={rows,overall,density};\n  }\n  renderSchemeComparison();\n  renderAllSchemeSheets();\n  updateCompactInfoRail();\n  updateCandidateSchemes();\n}\n\n\n// ---------- CompactCity first-screen strategy filter ----------\nconst ccCandidateState={};\nconst CC_PURPOSE={\n  redevelopment:[\'housing\',\'mixed\'],\n  reconstruction:[\'housing\',\'mixed\'],\n  activation:[\'housing\',\'commercial\',\'office\',\'lodging\',\'mixed\'],\n  station_complex:[\'housing\',\'commercial\',\'office\',\'mixed\'],\n  safe:[\'housing\',\'mixed\'],\n  longterm:[\'housing\',\'mixed\'],\n  public_complex:[\'housing\',\'commercial\',\'office\',\'mixed\'],\n  innovation:[\'housing\',\'commercial\',\'office\',\'lodging\',\'mixed\']\n};\nconst CC_LAND={\n  secured:[\'activation\',\'station_complex\',\'safe\',\'longterm\',\'innovation\'],\n  partial:[\'redevelopment\',\'reconstruction\',\'activation\',\'station_complex\',\'safe\',\'longterm\',\'innovation\'],\n  unsecured:[\'redevelopment\',\'reconstruction\',\'public_complex\',\'innovation\'],\n  owners:[\'redevelopment\',\'reconstruction\',\'activation\',\'longterm\',\'station_complex\']\n};\nconst CC_ACTOR={\n  private_owner:[\'redevelopment\',\'reconstruction\',\'activation\',\'station_complex\',\'safe\',\'longterm\'],\n  private_developer:[\'activation\',\'station_complex\',\'safe\',\'longterm\',\'innovation\'],\n  owners_association:[\'redevelopment\',\'reconstruction\',\'activation\',\'longterm\'],\n  public:[\'public_complex\',\'innovation\',\'safe\'],\n  public_private:[\'public_complex\',\'innovation\',\'activation\',\'safe\',\'longterm\']\n};\nfunction ccLabelOf(id){\n  const el=document.getElementById(id);\n  return el?.selectedOptions?.[0]?.textContent||\'\';\n}\nfunction ccContextFor(name){\n  const purpose=document.getElementById(\'ccPurpose\')?.value||\'\';\n  const land=document.getElementById(\'ccLandControl\')?.value||\'\';\n  const actor=document.getElementById(\'ccActor\')?.value||\'\';\n  const ready=!!(purpose&&land&&actor);\n  let score=0;\n  const reasons=[];\n  if(purpose){\n    if((CC_PURPOSE[name]||[]).includes(purpose)){score++;reasons.push(`목적사업(${ccLabelOf(\'ccPurpose\')}) 적합`);}\n    else reasons.push(`목적사업(${ccLabelOf(\'ccPurpose\')}) 비우선`);\n  }\n  if(land){\n    if((CC_LAND[land]||[]).includes(name)){score++;reasons.push(`토지확보 상태와 추진방식 적합`);}\n    else reasons.push(`토지확보 상태상 우선도 낮음`);\n  }\n  if(actor){\n    if((CC_ACTOR[actor]||[]).includes(name)){score++;reasons.push(`의뢰·추진주체 적합`);}\n    else reasons.push(`추진주체상 별도 구조 필요`);\n  }\n  const regulatory=schemeResults[name]?.overall||\'REVIEW\';\n  let state=\'neutral\',label=\'입력필요\';\n  if(ready){\n    if(regulatory===\'FAIL\'){state=\'off\';label=\'기준미달\';}\n    else if(score>=2){state=\'on\';label=regulatory===\'PASS\'?\'검토가능\':\'우선검토\';}\n    else if(score===1){state=\'mid\';label=\'조건확인\';}\n    else {state=\'off\';label=\'비우선\';}\n  }\n  return {purpose,land,actor,ready,score,reasons,regulatory,state,label};\n}\nfunction updateCandidateSchemes(){\n  const root=document.getElementById(\'ccSchemeIcons\');\n  if(!root)return;\n  let on=0,mid=0;\n  for(const name of Object.keys(schemeNames)){\n    const st=ccContextFor(name);\n    ccCandidateState[name]=st;\n    const btn=root.querySelector(`[data-scheme="${name}"]`);\n    if(!btn)continue;\n    btn.classList.remove(\'candidate-on\',\'candidate-mid\',\'candidate-off\');\n    if(st.state===\'on\'){btn.classList.add(\'candidate-on\');on++;}\n    else if(st.state===\'mid\'){btn.classList.add(\'candidate-mid\');mid++;}\n    else if(st.state===\'off\')btn.classList.add(\'candidate-off\');\n    const em=btn.querySelector(\'em\');if(em)em.textContent=st.label;\n  }\n  const sum=document.getElementById(\'ccCandidateSummary\');\n  const purpose=document.getElementById(\'ccPurpose\')?.value;\n  const land=document.getElementById(\'ccLandControl\')?.value;\n  const actor=document.getElementById(\'ccActor\')?.value;\n  if(sum){\n    if(!(purpose&&land&&actor))sum.textContent=\'목적사업 · 토지확보 · 의뢰주체를 선택하세요.\';\n    else sum.textContent=`우선검토 ${on}개 · 조건확인 ${mid}개 · 지도/공공데이터 요건과 함께 재판정`;\n  }\n}\nfunction showCandidateBasis(name){\n  document.querySelectorAll(\'#ccSchemeIcons button\').forEach(b=>b.classList.toggle(\'candidate-selected\',b.dataset.scheme===name));\n  const box=document.getElementById(\'ccSchemeBasis\');if(!box)return;\n  const st=ccCandidateState[name]||ccContextFor(name);\n  const res=schemeResults[name]||{overall:\'REVIEW\',density:{zone:\'-\',far:\'-\',contribution:\'-\'}};\n  const den=res.density||{zone:\'-\',far:\'-\',contribution:\'-\'};\n  const reg=overallLabel(res.overall||\'REVIEW\');\n  const reasons=st.reasons.length?st.reasons.join(\' · \'):\'상단의 목적사업·토지확보·의뢰주체를 먼저 선택\';\n  box.innerHTML=`<b>${escHtml(schemeNames[name])}</b>\n    <span><strong>${escHtml(st.label)}</strong> · ${escHtml(reasons)}<br>\n    근거: ${escHtml(schemeBasis[name]||\'-\')} · 현재 제도판정 ${escHtml(reg)} · 종상향 ${escHtml(den.zone||\'-\')} · 용적률 ${escHtml(den.far||\'-\')} · 공공기여 ${escHtml(den.contribution||\'-\')}</span>\n    <span class="cc-basis-actions"><button onclick="scrollToSchemeDetail(\'${name}\')">상세 검토표 ↓</button></span>`;\n}\nfunction scrollToSchemeDetail(name){\n  const el=document.getElementById(`schemeSheet_${name}`);\n  if(el)el.scrollIntoView({behavior:\'smooth\',block:\'start\'});\n}\nfunction ccTopEntries(mp,limit=5){\n  return [...mp.entries()].sort((a,b)=>b[1]-a[1]).slice(0,limit);\n}\nfunction updateCompactInfoRail(){\n  const area=Number(document.getElementById(\'area_m2\')?.value)||0;\n  const parcels=[...selectedParcelPnus].map(p=>parcelFeatureMap.get(String(p))).filter(Boolean);\n  const small=parcels.filter(f=>(f.properties||{}).is_small===true).length;\n  const official=parcels.filter(f=>(f.properties||{}).official_area_m2!=null).length;\n  const landCats=new Map();\n  for(const f of parcels){\n    const p=f.properties||{};\n    const cat=String((p.land_ledger||{}).lndcgrCodeNm||\'미확인\').trim()||\'미확인\';\n    landCats.set(cat,(landCats.get(cat)||0)+1);\n  }\n  const set=(id,v)=>{const el=document.getElementById(id);if(el)el.textContent=v;};\n  set(\'ccLandArea\',area?`${fmt(area,0)}㎡`:\'-\');\n  set(\'ccLandParcels\',parcels.length?`${parcels.length}필지`:\'-\');\n  set(\'ccLandSmall\',parcels.length?`${small}필지 (${(small/parcels.length*100).toFixed(1)}%)`:\'-\');\n  set(\'ccLandOfficial\',parcels.length?`${official}/${parcels.length}필지`:\'-\');\n  set(\'ccLandState\',parcels.length?\'필지원장 연결\':\'구역 설정 전\');\n  const landLegend=document.getElementById(\'ccLandLegend\');\n  if(landLegend){\n    const entries=ccTopEntries(landCats);\n    landLegend.innerHTML=entries.length?entries.map(([k,v])=>`<span>${escHtml(k)} <b>${v}</b></span>`).join(\'\'):\'<span>토지대장 조회 후 표시</span>\';\n  }\n\n  const records=selectedHubRecordsForSchemes();\n  const old=records.filter(r=>r.age_status===\'OLD\').length;\n  const floorArea=records.reduce((s,r)=>s+(Number(r.totArea)||0),0);\n  const purposes=new Map();\n  for(const r of records){\n    const p=String(r.mainPurpsCdNm||\'미확인\').trim()||\'미확인\';\n    purposes.set(p,(purposes.get(p)||0)+1);\n  }\n  set(\'ccBuildingTotal\',records.length?`${records.length}동`:(currentBuildingFeatures.length?`${currentBuildingFeatures.length}동(1차)`:\'-\'));\n  set(\'ccBuildingOld\',records.length?`${old}동`:\'-\');\n  set(\'ccBuildingOldRatio\',records.length?`${(old/records.length*100).toFixed(1)}%`:\'-\');\n  set(\'ccBuildingFloorArea\',floorArea?`${fmt(floorArea,0)}㎡`:\'-\');\n  set(\'ccBuildingState\',records.length?\'건축HUB 연결\':(currentBuildingFeatures.length?\'공간건물 1차\':\'구역 설정 전\'));\n  const bLegend=document.getElementById(\'ccBuildingLegend\');\n  if(bLegend){\n    const entries=ccTopEntries(purposes);\n    bLegend.innerHTML=entries.length?entries.map(([k,v])=>`<span>${escHtml(k)} <b>${v}</b></span>`).join(\'\'):\'<span>건축HUB 조회 후 표시</span>\';\n  }\n}\nfunction updateCompactMapRole(){\n  const role=document.getElementById(\'ccMapRole\');\n  const note=document.getElementById(\'ccMapInstruction\');\n  if(activeGeometry){\n    if(role)role.textContent=\'위치도 · 대상구역 설정\';\n    if(note)note.textContent=\'대상구역이 설정되었습니다. 지도는 위치도로 유지되며 필지·건축물·역세권 분석 결과가 겹쳐 표시됩니다.\';\n  }else{\n    if(role)role.textContent=\'사업지 범위를 그리시오\';\n    if(note)note.textContent=\'우측 상단의 그리기 아이콘으로 사업구역을 설정하세요. 구역 확정 후 이 지도는 위치도로 계속 사용됩니다.\';\n  }\n}\n\nfunction updateRedevelopmentStrategySignal(){\n  const total=Number(analysisState.metrics.total_buildings)||0;\n  const old=Number(analysisState.metrics.old_count)||0;\n  const ratio=total>0 ? old/total*100 : null;\n\n  const totalParcels=Number(document.getElementById(\'total_parcel_count\')?.value)||selectedParcelPnus.size||0;\n  const smallParcels=Number(document.getElementById(\'small_parcel_count\')?.value)||0;\n  const smallRatio=totalParcels>0 ? smallParcels/totalParcels*100 : null;\n\n  const totalEl=document.getElementById(\'strategyTotalBuildings\');\n  if(!totalEl)return;\n\n  document.getElementById(\'strategyOldBuildings\').textContent=total?old+\'동\':\'-\';\n  totalEl.textContent=total?total+\'동\':\'-\';\n  document.getElementById(\'strategyOldRatio\').textContent=ratio==null?\'-\':ratio.toFixed(1)+\'%\';\n  document.getElementById(\'strategySmallCount\').textContent=totalParcels?smallParcels+\'필지\':\'-\';\n  document.getElementById(\'strategySmallRatio\').textContent=smallRatio==null?\'-\':smallRatio.toFixed(1)+\'%\';\n\n  const signal=document.getElementById(\'strategySignal\');\n  const judge=document.getElementById(\'strategyAgeJudgement\');\n  const reason=document.getElementById(\'strategyReason\');\n\n  if(ratio==null){\n    signal.textContent=\'건축HUB 조회 필요\';\n    judge.textContent=\'미분석\';\n    reason.innerHTML=`건축HUB에서 사용승인일·구조·주용도를 불러오면 노후도 기반 초기신호를 표시합니다.${smallRatio!=null?` 현재 90㎡ 미만 필지는 <b>${smallParcels}/${totalParcels}필지 (${smallRatio.toFixed(1)}%)</b>입니다.`:\'\'}`;\n    return;\n  }\n\n  const smallText = smallRatio==null ? \'\' : ` · 90㎡ 미만 ${smallParcels}/${totalParcels}필지 (${smallRatio.toFixed(1)}%)`;\n\n  if(ratio>=60){\n    signal.textContent=\'재개발 검토 우선\';\n    judge.textContent=\'노후도 충족\';\n    reason.innerHTML=`노후건축물 <b>${old}/${total}동 (${ratio.toFixed(1)}%)</b>${smallText}. 노후도 측면에서는 재개발을 우선 사업방식으로 검토할 수 있으며, 과소필지 비율은 대상지의 물리적 정비여건을 보는 보조지표로 함께 봅니다.`;\n  }else{\n    signal.textContent=\'다른 사업방식 비교\';\n    judge.textContent=\'노후도 미충족\';\n    reason.innerHTML=`노후건축물 <b>${old}/${total}동 (${ratio.toFixed(1)}%)</b>${smallText}. 재개발 노후도는 낮아 역세권·지구단위·개별개발 등 다른 사업방식과 비교하되, 과소필지 비율은 별도 현황지표로 유지합니다.`;\n  }\n}\n\nfunction estimateHouseDensity(){\n  const rows=[];\n  let equivalent=0;\n  for(const [pnu,recs] of hubRecordsByPnu.entries()){\n    if(!selectedParcelPnus.has(String(pnu)))continue;\n    for(const r of recs){\n      const purpose=String(r.mainPurpsCdNm||\'\');\n      const floors=Math.max(1,Number(r.grndFlrCnt)||1);\n      const hh=Math.max(Number(r.hhldCnt)||0,Number(r.fmlyCnt)||0);\n      const arch=Number(r.archArea)||0;\n      let eq=0,method=\'\';\n\n      if(/공동주택|다가구주택|다세대주택|연립주택|아파트/.test(purpose)){\n        // BuildingHUB title does not give "maximum households on one floor".\n        // For initial screening, ceil(total household/family ÷ ground floors) is used.\n        eq=hh>0?Math.ceil(hh/floors):1;\n        method=hh>0?\'세대·가구수÷지상층수(예비)\':\'주거 1동(자료부족)\';\n      }else if(/단독주택/.test(purpose)){\n        eq=1;\n        method=\'단독주택 1동\';\n      }else{\n        // Ordinance: non-residential building = one building per 90㎡ of building area, decimals discarded.\n        eq=arch>0?Math.floor(arch/90):1;\n        method=arch>0?\'건축면적÷90㎡ 절사\':\'비주거 1동(면적자료부족)\';\n      }\n      equivalent+=Math.max(0,eq);\n      rows.push({r,eq:Math.max(0,eq),method,hh,floors,arch});\n    }\n  }\n\n  // Preliminary effective-area handling: exclude parcels whose cadastral land category\n  // is park or school site. "Preserved/completed" status is not available in the API.\n  let effective=0,excluded=0,areaFallback=0;\n  for(const pnu of selectedParcelPnus){\n    const f=parcelFeatureMap.get(String(pnu)); if(!f)continue;\n    const p=f.properties||{};\n    const area=Number(p._analysis_area_m2??p.official_area_m2??p.geometry_area_m2)||0;\n    const cat=String((p.land_ledger||{}).lndcgrCodeNm||\'\');\n    if(cat===\'공원\'||cat===\'학교용지\'){excluded+=area;continue;}\n    effective+=area;\n    if(p.official_area_m2==null)areaFallback++;\n  }\n  if(!(effective>0)){\n    effective=Number(document.getElementById(\'area_m2\').value)||0;\n  }\n  const ha=effective/10000;\n  const density=ha>0?equivalent/ha:null;\n\n  const tbody=document.getElementById(\'densityTableBody\');\n  tbody.innerHTML=rows.slice(0,600).map(x=>`<tr>\n    <td>${escHtml(x.r.platPlc||x.r.newPlatPlc||\'\')}</td>\n    <td>${escHtml(x.r.mainPurpsCdNm||\'-\')}</td>\n    <td>${x.hh||0}</td>\n    <td>${x.floors}</td>\n    <td>${x.arch?fmt(x.arch,2):\'-\'}</td>\n    <td><b>${x.eq}</b></td>\n    <td>${escHtml(x.method)}</td>\n  </tr>`).join(\'\');\n  document.getElementById(\'densityTableWrap\').style.display=rows.length?\'block\':\'none\';\n\n  const summary=document.getElementById(\'densitySummary\');\n  const badge=document.getElementById(\'densityBadge\');\n  if(density!=null){\n    document.getElementById(\'house_density_per_ha\').value=density.toFixed(2);\n    summary.innerHTML=`환산동수 <b>${equivalent}</b> · 유효면적(예비) <b>${fmt(effective,1)}㎡</b> · 호수밀도 <b>${density.toFixed(1)}동/ha</b>${excluded?` · 공원/학교용지 ${fmt(excluded,1)}㎡ 예비 제외`:\'\'}`;\n    badge.textContent=\'AUTO(초기검토)\';\n    badge.className=\'REVIEW\';\n    analysisState.metrics.house_density=density;\n    analysisState.quality.density=\'ESTIMATE\';\n  }else{\n    document.getElementById(\'house_density_per_ha\').value=\'\';\n    summary.textContent=\'호수밀도 자동산정에 필요한 건축물 또는 면적자료가 없습니다.\';\n    analysisState.quality.density=\'NONE\';\n  }\n  return {equivalent,effective,density,rows};\n}\n\nasync function analyzeBuildingHub(){\n  const pnus=[...selectedParcelPnus].filter(p=>/^\\d{19}$/.test(String(p)));\n  const summary=document.getElementById(\'hubSummary\');\n  const conn=document.getElementById(\'hubConn\');\n  const wrap=document.getElementById(\'hubTableWrap\');\n  const tbody=document.getElementById(\'hubTableBody\');\n\n  if(!pnus.length){\n    summary.textContent=\'선택된 PNU가 없습니다.\';\n    return;\n  }\n\n  conn.textContent=\'건축HUB 조회 중\';\n  conn.className=\'conn planned\';\n  summary.textContent=`선택필지 ${pnus.length}개 건축물대장 조회 중...`;\n  wrap.style.display=\'none\';\n  tbody.innerHTML=\'\';\n\n  const allRecords=[];\n  const errors=[];\n  let done=0;\n  const chunks=chunkArray(pnus,20);\n\n  for(const batch of chunks){\n    try{\n      const r=await fetch(\'/api/building-hub/title-batch\',{\n        method:\'POST\',\n        headers:{\'Content-Type\':\'application/json\'},\n        body:JSON.stringify({pnus:batch})\n      });\n      const d=await r.json();\n      if(!r.ok)throw new Error(d.detail||\'건축HUB 조회 실패\');\n      allRecords.push(...(d.records||[]));\n      errors.push(...(d.errors||[]));\n      done+=batch.length;\n      summary.textContent=`건축HUB 조회 ${done}/${pnus.length}필지 · 표제부 ${allRecords.length}건`;\n    }catch(e){\n      errors.push({error:String(e.message||e),batch_size:batch.length});\n      done+=batch.length;\n    }\n  }\n\n  // dedupe title rows by management PK\n  const recMap=new Map();\n  for(const r of allRecords){\n    const k=String(r.mgmBldrgstPk||`${r.pnu||\'\'}|${r.dongNm||\'\'}|${r.bldNm||\'\'}|${r.useAprDay||\'\'}`);\n    if(!recMap.has(k))recMap.set(k,r);\n  }\n  const records=[...recMap.values()];\n  hubRecordsByPnu.clear();\n  for(const r of records){const p=String(r.pnu||\'\');if(!hubRecordsByPnu.has(p))hubRecordsByPnu.set(p,[]);hubRecordsByPnu.get(p).push(r);}\n  refreshBuildingAgeClassification();\n\n  const knownAge=records.filter(r=>r.age_status===\'OLD\' || r.age_status===\'NOT_OLD\');\n  const old=records.filter(r=>r.age_status===\'OLD\');\n  const notOld=records.filter(r=>r.age_status===\'NOT_OLD\');\n  const unknownAge=records.filter(r=>r.age_status!==\'OLD\' && r.age_status!==\'NOT_OLD\');\n  const totArea=records.reduce((s,r)=>s+(validPositiveNumber(r.totArea)||0),0);\n  const oldArea=old.reduce((s,r)=>s+(validPositiveNumber(r.totArea)||0),0);\n\n  // The title-register count becomes the primary total count; VWorld remains spatial cross-check.\n  document.getElementById(\'total_building_count\').value=records.length;\n  document.getElementById(\'total_floor_area_m2\').value=totArea?totArea.toFixed(2):\'\';\n\n  if(records.length>0){\n    document.getElementById(\'old_building_count\').value=old.length;\n    document.getElementById(\'old_floor_area_m2\').value=oldArea.toFixed(2);\n    analysisState.quality.old=(knownAge.length===records.length)?\'OFFICIAL_AGE\':\'PARTIAL_AGE\';\n  }else{\n    document.getElementById(\'old_building_count\').value=\'\';\n    document.getElementById(\'old_floor_area_m2\').value=\'\';\n    for(const id of [\'hubStatTotal\',\'hubStatOld\',\'hubStatNotOld\',\'hubStatUnknown\',\'hubStatRatio\',\'hubStatFloorRatio\']){\n      const el=document.getElementById(id);if(el)el.textContent=\'-\';\n    }\n    analysisState.quality.old=\'NONE\';\n  }\n\n  const ageRatio=records.length?old.length/records.length*100:0;\n  const floorRatio=totArea?oldArea/totArea*100:0;\n  document.getElementById(\'hubStatTotal\').textContent=records.length+\'동\';\n  document.getElementById(\'hubStatOld\').textContent=old.length+\'동\';\n  document.getElementById(\'hubStatNotOld\').textContent=notOld.length+\'동\';\n  document.getElementById(\'hubStatUnknown\').textContent=unknownAge.length+\'동\';\n  document.getElementById(\'hubStatRatio\').textContent=records.length?ageRatio.toFixed(1)+\'%\':\'-\';\n  document.getElementById(\'hubStatFloorRatio\').textContent=totArea?floorRatio.toFixed(1)+\'%\':\'-\';\n  analysisState.metrics.old_count=old.length;\n  analysisState.metrics.total_buildings=records.length;\n  analysisState.metrics.old_ratio=records.length?old.length/records.length:null;\n  analysisState.metrics.old_floor_ratio=totArea?oldArea/totArea:null;\n  updateRedevelopmentStrategySignal();\n  runAllSchemeChecks();\n  summary.innerHTML=`선택필지 ${pnus.length}개 · 건축물대장 <b>${records.length}동</b> · 노후 <b>${old.length}동 (${ageRatio.toFixed(1)}%)</b> · 비노후 ${notOld.length}동 · 미판정 ${unknownAge.length}동 · 조회오류 ${errors.length}건`;\n\n  tbody.innerHTML=records.slice(0,500).map(r=>{\n    const oldTxt=r.age_status===\'OLD\' ? `노후후보(${r.age_threshold_years}년)` :\n                 r.age_status===\'NOT_OLD\' ? `비노후(${r.age_threshold_years}년)` : \'확인필요\';\n    return `<tr>\n      <td>${escHtml(r.platPlc||r.newPlatPlc||\'\')}</td>\n      <td>${escHtml(r.dongNm||r.bldNm||\'-\')}</td>\n      <td>${escHtml(hubFmtDay(r.useAprDay))}</td>\n      <td>${escHtml(r.strctCdNm||\'-\')}</td>\n      <td>${escHtml(r.mainPurpsCdNm||\'-\')}</td>\n      <td>${escHtml(r.grndFlrCnt??\'-\')}</td>\n      <td>${escHtml(r.totArea??\'-\')}</td>\n      <td>${escHtml(r.platArea??\'-\')}</td>\n      <td>${escHtml(r.hhldCnt??\'-\')}</td>\n      <td>${escHtml(r.fmlyCnt??\'-\')}</td>\n      <td>${oldTxt}</td>\n      <td>${escHtml(hubFmtDay(r.crtnDay))}</td>\n    </tr>`;\n  }).join(\'\');\n  wrap.style.display=records.length?\'block\':\'none\';\n\n  if(records.length){\n    conn.textContent=knownAge.length===records.length?\'건축HUB AUTO\':\'건축HUB AUTO(일부확인)\';\n    conn.className=knownAge.length===records.length?\'conn auto\':\'conn planned\';\n  }else{\n    conn.textContent=\'건축HUB REVIEW\';\n    conn.className=\'conn planned\';\n  }\n\n  document.getElementById(\'result\').innerHTML=\'<div class="empty">건축HUB 값을 분석객체에 반영했습니다. 노후건축물 소재필지와 호수밀도 초기값을 자동분류했습니다.</div>\';\n  refreshBuildingAgeClassification();\n  estimateHouseDensity();\n\n}\n\n\n\nfunction parseLandLedgerXml(xmlText){\n  const doc=new DOMParser().parseFromString(xmlText,\'application/xml\');\n  if(doc.querySelector(\'parsererror\'))throw new Error(\'토지대장 XML 파싱 오류\');\n  const err=doc.querySelector(\'fields > error, error\');\n  if(err && err.textContent.trim())throw new Error(\'VWorld 토지대장 오류: \'+err.textContent.trim());\n  const rows=[...doc.querySelectorAll(\'ladfrlVOList\')];\n  if(!rows.length)return null;\n  // Usually one current ledger row. Prefer the latest lastUpdtDt when more than one exists.\n  const vals=rows.map(row=>{\n    const g=n=>{const x=row.querySelector(n);return x?x.textContent.trim():\'\';};\n    return {\n      pnu:g(\'pnu\'), ldCodeNm:g(\'ldCodeNm\'), mnnmSlno:g(\'mnnmSlno\'),\n      regstrSeCodeNm:g(\'regstrSeCodeNm\'), lndcgrCodeNm:g(\'lndcgrCodeNm\'),\n      lndpclAr:Number(g(\'lndpclAr\'))||null,\n      posesnSeCodeNm:g(\'posesnSeCodeNm\'),\n      cnrsPsnCo:Number(g(\'cnrsPsnCo\'))||0,\n      ladFrtlScNm:g(\'ladFrtlScNm\'),\n      lastUpdtDt:g(\'lastUpdtDt\')\n    };\n  });\n  vals.sort((a,b)=>String(b.lastUpdtDt||\'\').localeCompare(String(a.lastUpdtDt||\'\')));\n  return vals[0];\n}\n\nfunction parseLandLedgerJson(data){\n  if(!data)return null;\n  const fields=data.fields||data.response?.fields||{};\n  const err=fields.error||data.error;\n  if(err)throw new Error(\'VWorld 토지대장 오류: \'+String(err));\n  let rows=fields.ladfrlVOList||data.ladfrlVOList||[];\n  if(!Array.isArray(rows))rows=[rows];\n  if(!rows.length)return null;\n  const vals=rows.map(r=>({\n    pnu:String(r.pnu||\'\'), ldCodeNm:String(r.ldCodeNm||\'\'), mnnmSlno:String(r.mnnmSlno||\'\'),\n    regstrSeCodeNm:String(r.regstrSeCodeNm||\'\'), lndcgrCodeNm:String(r.lndcgrCodeNm||\'\'),\n    lndpclAr:Number(r.lndpclAr)||null, posesnSeCodeNm:String(r.posesnSeCodeNm||\'\'),\n    cnrsPsnCo:Number(r.cnrsPsnCo)||0, ladFrtlScNm:String(r.ladFrtlScNm||\'\'),\n    lastUpdtDt:String(r.lastUpdtDt||\'\')\n  }));\n  vals.sort((a,b)=>String(b.lastUpdtDt||\'\').localeCompare(String(a.lastUpdtDt||\'\')));\n  return vals[0];\n}\n\n\nfunction parseLandCharacteristicsXml(xmlText){\n  const doc=new DOMParser().parseFromString(xmlText,\'application/xml\');if(doc.querySelector(\'parsererror\'))return null;const fields=[...doc.querySelectorAll(\'field\')];if(!fields.length)return null;\n  const vals=fields.map(row=>{const g=n=>row.querySelector(n)?.textContent?.trim()||\'\';return {pnu:g(\'pnu\'),lndpclAr:Number(g(\'lndpclAr\'))||null,lndcgrCodeNm:g(\'lndcgrCodeNm\')||g(\'lndcgrCode\'),stdrYear:g(\'stdrYear\'),stdrMt:g(\'stdrMt\'),lastUpdtDt:g(\'lastUpdtDt\'),_route:\'getLandCharacteristics\'};}).filter(x=>x.lndpclAr!=null);vals.sort((a,b)=>String(b.stdrYear||\'\').localeCompare(String(a.stdrYear||\'\')));return vals[0]||null;\n}\nasync function fetchLandCharacteristicsBrowser(pnu){\n  const base=\'https://api.vworld.kr/ned/data/getLandCharacteristics\';const now=new Date().getFullYear();\n  for(let y=now;y>=now-5;y--){\n    try{const u=new URL(base);for(const [k,v] of Object.entries({key:VWORLD_CLIENT_KEY,domain:VWORLD_CLIENT_DOMAIN,pnu:String(pnu),format:\'xml\',stdrYear:String(y)}))u.searchParams.set(k,v);const r=await fetch(u.toString(),{mode:\'cors\'});if(r.ok){const rec=parseLandCharacteristicsXml(await r.text());if(rec)return rec;}}catch(e){}\n    try{const data=await vworldJsonp(base,{key:VWORLD_CLIENT_KEY,domain:VWORLD_CLIENT_DOMAIN,pnu:String(pnu),format:\'json\',stdrYear:String(y)},9000);const f=data?.response?.fields?.field||data?.fields?.field;const arr=Array.isArray(f)?f:(f?[f]:[]);for(const x of arr){const a=Number(x.lndpclAr);if(Number.isFinite(a)&&a>0)return {pnu:String(pnu),lndpclAr:a,lndcgrCodeNm:String(x.lndcgrCodeNm||x.lndcgrCode||\'\'),stdrYear:String(x.stdrYear||y),stdrMt:String(x.stdrMt||\'\'),lastUpdtDt:String(x.lastUpdtDt||\'\'),_route:\'getLandCharacteristics_jsonp\'};}}catch(e){}\n  } return null;\n}\nasync function fetchLandLedgerBrowser(pnu){\n  const base=\'https://api.vworld.kr/ned/data/ladfrlList\';\n  const common={key:VWORLD_CLIENT_KEY,domain:VWORLD_CLIENT_DOMAIN,pnu:String(pnu)};\n\n  // 1) Browser CORS XML\n  try{\n    const u=new URL(base);\n    for(const [k,v] of Object.entries(common))u.searchParams.set(k,v);\n    u.searchParams.set(\'format\',\'xml\');\n    const r=await fetch(u.toString(),{method:\'GET\',mode:\'cors\'});\n    if(r.ok){\n      const t=await r.text();\n      const parsed=parseLandLedgerXml(t);\n      if(parsed)return {...parsed,_route:\'vworld_xml\'};\n    }\n  }catch(e){}\n\n  // 2) JSONP fallback (some VWorld endpoints accept JSON callback)\n  try{\n    const data=await vworldJsonp(base,{...common,format:\'json\'},12000);\n    const parsed=parseLandLedgerJson(data);\n    if(parsed)return {...parsed,_route:\'vworld_jsonp\'};\n  }catch(e){}\n\n  // 3) Render backend fallback (VWorld direct/proxy + legacy data.go.kr)\n  try{\n    const r=await fetch(\'/api/land/ledger-one\',{\n      method:\'POST\',\n      headers:{\'Content-Type\':\'application/json\'},\n      body:JSON.stringify({pnu:String(pnu)})\n    });\n    const d=await r.json();\n    if(r.ok && d.record)return d.record;\n  }catch(e){}\n\n  const ch=await fetchLandCharacteristicsBrowser(pnu);\n  if(ch)return ch;\n  return null;\n}\n\nfunction parcelGeometryArea(f){\n  try{return turf.area(turf.feature(f.geometry));}\n  catch(e){return null;}\n}\n\nasync function analyzeLandLedger(){\n  const pnus=[...selectedParcelPnus].filter(p=>/^\\d{19}$/.test(String(p)));\n  const summary=document.getElementById(\'landSummary\');\n  const conn=document.getElementById(\'landConn\');\n  const wrap=document.getElementById(\'landTableWrap\');\n  const tbody=document.getElementById(\'landTableBody\');\n\n  if(!pnus.length){\n    summary.textContent=\'선택된 PNU가 없습니다.\';\n    return;\n  }\n\n  conn.textContent=\'토지대장 조회 중\';\n  conn.className=\'conn planned\';\n  summary.textContent=`선택필지 ${pnus.length}개 토지임야대장 조회 중...`;\n  wrap.style.display=\'none\'; tbody.innerHTML=\'\';\n\n  const records=[];\n  let done=0;\n  const resultMap=new Map();\n\n  // Conservative concurrency for VWorld.\n  const results=await mapLimit(pnus,5,async pnu=>{\n    const rec=await fetchLandLedgerBrowser(pnu);\n    done++;\n    summary.textContent=`토지대장 조회 ${done}/${pnus.length}필지`;\n    return rec;\n  });\n\n  let official=0,small=0,geometryFallback=0;\n  let latestDates=[];\n  for(let i=0;i<pnus.length;i++){\n    const pnu=pnus[i];\n    const rec=results[i];\n    const officialArea=rec ? validPositiveNumber(rec.lndpclAr) : null;\n    if(rec && officialArea!=null){\n      rec.lndpclAr=officialArea;\n      official++;\n      if(rec.lastUpdtDt)latestDates.push(String(rec.lastUpdtDt));\n      resultMap.set(pnu,rec);\n    }\n  }\n\n  // Initial-screening rule: official ledger area first; if missing, cadastral polygon area.\n  // Every parcel gets an analysis area and can therefore be filtered on the map.\n  for(const [pnu,f] of parcelFeatureMap.entries()){\n    const rec=resultMap.get(String(pnu));\n    const props=f.properties||{};\n    props.geometry_area_m2=parcelGeometryArea(f);\n    const oa=rec ? validPositiveNumber(rec.lndpclAr) : null;\n    if(rec && oa!=null){\n      props.official_area_m2=oa;\n      props._analysis_area_m2=oa;\n      props._small_source=\'토지임야대장 공식면적\';\n      props.land_ledger=rec;\n    }else{\n      props.official_area_m2=null;\n      props._analysis_area_m2=Number.isFinite(Number(props.geometry_area_m2))?Number(props.geometry_area_m2):null;\n      props._small_source=\'연속지적 계산면적(예비)\';\n      props.land_ledger={lndcgrCodeNm:parseJibunLandCategory(props.jibun||\'\'),mnnmSlno:props.jibun||\'\',_route:\'LP_PA_CBND_BUBUN\'};\n      if(selectedParcelPnus.has(String(pnu)) && props._analysis_area_m2!=null)geometryFallback++;\n    }\n    props.is_small=props._analysis_area_m2!=null ? Number(props._analysis_area_m2)<90 : null;\n    if(selectedParcelPnus.has(String(pnu)) && props.is_small===true)small++;\n    f.properties=props;\n  }\n  syncParcelLayerFromState();\n\n  document.getElementById(\'total_parcel_count\').value=pnus.length;\n  document.getElementById(\'small_parcel_count\').value=small;\n  if(official===pnus.length){\n    conn.textContent=\'과소필지 AUTO\';\n    conn.className=\'conn auto\';\n    analysisState.quality.small=\'OFFICIAL\';\n  }else{\n    conn.textContent=\'과소필지 AUTO(예비)\';\n    conn.className=\'conn planned\';\n    analysisState.quality.small=\'MIXED\';\n  }\n\n  const selectedFs=pnus.map(p=>parcelFeatureMap.get(p)).filter(Boolean);\n  tbody.innerHTML=selectedFs.slice(0,600).map(f=>{\n    const p=f.properties||{};\n    const r=p.land_ledger||{};\n    const ga=p.geometry_area_m2;\n    const oa=p.official_area_m2;\n    const aa=p._analysis_area_m2;\n    const smallTxt=p.is_small===true ? (oa!=null?\'<span class="small-official">90㎡ 미만</span>\':\'<span class="small-estimate">90㎡ 미만(연속지적 예비)</span>\') : \'-\';\n    return `<tr>\n      <td>${escHtml(r.mnnmSlno||p.jibun||\'\')}</td>\n      <td>${escHtml(p.pnu||\'\')}</td>\n      <td>${escHtml(r.lndcgrCodeNm||\'-\')}</td>\n      <td>${oa==null?\'-\':fmt(oa,2)}</td>\n      <td>${ga==null?\'-\':fmt(ga,2)}</td>\n      <td>${hubPlatAreaForPnu(p.pnu)==null?\'-\':fmt(hubPlatAreaForPnu(p.pnu),2)}</td>\n      <td>${escHtml(r.posesnSeCodeNm||\'-\')}</td>\n      <td>${escHtml(r.cnrsPsnCo??\'-\')}</td>\n      <td>${escHtml(r.ladFrtlScNm||\'-\')}</td>\n      <td>${escHtml(hubFmtDay(r.lastUpdtDt||\'\'))}</td>\n      <td>${smallTxt}</td>\n    </tr>`;\n  }).join(\'\');\n  wrap.style.display=selectedFs.length?\'block\':\'none\';\n\n  latestDates=latestDates.filter(Boolean).sort();\n  const newest=latestDates.length?hubFmtDay(latestDates[latestDates.length-1]):\'-\';\n  const oldest=latestDates.length?hubFmtDay(latestDates[0]):\'-\';\n  const ratio=pnus.length?small/pnus.length*100:0;\n\n  if(official===pnus.length){\n    summary.innerHTML=`선택필지 ${pnus.length}개 · 공식면적 <b>${official}/${pnus.length}</b> · 90㎡ 미만 <b>${small}필지 (${ratio.toFixed(1)}%)</b> · 데이터기준일 범위 ${oldest} ~ ${newest}`;\n    document.getElementById(\'parcelStatus\').innerHTML=`<b>과소필지 AUTO:</b> 90㎡ 미만 ${small}/${pnus.length}필지 = ${ratio.toFixed(1)}%. 지도에서 빨간 필지로 분리했습니다.`;\n  }else{\n    summary.innerHTML=`선택필지 ${pnus.length}개 · 공식면적 <b>${official}/${pnus.length}</b> · 연속지적 계산면적 보완 <b>${geometryFallback}필지</b> · 90㎡ 미만 <b>${small}필지 (${ratio.toFixed(1)}%, 초기검토)</b>.`;\n    document.getElementById(\'parcelStatus\').innerHTML=`<b>과소필지 AUTO(초기검토):</b> 공식면적 우선 + 미확인 필지 연속지적 계산면적 보완으로 90㎡ 미만 ${small}/${pnus.length}필지 = ${ratio.toFixed(1)}%.`;\n  }\n  refreshAnalysisLayers();\n}\n\n\nfunction loadSample(){\n  const vals={total_building_count:100,old_building_count:75,total_parcel_count:100,small_parcel_count:32,road_basis_building_count:100,road_access_building_count_6m:55,house_density_per_ha:42,total_floor_area_m2:30000,old_floor_area_m2:14000,request_owner_consent_ratio:32};\n  Object.entries(vals).forEach(([k,v])=>document.getElementById(k).value=v);\n}\nfunction clearInputs(){\n  [\'total_building_count\',\'old_building_count\',\'total_parcel_count\',\'small_parcel_count\',\'road_basis_building_count\',\'road_access_building_count_6m\',\'house_density_per_ha\',\'total_floor_area_m2\',\'old_floor_area_m2\',\'request_owner_consent_ratio\',\'proposal_owner_consent_ratio\',\'proposal_land_area_consent_ratio\'].forEach(id=>document.getElementById(id).value=\'\');\n  document.getElementById(\'promotion_district\').checked=false; document.getElementById(\'area_5000_exception_approved\').checked=false;\n  if(activeGeometry) runAllAutoAnalyses();\n}\n\nasync function runEvaluation(){\n  updateRedevelopmentStrategySignal();\n  const total=Number(analysisState.metrics.total_buildings)||0;\n  if(!total){\n    alert(\'건축HUB 대장조회를 먼저 실행하세요.\');\n  }\n}\nfunction renderResult(d){\n  const s=d.physical_eligibility;\n  const rows=d.checks.map(c=>{\n    const src=(c.source_ids||[]).map(id=>{const x=d.sources[id];return x?`<a class="source-link" href="${x.url}" target="_blank">${id}</a>`:id}).join(\'<br>\');\n    return `<tr><td>${groupKo(c.group)}</td><td><b>${c.label}</b><div class="tiny">${c.note||\'\'}</div></td><td>${c.requirement}</td><td>${c.actual??\'-\'}</td><td><span class="pill ${c.status}">${c.status}</span></td><td>${src}</td></tr>`;\n  }).join(\'\');\n  const policy=(d.policy_watch||[]).map(x=>`<li><b>${x.status}</b> · ${x.current}<br>${x.engine_behavior}</li>`).join(\'\');\n  document.getElementById(\'result\').innerHTML=`\n    <div class="statusbox"><div class="status ${s.status}">${s.status}</div><div class="statusmsg">${s.message}</div><div class="tiny" style="margin-top:8px">${s.meaning} · 룰셋 ${d.engine.id}</div></div>\n    <table><thead><tr><th>구분</th><th>항목</th><th>기준</th><th>대상지</th><th>판정</th><th>근거</th></tr></thead><tbody>${rows}</tbody></table>\n    <details open><summary>판정 해석 및 한계</summary><div class="details-body"><ul class="note-list">${d.special_notes.map(x=>`<li>${x}</li>`).join(\'\')}</ul></div></details>\n    <details><summary>정책 변경 추적</summary><div class="details-body"><ul class="note-list">${policy||\'<li>없음</li>\'}</ul></div></details>`;\n}\nfunction groupKo(g){return {mandatory:\'필수\',selection:\'선택/간주\',consent:\'주민절차\'}[g]||g}\nrunAllSchemeChecks();\nupdateCompactMapRole();\nupdateCandidateSchemes();\nloadStoredStationDataset();\n\n</script>\n</body>\n</html>\n'

app = FastAPI(
    title="도시검토 플랫폼 - 서울 재개발 웹 MVP",
    version="1.5.0",
    description="웹 지도 + 서울 주택정비형 재개발 Rule Engine + VWorld 과소필지 자동분석",
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


class BuildingHubBatchInput(BaseModel):
    pnus: List[str] = Field(..., min_length=1, max_length=50)


class LandLedgerOneInput(BaseModel):
    pnu: str


@app.get("/", response_class=HTMLResponse)
def home():
    # VWorld 공식 웹 샘플처럼 브라우저에서 Data API를 직접 호출한다.
    # 키는 GitHub 소스에는 없고 Render 환경변수에서 런타임에 주입된다.
    return INDEX_HTML.replace("__VWORLD_CLIENT_KEY__", _vworld_key())


@app.get("/health")
def health():
    return {
        "ok": True,
        "app": "compactcity_platform_v1.5.0",
        "engine": RULES["rule_set_id"],
        "map": "leaflet-draw",
        "vworld_configured": vworld_ready(),
        "vworld_domain": _vworld_domain() if vworld_ready() else None,
        "parcel_auto": "browser_direct_ready" if vworld_ready() else "needs_VWORLD_API_KEY",
        "building_spatial_auto": "LT_C_SPBD_browser_direct_ready" if vworld_ready() else "needs_VWORLD_API_KEY",
        "building_hub": "ready" if building_hub_ready() else "needs_BUILDING_HUB_API_KEY",
        "land_ledger": "ladfrlList + getLandCharacteristics + geometry provisional",
        "road_access": "excluded_from_primary_private_screening",
        "analysis_object_model": "parcel/building common ledger retained for station-area/zoning/mixed-use expansion",
        "redevelopment_strategy": "age_ratio_primary_plus_small_parcel_secondary_screening",
        "scheme_sheets": ["housing_redevelopment","reconstruction","station_activation","safe_housing","station_complex_district","longterm_lease","public_housing_complex","urban_complex_innovation"],
        "scheme_age_stats": "BuildingHUB exact 20y/30y elapsed ratios, unknown dates kept in denominator",
        "density_public_contribution": "8-scheme zoning/FAR/public-contribution simultaneous review",
        "scheme_ui": "8-scheme simultaneous matrix plus eight visible detail sheets",
        "station_boundary_gis": "JUSO TL_SPSB_STATN SHP one-time browser load + IndexedDB + automatic 250/350/500m analysis",
        "first_screen": "purpose/land-control/actor filter + 8 candidate schemes + location map + compact land/building rail",
        "house_density": "excluded_from_primary_redevelopment_screening",
        "parcel_boundary_editor": "pnu_list_click_include_exclude_nearby_union",
        "provenance_ui": True,
    }


@app.post("/api/spatial/measure")
def spatial_measure(inp: GeometryInput):
    try:
        return measure_geojson(inp.geometry)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc



@app.get("/api/vworld/test")
def vworld_test():
    """VWorld 연결 진단. API 키 값은 반환하지 않습니다."""
    if not vworld_ready():
        raise HTTPException(status_code=503, detail="VWORLD_API_KEY가 설정되지 않았습니다.")
    params = {
        "key": _vworld_key(),
        "domain": _vworld_domain(),
        "service": "data",
        "version": "2.0",
        "request": "getfeature",
        "format": "json",
        "size": 1,
        "page": 1,
        "geometry": "false",
        "attribute": "true",
        "crs": "EPSG:4326",
        "data": VWORLD_LAYER_PARCEL,
        "geomfilter": "POINT(126.978,37.566)",
    }
    try:
        resp, route = _vworld_get(VWORLD_DATA_URL, params=params, timeout=15)
        result = {
            "http_status": resp.status_code,
            "route": route,
            "domain_sent": _vworld_domain(),
            "referer_sent": _vworld_referer(),
        }
        try:
            payload = resp.json()
            result["vworld_status"] = (payload.get("response") or {}).get("status")
            if str(result["vworld_status"]).upper() != "OK":
                result["vworld_error"] = _response_error_message(payload)
            else:
                fc = (((payload.get("response") or {}).get("result") or {}).get("featureCollection") or {})
                result["feature_count"] = len(fc.get("features") or [])
        except Exception:
            result["body_preview"] = resp.text[:500]
        return result
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"VWorld 테스트 실패: {exc}") from exc

@app.post("/api/parcels/analyze")
def parcel_analyze(inp: GeometryInput):
    if not vworld_ready():
        raise HTTPException(
            status_code=503,
            detail="VWorld API 키가 아직 설정되지 않았습니다. Render Environment에 VWORLD_API_KEY를 등록하면 과소필지 AUTO가 활성화됩니다.",
        )
    try:
        return analyze_parcels_for_geometry(inp.geometry)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"VWorld 통신 오류: {exc}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"필지 자동분석 오류: {exc}") from exc




@app.post("/api/land/ledger-one")
def land_ledger_one(inp: LandLedgerOneInput):
    pnu = str(inp.pnu or "").strip()
    if len(pnu) != 19 or not pnu.isdigit():
        raise HTTPException(status_code=422, detail="PNU는 19자리 숫자여야 합니다.")

    record = _server_land_ledger_vworld(pnu)
    if record is None:
        record = _server_land_ledger_legacy_data_go(pnu)

    return {
        "pnu": pnu,
        "record": record,
        "source": {
            "provider": "국토교통부",
            "dataset": "토지임야정보(속성정보)",
            "operation": "ladfrlList",
            "portal_modified": "2025-07-01",
        },
    }


@app.post("/api/building-hub/title-batch")
def building_hub_title_batch(inp: BuildingHubBatchInput):
    if not building_hub_ready():
        raise HTTPException(
            status_code=503,
            detail="BUILDING_HUB_API_KEY가 Render Environment에 설정되지 않았습니다.",
        )

    pnus = []
    seen = set()
    for p in inp.pnus:
        p = str(p).strip()
        if p not in seen:
            seen.add(p)
            pnus.append(p)

    records: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    # Small concurrent fan-out keeps each browser request short without flooding data.go.kr.
    with ThreadPoolExecutor(max_workers=min(5, len(pnus))) as ex:
        futures = {ex.submit(_query_building_hub_title, pnu): pnu for pnu in pnus}
        for fut in as_completed(futures):
            pnu = futures[fut]
            try:
                items = fut.result()
                records.extend(_normalize_building_title(item, pnu) for item in items)
            except Exception as exc:
                logger.warning("BuildingHUB title failed pnu=%s error=%s", pnu, exc)
                errors.append({"pnu": pnu, "error": str(exc)})

    return {
        "requested_pnu_count": len(pnus),
        "record_count": len(records),
        "records": records,
        "errors": errors,
        "source": {
            "provider": "국토교통부",
            "dataset": "건축HUB 건축물대장정보 서비스",
            "operation": "getBrTitleInfo",
            "data_portal_modified": "2026-07-10",
            "engine_as_of_date": "2026-08-24",
        },
    }


@app.post("/api/redevelopment/evaluate")
def redevelopment_evaluate(inp: RedevelopmentInput):
    return evaluate_redevelopment(inp.model_dump())


@app.post("/api/redevelopment/house-density")
def house_density(detail: Dict[str, Any]):
    return calculate_house_density(detail)
