"""The consumers of the 1 m rebuild (rf-lxa.8): data root, tile sizes, joins, rain network, weights, viewer, GIS mask,
model cards by build, x1 features and recipes, national table, the finish bookkeeping and switch, the comparisons.
Synthetic data only."""

import json

import geopandas as gpd
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.feather as pf
import pytest
import rasterio
import shapely
from rasterio.transform import from_origin

from river_finder import national, nz
from river_finder import nz1mcompare as C
from river_finder import nz1mfinish as F
from river_finder import nz1mjoin as J

X0, Y0 = 1_840_000.0, 5_760_000.0     # the core of q24y32


def _one_m_root(tmp_path):
    """A directory that reads as a 1 m root (``network.parquet``)."""
    d = tmp_path / "nz1m"
    d.mkdir()
    pd.DataFrame({"gid": [1], "truncated": [False]}).to_parquet(d / "network.parquet")
    return d


def test_root_selection(tmp_path, monkeypatch):
    cfg = tmp_path / "national.toml"
    monkeypatch.setattr(national, "ROOT_CONFIG", cfg)
    monkeypatch.delenv(national.NZ_ENV, raising=False)
    assert national.root_setting() == (national.REPO_ROOT / "data" / "nz", "default")
    cfg.write_text('[data]\nroot = "data/nz1m"\n')
    assert national.root_setting()[0] == national.REPO_ROOT / "data" / "nz1m"
    one = _one_m_root(tmp_path)
    with national.using_root(one):
        assert national.nz_dir() == one.resolve() and national.is_1m() and national.data_kind() == "1m"
        assert national.viewer_dir() == one.resolve() / "viewer_nz"
    assert national.NZ_ENV not in __import__("os").environ
    assert national.viewer_dir_for(national.REPO_ROOT / "data" / "nz") == national.VIEWER_8M
    assert national.data_kind(tmp_path) == "8m"


def test_tile_sizes_and_viewer_tiles(tmp_path):
    q = nz.Tile.parse("q24y32")
    assert q.size == nz.Q_TILE and q.core == (X0, Y0, X0 + 32_000, Y0 + 32_000) and q.index == 13224
    assert q.box == (X0 - 3000, Y0 - 3000, X0 + 35_000, Y0 + 35_000) and q.id == "q24y32"
    x = nz.Tile.parse("x12y16")
    assert x.core == (X0, Y0, X0 + 64_000, Y0 + 64_000) and x.index == 1612 and nz.Tile.of_index(1612) == x
    with pytest.raises(ValueError):
        nz.Tile.parse("merge_x12y16")
    from river_finder import viewer_nz as V

    root = _one_m_root(tmp_path)
    for t in ("q24y32", "q25y32"):
        d = root / "tiles" / t
        d.mkdir(parents=True)
        (d / "tile.json").write_text("{}")
        (d / "area_code.tif").write_bytes(b"")
    (root / "tiles" / "notes").mkdir()
    (root / "tiles" / "notes" / "tile.json").write_text("{}")
    with national.using_root(root):
        # a box 1 km east of q24y32's core edge: only its halo reaches there, and q25y32's core
        got = V._tiles_for((X0 + 32_500, Y0 + 100, X0 + 33_000, Y0 + 200))
    assert [t.id for t in got] == ["q24y32", "q25y32"]


def test_joins(tmp_path):
    lines = [np.array([[0.0, 0.0], [300.0, 0.0]]), np.array([[0.0, 1000.0], [200.0, 1100.0], [400.0, 1000.0]])]
    linz = gpd.GeoDataFrame({"name": ["Alpha River", ""], "river_section_id": [7, 9]},
                            geometry=[shapely.LineString([(-50, 10), (400, 10)]),
                                      shapely.LineString([(0, 1300), (400, 1300)])], crs="EPSG:2193")
    j = J.join_linz(lines, linz)
    assert j["river_linz"].tolist() == ["Alpha River", ""] and j["linz_section_id"].tolist() == [7, -1]
    assert j["linz_dist_m"].iloc[0] == pytest.approx(10, abs=0.1) and j["linz_dist_m"].iloc[1] > 150
    assert J.join_linz(lines, None)["river_linz"].tolist() == ["", ""]
    geo = gpd.GeoDataFrame({"hard_rock": [True, False], "rockgroup": ["greywacke ", "mudstone"]},
                           geometry=[shapely.box(-10, -10, 500, 500), shapely.box(-10, 500, 500, 2000)],
                           crs="EPSG:2193")
    x, y = J.midpoints([shapely.LineString(c) for c in lines])
    hard, rock = J.join_geology(x, y, geo)
    assert hard.tolist() == [1, 0] and rock.tolist() == ["greywacke", "mudstone"]
    layer = gpd.GeoDataFrame({"code": [0], "slug": ["bop_2019"], "year": [2020], "year_start": [2019],
                              "in_mosaic": [True]}, geometry=[shapely.box(-10, -10, 500, 500)], crs="EPSG:2193")
    sv = J.join_survey(lines, layer)
    assert sv["lidar_survey"].tolist() == ["bop_2019", ""] and sv["lidar_year"].iloc[0] == 2020
    assert pd.isna(sv["lidar_year"].iloc[1])
    s = J.sinuosity([shapely.LineString(c) for c in lines], np.array([300.0, 447.2]))
    assert s[0] == pytest.approx(1.0) and s[1] == pytest.approx(447.2 / 400, rel=1e-3)


