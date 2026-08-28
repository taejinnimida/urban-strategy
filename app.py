from __future__ import annotations

import math
import os
import logging
import json
import re
import zlib
import base64
import io
import zipfile
import html
import hmac
import threading
import uuid
from collections import deque
import xml.etree.ElementTree as ET
from datetime import date, datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache

import requests
try:
    import psycopg
except ImportError:  # local/offline test without PostgreSQL driver
    psycopg = None
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request, Response, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
from pyproj import CRS, Geod, Transformer
import shapefile
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, shape, mapping
from shapely.ops import transform as geometry_transform, unary_union
from shapely.strtree import STRtree
from shapely.validation import explain_validity

# 도로명주소 원본에는 링 방향이 뒤집힌 유효 폴리곤이 일부 포함된다.
# pyshp의 반복 경고만 억제하고, 아래 로더에서 buffer(0)으로 형상을 보정한다.
shapefile.VERBOSE = False

# ============================================================
# 도시검토 플랫폼 v2.5.0
# - 서버·정적화면·공간자료를 분리한 Docker 배포판
# - 서울 14개 정비·개발사업 Rule Engine + 공간근거·관리자 운영
# - 웹 지도 Polygon 면적 자동계산
# 기준일: 2026-08-26
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STRUCTURED_DATA_DIR = os.path.join(BASE_DIR, "data")
STRUCTURED_STATIC_HTML = os.path.join(BASE_DIR, "static", "app.html")

# GitHub 웹 업로드는 선택한 폴더 안의 파일을 저장소 루트로 평탄화할 수
# 있다. 정식 폴더 구조를 우선하되, 기존 단일폴더 배포방식도 자동 지원한다.
DATA_DIR = STRUCTURED_DATA_DIR if os.path.isdir(STRUCTURED_DATA_DIR) else BASE_DIR
STATIC_HTML_PATH = (
    STRUCTURED_STATIC_HTML
    if os.path.isfile(STRUCTURED_STATIC_HTML)
    else os.path.join(BASE_DIR, "app.html")
)
STATIC_DIR = os.path.dirname(STATIC_HTML_PATH)


def _data_path(name: str) -> str:
    return os.path.join(DATA_DIR, name)


@lru_cache(maxsize=1)
def _index_html() -> str:
    with open(STATIC_HTML_PATH, encoding="utf-8") as fp:
        return fp.read()

RULES = {'rule_set_id': 'seoul_housing_redevelopment_2026_08_v03', 'title': '서울 주택정비형 재개발 1차 입안대상 판정', 'scope': '서울특별시 주택정비형 재개발사업 정비계획 입안대상지역 1차 스크리닝', 'as_of': '2026-08-26', 'thresholds': {'area_normal_m2': 10000, 'area_exception_m2': 5000, 'old_building_count_ratio': 0.6, 'old_building_count_ratio_promotion_district': 0.5, 'old_building_count_deemed_selection_ratio': 0.75, 'small_parcel_ratio': 0.4, 'housing_road_access_ratio': 0.4, 'house_density_per_ha': 60, 'old_floor_area_ratio': 0.6, 'old_floor_area_ratio_promotion_district': 0.5, 'request_owner_consent_ratio': 0.3, 'proposal_owner_consent_ratio': 0.6, 'proposal_land_area_consent_ratio': 0.5}, 'policy_watch': [{'id': 'OLD_COUNT_DEEMED_70_WATCH', 'status': 'UNVERIFIED_NOT_ACTIVE', 'current': '노후·불량건축물 수 75% 이상이면 조례상 추가요건을 갖춘 것으로 보는 간주규정', 'possible_future': '70% 완화 가능성 언급이 있어 향후 시행령 개정 여부 추적 필요', 'engine_behavior': '현행 75%만 적용. 법령 공포·시행 전에는 70%를 판정에 사용하지 않음'}], 'sources': [{'id': 'ENFORCEMENT_DECREE_APPENDIX1', 'title': '도시 및 주거환경정비법 시행령 제7조제1항 별표 1', 'url': 'https://www.law.go.kr/lsInfoP.do?lsId=009521', 'note': '재개발 정비계획 입안대상지역 기본요건 및 노후·불량건축물 75% 간주규정'}, {'id': 'SEOUL_ORDINANCE_ART2_5', 'title': '서울특별시 도시 및 주거환경정비 조례 제2조제5호', 'url': 'https://law.go.kr/LSW/ordinInfoP.do?ordinSeq=2130189', 'note': '호수밀도 정의 및 유형별 산정기준'}, {'id': 'SEOUL_ORDINANCE_ART6', 'title': '서울특별시 도시 및 주거환경정비 조례 제6조', 'url': 'https://law.go.kr/LSW/ordinInfoP.do?ordinSeq=2130189', 'note': '주택정비형 재개발 면적·노후도·과소필지·주택접도율·호수밀도 요건'}, {'id': 'SEOUL_ORDINANCE_ART9_2', 'title': '서울특별시 도시 및 주거환경정비 조례 제9조의2', 'url': 'https://law.go.kr/LSW/ordinInfoP.do?ordinSeq=2130189', 'note': '정비계획 입안요청 동의비율'}, {'id': 'SEOUL_ORDINANCE_ART10', 'title': '서울특별시 도시 및 주거환경정비 조례 제10조', 'url': 'https://law.go.kr/LSW/ordinInfoP.do?ordinSeq=2130189', 'note': '정비계획 입안제안 동의요건'}]}
RULES['sources'].append({'id':'SEOUL_ORDINANCE_ART2_10','title':'서울특별시 도시 및 주거환경정비 조례 제2조제10호','url':'https://law.go.kr/LSW/ordinInfoP.do?ordinSeq=2130189','note':'주택접도율 정의: 도로 접도길이 4m 이상. 제6조에서 주택정비형 재개발은 도로폭 6m 이상 적용'})
RULES['rule_set_id'] = 'seoul_urban_strategy_14schemes_2026_08_v04'


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
ENGINE_AS_OF_DATE = datetime.now(ZoneInfo("Asia/Seoul")).date()


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
            "age_basis": "REFERENCE_ONLY",
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
        "age_basis": "REFERENCE_ONLY",
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

RENEWAL_LEGAL_ZIP_PATH = _data_path("uq181_legal.zip")
RENEWAL_PROJECT_ZIP_PATH = _data_path("uq120_project.zip")

# INDEX_HTML은 static/app.html에서 읽는다 (_index_html 참조)
STATION_REFERENCE_PATH = _data_path("stations.json")
@lru_cache(maxsize=1)
def _station_reference_data():
    with open(STATION_REFERENCE_PATH, encoding="utf-8") as fp:
        return json.load(fp)

CENTER_REFERENCE_PATH = _data_path("centers.json")
@lru_cache(maxsize=1)
def _center_reference_data():
    with open(CENTER_REFERENCE_PATH, encoding="utf-8") as fp:
        return json.load(fp)


RENEWAL_LEGAL_TYPES = {
    "UQ1221": ("housing_district", "주택정비형 재개발구역"),
    "UQ1222": ("urban_district", "도시정비형 재개발구역"),
    "UQ1231": ("housing_district", "주택정비형 재개발지구"),
    "UQ1232": ("urban_district", "도시정비형 재개발지구"),
    "UQ1240": ("reconstruction", "재건축사업구역"),
    "UQ1206": ("reconstruction", "주택재건축사업"),
    # 아래 유형은 독립 '정비사업 관련 현황도'에는 표시하되 기존
    # 재개발/재건축 사업방식 자동판정값을 덮어쓰지 않는 표시 전용 유형이다.
    "UQ1211": ("other_renewal", "주거환경개선사업"),
    "UQ1212": ("other_renewal", "주거환경관리사업"),
    "UQ1220": ("other_renewal", "재개발사업구역(세부분류 미확인)"),
    "UQ1250": ("other_renewal", "결합정비구역"),
    "UQ1260": ("other_renewal", "자율주택정비사업구역"),
    "UQ1270": ("other_renewal", "가로주택정비사업구역"),
    "UQ1280": ("other_renewal", "소규모재건축사업구역"),
    "UQ1290": ("other_renewal", "기타 정비구역"),
    # 2026-02 UQ181 실제 SHP의 소규모주택정비 세부분류 코드.
    "UQ1811": ("other_renewal", "자율주택정비사업"),
    "UQ1812": ("other_renewal", "가로주택정비사업"),
    "UQ1813": ("other_renewal", "소규모재건축사업"),
    "UQ1814": ("other_renewal", "소규모재개발사업"),
}
RENEWAL_PROJECT_TYPES = {
    "BZ101": ("housing_planned", "신속통합기획 후보·사업구역"),
    "BZ102": ("urban_planned", "도시정비형 재개발 사업구역"),
    "BZ103": ("housing_planned", "주택정비형 재개발 사업구역"),
    "BZ104": ("reconstruction", "공동주택 재건축 사업구역"),
    "BZ105": ("reconstruction", "단독주택 재건축 사업구역"),
}


def _read_embedded_shapefile(zip_path: str, stem: str):
    with zipfile.ZipFile(zip_path) as archive:
        shp_name = next(n for n in archive.namelist() if n.upper().endswith(f"/{stem}.SHP") or n.upper() == f"{stem}.SHP")
        dbf_name = next(n for n in archive.namelist() if n.upper().endswith(f"/{stem}.DBF") or n.upper() == f"{stem}.DBF")
        return shapefile.Reader(
            shp=io.BytesIO(archive.read(shp_name)),
            dbf=io.BytesIO(archive.read(dbf_name)),
            encoding="cp949",
        )


