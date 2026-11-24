import math

import geopandas as gpd
import numpy as np
from shapely.geometry import LineString

from river_finder.labels import (
    RUNNABLE,
    Section,
    one_path,
    parse_grade,
    section_placement,
    section_segments,
    segment_labels,
)


def test_parse_grade():
    assert parse_grade("3") == 3
    assert abs(parse_grade("4-4+") - (4 + 4 + 1 / 3) / 2) < 1e-9
    assert abs(parse_grade("III+") - 10 / 3) < 1e-9
    assert parse_grade("V") == 5
    assert math.isnan(parse_grade(None)) and math.isnan(parse_grade("flood only"))


def _chain():
    # five 250 m segments flowing east along y = 5_800_000 (NZTM), named "Test River"
    x0 = 1_850_000
    geoms = [LineString([(x0 + 250 * i, 5_800_000), (x0 + 250 * (i + 1), 5_800_000)]) for i in range(5)]
    return gpd.GeoDataFrame({
        "seg_id": range(5), "down_seg": [1, 2, 3, 4, -1], "up_seg": [-1, 0, 1, 2, 3], "length_m": 250.0,
        "area_km2": [3, 4, 5, 6, 7], "grad_s": [5, 30, 30, 30, 5], "river": "Test River",
    }, geometry=geoms, crs="EPSG:2193")


def _ll(x, y):
    from pyproj import Transformer

    return Transformer.from_crs("EPSG:2193", "EPSG:4326", always_xy=True).transform(x, y)


def test_extents():
    g = _chain()
    whole = Section("r", "Test River", "runnable")
    assert section_segments(g, whole) == [1, 2, 3]  # steep reaches only
    s = Section("r", "Test River", "runnable", put_in=_ll(1_850_300, 5_800_000), take_out=_ll(1_850_800, 5_800_000))
    assert section_segments(g, s) == [1, 2, 3]
    s = Section("r", "Test River", "runnable", outlet=_ll(1_851_100, 5_800_000), upstream_km=0.6)
    assert section_segments(g, s) == [4, 3, 2]


def test_segment_labels_grades():
    g = _chain()
    secs = [Section("r", "Test River", "runnable", grade="4-4+")]
    status, grade, held, lab, _ = segment_labels(g, "r", secs)
    assert list(status) == [0, RUNNABLE, RUNNABLE, RUNNABLE, 0]
    assert np.isnan(grade[0]) and grade[2] > 4


def _valleys():
    """Two valleys, both with reaches called "Test River" (250 m each, NZTM):
    A: 0 -> 1 -> 2 -> 3 -> 4 -> 5 (outlet), reach 2 too flat for the name rule (a one-reach gap);
    B: 6 -> 7 -> 8 (outlet), 20 km north, shorter."""
    x0, y0 = 1_850_000, 5_800_000
    ga = [LineString([(x0 + 250 * i, y0), (x0 + 250 * (i + 1), y0)]) for i in range(6)]
    gb = [LineString([(x0 + 250 * i, y0 + 20_000), (x0 + 250 * (i + 1), y0 + 20_000)]) for i in range(3)]
    return gpd.GeoDataFrame({
        "seg_id": range(9), "down_seg": [1, 2, 3, 4, 5, -1, 7, 8, -1], "up_seg": [-1, 0, 1, 2, 3, 4, -1, 6, 7],
        "length_m": 250.0, "area_km2": [3, 4, 5, 6, 7, 8, 3, 4, 5],
        "grad_s": [30, 30, 5, 30, 30, 5, 30, 30, 30], "river": "Test River",
    }, geometry=ga + gb, crs="EPSG:2193")


