"""Upstream rainfall totals down the river network (rf-t26.6), on tiny synthetic networks."""

import json
import os
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
import scipy.sparse as sp

from river_finder import cli
from river_finder.rainfall import (
    CatchmentWeights,
    GridSpec,
    HourlyBuffer,
    Network,
    ScaleFactors,
    UpstreamTotals,
    compute_totals,
    hour_index,
    hour_time,
    topo_order,
)
from river_finder.rainfall.config import parse_config
from river_finder.rainfall.pipeline import run_totals, status
from river_finder.rainfall.upstream import (
    ABOVE_EDGE,
    AREA_SHORT,
    EDGE,
    EDGE_BIG,
    OUTLET,
    RAW,
    SCALED,
    accumulate,
)

T0 = datetime(2026, 11, 19, 0, tzinfo=UTC)
H0 = hour_index(T0)

# Logical network, rows deliberately out of topological order:
#
#   A   B
#    \ /
#     C -> D -> E (outlet)    G (outlet, alone)    H -> I (outlet at a tile edge)
#               ^
#           F --'
ROW = {"E": 0, "A": 1, "D": 2, "G": 3, "C": 4, "F": 5, "B": 6, "I": 7, "H": 8}
DOWN = {"E": None, "A": "C", "D": "E", "G": None, "C": "D", "F": "E", "B": "C", "I": None, "H": "I"}
AREA_KM2 = {"E": 5.0, "A": 1.0, "D": 2.0, "G": 7.0, "C": 3.0, "F": 4.0, "B": 1.5, "I": 2.0, "H": 6.0}
REGIONS = ("R0", "R1")
REGION = {"E": 0, "A": 0, "D": 0, "G": 1, "C": 0, "F": 1, "B": 0, "I": 1, "H": 1}
VI = {"E": 1, "A": -1, "D": 0, "G": 0, "C": 2, "F": -1, "B": -1, "I": 1, "H": -1}
N = len(ROW)


def _col(d, dtype=np.float64):
    out = np.zeros(N, dtype=dtype)
    for k, r in ROW.items():
        out[r] = d[k]
    return out


DOWN_ROWS = np.array([-1 if DOWN[k] is None else ROW[DOWN[k]] for k in sorted(ROW, key=ROW.get)], dtype=np.int64)
AREA_M2 = _col(AREA_KM2) * 1e6
REGION_OF = _col(REGION, np.int16)
GID = np.arange(N, dtype=np.int64) * 7 + 1000


def brute(local, down=DOWN_ROWS):
    """Sum of ``local`` over every row upstream of each row (own included), by walking down from each row."""
    out = np.zeros_like(np.asarray(local, dtype=np.float64))
    for c in range(len(down)):
        r = c
        while r >= 0:
            out[r] += local[c]
            r = down[r]
    return out


def upstream_sets(down=DOWN_ROWS):
    sets = [set() for _ in down]
    for c in range(len(down)):
        r = c
        while r >= 0:
            sets[r].add(c)
            r = down[r]
    return sets


def _net(table_area_km2=None, edge=("I",), **kw):
    tab = brute(AREA_M2) / 1e6 if table_area_km2 is None else table_area_km2
    return Network.build(GID, DOWN_ROWS, AREA_M2, REGION_OF, REGIONS, tab, _col(VI, np.int32),
                         edge_gids=[GID[ROW[k]] for k in edge], **kw)


def _obs(rain, product="SYNTHETIC", hours=range(H0, H0 + 6), factors=None, buf=None):
    buf = buf or HourlyBuffer(N, "observed", REGIONS, REGION_OF)
    for h in hours:
        buf.put(h, rain(h), product, factors=factors)
    return buf


def _rain(h):
    rng = np.random.default_rng(h)
    return rng.gamma(0.8, 2.0, N).astype(np.float32)


