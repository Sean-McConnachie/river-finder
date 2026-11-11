import math

import geopandas as gpd
import numpy as np
from shapely.geometry import LineString

from river_finder.labels import RUNNABLE, Section, parse_grade, section_segments, segment_labels


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