def test_one_path_keeps_one_valley_and_bridges_a_small_gap():
    g = _valleys()
    down, L, A = g["down_seg"].to_numpy(), g["length_m"].to_numpy(), g["area_km2"].to_numpy()
    names = g["river"].to_numpy()
    rows = [0, 1, 3, 4, 6, 7, 8]                                   # what the name rule matches
    kept, info = one_path(rows, down, L, A, names, "Test River")
    assert kept.tolist() == [0, 1, 3, 4]                          # the longer valley, joined across reach 2
    assert info["pieces"] == 2 and info["branches"] == 1 and info["dropped_reaches"] == 3
    assert info["dropped_km"] == 0.75 and info["gap_rows"].tolist() == [2]
    # coordinates choose the piece nearest them, even when it is the shorter one
    mid = g.geometry.interpolate(0.5, normalized=True)
    xy = np.column_stack([mid.x, mid.y])
    kept, info = one_path(rows, down, L, A, names, "Test River", xy=xy, points=[(1_850_400, 5_820_100)])
    assert kept.tolist() == [6, 7, 8] and info["dropped_reaches"] == 4
    # a known river mouth (regions/*.toml) picks the piece whose water flows past it
    kept, _ = one_path(rows, down, L, A, names, "Test River", xy=xy, outlet=(1_850_700, 5_820_000))
    assert kept.tolist() == [6, 7, 8]


def test_river_outlet_from_region_files():
    from river_finder.labels import river_outlet

    x, y = river_outlet(Section("tutamoe", "Waima River", "runnable"))   # the Hokianga mouth, in NZTM
    assert 1_640_000 < x < 1_700_000 and 6_040_000 < y < 6_100_000
    assert river_outlet(Section("tutamoe", "Other Stream", "runnable")) is None


def test_one_path_gap_rule():
    # 0 (matched) -> six 250 m reaches -> 7 (matched): 1.5 km, too long for a gap of any reaches,
    # but it joins while the gap stays on the river (same name, or no name)
    down = np.array([1, 2, 3, 4, 5, 6, 7, -1])
    L, A = np.full(8, 250.0), np.arange(1.0, 9.0)
    for gap_names, pieces in ((["Test River"] * 6, 1), ([""] * 6, 1), (["Test River"] * 5 + ["Other Stream"], 2)):
        names = np.array(["Test River", *gap_names, "Test River"], dtype=object)
        kept, info = one_path([0, 7], down, L, A, names, "Test River")
        assert info["pieces"] == pieces
        assert kept.tolist() == ([0, 7] if pieces == 1 else [7])   # tie on length: the larger catchment


def test_one_path_keeps_one_branch():
    # two forks named alike: 0 -> 1 -> 2 and 3 -> 2, then 2 -> 4; the longer fork is kept
    down = np.array([1, 2, 4, 2, -1])
    L, A = np.full(5, 250.0), np.array([2.0, 3.0, 9.0, 4.0, 10.0])
    names = np.array(["Test River"] * 5, dtype=object)
    kept, info = one_path(range(5), down, L, A, names, "Test River")
    assert kept.tolist() == [0, 1, 2, 4] and info["pieces"] == 1 and info["branches"] == 2
    xy = np.array([[0, 0], [250, 0], [500, 0], [500, 900], [750, 0]], dtype=float)
    kept, _ = one_path(range(5), down, L, A, names, "Test River", xy=xy, points=[(500, 1000)])
    assert kept.tolist() == [2, 3, 4]                              # a put-in on the short fork


def test_name_rule_section_on_one_path():
    g = _valleys()
    rows, how, info = section_placement(g, Section("r", "Test River", "runnable"))
    assert rows == [0, 1, 3, 4] and how == "name (1 of 2 pieces)" and info["dropped_reaches"] == 3
    status, *_ = segment_labels(g, "r", [Section("r", "Test River", "runnable")])
    assert status.tolist() == [1, 1, 0, 1, 1, 0, 0, 0, 0]
    s = Section("r", "Test River", "runnable", take_out=_ll(1_850_700, 5_820_000))   # a point in valley B only
    assert section_segments(g, s) == [6, 7, 8]