def test_joins_run_skips_an_empty_row_group(tmp_path):
    # the merge writes one row group per tile, also for a tile with no reach in its core
    import pyarrow as pa
    import pyarrow.parquet as pq

    lines = [shapely.LineString([(0, 0), (300, 0)]), shapely.LineString([(0, 100), (200, 300)])]
    full = pa.table({"gid": pa.array([1, 2], pa.int64()), "length_m": [300.0, 283.0], "river_rec2": ["A", ""],
                     "river": ["A", ""], "geometry": shapely.to_wkb(lines)})
    with pq.ParquetWriter(tmp_path / "reaches.parquet", full.schema) as w:
        w.write_table(full.slice(0, 0))
        w.write_table(full)
        w.write_table(full.slice(0, 0))
    s = J.run(tmp_path, layer_dir=tmp_path, log=lambda *a: None)
    j = pd.read_parquet(tmp_path / "joins.parquet")
    assert j["gid"].tolist() == [1, 2] and j["river"].tolist() == ["A", ""] and isinstance(s, dict)


def test_reaches_1m_reads_the_joins(tmp_path):
    from river_finder.nzscore import reaches_1m

    root = _one_m_root(tmp_path)
    g = gpd.GeoDataFrame({"gid": [5, 6], "tile": ["q24y32", "q25y32"], "river": ["Rec2 Stream", ""],
                          "river_rec2": ["Rec2 Stream", ""]},
                         geometry=[shapely.LineString([(0, 0), (1, 1)])] * 2, crs="EPSG:2193")
    g.to_parquet(root / "reaches.parquet")
    with pytest.raises(SystemExit, match="joins"):
        reaches_1m(root)
    pd.DataFrame({"gid": [6, 5], "river": ["Linz Creek", "Rec2 Stream"], "bed_hard": [1, 0]}).to_parquet(
        root / "joins.parquet")
    r = reaches_1m(root, log=lambda m: None)
    assert r["river"].tolist() == ["Rec2 Stream", "Linz Creek"] and r["bed_hard"].tolist() == [0, 1]
    assert len(reaches_1m(root, tiles=[nz.Tile.parse("q25y32")], log=lambda m: None)) == 1


def test_rain_network_from_outlet_kind(tmp_path):
    import scipy.sparse as sp

    from river_finder.rainfall.grid import GridSpec
    from river_finder.rainfall.upstream import AREA_SHORT, EDGE, EDGE_BIG, network_from_files
    from river_finder.rainfall.weights import CatchmentWeights

    gid = np.array([10, 11, 12, 13], np.int64)
    down = np.array([1, -1, -1, -1])
    npix = np.array([100, 100, 100, 100])
    grid = GridSpec("toy", 170.0, -40.0, 1.0, 1.0, 2, 1)
    w = CatchmentWeights(grid, sp.csr_matrix((npix.astype(np.int32), (np.arange(4), np.zeros(4, int))), shape=(4, 2)),
                         gid, npix, 1e4, down, np.zeros(4, np.int16), ("R",), np.ones(4, np.float32),
                         np.ones(4, np.float32))
    wp = w.save(tmp_path / "weights_toy.npz")
    t = pd.DataFrame({"gid": gid, "down": down, "area_km2": [1.0, 2.0, 300.0, 1.0], "vi": -1,
                      "outlet_kind": [0, 3, 3, 1], "truncated": [True, False, False, False]})
    t.to_pickle(tmp_path / "segments_table.pkl")
    pd.DataFrame({"gid": [13], "status": ["unlinked"]}).to_csv(tmp_path / "relink_log.csv", index=False)
    net = network_from_files(wp, tmp_path / "segments_table.pkl", tmp_path / "relink_log.csv", log=lambda m: None)
    assert [bool(f & EDGE) for f in net.flags] == [False, True, True, False]   # outlet_kind 3, not the old log
    assert bool(net.flags[2] & EDGE_BIG) and bool(net.flags[0] & AREA_SHORT)
    assert net.meta["sources"]["merge"]["outlet_kind_3_edge"] == 2 and "relink_log" not in net.meta["sources"]