@lru_cache(maxsize=1)
def _renewal_reference_data():
    """Official Seoul SHPs converted to lightweight WGS84 GeoJSON at runtime."""
    transformer = Transformer.from_crs(5174, 4326, always_xy=True)
    features = []

    def append_source(zip_path, stem, source, type_map, promotion_codes=False):
        reader = _read_embedded_shapefile(zip_path, stem)
        fields = [f[0] for f in reader.fields[1:]]
        for sr in reader.iterShapeRecords():
            row = dict(zip(fields, sr.record))
            # SCLAS_CL is the operative detailed class (e.g. UQ1222/BZ103);
            # MLSFC_CL is only its broader parent class.
            code = str(row.get("SCLAS_CL") or row.get("MLSFC_CL") or "").strip()
            type_info = type_map.get(code)
            if type_info is None and promotion_codes and code.startswith("UQ51"):
                type_info = ("promotion", "재정비촉진지구·구역")
            if type_info is None and source == "project" and code in {"BZ401", "BZ402", "BZ403", "BZ404"}:
                type_info = ("promotion", "재정비촉진지구·구역")
            if type_info is None:
                continue
            try:
                geom = shape(sr.shape.__geo_interface__)
                if geom.is_empty:
                    continue
                geom = geom.simplify(0.25, preserve_topology=True)
                geom = geometry_transform(transformer.transform, geom)
            except Exception:
                continue
            features.append({
                "type": "Feature",
                "geometry": mapping(geom),
                "properties": {
                    "source": source,
                    "source_title": "서울 의제처리구역 위치정보(UQ181)" if source == "legal" else "서울 도시계획사업 현황(서울플랜+, UQ120)",
                    "source_layer": stem,
                    "code": code,
                    "renewal_type": type_info[0],
                    "type_label": type_info[1],
                    "name": str(row.get("DGM_NM") or "미상구역").strip(),
                    "notice_no": str(row.get("NTFC_SN") or "").strip(),
                    "notice_date": str(row.get("CREATE_DAT") or "").strip(),
                },
            })

    append_source(RENEWAL_LEGAL_ZIP_PATH, "UPIS_C_UQ181", "legal", RENEWAL_LEGAL_TYPES, True)
    append_source(RENEWAL_PROJECT_ZIP_PATH, "UPIS_C_UQ120", "project", RENEWAL_PROJECT_TYPES)
    return {
        "type": "FeatureCollection",
        "name": "서울시 정비구역·도시계획사업 참고도형",
        "features": features,
        "metadata": {
            "reference_month": "2026-02",
            "legal_source": "서울 의제처리구역 위치정보(UQ181)",
            "project_source": "서울 도시계획사업 현황(서울플랜+, UQ120)",
            "legal_priority": True,
            "disclaimer": "공개 GIS 중첩은 초기검토용 참고값이며 최종 결정고시·정비계획 도서를 재확인해야 합니다.",
        },
    }


@lru_cache(maxsize=1)
def _renewal_spatial_index():
    """Cache Shapely geometries so each browser does not download every Seoul zone."""
    fc = _renewal_reference_data()
    features = fc["features"]
    geometries = [shape(feature["geometry"]) for feature in features]
    return features, geometries, STRtree(geometries)


def _polygonal_only(geom):
    if isinstance(geom, (Polygon, MultiPolygon)):
        return geom
    if isinstance(geom, GeometryCollection):
        parts = [g for g in geom.geoms if isinstance(g, (Polygon, MultiPolygon)) and not g.is_empty]
        return unary_union(parts) if parts else None
    return None


def analyze_renewal_intersections(geometry: Dict[str, Any]) -> Dict[str, Any]:
    """Intersect one target boundary on the server and return only matched zones.

    Areas are measured in the source SHP CRS (EPSG:5174). Legal UQ181 zones
    outrank project/candidate UQ120 features, while promotion zones are reported
    on a separate track and never overwrite the redevelopment type.
    """
    try:
        site_wgs = _polygonal_only(shape(geometry))
    except Exception as exc:
        raise ValueError(f"구역계 GeoJSON을 읽을 수 없습니다: {exc}") from exc
    if site_wgs is None or site_wgs.is_empty:
        raise ValueError("구역계는 Polygon 또는 MultiPolygon이어야 합니다.")
    if not site_wgs.is_valid:
        site_wgs = _polygonal_only(site_wgs.buffer(0))
    if site_wgs is None or site_wgs.is_empty or not site_wgs.is_valid:
        raise ValueError("유효하지 않은 구역계입니다.")

    to_metric = Transformer.from_crs(4326, 5174, always_xy=True).transform
    to_wgs = Transformer.from_crs(5174, 4326, always_xy=True).transform
    site_metric = geometry_transform(to_metric, site_wgs)
    site_area = float(site_metric.area)
    if site_area <= 0:
        raise ValueError("구역계 면적이 0입니다.")

    features, geometries, tree = _renewal_spatial_index()
    overlaps = []
    context_features = []
    for index in tree.query(site_wgs, predicate="intersects"):
        feature = features[int(index)]
        source_wgs = geometries[int(index)]
        try:
            intersection_wgs = _polygonal_only(site_wgs.intersection(source_wgs))
            if intersection_wgs is None or intersection_wgs.is_empty:
                continue
            intersection_metric = _polygonal_only(geometry_transform(to_metric, intersection_wgs))
            if intersection_metric is None or intersection_metric.is_empty:
                continue
            overlap_area = float(intersection_metric.area)
            if overlap_area < 0.5:
                continue
            zone_metric = geometry_transform(to_metric, source_wgs)
            zone_area = float(zone_metric.area)
            result_geom = geometry_transform(to_wgs, intersection_metric.simplify(0.10, preserve_topology=True))
        except Exception:
            continue
        props = dict(feature.get("properties") or {})
        props.update({
            "overlap_area_m2": round(overlap_area, 2),
            "site_overlap_pct": round(overlap_area / site_area * 100, 4),
            "zone_overlap_pct": round(overlap_area / zone_area * 100, 4) if zone_area > 0 else None,
            "_overlap_area": round(overlap_area, 2),
            "_overlap_pct": round(overlap_area / site_area * 100, 4),
        })
        overlaps.append({"type": "Feature", "geometry": mapping(result_geom), "properties": props})
        # The independent renewal-status map needs the full official zone boundary
        # (light) and the actual site-overlap polygon (dark).  Return only matched
        # source zones so the payload stays small while map/judgment remain identical.
        context_props = dict(props)
        context_props["_display_role"] = "source_zone"
        context_features.append({
            "type": "Feature",
            "geometry": feature.get("geometry"),
            "properties": context_props,
        })

    overlaps.sort(key=lambda f: (
        0 if f["properties"].get("source") == "legal" else 1,
        -float(f["properties"].get("overlap_area_m2") or 0),
        str(f["properties"].get("name") or ""),
    ))
    decision_types = {"housing_district", "urban_district", "reconstruction", "housing_planned", "urban_planned"}
    non_promotion = [f for f in overlaps if f["properties"].get("renewal_type") in decision_types]
    promotions = [f for f in overlaps if f["properties"].get("renewal_type") == "promotion"]
    legal_non_promotion = [f for f in non_promotion if f["properties"].get("source") == "legal"]
    legal_promotions = [f for f in promotions if f["properties"].get("source") == "legal"]
    primary = (legal_non_promotion or non_promotion or [None])[0]
    primary_promotion = (legal_promotions or promotions or [None])[0]
    return {
        "status": "matched" if overlaps else "none",
        "site_area_m2": round(site_area, 2),
        "renewal_area_type": primary["properties"]["renewal_type"] if primary else "none",
        "promotion_status": "district" if primary_promotion else "none",
        "primary": primary,
        "primary_promotion": primary_promotion,
        "overlaps": overlaps,
        "context_features": context_features,
        "metadata": _renewal_reference_data()["metadata"],
        "selection_rule": "법정 UQ181 우선 → 중첩면적 우선, 재정비촉진지구·구역은 별도 트랙",
    }


# UQ181 압축파일 내부 코드표(레이어표_181.xlsx)의 법정 분류를 그대로 사용한다.
# 정비구역(UQ12xx/UQ18xx)과 재정비촉진(UQ51xx)은 별도 정비현황도에서 다룬다.
DEVELOPMENT_LEGAL_TYPES = {
    "UQ1100": ("urban_development", "도시개발구역"),
    "UQ1300": ("other_project", "농공단지"),
    "UQ1400": ("other_project", "산업단지"),
    "UQ1500": ("other_project", "전원개발사업구역·예정구역"),
    "UQ1600": ("other_project", "대지조성지구"),
    "UQ1700": ("other_project", "아파트지구개발사업"),
    "UQ1900": ("other_project", "토지구획정리사업구역"),
    "UQ2000": ("other_project", "관광지·관광단지"),
    "UQ2999": ("other_project", "기타 의제처리 사업구역"),
    "UQ5300": ("other_project", "지역균형발전촉진사업"),
    "UQ5400": ("other_project", "국민임대주택단지 예정지구"),
    "UQ5500": ("public_housing", "공공주택지구"),
    "UQ5600": ("other_project", "일단의주택지조성사업지역"),
    "UQ5700": ("other_project", "일단의공업용지조성사업지역"),
    "UQ5800": ("other_project", "일단의불량지구개량사업지역"),
    "UQ5900": ("other_project", "시가지조성사업"),
    "UQ6100": ("other_project", "시가지조성사업지구"),
    "UQ6200": ("other_project", "특정가구정비지구"),
    "UQ6300": ("other_project", "공공지원민간임대주택공급촉진지구"),
    "UQ6400": ("other_project", "시장정비구역"),
    "UQ6500": ("other_project", "택지개발지구"),
    "UQ9100": ("other_project", "주택건설사업"),
}


