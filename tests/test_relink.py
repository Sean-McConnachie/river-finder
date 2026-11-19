"""Reconnecting national reaches that stop at a tile edge or a lake (relink.py, rf-t26.8)."""

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import shapely
from rasterio.transform import from_origin
from shapely.geometry import LineString

from river_finder import nz
from river_finder.relink import Rec2Graph, check_network, relink_outlets, relink_table, roots, tile_local_km2

# Our reaches: name -> (line, area km2, downstream name or None, left its tile's core, tile).
# 1. Tile edge at x = 1000: A's river ends at (1010, 0); B routed it 60 m north, so the 24 m stitch found
#    nothing. B's continuation starts at b0 (its area came from the REC2 inflow).
# 2. Lake: c1 ends at a lake shore at x = 400; nothing of ours along the lake's REC2 line until the outflow
#    river d0 at x = 1900. A small lake-side stream (l0) runs near the line but is far too small.
# 3. Flat valley: e1 ends at the edge; B's continuation f0-f1 runs 150 m off REC2 and only f2 comes within
#    the snap distance, so the link must climb from f2 to the top of the continuation, f0.
# 4. A coastal mouth (g0, not eligible) and an edge outlet whose only downstream reach is too small (h0).
# 5. k0's REC2 path passes m0, a bigger river whose catchment is complete (its cells supply all its area):
#    k0 does not drain there in the next tile's routing, so linking would count k0's area twice.
SEGS = {
    "a0": ([(0, 0), (300, 0)], 10.0, "a1", False, "A"),
    "a1": ([(300, 0), (600, 0)], 10.5, "a2", False, "A"),
    "a2": ([(600, 0), (1010, 0)], 11.0, None, True, "A"),
    "b0": ([(1050, 60), (1300, 60)], 11.2, "b1", False, "B"),
    "b1": ([(1300, 60), (1600, 60)], 11.5, "b2", False, "B"),
    "b2": ([(1600, 60), (2000, 60)], 12.0, None, False, "B"),
    "c0": ([(0, 5000), (200, 5000)], 49.0, "c1", False, "A"),
    "c1": ([(200, 5000), (400, 5000)], 50.0, None, True, "A"),
    "l0": ([(500, 5080), (1800, 5080)], 5.0, None, False, "B"),
    "d0": ([(1900, 5000), (2200, 5000)], 52.0, "d1", False, "B"),
    "d1": ([(2200, 5000), (2600, 5000)], 53.0, None, False, "B"),
    "e0": ([(0, 20000), (500, 20000)], 30.0, "e1", False, "A"),
    "e1": ([(500, 20000), (1000, 20000)], 31.0, None, True, "A"),
    "f0": ([(1000, 20150), (1500, 20150)], 31.2, "f1", False, "B"),
    "f1": ([(1500, 20150), (2000, 20150)], 31.3, "f2", False, "B"),
    "f2": ([(2000, 20150), (2500, 20000)], 31.5, None, False, "B"),
    "g0": ([(0, 30000), (500, 30000)], 8.0, None, False, "A"),
    "h0": ([(0, 40000), (1000, 40000)], 40.0, None, True, "A"),
    "h1": ([(1000, 40000), (2000, 40000)], 30.0, None, False, "B"),
    "k0": ([(0, 50000), (1000, 50000)], 20.0, None, True, "A"),
    "m0": ([(1000, 50050), (2000, 50050)], 100.0, None, False, "B"),
}
HEADS = {"b0": 0.2, "d0": 2.0, "f0": 0.2}   # area came in as a REC2 inflow; their own cells: this much
# REC2: (nzsegment, line, cumulative area, next nzsegment or None)
REC2 = [
    (1, [(0, 30), (1000, 30)], 11.0, 2), (2, [(1000, 30), (2000, 30)], 12.0, None),
    (10, [(0, 5000), (1900, 5000)], 50.0, 11), (11, [(1900, 5000), (2600, 5000)], 53.0, None),
    (20, [(0, 20000), (1000, 20000)], 31.0, 21), (21, [(1000, 20000), (2500, 20000)], 32.0, None),
    (30, [(0, 30000), (600, 30000)], 8.0, None),
    (40, [(0, 40000), (1000, 40000)], 40.0, 41), (41, [(1000, 40000), (2000, 40000)], 41.0, None),
    (50, [(0, 50000), (1000, 50000)], 20.0, 51), (51, [(1000, 50000), (2000, 50000)], 120.0, None),
]


