"""The per-tile 1 m job (nz1m.py, rf-lxa.6): the tile grid and ids, the 2 m aggregation in the reader threads, the
lean accumulation with sparse inflows, reaches cut on smoothed length, the 2 m pass (local areas, HAND), lake fill,
the chunked seam correction, and a whole synthetic tile whose output columns are exactly the spec's per-tile
fields."""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from rasterio.transform import from_origin

from river_finder import lidarpipe as P
from river_finder import lidarspec as S
from river_finder import nz1m as N

sys.path.insert(0, str(Path(__file__).parent))
import synth1m as SY  # noqa: E402


def _dem(seed=0, nr=120, nc=140):
    rng = np.random.default_rng(seed)
    r, c = np.mgrid[0:nr, 0:nc]
    z = 50 + 0.5 * r + 0.2 * np.abs(c - nc / 2) + rng.normal(0, 0.3, (nr, nc))
    return np.round(z, 1).astype(np.float32)


def test_tile_grid_ids_and_gids():
    t = N.Tile32.parse("q11y15")
    assert (t.ix, t.iy, t.id, t.parent) == (11, 15, "q11y15", "x05y07")
    assert t.core == (1_424_000.0, 5_216_000.0, 1_456_000.0, 5_248_000.0)
    assert t.box == (1_421_000.0, 5_213_000.0, 1_459_000.0, 5_251_000.0)
    g = S.gid(t.ix, t.iy, 12)
    assert g == 10_000_000_000 + 1511 * 1_000_000 + 12 and S.tile_of(g) == (11, 15)
    with pytest.raises(ValueError):
        N.Tile32.parse("x05y07")
    # the 64 km tile's four quarters tile it exactly
    from river_finder.nz import Tile

    big = Tile.parse("x05y07").core
    qs = [N.Tile32(10 + i, 14 + j).core for i in (0, 1) for j in (0, 1)]
    assert min(q[0] for q in qs) == big[0] and max(q[2] for q in qs) == big[2]
    assert min(q[1] for q in qs) == big[1] and max(q[3] for q in qs) == big[3]


def test_agg2_into_matches_agg_and_water_flags():
    rng = np.random.default_rng(3)
    z = (100 + rng.normal(0, 0.5, (64, 80))).astype(np.float32)
    z[10:30, 10:40] = 7.0                             # flattened water
    z[40:60, 50:80] = (np.arange(30)[None, :] * 0.1 + np.arange(20)[:, None] * 0.05).astype(np.float32)  # planar
    z[0:5, 0:7] = np.nan
    z[33, 21] = np.nan
    zo = np.full((32, 40), np.nan, np.float32)
    wo = np.zeros((32, 40), np.uint8)
    a = P.Agg((64, 80), 2)
    for r0 in (0, 32):                                 # four windows, as the reader threads would get them
        for c0 in (0, 40):
            w = z[r0:r0 + 32, c0:c0 + 40]
            P.agg2_into(w, r0, c0, zo, wo)
            a.add(w, r0, c0, P.water_flags(w))
    m = a.mean()
    assert np.array_equal(np.isnan(zo), np.isnan(m)) and np.allclose(zo[np.isfinite(m)], m[np.isfinite(m)])
    n, nf, npl = P.w2_counts(wo)
    assert np.array_equal(n, a.cnt) and np.array_equal(nf, a.nflat) and np.array_equal(npl, a.nplanar)
    assert nf[8, 8] == 4 and npl[25, 30] == 4


def test_stream_in_reader_threads():
    rng = np.random.default_rng(1)
    z1 = rng.normal(10, 1, (300, 500)).astype(np.float32)
    box = (1000, 2000, 1500, 2300)
    src = N.ArraySource(z1, box)
    jobs = P.sheet_windows(src.sheets, box, win=64)
    out = np.full(z1.shape, np.nan, np.float32)
    st = P.stream(jobs, N._into1(out), threads=8, in_reader=True, read=src.read)
    assert np.array_equal(out, z1) and st["cells"] == z1.size and st["in_reader"]


def test_accumulate_u32_and_sparse_inflows_match_lean():
    z = _dem()
    d = P.route_lean(z.copy(), 2.0)
    acc, fl = P.accumulate_lean(d, 2.0)
    cnt, done = N._accumulate_u32(d)
    assert done == d.size and np.allclose(cnt * 4.0, acc)
    tf = from_origin(0, 240, 2, 2)
    infl = [(30.0, 200.0, 3.5, 7), (200.0, 150.0, 1.25, 8)]
    used_lean = P.inject(d, acc, fl, tf, infl, radius_cells=3)
    ext_c, ext_v, used = N.inflows_sparse(d, cnt, tf, infl, radius_cells=3)
    tot = cnt.ravel().astype(np.float64) * 4.0
    tot[ext_c] += ext_v * 4.0
    assert [u["row"] for u in used] == [u["row"] for u in used_lean]
    assert np.allclose(tot, acc.ravel()) and np.all(np.diff(ext_c) > 0)


