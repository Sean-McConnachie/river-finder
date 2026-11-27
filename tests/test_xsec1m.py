"""1 m cross-sections (xsec1m.py): shape exponent and form factor of V, parabola and box sections, the bankfull break
and entrenchment, banks, re-centring on an offset channel, the ambiguity flags, the stored int16 profile, the long
profile, the corridor reads and the flood hydraulics."""

import math

import numpy as np
import pandas as pd
import pytest
from rasterio.transform import from_origin
from shapely.geometry import LineString

from river_finder import ml
from river_finder import xsec as XS
from river_finder import xsec1m as X
from river_finder import xsec1mstudy as XST

U = X.U
A = np.abs(U)


def one(P):
    return {k: (v[0] if np.ndim(v) else v) for k, v in X.measure(P[None, :]).items()}


def test_grid():
    assert len(U) == 651 and U[X.CI] == 0 and U[0] == -500 and U[-1] == 500
    assert np.all(np.diff(U[X.CI - 150:X.CI + 151]) == 1) and np.all(np.diff(U[:X.N_OUT]) == 2)
    assert U[X.CI + 151] == 152


@pytest.mark.parametrize("name,P,b,m,ff", [
    ("V", 0.5 * A, 1.0, 2.0, 0.5),                        # w = 4 h, A = 2 h^2
    ("parabola", 0.01 * U ** 2, 0.5, 1.5, 2 / 3),         # w = 20 sqrt(h)
    ("box", np.where(A <= 10.5, 0.0, 100.0), 0.0, 1.0, 1.0),
])
def test_shape_exponent_and_form_factor(name, P, b, m, ff):
    r = one(P)
    assert r["b"] == pytest.approx(b, abs=0.01)
    assert r["m"] == pytest.approx(m, abs=0.01)
    assert r["ff1"] == pytest.approx(ff, abs=0.01)


def test_widths_areas_perimeters_of_a_v():
    r = one(0.5 * A)
    for j, h in enumerate(X.STAGES):
        assert r["w"][j] == pytest.approx(4 * h, rel=1e-6)
        assert r["a"][j] == pytest.approx(2 * h * h, rel=1e-6)
        assert r["p"][j] == pytest.approx(2 * h * math.sqrt(5), rel=1e-6)
        assert r["cens"][j] == 0
    assert r["wall150_l"] == pytest.approx(75) and r["bank50_r"] == pytest.approx(25)
    assert r["ang_l"] == pytest.approx(math.degrees(math.atan(0.5)), abs=0.1) and r["steep_l"] == 0


def test_bankfull_break_and_entrenchment():
    # 10 m water surface, 1:1 banks 1.5 m high, a flat floodplain to 100 m, valley walls 1:2
    fp = np.clip(A - 5, 0, 1.5) + np.clip(A - 100, 0, None) * 0.5
    r = one(fp)
    assert r["brk"] == 1 and r["hb"] == pytest.approx(1.45, abs=0.06)
    assert r["wbf"] == pytest.approx(10 + 2 * 1.4, abs=1.0)
    assert r["er"] > 10                                   # the floodplain: not entrenched
    # a gorge (vertical walls) and a V have no break; their entrenchment ratio is w(10) / w(5)
    for P, er in ((np.where(A <= 10.5, 0.0, 100.0), 1.0), (0.5 * A, 2.0)):
        g = one(P)
        assert g["brk"] == 0 and np.isnan(g["hb"]) and g["hb_use"] == 5.0
        assert g["er"] == pytest.approx(er, rel=0.02)


def test_banks_steep_share_and_asymmetry():
    box = one(np.where(A <= 10.5, 0.0, 100.0))
    assert box["steep_l"] == 1 and box["steep_r"] == 1 and box["ang_l"] > 85
    # left bank vertical, right bank 1:3
    P = np.where(U < -5.5, 50.0, np.where(U > 5, (U - 5) / 3.0, 0.0))
    r = one(P)
    assert r["steep_l"] == 1 and r["steep_r"] == 0
    assert r["asym_ang"] > 60 and r["asym_w"] == pytest.approx(5.96 / 16.04, abs=0.01)  # 5.04 m vs 11 m at 2 m


