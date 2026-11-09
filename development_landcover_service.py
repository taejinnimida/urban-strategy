"""Fixed-provider 2-6 land-cover collector.

Official documentation publishes WMS only. WFS is attempted ONLY after
GetCapabilities advertises EGIS:lv3_2025y as a FeatureType. Neither a WMS
image nor an incomplete WFS response is promoted to authoritative vector FACT.
No user-controlled request URLs, keys, WFS typeNames or GeoServer CQL filters.
"""
import io
import math
import struct
from xml.etree import ElementTree as ET

import requests
from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform

SOURCE = "https://aid.mcee.go.kr/api/land.do"
LAYER = "EGIS:lv3_2025y"
WMS_ENDPOINTS = (
    "https://api.mcee.go.kr/geoserver/wms",
    "https://api.mcee.go.kr/geoserver/gwc/service/wms",
)
WFS_ENDPOINT = "https://api.mcee.go.kr/geoserver/wfs"
PROJECT = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True).transform
INVERSE = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True).transform
PNG = b"\x89PNG\r\n\x1a\n"
MAX_IMAGE_BYTES = 12_000_000
WFS_LIMIT = 500


def _bounds(geometry):
    target = shape(geometry)
    if target.is_empty or not target.is_valid or target.geom_type not in ("Polygon", "MultiPolygon"):
        raise ValueError("invalid area")
    lon1, lat1, lon2, lat2 = target.bounds
    if not (123 <= lon1 < lon2 <= 133 and 32 <= lat1 < lat2 <= 40):
        raise ValueError("outside Korean map extent")
    metric = transform(PROJECT, target)
    xmin, ymin, xmax, ymax = metric.bounds
    pad_x, pad_y = max((xmax - xmin) * .075, 15), max((ymax - ymin) * .075, 15)
    xmin -= pad_x
    xmax += pad_x
    ymin -= pad_y
    ymax += pad_y
    lon_w, lat_s = INVERSE(xmin, ymin)
    lon_e, lat_n = INVERSE(xmax, ymax)
    return (xmin, ymin, xmax, ymax), [[lat_s, lon_w], [lat_n, lon_e]]


def _get(url, params, max_bytes):
    r = requests.get(
        url, params=params, timeout=(3, 8), allow_redirects=False,
        headers={"User-Agent": "urban-strategy-landcover/1", "Accept": "image/png,application/json,application/xml"},
    )
    try:
        code = r.status_code
        if code != 200:
            return None, "HTTP_" + str(code)
        content = r.content
        if not content or len(content) > max_bytes:
            return None, "PROVIDER_SIZE"
        return content, None
    finally:
        r.close()


def fetch_landcover_wms(geometry):
    """One 1024x768 WMS image aligned to a fixed project-area bbox."""
    try:
        bbox, image_bounds = _bounds(geometry)
    except (ValueError, TypeError, KeyError):
        return {"status": "ERROR", "error_code": "INVALID_GEOMETRY", "source": SOURCE}
    params = {
        "SERVICE": "WMS", "VERSION": "1.1.1", "REQUEST": "GetMap",
        "LAYERS": LAYER, "STYLES": "", "SRS": "EPSG:3857",
        "BBOX": ",".join(f"{v:.3f}" for v in bbox),
        "FORMAT": "image/png", "TRANSPARENT": "TRUE",
        "WIDTH": 1024, "HEIGHT": 768,
    }
    errors = []
    for url in WMS_ENDPOINTS:
        try:
            raw, error = _get(url, params, MAX_IMAGE_BYTES)
        except requests.Timeout:
            errors.append("TIMEOUT")
            continue
        except requests.RequestException:
            errors.append("CONNECTION_ERROR")
            continue
        if error:
            errors.append(error)
            continue
        if not raw.startswith(PNG) or len(raw) < 24:
            errors.append("NOT_PNG")
            continue
        width, height = struct.unpack(">II", raw[16:24])
        if (width, height) != (1024, 768):
            errors.append("INVALID_PNG_DIMENSIONS")
            continue
        return {
            "status": "MAP_ONLY",
            "detail": "환경공간정보서비스 2025년 세분류 토지피복지도 WMS 이미지 수신. 벡터 면적통계·CAD는 별도 검증 필요.",
            "source": "환경공간정보서비스 WMS",
            "source_url": SOURCE,
            "layer": LAYER,
            "bounds": image_bounds,
            "map_width": 1024,
            "map_height": 768,
            "_image": raw,
        }
    return {
        "status": "ERROR", "error_code": errors[-1] if errors else "WMS_UNAVAILABLE",
        "detail": "토지피복 WMS 제공기관 이미지 미수신. 지도 영상을 확보했다고 표시하지 않습니다.",
        "source": "환경공간정보서비스 WMS", "source_url": SOURCE,
    }