@lru_cache(maxsize=1)
def _development_reference_data():
    """서울 UQ181의 개발사업 법정구역만 WGS84 GeoJSON으로 변환한다."""
    transformer = Transformer.from_crs(5174, 4326, always_xy=True)
    reader = _read_embedded_shapefile(RENEWAL_LEGAL_ZIP_PATH, "UPIS_C_UQ181")
    fields = [f[0] for f in reader.fields[1:]]
    features = []
    for sr in reader.iterShapeRecords():
        row = dict(zip(fields, sr.record))
        lclass = str(row.get("LCLAS_CL") or "").strip()
        type_info = DEVELOPMENT_LEGAL_TYPES.get(lclass)
        if type_info is None:
            continue
        name = str(row.get("DGM_NM") or "미상구역").strip()
        kind, label = type_info
        # UQ5500에는 현행 SHP상 '도심 공공주택 복합지구'도 포함된다.
        # 명칭상 공공주택지구와 동일하게 표시하지 않고 별도 사업구역으로 보존한다.
        if lclass == "UQ5500" and re.search(r"도심\s*공공주택\s*복합지구", name):
            kind, label = "other_project", "도심 공공주택 복합지구"
        try:
            geom = shape(sr.shape.__geo_interface__)
            if geom.is_empty:
                continue
            geom = geom.simplify(0.25, preserve_topology=True)
            geom = geometry_transform(transformer.transform, geom)
        except Exception:
            continue
        features.append({
            "type": "Feature",
            "geometry": mapping(geom),
            "properties": {
                "source": "legal",
                "source_title": "서울 의제처리구역 위치정보(UQ181)",
                "source_layer": "UPIS_C_UQ181",
                "code": lclass,
                "development_kind": kind,
                "type_label": label,
                "name": name,
                "notice_no": str(row.get("NTFC_SN") or "").strip(),
                "notice_date": str(row.get("CREATE_DAT") or "").strip(),
            },
        })
    return {
        "type": "FeatureCollection",
        "name": "서울시 도시계획·개발사업 법정구역",
        "features": features,
        "metadata": {
            "reference_month": "2026-02",
            "source": "서울 의제처리구역 위치정보(UQ181)",
            "classification_source": "uq181_legal.zip 내부 레이어표_181.xlsx",
            "disclaimer": "공개 GIS 중첩은 초기검토용 참고값이며 최종 결정고시·사업계획·지구지정 도서를 재확인해야 합니다.",
        },
    }


@lru_cache(maxsize=1)
def _development_spatial_index():
    fc = _development_reference_data()
    features = fc["features"]
    geometries = [shape(feature["geometry"]) for feature in features]
    return features, geometries, STRtree(geometries)


def analyze_development_intersections(geometry: Dict[str, Any]) -> Dict[str, Any]:
    """도시개발·공공주택지구·기타 법정 사업구역을 서버에서 실제 중첩한다."""
    try:
        site_wgs = _polygonal_only(shape(geometry))
    except Exception as exc:
        raise ValueError(f"구역계 GeoJSON을 읽을 수 없습니다: {exc}") from exc
    if site_wgs is None or site_wgs.is_empty:
        raise ValueError("구역계는 Polygon 또는 MultiPolygon이어야 합니다.")
    if not site_wgs.is_valid:
        site_wgs = _polygonal_only(site_wgs.buffer(0))
    if site_wgs is None or site_wgs.is_empty or not site_wgs.is_valid:
        raise ValueError("유효하지 않은 구역계입니다.")

    to_metric = Transformer.from_crs(4326, 5174, always_xy=True).transform
    to_wgs = Transformer.from_crs(5174, 4326, always_xy=True).transform
    site_metric = geometry_transform(to_metric, site_wgs)
    site_area = float(site_metric.area)
    if site_area <= 0:
        raise ValueError("구역계 면적이 0입니다.")

    features, geometries, tree = _development_spatial_index()
    overlaps, context_features = [], []
    for index in tree.query(site_wgs, predicate="intersects"):
        feature = features[int(index)]
        source_wgs = geometries[int(index)]
        try:
            intersection_wgs = _polygonal_only(site_wgs.intersection(source_wgs))
            if intersection_wgs is None or intersection_wgs.is_empty:
                continue
            intersection_metric = _polygonal_only(geometry_transform(to_metric, intersection_wgs))
            if intersection_metric is None or intersection_metric.is_empty:
                continue
            overlap_area = float(intersection_metric.area)
            if overlap_area < 0.5:
                continue
            zone_area = float(geometry_transform(to_metric, source_wgs).area)
            result_geom = geometry_transform(to_wgs, intersection_metric.simplify(0.10, preserve_topology=True))
        except Exception:
            continue
        props = dict(feature.get("properties") or {})
        props.update({
            "overlap_area_m2": round(overlap_area, 2),
            "site_overlap_pct": round(overlap_area / site_area * 100, 4),
            "zone_overlap_pct": round(overlap_area / zone_area * 100, 4) if zone_area > 0 else None,
            "_overlap_area": round(overlap_area, 2),
            "_overlap_pct": round(overlap_area / site_area * 100, 4),
        })
        overlaps.append({"type": "Feature", "geometry": mapping(result_geom), "properties": props})
        context_props = dict(props)
        context_props["_display_role"] = "source_zone"
        context_features.append({"type": "Feature", "geometry": feature.get("geometry"), "properties": context_props})

    overlaps.sort(key=lambda f: (-float(f["properties"].get("overlap_area_m2") or 0), str(f["properties"].get("name") or "")))
    return {
        "status": "matched" if overlaps else "none",
        "site_area_m2": round(site_area, 2),
        "overlaps": overlaps,
        "context_features": context_features,
        "metadata": _development_reference_data()["metadata"],
    }


def _road_zip_path() -> Optional[str]:
    """관리자가 한 번 배치하면 모든 사용자가 자동 조회하는 서울 실폭도로 원본."""
    path = _data_path("road_seoul.zip")
    return path if os.path.isfile(path) and os.path.getsize(path) > 0 else None


def _json_property(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _road_width_m(properties: Dict[str, Any]) -> Optional[float]:
    """도로구간 속성에서 공식 폭원(m)을 보수적으로 읽는다."""
    by_upper = {str(key).upper(): value for key, value in (properties or {}).items()}
    for key in ("ROAD_BT", "ROAD_WIDTH", "ROAD_W", "WIDTH"):
        value = by_upper.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, (int, float)):
            width = float(value)
        else:
            matched = re.search(r"\d+(?:\.\d+)?", str(value).replace(",", ""))
            if not matched:
                continue
            width = float(matched.group(0))
        if 1 <= width <= 100:
            return width
    return None


def _centerline_road_polygon(geometry: Any, width_m: float) -> Optional[Any]:
    """WGS84 도로중심선을 공식 폭원의 절반만큼 양측 버퍼한다."""
    if geometry.geom_type not in {"LineString", "MultiLineString"}:
        return None
    to_metric = Transformer.from_crs(4326, 5174, always_xy=True).transform
    to_wgs = Transformer.from_crs(5174, 4326, always_xy=True).transform
    metric = geometry_transform(to_metric, geometry)
    polygon = metric.buffer(width_m / 2, cap_style=2, join_style=2)
    if polygon.is_empty:
        return None
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    return geometry_transform(to_wgs, polygon)


@lru_cache(maxsize=1)
def _road_spatial_layers() -> Dict[str, Any]:
    """VWorld/도로명주소 전자지도 ZIP의 실폭도로·도로구간을 공간색인한다.

    배포본에 포함된 서울 공식 실폭도로 원본(data/road_seoul.zip)을 사용한다.
    일반 사용자는 파일 업로드나 회원가입 없이 구역계만으로 자동 조회한다.
    """
    zip_path = _road_zip_path()
    if not zip_path:
        return {"available": False, "reason": "data/road_seoul.zip 미설치"}

    layers: Dict[str, List[Dict[str, Any]]] = {"rw": [], "manage": []}
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        all_stems = sorted({os.path.splitext(n)[0] for n in names if n.lower().endswith(".shp")})
        named_road_stems = [
            stem for stem in all_stems
            if "SPRD_RW" in os.path.basename(stem).upper()
            or "SPRD_MANAGE" in os.path.basename(stem).upper()
        ]
        # 전자지도 전체묶음에 포함된 건물·행정경계 폴리곤을 실폭도로로
        # 오인하지 않는다. 레이어명이 없는 단일 자료일 때만 도형유형 fallback.
        stems = named_road_stems or all_stems
        for stem in stems:
            shp_name = next((n for n in names if os.path.splitext(n)[0] == stem and n.lower().endswith(".shp")), None)
            shx_name = next((n for n in names if os.path.splitext(n)[0] == stem and n.lower().endswith(".shx")), None)
            dbf_name = next((n for n in names if os.path.splitext(n)[0] == stem and n.lower().endswith(".dbf")), None)
            if not (shp_name and shx_name and dbf_name):
                continue
            prj_name = next((n for n in names if os.path.splitext(n)[0] == stem and n.lower().endswith(".prj")), None)
            source_crs = CRS.from_user_input(os.getenv("ROAD_DATA_CRS", "EPSG:5174"))
            if prj_name:
                try:
                    source_crs = CRS.from_wkt(zf.read(prj_name).decode("utf-8", errors="ignore"))
                except Exception:
                    logging.warning("road PRJ parse failed; ROAD_DATA_CRS fallback used: %s", stem)
            to_wgs = Transformer.from_crs(source_crs, 4326, always_xy=True).transform
            reader = shapefile.Reader(
                shp=io.BytesIO(zf.read(shp_name)),
                shx=io.BytesIO(zf.read(shx_name)),
                dbf=io.BytesIO(zf.read(dbf_name)),
                encoding="cp949",
                encodingErrors="replace",
            )
            fields = [f[0] for f in reader.fields[1:]]
            stem_upper = os.path.basename(stem).upper()
            for sr in reader.iterShapeRecords():
                try:
                    geom = shape(sr.shape.__geo_interface__)
                    if geom.is_empty:
                        continue
                    if not geom.is_valid:
                        geom = geom.buffer(0)
                    geom = geometry_transform(to_wgs, geom)
                    props = {k: _json_property(v) for k, v in zip(fields, list(sr.record))}
                    kind = "rw" if ("SPRD_RW" in stem_upper or (not named_road_stems and geom.geom_type in {"Polygon", "MultiPolygon"})) else "manage"
                    layers[kind].append({"geometry": geom, "properties": props})
                except Exception:
                    continue

    # 실폭도로 폴리곤이 없는 공식 자료는 도로중심선의 ROAD_BT 등 폭원을
    # 이용해 예비 도로면을 만든다. 법정 접도율 자동확정에는 쓰지 않고
    # 프론트에서 ESTIMATE/REVIEW로 구분한다.
    road_mode = "real_width_polygon"
    if not layers["rw"] and layers["manage"]:
        derived: List[Dict[str, Any]] = []
        for row in layers["manage"]:
            width = _road_width_m(row["properties"])
            if width is None:
                continue
            polygon = _centerline_road_polygon(row["geometry"], width)
            if polygon is None:
                continue
            props = dict(row["properties"])
            props.update({
                "_derived_centerline": True,
                "_road_method": "centerline_width_buffer",
                "_width_m": width,
                "_width_source": "도로중심선+공식 폭원 버퍼(예비)",
            })
            derived.append({"geometry": polygon, "properties": props})
        if derived:
            layers["rw"] = derived
            road_mode = "centerline_width_buffer"

    for kind in ("rw", "manage"):
        geoms = [x["geometry"] for x in layers[kind]]
        layers[f"{kind}_tree"] = STRtree(geoms) if geoms else None
    layers.update({
        "available": bool(layers["rw"]),
        "source": "서버 내장 공식 실폭도로 TL_SPRD_RW" if road_mode == "real_width_polygon" else "공식 도로중심선+폭원 버퍼(예비)",
        "road_mode": road_mode,
        "file": os.path.basename(zip_path),
        "rw_count": len(layers["rw"]),
        "manage_count": len(layers["manage"]),
    })
    return layers