def test_weights_land_on_the_box_grid(tmp_path):
    """The 1 m tiles' local catchments span the box, their area code only the core: land is read on the box grid."""
    from river_finder.rainfall.config import load_config
    from river_finder.rainfall.weights import build_weights

    d = tmp_path / "q24y32"
    d.mkdir()
    lc = np.full((40, 40), 13224 * 10**6, np.int64)
    lc[:, :5] = -1
    with rasterio.open(d / "local_catchment_core.tif", "w", driver="GTiff", height=40, width=40, count=1,
                       dtype="int64", crs="EPSG:2193", transform=from_origin(X0 - 100, Y0 + 300, 10, 10),
                       nodata=-1) as ds:
        ds.write(lc, 1)
    with rasterio.open(d / "area_code.tif", "w", driver="GTiff", height=20, width=20, count=1, dtype="uint8",
                       crs="EPSG:2193", transform=from_origin(X0, Y0 + 200, 10, 10), nodata=0) as ds:
        ds.write(np.full((20, 20), 9, np.uint8), 1)
    table = pd.DataFrame({"gid": [13224 * 10**6], "down": [-1], "area_km2": [0.14], "region": ["R"],
                          "lon": [176.0], "lat": [-38.2]})
    cfg = load_config()
    _, stats = build_weights([d / "local_catchment_core.tif"], table, [cfg.grids["ecmwf"]], strip_rows=16,
                             log=lambda m: None)
    assert stats["land_pixels"] == 400 and stats["land_unassigned"] == 0
    assert stats["pixels_assigned"] == int((lc >= 0).sum()) and stats["pixel_area_m2"] == 100.0


def test_viewer_lookup_stats_and_pack():
    from river_finder import viewer_nz as V

    fn = V.gid_lookup(np.array([30, 10, 20]), np.array([3, 1, 2], np.uint8))
    out = fn(None, np.array([[10, -1], [20, 99]]))
    assert out.tolist() == [[1, 0], [2, 0]] and fn(None, np.array([[-1]])) is None
    s = V._tile_stats([{"cells": 100, "nodata_cells": 10, "cell_m": 2.0, "routing": {"flood_s": 3.0, "d8_s": 1.5,
                                                                                     "flat_cells": 7}}])
    assert s == {"valid_cells": 90, "cellsize_m": 2.0, "routing_seconds": 4}
    assert V._tile_stats([{"valid_cells": 5, "routing_seconds": 2.0}])["cellsize_m"] == 8.0
    n = 2
    S = {k: [0] * n for k in V.PACK} | {"features": [], "z": [[]] * n, "c": [[[176.0, -38.0], [176.001, -38.0]]] * n,
                                         "gr": [None] * n}
    _, h8 = V.pack_segments(S)
    assert {f["k"] for f in h8["fields"]} == set(V.PACK)
    S1 = S | {"s100": [12.5, 0.0], "stp": [3, 0], "smax": [4.2, 0], "f1m": [1, 0], "lake": [0, 1], "dsrc": [0, 3],
              "ly": [2021, None]}
    blob, h1 = V.pack_segments(S1)
    f = {x["k"]: x for x in h1["fields"]}
    assert set(V.PACK_1M) <= set(f)
    ly = np.frombuffer(blob, "<u2", n, f["ly"]["off"])
    assert ly.tolist() == [2022, 0]          # u16null: value + 1, 0 = none