# --- accumulation -------------------------------------------------------------------------------
def test_confluence_and_chain_match_brute_force_any_window():
    net = _net()
    assert set(net.order.tolist()) == set(range(N))
    pos = {r: i for i, r in enumerate(net.order)}
    assert all(pos[r] < pos[d] for r, d in enumerate(DOWN_ROWS) if d >= 0)   # upstream first
    assert np.allclose(net.up_area_m2, brute(AREA_M2))
    assert net.up_area_m2[ROW["E"]] == pytest.approx(16.5e6) and net.up_area_m2[ROW["C"]] == pytest.approx(5.5e6)
    buf = _obs(_rain)
    t = compute_totals(net, {"observed": buf}, windows=(6, 2, 1))       # 2 h: outside the defaults
    k = t.kind("observed")
    assert k.windows == (1, 2, 6)
    for x in (1, 2, 6):
        local_mm = buf.window(x).mm
        local_m3 = local_mm.astype(np.float64) / 1000 * AREA_M2
        want_m3 = brute(local_m3)
        assert np.allclose(t.m3("observed", x), want_m3, rtol=1e-6)
        assert np.allclose(t.mm("observed", x), want_m3 / brute(AREA_M2) * 1000, rtol=1e-6)
        assert (t.miss_hours("observed", x) == 0).all() and (t.miss_frac("observed", x) == 0).all()
        # a confluence: E = its tributaries D and F + its own local volume; a headwater is its own local
        m3 = t.m3("observed", x)
        assert m3[ROW["E"]] == pytest.approx(m3[ROW["D"]] + m3[ROW["F"]] + local_m3[ROW["E"]], rel=1e-6)
        assert m3[ROW["C"]] == pytest.approx(local_m3[[ROW["A"], ROW["B"], ROW["C"]]].sum(), rel=1e-6)
        assert m3[ROW["A"]] == pytest.approx(local_m3[ROW["A"]], rel=1e-6)
        assert t.mm("observed", x)[ROW["G"]] == pytest.approx(local_mm[ROW["G"]], rel=1e-6)
        c = k.info["windows_info"][str(x)]["checks"]
        assert c["mass_balance_max_rel_err"] < 1e-12 and c["outlets_vs_local_rel_err"] < 1e-12
    w2 = k.info["windows_info"]["2"]
    assert (w2["start"], w2["end"]) == (hour_time(H0 + 3).strftime("%Y-%m-%d %H:00Z"),
                                        hour_time(H0 + 5).strftime("%Y-%m-%d %H:00Z"))
    assert w2["products"] == {"SYNTHETIC": 2} and w2["hours_with_data"] == 2
    with pytest.raises(KeyError, match="was not computed"):
        t.mm("observed", 3)
    for bad in ((0,), (25,), (2.5,), ()):
        with pytest.raises(ValueError):
            compute_totals(net, {"observed": buf}, windows=bad)


def test_random_forest_sum_max_or_match_brute_force():
    rng = np.random.default_rng(7)
    m = 600
    parent = np.array([-1] + [int(rng.integers(0, t)) if rng.random() < 0.97 else -1 for t in range(1, m)])
    perm = rng.permutation(m)                       # logical node t lives in row perm[t]
    down = np.full(m, -1, dtype=np.int64)
    down[perm[parent >= 0]] = perm[parent[parent >= 0]]
    order = topo_order(down)
    vals = rng.random((3, m))
    want = np.stack([brute(v, down) for v in vals])
    got = accumulate(order, down, vals.copy())
    assert np.allclose(got, want)
    sets = upstream_sets(down)
    mx = rng.integers(0, 24, m).astype(np.uint8)
    assert np.array_equal(accumulate(order, down, mx.copy(), "max"), [mx[list(s)].max() for s in sets])
    bits = (1 << rng.integers(0, 8, m)).astype(np.uint8)
    want_or = [np.bitwise_or.reduce(bits[list(s)]) for s in sets]
    assert np.array_equal(accumulate(order, down, bits.copy(), "or"), want_or)


def test_cycles_and_bad_links_raise():
    with pytest.raises(ValueError, match="cycle: 2 rows"):
        topo_order(np.array([1, 0, -1]))
    with pytest.raises(ValueError, match="cycle"):
        topo_order(np.array([0]))                    # a reach draining into itself
    with pytest.raises(ValueError, match="row indices"):
        topo_order(np.array([5, -1, -1]))
    with pytest.raises(ValueError, match="C-contiguous"):
        accumulate(np.arange(3, dtype=np.int32), np.array([-1, 0, 0]), np.zeros((3, 2))[:, 0])