def analyze_road_intersections(geometry: Dict[str, Any]) -> Dict[str, Any]:
    layers = _road_spatial_layers()
    if not layers.get("available"):
        raise FileNotFoundError(str(layers.get("reason") or "서울 실폭도로 원본 미설치"))
    site = shape(geometry)
    if site.geom_type not in {"Polygon", "MultiPolygon"} or site.is_empty or not site.is_valid:
        raise ValueError("유효한 Polygon 또는 MultiPolygon 구역계가 필요합니다.")
    to_metric = Transformer.from_crs(4326, 5174, always_xy=True).transform
    to_wgs = Transformer.from_crs(5174, 4326, always_xy=True).transform
    query_area = geometry_transform(to_wgs, geometry_transform(to_metric, site).buffer(60))

    def selected(kind: str) -> List[Dict[str, Any]]:
        tree = layers.get(f"{kind}_tree")
        rows = layers.get(kind) or []
        if tree is None:
            return []
        out = []
        for idx in tree.query(query_area, predicate="intersects"):
            row = rows[int(idx)]
            out.append({
                "type": "Feature",
                "geometry": mapping(row["geometry"]),
                "properties": row["properties"],
            })
            if len(out) >= 5000:
                break
        return out

    return {
        "status": "matched",
        "rw": {"type": "FeatureCollection", "features": selected("rw")},
        "manage": {"type": "FeatureCollection", "features": selected("manage")},
        "metadata": {
            "source": layers.get("source"),
            "road_mode": layers.get("road_mode"),
            "file": layers.get("file"),
            "scope": "대상지 60m 버퍼",
            "official_original_required": True,
        },
    }


# ---------------------------------------------------------------------------
# Anonymous product analytics
# - No account, name, email, raw IP, or raw polygon is stored.
# - DATABASE_URL enables durable PostgreSQL storage.
# - Without DATABASE_URL, a clearly-labelled in-memory preview is used.
# ---------------------------------------------------------------------------
ANALYTICS_MEMORY = deque(maxlen=5000)
FEEDBACK_MEMORY = deque(maxlen=2000)
ANALYTICS_LOCK = threading.Lock()
ANALYTICS_DB_READY = False
ADMIN_SECURITY = HTTPBasic(auto_error=False)


def _database_url() -> str:
    return os.getenv("DATABASE_URL", "").strip()


def _analytics_storage_mode() -> str:
    return "postgres" if _database_url() else "memory"


