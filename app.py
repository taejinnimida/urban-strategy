from __future__ import annotations

import math
import os
import csv
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
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request, Response, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
from pyproj import CRS, Geod, Transformer
import shapefile
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, shape, mapping, box
from shapely.ops import transform as geometry_transform, unary_union
from shapely.strtree import STRtree
from shapely.validation import explain_validity

# 도로명주소 원본에는 링 방향이 뒤집힌 유효 폴리곤이 일부 포함된다.
# pyshp의 반복 경고만 억제하고, 아래 로더에서 buffer(0)으로 형상을 보정한다.
shapefile.VERBOSE = False

# ============================================================
# 도시검토 플랫폼 v2.5.0
# - 서버·정적화면·공간자료를 분리한 Docker 배포판
# - 서울 13개 독립 정비·개발사업 Rule Module + 소규모주택정비 shell + 공간근거·관리자 운영
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
    structured = os.path.join(STRUCTURED_DATA_DIR, name)
    if os.path.isfile(structured):
        return structured
    return os.path.join(BASE_DIR, name)


def _json_property(value: Any) -> Any:
    """SHP/DBF 속성값을 JSON 안전형으로 정규화한다.

    비오톱·산지구분도·기초단위구 로더가 같은 변환기를 공유한다.
    이 함수가 없으면 레코드별 예외가 내부에서 무시되어 모든 SHP가
    0건으로 읽히는 회귀가 발생하므로 공통 유틸로 고정한다.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        for encoding in ("utf-8", "cp949"):
            try:
                return raw.decode(encoding)
            except Exception:
                pass
        return raw.decode("latin1", errors="replace")
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except Exception:
        return str(value)


@lru_cache(maxsize=1)
def _index_html() -> str:
    with open(STATIC_HTML_PATH, encoding="utf-8") as fp:
        return fp.read()

# Legacy backend redevelopment evaluator removed in r6.
# Authoritative scheme decisions are produced in app.html from the shared Fact Store.


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


# Deprecated duplicate redevelopment evaluator intentionally absent.


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
VWORLD_SEARCH_URL = "https://api.vworld.kr/req/search"
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
BUILDING_HUB_RECAP_TITLE_URL = BUILDING_HUB_BASE_URL + "/getBrRecapTitleInfo"
BUILDING_HUB_ATCH_JIBUN_URL = BUILDING_HUB_BASE_URL + "/getBrAtchJibunInfo"
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

def _query_building_hub_rows(url: str, pnu: str) -> List[Dict[str, Any]]:
    key = _building_hub_key()
    if not key:
        raise RuntimeError("BUILDING_HUB_API_KEY가 설정되지 않았습니다.")
    base = _pnu_to_bld_params(pnu)
    page = 1
    size = 100
    out: List[Dict[str, Any]] = []
    while page <= 20:
        params = {"serviceKey": key, **base, "numOfRows": size, "pageNo": page}
        resp = requests.get(url, params=params, timeout=25)
        if resp.status_code >= 400:
            raise RuntimeError(f"건축HUB HTTP {resp.status_code}: {resp.text[:240]}")
        ctype = (resp.headers.get('content-type') or '').lower()
        text = resp.text
        try:
            if 'json' in ctype or text.lstrip().startswith('{'):
                items, total = _items_from_data_go_kr(resp.json())
            else:
                items, total = _items_from_data_go_kr_xml(text)
        except Exception as exc:
            raise RuntimeError(f"건축HUB 응답 파싱 실패: {text[:300].replace(chr(10), ' ')}") from exc
        out.extend(items)
        if len(out) >= total or len(items) < size:
            break
        page += 1
    return out


def _query_building_hub_title(pnu: str) -> List[Dict[str, Any]]:
    return _query_building_hub_rows(BUILDING_HUB_TITLE_URL, pnu)


def _query_building_hub_recap_title(pnu: str) -> List[Dict[str, Any]]:
    """대지 전체를 대표하는 총괄표제부를 우선 조회한다."""
    return _query_building_hub_rows(BUILDING_HUB_RECAP_TITLE_URL, pnu)


def _query_building_hub_atch_jibun(pnu: str) -> List[Dict[str, Any]]:
    """건축물대장 부속지번을 조회한다.

    비도시계획시설 병원이 여러 필지에 걸친 경우 대표필지 한 필지만
    법정 의료시설 부지로 쓰지 않기 위해 getBrAtchJibunInfo를 사용한다.
    """
    return _query_building_hub_rows(BUILDING_HUB_ATCH_JIBUN_URL, pnu)


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
        "bylotCnt", "crtnDay", "sigunguCd", "bjdongCd", "platGbCd", "bun", "ji",
    ]
    result = {k: item.get(k) for k in keep}
    result["pnu"] = pnu
    result.update(_age_annotation(item))
    return result


def _digits4(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits.zfill(4)[-4:] if digits else "0000"


def _building_hub_attachment_pnu(row: Dict[str, Any]) -> Optional[str]:
    sigungu = "".join(ch for ch in str(row.get("atchSigunguCd") or row.get("sigunguCd") or "") if ch.isdigit())
    bjdong = "".join(ch for ch in str(row.get("atchBjdongCd") or row.get("bjdongCd") or "") if ch.isdigit())
    if len(sigungu) != 5 or len(bjdong) != 5:
        return None
    plat = str(row.get("atchPlatGbCd") or row.get("platGbCd") or "0").strip()
    land_code = "2" if plat == "1" else "1"  # 건축HUB 산=1, PNU 산=2
    bun = _digits4(row.get("atchBun") or row.get("bun"))
    ji = _digits4(row.get("atchJi") or row.get("ji"))
    pnu = f"{sigungu}{bjdong}{land_code}{bun}{ji}"
    return pnu if len(pnu) == 19 and pnu.isdigit() else None


def _vworld_features_at_point(layer: str, lon: float, lat: float, size: int = 100) -> List[Dict[str, Any]]:
    """VWorld polygon layer에서 공식 위치점과 교차하는 도형을 조회한다."""
    if not _vworld_key():
        raise RuntimeError("VWorld API 키가 설정되지 않았습니다.")
    params = {
        "key": _vworld_key(), "domain": _vworld_domain(),
        "service": "data", "version": "2.0", "request": "getfeature",
        "format": "json", "size": min(max(int(size), 1), 1000), "page": 1,
        "geometry": "true", "attribute": "true", "crs": "EPSG:4326",
        "data": layer, "geomfilter": f"POINT({float(lon)},{float(lat)})",
    }
    resp, route = _vworld_get(VWORLD_DATA_URL, params=params, timeout=20)
    if resp.status_code >= 400:
        raise RuntimeError(f"VWorld {layer} HTTP {resp.status_code}: {resp.text[:240]}")
    try:
        payload = resp.json()
    except Exception as exc:
        raise RuntimeError(f"VWorld {layer} JSON 응답 해석 실패") from exc
    status = str((payload.get("response") or {}).get("status") or "").upper()
    if status == "NOT_FOUND":
        return []
    if status != "OK":
        raise RuntimeError(_response_error_message(payload))
    fc = (((payload.get("response") or {}).get("result") or {}).get("featureCollection") or {})
    out = []
    point = shape({"type": "Point", "coordinates": [lon, lat]})
    for f in fc.get("features") or []:
        if not f.get("geometry"):
            continue
        try:
            g = shape(f["geometry"])
            if not g.covers(point):
                continue
        except Exception:
            continue
        out.append({"type": "Feature", "id": f.get("id"), "geometry": f.get("geometry"), "properties": dict(f.get("properties") or {})})
    logger.info("VWorld point layer=%s route=%s hits=%s", layer, route, len(out))
    return out


def _vworld_parcel_at_point(lon: float, lat: float) -> Dict[str, Any]:
    """공식 시설 위치점이 실제로 포함되는 연속지적 필지를 찾는다.

    점이 지적경계에 정확히 걸려 둘 이상이 covers하는 경우에는 임의로
    최근접 필지를 선택하지 않고 ambiguous를 반환한다.
    """
    to_metric = Transformer.from_crs(4326, 5174, always_xy=True)
    to_wgs = Transformer.from_crs(5174, 4326, always_xy=True)
    x, y = to_metric.transform(float(lon), float(lat))
    probe_metric = shape({"type": "Point", "coordinates": [x, y]}).buffer(8.0)
    probe_wgs = geometry_transform(to_wgs.transform, probe_metric)
    candidates = _fetch_vworld_parcel_candidates(probe_wgs)
    point = shape({"type": "Point", "coordinates": [float(lon), float(lat)]})
    hits = []
    for f in candidates:
        try:
            if shape(f.get("geometry")).covers(point):
                hits.append(f)
        except Exception:
            continue
    unique: Dict[str, Dict[str, Any]] = {}
    for f in hits:
        pnu = str((f.get("properties") or {}).get("pnu") or "").strip()
        if pnu:
            unique[pnu] = f
    hits = list(unique.values())
    if len(hits) == 1:
        return {"status": "resolved", "feature": hits[0], "pnu": str((hits[0].get("properties") or {}).get("pnu") or "")}
    if len(hits) > 1:
        return {"status": "ambiguous", "feature": None, "pnu": None, "candidate_pnus": sorted(unique)}
    return {"status": "not_found", "feature": None, "pnu": None, "candidate_pnus": []}


def _vworld_parcel_by_address(address: str) -> Dict[str, Any]:
    """공식 시설주소를 VWorld 주소검색→검색좌표의 실제 지적 포함관계로 연결한다.

    지번주소뿐 아니라 보건소 공식목록의 도로명주소도 처리한다. 문자열 주소 자체로
    PNU를 확정하지 않고, 검색 좌표가 실제 포함되는 연속지적 필지를 재확인한다.
    최근접 필지 추정은 하지 않는다.
    """
    query = re.sub(r"\s+", " ", str(address or "")).strip()
    if not query:
        return {"status": "not_found", "feature": None, "pnu": None, "address": query}
    if not _vworld_key():
        return {"status": "unavailable", "feature": None, "pnu": None, "address": query, "reason": "VWorld API 키 미설정"}
    last_reason = None
    for category in ("parcel", "road"):
        params = {
            "key": _vworld_key(), "domain": _vworld_domain(),
            "service": "search", "version": "2.0", "request": "search",
            "format": "json", "size": 10, "page": 1,
            "query": query, "type": "ADDRESS", "category": category, "crs": "EPSG:4326",
        }
        try:
            resp, route = _vworld_get(VWORLD_SEARCH_URL, params=params, timeout=20)
            if resp.status_code >= 400:
                last_reason = f"VWorld 주소검색({category}) HTTP {resp.status_code}"
                continue
            payload = resp.json()
            rsp = payload.get("response") or {}
            status = str(rsp.get("status") or "").upper()
            if status != "OK":
                last_reason = _response_error_message(payload) if status != "NOT_FOUND" else f"{category} 주소검색 결과 없음"
                continue
            items = (((rsp.get("result") or {}).get("items")) or [])
            if not items:
                last_reason = f"{category} 주소검색 결과 없음"
                continue
            norm = lambda v: re.sub(r"\s+", "", str(v or ""))
            qn = norm(query)
            ranked = sorted(items, key=lambda it: (0 if any((norm((it.get("address") or {}).get(k)) and (norm((it.get("address") or {}).get(k)) in qn or qn in norm((it.get("address") or {}).get(k)))) for k in ("parcel","road")) else 1))
            for it in ranked[:5]:
                point = it.get("point") or {}
                try:
                    lon, lat = float(point.get("x")), float(point.get("y"))
                except Exception:
                    continue
                resolved = _vworld_parcel_at_point(lon, lat)
                if resolved.get("status") == "resolved":
                    result = dict(resolved)
                    result.update({"address": query, "route": route, "search_item": it, "address_category": category, "search_point": [lon, lat]})
                    return result
            last_reason = f"{category} 주소검색 결과 좌표의 지적필지 확정 실패"
        except Exception as exc:
            last_reason = str(exc)
    return {"status": "not_found", "feature": None, "pnu": None, "address": query, "reason": last_reason or "주소검색 실패"}


def _fetch_vworld_parcels_for_pnus(pnus: List[str], anchor_feature: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    wanted = {str(x) for x in pnus if str(x)}
    if not wanted or not anchor_feature or not anchor_feature.get("geometry"):
        return {}
    to_metric = Transformer.from_crs(4326, 5174, always_xy=True)
    to_wgs = Transformer.from_crs(5174, 4326, always_xy=True)
    anchor_metric = geometry_transform(to_metric.transform, shape(anchor_feature["geometry"]))
    search_wgs = geometry_transform(to_wgs.transform, anchor_metric.buffer(350.0))
    candidates = _fetch_vworld_parcel_candidates(search_wgs)
    out = {}
    for f in candidates:
        pnu = str((f.get("properties") or {}).get("pnu") or "").strip()
        if pnu in wanted:
            out[pnu] = f
    return out


def _medical_planning_facility_boundary(lon: float, lat: float) -> Dict[str, Any]:
    """보건위생시설 중 명칭/속성으로 '종합의료시설'이 확인되는 도형만 사용."""
    try:
        features = _vworld_features_at_point("LT_C_UPISUQ157", lon, lat, 100)
    except Exception as exc:
        return {"status": "error", "geometry": None, "error": str(exc), "features": []}
    matched = []
    for f in features:
        p = f.get("properties") or {}
        text = " ".join(str(v) for v in p.values() if isinstance(v, (str, int, float)))
        compact = re.sub(r"\s+", "", text)
        if "종합의료시설" in compact or "종합의료" in compact:
            matched.append(f)
    if not matched:
        return {"status": "none", "geometry": None, "features": features}
    try:
        geom = unary_union([shape(f["geometry"]) for f in matched if f.get("geometry")])
        geom = _polygonal_only(geom)
        if geom is None or geom.is_empty:
            return {"status": "invalid", "geometry": None, "features": matched}
        return {"status": "resolved", "geometry": mapping(geom), "features": matched}
    except Exception as exc:
        return {"status": "error", "geometry": None, "error": str(exc), "features": matched}


def _medical_building_site_boundary(primary_pnu: str, primary_feature: Dict[str, Any], facility_name: str) -> Dict[str, Any]:
    """비도시계획시설 병원의 건축물대장상 전체 대지를 지적경계로 복원한다.

    총괄표제부를 우선하고, 없거나 의료시설 매칭이 안 될 때 표제부로 보완한다.
    부속지번/외필지 일부라도 확인되지 않으면 참고경계만 반환하고 자동 PASS에는 쓰지 않는다.
    """
    if not building_hub_ready():
        return {"status": "unavailable", "geometry": None, "reason": "BUILDING_HUB_API_KEY 미설정"}

    def scored(rows: List[Dict[str, Any]], ledger_kind: str):
        nk = _name_key(facility_name)
        out = []
        for row in rows:
            purpose = str(row.get("mainPurpsCdNm") or row.get("etcPurps") or "")
            bld_name = str(row.get("bldNm") or "")
            score = 0
            if "의료시설" in purpose or "병원" in purpose:
                score += 10
            if nk and nk in _name_key(bld_name):
                score += 5
            if ledger_kind == "recap":
                score += 2
            if str(row.get("mainAtchGbCd") or "") in {"0", "1"}:
                score += 1
            if score:
                try: area = float(row.get("platArea") or 0)
                except Exception: area = 0.0
                out.append((score, area, row, ledger_kind))
        return out

    recap_error = None
    title_error = None
    candidates = []
    try:
        candidates.extend(scored(_query_building_hub_recap_title(primary_pnu), "recap"))
    except Exception as exc:
        recap_error = str(exc)
    if not candidates:
        try:
            candidates.extend(scored(_query_building_hub_title(primary_pnu), "title"))
        except Exception as exc:
            title_error = str(exc)
    if not candidates:
        reasons = []
        if recap_error: reasons.append(f"총괄표제부 조회 실패: {recap_error}")
        if title_error: reasons.append(f"표제부 조회 실패: {title_error}")
        if not reasons: reasons.append("건축물대장에서 의료시설 용도/병원명 매칭 실패")
        return {"status": "unmatched", "geometry": None, "reason": " / ".join(reasons)}

    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    selected = candidates[0][2]
    ledger_kind = candidates[0][3]
    mgm = str(selected.get("mgmBldrgstPk") or "").strip()
    try:
        attachments = _query_building_hub_atch_jibun(primary_pnu)
    except Exception as exc:
        return {
            "status": "partial", "geometry": primary_feature.get("geometry"),
            "reason": f"건축물대장 부속지번 조회 실패: {exc}", "primary_pnu": primary_pnu,
            "title": {**_normalize_building_title(selected, primary_pnu), "ledger_kind": ledger_kind},
        }

    related_rows = [r for r in attachments if not mgm or str(r.get("mgmBldrgstPk") or "").strip() == mgm]
    related_pnus = [primary_pnu]
    for row in related_rows:
        rpnu = _building_hub_attachment_pnu(row)
        if rpnu and rpnu not in related_pnus:
            related_pnus.append(rpnu)
    try: expected_attach = max(0, int(float(selected.get("bylotCnt") or 0)))
    except Exception: expected_attach = 0

    found = _fetch_vworld_parcels_for_pnus(related_pnus, primary_feature)
    if primary_pnu not in found:
        found[primary_pnu] = primary_feature
    title_info = {**_normalize_building_title(selected, primary_pnu), "ledger_kind": ledger_kind}
    missing = [rpnu for rpnu in related_pnus if rpnu not in found]
    partial_geom = _polygonal_only(unary_union([shape(f["geometry"]) for f in found.values() if f.get("geometry")])) if found else None
    if missing:
        return {
            "status": "partial", "geometry": mapping(partial_geom) if partial_geom is not None and not partial_geom.is_empty else primary_feature.get("geometry"),
            "reason": f"건축물대장 관련지번 중 지적경계 {len(missing)}필지 미복원",
            "primary_pnu": primary_pnu, "related_pnus": related_pnus, "missing_pnus": missing,
            "expected_attachment_count": expected_attach, "resolved_attachment_count": max(0, len(found)-1), "title": title_info,
        }
    if expected_attach > max(0, len(related_pnus)-1):
        return {
            "status": "partial", "geometry": mapping(partial_geom) if partial_geom is not None and not partial_geom.is_empty else primary_feature.get("geometry"),
            "reason": f"건축물대장 외필지수 미충족(대장 {expected_attach} / 부속지번 API {max(0,len(related_pnus)-1)})",
            "primary_pnu": primary_pnu, "related_pnus": related_pnus,
            "expected_attachment_count": expected_attach, "resolved_attachment_count": max(0, len(found)-1), "title": title_info,
        }
    geom = partial_geom
    if geom is None or geom.is_empty:
        return {"status": "invalid", "geometry": None, "reason": "건축물대장 대지 지적경계 Union 실패"}
    return {
        "status": "resolved", "geometry": mapping(geom), "primary_pnu": primary_pnu,
        "related_pnus": related_pnus, "parcel_count": len(found),
        "expected_attachment_count": expected_attach, "resolved_attachment_count": max(0, len(found)-1),
        "title": title_info,
        "reason": f"건축물대장 {'총괄표제부' if ledger_kind == 'recap' else '표제부'} 의료시설 매칭 + 부속지번 + 연속지적 경계 복원",
    }


def _medical_boundary_metrics(site_wgs, boundary_geometry: Dict[str, Any]) -> Dict[str, Any]:
    to_metric = Transformer.from_crs(4326, 5174, always_xy=True)
    to_wgs = Transformer.from_crs(5174, 4326, always_xy=True)
    site_metric = geometry_transform(to_metric.transform, site_wgs)
    boundary_wgs = _polygonal_only(shape(boundary_geometry))
    if boundary_wgs is None or boundary_wgs.is_empty:
        raise ValueError("의료시설 부지경계가 비어 있습니다.")
    boundary_metric = geometry_transform(to_metric.transform, boundary_wgs)
    distance = float(site_metric.distance(boundary_metric))
    buffer_metric = boundary_metric.buffer(350.0)
    return {
        "distance_boundary_m": round(distance, 1),
        "within_350": distance <= 350.0 + 1e-6,
        "buffer_350_geometry": mapping(geometry_transform(to_wgs.transform, buffer_metric)),
    }


def _resolve_medical_facility_boundary(item: Dict[str, Any], site_wgs) -> Dict[str, Any]:
    geometry = item.get("geometry") or {}
    coords = geometry.get("coordinates") or []
    if len(coords) < 2:
        return {"boundary_status": "REVIEW", "boundary_basis": "BOUNDARY_NOT_RESOLVED", "boundary_note": "공식 시설 위치좌표 없음", "auto_pass_eligible": False}
    lon, lat = float(coords[0]), float(coords[1])
    category = str(item.get("category") or "")
    stale_reference = str(item.get("data_status") or "").startswith("stale_reference_point")

    if category in {"general_hospital", "municipal_hospital"}:
        planning = _medical_planning_facility_boundary(lon, lat)
        if planning.get("status") == "resolved" and planning.get("geometry"):
            metrics = _medical_boundary_metrics(site_wgs, planning["geometry"])
            return {
                "boundary_status": "CONFIRMED", "boundary_basis": "URBAN_PLANNING_MEDICAL_FACILITY",
                "boundary_basis_label": "도시계획시설 종합의료시설 경계", "facility_boundary_geometry": planning["geometry"],
                "boundary_note": "공식 병원 위치점과 중첩하는 VWorld 보건위생시설 중 '종합의료시설' 확인",
                "auto_pass_eligible": True, **metrics,
            }

    point_error = None
    try:
        point_parcel = _vworld_parcel_at_point(lon, lat)
    except Exception as exc:
        point_error = str(exc)
        point_parcel = {"status": "error", "feature": None, "pnu": None}

    if category == "public_health_center":
        if point_parcel.get("status") != "resolved" or not point_parcel.get("feature"):
            note = "공식 위치점이 지적경계에 걸려 필지 확정 불가" if point_parcel.get("status") == "ambiguous" else "공식 위치점 소재 지적필지 미확인"
            if point_error: note = f"공식 위치점 소재 지적필지 조회 실패: {point_error}"
            return {"boundary_status": "REVIEW", "boundary_basis": "BOUNDARY_NOT_RESOLVED", "boundary_note": note, "auto_pass_eligible": False, "parcel_lookup_status": point_parcel.get("status")}
        feature = point_parcel["feature"]
        pnu = point_parcel.get("pnu")
        metrics = _medical_boundary_metrics(site_wgs, feature["geometry"])
        if stale_reference:
            return {
                "boundary_status": "REVIEW", "boundary_basis": "CADASTRAL_PARCEL_FROM_STALE_REFERENCE_POINT",
                "boundary_basis_label": "2023 보조 위치점 소재 지적필지(참고)", "facility_boundary_geometry": feature["geometry"],
                "boundary_note": "2023년 일회성 보조 위치자료이므로 지적경계를 복원해도 법정 PASS에는 사용하지 않음",
                "primary_pnu": pnu, "parcel_count": 1, "auto_pass_eligible": False, **metrics,
            }
        return {
            "boundary_status": "CONFIRMED", "boundary_basis": "CADASTRAL_PARCEL_FROM_OFFICIAL_POINT",
            "boundary_basis_label": "보건소 공식 위치점 소재 지적필지", "facility_boundary_geometry": feature["geometry"],
            "boundary_note": "서울시 공식 보건소 위치좌표가 포함되는 연속지적 필지경계를 시설부지로 적용",
            "primary_pnu": pnu, "parcel_count": 1, "auto_pass_eligible": True, **metrics,
        }

    parcel_candidates = []
    if point_parcel.get("status") == "resolved" and point_parcel.get("feature"):
        parcel_candidates.append(("official_point", point_parcel.get("pnu"), point_parcel.get("feature")))
    address_lookup = None
    parcel_address = str(item.get("parcel_address") or item.get("address") or "").strip()
    if parcel_address:
        address_lookup = _vworld_parcel_by_address(parcel_address)
        if address_lookup.get("status") == "resolved" and address_lookup.get("feature"):
            apnu = address_lookup.get("pnu")
            if not any(pnu == apnu for _, pnu, _ in parcel_candidates):
                parcel_candidates.append(("official_license_address", apnu, address_lookup.get("feature")))

    attempts = []
    for candidate_basis, candidate_pnu, candidate_feature in parcel_candidates:
        if not candidate_pnu or not candidate_feature: continue
        bsite = _medical_building_site_boundary(candidate_pnu, candidate_feature, str(item.get("name") or ""))
        attempts.append((candidate_basis, candidate_pnu, candidate_feature, bsite))
        if bsite.get("status") == "resolved" and bsite.get("geometry"):
            metrics = _medical_boundary_metrics(site_wgs, bsite["geometry"])
            title = bsite.get("title") or {}
            ledger_label = "총괄표제부" if title.get("ledger_kind") == "recap" else "표제부"
            basis_note = "공식 위치점 소재필지" if candidate_basis == "official_point" else "서울시 인허가 지번주소 소재필지"
            return {
                "boundary_status": "CONFIRMED", "boundary_basis": "BUILDING_REGISTER_SITE_PARCELS",
                "boundary_basis_label": "건축물대장 대지·부속지번 지적경계", "facility_boundary_geometry": bsite["geometry"],
                "boundary_note": f"{basis_note}에서 건축물대장 {ledger_label}·부속지번으로 전체 대지 복원",
                "auto_pass_eligible": True, "primary_pnu": candidate_pnu,
                "related_pnus": bsite.get("related_pnus") or [candidate_pnu], "parcel_count": bsite.get("parcel_count"),
                "building_title": title, "parcel_candidate_basis": candidate_basis, **metrics,
            }

    partial = None; partial_basis = None; partial_pnu = None; partial_note = None; partial_title = None
    for cbasis, cpnu, cfeature, bsite in attempts:
        if bsite.get("geometry"):
            partial, partial_basis, partial_pnu = bsite.get("geometry"), cbasis, cpnu
            partial_note, partial_title = bsite.get("reason"), bsite.get("title")
            break
        if partial is None and cfeature and cfeature.get("geometry"):
            partial, partial_basis, partial_pnu = cfeature.get("geometry"), cbasis, cpnu
            partial_note, partial_title = bsite.get("reason"), bsite.get("title")
    if partial is None and point_parcel.get("status") == "resolved" and point_parcel.get("feature"):
        partial, partial_basis, partial_pnu = point_parcel["feature"].get("geometry"), "official_point", point_parcel.get("pnu")
    if partial is None and address_lookup and address_lookup.get("status") == "resolved" and address_lookup.get("feature"):
        partial, partial_basis, partial_pnu = address_lookup["feature"].get("geometry"), "official_license_address", address_lookup.get("pnu")

    notes = []
    if point_error: notes.append(f"위치점 지적조회 오류: {point_error}")
    elif point_parcel.get("status") != "resolved": notes.append(f"위치점 지적조회 {point_parcel.get('status')}")
    if address_lookup and address_lookup.get("status") != "resolved": notes.append(f"인허가 지번주소 지적조회 {address_lookup.get('status')}")
    if partial_note: notes.append(partial_note)
    out = {
        "boundary_status": "REVIEW", "boundary_basis": "CADASTRAL_PARCEL_REFERENCE_ONLY" if partial else "BOUNDARY_NOT_RESOLVED",
        "boundary_basis_label": "병원 공식자료 기반 지적필지(건축물대장 전체대지 미확정)" if partial else "부지경계 미확정",
        "facility_boundary_geometry": partial, "boundary_note": " / ".join(notes) or "건축물대장 관련 대지 전체 미확인",
        "primary_pnu": partial_pnu, "building_title": partial_title, "parcel_candidate_basis": partial_basis,
        "auto_pass_eligible": False,
    }
    if partial:
        try: out.update(_medical_boundary_metrics(site_wgs, partial))
        except Exception: pass
    return out


VWORLD_LAND_LEDGER_URL = "https://api.vworld.kr/ned/data/ladfrlList"
VWORLD_LAND_USE_URL = "https://api.vworld.kr/ned/data/getLandUseAttr"
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



def _parse_land_use_xml(text: str) -> List[Dict[str, Any]]:
    """Parse VWorld NED getLandUseAttr XML.

    The API returns /response/fields/field rows.  Attribute names can be
    missing for some records, so parsing is deliberately defensive.
    """
    root = ET.fromstring(text)
    result_code = (root.findtext(".//resultCode") or "").strip()
    result_msg = (root.findtext(".//resultMsg") or "").strip()
    if result_code and result_code not in {"00", "0"}:
        raise RuntimeError(f"토지이용계획 API 오류 {result_code}: {result_msg or 'unknown'}")

    def val(row: ET.Element, name: str) -> str:
        node = row.find(name)
        return (node.text or "").strip() if node is not None else ""

    rows: List[Dict[str, Any]] = []
    for row in root.findall(".//fields/field"):
        rows.append({
            "pnu": val(row, "pnu"),
            "cnflcAt": val(row, "cnflcAt"),
            "cnflcAtNm": val(row, "cnflcAtNm"),
            "prposAreaDstrcCode": val(row, "prposAreaDstrcCode"),
            "prposAreaDstrcCodeNm": val(row, "prposAreaDstrcCodeNm"),
            "manageNo": val(row, "manageNo"),
            "lastUpdtDt": val(row, "lastUpdtDt"),
        })
    return rows


@lru_cache(maxsize=4096)
def _server_land_use_rows_vworld(pnu: str) -> tuple:
    if not _vworld_key():
        raise RuntimeError("VWORLD_API_KEY 미설정")
    params = {
        "format": "xml",
        "key": _vworld_key(),
        "domain": _vworld_domain(),
        "pnu": pnu,
        "numOfRows": 1000,
    }
    resp, route = _vworld_get(VWORLD_LAND_USE_URL, params=params, timeout=18)
    if resp.status_code >= 400:
        raise RuntimeError(f"VWorld HTTP {resp.status_code}")
    rows = _parse_land_use_xml(resp.text)
    # cache requires immutable return; endpoint converts to normal dict/list.
    return tuple(tuple(sorted({**row, "_route": f"server_{route}"}.items())) for row in rows)


def _land_use_rows_for_pnu(pnu: str) -> List[Dict[str, Any]]:
    return [dict(items) for items in _server_land_use_rows_vworld(pnu)]


def _land_use_category(name: str) -> Optional[str]:
    n = re.sub(r"\s+", "", str(name or ""))
    if "비오톱" in n and "1등급" in n:
        return "biotope_grade1"
    if "공익용산지" in n:
        return "public_interest_forest"
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

# 역명 -> 해당 역과 공간적으로 확실히 연결된 출입구 좌표 목록.
# 원본(TL_SPSB_ENTRC)에는 소속 역을 가리키는 속성 키가 없어, 배포 전 오프라인
# 전처리 단계에서 stations.json 폴리곤 기준 최근접 매칭 + 애매하면 제외(margin
# 검사)로 미리 만들어 둔 결과다. 런타임에 이름 정규화 등으로 추가 매칭을 시도하지 않는다.
STATION_ENTRANCE_REFERENCE_PATH = _data_path("station_entrances.json")
@lru_cache(maxsize=1)
def _station_entrance_reference_data():
    try:
        with open(STATION_ENTRANCE_REFERENCE_PATH, encoding="utf-8") as fp:
            return json.load(fp)
    except FileNotFoundError:
        return {}


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


def _road_width_m(properties: Dict[str, Any]) -> Optional[float]:
    """TL_SPRD_MANAGE 도로구간 속성에서 ROAD_BT 등 공식 폭원(m)을 읽는다."""
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


def _biotope_zip_path() -> Optional[str]:
    """서울시 개별비오톱(2025 기준) 중 1등급 폴리곤 묶음."""
    path = _data_path("biotope_seoul.zip")
    return path if os.path.isfile(path) and os.path.getsize(path) > 0 else None


@lru_cache(maxsize=1)
def _biotope_spatial_layers() -> Dict[str, Any]:
    """내장 비오톱1등급 ZIP을 WGS84로 변환하고 STRtree로 색인한다."""
    zip_path = _biotope_zip_path()
    if not zip_path:
        return {"available": False, "reason": "data/biotope_seoul.zip 미설치"}
    rows: List[Dict[str, Any]] = []
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        stems = sorted({os.path.splitext(n)[0] for n in names if n.lower().endswith(".shp")})
        for stem in stems:
            shp_name = next((n for n in names if os.path.splitext(n)[0] == stem and n.lower().endswith(".shp")), None)
            shx_name = next((n for n in names if os.path.splitext(n)[0] == stem and n.lower().endswith(".shx")), None)
            dbf_name = next((n for n in names if os.path.splitext(n)[0] == stem and n.lower().endswith(".dbf")), None)
            if not (shp_name and shx_name and dbf_name):
                continue
            source_crs = CRS.from_user_input(os.getenv("BIOTOPE_DATA_CRS", "EPSG:5174"))
            prj_name = next((n for n in names if os.path.splitext(n)[0] == stem and n.lower().endswith(".prj")), None)
            if prj_name:
                try:
                    source_crs = CRS.from_wkt(zf.read(prj_name).decode("utf-8", errors="ignore"))
                except Exception:
                    logging.warning("biotope PRJ parse failed; BIOTOPE_DATA_CRS fallback used: %s", stem)
            to_wgs = Transformer.from_crs(source_crs, 4326, always_xy=True).transform
            reader = shapefile.Reader(
                shp=io.BytesIO(zf.read(shp_name)),
                shx=io.BytesIO(zf.read(shx_name)),
                dbf=io.BytesIO(zf.read(dbf_name)),
                encoding="cp949",
                encodingErrors="replace",
            )
            fields = [f[0] for f in reader.fields[1:]]
            for sr in reader.iterShapeRecords():
                try:
                    props = {k: _json_property(v) for k, v in zip(fields, list(sr.record))}
                    # 배포본이 잘못 교체돼도 1등급 외 도형을 자동판정에 섞지 않는다.
                    if str(props.get("유형평가") or "").strip() != "1등급" and str(props.get("개별평가") or "").strip() != "1등급":
                        continue
                    geom = shape(sr.shape.__geo_interface__)
                    if geom.is_empty:
                        continue
                    if not geom.is_valid:
                        geom = geom.buffer(0)
                    geom = geometry_transform(to_wgs, geom)
                    if geom.is_empty:
                        continue
                    rows.append({"geometry": geom, "properties": props})
                except Exception:
                    continue
    tree = STRtree([r["geometry"] for r in rows]) if rows else None
    return {
        "available": bool(rows), "rows": rows, "tree": tree,
        "source": "서울시 개별비오톱(2025 기준) 원본 SHP · 유형평가/개별평가 중 1등급",
        "file": os.path.basename(zip_path), "count": len(rows),
    }


def analyze_biotope_intersections(geometry: Dict[str, Any]) -> Dict[str, Any]:
    """대상지와 비오톱1등급을 실제 교차하고 중첩면적·비율·클립도형을 반환한다."""
    layers = _biotope_spatial_layers()
    if not layers.get("available"):
        raise FileNotFoundError(str(layers.get("reason") or "비오톱1등급 원본 미설치"))
    site = shape(geometry)
    if site.geom_type not in {"Polygon", "MultiPolygon"} or site.is_empty or not site.is_valid:
        raise ValueError("유효한 Polygon 또는 MultiPolygon 구역계가 필요합니다.")
    tree = layers.get("tree")
    rows = layers.get("rows") or []
    clipped_rows: List[Dict[str, Any]] = []
    clipped_geoms = []
    if tree is not None:
        for idx in tree.query(site, predicate="intersects"):
            row = rows[int(idx)]
            try:
                inter = _polygonal_only(site.intersection(row["geometry"]))
            except Exception:
                inter = []
            if not inter:
                continue
            inter_geom = unary_union(inter)
            if inter_geom.is_empty:
                continue
            clipped_geoms.append(inter_geom)
            clipped_rows.append({"type": "Feature", "geometry": mapping(inter_geom), "properties": row["properties"]})
            if len(clipped_rows) >= 5000:
                break
    union_wgs = unary_union(clipped_geoms) if clipped_geoms else None
    to_metric = Transformer.from_crs(4326, 5174, always_xy=True).transform
    site_m2 = float(geometry_transform(to_metric, site).area)
    overlap_m2 = float(geometry_transform(to_metric, union_wgs).area) if union_wgs is not None and not union_wgs.is_empty else 0.0
    overlap_pct = (overlap_m2 / site_m2 * 100.0) if site_m2 > 0 else None
    return {
        "status": "matched" if clipped_rows else "none",
        "intersects": bool(clipped_rows and overlap_m2 > 0.5),
        "overlap_area_m2": overlap_m2,
        "overlap_pct": overlap_pct,
        "features": {"type": "FeatureCollection", "features": clipped_rows},
        "metadata": {
            "source": layers.get("source"), "file": layers.get("file"),
            "dataset_count": layers.get("count", 0), "return_count": len(clipped_rows),
            "grade_basis": "유형평가 또는 개별평가 중 하나라도 1등급",
            "geometry_basis": "site_exact_intersection",
        },
    }


def _forest_classification_zip_path() -> Optional[str]:
    """국토교통부 연속주제도 산지구분도(UF801), 서울 2026-08 원본."""
    path = _data_path("forest_classification_seoul_202608.zip")
    return path if os.path.isfile(path) and os.path.getsize(path) > 0 else None


def _forest_class_from_properties(props: Dict[str, Any]) -> Optional[str]:
    """UF801 MNUM 분류코드를 우선 사용하고 ALIAS는 결측 시 검증용으로만 쓴다."""
    mnum = re.sub(r"\s+", "", str(props.get("MNUM") or "").upper())
    match = re.search(r"UFM(100|110|120|200)", mnum)
    if match:
        return {
            "100": "conservation_forest",
            "110": "forestry_forest",
            "120": "public_interest_forest",
            "200": "semi_conservation_forest",
        }[match.group(1)]
    alias = re.sub(r"\s+", "", str(props.get("ALIAS") or ""))
    return {
        "보전산지": "conservation_forest",
        "임업용산지": "forestry_forest",
        "공익용산지": "public_interest_forest",
        "준보전산지": "semi_conservation_forest",
    }.get(alias)


@lru_cache(maxsize=1)
def _forest_classification_spatial_layers() -> Dict[str, Any]:
    """UF801을 WGS84로 변환해 공익용·임업용 산지를 서로 분리하여 색인한다."""
    zip_path = _forest_classification_zip_path()
    if not zip_path:
        return {"available": False, "reason": "forest_classification_seoul_202608.zip 미설치"}
    rows_by_class: Dict[str, List[Dict[str, Any]]] = {
        "conservation_forest": [],
        "forestry_forest": [],
        "public_interest_forest": [],
        "semi_conservation_forest": [],
    }
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        stems = sorted({os.path.splitext(n)[0] for n in names if n.lower().endswith(".shp")})
        for stem in stems:
            shp_name = next((n for n in names if os.path.splitext(n)[0] == stem and n.lower().endswith(".shp")), None)
            shx_name = next((n for n in names if os.path.splitext(n)[0] == stem and n.lower().endswith(".shx")), None)
            dbf_name = next((n for n in names if os.path.splitext(n)[0] == stem and n.lower().endswith(".dbf")), None)
            if not (shp_name and shx_name and dbf_name):
                continue
            source_crs = CRS.from_user_input(os.getenv("FOREST_CLASSIFICATION_DATA_CRS", "EPSG:5174"))
            prj_name = next((n for n in names if os.path.splitext(n)[0] == stem and n.lower().endswith(".prj")), None)
            if prj_name:
                try:
                    source_crs = CRS.from_wkt(zf.read(prj_name).decode("utf-8", errors="ignore"))
                except Exception:
                    logging.warning("forest classification PRJ parse failed; EPSG:5174 fallback used: %s", stem)
            to_wgs = Transformer.from_crs(source_crs, 4326, always_xy=True).transform
            reader = shapefile.Reader(
                shp=io.BytesIO(zf.read(shp_name)),
                shx=io.BytesIO(zf.read(shx_name)),
                dbf=io.BytesIO(zf.read(dbf_name)),
                encoding="cp949",
                encodingErrors="replace",
            )
            fields = [f[0] for f in reader.fields[1:]]
            for sr in reader.iterShapeRecords():
                try:
                    props = {k: _json_property(v) for k, v in zip(fields, list(sr.record))}
                    forest_class = _forest_class_from_properties(props)
                    if forest_class not in rows_by_class:
                        continue
                    geom = shape(sr.shape.__geo_interface__)
                    if geom.is_empty:
                        continue
                    if not geom.is_valid:
                        geom = geom.buffer(0)
                    geom = geometry_transform(to_wgs, geom)
                    if geom.is_empty:
                        continue
                    props["_forest_class"] = forest_class
                    props["_forest_class_basis"] = "MNUM_UFM_CODE"
                    rows_by_class[forest_class].append({"geometry": geom, "properties": props})
                except Exception:
                    continue
    trees = {
        key: STRtree([row["geometry"] for row in rows]) if rows else None
        for key, rows in rows_by_class.items()
    }
    return {
        "available": bool(rows_by_class["public_interest_forest"]),
        "rows_by_class": rows_by_class,
        "trees": trees,
        "counts": {key: len(rows) for key, rows in rows_by_class.items()},
        "source": "국토교통부 연속주제도 산지구분도(UF801) 서울 2026-08 · MNUM UFM120/110 분리",
        "file": os.path.basename(zip_path),
        "crs": "EPSG:5174",
    }


def _forest_class_intersection(site: Any, layers: Dict[str, Any], forest_class: str) -> Dict[str, Any]:
    rows = (layers.get("rows_by_class") or {}).get(forest_class) or []
    tree = (layers.get("trees") or {}).get(forest_class)
    clipped_rows: List[Dict[str, Any]] = []
    clipped_geoms = []
    if tree is not None:
        for idx in tree.query(site, predicate="intersects"):
            row = rows[int(idx)]
            try:
                inter = _polygonal_only(site.intersection(row["geometry"]))
            except Exception:
                inter = []
            if not inter:
                continue
            inter_geom = unary_union(inter)
            if inter_geom.is_empty:
                continue
            clipped_geoms.append(inter_geom)
            clipped_rows.append({"type": "Feature", "geometry": mapping(inter_geom), "properties": row["properties"]})
            if len(clipped_rows) >= 5000:
                break
    union_wgs = unary_union(clipped_geoms) if clipped_geoms else None
    to_metric = Transformer.from_crs(4326, 5174, always_xy=True).transform
    site_m2 = float(geometry_transform(to_metric, site).area)
    overlap_m2 = float(geometry_transform(to_metric, union_wgs).area) if union_wgs is not None and not union_wgs.is_empty else 0.0
    return {
        "intersects": bool(clipped_rows and overlap_m2 > 0.5),
        "overlap_area_m2": overlap_m2,
        "overlap_pct": (overlap_m2 / site_m2 * 100.0) if site_m2 > 0 else None,
        "features": {"type": "FeatureCollection", "features": clipped_rows},
        "return_count": len(clipped_rows),
    }


def analyze_forest_classification_intersections(geometry: Dict[str, Any]) -> Dict[str, Any]:
    """대상지와 UF801 공익용·임업용산지를 각각 실제 교차한다."""
    layers = _forest_classification_spatial_layers()
    if not layers.get("available"):
        raise FileNotFoundError(str(layers.get("reason") or "서울 산지구분도 원본 미설치"))
    site = shape(geometry)
    if site.geom_type not in {"Polygon", "MultiPolygon"} or site.is_empty or not site.is_valid:
        raise ValueError("유효한 Polygon 또는 MultiPolygon 구역계가 필요합니다.")
    public_result = _forest_class_intersection(site, layers, "public_interest_forest")
    forestry_result = _forest_class_intersection(site, layers, "forestry_forest")
    return {
        "status": "matched" if public_result["intersects"] or forestry_result["intersects"] else "none",
        "public_interest_forest": public_result,
        "forestry_forest": forestry_result,
        "metadata": {
            "source": layers.get("source"),
            "file": layers.get("file"),
            "source_crs": layers.get("crs"),
            "dataset_counts": layers.get("counts"),
            "classification_basis": "MNUM UFM120=공익용산지, UFM110=임업용산지",
            "geometry_basis": "site_exact_intersection",
        },
    }


def _polygon_parts(geom: Any) -> List[Any]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return [g for g in geom.geoms if not g.is_empty]
    if isinstance(geom, GeometryCollection):
        out: List[Any] = []
        for g in geom.geoms:
            out.extend(_polygon_parts(g))
        return out
    return []


def _street_block_site_parts(frame_metric: Any, barrier_union: Any, site_metric: Any) -> List[tuple[Any, float]]:
    """현재 barrier에서 대상지와 겹치는 열린 공간 조각을 큰 순서대로 반환한다."""
    try:
        open_space = frame_metric.difference(barrier_union)
    except Exception:
        return []
    site_area = max(1.0, float(site_metric.area))
    out: List[tuple[Any, float]] = []
    for part in _polygon_parts(open_space):
        if part.area <= 1.0:
            continue
        try:
            ia = float(part.intersection(site_metric).area)
        except Exception:
            ia = 0.0
        if ia > max(1.0, site_area * 0.002):
            out.append((part, ia))
    out.sort(key=lambda x: x[1], reverse=True)
    return out


def _street_block_facility_effect(primary: Any, facility: Any, frame_metric: Any, barrier_union: Any, site_metric: Any) -> tuple[bool, str]:
    """내부 고립시설은 제외하고 실제 블록을 분리/폐합하는 시설만 경계로 채택한다."""
    try:
        fac = facility.buffer(0.20, join_style=2)
        if fac.is_empty or not fac.intersects(primary):
            return False, "outside_primary"

        # 내부의 작은 공원·주차장·학교는 hole만 만들 뿐 가로구역을 둘로 나누지 않는다.
        split = primary.difference(fac)
        significant = [g for g in _polygon_parts(split) if g.area >= max(25.0, float(primary.area) * 0.01)]
        if len(significant) >= 2:
            return True, "traverse_split"

        # 도로만으로는 외곽으로 열린 블록이 시설을 더했을 때 닫히면 외곽경계 역할로 인정한다.
        before_open = primary.boundary.distance(frame_metric.boundary) <= 0.75
        if before_open:
            test_union = unary_union([barrier_union, fac]).buffer(0.20, join_style=2)
            test_parts = _street_block_site_parts(frame_metric, test_union, site_metric)
            if test_parts:
                after_primary = test_parts[0][0]
                after_open = after_primary.boundary.distance(frame_metric.boundary) <= 0.75
                if not after_open:
                    return True, "outer_boundary_closure"
    except Exception:
        return False, "geometry_error"
    return False, "isolated_internal"




def _basic_unit_zip_path() -> Optional[str]:
    """SGIS 2025 기초단위구 경계 ZIP을 찾는다.

    권장 파일명은 ``basic_unit_seoul.zip``이다. SGIS 원본 파일명을 유지해도
    파일명에 '기초단위구' 또는 'basic_unit'이 있으면 자동 인식한다.
    """
    preferred = [
        _data_path("basic_unit_seoul.zip"),
        _data_path("sgis_basic_unit_seoul.zip"),
        _data_path("basic_unit_2025_seoul.zip"),
    ]
    for path in preferred:
        if os.path.isfile(path):
            return path
    try:
        for name in os.listdir(DATA_DIR):
            low = name.lower()
            if not low.endswith('.zip'):
                continue
            if '기초단위구' in name or ('basic' in low and 'unit' in low):
                return os.path.join(DATA_DIR, name)
    except Exception:
        pass
    return None


@lru_cache(maxsize=1)
def _basic_unit_spatial_layers() -> Dict[str, Any]:
    """SGIS 기초단위구 SHP를 WGS84로 읽고 서울 영역만 공간색인한다.

    SGIS 자료제공 기준 좌표계는 EPSG:5179이며, ZIP 안 PRJ가 있으면 그 값을
    우선한다. 기초단위구는 가로구역 그 자체가 아니라 자동추정의 seed이다.
    """
    zip_path = _basic_unit_zip_path()
    if not zip_path:
        return {"available": False, "reason": "SGIS 기초단위구 ZIP 미설치"}
    rows: List[Dict[str, Any]] = []
    seoul_bbox = box(126.70, 37.40, 127.30, 37.75)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            stems = sorted({os.path.splitext(n)[0] for n in names if n.lower().endswith('.shp')})
            # 서울(11)로 보이는 stem을 먼저 읽는다. 원본명 규칙이 달라도 bbox 필터가 최종 검증한다.
            stems.sort(key=lambda x: (0 if re.search(r'(^|[_\\/])11([_\\/.]|$)|seoul|서울', x, re.I) else 1, x))
            for stem in stems:
                shp_name = next((n for n in names if os.path.splitext(n)[0] == stem and n.lower().endswith('.shp')), None)
                shx_name = next((n for n in names if os.path.splitext(n)[0] == stem and n.lower().endswith('.shx')), None)
                dbf_name = next((n for n in names if os.path.splitext(n)[0] == stem and n.lower().endswith('.dbf')), None)
                if not (shp_name and shx_name and dbf_name):
                    continue
                prj_name = next((n for n in names if os.path.splitext(n)[0] == stem and n.lower().endswith('.prj')), None)
                source_crs = CRS.from_epsg(5179)
                if prj_name:
                    try:
                        source_crs = CRS.from_wkt(zf.read(prj_name).decode('utf-8', errors='ignore'))
                    except Exception:
                        pass
                to_wgs = Transformer.from_crs(source_crs, 4326, always_xy=True).transform
                reader = shapefile.Reader(
                    shp=io.BytesIO(zf.read(shp_name)),
                    shx=io.BytesIO(zf.read(shx_name)),
                    dbf=io.BytesIO(zf.read(dbf_name)),
                    encoding='cp949', encodingErrors='replace'
                )
                fields = [f[0] for f in reader.fields[1:]]
                stem_added = 0
                for sr in reader.iterShapeRecords():
                    try:
                        geom = _polygonal_only(shape(sr.shape.__geo_interface__))
                        if geom is None or geom.is_empty:
                            continue
                        if not geom.is_valid:
                            geom = _polygonal_only(geom.buffer(0))
                        if geom is None or geom.is_empty:
                            continue
                        geom = geometry_transform(to_wgs, geom)
                        if geom.is_empty or not geom.intersects(seoul_bbox):
                            continue
                        props = {k: _json_property(v) for k, v in zip(fields, list(sr.record))}
                        props['_basic_unit_stem'] = os.path.basename(stem)
                        rows.append({'geometry': geom, 'properties': props})
                        stem_added += 1
                    except Exception:
                        continue
                # 시도단위 파일에서 서울 stem을 찾은 경우 다른 시도 stem 전체를 불필요하게 읽지 않는다.
                if stem_added > 100 and re.search(r'(^|[_\\/])11([_\\/.]|$)|seoul|서울', stem, re.I):
                    break
    except Exception as exc:
        return {"available": False, "reason": f"기초단위구 ZIP 읽기 실패: {exc}", "file": os.path.basename(zip_path)}
    if not rows:
        return {"available": False, "reason": "기초단위구 ZIP에서 서울 Polygon을 찾지 못함", "file": os.path.basename(zip_path)}
    geoms = [r['geometry'] for r in rows]
    base_dates = sorted({str((r.get('properties') or {}).get('BASE_DATE') or '').strip() for r in rows if str((r.get('properties') or {}).get('BASE_DATE') or '').strip()})
    return {
        'available': True,
        'rows': rows,
        'tree': STRtree(geoms),
        'source': '국가데이터처 SGIS 2025 기초단위구 경계(시도)',
        'file': os.path.basename(zip_path),
        'feature_count': len(rows),
        'base_dates': base_dates,
        'base_date': base_dates[-1] if base_dates else None,
        'source_crs_note': 'SGIS 제공기준 EPSG:5179 · PRJ 우선',
    }


def _shared_edge_barrier(shared: Any, road_union: Any, strong_union: Any = None) -> tuple[bool, str, float]:
    """두 기초단위구의 공통경계가 4m+ 도로/철도/하천에 의해 실제로 막히는지 판정한다."""
    try:
        length = float(shared.length)
        if length < 1.0:
            return False, 'point_or_short_touch', 0.0
        corridor = shared.buffer(1.25, cap_style=2, join_style=2)
        if corridor.is_empty or corridor.area <= 0:
            return False, 'empty_corridor', 0.0
        road_ratio = 0.0
        if road_union is not None and not road_union.is_empty and corridor.intersects(road_union):
            road_ratio = float(corridor.intersection(road_union).area) / float(corridor.area)
            if road_ratio >= 0.22:
                return True, 'road4m', road_ratio
        if strong_union is not None and not strong_union.is_empty and corridor.intersects(strong_union):
            strong_ratio = float(corridor.intersection(strong_union).area) / float(corridor.area)
            if strong_ratio >= 0.12:
                return True, 'rail_or_river', strong_ratio
        return False, 'mergeable', road_ratio
    except Exception:
        return False, 'geometry_error', 0.0


def _basic_unit_component(start_idx: int, geoms: List[Any], tree: Any, road_union: Any, strong_union: Any, max_units: int = 240) -> tuple[set[int], bool]:
    selected = {int(start_idx)}
    queue = [int(start_idx)]
    hit_limit = False
    while queue:
        idx = queue.pop(0)
        geom = geoms[idx]
        try:
            candidates = tree.query(geom.buffer(0.8), predicate='intersects')
        except Exception:
            candidates = []
        for raw in candidates:
            j = int(raw)
            if j == idx or j in selected:
                continue
            other = geoms[j]
            try:
                shared = geom.boundary.intersection(other.boundary)
                if shared.is_empty or float(shared.length) < 1.0:
                    continue
            except Exception:
                continue
            blocked, _, _ = _shared_edge_barrier(shared, road_union, strong_union)
            if blocked:
                continue
            selected.add(j)
            queue.append(j)
            if len(selected) >= max_units:
                hit_limit = True
                return selected, hit_limit
    return selected, hit_limit


def _street_block_from_basic_units(
    geometry: Dict[str, Any],
    barrier_features: Optional[List[Dict[str, Any]]] = None,
    road_features: Optional[List[Dict[str, Any]]] = None,
    max_radius_m: float = 500.0,
) -> Optional[Dict[str, Any]]:
    """SGIS 기초단위구를 seed로 삼고 VWorld 도로중심선 ROAD_BT로 병합여부를 판단한다.

    도로폭은 TL_SPRD_MANAGE의 ROAD_BT 등 공식 폭원 속성만 읽고,
    4m 이상 도로중심선이 기초단위구 공통경계를 가르는지를 병합/분리 판단의
    보조 Fact로 사용한다.
    """
    units = _basic_unit_spatial_layers()
    if not units.get('available'):
        return None
    try:
        site_wgs = _polygonal_only(shape(geometry))
    except Exception as exc:
        raise ValueError(f'구역계 GeoJSON을 읽을 수 없습니다: {exc}') from exc
    if site_wgs is None or site_wgs.is_empty:
        raise ValueError('구역계는 Polygon 또는 MultiPolygon이어야 합니다.')
    if not site_wgs.is_valid:
        site_wgs = _polygonal_only(site_wgs.buffer(0))
    if site_wgs is None or site_wgs.is_empty:
        raise ValueError('유효하지 않은 구역계입니다.')

    to_metric = Transformer.from_crs(4326, 5174, always_xy=True).transform
    to_wgs = Transformer.from_crs(5174, 4326, always_xy=True).transform
    site_metric = geometry_transform(to_metric, site_wgs)
    site_area = max(1.0, float(site_metric.area))
    frame_metric = site_metric.buffer(float(max_radius_m), cap_style=3, join_style=2).envelope
    frame_wgs = geometry_transform(to_wgs, frame_metric)

    local_rows: List[Dict[str, Any]] = []
    unit_tree = units['tree']
    for raw in unit_tree.query(frame_wgs, predicate='intersects'):
        row = units['rows'][int(raw)]
        try:
            gm = geometry_transform(to_metric, row['geometry'])
            if gm.is_empty or not gm.intersects(frame_metric):
                continue
            local_rows.append({'metric': gm, 'row': row})
        except Exception:
            continue
    if not local_rows:
        return {
            'status':'unresolved','block':None,'blocks':{'type':'FeatureCollection','features':[]},
            'road_barriers':{'type':'FeatureCollection','features':[]},'road_context':{'type':'FeatureCollection','features':[]},
            'facility_barriers':{'type':'FeatureCollection','features':[]},'basic_unit_context':{'type':'FeatureCollection','features':[]},
            'metadata':{'method':'sgis_basic_unit_roadbt_merge','reason':'대상지 주변 기초단위구 없음','basic_unit_file':units.get('file')}
        }

    road_min_width_m = 4.0
    if not road_features:
        return {
            'status':'unavailable','block':None,'blocks':{'type':'FeatureCollection','features':[]},
            'road_barriers':{'type':'FeatureCollection','features':[]},'road_context':{'type':'FeatureCollection','features':[]},
            'facility_barriers':{'type':'FeatureCollection','features':[]},'basic_unit_context':{'type':'FeatureCollection','features':[]},
            'metadata':{'method':'sgis_basic_unit_roadbt_merge','reason':'TL_SPRD_MANAGE ROAD_BT 도로자료 미확보','basic_unit_file':units.get('file')}
        }
    selected_items: List[Dict[str, Any]] = []
    road_metric: List[Any] = []
    under4_count = 0
    unknown_width_count = 0
    for feat in road_features or []:
        props = (feat or {}).get('properties') or {}
        width = _road_width_m(props)
        try:
            geom = shape((feat or {}).get('geometry') or {})
            if geom is None or geom.is_empty:
                continue
            gm = geometry_transform(to_metric, geom)
            if gm.is_empty or not gm.intersects(frame_metric):
                continue
            selected_items.append({'feature':feat,'metric':gm,'width':width})
            if width is None:
                unknown_width_count += 1
            elif width >= road_min_width_m:
                # 폭 자체를 면도형으로 재현하려는 것이 아니라 공통경계와 도로중심선의
                # 위치관계를 확인하기 위한 작은 위상 허용폭만 사용한다.
                road_metric.append(gm.buffer(1.0, cap_style=2, join_style=2))
            else:
                under4_count += 1
        except Exception:
            continue
    road_union = unary_union(road_metric).buffer(0) if road_metric else GeometryCollection()

    strong_features: List[Dict[str, Any]] = []
    strong_metric: List[Any] = []
    for feat in barrier_features or []:
        p = (feat or {}).get('properties') or {}
        typ = str(p.get('_block_barrier_type') or '')
        if not re.search(r'철도|하천', typ):
            continue
        try:
            gm = _polygonal_only(shape((feat or {}).get('geometry') or {}))
            if gm is None or gm.is_empty:
                continue
            mm = geometry_transform(to_metric, gm)
            if mm.intersects(frame_metric):
                strong_metric.append(mm)
                strong_features.append(feat)
        except Exception:
            continue
    strong_union = unary_union(strong_metric).buffer(0.10, join_style=2) if strong_metric else GeometryCollection()

    geoms = [x['metric'] for x in local_rows]
    tree = STRtree(geoms)
    initial: List[int] = []
    for i, gm in enumerate(geoms):
        try:
            ia = float(gm.intersection(site_metric).area)
        except Exception:
            ia = 0.0
        if ia >= max(1.0, site_area * 0.002):
            initial.append(i)
    if not initial:
        return {
            'status':'unresolved','block':None,'blocks':{'type':'FeatureCollection','features':[]},
            'road_barriers':{'type':'FeatureCollection','features':[]},'road_context':{'type':'FeatureCollection','features':[]},
            'facility_barriers':{'type':'FeatureCollection','features':strong_features},
            'basic_unit_context':{'type':'FeatureCollection','features':[]},
            'metadata':{'method':'sgis_basic_unit_roadbt_merge','reason':'대상지와 중첩되는 기초단위구 없음','basic_unit_file':units.get('file')}
        }

    components: List[tuple[set[int], Any, float, bool]] = []
    seen_keys = set()
    for start in initial:
        comp, hit_limit = _basic_unit_component(start, geoms, tree, road_union, strong_union)
        key = tuple(sorted(comp))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        merged = unary_union([geoms[i] for i in comp]).buffer(0)
        try:
            ia = float(merged.intersection(site_metric).area)
        except Exception:
            ia = 0.0
        if ia > max(1.0, site_area * 0.002):
            components.append((comp, merged, ia, hit_limit))
    components.sort(key=lambda x: x[2], reverse=True)
    if not components:
        return None

    significant = [x for x in components if x[2] >= max(5.0, site_area * 0.05)]
    primary_comp, primary, primary_site_area, primary_limit = components[0]
    block_area = float(primary.area)
    multi = len(significant) > 1

    context_idxs = set()
    for comp, _, _, _ in components[:6]:
        context_idxs.update(comp)
    for i in list(context_idxs):
        try:
            for raw in tree.query(geoms[i].buffer(1.0), predicate='intersects'):
                context_idxs.add(int(raw))
        except Exception:
            pass
        if len(context_idxs) > 160:
            break
    unit_context = []
    for i in list(context_idxs)[:160]:
        row = local_rows[i]['row']
        props = dict(row.get('properties') or {})
        props.update({'_basic_unit_selected': i in primary_comp, '_basic_unit_seed': True})
        unit_context.append({'type':'Feature','geometry':mapping(row['geometry']),'properties':props})

    map_margin = primary.buffer(35)
    road_barriers, road_context = [], []
    for item in selected_items:
        feat, gm, width = item['feature'], item['metric'], item['width']
        try:
            if not gm.intersects(map_margin):
                continue
            props = dict((feat or {}).get('properties') or {})
            props.update({'_block_width_m':width,'_block_barrier_used':bool(width is not None and width >= road_min_width_m),
                          '_block_width_basis':'TL_SPRD_MANAGE ROAD_BT'})
            out = {'type':'Feature','geometry':(feat or {}).get('geometry'),'properties':props}
            (road_barriers if width is not None and width >= road_min_width_m else road_context).append(out)
        except Exception:
            continue

    primary_wgs = geometry_transform(to_wgs, primary)
    primary_site_pct = primary_site_area / site_area * 100.0 if site_area > 0 else None
    primary_block_occupancy_pct = primary_site_area / block_area * 100.0 if block_area > 0 else None
    status = 'resolved' if not primary_limit else 'partial'
    block_features=[]
    for comp,g,ia,_ in significant[:12]:
        ba=float(g.area)
        block_features.append({'type':'Feature','geometry':mapping(geometry_transform(to_wgs,g)),'properties':{
            'site_intersection_m2':ia,'block_area_m2':ba,'site_share_of_block_pct':(ia/ba*100.0 if ba>0 else None),
            'block_coverage_of_site_pct':(ia/site_area*100.0 if site_area>0 else None),'merged_basic_units':len(comp)}})
    return {
        'status': status,
        'block': {'type':'Feature','geometry':mapping(primary_wgs),'properties':{
            'block_area_m2':block_area,'site_intersection_m2':primary_site_area,
            'site_share_of_block_pct':primary_block_occupancy_pct,'block_coverage_of_site_pct':primary_site_pct,
            'road_min_width_m':road_min_width_m,'source_method':'sgis_basic_unit_roadbt_merge',
            'merged_basic_units':len(primary_comp)}},
        'blocks': {'type':'FeatureCollection','features':block_features},
        'basic_unit_context': {'type':'FeatureCollection','features':unit_context},
        'road_barriers': {'type':'FeatureCollection','features':road_barriers},
        'road_context': {'type':'FeatureCollection','features':road_context},
        'facility_barriers': {'type':'FeatureCollection','features':strong_features},
        'metadata': {
            'method':'sgis_basic_unit_roadbt_merge',
            'basic_unit_source':units.get('source'),'basic_unit_file':units.get('file'),'basic_unit_feature_count':units.get('feature_count'),
            'local_basic_unit_count':len(local_rows),'merged_basic_unit_count':len(primary_comp),
            'road_source':'VWorld TL_SPRD_MANAGE ROAD_BT','road_mode':'centerline_width_attribute','road_min_width_m':road_min_width_m,
            'road_count':len(road_barriers),'road_under4_context_count':len(road_context),'road_under4_total_count':under4_count,
            'road_width_unknown_count':unknown_width_count,'strong_facility_count':len(strong_features),
            'block_area_m2':block_area,'site_intersection_m2':primary_site_area,
            'site_primary_block_pct':primary_site_pct,'site_share_of_primary_block_pct':primary_block_occupancy_pct,
            'site_spans_multiple_blocks':multi,'merge_limit_reached':primary_limit,'legal_width_rule':False,
            'basic_unit_is_legal_street_block':False,'authoritative_street_block':False,
            'future_street_block_interface':'MOIS_BASIC_UNIT_OR_VERIFIED_PLANNING_ROAD_BLOCK',
            'engine_note':'현재 내장 기초단위구는 가로구역 후보 골격(ESTIMATE)이다. VWorld TL_SPRD_MANAGE ROAD_BT(4m 이상)와 철도·하천으로 인접 기초단위구 병합 여부를 판단하며 법정 가로구역으로 자동확정하지 않는다. 향후 행안부 기초단위구/공식 가로구역 또는 검증된 도시계획시설도로 블록 자료가 연결되면 authoritative_street_block=true로 승격한다.',
        }
    }


def analyze_street_block(
    geometry: Dict[str, Any],
    barrier_features: Optional[List[Dict[str, Any]]] = None,
    road_features: Optional[List[Dict[str, Any]]] = None,
    max_radius_m: float = 500.0,
) -> Dict[str, Any]:
    """기초단위구 seed + 도로중심선 ROAD_BT 방식만 사용한다.

    기초단위구가 없거나 자동확정에 실패하면 자료부족을 명시한다.
    도로 Fact는 TL_SPRD_MANAGE ROAD_BT만 사용한다.
    """
    basic = _street_block_from_basic_units(geometry, barrier_features, road_features, max_radius_m)
    if basic is not None:
        return basic
    unit_layers = _basic_unit_spatial_layers()
    return {
        'status':'unavailable','block':None,'blocks':{'type':'FeatureCollection','features':[]},
        'road_barriers':{'type':'FeatureCollection','features':[]},'road_context':{'type':'FeatureCollection','features':[]},
        'facility_barriers':{'type':'FeatureCollection','features':[]},'basic_unit_context':{'type':'FeatureCollection','features':[]},
        'metadata':{
            'method':'sgis_basic_unit_roadbt_merge','preferred_method':'sgis_basic_unit_roadbt_merge',
            'basic_unit_available':bool(unit_layers.get('available')),
            'basic_unit_reason':None if unit_layers.get('available') else unit_layers.get('reason'),
            'road_feature_count':len(road_features or []),'fallback_used':False,
            'reason':'기초단위구 자료가 없거나 가로구역 후보를 자동확정하지 못했습니다. TL_SPRD_MANAGE ROAD_BT 자료를 확인하세요.'
        }
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



# ============================================================
# 서울 구릉지 공식 원도형 (도시·주거환경정비기본계획/생활권계획)
# - 공식 계획의 구릉지 기준: 해발고도 40m 이상 + 경사도 10도 이상
# - 운영 플랫폼에서는 임의 DEM 재생성 도형을 확정값으로 사용하지 않는다.
# - 서울시 원 SHP/ZIP을 SEOUL_HILL_SHP_PATH 또는 아래 파일명으로 배치하면
#   대상구역 중첩·최근접 거리를 서버에서 계산한다.
# ============================================================
HILL_SOURCE_CANDIDATES = (
    "hill_seoul.zip", "seoul_hill.zip", "seoul_hillside.zip", "hillside_seoul.zip",
    "구릉지.zip", "서울시_구릉지.zip", "서울시구릉지.zip",
)
HILL_SOURCE_ENV = "SEOUL_HILL_SHP_PATH"
HILL_SOURCE_TITLE = "서울시 도시·주거환경정비기본계획/생활권계획 구릉지 원도형"
HILL_CRITERION = "해발고도 40m 이상 AND 경사도 10도 이상"


def _hill_zip_path() -> Optional[str]:
    env = os.getenv(HILL_SOURCE_ENV, "").strip()
    candidates = []
    if env:
        candidates.append(env if os.path.isabs(env) else os.path.join(DATA_DIR, env))
    candidates.extend(_data_path(name) for name in HILL_SOURCE_CANDIDATES)
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def _decode_zip_prj(raw: bytes) -> str:
    for enc in ("utf-8", "cp949", "euc-kr", "latin1"):
        try:
            return raw.decode(enc).strip()
        except Exception:
            pass
    return ""


def _hill_source_crs(zf: zipfile.ZipFile, shp_name: str) -> CRS:
    stem = shp_name.rsplit(".", 1)[0]
    prj_name = next((n for n in zf.namelist() if n.rsplit(".",1)[0].lower() == stem.lower() and n.lower().endswith('.prj')), None)
    if prj_name:
        try:
            txt = _decode_zip_prj(zf.read(prj_name))
            if txt:
                return CRS.from_wkt(txt)
        except Exception:
            logging.exception("failed to parse hill SHP PRJ")
    # 서울시 생활권/UPIS 공간자료의 공개 SHP 기본좌표계와 동일한 fallback.
    return CRS.from_epsg(5174)


def _hill_category_from_row(row: Dict[str, Any]) -> str:
    texts = [str(v or '').strip() for v in row.values()]
    merged = ' '.join(texts)
    if re.search(r"훼손\s*\(?우려\)?|훼손우려", merged):
        return "훼손(우려) 구릉지"
    if "양호" in merged and "구릉" in merged:
        return "양호한 구릉지"
    if "양호" in merged:
        return "양호한 구릉지"
    if "구릉" in merged:
        return next((t for t in texts if "구릉" in t), "구릉지")
    return "구릉지"


@lru_cache(maxsize=1)
def _hill_reference_data() -> Dict[str, Any]:
    zip_path = _hill_zip_path()
    if not zip_path:
        return {
            "type":"FeatureCollection", "features":[],
            "metadata":{
                "available":False, "source_title":HILL_SOURCE_TITLE,
                "criterion":HILL_CRITERION, "source_path":None,
                "status":"OFFICIAL_SHP_NOT_BUNDLED",
                "note":"공식 구릉지 원도형 SHP를 찾지 못했습니다. 임의 DEM 복원도형으로 자동 PASS/FAIL하지 않습니다.",
            },
        }
    with zipfile.ZipFile(zip_path) as zf:
        shp_names = [n for n in zf.namelist() if n.lower().endswith('.shp') and not n.endswith('/')]
        if not shp_names:
            raise RuntimeError(f"구릉지 ZIP에 SHP가 없습니다: {os.path.basename(zip_path)}")
        # 원본 ZIP에 여러 SHP가 있을 때 파일명에 구릉/hill이 있는 것을 우선한다.
        shp_name = next((n for n in shp_names if re.search(r"구릉|hill|slope", n, re.I)), shp_names[0])
        stem = shp_name.rsplit('.',1)[0]
        dbf_name = next((n for n in zf.namelist() if n.rsplit('.',1)[0].lower()==stem.lower() and n.lower().endswith('.dbf')), None)
        shx_name = next((n for n in zf.namelist() if n.rsplit('.',1)[0].lower()==stem.lower() and n.lower().endswith('.shx')), None)
        if not dbf_name:
            raise RuntimeError(f"구릉지 SHP의 DBF가 없습니다: {shp_name}")
        kwargs={"shp":io.BytesIO(zf.read(shp_name)),"dbf":io.BytesIO(zf.read(dbf_name)),"encoding":"cp949"}
        if shx_name: kwargs["shx"]=io.BytesIO(zf.read(shx_name))
        try:
            reader=shapefile.Reader(**kwargs)
        except UnicodeDecodeError:
            kwargs["encoding"]="utf-8"
            reader=shapefile.Reader(**kwargs)
        fields=[f[0] for f in reader.fields[1:]]
        src_crs=_hill_source_crs(zf, shp_name)
        to_wgs=Transformer.from_crs(src_crs,4326,always_xy=True).transform
        features=[]
        for sr in reader.iterShapeRecords():
            row=dict(zip(fields,sr.record))
            try:
                geom=_polygonal_only(shape(sr.shape.__geo_interface__))
                if geom is None or geom.is_empty: continue
                if not geom.is_valid: geom=_polygonal_only(geom.buffer(0))
                if geom is None or geom.is_empty: continue
                # 0.25m 단순화는 원자료 정밀도를 해치지 않으면서 웹 payload를 줄인다.
                geom=geom.simplify(0.25,preserve_topology=True)
                geom=geometry_transform(to_wgs,geom)
            except Exception:
                continue
            name=str(row.get('DGM_NM') or row.get('NAME') or row.get('NM') or '').strip()
            features.append({
                "type":"Feature","geometry":mapping(geom),
                "properties":{
                    "category":_hill_category_from_row(row),
                    "name":name,
                    "source_title":HILL_SOURCE_TITLE,
                    "source_file":os.path.basename(zip_path),
                    "source_layer":shp_name,
                    "criterion":HILL_CRITERION,
                }
            })
    return {
        "type":"FeatureCollection","features":features,
        "metadata":{
            "available":True,"source_title":HILL_SOURCE_TITLE,"criterion":HILL_CRITERION,
            "source_file":os.path.basename(zip_path),"source_layer":shp_name,
            "source_crs":src_crs.to_string(),"feature_count":len(features),
            "status":"OFFICIAL_SHP_READY",
            "note":"서울시 공식 원도형으로만 확정 중첩을 계산합니다.",
        }
    }


@lru_cache(maxsize=1)
def _hill_spatial_index():
    fc=_hill_reference_data(); features=fc.get('features') or []
    geoms=[shape(f['geometry']) for f in features]
    return features, geoms, STRtree(geoms) if geoms else None


def analyze_hill_intersections(geometry: Dict[str, Any]) -> Dict[str, Any]:
    try:
        site_wgs=_polygonal_only(shape(geometry))
    except Exception as exc:
        raise ValueError(f"구역계 GeoJSON을 읽을 수 없습니다: {exc}") from exc
    if site_wgs is None or site_wgs.is_empty:
        raise ValueError("구역계는 Polygon 또는 MultiPolygon이어야 합니다.")
    if not site_wgs.is_valid:
        site_wgs=_polygonal_only(site_wgs.buffer(0))
    if site_wgs is None or site_wgs.is_empty:
        raise ValueError("유효하지 않은 구역계입니다.")
    fc=_hill_reference_data(); meta=dict(fc.get('metadata') or {})
    if not meta.get('available'):
        return {
            "status":"unavailable","source_status":"OFFICIAL_SHP_NOT_BUNDLED",
            "intersects":None,"overlap_area_m2":None,"overlap_pct":None,"distance_m":None,
            "overlaps":[],"context_features":[],"metadata":meta,
        }
    features,geoms,tree=_hill_spatial_index()
    to_metric=Transformer.from_crs(4326,5174,always_xy=True).transform
    site_metric=geometry_transform(to_metric,site_wgs); site_area=float(site_metric.area)
    overlaps=[]; context=[]; union_parts=[]
    for idx in tree.query(site_wgs,predicate='intersects'):
        i=int(idx); src=geoms[i]
        try:
            inter=_polygonal_only(site_wgs.intersection(src))
            if inter is None or inter.is_empty: continue
            inter_m=_polygonal_only(geometry_transform(to_metric,inter))
            if inter_m is None or inter_m.is_empty or inter_m.area<0.5: continue
        except Exception:
            continue
        union_parts.append(inter_m)
        props=dict(features[i].get('properties') or {})
        props['overlap_area_m2']=round(float(inter_m.area),2)
        overlaps.append({"type":"Feature","geometry":mapping(inter),"properties":props})
        context.append(features[i])
    union=unary_union(union_parts) if union_parts else None
    overlap_area=float(union.area) if union is not None and not union.is_empty else 0.0
    distance_m=0.0 if overlap_area>0 else None
    nearest_feature=None
    if overlap_area<=0 and tree is not None and geoms:
        try:
            nearest_idx=int(tree.nearest(site_wgs)); nearest=geoms[nearest_idx]
            nearest_feature=features[nearest_idx]
            distance_m=float(site_metric.distance(geometry_transform(to_metric,nearest)))
            # 연접 판단을 사용자가 검증할 수 있도록 최근접 공식 도형도 반환한다.
            context=[nearest_feature]
        except Exception:
            distance_m=None
    return {
        "status":"confirmed","source_status":"OFFICIAL_SHP_READY",
        "intersects":overlap_area>0.5,
        "overlap_area_m2":round(overlap_area,2),
        "overlap_pct":round(overlap_area/site_area*100,4) if site_area>0 else None,
        "distance_m":round(distance_m,2) if distance_m is not None else None,
        "overlaps":overlaps,"context_features":context,"metadata":meta,
    }

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

    Exact configured names are checked first.  A normalized fallback is kept only
    to survive hosting-platform name normalization; the secret value is never
    exposed in API responses/logs.
    """
    for env_name in SEOUL_OPEN_DATA_KEY_ENV_NAMES:
        value = (os.getenv(env_name) or "").strip()
        if value:
            return value, env_name

    aliases = {
        "seoulopendatakey",
        "dataseoulgokrkey",
    }
    for env_name, raw in os.environ.items():
        normalized = re.sub(r"[^a-z0-9]", "", str(env_name).lower())
        if normalized in aliases:
            value = str(raw or "").strip()
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

