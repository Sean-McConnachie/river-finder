"""Rainfall per catchment (rf-t26.1): grids, weight matrix, rolling buffer, config, ingest."""

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
import rasterio
import scipy.sparse as sp
from pyproj import Transformer
from rasterio.transform import from_origin

from river_finder.rainfall import (
    ECMWF_GLOBAL,
    GSMAP_GLOBAL,
    CatchmentWeights,
    GridSpec,
    HourlyBuffer,
    HourlyField,
    ScaleFactors,
    SyntheticSource,
    build_weights,
    hour_index,
    hour_time,
    ingest,
    load_config,
)
from river_finder.rainfall.config import parse_config

T0 = datetime(2026, 11, 19, 0, tzinfo=UTC)
H0 = hour_index(T0)


# --- grids --------------------------------------------------------------------------------------
def test_grid_offsets_and_crop():
    cfg = load_config()
    gs, ec = cfg.grids["gsmap"], cfg.grids["ecmwf"]
    assert gs.offset_in(GSMAP_GLOBAL) == (938, 1658)
    assert ec.offset_in(ECMWF_GLOBAL) == (1238, 3458)  # ECMWF longitudes start at 180 (= -180)
    # GSMaP cells are centred on x.x5, ECMWF cells on x.x0: 172.02 E sits in different cells
    r, c = gs.cell_rc(172.02, -43.02)
    assert abs(gs.lons[c] - 172.05) < 1e-9 and abs(gs.lats[r] - -43.05) < 1e-9
    r, c = ec.cell_rc(172.02, -43.02)
    assert abs(ec.lons[c] - 172.0) < 1e-9 and abs(ec.lats[r] - -43.0) < 1e-9
    with pytest.raises(ValueError, match="not on the"):
        GridSpec("x", 165.8, -33.85, 0.1, -0.1, 10, 10).offset_in(GSMAP_GLOBAL)
    # crop a global field
    glob = np.arange(GSMAP_GLOBAL.size, dtype=np.float32).reshape(GSMAP_GLOBAL.shape)
    sub = gs.crop(glob, GSMAP_GLOBAL)
    assert sub.shape == gs.shape and sub[0, 0] == glob[938, 1658] and sub[-1, -1] == glob[938 + 139, 1658 + 131]
    assert hour_time(H0) == T0 and hour_index(T0 + timedelta(hours=5)) == H0 + 5
    with pytest.raises(ValueError):
        hour_index(datetime(2026, 11, 19, 0, 30, tzinfo=UTC))


# --- weights ------------------------------------------------------------------------------------
TOY = GridSpec("toy", lon0=0.5, lat0=0.5, dlon=1.0, dlat=1.0, nx=2, ny=1)  # two cells side by side


def _toy_weights(counts_rows, n_cells=2, area=1.0):
    rows, cells, vals = [], [], []
    for r, row in enumerate(counts_rows):
        for c, v in row.items():
            rows.append(r)
            cells.append(c)
            vals.append(v)
    counts = sp.csr_matrix((np.array(vals, np.int32), (rows, cells)), shape=(len(counts_rows), n_cells))
    n = len(counts_rows)
    npix = np.array([sum(r.values()) for r in counts_rows], dtype=np.int64)
    return CatchmentWeights(TOY, counts, np.arange(n, dtype=np.int64) + 1000, npix, area,
                            np.full(n, -1, dtype=np.int64), np.zeros(n, dtype=np.int16), ("all",),
                            np.zeros(n, np.float32), np.zeros(n, np.float32))


def test_toy_catchment_mm_and_m3():
    # catchment 0: 3 pixels in cell 0 and 1 in cell 1; catchment 1: 2 pixels, all in cell 1
    w = _toy_weights([{0: 3, 1: 1}, {1: 2}], area=64.0)
    assert np.allclose(w.W.toarray(), [[0.75, 0.25], [0.0, 1.0]])
    mm = w.mm(np.array([[4.0, 8.0]]))
    assert np.allclose(mm, [5.0, 8.0])                     # 0.75 * 4 + 0.25 * 8
    assert np.allclose(w.area_m2, [256.0, 128.0])
    assert np.allclose(w.m3(mm), [5.0 / 1000 * 256, 8.0 / 1000 * 128])
    assert np.isnan(w.mm(np.array([[np.nan, 1.0]])))[0]   # a NaN cell makes the catchment NaN
    assert w.mm(np.array([[np.nan, 1.0]]))[1] == 1.0