def _rec2() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({"nzsegment": [r[0] for r in REC2], "HydroID": [r[0] for r in REC2],
                             "NextDownID": [r[3] or -1 for r in REC2], "cum_area_km2": [r[2] for r in REC2]},
                            geometry=[LineString(r[1]) for r in REC2], crs="EPSG:2193")


def _network(names):
    pos = {k: i for i, k in enumerate(names)}
    lines = [LineString(SEGS[k][0]) for k in names]
    down = np.array([pos[SEGS[k][2]] if SEGS[k][2] else -1 for k in names])
    area = np.array([SEGS[k][1] for k in names])
    end = np.array([SEGS[k][0][-1] for k in names], dtype=float)
    elig = np.array([SEGS[k][3] for k in names])
    xy, ix = shapely.get_coordinates(np.array(lines, dtype=object), return_index=True)
    return pos, down, area, end, elig, xy, ix, lines


def _local_km2(names, down, area):
    # pixel local areas: a reach's own cells supply its area minus its upstream reaches' (heads: HEADS)
    loc = area - np.bincount(down[down >= 0], weights=area[down >= 0], minlength=len(names))
    return np.array([HEADS.get(k, v) for k, v in zip(names, loc, strict=True)])


def _run(names, pixels=True, **kw):
    pos, down, area, end, elig, xy, ix, _ = _network(names)
    rank = {k: i for i, k in enumerate(sorted(names))}
    new, log = relink_outlets(down, area, end, elig, xy, ix, Rec2Graph.from_frame(_rec2()),
                              local_km2=_local_km2(names, down, area) if pixels else None,
                              key=np.array([rank[k] for k in names]), **kw)
    return pos, down, new, log.set_index(log["row"].map({v: k for k, v in pos.items()}))


def test_relinks_across_tile_edge_lake_and_flat_valley():
    names = list(SEGS)
    pos, down, new, log = _run(names)
    assert new[pos["a2"]] == pos["b0"]          # tile edge: to the top of B's continuation
    assert new[pos["c1"]] == pos["d0"]          # lake: through the lake to the outflow river, not the stream
    assert new[pos["e1"]] == pos["f0"]          # flat valley: first hit f2, then up to f0
    assert log.loc["e1", "first_hit"] == pos["f2"] and log.loc["e1", "via_head"]
    assert new[pos["g0"]] == -1                 # a coastal mouth is not eligible
    assert new[pos["h0"]] == -1 and log.loc["h0", "status"] == "reaches on the REC2 path too small"
    assert new[pos["k0"]] == -1 and log.loc["k0", "status"] == "reaches on the REC2 path already complete"
    changed = np.flatnonzero(new != down)
    assert set(changed) == {pos["a2"], pos["c1"], pos["e1"]}
    assert (log["status"] == "linked").sum() == 3
    r = check_network(new, np.array([SEGS[k][1] for k in names]))
    assert r["in_cycles"] == 0 and r["reach_no_outlet"] == 0 and r["decreases"] == 0
    # pixel local areas summed down the new network give every reach its area_km2
    pos_, down_, area, *_ = _network(names)
    acc = check_network(new, area, _local_km2(names, down_, area))["_summed_local_km2"]
    for k in ("b0", "b2", "d1", "f2"):
        assert abs(acc[pos[k]] - SEGS[k][1]) < 0.25


