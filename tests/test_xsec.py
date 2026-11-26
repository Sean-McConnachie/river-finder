"""Cross-sections (xsec.py) on synthetic DEMs: V gorge, U valley, wide flat floor, a pinch point, an off-axis line,
a diagonal channel, the Manning flood stage and the ML feature columns."""

import math

import numpy as np
import pandas as pd
import pytest
from rasterio.transform import from_origin
from shapely.geometry import LineString

from river_finder import ml
from river_finder import xsec as XS

CS = 8.0
NC, NR = 301, 121
X0, Y0 = 1_000_000.0, 5_000_000.0          # top-left corner
TR = from_origin(X0, Y0, CS, CS)
XC = X0 + (NC // 2 + 0.5) * CS              # x of the centre column's cell centre
COLX = X0 + (np.arange(NC) + 0.5) * CS
ROWY = Y0 - (np.arange(NR) + 0.5) * CS


def trapezoid(floor_m: float, slope: float, rows: int = NR) -> np.ndarray:
    """Valley along y: flat floor ``floor_m`` wide centred on XC, walls rising ``slope`` m per m."""
    d = np.maximum(np.abs(COLX - XC) - floor_m / 2.0, 0.0)
    return np.tile(slope * d, (rows, 1)).astype(np.float32)


def line(x: float = XC, top_row: int = 20, bottom_row: int = 70) -> LineString:
    return LineString([(x, ROWY[top_row]), (x, ROWY[bottom_row])])


def one_reach(z, geom=None, **kw) -> pd.Series:
    sec = XS.extract(z, TR, [geom or line()], **kw)
    return XS.reach_table(sec, 1, res_m=CS).iloc[0]


def test_stations_spacing_and_normals():
    ri, s, x, y, nx, ny = XS.stations([line(), LineString([(XC, ROWY[10]), (XC, ROWY[12])])])
    assert list(np.bincount(ri)) == [8, 1]                       # 400 m -> 8 sections, 16 m -> one at the middle
    assert np.allclose(s[:8], np.arange(25.0, 400.0, 50.0))
    assert s[8] == pytest.approx(8.0)
    assert np.allclose(np.abs(nx), 1.0) and np.allclose(ny, 0.0)  # perpendicular to a north-south line


def test_v_gorge_widths_areas_banks():
    m = 0.5                                     # walls rise 1 m per 2 m: w(h) = 4 h, A(h) = 2 h^2
    r = one_reach(trapezoid(0.0, m))
    for h in XS.STAGES:
        tg = XS.stage_tag(h)
        assert r[f"xs_w{tg}_med"] == pytest.approx(2 * h / m, rel=1e-3)
        assert r[f"xs_a{tg}_med"] == pytest.approx(h * h / m, rel=1e-3)
        assert r[f"xs_p{tg}_med"] == pytest.approx(2 * h * math.sqrt(1 + 1 / m ** 2), rel=1e-3)
        assert r[f"xs_cens{tg}"] == 0
    assert r["xs_n"] == 8
    assert r["xs_bank_lo_med"] == pytest.approx(m * XS.BANK_M, abs=m * CS)       # within 50 m (half-cell samples)
    assert r["xs_gorge_lo_med"] == pytest.approx(m * XS.GORGE_M, abs=m * CS)
    assert r["xs_flat_frac"] == 0


def test_u_valley_floor_and_walls():
    B, m = 48.0, 2.0                            # floor 48 m, steep walls: w = B + 2h/m, A = B h + h^2/m
    r = one_reach(trapezoid(B, m))
    for h in (2.0, 5.0, 10.0, 20.0):
        tg = XS.stage_tag(h)
        assert r[f"xs_w{tg}_med"] == pytest.approx(B + 2 * h / m, rel=1e-3)
        assert r[f"xs_a{tg}_med"] == pytest.approx(B * h + h * h / m, rel=1e-3)
    # shape factor A / (w h): U near 1, V exactly 1/2
    v = one_reach(trapezoid(0.0, 0.5))
    assert r["xs_a10_med"] / (r["xs_w10_med"] * 10) > 0.85
    assert v["xs_a10_med"] / (v["xs_w10_med"] * 10) == pytest.approx(0.5, rel=1e-3)


def test_wide_flat_floor_is_censored():
    r = one_reach(trapezoid(2000.0, 1.0))
    assert r["xs_w10_med"] == pytest.approx(2 * XS.HALF_WIDTH_M)
    assert r["xs_a10_med"] == pytest.approx(2 * XS.HALF_WIDTH_M * 10, rel=1e-6)
    assert r["xs_cens10"] == 1 and r["xs_flat_frac"] == 1
    # no-data (sea) counts as flat ground at the channel bottom: also censored
    z = trapezoid(0.0, 0.5)
    z[:, NC // 2 + 3:] = np.nan
    s = one_reach(z)
    assert s["xs_cens10"] == 1 and s["xs_w10_med"] == pytest.approx(XS.HALF_WIDTH_M + 20.0, rel=1e-3)
    assert s["xs_nodata"] > 0.4


def test_off_axis_line_snaps_to_the_bottom():
    z = trapezoid(0.0, 0.5)
    sec = XS.extract(z, TR, [line(XC + CS)])
    assert np.allclose(sec["offset"], -CS)
    r = XS.reach_table(sec, 1).iloc[0]
    assert r["xs_w10_med"] == pytest.approx(40.0, rel=1e-3)


def test_diagonal_channel():
    # V along the line x - XC = y - YC, walls rising 0.5 m per m of perpendicular distance
    YC = ROWY[NR // 2]
    xx, yy = np.meshgrid(COLX, ROWY)
    z = (0.5 * np.abs((xx - XC) - (yy - YC)) / math.sqrt(2)).astype(np.float32)
    g = LineString([(XC - 150, YC - 150), (XC + 150, YC + 150)])
    r = one_reach(z, g)
    assert r["xs_w10_med"] == pytest.approx(40.0, rel=0.03)
    assert r["xs_w20_med"] == pytest.approx(80.0, rel=0.03)


def test_pinch_point_and_constriction_index():
    # three reaches down one valley (rows 0-120): floor 160 m, except a 40 m band at the middle reach's 4th section
    z = trapezoid(160.0, 1.0)
    pinch = trapezoid(16.0, 1.0)
    ys = [ROWY[0] - 4, ROWY[40] - 4, ROWY[80] - 4, ROWY[120] + 4]
    geoms = [LineString([(XC, ys[k]), (XC, ys[k + 1])]) for k in range(3)]
    st_y = ys[1] - 175.0                        # middle reach, station at 175 m
    band = np.abs(ROWY - st_y) <= 20.0
    z[band] = pinch[band]
    sec = XS.extract(z, TR, geoms)
    rt = XS.reach_table(sec, 3, res_m=CS)
    assert list(rt["xs_n"]) == [6, 6, 6]
    assert rt.loc[1, "xs_w10_min"] == pytest.approx(16.0 + 20.0, rel=1e-3)
    assert rt.loc[1, "xs_w10_med"] == pytest.approx(180.0, rel=1e-3)
    f = rt.assign(down=[1, 2, -1], up=[-1, 0, 1], maf_m3s=50.0, grad_s=20.0)
    XS.derive(f)
    assert f.loc[1, "xs_pinch10_ratio"] == pytest.approx(36.0 / 180.0, rel=1e-3)
    assert f.loc[0, "xs_pinch10_ratio"] == pytest.approx(1.0, rel=1e-3)
    assert f.loc[2, "xs_omega10_wm2"] == pytest.approx(1000 * 9.81 * 50.0 * 0.02 / 180.0, rel=1e-3)
    assert (ml.column(f, "xs_pinch") < 0)[1] and abs(ml.column(f, "xs_pinch")[0]) < 1e-6


def test_manning_flood_stage():
    B, m, Q, S, n = 32.0, 1.0, 200.0, 0.01, XS.MANNING_N   # floor edges on cell centres: the DEM is exact
    r = one_reach(trapezoid(B, m)).to_frame().T.assign(maf_m3s=Q, grad_s=S * 1000)

    def q(h):
        A = B * h + h * h / m
        P = B + 2 * h * math.sqrt(1 + 1 / m ** 2)
        return A * (A / P) ** (2 / 3) * math.sqrt(S) / n

    lo, hi = 0.01, 50.0                         # exact stage by bisection
    for _ in range(80):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if q(mid) < Q else (lo, mid)
    h = XS.hydraulics(r).iloc[0]
    assert h["xs_h_q"] == pytest.approx(lo, rel=0.05)
    assert h["xs_v_q"] == pytest.approx(Q / (B * lo + lo * lo / m), rel=0.05)
    assert h["xs_omega_q"] == pytest.approx(1000 * 9.81 * Q * S / (B + 2 * lo / m), rel=0.05)


def test_reach_without_valid_section():
    z = np.full((NR, NC), np.nan, np.float32)
    rt = XS.reach_table(XS.extract(z, TR, [line()]), 1)
    assert rt.loc[0, "xs_n"] == 0 and np.isnan(rt.loc[0, "xs_w10_med"])
    f = rt.assign(down=-1, up=-1, maf_m3s=10.0, grad_s=5.0)
    XS.derive(f)
    assert ml.column(f, "xs_w10")[0] == pytest.approx(3.0)      # a reach with no section counts as wide open
    assert ml.column(f, "xs_pinch")[0] == 0.0


def test_attach_and_registry():
    xs = pd.DataFrame({"gid": [7, 9], "xs_n": [3, 2], "xs_w10_med": [50.0, 400.0]})
    f = pd.DataFrame({"gid": [9, 7, 8]})
    XS.attach(f, xs, "gid")
    assert list(f["xs_n"]) == [2, 3, 0] and np.isnan(f.loc[2, "xs_w10_med"])
    spec = ml.spec_by_id("gbm_xsec")
    assert spec.features == ml.BASE + ml.TERRAIN + ml.XSEC
    assert ml.spec_by_id("gbm_terrain").features == ml.BASE + ml.TERRAIN     # existing recipes unchanged
    assert not ml.available(pd.DataFrame({"x": [1]}), spec) and ml.available(f, spec)
    assert ml.available(pd.DataFrame({"x": [1]}), ml.spec_by_id("gbm_terrain"))
    for name in ml.XSEC_ALL:
        assert name in ml.EXTRA and ml.EXTRA[name][2] == "xsec" and ml.describe(name)