def _write_tile(path, gid, transform):
    with rasterio.open(path, "w", driver="GTiff", height=gid.shape[0], width=gid.shape[1], count=1, dtype="int64",
                       crs="EPSG:2193", transform=transform, nodata=-1) as dst:
        dst.write(gid, 1)


def test_build_weights_matches_exact_transform(tmp_path):
    cfg = load_config()
    grids = [cfg.grids["gsmap"], cfg.grids["ecmwf"]]
    # an 11 x 12 km box of 100 m pixels around 172.0 E, 43.0 S: GSMaP and ECMWF cell edges both cross it
    to_tm = Transformer.from_crs("EPSG:4326", "EPSG:2193", always_xy=True)
    x0, y1 = to_tm.transform(171.96, -42.96)
    x0, y1 = round(x0, -2), round(y1, -2)
    h, w, res = 120, 110, 100.0
    tr = from_origin(x0, y1, res, res)
    rr, cc = np.mgrid[0:h, 0:w]
    tile = 1234
    local = (rr // 25) * 10 + cc // 25                     # 25 x 25-pixel catchments
    gid = np.where((rr + cc) % 17 == 0, -1, tile * 1_000_000 + local).astype(np.int64)  # some pixels drain nowhere
    gid[0, :5] = tile * 1_000_000 + 999                    # a gid that is not in the table
    (tmp_path / "t").mkdir()
    _write_tile(tmp_path / "t" / "local_catchment_core.tif", gid, tr)
    ids = np.unique(local)
    table = pd.DataFrame({"gid": np.r_[tile * 1_000_000 + ids, tile * 1_000_000 + 500],  # last: no pixels
                          "down": -1, "region": "R"})
    npx = np.array([(gid == g).sum() for g in table["gid"]])
    table["area_km2"] = npx * res * res / 1e6
    table["lon"], table["lat"] = 172.0, -43.0
    out, stats = build_weights([tmp_path / "t" / "local_catchment_core.tif"], table, grids, strip_rows=32,
                               lattice_step=8, check_points=500, log=lambda m: None)
    assert stats["pixels_unknown_gid"] == 5
    assert stats["pixels_assigned"] == int(npx.sum())
    # exact: transform every pixel centre and count
    to_ll = Transformer.from_crs("EPSG:2193", "EPSG:4326", always_xy=True)
    lon, lat = to_ll.transform(x0 + (cc + 0.5) * res, y1 - (rr + 0.5) * res)
    row_of = {int(g): i for i, g in enumerate(table["gid"])}
    for gs in grids:
        cw = out[gs.name]
        cell = gs.cell_index(lon, lat)
        exact = np.zeros((len(table), gs.size), dtype=np.int64)
        for g, c in zip(gid.ravel(), cell.ravel(), strict=True):
            if int(g) in row_of:
                exact[row_of[int(g)], c] += 1
        got = cw.counts.toarray()
        assert np.array_equal(got[:-1], exact[:-1])
        # the catchment without pixels: weight 1 on the cell of its midpoint, area 0
        assert cw.fallback[-1] and cw.area_m2[-1] == 0 and got[-1, gs.cell_index(172.0, -43.0)] == 1
        ch = cw.meta["checks"]
        assert ch["weight_sum_max_abs_dev"] < 1e-12 and ch["rows_empty"] == 0 and ch["missing_no_pixels"] == 1
        assert ch["area_ratio_within_10pct"] == 1.0
        assert cw.meta["interp_cell_mismatches"] == 0 and cw.meta["interp_max_error_m"] < 0.05  # 800 m lattice
        assert cw.meta["lattice_blocks_split_by_cell_edge"] > 0
        assert len({int(x) for x in np.flatnonzero(exact.sum(axis=0))}) >= 4  # the box spans several cells
        # save / load
        p = cw.save(tmp_path / f"w_{gs.name}.npz")
        back = CatchmentWeights.load(p)
        assert back.grid == gs and np.array_equal(back.counts.toarray(), got)
        assert back.rows_digest == cw.rows_digest and back.regions == ("R",)
        f = np.random.default_rng(1).random(gs.shape)
        assert np.allclose(back.mm(f), cw.mm(f))
    assert not np.array_equal(out["gsmap"].counts.indices, out["ecmwf"].counts.indices)


# --- buffer -------------------------------------------------------------------------------------
def _obs(n=3, **kw):
    return HourlyBuffer(n, "observed", **kw)


def test_windows_any_x():
    b = _obs()
    for h in range(H0, H0 + 30):  # 30 hours: the first 6 roll out
        assert b.put(h, np.array([1.0, 2.0, 0.5]), "SYNTHETIC").status == "new"
    assert b.anchor == H0 + 29 and b.hours_held() == list(range(H0 + 6, H0 + 30))
    for x, t in b.windows().items():
        assert t.all_complete and np.allclose(t.mm, [x, 2 * x, 0.5 * x])
        assert t.last_hour == H0 + 29 and t.first_hour == H0 + 30 - x and t.products == {"SYNTHETIC": x}
    assert np.allclose(b.window(17).mm, [17, 34, 8.5])
    for bad in (0, 25, 2.5):
        with pytest.raises(ValueError):
            b.window(bad)
    assert b.put(H0, np.ones(3), "SYNTHETIC").status == "outside"  # older than the 24 h held


def test_better_product_overwrites_same_hour():
    b = _obs()
    assert b.put(H0, np.full(3, 1.0), "GSMAP_NOW", "jaxa").status == "new"
    r = b.put(H0, np.full(3, 2.0), "GSMAP_NRT", "jaxa")
    assert (r.status, r.previous) == ("replaced", "GSMAP_NOW")
    row = b.slot_table()[-1]
    assert row["product"] == "GSMAP_NRT" and row["replaced"] == "GSMAP_NOW" and row["writes"] == 2
    assert np.allclose(b.window(1).mm, 2.0) and b.window(1).products == {"GSMAP_NRT": 1}
    assert b.put(H0, np.full(3, 9.0), "GSMAP_NOW").status == "skipped"   # a late NOW never undoes NRT
    assert np.allclose(b.window(1).mm, 2.0)
    assert b.put(H0, np.full(3, 3.0), "GSMAP_NRT").status == "updated"   # re-ingest of the same product
    assert np.allclose(b.window(1).mm, 3.0)
    assert b.put(H0, np.full(3, 9.0), "GSMAP_NOW", force=True).status == "replaced"


def test_missing_hours_are_reported():
    b = _obs()
    for h in range(H0, H0 + 6):
        if h == H0 + 2:
            continue                                   # no data at all for this hour
        v = np.array([1.0, 1.0, np.nan if h == H0 + 4 else 1.0], dtype=np.float32)  # catchment 2 lacks one hour
        b.put(h, v, "SYNTHETIC")
    t = b.window(6)
    assert t.missing_hours == [H0 + 2] and not t.all_complete
    assert list(t.n_missing) == [1, 1, 2]
    assert np.allclose(t.mm, [5, 5, 4])               # sums of the hours that exist, never padded silently
    assert np.isnan(t.strict()).all()
    assert np.allclose(b.window(1).mm, 1.0) and b.window(1).all_complete
    late = b.window(3, end=H0 + 7)                    # "now" is 2 h past the latest data
    assert late.missing_hours == [H0 + 6, H0 + 7] and list(late.n_missing) == [2, 2, 2]


def test_scale_factors_per_region():
    regions = ("West Coast", "Canterbury")
    b = _obs(region_of=np.array([0, 1, -1]), regions=regions)
    f = ScaleFactors("gsmap", regions, [2.0, 0.5], [3, 4])
    b.put(H0, np.array([1.0, 2.0, 4.0]), "GSMAP_NRT", factors=f)
    assert np.allclose(b.window(1, scaled=False).mm, [1, 2, 4])
    assert np.allclose(b.window(1).mm, [2, 1, 4])     # no region -> factor 1
    s = H0 % 24
    assert list(b.factor[s]) == [2.0, 0.5] and list(b.factor_version[s]) == [3, 4]
    assert b.slot_table()[-1]["factor_versions"] == [3, 4]
    with pytest.raises(ValueError, match="for ecmwf"):
        b.put(H0 + 1, np.ones(3), "GSMAP_NRT", factors=ScaleFactors("ecmwf", regions, [1, 1], [1, 1]))
    assert b.anchor == H0                              # a refused write changes nothing
    assert b.apply_factors(ScaleFactors("gsmap", regions, [1.0, 3.0], [5, 6])) == 1
    assert np.allclose(b.window(1).mm, [1, 6, 4]) and list(b.factor_version[s]) == [5, 6]


def test_forecast_buffer_next_hours():
    fc = HourlyBuffer(2, "forecast")
    run = H0 - 6
    assert fc.put(H0 + 1, np.ones(2), "ECMWF_FC", run_hour=run, lead=7).status == "outside"  # not anchored yet
    fc.advance(H0)
    for h in range(H0 + 1, H0 + 26):
        st = fc.put(h, np.full(2, 0.5), "ECMWF_FC", run_hour=run, lead=h - run).status
        assert st == ("new" if h <= H0 + 24 else "outside")
    assert np.allclose(fc.ahead(3).mm, 1.5) and fc.ahead(24).all_complete
    assert fc.put(H0 + 1, np.full(2, 2.0), "ECMWF_FC", run_hour=run + 6, lead=1).status == "updated"  # newer run
    assert fc.put(H0 + 1, np.full(2, 9.0), "ECMWF_FC", run_hour=run, lead=7).status == "skipped"     # older run
    assert np.allclose(fc.ahead(1).mm, 2.0)
    fc.advance(H0 + 2)                                 # two hours later: those hours are now past
    assert fc.hours_held()[0] == H0 + 3 and fc.ahead(22).all_complete       # H0+3 .. H0+24 held
    assert fc.ahead(23).missing_hours == [H0 + 25]
    with pytest.raises(ValueError):
        fc.window(3)
    with pytest.raises(ValueError):
        fc.put(H0 + 5, np.ones(2), "GSMAP_NOW")
    with pytest.raises(ValueError):
        HourlyBuffer(2, "observed").put(H0, np.ones(2), "ECMWF_FC")


def test_buffer_save_load(tmp_path):
    regions = ("A", "B")
    b = _obs(n=4, regions=regions, region_of=np.array([0, 1, 1, -1]), rows_digest="abc")
    for h in range(H0, H0 + 5):
        b.put(h, np.arange(4, dtype=np.float32) + h - H0, "GSMAP_NOW", "jaxa")
    b.put(H0 + 4, np.full(4, 7.0), "GSMAP_NRT", factors=ScaleFactors("gsmap", regions, [1.5, 1.0], [2, 2]))
    for compress in (True, False):
        p = b.save(tmp_path / f"obs_{compress}.npz", compress=compress)
        c = HourlyBuffer.load(p)
        assert (c.kind, c.anchor, c.regions, c.rows_digest) == ("observed", b.anchor, regions, "abc")
        for k in ("raw", "scaled", "hour", "product", "source", "run_hour", "lead", "replaced", "writes",
                  "factor", "factor_version", "region_of"):
            assert np.array_equal(getattr(b, k), getattr(c, k), equal_nan=k in ("raw", "scaled")), k
        assert np.allclose(c.window(5).mm, b.window(5).mm) and c.slot_table() == b.slot_table()


# --- config -------------------------------------------------------------------------------------
BASE = {
    "windows": [1, 3, 6, 12, 24],
    "observed": {"source": "gsmap", "compare": ["ecmwf"]},
    "forecast": {"source": "ecmwf"},
    "grids": {"gsmap": {"lon0": 165.85, "lat0": -33.85, "dlon": 0.1, "dlat": -0.1, "nx": 132, "ny": 140},
              "ecmwf": {"lon0": 165.8, "lat0": -33.8, "dlon": 0.1, "dlat": -0.1, "nx": 133, "ny": 141}},
}


def _with(**changes):
    d = {k: (dict(v) if isinstance(v, dict) else v) for k, v in BASE.items()}
    d["grids"] = {k: dict(v) for k, v in BASE["grids"].items()}
    for path, v in changes.items():
        keys = path.split("__")
        tgt = d
        for k in keys[:-1]:
            tgt = tgt[k]
        tgt[keys[-1]] = v
    return d


def test_config_committed_and_validation():
    cfg = load_config()
    assert cfg.windows == (1, 3, 6, 12, 24) and cfg.observed_source == "gsmap" and cfg.forecast_source == "ecmwf"
    assert cfg.weights_path("gsmap").name == "weights_gsmap.npz" and cfg.rain_dir.parts[-2:] == ("nz", "rain")
    assert load_config(nz_data="/x/nz").rain_dir.as_posix() == "/x/nz/rain"
    ok = parse_config(_with(windows=[24, 2, 7]))
    assert ok.windows == (2, 7, 24)
    sw = parse_config(_with(observed={"source": "ecmwf"}))
    assert sw.observed_source == "ecmwf" and sw.compare == ()
    for bad, msg in [
        (_with(windows=[0, 3]), "1..24"), (_with(windows=[25]), "1..24"), (_with(windows=[1, 1]), "duplicate"),
        (_with(windows=[]), "non-empty"), (_with(windows=[1.5]), "1..24"), (_with(windows=[True]), "1..24"),
        (_with(observed={"source": "radar"}), "no \\[grids.radar\\]"),
        (_with(forecast={"source": "gsmap"}), "no forecast product"),
        (_with(observed={"source": "gsmap", "compare": ["gsmap"]}), "must not repeat"),
        (_with(grids__gsmap__lon0=165.8), "not on the gsmap_global grid"),
        (_with(grids__ecmwf__nx=0), "nx and ny"),
        (_with(grids__ecmwf__nx=1.5), "integer"),
        (_with(extra=1), "unknown key"),
        (_with(paths={"tiels": "x"}), "unknown key"),
    ]:
        with pytest.raises(ValueError, match=msg):
            parse_config(bad)


# --- sources + ingest ---------------------------------------------------------------------------
def test_hourly_field_validation():
    g = TOY
    f = HourlyField(g, np.array([[1.0, -1e-9]]), T0, "ECMWF_FC", run=T0 - timedelta(hours=6))
    assert f.lead_hours == 6 and f.run_hour == H0 - 6 and f.mm[0, 1] == 0.0 and f.source == "ecmwf"
    with pytest.raises(ValueError, match="negative"):
        HourlyField(g, np.array([[1.0, -999.0]]), T0, "GSMAP_NOW")
    with pytest.raises(ValueError, match="shape"):
        HourlyField(g, np.zeros((2, 2)), T0, "GSMAP_NOW")
    with pytest.raises(ValueError, match="unknown"):
        HourlyField(g, np.zeros((1, 2)), T0, "RADAR")
    with pytest.raises(ValueError, match="timezone"):
        HourlyField(g, np.zeros((1, 2)), datetime(2026, 1, 1), "GSMAP_NOW")


def test_ingest_synthetic_source():
    w = _toy_weights([{0: 3, 1: 1}, {1: 2}], area=1e6)    # 4 km2 and 2 km2
    b = HourlyBuffer.for_weights(w, "observed")
    src = SyntheticSource(TOY, lambda h: np.array([[1.0, 3.0]]) * (h - H0), missing={H0 + 3})
    res = ingest(src, b, w, T0, T0 + timedelta(hours=6))
    assert [r.status for r in res] == ["new"] * 5 and [r.hour for r in res] == [H0 + i for i in (1, 2, 4, 5, 6)]
    t = b.window(6)
    assert t.missing_hours == [H0 + 3]
    per_hour = np.array([0.75 * 1 + 0.25 * 3, 3.0])       # mm per unit hour factor
    assert np.allclose(t.mm, per_hour * (1 + 2 + 4 + 5 + 6))
    assert np.allclose(t.m3(w.area_m2), t.mm / 1000 * np.array([4e6, 2e6]))
    better = SyntheticSource(TOY, 0.0, product_tag="SYNTHETIC_BETTER")
    assert {r.status for r in ingest(better, b, w, T0 + timedelta(hours=4), T0 + timedelta(hours=6))} == {"replaced"}
    assert np.allclose(b.window(2).mm, 0.0)
    with pytest.raises(ValueError, match="grid"):
        ingest(SyntheticSource(GSMAP_GLOBAL), b, w, T0, T0 + timedelta(hours=1))
