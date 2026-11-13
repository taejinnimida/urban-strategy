"""Regression checks for landscape candidate discovery and verified selection."""
from pathlib import Path
import landscape_app as app


def test_five_total_with_balanced_ranges_and_optional_traffic():
    rows=[
        {"id":"sub1","category":"subway","band":"near","distance":100},
        {"id":"sub2","category":"subway","band":"near","distance":140},
        {"id":"p1","category":"park","band":"mid","distance":120},
        {"id":"sq1","category":"square","band":"far","distance":800},
        {"id":"i1","category":"intersection","band":"near","distance":10,
         "requiresVerification":True,"roadWidthM":None,"trafficVolume":None},
        {"id":"i2","category":"intersection","band":"near","distance":200,
         "roadWidthM":24,"trafficVolume":None},
        {"id":"i3","category":"intersection","band":"mid","distance":140,
         "roadWidthM":22,"trafficVolume":62000},
    ]
    result=app.viewpoint_rank(app.ViewpointRankReq(candidates=rows,target_per_band=5))
    assert result["policy"]=="national_practical_five"
    assert result["baseline_ids"]==["sub1","p1","sq1","sub2","i3"]
    assert len(result["baseline_ids"])==5
    assert "i1" not in result["selected_ids"]
    assert app._viewpoint_auto_eligible(next(r for r in rows if r["id"]=="i2"))


def test_manual_override_retains_official_and_does_not_backfill():
    rows=[
        {"id":"p1","category":"park","band":"mid","distance":90,"userExcluded":True},
        {"id":"p2","category":"square","band":"mid","distance":100},
        {"id":"extra","category":"intersection","band":"mid","distance":30,
         "userSelected":True,"requiresVerification":True},
        {"id":"official","category":"designated","band":"mid","distance":20,
         "mandatory":True,"userExcluded":True},
    ]
    result=app.viewpoint_rank(app.ViewpointRankReq(candidates=rows,target_per_band=1))
    assert set(result["selected_ids"])=={"extra","official"}
    assert result["baseline_ids"]==["p1"]
    assert result["designated_ids"]==["official"]


def test_vworld_poi_results_survive_overpass_outage(monkeypatch):
    app.VIEWPOINT_CACHE.clear()
    monkeypatch.setattr(app,"_overpass_candidates",
        lambda *args: ([] ,{"name":"OSM Overpass","state":"FAILED","detail":"timeout"}))
    monkeypatch.setattr(app,"_vworld_poi_candidates",
        lambda *args: ([{"id":"park","lat":37.55,"lng":126.99,
                         "category":"park","name":"example"}],
                       {"name":"VWorld POI","state":"OK","queries_ok":1,"queries_total":1}))
    result=app.viewpoint_candidates(lat=37.55,lng=126.99,radius=1100)
    assert result["status"]=="OK"
    assert result["partial"] is True
    assert [x["id"] for x in result["candidates"]]==["park"]
    cached=app.viewpoint_candidates(lat=37.55,lng=126.99,radius=1100)
    assert cached["cached"] is True


def test_seoul_zoning_reuses_official_source_and_map_frontend():
    assert app._SEOUL_ZONING_LAYER=="tile_map:upis_c_uq111"
    text=(Path(app.BASE)/"landscape-review"/"index.html").read_text(encoding="utf-8")
    assert "/api/viewpoint/seoul-zoning/{z}/{x}/{y}.svg" in text
    assert "data.candidates" in text
    assert "loadCandidateZoning();" in text


def test_width_verified_intersection_needs_no_traffic():
    row={"id":"i1","category":"intersection","roadWidthM":21,"trafficVolume":None}
    assert app._viewpoint_auto_eligible(row)
    assert not app._viewpoint_auto_eligible({**row,"roadWidthM":None})
    assert not app._viewpoint_auto_eligible({**row,"roadWidthM":19})
    assert not app._viewpoint_auto_eligible({**row,"requiresVerification":True})
    assert app._viewpoint_auto_eligible({**row,"trafficVolume":3000})


def test_empty_success_is_distinct_from_provider_failure(monkeypatch):
    app.VIEWPOINT_CACHE.clear()
    monkeypatch.setattr(app, "_overpass_candidates",
        lambda *args: ([], {"name": "OSM Overpass", "state": "OK"}))
    monkeypatch.setattr(app, "_vworld_poi_candidates",
        lambda *args: ([], {"name": "VWorld POI", "state": "OK"}))
    result=app.viewpoint_candidates(lat=37.56,lng=126.98,radius=1150)
    assert result["status"]=="OK"
    assert result["empty"] is True
    assert result["candidates"]==[] and result["elements"]==[]


def test_vworld_poi_request_has_server_origin(monkeypatch):
    monkeypatch.setenv("VWORLD_API_KEY","test-only-key")
    monkeypatch.setattr(app, "_domain", lambda: "https://landscape.example")
    seen=[]
    class FakeResponse:
        def raise_for_status(self): pass
        def json(self): return {"response":{"status":"NOT_FOUND"}}
    def fake_get(url, **kwargs):
        seen.append(kwargs)
        return FakeResponse()
    monkeypatch.setattr(app.requests, "get", fake_get)
    rows,state=app._vworld_poi_candidates(37.56,126.98,1150)
    assert not rows and state["state"]=="OK"
    assert len(seen)==7
    assert all(req["headers"]["Referer"]=="https://landscape.example/" for req in seen)

def test_centerline_adapter_reuses_seoul_width_rules():
    import landscape_roads as roads
    assert roads._road_width_m({"ROAD_BT":"20"})==20
    assert roads._road_width_m({"road_bt":"24.5m"})==24.5
    assert roads._road_width_m({"ROAD_BT":""}) is None
    assert roads._road_width_m({"ROAD_BT":"폭원 미확인"}) is None
    assert roads._road_width_m({"ROAD_BT":"12"})==12


def test_centerline_crossing_geometry_candidate_with_no_traffic(monkeypatch):
    import landscape_roads as roads
    from shapely.geometry import LineString
    from pyproj import Transformer
    x,y=Transformer.from_crs(4326,5179,always_xy=True).transform(126.978,37.5665)
    sample=[
        {"geometry":LineString([(x-80,y),(x+80,y)]),
         "properties":{"RDS_MAN_NO":"A","ROAD_BT":25,"RN":"동서대로"}},
        {"geometry":LineString([(x,y-80),(x,y+80)]),
         "properties":{"RDS_MAN_NO":"B","ROAD_BT":22,"RN":"남북대로"}},
        {"geometry":LineString([(x+100,y-80),(x+100,y+80)]),
         "properties":{"RDS_MAN_NO":"C","ROAD_BT":12,"RN":"국지도로"}},
    ]
    monkeypatch.setattr(roads,"_source_shp",lambda:"/fake/TL_SPRD_MANAGE")
    monkeypatch.setattr(roads,"_road_shape_records",lambda *args: sample)
    result=roads.candidates_from_centerlines(37.5665,126.978,1000)
    assert result["status"]=="OK"
    assert len(result["candidates"])==1
    candidate=result["candidates"][0]
    assert candidate["roadWidthM"]==22
    assert candidate["trafficVolume"] is None
    assert candidate["junctionVerified"] is False
    assert candidate["source"]=="TL_SPRD_MANAGE ROAD_BT"


def test_road_source_missing_never_creates_fake_intersections(monkeypatch):
    import landscape_roads as roads
    monkeypatch.setattr(roads,"_source_shp",lambda:None)
    result=roads.candidates_from_centerlines(37.5665,126.978,1000)
    assert result["status"]=="DATA_UNAVAILABLE"
    assert not result["candidates"]