def test_reaches_cut_on_smoothed_length_and_local_areas():
    z = _dem(nr=400, nc=200)
    d = P.route_lean(z.copy(), 2.0)
    cnt, _ = N._accumulate_u32(d)
    tf = from_origin(0, 800, 2, 2)
    core = (0, 0, 400, 800)
    e = np.zeros(0, np.int64), np.zeros(0)
    links, R = N.build_reaches(d, cnt, *e, tf, core, min_area_km2=0.01, seg_len_m=100.0)
    assert len(R) > 10 and R["seg_id"].tolist() == list(range(len(R)))
    long = R[R.groupby("link")["seg_id"].transform("size") > 2]
    assert long["length_m"].between(50, 150).all()                     # ~100 m of smoothed length
    ds = R["down_seg"].to_numpy()
    assert ((ds == -1) | (ds > np.arange(len(R)))).all() or (ds >= 0).sum() > 0
    # the 2 m pass: local areas as lidarpipe.local_areas, HAND counts by brute force
    soc = N.seg_of_cell(d.shape, links, R)
    seg = pd.DataFrame({"cells": [links[li]["path"][a:b + 1] for li, a, b in zip(R["link"], R["a"], R["b"],
                                                                                 strict=True)]})
    ref, _ = P.local_areas(d, seg, 2.0)
    keep = np.ones(len(R), bool)
    lc10 = np.full((80, 40), -1, np.int32)
    h10 = np.full((80, 40), 255, np.uint8)
    zf = z.copy()
    d2 = P.route_lean(zf, 2.0)
    assert np.array_equal(d2, d)
    z10 = np.zeros((80, 40), np.uint8)
    out = N._pass2m(d, zf, soc, keep, np.full(d.shape, 100, np.uint8), z10, z10, z10, 5,
                    np.asarray(N.HAND_TH), len(R), lc10, h10)
    assert np.allclose(out[0] * 4e-6, ref)
    # brute force HAND <= 5 m for a few reaches
    off = P.offsets(d.shape[1])
    df, sf, zz = d.ravel(), soc.ravel(), zf.ravel()
    cnt5 = np.zeros(len(R), int)
    for i in range(d.size):
        v = i
        while sf[v] < 0 and df[v] < 8:
            v += off[df[v]]
        if sf[v] >= 0 and zz[i] - zz[v] <= 5.0:
            cnt5[sf[v]] += 1
    assert np.array_equal(out[7][:, 2], cnt5)
    assert (lc10[lc10 >= 0] < len(R)).all() and (h10 < 255).any()


