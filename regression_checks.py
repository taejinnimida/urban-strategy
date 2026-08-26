"""배포 전 핵심 회귀검사. 실행: python regression_checks.py"""

from pathlib import Path

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

    outside = box(127.49, 37.49, 127.50, 37.50)
    empty = app.analyze_renewal_intersections(outside.__geo_interface__)
    assert empty["status"] == "none"
    assert empty["renewal_area_type"] == "none"


def check_area_gate() -> None:
    result = app.evaluate_redevelopment({"area_m2": 1399})
    area = next(row for row in result["checks"] if row["id"] == "AREA")
    assert area["status"] == "FAIL"
    assert result["physical_eligibility"]["status"] == "FAIL"


def check_centerline_width_buffer() -> None:
    assert app._road_width_m({"road_bt": "8.0m"}) == 8.0
    line = LineString([(126.977, 37.565), (126.978, 37.565)])
    polygon = app._centerline_road_polygon(line, 8.0)
    assert polygon is not None
    assert polygon.geom_type in {"Polygon", "MultiPolygon"}
    assert polygon.area > 0


def check_feedback_and_ui() -> None:
    feedback_id = app._store_feedback({
        "visitor_id": "regression-visitor",
        "category": "data",
        "message": "회귀검사",
        "pnu_list": [],
        "recommendations": [],
    })
    assert any(row.get("id") == feedback_id for row in app._feedback_rows())
    assert app._set_feedback_status(feedback_id, "done")

    html = Path(app.STATIC_DIR, "app.html").read_text(encoding="utf-8")
    for text in ("오류·개선의견", 'href="/admin"', "planningRenewalSummary", "실폭도로·접도율 공간분석"):
        assert text in html
    assert "/api/spatial/renewal-intersections" in html
    assert "/api/spatial/roads" in html
    assert "st.areaGate!=='PASS'||st.structural!=='PASS'" in html


def main() -> None:
    check_measurement()
    check_renewal_server_intersection()
    check_area_gate()
    check_centerline_width_buffer()
    check_feedback_and_ui()
    print("v2.3.0 regression checks: PASS")


if __name__ == "__main__":
    main()