@lru_cache(maxsize=8)
def _seoul_space_catalog_keyword(keyword: str) -> Dict[str, Any]:
    """Search Seoul's spatial-information inventory for a legacy/original layer.

    This is metadata discovery only. A catalogue hit never becomes a spatial PASS;
    the actual official polygon ZIP must still be bundled/connected.
    """
    key=(keyword or '').strip()[:40]
    if not key:
        return {"status":"invalid","keyword":key,"matches":[]}
    if not _seoul_open_data_key():
        return {"status":"unavailable","keyword":key,"matches":[],"message":"서울 열린데이터 API 키 미설정"}
    try:
        rows=_seoul_open_data_rows('spaceInfoList', limit=30000)
    except Exception as exc:
        return {"status":"error","keyword":key,"matches":[],"message":str(exc)}
    needle=_name_key(key)
    search_fields=('KORN_NM','ENG_NM','DATA_INFO','BIZ_NM','SPC_DATA_CRT_ORGNL_DATA','SYS_NM','LYR_ID')
    out=[]
    for row in rows:
        blob=' '.join(str(row.get(f) or '') for f in search_fields)
        if needle and needle not in _name_key(blob):
            continue
        out.append({f:row.get(f) for f in ('LYR_ID','KORN_NM','ENG_NM','DATA_INFO','BIZ_NM','SPC_DATA_CRT_ORGNL_DATA','VCTR','RST','CRD','ETBL_SCP','FRST_CRT_YMD','LAST_UPDT_YMD','RLS_YN','RLS_LMT_BSS') if f in row})
    return {"status":"ok","keyword":key,"matches":out,"scanned":len(rows),"service":"spaceInfoList"}