def _ensure_analytics_table() -> None:
    global ANALYTICS_DB_READY
    if ANALYTICS_DB_READY or not _database_url():
        return
    if psycopg is None:
        raise RuntimeError("DATABASE_URL is configured but psycopg is not installed")
    with ANALYTICS_LOCK:
        if ANALYTICS_DB_READY:
            return
        with psycopg.connect(_database_url()) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS analytics_events (
                    id BIGSERIAL PRIMARY KEY,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    analysis_id VARCHAR(80),
                    visitor_id VARCHAR(80) NOT NULL,
                    session_id VARCHAR(80),
                    event_type VARCHAR(40) NOT NULL,
                    address_text TEXT,
                    pnu_list JSONB NOT NULL DEFAULT '[]'::jsonb,
                    area_m2 DOUBLE PRECISION,
                    parcel_count INTEGER,
                    centroid_lat DOUBLE PRECISION,
                    centroid_lng DOUBLE PRECISION,
                    recommendations JSONB NOT NULL DEFAULT '[]'::jsonb,
                    result_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
                    user_agent_group VARCHAR(40)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS analytics_events_created_idx ON analytics_events(created_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS analytics_events_visitor_idx ON analytics_events(visitor_id)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS feedback_reports (
                    id VARCHAR(36) PRIMARY KEY,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    analysis_id VARCHAR(80),
                    visitor_id VARCHAR(80) NOT NULL,
                    session_id VARCHAR(80),
                    category VARCHAR(30) NOT NULL,
                    message TEXT NOT NULL,
                    contact TEXT,
                    page_context VARCHAR(80),
                    address_text TEXT,
                    pnu_list JSONB NOT NULL DEFAULT '[]'::jsonb,
                    area_m2 DOUBLE PRECISION,
                    recommendations JSONB NOT NULL DEFAULT '[]'::jsonb,
                    status VARCHAR(20) NOT NULL DEFAULT 'open',
                    user_agent_group VARCHAR(40)
                )
            """)
            conn.execute("ALTER TABLE analytics_events ADD COLUMN IF NOT EXISTS analysis_id VARCHAR(80)")
            conn.execute("ALTER TABLE feedback_reports ADD COLUMN IF NOT EXISTS analysis_id VARCHAR(80)")
            conn.execute("CREATE INDEX IF NOT EXISTS feedback_reports_created_idx ON feedback_reports(created_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS feedback_reports_status_idx ON feedback_reports(status)")
            conn.commit()
        ANALYTICS_DB_READY = True


def _store_analytics_event(data: Dict[str, Any]) -> None:
    if _database_url():
        _ensure_analytics_table()
        with psycopg.connect(_database_url()) as conn:
            conn.execute("""
                INSERT INTO analytics_events
                (analysis_id, visitor_id, session_id, event_type, address_text, pnu_list,
                 area_m2, parcel_count, centroid_lat, centroid_lng,
                 recommendations, result_summary, user_agent_group)
                VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)
            """, (
                data.get("analysis_id"), data["visitor_id"], data.get("session_id"), data["event_type"],
                data.get("address_text"), json.dumps(data.get("pnu_list") or [], ensure_ascii=False),
                data.get("area_m2"), data.get("parcel_count"), data.get("centroid_lat"), data.get("centroid_lng"),
                json.dumps(data.get("recommendations") or [], ensure_ascii=False),
                json.dumps(data.get("result_summary") or {}, ensure_ascii=False), data.get("user_agent_group"),
            ))
            conn.commit()
    else:
        row = dict(data)
        row["created_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        with ANALYTICS_LOCK:
            ANALYTICS_MEMORY.appendleft(row)


def _analytics_rows(limit: int = 500) -> List[Dict[str, Any]]:
    if _database_url():
        _ensure_analytics_table()
        with psycopg.connect(_database_url()) as conn:
            rows = conn.execute("""
                SELECT created_at, analysis_id, visitor_id, session_id, event_type, address_text,
                       pnu_list, area_m2, parcel_count, centroid_lat, centroid_lng,
                       recommendations, result_summary, user_agent_group
                FROM analytics_events ORDER BY created_at DESC LIMIT %s
            """, (limit,)).fetchall()
        keys = ["created_at","analysis_id","visitor_id","session_id","event_type","address_text","pnu_list","area_m2","parcel_count","centroid_lat","centroid_lng","recommendations","result_summary","user_agent_group"]
        return [dict(zip(keys, row)) for row in rows]
    with ANALYTICS_LOCK:
        return list(ANALYTICS_MEMORY)[:limit]


def _store_feedback(data: Dict[str, Any]) -> str:
    feedback_id = str(uuid.uuid4())
    row = dict(data)
    row.update({
        "id": feedback_id,
        "status": "open",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    })
    if _database_url():
        _ensure_analytics_table()
        with psycopg.connect(_database_url()) as conn:
            conn.execute("""
                INSERT INTO feedback_reports
                (id, analysis_id, visitor_id, session_id, category, message, contact, page_context,
                 address_text, pnu_list, area_m2, recommendations, status, user_agent_group)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,'open',%s)
            """, (
                feedback_id, data.get("analysis_id"), data["visitor_id"], data.get("session_id"), data["category"],
                data["message"], data.get("contact"), data.get("page_context"), data.get("address_text"),
                json.dumps(data.get("pnu_list") or [], ensure_ascii=False), data.get("area_m2"),
                json.dumps(data.get("recommendations") or [], ensure_ascii=False), data.get("user_agent_group"),
            ))
            conn.commit()
    else:
        with ANALYTICS_LOCK:
            FEEDBACK_MEMORY.appendleft(row)
    return feedback_id


def _feedback_rows(limit: int = 1000) -> List[Dict[str, Any]]:
    if _database_url():
        _ensure_analytics_table()
        with psycopg.connect(_database_url()) as conn:
            rows = conn.execute("""
                SELECT id, created_at, updated_at, analysis_id, visitor_id, session_id, category, message,
                       contact, page_context, address_text, pnu_list, area_m2,
                       recommendations, status, user_agent_group
                FROM feedback_reports ORDER BY created_at DESC LIMIT %s
            """, (limit,)).fetchall()
        keys = ["id","created_at","updated_at","analysis_id","visitor_id","session_id","category","message","contact","page_context","address_text","pnu_list","area_m2","recommendations","status","user_agent_group"]
        return [dict(zip(keys, row)) for row in rows]
    with ANALYTICS_LOCK:
        return list(FEEDBACK_MEMORY)[:limit]


def _set_feedback_status(feedback_id: str, status: str) -> bool:
    if _database_url():
        _ensure_analytics_table()
        with psycopg.connect(_database_url()) as conn:
            result = conn.execute(
                "UPDATE feedback_reports SET status=%s, updated_at=NOW() WHERE id=%s",
                (status, feedback_id),
            )
            conn.commit()
            return result.rowcount > 0
    with ANALYTICS_LOCK:
        for row in FEEDBACK_MEMORY:
            if row.get("id") == feedback_id:
                row["status"] = status
                row["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                return True
    return False


def _admin_auth(credentials: Optional[HTTPBasicCredentials] = Depends(ADMIN_SECURITY)) -> bool:
    configured = os.getenv("ADMIN_PASSWORD", "")
    if not configured:
        raise HTTPException(status_code=503, detail="ADMIN_PASSWORD environment variable is not configured")
    supplied = credentials.password if credentials else ""
    user = credentials.username if credentials else ""
    if not (hmac.compare_digest(user.encode(), b"admin") and hmac.compare_digest(supplied.encode(), configured.encode())):
        raise HTTPException(status_code=401, detail="Admin authentication required", headers={"WWW-Authenticate": "Basic"})
    return True


SEOUL_OPEN_DATA_BASE = "http://openapi.seoul.go.kr:8088"
# 서울시 공공의료 공식 페이지(시립병원 건강돌봄 네트워크, 2024-03-18)에
# 열거된 서울 소재 시립병원 명칭.  병원 인허가 API의 업태가 '병원'인 경우에만
# 시립병원 후보로 사용하며, 종합병원은 별도 법정 유형으로 분류한다.
SEOUL_MUNICIPAL_HOSPITAL_NAMES = {
    "서울의료원", "보라매병원", "서남병원", "서북병원", "북부병원", "동부병원",
    "어린이병원", "은평병원",
}

SEOUL_OPEN_DATA_KEY_ENV_NAMES = (
    "SEOUL_OPEN_DATA_KEY",
    "data.seoul.go.kr_KEY",  # Render에 기존 등록된 이름도 그대로 지원
    "DATA_SEOUL_GO_KR_KEY",
)

def _seoul_open_data_key_info() -> tuple[str, str]:
    """Return the configured Seoul Open Data key and the env-var name only.

    The key value itself is never exposed in API responses/logs.  v2.5.0 초기
    배포본은 SEOUL_OPEN_DATA_KEY만 읽었지만 실제 Render 환경에는
    data.seoul.go.kr_KEY 이름으로 등록되어 있었으므로 두 이름을 모두
    허용한다.
    """
    for env_name in SEOUL_OPEN_DATA_KEY_ENV_NAMES:
        value = os.getenv(env_name, "").strip()
        if value:
            return value, env_name
    return "", ""

def _seoul_open_data_key() -> str:
    return _seoul_open_data_key_info()[0]

def _seoul_open_data_rows(service: str, limit: int = 10000) -> List[Dict[str, Any]]:
    """Read Seoul Open Data rows without turning API uncertainty into a PASS.

    Seoul Open Data sometimes returns API errors in a top-level RESULT object
    rather than under the requested service key.  Treat that as an explicit
    error instead of silently returning zero rows, because zero rows must never
    be mistaken for 'no nearby facility'.
    """
    key = _seoul_open_data_key()
    if not key:
        return []
    rows: List[Dict[str, Any]] = []
    start = 1
    page = 1000
    while start <= limit:
        end = min(start + page - 1, limit)
        url = f"{SEOUL_OPEN_DATA_BASE}/{quote(key, safe='')}/json/{service}/{start}/{end}/"
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        try:
            payload = resp.json()
        except Exception as exc:
            raise RuntimeError(f"서울 열린데이터광장 {service} JSON 응답 해석 실패") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"서울 열린데이터광장 {service} 응답 형식 오류")

        top_result = payload.get("RESULT") or {}
        top_code = str(top_result.get("CODE") or "") if isinstance(top_result, dict) else ""
        if top_code and top_code not in {"INFO-000", "INFO-200"}:
            raise RuntimeError(f"서울 열린데이터광장 {service} 오류: {top_code} {top_result.get('MESSAGE','')}")

        body = payload.get(service)
        if not isinstance(body, dict):
            # 서비스명 대소문자는 API에서 중요하지만 응답 wrapper는 간혹
            # 표기가 달라질 수 있어 case-insensitive로 한 번 더 찾는다.
            body = next((v for k, v in payload.items() if str(k).lower() == service.lower() and isinstance(v, dict)), None)
        if not isinstance(body, dict):
            keys = ", ".join(str(k) for k in list(payload.keys())[:5])
            raise RuntimeError(f"서울 열린데이터광장 {service} 응답에 서비스 블록 없음 ({keys or 'empty'})")

        result = body.get("RESULT") or {}
        code = str(result.get("CODE") or "") if isinstance(result, dict) else ""
        if code and code not in {"INFO-000", "INFO-200"}:
            raise RuntimeError(f"서울 열린데이터광장 {service} 오류: {code} {result.get('MESSAGE','')}")
        page_rows = body.get("row") or []
        if not isinstance(page_rows, list):
            raise RuntimeError(f"서울 열린데이터광장 {service} row 형식 오류")
        rows.extend(x for x in page_rows if isinstance(x, dict))
        try:
            total = int(str(body.get("list_total_count") or len(rows)).replace(",", ""))
        except ValueError:
            total = len(rows)
        if not page_rows or end >= total:
            break
        start = end + 1
    return rows

def _name_key(value: Any) -> str:
    return re.sub(r"[^가-힣A-Za-z0-9]", "", str(value or "")).lower()

def _safe_medical_reference(geometry: Dict[str, Any]) -> Dict[str, Any]:
    """Return official *reference points* near a site for the safe-housing screen.

    The OpenAPI rows provide point coordinates.  The safe-housing legal test is
    measured from the eligible medical facility *site boundary*, so these points
    are map/evidence candidates only and can never create an automatic PASS.
    """
    try:
        site_wgs = _polygonal_only(shape(geometry))
    except Exception as exc:
        raise ValueError(f"구역계 GeoJSON을 읽을 수 없습니다: {exc}") from exc
    if site_wgs is None or site_wgs.is_empty:
        raise ValueError("구역계는 Polygon 또는 MultiPolygon이어야 합니다.")
    to_metric = Transformer.from_crs(4326, 5174, always_xy=True)
    to_wgs = Transformer.from_crs(5174, 4326, always_xy=True)
    site_metric = geometry_transform(to_metric.transform, site_wgs)

    key, key_env = _seoul_open_data_key_info()
    metadata = {
        "criterion": "의료시설 대상부지 경계로부터 350m",
        "auto_pass_eligible": False,
        "reason": "공개 API는 의료시설 위치점 중심이며 법정 기준인 대상부지 경계도형이 아니므로 자동 PASS에 사용하지 않음",
        "hospital_source": "서울시 병원 인허가 정보 (LOCALDATA_010101, 매일 갱신)",
        "health_center_source": "서울시 의원 인허가 정보 (LOCALDATA_010102, 매일 갱신; 보건소만 선별)",
        "health_center_fallback": "서울시 시설물 정보 (tbEntranceItem, 2023 일회성) — LOCALDATA_010102 실패 시 위치 참고용",
        "official_rule": "서울특별시 안심주택 공급 지원에 관한 조례 제2조 및 안심주택 건립·운영기준 1-3-2",
        "credential_env": key_env or None,
    }
    if not key:
        return {
            "status": "unavailable", "items": [], "metadata": metadata,
            "auto_pass_eligible": False,
            "message": "서울 열린데이터광장 인증키 미설정 · SEOUL_OPEN_DATA_KEY 또는 data.seoul.go.kr_KEY 확인",
            "errors": ["서울 열린데이터광장 인증키 환경변수를 찾지 못했습니다."],
            "warnings": [], "source_stats": {},
        }

    def _row_is_active(row: Dict[str, Any]) -> bool:
        state = " ".join(str(row.get(k) or "") for k in ("TRDSTATENM", "DTLSTATENM"))
        return not any(token in state for token in ("폐업", "취소", "말소", "휴업"))

    def _row_point_5174(row: Dict[str, Any]) -> tuple[float, float, float, float, float]:
        x, y = float(row.get("X")), float(row.get("Y"))
        if not (50000 <= x <= 400000 and 300000 <= y <= 700000):
            raise ValueError("EPSG:5174 범위를 벗어난 좌표")
        pt = shape({"type": "Point", "coordinates": [x, y]})
        dist = float(site_metric.distance(pt))
        lon, lat = to_wgs.transform(x, y)
        if not (124 <= lon <= 132 and 33 <= lat <= 39):
            raise ValueError("WGS84 변환 결과가 국내 범위를 벗어남")
        return x, y, lon, lat, dist

    items: List[Dict[str, Any]] = []
    errors: List[str] = []
    warnings: List[str] = []
    stats: Dict[str, Any] = {}

    # 1) 종합병원 + 서울시 관리 시립병원: 병원 인허가 API
    try:
        hospital_rows = _seoul_open_data_rows("LOCALDATA_010101", 5000)
        municipal_keys = {_name_key(x) for x in SEOUL_MUNICIPAL_HOSPITAL_NAMES}
        eligible_total = 0
        coord_skipped = 0
        for row in hospital_rows:
            if not _row_is_active(row):
                continue
            type_name = str(row.get("METRORGASSRNM") or row.get("UPTAENM") or "").strip()
            name = str(row.get("BPLCNM") or "").strip()
            category = None
            if type_name == "종합병원" or str(row.get("UPTAENM") or "").strip() == "종합병원":
                category = "general_hospital"
            elif any(k and k in _name_key(name) for k in municipal_keys):
                # 종합병원이 아닌 시립병원도 안심주택 의료시설 후보로 별도 보존.
                category = "municipal_hospital"
            if not category:
                continue
            eligible_total += 1
            try:
                _, _, lon, lat, dist = _row_point_5174(row)
            except Exception:
                coord_skipped += 1
                continue
            if dist > 1500:
                continue
            items.append({
                "category": category, "name": name or "의료시설", "distance_point_m": round(dist, 1),
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "address": str(row.get("RDNWHLADDR") or row.get("SITEWHLADDR") or ""),
                "data_status": "current_reference_point", "auto_pass_eligible": False,
                "source": "서울시 병원 인허가 정보", "source_service": "LOCALDATA_010101",
                "facility_type": type_name,
            })
        stats["hospital"] = {
            "service": "LOCALDATA_010101", "rows": len(hospital_rows),
            "eligible_total": eligible_total, "coordinate_skipped": coord_skipped,
        }
    except Exception as exc:
        errors.append(f"병원 인허가 LOCALDATA_010101: {exc}")
        stats["hospital"] = {"service": "LOCALDATA_010101", "error": str(exc)}

    # 2) 보건소: 의원 인허가 API의 '보건소' 유형을 우선 사용한다.
    #    이 데이터는 매일 갱신되며 EPSG:5174 좌표를 제공한다.
    health_center_found = 0
    clinic_failed = False
    try:
        clinic_rows = _seoul_open_data_rows("LOCALDATA_010102", 5000)
        eligible_total = 0
        coord_skipped = 0
        for row in clinic_rows:
            if not _row_is_active(row):
                continue
            name = str(row.get("BPLCNM") or "").strip()
            type_name = " ".join(filter(None, [
                str(row.get("METRORGASSRNM") or "").strip(),
                str(row.get("UPTAENM") or "").strip(),
            ])).strip()
            probe = f"{type_name} {name}"
            if "보건소" not in probe or "보건지소" in probe:
                continue
            eligible_total += 1
            try:
                _, _, lon, lat, dist = _row_point_5174(row)
            except Exception:
                coord_skipped += 1
                continue
            health_center_found += 1
            if dist > 1500:
                continue
            items.append({
                "category": "public_health_center", "name": name or "보건소", "distance_point_m": round(dist, 1),
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "address": str(row.get("RDNWHLADDR") or row.get("SITEWHLADDR") or ""),
                "data_status": "current_reference_point", "auto_pass_eligible": False,
                "source": "서울시 의원 인허가 정보", "source_service": "LOCALDATA_010102",
                "facility_type": type_name or "보건소",
            })
        stats["health_center"] = {
            "service": "LOCALDATA_010102", "rows": len(clinic_rows),
            "eligible_total": eligible_total, "coordinate_skipped": coord_skipped,
        }
        if eligible_total == 0:
            warnings.append("LOCALDATA_010102 응답에서 보건소 유형을 찾지 못해 시설물 자료를 보조조회합니다.")
    except Exception as exc:
        clinic_failed = True
        warnings.append(f"보건소 최신 인허가 LOCALDATA_010102 조회 실패: {exc}")
        stats["health_center"] = {"service": "LOCALDATA_010102", "error": str(exc)}

    # 3) 보건소 보조자료: 최신 인허가 API가 실패하거나 보건소 행이 없을 때만 사용.
    #    2023년 일회성 자료이므로 위치 참고 외에는 사용하지 않는다.
    if clinic_failed or health_center_found == 0:
        try:
            facility_rows = _seoul_open_data_rows("tbEntranceItem", 10000)
            fallback_total = 0
            for row in facility_rows:
                usage = str(row.get("FCLT_USG_SE") or "")
                name = str(row.get("FCLT_NM") or "").strip()
                if "보건소" not in usage and "보건소" not in name:
                    continue
                if "보건지소" in usage or "보건지소" in name:
                    continue
                fallback_total += 1
                try:
                    lat, lon = float(row.get("LAT")), float(row.get("LOT"))
                    x, y = to_metric.transform(lon, lat)
                    pt = shape({"type": "Point", "coordinates": [x, y]})
                    dist = float(site_metric.distance(pt))
                except Exception:
                    continue
                if dist > 1500:
                    continue
                items.append({
                    "category": "public_health_center", "name": name or "보건소", "distance_point_m": round(dist, 1),
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                    "address": str(row.get("RDN_ADDR") or row.get("LOTNO_ADDR") or ""),
                    "data_status": "stale_reference_point_2023", "auto_pass_eligible": False,
                    "source": "서울시 시설물 정보(2023 일회성)", "source_service": "tbEntranceItem",
                    "facility_type": usage or "보건소",
                })
            stats["health_center_fallback"] = {
                "service": "tbEntranceItem", "rows": len(facility_rows), "eligible_total": fallback_total,
            }
        except Exception as exc:
            errors.append(f"보건소 보조자료 tbEntranceItem: {exc}")
            stats["health_center_fallback"] = {"service": "tbEntranceItem", "error": str(exc)}

    # 동일 시설이 주/보조 API에 함께 잡히는 경우 지도 중복표시 방지.
    deduped: Dict[tuple[str, str, int, int], Dict[str, Any]] = {}
    for item in items:
        coords = (item.get("geometry") or {}).get("coordinates") or [0, 0]
        key_tuple = (
            str(item.get("category") or ""), _name_key(item.get("name")),
            int(round(float(coords[0]) * 100000)), int(round(float(coords[1]) * 100000)),
        )
        old = deduped.get(key_tuple)
        if old is None or str(item.get("data_status")) == "current_reference_point":
            deduped[key_tuple] = item
    items = list(deduped.values())
    items.sort(key=lambda x: (float(x.get("distance_point_m") or 1e12), str(x.get("category")), str(x.get("name"))))

    nearby_counts = {
        "general_hospital": sum(1 for x in items if x.get("category") == "general_hospital"),
        "municipal_hospital": sum(1 for x in items if x.get("category") == "municipal_hospital"),
        "public_health_center": sum(1 for x in items if x.get("category") == "public_health_center"),
    }
    return {
        "status": "reference" if items else ("error" if errors else "none"),
        "auto_pass_eligible": False,
        "items": items[:30],
        "metadata": metadata,
        "errors": errors,
        "warnings": warnings,
        "source_stats": stats,
        "nearby_counts": nearby_counts,
        "message": "공식 위치점은 후보검색용이며 의료시설 대상부지 경계 350m는 결정도서·공부로 재확인해야 합니다.",
    }


app = FastAPI(
    title="도시검토 플랫폼 - 서울 재개발 웹 MVP",
    version="2.5.0",
    description="구역계 자동분석 + 서울 정비·개발 14개 사업방식 Rule Engine",
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


class AnalyticsEventInput(BaseModel):
    analysis_id: Optional[str] = Field(None, min_length=8, max_length=80)
    visitor_id: str = Field(..., min_length=8, max_length=80)
    session_id: Optional[str] = Field(None, max_length=80)
    event_type: str = Field(..., pattern="^(page_view|analysis_complete|detail_open|simulation_open|report_open)$")
    address_text: Optional[str] = Field(None, max_length=1000)
    pnu_list: List[str] = Field(default_factory=list, max_length=200)
    area_m2: Optional[float] = Field(None, ge=0)
    parcel_count: Optional[int] = Field(None, ge=0)
    centroid_lat: Optional[float] = Field(None, ge=-90, le=90)
    centroid_lng: Optional[float] = Field(None, ge=-180, le=180)
    recommendations: List[Dict[str, Any]] = Field(default_factory=list, max_length=10)
    result_summary: Dict[str, Any] = Field(default_factory=dict)


class AdminVisitorInput(BaseModel):
    visitor_id: Optional[str] = Field(None, min_length=8, max_length=80)


class FeedbackInput(BaseModel):
    analysis_id: Optional[str] = Field(None, min_length=8, max_length=80)
    visitor_id: str = Field(..., min_length=8, max_length=80)
    session_id: Optional[str] = Field(None, max_length=80)
    category: str = Field(..., pattern="^(data|decision|screen|suggestion|other)$")
    message: str = Field(..., min_length=2, max_length=4000)
    contact: Optional[str] = Field(None, max_length=200)
    page_context: Optional[str] = Field(None, max_length=80)
    address_text: Optional[str] = Field(None, max_length=1000)
    pnu_list: List[str] = Field(default_factory=list, max_length=200)
    area_m2: Optional[float] = Field(None, ge=0)
    recommendations: List[Dict[str, Any]] = Field(default_factory=list, max_length=10)


class FeedbackStatusInput(BaseModel):
    status: str = Field(..., pattern="^(open|checking|done)$")


def _user_agent_group(request: Request) -> str:
    ua = request.headers.get("user-agent", "").lower()
    device = "mobile" if any(x in ua for x in ("android", "iphone", "mobile")) else "desktop"
    browser = "edge" if "edg/" in ua else "chrome" if "chrome/" in ua else "safari" if "safari/" in ua else "firefox" if "firefox/" in ua else "other"
    return f"{device}/{browser}"


@app.post("/api/analytics/events")
def analytics_event(payload: AnalyticsEventInput, request: Request):
    if request.cookies.get("urban_admin_exclude") == "1":
        return {"ok": True, "excluded": True}
    data = payload.model_dump()
    data["pnu_list"] = [str(x)[:19] for x in data.get("pnu_list", [])[:200]]
    data["recommendations"] = data.get("recommendations", [])[:10]
    data["user_agent_group"] = _user_agent_group(request)
    try:
        _store_analytics_event(data)
    except Exception:
        logging.exception("analytics event storage failed")
        return JSONResponse(status_code=202, content={"ok": False, "stored": False})
    return {"ok": True, "stored": True, "storage": _analytics_storage_mode()}


@app.post("/api/feedback")
def create_feedback(payload: FeedbackInput, request: Request):
    data = payload.model_dump()
    data["pnu_list"] = [str(x)[:19] for x in data.get("pnu_list", [])[:200]]
    data["recommendations"] = data.get("recommendations", [])[:10]
    data["contact"] = (data.get("contact") or "").strip() or None
    data["user_agent_group"] = _user_agent_group(request)
    try:
        feedback_id = _store_feedback(data)
    except Exception:
        logging.exception("feedback storage failed")
        raise HTTPException(status_code=500, detail="의견을 저장하지 못했습니다.")
    return {"ok": True, "feedback_id": feedback_id, "storage": _analytics_storage_mode()}


@app.post("/admin/feedback/{feedback_id}/status")
def update_feedback_status(feedback_id: str, payload: FeedbackStatusInput, _: bool = Depends(_admin_auth)):
    if not _set_feedback_status(feedback_id, payload.status):
        raise HTTPException(status_code=404, detail="Feedback not found")
    return {"ok": True, "status": payload.status}


@app.post("/admin/exclude-me")
def admin_exclude_me(payload: AdminVisitorInput, response: Response, _: bool = Depends(_admin_auth)):
    if payload.visitor_id:
        if _database_url():
            _ensure_analytics_table()
            with psycopg.connect(_database_url()) as conn:
                conn.execute("DELETE FROM analytics_events WHERE visitor_id=%s", (payload.visitor_id,))
                conn.commit()
        else:
            with ANALYTICS_LOCK:
                kept=[r for r in ANALYTICS_MEMORY if r.get("visitor_id") != payload.visitor_id]
                ANALYTICS_MEMORY.clear();ANALYTICS_MEMORY.extend(kept)
    response.set_cookie("urban_admin_exclude", "1", max_age=60 * 60 * 24 * 365 * 5, httponly=True, secure=True, samesite="lax")
    return {"ok": True, "message": "This browser is excluded and its earlier anonymous events were removed."}


@app.post("/admin/include-me")
def admin_include_me(response: Response, _: bool = Depends(_admin_auth)):
    response.delete_cookie("urban_admin_exclude")
    return {"ok": True, "message": "This browser is included in analytics."}


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, _: bool = Depends(_admin_auth)):
    rows = _analytics_rows(5000)
    feedback = _feedback_rows(2000)
    analyses = [r for r in rows if r.get("event_type") == "analysis_complete"]
    visitors = {r.get("visitor_id") for r in rows if r.get("visitor_id")}
    analysis_visitors = {r.get("visitor_id") for r in analyses if r.get("visitor_id")}
    today = datetime.now().astimezone().date()
    def row_date(r):
        v = r.get("created_at")
        if isinstance(v, datetime): return v.astimezone().date()
        try: return datetime.fromisoformat(str(v)).astimezone().date()
        except Exception: return None
    today_analyses = sum(1 for r in analyses if row_date(r) == today)
    open_feedback = sum(1 for r in feedback if r.get("status") != "done")
    road_installed = bool(_road_zip_path())
    excluded = request.cookies.get("urban_admin_exclude") == "1"
    table_rows = []
    for r in analyses[:300]:
        created = r.get("created_at")
        if isinstance(created, datetime): created = created.astimezone().strftime("%Y-%m-%d %H:%M")
        recs = r.get("recommendations") or []
        if isinstance(recs, str):
            try: recs = json.loads(recs)
            except Exception: recs = []
        rec_text = " / ".join(str(x.get("name") or x.get("scheme") or "") for x in recs[:3] if isinstance(x, dict)) or "추천 없음"
        pnus = r.get("pnu_list") or []
        if isinstance(pnus, str):
            try: pnus = json.loads(pnus)
            except Exception: pnus = []
        lat, lng = r.get("centroid_lat"), r.get("centroid_lng")
        map_link = f'<a href="https://map.kakao.com/link/map/{lat},{lng}" target="_blank">지도</a>' if lat is not None and lng is not None else "-"
        table_rows.append(f"""
          <tr><td>{html.escape(str(created))}</td><td><code>{html.escape(str(r.get('analysis_id') or '-'))}</code></td><td><code>{html.escape(str(r.get('visitor_id',''))[-10:])}</code></td>
          <td>{html.escape(str(r.get('address_text') or '-'))}</td><td>{float(r.get('area_m2') or 0):,.0f}㎡</td>
          <td>{int(r.get('parcel_count') or 0)}필지</td><td>{html.escape(rec_text)}</td><td>{map_link}</td>
          <td><details><summary>{len(pnus)}개 PNU</summary>{'<br>'.join(html.escape(str(x)) for x in pnus)}</details></td></tr>
        """)
    category_labels = {"data":"데이터 오류", "decision":"판정 오류", "screen":"화면 오류", "suggestion":"기능 제안", "other":"기타"}
    status_labels = {"open":"접수", "checking":"확인 중", "done":"처리완료"}
    feedback_rows = []
    for r in feedback[:500]:
        created = r.get("created_at")
        if isinstance(created, datetime):
            created = created.astimezone().strftime("%Y-%m-%d %H:%M")
        status = str(r.get("status") or "open")
        options = "".join(
            f'<option value="{key}"{" selected" if key == status else ""}>{label}</option>'
            for key, label in status_labels.items()
        )
        feedback_rows.append(f"""
          <tr><td>{html.escape(str(created))}</td><td><code>{html.escape(str(r.get('analysis_id') or '-'))}</code></td><td>{html.escape(category_labels.get(str(r.get('category')), str(r.get('category') or '-')))}</td>
          <td class="wrap">{html.escape(str(r.get('message') or '-'))}</td><td class="wrap">{html.escape(str(r.get('address_text') or '-'))}</td>
          <td>{float(r.get('area_m2') or 0):,.0f}㎡</td><td>{html.escape(str(r.get('contact') or '-'))}</td>
          <td><select onchange="setFeedbackStatus('{html.escape(str(r.get('id') or ''))}',this.value)">{options}</select></td></tr>
        """)
    storage_note = "PostgreSQL 영구저장" if _analytics_storage_mode() == "postgres" else "⚠ 메모리 임시저장 · 재시작/배포 시 삭제 · DATABASE_URL 필요"
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>도시검토 관리자</title><style>
    body{{font-family:system-ui,'Noto Sans KR',sans-serif;margin:0;background:#f3f5f7;color:#101828}}header{{padding:18px 24px;background:#101828;color:white;display:flex;justify-content:space-between;align-items:center}}main{{padding:18px;max-width:1500px;margin:auto}}.cards{{display:grid;grid-template-columns:repeat(6,1fr);gap:12px}}.card{{background:white;border:1px solid #e4e7ec;border-radius:12px;padding:16px}}.card span{{font-size:12px;color:#667085}}.card b{{display:block;font-size:26px;margin-top:5px}}.tools{{margin:14px 0;display:flex;gap:8px;align-items:center;flex-wrap:wrap}}button,select{{padding:9px 12px;border:1px solid #d0d5dd;border-radius:8px;background:white;font-weight:700;cursor:pointer}}.warn{{color:#b54708}}.table{{overflow:auto;background:white;border:1px solid #e4e7ec;border-radius:12px;margin-bottom:24px}}table{{border-collapse:collapse;width:100%;font-size:12px}}th,td{{padding:9px;border-bottom:1px solid #eaecf0;text-align:left;vertical-align:top;white-space:nowrap}}th{{background:#f9fafb;position:sticky;top:0}}td.wrap{{white-space:normal;min-width:260px;line-height:1.5}}code{{font-size:11px}}@media(max-width:900px){{.cards{{grid-template-columns:1fr 1fr}}}}
    </style><script>function setFeedbackStatus(id,status){{fetch('/admin/feedback/'+encodeURIComponent(id)+'/status',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{status}})}}).then(r=>{{if(!r.ok)throw new Error();}}).catch(()=>alert('처리상태 저장 실패'));}}</script></head><body><header><div><b>도시검토 관리자</b><div style="font-size:11px;opacity:.75">{storage_note}</div></div><a href="/" style="color:white">서비스로</a></header><main>
    <div class="cards"><div class="card"><span>전체 익명 방문자</span><b>{len(visitors):,}</b></div><div class="card"><span>분석 실행 방문자</span><b>{len(analysis_visitors):,}</b></div><div class="card"><span>총 분석 실행</span><b>{len(analyses):,}</b></div><div class="card"><span>오늘 분석</span><b>{today_analyses:,}</b></div><div class="card"><span>미처리 오류·의견</span><b>{open_feedback:,}</b></div><div class="card"><span>공식 도로 GIS</span><b>{'설치됨' if road_installed else '미설치'}</b></div></div>
    <div class="tools"><button onclick="fetch('/admin/exclude-me',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{visitor_id:localStorage.getItem('urban_visitor_id_v1')}})}}).then(()=>location.reload())">이 브라우저·기존기록 통계 제외</button><button onclick="fetch('/admin/include-me',{{method:'POST'}}).then(()=>location.reload())">앞으로 통계 다시 포함</button><span class="{'warn' if excluded else ''}">{'현재 관리자 브라우저는 통계에서 제외됩니다.' if excluded else '현재 브라우저도 통계에 포함됩니다.'}</span></div>
    <h2>최근 대상지 분석</h2><div class="table"><table><thead><tr><th>시각</th><th>분석번호</th><th>익명사용자</th><th>입력주소</th><th>면적</th><th>필지</th><th>추천결과</th><th>위치</th><th>PNU</th></tr></thead><tbody>{''.join(table_rows) or '<tr><td colspan="9">아직 분석 기록이 없습니다.</td></tr>'}</tbody></table></div>
    <h2>오류·개선의견</h2><div class="table"><table><thead><tr><th>접수시각</th><th>분석번호</th><th>유형</th><th>내용</th><th>대상지</th><th>면적</th><th>연락처</th><th>처리상태</th></tr></thead><tbody>{''.join(feedback_rows) or '<tr><td colspan="8">접수된 오류·의견이 없습니다.</td></tr>'}</tbody></table></div>
    </main></body></html>"""