def test_no_data_counts_as_water_and_censoring():
    P = np.full(len(U), np.nan, np.float32)
    P[X.CI] = 0.0
    r = one(P)
    assert r["w"][0] == pytest.approx(1000) and r["cens"][0] == 3 and r["nodata"] == pytest.approx(1.0)
    bad = X.measure(np.full((1, len(U)), np.nan))
    assert np.isnan(bad["w"]).all() and np.isnan(bad["b"]).all()


def test_int16_profile_round_trip_keeps_the_measures():
    rng = np.random.default_rng(1)
    P = (0.3 * A + rng.normal(0, 0.05, len(U)) + np.clip(A - 40, 0, None) * 0.7).astype(np.float32)
    P -= P[X.CI]
    P[200:210] = np.nan
    q = X.quantise(P[None, :])
    assert q.dtype == np.int16 and (q[0, 200:210] == X.NODATA_I16).all()
    a, b = X.measure(P[None, :]), X.measure(X.dequantise(q))
    assert np.allclose(a["w"], b["w"], atol=0.05) and np.allclose(a["a"], b["a"], rtol=0.01, atol=0.05)
    for k in ("b", "ff1", "hb_use", "er", "ang_l", "steep_r"):
        assert a[k][0] == pytest.approx(b[k][0], abs=0.02), k
    # the differences outward from the channel (and the no-data bits) decode exactly, also next to no data
    Q = np.vstack([q, X.quantise(np.full((1, len(U)), np.nan)), X.quantise((0.5 * A)[None, :])])
    Q[2, :3] = X.NODATA_I16
    d, mask = X.encode(Q)
    assert d.shape == (3, len(U) - 1) and d.dtype == np.int16 and mask.dtype == np.uint8
    assert np.array_equal(X.decode(d, mask), Q)
    assert np.abs(d[2]).max() == 100                      # 0.5 m per 1 m sample, 1 m per 2 m sample (cm)


# ---------------------------------------------------------------------------------------------- on a 1 m DEM
W, H = 1400, 400
X0, Y1 = 1_000_000.0, 5_000_400.0
TF1 = from_origin(X0, Y1, 1, 1)
COLX = X0 + np.arange(W) + 0.5


def dem(z_of_x) -> tuple[np.ndarray, np.ndarray]:
    z = np.tile(z_of_x(COLX).astype(np.float32), (H, 1))
    z += np.linspace(2.0, 0.0, H, dtype=np.float32)[:, None]        # 5 m/km downstream (southwards)
    return z, X.block_mean2(z)


def run(z1, z2, xline, search=X.SEARCH_M):
    g = [LineString([(xline, Y1 - 50), (xline, Y1 - 350)])]
    ri, s, x, y, nx, ny = XS.stations(g, X.SPACING_M, X.TANGENT_M)
    sec = X.extract(z1, TF1, z2, from_origin(X0, Y1, 2, 2), ri, s, x, y, nx, ny, search=search)
    return sec, X.measure(sec["prof"])


def test_recentres_on_an_offset_channel():
    xc = X0 + 700.5
    z1, z2 = dem(lambda x: 0.5 * np.abs(x - xc))
    sec, m = run(z1, z2, xc - 30)                        # the routed line 30 m off the channel
    assert (sec["flags"] & X.DROP == 0).all()
    assert np.allclose(np.abs(sec["off_line"]), 30, atol=1)
    assert np.allclose(sec["cx"], xc, atol=1.0)
    assert np.allclose(m["w"][:, X.STAGES.index(2.0)], 8.0, atol=0.6)
    assert np.allclose(m["b"], 1.0, atol=0.05)


def test_twin_and_edge_flags():
    a, b = X0 + 660.5, X0 + 740.5                         # two channels at one level, a 3 m ridge between
    z1, z2 = dem(lambda x: np.minimum(np.minimum(np.abs(x - a), np.abs(x - b)) * 0.5, 3.0))
    sec, _ = run(z1, z2, X0 + 700.5)
    assert ((sec["flags"] & X.FLAG_TWIN) != 0).all()
    xc = X0 + 700.5                                      # the channel 130 m away: beyond the window
    z1, z2 = dem(lambda x: 0.2 * np.abs(x - xc))
    sec, _ = run(z1, z2, xc - 130)
    assert ((sec["flags"] & X.FLAG_EDGE) != 0).all()