def test_without_pixel_areas_a_complete_river_is_not_detected():
    pos, _, new, log = _run(list(SEGS), pixels=False)
    assert new[pos["a2"]] == pos["b0"] and new[pos["e1"]] == pos["f0"]
    assert new[pos["k0"]] == pos["m0"]       # by area alone, m0 looks like k0's way on


def test_links_do_not_depend_on_row_order():
    names = list(SEGS)
    _, _, new, _ = _run(names)
    rev = names[::-1]
    pos_r, _, new_r, _ = _run(rev)
    back = {i: k for k, i in pos_r.items()}
    pos = {k: i for i, k in enumerate(names)}
    # same links whatever the row order
    for k in names:
        i = new[pos[k]]
        j = new_r[pos_r[k]]
        assert (names[i] if i >= 0 else None) == (back[j] if j >= 0 else None)


def test_no_cycle_when_two_outlets_point_into_each_other():
    # o1's REC2 path passes q0 (o2's river) and o2's passes p0/p1 (o1's river). Linking both would make a
    # loop; the bigger outlet links first and the other is refused.
    segs = {
        "p0": ([(0, 10000), (500, 10000)], 20.0, "p1"), "p1": ([(500, 10000), (1000, 10000)], 21.0, None),
        "q0": ([(1000, 10200), (1500, 10200)], 20.5, "q1"), "q1": ([(1500, 10200), (2000, 10200)], 20.8, None),
    }
    rec2 = gpd.GeoDataFrame({"nzsegment": [1, 2, 3], "HydroID": [1, 2, 3], "NextDownID": [2, 3, -1],
                             "cum_area_km2": [21.0, 21.5, 22.0]},
                            geometry=[LineString([(500, 10000), (1000, 10000), (1000, 10200), (1500, 10200)]),
                                      LineString([(1500, 10200), (2000, 10200)]),
                                      LineString([(2000, 10200), (2000, 10000), (600, 10000)])], crs="EPSG:2193")
    names = list(segs)
    pos = {k: i for i, k in enumerate(names)}
    lines = [LineString(segs[k][0]) for k in names]
    down = np.array([pos[segs[k][2]] if segs[k][2] else -1 for k in names])
    area = np.array([segs[k][1] for k in names])
    end = np.array([segs[k][0][-1] for k in names], dtype=float)
    xy, ix = shapely.get_coordinates(np.array(lines, dtype=object), return_index=True)
    new, log = relink_outlets(down, area, end, np.ones(4, bool), xy, ix, Rec2Graph.from_frame(rec2))
    assert new[pos["p1"]] == pos["q0"]
    assert new[pos["q1"]] == -1
    assert log.set_index("row").loc[pos["q1"], "status"] == "would make a cycle"
    assert (roots(new) >= 0).all()
    assert check_network(new, area)["in_cycles"] == 0


def _write_tiles(tmp_path, names):
    """Each tile's segments_core.gpkg, and a local_catchment_core.tif (100 m cells, 0.01 km2) holding as
    many cells of each reach's gid as its pixel local area."""
    tiles = tmp_path / "tiles"
    lines = {k: LineString(SEGS[k][0]) for k in names}
    gid = {k: (1 if SEGS[k][4] == "A" else 2) * 1_000_000 + i for i, k in enumerate(names)}
    _, down, area, *_ = _network(names)
    loc = dict(zip(names, _local_km2(names, down, area), strict=True))
    for tile in ("A", "B"):
        ks = [k for k in names if SEGS[k][4] == tile]
        d = tiles / tile
        d.mkdir(parents=True)
        cells = np.concatenate([np.full(round(loc[k] / 0.01), gid[k], dtype=np.int64) for k in ks])
        lc = np.full(200 * 200, -1, dtype=np.int64)
        lc[:len(cells)] = cells
        with rasterio.open(d / "local_catchment_core.tif", "w", driver="GTiff", width=200, height=200, count=1,
                           dtype="int64", nodata=-1, crs="EPSG:2193",
                           transform=from_origin(0, 60000, 100, 100)) as dst:
            dst.write(lc.reshape(200, 200), 1)
        gpd.GeoDataFrame({"gid": [gid[k] for k in ks], "end_x": [SEGS[k][0][-1][0] for k in ks],
                          "end_y": [SEGS[k][0][-1][1] for k in ks], "exits_core": [SEGS[k][3] for k in ks],
                          "length_m": [lines[k].length for k in ks]},
                         geometry=[lines[k] for k in ks], crs="EPSG:2193").to_file(d / "segments_core.gpkg")
    _rec2().to_file(tmp_path / "rec2.gpkg")
    return gid