def _name_key(value: Any) -> str:
    return re.sub(r"[^가-힣A-Za-z0-9]", "", str(value or "")).lower()

def _safe_medical_reference_data() -> Dict[str, Any]:
    path = _data_path("safe_medical_reference.json")
    try:
        with open(path, encoding="utf-8") as fp:
            data = json.load(fp)
        if not isinstance(data, dict):
            raise ValueError("JSON root is not an object")
        return data
    except Exception as exc:
        logger.error("safe medical reference load failed path=%s error=%s", path, exc)
        return {"health_centers": [], "municipal_hospitals": [], "sources": {}, "load_error": str(exc)}

def _safe_medical_name_match(name: str, ref: Dict[str, Any]) -> bool:
    nk = _name_key(name)
    if not nk:
        return False
    values = [ref.get("name")] + list(ref.get("aliases") or [])
    for value in values:
        rk = _name_key(value)
        if rk and (rk == nk or rk in nk or nk in rk):
            return True
    return False

def _tb_hospital_snapshot_rows() -> List[Dict[str, Any]]:
    path = _data_path("TbHospitalInfo_snapshot_20260808.csv")
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, encoding="utf-8-sig", newline="") as fp:
        for r in csv.DictReader(fp):
            out.append({
                "HPID": r.get("기관ID"), "DUTYADDR": r.get("주소"),
                "DUTYDIV": r.get("병원분류"), "DUTYDIVNAM": r.get("병원분류명"),
                "DUTYNAME": r.get("기관명"), "WGS84LON": r.get("병원경도"),
                "WGS84LAT": r.get("병원위도"), "WORK_DTTM": r.get("작업시간"),
            })
    return out

