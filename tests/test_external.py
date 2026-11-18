import geopandas as gpd
import numpy as np
from shapely.geometry import LineString, Point

from river_finder import external as X


def _lines():
    # two parallel east-flowing reaches 1 km apart
    return gpd.GeoSeries([LineString([(0, 0), (1000, 0)]), LineString([(0, 1000), (1000, 1000)])], crs=2193)


def test_reach_access_nearest_per_class():
    acc = gpd.GeoDataFrame({"cls": ["road", "track", "foot"]},
                           geometry=[LineString([(0, -300), (1000, -300)]),      # 300 m south of reach 0
                                     LineString([(500, 1200), (600, 1200)]),     # 200 m north of reach 1
                                     LineString([(500, 0), (500, 50)])], crs=2193)  # touches reach 0
    d = X.reach_access(_lines(), acc)
    assert np.allclose(d["d_road_m"], [300, 1300])
    assert np.allclose(d["d_track_m"], [1200, 200])
    assert np.allclose(d["d_foot_m"], [0, 950])


def test_reach_access_caps_far_lines():
    acc = gpd.GeoDataFrame({"cls": ["road", "track", "foot"]},
                           geometry=[LineString([(0, 50_000), (1, 50_000)])] * 3, crs=2193)
    d = X.reach_access(_lines(), acc)
    assert (d["d_road_m"] == X.MAX_DIST).all()


def test_reach_falls_snaps_and_keeps_nearby_only():
    falls = gpd.GeoDataFrame(
        {"kind": ["waterfall", "waterfall", "waterfall", "rapids", "waterfall"],
         "name": ["A", "B", "C", "R", "Far"], "height_m": [12.0, np.nan, 30.0, np.nan, 5.0]},
        geometry=[Point(200, 40), Point(800, -60), Point(500, 1500), Point(300, 990), Point(9000, 9000)], crs=2193)
    per, wf = X.reach_falls(_lines(), falls)
    assert list(per["falls_n"]) == [2, 0]          # C is 500 m away: on the map but not on a reach
    assert list(per["falls_max_h_m"]) == [12.0, 0.0]
    assert list(wf["name"]) == ["A", "B", "C", "R"]  # "Far" is outside the region
    assert list(wf["reach"]) == [0, 0, -1, -1]       # rapids are never snapped


def test_private_roads_and_heights():
    assert X._private([{"access_type": "denied", "when": {"mode": None}}])
    assert X._private([{"access_type": "denied", "when": {"mode": ["motor_vehicle"]}}])
    assert not X._private([{"access_type": "denied", "when": {"mode": ["hgv"]}}])
    assert not X._private([{"access_type": "allowed", "when": {"mode": ["bicycle"]}}])
    assert X._height("22.0") == 22.0 and X._height("~5 m") == 5.0 and np.isnan(X._height("tall"))