# --- missing data, products, scale factors -------------------------------------------------------
def test_missing_hours_propagate_downstream_only():
    net = _net()

    def rain(h):
        v = np.ones(N, dtype=np.float32)
        if h == H0 + 3:
            v[ROW["A"]] = np.nan                     # catchment A lacks one hour
        return v

    buf = _obs(rain, hours=[h for h in range(H0, H0 + 6) if h != H0 + 1])   # H0+1 missing everywhere
    t = compute_totals(net, {"observed": buf}, windows=(1, 3, 6))
    below_a = {"A", "C", "D", "E"}
    # 1 h (H0+5): complete
    assert (t.miss_hours("observed", 1) == 0).all()
    # 3 h (H0+3..H0+5): A's gap flags A and everything below it, nothing else
    mh3 = t.miss_hours("observed", 3)
    assert {k for k, r in ROW.items() if mh3[r]} == below_a and mh3.max() == 1
    mf3 = t.miss_frac("observed", 3)
    assert mf3[ROW["C"]] == pytest.approx(1.0 * 1 / (3 * 5.5))      # A's 1 km2 x 1 h of C's 5.5 km2 x 3 h
    assert mf3[ROW["E"]] == pytest.approx(1.0 / (3 * 16.5)) and mf3[ROW["G"]] == 0
    assert np.isnan(t.mm("observed", 3, strict=True)[ROW["E"]]) and not np.isnan(t.mm("observed", 3, True)[ROW["G"]])
    # depth over what was counted: C got 3 h on B and C, 2 h on A
    assert t.mm("observed", 3)[ROW["C"]] == pytest.approx((1.0 * 2 + 1.5 * 3 + 3.0 * 3) / 5.5, rel=1e-6)
    # 6 h: H0+1 is missing everywhere (1 h for all), A misses 2
    mh6 = t.miss_hours("observed", 6)
    assert all(mh6[ROW[k]] == (2 if k in below_a else 1) for k in ROW)
    mf6 = t.miss_frac("observed", 6)
    assert mf6[ROW["E"]] == pytest.approx((1.0 * 2 + 15.5 * 1) / (6 * 16.5))
    assert mf6[ROW["G"]] == pytest.approx(1 / 6)
    info = t.kind("observed").info["windows_info"]
    assert len(info["6"]["missing_hours"]) == 1 and info["6"]["reaches_incomplete"] == N
    assert info["3"]["reaches_incomplete"] == 4 and info["1"]["reaches_incomplete"] == 0


def test_products_and_scale_flags_propagate():
    net = _net()
    f = ScaleFactors("synthetic", REGIONS, [2.0, 1.0], [3, 0])         # R0 gauge-scaled (v3), R1 not
    buf = _obs(lambda h: np.ones(N, np.float32), hours=range(H0, H0 + 4))

    def better(h):
        v = np.ones(N, dtype=np.float32)
        v[ROW["B"]] = np.nan                         # B has no value in the better product's hours
        return v

    _obs(better, "SYNTHETIC_BETTER", hours=range(H0 + 4, H0 + 6), factors=f, buf=buf)
    t = compute_totals(net, {"observed": buf}, windows=(2, 6))
    k = t.kind("observed")
    assert k.product_tags == ("SYNTHETIC", "SYNTHETIC_BETTER")
    p2, p6 = k.products[0], k.products[1]
    assert p2[ROW["B"]] == 0 and p2[ROW["C"]] == 2 and p2[ROW["A"]] == 2           # BETTER only, B had none
    assert p6[ROW["B"]] == 1 and (p6[[r for n_, r in ROW.items() if n_ != "B"]] == 3).all()
    s2 = k.scale[0]
    assert s2[ROW["A"]] == SCALED and s2[ROW["B"]] == 0 and s2[ROW["G"]] == RAW and s2[ROW["F"]] == RAW
    assert s2[ROW["E"]] == RAW | SCALED                                            # R0 scaled + F's R1 raw
    assert k.scale[1][ROW["A"]] == RAW | SCALED                                    # 4 raw hours + 2 scaled
    # scaled depth: A gets 2 x 2 h in window 2; raw gives 1 x 2 h
    assert t.mm("observed", 2)[ROW["A"]] == pytest.approx(4.0)
    raw = compute_totals(net, {"observed": buf}, windows=(2,), scaled=False)
    assert raw.mm("observed", 2)[ROW["A"]] == pytest.approx(2.0) and (raw.kind("observed").scale[0] <= RAW).all()
    at = t.at(ROW["E"])["observed"][2]
    assert at["products"] == ["SYNTHETIC_BETTER"] and at["scale"] == "mixed"
    prov = t.provenance(ROW["E"], "observed", 2)
    assert [p["product"] for p in prov] == ["SYNTHETIC_BETTER"] * 2
    assert prov[0]["factor_version"] == {"R0": 3, "R1": 0} and prov[0]["factor"] == {"R0": 2.0, "R1": 1.0}
    assert t.provenance(ROW["G"], "observed", 6)[0]["factor_version"] == {"R1": 0}
    assert k.info["windows_info"]["6"]["products"] == {"SYNTHETIC": 4, "SYNTHETIC_BETTER": 2}
    assert k.info["windows_info"]["6"]["factor_versions"] == [0, 3]