def _tb_hospital_rows_live_or_snapshot() -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """TbHospitalInfo live first; bundled monthly snapshot is a continuity fallback."""
    key, key_env = _seoul_open_data_key_info()
    if key:
        try:
            rows = _seoul_open_data_rows("TbHospitalInfo", 20000)
            if rows:
                return rows, {"service": "TbHospitalInfo", "mode": "live", "rows": len(rows), "credential_env": key_env}
        except Exception as exc:
            live_error = str(exc)
        else:
            live_error = "empty response"
    else:
        live_error = "서울 열린데이터광장 인증키 미설정"
    rows = _tb_hospital_snapshot_rows()
    return rows, {
        "service": "TbHospitalInfo", "mode": "snapshot_fallback", "rows": len(rows),
        "snapshot": "data/TbHospitalInfo_snapshot_20260808.csv", "live_error": live_error,
    }

def _row_wgs84_point(row: Dict[str, Any]) -> Optional[tuple[float, float]]:
    try:
        lon, lat = float(row.get("WGS84LON")), float(row.get("WGS84LAT"))
        if 124 <= lon <= 132 and 33 <= lat <= 39:
            return lon, lat
    except Exception:
        pass
    return None

@lru_cache(maxsize=512)
def _representative_parcel_cached(lon_key: Optional[float], lat_key: Optional[float], address: str) -> Dict[str, Any]:
    """Resolve one representative parcel and cache the result for repeated analyses."""
    point_result = None
    if lon_key is not None and lat_key is not None:
        try:
            point_result = _vworld_parcel_at_point(float(lon_key), float(lat_key))
        except Exception as exc:
            point_result = {"status": "error", "feature": None, "pnu": None, "reason": str(exc)}
        if point_result.get("status") == "resolved" and point_result.get("feature"):
            return {**point_result, "basis": "official_coordinate"}
    if address:
        try:
            addr_result = _vworld_parcel_by_address(address)
        except Exception as exc:
            addr_result = {"status": "error", "feature": None, "pnu": None, "reason": str(exc)}
        if addr_result.get("status") == "resolved" and addr_result.get("feature"):
            return {**addr_result, "basis": "official_address"}
        if point_result:
            return {**addr_result, "point_status": point_result.get("status"), "point_reason": point_result.get("reason")}
        return addr_result
    return point_result or {"status": "not_found", "feature": None, "pnu": None, "reason": "좌표·주소 없음"}