def test_relink_table_keeps_rows_and_columns(tmp_path):
    names = list(SEGS)
    gid = _write_tiles(tmp_path, names)
    rng = np.random.default_rng(1)
    order = [names[i] for i in rng.permutation(len(names))]        # table order differs from the tiles'
    pos = {k: i for i, k in enumerate(order)}
    t = pd.DataFrame({
        "seg_id": np.arange(len(order)), "gid": [gid[k] for k in order],
        "down_gid": [gid[SEGS[k][2]] if SEGS[k][2] else -1 for k in order],
        "down": [pos[SEGS[k][2]] if SEGS[k][2] else -1 for k in order],
        "area_km2": [SEGS[k][1] for k in order], "river": order, "vi": np.arange(len(order))[::-1],
        "up": -1})
    loc = tile_local_km2(tmp_path / "tiles", t["gid"].to_numpy())
    _, down, area, *_ = _network(names)
    want = dict(zip(names, _local_km2(names, down, area), strict=True))
    assert np.allclose(loc, [want[k] for k in order])
    fixed, log = relink_table(t, tmp_path / "tiles", tmp_path / "rec2.gpkg", log=lambda m: None)
    same = [c for c in t.columns if c not in ("down", "down_gid", "up")]
    pd.testing.assert_frame_equal(fixed[same], t[same])
    assert list(fixed.index) == list(t.index)
    d = fixed["down"].to_numpy()
    assert d[pos["a2"]] == pos["b0"] and d[pos["c1"]] == pos["d0"] and d[pos["e1"]] == pos["f0"]
    assert (fixed["down_gid"].to_numpy() == np.where(d >= 0, fixed["gid"].to_numpy()[np.maximum(d, 0)], -1)).all()
    assert fixed["up"].to_numpy()[pos["b0"]] == pos["a2"]
    assert set(log.loc[log["status"] == "linked", "gid"]) == {gid["a2"], gid["c1"], gid["e1"]}
    # a second pass links nothing new
    again, _ = relink_table(fixed, tmp_path / "tiles", tmp_path / "rec2.gpkg", log=lambda m: None)
    assert (again["down"].to_numpy() == d).all()

    # nz.merge_segments' relink (the rebuild path) makes the same links in gid terms
    g = pd.concat([gpd.read_file(tmp_path / "tiles" / k / "segments_core.gpkg") for k in ("A", "B")],
                  ignore_index=True)
    gd = {gid[k]: (gid[SEGS[k][2]] if SEGS[k][2] else -1) for k in names}
    g["down_gid"] = g["gid"].map(gd)
    g["area_km2"] = g["gid"].map({gid[k]: SEGS[k][1] for k in names})
    g["river"] = ""
    g = gpd.GeoDataFrame(g, geometry="geometry", crs="EPSG:2193")
    old = nz.nz_dir
    nz.nz_dir = lambda: tmp_path
    try:
        nz._relink(g, log=lambda m: None)
    finally:
        nz.nz_dir = old
    assert dict(zip(g["gid"], g["down_gid"], strict=True)) == dict(zip(fixed["gid"], fixed["down_gid"], strict=True))
    assert (tmp_path / "relink_log.csv").exists()