def test_forecast_and_observed_together_any_windows():
    net = _net()
    obs = _obs(_rain)
    fc = HourlyBuffer(N, "forecast", REGIONS, REGION_OF)
    fc.advance(H0 + 5)
    for h in range(H0 + 6, H0 + 30):
        fc.put(h, _rain(h) * 0.5, "SYNTHETIC_FC", run_hour=H0, lead=h - H0)
    t = compute_totals(net, {"observed": obs, "forecast": fc}, windows=(2, 5, 24), now_hour=H0 + 7)
    for x in (2, 5, 24):
        want = brute(fc.ahead(x).mm.astype(np.float64) / 1000 * AREA_M2)
        assert np.allclose(t.m3("forecast", x), want, rtol=1e-6)
    assert (t.miss_hours("forecast", 24) == 0).all()
    assert t.miss_hours("observed", 24).min() == 18                    # only 6 observed hours held
    fi = t.kind("forecast").info
    assert fi["run_age_hours_at_start"] == 5 and fi["hours_since_start"] == 2
    assert fi["windows_info"]["5"]["leads"] == [6, 10]
    assert t.kind("observed").info["hours_behind_now"] == 2
    # windows ending at another hour (here: "now", 2 h after the latest observed hour) count the gap as missing
    late = compute_totals(net, {"observed": obs}, windows=(3,), anchor={"observed": H0 + 7})
    assert (late.miss_hours("observed", 3) == 2).all()
    empty = HourlyBuffer(N, "forecast", REGIONS, REGION_OF)            # never anchored: skipped
    assert set(compute_totals(net, {"observed": obs, "forecast": empty}, windows=(1,)).kinds) == {"observed"}
    with pytest.raises(ValueError, match="rows do not match"):
        compute_totals(net, {"observed": HourlyBuffer(N - 1, "observed")})
    with pytest.raises(ValueError, match="is a forecast buffer"):
        compute_totals(net, {"observed": fc})


# --- network flags ------------------------------------------------------------------------------
def test_network_flags_and_area_check():
    tab = brute(AREA_M2) / 1e6
    tab[ROW["E"]] = 20.0                 # 3.5 km2 (18 %) more than its pixels: area_short
    tab[ROW["D"]] += 0.1                 # 0.1 km2 more: within tolerance
    tab[ROW["I"]] = 250.0                # a big river cut at a tile edge
    net = _net(tab)
    fl = {k: int(net.flags[r]) for k, r in ROW.items()}
    assert fl["E"] == OUTLET | AREA_SHORT and fl["G"] == OUTLET and fl["D"] == 0
    assert fl["I"] & (OUTLET | EDGE | EDGE_BIG | ABOVE_EDGE) == OUTLET | EDGE | EDGE_BIG | ABOVE_EDGE
    assert fl["H"] & ABOVE_EDGE and not fl["H"] & EDGE and fl["A"] == 0
    assert net.flag_counts()["tile_edge_outlet"] == 1 and net.flag_counts()["drains_to_tile_edge"] == 2
    assert not _net(tab, edge_big_gids=[]).flags[ROW["I"]] & EDGE_BIG   # rf-t26.11's list is given explicitly
    assert int(net.up_regions[ROW["E"]]) == 0b11 and int(net.up_regions[ROW["G"]]) == 0b10
    c = net.area_check()
    assert c["edge_outlets"] == 1 and c["area_short_reaches"] == 2 and fl["I"] & AREA_SHORT   # E and I
    assert c["pixel_km2_total"] == pytest.approx(AREA_M2.sum() / 1e6) and c["table_km2_at_outlets"] > 260