def fetch_landcover_wfs(geometry):
    """Probe advertised WFS availability, then attempt a bounded GeoJSON query.

    No assumption is made that a GeoServer WMS layer is also a WFS feature type.
    The layer is an official land-cover *WMS* in currently published guidance.
    """
    try:
        bbox, _ = _bounds(geometry)
        lon_w, lat_s = INVERSE(bbox[0], bbox[1])
        lon_e, lat_n = INVERSE(bbox[2], bbox[3])
    except (ValueError, TypeError, KeyError):
        return {"status": "ERROR", "error_code": "INVALID_GEOMETRY", "source": SOURCE}
    source = {"source": "환경공간정보서비스 GeoServer WFS 확인", "source_url": SOURCE}
    try:
        xml, error = _get(WFS_ENDPOINT, {
            "service": "WFS", "version": "1.0.0", "request": "GetCapabilities",
        }, 2_000_000)
        if error:
            return {**source, "status": "WFS_UNVERIFIED", "error_code": error,
                    "detail": "공식 문서에 WFS가 안내되지 않았고 서비스 기능 확인에 실패했습니다."}
        if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
            return {**source, "status": "WFS_UNVERIFIED", "error_code": "UNSAFE_XML"}
        root = ET.fromstring(xml)
        names = []
        for el in root.iter():
            if el.tag.split("}")[-1] != "FeatureType":
                continue
            for child in el:
                if child.tag.split("}")[-1] == "Name" and child.text:
                    names.append(child.text.strip())
        if LAYER not in names:
            return {**source, "status": "WFS_NOT_ADVERTISED",
                    "detail": "공개 GeoServer WFS 기능목록에 2025년 토지피복 세분류 레이어가 없습니다. WMS만 확인 가능합니다."}
        raw, error = _get(WFS_ENDPOINT, {
            "service": "WFS", "version": "1.0.0", "request": "GetFeature",
            "typeName": LAYER, "srsName": "EPSG:4326",
            "bbox": ",".join(f"{v:.7f}" for v in (lon_w, lat_s, lon_e, lat_n)),
            "outputFormat": "application/json", "maxFeatures": WFS_LIMIT,
        }, 8_000_000)
        if error:
            return {**source, "status": "WFS_UNVERIFIED", "error_code": error,
                    "detail": "WFS 레이어는 확인됐으나 공간도형 수신에 실패했습니다."}
        try:
            payload = __import__("json").loads(raw)
        except (ValueError, UnicodeDecodeError):
            return {**source, "status": "WFS_UNVERIFIED", "error_code": "NOT_GEOJSON"}
        features = payload.get("features")
        if payload.get("type") != "FeatureCollection" or not isinstance(features, list):
            return {**source, "status": "WFS_UNVERIFIED", "error_code": "INVALID_RESPONSE"}
        if not features:
            return {**source, "status": "WFS_EMPTY",
                    "detail": "WFS 결과 0건. 토지피복 비해당을 의미하지 않습니다."}
        if len(features) >= WFS_LIMIT:
            return {**source, "status": "WFS_TRUNCATED", "error_code": "PAGE_LIMIT",
                    "detail": "WFS 한도 500건에 도달해 완전한 공간자료로 사용할 수 없습니다."}
        target = shape(geometry)
        clipped = []
        for f in features:
            props = f.get("properties") or {}
            labels = {str(k).lower(): v for k, v in props.items()}
            name = next((str(labels[k]).strip() for k in
                        ("l3_name", "l3_nm", "lcc_nm", "lcover_nm", "class_name", "name",
                         "l3_code", "lcc_code", "lcover_code", "code")
                         if labels.get(k) is not None and str(labels[k]).strip()), None)
            if not name:
                return {**source, "status": "WFS_UNVERIFIED", "error_code": "MISSING_CLASS",
                        "detail": "WFS 도형에는 분류 속성명이 확인되지 않았습니다."}
            geom = shape(f.get("geometry"))
            if not geom.is_valid or geom.geom_type not in ("Polygon", "MultiPolygon"):
                return {**source, "status": "WFS_UNVERIFIED", "error_code": "INVALID_GEOMETRY"}
            intersection = geom.intersection(target)
            if not intersection.is_empty and intersection.area > 0:
                from shapely.geometry import mapping
                clipped.append({"type": "Feature", "geometry": mapping(intersection),
                                "properties": {"name": name}})
        # The survey FACT layer independently validates spatial coverage,
        # class coverage, topology and official provenance before CAD.
        return {**source, "status": "PARTIAL", "features": clipped,
                "verified_feature_type": True, "complete_page": True,
                "layer": LAYER, "as_of": "2025",
                "detail": "공식 GeoServer WFS FeatureType 확인 후 GeoJSON 수신. 면적 수록범위·중첩 검증을 통과해야 CAD 가능."}
    except (requests.RequestException, ET.ParseError, TypeError, ValueError, KeyError):
        return {**source, "status": "WFS_UNVERIFIED", "error_code": "PROVIDER_RESPONSE",
                "detail": "WFS 연결/응답 검증에 실패했습니다."}
