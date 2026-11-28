"""The national lake table and the tile merge of the 1 m rebuild (nz1mlakes.py, nz1mmerge.py, rf-lxa.5): the hole fill
with the national table (forced, tidal), the lake join and re-run check, the edge stitch, lakes as single nodes, the
cycle guard, and two synthetic tiles sharing an edge (with the lake across it) merged against one whole tile."""

import json
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import shapely
from rasterio.transform import from_origin

from river_finder import lidarspec as S
from river_finder import nz1m as N
from river_finder import nz1mlakes as LK
from river_finder import nz1mmerge as M

sys.path.insert(0, str(Path(__file__).parent))
import synth1m as SY  # noqa: E402


# ---------------------------------------------------------------------------------------------- lake fill and table
def _lake_grid(level=105.0, rim_flat=True):
    H = W = 64
    z = np.fromfunction(lambda r, c: 100 + 0.5 * r + 0.3 * c, (H, W)).astype(np.float32)
    z[20:44, 20:44] = level
    z[24:40, 24:40] = np.nan
    wc = np.full((H, W), 4 * 25, np.uint8)
    if rim_flat:
        wc[20:44, 20:44] = 4 * 25 + 4 * 5
    wc[np.isnan(z)] = 0
    z8 = np.full((H // 4, W // 4), 90.0, np.float32)
    return z, wc, z8, from_origin(0, 128, 2, 2)


def test_fill_holes_takes_the_national_level_forced_and_tidal():
    poly = shapely.box(30, 30, 98, 98)                   # the hole's cells (x, y 48-80) lie inside
    z, wc, z8, tf = _lake_grid()
    r = [h for h in N.fill_holes(z, wc, z8, tf, lake_table=[(poly, 104.9, "hole")]) if h["km2"] > 0.0005][0]
    assert r["filled"] and r["level"] == pytest.approx(104.9) and r["own_level"] == pytest.approx(105.0)
    assert not r["forced"] and np.allclose(z[24:40, 24:40], 104.9)
    # the tile's own rim test fails (no flattened rim): the table still fills it
    z, wc, z8, tf = _lake_grid(rim_flat=False)
    assert not [h for h in N.fill_holes(z.copy(), wc.copy(), z8, tf) if h["filled"]]
    r = [h for h in N.fill_holes(z, wc, z8, tf, lake_table=[(poly, 104.9, "hole")]) if h["km2"] > 0.0005][0]
    assert r["filled"] and r["forced"] and np.allclose(z[24:40, 24:40], 104.9)
    # tidal: a hole at sea level stays no data, by its own level and by the table
    z, wc, z8, tf = _lake_grid(level=0.4)
    r = [h for h in N.fill_holes(z, wc, z8, tf) if h["km2"] > 0.0005][0]
    assert not r["filled"] and r["tidal"] and np.isnan(z[24:40, 24:40]).all()
    z, wc, z8, tf = _lake_grid()
    r = [h for h in N.fill_holes(z, wc, z8, tf, lake_table=[(poly, 0.3, "tidal")]) if h["km2"] > 0.0005][0]
    assert not r["filled"] and r["tidal"]


def _rec(tile, lakes, holes=(), tidal=()):
    return {"tile": tile, "box": [0, 0, 1, 1], "core": [0, 0, 1, 1], "lakes": list(lakes), "tidal": list(tidal),
            "holes": list(holes), "lake_table": False}


def _lake(lid, kind, level, bb, holes=(), drains="out", ox=np.nan, oy=np.nan, oa=np.nan, og=-1, reaches=()):
    return {"lake": lid, "kind": kind, "level": level, "km2": shapely.box(*bb).area / 1e6, "holes": list(holes),
            "bbox": list(bb), "wkt": shapely.box(*bb).wkt, "drains": drains, "outlet_x": ox, "outlet_y": oy,
            "outlet_area_tile_km2": oa, "outlet_gid": og, "reaches": list(reaches), "touches_box": False}


def _hole(h, x, y, level, rim, filled=True):
    return {"hole": h, "x": x, "y": y, "km2": 1.0, "cells": 250_000, "level": level, "own_level": level,
            "rim_flat_cells": rim, "filled": filled}


def test_lake_join_one_level_one_outlet_and_rerun_check():
    a = _rec("q00y00", [_lake(0, "hole", 100.00, (0, 0, 2000, 1000), holes=[1], ox=1000, oy=-5, oa=50.0, og=7)],
             holes=[_hole(1, 500, 500, 100.00, 900)])
    b = _rec("q01y00", [_lake(3, "hole", 100.05, (1500, 0, 3000, 1000), holes=[2], drains="sink")],
             holes=[_hole(2, 2500, 500, 100.05, 300)])
    c = _rec("q00y01", [_lake(1, "flat", 100.01, (0, 990, 2000, 1500)), _lake(2, "flat", 250.0, (0, 5000, 900, 5600))],
             holes=[_hole(5, 1000, 1200, None, 0, filled=False)],
             tidal=[{"level": 0.3, "km2": 2.0, "bbox": [9000, 0, 9500, 400],
                     "wkt": shapely.box(9000, 0, 9500, 400).wkt}])
    L, R = LK.join([a, b, c])
    assert len(L) == 3 and sorted(L["kind"]) == ["flat", "hole", "tidal"]
    lk = L[L["kind"] == "hole"].iloc[0]
    assert lk["level_m"] == pytest.approx(100.0) and lk["n_records"] == 3 and lk["tiles"] == "q00y00,q00y01,q01y00"
    assert lk["outlet_gid"] == 7 and lk["outlet_tile"] == "q00y00" and lk["n_outlets"] == 1
    assert R.groupby("lake_id").size().max() == 3
    rr = LK.check(L, [a, b, c])
    assert [(r["tile"], r["hole"]) for r in rr] == [("q01y00", 2), ("q00y01", 5)]
    assert rr[0]["reason"].startswith("level off by +0.050") and rr[1]["reason"] == "national hole lake left unfilled"


# ---------------------------------------------------------------------------------------------- network pieces
def test_cycle_guard_cuts_the_weakest_link():
    down = np.array([1, 2, 0, 2, -1, 4], np.int64)          # 0 -> 1 -> 2 -> 0, 3 joins it; 5 -> 4 outlet
    stitch = np.array([0, 3, 1, 0, 0, 0], np.uint8)
    order = M.kahn(down)
    assert len(order) == 3 and M.find_cycles(down, order) == [[0, 1, 2]]
    cut = M.break_cycles(down, stitch, np.ones(6), log=lambda *a: None)
    assert len(cut) == 1 and cut[0]["cut_row"] == 1 and down[1] == -1 and stitch[1] == M.STITCH_NONE
    assert len(M.kahn(down)) == 6


def _frame(rows):
    T = pd.DataFrame(rows)
    for c, v in (("area_tile_mid_km2", None), ("length_m", 250.0), ("inflow", False), ("down_seg", -1),
                 ("z_top", 0.0), ("z_bot", 0.0), ("start_x", 0.0), ("start_y", 1e6)):
        if c not in T:
            T[c] = T["area_tile_km2"] if v is None else v
    T["tile"] = T["tile"].astype("category")
    return T


def test_stitch_takes_the_line_through_the_exit_point_and_skips_the_one_ending_there():
    # A's reach 0 leaves its core at (100, 0). B's reach 1 ends there (its last vertex); reach 2 starts there, with
    # an in-tile area far below A's (B's REC2 inflow was short): the same cell, so it is the continuation; reach 3
    # runs 3 m away (another channel: its area ratio decides, and it is no match for the exact one)
    T = _frame([{"gid": 10, "tile": "a", "exits_core": True, "end_x": 100.0, "end_y": 0.0, "area_tile_km2": 5.0},
                {"gid": 20, "tile": "b", "exits_core": False, "end_x": 100.0, "end_y": 0.0, "area_tile_km2": 2.0},
                {"gid": 21, "tile": "b", "exits_core": False, "end_x": 100.0, "end_y": -250.0, "area_tile_km2": 2.5},
                {"gid": 22, "tile": "b", "exits_core": False, "end_x": 103.0, "end_y": -300.0, "area_tile_km2": 7.0}])
    edge = {"xy": np.array([[100, 250], [100, 0], [100, 0], [100, -250], [103, 3], [103, -300]], float),
            "k": np.array([1, 1, 2, 2, 3, 3]), "last": np.array([False, True, False, True, False, True])}
    down, stitch = np.full(4, -1, np.int64), np.full(4, M.STITCH_NONE, np.uint8)
    st = M.stitch_edges(T, edge, down, stitch)
    assert down[0] == 2 and stitch[0] == M.STITCH_EDGE and st["stitched"] == 1 and st["exact"] == 1
    # without the exact line: the 3 m one, by its area ratio (7 / 5); with a ratio of 10 it is refused
    edge2 = {k: v[[0, 1, 4, 5]] for k, v in edge.items()}
    down, stitch = np.full(4, -1, np.int64), np.full(4, M.STITCH_NONE, np.uint8)
    assert M.stitch_edges(T, edge2, down, stitch)["exact"] == 0 and down[0] == 3
    T.loc[3, "area_tile_km2"] = 50.0
    down, stitch = np.full(4, -1, np.int64), np.full(4, M.STITCH_NONE, np.uint8)
    st = M.stitch_edges(T, edge2, down, stitch)
    assert down[0] == -1 and st["area_ratio"] == 1


CORES2 = {"q00y00": [0.0, 0.0, 1000.0, 1000.0], "q01y00": [1000.0, 0.0, 2000.0, 1000.0]}


def _fallback_frame(rows):
    T = _frame(rows)
    for c, v in (("dem_source", "lidar"), ("mid_x", 0.0), ("mid_y", 0.0), ("local_km2", 0.1)):
        if c not in T:
            T[c] = v
    for c, e in (("start_x", "end_x"), ("start_y", "end_y")):
        T[c] = T[c].fillna(T[e]) if c in T else T[e]
    return T


def _labels(root, tid, cells: dict):
    """A ``local_catchment_core.tif`` for ``tid``: -1 but ``{(x, y): gid}`` (10 m cells by their centre)."""
    import rasterio

    x0, y0, x1, y1 = CORES2[tid]
    a = np.full(int((y1 - y0 + 200) / 10), -1, np.int64)
    a = np.full((len(a), int((x1 - x0 + 200) / 10)), -1, np.int64)
    tf = from_origin(x0 - 100, y1 + 100, 10.0, 10.0)
    for (x, y), g in cells.items():
        r, c = rasterio.transform.rowcol(tf, x, y)
        a[r, c] = g
    d = N.tile_dir(tid, root)
    d.mkdir(parents=True, exist_ok=True)
    with rasterio.open(d / "local_catchment_core.tif", "w", driver="GTiff", width=a.shape[1], height=a.shape[0],
                       count=1, dtype="int64", crs="EPSG:2193", transform=tf, nodata=-1) as w:
        w.write(a, 1)


def test_catchment_stitch_takes_the_receiving_tiles_own_routing(tmp_path):
    # A's reach 0 leaves its core 30 m before the east edge (its line ends at x 970); nothing of B passes there.
    # B's labels past the edge: 15 m in, reach 2001 (2 km2) and 2002 (30 km2, but 40 m higher); 45 m in, 2003
    # (5 km2, which leaves B's core straight back into A)
    T = _fallback_frame([
        {"gid": 10, "tile": "q00y00", "exits_core": True, "end_x": 970.0, "end_y": 500.0, "area_tile_km2": 80.0,
         "z_bot": 10.0},
        {"gid": 2001, "tile": "q01y00", "exits_core": False, "end_x": 1300.0, "end_y": 400.0, "area_tile_km2": 2.0,
         "z_bot": 9.0},
        {"gid": 2002, "tile": "q01y00", "exits_core": False, "end_x": 1300.0, "end_y": 600.0, "area_tile_km2": 30.0,
         "z_bot": 50.0},
        {"gid": 2003, "tile": "q01y00", "exits_core": True, "end_x": 1003.0, "end_y": 300.0, "area_tile_km2": 5.0,
         "z_bot": 8.0}])
    _labels(tmp_path, "q01y00", {(1015.0, 495.0): 2001, (1015.0, 515.0): 2002, (1045.0, 505.0): 2003})

    def run(T):
        down, stitch = np.full(len(T), -1, np.int64), np.full(len(T), M.STITCH_NONE, np.uint8)
        st = M.stitch_catchments(T, down, stitch, CORES2, tmp_path)
        return down, stitch, st

    down, stitch, st = run(T)
    assert down[0] == 1 and stitch[0] == M.STITCH_EDGE and st["linked"] == 1   # the shallowest depth, not too high
    T.loc[1, "z_bot"] = 12.0                     # 2001 now 2 m higher than the exit: the next depth, 2003
    down, _, st = run(T)
    assert down[0] == 3
    T.loc[[0, 2], "dem_source"] = "8m"           # on the 8 m fallback 20 m is allowed: 2002 (the larger) wins
    T.loc[2, "z_bot"] = 25.0
    down, _, st = run(T)
    assert down[0] == 2
    T.loc[2, "z_bot"] = 60.0
    T.loc[3, "z_bot"] = 70.0
    T.loc[1, "z_bot"] = 70.0
    down, _, st = run(T)
    assert down[0] == -1 and st["refused"] == 1  # labels, but none acceptable


def test_fallback_targets_prefer_no_return_and_never_make_a_cycle():
    T = _fallback_frame([
        {"gid": 10, "tile": "q00y00", "exits_core": True, "end_x": 990.0, "end_y": 500.0, "area_tile_km2": 50.0},
        {"gid": 2001, "tile": "q01y00", "exits_core": False, "end_x": 1300.0, "end_y": 500.0, "area_tile_km2": 2.0},
        {"gid": 2002, "tile": "q01y00", "exits_core": True, "end_x": 1002.0, "end_y": 700.0, "area_tile_km2": 9.0}])
    names = T["tile"].astype(str).to_numpy()
    area = T["area_tile_km2"].to_numpy(float)
    back = M._into_tiles(T, CORES2)
    assert list(back) == ["q01y00", "", "q00y00"]
    down = np.full(3, -1, np.int64)
    j = np.array([1, 2])
    assert M._first_target(j, 0, names, area, back, down) == 1     # 2002 returns into A: after 2001
    down[1] = 0                                                      # 2001 drains into the exit: a cycle
    assert M._first_target(j, 0, names, area, back, down) == 2
    down[2] = 0
    assert M._first_target(j, 0, names, area, back, down) == -1


def test_rec2_path_stitch_links_the_first_reach_down_rec2():
    from river_finder.relink import Rec2Graph

    # REC2 runs from A (x 900) east into B along y 500; B's reaches: 2001 starts 400 m on, 60 m off the line (a cycle
    # back to the exit), 2002 starts 900 m on, 30 m off the line
    rec2 = gpd.GeoDataFrame({"nzsegment": [1, 2], "HydroID": [11, 12], "NextDownID": [12, -1],
                             "cum_area_km2": [60.0, 61.0]},
                            geometry=[shapely.LineString([(800, 500), (1200, 500)]),
                                      shapely.LineString([(1200, 500), (3000, 500)])], crs=2193)
    T = _fallback_frame([
        {"gid": 10, "tile": "q00y00", "exits_core": True, "end_x": 990.0, "end_y": 500.0, "area_tile_km2": 50.0},
        {"gid": 2001, "tile": "q01y00", "exits_core": False, "end_x": 1500.0, "end_y": 900.0, "area_tile_km2": 3.0,
         "start_x": 1400.0, "start_y": 560.0},
        {"gid": 2002, "tile": "q01y00", "exits_core": False, "end_x": 2500.0, "end_y": 530.0, "area_tile_km2": 1.0,
         "start_x": 1900.0, "start_y": 530.0}])
    edge = {"xy": np.zeros((0, 2)), "k": np.zeros(0, np.int64), "last": np.zeros(0, bool)}
    down, stitch = np.full(3, -1, np.int64), np.full(3, M.STITCH_NONE, np.uint8)
    down[1] = 0
    st = M.stitch_rec2_path(T, edge, down, stitch, CORES2, Rec2Graph.from_frame(rec2))
    assert down[0] == 2 and stitch[0] == M.STITCH_EDGE and st["linked"] == 1
    down[:] = -1
    down[0] = -1
    T.loc[0, "area_tile_km2"] = 1000.0           # no REC2 reach of a similar area: nothing
    st = M.stitch_rec2_path(T, edge, down, stitch, CORES2, Rec2Graph.from_frame(rec2))
    assert down[0] == -1 and st["no_rec2"] == 1


def test_open_entries_from_a_merged_tile_need_an_unresolved_exit_near():
    # B's reach 1 takes 40 km2 in from its halo (A's side) and nothing links in
    T = _fallback_frame([
        {"gid": 10, "tile": "q00y00", "exits_core": True, "end_x": 990.0, "end_y": 100.0, "area_tile_km2": 40.0},
        {"gid": 2001, "tile": "q01y00", "exits_core": False, "end_x": 1300.0, "end_y": 500.0, "area_tile_km2": 40.0,
         "local_km2": 0.5, "start_x": 1005.0, "start_y": 500.0}])
    pos = pd.Index(T["gid"].to_numpy(np.int64))
    down = np.full(2, -1, np.int64)
    assert M.open_entries(T, pos, down)[1] == pytest.approx(39.5)                       # no cores: as before
    assert M.open_entries(T, pos, down, cores={"q01y00": CORES2["q01y00"]})[1] > 0   # A not merged
    assert M.open_entries(T, pos, down, cores=CORES2)[1] == 0                         # A merged, no exit lost
    assert M.open_entries(T, pos, down, cores=CORES2, unresolved=np.array([0]))[1] > 0   # A's exit 400 m away
    T.loc[0, "end_y"] = 4000.0
    assert M.open_entries(T, pos, down, cores=CORES2, unresolved=np.array([0]))[1] == 0
    assert M.open_entries(T, pos, down, lake_member=np.array([False, True]))[1] == 0  # a lake node's member


def test_rec2_clears_truncation_where_the_area_is_there():
    T = _fallback_frame([{"gid": 1, "tile": "q00y00", "exits_core": False, "end_x": 0.0, "end_y": 0.0,
                          "area_tile_km2": 1.0},
                         {"gid": 2, "tile": "q00y00", "exits_core": False, "end_x": 0.0, "end_y": 0.0,
                          "area_tile_km2": 1.0}])
    pos = pd.Index([1, 2])
    down = np.array([1, -1], np.int64)
    order = M.kahn(down)
    area = np.array([10.0, 20.0])
    missing = np.array([30.0, 0.0])
    tr, miss, _ = M.truncation(T, pos, down, order, area, missing)
    assert tr.tolist() == [True, True] and miss.tolist() == [30.0, 30.0]
    tr, _, _ = M.truncation(T, pos, down, order, area, missing, rec2_at=np.array([10.5, np.nan]), area_mid=area)
    assert tr.tolist() == [False, True]          # REC2 has 10.5 km2 where the network has 10; no REC2: as before


def test_lake_nodes_redirect_to_the_one_outlet_without_cycles():
    # lake 0: members 1 -> 2 -> (lost at the box edge), 3 -> 4 (the outlet) -> 5 (member below the outlet) -> 6 sea;
    # 7 leaves its core and ends in the lake with no stitch
    poly = shapely.box(0, 0, 1000, 1000)
    T = _frame([{"gid": 100 + i, "tile": "a" if i < 7 else "b", "exits_core": i in (2, 7), "end_x": x, "end_y": y,
                 "area_tile_km2": a, "start_x": x, "start_y": y + 10, "mid_x": x, "mid_y": y + 5}
                for i, (x, y, a) in enumerate([(-500, -500, 1.0), (200, 200, 2.0), (300, 990, 3.0), (400, 400, 1.0),
                                               (500, 0, 9.0), (500, -100, 9.5), (500, -300, 10.0), (800, 800, 1.5)])])
    down = np.array([1, 2, -1, 4, 5, 6, -1, -1], np.int64)
    stitch = np.array([0, 0, 4, 0, 0, 0, 0, 4], np.uint8)
    L = pd.DataFrame({"lake_id": [0], "kind": ["hole"], "km2": [1.0], "outlet_gid": [104], "outlet_x": [500.0],
                      "outlet_y": [0.0], "outlet_area_tile_km2": [9.0], "geometry": [poly]})
    import geopandas as gpd

    L = gpd.GeoDataFrame(L, geometry="geometry")
    R = pd.DataFrame({"lake_id": [0], "reaches": [[101, 102, 103, 105]]})
    lake_id, summ = M.lake_nodes(T, L, R, pd.Index(T["gid"]), down, stitch)
    assert down.tolist() == [1, 2, 4, 4, 5, 6, -1, 4]
    assert stitch[2] == M.STITCH_LAKE and stitch[7] == M.STITCH_LAKE and stitch[5] == 0
    assert lake_id.tolist() == [-1, 0, 0, 0, 0, 0, -1, 0]
    assert summ[0]["redirected"] == 1 and summ[0]["enders"] == 1 and len(M.kahn(down)) == len(T)


def test_zero_length_reaches_merge_into_the_reach_below():
    """rf-lxa.12: 0, 1 -> Z2 -> Z3 -> 4 <- Z5 (a zero-length head); 4 -> 6 -> sea; 11 (tile b) -> Z12 -> 6 across the
    edge; 7, 8 -> Z9 (a zero-length sea outlet); Z10 alone (head and outlet)."""
    B = S.SEG_BASE
    spec = [  # tile, length, local, down (row), down_seg (row, in-tile), exits, stitch
        ("a", 250, 1.0, 2, 2, False, 0), ("a", 250, 2.0, 2, 2, False, 0), ("a", 0, 0.01, 3, 3, False, 0),
        ("a", 0, 0.02, 4, 4, False, 0), ("a", 250, 0.5, 6, 6, False, 0), ("a", 0, 0.6, 4, 4, False, 0),
        ("a", 250, 0.3, -1, -1, False, 0), ("a", 250, 1.5, 9, 9, False, 0), ("a", 250, 0.7, 9, 9, False, 0),
        ("a", 0, 0.05, -1, -1, False, 0), ("a", 0, 0.4, -1, -1, False, 0), ("b", 250, 3.0, 12, 99, True, 1),
        ("a", 0, 0.2, 6, 6, False, 0)]
    T = pd.DataFrame({"gid": [(1 if t == "a" else 2) * B + i for i, (t, *_) in enumerate(spec)],
                      "tile": pd.Categorical([s[0] for s in spec]), "seg_id": np.arange(len(spec)),
                      "length_m": [float(s[1]) for s in spec], "local_km2": [s[2] for s in spec],
                      "down_seg": [s[4] for s in spec], "exits_core": [s[5] for s in spec]})
    down = np.array([s[3] for s in spec], np.int64)
    stitch = np.array([s[6] for s in spec], np.uint8)
    rng = np.random.default_rng(1)
    T["local_z_mean_m"] = rng.uniform(100, 200, len(T))
    T["local_z_max_m"] = T["local_z_mean_m"] + rng.uniform(1, 50, len(T))
    for c in M.LOCAL_SUMS:
        T[c] = T["local_km2"] * rng.uniform(0, 1, len(T))
    order = M.kahn(down)
    full = M.acc_sum(order, down, T["local_km2"].to_numpy())
    T["area_tile_km2"] = T["area_tile_mid_km2"] = full
    T["z_top"], T["z_bot"], T["gradient_m_per_km"] = 10.0, 9.0, 4.0
    okind = np.where(down >= 0, M.OUT_NONE, M.OUT_SEA).astype(np.uint8)
    missing = np.zeros(len(T))
    missing[5] = 0.3
    pos = pd.Index(T["gid"])
    T2, d, st, ok, lid, miss, row_map, fix, info = M.drop_zero_length(T, pos, down, stitch, okind, np.full(len(T), -1),
                                                                       missing, log=lambda *a: None)
    keep = [0, 1, 4, 6, 7, 8, 11]
    assert T2["gid"].tolist() == T["gid"].to_numpy()[keep].tolist()              # the other gids stay
    assert d.tolist() == [2, 2, 3, -1, -1, -1, 3] and len(M.kahn(d)) == len(T2)   # rerouted, no cycle
    assert st[6] == M.STITCH_EDGE and ok[4] == ok[5] == M.OUT_SEA
    loc = T2["local_km2"].to_numpy()
    assert loc[2] == pytest.approx(0.5 + 0.01 + 0.02 + 0.6) and loc[3] == pytest.approx(0.3 + 0.2)
    assert loc[4] == pytest.approx(1.5 + 0.05) and loc.sum() == pytest.approx(T["local_km2"].sum() - 0.4)
    assert info["n"] == 6 and info["into_reach"] == 4 and info["at_outlet"] == 2 and info["rerouted"] == 5
    assert info["new_outlets"] == 2 and info["lost"] == 1 and info["lost_km2"] == pytest.approx(0.4)
    g = T["gid"].to_numpy()
    assert info["merged"] == [[g[2], g[4]], [g[3], g[4]], [g[5], g[4]], [g[9], g[7]], [g[10], -1], [g[12], g[6]]]
    assert row_map.tolist() == [0, 1, 2, 2, 2, 2, 3, 4, 5, 4, -1, 6, 3]
    assert miss[2] == pytest.approx(0.3)
    # the local elevation: area-weighted mean and the higher maximum of the reaches merged
    zm, zx, lk = T["local_z_mean_m"].to_numpy(), T["local_z_max_m"].to_numpy(), T["local_km2"].to_numpy()
    r = [4, 2, 3, 5]
    assert T2["local_z_mean_m"].iat[2] == pytest.approx((zm[r] * lk[r]).sum() / lk[r].sum())
    assert T2["local_z_max_m"].iat[2] == zx[r].max()
    # tile links: 0, 1 named Z2 (-> Z3 -> 4 in the tile) and now name 4; 7, 8 named the outlet Z9: the sea
    assert T2["down_seg"].tolist() == [4, 4, 6, -1, -1, -1, 99] and not T2["exits_core"].iloc[:6].any()
    assert set(fix.index) == set(g[[0, 1, 4, 6, 7, 8]]) and set(M.ZERO_FIX_COLS) <= set(fix.columns)
    # the network areas: those of the full network (but where an outlet took a zero-length outlet's local area)
    F = M.network_fields(T2, d, M.kahn(d))
    F0 = M.network_fields(T, down, order)
    want = full[keep] + np.array([0, 0, 0, 0, 0.05, 0, 0])
    assert np.allclose(F["area_km2"], want)
    assert np.allclose(F["head_m"][[0, 1, 2, 3, 6]], F0["head_m"][[0, 1, 4, 6, 11]])
    # nothing of zero length: the inputs back
    T3 = T[T["length_m"] > 0].reset_index(drop=True)
    res = M.drop_zero_length(T3, pd.Index(T3["gid"]), np.full(len(T3), -1), np.zeros(len(T3), np.uint8),
                             np.zeros(len(T3), np.uint8), np.full(len(T3), -1), np.zeros(len(T3)))
    assert res[0] is T3 and res[-1]["n"] == 0 and res[6].tolist() == list(range(len(T3)))


# ---------------------------------------------------------------------------------------------- two synthetic tiles
def _run(tile, z1, z8, core, box, root):
    od = root / "tiles" / tile.id
    r0, c0 = int(SY.BOX[3] - box[3]), int(box[0] - SY.BOX[0])
    sub = np.ascontiguousarray(z1[r0:r0 + int(box[3] - box[1]), c0:c0 + int(box[2] - box[0])])
    s8 = np.ascontiguousarray(z8[r0 // 8:(r0 + int(box[3] - box[1])) // 8, c0 // 8:(c0 + int(box[2] - box[0])) // 8])
    return N.process(tile, N.ArraySource(sub, box), lambda: s8, None, None, od, threads=4, log=lambda *a: None,
                     core=core, box=box)


@pytest.fixture(scope="module")
def two_tiles(tmp_path_factory):
    z1, _, z8, level = SY.terrain()
    root = tmp_path_factory.mktemp("nz1m")
    old = N.CHUNK_M
    N.CHUNK_M = 1000.0
    split = SY.Y0 + 2000                                     # through the lake's hole
    try:
        whole = _run(N.Tile32(0, 2), z1, z8, SY.CORE, SY.BOX, root)
        # halos that hold every link crossing the edge (the main valley's link runs 1.5 km, from its head to the
        # tributary), as the 3 km halo does on real tiles; the boxes stop at the synthetic world's edge
        south = _run(N.Tile32(0, 0), z1, z8, (SY.CORE[0], SY.CORE[1], SY.CORE[2], split), SY.BOX, root)
        north = _run(N.Tile32(0, 1), z1, z8, (SY.CORE[0], split, SY.CORE[2], SY.CORE[3]),
                     (SY.BOX[0], split - 1200, SY.BOX[2], SY.BOX[3]), root)
    finally:
        N.CHUNK_M = old
    out = root / "merged"
    res = M.merge(["q00y00", "q00y01"], root=root, out=out, rec2=None, flood=None, regions=False, geology=False,
                  log=lambda *a: None)
    return root, out, res, whole, south, north, level


def test_two_tiles_merge_like_one_tile(two_tiles):
    import geopandas as gpd

    root, out, res, whole, south, north, level = two_tiles
    s = res["summary"]
    assert s["cycles"] == [] and s["edge_stitch"]["exits"] > 0
    # every exit links but those leaving the merged cores (south into the sea's halo)
    assert s["edge_stitch"]["stitched"] >= 1 and s["edge_stitch"]["entry"] == 0
    es = s["edge_stitch"]
    assert es["stitched"] + s["links"]["lake"] == es["exits"] - s["unresolved_edge"]["outside"]
    assert s["unresolved_edge"]["inside"] == 0                            # nothing ends at the shared edge
    net = pd.read_parquet(out / "network.parquet")
    assert net["gid"].is_unique and set(net["gid"]) == set(res["T"]["gid"])               # gids unchanged
    # every merge field of the spec but vi (the viewer index, set by nz score), and the joins it can do here
    spec = {f.name for f in S.FIELDS if f.stage == "merge"} - {"vi"}
    assert spec <= set(net.columns), spec - set(net.columns)
    g = gpd.read_parquet(out / "reaches.parquet")
    assert len(g) == len(net) and {"area_km2", "down_gid", "stitch", "outlet_kind"} <= set(g.columns)
    assert set(S.tile_columns()["segments_core.gpkg"]) <= set(g.columns)
    # areas: every reference reach of 0.5 km2 or more against the merged reach under its midpoint
    import pyogrio

    ref = pyogrio.read_dataframe(root / "tiles" / "q00y02" / "segments_core.gpkg")
    ref = ref[ref["area_tile_km2"] >= 0.5]
    mid = shapely.line_interpolate_point(ref.geometry.to_numpy(), 0.5, normalized=True)
    tree = shapely.STRtree(g.geometry.to_numpy())
    pi, gi = tree.query(mid, predicate="dwithin", distance=2.0)
    best = {}
    for p, q in zip(pi, gi, strict=True):
        d = shapely.distance(mid[p], g.geometry.iloc[q])
        if p not in best or d < best[p][0]:
            best[p] = (d, q)
    assert len(best) >= 0.95 * len(ref)                    # the same lines
    a_ref = ref["area_tile_km2"].to_numpy()[list(best)]
    a_net = g["area_km2"].to_numpy()[[q for _, q in best.values()]]
    err = np.abs(a_net / a_ref - 1)
    assert np.mean(err <= 0.02) >= 0.95 and err[a_ref >= 2.0].max() <= 0.01
    # each cell is counted once: the two tiles' local areas add up to the whole tile's
    whole_local = pyogrio.read_dataframe(root / "tiles" / "q00y02" / "segments_core.gpkg")["local_km2"].sum()
    assert g["local_km2"].sum() == pytest.approx(whole_local, rel=0.005)


def test_the_lake_across_the_edge_is_one_node(two_tiles):
    root, out, res, whole, south, north, level = two_tiles
    lakes = res["lakes"]
    hole = lakes[lakes["kind"] == "hole"]
    assert len(hole) == 1 and hole["level_m"].iloc[0] == pytest.approx(level, abs=0.02)
    assert hole["n_records"].iloc[0] == 2 and hole["outlet_gid"].iloc[0] >= 0
    net = res["net"]
    o = int(hole["outlet_gid"].iloc[0])
    lid = int(hole["lake_id"].iloc[0])
    mem = net[(net["lake_id"] == lid) & (net["gid"] != o)]
    # every lake reach drains to the outlet; the outlet carries the lake's whole catchment
    pos = pd.Series(np.arange(len(net)), index=net["gid"])
    down = net["down_gid"].to_numpy()
    for g0 in mem["gid"]:
        x, n = int(g0), 0
        while x != o and x >= 0 and n < 1000:
            x, n = int(down[pos[x]]), n + 1
        assert x == o
    import pyogrio

    w = [x for x in whole["lakes"] if x["kind"] == "hole"][0]
    ref = pyogrio.read_dataframe(root / "tiles" / "q00y02" / "segments_core.gpkg", read_geometry=False)
    a_ref = ref.set_index("gid").loc[w["outlet_gid"], "area_tile_km2"]
    assert net.set_index("gid").loc[o, "area_km2"] == pytest.approx(a_ref, rel=0.01)
    assert json.loads((out / "merge.json").read_text())["lakes"]["by_kind"]["hole"] == 1
    assert (out / "lakes.gpkg").exists() and (out / "x1.arrow").exists() and (out / "steps.arrow").exists()


def test_merge_drops_zero_length_reaches(two_tiles, tmp_path):
    """rf-lxa.12 on the two tiles: three reaches made zero-length in a copy (a reach two reaches drain into, a head
    reach and the lake's outlet reach) leave every national file; what drained into them drains into the reach
    below, which takes their local sums; every other reach keeps its gid and its network area."""
    import shutil

    import geopandas as gpd
    import pyarrow.feather as feather
    import pyogrio

    root, out, res, *_ = two_tiles
    net0 = pd.read_parquet(out / "network.parquet").set_index("gid")
    g0 = gpd.read_parquet(out / "reaches.parquet").set_index("gid")
    lakes = res["lakes"]
    o = int(lakes.loc[lakes["kind"] == "hole", "outlet_gid"].iloc[0])
    nup = net0.groupby("down_gid").size()
    tile_of = g0["tile"].astype(str)
    conf = [x for x in nup[nup >= 2].index if x >= 0 and x != o and net0.loc[x, "down_gid"] >= 0
            and (tile_of[net0.index[net0["down_gid"] == x]] == tile_of[x]).all()][0]
    head = [x for x in net0.index[~net0.index.isin(nup.index) & (net0["down_gid"] >= 0)]
            if net0.loc[x, "lake_id"] < 0 and x not in (o, conf) and net0.loc[x, "down_gid"] not in (o, conf)][0]
    zero = [conf, head, o]
    assert net0.loc[o, "down_gid"] >= 0
    root2 = tmp_path / "nz1m"
    for t in ("q00y00", "q00y01"):
        shutil.copytree(root / "tiles" / t, root2 / "tiles" / t)
        p = root2 / "tiles" / t / "segments_core.gpkg"
        g = pyogrio.read_dataframe(p)
        g.loc[g["gid"].isin(zero), "length_m"] = 0.0
        p.unlink()
        pyogrio.write_dataframe(g, p, layer="segments", driver="GPKG")
    out2 = root2 / "merged"
    r2 = M.merge(["q00y00", "q00y01"], root=root2, out=out2, rec2=None, flood=None, regions=False, geology=False,
                 log=lambda *a: None)
    zl = r2["summary"]["zero_length"]
    assert zl["n"] == 3 and zl["into_reach"] == 3 and zl["lost"] == 0
    assert sorted(m[0] for m in zl["merged"]) == sorted(zero)
    assert all(m[1] == net0.loc[m[0], "down_gid"] for m in zl["merged"])
    net = pd.read_parquet(out2 / "network.parquet").set_index("gid")
    g = gpd.read_parquet(out2 / "reaches.parquet").set_index("gid")
    x1 = feather.read_table(out2 / "x1.arrow").to_pandas()
    assert set(net.index) == set(g.index) == set(x1["gid"]) == set(net0.index) - set(zero)
    assert (g["length_m"] > 0).all() and len(M.kahn(r2["down"])) == len(net)
    # what drained into them drains into the reach below
    for z in zero:
        for u in net0.index[net0["down_gid"] == z]:
            assert net.loc[u, "down_gid"] == net0.loc[z, "down_gid"]
    # every other reach keeps its network area; the local sums move into the reach below, the totals stay
    assert np.allclose(net["area_km2"], net0.loc[net.index, "area_km2"], rtol=1e-6)
    for c in ("local_km2", "local_lidar_km2", "local_lake_km2"):
        assert g[c].astype(float).sum() == pytest.approx(g0[c].astype(float).sum(), rel=1e-6)
    for z in zero:
        d = net0.loc[z, "down_gid"]
        add = g0.loc[[x for x in zero if net0.loc[x, "down_gid"] == d], "local_km2"].sum()
        assert g.loc[d, "local_km2"] == pytest.approx(g0.loc[d, "local_km2"] + add, rel=1e-5)
    # the lake's outlet is now the reach below the old one; its members drain to it
    lk = r2["lakes"]
    o2 = int(lk.loc[lk["kind"] == "hole", "outlet_gid"].iloc[0])
    assert o2 == net0.loc[o, "down_gid"]
    lid = int(lk.loc[lk["kind"] == "hole", "lake_id"].iloc[0])
    for x in net.index[(net["lake_id"] == lid) & (net.index != o2)]:
        n = 0
        while x != o2 and x >= 0 and n < 1000:
            x, n = int(net.loc[x, "down_gid"]), n + 1
        assert x == o2
    js = json.loads((out2 / "merge.json").read_text())
    assert js["reaches"] == len(net0) - 3 and js["zero_length"]["n"] == 3


def test_single_tile_merge_flags_truncation(two_tiles):
    root, *_ = two_tiles
    res = M.merge(["q00y00"], root=root, write=False, rec2=None, flood=None, regions=False, geology=False,
                  log=lambda *a: None)
    net, s = res["net"], res["summary"]
    # the north tile is missing: the rivers coming from it are short and flagged; nothing stitches
    assert s["links"]["edge"] == 0 and net["truncated"].any()
    big = net["area_km2"].idxmax()
    assert net.loc[big, "truncated"] and net.loc[big, "area_check"] > 1.1


def test_merge_takes_a_tile_with_no_reach(two_tiles):
    """A tile with no channel (a small island, like q24y38) merges as an empty tile: same network as without it."""
    root, *_ = two_tiles
    z1, _, z8, _ = SY.terrain()
    n = z1.shape[0]
    island = np.zeros(z1.shape, bool)
    island[n // 2 - 60:n // 2 + 60, n // 2 - 60:n // 2 + 60] = True
    zi = np.where(island, np.float32(5.0), np.nan).astype(np.float32)
    z8i = np.where(island.reshape(n // 8, 8, n // 8, 8).any(axis=(1, 3)), np.float32(6.0), np.nan).astype(np.float32)
    info = N.process(N.Tile32(5, 0), N.ArraySource(zi, SY.BOX), lambda: z8i, None, None, root / "tiles" / "q05y00",
                     threads=2, log=lambda *a: None, core=SY.CORE, box=SY.BOX)
    assert info["reaches_box"] == 0
    kw = dict(root=root, write=False, rec2=None, flood=None, regions=False, geology=False, log=lambda *a: None)
    with_island = M.merge(["q00y00", "q05y00"], **kw)["net"]
    without = M.merge(["q00y00"], **kw)["net"]
    assert len(with_island) == len(without)
    assert np.allclose(with_island["area_km2"].to_numpy(), without["area_km2"].to_numpy())
    out = root / "merged_island"                             # and the written national files
    M.merge(["q00y00", "q05y00"], root=root, out=out, rec2=None, flood=None, regions=False, geology=False,
            log=lambda *a: None)
    assert (out / "network.parquet").exists() and (out / "merge.json").exists()