# --- files and API ------------------------------------------------------------------------------
def test_save_load_and_api(tmp_path):
    net = _net()
    net2 = Network.load(net.save(tmp_path / "network.npz"))
    for k in Network._ARRAYS:
        assert np.array_equal(getattr(net, k), getattr(net2, k)), k
    obs = _obs(_rain)
    fc = HourlyBuffer(N, "forecast", REGIONS, REGION_OF)
    fc.advance(H0 + 5)
    for h in range(H0 + 6, H0 + 12):
        fc.put(h, _rain(h), "SYNTHETIC_FC", run_hour=H0, lead=h - H0)
    t = compute_totals(net2, {"observed": obs, "forecast": fc}, windows=(1, 6))
    p = t.save(tmp_path / "totals.npz")
    u = UpstreamTotals.load(p)
    meta = json.loads(p.with_suffix(".json").read_text())
    assert meta["windows"] == [1, 6] and set(meta["kinds"]) == {"observed", "forecast"} and meta["rows"] == N
    assert meta["kinds"]["forecast"]["product_tags"] == ["SYNTHETIC_FC"] and "created" in meta
    for kind in ("observed", "forecast"):
        a, b = t.kind(kind), u.kind(kind)
        for f in UpstreamTotals.PER_WINDOW:
            assert np.array_equal(getattr(a, f), getattr(b, f)), f
        for f in a.slots:
            assert np.array_equal(a.slots[f], b.slots[f]), f
        assert np.allclose(u.m3(kind, 6), t.m3(kind, 6))
    e = u.at(ROW["E"])
    assert e["gid"] == GID[ROW["E"]] and e["region"] == "R0" and e["vi"] == 1 and e["flags"] == ["outlet"]
    assert e["up_area_km2"] == pytest.approx(16.5)
    assert e["observed"][6]["mm"] == pytest.approx(float(t.mm("observed", 6)[ROW["E"]]))
    assert e["forecast"][1]["m3"] == pytest.approx(float(t.m3("forecast", 1)[ROW["E"]]), rel=1e-6)
    assert e["observed"][6]["products"] == ["SYNTHETIC"] and e["observed"][6]["scale"] == "raw"
    assert u.at(ROW["I"])["flags"] == ["outlet", "tile_edge_outlet", "drains_to_tile_edge"]
    df = u.for_gids([GID[ROW["C"]], GID[ROW["G"]]])
    assert list(df["gid"]) == [GID[ROW["C"]], GID[ROW["G"]]] and list(df["region"]) == ["R0", "R1"]
    assert {"obs_mm_1h", "obs_m3_6h", "fc_miss_hours_6h", "fc_miss_frac_1h", "obs_products_6h",
            "fc_scale_1h"} <= set(df.columns)
    assert df["obs_mm_6h"].iloc[0] == pytest.approx(float(t.mm("observed", 6)[ROW["C"]]))
    with pytest.raises(KeyError, match="unknown gid"):
        u.for_gids([GID[0], 5])
    assert list(u.rows_of([GID[ROW["H"]], -3])) == [ROW["H"], -1]
    vr = u.viewer_rows()
    assert list(vr["R0"]) == [ROW["D"], ROW["E"], ROW["C"]] and list(vr["R1"]) == [ROW["G"], ROW["I"]]
    assert list(u.vi[vr["R0"]]) == [0, 1, 2]                            # position j holds vi j
    assert list(u.viewer_rows("R1")) == ["R1"]
    sub = u.frame(vr["R1"], kinds=["forecast"], windows=[6], fields=("mm",))
    assert list(sub.columns) == ["gid", "region", "vi", "up_area_km2", "table_area_km2", "flags", "fc_mm_6h"]
    with pytest.raises(KeyError, match="no forecast"):
        compute_totals(net, {"observed": obs}, windows=(1,)).mm("forecast", 1)


# --- CLI step end to end ------------------------------------------------------------------------
TOY = GridSpec("toy", lon0=0.5, lat0=0.5, dlon=1.0, dlat=1.0, nx=2, ny=1)