def test_truncation_rasters_and_overlapping_vrt(tmp_path):
    root = _one_m_root(tmp_path)
    pd.DataFrame({"gid": [1, 2, 3], "truncated": [True, False, True]}).to_parquet(root / "network.parquet")
    paths = []
    for t, x0, v in (("q24y32", X0 - 3000, 1), ("q25y32", X0 - 1000, 2)):   # boxes overlap by 2 km
        d = root / "tiles" / t
        d.mkdir(parents=True)
        a = np.full((4, 4), v, np.int64)
        a[0, 0] = -1
        if v == 2:
            a[1, 1] = 3
        with rasterio.open(d / "local_catchment_core.tif", "w", driver="GTiff", height=4, width=4, count=1,
                           dtype="int64", crs="EPSG:2193", transform=from_origin(x0, Y0, 1000, 1000),
                           nodata=-1) as ds:
            ds.write(a, 1)
        paths.append(d / "local_catchment_core.tif")
    with national.using_root(root):
        out = nz.truncation_rasters(root / "gis" / "truncated", log=lambda m: None)
    with rasterio.open(out[0]) as ds:
        m = ds.read(1)
    assert m[0, 0] == 255 and (m.ravel()[1:] == 1).all()
    with rasterio.open(out[1]) as ds:
        m = ds.read(1)
    assert m[1, 1] == 1 and m[2, 2] == 0
    nz._vrt(paths, root / "lc.vrt", "Int64", -1)
    assert "ComplexSource" in (root / "lc.vrt").read_text() and "<NODATA>-1</NODATA>" in (root / "lc.vrt").read_text()
    nz._vrt(paths[:1], root / "one.vrt", "Int64", -1)
    assert "SimpleSource" in (root / "one.vrt").read_text()


# --- rf-lxa.13: the gids the merge dropped (zero-length reaches) in the readers of local_catchment_core.tif
A1, A2, A3, A4, A5 = (13224 * 10**6 + i for i in range(1, 6))     # q24y32
B1 = 13225 * 10**6 + 1                                              # q25y32
DROPPED = [[A3, A2], [A4, B1], [A5, -1]]     # into the reach below, into another tile's reach, lost


