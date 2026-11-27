"""National tiling: REC2 inflows and outlets at the box edge, and the viewer's segment packing."""

import geopandas as gpd
import numpy as np
from rasterio.transform import from_origin
from shapely.geometry import LineString

from river_finder.nz import border_outlets, inflow_points
from river_finder.routing import route
from river_finder.viewer_nz import pack_segments


def _reach(seg, up, down, cum, cat):
    return {"nzsegment": seg, "upcoordX": up[0], "upcoordY": up[1], "downcoordX": down[0], "downcoordY": down[1],
            "cum_area_km2": cum, "cat_area_km2": cat, "geometry": LineString([up, down])}


def test_inflow_and_outlet_points():
    bb = (0.0, 0.0, 1000.0, 1000.0)
    rec2 = gpd.GeoDataFrame([
        _reach(1, (500, -500), (500, 500), 100.0, 2.0),   # enters through the bottom edge
        _reach(2, (500, 500), (1500, 500), 103.0, 1.0),   # leaves through the right edge
        _reach(3, (200, 200), (300, 300), 1.0, 1.0),      # wholly inside
    ], crs="EPSG:2193")
    pts = inflow_points(rec2, bb, inset_m=100.0)
    assert len(pts) == 1
    x, y, area, seg = pts[0]
    assert seg == 1 and abs(x - 500) < 1e-6 and abs(y - 100) < 1e-6
    assert abs(area - (100.0 - 2.0 * 0.5)) < 1e-6   # half the reach (and its local area) is inside
    seed = border_outlets(rec2, bb, from_origin(0, 1000, 10, 10), (100, 100), half_cells=2)
    rows, cols = np.nonzero(seed)
    assert set(cols) == {99} and set(rows) == {48, 49, 50, 51, 52}


def test_lake_at_box_edge_drains_to_real_outlet():
    # A flat lake touching the left edge drains to a river leaving on the right. With the whole
    # border as outlet, the lake would drain out of the left edge.
    z = np.full((20, 40), 10.0)
    z[:, :20] = 5.0                         # lake surface, touching the left edge
    z[9:11, 20:] = np.linspace(5, 1, 20)    # outlet channel
    valid = np.ones_like(z, dtype=bool)
    seed = np.zeros_like(valid)
    seed[9:11, -1] = True
    out = route(z, valid, 8.0, border_seed=seed)
    total = valid.sum() * 64.0
    assert np.nansum(out["acc"][9:11, -1]) == total   # everything leaves through the two outlet cells


def test_pack_segments_round_trip():
    S = {"rivers": ["A"], "listed": [], "features": ["f1", "f2"], "sections": [],
         "d": [1, -1], "u": [-1, 0], "r": [0, -1], "len": [250, 240], "g": [12.3, 45.6], "s50": [80.0, 120.5],
         "gorge": [12.0, 30.0], "zt": [120.4, 100.0], "zb": [100.0, 89.1], "head": [50.0, 60.0],
         "mel": [0.1234, 0.5], "hw": [0.5, 1.25], "reg": [55.5, 99.9], "sig": [10.0, 20.0],
         "a": [2.5, 300.0], "maf": [5.0, 120.0], "ksn": [30.0, 80.0], "pow": [0.25, 4.0], "gr": [None, 4.17],
         "hard": [1, 0], "sta": [0, 1], "lho": [0, 0], "lab": [0, 1], "ho": [0, 0],
         "z": [[0.5, -1.2], [2.0, 0.0]],
         "c": [[[175.9, -37.8], [175.9012, -37.8011]], [[175.9012, -37.8011], [175.902, -37.8], [175.903, -37.799]]]}
    blob, head = pack_segments(S)
    out = {}
    for f in head["fields"]:
        if f["kind"] == "i32":
            out[f["k"]] = np.frombuffer(blob, "<i4", 2, f["off"])
        elif f["kind"] == "u8":
            out[f["k"]] = np.frombuffer(blob, np.uint8, 2, f["off"])
        else:
            q = np.frombuffer(blob, "<u2", 2, f["off"]).astype(float)
            if f["kind"] == "u16log":
                out[f["k"]] = 10 ** (q * f["scale"] + f["add"])
            elif f["kind"] == "u16null":
                out[f["k"]] = np.where(q == 0, np.nan, (q - 1) * f["scale"] + f["add"])
            else:
                out[f["k"]] = q * f["scale"] + f["add"]
    assert list(out["d"]) == [1, -1]
    assert np.allclose(out["g"], S["g"], atol=0.05)
    assert np.allclose(out["zt"], S["zt"], atol=0.05)
    assert np.allclose(out["a"], S["a"], rtol=1e-3)
    assert np.isnan(out["gr"][0]) and abs(out["gr"][1] - 4.17) < 0.01
    C = head["coords"]
    cnt = np.frombuffer(blob, "<u2", 2, C["count"])
    first = np.frombuffer(blob, "<i4", 4, C["first"]).reshape(2, 2)
    dl = np.frombuffer(blob, "<i2", 2 * C["ndelta"], C["delta"]).reshape(-1, 2)
    assert list(cnt) == [2, 3] and C["ndelta"] == 3
    last = (first[1] + dl[1:].sum(0)) * 1e-5
    assert np.allclose(last, S["c"][1][-1], atol=1e-5)
    z = np.frombuffer(blob, np.int8, 4, head["z"]["off"]).reshape(2, 2) * 0.1
    assert np.allclose(z, S["z"], atol=0.05)


def test_viewer_build_writes_data_files_only(tmp_path):
    """``nz viewer`` writes the web app's region data and ``regions_index.json``; the legacy Leaflet page and its
    page-only paths are gone (rf-aq6.18)."""
    import json

    import pytest

    from river_finder import cli, national, viewer_nz

    out = tmp_path / "viewer_nz"
    with national.using_root(tmp_path / "nz"):   # no scored region, no reference area
        p = viewer_nz.build(out, refs=[])
    assert p == out / "regions_index.json" and json.loads(p.read_text()) == []
    assert [f.name for f in out.iterdir()] == ["regions_index.json"]
    for argv in (["nz", "viewer", "--page-only"], ["viewer", "kaimai"]):
        with pytest.raises(SystemExit):
            cli.main(argv)