def _write_inputs(root):
    px = 1e4
    npix = (AREA_M2 / px).astype(np.int64)
    counts = sp.csr_matrix((npix.astype(np.int32), (np.arange(N), np.arange(N) % 2)), shape=(N, 2))
    tab = brute(AREA_M2) / 1e6
    w = CatchmentWeights(TOY, counts, GID, npix, px, DOWN_ROWS, REGION_OF, REGIONS, tab.astype(np.float32),
                         (AREA_M2 / 1e6).astype(np.float32))
    (root / "rain").mkdir(parents=True)
    w.save(root / "rain" / "weights_ecmwf.npz")
    pd.DataFrame({"gid": GID, "down": DOWN_ROWS, "vi": _col(VI, np.int64), "area_km2": tab,
                  "region": [REGIONS[r] for r in REGION_OF]}).to_pickle(root / "segments_table.pkl")
    pd.DataFrame({"gid": [GID[ROW["I"]], GID[ROW["E"]]], "status": ["no reach on the REC2 path", "linked"]}).to_csv(
        root / "relink_log.csv", index=False)
    obs = HourlyBuffer.for_weights(w, "observed")
    for h in range(H0, H0 + 24):
        obs.put(h, w.mm(np.array([[1.0, 2.0]])), "ECMWF_OBS", run_hour=h - 3, lead=3)
    obs.save(root / "rain" / "obs_ecmwf.npz")
    fc = HourlyBuffer.for_weights(w, "forecast")
    fc.advance(H0 + 23)
    for h in range(H0 + 24, H0 + 47):                                   # 23 of 24 hours: the last is missing
        fc.put(h, w.mm(np.array([[0.5, 0.5]])), "ECMWF_FC", run_hour=H0 + 18, lead=h - H0 - 18)
    fc.save(root / "rain" / "fc_ecmwf.npz")
    return w


def test_run_totals_step_and_cli(tmp_path, capsys):
    _write_inputs(tmp_path)
    cfg = parse_config({"windows": [1, 6, 24], "observed": {"source": "ecmwf"}, "forecast": {"source": "ecmwf"},
                        "grids": {"ecmwf": {"lon0": 165.8, "lat0": -33.8, "dlon": 0.1, "dlat": -0.1, "nx": 133,
                                            "ny": 141}}, "paths": {"nz_data": str(tmp_path)}})
    logs = []
    now = T0 + timedelta(hours=25)
    t = run_totals(cfg, now=now, log=logs.append)
    assert cfg.totals_path.exists() and cfg.network_path.exists()
    assert any("building from segments_table.pkl (no network.npz)" in s for s in logs)
    m3 = t.m3("observed", 24)
    local = np.where(np.arange(N) % 2 == 0, 1.0, 2.0) * 24 / 1000 * AREA_M2
    assert np.allclose(m3, brute(local), rtol=1e-6)
    assert t.flags[ROW["I"]] & EDGE and not t.flags[ROW["E"]] & EDGE
    assert (t.miss_hours("forecast", 24) == 1).all() and (t.miss_hours("forecast", 6) == 0).all()
    meta = json.loads(cfg.totals_path.with_suffix(".json").read_text())
    assert meta["kinds"]["observed"]["family"] == "ecmwf" and meta["kinds"]["observed"]["hours_behind_now"] == 2
    assert meta["kinds"]["forecast"]["windows_info"]["24"]["missing_hours"]
    assert meta["network"]["flags"]["tile_edge_outlet"] == 1 and "seconds" in meta
    logs.clear()
    run_totals(cfg, windows=[2], now=now, log=logs.append)            # the saved network is reused
    assert any(s.startswith("network: ") and "built" in s for s in logs)
    assert UpstreamTotals.load(cfg.totals_path).windows == (2,)
    st = cfg.segments_path.stat()
    os.utime(cfg.segments_path, (st.st_atime, st.st_mtime + 10))        # the table changed: rebuilt
    logs.clear()
    run_totals(cfg, now=now, log=logs.append)
    assert any("changed since network.npz" in s for s in logs)
    lines = []
    status(cfg, log=lines.append)
    assert any(s.startswith("upstream totals") and "windows [1, 6, 24]" in s for s in lines)
    cli.main(["rain", "totals", "--data", str(tmp_path), "--windows", "3", "7"])
    assert "totals ->" in capsys.readouterr().out
    assert UpstreamTotals.load(cfg.totals_path).windows == (3, 7)