def _dropped_root(tmp_path, merged=DROPPED):
    """A 1 m root of two 20 x 20 tiles (10 m cells, 1 cell = 1e-4 km2) whose merge dropped A3 (into A2), A4 (into
    B1, in the other tile) and A5 (lost). Tile A: rows 0-9 A1 | A2, rows 10-14 A3 | A4, rows 15-19 A5 | none;
    tile B: B1 but its first row."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    root = _one_m_root(tmp_path)
    pd.DataFrame({"gid": [A1, A2, B1], "truncated": [False, True, False]}).to_parquet(root / "network.parquet")
    zl = {"n": len(merged), "merged": merged} if merged is not None else None
    (root / "merge.json").write_text(json.dumps({"tiles": ["q24y32", "q25y32"]} | ({"zero_length": zl} if zl else {})))
    a = np.full((20, 20), -1, np.int64)
    a[:10, :10], a[:10, 10:], a[10:15, :10], a[10:15, 10:], a[15:, :10] = A1, A2, A3, A4, A5
    b = np.full((20, 20), B1, np.int64)
    b[0] = -1
    raw = {}
    for t, x0, v in (("q24y32", X0, a), ("q25y32", X0 + 200, b)):
        d = root / "tiles" / t
        d.mkdir(parents=True)
        with rasterio.open(d / "local_catchment_core.tif", "w", driver="GTiff", height=20, width=20, count=1,
                           dtype="int64", crs="EPSG:2193", transform=from_origin(x0, Y0 + 200, 10, 10),
                           nodata=-1) as ds:
            ds.write(v, 1)
        raw[t] = v
    return root, raw


def test_gid_map_from_merge_json(tmp_path):
    assert not national.merged_gids(tmp_path / "nowhere") and len(national.merged_gids(tmp_path / "nowhere")) == 0
    old, _ = _dropped_root(tmp_path / "old", merged=None)     # a merge before rf-lxa.12: no zero_length section
    a = np.array([[A3, -1]])
    assert not national.merged_gids(old) and national.merged_gids(old)(a) is a
    root, raw = _dropped_root(tmp_path / "new")
    g = national.merged_gids(root)
    assert len(g) == 3 and g.as_dict() == {A3: A2, A4: B1, A5: -1} and national.merged_gids(root) is g   # read once
    out = g(raw["q24y32"])
    assert out.dtype == np.int64 and (out[10:15, :10] == A2).all() and (out[10:15, 10:] == B1).all()
    assert (out[15:] == -1).all() and (out[:10] == raw["q24y32"][:10]).all()
    assert g(raw["q25y32"]) is raw["q25y32"] and g(np.zeros(0, np.int64)).size == 0   # nothing to map: as it is
    assert g(np.array([A3, A1, 7], np.int64)).tolist() == [A2, A1, 7]
    with national.using_root(root):
        assert national.merged_gids().as_dict() == g.as_dict()
    assert national.GidMap([[1, 2], [2, 3]]).as_dict() == {1: 3, 2: 3}     # chains followed to their end
    assert national.GidMap([[1, 2], [2, 1]]).as_dict() == {1: -1, 2: -1}


def test_readers_map_dropped_gids(tmp_path):
    """Rain weights, the pixel local areas, the gauge locator, the GIS truncation mask and local-catchment mosaic
    and the viewer lookups count a dropped gid's cells for the reach that took them (none for a lost one)."""
    from river_finder import viewer_nz as V
    from river_finder.rainfall.config import load_config
    from river_finder.rainfall.validate import locate_gauges
    from river_finder.rainfall.weights import build_weights, tile_paths
    from river_finder.relink import tile_local_km2

    root, raw = _dropped_root(tmp_path)
    gmap = national.merged_gids(root)
    table = pd.DataFrame({"gid": [A1, A2, B1], "down": [-1, -1, -1], "area_km2": [0.01, 0.015, 0.043],
                          "region": ["R"] * 3, "lon": [176.0] * 3, "lat": [-38.2] * 3})
    grid = load_config().grids["ecmwf"]
    paths = tile_paths(root / "tiles")
    old, s0 = build_weights(paths, table, [grid], strip_rows=8, log=lambda m: None)
    new, s1 = build_weights(paths, table, [grid], strip_rows=8, gid_map=gmap, log=lambda m: None)
    assert old["ecmwf"].npix.tolist() == [100, 100, 380] and s0["pixels_unknown_gid"] == 150
    assert new["ecmwf"].npix.tolist() == [100, 150, 430] and s1["pixels_unknown_gid"] == 50   # A5: lost
    assert s1["pixels_assigned"] == 680 and s1["gids_mapped"] == 3
    np.testing.assert_allclose(new["ecmwf"].area_m2 / 1e6, table["area_km2"])      # pixel areas = local areas
    np.testing.assert_allclose(np.asarray(new["ecmwf"].W.sum(axis=1)).ravel(), 1.0)
    np.testing.assert_allclose(tile_local_km2(root / "tiles", table["gid"].to_numpy()), [0.01, 0.015, 0.043])
    np.testing.assert_allclose(tile_local_km2(root / "tiles", table["gid"].to_numpy(), gid_map=national.GidMap()),
                               [0.01, 0.01, 0.038])
    # gauges on an A3 and an A4 cell, and on A5 (lost: the nearest catchment, A3's cells -> A2)
    x = np.array([X0 + 25, X0 + 155, X0 + 25])
    y = np.array([Y0 + 200 - 115, Y0 + 200 - 125, Y0 + 200 - 165])
    assert locate_gauges(root / "tiles", x, y).tolist() == [A2, B1, A2]
    with national.using_root(root):
        tm = nz.truncation_rasters(root / "gis" / "truncated", log=lambda m: None)
        lcp = nz.local_catchment_rasters(root / "gis" / "local_catchment", log=lambda m: None)
    with rasterio.open(tm[0]) as ds:
        m = ds.read(1)
    assert (m[:15, 10:] == np.r_[[1] * 10, [0] * 5][:, None]).all()      # A2 and A3 truncated, A4 -> B1 not
    assert (m[:10, :10] == 0).all() and (m[15:] == 255).all()
    assert lcp == [root / "gis" / "local_catchment" / "q24y32.tif", root / "tiles" / "q25y32" /
                   "local_catchment_core.tif"]
    with rasterio.open(lcp[0]) as ds:
        assert (ds.read(1) == gmap(raw["q24y32"])).all() and ds.nodata == -1 and ds.dtypes[0] == "int64"
        assert "dropped_gids_mapped" in ds.tags()
    fn = V.gid_lookup(np.array([A2, B1]), np.array([7, 9], np.uint8), gmap)
    out = fn(None, raw["q24y32"])
    assert (out[:15, 10:] == np.r_[[7] * 10, [9] * 5][:, None]).all() and (out[10:15, :10] == 7).all()
    assert (out[15:] == 0).all() and (out[:10, :10] == 0).all()
    assert (V.gid_lookup(np.array([A2]), np.array([7], np.uint8))(None, raw["q24y32"])[10:15, :10] == 0).all()


def _card(mid, order, data=None, kind="logistic"):
    c = {"id": mid, "order": order, "kind": kind, "title": mid, "description": "", "created": "2026-11-27",
         "coef": {}, "intercept": 0.0, "features": [], "labels": {"variant": "all", "split": "s", "positives": 1}}
    if data:
        c["data"] = data
    return c


