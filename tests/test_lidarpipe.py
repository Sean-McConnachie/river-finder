"""1 m LiDAR pipeline pieces (lidarpipe.py): the lean router gives the receivers of routing.route, block
aggregation, water flags, window jobs, inflows, channel segments, lake fill, seam correction, burning and profile
metrics, on synthetic grids (no network)."""

import numpy as np
import pytest
from rasterio.transform import from_origin

from river_finder import lidarpipe as P
from river_finder.routing import route


def _terrain(seed, nr=60, nc=70, flats=True, holes=True):
    rng = np.random.default_rng(seed)
    r, c = np.mgrid[0:nr, 0:nc]
    z = 0.4 * r + 0.15 * np.abs(c - nc / 2) + rng.normal(0, 0.8, (nr, nc))
    z = np.round(z, 1)                               # ties and flats like a quantised DEM
    if flats:
        z[10:25, 20:45] = np.round(z[10:25, 20:45].min(), 1)   # a lake-like flat
        z[40:44, 5:60] = 30.0                                    # a dam across the valley (pit behind it)
    z = z.astype(np.float32)
    if holes:
        z[0:3, 0:8] = np.nan                         # sea in a corner
        z[30:33, 50:54] = np.nan                     # an inland hole
    return z


@pytest.mark.parametrize("seed", [0, 1, 2, 3])
@pytest.mark.parametrize("border", [False, True])
def test_route_lean_matches_routing_route(seed, border):
    z = _terrain(seed)
    valid = np.isfinite(z)
    seed_mask = None
    if border:
        seed_mask = np.zeros(z.shape, bool)
        seed_mask[0, 30:40] = True                   # only part of the border is an outlet
    ref = route(np.where(valid, z, 0.0).astype(np.float64), valid, 10.0, border_seed=seed_mask)
    zz = z.copy()
    d = P.route_lean(zz, 10.0, border_seed=seed_mask)
    assert np.array_equal(P.receivers(d), ref["rec"])
    assert np.allclose(np.where(valid, zz, 0), np.where(valid, ref["filled"], 0))
    acc, fl = P.accumulate_lean(d, 10.0)
    assert np.allclose(np.where(valid, acc, np.nan), ref["acc"], equal_nan=True)
    assert np.array_equal((fl & 1).astype(bool) & valid, ref["truncated"])


def test_route_lean_grows_heap_and_pit_queue():
    # a big flat with one outlet forces a long FIFO pit queue; random noise a big heap
    z = np.full((300, 300), 5.0, np.float32)
    z[0, 150] = 1.0
    seed = np.zeros(z.shape, bool)
    seed[0, 150] = True
    d = P.route_lean(z.copy(), 1.0, border_seed=seed)
    acc, _ = P.accumulate_lean(d, 1.0)
    assert acc[0, 150] == 300 * 300
    ref = route(z.astype(np.float64), np.ones(z.shape, bool), 1.0, border_seed=seed)
    assert np.array_equal(P.receivers(d), ref["rec"])
    rng = np.random.default_rng(5)
    z2 = rng.normal(0, 1, (400, 400)).astype(np.float32)
    ref = route(z2.astype(np.float64), np.ones(z2.shape, bool), 1.0)
    assert np.array_equal(P.receivers(P.route_lean(z2.copy(), 1.0)), ref["rec"])


def test_route_lean_rejects_float64():
    with pytest.raises(TypeError):
        P.route_lean(np.zeros((3, 3)), 1.0)


def test_agg_mean_min_count_and_shares():
    shape1 = (20, 30)
    z = np.arange(600, dtype=np.float32).reshape(shape1)
    z[0, 0] = np.nan
    a = P.Agg(shape1, 5)
    flags = np.zeros(shape1, np.uint8)
    flags[:5, :5] = 1
    flags[0, 0] = 0                          # water_flags gives 0 on nodata
    flags[5:10, :5] = 2
    # two windows that split coarse cells (the second starts mid-cell)
    a.add(z[:, :13], 0, 0, flags[:, :13])
    a.add(z[:, 13:], 0, 13, flags[:, 13:])
    m, mn = a.mean(), a.minimum()
    blocks = z.reshape(4, 5, 6, 5)
    assert np.allclose(m, np.nanmean(blocks, axis=(1, 3)))
    assert np.allclose(mn, np.nanmin(blocks, axis=(1, 3)))
    assert a.cnt[0, 0] == 24
    sh = a.share("flat")
    assert sh[0, 0] == pytest.approx(24 / 24) and sh[1, 0] == 0
    assert a.share("planar")[1, 0] == 1.0
    a2 = P.Agg((10, 10), 5, water=False)
    a2.add(np.full((10, 10), np.nan, np.float32), 0, 0)
    assert np.isnan(a2.mean()).all()
    with pytest.raises(ValueError):
        P.Agg((11, 10), 5)