def _representative_parcel_for_facility(*, lon: Optional[float], lat: Optional[float], address: str) -> Dict[str, Any]:
    lon_key = round(float(lon), 7) if lon is not None else None
    lat_key = round(float(lat), 7) if lat is not None else None
    return _representative_parcel_cached(lon_key, lat_key, re.sub(r"\s+", " ", str(address or "")).strip())


def _medical_match_key(value: Any) -> str:
    """Conservative facility-name key: normalize punctuation and a leading Seoul-city prefix only."""
    k = _name_key(value)
    if k.startswith("서울특별시"):
        k = k[len("서울특별시"):]
    return k


def _safe_medical_match_ref(name: str, refs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Exact normalized match only. Avoid substring false matches such as generic '어린이병원'."""
    nk = _medical_match_key(name)
    if not nk:
        return None
    for ref in refs:
        values = [ref.get("name")] + list(ref.get("aliases") or [])
        if any(_medical_match_key(v) == nk for v in values if v):
            return ref
    return None


def _safe_medical_reference(geometry: Dict[str, Any]) -> Dict[str, Any]:
    """Fast safe-housing medical-center screen.

    Pipeline:
      1) Build the eligible point table from TbHospitalInfo + official whitelists.
      2) Screen all facilities locally by point distance first (no VWorld call).
      3) Resolve representative cadastral parcels only for facilities within 1.5 km.
      4) Create the exact 350 m buffer from each resolved parcel and return every match.

    This prevents city-wide parcel lookups from consuming the 60 s analysis budget.
    """
    try:
        site_wgs = _polygonal_only(shape(geometry))
    except Exception as exc:
        raise ValueError(f"구역계 GeoJSON을 읽을 수 없습니다: {exc}") from exc
    if site_wgs is None or site_wgs.is_empty:
        raise ValueError("구역계는 Polygon 또는 MultiPolygon이어야 합니다.")

    to_metric = Transformer.from_crs(4326, 5174, always_xy=True)
    site_metric = geometry_transform(to_metric.transform, site_wgs)
    ref = _safe_medical_reference_data()
    health_refs = list(ref.get("health_centers") or [])
    municipal_refs = list(ref.get("municipal_hospitals") or [])
    errors: List[str] = []
    warnings: List[str] = []
    stats: Dict[str, Any] = {}
    key, key_env = _seoul_open_data_key_info()
    metadata = {
        "criterion": "인정 의료시설 대표필지 경계로부터 350m",
        "boundary_method": "대표지번 1필지(초기검토용)",
        "representative_parcel_note": "실제 의료시설 대지와 일부 차이가 있을 수 있으므로 정밀검토 시 토지이용현황 재확인",
        "hospital_source": "서울시 병의원 위치 정보 TbHospitalInfo (월 1회 갱신; 장애 시 패키지 snapshot)",
        "health_center_source": "서울시 공식 25개 보건소 whitelist + TbHospitalInfo 현행 좌표",
        "municipal_hospital_source": "서울시 공식 서울시립병원 whitelist + TbHospitalInfo 현행 좌표",
        "official_rule": "안심주택 의료시설 중심지역: 종합병원·서울시 관리 시립병원·보건소",
        "screening_method": "시설 point 1.5km 선스크리닝 → 후보만 대표PNU/연속지적 조회 → 필지경계 350m",
        "credential_env": key_env or None,
        "reference_version": ref.get("version"),
    }

    rows, hospital_stat = _tb_hospital_rows_live_or_snapshot()
    stats["hospital"] = hospital_stat
    if not rows:
        errors.append("TbHospitalInfo 및 패키지 snapshot 모두 비어 있습니다.")

    # 실시간 API가 일부 공식 보건소·시립병원을 누락해도 인정대상 표가 줄지 않도록
    # 패키지 snapshot에서 공식 whitelist와 정확히 일치하는 행만 보충한다.
    official_snapshot_supplements = 0
    if rows and hospital_stat.get("mode") == "live":
        known_names = {_medical_match_key(r.get("DUTYNAME")) for r in rows if r.get("DUTYNAME")}
        supplemented = list(rows)
        for snap in _tb_hospital_snapshot_rows():
            name = str(snap.get("DUTYNAME") or "").strip()
            name_key = _medical_match_key(name)
            if not name_key or name_key in known_names:
                continue
            if _safe_medical_match_ref(name, health_refs) or _safe_medical_match_ref(name, municipal_refs):
                supplemented.append(snap)
                known_names.add(name_key)
                official_snapshot_supplements += 1
        rows = supplemented
    stats["hospital"]["official_snapshot_supplements"] = official_snapshot_supplements

    # Build a compact eligible point table without any cadastral/API work.
    point_candidates: List[Dict[str, Any]] = []
    municipal_seen = set()
    health_seen = set()
    general_seen = set()
    general_count = municipal_count = health_count = 0

    for row in rows:
        name = str(row.get("DUTYNAME") or "").strip()
        if not name:
            continue
        pt = _row_wgs84_point(row)
        if not pt:
            continue
        lon, lat = pt
        type_name = str(row.get("DUTYDIVNAM") or "").strip()
        address = str(row.get("DUTYADDR") or "").strip()

        h_ref = _safe_medical_match_ref(name, health_refs)
        m_ref = _safe_medical_match_ref(name, municipal_refs)
        if h_ref:
            canonical = str(h_ref.get("name") or name)
            ckey = _medical_match_key(canonical)
            if ckey in health_seen:
                continue
            health_seen.add(ckey)
            health_count += 1
            point_candidates.append({
                "category":"public_health_center", "name":canonical,
                "address":str(h_ref.get("address") or address), "lon":lon, "lat":lat,
                "facility_type":"보건소", "source":"서울시 공식 25개 보건소 + TbHospitalInfo",
                "source_service":"TbHospitalInfo", "institution_id":row.get("HPID"),
                "work_dttm":row.get("WORK_DTTM"), "district":h_ref.get("district"),
                "official_url":h_ref.get("official_url"),
            })
            continue
        if m_ref:
            canonical = str(m_ref.get("name") or name)
            ckey = _medical_match_key(canonical)
            if ckey in municipal_seen:
                continue
            municipal_seen.add(ckey)
            municipal_count += 1
            point_candidates.append({
                "category":"municipal_hospital", "name":canonical,
                "address":str(m_ref.get("address") or address), "lon":lon, "lat":lat,
                "facility_type":"시립병원", "source":"서울시 공식 시립병원 + TbHospitalInfo",
                "source_service":"TbHospitalInfo", "institution_id":row.get("HPID"),
                "work_dttm":row.get("WORK_DTTM"),
            })
            continue
        if type_name == "종합병원":
            general_key = _medical_match_key(name)
            if general_key in general_seen:
                continue
            general_seen.add(general_key)
            general_count += 1
            point_candidates.append({
                "category":"general_hospital", "name":name, "address":address,
                "lon":lon, "lat":lat, "facility_type":"종합병원",
                "source":"서울시 병의원 위치 정보", "source_service":"TbHospitalInfo",
                "institution_id":row.get("HPID"), "work_dttm":row.get("WORK_DTTM"),
            })

    # Point-distance screen FIRST. This is the key performance change.
    screened: List[Dict[str, Any]] = []
    for cand in point_candidates:
        try:
            p = shape({"type":"Point","coordinates":[float(cand["lon"]),float(cand["lat"])]})
            pm = geometry_transform(to_metric.transform, p)
            d = float(site_metric.distance(pm))
        except Exception:
            continue
        cand = dict(cand)
        cand["distance_point_m"] = round(d, 1)
        if d <= 1500.0:
            screened.append(cand)
    screened.sort(key=lambda x: float(x.get("distance_point_m") or 1e12))

    items: List[Dict[str, Any]] = []
    parcel_calls = 0
    for cand in screened:
        lon, lat = float(cand["lon"]), float(cand["lat"])
        parcel_calls += 1
        parcel = _representative_parcel_for_facility(lon=lon, lat=lat, address=str(cand.get("address") or ""))
        point_geom = {"type":"Point","coordinates":[lon,lat]}
        base = {
            "category":cand["category"], "name":cand["name"], "address":cand.get("address"),
            "geometry":point_geom, "distance_point_m":cand.get("distance_point_m"),
            "facility_type":cand.get("facility_type"), "source":cand.get("source"),
            "source_service":cand.get("source_service"), "institution_id":cand.get("institution_id"),
            "work_dttm":cand.get("work_dttm"),
        }
        for k in ("district","official_url"):
            if cand.get(k) is not None: base[k]=cand.get(k)
        if parcel.get("status") != "resolved" or not parcel.get("feature"):
            items.append({**base,
                "boundary_status":"REVIEW", "boundary_basis":"REPRESENTATIVE_PARCEL_NOT_RESOLVED",
                "boundary_note":f"대표필지 확정 실패: {parcel.get('reason') or parcel.get('status')}",
                "auto_pass_eligible":False,
            })
            continue
        feature = parcel["feature"]
        try:
            metrics = _medical_boundary_metrics(site_wgs, feature["geometry"])
        except Exception as exc:
            items.append({**base,
                "boundary_status":"REVIEW", "boundary_basis":"REPRESENTATIVE_PARCEL_GEOMETRY_ERROR",
                "boundary_note":f"대표필지 geometry 처리 실패: {exc}", "auto_pass_eligible":False,
            })
            continue
        items.append({**base,
            "distance_boundary_m":metrics.get("distance_boundary_m"), "within_350":metrics.get("within_350"),
            "buffer_350_geometry":metrics.get("buffer_350_geometry"), "facility_boundary_geometry":feature["geometry"],
            "primary_pnu":parcel.get("pnu"), "parcel_count":1,
            "boundary_status":"CONFIRMED", "boundary_basis":"REPRESENTATIVE_CADASTRAL_PARCEL",
            "boundary_basis_label":"대표지번 연속지적 필지", "parcel_candidate_basis":parcel.get("basis"),
            "boundary_note":"공식 좌표가 포함되는 대표지번 1필지를 초기검토용 의료시설 부지로 적용",
            "auto_pass_eligible":True,
        })

    confirmed = [x for x in items if x.get("boundary_status") == "CONFIRMED" and x.get("facility_boundary_geometry")]
    confirmed_350 = [x for x in confirmed if x.get("within_350") is True]
    review = [x for x in items if x.get("boundary_status") != "CONFIRMED"]
    items.sort(key=lambda x: float(x.get("distance_boundary_m") if x.get("distance_boundary_m") is not None else x.get("distance_point_m") if x.get("distance_point_m") is not None else 1e12))

    stats["medical_reference"] = {
        "point_table_total":len(point_candidates), "general_hospital":general_count,
        "municipal_hospital":municipal_count, "public_health_center":health_count,
        "official_health_centers":len(health_refs), "official_municipal_hospitals":len(municipal_refs),
        "point_screened_1500m":len(screened), "parcel_lookup_calls":parcel_calls,
        "municipal_unmatched":max(0,len(municipal_refs)-len(municipal_seen)),
        "health_center_unmatched":max(0,len(health_refs)-len(health_seen)),
    }
    stats["boundary_resolution"] = {"method":"point_prefilter_then_representative_parcel","confirmed":len(confirmed),"within_350":len(confirmed_350),"review":len(review)}
    nearby_counts = {
        "general_hospital":sum(1 for x in items if x.get("category")=="general_hospital"),
        "municipal_hospital":sum(1 for x in items if x.get("category")=="municipal_hospital"),
        "public_health_center":sum(1 for x in items if x.get("category")=="public_health_center"),
        "boundary_confirmed":len(confirmed), "boundary_confirmed_350":len(confirmed_350), "boundary_review":len(review),
    }
    if confirmed_350:
        status="resolved"; message=f"대표필지 경계 기준 350m 이내 인정 의료시설 {len(confirmed_350)}건 확인"
    elif confirmed:
        status="resolved"; message="대표필지는 확인됐으나 350m 이내 인정 의료시설은 확인되지 않았습니다."
    else:
        status="reference" if items else ("error" if errors else "none")
        message="의료시설 후보는 확인했으나 대표필지를 확정하지 못해 REVIEW입니다." if items else "인근 인정 의료시설 후보를 확인하지 못했습니다."
    return {
        "status":status, "auto_pass_eligible":bool(confirmed_350), "items":items[:40],
        "candidates_350":confirmed_350[:40], "metadata":metadata, "errors":errors, "warnings":warnings,
        "source_stats":stats, "nearby_counts":nearby_counts, "message":message,
    }


app = FastAPI(
    title="도시검토 플랫폼 - 서울 재개발 웹 MVP",
    version="2.5.0",
    description="구역계 자동분석 + 서울 정비·개발 13개 독립 사업모듈 + 소규모주택정비 보류 shell",
)


class GeometryInput(BaseModel):
    geometry: Dict[str, Any]


class StreetBlockInput(BaseModel):
    geometry: Dict[str, Any]
    barrier_features: List[Dict[str, Any]] = Field(default_factory=list, max_length=3000)
    road_features: List[Dict[str, Any]] = Field(default_factory=list, max_length=5000)
    max_radius_m: float = Field(500.0, ge=120.0, le=1000.0)


class BuildingHubBatchInput(BaseModel):
    pnus: List[str] = Field(..., min_length=1, max_length=50)


class LandLedgerOneInput(BaseModel):
    pnu: str


class PnuListInput(BaseModel):
    pnus: List[str] = Field(..., min_length=1, max_length=200)


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
    road_ready = vworld_ready()
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
    <div class="cards"><div class="card"><span>전체 익명 방문자</span><b>{len(visitors):,}</b></div><div class="card"><span>분석 실행 방문자</span><b>{len(analysis_visitors):,}</b></div><div class="card"><span>총 분석 실행</span><b>{len(analyses):,}</b></div><div class="card"><span>오늘 분석</span><b>{today_analyses:,}</b></div><div class="card"><span>미처리 오류·의견</span><b>{open_feedback:,}</b></div><div class="card"><span>도로중심선 API</span><b>{'준비됨' if road_ready else 'VWorld 키 확인'}</b></div></div>
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


@app.get("/api/reference/station-entrances")
def reference_station_entrances():
    """역명 -> 공식 연결된 출입구 좌표 목록(안심주택 350m 예외경로 전용).

    소속이 애매해서 배포 전 전처리 단계에서 제외된 출입구는 포함하지 않는다.
    프론트엔드는 이 결과를 그대로 신뢰하고, 이름 정규화 등으로 재매칭을 시도하지 않는다.
    """
    stations = _station_entrance_reference_data()
    matched = sum(len(rows) for rows in stations.values() if isinstance(rows, list))
    return {
        "metadata": {
            "source": "도로명주소 전자지도 TL_SPSB_ENTRC 2026.08.01",
            "linkage_basis": "역사경계 내부 또는 공간적으로 명확한 출입구만 사전 연결",
            "linkage_complete": False,
            "official_relation_key": False,
            "source_entrance_count": 1743,
            "matched_entrance_count": matched,
            "excluded_ambiguous_count": max(0, 1743 - matched),
            "station_count": len(stations),
        },
        "stations": stations,
    }


# R22 station-line runtime hotfix.  This block is intentionally backend-only:
# the existing multi-station frontend already consumes /api/reference/station-lines.
STATION_RUNTIME_BUILD_MARKER = "R22_STATION_HOTFIX_20260901_0915"
APP_BUILD_MARKER = "R31_SAFE_MEDICAL_PERFORMANCE_MERGED_20260903"
_STATION_LINE_CACHE_LOCK = threading.Lock()
_STATION_LINE_CACHE: Dict[str, Any] = {
    "expires_at": 0.0,
    "data": None,
    "credential_env": None,
}
_STATION_DIRECT_PROBE_CACHE_LOCK = threading.Lock()
_STATION_DIRECT_PROBE_CACHE: Dict[str, Dict[str, Any]] = {}


def _normalize_station_public_name(value: Any) -> str:
    nm = re.sub(r"\s+", "", str(value or "").strip())
    nm = re.sub(r"\([^)]*\)|（[^）]*）|\[[^]]*\]", "", nm)
    if nm.endswith("역"):
        nm = nm[:-1]
    return nm


def _normalize_subway_line_name(value: Any) -> str:
    ln = re.sub(r"\s+", "", str(value or "").strip())
    if not ln:
        return ""
    ln = re.sub(r"^0+([1-9])호선$", r"\1호선", ln)
    # The daily ridership table sometimes uses operational labels; retain them as
    # distinct lines rather than collapsing different rail services.
    return ln


def _station_line_add(grouped: Dict[str, Dict[str, Any]], name: Any, line: Any, source: str) -> None:
    nm = _normalize_station_public_name(name)
    ln = _normalize_subway_line_name(line)
    if not nm or not ln:
        return
    key_name = _name_key(nm)
    rec = grouped.setdefault(key_name, {"name": nm + "역", "lines": [], "sources": []})
    if ln not in rec["lines"]:
        rec["lines"].append(ln)
    if source not in rec["sources"]:
        rec["sources"].append(source)


def _fetch_search_stn_table(grouped: Dict[str, Dict[str, Any]], errors: List[str], source_counts: Dict[str, int]) -> None:
    """Stable station-line table first: enough to confirm e.g. Wangsimni 2+5 transfer."""
    try:
        rows = _seoul_open_data_rows("SearchSTNBySubwayLineInfo", 5000)
        source_counts["SearchSTNBySubwayLineInfo"] = len(rows)
        for row in rows:
            _station_line_add(grouped, row.get("STATION_NM"), row.get("LINE_NUM"), "SearchSTNBySubwayLineInfo")
    except Exception as exc:
        errors.append(f"SearchSTNBySubwayLineInfo: {exc}")


def _fetch_card_subway_recent(key: str, grouped: Dict[str, Dict[str, Any]], errors: List[str], source_counts: Dict[str, int]) -> Optional[str]:
    """Broader operator coverage; only used as a supplement to the stable table."""
    now = datetime.now(ZoneInfo("Asia/Seoul")).date()
    for days_back in range(3, 8):
        d = date.fromordinal(now.toordinal() - days_back).strftime("%Y%m%d")
        try:
            url = f"{SEOUL_OPEN_DATA_BASE}/{quote(key, safe='')}/json/CardSubwayStatsNew/1/1000/{d}"
            resp = requests.get(url, timeout=8)
            resp.raise_for_status()
            payload = resp.json()
            top = payload.get("RESULT") if isinstance(payload, dict) else None
            if isinstance(top, dict):
                code = str(top.get("CODE") or "")
                if code and code not in {"INFO-000", "INFO-200"}:
                    raise RuntimeError(f"{code} {top.get('MESSAGE','')}")
            body = payload.get("CardSubwayStatsNew") if isinstance(payload, dict) else None
            rows = body.get("row") if isinstance(body, dict) else None
            if not isinstance(rows, list) or not rows:
                errors.append(f"CardSubwayStatsNew {d}: row 0건")
                continue
            source_counts["CardSubwayStatsNew"] = len(rows)
            for row in rows:
                if isinstance(row, dict):
                    _station_line_add(grouped, row.get("SUB_STA_NM"), row.get("LINE_NUM"), "CardSubwayStatsNew")
            return d
        except Exception as exc:
            errors.append(f"CardSubwayStatsNew {d}: {exc}")
    return None


def _seoul_station_line_reference(force: bool = False) -> Dict[str, Any]:
    """Official station-line reference; unavailable/empty results are never long-cached."""
    key, key_env = _seoul_open_data_key_info()
    now_ts = datetime.now().timestamp()
    with _STATION_LINE_CACHE_LOCK:
        cached = _STATION_LINE_CACHE.get("data")
        same_env = _STATION_LINE_CACHE.get("credential_env") == (key_env or None)
        if key and same_env and not force and cached is not None and now_ts < float(_STATION_LINE_CACHE.get("expires_at") or 0):
            out = dict(cached)
            out["metadata"] = dict(out.get("metadata") or {}, cache_hit=True)
            return out

    meta = {
        "geometry_source": "MOIS TL_SPSB_STATN",
        "line_primary_source": "서울교통공사 SearchSTNBySubwayLineInfo",
        "line_secondary_source": "서울 열린데이터광장 CardSubwayStatsNew",
        "credential_env": key_env or None,
        "key_configured": bool(key),
        "cache_hit": False,
        "build_marker": STATION_RUNTIME_BUILD_MARKER,
    }
    if not key:
        return {
            "status": "unavailable",
            "stations": [],
            "metadata": meta,
            "message": "서울 열린데이터광장 인증키 미설정",
        }

    grouped: Dict[str, Dict[str, Any]] = {}
    errors: List[str] = []
    source_counts = {"SearchSTNBySubwayLineInfo": 0, "CardSubwayStatsNew": 0}
    _fetch_search_stn_table(grouped, errors, source_counts)
    used_date = _fetch_card_subway_recent(key, grouped, errors, source_counts)

    stations = sorted(grouped.values(), key=lambda x: x["name"])
    broad_source_ok = source_counts.get("CardSubwayStatsNew", 0) > 0
    for rec in stations:
        rec["lines"] = sorted(set(rec["lines"]))
        rec["line_count"] = len(rec["lines"])
        if rec["line_count"] >= 2:
            rec["transfer"] = True
            rec["transfer_status"] = "CONFIRMED_TRANSFER"
        elif broad_source_ok and "CardSubwayStatsNew" in rec.get("sources", []):
            rec["transfer"] = False
            rec["transfer_status"] = "CONFIRMED_SINGLE_LINE"
        else:
            # One line from a partial operator table does not prove non-transfer.
            rec["transfer"] = None
            rec["transfer_status"] = "UNRESOLVED_SINGLE_SOURCE"

    status = "ok" if stations and not errors else ("partial" if stations else "error")
    wang = next((x for x in stations if _name_key(_normalize_station_public_name(x.get("name"))) == _name_key("왕십리")), None)
    meta.update({
        "ridership_date": used_date,
        "station_count": len(stations),
        "source_counts": source_counts,
        "errors": errors[:10],
        "wangsimni_probe": {
            "found": bool(wang),
            "lines": list((wang or {}).get("lines") or []),
            "line_count": int((wang or {}).get("line_count") or 0),
            "transfer": (wang or {}).get("transfer"),
        },
    })
    result = {"status": status, "stations": stations, "metadata": meta}

    # Long cache only when actual station rows exist; failure/zero rows self-heal quickly.
    ttl = 21600 if stations else 60
    with _STATION_LINE_CACHE_LOCK:
        _STATION_LINE_CACHE.update({
            "expires_at": now_ts + ttl,
            "data": result,
            "credential_env": key_env or None,
        })
    return result


def _direct_station_line_probe(station_name: str, force: bool = False) -> Dict[str, Any]:
    """Diagnostic single-station probe used to verify runtime merge before scheme testing."""
    nm = _normalize_station_public_name(station_name)
    if not nm:
        return {"status": "invalid", "name": station_name, "lines": [], "line_count": 0, "transfer": None}
    cache_key = _name_key(nm)
    now_ts = datetime.now().timestamp()
    with _STATION_DIRECT_PROBE_CACHE_LOCK:
        cached = _STATION_DIRECT_PROBE_CACHE.get(cache_key)
        if not force and cached and now_ts < float(cached.get("expires_at") or 0):
            return dict(cached.get("data") or {}, cache_hit=True)

    key, key_env = _seoul_open_data_key_info()
    if not key:
        return {
            "status": "unavailable", "name": nm + "역", "lines": [], "line_count": 0,
            "transfer": None, "key_configured": False, "credential_env": None,
            "build_marker": STATION_RUNTIME_BUILD_MARKER,
        }

    errors: List[str] = []
    lines: List[str] = []
    try:
        ref = _seoul_station_line_reference(force=force)
        row = next((x for x in ref.get("stations", []) if _name_key(_normalize_station_public_name(x.get("name"))) == cache_key), None)
        if row:
            lines.extend(row.get("lines") or [])
    except Exception as exc:
        errors.append(f"global reference: {exc}")

    if len(set(lines)) < 2:
        try:
            station_q = quote(nm, safe="")
            url = f"{SEOUL_OPEN_DATA_BASE}/{quote(key, safe='')}/json/SearchInfoBySubwayNameService/1/50/{station_q}/"
            resp = requests.get(url, timeout=8)
            resp.raise_for_status()
            payload = resp.json()
            top = payload.get("RESULT") if isinstance(payload, dict) else None
            if isinstance(top, dict):
                code = str(top.get("CODE") or "")
                if code and code not in {"INFO-000", "INFO-200"}:
                    raise RuntimeError(f"{code} {top.get('MESSAGE','')}")
            body = payload.get("SearchInfoBySubwayNameService") if isinstance(payload, dict) else None
            if isinstance(body, dict):
                for row in body.get("row") or []:
                    if not isinstance(row, dict):
                        continue
                    if _name_key(_normalize_station_public_name(row.get("STATION_NM"))) != cache_key:
                        continue
                    ln = _normalize_subway_line_name(row.get("LINE_NUM"))
                    if ln:
                        lines.append(ln)
        except Exception as exc:
            errors.append(f"SearchInfoBySubwayNameService: {exc}")

    lines = sorted(set(lines))
    transfer = True if len(lines) >= 2 else None
    result = {
        "status": "ok" if lines else "error",
        "name": nm + "역",
        "lines": lines,
        "line_count": len(lines),
        "transfer": transfer,
        "transfer_status": "CONFIRMED_TRANSFER" if transfer else "UNRESOLVED",
        "key_configured": True,
        "credential_env": key_env,
        "errors": errors[:5],
        "build_marker": STATION_RUNTIME_BUILD_MARKER,
    }
    with _STATION_DIRECT_PROBE_CACHE_LOCK:
        _STATION_DIRECT_PROBE_CACHE[cache_key] = {
            "expires_at": now_ts + (21600 if lines else 60),
            "data": result,
        }
    return result


@app.get("/api/reference/station-line/{station_name}")
def reference_station_line(station_name: str, force: bool = False):
    return _direct_station_line_probe(station_name, force=force)


@app.get("/api/reference/station-lines")
def reference_station_lines(force: bool = False):
    """Official station-line reference. ?force=1 bypasses the success cache."""
    return _seoul_station_line_reference(force=force)


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
        "engine": "site_fact_store_v2.5.0_r11",
        "map": "leaflet-draw",
        "vworld_configured": vworld_ready(),
        "build_marker": APP_BUILD_MARKER,
        "station_runtime_build_marker": STATION_RUNTIME_BUILD_MARKER,
        "seoul_open_data_configured": bool(_seoul_open_data_key()),
        "seoul_open_data_env": _seoul_open_data_key_info()[1] or None,
        "seoul_env_names_detected": sorted([k for k in os.environ if "seoul" in k.lower() or "data.seoul" in k.lower()]),
        "analytics_storage": _analytics_storage_mode(),
        "admin_configured": bool(os.getenv("ADMIN_PASSWORD", "")),
        "vworld_domain": _vworld_domain() if vworld_ready() else None,
        "parcel_auto": "browser_direct_ready" if vworld_ready() else "needs_VWORLD_API_KEY",
        "building_spatial_auto": "LT_C_SPBD_browser_direct_ready" if vworld_ready() else "needs_VWORLD_API_KEY",
        "building_hub": "ready" if building_hub_ready() else "needs_BUILDING_HUB_API_KEY",
        "land_ledger": "ladfrlList + getLandCharacteristics + geometry provisional",
        "road_access": "VWorld TL_SPRD_MANAGE + ROAD_BT for cadastral/frontage calculations",
        "road_bundled_configured": bool(_road_zip_path()),
        "analysis_object_model": "parcel/building common ledger retained for station-area/zoning/mixed-use expansion",
        "redevelopment_strategy": "scheme-specific legal aging facts + area/aging/additional-entry AND-OR gates",
        "scheme_sheets": ["housing_redevelopment","reconstruction","residential_environment","smallscale_housing_5_routes","general_housing","safe_housing","shared_housing","longterm_lease","public_housing_complex","urban_redevelopment","station_activation","growth_potential","urban_complex_innovation","station_complex_district","prior_negotiation"],
        "scheme_age_stats": "BuildingHUB raw facts -> urban-planning / urban-renewal / policy-specific derived aging facts; unknowns remain bounded REVIEW",
        "density_public_contribution": "16 independent scheme modules + three future shells; zoning/FAR/public-contribution review remains scheme-specific",
        "scheme_ui": "six-family UI; 16 independent modules including smallscale 5-route family and prior negotiation + three future shells",
        "station_boundary_gis": "embedded MOIS 2026-08 TL_SPSB_STATN + site-centroid 1km multi-station candidates + physical same-name clustering + per-station 250/350/500m facts + spatially filtered VWorld line fallback",
        "station_fact_engine": "R22_MULTI_STATION_V2; nearest station is display/legacy only, scheme rules select their own qualifying station",
        "first_screen": "boundary-first manual review trigger + six scheme families + 16 independent modules + three future shells",
        "location_map": "boundary-only main map; parcel/building diagrams rendered in compact side mini maps",
        "reconstruction_gate": "requires apartment-complex evidence or explicit reconstruction target confirmation",
        "site_status_card": "neutral raw land/building facts + visible regime-specific aging facts + scheme-specific supplemental facts",
        "planning_gis": "VWorld zoning/district/facility/district-unit-plan polygon intersection engine",
        "renewal_gis": "server-side UQ181/UQ120 intersection; legal-priority; promotion separate; full matched boundaries returned for status map",
        "development_gis": "VWorld district-unit plan + bundled Seoul UQ181 urban-development/public-housing/other legal project intersections",
        "safe_housing_location_paths": "station / arterial-road-side / medical-facility-center evaluated separately; OR combined",
        "safe_medical_reference": "TbHospitalInfo general hospitals + official Seoul municipal hospitals/25 district health centers; one representative cadastral parcel and its 350m buffer",
        "safe_medical_key_env": _seoul_open_data_key_info()[1] or None,
        "road_width_gis": "VWorld TL_SPRD_MANAGE ROAD_BT is the sole road-width Fact source",
        "street_block_gis": "SGIS 2025 basic-unit seed + VWorld TL_SPRD_MANAGE ROAD_BT 4m+ merge verification; ESTIMATE only and never authoritative PASS/FAIL until official street-block data is connected",
        "street_block_future_interface": "MOIS basic-unit / official street-block or verified planning-road block -> authoritative_street_block=true",
        "arterial_road_future_interface": "official address-based road function/classification -> road_function / statutory_classification fields; width-only candidates remain REVIEW",
        "activation_arterial_gis": "Seoul published linear-commercial road list + VWorld LT_C_UQ111 zoning + TL_SPRD_MANAGE road centerlines; dedicated station-activation arterial map",
        "street_block_basic_unit_configured": bool(_basic_unit_zip_path()),
        "street_block_basic_unit_file": os.path.basename(_basic_unit_zip_path()) if _basic_unit_zip_path() else None,
        "responsive_ui": "desktop/tablet/mobile responsive layout with mobile workflow and selected-scheme cards",
        "smallscale_group": "five user review routes: autonomous / block / small-scale reconstruction / small-scale redevelopment / Moa Town+Moa Housing policy route; Moa is not a fifth statutory project",
        "workspace_ui": "three-column location/spatial evidence/integrated status layout; all decision facts surface in spatial-status boxes",
        "boundary_input_ui": "draw polygon / Seoul parcel address / SHP ZIP; normal, satellite, or satellite+planning map mode",
        "mini_map_hierarchy": "strong in-site features with thin surrounding spatial context",
        "house_density": "shared factual calculation; redevelopment uses >=60/ha as one additional entry criterion and residential-environment uses >=80/ha as a mandatory non-management criterion",
        "parcel_boundary_editor": "pnu_list_click_include_exclude_nearby_union",
        "scheme_architecture": "site facts -> scheme-specific facts -> independent scheme evaluation -> review sheet -> priority comparison",
        "scheme_module_api": "2026-09-02-r22-station-area-frontage-no-hierarchy",
        "independent_scheme_modules": "16 independent modules including smallscale 5-route family and prior_negotiation; urban_innovation_zone / facility_complex_zone / mixed_use_zone remain future shells",
        "scheme_specific_spatial_checks": "scheme module may request additional official spatial facts; missing facts remain REVIEW, never inferred PASS",
        "hill_official_gis": "disabled_public_shp_not_found",
        "hill_official_file": None,
        "spatial_evidence_maps": "common cadastral base + colored zoning + scheme-specific road/frontage facts + safe-housing medical reference; map facts and scheme facts share one Fact Store",
        "purpose_filter": "safe-housing rule module runs only when purpose=housing_rental; other schemes keep existing purpose/candidate logic",
        "provenance_ui": True,
    }


def _prototype_low_memory_mode() -> bool:
    # R21 prototype: correctness over throughput. Render-class small instances should not
    # keep multiple Seoul-wide SHP/STRtree caches resident at the same time.
    return str(os.getenv("SPATIAL_LOW_MEMORY", "1")).strip().lower() not in {"0", "false", "no", "off"}


def _release_heavy_analysis_cache(kind: str) -> None:
    if not _prototype_low_memory_mode():
        return
    try:
        if kind == "renewal":
            _renewal_spatial_index.cache_clear()
            _renewal_reference_data.cache_clear()
        elif kind == "development":
            _development_spatial_index.cache_clear()
            _development_reference_data.cache_clear()
    except Exception:
        logging.exception("failed to release %s spatial cache", kind)


@app.post("/api/spatial/measure")
def spatial_measure(inp: GeometryInput):
    try:
        return measure_geojson(inp.geometry)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/reference/seoul-space-catalog")
def seoul_space_catalog(keyword: str = "구릉지"):
    """서울시 공간정보 목록에서 구릉지/특성주거지 원 레이어 메타데이터를 탐색합니다."""
    return _seoul_space_catalog_keyword(keyword)


@app.get("/api/reference/hill-status")
def hill_status():
    fc=_hill_reference_data()
    return fc.get('metadata') or {}


@app.post("/api/spatial/hill-intersections")
def hill_intersections(inp: GeometryInput):
    """서울시 공식 구릉지 원도형과 대상구역의 중첩/최근접 거리를 계산합니다."""
    try:
        return analyze_hill_intersections(inp.geometry)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logging.exception("hill intersection failed")
        raise HTTPException(status_code=500, detail=f"구릉지 중첩분석 오류: {exc}") from exc
    finally:
        if _prototype_low_memory_mode():
            try:
                _hill_spatial_index.cache_clear(); _hill_reference_data.cache_clear()
            except Exception:
                pass


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
    finally:
        _release_heavy_analysis_cache("renewal")


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
    finally:
        _release_heavy_analysis_cache("development")


@app.post("/api/reference/safe-medical-nearby")
def safe_medical_nearby(inp: GeometryInput):
    """안심주택 인정 의료시설의 대표지번 1필지 경계와 350m 범위를 계산합니다.

    종합병원·서울시 관리 시립병원·25개 자치구 보건소를 대상으로 하며,
    대표필지는 실제 의료시설 전체 대지와 다를 수 있으므로 초기검토용입니다.
    """
    try:
        return _safe_medical_reference(inp.geometry)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logging.exception("safe medical reference failed")
        return {"status": "error", "items": [], "errors": [str(exc)], "message": "의료시설 공식 위치자료 조회 실패 · 공식자료 확인 필요"}


@app.get("/api/spatial/biotope-data-status")
def biotope_data_status():
    """내장 비오톱1등급 SHP 로드상태 진단."""
    path = _biotope_zip_path()
    if not path:
        return {"available": False, "fact_status": "MISSING", "message": "내장 비오톱1등급 자료 없음 · biotope_seoul.zip 필요"}
    layers = _biotope_spatial_layers()
    if layers.get("available"):
        return {"available": True, "fact_status": "BIOTOPE_GRADE1_READY", "message": f"비오톱1등급 폴리곤 {layers.get('count', 0)}건 사용 가능", "file": layers.get("file"), "source": layers.get("source")}
    return {"available": False, "fact_status": "LOAD_FAILED", "message": str(layers.get("reason") or "비오톱 ZIP 로드 실패")}


@app.post("/api/spatial/biotope-intersections")
def biotope_intersections(inp: GeometryInput):
    """내장 비오톱1등급 원본과 대상지를 실제 공간교차합니다."""
    try:
        return analyze_biotope_intersections(inp.geometry)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logging.exception("biotope intersection failed")
        raise HTTPException(status_code=500, detail=f"비오톱1등급 중첩분석 오류: {exc}") from exc


@app.get("/api/spatial/forest-classification-data-status")
def forest_classification_data_status():
    """내장 UF801 서울 산지구분도 로드상태와 분류별 건수를 진단한다."""
    path = _forest_classification_zip_path()
    if not path:
        return {"available": False, "fact_status": "MISSING", "message": "내장 서울 산지구분도 없음"}
    layers = _forest_classification_spatial_layers()
    if layers.get("available"):
        counts = layers.get("counts") or {}
        return {
            "available": True,
            "fact_status": "FOREST_CLASSIFICATION_READY",
            "message": f"공익용산지 {counts.get('public_interest_forest', 0)}건 · 임업용산지 {counts.get('forestry_forest', 0)}건 사용 가능",
            "file": layers.get("file"),
            "source": layers.get("source"),
            "counts": counts,
        }
    return {"available": False, "fact_status": "LOAD_FAILED", "message": str(layers.get("reason") or "산지구분도 ZIP 로드 실패")}


@app.post("/api/spatial/forest-classification-intersections")
def forest_classification_intersections(inp: GeometryInput):
    """내장 UF801 공익용·임업용산지와 대상지를 독립적으로 실제 공간교차한다."""
    try:
        return analyze_forest_classification_intersections(inp.geometry)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logging.exception("forest classification intersection failed")
        raise HTTPException(status_code=500, detail=f"산지구분도 중첩분석 오류: {exc}") from exc


@app.post("/api/spatial/street-block")
def street_block(inp: StreetBlockInput):
    """SGIS 기초단위구 seed를 TL_SPRD_MANAGE ROAD_BT 4m+ 도로중심선으로 병합 검증합니다.

    도로 Fact는 TL_SPRD_MANAGE ROAD_BT만 사용하며,
    기초단위구/ROAD_BT 자료가 없으면 잘못된 도형으로 대체하지 않습니다.
    """
    try:
        return analyze_street_block(inp.geometry, inp.barrier_features, inp.road_features, inp.max_radius_m)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logging.exception("street block analysis failed")
        raise HTTPException(status_code=500, detail=f"가로구역 자동추출 오류: {exc}") from exc


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


@app.post("/api/spatial/land-use-restrictions")
def land_use_restrictions(inp: PnuListInput):
    """Parcel-level conservation restrictions from VWorld NED land-use plan.

    This endpoint intentionally does NOT manufacture regulation geometry.
    It reports which selected cadastral parcels have a matching official
    land-use-plan row.  Exact overlap geometry/area requires the source SHP.
    """
    pnus: List[str] = []
    seen = set()
    for raw in inp.pnus:
        pnu = str(raw or "").strip()
        if len(pnu) != 19 or not pnu.isdigit() or pnu in seen:
            continue
        seen.add(pnu)
        pnus.append(pnu)
    if not pnus:
        raise HTTPException(status_code=422, detail="유효한 19자리 PNU가 없습니다.")
    if not _vworld_key():
        raise HTTPException(status_code=503, detail="VWORLD_API_KEY가 설정되지 않았습니다.")

    categories: Dict[str, Dict[str, Any]] = {
        "biotope_grade1": {"affected_pnus": [], "rows": []},
        "public_interest_forest": {"affected_pnus": [], "rows": []},
    }
    success_pnus: List[str] = []
    errors: List[Dict[str, str]] = []

    def work(pnu: str):
        return pnu, _land_use_rows_for_pnu(pnu)

    # Low concurrency on purpose: the prototype must not hammer VWorld and is
    # designed for only a few simultaneous users.
    with ThreadPoolExecutor(max_workers=min(4, len(pnus))) as pool:
        futures = {pool.submit(work, pnu): pnu for pnu in pnus}
        for fut in as_completed(futures):
            pnu = futures[fut]
            try:
                _, rows = fut.result()
                success_pnus.append(pnu)
                for row in rows:
                    cat = _land_use_category(row.get("prposAreaDstrcCodeNm"))
                    if not cat:
                        continue
                    clean = {
                        "pnu": pnu,
                        "relation_code": str(row.get("cnflcAt") or ""),
                        "relation_name": str(row.get("cnflcAtNm") or ""),
                        "code": str(row.get("prposAreaDstrcCode") or ""),
                        "name": str(row.get("prposAreaDstrcCodeNm") or ""),
                        "manage_no": str(row.get("manageNo") or ""),
                        "last_update": str(row.get("lastUpdtDt") or ""),
                    }
                    categories[cat]["rows"].append(clean)
                    if pnu not in categories[cat]["affected_pnus"]:
                        categories[cat]["affected_pnus"].append(pnu)
            except Exception as exc:
                errors.append({"pnu": pnu, "error": str(exc)[:300]})

    success_set = set(success_pnus)
    complete = len(success_set) == len(pnus)
    for cat in categories.values():
        cat["affected_pnus"].sort()
        cat["present"] = bool(cat["affected_pnus"])
        # Positive evidence is conclusive even when another PNU failed.  A
        # negative result is conclusive only when every selected PNU was read.
        cat["known"] = bool(cat["present"] or complete)
        cat["checked_parcels"] = len(success_set)

    return {
        "status": "available" if complete else ("partial" if success_set else "error"),
        "queried_parcels": len(pnus),
        "success_parcels": len(success_set),
        "error_parcels": len(errors),
        "biotope_grade1": categories["biotope_grade1"],
        "public_interest_forest": categories["public_interest_forest"],
        "errors": errors,
        "source": {
            "provider": "VWorld NED",
            "dataset": "토지이용계획정보",
            "operation": "getLandUseAttr",
            "geometry_basis": "parcel_attribute_only",
            "note": "비오톱1등급·공익용산지는 해당/저촉 필지를 표시하며 규제 원도형 또는 정확 중첩면적을 의미하지 않습니다.",
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


@app.post("/api/redevelopment/house-density")
def house_density(detail: Dict[str, Any]):
    return calculate_house_density(detail)