def test_cards_by_build(tmp_path, monkeypatch):
    from river_finder import models as M

    md = tmp_path / "models"
    M.set_models_dir(md)
    try:
        md.mkdir()
        for c in (_card("v1", 1), _card("v4", 4), _card("v5", 5, "1m")):
            (md / f"{c['id']}.json").write_text(json.dumps(c))
        (md / "evaluations.json").write_text(json.dumps({"evaluations": [{"model": "v4", "auc": 0.9}]}))
        (md / "evaluations-1m.json").write_text(json.dumps({"evaluations": [{"model": "v5", "auc": 0.95}]}))
        assert [c["id"] for c in M.list_cards()] == ["v1", "v4", "v5"]
        assert [c["id"] for c in M.list_cards("8m")] == ["v1", "v4"] and M.card_data(M.load_card("v5")) == "1m"
        monkeypatch.setattr(M, "cv_summary", lambda: None)
        reg8 = M.viewer_models()
        assert [c["id"] for c in reg8["models"]] == ["v1", "v4"] and reg8["default"] == "v4"
        assert "archived" not in reg8                         # the 8 m site does not list the 1 m cards
        with national.using_root(_one_m_root(tmp_path)):
            reg = M.viewer_models()
            assert [c["id"] for c in reg["models"]] == ["v5"] and reg["default"] == "v5" and reg["data"] == "1m"
            assert [c["id"] for c in reg["archived"]] == ["v1", "v4"]
            assert reg["evaluations"]["evaluations"][0]["model"] == "v5"
            assert reg["archived_evaluations"]["evaluations"] == [{"model": "v4", "auc": 0.9}]
            assert M.evaluations_path().name == "evaluations-1m.json"
            M.save_card(_card("v6", 6))
            assert M.load_card("v6")["data"] == "1m"
    finally:
        M.set_models_dir(national.REPO_ROOT / "models")


def _with_x1(t: pd.DataFrame, seed: int = 4) -> pd.DataFrame:
    """Synthetic 1 m section columns, as x1.arrow holds them: steep reaches get box-like, walled channels."""
    from river_finder import xsec1m as X1

    rng = np.random.default_rng(seed)
    n = len(t)
    narrow = 1.0 / (1.0 + t["grad_s"].to_numpy() / 20.0)
    x = {"gid": t["gid"].to_numpy(np.int64), "x1_n": np.full(n, 5, np.int16)}
    for h in X1.STAGES:
        tg = X1.stage_tag(h)
        w = np.clip((5 + 20 * h) * narrow * rng.uniform(0.7, 1.3, n), 1, 1000)
        x[f"x1_w{tg}_med"], x[f"x1_w{tg}_min"] = w, w * rng.uniform(0.3, 1.0, n)
        x[f"x1_a{tg}_med"], x[f"x1_p{tg}_med"] = 0.6 * w * h, w + 2 * h
    for c, v in {"x1_wall_lo_med": 100 * (1 - narrow), "x1_vf_med": narrow * 5, "x1_b_med": narrow,
                 "x1_ffbf_med": 1 - narrow / 2, "x1_ang_med": 60 * (1 - narrow), "x1_steep_med": 1 - narrow,
                 "x1_rough": rng.uniform(0.05, 1, n), "x1_dropconc": rng.random(n), "x1_w0_med": 3.0}.items():
        x[c] = np.asarray(v, float) * np.ones(n)
    x = {k: (v.astype(np.float32) if np.asarray(v).dtype.kind == "f" else v) for k, v in x.items()}
    return pd.DataFrame(x)