def test_fill_holes_matches_fill_lakes_and_skips_the_sea():
    H = W = 64
    z = np.fromfunction(lambda r, c: 100 + 0.5 * r + 0.3 * c, (H, W)).astype(np.float32)
    z[20:44, 20:44] = 105.0
    z[24:40, 24:40] = np.nan
    z[:, :4] = np.nan                                   # the sea (no 8 m land)
    wc = np.full((H, W), 4 * 25, np.uint8)
    wc[20:44, 20:44] = 4 * 25 + 4 * 5                  # four flattened 1 m cells each
    wc[np.isnan(z)] = 0
    z8 = np.full((H // 4, W // 4), 90.0, np.float32)
    z8[:, :1] = np.nan
    ref = z.copy()
    flat = np.where(wc == 120, 1.0, 0.0).astype(np.float32)
    P.fill_lakes(ref, flat, land=np.repeat(np.repeat(np.isfinite(z8), 4, 0), 4, 1))
    recs = N.fill_holes(z, wc, z8, from_origin(0, 128, 2, 2))
    assert np.array_equal(np.isnan(z), np.isnan(ref)) and np.allclose(z[24:40, 24:40], 105.0)
    assert (wc[24:40, 24:40] == N.W_LAKE).all() and np.isnan(z[:, :4]).all()
    lake = [r for r in recs if r["filled"]]
    assert len(lake) == 1 and lake[0]["level"] == pytest.approx(105.0) and lake[0]["land_share"] == 1.0


def test_mask_sea_only_surveyed_water_outside_the_8m_land():
    z = np.full((16, 16), 5.0, np.float32)
    wc = np.full((16, 16), 4 * 25, np.uint8)            # four valid 1 m cells, dry
    wc[:, :8] = 4 * 25 + 4 * 5                           # flattened water on the left half ...
    z[:, :8] = -0.8
    z8 = np.full((4, 4), 3.0, np.float32)
    z8[:, :1] = np.nan                                   # ... the 8 m DEM's sea covers columns 0-3
    wc[0:4, 0:4] = 4 * 25                                # a dry beach outside the 8 m land: kept
    z[12:16, 0:4] = 15.0                                 # water above the sea limit: kept
    n = N._mask_sea(z, wc, z8, 4, N.SEA_MAX_Z)
    assert n == 8 * 4 and np.isnan(z[4:12, :4]).all() and (wc[4:12, :4] == P.W2_SEA).all()
    assert np.isfinite(z[:4, :4]).all() and np.isfinite(z[12:, :4]).all()
    assert np.isfinite(z[:, 4:]).all()                   # inside the 8 m land: a lake or river, not the sea


def test_seam_fill_is_continuous_like_seam_correct():
    H, W = 200, 240
    r, c = np.mgrid[0:H, 0:W]
    truth = (300 + 0.2 * r + 0.05 * c + 0.5 * np.sin(r / 3.0)).astype(np.float32)
    off = (3.0 + 1.5 * np.sin(r / 9.0)).astype(np.float32)
    z8 = (truth + off).reshape(H // 4, 4, W // 4, 4).mean(axis=(1, 3)).astype(np.float32)
    fine = truth.copy()
    fine[:, 150:] = np.nan
    wc = np.where(np.isnan(fine), 0, 100).astype(np.uint8)
    z = fine.copy()
    st = N.seam_fill(z, wc, z8, band_m=100, sigma_m=40, taper_m=200, work_m=10, edge_m=25, chunk=64)
    z8_2 = np.full((H, W), np.nan, np.float32)
    for i in range(H):
        for j in range(W):
            z8_2[i, j] = N._bilin(z8, i, j, 4)
    ref, _ = P.seam_correct(fine, z8_2, 2.0, band_m=100, sigma_m=40, taper_m=200, work_m=2.0, edge_m=25.0)
    assert st["corrected"] and np.isfinite(z).all() and (wc[:, 150:] == N.W_FALLBACK).all()
    assert np.array_equal(z[:, :150], truth[:, :150])
    step = np.abs((z[:, 150] - z[:, 149]) - (truth[:, 150] - truth[:, 149])).mean()
    raw = np.abs((z8_2[:, 150] - z[:, 149]) - (truth[:, 150] - truth[:, 149])).mean()
    assert raw > 2.0 and step < 0.25
    d = np.abs(z[:, 150:] - ref[:, 150:])               # seam_correct worked at 2 m: the same within ~0.1 m
    assert d[:, :75].max() < 0.15 and d.max() < 0.35 and d.mean() < 0.1
    assert np.abs(z[:, 232:] - z8_2[:, 232:]).max() < 1e-4    # past the taper: the 8 m DEM


@pytest.fixture(scope="module")
def synth_tile(tmp_path_factory):
    z1, truth, z8, level = SY.terrain()
    od = tmp_path_factory.mktemp("q00y00")
    old = N.CHUNK_M
    N.CHUNK_M = 1000.0                                  # several 1 m chunks
    try:
        info = N.process(N.Tile32(0, 0), N.ArraySource(z1, SY.BOX), lambda: z8, None, None, od, threads=4,
                         log=lambda *a: None, core=SY.CORE, box=SY.BOX)
    finally:
        N.CHUNK_M = old
    return od, info, level


def test_tile_outputs_are_the_spec_fields(synth_tile):
    import pyarrow.feather as feather
    import pyogrio

    od, info, _ = synth_tile
    cols = S.tile_columns()
    g = pyogrio.read_dataframe(od / "segments_core.gpkg")
    assert [c for c in g.columns if c != "geometry"] == cols["segments_core.gpkg"]
    x1 = feather.read_table(od / "x1_reach.arrow").to_pandas()
    assert list(x1.columns) == cols["x1_reach.arrow"]
    spec = {f.name for f in S.FIELDS if f.stage in ("1m", "2m")} - {"geometry"}
    assert set(g.columns[:-1]) | set(x1.columns) == spec                # no field missing, none unexplained
    kept = {f["name"] for f in S.TILE_FILES if f["kept"] in ("yes", "optional")} - {"dem_10m.tif"}
    assert {p.name for p in od.iterdir()} == kept
    assert [c[0] for c in S.STEP_STORE["columns"]] == feather.read_table(od / "steps.arrow").column_names
    assert [c[0] for c in S.SECTION_STORE["columns"]] == feather.read_table(od / "xsec.arrow").column_names
    assert [c[0] for c in S.PROFILE_STORE["columns"]] == feather.read_table(od / "profiles.arrow").column_names
    assert json.loads((od / "tile.json").read_text())["done"] and N.is_done(od.name, od.parent.parent) is False


def test_synthetic_tile_contents(synth_tile):
    import pyarrow.feather as feather
    import pyogrio
    import rasterio

    od, info, level = synth_tile
    g = pyogrio.read_dataframe(od / "segments_core.gpkg")
    assert (g["gid"] // 1_000_000 == S.tile_index(0, 0)).all() and g["gid"].is_unique
    mid = g.geometry.interpolate(0.5, normalized=True)
    assert ((mid.x >= SY.CORE[0]) & (mid.x < SY.CORE[2]) & (mid.y >= SY.CORE[1]) & (mid.y < SY.CORE[3])).all()
    # the 6 m fall is one bed step on the main valley; the profiles decode to the stations of every core reach
    st = feather.read_table(od / "steps.arrow").to_pandas()
    main = st[(np.abs(st["x_cm"] / 100 - (SY.X0 + 1400)) < 10) & (np.abs(st["y_cm"] / 100 - SY.FALL_Y) < 10)]
    assert len(main) == 1 and main["h"].iloc[0] == pytest.approx(6.0, abs=0.3) and main["kind"].iloc[0] == 0
    pr = feather.read_table(od / "profiles.arrow")
    dec = P.decode_profiles({c: pr[c].to_numpy() if c in ("gid", "n", "z0_cm") else pr[c].to_pylist()
                             for c in pr.column_names})
    assert set(np.unique(dec["reach"])) == set(g["gid"]) and len(dec["z"]) <= info["stations"]
    assert (np.abs(pr["n"].to_numpy() - g.set_index("gid").loc[pr["gid"].to_numpy(), "length_m"].to_numpy()) <= 2).all()
    # the lake: filled at the rim level, one outlet reach in the core, its reaches flagged
    lk = [x for x in info["lakes"] if x["kind"] == "hole"]
    assert len(lk) == 1 and lk[0]["level"] == pytest.approx(level, abs=0.02) and lk[0]["outlet_in_core"]
    assert g.loc[g["lake"], "gid"].tolist() == lk[0]["reaches"] and (g.loc[g["lake"], "lidar_share"] < 1).all()
    # the fallback gap: corrected and reported; sections on the reaches of 1 km2 or more
    assert info["fallback"]["corrected"] and info["fallback"]["gap_km2"] == pytest.approx(0.16, abs=0.01)
    x1 = feather.read_table(od / "x1_reach.arrow").to_pandas().set_index("gid")
    big = g.loc[g["area_tile_km2"] >= 1.0, "gid"]
    assert (x1.loc[big, "x1_n"] > 0).all() and (x1.drop(big)["x1_n"] == 0).all()
    assert info["chunks"] > 1
    # local catchments: the core reaches' local areas are the raster's cells; owned cells + halo + lost = the box
    with rasterio.open(od / "local_catchment_core.tif") as ds:
        lc = ds.read(1)
    ids, n = np.unique(lc[lc >= 0], return_counts=True)
    assert set(ids) <= set(g["gid"])
    loc = g.set_index("gid")["local_km2"]
    assert np.allclose(n * 1e-4, loc.loc[ids], atol=0.02)
    land = (SY.BOX[2] - SY.BOX[0]) * (SY.BOX[3] - SY.SEA_Y) / 1e6
    tot = loc.sum() + info["halo_owned_km2"] + info["lost_km2"]
    assert tot == pytest.approx(land, rel=0.01)


def test_tile_with_no_core_reach_writes_empty_outputs(tmp_path):
    """A tile whose core holds no reach (a sliver of land at a tile corner, like q21y40) finishes with empty stores
    instead of failing in the 1 m pass."""
    import pyarrow.feather as feather
    import pyogrio

    z1, _, z8, _ = SY.terrain()
    core = (SY.X0 + 2280, SY.Y0 + 2480, SY.X0 + 2520, SY.Y0 + 2720)    # high ground in the north-east corner
    od = tmp_path / "q00y00"
    od.mkdir()
    info = N.process(N.Tile32(0, 0), N.ArraySource(z1, SY.BOX), lambda: z8, None, None, od, threads=2,
                     log=lambda *a: None, core=core, box=SY.BOX)
    assert info["done"] and info["stations"] == 0 and info["sections"] == 0
    assert len(pyogrio.read_dataframe(od / "segments_core.gpkg")) == 0
    assert feather.read_table(od / "profiles.arrow").num_rows == 0
    assert feather.read_table(od / "x1_reach.arrow").num_rows == 0
