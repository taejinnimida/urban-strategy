"""Regression checks for landscape candidate discovery and verified selection."""
from pathlib import Path
import landscape_app as app


def test_priority_and_unverified_intersection_are_not_invented():
    rows=[
        {"id":"p1","category":"park","band":"near","distance":90},
        {"id":"i1","category":"intersection","band":"near","distance":10,
         "requiresVerification":True,"roadWidthM":None,"trafficVolume":None},
        {"id":"i2","category":"intersection","band":"near","distance":200,
         "roadWidthM":24,"trafficVolume":53000},
        {"id":"i3","category":"intersection","band":"near","distance":140,
         "roadWidthM":22,"trafficVolume":62000},
    ]
    result=app.viewpoint_rank(app.ViewpointRankReq(candidates=rows,target_per_band=3))
    assert result["bands"]["near"]["selected_ids"]==["p1","i3","i2"]
    assert "i1" not in result["selected_ids"]


def test_user_click_override_may_exceed_baseline_but_never_removes_mandatory():
    rows=[
        {"id":"p1","category":"park","band":"mid","distance":90,"userExcluded":True},
        {"id":"p2","category":"square","band":"mid","distance":100},
        {"id":"extra","category":"intersection","band":"mid","distance":30,
         "userSelected":True,"requiresVerification":True},
        {"id":"official","category":"designated","band":"mid","distance":20,
         "mandatory":True,"userExcluded":True},
    ]
    result=app.viewpoint_rank(app.ViewpointRankReq(candidates=rows,target_per_band=1))
    ids=set(result["bands"]["mid"]["selected_ids"])
    assert ids=={"extra","official"}


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


def test_no_road_volume_is_promoted_to_measured_traffic():
    row={"id":"i1","category":"intersection","roadWidthM":21,"trafficVolume":None}
    assert not app._viewpoint_auto_eligible(row)
    assert not app._viewpoint_auto_eligible({**row,"trafficVolume":3000,"roadWidthM":None})
    assert app._viewpoint_auto_eligible({**row,"trafficVolume":3000})