def test_x1_features_recipes_and_card(tmp_path, monkeypatch):
    from test_ml import _synthetic_table

    from river_finder import ml
    from river_finder import models as M
    from river_finder.labels import Section
    from river_finder.models import Mapped

    assert ml.spec_by_id("gbm_xsec").features == ml.BASE + ml.TERRAIN + ml.XSEC
    assert ml.spec_by_id("logit_xsec").features == ml.spec_by_id("logit_plus").features + ml.XSEC
    assert ml.sections_of(ml.XSEC) == "1m" and ml.sections_of(ml.XSEC8) == "8m" and not ml.sections_of(ml.BASE)
    for name in ml.XSEC1M_ALL:
        assert name in ml.EXTRA and ml.EXTRA[name][2] == "x1" and ml.describe(name)
    from river_finder import xsec1mstudy as XS1

    assert all(XS1.FEATURES1M[k][0] is ml.EXTRA[k][0] for k in XS1.KEEP1M)   # one builder, the study's groups
    root = _one_m_root(tmp_path)
    t = _synthetic_table().assign(gid=lambda f: 13224 * 10**6 + np.arange(len(f)))
    x1 = _with_x1(t)
    pf.write_feather(pa.Table.from_pandas(x1.iloc[: len(t) - 10]), root / "x1.arrow")   # 10 reaches without
    j = ml.join_x1(t, root / "x1.arrow", log=lambda m: None)
    assert (j["x1_n"].iloc[-10:] == 0).all() and np.isfinite(j["x1_v_q"].iloc[:5]).all()
    for name in ml.XSEC1M_ALL:
        assert np.isfinite(ml.column(j, name)).all(), name       # fills for reaches without sections
    assert ml.available(j, ml.spec_by_id("gbm_xsec")) and not ml.available(j, ml.spec_by_id("gbm_xsec8"))
    mapped = []
    for c in range(0, 120, 3):
        rows = np.arange(c * 12 + 6, c * 12 + 11)
        t.loc[rows, "grad_s"] = 60.0
        mapped.append(Mapped(Section(region=f"R{c % 4}", river=f"River {c}", status="runnable", grade="3"),
                             f"River {c} | R{c % 4}", rows, "name"))
    split = {"name": "s", "train": sorted(m.key for i, m in enumerate(mapped) if i % 4),
             "test": sorted(m.key for m in mapped[::4])}
    M.set_models_dir(tmp_path / "models")
    monkeypatch.setitem(ml.GBM_PARAMS, "n_estimators", 20)
    try:
        with national.using_root(root):
            card, text = ml.train_card(t, mapped, split, "v5t", "x1 test", "", spec_id="gbm_xsec", order=9)
            assert set(ml.XSEC) <= set(card["features"])
            ml.save_gbm(card, text)
            assert M.load_card("v5t")["data"] == "1m"
            ml._BOOSTERS.clear()
            lg = M.logits(M.load_card("v5t"), t)            # the x1_* joined from x1.arrow by gid
            assert np.isfinite(lg).all() and len(lg) == len(t)
        with pytest.raises(KeyError, match="1 m section"):
            ml.enrich(t.drop(columns="gid"), xsec="1m")
    finally:
        M.set_models_dir(national.REPO_ROOT / "models")


def test_national_table_carries_the_1m_fields(tmp_path):
    from river_finder import models as M

    root = _one_m_root(tmp_path)
    d = root / "regions" / "bay-of-plenty"
    d.mkdir(parents=True)
    n = 3
    cols = {c: np.ones(n) for c in M._RAW + ["valley_width_m", "area_km2", "length_m", "score_regression"]}
    g = gpd.GeoDataFrame({"seg_id": np.arange(n), "gid": 13225 * 10**6 + np.arange(n),
                          "down_gid": [13225 * 10**6 + 1, -1, -1], "river": ["A", "", "B"], "label": 0,
                          "holdout": False, **cols, "steps_n": [0, 2, 5], "lake": [False, True, False],
                          "outlet_kind": [0, 1, 3], "river_linz": ["", "", "B"], "dem_source": "lidar"},
                         geometry=[shapely.LineString([(X0 + i, Y0), (X0 + i + 100, Y0)]) for i in range(n)],
                         crs="EPSG:2193")
    g.to_file(d / "segments_scored.gpkg", layer="segments", driver="GPKG")
    with national.using_root(root):
        t = M.national_table(rebuild=True, log=lambda m: None)
    assert {"steps_n", "lake", "outlet_kind", "river_linz", "dem_source"} <= set(t.columns)
    assert t["lake"].dtype == bool and t["down"].tolist() == [1, -1, -1] and t["region"].iloc[0] == "Bay of Plenty"
    assert not (root / "segments_table_relink_log.csv").exists()


def _fake_steps(calls):
    def mk(name):
        def fn(ctx):
            calls.append(name)
            return {"n": len(calls)}
        return fn

    return [F.Step("inputs", mk("inputs"), (), "a"), F.Step("joins", mk("joins"), ("inputs",), "b"),
            F.Step("score", mk("score"), ("joins",), "c"), F.Step("compare", mk("compare"), ("inputs",), "d")]