@app.get("/", response_class=HTMLResponse)
def home():
    # VWorld 공식 웹 샘플처럼 브라우저에서 Data API를 직접 호출한다.
    # 키는 GitHub 소스에는 없고 Render 환경변수에서 런타임에 주입된다.
    return _index_html().replace("__VWORLD_CLIENT_KEY__", _vworld_key())


@app.get("/api/reference/stations")
def reference_stations():
    """내장 지하철역사 기준자료. API 키나 원본 SHP 파일을 외부에 노출하지 않습니다."""
    return _station_reference_data()


@app.get("/api/reference/centers")
def reference_centers():
    """서울시 중심지체계 도형 기준자료."""
    return _center_reference_data()


@app.get("/api/reference/renewal-zones")
def reference_renewal_zones():
    """서울시 공식 SHP 기반 정비구역·사업구역 참고도형."""
    return _renewal_reference_data()


@app.get("/health")
def health():
    return {
        "ok": True,
        "app": "seoul_urban_renewal_platform_v2.5.0",
        "engine": RULES["rule_set_id"],
        "map": "leaflet-draw",
        "vworld_configured": vworld_ready(),
        "analytics_storage": _analytics_storage_mode(),
        "admin_configured": bool(os.getenv("ADMIN_PASSWORD", "")),
        "vworld_domain": _vworld_domain() if vworld_ready() else None,
        "parcel_auto": "browser_direct_ready" if vworld_ready() else "needs_VWORLD_API_KEY",
        "building_spatial_auto": "LT_C_SPBD_browser_direct_ready" if vworld_ready() else "needs_VWORLD_API_KEY",
        "building_hub": "ready" if building_hub_ready() else "needs_BUILDING_HUB_API_KEY",
        "land_ledger": "ladfrlList + getLandCharacteristics + geometry provisional",
        "road_access": "official real-width preferred; centerline+width provisional REVIEW fallback",
        "analysis_object_model": "parcel/building common ledger retained for station-area/zoning/mixed-use expansion",
        "redevelopment_strategy": "scheme-specific legal aging facts + area/aging/additional-entry AND-OR gates",
        "scheme_sheets": ["housing_redevelopment","reconstruction","urban_redevelopment","residential_environment","smallscale_housing","general_housing","station_activation","growth_potential","safe_housing","shared_housing","station_complex_district","longterm_lease","public_housing_complex","urban_complex_innovation"],
        "scheme_age_stats": "BuildingHUB raw facts -> urban-planning / urban-renewal / policy-specific derived aging facts; unknowns remain bounded REVIEW",
        "density_public_contribution": "14-scheme zoning/FAR/public-contribution simultaneous review",
        "scheme_ui": "14-scheme simultaneous matrix plus fourteen visible detail sheets",
        "station_boundary_gis": "embedded MOIS 2026-08 TL_SPSB_STATN + entrc station-name matching + Seoul center hierarchy + VWorld line fallback",
        "first_screen": "boundary-first automatic analysis + 14 candidate schemes + location map + compact land/building rail",
        "location_map": "boundary-only main map; parcel/building diagrams rendered in compact side mini maps",
        "reconstruction_gate": "requires apartment-complex evidence or explicit reconstruction target confirmation",
        "site_status_card": "neutral raw land/building facts + visible regime-specific aging facts + scheme-specific supplemental facts",
        "planning_gis": "VWorld zoning/district/facility/district-unit-plan polygon intersection engine",
        "renewal_gis": "server-side UQ181/UQ120 intersection; legal-priority; promotion separate; full matched boundaries returned for status map",
        "development_gis": "VWorld district-unit plan + bundled Seoul UQ181 urban-development/public-housing/other legal project intersections",
        "safe_housing_location_paths": "station / arterial-road-side / medical-facility-center evaluated separately; OR combined",
        "safe_medical_reference": "Seoul hospital + clinic licensing current points (REVIEW only; site-boundary geometry still required)" if _seoul_open_data_key() else "needs Seoul Open Data key; no automatic medical PASS",
        "safe_medical_key_env": _seoul_open_data_key_info()[1] or None,
        "road_width_gis": "server bundled official ZIP" if _road_zip_path() else "official ZIP hook ready; data/road_seoul.zip not installed",
        "responsive_ui": "desktop/tablet/mobile responsive layout with mobile workflow and selected-scheme cards",
        "smallscale_group": "block renewal/autonomous renewal/small-scale reconstruction/Moa Town alternative group",
        "workspace_ui": "three-column location/spatial evidence/integrated status layout; all decision facts surface in spatial-status boxes",
        "boundary_input_ui": "draw polygon or input Seoul gu/dong/jibun parcel addresses",
        "mini_map_hierarchy": "strong in-site features with thin surrounding spatial context",
        "house_density": "excluded_from_primary_redevelopment_screening",
        "parcel_boundary_editor": "pnu_list_click_include_exclude_nearby_union",
        "scheme_architecture": "site facts -> scheme-specific facts -> independent scheme evaluation -> review sheet -> priority comparison",
        "scheme_module_api": "2026-08-28-v5-nine-independent-five-shells",
        "independent_scheme_modules": "activation / growth_potential / safe_housing / shared_housing / station_complex / longterm / public_complex / innovation / urban_redevelopment; redevelopment / reconstruction / residential_environment / smallscale / general_housing are shell-only until redesigned",
        "scheme_specific_spatial_checks": "scheme module may request additional official spatial facts; missing facts remain REVIEW, never inferred PASS",
        "spatial_evidence_maps": "common cadastral base + colored zoning + scheme-specific road/frontage facts + safe-housing medical reference; map facts and scheme facts share one Fact Store",
        "purpose_filter": "safe-housing rule module runs only when purpose=housing_rental; other schemes keep existing purpose/candidate logic",
        "provenance_ui": True,
    }