def test_low_flag_and_long_profile():
    reach = np.zeros(8, int)
    s = np.arange(8) * 25.0
    z0 = 100 - 0.01 * s
    z0[5] -= 6.0                                          # one section found a lower channel
    low = X.low_flags(reach, s, z0, np.ones(8, bool))
    assert low.tolist() == [False] * 5 + [True] + [False] * 2
    sd = np.arange(0, 250.0)
    r, conc, grad, L = X.profile_metrics(sd, 100 - 0.02 * sd)
    assert r == pytest.approx(0, abs=1e-9) and conc == pytest.approx(0.1, abs=0.01) and grad == pytest.approx(20)
    step = np.where(sd < 125, 100.0, 95.0) - 0.001 * sd
    r, conc, grad, L = X.profile_metrics(sd, step)
    assert conc > 0.9 and r > 1


def test_corridor_jobs_read_only_blocks_near_the_points():
    sheets = [{"href": "s", "bounds": (0, 0, 4096, 4096)}]
    jobs = X.corridor_jobs(sheets, (0, 0, 4096, 4096), np.array([256.0]), np.array([4096 - 256.0]), 100.0)
    assert jobs == [("s", 0, 0, 512, 512, 0, 0)]          # one 512 block, top left
    jobs = X.corridor_jobs(sheets, (0, 0, 4096, 4096), np.array([1024.0]), np.array([2048.0]), 600.0)
    cells = sum(j[3] * j[4] for j in jobs)
    assert 4 * 512 ** 2 <= cells <= 16 * 512 ** 2
    for _, c, r, w, h, br, bc in jobs:                    # box offsets = sheet offsets here
        assert (br, bc) == (r, c) and w % 512 == 0 and h % 512 == 0


def test_reach_table_and_spacing():
    n = 6
    cols = {c: np.full(n, 0.5) for c in X.SCALARS}
    for h in X.STAGES:
        tg = X.stage_tag(h)
        for k in ("w", "a", "p", "dl"):
            cols[f"{k}{tg}"] = np.ones(n)
        cols[f"c{tg}"] = np.zeros(n, np.int8)
    for k in ("off_line", "wbf", "abf", "pbf", "wfp", "er", "ffbf", "wd_bf", "rbf", "ff1", "w0", "asym_w", "asym_ang"):
        cols[k] = np.ones(n)
    cols.update(reach=np.zeros(n, int), s=12.5 + 25 * np.arange(n), flags=np.zeros(n, np.uint8),
                w1=np.array([10, 10, 10, 4, 10, 10.0]), hb=np.ones(n), hb_use=np.ones(n), brk=np.ones(n))
    sec = pd.DataFrame(cols)
    r = X.reach_table(sec, 2)
    assert r.loc[0, "x1_n"] == 6 and r.loc[1, "x1_n"] == 0 and np.isnan(r.loc[1, "x1_w1_med"])
    assert r.loc[0, "x1_pinch_frac"] == pytest.approx(1 / 6) and r.loc[0, "x1_w1_min"] == 4
    assert X.reach_table(sec, 2, every=2).loc[0, "x1_n"] == 3


def test_hydraulics_measured_and_estimated():
    # a 20 m box channel: A = 20 h, P = 20 + 2 h (from the water surface)
    t = pd.DataFrame({"maf_m3s": [50.0], "grad_s": [10.0], "x1_w0_med": [20.0]})
    for h in X.STAGES:
        tg = X.stage_tag(h)
        t[f"x1_a{tg}_med"] = 20 * h
        t[f"x1_p{tg}_med"] = 20 + 2 * h
        t[f"x1_w{tg}_med"] = 20.0
    h = X.hydraulics(t)
    hq = h["xs_h_q"].iloc[0]
    q = 20 * hq * (20 * hq / (20 + 2 * hq)) ** (2 / 3) * math.sqrt(0.01) / XS.MANNING_N
    assert q == pytest.approx(50, rel=0.05)
    e = X.hydraulics(t, estimate=True)
    assert e["xs_h_q"].iloc[0] < hq and e["xs_d_u"].iloc[0] > 0
    X.derive(t)
    assert {"x1_v_q", "x1_omega_q", "x1_v_q_est", "x1_omega_q_est"} <= set(t.columns)


def test_feature_registry_is_temporary():
    before = dict(ml.EXTRA)
    with XST.registered():
        assert all(k in ml.EXTRA for k in XST.FEATURES1M)
    assert ml.EXTRA == before
    assert set(XST.GROUPS) >= {"hyd", "shape", "bankfull", "entrench", "banks", "var", "rough", "valley"}