def test_water_flags_flat_and_planar():
    z = np.zeros((10, 10), np.float32)
    z[:, 5:] = np.arange(5, dtype=np.float32)[None, :] * 0.5      # a plane (planar, not flat)
    z[2, 2] = 1.0                                                   # a bump in the flat part
    f = P.water_flags(z)
    assert f[5, 1] == 1 and f[5, 7] == 2
    assert f[2, 2] == 0 and f[1, 1] == 0                            # the bump and its neighbours
    assert (f[0, :] == 0).all()                                     # border


def test_sheet_windows_cover_box_once():
    sheets = [{"href": "a", "bounds": (0, 0, 3000, 4000)}, {"href": "b", "bounds": (3000, 0, 6000, 4000)}]
    box = (1000, 500, 4500, 3500)
    jobs = P.sheet_windows(sheets, box, win=1024)
    cover = np.zeros((3000, 3500), np.int32)
    for _href, _c, _r, w, h, br, bc in jobs:
        cover[br:br + h, bc:bc + w] += 1
    assert (cover == 1).all()
    # windows start on the sheet's 1024 block grid unless clipped by the box
    for href, c, *_ in jobs:
        assert c % 1024 == 0 or (href == "a" and c == 1000) or (href == "b" and c == 0)


def test_inject_adds_area_downstream_and_clears_truncation():
    nr, nc = 20, 5
    r, c = np.mgrid[0:nr, 0:nc]
    z = (r * 1.0 + np.abs(c - 2) * 3.0).astype(np.float32)          # valley draining to row 0
    d = P.route_lean(z, 10.0)
    acc, fl = P.accumulate_lean(d, 10.0)
    before = acc[0, 2]
    tf = from_origin(0, nr * 10, 10, 10)
    used = P.inject(d, acc, fl, tf, [(25.0, 185.0, 3.0, 7)], radius_cells=1)
    assert used and used[0]["id"] == 7
    assert acc[0, 2] == pytest.approx(before + 3e6)
    assert fl[0, 2] & 2


def test_segments_cut_links_and_link_downstream():
    nr, nc = 200, 41
    r, c = np.mgrid[0:nr, 0:nc]
    z = (r * 0.5 + np.abs(c - 20) * 2.0).astype(np.float32)
    z[:, 20] -= 0.01 * (c[:, 20] == 20)
    d = P.route_lean(z, 5.0)
    acc, fl = P.accumulate_lean(d, 5.0)
    g = P.segments(d, acc, z, from_origin(0, nr * 5, 5, 5), min_area_km2=0.01, seg_len_m=250, trunc=fl)
    assert len(g) >= 3
    main = g.sort_values("area_km2").iloc[-1]
    assert main["area_km2"] == pytest.approx(acc.max() / 1e6, rel=0.05)
    assert (g["gradient_m_per_km"] > 0).all()
    down = g["down_seg"].to_numpy()
    assert (down[down >= 0] < len(g)).all()


def test_local_areas_sum_to_the_outlet_area():
    nr, nc = 120, 61
    r, c = np.mgrid[0:nr, 0:nc]
    z = (r * 0.5 + np.abs(c - 30) * 2.0 + 0.3 * np.sin(c / 3.0)).astype(np.float32)
    d = P.route_lean(z, 5.0)
    acc, fl = P.accumulate_lean(d, 5.0)
    g = P.segments(d, acc, z, from_origin(0, nr * 5, 5, 5), min_area_km2=0.005, seg_len_m=100, trunc=fl)
    loc, none = P.local_areas(d, g, 5.0)
    assert loc.sum() + none == pytest.approx(nr * nc * 25 / 1e6)
    # network accumulation of local areas reproduces each segment's area at its bottom (within one cell each)
    down = g["down_seg"].to_numpy()
    tot = loc.copy()
    order = np.argsort(-g["z_top"].to_numpy())
    for i in order:
        if down[i] >= 0:
            tot[down[i]] += tot[i]
    big = g["area_km2"].to_numpy() > 0.05
    assert np.allclose(tot[big], g["area_km2"].to_numpy()[big], rtol=0.1)