@app.post("/api/spatial/measure")
def spatial_measure(inp: GeometryInput):
    try:
        return measure_geojson(inp.geometry)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/spatial/renewal-intersections")
def renewal_intersections(inp: GeometryInput):
    """서울시 정비구역·정비예정/사업구역·재정비촉진구역 서버 중첩분석."""
    try:
        return analyze_renewal_intersections(inp.geometry)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logging.exception("renewal intersection failed")
        raise HTTPException(status_code=500, detail=f"정비구역 중첩분석 오류: {exc}") from exc


@app.post("/api/spatial/development-intersections")
def development_intersections(inp: GeometryInput):
    """서울 UQ181 도시개발·공공주택지구·기타 사업구역 서버 중첩분석."""
    try:
        return analyze_development_intersections(inp.geometry)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logging.exception("development intersection failed")
        raise HTTPException(status_code=500, detail=f"개발사업구역 중첩분석 오류: {exc}") from exc


@app.post("/api/reference/safe-medical-nearby")
def safe_medical_nearby(inp: GeometryInput):
    """안심주택 의료시설 중심지역의 공식 위치자료 후보조회.

    공개자료가 위치점만 제공하므로 법정 '대상부지 경계 350m' 자동 PASS에는
    사용하지 않고 REVIEW용 참고자료만 반환한다.
    """
    try:
        return _safe_medical_reference(inp.geometry)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logging.exception("safe medical reference failed")
        return {"status": "error", "items": [], "errors": [str(exc)], "message": "의료시설 공식 위치자료 조회 실패 · 공식자료 확인 필요"}


@app.post("/api/spatial/roads")
def road_intersections(inp: GeometryInput):
    """관리자 설치 공식 실폭도로에서 대상지 주변 도형만 반환합니다."""
    try:
        return analyze_road_intersections(inp.geometry)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logging.exception("road intersection failed")
        raise HTTPException(status_code=500, detail=f"실폭도로 중첩분석 오류: {exc}") from exc



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
            "engine_as_of_date": ENGINE_AS_OF_DATE.isoformat(),
        },
    }


@app.post("/api/redevelopment/evaluate")
def redevelopment_evaluate(inp: RedevelopmentInput):
    return evaluate_redevelopment(inp.model_dump())


@app.post("/api/redevelopment/house-density")
def house_density(detail: Dict[str, Any]):
    return calculate_house_density(detail)
