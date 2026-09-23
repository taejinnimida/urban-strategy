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
import hashlib
import threading
import time
import uuid
import sys
import struct
import sqlite3
import shutil
from array import array
from collections import deque
import xml.etree.ElementTree as ET
from datetime import date, datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote, unquote, urlparse, urljoin, parse_qsl, urlunparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path

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
from shapely.prepared import prep
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
ROOT_STATIC_HTML = os.path.join(BASE_DIR, "app.html")
STRUCTURED_STATIC_HTML = os.path.join(BASE_DIR, "static", "app.html")

# 배포 기준본은 저장소 루트의 app.html이다.
# 과거 structured 배포의 static/app.html이 남아 있더라도 최신 루트 app.html을
# 가리지 않도록 루트 파일을 우선하고, 루트 파일이 없을 때만 fallback한다.
DATA_DIR = STRUCTURED_DATA_DIR if os.path.isdir(STRUCTURED_DATA_DIR) else BASE_DIR
STATIC_HTML_PATH = (
    ROOT_STATIC_HTML
    if os.path.isfile(ROOT_STATIC_HTML)
    else STRUCTURED_STATIC_HTML
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


_INDEX_HTML_CACHE = {"mtime_ns": None, "text": None}

def _index_html() -> str:
    """app.html을 파일 변경시 자동 재로딩한다.

    Render 재배포뿐 아니라 실행 중 파일 교체 시에도 이전 HTML이 메모리에
    고정되지 않도록 mtime을 확인한다.
    """
    stat = os.stat(STATIC_HTML_PATH)
    if _INDEX_HTML_CACHE["text"] is None or _INDEX_HTML_CACHE["mtime_ns"] != stat.st_mtime_ns:
        with open(STATIC_HTML_PATH, encoding="utf-8") as fp:
            _INDEX_HTML_CACHE["text"] = fp.read()
        _INDEX_HTML_CACHE["mtime_ns"] = stat.st_mtime_ns
    return _INDEX_HTML_CACHE["text"]

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
VWORLD_LAYER_INDUSTRIAL_PARK = "LT_C_DAMDAN"
SMALL_PARCEL_THRESHOLD_M2 = 90.0

logger = logging.getLogger("urban_strategy.vworld")
logging.basicConfig(level=logging.INFO)


def _vworld_key() -> str:
    return (os.getenv("VWORLD_API_KEY") or "").strip()


def _vworld_client_key() -> str:
    """Browser-side VWorld key. Optional dedicated key, otherwise reuse server key.

    The browser key is necessarily visible to the browser. Operators may set
    VWORLD_CLIENT_KEY to a domain-restricted VWorld key; existing deployments
    remain compatible because VWORLD_API_KEY is used when it is absent.
    """
    return (os.getenv("VWORLD_CLIENT_KEY") or _vworld_key() or "").strip()


def _require_vworld_key() -> str:
    """Fail fast at public VWorld-backed API entry points when the server key is missing."""
    key = _vworld_key()
    if not key:
        raise HTTPException(
            status_code=502,
            detail=(
                "VWorld API 키(VWORLD_API_KEY) 환경변수 미설정. "
                "Render Dashboard의 Environment 섹션에서 설정하세요."
            ),
        )
    return key


def _vworld_slot_count() -> int:
    """Shared VWorld concurrency. Default 2; operator may raise to at most 3."""
    try:
        value = int((os.getenv("VWORLD_SLOTS") or "2").strip())
    except (TypeError, ValueError):
        value = 2
    return max(1, min(value, 3))


def _normalize_vworld_domain(raw: str) -> str:
    raw = str(raw or "").strip().rstrip("/")
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    if raw == "localhost" or raw.startswith("localhost:"):
        return "http://" + raw
    return "https://" + raw


def _vworld_domain() -> str:
    raw = (
        (os.getenv("VWORLD_DOMAIN") or "").strip()
        or (os.getenv("RENDER_EXTERNAL_HOSTNAME") or "").strip()
        or "localhost"
    )
    return _normalize_vworld_domain(raw)


def _vworld_referer(domain: Optional[str] = None) -> str:
    return (_normalize_vworld_domain(domain or "") or _vworld_domain()) + "/"


def _vworld_headers(domain: Optional[str] = None) -> Dict[str, str]:
    return {
        "Referer": _vworld_referer(domain),
        "User-Agent": "urban-strategy/0.4.2",
        "Accept": "application/json, text/xml;q=0.9, */*;q=0.8",
    }


_VWORLD_DIAGNOSTIC_LOCAL = threading.local()


def _set_vworld_diagnostic(**values: Any) -> None:
    _VWORLD_DIAGNOSTIC_LOCAL.last = dict(values)


def _last_vworld_diagnostic() -> Dict[str, Any]:
    return dict(getattr(_VWORLD_DIAGNOSTIC_LOCAL, "last", {}) or {})


# Bound all VWorld consumers, including ledger and roads, to one shared concurrency limit.
_VWORLD_SLOTS = threading.BoundedSemaphore(_vworld_slot_count())
_VWORLD_FAILURE_LOCK = threading.Lock()
_VWORLD_FAILURES: Dict[str, Dict[str, Any]] = {}
# R22: one dual-route transport failure opens a short server-wide VWorld circuit.
# This prevents a 40~50 parcel site from repeating the same dead upstream call.
_VWORLD_GLOBAL_CIRCUIT: Dict[str, Any] = {
    "until": 0.0, "reason": "", "endpoint": "", "opened_at": 0.0,
}
VWORLD_GLOBAL_CIRCUIT_SEC = max(5, min(int(os.getenv("VWORLD_GLOBAL_CIRCUIT_SEC", "30")), 120))


class VWorldTransportError(RuntimeError):
    pass


def _vworld_circuit_snapshot() -> Dict[str, Any]:
    now = time.monotonic()
    with _VWORLD_FAILURE_LOCK:
        state = dict(_VWORLD_GLOBAL_CIRCUIT)
    remaining = max(0.0, float(state.get("until") or 0.0) - now)
    return {
        "open": remaining > 0,
        "retry_after_seconds": math.ceil(remaining) if remaining > 0 else 0,
        "reason": str(state.get("reason") or ""),
        "endpoint": str(state.get("endpoint") or ""),
    }


def _open_vworld_circuit(endpoint: str, reason: str) -> None:
    now = time.monotonic()
    with _VWORLD_FAILURE_LOCK:
        _VWORLD_GLOBAL_CIRCUIT.update({
            "until": now + float(VWORLD_GLOBAL_CIRCUIT_SEC),
            "reason": str(reason or "VWORLD_UPSTREAM_UNAVAILABLE")[:240],
            "endpoint": str(endpoint or ""),
            "opened_at": now,
        })


def _clear_vworld_circuit() -> None:
    with _VWORLD_FAILURE_LOCK:
        _VWORLD_GLOBAL_CIRCUIT.update({"until": 0.0, "reason": "", "endpoint": "", "opened_at": 0.0})


def _vworld_timeout_tuple(value: Any, *, connect_default: float) -> tuple[float, float]:
    """Normalize timeout while preserving legacy numeric stage budgets.

    A 2-tuple is treated as requests' (connect, read) timeout. A numeric value is
    treated as the total budget for that one route, matching the pre-R20 callers
    that split small land-ledger budgets between direct and proxy stages.
    """
    if isinstance(value, (tuple, list)) and len(value) == 2:
        connect = max(0.1, float(value[0]))
        read = max(0.1, float(value[1]))
        return connect, read
    total = max(0.2, float(value))
    connect = max(0.1, min(float(connect_default), total / 3.0))
    read = max(0.1, total - connect)
    return connect, read


def _vworld_get(
    url: str,
    params: Dict[str, Any],
    timeout: Any = (4.0, 21.0),
    proxy_timeout: Optional[Any] = None,
    referer_domain: Optional[str] = None,
    force_retry: bool = False,
):
    """One direct attempt and one fallback with independent route timeouts.

    Direct/proxy no longer share the former 24-second total budget. This lets a
    slow direct attempt fall back to the proxy with its full configured timeout.
    Failed responses are never facts; cooldown remains unless force_retry=True.
    """
    started = time.monotonic()
    endpoint = urlparse(url).netloc + urlparse(url).path
    domain = _normalize_vworld_domain(str(params.get("domain") or "")) or _normalize_vworld_domain(referer_domain or "") or _vworld_domain()
    direct_timeout = _vworld_timeout_tuple(timeout, connect_default=4.0)
    proxy_timeout_value = proxy_timeout if proxy_timeout is not None else timeout
    proxy_timeout_tuple = _vworld_timeout_tuple(proxy_timeout_value, connect_default=5.0)
    diagnostic = {
        "route": "", "endpoint": endpoint, "domain_sent": domain,
        "direct_status": None, "proxy_status": None,
        "direct_error": "", "proxy_error": "",
        "direct_timeout": direct_timeout, "proxy_timeout": proxy_timeout_tuple,
        "force_retry": bool(force_retry),
    }

    def record():
        diagnostic["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        _set_vworld_diagnostic(**diagnostic)

    def check_cooldown():
        if force_retry:
            return
        global_state = _vworld_circuit_snapshot()
        if global_state.get("open"):
            remaining = int(global_state.get("retry_after_seconds") or 0)
            diagnostic.update(
                route="global_circuit", retry_after_seconds=remaining,
                circuit_endpoint=global_state.get("endpoint"),
                circuit_reason=global_state.get("reason"),
            )
            record()
            raise VWorldTransportError(
                f"VWORLD_CIRCUIT_OPEN · 외부연결 장애 확인 · {remaining}초 후 재확인 · 미확인 유지"
            )
        with _VWORLD_FAILURE_LOCK:
            state = _VWORLD_FAILURES.get(endpoint, {})
            remaining = float(state.get("until", 0)) - time.monotonic()
        if remaining > 0:
            diagnostic.update(route="cooldown", retry_after_seconds=math.ceil(remaining))
            record()
            raise VWorldTransportError(
                f"VWORLD_COOLDOWN · 연결 실패 누적 · {math.ceil(remaining)}초 후 재확인 · 미확인 유지"
            )

    check_cooldown()
    if not _VWORLD_SLOTS.acquire(timeout=5.0):
        diagnostic.update(route="queue", direct_error="QUEUE_BUSY")
        record()
        raise VWorldTransportError("VWORLD_QUEUE_BUSY · 외부 조회 대기 초과 · 미확인 유지")
    try:
        check_cooldown()
        for route, route_timeout in (("direct", direct_timeout), ("vworld_proxy", proxy_timeout_tuple)):
            diagnostic["route"] = route
            prefix = "direct" if route == "direct" else "proxy"
            try:
                if route == "direct":
                    resp = requests.get(
                        url, params=params, headers=_vworld_headers(referer_domain or domain),
                        timeout=route_timeout,
                    )
                else:
                    inner_url = requests.Request("GET", url, params=params).prepare().url
                    resp = requests.get(
                        VWORLD_PROXY_URL + quote(inner_url, safe=""),
                        headers=_vworld_headers(referer_domain or domain),
                        timeout=route_timeout,
                    )
                diagnostic[prefix + "_status"] = resp.status_code
                content_type = str(resp.headers.get("content-type") or "").lower()
                html_response = "text/html" in content_type or resp.content.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html"))
                if resp.status_code < 500 and not html_response:
                    with _VWORLD_FAILURE_LOCK:
                        _VWORLD_FAILURES.pop(endpoint, None)
                    _clear_vworld_circuit()
                    record()
                    return resp, route
                diagnostic[prefix + "_error"] = f"HTTP_{resp.status_code}" if resp.status_code >= 500 else "NON_DATA_HTML"
                resp.close()
            except requests.RequestException as exc:
                diagnostic[prefix + "_error"] = type(exc).__name__

        with _VWORLD_FAILURE_LOCK:
            state = _VWORLD_FAILURES.setdefault(endpoint, {"count": 0, "until": 0})
            state["count"] += 1
            if state["count"] >= 3:
                state["until"] = time.monotonic() + 30
        _open_vworld_circuit(
            endpoint,
            f"direct={diagnostic['direct_error']} · proxy={diagnostic['proxy_error']}",
        )
        diagnostic["global_circuit_opened"] = True
        diagnostic["global_circuit_seconds"] = VWORLD_GLOBAL_CIRCUIT_SEC
        record()
        logger.warning(
            "VWorld transport failed endpoint=%s direct=%s proxy=%s elapsed_ms=%s",
            endpoint, diagnostic["direct_error"], diagnostic["proxy_error"], diagnostic["elapsed_ms"],
        )
        raise VWorldTransportError(
            f"VWORLD_UPSTREAM_UNAVAILABLE · direct={diagnostic['direct_error']} · "
            f"proxy={diagnostic['proxy_error']} · 외부자료 미확보"
        )
    finally:
        _VWORLD_SLOTS.release()


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
    with ThreadPoolExecutor(max_workers=min(2, max(1, len(pnus)))) as ex:
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
BUILDING_HUB_FLOOR_URL = BUILDING_HUB_BASE_URL + "/getBrFlrOulnInfo"
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


def _query_building_hub_floor(pnu: str) -> List[Dict[str, Any]]:
    """건축HUB 층별개요. 지하층 주거사용 여부 판정에만 사용한다."""
    return _query_building_hub_rows(BUILDING_HUB_FLOOR_URL, pnu)


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


def _normalize_building_floor(item: Dict[str, Any], pnu: str) -> Dict[str, Any]:
    keep = [
        "mgmBldrgstPk", "flrGbCd", "flrGbCdNm", "flrNo", "flrNoNm",
        "mainPurpsCd", "mainPurpsCdNm", "etcPurps", "area",
        "strctCd", "strctCdNm", "crtnDay", "sigunguCd", "bjdongCd",
        "platGbCd", "bun", "ji", "dongNm", "bldNm",
    ]
    result = {k: item.get(k) for k in keep}
    result["pnu"] = pnu
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


def _vworld_features_in_bbox(layer: str, target_geom, size: int = 1000, max_pages: int = 5) -> List[Dict[str, Any]]:
    """VWorld polygon layer의 target bbox 후보를 조회한 뒤 실제 교차도형만 반환한다.

    산업단지 경계(LT_C_DAMDAN)처럼 서버에서 최신 공간경계를 확인해야 하는
    레이어에 사용한다. VWorld 2D Data API의 10㎢ BOX 제한을 초과하면 명시적으로
    REVIEW 경로로 넘기기 위해 예외를 발생시킨다.
    """
    if not _vworld_key():
        raise RuntimeError("VWorld API 키가 설정되지 않았습니다.")
    minx, miny, maxx, maxy = target_geom.bounds
    bbox_poly = box(minx, miny, maxx, maxy)
    bbox_area, _ = GEOD.geometry_area_perimeter(bbox_poly)
    if abs(float(bbox_area)) > 10_000_000:
        raise RuntimeError(f"VWorld {layer} 조회 범위가 10㎢를 넘습니다.")

    all_features: List[Dict[str, Any]] = []
    seen = set()
    page_size = min(max(int(size), 1), 1000)
    for page in range(1, max_pages + 1):
        params = {
            "key": _vworld_key(), "domain": _vworld_domain(),
            "service": "data", "version": "2.0", "request": "getfeature",
            "format": "json", "size": page_size, "page": page,
            "geometry": "true", "attribute": "true", "crs": "EPSG:4326",
            "data": layer, "geomfilter": f"BOX({minx},{miny},{maxx},{maxy})",
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
            break
        if status != "OK":
            raise RuntimeError(_response_error_message(payload))
        fc = (((payload.get("response") or {}).get("result") or {}).get("featureCollection") or {})
        feats = fc.get("features") or []
        for f in feats:
            fid = str(f.get("id") or "")
            props = dict(f.get("properties") or {})
            if not fid:
                lowered = {str(k).lower(): v for k, v in props.items()}
                fid = str(lowered.get("dan_id") or lowered.get("id") or "")
            if fid and fid in seen:
                continue
            if not f.get("geometry"):
                continue
            try:
                g = shape(f["geometry"])
                if g.is_empty or not g.intersects(target_geom):
                    continue
            except Exception:
                continue
            if fid:
                seen.add(fid)
            all_features.append({"type": "Feature", "id": f.get("id"), "geometry": f.get("geometry"), "properties": props})
        logger.info("VWorld bbox layer=%s route=%s page=%s hits=%s", layer, route, page, len(feats))
        if len(feats) < page_size:
            break
    return all_features


# UQ111 용도지역은 사업판정의 핵심 FACT이므로 브라우저 JSONP에 직접 의존하지 않는다.
# 동일 구역계의 정상 조회결과만 짧게 캐시하고, 일시장애 시 최근 성공값을 fallback으로 사용한다.
_ZONING_FACT_CACHE: Dict[str, Dict[str, Any]] = {}
_ZONING_FACT_CACHE_LOCK = threading.Lock()
_ZONING_FACT_CACHE_TTL_SEC = 600
_ZONING_FACT_CACHE_STALE_SEC = 3600


def _zoning_cache_key(geometry: Dict[str, Any]) -> str:
    raw = json.dumps(geometry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _zoning_cache_get(key: str, allow_stale: bool = False) -> Optional[Dict[str, Any]]:
    now = time.time()
    with _ZONING_FACT_CACHE_LOCK:
        row = _ZONING_FACT_CACHE.get(key)
        if not row:
            return None
        age = now - float(row.get("saved_at") or 0)
        limit = _ZONING_FACT_CACHE_STALE_SEC if allow_stale else _ZONING_FACT_CACHE_TTL_SEC
        if age > limit:
            if age > _ZONING_FACT_CACHE_STALE_SEC:
                _ZONING_FACT_CACHE.pop(key, None)
            return None
        return {"features": row.get("features") or [], "age_sec": round(age, 1)}


def _zoning_cache_put(key: str, features: List[Dict[str, Any]]) -> None:
    # 용도지역 0건은 정상 FACT로 캐시하지 않는다. 서울 대상지에서 UQ111 0건은
    # 일시 조회실패/원자료 누락과 구분되지 않으므로 다음 실행에서 다시 확인해야 한다.
    if not features:
        return
    with _ZONING_FACT_CACHE_LOCK:
        if key not in _ZONING_FACT_CACHE and len(_ZONING_FACT_CACHE) >= 128:
            oldest = min(_ZONING_FACT_CACHE.items(), key=lambda kv: float(kv[1].get("saved_at") or 0))[0]
            _ZONING_FACT_CACHE.pop(oldest, None)
        _ZONING_FACT_CACHE[key] = {"saved_at": time.time(), "features": features}


def analyze_zoning_features(geometry: Dict[str, Any]) -> Dict[str, Any]:
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

    key = _zoning_cache_key(geometry)
    cached = _zoning_cache_get(key, allow_stale=False)
    if cached and cached.get("features"):
        return {
            "status": "cached", "known": True, "features": cached["features"],
            "cache_hit": True, "cache_age_sec": cached.get("age_sec"),
            "layer": "LT_C_UQ111",
            "source": {"provider": "VWorld", "dataset": "LT_C_UQ111", "route": "server_cache"},
        }

    try:
        features = _vworld_features_in_bbox("LT_C_UQ111", site_wgs, size=300, max_pages=5)
        if features:
            _zoning_cache_put(key, features)
            return {
                "status": "available", "known": True, "features": features,
                "cache_hit": False, "cache_age_sec": 0, "layer": "LT_C_UQ111",
                "source": {"provider": "VWorld", "dataset": "LT_C_UQ111", "route": "server_direct_or_proxy"},
            }

        stale = _zoning_cache_get(key, allow_stale=True)
        if stale and stale.get("features"):
            return {
                "status": "stale_cache", "known": True, "features": stale["features"],
                "cache_hit": True, "cache_age_sec": stale.get("age_sec"),
                "warning": "VWorld UQ111 재조회 0건 · 최근 정상 FACT 재사용",
                "layer": "LT_C_UQ111",
                "source": {"provider": "VWorld", "dataset": "LT_C_UQ111", "route": "server_stale_cache"},
            }
        return {
            "status": "no_data", "known": False, "features": [], "cache_hit": False,
            "layer": "LT_C_UQ111",
            "source": {"provider": "VWorld", "dataset": "LT_C_UQ111", "route": "server_direct_or_proxy"},
            "message": "LT_C_UQ111 조회 결과가 0건입니다. 핵심 FACT를 확정하지 않습니다.",
        }
    except Exception as exc:
        stale = _zoning_cache_get(key, allow_stale=True)
        if stale and stale.get("features"):
            return {
                "status": "stale_cache", "known": True, "features": stale["features"],
                "cache_hit": True, "cache_age_sec": stale.get("age_sec"),
                "warning": f"VWorld UQ111 일시장애 · 최근 정상 FACT 재사용 · {str(exc)[:180]}",
                "layer": "LT_C_UQ111",
                "source": {"provider": "VWorld", "dataset": "LT_C_UQ111", "route": "server_stale_cache"},
            }
        raise


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
    site_area = float(site_metric.area)
    inter = site_metric.intersection(buffer_metric)
    inter_area = float(inter.area) if not inter.is_empty else 0.0
    coverage = (inter_area / site_area * 100.0) if site_area > 0 else None
    return {
        "distance_boundary_m": round(distance, 1),
        "within_350": distance <= 350.0 + 1e-6,
        "coverage_350_pct": round(coverage, 3) if coverage is not None else None,
        "overlap_350_area_m2": round(inter_area, 3),
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
VWORLD_INDVD_LAND_PRICE_URL = "https://api.vworld.kr/ned/data/getIndvdLandPriceAttr"
# data.go.kr 토지임야정보조회서비스. HTTPS/HTTP는 같은 host/path의 동일 서비스다.
# 정상 경로에서는 HTTPS 한 번만 사용하고, HTTP 별도 순차 재시도는 하지 않는다.
# 과거 활용가이드에 HTTP 예시가 있었지만 이를 독립 데이터 소스로 취급하지 않는다.
LEGACY_LAND_LEDGER_URL = "https://apis.data.go.kr/1611000/nsdi/eios/LadfrlService/ladfrlList.xml"

# 토지대장은 같은 PNU를 짧은 시간에 반복 조회해도 값이 즉시 바뀌는 자료가 아니다.
# 일시 장애를 영구 기억하지 않도록 성공/실패 TTL을 분리한다.
LAND_LEDGER_CACHE_TTL_POSITIVE_SEC = int(os.getenv("LAND_LEDGER_CACHE_TTL_POSITIVE_SEC", "1800"))
LAND_LEDGER_CACHE_TTL_NEGATIVE_SEC = int(os.getenv("LAND_LEDGER_CACHE_TTL_NEGATIVE_SEC", "300"))
LAND_LEDGER_CACHE_LOCK = threading.Lock()
LAND_LEDGER_CACHE: Dict[str, Dict[str, Any]] = {}
# 브라우저가 PNU 5개를 동시에 보내도 외부 소스 fan-out이 과도하게 커지지 않도록 제한한다.
LAND_LEDGER_SOURCE_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="land-ledger-source")

# R28: Seoul AL_D003 bundled snapshot.  The repository carries a compressed
# SQLite index (not the 114MB CSV).  It is extracted lazily to /tmp and queried
# by PNU only when live VWorld NED did not produce a usable record.
LAND_LEDGER_LOCAL_ARCHIVE_ENV = "LAND_LEDGER_LOCAL_ARCHIVE"
LAND_LEDGER_LOCAL_DB_ENV = "LAND_LEDGER_LOCAL_DB"
LAND_LEDGER_LOCAL_EXTRACT_LOCK = threading.Lock()


def _land_ledger_local_direct_db_path() -> Optional[str]:
    env = (os.getenv(LAND_LEDGER_LOCAL_DB_ENV) or "").strip()
    candidates: List[Path] = []
    if env:
        candidates.append(Path(env))
    candidates.extend(sorted(Path(STRUCTURED_DATA_DIR).glob("land_ledger_seoul_*.sqlite"), reverse=True))
    candidates.extend(sorted(Path(BASE_DIR).glob("land_ledger_seoul_*.sqlite"), reverse=True))
    for path in candidates:
        try:
            if path.is_file() and path.stat().st_size > 1024:
                return str(path)
        except Exception:
            continue
    return None


def _land_ledger_local_archive_path() -> Optional[str]:
    env = (os.getenv(LAND_LEDGER_LOCAL_ARCHIVE_ENV) or "").strip()
    candidates: List[Path] = []
    if env:
        candidates.append(Path(env))
    candidates.extend(sorted(Path(STRUCTURED_DATA_DIR).glob("land_ledger_seoul_*.sqlite.zip"), reverse=True))
    candidates.extend(sorted(Path(BASE_DIR).glob("land_ledger_seoul_*.sqlite.zip"), reverse=True))
    for path in candidates:
        try:
            if path.is_file() and path.stat().st_size > 1024:
                return str(path)
        except Exception:
            continue
    return None


def _ensure_land_ledger_local_db() -> Optional[str]:
    direct = _land_ledger_local_direct_db_path()
    if direct:
        return direct
    archive = _land_ledger_local_archive_path()
    if not archive:
        return None
    with LAND_LEDGER_LOCAL_EXTRACT_LOCK:
        try:
            with zipfile.ZipFile(archive) as zf:
                members = [m for m in zf.namelist() if m.lower().endswith(".sqlite") and not m.endswith("/")]
                if not members:
                    logger.warning("bundled land-ledger archive has no sqlite member: %s", archive)
                    return None
                member = members[0]
                target = os.path.join("/tmp", os.path.basename(member))
                if os.path.isfile(target) and os.path.getsize(target) > 1024:
                    return target
                tmp = target + ".tmp"
                with zf.open(member) as src, open(tmp, "wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
                os.replace(tmp, target)
                return target
        except Exception as exc:
            logger.exception("bundled land-ledger sqlite extraction failed: %s", exc)
            return None


@lru_cache(maxsize=4096)
def _local_land_ledger_lookup(pnu: str) -> Optional[Dict[str, Any]]:
    pnu = str(pnu or "").strip()
    if len(pnu) != 19 or not pnu.isdigit():
        return None
    db_path = _ensure_land_ledger_local_db()
    if not db_path:
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            row = conn.execute(
                "SELECT pnu,ldCodeNm,mnnmSlno,regstrSeCodeNm,lndcgrCodeNm,lndpclAr,"
                "posesnSeCodeNm,cnrsPsnCo,ladFrtlScNm,lastUpdtDt FROM land_ledger WHERE pnu=?",
                (pnu,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        return {
            "pnu": row[0], "ldCodeNm": row[1] or "", "mnnmSlno": row[2] or "",
            "regstrSeCodeNm": row[3] or "", "lndcgrCodeNm": row[4] or "",
            "lndpclAr": float(row[5]) if row[5] is not None else None,
            "posesnSeCodeNm": row[6] or "", "cnrsPsnCo": int(row[7] or 0),
            "ladFrtlScNm": row[8] or "", "lastUpdtDt": row[9] or "",
            "_route": "bundled_AL_D003_local_snapshot",
            "_source_type": "LOCAL_SNAPSHOT",
            "_source_date": row[9] or "",
        }
    except Exception as exc:
        logger.info("bundled land-ledger lookup failed pnu=%s err=%s", pnu, exc)
        return None


def _land_ledger_local_snapshot_status() -> Dict[str, Any]:
    direct = _land_ledger_local_direct_db_path()
    archive = _land_ledger_local_archive_path()
    path = direct or archive
    return {
        "configured": bool(path),
        "file": os.path.basename(path) if path else None,
        "mode": "sqlite" if direct else ("sqlite_zip_lazy_extract" if archive else None),
        "data_date": "2026-09-04" if path and "20260904" in os.path.basename(path) else None,
    }


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


def _server_land_ledger_vworld(pnu: str, timeout: int = 6) -> Optional[Dict[str, Any]]:
    if not _vworld_key():
        return None
    params = {
        "format": "xml",
        "key": _vworld_key(),
        "domain": _vworld_domain(),
        "pnu": pnu,
    }
    try:
        # Land-ledger path caps one VWorld source stage to roughly 6s total
        # (direct first, then official proxy with the remaining budget).
        direct_budget = min(float(timeout), 4.0)
        proxy_budget = max(1.0, float(timeout) - direct_budget)
        resp, route = _vworld_get(VWORLD_LAND_LEDGER_URL, params=params, timeout=direct_budget, proxy_timeout=proxy_budget)
        if resp.status_code >= 400:
            return None
        record = _parse_land_ledger_xml(resp.text)
        if record:
            record["_route"] = f"server_{route}"
        return record
    except Exception as exc:
        logger.info("server VWorld land ledger failed pnu=%s err=%s", pnu, exc)
        return None


def _server_land_ledger_legacy_data_go(pnu: str, timeout: int = 6) -> Optional[Dict[str, Any]]:
    # data.go.kr account keys are often shared across approved APIs. This is only
    # a fallback peer for the full land-ledger record; authorization failure is ignored.
    # HTTPS/HTTP aliases are the same service, so do not burn a second timeout on HTTP.
    key = _building_hub_key()
    if not key:
        return None
    params = {"serviceKey": key, "pnu": pnu, "numOfRows": 100}
    try:
        resp = requests.get(LEGACY_LAND_LEDGER_URL, params=params, timeout=timeout, allow_redirects=True)
        if resp.status_code >= 400:
            return None
        record = _parse_land_ledger_xml(resp.text)
        if record:
            record["_route"] = "legacy_data_go_https"
        return record
    except Exception as exc:
        logger.info("server data.go land ledger failed pnu=%s err=%s", pnu, exc)
        return None


def _parse_land_characteristics_xml(text: str) -> Optional[Dict[str, Any]]:
    """Parse the newest positive-area row from VWorld getLandCharacteristics.

    This is a server-side fallback for browsers that cannot call VWorld NED
    directly because of CORS.  It intentionally returns only fields that are
    actually present in the official characteristics response.
    """
    root = ET.fromstring(text)

    result_code = (root.findtext(".//resultCode") or "").strip()
    result_msg = (root.findtext(".//resultMsg") or "").strip()
    if result_code and result_code not in {"00", "0"}:
        raise RuntimeError(f"토지특성정보 API 오류 {result_code}: {result_msg or 'unknown'}")

    def val(row: ET.Element, name: str) -> str:
        node = row.find(name)
        return (node.text or "").strip() if node is not None else ""

    parsed: List[Dict[str, Any]] = []
    for row in root.findall(".//field"):
        area_raw = val(row, "lndpclAr")
        try:
            area = float(area_raw) if area_raw else None
        except ValueError:
            area = None
        if area is None or area <= 0:
            continue
        parsed.append({
            "pnu": val(row, "pnu"),
            "lndpclAr": area,
            "lndcgrCodeNm": val(row, "lndcgrCodeNm") or val(row, "lndcgrCode"),
            "stdrYear": val(row, "stdrYear"),
            "stdrMt": val(row, "stdrMt"),
            "lastUpdtDt": val(row, "lastUpdtDt"),
        })

    if not parsed:
        # Defensive fallback for response variants without <field>.
        area_raw = (root.findtext(".//lndpclAr") or "").strip()
        try:
            area = float(area_raw) if area_raw else None
        except ValueError:
            area = None
        if area is None or area <= 0:
            return None
        return {
            "pnu": (root.findtext(".//pnu") or "").strip(),
            "lndpclAr": area,
            "lndcgrCodeNm": (root.findtext(".//lndcgrCodeNm") or root.findtext(".//lndcgrCode") or "").strip(),
            "stdrYear": (root.findtext(".//stdrYear") or "").strip(),
            "stdrMt": (root.findtext(".//stdrMt") or "").strip(),
            "lastUpdtDt": (root.findtext(".//lastUpdtDt") or "").strip(),
        }

    parsed.sort(
        key=lambda r: (
            str(r.get("stdrYear") or ""),
            str(r.get("stdrMt") or ""),
            str(r.get("lastUpdtDt") or ""),
        ),
        reverse=True,
    )
    return parsed[0]


def _server_land_characteristics_vworld(pnu: str, timeout: int = 6) -> Optional[Dict[str, Any]]:
    """Server-side VWorld characteristics lookup; never exposes CORS to client."""
    if not _vworld_key():
        return None
    params = {
        "format": "xml",
        "key": _vworld_key(),
        "domain": _vworld_domain(),
        "pnu": pnu,
        "numOfRows": 50,
    }
    try:
        direct_budget = min(float(timeout), 4.0)
        proxy_budget = max(1.0, float(timeout) - direct_budget)
        resp, route = _vworld_get(VWORLD_LAND_URL, params=params, timeout=direct_budget, proxy_timeout=proxy_budget)
        if resp.status_code >= 400:
            return None
        record = _parse_land_characteristics_xml(resp.text)
        if record:
            record["_route"] = f"server_land_characteristics_{route}"
        return record
    except Exception as exc:
        logger.info("server VWorld land characteristics failed pnu=%s err=%s", pnu, exc)
        return None



def _parse_individual_land_price_xml(text: str, requested_year: int) -> Optional[Dict[str, Any]]:
    """Parse VWorld NED getIndvdLandPriceAttr for one PNU/year.

    The business-feasibility coefficient must compare the Seoul numerator and
    subject-site denominator for the same reference year, so this parser never
    substitutes a different year silently.
    """
    root = ET.fromstring(text)
    result_code = (root.findtext(".//resultCode") or "").strip()
    result_msg = (root.findtext(".//resultMsg") or "").strip()
    if result_code and result_code not in {"00", "0"}:
        raise RuntimeError(f"개별공시지가 API 오류 {result_code}: {result_msg or 'unknown'}")

    def val(row: ET.Element, name: str) -> str:
        node = row.find(name)
        return (node.text or "").strip() if node is not None else ""

    rows: List[Dict[str, Any]] = []
    for row in root.findall(".//field"):
        year_raw = val(row, "stdrYear")
        try:
            year = int(year_raw)
        except Exception:
            continue
        if year != int(requested_year):
            continue
        price_raw = val(row, "pblntfPclnd")
        try:
            price = float(price_raw)
        except Exception:
            continue
        if not math.isfinite(price) or price <= 0:
            continue
        rows.append({
            "pnu": val(row, "pnu"),
            "year": year,
            "month": val(row, "stdrMt"),
            "price_per_m2": price,
            "announcement_date": val(row, "pblntfDe"),
            "standard_land": val(row, "stdLandAt"),
            "last_update": val(row, "lastUpdtDt"),
            "legal_dong": val(row, "ldCodeNm"),
            "jibun": val(row, "mnnmSlno"),
        })

    if not rows:
        return None
    rows.sort(key=lambda r: (str(r.get("month") or ""), str(r.get("last_update") or "")), reverse=True)
    return rows[0]


def _server_individual_land_price_vworld(pnu: str, year: int, timeout: int = 8) -> Optional[Dict[str, Any]]:
    if not _vworld_key():
        return None
    params = {
        "format": "xml",
        "key": _vworld_key(),
        "domain": _vworld_domain(),
        "pnu": pnu,
        "stdrYear": int(year),
        "numOfRows": 20,
    }
    try:
        direct_budget = min(float(timeout), 5.0)
        proxy_budget = max(1.0, float(timeout) - direct_budget)
        resp, route = _vworld_get(
            VWORLD_INDVD_LAND_PRICE_URL,
            params=params,
            timeout=direct_budget,
            proxy_timeout=proxy_budget,
        )
        if resp.status_code >= 400:
            return None
        rec = _parse_individual_land_price_xml(resp.text, int(year))
        if rec:
            rec["_route"] = f"server_individual_land_price_{route}"
        return rec
    except Exception as exc:
        logger.info("server VWorld individual land price failed pnu=%s year=%s err=%s", pnu, year, exc)
        return None



# R32: bundled Seoul individual official land-price snapshot.
# Live VWorld remains the first source.  The local DB is consulted only when
# the live request did not return the exact requested reference year.
LAND_PRICE_LOCAL_ARCHIVE_ENV = "LAND_PRICE_LOCAL_ARCHIVE"
LAND_PRICE_LOCAL_DB_ENV = "LAND_PRICE_LOCAL_DB"
LAND_PRICE_LOCAL_EXTRACT_LOCK = threading.Lock()


def _land_price_path_year(path: Path) -> Optional[int]:
    m = re.search(r"(?:^|_)(20\d{2})(?:\.|_|$)", path.name)
    return int(m.group(1)) if m else None


def _land_price_local_direct_db_path(year: Optional[int] = None) -> Optional[str]:
    env = (os.getenv(LAND_PRICE_LOCAL_DB_ENV) or "").strip()
    candidates: List[Path] = []
    if env:
        candidates.append(Path(env))
    candidates.extend(sorted(Path(STRUCTURED_DATA_DIR).glob("land_price_seoul_*.sqlite"), reverse=True))
    candidates.extend(sorted(Path(BASE_DIR).glob("land_price_seoul_*.sqlite"), reverse=True))
    valid: List[Path] = []
    for path in candidates:
        try:
            if path.is_file() and path.stat().st_size > 1024:
                valid.append(path)
        except Exception:
            continue
    if year is not None:
        matched = [p for p in valid if _land_price_path_year(p) == int(year)]
        if matched:
            return str(matched[0])
        # Environment path may be a multi-year DB without a year in its filename.
        if env:
            ep = Path(env)
            if ep in valid and _land_price_path_year(ep) is None:
                return str(ep)
        return None
    return str(valid[0]) if valid else None


def _land_price_local_archive_path(year: Optional[int] = None) -> Optional[str]:
    env = (os.getenv(LAND_PRICE_LOCAL_ARCHIVE_ENV) or "").strip()
    candidates: List[Path] = []
    if env:
        candidates.append(Path(env))
    candidates.extend(sorted(Path(STRUCTURED_DATA_DIR).glob("land_price_seoul_*.sqlite.zip"), reverse=True))
    candidates.extend(sorted(Path(BASE_DIR).glob("land_price_seoul_*.sqlite.zip"), reverse=True))
    valid: List[Path] = []
    for path in candidates:
        try:
            if path.is_file() and path.stat().st_size > 1024:
                valid.append(path)
        except Exception:
            continue
    if year is not None:
        matched = [p for p in valid if _land_price_path_year(Path(p.name[:-4])) == int(year)]
        if matched:
            return str(matched[0])
        if env:
            ep = Path(env)
            if ep in valid and _land_price_path_year(Path(ep.name[:-4] if ep.name.lower().endswith('.zip') else ep.name)) is None:
                return str(ep)
        return None
    return str(valid[0]) if valid else None


def _ensure_land_price_local_db(year: Optional[int] = None) -> Optional[str]:
    direct = _land_price_local_direct_db_path(year)
    if direct:
        return direct
    archive = _land_price_local_archive_path(year)
    if not archive:
        return None
    with LAND_PRICE_LOCAL_EXTRACT_LOCK:
        try:
            with zipfile.ZipFile(archive) as zf:
                members = [m for m in zf.namelist() if m.lower().endswith(".sqlite") and not m.endswith("/")]
                if not members:
                    logger.warning("bundled land-price archive has no sqlite member: %s", archive)
                    return None
                member = members[0]
                target = os.path.join("/tmp", os.path.basename(member))
                if os.path.isfile(target) and os.path.getsize(target) > 1024:
                    return target
                tmp = target + ".tmp"
                with zf.open(member) as src, open(tmp, "wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
                os.replace(tmp, target)
                return target
        except Exception as exc:
            logger.exception("bundled land-price sqlite extraction failed: %s", exc)
            return None


@lru_cache(maxsize=8192)
def _local_individual_land_price_lookup(pnu: str, year: int) -> Optional[Dict[str, Any]]:
    pnu = str(pnu or "").strip()
    try:
        requested_year = int(year)
    except Exception:
        return None
    if len(pnu) != 19 or not pnu.isdigit():
        return None
    db_path = _ensure_land_price_local_db(requested_year)
    if not db_path:
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            row = conn.execute(
                "SELECT pnu, price_per_m2, year, base_date FROM land_price WHERE pnu=? AND year=?",
                (pnu, requested_year),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        price = float(row[1]) if row[1] is not None else None
        if price is None or not math.isfinite(price) or price <= 0:
            return None
        return {
            "pnu": row[0],
            "year": int(row[2]),
            "month": "01",
            "price_per_m2": price,
            "announcement_date": row[3] or "",
            "standard_land": "",
            "last_update": row[3] or "",
            "legal_dong": "",
            "jibun": "",
            "_route": "bundled_land_price_local_snapshot",
            "_source_type": "LOCAL_SNAPSHOT",
            "_source_date": row[3] or "",
        }
    except Exception as exc:
        logger.info("bundled land-price lookup failed pnu=%s year=%s err=%s", pnu, requested_year, exc)
        return None


def _land_price_local_snapshot_status() -> Dict[str, Any]:
    paths: List[Path] = []
    env_db = (os.getenv(LAND_PRICE_LOCAL_DB_ENV) or "").strip()
    env_zip = (os.getenv(LAND_PRICE_LOCAL_ARCHIVE_ENV) or "").strip()
    if env_db:
        paths.append(Path(env_db))
    if env_zip:
        paths.append(Path(env_zip))
    paths.extend(sorted(Path(STRUCTURED_DATA_DIR).glob("land_price_seoul_*.sqlite")))
    paths.extend(sorted(Path(STRUCTURED_DATA_DIR).glob("land_price_seoul_*.sqlite.zip")))
    paths.extend(sorted(Path(BASE_DIR).glob("land_price_seoul_*.sqlite")))
    paths.extend(sorted(Path(BASE_DIR).glob("land_price_seoul_*.sqlite.zip")))
    seen = set(); files = []; years = set()
    for path in paths:
        try:
            key = str(path.resolve())
            if key in seen or not path.is_file() or path.stat().st_size <= 1024:
                continue
            seen.add(key)
        except Exception:
            continue
        files.append(path.name)
        name = path.name[:-4] if path.name.lower().endswith('.zip') else path.name
        y = _land_price_path_year(Path(name))
        if y is not None:
            years.add(y)
    return {
        "configured": bool(files),
        "files": files,
        "years": sorted(years),
        "mode": "multi_snapshot_exact_year",
        "fallback_policy": "exact_requested_year_only",
    }


def _resolve_individual_land_price(pnu: str, year: int, timeout: int = 8) -> Optional[Dict[str, Any]]:
    # User-approved ordering: VWorld live first, bundled official snapshot second.
    live = _server_individual_land_price_vworld(pnu, int(year), timeout) if _vworld_key() else None
    if live:
        live.setdefault("_source_type", "VWORLD_LIVE")
        return live
    local = _local_individual_land_price_lookup(pnu, int(year))
    if local:
        local["_live_attempted"] = bool(_vworld_key())
        return local
    return None

def _land_ledger_cache_get(pnu: str) -> Optional[Dict[str, Any]]:
    now = time.monotonic()
    with LAND_LEDGER_CACHE_LOCK:
        item = LAND_LEDGER_CACHE.get(pnu)
        if not item:
            return None
        if float(item.get("expires_at") or 0) <= now:
            LAND_LEDGER_CACHE.pop(pnu, None)
            return None
        payload = dict(item.get("payload") or {})
        record = payload.get("record")
        payload["record"] = dict(record) if isinstance(record, dict) else None
        payload["cache_hit"] = True
        return payload


def _land_ledger_cache_put(pnu: str, payload: Dict[str, Any]) -> None:
    record = payload.get("record")
    if isinstance(record, dict):
        ttl = LAND_LEDGER_CACHE_TTL_POSITIVE_SEC
    elif payload.get("external_upstream_error"):
        # Do not preserve a VWorld outage as a five-minute negative fact.
        ttl = min(30, LAND_LEDGER_CACHE_TTL_NEGATIVE_SEC)
    else:
        ttl = LAND_LEDGER_CACHE_TTL_NEGATIVE_SEC
    cached = dict(payload)
    cached["record"] = dict(record) if isinstance(record, dict) else None
    cached["cache_hit"] = False
    with LAND_LEDGER_CACHE_LOCK:
        LAND_LEDGER_CACHE[pnu] = {
            "expires_at": time.monotonic() + max(1, int(ttl)),
            "payload": cached,
        }


def _resolve_land_ledger_uncached(pnu: str) -> Dict[str, Any]:
    """Resolve one PNU without changing legal/source semantics.

    Two *full land-ledger* routes are started together because they normalize to
    the same ladfrlList schema.  The first valid full-ledger record wins.
    VWorld land-characteristics remains lower-priority and is queried only if
    both full-ledger routes fail/return no record.
    """
    started = time.perf_counter()
    full_sources = []
    if _vworld_key():
        full_sources.append(("vworld_ladfrl", _server_land_ledger_vworld))
    if _building_hub_key():
        full_sources.append(("data_go_ladfrl", _server_land_ledger_legacy_data_go))

    attempts: List[Dict[str, Any]] = []
    circuit_before = _vworld_circuit_snapshot()
    if circuit_before.get("open"):
        full_sources = [(name, fn) for name, fn in full_sources if not name.startswith("vworld_")]
        if _vworld_key():
            attempts.append({
                "source": "vworld_ladfrl", "ok": False, "skipped": "VWORLD_CIRCUIT_OPEN",
                "retry_after_seconds": circuit_before.get("retry_after_seconds", 0),
                "completed_ms": round((time.perf_counter()-started)*1000.0, 1),
            })
    if full_sources:
        futures = {
            LAND_LEDGER_SOURCE_EXECUTOR.submit(fn, pnu, 6): name
            for name, fn in full_sources
        }
        # IMPORTANT: no context-manager shutdown(wait=True) here.  If one full
        # ledger succeeds, the endpoint must not wait for the slower peer.
        for fut in as_completed(futures):
            name = futures[fut]
            t0 = time.perf_counter()
            try:
                record = fut.result()
                attempts.append({"source": name, "ok": bool(record), "completed_ms": round((time.perf_counter()-started)*1000.0, 1)})
            except Exception as exc:
                record = None
                attempts.append({"source": name, "ok": False, "error": str(exc)[:160], "completed_ms": round((time.perf_counter()-started)*1000.0, 1)})
            if record is not None:
                return {
                    "record": record,
                    "dataset": "토지임야정보(속성정보)",
                    "operation": "ladfrlList",
                    "selected_source": name,
                    "attempts": attempts,
                    "elapsed_ms": round((time.perf_counter()-started)*1000.0, 1),
                    "cache_hit": False,
                    "external_upstream_error": bool(_vworld_circuit_snapshot().get("open")),
                    "vworld_circuit": _vworld_circuit_snapshot(),
                }

    # Full ledger is preferred over characteristics.  Characteristics is only a
    # fallback for area/category fields and therefore never races ahead of a
    # still-possible full ledger result.
    circuit_after_full = _vworld_circuit_snapshot()
    if _vworld_key() and not circuit_after_full.get("open"):
        char_record = _server_land_characteristics_vworld(pnu, 6)
        attempts.append({
            "source": "vworld_land_characteristics",
            "ok": bool(char_record),
            "completed_ms": round((time.perf_counter()-started)*1000.0, 1),
        })
    else:
        char_record = None
        if _vworld_key():
            attempts.append({
                "source": "vworld_land_characteristics", "ok": False, "skipped": "VWORLD_CIRCUIT_OPEN",
                "retry_after_seconds": circuit_after_full.get("retry_after_seconds", 0),
                "completed_ms": round((time.perf_counter()-started)*1000.0, 1),
            })
    final_circuit = _vworld_circuit_snapshot()
    if char_record is not None:
        return {
            "record": char_record,
            "dataset": "토지특성정보",
            "operation": "getLandCharacteristics",
            "selected_source": "vworld_land_characteristics",
            "attempts": attempts,
            "elapsed_ms": round((time.perf_counter()-started)*1000.0, 1),
            "cache_hit": False,
            "external_upstream_error": bool(final_circuit.get("open")),
            "vworld_circuit": final_circuit,
        }

    # R28: live sources exhausted -> bundled Seoul AL_D003 snapshot.
    local_record = _local_land_ledger_lookup(pnu)
    attempts.append({
        "source": "local_AL_D003_snapshot", "ok": bool(local_record),
        "completed_ms": round((time.perf_counter()-started)*1000.0, 1),
    })
    return {
        "record": local_record,
        "dataset": "토지임야정보(속성정보) 보유자료" if local_record is not None else "토지임야정보(속성정보)",
        "operation": "PNU_LOCAL_LOOKUP" if local_record is not None else "ladfrlList",
        "selected_source": "local_AL_D003_snapshot" if local_record is not None else None,
        "attempts": attempts,
        "elapsed_ms": round((time.perf_counter()-started)*1000.0, 1),
        "cache_hit": False,
        "external_upstream_error": bool(final_circuit.get("open")),
        "vworld_circuit": final_circuit,
    }


def _resolve_land_ledger(pnu: str) -> Dict[str, Any]:
    cached = _land_ledger_cache_get(pnu)
    if cached is not None:
        return cached
    payload = _resolve_land_ledger_uncached(pnu)
    _land_ledger_cache_put(pnu, payload)
    return payload


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
    # 토지이음/VWorld NED 토지이용계획정보 중 공용 보전 FACT만 분류한다.
    # 도시재생혁신지구는 사용자 확정 김포공항 SHP를 기존 추진사업 FACT에 직접 병합하므로
    # 이 NED 분류·판정 경로에서는 더 이상 다루지 않는다.
    if "비오톱" in n and "1등급" in n:
        return "biotope_grade1"
    if "공익용산지" in n:
        return "public_interest_forest"
    return None


def _regulatory_land_use_category(name: str) -> Optional[str]:
    """개발·계획제한 독립 정보모듈용 NED 명칭 분류.

    양성 행만 규제 존재의 근거로 사용한다. 전용 공간원도형이 연결되지 않은
    항목은 NED 음성만으로 '규제 없음'을 확정하지 않는다.
    """
    n = re.sub(r"\s+", "", str(name or ""))
    if not n:
        return None
    if "도시자연공원구역" in n:
        return "urban_natural_park"
    if ("자연공원" in n or "공원구역" in n) and "도시자연공원" not in n:
        return "natural_park"
    if "생태경관보전지역" in n or "생태·경관보전지역" in n:
        return "ecological_landscape"
    if "야생생물특별보호구역" in n or "야생동식물특별보호구역" in n:
        return "wildlife_special"
    if "공익용산지" in n:
        return "public_interest_forest"
    if "보전산지" in n and "준보전산지" not in n:
        return "conservation_forest"
    if "상수원보호구역" in n:
        return "water_source"
    if "하천구역" in n:
        return "river_zone"
    if "철도보호지구" in n or "철도보호구역" in n:
        return "railroad_protection"
    if "장애물제한표면" in n or "공항시설보호지구" in n:
        return "airport_obstacle"
    if any(k in n for k in ("군사시설보호", "비행안전구역", "비행안전", "대공방어")):
        return "military_flight"
    if "자연재해위험개선지구" in n or "자연재해위험지구" in n:
        return "disaster_risk"
    if "산사태취약지역" in n or "산사태위험지역" in n:
        return "landslide_risk"
    if "급경사지붕괴위험지역" in n or "급경사지붕괴위험지구" in n:
        return "steep_slope_risk"
    if "홍수관리구역" in n:
        return "flood_management"
    if "역사문화환경보존지역" in n or "역사문화환경보존구역" in n:
        return "heritage_environment"
    if "교육환경보호구역" in n:
        return "education_environment"
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


ROUTE_COMMERCIAL_REFERENCE_PATH = _data_path("route_commercial_reference.geojson")
@lru_cache(maxsize=1)
def _route_commercial_reference_data():
    """역세권활성화 간선가로형 검토용 내부 판정모형 참조도형.

    법정 용도지역 원도나 서울시 공식 노선형 상업지역 도형이 아니다.
    일반 용도지역 FACT를 대체하지 않고 간선가로형 입지판정에만 사용한다.
    """
    try:
        with open(ROUTE_COMMERCIAL_REFERENCE_PATH, encoding="utf-8") as fp:
            data = json.load(fp)
    except FileNotFoundError:
        return {
            "type": "FeatureCollection",
            "name": "route_commercial_model_reference",
            "metadata": {
                "available": False,
                "source_type": "MODEL_REFERENCE",
                "legal_source": False,
                "reference_name": "노선형 상업지역 판정용 참조도형",
                "reason": "route_commercial_reference.geojson 미설치",
            },
            "features": [],
        }
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
        raise RuntimeError("route_commercial_reference.geojson 형식 오류")
    meta = dict(data.get("metadata") or {})
    meta.update({
        "available": True,
        "source_type": "MODEL_REFERENCE",
        "legal_source": False,
        "reference_name": meta.get("reference_name") or "노선형 상업지역 판정용 참조도형",
        "model_use": meta.get("model_use") or "역세권활성화 간선가로형 검토 전용",
    })
    data["metadata"] = meta
    return data

REGULATION_CHANGE_MONITOR_PATH = _data_path("regulation_change_monitor.json")
@lru_cache(maxsize=1)
def _regulation_change_monitor_data():
    """사업판정 제도변화 모니터링 피드.

    최신 법령/운영기준 탐지 프로세스와 판정엔진을 분리하여,
    새 기준이 확인되었지만 코드에 아직 반영되지 않은 경우 UI에서 먼저 경고한다.
    """
    try:
        with open(REGULATION_CHANGE_MONITOR_PATH, encoding="utf-8") as fp:
            data = json.load(fp)
    except FileNotFoundError:
        return {
            "schema": "regulation_change_monitor_v1",
            "checked_at": None,
            "alerts": [],
            "changes": [],
            "note": "regulation_change_monitor.json 미설치",
            "available": False,
        }
    if not isinstance(data, dict):
        raise RuntimeError("regulation_change_monitor.json 형식 오류")
    data.setdefault("schema", "regulation_change_monitor_v1")
    data.setdefault("alerts", [])
    data.setdefault("changes", [])
    data["available"] = True
    return data

SAFE_DOWNTOWN_EXCLUSION_REFERENCE_PATH = _data_path("safe_downtown_exclusion_reference.geojson")
@lru_cache(maxsize=1)
def _safe_downtown_exclusion_reference_data():
    """사용자 제공 DXF를 변환한 안심주택 서울도심 배제구간 내부 판정 참조도형."""
    try:
        with open(SAFE_DOWNTOWN_EXCLUSION_REFERENCE_PATH, encoding="utf-8") as fp:
            data = json.load(fp)
    except FileNotFoundError:
        return {
            "type": "FeatureCollection",
            "name": "safe_downtown_exclusion_reference",
            "metadata": {
                "available": False,
                "source_type": "USER_CURATED_MODEL_REFERENCE",
                "legal_source": False,
                "reference_name": "안심주택 서울도심 배제범위 내부 참조도형",
                "reason": "safe_downtown_exclusion_reference.geojson 미설치",
            },
            "features": [],
        }
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
        raise RuntimeError("safe_downtown_exclusion_reference.geojson 형식 오류")
    meta = dict(data.get("metadata") or {})
    meta.update({
        "available": True,
        "source_type": "USER_CURATED_MODEL_REFERENCE",
        "legal_source": False,
        "reference_name": meta.get("reference_name") or "안심주택 서울도심 배제범위 내부 참조도형",
        "model_use": "안심주택 서울도심 기본계획 범역 내 배제구간 판정 전용",
    })
    data["metadata"] = meta
    return data


def _safe_downtown_exclusion_analysis(geometry: Dict[str, Any]) -> Dict[str, Any]:
    site = shape(geometry)
    if site.geom_type not in {"Polygon", "MultiPolygon"} or site.is_empty:
        raise ValueError("Polygon 또는 MultiPolygon만 지원합니다.")
    if not site.is_valid:
        site = site.buffer(0)
    fc = _safe_downtown_exclusion_reference_data()
    meta = dict(fc.get("metadata") or {})
    if not meta.get("available"):
        return {"status":"unavailable","known":False,"present":None,"overlap_area_m2":None,"overlap_pct":None,"features":[],"metadata":meta}
    site_area = abs(float(GEOD.geometry_area_perimeter(site)[0]))
    overlaps=[]; overlap_geoms=[]
    for ft in fc.get("features") or []:
        try:
            g=shape(ft.get("geometry"))
            x=site.intersection(g)
            if x.is_empty: continue
            a=abs(float(GEOD.geometry_area_perimeter(x)[0]))
            if a<=0.01: continue
            overlap_geoms.append(x)
            overlaps.append({"type":"Feature","geometry":mapping(x),"properties":dict(ft.get("properties") or {},_overlap_area_m2=a)})
        except Exception:
            continue
    if overlap_geoms:
        union=unary_union(overlap_geoms)
        overlap_area=abs(float(GEOD.geometry_area_perimeter(union)[0]))
    else:
        overlap_area=0.0
    return {
        "status":"available",
        "known":True,
        "present":overlap_area>0.01,
        "site_area_m2":site_area,
        "overlap_area_m2":overlap_area,
        "overlap_pct":(overlap_area/site_area*100.0 if site_area>0 else None),
        "features":fc.get("features") or [],
        "overlap_features":overlaps,
        "metadata":meta,
    }


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


# R29: 서울플랜+ UQ120의 공식 사업유형/추진단계 코드표를 별도 '기존 추진사업 FACT'로 사용한다.
# 이 레지스트리는 정비구역 법정 배제판정에 섞지 않고, 대상지에서 실제로 추진 중인 사업의
# 명칭·유형·현재 단계를 보여주는 현황정보 전용이다.
PLANPLUS_PROJECT_TYPES = {'BZ101': ('renewal', '신속통합기획'),
 'BZ102': ('renewal', '재개발(도시정비형)'),
 'BZ103': ('renewal', '재개발(주택정비형)'),
 'BZ104': ('renewal', '재건축(단독)'),
 'BZ105': ('renewal', '재건축(공동)'),
 'BZ107': ('renewal', '주거환경개선(관리형)'),
 'BZ108': ('renewal', '주거환경개선(정비형)'),
 'BZ201': ('smallscale', '모아타운'),
 'BZ202': ('smallscale', '가로주택정비사업'),
 'BZ203': ('smallscale', '자율주택정비사업'),
 'BZ204': ('smallscale', '소규모재건축사업'),
 'BZ205': ('smallscale', '소규모재개발사업'),
 'BZ301': ('station', '역세권 장기전세주택'),
 'BZ302': ('station', '역세권활성화사업'),
 'BZ303': ('station', '청년안심주택'),
 'BZ306': ('station', '미리내집'),
 'BZ401': ('promotion', '재정비촉진지구'),
 'BZ402': ('promotion', '재정비촉진구역'),
 'BZ403': ('promotion', '존치정비구역'),
 'BZ404': ('promotion', '존치관리구역'),
 'BZ501': ('national', '공공주택지구조성사업'),
 'BZ502': ('national', '도심 공공주택 복합사업'),
 'BZ601': ('other', '도시개발사업'),
 'BZ602': ('other', '리모델링활성화구역'),
 'BZ603': ('other', '시장정비사업')}
URBAN_REGEN_INNOVATION_SHP_BASE = _data_path("urban_regeneration_innovation_gimpo")
URBAN_REGEN_INNOVATION_SHP_REQUIRED = tuple(
    URBAN_REGEN_INNOVATION_SHP_BASE + ext for ext in (".shp", ".shx", ".dbf", ".prj")
)


def _urban_regen_innovation_shp_ready() -> bool:
    return all(os.path.isfile(path) for path in URBAN_REGEN_INNOVATION_SHP_REQUIRED)


@lru_cache(maxsize=1)
def _urban_regen_innovation_project_features() -> Dict[str, Any]:
    """사용자 확정 DXF에서 변환한 김포공항 도시재생혁신지구 SHP를
    기존 추진사업 FACT(UQ120 registry)에 병합한다.

    UQ120/BZ604 도시재생활성화지역은 더 이상 사용하지 않으며, 별도 NED·참조
    GeoJSON·대표지번·근접버퍼 분석모듈도 사용하지 않는다.
    """
    metadata = {
        "available": False,
        "source": "user_confirmed_shp",
        "source_title": "김포공항 도시재생혁신지구 SHP",
        "source_file": "urban_regeneration_innovation_gimpo.shp",
        "source_crs": "EPSG:5181",
        "geometry_basis": "user_confirmed_dxf_converted_to_shp",
        "feature_count": 0,
        "source_area_m2": None,
    }
    if not _urban_regen_innovation_shp_ready():
        metadata["reason"] = "urban_regeneration_innovation_gimpo SHP 구성파일 미설치"
        return {"features": [], "metadata": metadata}

    try:
        reader = shapefile.Reader(
            URBAN_REGEN_INNOVATION_SHP_BASE,
            encoding="utf-8",
            encodingErrors="replace",
        )
        fields = [f[0] for f in reader.fields[1:]]
    except Exception as exc:
        metadata["reason"] = f"김포공항 도시재생혁신지구 SHP 로딩 실패: {str(exc)[:180]}"
        return {"features": [], "metadata": metadata}
    to_wgs = Transformer.from_crs(5181, 4326, always_xy=True).transform
    features: List[Dict[str, Any]] = []
    source_area_m2 = 0.0
    for idx, sr in enumerate(reader.iterShapeRecords(), start=1):
        row = dict(zip(fields, list(sr.record)))
        try:
            geom = shape(sr.shape.__geo_interface__)
            if not geom.is_valid:
                geom = geom.buffer(0)
            geom = _polygonal_only(geom)
            if geom is None or geom.is_empty:
                continue
            source_area_m2 += float(geom.area)
            geom_wgs = geometry_transform(to_wgs, geom)
        except Exception:
            continue
        features.append({
            "type": "Feature",
            "geometry": mapping(geom_wgs),
            "properties": {
                "source": "planplus_project",
                "source_title": "김포공항 도시재생혁신지구 SHP",
                "source_layer": "urban_regeneration_innovation_gimpo",
                "source_feature_id": f"GIMPO_URBAN_REGEN_INNOVATION_{idx}",
                "project_code": "URBAN_REGEN_INNOVATION_GIMPO",
                "project_group": "other",
                "type_label": "도시재생혁신지구",
                "name": str(row.get("NAME_KR") or "김포공항 도시재생혁신지구"),
                "project_stage_code": "",
                "project_stage_label": "기준경계",
                "group_code": "",
                "district_code": "11500",
                "notice_no": "",
                "notice_date": "",
                "data_reference_date": "2026-09-18",
                "history_status": "reference_boundary",
                "source_type": "USER_CONFIRMED_SHP",
            },
        })
    metadata.update({
        "available": bool(features),
        "feature_count": len(features),
        "source_area_m2": round(source_area_m2, 2),
    })
    if not features:
        metadata["reason"] = "김포공항 도시재생혁신지구 SHP 형상 0건"
    return {"features": features, "metadata": metadata}


PLANPLUS_STAGE_LABELS = {'PP0101': '대상지선정(추진중)',
 'PP0102': '대상지선정',
 'PP0103': '기획완료',
 'PP0104': '보류',
 'PP0201': '입안제안',
 'PP0202': '열람공고',
 'PP0203': '위원회심의',
 'PP0204': '구역지정',
 'PP0205': '추진위구성',
 'PP0206': '조합설립인가',
 'PP0207': '건축심의',
 'PP0208': '사업시행인가',
 'PP0209': '관리처분계획인가',
 'PP0210': '착공',
 'PP0211': '준공',
 'PP0301': '대상지선정',
 'PP0302': '정비계획수립',
 'PP0303': '위원회심의',
 'PP0304': '구역지정',
 'PP0305': '사업시행인가',
 'PP0306': '착공',
 'PP0307': '준공(일부)',
 'PP0308': '준공',
 'PP0401': '수립범위 자문',
 'PP0402': '대상지선정',
 'PP0403': '관리지역고시',
 'PP0404': '사전자문',
 'PP0405': '위원회심의',
 'PP0406': '관리지역고시',
 'PP0500': '조합설립인가 추진중(연번부여)',
 'PP0501': '조합설립인가',
 'PP0502': '건축심의',
 'PP0503': '사업시행인가',
 'PP0504': '착공',
 'PP0505': '준공',
 'PP0601': '주민합의체 구성',
 'PP0602': '건축심의',
 'PP0603': '사업시행인가',
 'PP0604': '착공',
 'PP0605': '준공',
 'PP0701': '조합설립추진중',
 'PP0702': '조합설립인가',
 'PP0703': '건축심의',
 'PP0704': '사업시행계획인가',
 'PP0705': '착공',
 'PP0706': '준공',
 'PP0801': '대상지선정',
 'PP0802': '사전검토',
 'PP0803': '입안제안',
 'PP0804': '열람공고',
 'PP0805': '위원회심의',
 'PP0806': '구역지정',
 'PP0807': '건축심의',
 'PP0808': '사업계획승인',
 'PP0809': '착공',
 'PP0810': '준공',
 'PP0901': '대상지선정',
 'PP0902': '통심위 사전자문',
 'PP0903': '입안제안',
 'PP0904': '열람공고',
 'PP0905': '위원회심의',
 'PP0906': '구역지정',
 'PP0907': '건축심의',
 'PP0908': '사업계획승인',
 'PP0909': '건축허가',
 'PP0910': '착공',
 'PP0911': '사용승인',
 'PP0912': '입주',
 'PP1001': '지구지정',
 'PP1002': '지구변경',
 'PP1101': '대상지선정',
 'PP1102': '촉진계획수립(변경)',
 'PP1103': '열람공고',
 'PP1104': '위원회심의',
 'PP1105': '구역지정',
 'PP1107': '추진위구성',
 'PP1108': '조합설립인가',
 'PP1109': '건축심의',
 'PP1110': '사업시행인가',
 'PP1111': '관리처분계획인가',
 'PP1112': '착공',
 'PP1113': '준공',
 'PP1201': '예정지구지정',
 'PP1202': '후보지선정',
 'PP1203': '지구지정',
 'PP1204': '설계공모완료',
 'PP1205': '사업계획승인',
 'PP1206': '착공',
 'PP1207': '준공',
 'PP1208': '입주중',
 'PP1209': '후보지철회',
 'PP1301': '입안제안',
 'PP1302': '열람공고',
 'PP1303': '위원회심의',
 'PP1304': '구역지정',
 'PP1305': '실시계획인가',
 'PP1306': '준공',
 'PP1401': '조합설립인가',
 'PP1402': '1차 안전진단',
 'PP1403': '건축심의',
 'PP1404': '리모델링허가승인',
 'PP1405': '2차 안전진단',
 'PP1406': '착공',
 'PP1407': '준공',
 'PP1501': '추진계획수립중',
 'PP1502': '추진계획승인',
 'PP1503': '조합설립인가',
 'PP1504': '사업시행계획인가',
 'PP1505': '관리처분계획인가',
 'PP1506': '착공',
 'PP1507': '준공',
 'PP1601': '대상지선정',
 'PP1602': '활성화계획수립',
 'PP1603': '마중물사업(추진중)',
 'PP1604': '사업완료',
 'PP1801': '대상지선정',
 'PP1802': '통심위 사전자문',
 'PP1803': '입안제안',
 'PP1804': '열람공고',
 'PP1805': '위원회심의',
 'PP1806': '구역지정',
 'PP1807': '건축심의/통합심의',
 'PP1808': '사업계획승인',
 'PP1809': '착공',
 'PP1810': '준공',
 'PP1901': '입주자 모집공고 완료',
 'PP2001': '입안제안',
 'PP2002': '열람공고',
 'PP2003': '위원회심의',
 'PP2004': '구역지정',
 'PP2005': '지구계획승인(변경)',
 'PP2006': '착공',
 'PP2007': '준공',
 'PP2101': '구역지정',
 'PP2102': '구역변경'}



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
                    "source_feature_id": str(row.get("PRESENT_SN") or "").strip(),
                    "notice_no": str(row.get("NTFC_SN") or "").strip(),
                    # CREATE_DAT은 고시일이 아니라 배포 데이터 생성일이므로 고시일로 오인하지 않는다.
                    "notice_date": "",
                    "data_reference_date": str(row.get("CREATE_DAT") or "").strip(),
                    "project_stage_code": str(row.get("PROPEL_CD") or "").strip() if source == "project" else "",
                    "project_stage_label": PLANPLUS_STAGE_LABELS.get(str(row.get("PROPEL_CD") or "").strip(), "") if source == "project" else "",
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


@lru_cache(maxsize=1)
def _planplus_project_reference_data():
    """서울플랜+ UQ120 전체 사업유형을 기존 추진사업 현황 FACT로 변환한다.

    정비구역 판정용 `_renewal_reference_data()`와 의도적으로 분리한다.
    UQ120의 CREATE_DAT은 고시일이 아닌 데이터 생성일이며, PROPEL_CD는 현재 추진단계 코드다.
    """
    transformer = Transformer.from_crs(5174, 4326, always_xy=True)
    reader = _read_embedded_shapefile(RENEWAL_PROJECT_ZIP_PATH, "UPIS_C_UQ120")
    fields = [f[0] for f in reader.fields[1:]]
    features = []
    for sr in reader.iterShapeRecords():
        row = dict(zip(fields, sr.record))
        code = str(row.get("SCLAS_CL") or row.get("ATRB_SE") or row.get("MLSFC_CL") or "").strip()
        type_info = PLANPLUS_PROJECT_TYPES.get(code)
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
        stage_code = str(row.get("PROPEL_CD") or "").strip()
        group, type_label = type_info
        features.append({
            "type": "Feature",
            "geometry": mapping(geom),
            "properties": {
                "source": "planplus_project",
                "source_title": "서울 도시계획사업 현황(서울플랜+, UQ120)",
                "source_layer": "UPIS_C_UQ120",
                "source_feature_id": str(row.get("PRESENT_SN") or "").strip(),
                "project_code": code,
                "project_group": group,
                "type_label": type_label,
                "name": str(row.get("DGM_NM") or "미상사업").strip(),
                "project_stage_code": stage_code,
                "project_stage_label": PLANPLUS_STAGE_LABELS.get(stage_code, "단계코드 미확인" if stage_code else "추진단계 미입력"),
                "group_code": str(row.get("GRP") or "").strip(),
                "district_code": str(row.get("SIGNGU_SE") or "").strip(),
                "notice_no": str(row.get("NTFC_SN") or "").strip(),
                "notice_date": "",
                "data_reference_date": str(row.get("CREATE_DAT") or "").strip(),
                "history_status": "current_stage_only",
            },
        })
    innovation = _urban_regen_innovation_project_features()
    features.extend(innovation.get("features") or [])
    return {
        "type": "FeatureCollection",
        "name": "서울플랜+ 기존 추진사업 현황 + 김포공항 도시재생혁신지구",
        "features": features,
        "metadata": {
            "reference_month": "2026-02",
            "source": "서울 도시계획사업 현황(서울플랜+, UQ120) + 김포공항 도시재생혁신지구 SHP",
            "classification_source": "uq120_project.zip 내부 서울플랜+ 코드정의표",
            "history_scope": "현재 추진단계까지 제공 · 단계별 고시/승인일 이력은 후속 결정고시 연계 필요",
            "create_dat_semantics": "CREATE_DAT은 고시일이 아닌 데이터 생성일",
            "urban_regeneration_innovation_shp": innovation.get("metadata") or {},
        },
    }


@lru_cache(maxsize=1)
def _planplus_project_spatial_index():
    fc = _planplus_project_reference_data()
    features = fc["features"]
    geometries = [shape(feature["geometry"]) for feature in features]
    return features, geometries, STRtree(geometries)


def _planplus_project_intersections(site_wgs: Any, site_metric: Any, site_area: float, to_metric: Any, to_wgs: Any) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    features, geometries, tree = _planplus_project_spatial_index()
    overlaps: List[Dict[str, Any]] = []
    context_features: List[Dict[str, Any]] = []
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
        context_props = dict(props)
        context_props["_display_role"] = "source_project"
        context_features.append({"type": "Feature", "geometry": feature.get("geometry"), "properties": context_props})
    overlaps.sort(key=lambda f: (
        -float(f["properties"].get("overlap_area_m2") or 0),
        str(f["properties"].get("project_code") or ""),
        str(f["properties"].get("name") or ""),
    ))
    return overlaps, context_features


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
    project_registry_overlaps, project_registry_context = _planplus_project_intersections(
        site_wgs, site_metric, site_area, to_metric, to_wgs
    )
    return {
        "status": "matched" if overlaps else "none",
        "site_area_m2": round(site_area, 2),
        "renewal_area_type": primary["properties"]["renewal_type"] if primary else "none",
        "promotion_status": "district" if primary_promotion else "none",
        "primary": primary,
        "primary_promotion": primary_promotion,
        "overlaps": overlaps,
        "context_features": context_features,
        "project_registry_overlaps": project_registry_overlaps,
        "project_registry_context_features": project_registry_context,
        "project_registry_metadata": _planplus_project_reference_data()["metadata"],
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
                "notice_date": "",
                "data_reference_date": str(row.get("CREATE_DAT") or "").strip(),
            },
        })
    return {
        "type": "FeatureCollection",
        "name": "서울시 도시계획·개발사업 법정구역",
        "features": features,
        "metadata": {
            "reference_month": "2026-09",
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


def analyze_industrial_park_intersections(geometry: Dict[str, Any]) -> Dict[str, Any]:
    """VWorld 최신 산업단지 경계(LT_C_DAMDAN)와 대상지를 실제 중첩한다.

    2040 서울 공업지역기본계획은 산업단지 등 다른 법률로 결정된 공업지역을
    적용대상에서 제외하므로, UQ181의 보조코드가 아니라 전국 산업단지 전용
    경계 레이어를 우선 사용한다.
    """
    site_wgs = _polygonal_only(shape(geometry))
    if site_wgs is None or site_wgs.is_empty:
        raise ValueError("구역계는 Polygon 또는 MultiPolygon이어야 합니다.")
    if not site_wgs.is_valid:
        site_wgs = _polygonal_only(site_wgs.buffer(0))
    if site_wgs is None or site_wgs.is_empty or not site_wgs.is_valid:
        raise ValueError("유효하지 않은 구역계입니다.")

    candidates = _vworld_features_in_bbox(VWORLD_LAYER_INDUSTRIAL_PARK, site_wgs, size=200, max_pages=3)
    to_metric = Transformer.from_crs(4326, 5174, always_xy=True).transform
    site_metric = geometry_transform(to_metric, site_wgs)
    site_area = float(site_metric.area)
    overlaps: List[Dict[str, Any]] = []
    context_features: List[Dict[str, Any]] = []
    for f in candidates:
        try:
            source_wgs = _polygonal_only(shape(f.get("geometry")))
            if source_wgs is None or source_wgs.is_empty:
                continue
            inter = _polygonal_only(site_wgs.intersection(source_wgs))
            if inter is None or inter.is_empty:
                continue
            inter_metric = _polygonal_only(geometry_transform(to_metric, inter))
            if inter_metric is None or inter_metric.is_empty:
                continue
            overlap_area = float(inter_metric.area)
            if overlap_area < 0.5:
                continue
            source_area = float(geometry_transform(to_metric, source_wgs).area)
        except Exception:
            continue
        raw = dict(f.get("properties") or {})
        low = {str(k).lower(): _json_property(v) for k, v in raw.items()}
        name = str(low.get("dan_name") or low.get("dan_nm") or low.get("name") or "산업단지").strip()
        dan_id = str(low.get("dan_id") or low.get("dan_cd") or "").strip()
        dan_type_raw = str(low.get("dan_type") or low.get("type") or "").strip()
        type_label = {"1": "국가산업단지", "2": "일반산업단지", "3": "도시첨단산업단지", "4": "농공단지"}.get(dan_type_raw, "산업단지")
        props = {
            "source": "vworld_live_industrial_park",
            "source_title": "VWorld 산업단지 경계(LT_C_DAMDAN)",
            "source_layer": VWORLD_LAYER_INDUSTRIAL_PARK,
            "development_kind": "industrial_park",
            "dan_id": dan_id,
            "dan_type": dan_type_raw,
            "type_label": type_label,
            "name": name,
            "overlap_area_m2": round(overlap_area, 2),
            "site_overlap_pct": round(overlap_area / site_area * 100, 4) if site_area > 0 else None,
            "zone_overlap_pct": round(overlap_area / source_area * 100, 4) if source_area > 0 else None,
            "_overlap_area": round(overlap_area, 2),
            "_overlap_pct": round(overlap_area / site_area * 100, 4) if site_area > 0 else None,
        }
        overlaps.append({"type": "Feature", "geometry": mapping(inter), "properties": props})
        context_features.append({"type": "Feature", "geometry": f.get("geometry"), "properties": {**props, "_display_role": "source_zone"}})
    overlaps.sort(key=lambda f: (-float((f.get("properties") or {}).get("overlap_area_m2") or 0), str((f.get("properties") or {}).get("name") or "")))
    return {
        "status": "matched" if overlaps else "none",
        "overlaps": overlaps,
        "context_features": context_features,
        "metadata": {
            "available": True,
            "source": "VWorld 2D Data API",
            "source_layer": VWORLD_LAYER_INDUSTRIAL_PARK,
            "source_title": "국토교통부 산업단지 단지경계",
            "source_type": "VWORLD_LIVE",
            "note": "국가·일반·도시첨단·농공단지 전용 경계. 2040 서울 공업지역기본계획 적용제외 판정에 사용",
        },
    }


def analyze_development_intersections(geometry: Dict[str, Any], include_industrial: bool = True) -> Dict[str, Any]:
    """도시개발·공공주택지구·기타 법정 사업구역을 서버에서 실제 중첩한다.

    include_industrial=False이면 번들 UQ181 중첩만 즉시 계산하고 VWorld LT_C_DAMDAN은 호출하지 않는다.
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
    industrial_parks, industrial_park_context, industrial_park_metadata = [], [], {
        "available": False,
        "fallback_available": False,
        "fallback_authoritative": False,
        "source": "VWorld 2D Data API",
        "source_layer": VWORLD_LAYER_INDUSTRIAL_PARK,
        "source_title": "국토교통부 산업단지 단지경계",
        "source_type": "VWORLD_LIVE",
        "error": "후속조회 대기" if not include_industrial else "미조회",
        "note": "UQ181 로컬 개발사업 현황을 먼저 표시하고 산업단지 전용경계는 별도 보강조회한다." if not include_industrial else "",
    }
    if include_industrial:
        try:
            industrial_result = analyze_industrial_park_intersections(geometry)
            industrial_parks = industrial_result.get("overlaps") or []
            industrial_park_context = industrial_result.get("context_features") or []
            industrial_park_metadata = industrial_result.get("metadata") or {}
        except Exception as exc:
            # 산업단지 전용경계는 LT_C_DAMDAN을 권위자료로 사용한다.
            # UQ181에는 현재 배포자료상 UQ1400/UQ1300 산업단지 계열이 없으므로
            # API 실패를 "비중첩 확정"으로 대체하지 않는다.
            industrial_park_metadata = {
                "available": False,
                "fallback_available": False,
                "fallback_authoritative": False,
                "source": "VWorld 2D Data API",
                "source_layer": VWORLD_LAYER_INDUSTRIAL_PARK,
                "source_title": "국토교통부 산업단지 단지경계",
                "source_type": "VWORLD_LIVE",
                "error": str(exc),
                "note": "산업단지 전용경계 조회 실패 · 적용제외 여부는 REVIEW 유지",
            }
            logger.warning("industrial park boundary analysis unavailable: %s", exc)
    return {
        "status": "matched" if overlaps else "none",
        "site_area_m2": round(site_area, 2),
        "overlaps": overlaps,
        "context_features": context_features,
        "industrial_parks": industrial_parks,
        "industrial_park_context_features": industrial_park_context,
        "industrial_park_metadata": industrial_park_metadata,
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




ROAD_SHAPE_REQUIRED = tuple(
    f"{stem}{ext}"
    for stem in ("TL_SPRD_RW", "TL_SPRD_MANAGE")
    for ext in (".shp", ".shx", ".dbf")
)


def _road_shape_dir_complete(path: str) -> bool:
    return bool(path) and os.path.isdir(path) and all(
        os.path.isfile(os.path.join(path, name)) and os.path.getsize(os.path.join(path, name)) > 0
        for name in ROAD_SHAPE_REQUIRED
    )


def _road_zip_candidates() -> List[str]:
    return [
        os.path.join(STRUCTURED_DATA_DIR, "road_shp_seoul.zip"),
        os.path.join(BASE_DIR, "road_shp_seoul.zip"),
        os.path.join(STRUCTURED_DATA_DIR, "road_review_package.zip"),
        os.path.join(BASE_DIR, "road_review_package.zip"),
    ]


def _road_zip_path() -> Optional[str]:
    """/health 등에서 참조 — 배포 패키지에 도로 SHP 번들 zip이 실제로 존재하는지 확인.

    FIX(r6-fix2): 이 함수가 정의되지 않은 채 /health 핸들러에서
    ``bool(_road_zip_path())``로 호출되고 있어 NameError로 /health가 500을 반환했다.
    Render의 healthCheckPath가 /health이므로 배포 자체가 불안정해질 수 있었다.
    """
    return next((p for p in _road_zip_candidates() if os.path.isfile(p) and os.path.getsize(p) > 0), None)


@lru_cache(maxsize=1)
def _road_shape_zip_cache_dir() -> Optional[str]:
    """평탄화 배포에서도 도로 FACT 압축파일을 자동 사용한다.

    우선순위는 ``road_shp_seoul.zip``(RW+MANAGE 최소패키지)이고,
    기존 검토패키지 ``road_review_package.zip``도 하위호환으로 읽는다.
    전체 ZIP을 풀지 않고 필요한 6개 SHP 구성파일만 /tmp에 1회 추출한다.
    """
    archive = _road_zip_path()
    if not archive:
        return None
    cache_dir = os.path.join("/tmp", "urban_strategy_road_shp_seoul")
    os.makedirs(cache_dir, exist_ok=True)
    if _road_shape_dir_complete(cache_dir):
        return cache_dir
    try:
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
            for required in ROAD_SHAPE_REQUIRED:
                member = next((n for n in names if n.replace('\\','/').endswith('/' + required) or n.replace('\\','/') == required), None)
                if not member:
                    logging.warning("road zip missing %s in %s", required, archive)
                    return None
                target = os.path.join(cache_dir, required)
                with zf.open(member) as src, open(target, 'wb') as dst:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        dst.write(chunk)
    except Exception as exc:
        logging.warning("road zip extract failed %s: %s", archive, exc)
        return None
    return cache_dir if _road_shape_dir_complete(cache_dir) else None


def _road_shape_dir() -> str:
    """서울 도로도형 원본 SHP(RW/MANAGE) 설치 경로.

    road_review_package DATA_SUMMARY 기준 원좌표계는 GRS80 Unified CS
    (central meridian 127.5, false easting 1,000,000 / northing 2,000,000),
    즉 EPSG:5179로 읽는다. 법적 기준값이 아니라 원자료 CRS 정의다.
    """
    structured = os.path.join(STRUCTURED_DATA_DIR, "road_shp_seoul")
    if _road_shape_dir_complete(structured):
        return structured
    flat = os.path.join(BASE_DIR, "road_shp_seoul")
    if _road_shape_dir_complete(flat):
        return flat
    cached = _road_shape_zip_cache_dir()
    return cached or flat

def _road_shape_base(stem: str) -> Optional[str]:
    base = os.path.join(_road_shape_dir(), stem)
    required = [base + ext for ext in (".shp", ".shx", ".dbf")]
    return base if all(os.path.isfile(p) and os.path.getsize(p) > 0 for p in required) else None

def _road_shape_records(stem: str, bbox_metric: List[float]) -> List[Dict[str, Any]]:
    """SHP 전체를 메모리에 올리지 않고 pyshp bbox 필터로 주변 레코드만 읽는다."""
    base = _road_shape_base(stem)
    if not base:
        return []
    reader = shapefile.Reader(base, encoding="cp949", encodingErrors="replace")
    fields = [f[0] for f in reader.fields[1:]]
    rows: List[Dict[str, Any]] = []
    polygon_layer = stem.upper() == "TL_SPRD_RW"
    for sr in reader.iterShapeRecords(bbox=bbox_metric):
        try:
            props = {k: _json_property(v) for k, v in zip(fields, list(sr.record))}
            geom = shape(sr.shape.__geo_interface__)
            if geom is None or geom.is_empty:
                continue
            quality = "valid"
            if not geom.is_valid:
                repaired = geom.buffer(0)
                if repaired is None or repaired.is_empty or not repaired.is_valid:
                    quality = "invalid_unrepaired"
                else:
                    geom = repaired
                    quality = "repaired_buffer0"
            if polygon_layer:
                geom = _polygonal_only(geom)
                if geom is None or geom.is_empty:
                    continue
            rows.append({"geometry": geom, "properties": props, "geometry_quality": quality})
        except Exception as exc:
            logging.debug("road SHP row skipped %s: %s", stem, exc)
            continue
    return rows

def _road_name(properties: Dict[str, Any]) -> str:
    by_upper = {str(k).upper(): v for k, v in (properties or {}).items()}
    return str(by_upper.get("RN") or by_upper.get("ROAD_NM") or by_upper.get("ROAD_NAME") or "").strip()

def analyze_local_road_facts(geometry: Dict[str, Any], radius_m: float = 220.0) -> Dict[str, Any]:
    """TL_SPRD_RW 실제 도로면 + TL_SPRD_MANAGE ROAD_BT를 대상지 주변에서만 결합한다.

    - RW와 MANAGE는 다대다 관계를 그대로 보존한다.
    - RW 하나에 대표 폭원을 강제로 부여하지 않는다.
    - MANAGE별 ROAD_BT buffer는 도로형상을 새로 만드는 용도가 아니라,
      해당 중심선에 대응하는 RW 실제 도로면 부분을 골라내는 association mask로만 쓴다.
    - nearest 거리 보조는 운영 threshold가 확정되지 않았으므로 사용하지 않는다.
      intersects 매칭이 0건이면 REVIEW 근거로 남긴다.
    """
    rw_base = _road_shape_base("TL_SPRD_RW")
    manage_base = _road_shape_base("TL_SPRD_MANAGE")
    if not rw_base or not manage_base:
        return {
            "status": "unavailable",
            "rw_features": [], "manage_features": [], "surface_features": [], "match_links": [],
            "metadata": {"reason": "TL_SPRD_RW/TL_SPRD_MANAGE 원본 SHP 미설치"},
        }
    try:
        site_wgs = _polygonal_only(shape(geometry))
    except Exception as exc:
        raise ValueError(f"구역계 GeoJSON을 읽을 수 없습니다: {exc}") from exc
    if site_wgs is None or site_wgs.is_empty:
        raise ValueError("구역계는 Polygon 또는 MultiPolygon이어야 합니다.")
    if not site_wgs.is_valid:
        site_wgs = _polygonal_only(site_wgs.buffer(0))
    if site_wgs is None or site_wgs.is_empty:
        raise ValueError("유효하지 않은 구역계입니다.")

    # DATA_SUMMARY의 GRS80 Unified CS 정의.
    to_metric = Transformer.from_crs(4326, 5179, always_xy=True).transform
    to_wgs = Transformer.from_crs(5179, 4326, always_xy=True).transform
    site_metric = geometry_transform(to_metric, site_wgs)
    frame_metric = site_metric.buffer(float(radius_m)).envelope
    bbox_metric = list(frame_metric.bounds)

    rw_rows = _road_shape_records("TL_SPRD_RW", bbox_metric)
    manage_rows = _road_shape_records("TL_SPRD_MANAGE", bbox_metric)

    # 검색 bbox로 실제 geometry도 클립하여 큰 교차로/광장형 원도형이 응답을 비대하게 만들지 않게 한다.
    local_rw: List[Dict[str, Any]] = []
    for row in rw_rows:
        try:
            gm = row["geometry"].intersection(search_metric)
            gm = _polygonal_only(gm)
            if gm is None or gm.is_empty:
                continue
            local_rw.append({**row, "geometry": gm})
        except Exception:
            continue
    local_manage: List[Dict[str, Any]] = []
    for row in manage_rows:
        try:
            gm = row["geometry"].intersection(frame_metric)
            if gm is None or gm.is_empty:
                continue
            local_manage.append({**row, "geometry": gm})
        except Exception:
            continue

    manage_geoms = [r["geometry"] for r in local_manage]
    manage_tree = STRtree(manage_geoms) if manage_geoms else None
    manage_to_rw: Dict[int, List[Any]] = {i: [] for i in range(len(local_manage))}
    links: List[Dict[str, Any]] = []
    rw_features: List[Dict[str, Any]] = []

    for rw_idx, row in enumerate(local_rw):
        rw_geom = row["geometry"]
        props = dict(row.get("properties") or {})
        rw_sn = props.get("RW_SN")
        matched: List[tuple[int, float]] = []
        if manage_tree is not None:
            try:
                for raw in manage_tree.query(rw_geom, predicate="intersects"):
                    mi = int(raw)
                    mg = manage_geoms[mi]
                    try:
                        overlap_len = float(mg.intersection(rw_geom).length)
                    except Exception:
                        overlap_len = 0.0
                    matched.append((mi, overlap_len))
                    manage_to_rw.setdefault(mi, []).append(rw_sn)
            except Exception:
                matched = []
        matched.sort(key=lambda x: x[1], reverse=True)
        width_values: List[float] = []
        road_names: List[str] = []
        manage_ids: List[Any] = []
        for mi, overlap_len in matched:
            mp = local_manage[mi].get("properties") or {}
            mid = mp.get("RDS_MAN_NO")
            width = _road_width_m(mp)
            name = _road_name(mp)
            manage_ids.append(mid)
            if width is not None and width not in width_values:
                width_values.append(width)
            if name and name not in road_names:
                road_names.append(name)
            links.append({
                "rw_sn": rw_sn, "rds_man_no": mid,
                "road_bt": width, "road_name": name,
                "overlap_length_m": round(overlap_len, 3),
                "match_method": "intersects",
            })
        review_reasons: List[str] = []
        if not matched:
            review_reasons.append("MANAGE intersects 매칭 0건")
        if len(width_values) > 1:
            review_reasons.append("ROAD_BT 복수값 · 구간별 사용 필요")
        if row.get("geometry_quality") != "valid":
            review_reasons.append(f"RW geometry {row.get('geometry_quality')}")
        out_props = dict(props)
        out_props.update({
            "_road_fact_source": "TL_SPRD_RW+TL_SPRD_MANAGE",
            "_matched_manage_ids": manage_ids,
            "_road_bt_values": width_values,
            "_road_bt_min": min(width_values) if width_values else None,
            "_road_bt_max": max(width_values) if width_values else None,
            "_road_name_candidates": road_names,
            "_match_method": "intersects" if matched else "unmatched",
            "_geometry_quality": row.get("geometry_quality"),
            "_review_reason": " / ".join(review_reasons) if review_reasons else None,
        })
        rw_features.append({"type": "Feature", "geometry": mapping(geometry_transform(to_wgs, rw_geom)), "properties": out_props})

    manage_features: List[Dict[str, Any]] = []
    surface_features: List[Dict[str, Any]] = []
    for mi, row in enumerate(local_manage):
        props = dict(row.get("properties") or {})
        mg = row["geometry"]
        width = _road_width_m(props)
        matched_rw = [x for x in manage_to_rw.get(mi, []) if x is not None]
        out_props = dict(props)
        out_props.update({
            "_road_fact_source": "TL_SPRD_RW+TL_SPRD_MANAGE",
            "_matched_rw_ids": matched_rw,
            "_rw_match_count": len(matched_rw),
            "_road_bt_m": width,
            "_geometry_quality": row.get("geometry_quality"),
            "_review_reason": None if matched_rw else "RW intersects 매칭 0건",
        })
        manage_features.append({"type": "Feature", "geometry": mapping(geometry_transform(to_wgs, mg)), "properties": out_props})

        # 폭원이 확인된 MANAGE별로, 그 선과 실제로 교차한 RW 도로면 중 해당 선 주변 부분만 남긴다.
        # ROAD_BT/2 buffer는 shape approximation이 아니라 RW-MANAGE association mask다.
        if width is None or width <= 0 or not matched_rw:
            continue
        try:
            association_mask = mg.buffer(width / 2.0, cap_style=2, join_style=2)
        except Exception:
            continue
        for rw_idx, rw_row in enumerate(local_rw):
            rw_sn = (rw_row.get("properties") or {}).get("RW_SN")
            if rw_sn not in matched_rw:
                continue
            try:
                surf = _polygonal_only(rw_row["geometry"].intersection(association_mask))
                if surf is None or surf.is_empty:
                    continue
                sp = dict(props)
                sp.update({
                    "RW_SN": rw_sn,
                    "_road_fact_source": "TL_SPRD_RW∩MANAGE association",
                    "_road_bt_m": width,
                    "_surface_method": "RW actual surface clipped by MANAGE ROAD_BT association mask",
                })
                surface_features.append({"type": "Feature", "geometry": mapping(geometry_transform(to_wgs, surf)), "properties": sp})
            except Exception:
                continue

    invalid_rw = sum(1 for r in local_rw if r.get("geometry_quality") != "valid")
    unmatched_rw = sum(1 for f in rw_features if not (f.get("properties") or {}).get("_matched_manage_ids"))
    mixed_rw = sum(1 for f in rw_features if len((f.get("properties") or {}).get("_road_bt_values") or []) > 1)
    return {
        "status": "resolved" if manage_features or rw_features else "none",
        "rw_features": rw_features,
        "manage_features": manage_features,
        "surface_features": surface_features,
        "match_links": links,
        "metadata": {
            "method": "local_bbox_rw_manage_many_to_many",
            "source_crs": "EPSG:5179",
            "radius_m": float(radius_m),
            "rw_count": len(rw_features),
            "manage_count": len(manage_features),
            "surface_count": len(surface_features),
            "match_link_count": len(links),
            "rw_unmatched_count": unmatched_rw,
            "rw_mixed_width_count": mixed_rw,
            "rw_repaired_or_invalid_count": invalid_rw,
            "nearest_fallback_used": False,
            "note": "RW 실제 도로면과 MANAGE ROAD_BT는 다대다로 보존하며 대표 폭원으로 강제 축약하지 않음",
        },
    }



# -----------------------------------------------------------------------------
# R18 pipeline stabilization: TL_SPRD_MANAGE ROAD_BT is the sole road-width Fact.
# The legacy TL_SPRD_RW dependency is intentionally removed.  A buffered surface
# derived from each MANAGE centerline + ROAD_BT is used only as a computational
# separator/contact surface; it is not asserted to be an official road polygon.
# -----------------------------------------------------------------------------
ROAD_SHAPE_REQUIRED = tuple(f"TL_SPRD_MANAGE{ext}" for ext in (".shp", ".shx", ".dbf"))


def _road_shape_base(stem: str) -> Optional[str]:
    """Find a local road SHP base. R18 accepts flat-root MANAGE files as well as bundles."""
    candidates = [
        os.path.join(STRUCTURED_DATA_DIR, "road_shp_seoul", stem),
        os.path.join(BASE_DIR, "road_shp_seoul", stem),
        os.path.join(STRUCTURED_DATA_DIR, stem),
        os.path.join(BASE_DIR, stem),
    ]
    for base in candidates:
        required = [base + ext for ext in (".shp", ".shx", ".dbf")]
        if all(os.path.isfile(q) and os.path.getsize(q) > 0 for q in required):
            return base
    cached = _road_shape_zip_cache_dir()
    if cached:
        base = os.path.join(cached, stem)
        required = [base + ext for ext in (".shp", ".shx", ".dbf")]
        if all(os.path.isfile(q) and os.path.getsize(q) > 0 for q in required):
            return base
    return None


def analyze_local_road_facts(geometry: Dict[str, Any], radius_m: float = 220.0) -> Dict[str, Any]:
    """Return local TL_SPRD_MANAGE centerlines and ROAD_BT-derived computational surfaces.

    No TL_SPRD_RW geometry is read or required.  Width-buffer surfaces are explicitly
    marked ESTIMATE and exist only to support frontage/street-block geometry operations.
    """
    manage_base = _road_shape_base("TL_SPRD_MANAGE")
    if not manage_base:
        return {
            "status": "unavailable",
            "rw_features": [], "manage_features": [], "surface_features": [], "match_links": [],
            "metadata": {"reason": "TL_SPRD_MANAGE 원본 SHP 미설치", "road_mode": "manage_only"},
        }
    try:
        site_wgs = _polygonal_only(shape(geometry))
    except Exception as exc:
        raise ValueError(f"구역계 GeoJSON을 읽을 수 없습니다: {exc}") from exc
    if site_wgs is None or site_wgs.is_empty:
        raise ValueError("구역계는 Polygon 또는 MultiPolygon이어야 합니다.")
    if not site_wgs.is_valid:
        site_wgs = _polygonal_only(site_wgs.buffer(0))
    if site_wgs is None or site_wgs.is_empty:
        raise ValueError("유효하지 않은 구역계입니다.")

    to_metric = Transformer.from_crs(4326, 5179, always_xy=True).transform
    to_wgs = Transformer.from_crs(5179, 4326, always_xy=True).transform
    site_metric = geometry_transform(to_metric, site_wgs)
    search_metric = site_metric.buffer(float(radius_m))
    frame_metric = search_metric.envelope
    rows = _road_shape_records("TL_SPRD_MANAGE", list(frame_metric.bounds))

    manage_features: List[Dict[str, Any]] = []
    surface_features: List[Dict[str, Any]] = []
    width_known = 0
    width_missing = 0
    repaired = 0
    for row in rows:
        try:
            gm = row["geometry"].intersection(frame_metric)
            if gm is None or gm.is_empty:
                continue
            props = dict(row.get("properties") or {})
            width = _road_width_m(props)
            if width is None:
                width_missing += 1
            else:
                width_known += 1
            if row.get("geometry_quality") != "valid":
                repaired += 1
            props.update({
                "_road_fact_source": "TL_SPRD_MANAGE ROAD_BT",
                "_road_bt_m": width,
                "_width_m": width,
                "_geometry_quality": row.get("geometry_quality"),
                "_review_reason": None if width is not None else "ROAD_BT 미확보",
                "_rw_match_count": 0,
            })
            manage_features.append({
                "type": "Feature",
                "geometry": mapping(geometry_transform(to_wgs, gm)),
                "properties": props,
            })
            if width is None or width <= 0:
                continue
            # Computational separator/contact surface only; not an official road-area polygon.
            surf = gm.buffer(width / 2.0, cap_style=2, join_style=2).intersection(search_metric)
            if surf is None or surf.is_empty:
                continue
            surf = _polygonal_only(surf)
            if surf is None or surf.is_empty:
                continue
            sprops = dict(props)
            sprops.update({
                "_road_surface_basis": "MANAGE_CENTERLINE_BUFFER_ROAD_BT",
                "_road_surface_estimate": True,
                "_scheme_surface_fallback": True,
            })
            surface_features.append({
                "type": "Feature",
                "geometry": mapping(geometry_transform(to_wgs, surf)),
                "properties": sprops,
            })
        except Exception as exc:
            logging.debug("MANAGE road row skipped: %s", exc)
            continue

    return {
        "status": "matched" if manage_features else "none",
        "rw_features": [],
        "manage_features": manage_features,
        "surface_features": surface_features,
        "match_links": [],
        "metadata": {
            "source": "TL_SPRD_MANAGE ROAD_BT",
            "road_mode": "manage_only_centerline_width",
            "search_radius_m": float(radius_m),
            "manage_count": len(manage_features),
            "surface_count": len(surface_features),
            "width_known_count": width_known,
            "width_missing_count": width_missing,
            "repaired_or_invalid_count": repaired,
            "rw_used": False,
            "note": "ROAD_BT 중심선 버퍼는 접도·가로구역 계산용 개략면이며 공식 도로구역/실폭도로면으로 확정하지 않음",
        },
    }


def _school_absolute_protection_zip_path() -> Optional[str]:
    """국가공간정보 연속주제도 UO101 서울 교육환경보호구역 원본.

    이번 안심주택 배제 FACT에는 MNUM의 UOA110(절대보호구역)만 사용한다.
    UOA120 상대보호구역과 UOA100 기타 구역은 자동판정에 섞지 않는다.
    """
    path = _data_path("school_protection_seoul_202608.zip")
    return path if os.path.isfile(path) and os.path.getsize(path) > 0 else None


@lru_cache(maxsize=1)
def _school_absolute_protection_layers() -> Dict[str, Any]:
    """UO101 중 UOA110 절대보호구역만 WGS84로 변환하고 STRtree로 1회 색인한다."""
    zip_path = _school_absolute_protection_zip_path()
    if not zip_path:
        return {"available": False, "reason": "school_protection_seoul_202608.zip 미설치"}
    rows: List[Dict[str, Any]] = []
    repaired = 0
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            stem = next((os.path.splitext(n)[0] for n in names if n.lower().endswith(".shp") and "UO101" in os.path.basename(n).upper()), None)
            if not stem:
                return {"available": False, "reason": "UO101 SHP를 ZIP에서 찾지 못함"}
            shp_name = next((n for n in names if os.path.splitext(n)[0] == stem and n.lower().endswith(".shp")), None)
            shx_name = next((n for n in names if os.path.splitext(n)[0] == stem and n.lower().endswith(".shx")), None)
            dbf_name = next((n for n in names if os.path.splitext(n)[0] == stem and n.lower().endswith(".dbf")), None)
            if not (shp_name and shx_name and dbf_name):
                return {"available": False, "reason": "UO101 SHP/SHX/DBF 구성 불완전"}
            source_crs = CRS.from_user_input("EPSG:5174")
            prj_name = next((n for n in names if os.path.splitext(n)[0] == stem and n.lower().endswith(".prj")), None)
            if prj_name:
                try:
                    source_crs = CRS.from_wkt(zf.read(prj_name).decode("utf-8", errors="ignore"))
                except Exception:
                    logging.warning("school absolute protection PRJ parse failed; EPSG:5174 fallback used")
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
                    mnum = str(props.get("MNUM") or "").upper()
                    if "UOA110" not in mnum:
                        continue
                    geom = shape(sr.shape.__geo_interface__)
                    if geom.is_empty:
                        continue
                    geometry_quality = "valid"
                    if not geom.is_valid:
                        geom = geom.buffer(0)
                        geometry_quality = "repaired_buffer0"
                        repaired += 1
                    if geom.is_empty:
                        continue
                    geom = geometry_transform(to_wgs, geom)
                    if geom.is_empty:
                        continue
                    props["_zone_type"] = "ABSOLUTE_PROTECTION_UOA110"
                    props["_geometry_quality"] = geometry_quality
                    rows.append({"geometry": geom, "properties": props})
                except Exception:
                    continue
        geoms = [r["geometry"] for r in rows]
        return {
            "available": bool(rows),
            "rows": rows,
            "tree": STRtree(geoms) if geoms else None,
            "source": "국가공간정보 연속주제도 UO101 · UOA110 절대보호구역",
            "file": os.path.basename(zip_path),
            "count": len(rows),
            "repaired_count": repaired,
        }
    except Exception as exc:
        logging.exception("school absolute protection SHP load failed")
        return {"available": False, "reason": str(exc)}


def analyze_school_absolute_protection_intersections(geometry: Dict[str, Any]) -> Dict[str, Any]:
    """안심주택 학교 출입문 50m 배제 FACT용 절대보호구역 공간중첩.

    UO101의 UOA110 원본 도형을 그대로 사용하며 별도의 50m 버퍼를 새로 생성하지 않는다.
    즉 이미 공시된 절대보호구역과 대상지의 실제 중첩만 계산한다.
    """
    layers = _school_absolute_protection_layers()
    if not layers.get("available"):
        raise FileNotFoundError(str(layers.get("reason") or "학교 절대보호구역 원본 미설치"))
    site = shape(geometry)
    if site.geom_type not in {"Polygon", "MultiPolygon"} or site.is_empty or not site.is_valid:
        raise ValueError("유효한 Polygon 또는 MultiPolygon 구역계가 필요합니다.")
    rows = layers.get("rows") or []
    tree = layers.get("tree")
    hit_features: List[Dict[str, Any]] = []
    overlap_features: List[Dict[str, Any]] = []
    overlap_geoms = []
    if tree is not None:
        for idx in tree.query(site, predicate="intersects"):
            row = rows[int(idx)]
            try:
                inter_parts = _polygonal_only(site.intersection(row["geometry"]))
            except Exception:
                inter_parts = []
            if not inter_parts:
                continue
            inter_geom = unary_union(inter_parts)
            if inter_geom.is_empty:
                continue
            props = dict(row.get("properties") or {})
            hit_features.append({"type": "Feature", "geometry": mapping(row["geometry"]), "properties": props})
            overlap_features.append({"type": "Feature", "geometry": mapping(inter_geom), "properties": props})
            overlap_geoms.append(inter_geom)
    union_wgs = unary_union(overlap_geoms) if overlap_geoms else None
    to_metric = Transformer.from_crs(4326, 5174, always_xy=True).transform
    site_m2 = float(geometry_transform(to_metric, site).area)
    overlap_m2 = float(geometry_transform(to_metric, union_wgs).area) if union_wgs is not None and not union_wgs.is_empty else 0.0
    overlap_pct = (overlap_m2 / site_m2 * 100.0) if site_m2 > 0 else None
    schools = []
    for f in hit_features:
        p = f.get("properties") or {}
        name = str(p.get("REMARK") or p.get("ALIAS") or "절대보호구역").strip()
        if name and name not in schools:
            schools.append(name)
    return {
        "status": "matched" if hit_features else "none",
        "fact_status": "EXCLUDED_AREA_PRESENT" if hit_features else "NO_OVERLAP",
        "known": True,
        "present": bool(hit_features),
        "overlap_area_m2": overlap_m2,
        "overlap_pct": overlap_pct,
        "zone_count": len(hit_features),
        "school_names": schools,
        "features": hit_features,
        "overlap_features": overlap_features,
        "source": layers.get("source"),
        "source_type": "OFFLINE_SHP_LSMD_CONT_UO101_UOA110",
        "file": layers.get("file"),
        "record_count": layers.get("count", 0),
        "repaired_count": layers.get("repaired_count", 0),
        "criterion": "학교 출입문으로부터 50m 이내 사업대상지 제외 · UO101 UOA110 절대보호구역 도형 사용",
        "note": "절대보호구역 원본 도형과 대상지의 중첩을 계산하며, 상대보호구역(UOA120)은 이 판정에 사용하지 않음",
    }



# -----------------------------------------------------------------------------
# R18: school absolute-protection analysis queries only the site's source-CRS bbox.
# The previous first request transformed/indexed every UOA110 feature in Seoul and
# could stall the UI for minutes on a small Render instance.
# -----------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _school_absolute_local_shape() -> Dict[str, Any]:
    zip_path = _school_absolute_protection_zip_path()
    if not zip_path:
        return {"available": False, "reason": "school_protection_seoul_202608.zip 미설치"}
    cache_dir = os.path.join("/tmp", "urban_strategy_school_uo101")
    os.makedirs(cache_dir, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            stem = next((os.path.splitext(n)[0] for n in names if n.lower().endswith(".shp") and "UO101" in os.path.basename(n).upper()), None)
            if not stem:
                return {"available": False, "reason": "UO101 SHP를 ZIP에서 찾지 못함"}
            base_name = os.path.basename(stem)
            out_base = os.path.join(cache_dir, base_name)
            for ext in (".shp", ".shx", ".dbf", ".prj"):
                member = next((n for n in names if os.path.splitext(n)[0] == stem and n.lower().endswith(ext)), None)
                if not member:
                    if ext == ".prj":
                        continue
                    return {"available": False, "reason": f"UO101 {ext} 구성파일 누락"}
                target = out_base + ext
                if not (os.path.isfile(target) and os.path.getsize(target) > 0):
                    with zf.open(member) as src, open(target, "wb") as dst:
                        while True:
                            chunk = src.read(1024 * 1024)
                            if not chunk:
                                break
                            dst.write(chunk)
            source_crs = CRS.from_user_input("EPSG:5174")
            prj = out_base + ".prj"
            if os.path.isfile(prj):
                try:
                    source_crs = CRS.from_wkt(Path(prj).read_text(encoding="utf-8", errors="ignore"))
                except Exception:
                    logging.warning("school local PRJ parse failed; EPSG:5174 fallback used")
            return {"available": True, "base": out_base, "source_crs": source_crs, "file": os.path.basename(zip_path)}
    except Exception as exc:
        logging.exception("school local SHP prepare failed")
        return {"available": False, "reason": str(exc)}


def analyze_school_absolute_protection_intersections(geometry: Dict[str, Any]) -> Dict[str, Any]:
    local = _school_absolute_local_shape()
    if not local.get("available"):
        raise FileNotFoundError(str(local.get("reason") or "학교 절대보호구역 원본 미설치"))
    site = shape(geometry)
    if site.geom_type not in {"Polygon", "MultiPolygon"} or site.is_empty:
        raise ValueError("유효한 Polygon 또는 MultiPolygon 구역계가 필요합니다.")
    if not site.is_valid:
        site = site.buffer(0)
    if site.is_empty or not site.is_valid:
        raise ValueError("유효한 Polygon 또는 MultiPolygon 구역계가 필요합니다.")

    source_crs = local["source_crs"]
    to_source = Transformer.from_crs(4326, source_crs, always_xy=True).transform
    to_wgs = Transformer.from_crs(source_crs, 4326, always_xy=True).transform
    site_source = geometry_transform(to_source, site)
    bbox = list(site_source.bounds)
    reader = shapefile.Reader(local["base"], encoding="cp949", encodingErrors="replace")
    fields = [f[0] for f in reader.fields[1:]]
    hit_features: List[Dict[str, Any]] = []
    overlap_features: List[Dict[str, Any]] = []
    overlap_geoms = []
    candidate_count = 0
    repaired = 0
    for sr in reader.iterShapeRecords(bbox=bbox):
        try:
            candidate_count += 1
            props = {k: _json_property(v) for k, v in zip(fields, list(sr.record))}
            if "UOA110" not in str(props.get("MNUM") or "").upper():
                continue
            geom_source = shape(sr.shape.__geo_interface__)
            if geom_source.is_empty:
                continue
            quality = "valid"
            if not geom_source.is_valid:
                geom_source = geom_source.buffer(0)
                quality = "repaired_buffer0"
                repaired += 1
            if geom_source.is_empty:
                continue
            geom = geometry_transform(to_wgs, geom_source)
            if geom.is_empty or not geom.intersects(site):
                continue
            inter_geom = _polygonal_only(site.intersection(geom))
            if inter_geom is None or inter_geom.is_empty:
                continue
            props["_zone_type"] = "ABSOLUTE_PROTECTION_UOA110"
            props["_geometry_quality"] = quality
            hit_features.append({"type": "Feature", "geometry": mapping(geom), "properties": props})
            overlap_features.append({"type": "Feature", "geometry": mapping(inter_geom), "properties": props})
            overlap_geoms.append(inter_geom)
        except Exception:
            continue

    union_wgs = unary_union(overlap_geoms) if overlap_geoms else None
    to_metric = Transformer.from_crs(4326, 5174, always_xy=True).transform
    site_m2 = float(geometry_transform(to_metric, site).area)
    overlap_m2 = float(geometry_transform(to_metric, union_wgs).area) if union_wgs is not None and not union_wgs.is_empty else 0.0
    overlap_pct = (overlap_m2 / site_m2 * 100.0) if site_m2 > 0 else None
    schools: List[str] = []
    for f in hit_features:
        pp = f.get("properties") or {}
        name = str(pp.get("REMARK") or pp.get("ALIAS") or "절대보호구역").strip()
        if name and name not in schools:
            schools.append(name)
    return {
        "status": "matched" if hit_features else "none",
        "fact_status": "EXCLUDED_AREA_PRESENT" if hit_features else "NO_OVERLAP",
        "known": True,
        "present": bool(hit_features),
        "overlap_area_m2": overlap_m2,
        "overlap_pct": overlap_pct,
        "zone_count": len(hit_features),
        "school_names": schools,
        "features": hit_features,
        "overlap_features": overlap_features,
        "source": "국가공간정보 연속주제도 UO101 · UOA110 절대보호구역",
        "source_type": "OFFLINE_SHP_LSMD_CONT_UO101_UOA110_BBOX",
        "file": local.get("file"),
        "record_count": None,
        "bbox_candidate_count": candidate_count,
        "repaired_count": repaired,
        "criterion": "학교 출입문으로부터 50m 이내 사업대상지 제외 · UO101 UOA110 절대보호구역 도형 사용",
        "note": "서울 전역을 사전 변환하지 않고 대상지 bbox 주변 UOA110 원도형만 조회·교차함",
    }


def analyze_school_protection_intersections(geometry: Dict[str, Any]) -> Dict[str, Any]:
    """UO101 절대(UOA110)·상대(UOA120) 보호구역을 규제정보용으로 함께 조회한다.

    기존 안심주택 UOA110 판정 함수와 endpoint는 변경하지 않는다.
    """
    local = _school_absolute_local_shape()
    if not local.get("available"):
        raise FileNotFoundError(str(local.get("reason") or "학교 보호구역 원본 미설치"))
    site = shape(geometry)
    if site.geom_type not in {"Polygon", "MultiPolygon"} or site.is_empty:
        raise ValueError("유효한 Polygon 또는 MultiPolygon 구역계가 필요합니다.")
    if not site.is_valid:
        site = site.buffer(0)
    if site.is_empty or not site.is_valid:
        raise ValueError("유효한 Polygon 또는 MultiPolygon 구역계가 필요합니다.")

    source_crs = local["source_crs"]
    to_source = Transformer.from_crs(4326, source_crs, always_xy=True).transform
    to_wgs = Transformer.from_crs(source_crs, 4326, always_xy=True).transform
    site_source = geometry_transform(to_source, site)
    reader = shapefile.Reader(local["base"], encoding="cp949", encodingErrors="replace")
    fields = [f[0] for f in reader.fields[1:]]
    buckets = {"absolute": [], "relative": []}
    overlap_buckets = {"absolute": [], "relative": []}
    overlap_geoms = {"absolute": [], "relative": []}
    candidate_count = 0
    for sr in reader.iterShapeRecords(bbox=list(site_source.bounds)):
        try:
            candidate_count += 1
            props = {k: _json_property(v) for k, v in zip(fields, list(sr.record))}
            mnum = str(props.get("MNUM") or "").upper()
            key = "absolute" if "UOA110" in mnum else ("relative" if "UOA120" in mnum else None)
            if not key:
                continue
            geom_source = shape(sr.shape.__geo_interface__)
            if geom_source.is_empty:
                continue
            if not geom_source.is_valid:
                geom_source = geom_source.buffer(0)
            if geom_source.is_empty:
                continue
            geom = geometry_transform(to_wgs, geom_source)
            if geom.is_empty or not geom.intersects(site):
                continue
            inter_geom = _polygonal_only(site.intersection(geom))
            if inter_geom is None or inter_geom.is_empty:
                continue
            p = dict(props)
            p["_zone_type"] = "ABSOLUTE_PROTECTION_UOA110" if key == "absolute" else "RELATIVE_PROTECTION_UOA120"
            buckets[key].append({"type": "Feature", "geometry": mapping(geom), "properties": p})
            overlap_buckets[key].append({"type": "Feature", "geometry": mapping(inter_geom), "properties": p})
            overlap_geoms[key].append(inter_geom)
        except Exception:
            continue

    to_metric = Transformer.from_crs(4326, 5174, always_xy=True).transform
    site_m2 = float(geometry_transform(to_metric, site).area)
    def pack(key: str) -> Dict[str, Any]:
        union_wgs = unary_union(overlap_geoms[key]) if overlap_geoms[key] else None
        area = float(geometry_transform(to_metric, union_wgs).area) if union_wgs is not None and not union_wgs.is_empty else 0.0
        names = []
        for f in buckets[key]:
            p = f.get("properties") or {}
            name = str(p.get("REMARK") or p.get("ALIAS") or ("절대보호구역" if key == "absolute" else "상대보호구역")).strip()
            if name and name not in names:
                names.append(name)
        return {
            "known": True, "present": bool(buckets[key]), "overlap_area_m2": area,
            "overlap_pct": (area / site_m2 * 100.0) if site_m2 > 0 else None,
            "zone_count": len(buckets[key]), "school_names": names,
            "features": buckets[key], "overlap_features": overlap_buckets[key],
        }
    absolute, relative = pack("absolute"), pack("relative")
    return {
        "status": "matched" if absolute["present"] or relative["present"] else "none",
        "known": True, "absolute": absolute, "relative": relative,
        "source": "국가공간정보 연속주제도 UO101 · UOA110/UOA120 학교 교육환경보호구역",
        "source_type": "OFFLINE_SHP_LSMD_CONT_UO101_BBOX",
        "file": local.get("file"), "bbox_candidate_count": candidate_count,
        "note": "규제정보 제공용 중첩 FACT이며 교육환경평가·학교 일조·소음·통학안전 등 별도 검토 필요 여부를 자동 확정하지 않음",
    }


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
    if site.geom_type not in {"Polygon", "MultiPolygon"} or site.is_empty:
        raise ValueError("유효한 Polygon 또는 MultiPolygon 구역계가 필요합니다.")
    if not site.is_valid:
        site = site.buffer(0)
    if site.is_empty or not site.is_valid or site.geom_type not in {"Polygon", "MultiPolygon"}:
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
    if site.geom_type not in {"Polygon", "MultiPolygon"} or site.is_empty:
        raise ValueError("유효한 Polygon 또는 MultiPolygon 구역계가 필요합니다.")
    if not site.is_valid:
        site = site.buffer(0)
    if site.is_empty or not site.is_valid or site.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError("유효한 Polygon 또는 MultiPolygon 구역계가 필요합니다.")
    public_result = _forest_class_intersection(site, layers, "public_interest_forest")
    forestry_result = _forest_class_intersection(site, layers, "forestry_forest")
    conservation_result = _forest_class_intersection(site, layers, "conservation_forest")
    return {
        "status": "matched" if public_result["intersects"] or forestry_result["intersects"] or conservation_result["intersects"] else "none",
        "public_interest_forest": public_result,
        "forestry_forest": forestry_result,
        "conservation_forest": conservation_result,
        "metadata": {
            "source": layers.get("source"),
            "file": layers.get("file"),
            "source_crs": layers.get("crs"),
            "dataset_counts": layers.get("counts"),
            "classification_basis": "MNUM UFM100=보전산지(상위분류), UFM120=공익용산지, UFM110=임업용산지 · 상위/하위 중복합산 금지",
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


def _shared_edge_barrier(
    shared: Any,
    road_union: Any,
    strong_union: Any = None,
    road_prepared: Any = None,
    strong_prepared: Any = None,
) -> tuple[bool, str, float]:
    """두 기초단위구의 공통경계가 도로/철도/하천에 의해 막히는지 판정한다.

    R22 성능개선: 고정된 union에 대한 반복 intersects predicate는 prepared geometry를
    사용한다. 실제 중첩면적은 기존 원본 geometry의 intersection()으로 계산하므로
    판정식·임계값·산정결과는 변경하지 않는다.
    """
    try:
        length = float(shared.length)
        if length < 1.0:
            return False, 'point_or_short_touch', 0.0
        corridor = shared.buffer(1.25, cap_style=2, join_style=2)
        if corridor.is_empty or corridor.area <= 0:
            return False, 'empty_corridor', 0.0
        road_ratio = 0.0
        if road_union is not None and not road_union.is_empty:
            try:
                road_hit = bool(road_prepared.intersects(corridor)) if road_prepared is not None else bool(corridor.intersects(road_union))
            except Exception:
                road_hit = bool(corridor.intersects(road_union))
            if road_hit:
                road_ratio = float(corridor.intersection(road_union).area) / float(corridor.area)
                if road_ratio >= 0.22:
                    return True, 'road4m', road_ratio
        if strong_union is not None and not strong_union.is_empty:
            try:
                strong_hit = bool(strong_prepared.intersects(corridor)) if strong_prepared is not None else bool(corridor.intersects(strong_union))
            except Exception:
                strong_hit = bool(corridor.intersects(strong_union))
            if strong_hit:
                strong_ratio = float(corridor.intersection(strong_union).area) / float(corridor.area)
                if strong_ratio >= 0.12:
                    return True, 'rail_or_river', strong_ratio
        return False, 'mergeable', road_ratio
    except Exception:
        return False, 'geometry_error', 0.0


def _basic_unit_component(
    start_idx: int, geoms: List[Any], tree: Any, road_union: Any, strong_union: Any,
    max_units: int = 240, road_prepared: Any = None, strong_prepared: Any = None,
) -> tuple[set[int], bool]:
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
            blocked, _, _ = _shared_edge_barrier(
                shared, road_union, strong_union, road_prepared, strong_prepared
            )
            if blocked:
                continue
            selected.add(j)
            queue.append(j)
            if len(selected) >= max_units:
                hit_limit = True
                return selected, hit_limit
    return selected, hit_limit




def _ensure_basic_unit_neighbors(
    idx: int,
    geoms: List[Any],
    tree: Any,
    neighbor_order: List[Optional[List[int]]],
    shared_edges: Dict[tuple[int, int], Any],
    invalid_edges: set[tuple[int, int]],
    topology_stats: Optional[Dict[str, Any]] = None,
) -> List[int]:
    """R23: 방문한 기초단위구의 인접쌍만 lazy 계산하고 이후 모든 seed/pass/Rule에서 재사용한다."""
    if idx < 0 or idx >= len(neighbor_order):
        return []
    cached = neighbor_order[idx]
    if cached is not None:
        if topology_stats is not None:
            topology_stats['cache_hits'] = int(topology_stats.get('cache_hits', 0)) + 1
        return cached

    started = time.perf_counter()
    order: List[int] = []
    try:
        candidates = tree.query(geoms[idx].buffer(0.8), predicate='intersects')
    except Exception:
        candidates = []
    if topology_stats is not None:
        topology_stats['query_count'] = int(topology_stats.get('query_count', 0)) + 1

    for raw in candidates:
        j = int(raw)
        if j == idx:
            continue
        key = (idx, j) if idx < j else (j, idx)
        if key in invalid_edges:
            continue
        shared = shared_edges.get(key)
        if shared is None:
            if topology_stats is not None:
                topology_stats['shared_calc_count'] = int(topology_stats.get('shared_calc_count', 0)) + 1
            try:
                shared = geoms[idx].boundary.intersection(geoms[j].boundary)
                if shared.is_empty or float(shared.length) < 1.0:
                    invalid_edges.add(key)
                    continue
                shared_edges[key] = shared
            except Exception:
                invalid_edges.add(key)
                continue
        # 기존 tree.query 순서를 보존하여 max_units 조기종료 방문순서 변화 위험을 최소화한다.
        order.append(j)

    neighbor_order[idx] = order
    if topology_stats is not None:
        topology_stats['topology_ms'] = round(
            float(topology_stats.get('topology_ms', 0.0)) + (time.perf_counter()-started)*1000.0, 3
        )
        topology_stats['materialized_node_count'] = int(topology_stats.get('materialized_node_count', 0)) + 1
    return order


def _build_basic_unit_neighbor_topology(geoms: List[Any], tree: Any) -> Dict[str, Any]:
    """검증/진단용 full builder. 실제 R23 엔진은 _ensure_basic_unit_neighbors()의 lazy cache를 사용한다."""
    neighbor_order: List[Optional[List[int]]] = [None for _ in geoms]
    shared_edges: Dict[tuple[int, int], Any] = {}
    invalid_edges: set[tuple[int, int]] = set()
    stats: Dict[str, Any] = {'query_count':0,'shared_calc_count':0,'cache_hits':0,'topology_ms':0.0,'materialized_node_count':0}
    for i in range(len(geoms)):
        _ensure_basic_unit_neighbors(i, geoms, tree, neighbor_order, shared_edges, invalid_edges, stats)
    return {
        'neighbor_order': [x or [] for x in neighbor_order],
        'shared_edges': shared_edges,
        'invalid_edges': invalid_edges,
        'query_count': stats['query_count'],
        'edge_count': len(shared_edges),
        'shared_calc_count': stats['shared_calc_count'],
        'topology_ms': stats['topology_ms'],
    }


def _basic_unit_component_graph(
    start_idx: int,
    neighbor_order: List[Optional[List[int]]],
    shared_edges: Dict[tuple[int, int], Any],
    road_union: Any,
    strong_union: Any,
    max_units: int = 240,
    road_prepared: Any = None,
    strong_prepared: Any = None,
    barrier_cache: Optional[Dict[tuple[int, int], tuple[bool, str, float]]] = None,
    stats: Optional[Dict[str, int]] = None,
    geoms: Optional[List[Any]] = None,
    tree: Any = None,
    invalid_edges: Optional[set[tuple[int, int]]] = None,
    topology_stats: Optional[Dict[str, Any]] = None,
) -> tuple[set[int], bool]:
    """R23: lazy neighbor topology + component-pass 전용 barrier cache를 사용하는 BFS."""
    cache = barrier_cache if barrier_cache is not None else {}
    invalid = invalid_edges if invalid_edges is not None else set()
    selected = {int(start_idx)}
    queue = [int(start_idx)]
    hit_limit = False
    while queue:
        idx = queue.pop(0)
        if idx < 0 or idx >= len(neighbor_order):
            continue
        neighbors = neighbor_order[idx]
        if neighbors is None:
            if geoms is None or tree is None:
                neighbors = []
                neighbor_order[idx] = neighbors
            else:
                neighbors = _ensure_basic_unit_neighbors(
                    idx, geoms, tree, neighbor_order, shared_edges, invalid, topology_stats
                )
        elif topology_stats is not None:
            topology_stats['cache_hits'] = int(topology_stats.get('cache_hits', 0)) + 1
        for j in neighbors:
            if j == idx or j in selected:
                continue
            key = (idx, j) if idx < j else (j, idx)
            result = cache.get(key)
            if result is None:
                shared = shared_edges.get(key)
                if shared is None:
                    continue
                result = _shared_edge_barrier(
                    shared, road_union, strong_union, road_prepared, strong_prepared
                )
                cache[key] = result
                if stats is not None:
                    stats['barrier_evaluations'] = int(stats.get('barrier_evaluations', 0)) + 1
            elif stats is not None:
                stats['barrier_cache_hits'] = int(stats.get('barrier_cache_hits', 0)) + 1
            if result[0]:
                continue
            selected.add(j)
            queue.append(j)
            if len(selected) >= max_units:
                hit_limit = True
                return selected, hit_limit
    return selected, hit_limit


def _street_block_common_context(
    geometry: Dict[str, Any],
    road_features: Optional[List[Dict[str, Any]]] = None,
    max_radius_m: float = 500.0,
    road_surface_features: Optional[List[Dict[str, Any]]] = None,
    road_area_features: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """제도별 가로구역이 공통으로 쓰는 공간 FACT를 한 번만 준비한다.

    R14 성능개선: 대상지/기초단위구/도로 FACT의 파싱·좌표변환과 로컬 STRtree 생성을
    제도별 요청마다 반복하지 않는다. 여기서는 Rule을 적용하지 않고 공통 context만 만든다.
    """
    started = time.perf_counter()
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
            'early_result': {
                'status':'unresolved','block':None,'blocks':{'type':'FeatureCollection','features':[]},
                'road_barriers':{'type':'FeatureCollection','features':[]},'road_context':{'type':'FeatureCollection','features':[]},
                'facility_barriers':{'type':'FeatureCollection','features':[]},'basic_unit_context':{'type':'FeatureCollection','features':[]},
                'metadata':{'method':'sgis_basic_unit_roadbt_merge','reason':'대상지 주변 기초단위구 없음','basic_unit_file':units.get('file')}
            },
            'common_prep_ms': round((time.perf_counter()-started)*1000.0, 2),
        }
    if not road_features:
        return {
            'early_result': {
                'status':'unavailable','block':None,'blocks':{'type':'FeatureCollection','features':[]},
                'road_barriers':{'type':'FeatureCollection','features':[]},'road_context':{'type':'FeatureCollection','features':[]},
                'facility_barriers':{'type':'FeatureCollection','features':[]},'basic_unit_context':{'type':'FeatureCollection','features':[]},
                'metadata':{'method':'sgis_basic_unit_roadbt_merge','reason':'TL_SPRD_MANAGE ROAD_BT 도로자료 미확보','basic_unit_file':units.get('file')}
            },
            'common_prep_ms': round((time.perf_counter()-started)*1000.0, 2),
        }

    selected_items: List[Dict[str, Any]] = []
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
        except Exception:
            continue

    # 실폭도로면은 threshold 적용 전에 전부 1회 좌표변환한다. 4m/6m/전체도로 Rule은 apply 단계에서 분기한다.
    surface_items: List[Dict[str, Any]] = []
    for feat in road_surface_features or []:
        props = (feat or {}).get('properties') or {}
        width = _road_width_m(props)
        try:
            geom = _polygonal_only(shape((feat or {}).get('geometry') or {}))
            if geom is None or geom.is_empty:
                continue
            gm = _polygonal_only(geometry_transform(to_metric, geom))
            if gm is None or gm.is_empty or not gm.intersects(frame_metric):
                continue
            surface_items.append({'feature':feat,'metric':gm,'width':width})
        except Exception:
            continue

    # 폭원과 무관한 외곽 폐합도로 후보도 1회만 파싱한다.
    outer_candidate_metric: List[Any] = []
    for feat in road_area_features or []:
        try:
            geom = _polygonal_only(shape((feat or {}).get('geometry') or {}))
            if geom is None or geom.is_empty:
                continue
            gm = _polygonal_only(geometry_transform(to_metric, geom))
            if gm is None or gm.is_empty or not gm.intersects(frame_metric):
                continue
            try:
                overlap_area = float(gm.intersection(site_metric).area)
            except Exception:
                overlap_area = 0.0
            if overlap_area <= 1.0:
                outer_candidate_metric.append(gm)
        except Exception:
            continue

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
            'early_result': {
                'status':'unresolved','block':None,'blocks':{'type':'FeatureCollection','features':[]},
                'road_barriers':{'type':'FeatureCollection','features':[]},'road_context':{'type':'FeatureCollection','features':[]},
                'facility_barriers':{'type':'FeatureCollection','features':[]},
                'basic_unit_context':{'type':'FeatureCollection','features':[]},
                'metadata':{'method':'sgis_basic_unit_roadbt_merge','reason':'대상지와 중첩되는 기초단위구 없음','basic_unit_file':units.get('file')}
            },
            'common_prep_ms': round((time.perf_counter()-started)*1000.0, 2),
        }

    # R23 lazy graph: 전체 750m를 선계산하지 않고 실제 flood-fill이 방문하는 node만 materialize한다.
    neighbor_order: List[Optional[List[int]]] = [None for _ in geoms]
    shared_edges: Dict[tuple[int, int], Any] = {}
    neighbor_invalid_edges: set[tuple[int, int]] = set()
    neighbor_topology_stats: Dict[str, Any] = {
        'query_count':0,'shared_calc_count':0,'cache_hits':0,'topology_ms':0.0,'materialized_node_count':0
    }

    return {
        'units': units,
        'to_metric': to_metric,
        'to_wgs': to_wgs,
        'site_metric': site_metric,
        'site_area': site_area,
        'frame_metric': frame_metric,
        'local_rows': local_rows,
        'selected_items': selected_items,
        'surface_items': surface_items,
        'outer_candidate_metric': outer_candidate_metric,
        'geoms': geoms,
        'tree': tree,
        'initial': initial,
        'neighbor_order': neighbor_order,
        'shared_edges': shared_edges,
        'neighbor_invalid_edges': neighbor_invalid_edges,
        'neighbor_topology_stats': neighbor_topology_stats,
        'max_radius_m': float(max_radius_m),
        'common_prep_ms': round((time.perf_counter()-started)*1000.0, 2),
    }


def _street_block_apply_rule(
    context: Optional[Dict[str, Any]],
    barrier_features: Optional[List[Dict[str, Any]]] = None,
    road_min_width_m: float = 4.0,
    outer_closure_all_roads: bool = False,
) -> Optional[Dict[str, Any]]:
    """공통 context에 제도별 가로구역 Rule만 적용한다. R13의 판정 알고리즘은 그대로 유지한다."""
    if context is None:
        return None
    if context.get('early_result') is not None:
        result = context['early_result']
        try:
            result = json.loads(json.dumps(result, ensure_ascii=False))
        except Exception:
            result = dict(result)
        if isinstance(result.get('metadata'), dict):
            result['metadata']['common_prep_ms'] = context.get('common_prep_ms')
        return result

    rule_started = time.perf_counter()
    units = context['units']
    to_metric = context['to_metric']
    to_wgs = context['to_wgs']
    site_metric = context['site_metric']
    site_area = context['site_area']
    frame_metric = context['frame_metric']
    local_rows = context['local_rows']
    selected_items = context['selected_items']
    surface_items = context['surface_items']
    geoms = context['geoms']
    tree = context['tree']
    initial = context['initial']
    neighbor_order = context.get('neighbor_order')
    shared_edges = context.get('shared_edges')
    neighbor_invalid_edges = context.get('neighbor_invalid_edges')
    neighbor_topology_stats = context.get('neighbor_topology_stats')
    max_radius_m = context['max_radius_m']

    road_min_width_m = max(0.0, float(road_min_width_m or 0.0))
    road_metric: List[Any] = []
    under4_count = sum(1 for item in selected_items if item['width'] is not None and item['width'] < road_min_width_m)
    unknown_width_count = sum(1 for item in selected_items if item['width'] is None)
    surface_used_count = 0
    outer_closure_count = 0

    for item in surface_items:
        width = item['width']
        if road_min_width_m > 0 and (width is None or width < road_min_width_m):
            continue
        road_metric.append(item['metric'])
        surface_used_count += 1

    # 성장잠재권의 '모든 도로 경계' 모드는 폭원 미상 중심선도 위상장벽으로 보완한다.
    if road_min_width_m <= 0:
        for item in selected_items:
            if item['width'] is None:
                try:
                    road_metric.append(item['metric'].buffer(1.0, cap_style=2, join_style=2))
                except Exception:
                    pass

    outer_candidate_metric = context['outer_candidate_metric'] if outer_closure_all_roads else []

    if not road_metric:
        for item in selected_items:
            width = item['width']
            if width is not None and width >= road_min_width_m:
                road_metric.append(item['metric'].buffer(1.0, cap_style=2, join_style=2))
    road_union = unary_union(road_metric).buffer(0) if road_metric else GeometryCollection()

    barrier_candidates: List[tuple[Dict[str, Any], Any]] = []
    for feat in barrier_features or []:
        try:
            gm = _polygonal_only(shape((feat or {}).get('geometry') or {}))
            if gm is None or gm.is_empty:
                continue
            mm = _polygonal_only(geometry_transform(to_metric, gm))
            if mm is not None and not mm.is_empty and mm.intersects(frame_metric):
                barrier_candidates.append((feat, mm))
        except Exception:
            continue
    strong_features: List[Dict[str, Any]] = []
    strong_metric: List[Any] = []
    strong_union = GeometryCollection()
    component_pass_count = 0
    component_seed_runs = 0
    component_pass_ms: List[float] = []
    prepared_build_ms: List[float] = []
    component_barrier_evaluations: List[int] = []
    component_barrier_cache_hits: List[int] = []

    def _components_for(road_barrier_union: Any) -> List[tuple[set[int], Any, float, bool]]:
        nonlocal component_pass_count, component_seed_runs
        pass_started = time.perf_counter()
        prep_started = time.perf_counter()
        road_prepared = None
        strong_prepared = None
        try:
            if road_barrier_union is not None and not road_barrier_union.is_empty:
                road_prepared = prep(road_barrier_union)
        except Exception:
            road_prepared = None
        try:
            if strong_union is not None and not strong_union.is_empty:
                strong_prepared = prep(strong_union)
        except Exception:
            strong_prepared = None
        prepared_build_ms.append(round((time.perf_counter()-prep_started)*1000.0, 3))
        component_pass_count += 1

        out: List[tuple[set[int], Any, float, bool]] = []
        seen_keys = set()
        barrier_cache: Dict[tuple[int, int], tuple[bool, str, float]] = {}
        barrier_stats = {'barrier_evaluations': 0, 'barrier_cache_hits': 0}
        use_graph = isinstance(neighbor_order, list) and isinstance(shared_edges, dict)
        for start in initial:
            component_seed_runs += 1
            if use_graph:
                comp, hit_limit = _basic_unit_component_graph(
                    start, neighbor_order, shared_edges, road_barrier_union, strong_union,
                    road_prepared=road_prepared, strong_prepared=strong_prepared,
                    barrier_cache=barrier_cache, stats=barrier_stats,
                    geoms=geoms, tree=tree, invalid_edges=neighbor_invalid_edges,
                    topology_stats=neighbor_topology_stats,
                )
            else:
                comp, hit_limit = _basic_unit_component(
                    start, geoms, tree, road_barrier_union, strong_union,
                    road_prepared=road_prepared, strong_prepared=strong_prepared,
                )
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
                out.append((comp, merged, ia, hit_limit))
        out.sort(key=lambda x: x[2], reverse=True)
        component_barrier_evaluations.append(int(barrier_stats['barrier_evaluations']))
        component_barrier_cache_hits.append(int(barrier_stats['barrier_cache_hits']))
        component_pass_ms.append(round((time.perf_counter()-pass_started)*1000.0, 2))
        return out

    components = _components_for(road_union)
    if not components:
        return None

    if barrier_candidates:
        provisional = components[0][1]
        barrier_base = road_union
        for feat, mm in barrier_candidates:
            effective, reason = _street_block_facility_effect(provisional, mm, frame_metric, barrier_base, site_metric)
            if effective:
                props = dict((feat or {}).get('properties') or {})
                props['_block_barrier_effect'] = reason
                strong_features.append({'type':'Feature','geometry':(feat or {}).get('geometry'),'properties':props})
                strong_metric.append(mm)
        if strong_metric:
            strong_union = unary_union(strong_metric).buffer(0.10, join_style=2)
            components = _components_for(road_union)
            if not components:
                return None

    if outer_closure_all_roads and outer_candidate_metric:
        provisional = components[0][1]
        try:
            outer_band = provisional.boundary.buffer(2.0, cap_style=2, join_style=2)
        except Exception:
            outer_band = None
        selected_outer: List[Any] = []
        if outer_band is not None and not outer_band.is_empty:
            for gm in outer_candidate_metric:
                try:
                    if gm.intersects(outer_band):
                        selected_outer.append(gm)
                except Exception:
                    continue
        if selected_outer:
            outer_closure_count = len(selected_outer)
            road_union = unary_union([road_union, *selected_outer]).buffer(0)
            components = _components_for(road_union)
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
    try:
        frame_boundary_touched = bool(not frame_metric.buffer(-5.0).contains(primary))
    except Exception:
        frame_boundary_touched = False
    status = 'resolved' if not primary_limit and not frame_boundary_touched else 'partial'
    block_features=[]
    for comp,g,ia,_ in significant[:12]:
        ba=float(g.area)
        block_features.append({'type':'Feature','geometry':mapping(geometry_transform(to_wgs,g)),'properties':{
            'site_intersection_m2':ia,'block_area_m2':ba,'site_share_of_block_pct':(ia/ba*100.0 if ba>0 else None),
            'block_coverage_of_site_pct':(ia/site_area*100.0 if site_area>0 else None),'merged_basic_units':len(comp)}})
    rule_ms = round((time.perf_counter()-rule_started)*1000.0, 2)
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
            'road_source':('TL_SPRD_RW actual surface + TL_SPRD_MANAGE ROAD_BT' if surface_used_count else 'VWorld TL_SPRD_MANAGE ROAD_BT'),'road_mode':('rw_surface_plus_centerline_width' if surface_used_count else 'centerline_width_attribute'),'road_min_width_m':road_min_width_m,
            'road_count':len(road_barriers),'road_surface_count':surface_used_count,'outer_closure_road_count':outer_closure_count,
            'road_context_below_threshold_count':len(road_context),'road_below_threshold_total_count':under4_count,
            'road_width_unknown_count':unknown_width_count,'strong_facility_count':len(strong_features),
            'block_area_m2':block_area,'site_intersection_m2':primary_site_area,
            'site_primary_block_pct':primary_site_pct,'site_share_of_primary_block_pct':primary_block_occupancy_pct,
            'site_spans_multiple_blocks':multi,'merge_limit_reached':primary_limit,'frame_boundary_touched':frame_boundary_touched,'analysis_radius_m':float(max_radius_m),'legal_width_rule':False,
            'road_min_width_m':road_min_width_m,'outer_closure_all_roads':bool(outer_closure_all_roads),
            'basic_unit_is_legal_street_block':False,'authoritative_street_block':False,
            'future_street_block_interface':'MOIS_BASIC_UNIT_OR_VERIFIED_PLANNING_ROAD_BLOCK',
            'engine_note':'현재 내장 기초단위구는 가로구역 후보 골격(ESTIMATE)이다. TL_SPRD_MANAGE ROAD_BT 폭원 근거와, 사용 가능할 때 TL_SPRD_RW 실제 도로면을 결합해 인접 기초단위구 병합 여부를 판단하며 법정 가로구역으로 자동확정하지 않는다. 향후 행안부 기초단위구/공식 가로구역 또는 검증된 도시계획시설도로 블록 자료가 연결되면 authoritative_street_block=true로 승격한다.',
            'common_prep_ms': context.get('common_prep_ms'), 'rule_apply_ms': rule_ms,
            'prepared_geometry': True, 'component_pass_count': component_pass_count,
            'component_seed_runs': component_seed_runs, 'component_pass_ms': component_pass_ms,
            'prepared_build_ms': prepared_build_ms,
            'component_barrier_evaluations': component_barrier_evaluations,
            'component_barrier_cache_hits': component_barrier_cache_hits,
            'neighbor_graph': True, 'neighbor_graph_mode':'lazy',
            'neighbor_edge_count': len(shared_edges or {}),
            'neighbor_query_count': (neighbor_topology_stats or {}).get('query_count'),
            'neighbor_shared_calc_count': (neighbor_topology_stats or {}).get('shared_calc_count'),
            'neighbor_topology_ms': (neighbor_topology_stats or {}).get('topology_ms'),
            'neighbor_materialized_node_count': (neighbor_topology_stats or {}).get('materialized_node_count'),
            'neighbor_cache_hits': (neighbor_topology_stats or {}).get('cache_hits'),
        }
    }


def _street_block_from_basic_units(
    geometry: Dict[str, Any],
    barrier_features: Optional[List[Dict[str, Any]]] = None,
    road_features: Optional[List[Dict[str, Any]]] = None,
    max_radius_m: float = 500.0,
    road_surface_features: Optional[List[Dict[str, Any]]] = None,
    road_area_features: Optional[List[Dict[str, Any]]] = None,
    road_min_width_m: float = 4.0,
    outer_closure_all_roads: bool = False,
) -> Optional[Dict[str, Any]]:
    """기존 단일 제도 API 호환 wrapper. 내부는 R14 공통 context + Rule 분리 엔진을 사용한다."""
    context = _street_block_common_context(
        geometry, road_features, max_radius_m, road_surface_features, road_area_features,
    )
    return _street_block_apply_rule(context, barrier_features, road_min_width_m, outer_closure_all_roads)

def analyze_street_block(
    geometry: Dict[str, Any],
    barrier_features: Optional[List[Dict[str, Any]]] = None,
    road_features: Optional[List[Dict[str, Any]]] = None,
    max_radius_m: float = 500.0,
    road_surface_features: Optional[List[Dict[str, Any]]] = None,
    road_area_features: Optional[List[Dict[str, Any]]] = None,
    road_min_width_m: float = 4.0,
    outer_closure_all_roads: bool = False,
) -> Dict[str, Any]:
    """기초단위구 seed + 독립 도로 FACT(TL_SPRD_RW + TL_SPRD_MANAGE ROAD_BT)를 사용한다.

    기초단위구가 없거나 자동확정에 실패하면 자료부족을 명시한다.
    폭원 근거는 TL_SPRD_MANAGE ROAD_BT이며, 실제 barrier 형상은 RW 실폭면 결합값을 우선한다.
    """
    basic = _street_block_from_basic_units(
        geometry, barrier_features, road_features, max_radius_m, road_surface_features,
        road_area_features, road_min_width_m, outer_closure_all_roads,
    )
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



def analyze_street_block_batch(
    geometry: Dict[str, Any],
    road_features: Optional[List[Dict[str, Any]]] = None,
    road_surface_features: Optional[List[Dict[str, Any]]] = None,
    road_area_features: Optional[List[Dict[str, Any]]] = None,
    schemes: Optional[Dict[str, Dict[str, Any]]] = None,
    max_radius_m: float = 500.0,
) -> Dict[str, Any]:
    """R14: 동일 대상지의 제도별 가로구역을 공통 공간전처리 1회 후 순차 Rule 적용한다.

    병렬화하지 않는다. Render 소형 인스턴스에서 대형 공간연산 동시실행을 피하면서
    제도별로 중복되던 좌표변환·로컬 STRtree·도로 geometry 파싱만 제거한다.
    """
    started = time.perf_counter()
    scheme_map = schemes or {}
    if not scheme_map:
        raise ValueError('가로구역 batch에는 최소 1개 제도 Rule이 필요합니다.')
    if len(scheme_map) > 6:
        raise ValueError('가로구역 batch는 최대 6개 제도까지 허용합니다.')
    context = _street_block_common_context(
        geometry, road_features, max_radius_m, road_surface_features, road_area_features,
    )
    prep_ms = None if context is None else context.get('common_prep_ms')
    results: Dict[str, Any] = {}
    rule_ms: Dict[str, float] = {}
    for key, rule in scheme_map.items():
        t0 = time.perf_counter()
        result = _street_block_apply_rule(
            context,
            (rule or {}).get('barrier_features') or [],
            float((rule or {}).get('road_min_width_m') or 0.0),
            bool((rule or {}).get('outer_closure_all_roads', False)),
        )
        if result is None:
            unit_layers = _basic_unit_spatial_layers()
            result = {
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
        elapsed = round((time.perf_counter()-t0)*1000.0, 2)
        if isinstance(result.get('metadata'), dict):
            result['metadata']['batch_rule_ms'] = elapsed
            result['metadata']['batch_shared_context'] = True
        results[str(key)] = result
        rule_ms[str(key)] = elapsed
    return {
        'status':'ok',
        'results':results,
        'shared_meta':{
            'analysis_radius_m':float(max_radius_m),
            'common_prep_ms':prep_ms,
            'rule_ms':rule_ms,
            'scheme_count':len(results),
            'batch_total_ms':round((time.perf_counter()-started)*1000.0, 2),
            'common_context_reused':True,
            'execution_mode':'sequential_rules_shared_context',
        },
    }

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
# 서울 구릉지 지형 FACT (R37)
# - 서울시 공식 1:5,000 등고선·표고점(2025-03-18 추출본)을 전처리하여
#   20m 지형격자(표고 + Horn 3x3 경사도)를 생성한 플랫폼 산출 참조자료.
# - 법정 '구릉지 원도형'을 사칭하지 않으며 source_type=PLATFORM_DERIVED_REFERENCE.
# - 표고 40m / 경사 10도는 공통 지형 FACT를 만들기 위한 참조 임계값이다.
#   사업별 PASS/FAIL은 각 사업의 최신 RULE에서 별도로 결정한다.
# ============================================================
HILL_GRID_META_FILE = "hill_terrain_20m_meta.json"
HILL_GRID_ELEV_FILE = "hill_elevation_20m_i16.zlib"
HILL_GRID_SLOPE_FILE = "hill_slope_20m_i16.zlib"
HILL_SOURCE_TITLE = "서울시 등고선·표고점(1:5,000) 기반 플랫폼 산출 구릉지 지형참조"
HILL_SOURCE_TYPE = "PLATFORM_DERIVED_REFERENCE"
HILL_REFERENCE_ELEVATION_M = 40.0
HILL_REFERENCE_SLOPE_DEG = 10.0
HILL_REFERENCE_CRITERION = "표고 40m 이상 AND 지형경사 10도 이상 (공통 FACT 참조임계값)"


def _hill_grid_paths() -> Dict[str, str]:
    return {
        "meta": _data_path(HILL_GRID_META_FILE),
        "elevation": _data_path(HILL_GRID_ELEV_FILE),
        "slope": _data_path(HILL_GRID_SLOPE_FILE),
    }


@lru_cache(maxsize=1)
def _hill_grid_meta() -> Dict[str, Any]:
    paths = _hill_grid_paths()
    if not all(os.path.isfile(paths[k]) for k in ("meta", "elevation", "slope")):
        missing = [os.path.basename(paths[k]) for k in ("meta", "elevation", "slope") if not os.path.isfile(paths[k])]
        return {
            "available": False,
            "status": "TERRAIN_REFERENCE_NOT_BUNDLED",
            "source_type": HILL_SOURCE_TYPE,
            "source_title": HILL_SOURCE_TITLE,
            "criterion": HILL_REFERENCE_CRITERION,
            "missing_files": missing,
            "note": "서울시 공식 등고선·표고점 기반 전처리 결과가 설치되지 않았습니다.",
        }
    with open(paths["meta"], encoding="utf-8") as fp:
        meta = json.load(fp)
    meta = dict(meta or {})
    meta.update({
        "available": True,
        "status": "TERRAIN_REFERENCE_READY",
        "source_type": HILL_SOURCE_TYPE,
        "source_title": HILL_SOURCE_TITLE,
        "criterion": HILL_REFERENCE_CRITERION,
        "source_file": meta.get("source_file") or "서울시 등고선.zip",
    })
    return meta


@lru_cache(maxsize=1)
def _hill_grid_arrays():
    meta = _hill_grid_meta()
    if not meta.get("available"):
        return None, None
    paths = _hill_grid_paths()
    expected = int(meta["width"]) * int(meta["height"])
    elev = array("h")
    elev.frombytes(zlib.decompress(Path(paths["elevation"]).read_bytes()))
    slope = array("h")
    slope.frombytes(zlib.decompress(Path(paths["slope"]).read_bytes()))
    if sys.byteorder != "little":
        elev.byteswap(); slope.byteswap()
    if len(elev) != expected or len(slope) != expected:
        raise RuntimeError(f"구릉지 지형격자 길이 불일치: expected={expected}, elev={len(elev)}, slope={len(slope)}")
    return elev, slope


def _hill_grid_cell(meta: Dict[str, Any], row: int, col: int) -> Polygon:
    res = float(meta["resolution_m"]); ox = float(meta["origin_x_west_edge"]); oy = float(meta["origin_y_north_edge"])
    x0 = ox + col * res; x1 = x0 + res
    y1 = oy - row * res; y0 = y1 - res
    return box(x0, y0, x1, y1)


def _hill_grid_window(meta: Dict[str, Any], geom_metric, extra_m: float = 0.0):
    g = geom_metric.buffer(extra_m) if extra_m > 0 else geom_metric
    minx, miny, maxx, maxy = g.bounds
    res = float(meta["resolution_m"]); ox = float(meta["origin_x_west_edge"]); oy = float(meta["origin_y_north_edge"])
    width = int(meta["width"]); height = int(meta["height"])
    c0 = max(0, int(math.floor((minx - ox) / res)))
    c1 = min(width - 1, int(math.floor((maxx - ox) / res)))
    r0 = max(0, int(math.floor((oy - maxy) / res)))
    r1 = min(height - 1, int(math.floor((oy - miny) / res)))
    return r0, r1, c0, c1


def _hill_grid_values(meta: Dict[str, Any], elev, slope, row: int, col: int):
    idx = row * int(meta["width"]) + col
    nodata = int(meta.get("nodata_i16", -32768))
    er = int(elev[idx]); sr = int(slope[idx])
    e = None if er == nodata else er / float((meta.get("scales") or {}).get("elevation_per_meter", 10.0))
    s = None if sr == nodata else sr / float((meta.get("scales") or {}).get("slope_per_degree", 100.0))
    return e, s


def _weighted_mean(pairs):
    den = sum(a for _, a in pairs)
    return (sum(v * a for v, a in pairs) / den) if den > 0 else None


def _weighted_median(pairs):
    if not pairs:
        return None
    ordered = sorted((float(v), float(a)) for v, a in pairs if a > 0)
    total = sum(a for _, a in ordered)
    acc = 0.0
    for v, a in ordered:
        acc += a
        if acc >= total * 0.5:
            return v
    return ordered[-1][0]


def _hill_feature_from_metric(geom_metric, category_code: str, category_label: str, to_wgs):
    if geom_metric is None or geom_metric.is_empty:
        return None
    try:
        g = geom_metric.buffer(0) if not geom_metric.is_valid else geom_metric
        g = g.simplify(1.0, preserve_topology=True)
        gw = geometry_transform(to_wgs, g)
    except Exception:
        return None
    return {"type":"Feature", "geometry":mapping(gw), "properties":{
        "category_code":category_code, "category":category_label,
        "source_type":HILL_SOURCE_TYPE, "source_title":HILL_SOURCE_TITLE,
        "criterion":HILL_REFERENCE_CRITERION,
    }}


def _hill_nearest_combined_distance(site_metric, meta, elev, slope, start_m: float = 500.0, max_m: float = 8000.0):
    radius = start_m
    best = None
    while radius <= max_m:
        r0, r1, c0, c1 = _hill_grid_window(meta, site_metric, radius)
        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                e, s = _hill_grid_values(meta, elev, slope, r, c)
                if e is None or s is None or e < HILL_REFERENCE_ELEVATION_M or s < HILL_REFERENCE_SLOPE_DEG:
                    continue
                cell = _hill_grid_cell(meta, r, c)
                d = float(site_metric.distance(cell))
                if d <= radius and (best is None or d < best):
                    best = d
                    if best <= 0:
                        return 0.0
        if best is not None:
            return best
        radius *= 2.0
    return None


def analyze_hill_intersections(geometry: Dict[str, Any]) -> Dict[str, Any]:
    try:
        site_wgs = _polygonal_only(shape(geometry))
    except Exception as exc:
        raise ValueError(f"구역계 GeoJSON을 읽을 수 없습니다: {exc}") from exc
    if site_wgs is None or site_wgs.is_empty:
        raise ValueError("구역계는 Polygon 또는 MultiPolygon이어야 합니다.")
    if not site_wgs.is_valid:
        site_wgs = _polygonal_only(site_wgs.buffer(0))
    if site_wgs is None or site_wgs.is_empty:
        raise ValueError("유효하지 않은 구역계입니다.")

    meta = dict(_hill_grid_meta() or {})
    if not meta.get("available"):
        return {
            "status":"unavailable", "source_status":meta.get("status") or "TERRAIN_REFERENCE_NOT_BUNDLED",
            "intersects":None, "overlap_area_m2":None, "overlap_pct":None, "distance_m":None,
            "elevation":{}, "slope":{}, "reference_areas":{},
            "overlaps":[], "context_features":[], "metadata":meta,
        }
    elev, slope = _hill_grid_arrays()
    if elev is None or slope is None:
        raise RuntimeError("구릉지 지형격자를 읽지 못했습니다.")

    to_metric = Transformer.from_crs(4326, int(str(meta.get("grid_crs","EPSG:5174")).split(":")[-1]), always_xy=True).transform
    to_wgs = Transformer.from_crs(int(str(meta.get("grid_crs","EPSG:5174")).split(":")[-1]), 4326, always_xy=True).transform
    site_metric = geometry_transform(to_metric, site_wgs)
    site_area = float(site_metric.area)
    r0, r1, c0, c1 = _hill_grid_window(meta, site_metric, 0)

    elev_pairs=[]; slope_pairs=[]
    area_e40=0.0; area_s10=0.0; area_combined=0.0; valid_area=0.0; nodata_area=0.0
    combined_parts=[]
    inspected_cells=0
    for r in range(r0, r1 + 1):
        for c in range(c0, c1 + 1):
            cell = _hill_grid_cell(meta, r, c)
            if not cell.intersects(site_metric):
                continue
            try:
                inter = cell.intersection(site_metric)
            except Exception:
                continue
            a = float(inter.area)
            if a <= 0.01:
                continue
            inspected_cells += 1
            e, s = _hill_grid_values(meta, elev, slope, r, c)
            if e is None:
                nodata_area += a
                continue
            valid_area += a
            elev_pairs.append((e, a))
            if s is not None:
                slope_pairs.append((s, a))
            if e >= HILL_REFERENCE_ELEVATION_M:
                area_e40 += a
            if s is not None and s >= HILL_REFERENCE_SLOPE_DEG:
                area_s10 += a
            if s is not None and e >= HILL_REFERENCE_ELEVATION_M and s >= HILL_REFERENCE_SLOPE_DEG:
                area_combined += a
                combined_parts.append(inter)

    e_vals=[v for v,_ in elev_pairs]; s_vals=[v for v,_ in slope_pairs]
    overlap_pct=(area_combined/site_area*100.0) if site_area>0 else None
    distance_m=0.0 if area_combined>0.5 else _hill_nearest_combined_distance(site_metric, meta, elev, slope)

    # 도면은 대상지 주변 400m 범위에서 세 FACT 레이어를 동적으로 벡터화한다.
    context_geom = site_metric.buffer(400.0)
    cr0, cr1, cc0, cc1 = _hill_grid_window(meta, context_geom, 0)
    elev_cells=[]; slope_cells=[]; combined_cells=[]
    for r in range(cr0, cr1 + 1):
        for c in range(cc0, cc1 + 1):
            e, s = _hill_grid_values(meta, elev, slope, r, c)
            if e is None and s is None:
                continue
            cell = _hill_grid_cell(meta, r, c)
            if not cell.intersects(context_geom):
                continue
            if e is not None and e >= HILL_REFERENCE_ELEVATION_M:
                elev_cells.append(cell)
            if s is not None and s >= HILL_REFERENCE_SLOPE_DEG:
                slope_cells.append(cell)
            if e is not None and s is not None and e >= HILL_REFERENCE_ELEVATION_M and s >= HILL_REFERENCE_SLOPE_DEG:
                combined_cells.append(cell)

    context_features=[]
    for cells, code, label in (
        (elev_cells,"elevation_ge_40m","표고 40m 이상"),
        (slope_cells,"slope_ge_10deg","지형경사 10° 이상"),
        (combined_cells,"combined_reference","표고40m + 경사10° 동시충족"),
    ):
        if not cells: continue
        try:
            geom = unary_union(cells).intersection(context_geom)
            feat = _hill_feature_from_metric(geom, code, label, to_wgs)
            if feat: context_features.append(feat)
        except Exception:
            continue

    overlaps=[]
    if combined_parts:
        try:
            feat=_hill_feature_from_metric(unary_union(combined_parts),"combined_overlap","대상지 내 동시충족 영역",to_wgs)
            if feat: overlaps.append(feat)
        except Exception:
            pass

    quality = "CONFIRMED" if valid_area >= site_area*0.98 else ("PARTIAL" if valid_area>0 else "NO_DATA")
    meta.update({
        "quality":quality,
        "reference_kind":"terrain_grid",
        "reference_note":"서울시 공식 등고선·표고점에서 플랫폼이 생성한 20m 지형격자이며 법정 구릉지 원도형이 아닙니다.",
    })
    return {
        "status":"confirmed" if quality=="CONFIRMED" else ("partial" if quality=="PARTIAL" else "unavailable"),
        "source_status":"TERRAIN_REFERENCE_READY",
        "intersects":area_combined>0.5,
        "overlap_area_m2":round(area_combined,2),
        "overlap_pct":round(overlap_pct,4) if overlap_pct is not None else None,
        "distance_m":round(distance_m,2) if distance_m is not None else None,
        "site_area_m2":round(site_area,2),
        "valid_terrain_area_m2":round(valid_area,2),
        "nodata_area_m2":round(nodata_area,2),
        "nodata_pct":round(nodata_area/site_area*100.0,4) if site_area>0 else None,
        "pixel_count_in_site":inspected_cells,
        "elevation":{
            "min_m":round(min(e_vals),2) if e_vals else None,
            "max_m":round(max(e_vals),2) if e_vals else None,
            "mean_m":round(_weighted_mean(elev_pairs),2) if elev_pairs else None,
            "representative_m":round(_weighted_median(elev_pairs),2) if elev_pairs else None,
        },
        "slope":{
            "min_degree":round(min(s_vals),2) if s_vals else None,
            "max_degree":round(max(s_vals),2) if s_vals else None,
            "mean_degree":round(_weighted_mean(slope_pairs),2) if slope_pairs else None,
        },
        "reference_areas":{
            "elevation_ge_40m_sqm":round(area_e40,2),
            "slope_ge_10deg_sqm":round(area_s10,2),
            "combined_sqm":round(area_combined,2),
        },
        "overlaps":overlaps,
        "context_features":context_features,
        "metadata":meta,
    }


def _hill_reference_data() -> Dict[str, Any]:
    """기존 hill-status API 호환용 메타데이터 래퍼."""
    return {"type":"FeatureCollection","features":[],"metadata":dict(_hill_grid_meta() or {})}

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

# ---------------------------------------------------------------------------
# 지구단위계획 CURRENT PLAN 참조정보
# - 공간중첩 자체는 기존 VWorld LT_C_UPISUQ161 결과를 사용한다.
# - 서울 열린데이터광장 upisCUq161 / upisDistUnitPlan은 관리코드·조서정보를
#   확인하는 참조 경로다. 공공누리 4유형 자료(upisDistUnitPlan)는 원문값을
#   필요한 범위에서만 전달하며, 사업판정·추천·밀도 산정에는 사용하지 않는다.
# ---------------------------------------------------------------------------
_DISTRICT_UNIT_REFERENCE_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_DISTRICT_UNIT_REFERENCE_CACHE_TTL = 6 * 60 * 60


def _row_value_ci(row: Dict[str, Any], *names: str) -> str:
    if not isinstance(row, dict):
        return ""
    for name in names:
        if name in row and row.get(name) not in (None, ""):
            return str(row.get(name)).strip()
    lower = {str(k).lower(): v for k, v in row.items()}
    for name in names:
        value = lower.get(str(name).lower())
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _district_unit_feature_key_candidates(hit: Dict[str, Any]) -> Dict[str, List[str]]:
    props = hit.get("properties") if isinstance(hit, dict) else {}
    props = props if isinstance(props, dict) else {}

    def vals(*names: str) -> List[str]:
        out: List[str] = []
        for name in names:
            value = _row_value_ci(props, name)
            if value and value not in out:
                out.append(value)
        direct = _row_value_ci(hit if isinstance(hit, dict) else {}, *names)
        if direct and direct not in out:
            out.append(direct)
        return out

    objt = vals("OBJT_ID", "objt_id", "OBJECTID", "objectid")
    feature_id = str(hit.get("feature_id") or "").strip() if isinstance(hit, dict) else ""
    if feature_id:
        if feature_id not in objt:
            objt.append(feature_id)
        tail = feature_id.rsplit(".", 1)[-1]
        if tail and tail not in objt:
            objt.append(tail)

    return {
        "rpt": vals("FIG_RPT_MNG_CD", "fig_rpt_mng_cd", "RPT_MNG_CD", "rpt_mng_cd"),
        "announcement": vals("DCSN_ANCMNT_MNG_CD", "dcsn_ancmnt_mng_cd", "ANCMNT_MNG_CD", "ancmnt_mng_cd"),
        "stut": vals("STUT_FIG_MNG_NO", "stut_fig_mng_no", "STUT_FIG_MNG_NM"),
        "objt": objt,
        "label": [x for x in [str(hit.get("name") or "").strip(), _row_value_ci(props, "LBL_NM", "lbl_nm")] if x],
    }


def _district_unit_reference_tables() -> Dict[str, Any]:
    key = _seoul_open_data_key()
    if not key:
        return {"status": "NO_KEY", "uq161": [], "plans": [], "message": "SEOUL_OPEN_DATA_KEY 미설정"}

    now = time.time()
    cached = _DISTRICT_UNIT_REFERENCE_CACHE.get("data")
    if cached is not None and now - float(_DISTRICT_UNIT_REFERENCE_CACHE.get("ts") or 0) < _DISTRICT_UNIT_REFERENCE_CACHE_TTL:
        return cached

    try:
        # UQ161은 공간도형의 관리코드 확인용(공공누리 1유형),
        # 지구단위계획 조서는 CURRENT PLAN 참조용(공공누리 4유형)이다.
        uq161 = _seoul_open_data_rows("upisCUq161", limit=20000)
        plans = _seoul_open_data_rows("upisDistUnitPlan", limit=20000)
        data = {"status": "OK", "uq161": uq161, "plans": plans, "message": ""}
    except Exception as exc:
        data = {"status": "ERROR", "uq161": [], "plans": [], "message": str(exc)[:300]}
    _DISTRICT_UNIT_REFERENCE_CACHE["ts"] = now
    _DISTRICT_UNIT_REFERENCE_CACHE["data"] = data
    return data


def _district_unit_reference_lookup(hits: List[Dict[str, Any]]) -> Dict[str, Any]:
    base = {
        "status": "NO_MATCH",
        "known": False,
        "matches": [],
        "announcement_codes": [],
        "message": "공식 조서 자동연결 미확정",
        "source_type": "SEOUL_OPEN_DATA_REFERENCE_ONLY",
        "license": "UQ161_KOGL_TYPE_1__DIST_UNIT_PLAN_KOGL_TYPE_4",
        "services": ["upisCUq161", "upisDistUnitPlan"],
        "match_policy": "관리코드 우선 → 객체/현황도형번호 → 유일한 정확 라벨 일치; 유사명칭 추정 금지",
        "effect_on_scheme_status": "NONE",
    }
    if not hits:
        return {**base, "status": "NOT_APPLICABLE", "known": True, "message": "지구단위계획구역 중첩 없음"}

    tables = _district_unit_reference_tables()
    if tables.get("status") == "NO_KEY":
        return {**base, "status": "NO_KEY", "message": tables.get("message") or "서울 열린데이터 API 키 미설정"}
    if tables.get("status") != "OK":
        return {**base, "status": "ERROR", "message": tables.get("message") or "서울 열린데이터 조회 실패"}

    uq161_rows: List[Dict[str, Any]] = list(tables.get("uq161") or [])
    plan_rows: List[Dict[str, Any]] = list(tables.get("plans") or [])

    uq_by_rpt: Dict[str, List[Dict[str, Any]]] = {}
    uq_by_ann: Dict[str, List[Dict[str, Any]]] = {}
    uq_by_stut: Dict[str, List[Dict[str, Any]]] = {}
    uq_by_objt: Dict[str, List[Dict[str, Any]]] = {}
    uq_by_label: Dict[str, List[Dict[str, Any]]] = {}
    for row in uq161_rows:
        for value, target in (
            (_row_value_ci(row, "FIG_RPT_MNG_CD"), uq_by_rpt),
            (_row_value_ci(row, "DCSN_ANCMNT_MNG_CD"), uq_by_ann),
            (_row_value_ci(row, "STUT_FIG_MNG_NO"), uq_by_stut),
            (_row_value_ci(row, "OBJT_ID"), uq_by_objt),
        ):
            if value:
                target.setdefault(value, []).append(row)
        label = _name_key(_row_value_ci(row, "LBL_NM"))
        if label:
            uq_by_label.setdefault(label, []).append(row)

    plan_by_rpt: Dict[str, List[Dict[str, Any]]] = {}
    plan_by_ann: Dict[str, List[Dict[str, Any]]] = {}
    for row in plan_rows:
        rpt = _row_value_ci(row, "RPT_MNG_CD")
        ann = _row_value_ci(row, "DCSN_ANCMNT_MNG_CD")
        if rpt:
            plan_by_rpt.setdefault(rpt, []).append(row)
        if ann:
            plan_by_ann.setdefault(ann, []).append(row)

    out_matches: List[Dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    announcement_codes: List[str] = []

    for hit in hits[:20]:
        if not isinstance(hit, dict):
            continue
        keys = _district_unit_feature_key_candidates(hit)
        official_rows: List[Dict[str, Any]] = []
        match_method = ""

        # 1) 관리코드가 직접 넘어오면 가장 신뢰도 높은 연결
        for rpt in keys["rpt"]:
            official_rows.extend(uq_by_rpt.get(rpt, []))
        if official_rows:
            match_method = "FIG_RPT_MNG_CD"
        if not official_rows:
            for ann in keys["announcement"]:
                official_rows.extend(uq_by_ann.get(ann, []))
            if official_rows:
                match_method = "DCSN_ANCMNT_MNG_CD"
        if not official_rows:
            for stut in keys["stut"]:
                official_rows.extend(uq_by_stut.get(stut, []))
            if official_rows:
                match_method = "STUT_FIG_MNG_NO"
        if not official_rows:
            for objt in keys["objt"]:
                official_rows.extend(uq_by_objt.get(objt, []))
            if official_rows:
                match_method = "OBJT_ID"
        # 마지막 수단은 '유일한 정확 라벨'만 허용. 부분/유사문자열 매칭은 금지한다.
        if not official_rows:
            for label in keys["label"]:
                rows = uq_by_label.get(_name_key(label), [])
                if len(rows) == 1:
                    official_rows = rows[:]
                    match_method = "LBL_NM_EXACT_UNIQUE"
                    break

        uniq_official: Dict[str, Dict[str, Any]] = {}
        for row in official_rows:
            identity = _row_value_ci(row, "OBJT_ID") or _row_value_ci(row, "STUT_FIG_MNG_NO") or _row_value_ci(row, "FIG_RPT_MNG_CD")
            uniq_official[identity or json.dumps(row, ensure_ascii=False, sort_keys=True)] = row

        for uq in uniq_official.values():
            rpt = _row_value_ci(uq, "FIG_RPT_MNG_CD")
            ann = _row_value_ci(uq, "DCSN_ANCMNT_MNG_CD")
            linked_plans = list(plan_by_rpt.get(rpt, [])) if rpt else []
            if not linked_plans and ann:
                linked_plans = list(plan_by_ann.get(ann, []))
            if not linked_plans:
                linked_plans = [{}]

            for plan in linked_plans:
                plan_ann = _row_value_ci(plan, "DCSN_ANCMNT_MNG_CD") or ann
                if plan_ann and plan_ann not in announcement_codes:
                    announcement_codes.append(plan_ann)
                identity = (_row_value_ci(plan, "RPT_MNG_CD") or rpt, plan_ann)
                if identity in seen:
                    continue
                seen.add(identity)
                out_matches.append({
                    "match_method": match_method or "MANAGEMENT_CODE",
                    "label": _row_value_ci(uq, "LBL_NM") or str(hit.get("name") or "").strip(),
                    "rpt_mng_cd": _row_value_ci(plan, "RPT_MNG_CD") or rpt,
                    "project_code": _row_value_ci(plan, "PRJC_CD"),
                    "rpt_type": _row_value_ci(plan, "RPT_TYPE"),
                    "category": _row_value_ci(plan, "LCLSF"),
                    "region_name": _row_value_ci(plan, "RGN_NM"),
                    "location_name": _row_value_ci(plan, "PSTN_NM"),
                    "area_existing": _row_value_ci(plan, "AREA_EXS"),
                    "area_change_code": _row_value_ci(plan, "AREA_ICDC_CD"),
                    "area_change": _row_value_ci(plan, "AREA_CHG"),
                    "area_after_change": _row_value_ci(plan, "AREA_CHG_AFTR"),
                    "announcement_code": plan_ann,
                    "spatial_object_id": _row_value_ci(uq, "OBJT_ID"),
                    "spatial_figure_mng_no": _row_value_ci(uq, "STUT_FIG_MNG_NO"),
                    "floorplan_no": _row_value_ci(uq, "FLRPLN_NO"),
                    "spatial_created_at": _row_value_ci(uq, "STUT_FIG_CRT_DT"),
                })

    if not out_matches:
        return {**base, "status": "NO_MATCH", "message": "공간중첩은 확인됐으나 서울시 공식 조서와 관리코드 자동연결이 확인되지 않았습니다."}
    return {
        **base,
        "status": "CONFIRMED",
        "known": True,
        "matches": out_matches[:40],
        "announcement_codes": announcement_codes[:40],
        "message": f"서울시 공식 지구단위계획 조서 {len(out_matches)}건 관리코드 연결",
    }


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


@lru_cache(maxsize=1)
def _safe_medical_offline_parcel_index() -> Dict[str, Any]:
    """VWorld 실패 시에만 쓰는 안심주택 의료시설 대표필지 스냅샷 색인.

    서울 전체 2020-12 연속지적도 934,780필지를 런타임에 적재하지 않고,
    사전 검증된 의료시설 91개 대표필지만 WGS84 GeoJSON으로 번들한다.
    """
    path = _data_path("safe_medical_parcels_202012.geojson")
    if not os.path.isfile(path):
        return {"available": False, "reason": "data/safe_medical_parcels_202012.geojson 미설치", "features": [], "geometries": [], "tree": None}
    try:
        with open(path, encoding="utf-8") as fp:
            payload = json.load(fp)
        features = []
        geometries = []
        for raw in payload.get("features") or []:
            if not isinstance(raw, dict) or not raw.get("geometry"):
                continue
            try:
                geom = shape(raw["geometry"])
            except Exception:
                continue
            if geom.is_empty:
                continue
            feature = {
                "type": "Feature",
                "geometry": raw["geometry"],
                "properties": dict(raw.get("properties") or {}),
            }
            features.append(feature)
            geometries.append(geom)
        return {
            "available": bool(features),
            "reason": None if features else "오프라인 의료시설 대표필지 스냅샷이 비어 있습니다.",
            "features": features,
            "geometries": geometries,
            "tree": STRtree(geometries) if geometries else None,
            "metadata": dict(payload.get("metadata") or {}),
        }
    except Exception as exc:
        logger.warning("safe medical offline cadastral snapshot load failed path=%s error=%s", path, exc)
        return {"available": False, "reason": str(exc), "features": [], "geometries": [], "tree": None}


def _safe_medical_offline_parcel_at_point(lon: float, lat: float) -> Dict[str, Any]:
    """공식 의료시설 좌표를 2020-12 오프라인 대표필지 스냅샷에 point-in-polygon 매칭한다."""
    index = _safe_medical_offline_parcel_index()
    if not index.get("available") or index.get("tree") is None:
        return {"status": "unavailable", "feature": None, "pnu": None, "reason": index.get("reason") or "오프라인 대표필지 스냅샷 미사용 가능"}
    point = shape({"type": "Point", "coordinates": [float(lon), float(lat)]})
    hits = []
    try:
        candidate_indexes = index["tree"].query(point, predicate="intersects")
    except Exception:
        candidate_indexes = index["tree"].query(point)
    for idx in candidate_indexes:
        try:
            i = int(idx)
            geom = index["geometries"][i]
            if geom.covers(point):
                hits.append(index["features"][i])
        except Exception:
            continue
    unique: Dict[str, Dict[str, Any]] = {}
    for feature in hits:
        pnu = str((feature.get("properties") or {}).get("pnu") or "").strip()
        if pnu:
            unique[pnu] = feature
    hits = list(unique.values())
    if len(hits) == 1:
        feature = hits[0]
        return {
            "status": "resolved",
            "feature": feature,
            "pnu": str((feature.get("properties") or {}).get("pnu") or ""),
            "source_type": "OFFLINE_CADASTRAL_SNAPSHOT_202012",
            "snapshot_date": "2020-12",
        }
    if len(hits) > 1:
        return {"status": "ambiguous", "feature": None, "pnu": None, "candidate_pnus": sorted(unique), "source_type": "OFFLINE_CADASTRAL_SNAPSHOT_202012"}
    return {"status": "not_found", "feature": None, "pnu": None, "candidate_pnus": [], "source_type": "OFFLINE_CADASTRAL_SNAPSHOT_202012"}

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
    """Use the bundled official monthly snapshot for interactive site analysis.

    R17 queried up to 20,000 TbHospitalInfo rows (1000 rows/page) every time the
    user drew a site.  That city-wide refresh is unrelated to the selected site
    and was a major latency source.  The packaged snapshot is itself an official
    TbHospitalInfo extract and is the stable FACT table for one analysis run.
    Live refresh is used only when the snapshot is absent; updating the packaged
    snapshot is a data-maintenance task, not a per-click task.
    """
    rows = _tb_hospital_snapshot_rows()
    key, key_env = _seoul_open_data_key_info()
    if rows:
        return rows, {
            "service": "TbHospitalInfo", "mode": "official_snapshot_primary",
            "rows": len(rows), "snapshot": "data/TbHospitalInfo_snapshot_20260808.csv",
            "credential_env": key_env or None,
            "note": "대상지 분석 시 도시 전체 실시간 재조회 생략; 패키지 공식 월간 스냅샷 사용",
        }
    if key:
        try:
            live = _seoul_open_data_rows("TbHospitalInfo", 20000)
            if live:
                return live, {"service": "TbHospitalInfo", "mode": "live_snapshot_missing", "rows": len(live), "credential_env": key_env}
        except Exception as exc:
            live_error = str(exc)
        else:
            live_error = "empty response"
    else:
        live_error = "서울 열린데이터광장 인증키 미설정"
    return [], {
        "service": "TbHospitalInfo", "mode": "unavailable", "rows": 0,
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
    """Resolve one representative parcel with LOCAL-FIRST semantics.

    Source chain (R25): bundled 2020-12 medical representative parcel snapshot
    -> VWorld coordinate -> VWorld address -> unresolved/REVIEW.

    The bundled snapshot is deliberately used first so a Render/VWorld outage
    cannot erase an already-packaged medical FACT.  The client may separately
    attempt a browser-side VWorld refresh for unresolved facilities; this server
    function never downgrades a resolved local parcel because an external API is
    unavailable.
    """
    offline_result = None
    if lon_key is not None and lat_key is not None:
        try:
            offline_result = _safe_medical_offline_parcel_at_point(float(lon_key), float(lat_key))
        except Exception as exc:
            offline_result = {
                "status": "error", "feature": None, "pnu": None, "reason": str(exc),
                "source_type": "OFFLINE_CADASTRAL_SNAPSHOT_202012",
            }
        if offline_result.get("status") == "resolved" and offline_result.get("feature"):
            return {
                **offline_result,
                "basis": "offline_cadastral_snapshot_202012",
                "boundary_note": "오프라인 연속지적도 스냅샷(기준일 2020-12), 최신 분할·합병 미반영 가능",
                "external_refresh_recommended": True,
            }

    point_result = None
    addr_result = None
    if lon_key is not None and lat_key is not None:
        try:
            point_result = _vworld_parcel_at_point(float(lon_key), float(lat_key))
        except Exception as exc:
            point_result = {"status": "error", "feature": None, "pnu": None, "reason": str(exc)}
        if point_result.get("status") == "resolved" and point_result.get("feature"):
            return {**point_result, "basis": "official_coordinate", "source_type": "VWORLD_LIVE_CADASTRAL"}
    if address:
        try:
            addr_result = _vworld_parcel_by_address(address)
        except Exception as exc:
            addr_result = {"status": "error", "feature": None, "pnu": None, "reason": str(exc)}
        if addr_result.get("status") == "resolved" and addr_result.get("feature"):
            return {**addr_result, "basis": "official_address", "source_type": "VWORLD_LIVE_CADASTRAL"}

    if addr_result is not None:
        result = dict(addr_result)
        if point_result:
            result.update({"point_status": point_result.get("status"), "point_reason": point_result.get("reason")})
    else:
        result = dict(point_result or {"status": "not_found", "feature": None, "pnu": None, "reason": "좌표·주소 없음"})
    if offline_result is not None:
        result["offline_fallback_status"] = offline_result.get("status")
        if offline_result.get("reason"):
            result["offline_fallback_reason"] = offline_result.get("reason")
    return result


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
        "screening_method": "시설 point 1.5km 선스크리닝 → 보유 2020-12 대표필지 스냅샷 우선 → 미확정 후보만 VWorld → 필지경계 350m",
        "parcel_resolution_order": "OFFLINE_CADASTRAL_SNAPSHOT_202012 -> VWORLD_SERVER -> VWORLD_BROWSER_FALLBACK(client)",
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

    def _resolve_medical_candidate(cand: Dict[str, Any]) -> Dict[str, Any]:
        lon, lat = float(cand["lon"]), float(cand["lat"])
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
            if cand.get(k) is not None:
                base[k]=cand.get(k)
        if parcel.get("status") != "resolved" or not parcel.get("feature"):
            return {**base,
                "boundary_status":"REVIEW", "boundary_basis":"REPRESENTATIVE_PARCEL_NOT_RESOLVED",
                "boundary_note":f"대표필지 확정 실패: {parcel.get('reason') or parcel.get('status')}",
                "auto_pass_eligible":False,
            }
        feature = parcel["feature"]
        try:
            metrics = _medical_boundary_metrics(site_wgs, feature["geometry"])
        except Exception as exc:
            return {**base,
                "boundary_status":"REVIEW", "boundary_basis":"REPRESENTATIVE_PARCEL_GEOMETRY_ERROR",
                "boundary_note":f"대표필지 geometry 처리 실패: {exc}", "auto_pass_eligible":False,
            }
        is_offline_snapshot = parcel.get("basis") == "offline_cadastral_snapshot_202012"
        return {**base,
            "distance_boundary_m":metrics.get("distance_boundary_m"), "within_350":metrics.get("within_350"),
            "coverage_350_pct":metrics.get("coverage_350_pct"), "overlap_350_area_m2":metrics.get("overlap_350_area_m2"),
            "buffer_350_geometry":metrics.get("buffer_350_geometry"), "facility_boundary_geometry":feature["geometry"],
            "primary_pnu":parcel.get("pnu"), "parcel_count":1,
            "boundary_status":"CONFIRMED",
            "boundary_basis":"OFFLINE_CADASTRAL_SNAPSHOT_202012" if is_offline_snapshot else "REPRESENTATIVE_CADASTRAL_PARCEL",
            "boundary_basis_label":"오프라인 연속지적도 스냅샷(2020-12)" if is_offline_snapshot else "대표지번 연속지적 필지",
            "parcel_candidate_basis":parcel.get("basis"),
            "source_type":"OFFLINE_CADASTRAL_SNAPSHOT_202012" if is_offline_snapshot else str(parcel.get("source_type") or "VWORLD_LIVE_CADASTRAL"),
            "boundary_note":parcel.get("boundary_note") if is_offline_snapshot else "공식 좌표가 포함되는 대표지번 1필지를 초기검토용 의료시설 부지로 적용",
            "auto_pass_eligible":True,
        }

    # Only nearby eligible facilities reach parcel resolution.  Resolve them in a
    # small bounded pool so one slow VWorld request does not serialize every
    # candidate; each failed candidate remains REVIEW rather than becoming PASS.
    items: List[Dict[str, Any]] = []
    parcel_calls = len(screened)
    if screened:
        with ThreadPoolExecutor(max_workers=min(2, len(screened))) as pool:
            future_map = {pool.submit(_resolve_medical_candidate, cand): cand for cand in screened}
            for fut in as_completed(future_map):
                cand = future_map[fut]
                try:
                    items.append(fut.result())
                except Exception as exc:
                    items.append({
                        "category":cand.get("category"), "name":cand.get("name"), "address":cand.get("address"),
                        "geometry":{"type":"Point","coordinates":[float(cand["lon"]),float(cand["lat"])]},
                        "distance_point_m":cand.get("distance_point_m"), "boundary_status":"REVIEW",
                        "boundary_basis":"REPRESENTATIVE_PARCEL_LOOKUP_ERROR",
                        "boundary_note":f"대표필지 조회 오류: {exc}", "auto_pass_eligible":False,
                    })

    confirmed = [x for x in items if x.get("boundary_status") == "CONFIRMED" and x.get("facility_boundary_geometry")]
    # 안심주택 운영기준은 사업대상지 면적의 50% 이상이 중심지역에 포함되는 것을 일반경로로 본다.
    confirmed_350 = [x for x in confirmed if x.get("coverage_350_pct") is not None and float(x.get("coverage_350_pct")) >= 50.0]
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
    negative_complete = (
        not errors
        and stats["medical_reference"].get("municipal_unmatched", 0) == 0
        and stats["medical_reference"].get("health_center_unmatched", 0) == 0
        and len(review) == 0
        and bool(rows)
    )
    nearby_counts = {
        "general_hospital":sum(1 for x in items if x.get("category")=="general_hospital"),
        "municipal_hospital":sum(1 for x in items if x.get("category")=="municipal_hospital"),
        "public_health_center":sum(1 for x in items if x.get("category")=="public_health_center"),
        "boundary_confirmed":len(confirmed), "boundary_confirmed_350":len(confirmed_350), "boundary_review":len(review),
        "negative_complete": negative_complete,
    }
    if confirmed_350:
        status="resolved"; message=f"대표필지 경계 기준 350m 이내 인정 의료시설 {len(confirmed_350)}건 확인"
    elif confirmed:
        status="resolved"; message="대표필지는 확인됐으나 350m 이내 인정 의료시설은 확인되지 않았습니다."
    else:
        status="reference" if items else ("error" if errors else "none")
        message="의료시설 후보는 확인했으나 대표필지를 확정하지 못해 REVIEW입니다." if items else "인근 인정 의료시설 후보를 확인하지 못했습니다."
    return {
        "status":status, "auto_pass_eligible":bool(confirmed_350), "negative_complete":negative_complete, "items":items[:40],
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


class PlanningLayersInput(BaseModel):
    geometry: Dict[str, Any]
    layer_ids: List[str] = Field(..., min_length=1, max_length=5)
    force_retry: bool = False
    # 브라우저 origin을 함께 보내 서버 호출의 VWorld domain/referer를 동일하게 맞춘다.
    # 서버는 실제 요청 Host와 일치하는 origin만 허용한다.
    client_origin: Optional[str] = None


class DistrictUnitPlanReferenceInput(BaseModel):
    # 브라우저에서 확인한 UQ161 중첩도형의 최소 식별정보만 받는다.
    # 이 endpoint는 CURRENT PLAN 참조정보 제공용이며 사업 PASS/FAIL에 사용하지 않는다.
    hits: List[Dict[str, Any]] = Field(default_factory=list, max_length=20)


@app.post("/api/spatial/zoning")
def spatial_zoning(inp: GeometryInput):
    """사업판정용 핵심 용도지역 FACT.

    브라우저의 VWorld JSONP 직접조회 대신 서버 direct→official proxy fallback과
    최근 정상 FACT 캐시를 사용한다. 0건은 NONE으로 확정하지 않는다.
    """
    _require_vworld_key()
    try:
        return analyze_zoning_features(inp.geometry)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("zoning UQ111 analysis failed")
        message = str(exc)[:240]
        if "HTTP 429" in message or "429" in message and "VWorld" in message:
            raise HTTPException(status_code=429, detail=f"용도지역 LT_C_UQ111 일시 호출제한: {message}") from exc
        raise HTTPException(status_code=502, detail=f"용도지역 LT_C_UQ111 조회 실패: {message}") from exc


# R13: 도시관리계획 23개 레이어를 브라우저 JSONP가 아니라 서버의
# direct -> VWorld 공식 proxy 경로로만 조회한다. 여러 브라우저 요청이 동시에
# 들어와도 이 세마포어 하나가 서버 프로세스 전체 VWorld 호출을 최대 2개로 제한한다.
PLANNING_LAYER_IDS = frozenset({
    "LT_C_UQ111", "LT_C_UQ121", "LT_C_UQ123", "LT_C_UQ124", "LT_C_UQ125",
    "LT_C_UQ126", "LT_C_UQ128", "LT_C_UQ129", "LT_C_UQ130", "LT_C_UQ162",
    "LT_C_UD801", "LT_C_UO301", "LT_C_UPISUQ151", "LT_C_UPISUQ152",
    "LT_C_UPISUQ153", "LT_C_UPISUQ154", "LT_C_UPISUQ155", "LT_C_UPISUQ156",
    "LT_C_UPISUQ157", "LT_C_UPISUQ158", "LT_C_UPISUQ159", "LT_C_UPISUQ161",
    "LT_C_UPISUQ171",
})
_PLANNING_VWORLD_SEMAPHORE = threading.BoundedSemaphore(_vworld_slot_count())
_PLANNING_LAYER_CACHE: Dict[str, Dict[str, Any]] = {}
_PLANNING_LAYER_CACHE_LOCK = threading.Lock()
_PLANNING_LAYER_CACHE_TTL_SEC = 10 * 60
_PLANNING_REQUEST_LOCAL = threading.local()


def _validated_planning_client_origin(raw: Optional[str], request: Request) -> str:
    """브라우저가 실제 접속한 origin을 VWorld 인증 domain으로 재사용한다.

    임의 외부 origin으로 서버 키를 중계하지 않도록 X-Forwarded-Host/Host와
    hostname이 같은 경우에만 허용하고, 불일치/파싱실패는 기존 서버 domain으로
    폴백한다. 키 자체는 브라우저 응답에 노출하지 않는다.
    """
    fallback = _vworld_domain()
    value = _normalize_vworld_domain(raw or "")
    if not value:
        return fallback
    try:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return fallback
        forwarded = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",", 1)[0].strip()
        request_host = forwarded.split(":", 1)[0].strip().lower()
        if request_host and parsed.hostname.lower() != request_host:
            logger.warning("Planning VWorld client_origin rejected origin=%s request_host=%s", value, request_host)
            return fallback
        return value
    except Exception:
        return fallback


def _planning_request_domain() -> str:
    return _normalize_vworld_domain(getattr(_PLANNING_REQUEST_LOCAL, "domain", "") or "") or _vworld_domain()


def _planning_request_force_retry() -> bool:
    return bool(getattr(_PLANNING_REQUEST_LOCAL, "force_retry", False))


class PlanningLayerFetchError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False, retry_after: Optional[float] = None, route: str = "", direct_error: str = "", proxy_status: Optional[int] = None, domain_sent: str = ""):
        super().__init__(message)
        self.retryable = bool(retryable)
        self.retry_after = retry_after
        self.route = str(route or "")
        self.direct_error = str(direct_error or "")[:300]
        self.proxy_status = proxy_status
        self.domain_sent = str(domain_sent or "")


def _planning_geometry_signature(geometry: Dict[str, Any]) -> str:
    raw = json.dumps(geometry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _planning_cache_get(cache_key: str) -> Optional[Dict[str, Any]]:
    with _PLANNING_LAYER_CACHE_LOCK:
        row = _PLANNING_LAYER_CACHE.get(cache_key)
        if not row or time.time() - float(row.get("saved_at") or 0) > _PLANNING_LAYER_CACHE_TTL_SEC:
            if row:
                _PLANNING_LAYER_CACHE.pop(cache_key, None)
            return None
        return json.loads(json.dumps(row["result"], ensure_ascii=False))


def _planning_cache_put(cache_key: str, result: Dict[str, Any]) -> None:
    # ERROR는 저장하지 않으며 기존 정상 캐시도 절대 덮어쓰지 않는다.
    if result.get("status") not in {"SUCCESS_DATA", "SUCCESS_EMPTY"}:
        return
    with _PLANNING_LAYER_CACHE_LOCK:
        _PLANNING_LAYER_CACHE[cache_key] = {
            "saved_at": time.time(),
            "result": json.loads(json.dumps(result, ensure_ascii=False)),
        }
        if len(_PLANNING_LAYER_CACHE) > 300:
            oldest = sorted(_PLANNING_LAYER_CACHE, key=lambda k: _PLANNING_LAYER_CACHE[k]["saved_at"])[:50]
            for key in oldest:
                _PLANNING_LAYER_CACHE.pop(key, None)


def _planning_retry_after(resp: requests.Response) -> Optional[float]:
    raw = (resp.headers.get("Retry-After") or "").strip()
    try:
        return max(0.0, min(float(raw), 30.0)) if raw else None
    except ValueError:
        return None


def _fetch_planning_layer_once(layer_id: str, geometry: Dict[str, Any]) -> tuple[List[Dict[str, Any]], str]:
    if not _vworld_key():
        raise PlanningLayerFetchError("VWORLD_API_KEY가 설정되지 않았습니다.", retryable=False)
    target = shape(geometry)
    if target.geom_type not in {"Polygon", "MultiPolygon"} or target.is_empty:
        raise PlanningLayerFetchError("유효한 Polygon 또는 MultiPolygon 구역계가 필요합니다.", retryable=False)
    if not target.is_valid:
        target = target.buffer(0)
    if target.is_empty or not target.is_valid:
        raise PlanningLayerFetchError("유효하지 않은 구역계입니다.", retryable=False)
    minx, miny, maxx, maxy = target.bounds
    planning_domain = _planning_request_domain()
    all_features: List[Dict[str, Any]] = []
    seen = set()
    routes = []
    for page in range(1, 11):
        params = {
            "key": _vworld_key(), "domain": planning_domain, "service": "data",
            "version": "2.0", "request": "getfeature", "format": "json",
            "size": 1000, "page": page, "geometry": "true", "attribute": "true",
            "crs": "EPSG:4326", "data": layer_id,
            "geomfilter": f"BOX({minx},{miny},{maxx},{maxy})",
        }
        try:
            with _PLANNING_VWORLD_SEMAPHORE:
                resp, route = _vworld_get(
                    VWORLD_DATA_URL,
                    params=params,
                    timeout=(4.0, 21.0),
                    proxy_timeout=(5.0, 25.0),
                    referer_domain=planning_domain,
                    force_retry=_planning_request_force_retry(),
                )
        except Exception as exc:
            diag = _last_vworld_diagnostic()
            # direct + official proxy are already the two transport attempts for this
            # layer request. Do not multiply a full 25s + 30s transport failure by
            # the outer three-attempt loop; let the explicit user retry bypass cooldown.
            transport_failed = isinstance(exc, VWorldTransportError)
            raise PlanningLayerFetchError(
                str(exc),
                retryable=not transport_failed,
                route=str(diag.get("route") or ""),
                direct_error=str(diag.get("direct_error") or ""),
                proxy_status=diag.get("proxy_status"),
                domain_sent=str(diag.get("domain_sent") or planning_domain),
            ) from exc
        routes.append(route)
        diag = _last_vworld_diagnostic()
        error_kwargs = {"route": route, "direct_error": str(diag.get("direct_error") or ""), "proxy_status": diag.get("proxy_status"), "domain_sent": str(diag.get("domain_sent") or planning_domain)}
        if resp.status_code == 429:
            raise PlanningLayerFetchError("VWorld HTTP 429 호출제한", retryable=True, retry_after=_planning_retry_after(resp), **error_kwargs)
        if resp.status_code >= 500:
            raise PlanningLayerFetchError(f"VWorld HTTP {resp.status_code}", retryable=True, **error_kwargs)
        if resp.status_code >= 400:
            raise PlanningLayerFetchError(f"VWorld HTTP {resp.status_code}: {resp.text[:240]}", retryable=False, **error_kwargs)
        try:
            payload = resp.json()
        except Exception as exc:
            raise PlanningLayerFetchError("VWorld JSON 응답 파싱 오류", retryable=True) from exc
        response = payload.get("response") or {}
        status = str(response.get("status") or "").upper()
        if status == "NOT_FOUND":
            break
        if status != "OK":
            message = _response_error_message(payload)
            retryable = bool(re.search(r"tempor|timeout|limit|429|server|unavailable|일시|제한", message, re.I))
            diag = _last_vworld_diagnostic()
            raise PlanningLayerFetchError(message, retryable=retryable, route=route, direct_error=str(diag.get("direct_error") or ""), proxy_status=diag.get("proxy_status"), domain_sent=str(diag.get("domain_sent") or planning_domain))
        feats = (((response.get("result") or {}).get("featureCollection") or {}).get("features") or [])
        for feature in feats:
            if not feature.get("geometry"):
                continue
            props = feature.get("properties") or {}
            key = str(feature.get("id") or props.get("unq_mnno") or props.get("UNQ_MNNO") or "")
            if not key:
                key = hashlib.sha1(json.dumps(feature.get("geometry"), sort_keys=True).encode("utf-8")).hexdigest()
            if key in seen:
                continue
            try:
                if not shape(feature["geometry"]).intersects(target):
                    continue
            except Exception:
                continue
            seen.add(key)
            all_features.append(feature)
        if len(feats) < 1000:
            break
    else:
        raise PlanningLayerFetchError(f"{layer_id} 후보가 10,000건을 넘어 조회를 중단했습니다.", retryable=False)
    route = "vworld_proxy" if "vworld_proxy" in routes else "direct"
    return all_features, route


def _planning_layer_result(layer_id: str, geometry: Dict[str, Any]) -> Dict[str, Any]:
    started = time.monotonic()
    signature = _planning_geometry_signature(geometry)
    cache_key = f"{signature}:{layer_id}"
    cached = _planning_cache_get(cache_key)
    if cached is not None:
        cached.update({"cache_hit": True, "elapsed_ms": int((time.monotonic() - started) * 1000)})
        return cached
    attempts = 0
    last_error = ""
    route = ""
    direct_error = ""
    proxy_status: Optional[int] = None
    domain_sent = ""
    for retry_index in range(3):
        attempts += 1
        try:
            features, route = _fetch_planning_layer_once(layer_id, geometry)
            diag_getter = globals().get("_last_vworld_diagnostic")
            diag = diag_getter() if callable(diag_getter) else {}
            direct_error = str(diag.get("direct_error") or "")[:300]
            proxy_status = diag.get("proxy_status")
            domain_sent = str(diag.get("domain_sent") or domain_sent)
            result = {
                "layer_id": layer_id,
                "status": "SUCCESS_DATA" if features else "SUCCESS_EMPTY",
                "feature_count": len(features), "features": features, "error": "",
                "attempts": attempts, "route": route, "direct_error": direct_error,
                "proxy_status": proxy_status, "domain_sent": domain_sent,
                "elapsed_ms": int((time.monotonic() - started) * 1000), "cache_hit": False,
            }
            _planning_cache_put(cache_key, result)
            return result
        except PlanningLayerFetchError as exc:
            last_error = str(exc)[:400]
            route = exc.route or route
            direct_error = exc.direct_error or direct_error
            proxy_status = exc.proxy_status if exc.proxy_status is not None else proxy_status
            domain_sent = exc.domain_sent or domain_sent
            if not exc.retryable or retry_index >= 2:
                break
            delay = exc.retry_after if exc.retry_after is not None else (1.5 if retry_index == 0 else 4.0)
            time.sleep(max(0.0, min(delay, 30.0)))
        except Exception as exc:
            last_error = str(exc)[:400]
            if retry_index >= 2:
                break
            time.sleep(1.5 if retry_index == 0 else 4.0)
    return {
        "layer_id": layer_id, "status": "ERROR", "feature_count": 0, "features": [],
        "error": last_error or "도시관리계획 레이어 조회 실패", "attempts": attempts,
        "route": route, "direct_error": direct_error, "proxy_status": proxy_status, "domain_sent": domain_sent,
        "elapsed_ms": int((time.monotonic() - started) * 1000), "cache_hit": False,
    }


@app.post("/api/spatial/planning-layers")
def spatial_planning_layers(inp: PlanningLayersInput, request: Request):
    _require_vworld_key()
    layer_ids = list(dict.fromkeys(str(x or "").strip() for x in inp.layer_ids))
    invalid = [x for x in layer_ids if x not in PLANNING_LAYER_IDS]
    if invalid:
        raise HTTPException(status_code=422, detail=f"허용되지 않은 도시관리계획 레이어: {', '.join(invalid)}")
    client_domain = _validated_planning_client_origin(inp.client_origin, request)

    def work(layer_id: str) -> Dict[str, Any]:
        _PLANNING_REQUEST_LOCAL.domain = client_domain
        _PLANNING_REQUEST_LOCAL.force_retry = bool(inp.force_retry)
        try:
            result = _planning_layer_result(layer_id, inp.geometry)
            if result.get("cache_hit") or not result.get("domain_sent"):
                result["domain_sent"] = client_domain
            return result
        finally:
            _PLANNING_REQUEST_LOCAL.domain = ""
            _PLANNING_REQUEST_LOCAL.force_retry = False

    results: List[Dict[str, Any]] = []
    # 요청 내부/계획레이어/전체 VWorld transport가 동일한 VWORLD_SLOTS(기본 2, 최대 3)를 따른다.
    with ThreadPoolExecutor(max_workers=min(_vworld_slot_count(), len(layer_ids))) as pool:
        futures = {pool.submit(work, layer_id): layer_id for layer_id in layer_ids}
        by_id = {}
        for future in as_completed(futures):
            layer_id = futures[future]
            try:
                by_id[layer_id] = future.result()
            except Exception as exc:
                by_id[layer_id] = {"layer_id": layer_id, "status": "ERROR", "feature_count": 0, "features": [], "error": str(exc)[:400], "attempts": 1, "route": "", "direct_error": "", "proxy_status": None, "domain_sent": client_domain, "elapsed_ms": 0, "cache_hit": False}
        results = [by_id[layer_id] for layer_id in layer_ids]
    return {"geometry_signature": _planning_geometry_signature(inp.geometry), "requested": len(layer_ids), "client_domain": client_domain, "server_domain": _vworld_domain(), "results": results}


class RoadFactInput(BaseModel):
    geometry: Dict[str, Any]
    radius_m: float = Field(220.0, ge=0.0, le=1000.0)

class StreetBlockInput(BaseModel):
    geometry: Dict[str, Any]
    barrier_features: List[Dict[str, Any]] = Field(default_factory=list, max_length=3000)
    road_features: List[Dict[str, Any]] = Field(default_factory=list, max_length=5000)
    road_surface_features: List[Dict[str, Any]] = Field(default_factory=list, max_length=5000)
    # TL_SPRD_RW 실제 도로면 전체. 폭원이 없더라도 선택 사업지 바깥에서 블록 외곽을
    # 실제로 닫는 도로는 폭원과 무관하게 폐합경계로 사용할 수 있게 별도 전달한다.
    road_area_features: List[Dict[str, Any]] = Field(default_factory=list, max_length=5000)
    road_min_width_m: float = Field(4.0, ge=0.0, le=50.0)
    outer_closure_all_roads: bool = False
    max_radius_m: float = Field(500.0, ge=120.0, le=1000.0)


class StreetBlockSchemeRuleInput(BaseModel):
    barrier_features: List[Dict[str, Any]] = Field(default_factory=list, max_length=3000)
    road_min_width_m: float = Field(4.0, ge=0.0, le=50.0)
    outer_closure_all_roads: bool = False


class StreetBlockBatchInput(BaseModel):
    geometry: Dict[str, Any]
    road_features: List[Dict[str, Any]] = Field(default_factory=list, max_length=5000)
    road_surface_features: List[Dict[str, Any]] = Field(default_factory=list, max_length=5000)
    road_area_features: List[Dict[str, Any]] = Field(default_factory=list, max_length=5000)
    schemes: Dict[str, StreetBlockSchemeRuleInput]
    max_radius_m: float = Field(500.0, ge=120.0, le=1000.0)


class BuildingHubBatchInput(BaseModel):
    pnus: List[str] = Field(..., min_length=1, max_length=50)


class LandLedgerOneInput(BaseModel):
    pnu: str
    # Browser VWorld NED was already attempted by the client.  When true,
    # use the bundled AL_D003 snapshot before spending another Render->VWorld call.
    browser_live_attempted: bool = False


class PnuListInput(BaseModel):
    pnus: List[str] = Field(..., min_length=1, max_length=200)


class LandPriceBatchInput(BaseModel):
    pnus: List[str] = Field(..., min_length=1, max_length=200)
    year: int = Field(..., ge=2000, le=2100)


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


class AIComprehensiveAnalysisInput(BaseModel):
    # 프론트에서 정리한 FACT + 사업별 RULE 결과 요약만 받는다.
    # 원본 코드·원시 공간자료·법령 전문은 이 API로 전달하지 않는다.
    summary: Dict[str, Any]


def _compact_text(value: Any, limit: int = 280) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _fallback_ai_comprehensive_sentences(summary: Dict[str, Any]) -> List[str]:
    """AI 키가 없거나 호출이 실패했을 때 FACT/RULE 요약만으로 만드는 안전한 설명.

    이 함수는 새 판정을 하지 않고 입력 JSON의 값·판정·추천순서만 문장화한다.
    UI에서는 반드시 '판정엔진 요약'으로 표시한다.
    """
    site = summary.get("site_summary") or {}
    spatial = summary.get("spatial_facts") or {}
    businesses = summary.get("business_results") or []
    rec = summary.get("recommended_business") or {}
    alternatives = summary.get("alternative_businesses") or []
    unknowns = summary.get("unknown_items") or []

    area = site.get("area_m2")
    zoning = _compact_text(site.get("zoning")) or "용도지역 미확인"
    parcels = site.get("parcel_count")
    buildings = site.get("building_count")
    old_count = site.get("old_building_count")
    old_ratio = site.get("old_building_ratio_pct")

    s1 = f"대상지는 {area:,.0f}㎡ 규모이며 주용도지역은 '{zoning}'으로 확인됩니다." if isinstance(area, (int, float)) else f"대상지 면적은 추가 확인이 필요하며 주용도지역은 '{zoning}'으로 확인됩니다."
    if isinstance(buildings, (int, float)) and isinstance(old_count, (int, float)):
        age_tail = f"({old_ratio:.1f}%)" if isinstance(old_ratio, (int, float)) else ""
        s2 = f"현재 수집된 건축물은 {int(buildings)}동이고 노후건축물은 {int(old_count)}동{age_tail}이며, 필지는 {int(parcels)}필지입니다." if isinstance(parcels, (int, float)) else f"현재 수집된 건축물은 {int(buildings)}동이고 노후건축물은 {int(old_count)}동{age_tail}입니다."
    else:
        s2 = "필지·건축물·노후도 중 일부는 자료가 충분하지 않아 추가 확인이 필요합니다."

    station = spatial.get("station") or {}
    center = spatial.get("center") or {}
    station_bits = []
    if station.get("name"): station_bits.append(f"최근접역 {station.get('name')}")
    if isinstance(station.get("distance_m"), (int, float)): station_bits.append(f"거리 {station.get('distance_m'):,.0f}m")
    if isinstance(station.get("line_count"), (int, float)): station_bits.append(f"{int(station.get('line_count'))}개 노선")
    center_text = _compact_text(center.get("label") or center.get("value"))
    s3 = ("역세권·중심지 FACT는 " + " · ".join(station_bits + ([f"중심지 {center_text}"] if center_text else [])) + "로 정리됩니다.") if (station_bits or center_text) else "역세권·중심지 FACT는 현재 자료만으로 확정하기 어려워 추가 확인이 필요합니다."

    road = spatial.get("road") or {}
    sb = spatial.get("street_block") or {}
    road_bits = []
    if isinstance(road.get("max_width_m"), (int, float)): road_bits.append(f"최대 확인 도로폭 {road.get('max_width_m'):g}m")
    if isinstance(road.get("face_count"), (int, float)): road_bits.append(f"접도면 {int(road.get('face_count'))}면")
    if sb.get("loaded") is True:
        cnt = sb.get("block_count")
        road_bits.append(f"가로구역 {int(cnt)}개" if isinstance(cnt, (int, float)) else "가로구역 FACT 확보")
    s4 = ("도로·가로구역 현황은 " + " · ".join(road_bits) + "로 확인됩니다.") if road_bits else "도로·가로구역 자료는 REVIEW 항목을 포함해 후속 확인이 필요합니다."

    overlaps = spatial.get("overlaps") or {}
    overlap_names = []
    for key in ("renewal", "district_plans", "planning_facilities"):
        for item in overlaps.get(key) or []:
            nm = _compact_text(item.get("name") if isinstance(item, dict) else item, 60)
            if nm and nm not in overlap_names: overlap_names.append(nm)
    s5 = f"지구단위·정비구역·도시계획시설 중첩은 {', '.join(overlap_names[:4])} 등이 현재 현황자료에 잡혀 있습니다." if overlap_names else "지구단위·정비구역 등 중첩현황은 현재 확보된 자료 범위에서 별도 중첩이 확인되지 않았거나 추가 확인이 필요합니다."

    rec_name = _compact_text(rec.get("business") or rec.get("name"), 80)
    rec_status = _compact_text(rec.get("status") or rec.get("display_label"), 80)
    rec_reason = _compact_text(rec.get("reason"), 220)
    if rec_name:
        s6 = f"현재 조건에서 우선 검토할 사업은 {rec_name}이며, 판정엔진의 상대비교 결과는 {rec_status or '우선순위 후보'}입니다."
        s7 = f"우선순위 근거는 {rec_reason}입니다." if rec_reason else "우선순위는 기존 PASS·CONDITIONAL·REVIEW 결과와 계획가능용적률 비교순서를 그대로 따른 것입니다."
    else:
        s6 = "현재 판정엔진 결과만으로 우선 추천할 사업을 확정하기 어렵습니다."
        s7 = "사업별 미충족 조건과 REVIEW 항목을 보완한 뒤 상대비교를 다시 수행하는 것이 필요합니다."

    alt_names = [_compact_text(x.get("business") if isinstance(x, dict) else x, 80) for x in alternatives]
    alt_names = [x for x in alt_names if x]
    s8 = f"차순위 대안은 {', '.join(alt_names[:2])}이며, 우선사업과 제도조건·사업성 가정을 함께 비교하는 것이 적절합니다." if alt_names else "차순위 대안은 현재 판정결과에서 뚜렷하게 도출되지 않았습니다."

    rec_business = next((b for b in businesses if _compact_text(b.get("business"),80)==rec_name), None) if rec_name else None
    gap_texts = []
    for item in (rec_business or {}).get("gaps") or []:
        if isinstance(item, dict):
            t = _compact_text(item.get("gap") or item.get("item") or item.get("action"), 100)
        else: t = _compact_text(item, 100)
        if t and t not in gap_texts: gap_texts.append(t)
    s9 = f"우선사업의 주요 미충족·제약사항은 {', '.join(gap_texts[:3])}로 정리되며, 확정판정이 아니라 보완대상으로 봐야 합니다." if gap_texts else "우선사업에서 현재 확인된 명시적 미충족 gap은 크지 않지만 REVIEW 항목은 별도 확인해야 합니다."

    change = _compact_text((rec_business or {}).get("condition_change"), 180)
    s10 = f"개선 가능사항으로는 {change}를 우선 검토할 수 있습니다." if change else "구역확대·기간경과·사업조건 변경 가능성은 기존 판정표에 계산된 항목이 있을 때만 후속 대안으로 검토합니다."

    unknown_texts=[]
    for x in unknowns:
        t=_compact_text(x.get("text") if isinstance(x, dict) else x, 100)
        if t and t not in unknown_texts: unknown_texts.append(t)
    s11 = f"추가 확인이 필요한 항목은 {', '.join(unknown_texts[:3])} 등이며, 이 항목은 충족 또는 미충족으로 단정하지 않습니다." if unknown_texts else "API 실패나 미확보 자료가 새로 발생하면 해당 항목은 충족·미충족이 아니라 추가 확인 필요로 처리해야 합니다."
    s12 = "본 결과는 초기 사업전략 비교용이며 대지형상·문화재·높이·경관·건축배치 등 설계·인허가 변수는 별도 전문검토가 필요합니다."
    return [s1,s2,s3,s4,s5,s6,s7,s8,s9,s10,s11,s12]


def _openai_ai_comprehensive(summary: Dict[str, Any]) -> Dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return {"mode": "rules_fallback", "model": "", "sentences": _fallback_ai_comprehensive_sentences(summary), "note": "OPENAI_API_KEY 미설정"}

    model = os.getenv("OPENAI_AI_MODEL", "gpt-5-mini").strip() or "gpt-5-mini"
    timeout_s = max(10, min(90, int(os.getenv("OPENAI_AI_TIMEOUT_SECONDS", "45") or 45)))
    input_json = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
    if len(input_json.encode("utf-8")) > 80_000:
        raise HTTPException(status_code=413, detail="AI 분석 요약 JSON이 너무 큽니다.")

    instructions = (
        "당신은 서울 정비·개발사업 초기검토 플랫폼의 설명 전용 AI다. "
        "사업을 새로 판정하지 말고, 입력 JSON에 이미 존재하는 FACT와 RULE 결과와 추천순서만 설명한다. "
        "입력에 없는 법적 기준·수치·현황·사실을 절대 추가하거나 추정하지 않는다. "
        "REVIEW 또는 UNKNOWN은 충족/미충족으로 단정하지 말고 '추가 확인 필요'로 표현한다. "
        "PASS도 인허가 확정으로 표현하지 말고 '초기 검토상 적용 가능성이 높음' 또는 '법·제도상 우선 검토 가능'처럼 표현한다. "
        "사업별 상대비교를 중심으로 8~12개의 한국어 문장을 작성한다. "
        "대상지 핵심 특성, 정비 특성, 역세권·중심지·도로 잠재력, 우선사업, 추천이유, 차순위, gap/제약, 개선 가능사항, 미확인사항, 설계·인허가 별도검토, 초기 전략 의견을 가능한 범위에서 포함한다. "
        "법령 전문을 쓰지 말고 입력에 있는 법적 근거명도 꼭 필요한 경우에만 짧게 언급한다. "
        "숫자는 입력 JSON에 있는 숫자만 사용한다."
    )
    payload = {
        "model": model,
        "instructions": instructions,
        "input": input_json,
        "store": False,
        "max_output_tokens": 900,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "urban_strategy_ai_comprehensive",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "sentences": {"type": "array", "minItems": 8, "maxItems": 12, "items": {"type": "string"}}
                    },
                    "required": ["sentences"],
                    "additionalProperties": False
                }
            },
            "verbosity": "low"
        }
    }
    try:
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload, timeout=timeout_s
        )
        response.raise_for_status()
        data = response.json()
        output_text = data.get("output_text")
        if not output_text:
            texts=[]
            for item in data.get("output") or []:
                if item.get("type") != "message":
                    continue
                for content in item.get("content") or []:
                    if content.get("type") == "output_text" and content.get("text"):
                        texts.append(content.get("text"))
            output_text = "\n".join(texts)
        parsed = json.loads(output_text or "{}")
        sentences = [_compact_text(x, 500) for x in (parsed.get("sentences") or []) if _compact_text(x, 500)]
        if not 8 <= len(sentences) <= 12:
            raise ValueError("AI 문장 수가 8~12 범위를 벗어남")
        return {"mode": "openai", "model": model, "sentences": sentences, "note": "FACT/RULE 요약 객체만 전달"}
    except Exception as exc:
        logging.warning("AI comprehensive analysis fallback: %s", exc)
        return {"mode": "rules_fallback", "model": model, "sentences": _fallback_ai_comprehensive_sentences(summary), "note": f"AI 호출 실패 · 판정엔진 요약 사용: {_compact_text(exc, 160)}"}


def _user_agent_group(request: Request) -> str:
    ua = request.headers.get("user-agent", "").lower()
    device = "mobile" if any(x in ua for x in ("android", "iphone", "mobile")) else "desktop"
    browser = "edge" if "edg/" in ua else "chrome" if "chrome/" in ua else "safari" if "safari/" in ua else "firefox" if "firefox/" in ua else "other"
    return f"{device}/{browser}"


@app.post("/api/ai/comprehensive-analysis")
def ai_comprehensive_analysis(payload: AIComprehensiveAnalysisInput):
    summary = payload.summary or {}
    if not isinstance(summary, dict) or not summary:
        raise HTTPException(status_code=400, detail="AI 분석용 FACT/RULE 요약 객체가 없습니다.")
    return _openai_ai_comprehensive(summary)


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
    html = _index_html().replace("__VWORLD_CLIENT_KEY__", _vworld_client_key())
    return HTMLResponse(
        content=html,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


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
APP_BUILD_MARKER = "R32_VWORLD_THEN_LOCAL_LAND_PRICE_20260923"
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
    """단일 역사 노선 보강조회.

    한 개 노선만 보인다는 사실을 곧바로 '비환승 확정'으로 쓰지 않는다.
    다만 (1) 전체 노선표와 (2) 역명 직접조회라는 서로 다른 공식 조회경로가
    동일한 단일 노선으로 교차확인되면 CONFIRMED_SINGLE_LINE으로 승격한다.
    어느 경로에서든 2개 이상 노선이 확인되면 CONFIRMED_TRANSFER가 우선한다.
    """
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
            "transfer": None, "transfer_status": "UNRESOLVED",
            "key_configured": False, "credential_env": None,
            "build_marker": STATION_RUNTIME_BUILD_MARKER,
        }

    errors: List[str] = []
    global_lines: List[str] = []
    global_transfer_status = ""
    global_sources: List[str] = []
    try:
        ref = _seoul_station_line_reference(force=force)
        row = next((x for x in ref.get("stations", []) if _name_key(_normalize_station_public_name(x.get("name"))) == cache_key), None)
        if row:
            global_lines = sorted(set(_normalize_subway_line_name(x) for x in (row.get("lines") or []) if _normalize_subway_line_name(x)))
            global_transfer_status = str(row.get("transfer_status") or "")
            global_sources = list(row.get("sources") or [])
    except Exception as exc:
        errors.append(f"global reference: {exc}")

    direct_lines: List[str] = []
    direct_query_ok = False
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
            direct_query_ok = True
            for row in body.get("row") or []:
                if not isinstance(row, dict):
                    continue
                if _name_key(_normalize_station_public_name(row.get("STATION_NM"))) != cache_key:
                    continue
                ln = _normalize_subway_line_name(row.get("LINE_NUM"))
                if ln:
                    direct_lines.append(ln)
    except Exception as exc:
        errors.append(f"SearchInfoBySubwayNameService: {exc}")

    direct_lines = sorted(set(direct_lines))
    lines = sorted(set(global_lines + direct_lines))
    transfer: Optional[bool] = None
    transfer_status = "UNRESOLVED"
    confirmation_basis = ""

    if len(lines) >= 2 or global_transfer_status == "CONFIRMED_TRANSFER":
        transfer = True
        transfer_status = "CONFIRMED_TRANSFER"
        confirmation_basis = "공식 노선자료에서 2개 이상 노선 확인"
    elif len(lines) == 1:
        if global_transfer_status == "CONFIRMED_SINGLE_LINE":
            transfer = False
            transfer_status = "CONFIRMED_SINGLE_LINE"
            confirmation_basis = "광역 보강자료 포함 공식 노선표에서 단일노선 확인"
        elif direct_query_ok and len(global_lines) == 1 and len(direct_lines) == 1 and global_lines[0] == direct_lines[0]:
            transfer = False
            transfer_status = "CONFIRMED_SINGLE_LINE"
            confirmation_basis = "공식 전체노선표와 역명 직접조회가 동일 단일노선으로 교차확인"
        else:
            confirmation_basis = "단일 소스 또는 교차확인 미완료"

    result = {
        "status": "ok" if lines else ("partial" if direct_query_ok or global_lines else "error"),
        "name": nm + "역",
        "lines": lines,
        "line_count": len(lines),
        "transfer": transfer,
        "transfer_status": transfer_status,
        "confirmation_basis": confirmation_basis,
        "global_lines": global_lines,
        "direct_lines": direct_lines,
        "global_transfer_status": global_transfer_status,
        "global_sources": global_sources,
        "direct_query_ok": direct_query_ok,
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


@app.get("/api/reference/route-commercial")
def reference_route_commercial():
    """역세권활성화 간선가로형 내부 판정모형용 노선형 상업지역 참조도형."""
    return _route_commercial_reference_data()




@app.get("/api/reference/regulation-change-monitor")
def reference_regulation_change_monitor():
    return _regulation_change_monitor_data()


@app.get("/api/reference/safe-downtown-exclusion")
def reference_safe_downtown_exclusion():
    return _safe_downtown_exclusion_reference_data()


def _reference_data_readiness() -> Dict[str, bool]:
    return {
        "stations": os.path.isfile(_data_path("stations.json")),
        "centers": os.path.isfile(_data_path("centers.json")),
        "station_entrances": os.path.isfile(_data_path("station_entrances.json")),
        "renewal_legal": os.path.isfile(_data_path("uq181_legal.zip")),
        "renewal_project": os.path.isfile(_data_path("uq120_project.zip")),
        "safe_medical": os.path.isfile(_data_path("safe_medical_reference.json")),
        "biotope": os.path.isfile(_data_path("biotope_seoul.zip")),
        "public_forest": os.path.isfile(_data_path("forest_classification_seoul_202608.zip")),
        "school_protection": os.path.isfile(_data_path("school_protection_seoul_202608.zip")),
        "route_commercial_model": os.path.isfile(_data_path("route_commercial_reference.geojson")),
        "urban_regeneration_innovation_shp": _urban_regen_innovation_shp_ready(),
        "regulation_change_monitor": os.path.isfile(_data_path("regulation_change_monitor.json")),
        "safe_downtown_exclusion_model": os.path.isfile(_data_path("safe_downtown_exclusion_reference.geojson")),
        "hill_terrain_model": all(os.path.isfile(_data_path(x)) for x in (HILL_GRID_META_FILE,HILL_GRID_ELEV_FILE,HILL_GRID_SLOPE_FILE)),
        "basic_unit": bool(_basic_unit_zip_path()),
    }


@app.get("/health")
def health():
    reference_data = _reference_data_readiness()
    return {
        "ok": True,
        "app": "seoul_urban_renewal_platform_v2.5.0",
        "engine": "site_fact_store_v2.5.0_r11",
        "map": "leaflet-draw",
        "vworld_configured": vworld_ready(),
        "vworld_client_configured": bool(_vworld_client_key()),
        "vworld_client_key_source": "VWORLD_CLIENT_KEY" if (os.getenv("VWORLD_CLIENT_KEY") or "").strip() else ("VWORLD_API_KEY" if _vworld_key() else None),
        "planning_browser_fallback_patch_marker": "R23_SERVER_FIRST_BROWSER_FALLBACK_20260923",
        "local_first_fact_status_patch_marker": "R25_LOCAL_FIRST_FACT_STATUS_20260923",
        "land_ledger_local_fallback_patch_marker": "R28_VWORLD_THEN_AL_D003_20260923",
        "build_marker": APP_BUILD_MARKER,
        "pipeline_patch_marker": "R18_PIPELINE_STABILIZATION_20260910",
        "regulatory_disaster_vworld_patch_marker": "R15_DISASTER_BUNDLES_VWORLD_DOMAIN_DIAGNOSTICS_20260921",
        "urban_regeneration_source_marker": "GIMPO_SHP_INTEGRATED_PROJECT_FACT_R3_20260918",
        "station_runtime_build_marker": STATION_RUNTIME_BUILD_MARKER,
        "seoul_open_data_configured": bool(_seoul_open_data_key()),
        "seoul_open_data_env": _seoul_open_data_key_info()[1] or None,
        "seoul_env_names_detected": sorted([k for k in os.environ if "seoul" in k.lower() or "data.seoul" in k.lower()]),
        "analytics_storage": _analytics_storage_mode(),
        "admin_configured": bool(os.getenv("ADMIN_PASSWORD", "")),
        "vworld_domain": _vworld_domain() if vworld_ready() else None,
        "vworld_circuit": _vworld_circuit_snapshot(),
        "external_circuit_patch_marker": "R22_VWORLD_GLOBAL_CIRCUIT_20260922",
        "parcel_auto": "browser_direct_ready" if vworld_ready() else "needs_VWORLD_API_KEY",
        "building_spatial_auto": "LT_C_SPBD_browser_direct_ready" if vworld_ready() else "needs_VWORLD_API_KEY",
        "building_hub": "ready" if building_hub_ready() else "needs_BUILDING_HUB_API_KEY",
        "land_ledger": "browser VWorld NED live first; bundled Seoul AL_D003 local snapshot fallback; existing server VWorld remains last fallback",
        "land_ledger_browser_fallback": True,
        "land_ledger_local_snapshot": _land_ledger_local_snapshot_status(),
        "land_price_local_fallback_patch_marker": "R32_VWORLD_THEN_LOCAL_OFFICIAL_LAND_PRICE_20260923",
        "official_land_price": "VWorld NED live first; bundled Seoul official land-price snapshot exact-year fallback; year mismatch remains REVIEW",
        "land_price_local_snapshot": _land_price_local_snapshot_status(),
        "road_access": "bundled TL_SPRD_MANAGE + ROAD_BT first; VWorld browser fallback; missing Fact remains REVIEW",
        "road_bundled_configured": bool(_road_zip_path()),
        "reference_data": reference_data,
        "reference_data_missing": [k for k, v in reference_data.items() if not v],
        "analysis_reference_ready": all(reference_data.get(k, False) for k in ("stations", "centers", "renewal_legal", "renewal_project")),
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
        "planning_gis": "VWorld zoning/district/facility/district-unit-plan polygon intersection engine; server first, browser JSONP fallback on server/upstream failure",
        "vworld_planning_domain_policy": "browser origin accepted only when request Host matches; fallback VWORLD_DOMAIN/RENDER_EXTERNAL_HOSTNAME",
        "disaster_bundled_landslide_raster": os.path.isfile(LANDSLIDE_RISK_RLE_PATH) and os.path.isfile(LANDSLIDE_RISK_META_PATH),
        "disaster_bundled_risk_district": os.path.isfile(NATURAL_DISASTER_RISK_DISTRICT_ZIP),
        "renewal_gis": "server-side UQ181/UQ120 intersection; legal-priority; promotion separate; full matched boundaries returned for status map",
        "development_gis": "VWorld district-unit plan + bundled Seoul UQ181 legal projects + VWorld LT_C_DAMDAN industrial-park boundaries",
        "safe_housing_location_paths": "station / arterial-road-side / medical-facility-center evaluated separately; OR combined",
        "safe_medical_reference": "packaged official TbHospitalInfo monthly snapshot + official Seoul municipal hospitals/25 district health centers; offline 2020-12 representative parcel first; unresolved candidates can use browser VWorld fallback; 350m buffer",
        "safe_medical_local_first": True,
        "flood_reference_sources": {"expected_dataset_page": SEOUL_FLOOD_EXPECTED_DATASET_PAGE, "trace_dataset_page": SEOUL_FLOOD_TRACE_2025_DATASET_PAGE, "expected_direct_url_configured": bool(SEOUL_FLOOD_EXPECTED_URL), "trace_direct_url_configured": bool(SEOUL_FLOOD_TRACE_2025_URL)},
        "ecvam_reference": {"configured": _ecvam_configured(), "bootstrap_url": ECVAM_API_CONFIRM_URL, "endpoint_policy": "official apiConfirm bootstrap -> discovered WMS endpoint; compatibility fallback only"},
        "safe_medical_key_env": _seoul_open_data_key_info()[1] or None,
        "road_width_gis": "VWorld TL_SPRD_MANAGE ROAD_BT is the sole road-width Fact source",
        "street_block_gis": "SGIS 2025 basic-unit seed + shared TL_SPRD_MANAGE ROAD_BT geometry with scheme-specific street-block rules: smallscale 6m existing roads + all urban-planning facility roads + statutory facilities, activation/station-complex 4m + nonbuildable facilities, growth-potential all roads + defined facilities; ESTIMATE until authoritative official block data is connected",
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
        "hill_terrain_fact": "Seoul 1:5,000 contour + spot-height official source -> platform-derived 20m terrain grid; 40m elevation / 10deg terrain-slope reference layers; not an official hill polygon",
        "hill_terrain_file": HILL_GRID_META_FILE if reference_data.get("hill_terrain_model") else None,
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


@app.get("/api/reference/heritage-wms-map")
def heritage_wms_map(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    width: int = 760,
    height: int = 520,
):
    """Display-only proxy for the National Heritage Spatial Information WMS.

    r48 UI addition.  Analysis/decision Facts remain the existing VWorld
    LT_C_UO301 vector intersections.  The WMS image is only a visual
    cross-check layer, so a WMS outage never becomes a PASS/FAIL Fact.

    The public WMS request labels its local Korea 2000 unified coordinates as
    EPSG:9020203.  Those numeric coordinates correspond to the EPSG:5179
    coordinate space used here for bbox transformation.
    """
    if not (-180 <= min_lon < max_lon <= 180 and -90 <= min_lat < max_lat <= 90):
        raise HTTPException(status_code=400, detail="invalid WGS84 bbox")
    width = max(320, min(int(width), 1200))
    height = max(220, min(int(height), 900))
    try:
        tf = Transformer.from_crs(4326, 5179, always_xy=True)
        pts = [
            tf.transform(min_lon, min_lat),
            tf.transform(min_lon, max_lat),
            tf.transform(max_lon, min_lat),
            tf.transform(max_lon, max_lat),
        ]
        xs = [x for x, _ in pts]
        ys = [y for _, y in pts]
        bbox = f"{min(xs):.3f},{min(ys):.3f},{max(xs):.3f},{max(ys):.3f}"
        params = {
            "domain": "https://gis-heritage.go.kr/",
            "service": "WMS",
            "version": "1.3.0",
            "request": "GetMap",
            "LAYERS": "TB_ODTR_MID,TB_OUSR_MID,TB_MDQT_MID,TB_MUSQ_MID,TB_HRNR_MID,TB_SHOV_MID,TB_ERHT_MID,TB_THFS_MID",
            "styles": "default,default,default,default,default,default,default,default",
            "bBox": bbox,
            "width": str(width),
            "height": str(height),
            "format": "image/png",
            "crs": "EPSG:9020203",
            "exceptions": "INIMAGE",
        }
        r = requests.get(
            "https://gis-heritage.go.kr/checkKey.do",
            params=params,
            timeout=12,
            headers={
                "User-Agent": "urban-strategy/2.5.0 heritage-WMS-display",
                "Referer": "https://gis-heritage.go.kr/",
                "Accept": "image/png,image/*;q=0.8,*/*;q=0.5",
            },
        )
        content_type = str(r.headers.get("content-type") or "").lower()
        if r.status_code != 200 or not r.content:
            raise HTTPException(status_code=502, detail=f"heritage WMS HTTP {r.status_code}")
        # Some WMS servers return an exception image with image/png; show it so
        # the map itself communicates the source-side problem.  Non-image text
        # is not passed through to the browser as a map.
        if "image" not in content_type and not r.content.startswith(b"\x89PNG"):
            raise HTTPException(status_code=502, detail="heritage WMS non-image response")
        return Response(
            content=r.content,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=900"},
        )
    except HTTPException:
        raise
    except Exception as exc:
        logging.warning("heritage WMS proxy failed: %s", exc)
        raise HTTPException(status_code=502, detail="heritage WMS unavailable") from exc


@app.post("/api/spatial/safe-downtown-exclusion")
def safe_downtown_exclusion_intersections(inp: GeometryInput):
    try:
        return _safe_downtown_exclusion_analysis(inp.geometry)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logging.exception("safe downtown exclusion intersection failed")
        raise HTTPException(status_code=500, detail=f"안심주택 서울도심 배제범위 분석 오류: {exc}") from exc


@app.get("/api/reference/hill-status")
def hill_status():
    fc=_hill_reference_data()
    return fc.get('metadata') or {}


@app.post("/api/spatial/hill-intersections")
def hill_intersections(inp: GeometryInput):
    """서울시 공식 등고선·표고점 기반 플랫폼 지형참조와 대상구역을 분석합니다."""
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
                _hill_grid_arrays.cache_clear(); _hill_grid_meta.cache_clear()
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


@app.post("/api/reference/district-unit-plan-current")
def district_unit_plan_current_reference(inp: DistrictUnitPlanReferenceInput):
    """지구단위계획 CURRENT PLAN 공식 참조정보를 관리코드로 연결한다.

    공간중첩 판정은 기존 LT_C_UPISUQ161 결과가 담당한다. 이 endpoint는
    서울 열린데이터광장의 UQ161 속성/지구단위계획 조서를 참조해 구역명·위치·
    면적·결정고시관리코드를 연결할 뿐이며 사업 PASS/FAIL/REVIEW를 변경하지 않는다.
    """
    try:
        return _district_unit_reference_lookup(list(inp.hits or []))
    except Exception as exc:
        logging.exception("district unit plan current reference failed")
        return {
            "status": "ERROR", "known": False, "matches": [], "announcement_codes": [],
            "message": str(exc)[:300], "source_type": "SEOUL_OPEN_DATA_REFERENCE_ONLY",
            "license": "UQ161_KOGL_TYPE_1__DIST_UNIT_PLAN_KOGL_TYPE_4",
            "services": ["upisCUq161", "upisDistUnitPlan"],
            "effect_on_scheme_status": "NONE",
        }


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


@app.get("/api/spatial/school-absolute-protection-data-status")
def school_absolute_protection_data_status():
    """내장 UO101 UOA110 학교 절대보호구역 SHP 로드상태 진단."""
    path = _school_absolute_protection_zip_path()
    if not path:
        return {"available": False, "fact_status": "MISSING", "message": "학교 절대보호구역 원본 없음"}
    layers = _school_absolute_protection_layers()
    if layers.get("available"):
        return {
            "available": True,
            "fact_status": "SCHOOL_ABSOLUTE_PROTECTION_READY",
            "message": f"UOA110 절대보호구역 {layers.get('count', 0)}건 사용 가능",
            "file": layers.get("file"),
            "source": layers.get("source"),
            "repaired_count": layers.get("repaired_count", 0),
        }
    return {"available": False, "fact_status": "LOAD_FAILED", "message": str(layers.get("reason") or "학교 절대보호구역 SHP 로드 실패")}


@app.post("/api/spatial/school-absolute-protection-intersections")
def school_absolute_protection_intersections(inp: GeometryInput):
    """UO101 UOA110 학교 절대보호구역과 대상지를 실제 공간교차한다."""
    try:
        return analyze_school_absolute_protection_intersections(inp.geometry)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logging.exception("school absolute protection intersection failed")
        raise HTTPException(status_code=500, detail=f"학교 절대보호구역 중첩분석 오류: {exc}") from exc


@app.post("/api/spatial/school-protection-intersections")
def school_protection_intersections(inp: GeometryInput):
    """규제정보용 UO101 UOA110/UOA120 대상지 중첩 분석. 기존 안심주택 판정과 독립."""
    try:
        return analyze_school_protection_intersections(inp.geometry)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logging.exception("school protection intersection failed")
        raise HTTPException(status_code=500, detail=f"학교 교육환경보호구역 중첩분석 오류: {exc}") from exc


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


@app.post("/api/spatial/road-facts")
def road_facts(inp: RoadFactInput):
    """서울 원본 RW 실폭도로 + MANAGE ROAD_BT의 대상지 주변 다대다 도로 FACT."""
    try:
        return analyze_local_road_facts(inp.geometry, inp.radius_m)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logging.exception("road fact analysis failed")
        raise HTTPException(status_code=500, detail=f"도로 FACT 분석 실패: {exc}") from exc

@app.post("/api/spatial/street-block-batch")
def street_block_batch(inp: StreetBlockBatchInput):
    """동일 도로 FACT를 공유하는 여러 제도 가로구역을 공통 전처리 1회로 순차 산정한다."""
    try:
        scheme_dict = {
            key: {
                'barrier_features': value.barrier_features,
                'road_min_width_m': value.road_min_width_m,
                'outer_closure_all_roads': value.outer_closure_all_roads,
            }
            for key, value in inp.schemes.items()
        }
        return analyze_street_block_batch(
            inp.geometry, inp.road_features, inp.road_surface_features, inp.road_area_features,
            scheme_dict, inp.max_radius_m,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logging.exception("street block batch analysis failed")
        raise HTTPException(status_code=500, detail=f"가로구역 batch 자동추출 오류: {exc}") from exc


@app.post("/api/spatial/street-block")
def street_block(inp: StreetBlockInput):
    """선택 사업지를 seed로 주변 기초단위구를 확장해 제도별 가로구역 후보를 찾습니다.

    내부도로의 분리 임계값(4m/6m)은 요청별로 받고, outer_closure_all_roads=True이면
    1차 제도별 블록 외곽경계에 실제로 닿는 TL_SPRD_RW 도로면은 폭원과 무관하게 2차 폐합경계로 사용합니다.
    """
    try:
        return analyze_street_block(
            inp.geometry, inp.barrier_features, inp.road_features, inp.max_radius_m,
            inp.road_surface_features, inp.road_area_features, inp.road_min_width_m,
            inp.outer_closure_all_roads,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logging.exception("street block analysis failed")
        raise HTTPException(status_code=500, detail=f"가로구역 자동추출 오류: {exc}") from exc


@app.get("/api/vworld/test")
def vworld_test(layer: str = "LT_C_UQ111", lon: float = 126.978, lat: float = 37.566):
    """Single-layer connection diagnostic; no credentials or raw response body."""
    if layer not in {"LT_C_UQ111", VWORLD_LAYER_PARCEL, "TL_SPRD_MANAGE"}:
        raise HTTPException(status_code=422, detail="진단 지원 레이어가 아닙니다.")
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise HTTPException(status_code=422, detail="유효한 경위도가 필요합니다.")
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
        "data": layer,
        "geomfilter": f"POINT({lon},{lat})",
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
            result["response_format"] = "NON_JSON"
        result["transport"] = _last_vworld_diagnostic()
        result["layer"] = layer
        return result
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"message": "VWorld 단일 조회 실패 · 미확인", "transport": _last_vworld_diagnostic()}) from exc

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

    # Browser-first client already tried live VWorld NED.  In that case do not
    # spend another Render->VWorld round before using the bundled AL_D003 snapshot.
    # Other callers keep the historical live-server-first resolver, whose final
    # fallback is also the same local snapshot.
    if inp.browser_live_attempted:
        local_record = _local_land_ledger_lookup(pnu)
        if local_record is not None:
            resolved = {
                "record": local_record,
                "dataset": "토지임야정보(속성정보) 보유자료",
                "operation": "PNU_LOCAL_LOOKUP",
                "selected_source": "local_AL_D003_snapshot",
                "attempts": [{"source": "local_AL_D003_snapshot", "ok": True, "completed_ms": 0.0}],
                "elapsed_ms": 0.0,
                "cache_hit": False,
                "external_upstream_error": bool(_vworld_circuit_snapshot().get("open")),
                "vworld_circuit": _vworld_circuit_snapshot(),
            }
        else:
            resolved = _resolve_land_ledger(pnu)
    else:
        resolved = _resolve_land_ledger(pnu)
    record = resolved.get("record")
    dataset = str(resolved.get("dataset") or "토지임야정보(속성정보)")
    operation = str(resolved.get("operation") or "ladfrlList")

    return {
        "pnu": pnu,
        "record": record,
        "vworld_ready": bool(_vworld_key()),
        "cache_hit": bool(resolved.get("cache_hit")),
        "selected_source": resolved.get("selected_source"),
        "local_snapshot_used": resolved.get("selected_source") == "local_AL_D003_snapshot",
        "local_snapshot_date": (record or {}).get("_source_date") if isinstance(record, dict) else None,
        "elapsed_ms": resolved.get("elapsed_ms"),
        "attempts": resolved.get("attempts") or [],
        "external_upstream_error": bool(resolved.get("external_upstream_error")),
        "vworld_circuit": resolved.get("vworld_circuit") or _vworld_circuit_snapshot(),
        "source": {
            "provider": "국토교통부",
            "dataset": dataset,
            "operation": operation,
            "portal_modified": "2025-07-01",
        },
    }


@app.post("/api/land/characteristics-one")
def land_characteristics_one(inp: LandLedgerOneInput):
    """Server proxy for VWorld getLandCharacteristics.

    Kept as a separate endpoint so any future client path can obtain official
    area/category data without ever attempting a browser CORS request.
    """
    pnu = str(inp.pnu or "").strip()
    if len(pnu) != 19 or not pnu.isdigit():
        raise HTTPException(status_code=422, detail="PNU는 19자리 숫자여야 합니다.")
    record = _server_land_characteristics_vworld(pnu)
    return {
        "pnu": pnu,
        "record": record,
        "vworld_ready": bool(_vworld_key()),
        "source": {
            "provider": "국토교통부",
            "dataset": "토지특성정보",
            "operation": "getLandCharacteristics",
            "portal_modified": "2025-07-01",
        },
    }


@app.post("/api/land/official-price-batch")
def land_official_price_batch(inp: LandPriceBatchInput):
    """Selected-parcel official land price FACT for renewal-business feasibility.

    Source order is intentionally fixed: live VWorld NED first, then the
    bundled Seoul official-land-price snapshot for the *same requested year*.
    A different year is never substituted because the business-feasibility
    coefficient requires numerator and denominator to share the reference year.
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

    rows: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    max_workers = min(2, len(pnus))
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="land-price") as pool:
        future_map = {
            pool.submit(_resolve_individual_land_price, pnu, int(inp.year), 8): pnu
            for pnu in pnus
        }
        for fut in as_completed(future_map):
            pnu = future_map[fut]
            try:
                rec = fut.result()
            except Exception as exc:
                rec = None
                errors.append({"pnu": pnu, "error": str(exc)[:240]})
            if rec:
                rows.append(rec)
            elif not any(e.get("pnu") == pnu for e in errors):
                errors.append({
                    "pnu": pnu,
                    "error": f"{int(inp.year)}년 개별공시지가 미확보 (VWorld 실시간 + 보유자료 동일연도 조회)",
                })

    rows.sort(key=lambda r: str(r.get("pnu") or ""))
    live_count = sum(1 for r in rows if r.get("_source_type") == "VWORLD_LIVE")
    local_count = sum(1 for r in rows if r.get("_source_type") == "LOCAL_SNAPSHOT")
    return {
        "year": int(inp.year),
        "requested": len(pnus),
        "resolved": len(rows),
        "resolved_live": live_count,
        "resolved_local": local_count,
        "rows": rows,
        "errors": errors,
        "source": {
            "provider": "국토교통부 / VWorld NED + 보유 공식자료",
            "dataset": "개별공시지가정보",
            "operation": "getIndvdLandPriceAttr -> exact-year local fallback",
            "field": "pblntfPclnd / price_per_m2",
            "vworld_first": True,
            "local_snapshot": _land_price_local_snapshot_status(),
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
    with ThreadPoolExecutor(max_workers=min(2, len(pnus))) as pool:
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



# -----------------------------------------------------------------------------
# R12: 재해규제 독립분석용 서울시 공식 침수공간자료
# - 서울 열린데이터광장 공개 ZIP을 최초 요청 시 /tmp에 캐시한다.
# - 서비스 장애/파일구조 변경 시 ERROR로 돌려 UNKNOWN을 유지하며 비해당으로 오판하지 않는다.
# - 침수예상도는 위험예측 FACT, 침수흔적도는 과거 발생이력 FACT로 서로 구분한다.
# -----------------------------------------------------------------------------
# R25: 서울 열린데이터광장의 bigfile 직접-download URL은 seq 값이 바뀌는
# 비영구 링크다.  2026-09-23 재검증 결과 데이터셋 페이지와 파일 자체는
# 공개 중이지만 기존 nio_download 고정 URL은 HTML/오류 응답을 반환했다.
# 운영 중에는 검증된 직접 URL을 환경변수로만 주입하고, 코드에는 안정적인
# 공식 데이터셋 landing page를 provenance로 보존한다.
SEOUL_FLOOD_EXPECTED_DATASET_PAGE = "https://data.seoul.go.kr/dataList/OA-21172/A/1/datasetView.do"
SEOUL_FLOOD_TRACE_2025_DATASET_PAGE = "https://data.seoul.go.kr/dataList/OA-15636/F/1/datasetView.do"
SEOUL_FLOOD_EXPECTED_URL = (os.getenv("SEOUL_FLOOD_EXPECTED_URL") or "").strip()
SEOUL_FLOOD_TRACE_2025_URL = (os.getenv("SEOUL_FLOOD_TRACE_2025_URL") or "").strip()
_DISASTER_DOWNLOAD_LOCK = threading.Lock()


def _download_official_zip(url: str, cache_name: str, *, source_page: str = "") -> str:
    cache_dir = os.path.join("/tmp", "urban_strategy_disaster")
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, cache_name)
    if os.path.isfile(path) and os.path.getsize(path) > 100 and zipfile.is_zipfile(path):
        return path
    if not str(url or "").strip():
        raise RuntimeError(
            "공식 데이터셋은 공개 중이나 직접 다운로드 URL이 동적입니다. "
            f"검증된 직접 URL을 환경변수로 설정하세요{(' · '+source_page) if source_page else ''}"
        )
    with _DISASTER_DOWNLOAD_LOCK:
        if os.path.isfile(path) and os.path.getsize(path) > 100 and zipfile.is_zipfile(path):
            return path
        tmp = path + ".part"
        try:
            resp = requests.get(url, timeout=45, headers={"User-Agent": "urban-strategy/2.5 official-public-data"})
            resp.raise_for_status()
            with open(tmp, "wb") as fp:
                fp.write(resp.content)
            if not zipfile.is_zipfile(tmp):
                raise RuntimeError(
                    f"공식 ZIP 응답 형식 오류 · content-type={resp.headers.get('content-type','')} "
                    "· 직접 다운로드 URL 갱신 필요"
                )
            os.replace(tmp, path)
        finally:
            if os.path.isfile(tmp):
                try: os.remove(tmp)
                except OSError: pass
    return path


def _zip_shapefile_stems(zf: zipfile.ZipFile) -> List[str]:
    names = zf.namelist()
    stems=[]
    for n in names:
        if not n.lower().endswith('.shp'):
            continue
        stem=os.path.splitext(n)[0]
        if any(os.path.splitext(x)[0]==stem and x.lower().endswith('.dbf') for x in names) and any(os.path.splitext(x)[0]==stem and x.lower().endswith('.shx') for x in names):
            stems.append(stem)
    return stems


def _analyze_remote_polygon_zip(geometry: Dict[str, Any], *, url: str, cache_name: str, source_label: str, default_epsg: int = 5186, source_page: str = "") -> Dict[str, Any]:
    site=shape(geometry)
    if site.geom_type not in {"Polygon","MultiPolygon"} or site.is_empty:
        raise ValueError("유효한 Polygon 또는 MultiPolygon 구역계가 필요합니다.")
    if not site.is_valid:
        site=site.buffer(0)
    if site.is_empty or not site.is_valid:
        raise ValueError("유효하지 않은 구역계입니다.")
    path=_download_official_zip(url, cache_name, source_page=source_page)
    context_features=[];overlap_features=[];overlap_geoms=[];source_files=[];candidate_count=0
    with zipfile.ZipFile(path) as zf:
        names=zf.namelist();stems=_zip_shapefile_stems(zf)
        if not stems:
            raise RuntimeError("공식 ZIP에서 SHP/SHX/DBF 세트를 찾지 못했습니다.")
        for stem in stems:
            shp=next(n for n in names if os.path.splitext(n)[0]==stem and n.lower().endswith('.shp'))
            shx=next(n for n in names if os.path.splitext(n)[0]==stem and n.lower().endswith('.shx'))
            dbf=next(n for n in names if os.path.splitext(n)[0]==stem and n.lower().endswith('.dbf'))
            prj=next((n for n in names if os.path.splitext(n)[0]==stem and n.lower().endswith('.prj')),None)
            source_crs=CRS.from_epsg(default_epsg)
            if prj:
                try: source_crs=CRS.from_wkt(zf.read(prj).decode('utf-8',errors='ignore'))
                except Exception: pass
            to_source=Transformer.from_crs(4326,source_crs,always_xy=True).transform
            to_wgs=Transformer.from_crs(source_crs,4326,always_xy=True).transform
            site_src=geometry_transform(to_source,site)
            reader=shapefile.Reader(shp=io.BytesIO(zf.read(shp)),shx=io.BytesIO(zf.read(shx)),dbf=io.BytesIO(zf.read(dbf)),encoding='cp949',encodingErrors='replace')
            fields=[f[0] for f in reader.fields[1:]]
            source_files.append(os.path.basename(shp))
            for sr in reader.iterShapeRecords(bbox=list(site_src.bounds)):
                try:
                    candidate_count+=1
                    props={k:_json_property(v) for k,v in zip(fields,list(sr.record))}
                    gsrc=shape(sr.shape.__geo_interface__)
                    if gsrc.is_empty: continue
                    if not gsrc.is_valid: gsrc=gsrc.buffer(0)
                    if gsrc.is_empty or not gsrc.intersects(site_src): continue
                    gwgs=geometry_transform(to_wgs,gsrc)
                    if gwgs.is_empty or not gwgs.intersects(site): continue
                    inter=_polygonal_only(site.intersection(gwgs))
                    if inter is None or inter.is_empty: continue
                    props['_source_file']=os.path.basename(shp)
                    context_features.append({'type':'Feature','geometry':mapping(gwgs),'properties':props})
                    overlap_features.append({'type':'Feature','geometry':mapping(inter),'properties':props})
                    overlap_geoms.append(inter)
                except Exception:
                    continue
    to_metric=Transformer.from_crs(4326,5174,always_xy=True).transform
    site_area=float(geometry_transform(to_metric,site).area)
    union=unary_union(overlap_geoms) if overlap_geoms else None
    area=float(geometry_transform(to_metric,union).area) if union is not None and not union.is_empty else 0.0
    return {
        'status':'matched' if overlap_features else 'none','known':True,'present':bool(overlap_features),
        'overlap_area_m2':area,'overlap_pct':(area/site_area*100.0) if site_area>0 else None,
        'feature_count':len(context_features),'features':context_features,'overlap_features':overlap_features,
        'bbox_candidate_count':candidate_count,'source':source_label,'source_type':'OFFICIAL_REMOTE_SHP',
        'source_files':source_files,'cache_file':os.path.basename(path),'source_page':source_page,
    }


# R15: 사용자 제공 공식 재해 원자료 번들
# - 자연재해위험개선지구: LSMD_CONT_UP201 서울 202609 SHP 원도형
# - 산사태위험지도: 2026 산불추가 서울 10m TIFF를 무손실 row-RLE로 변환한 파생자료
#   (원본 11.zip도 함께 보존). 값 127은 '위험 없음'이 아니라 NoData이다.
NATURAL_DISASTER_RISK_DISTRICT_ZIP = _data_path("source_natural_disaster_risk_district_seoul_202609.zip")
LANDSLIDE_RISK_RLE_PATH = _data_path("landslide_risk_2026_seoul_10m_rle.zlib")
LANDSLIDE_RISK_META_PATH = _data_path("landslide_risk_2026_seoul_10m_meta.json")
LANDSLIDE_RISK_SOURCE_ZIP = _data_path("source_landslide_risk_2026_seoul_11.zip")


def _analyze_local_polygon_zip(
    geometry: Dict[str, Any], *, path: str, source_label: str, default_epsg: int = 5186,
    include_codes: Optional[List[str]] = None, code_field: str = "MNUM",
    group_field: Optional[str] = None, scope_note: Optional[str] = None,
    detail: Optional[str] = None,
) -> Dict[str, Any]:
    """Bundled official SHP ZIP intersection analyzer.

    include_codes filters records by exact/prefix code (e.g. UJB100); group_field additionally
    reports non-overlapping-by-value area distribution for the selected site. This is used only
    as FACT geometry and never as an automatic scheme PASS/FAIL switch.
    """
    site = shape(geometry)
    if site.geom_type not in {"Polygon", "MultiPolygon"} or site.is_empty:
        raise ValueError("유효한 Polygon 또는 MultiPolygon 구역계가 필요합니다.")
    if not site.is_valid:
        site = site.buffer(0)
    if site.is_empty or not site.is_valid:
        raise ValueError("유효하지 않은 구역계입니다.")
    if not os.path.isfile(path) or not zipfile.is_zipfile(path):
        raise RuntimeError(f"번들 SHP ZIP을 찾지 못했습니다: {os.path.basename(path)}")

    code_filters = [str(v).strip().upper() for v in (include_codes or []) if str(v).strip()]
    context_features: List[Dict[str, Any]] = []
    overlap_features: List[Dict[str, Any]] = []
    overlap_geoms = []
    grouped_geoms: Dict[str, List[Any]] = {}
    source_files: List[str] = []
    candidate_count = 0
    blank_attribute_counts = {"NTFDATE": 0, "ALIAS": 0, "REMARK": 0}
    total_records = 0
    matched_source_records = 0
    record_errors = 0
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        stems = _zip_shapefile_stems(zf)
        if not stems:
            raise RuntimeError("번들 ZIP에서 SHP/SHX/DBF 세트를 찾지 못했습니다.")
        for stem in stems:
            shp = next(n for n in names if os.path.splitext(n)[0] == stem and n.lower().endswith('.shp'))
            shx = next(n for n in names if os.path.splitext(n)[0] == stem and n.lower().endswith('.shx'))
            dbf = next(n for n in names if os.path.splitext(n)[0] == stem and n.lower().endswith('.dbf'))
            prj = next((n for n in names if os.path.splitext(n)[0] == stem and n.lower().endswith('.prj')), None)
            source_crs = CRS.from_epsg(default_epsg)
            if prj:
                try:
                    source_crs = CRS.from_wkt(zf.read(prj).decode('utf-8', errors='ignore'))
                except Exception:
                    pass
            to_source = Transformer.from_crs(4326, source_crs, always_xy=True).transform
            to_wgs = Transformer.from_crs(source_crs, 4326, always_xy=True).transform
            site_src = geometry_transform(to_source, site)
            reader = shapefile.Reader(
                shp=io.BytesIO(zf.read(shp)), shx=io.BytesIO(zf.read(shx)), dbf=io.BytesIO(zf.read(dbf)),
                encoding='cp949', encodingErrors='replace'
            )
            fields = [f[0] for f in reader.fields[1:]]
            if code_filters and code_field not in fields:
                raise RuntimeError(f"SHP 필수 속성 누락: {code_field}")
            total_records += len(reader)
            source_files.append(os.path.basename(shp))
            for sr in reader.iterShapeRecords(bbox=list(site_src.bounds)):
                try:
                    candidate_count += 1
                    props = {k: _json_property(v) for k, v in zip(fields, list(sr.record))}
                    if code_filters:
                        raw_code = str(props.get(code_field) or '').strip().upper()
                        if not any(raw_code == c or raw_code.startswith(c) or c in raw_code for c in code_filters):
                            continue
                    matched_source_records += 1
                    for key in blank_attribute_counts:
                        if key in props and not str(props.get(key) or '').strip():
                            blank_attribute_counts[key] += 1
                    gsrc = shape(sr.shape.__geo_interface__)
                    if gsrc.is_empty:
                        continue
                    if not gsrc.is_valid:
                        gsrc = gsrc.buffer(0)
                    if gsrc.is_empty or not gsrc.intersects(site_src):
                        continue
                    gwgs = geometry_transform(to_wgs, gsrc)
                    inter = _polygonal_only(site.intersection(gwgs))
                    if inter is None or inter.is_empty:
                        continue
                    props['_source_file'] = os.path.basename(shp)
                    context_features.append({'type': 'Feature', 'geometry': mapping(gwgs), 'properties': props})
                    overlap_features.append({'type': 'Feature', 'geometry': mapping(inter), 'properties': props})
                    overlap_geoms.append(inter)
                    if group_field:
                        key = str(props.get(group_field) if props.get(group_field) is not None else '미기재').strip() or '미기재'
                        grouped_geoms.setdefault(key, []).append(inter)
                except Exception:
                    record_errors += 1
                    continue
    if not source_files or total_records == 0:
        raise RuntimeError("번들 SHP에 검증 가능한 레코드가 없습니다.")
    if record_errors:
        raise RuntimeError(f"번들 SHP 후보 {record_errors}건 처리 실패 · 비중첩 확정 금지")
    to_metric = Transformer.from_crs(4326, 5174, always_xy=True).transform
    site_area = float(geometry_transform(to_metric, site).area)
    union = unary_union(overlap_geoms) if overlap_geoms else None
    area = float(geometry_transform(to_metric, union).area) if union is not None and not union.is_empty else 0.0
    distribution = []
    for value, geoms in sorted(grouped_geoms.items(), key=lambda kv: kv[0]):
        gu = unary_union(geoms)
        ga = float(geometry_transform(to_metric, gu).area) if gu is not None and not gu.is_empty else 0.0
        distribution.append({'value': value, 'area_m2': ga, 'pct_of_site': (ga / site_area * 100.0) if site_area > 0 else None})
    return {
        'status': 'matched' if overlap_features else 'none', 'known': True, 'present': bool(overlap_features),
        'overlap_area_m2': area, 'overlap_pct': (area / site_area * 100.0) if site_area > 0 else None,
        'feature_count': len(context_features), 'features': context_features, 'overlap_features': overlap_features,
        'bbox_candidate_count': candidate_count, 'source': source_label, 'source_type': 'BUNDLED_OFFICIAL_SHP',
        'source_files': source_files, 'bundle_file': os.path.basename(path), 'source_record_count': total_records,
        'matched_source_records': matched_source_records, 'blank_attribute_counts': blank_attribute_counts,
        'distribution': distribution,
        'detail': detail or '사용자 제공 공식 공간원자료와 대상지의 실제 도형 중첩을 계산한 FACT입니다.',
        'scope_note': scope_note or '이 결과는 번들된 공식 공간원자료의 범위·기준시점에 한정하며 최신 결정조서·고시는 별도 확인합니다.',
    }


@lru_cache(maxsize=1)
def _load_landslide_risk_reference() -> tuple[Dict[str, Any], bytes]:
    if not os.path.isfile(LANDSLIDE_RISK_META_PATH) or not os.path.isfile(LANDSLIDE_RISK_RLE_PATH):
        raise RuntimeError("산사태위험지도 10m 번들 파생자료가 없습니다.")
    with open(LANDSLIDE_RISK_META_PATH, encoding='utf-8') as fp:
        meta = json.load(fp)
    raw = zlib.decompress(Path(LANDSLIDE_RISK_RLE_PATH).read_bytes())
    record_size = int(meta.get('record_size') or 9)
    expected = int(meta.get('record_count') or 0) * record_size
    if expected <= 0 or len(raw) != expected:
        raise RuntimeError(f"산사태위험지도 RLE 무결성 오류 expected={expected} actual={len(raw)}")
    if len(meta.get('row_record_offsets') or []) != int(meta.get('height') or 0) + 1:
        raise RuntimeError("산사태위험지도 row offset 메타데이터 오류")
    return meta, raw


def _analyze_landslide_risk_raster(geometry: Dict[str, Any]) -> Dict[str, Any]:
    site = shape(geometry)
    if site.geom_type not in {"Polygon", "MultiPolygon"} or site.is_empty:
        raise ValueError("유효한 Polygon 또는 MultiPolygon 구역계가 필요합니다.")
    if not site.is_valid:
        site = site.buffer(0)
    if site.is_empty or not site.is_valid:
        raise ValueError("유효하지 않은 구역계입니다.")

    meta, raw = _load_landslide_risk_reference()
    source_crs = CRS.from_string(str(meta.get('crs') or 'EPSG:5181'))
    to_source = Transformer.from_crs(4326, source_crs, always_xy=True).transform
    to_wgs = Transformer.from_crs(source_crs, 4326, always_xy=True).transform
    site_src = geometry_transform(to_source, site)
    site_area = float(site_src.area)
    left = float(meta['left']); top = float(meta['top'])
    px = float(meta['pixel_width_m']); py = float(meta['pixel_height_m'])
    width = int(meta['width']); height = int(meta['height'])
    right = left + width * px; bottom = top - height * py
    raster_extent = box(left, bottom, right, top)
    inside = _polygonal_only(site_src.intersection(raster_extent))
    inside_area = float(inside.area) if inside is not None and not inside.is_empty else 0.0
    if inside_area <= 0:
        return {
            'status': 'out_of_coverage', 'known': False, 'present': False, 'overlap_area_m2': None, 'overlap_pct': None,
            'valid_coverage_area_m2': 0.0, 'valid_coverage_pct': 0.0, 'nodata_area_m2': None, 'nodata_pct': None,
            'outside_extent_area_m2': site_area, 'features': [], 'overlap_features': [], 'distribution': [],
            'source': '산림청 산사태위험지도 2026 산불추가 · 서울 10m 원자료(사용자 제공)', 'source_type': 'BUNDLED_OFFICIAL_RASTER',
            'resolution_m': 10, 'crs': str(meta.get('crs') or 'EPSG:5181'), 'nodata_value': int(meta.get('nodata') or 127),
            'source_process_date': meta.get('source_process_date'), 'detail': '대상지가 제공 래스터 범위 밖이므로 비해당으로 판정하지 않습니다.'
        }

    minx, miny, maxx, maxy = inside.bounds
    row_min = max(0, int(math.floor((top - maxy) / py)))
    row_max = min(height - 1, int(math.floor((top - miny) / py)))
    col_min = max(0, int(math.floor((minx - left) / px)))
    col_max = min(width - 1, int(math.floor((maxx - left) / px)))
    offsets = meta['row_record_offsets']
    rec_size = int(meta.get('record_size') or 9)
    by_grade: Dict[int, List[Any]] = {g: [] for g in range(1, 6)}
    grade_area = {g: 0.0 for g in range(1, 6)}
    candidate_runs = 0
    for row in range(row_min, row_max + 1):
        start_idx = int(offsets[row]); end_idx = int(offsets[row + 1])
        for rec_idx in range(start_idx, end_idx):
            r, c0, c1, grade = struct.unpack_from('<IHHB', raw, rec_idx * rec_size)
            if c1 <= col_min or c0 > col_max or grade not in by_grade:
                continue
            candidate_runs += 1
            x0 = left + c0 * px; x1 = left + c1 * px
            y1 = top - r * py; y0 = y1 - py
            inter = _polygonal_only(site_src.intersection(box(x0, y0, x1, y1)))
            if inter is None or inter.is_empty:
                continue
            area = float(inter.area)
            if area <= 1e-6:
                continue
            grade_area[int(grade)] += area
            by_grade[int(grade)].append(inter)

    valid_area = sum(grade_area.values())
    nodata_area = max(0.0, inside_area - valid_area)
    outside_area = max(0.0, site_area - inside_area)
    overlap_features: List[Dict[str, Any]] = []
    distribution: List[Dict[str, Any]] = []
    for grade in range(1, 6):
        area = grade_area[grade]
        distribution.append({
            'grade': grade, 'area_m2': area,
            'pct_of_site': (area / site_area * 100.0) if site_area > 0 else None,
            'pct_of_valid': (area / valid_area * 100.0) if valid_area > 0 else None,
        })
        if not by_grade[grade]:
            continue
        merged = unary_union(by_grade[grade])
        if merged.is_empty:
            continue
        gwgs = geometry_transform(to_wgs, merged)
        overlap_features.append({
            'type': 'Feature', 'geometry': mapping(gwgs),
            'properties': {'_landslide_grade': grade, 'grade': grade, 'area_m2': area, 'source': '11.tif'}
        })
    known = valid_area > 0.5
    return {
        'status': 'matched' if known else 'nodata', 'known': known, 'present': known,
        'overlap_area_m2': valid_area if known else None,
        'overlap_pct': (valid_area / site_area * 100.0) if known and site_area > 0 else None,
        'valid_coverage_area_m2': valid_area, 'valid_coverage_pct': (valid_area / site_area * 100.0) if site_area > 0 else None,
        'nodata_area_m2': nodata_area, 'nodata_pct': (nodata_area / site_area * 100.0) if site_area > 0 else None,
        'outside_extent_area_m2': outside_area, 'outside_extent_pct': (outside_area / site_area * 100.0) if site_area > 0 else None,
        'distribution': distribution, 'features': overlap_features, 'overlap_features': overlap_features,
        'candidate_run_count': candidate_runs,
        'source': '산림청 산사태위험지도 2026 산불추가 · 서울 10m 원자료(사용자 제공)',
        'source_type': 'BUNDLED_OFFICIAL_RASTER', 'resolution_m': 10, 'crs': str(meta.get('crs') or 'EPSG:5181'),
        'nodata_value': int(meta.get('nodata') or 127), 'source_process_date': meta.get('source_process_date'),
        'bundle_file': os.path.basename(LANDSLIDE_RISK_SOURCE_ZIP),
        'detail': '1~5등급 유효 셀만 등급별 면적을 계산합니다. 값 127은 위험 없음이 아니라 NoData/등급 미제공이므로 음성 판정에 사용하지 않습니다.',
    }


def analyze_disaster_reference_intersections(geometry: Dict[str, Any]) -> Dict[str, Any]:
    specs=[
        ('flood_expected',SEOUL_FLOOD_EXPECTED_URL,'seoul_flood_expected.zip','서울특별시 풍수해 침수예상도 · 서울 열린데이터광장 OA-21172',SEOUL_FLOOD_EXPECTED_DATASET_PAGE),
        ('flood_trace_2025',SEOUL_FLOOD_TRACE_2025_URL,'seoul_flood_trace_2025.zip','서울특별시 2025년 침수흔적도 · 서울 열린데이터광장 OA-15636',SEOUL_FLOOD_TRACE_2025_DATASET_PAGE),
    ]
    out={};errors=[]
    for key,url,cache_name,label,source_page in specs:
        try: out[key]=_analyze_remote_polygon_zip(geometry,url=url,cache_name=cache_name,source_label=label,default_epsg=5186,source_page=source_page)
        except Exception as exc:
            out[key]={'status':'external_source_unavailable','known':False,'present':False,'overlap_area_m2':None,'overlap_pct':None,'features':[],'overlap_features':[],'source':label,'source_type':'OFFICIAL_REMOTE_SHP','source_page':source_page,'error':str(exc)[:300]}
            errors.append({'source':key,'error':str(exc)[:300],'source_page':source_page})

    try:
        out['natural_disaster_risk_district'] = _analyze_local_polygon_zip(
            geometry, path=NATURAL_DISASTER_RISK_DISTRICT_ZIP,
            source_label='LSMD_CONT_UP201 서울 202609 자연재해위험개선지구 원도형(사용자 제공)', default_epsg=5186
        )
    except Exception as exc:
        out['natural_disaster_risk_district']={'status':'error','known':False,'present':False,'overlap_area_m2':None,'overlap_pct':None,'features':[],'overlap_features':[],'source':'LSMD_CONT_UP201 서울 202609 자연재해위험개선지구 원도형(사용자 제공)','source_type':'BUNDLED_OFFICIAL_SHP','error':str(exc)[:300]}
        errors.append({'source':'natural_disaster_risk_district','error':str(exc)[:300]})

    try:
        out['landslide_risk_map'] = _analyze_landslide_risk_raster(geometry)
    except Exception as exc:
        out['landslide_risk_map']={'status':'error','known':False,'present':False,'overlap_area_m2':None,'overlap_pct':None,'features':[],'overlap_features':[],'distribution':[],'source':'산림청 산사태위험지도 2026 산불추가 · 서울 10m 원자료(사용자 제공)','source_type':'BUNDLED_OFFICIAL_RASTER','error':str(exc)[:300]}
        errors.append({'source':'landslide_risk_map','error':str(exc)[:300]})

    source_count=len(specs)+2
    return {'status':'available' if not errors else ('partial' if len(errors)<source_count else 'error'),**out,'errors':errors,
            'note':'침수예상도=위험예측, 침수흔적도=과거 발생이력, 자연재해위험개선지구=제공 LSMD 원도형, 산사태위험지도=10m 1~5등급 분포 FACT로 분리한다. 산사태 값 127은 위험 없음이 아니라 NoData이다.'}


@app.post("/api/spatial/disaster-reference-intersections")
def disaster_reference_intersections(inp: GeometryInput):
    try:
        return analyze_disaster_reference_intersections(inp.geometry)
    except ValueError as exc:
        raise HTTPException(status_code=422,detail=str(exc)) from exc
    except Exception as exc:
        logging.exception('disaster reference analysis failed')
        raise HTTPException(status_code=502,detail=f"재해 공간자료 조회 실패: {str(exc)[:240]}") from exc


@app.post("/api/spatial/regulatory-land-use-restrictions")
def regulatory_land_use_restrictions(inp: PnuListInput):
    """개발·계획제한 독립 모듈용 VWorld NED 양성 규제행 조회.

    전용 공간원도형이 없는 항목의 음성 결과는 '없음' 확정근거로 사용하지 않도록
    complete 여부와 양성행만 반환한다. 기존 사업판정 endpoint와 완전히 분리한다.
    """
    pnus: List[str] = []
    seen = set()
    for raw in inp.pnus:
        pnu = str(raw or "").strip()
        if len(pnu) != 19 or not pnu.isdigit() or pnu in seen:
            continue
        seen.add(pnu); pnus.append(pnu)
    if not pnus:
        raise HTTPException(status_code=422, detail="유효한 19자리 PNU가 없습니다.")
    if not _vworld_key():
        raise HTTPException(status_code=503, detail="VWORLD_API_KEY가 설정되지 않았습니다.")

    keys = [
        "urban_natural_park", "natural_park", "ecological_landscape", "wildlife_special",
        "public_interest_forest", "conservation_forest", "water_source", "river_zone",
        "railroad_protection", "airport_obstacle", "military_flight", "disaster_risk",
        "landslide_risk", "steep_slope_risk", "flood_management",
        "heritage_environment", "education_environment",
    ]
    categories: Dict[str, Dict[str, Any]] = {k: {"affected_pnus": [], "rows": []} for k in keys}
    success_pnus: List[str] = []
    errors: List[Dict[str, str]] = []
    def work(pnu: str):
        return pnu, _land_use_rows_for_pnu(pnu)
    with ThreadPoolExecutor(max_workers=min(2, len(pnus))) as pool:
        futures = {pool.submit(work, pnu): pnu for pnu in pnus}
        for fut in as_completed(futures):
            pnu = futures[fut]
            try:
                _, rows = fut.result(); success_pnus.append(pnu)
                for row in rows:
                    cat = _regulatory_land_use_category(row.get("prposAreaDstrcCodeNm"))
                    if cat not in categories:
                        continue
                    clean = {
                        "pnu": pnu, "relation_code": str(row.get("cnflcAt") or ""),
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
    success_set = set(success_pnus); complete = len(success_set) == len(pnus)
    for value in categories.values():
        value["affected_pnus"].sort(); value["present"] = bool(value["affected_pnus"])
        value["positive_evidence"] = bool(value["present"]); value["checked_parcels"] = len(success_set)
        value["negative_complete"] = bool(complete)
    return {
        "status": "available" if complete else ("partial" if success_set else "error"),
        "queried_parcels": len(pnus), "success_parcels": len(success_set), "error_parcels": len(errors),
        "requested_pnus": pnus,
        "successful_pnus": sorted(success_set),
        "failed_pnus": sorted({row["pnu"] for row in errors}),
        "categories": categories, "errors": errors,
        "source": {"provider": "국토교통부 / VWorld NED", "dataset": "토지이용계획정보", "operation": "getLandUseAttr", "geometry_basis": "parcel_attribute_only", "negative_rule": "전용 공간원도형 미연결 항목은 NED 음성만으로 비해당 확정 금지"},
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
    pnu_status: Dict[str, Dict[str, Any]] = {}

    # Small concurrent fan-out keeps each browser request short without flooding data.go.kr.
    with ThreadPoolExecutor(max_workers=min(2, len(pnus))) as ex:
        futures = {ex.submit(_query_building_hub_title, pnu): pnu for pnu in pnus}
        for fut in as_completed(futures):
            pnu = futures[fut]
            try:
                items = fut.result()
                records.extend(_normalize_building_title(item, pnu) for item in items)
                pnu_status[pnu] = {"pnu": pnu, "status": "SUCCESS_DATA" if items else "SUCCESS_EMPTY", "record_count": len(items), "error": ""}
            except Exception as exc:
                logger.warning("BuildingHUB title failed pnu=%s error=%s", pnu, exc)
                errors.append({"pnu": pnu, "error": str(exc)})
                pnu_status[pnu] = {"pnu": pnu, "status": "ERROR", "record_count": 0, "error": str(exc)}

    return {
        "requested_pnu_count": len(pnus),
        "record_count": len(records),
        "records": records,
        "errors": errors,
        "pnu_status": [pnu_status[pnu] for pnu in pnus],
        "successful_pnus": [pnu for pnu in pnus if pnu_status.get(pnu, {}).get("status") == "SUCCESS_DATA"],
        "empty_pnus": [pnu for pnu in pnus if pnu_status.get(pnu, {}).get("status") == "SUCCESS_EMPTY"],
        "failed_pnus": [pnu for pnu in pnus if pnu_status.get(pnu, {}).get("status") == "ERROR"],
        "complete": all(pnu_status.get(pnu, {}).get("status") in {"SUCCESS_DATA", "SUCCESS_EMPTY"} for pnu in pnus),
        "source": {
            "provider": "국토교통부",
            "dataset": "건축HUB 건축물대장정보 서비스",
            "operation": "getBrTitleInfo",
            "data_portal_modified": "2026-07-10",
            "engine_as_of_date": ENGINE_AS_OF_DATE.isoformat(),
        },
    }


@app.post("/api/building-hub/floor-batch")
def building_hub_floor_batch(inp: BuildingHubBatchInput):
    if not building_hub_ready():
        raise HTTPException(status_code=503, detail="BUILDING_HUB_API_KEY가 Render Environment에 설정되지 않았습니다.")
    pnus=[];seen=set()
    for p in inp.pnus:
        p=str(p).strip()
        if p and p not in seen:
            seen.add(p);pnus.append(p)
    records: List[Dict[str, Any]]=[]; errors: List[Dict[str, Any]]=[]; pnu_status: Dict[str, Dict[str, Any]]={}
    with ThreadPoolExecutor(max_workers=min(2, len(pnus) or 1)) as ex:
        futures={ex.submit(_query_building_hub_floor,pnu):pnu for pnu in pnus}
        for fut in as_completed(futures):
            pnu=futures[fut]
            try:
                items=fut.result();records.extend(_normalize_building_floor(item,pnu) for item in items);pnu_status[pnu]={"pnu":pnu,"status":"SUCCESS_DATA" if items else "SUCCESS_EMPTY","record_count":len(items),"error":""}
            except Exception as exc:
                logger.warning("BuildingHUB floor failed pnu=%s error=%s",pnu,exc)
                errors.append({"pnu":pnu,"error":str(exc)});pnu_status[pnu]={"pnu":pnu,"status":"ERROR","record_count":0,"error":str(exc)}
    return {
        "requested_pnu_count":len(pnus),"record_count":len(records),"records":records,"errors":errors,
        "pnu_status":[pnu_status[pnu] for pnu in pnus],"successful_pnus":[pnu for pnu in pnus if pnu_status.get(pnu,{}).get("status")=="SUCCESS_DATA"],"empty_pnus":[pnu for pnu in pnus if pnu_status.get(pnu,{}).get("status")=="SUCCESS_EMPTY"],"failed_pnus":[pnu for pnu in pnus if pnu_status.get(pnu,{}).get("status")=="ERROR"],"complete":all(pnu_status.get(pnu,{}).get("status") in {"SUCCESS_DATA","SUCCESS_EMPTY"} for pnu in pnus),
        "source":{"provider":"국토교통부","dataset":"건축HUB 건축물대장정보 서비스","operation":"getBrFlrOulnInfo","engine_as_of_date":ENGINE_AS_OF_DATE.isoformat()},
    }


@app.post("/api/redevelopment/house-density")
def house_density(detail: Dict[str, Any]):
    return calculate_house_density(detail)


# Restored routes required by the deployed UI (GitHub baseline).
DEVELOPMENT_RESTRICTION_ZONE_ZIP = _data_path("source_development_restriction_zone_seoul_202609.zip")
WATER_SOURCE_PROTECTION_ZONE_ZIP = _data_path("source_water_source_protection_zone_seoul_202609.zip")
WILDLIFE_PROTECTION_ZONE_ZIP = _data_path("source_wildlife_protection_zone_seoul_202609.zip")
RIVER_ZONE_ZIP = _data_path("source_river_zone_seoul_202609.zip")
SMALL_RIVER_ZONE_ZIP = _data_path("source_small_river_zone_seoul_202609.zip")
RAILROAD_PROTECTION_ZONE_ZIP = _data_path("source_railroad_protection_zone_seoul_202609.zip")
AIRPORT_OBSTACLE_SURFACE_ZIP = _data_path("source_airport_obstacle_surface_seoul_202609.zip")
AIRPORT_NOISE_ZONE_ZIP = _data_path("source_airport_noise_zone_seoul_202609.zip")
HANRIVER_LANDFILL_RESTRICTION_ZIP = _data_path("source_hanriver_landfill_restriction_seoul_202609.zip")
ECOLOGICAL_NATURE_MAP_ZIP = _data_path("source_ecological_nature_map_seoul_flat.zip")
LANDSCAPE_MANAGEMENT_ZONE_ZIP = _data_path("source_landscape_management_zone_seoul_202609.zip")
CULTURE_DISTRICT_ZIP = _data_path("source_culture_district_seoul_202609.zip")

ECVAM_API_CONFIRM_URL = "https://ecvam.neins.go.kr/apiConfirm.do"
ECVAM_WMS_URL = "https://ecvam.neins.go.kr/apicall.do"
ECVAM_LOG_URL = "https://ecvam.neins.go.kr/common/insertApiCallLog.ajax"
ECVAM_ALLOWED_WMS_LAYERS = {
    "nem_law_01": "생태·경관보전지역",
    "nem_law_02": "시도생태·경관보전지역",
    "nem_law_10": "공원자연보존지구",
    "nem_law_11": "공원자연환경지구",
    "nem_law_12": "공원마을지구",
    "nem_law_13": "공원문화유산지구",
}
_ECVAM_API_CHECK = {"key_hash": "", "checked_at": 0.0, "ok": False, "error": "", "wms_url": "", "endpoint_source": ""}
_ECVAM_API_CHECK_LOCK = threading.Lock()


def _redact_ecvam_text(value: Any) -> str:
    text = str(value or "")
    key = _ecvam_key()
    if key:
        text = text.replace(key, "***")
    return re.sub(r"(?i)(APIKEY=)[^&\s\"']+", r"\1***", text)


def _ecvam_wms_url_from_bootstrap(text: str) -> tuple[str, str]:
    """Discover the WMS request target from the official apiConfirm bootstrap."""
    normalized = str(text or "").replace("\\/", "/").replace("&amp;", "&")
    matches = re.findall(r"[\"']([^\"']*apicall\.do[^\"']*)[\"']", normalized, flags=re.I)
    for raw in matches:
        candidate = raw.strip()
        if not candidate:
            continue
        if candidate.startswith("/"):
            candidate = urljoin("https://ecvam.neins.go.kr/", candidate)
        elif not re.match(r"^https?://", candidate, flags=re.I):
            candidate = urljoin("https://ecvam.neins.go.kr/", candidate)
        try:
            parsed = urlparse(candidate)
            if parsed.hostname and parsed.hostname.lower() == "ecvam.neins.go.kr" and parsed.path.lower().endswith("/apicall.do"):
                return candidate, "bootstrap"
        except Exception:
            continue
    return ECVAM_WMS_URL, "compatibility_fallback"


def _ecvam_request_target(ready: Dict[str, Any]) -> tuple[str, Dict[str, str]]:
    raw = str(ready.get("_wms_url") or ECVAM_WMS_URL)
    parsed = urlparse(raw)
    base_params = {str(k): str(v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)}
    base_url = urlunparse((parsed.scheme or "https", parsed.netloc or "ecvam.neins.go.kr", parsed.path or "/apicall.do", "", "", ""))
    base_params.setdefault("APIKEY", _ecvam_key())
    base_params.setdefault("DOMAIN", _vworld_domain())
    return base_url, base_params


def _ecvam_key() -> str:
    # R17 표준 환경변수. 과거 소문자 ecvam fallback은 의도적으로 두지 않는다.
    return (os.getenv("ECVAM_API_KEY") or "").strip()


def _ecvam_configured() -> bool:
    return bool(_ecvam_key())


def _ensure_ecvam_api_ready(force: bool = False) -> Dict[str, Any]:
    """Validate the official ECVAM bootstrap and discover its WMS endpoint."""
    key = _ecvam_key()
    if not key:
        raise HTTPException(status_code=503, detail="ECVAM_API_KEY가 설정되지 않았습니다.")
    key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
    now = time.time()
    with _ECVAM_API_CHECK_LOCK:
        if (not force and _ECVAM_API_CHECK.get("key_hash") == key_hash
                and now - float(_ECVAM_API_CHECK.get("checked_at") or 0) < 900):
            if _ECVAM_API_CHECK.get("ok"):
                return {
                    "ok": True, "cached": True,
                    "endpoint_source": _ECVAM_API_CHECK.get("endpoint_source") or "unknown",
                    "_wms_url": _ECVAM_API_CHECK.get("wms_url") or ECVAM_WMS_URL,
                }
            raise HTTPException(status_code=502, detail=str(_ECVAM_API_CHECK.get("error") or "ECVAM API 인증 확인 실패"))
    wms_url = ECVAM_WMS_URL
    endpoint_source = "compatibility_fallback"
    try:
        r = requests.get(
            ECVAM_API_CONFIRM_URL, params={"APIKEY": key}, timeout=10,
            headers={"User-Agent": "urban-strategy/2.5.0 ECVAM-WMS", "Referer": _vworld_referer()},
        )
        text = r.text or ""
        bootstrap_ok = r.status_code == 200 and "ecvamLayerCreate" in text
        layer_ok = all(layer in text for layer in ECVAM_ALLOWED_WMS_LAYERS)
        if bootstrap_ok:
            wms_url, endpoint_source = _ecvam_wms_url_from_bootstrap(text)
        ok = bootstrap_ok and layer_ok
        error = "" if ok else f"ECVAM API bootstrap HTTP {r.status_code} 또는 레이어 정의 미확인"
    except requests.RequestException as exc:
        ok = False
        error = f"ECVAM API 인증 요청 실패: {type(exc).__name__}"
    error = _redact_ecvam_text(error)
    with _ECVAM_API_CHECK_LOCK:
        _ECVAM_API_CHECK.update({
            "key_hash": key_hash, "checked_at": now, "ok": ok, "error": error,
            "wms_url": wms_url if ok else "", "endpoint_source": endpoint_source if ok else "",
        })
    if not ok:
        raise HTTPException(status_code=502, detail=error)
    return {"ok": True, "cached": False, "endpoint_source": endpoint_source, "_wms_url": wms_url}




@app.get("/api/reference/ecvam-status")
def ecvam_status(probe: bool = False):
    """Safe ECVAM readiness metadata; never returns the API key."""
    out = {
        "configured": _ecvam_configured(),
        "source": "국토환경성평가지도 OpenAPI WMS",
        "mode": "MAP_ONLY",
        "layers": [{"id": k, "label": v} for k, v in ECVAM_ALLOWED_WMS_LAYERS.items()],
        "quantitative_overlap": False,
        "note": "WMS 도면 교차확인용입니다. 벡터 원도형이 아니므로 중첩면적·중첩률·해당/비해당 자동판정에 사용하지 않습니다.",
    }
    out["official_bootstrap_url"] = ECVAM_API_CONFIRM_URL
    out["official_api_guide"] = "https://ecvam.neins.go.kr/api/apiGuide.do"
    if probe and out["configured"]:
        try:
            ready = _ensure_ecvam_api_ready(force=True)
            probe_out = {"ok": True, "bootstrap": "OK", "endpoint_source": ready.get("endpoint_source")}
            try:
                target, base_params = _ecvam_request_target(ready)
                tf = Transformer.from_crs(4326, 3857, always_xy=True)
                x1, y1 = tf.transform(126.95, 37.50); x2, y2 = tf.transform(127.05, 37.60)
                params = {**base_params,
                    "SERVICE":"WMS", "VERSION":"1.1.0", "REQUEST":"GetMap", "LAYERS":"nem_law_01",
                    "STYLES":"", "SRS":"EPSG:3857", "BBOX":f"{min(x1,x2):.3f},{min(y1,y2):.3f},{max(x1,x2):.3f},{max(y1,y2):.3f}",
                    "WIDTH":"64", "HEIGHT":"64", "FORMAT":"image/png", "TRANSPARENT":"TRUE",
                }
                rr = requests.get(target, params=params, timeout=10, headers={
                    "User-Agent":"urban-strategy/2.5.0 ECVAM-WMS-probe", "Accept":"image/png,image/*;q=0.8,*/*;q=0.5", "Referer":_vworld_referer(),
                })
                ctype = str(rr.headers.get("content-type") or "").lower()
                probe_out["wms"] = {"ok": rr.status_code == 200 and bool(rr.content) and ("image" in ctype or rr.content.startswith(b"\x89PNG")), "http_status": rr.status_code, "content_type": ctype[:80]}
                if not probe_out["wms"]["ok"]:
                    probe_out["wms"]["error"] = _redact_ecvam_text((rr.text or "").replace("\n"," ")[:180])
            except Exception as exc:
                probe_out["wms"] = {"ok": False, "error": _redact_ecvam_text(f"{type(exc).__name__}: {exc}")}
            # Overall probe means the bootstrap AND one real GetMap request worked.
            probe_out["ok"] = bool(probe_out.get("wms", {}).get("ok"))
            out["probe"] = probe_out
        except HTTPException as exc:
            out["probe"] = {"ok": False, "bootstrap": "ERROR", "error": _redact_ecvam_text(str(exc.detail))}
    return out


@app.get("/api/reference/ecvam-wms-map")
def ecvam_wms_map(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    layers: str = "nem_law_01,nem_law_02,nem_law_10,nem_law_11,nem_law_12,nem_law_13",
    width: int = 760,
    height: int = 520,
):
    """Server-side display proxy for selected ECVAM WMS layers.

    R17 exposes ecological-landscape conservation layers plus four national-park
    zoning layers for site review.  The API key stays server-side.  Returned pixels are a map
    cross-check only; no geometric overlap or PASS/FAIL inference is made.
    """
    if not (-180 <= min_lon < max_lon <= 180 and -90 <= min_lat < max_lat <= 90):
        raise HTTPException(status_code=400, detail="invalid WGS84 bbox")
    requested = [x.strip() for x in str(layers or "").split(",") if x.strip()]
    if not requested or any(x not in ECVAM_ALLOWED_WMS_LAYERS for x in requested):
        raise HTTPException(status_code=400, detail="허용되지 않은 ECVAM WMS 레이어입니다.")
    # preserve caller order while removing duplicates
    requested = list(dict.fromkeys(requested))
    width = max(320, min(int(width), 1200))
    height = max(220, min(int(height), 900))
    ready = _ensure_ecvam_api_ready()
    try:
        target_url, base_params = _ecvam_request_target(ready)
        tf = Transformer.from_crs(4326, 3857, always_xy=True)
        pts = [
            tf.transform(min_lon, min_lat), tf.transform(min_lon, max_lat),
            tf.transform(max_lon, min_lat), tf.transform(max_lon, max_lat),
        ]
        xs = [x for x, _ in pts]; ys = [y for _, y in pts]
        params = {**base_params,
            "SERVICE": "WMS", "VERSION": "1.1.0", "REQUEST": "GetMap",
            "LAYERS": ",".join(requested), "STYLES": "", "SRS": "EPSG:3857",
            "BBOX": f"{min(xs):.3f},{min(ys):.3f},{max(xs):.3f},{max(ys):.3f}",
            "WIDTH": str(width), "HEIGHT": str(height),
            "FORMAT": "image/png", "TRANSPARENT": "TRUE",
        }
        r = requests.get(
            target_url, params=params, timeout=15,
            headers={
                "User-Agent": "urban-strategy/2.5.0 ECVAM-WMS-display",
                "Accept": "image/png,image/*;q=0.8,*/*;q=0.5",
                "Referer": _vworld_referer(),
            },
        )
        content_type = str(r.headers.get("content-type") or "").lower()
        if r.status_code != 200 or not r.content:
            raise HTTPException(status_code=502, detail=f"ECVAM WMS HTTP {r.status_code}")
        if "image" not in content_type and not r.content.startswith(b"\x89PNG"):
            preview = (r.text or "").replace("\n", " ")[:140]
            raise HTTPException(status_code=502, detail=f"ECVAM WMS non-image response: {_redact_ecvam_text(preview)}")
        # Official bootstrap logs each layer call separately; mirror that on a best-effort basis.
        try:
            requests.get(
                ECVAM_LOG_URL,
                params={"APIKEY": _ecvam_key(), "DOMAIN": _vworld_domain(), "LAYERS": ",".join(requested), "callback": "ecvamLog"},
                timeout=3, headers={"User-Agent": "urban-strategy/2.5.0 ECVAM-WMS-log"},
            )
        except Exception:
            pass
        return Response(content=r.content, media_type="image/png", headers={"Cache-Control": "public, max-age=900"})
    except HTTPException:
        raise
    except Exception as exc:
        logging.warning("ECVAM WMS proxy failed: %s", exc)
        raise HTTPException(status_code=502, detail="ECVAM WMS unavailable") from exc


@app.post("/api/spatial/development-local-intersections")
def development_local_intersections(inp: GeometryInput):
    """서울 UQ181 로컬 개발사업구역만 즉시 중첩한다. 외부 VWorld 산업단지 조회는 수행하지 않는다."""
    try:
        return analyze_development_intersections(inp.geometry, include_industrial=False)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logging.exception("local development intersection failed")
        raise HTTPException(status_code=500, detail=f"로컬 개발사업구역 중첩분석 오류: {exc}") from exc
    finally:
        _release_heavy_analysis_cache("development")


@app.post("/api/spatial/industrial-park-intersections")
def industrial_park_intersections(inp: GeometryInput):
    """산업단지 전용경계(LT_C_DAMDAN)만 별도 보강 조회한다."""
    try:
        return analyze_industrial_park_intersections(inp.geometry)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except VWorldTransportError as exc:
        logging.warning("industrial park upstream unavailable: %s", exc)
        raise HTTPException(
            status_code=503,
            detail=f"EXTERNAL_UPSTREAM_ERROR · VWorld LT_C_DAMDAN · {str(exc)[:220]}",
        ) from exc
    except Exception as exc:
        logging.exception("industrial park intersection failed")
        raise HTTPException(status_code=503, detail=f"산업단지 전용경계 조회 오류: {str(exc)[:240]}") from exc


def analyze_local_disaster_reference_intersections(geometry: Dict[str, Any]) -> Dict[str, Any]:
    """외부망과 무관한 번들 재해자료만 즉시 분석합니다."""
    out={};errors=[]
    try:
        out['natural_disaster_risk_district'] = _analyze_local_polygon_zip(
            geometry, path=NATURAL_DISASTER_RISK_DISTRICT_ZIP,
            source_label='LSMD_CONT_UP201 서울 202609 자연재해위험개선지구 원도형(사용자 제공)', default_epsg=5186
        )
    except Exception as exc:
        out['natural_disaster_risk_district']={'status':'error','known':False,'present':False,'overlap_area_m2':None,'overlap_pct':None,'features':[],'overlap_features':[],'source':'LSMD_CONT_UP201 서울 202609 자연재해위험개선지구 원도형(사용자 제공)','source_type':'BUNDLED_OFFICIAL_SHP','error':str(exc)[:300]}
        errors.append({'source':'natural_disaster_risk_district','error':str(exc)[:300]})
    try:
        out['landslide_risk_map'] = _analyze_landslide_risk_raster(geometry)
    except Exception as exc:
        out['landslide_risk_map']={'status':'error','known':False,'present':False,'overlap_area_m2':None,'overlap_pct':None,'features':[],'overlap_features':[],'distribution':[],'source':'산림청 산사태위험지도 2026 산불추가 · 서울 10m 원자료(사용자 제공)','source_type':'BUNDLED_OFFICIAL_RASTER','error':str(exc)[:300]}
        errors.append({'source':'landslide_risk_map','error':str(exc)[:300]})
    return {'status':'available' if not errors else ('partial' if len(errors)<2 else 'error'),**out,'errors':errors,
            'note':'외부 API를 기다리지 않고 자연재해위험개선지구와 산사태위험지도 번들 원자료만 분석한다.'}


@app.post("/api/spatial/local-disaster-reference-intersections")
def local_disaster_reference_intersections(inp: GeometryInput):
    try:
        return analyze_local_disaster_reference_intersections(inp.geometry)
    except ValueError as exc:
        raise HTTPException(status_code=422,detail=str(exc)) from exc
    except Exception as exc:
        logging.exception('local disaster reference analysis failed')
        raise HTTPException(status_code=500,detail=f"로컬 재해 공간자료 분석 실패: {str(exc)[:240]}") from exc


def analyze_bundled_regulatory_references(geometry: Dict[str, Any]) -> Dict[str, Any]:
    """R16 evidence-backed regulatory layers supplied as official Seoul/national SHP data.

    Unsupported placeholders are intentionally absent. Every returned layer has a bundled
    geometry source, so a non-overlap can be stated only as 'no overlap in bundled source'.
    """
    specs = [
        ('development_restriction', DEVELOPMENT_RESTRICTION_ZONE_ZIP, '개발제한구역 LSMD_CONT_UD801 서울(사용자 제공)', 5186, ['UDV100'], 'MNUM', None),
        ('ecological_nature_grade1', ECOLOGICAL_NATURE_MAP_ZIP, '환경부 생태자연도 서울 원도형(사용자 제공) · 1등급', 5186, ['1'], '생태자연도', None),
        ('wildlife_protection', WILDLIFE_PROTECTION_ZONE_ZIP, '야생생물 보호구역 LSMD_CONT_UM221 서울(사용자 제공)', 5186, ['UMS210','UMS220'], 'MNUM', None),
        ('water_source_protection', WATER_SOURCE_PROTECTION_ZONE_ZIP, '상수원보호구역 LSMD_CONT_UM710 서울(사용자 제공)', 5186, ['UMI100'], 'MNUM', None),
        ('river_zone', RIVER_ZONE_ZIP, '하천구역 LSMD_CONT_UJ201 서울(사용자 제공)', 5186, ['UJB100'], 'MNUM', None),
        ('small_river_zone', SMALL_RIVER_ZONE_ZIP, '소하천구역 LSMD_CONT_UJ301 서울(사용자 제공)', 5186, ['UJC100'], 'MNUM', None),
        ('hanriver_landfill_restriction', HANRIVER_LANDFILL_RESTRICTION_ZIP, '한강수계 폐기물매립시설 설치제한지역 LSMD_CONT_UM730 서울(사용자 제공)', 5174, ['UMK600'], 'MNUM', None),
        ('landscape_management', LANDSCAPE_MANAGEMENT_ZONE_ZIP, '서울시 중점경관관리구역 LSMD_CONT_ZQ001(사용자 제공)', 5186, None, 'MNUM', None),
        ('culture_district', CULTURE_DISTRICT_ZIP, '문화지구 LSMD_CONT_UO801 서울(사용자 제공)', 5174, ['UOH100'], 'MNUM', None),
        ('railroad_protection', RAILROAD_PROTECTION_ZONE_ZIP, '철도보호지구 LSMD_CONT_UI310 서울(사용자 제공)', 5186, ['UIK100'], 'MNUM', None),
        ('airport_obstacle', AIRPORT_OBSTACLE_SURFACE_ZIP, '공항 장애물제한표면 LSMD_CONT_UI701 서울(사용자 제공)', 5186, ['UIG510','UIG520','UIG530','UIG540','UIG550','UIG560','UIG570','UIG600'], 'MNUM', None),
        ('airport_noise', AIRPORT_NOISE_ZONE_ZIP, '공항 소음대책지역 LSMD_CONT_UI702 서울(사용자 제공)', 5174, ['UIN100','UIN200','UIN300'], 'MNUM', None),
        ('flood_management', RIVER_ZONE_ZIP, '홍수관리구역 LSMD_CONT_UJ201 서울(사용자 제공)', 5186, ['UJB400'], 'MNUM', None),
    ]
    out: Dict[str, Any] = {}
    errors: List[Dict[str, str]] = []
    for key, path, label, epsg, codes, code_field, group_field in specs:
        try:
            out[key] = _analyze_local_polygon_zip(
                geometry, path=path, source_label=label, default_epsg=epsg,
                include_codes=codes, code_field=code_field, group_field=group_field,
                scope_note='사용자 확보 공식 공간원자료와의 중첩 결과입니다. 최신 고시·결정조서와 원자료 기준시점은 최종 인허가 검토에서 재확인합니다.',
            )
        except Exception as exc:
            out[key] = {
                'status':'error','known':False,'present':False,'overlap_area_m2':None,'overlap_pct':None,
                'features':[],'overlap_features':[],'source':label,'source_type':'BUNDLED_OFFICIAL_SHP','error':str(exc)[:300]
            }
            errors.append({'source':key,'error':str(exc)[:300]})
    return {
        'status':'available' if not errors else ('partial' if len(errors)<len(specs) else 'error'),
        **out, 'errors':errors,
        'note':'R16은 번들 원도형이 확보된 규제만 반환한다. 군사시설보호, 산사태취약지역 지정도, 급경사지 붕괴위험지역 등 원도형 미확보 항목은 이 API와 UI에서 제외한다.'
    }


@app.post("/api/spatial/regulatory-reference-intersections")
def regulatory_reference_intersections(inp: GeometryInput):
    try:
        return analyze_bundled_regulatory_references(inp.geometry)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logging.exception('bundled regulatory reference analysis failed')
        raise HTTPException(status_code=502, detail=f"규제 공간자료 분석 실패: {str(exc)[:240]}") from exc