def test_finish_bookkeeping_and_switch(tmp_path, monkeypatch):
    calls = []
    steps = _fake_steps(calls)
    monkeypatch.setattr(F, "STEPS", steps)
    monkeypatch.setattr(F, "BY_NAME", {s.name: s for s in steps})
    root = _one_m_root(tmp_path)
    log = lambda m: None   # noqa: E731
    st = F.finish(root, models_dir=tmp_path / "models", log=log)
    assert calls == ["inputs", "joins", "score", "compare"] and set(st["steps"]) == set(calls)
    assert st["steps"]["score"]["peak_rss_gb"] > 0 and F.state_path(root).exists()
    F.finish(root, models_dir=tmp_path / "models", log=log)
    assert len(calls) == 4                                  # all done: nothing runs
    F.finish(root, steps=["joins"], models_dir=tmp_path / "models", log=log)
    st = F.load_state(root)
    assert calls[-1] == "joins" and "score" not in st["steps"] and "compare" in st["steps"]
    assert "joins ran again" in st["stale"]["score"]
    with pytest.raises(SystemExit, match="needs joins"):
        F.finish(tmp_path / "other", steps=["score"], models_dir=tmp_path / "models", log=log)
    F.finish(root, models_dir=tmp_path / "models", log=log)
    assert calls[-1] == "score"
    with pytest.raises(SystemExit, match="unknown steps"):
        F.finish(root, steps=["nope"], log=log)
    cfg = tmp_path / "national.toml"
    cfg.write_text('# comment\n[data]\nroot = "data/nz"\n')
    monkeypatch.setattr(national, "ROOT_CONFIG", cfg)
    with pytest.raises(SystemExit, match="not switching"):
        F.switch(root, log=log)
    F.switch(root, force=True, log=log)
    assert f'root = "{root.resolve()}"' in cfg.read_text() and cfg.read_text().startswith("# comment")
    F.switch(root, back=True, log=log)
    assert 'root = "data/nz"' in cfg.read_text()
    from river_finder import models as M

    M.set_models_dir(national.REPO_ROOT / "models")


def test_inputs_links_layers_merge_and_tiles(tmp_path):
    inp, merge, tiles, root = (tmp_path / n for n in ("inputs", "merge", "tiles", "root"))
    for d in (inp, merge, tiles, root):
        d.mkdir()
    (inp / "rec2.gpkg").write_bytes(b"x" * 10)
    (inp / "linz").mkdir()
    (inp / "linz" / "a.gpkg").write_bytes(b"y")
    for f in F.MERGE_FILES:
        (merge / f).write_text(json.dumps({"tiles": ["q24y32"], "reaches": 3}) if f == "merge.json" else "z")
    (tiles / "q24y32").mkdir()
    (tiles / "q24y32" / "tile.json").write_text(json.dumps({"done": True, "build": "lidar1m-x"}))
    ctx = F.Ctx(root=root, inputs=inp, merge=merge, tiles=tiles, ref8m=inp, log=lambda m: None)
    out = F.step_inputs(ctx)
    assert out["files"]["rec2.gpkg"] == "copy" and out["dirs"]["linz"]["copy"] == 1 and out["builds"] == ["lidar1m-x"]
    assert (root / "tiles" / "q24y32" / "tile.json").exists() and (root / "network.parquet").exists()
    assert "flood.gpkg" in out["missing"]
    (tiles / "q24y32" / "tile.json").write_text(json.dumps({"done": False}))
    with pytest.raises(SystemExit, match="done tile.json"):
        F.step_inputs(ctx)


def test_compare_helpers():
    g1 = gpd.GeoDataFrame({"area_km2": [10.0, 50.0], "river": ["A", "B"]},
                          geometry=[shapely.LineString([(0, 0), (500, 0)]), shapely.LineString([(0, 20), (500, 20)])],
                          crs="EPSG:2193")
    g5 = gpd.GeoDataFrame({"area_km2": [11.0, 45.0, 10.0]},
                          geometry=[shapely.LineString([(100, 5), (200, 5)]),
                                    shapely.LineString([(100, 15), (200, 15)]),
                                    shapely.LineString([(100, 500), (200, 500)])], crs="EPSG:2193")
    assert C.match_reaches(g5, g1).tolist() == [0, 1, -1]
    r = C.rec2_agreement(pd.DataFrame({"area_mid_km2": [10.0, 30.0, 0.5], "rec2_area_at_km2": [10.5, 20.0, 0.5]}))
    assert r["all"]["n"] == 2 and r["all"]["within_10pct"] == 0.5 and r["gt_5km2"]["n"] == 2
    t = pd.DataFrame({"down": [1, 2, -1, 2], "up": [-1, 0, 1, -1], "x": [0.0, 100, 200, 0], "y": 0.0,
                      "length_m": 100.0})
    s = C.subset(t, np.array([False, True, True, True]))
    assert s["down"].tolist() == [1, -1, 1] and s["up"].tolist() == [-1, 0, -1]
    assert C._overlap(t, [0, 1], t, [1]) == 1.0 and C._overlap(t, [0, 1], t, []) is None