def test_fill_lakes_fills_flat_rim_holes_not_the_sea():
    z = np.fromfunction(lambda r, c: 100 + 0.5 * r + 0.3 * c, (60, 60)).astype(np.float32)
    z[20:40, 20:40] = 105.0                  # flattened lake surface ...
    z[24:36, 24:36] = np.nan                 # ... with a no-data interior
    z[:, :3] = np.nan                        # the sea along one edge (sloping rim)
    flat = np.zeros(z.shape, np.float32)
    flat[20:40, 20:40] = 1.0
    recs = P.fill_lakes(z, flat)
    assert np.all(z[24:36, 24:36] == 105.0)
    assert np.isnan(z[:, :3]).all()
    assert sum(r["filled"] for r in recs) == 1
    # a sea whose coastal water the survey flattened: only the land mask (the 8 m DEM has no sea) tells it apart
    z2 = np.fromfunction(lambda r, c: 100 + 0.5 * r + 0.3 * c, (60, 60)).astype(np.float32)
    z2[:, :10] = -1.4
    z2[:, :6] = np.nan
    fl2 = np.zeros(z2.shape, np.float32)
    fl2[:, :10] = 1.0
    land = np.ones(z2.shape, bool)
    land[:, :6] = False
    assert P.fill_lakes(z2.copy(), fl2)[0]["filled"]                 # without the mask the sea looks like a lake
    recs = P.fill_lakes(z2, fl2, land=land)
    assert not recs[0]["filled"] and np.isnan(z2[:, :6]).all()


def test_seam_correct_is_continuous_and_keeps_lidar():
    nr, nc = 200, 200
    r, c = np.mgrid[0:nr, 0:nc]
    truth = (500 + 0.8 * r + 0.2 * c).astype(np.float32)
    z8 = truth + 4.0                         # the fallback is 4 m too high
    fine = truth.copy()
    fine[:, 120:] = np.nan                   # gap on the right
    merged, st = P.seam_correct(fine, z8, cell=5.0, band_m=100, sigma_m=50, taper_m=400, work_m=5.0)
    assert np.array_equal(merged[:, :120], truth[:, :120])
    step_raw = np.abs(z8[:, 120] - truth[:, 119]).mean()
    step = np.abs(merged[:, 120] - merged[:, 119] - (truth[:, 120] - truth[:, 119])).mean()
    assert step_raw > 3.5 and step < 0.5
    assert np.allclose(merged[:, 199], z8[:, 199])                  # beyond the taper: the fallback as it is
    merged2, _ = P.seam_correct(fine, z8, cell=5.0, band_m=100, sigma_m=50, taper_m=400, work_m=25.0)
    assert np.abs(merged2[:, 120] - merged2[:, 119] - 0.2).mean() < 0.6


def test_seam_correct_edge_blend_is_continuous_cell_by_cell():
    nr, nc = 160, 160
    r, c = np.mgrid[0:nr, 0:nc]
    truth = (300 + 0.5 * r).astype(np.float32)
    z8 = truth + (3.0 + 2.0 * np.sin(r / 4.0)).astype(np.float32)   # offset varying along the seam (8 m wiggles)
    fine = truth.copy()
    fine[:, 100:] = np.nan
    smooth, _ = P.seam_correct(fine, z8, cell=5.0, band_m=100, sigma_m=100, taper_m=400, work_m=5.0)
    edge, _ = P.seam_correct(fine, z8, cell=5.0, band_m=100, sigma_m=100, taper_m=400, work_m=5.0, edge_m=25.0)
    step = lambda m: np.abs((m[:, 100] - m[:, 99]) - (truth[:, 100] - truth[:, 99])).mean()  # noqa: E731
    assert step(edge) < 0.1 < step(smooth)
    assert np.array_equal(edge[:, :100], truth[:, :100])


def test_burn_lowers_line_cells():
    from shapely.geometry import LineString

    z = np.zeros((10, 10), np.float32)
    n = P.burn(z, [LineString([(0.5, 5.5), (9.5, 5.5)])], from_origin(0, 10, 1, 1), 2.0)
    assert n == 10 and z[4, :].min() == -2.0 and z[3, :].max() == 0.0


def test_smooth_distance_removes_the_d8_staircase():
    # a 45 / 0 degree alternating staircase along a line at ~26.6 degrees: raw D8 length is ~8 % too long
    steps = np.array([[1, 1], [1, 0]] * 500, float)
    xy = np.vstack([[0, 0], np.cumsum(steps, axis=0)])
    true = np.hypot(*(xy[-1] - xy[0]))
    raw = np.hypot(*np.diff(xy, axis=0).T).sum()
    sm = P.smooth_distance(xy, 20.0, 1.0)
    assert raw / true > 1.05
    assert sm[-1] == pytest.approx(true, rel=0.01)
    assert len(sm) == len(xy) and np.all(np.diff(sm) >= 0)


def test_profile_metrics():
    s = np.arange(0, 1001, 1.0)
    z = np.where(s < 500, 100 - 0.01 * s, 95.0)                     # 10 m/km then flat
    z[300] = 120                                                    # a bridge
    zm = P.running_min(z)
    assert zm[300] == pytest.approx(97.01) and zm[-1] == 95.0
    assert P.steepest(s, zm, 100.0) == pytest.approx(10.0, abs=0.2)
    assert P.longest_flat(s, zm, 0.05) == pytest.approx(500, abs=6)
    assert np.isnan(P.steepest(s[:10], zm[:10], 100.0))
