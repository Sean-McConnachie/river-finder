import numpy as np
import pytest

from river_finder.routing import accumulate, priority_flood, route, upstream_mask


def ones(shape):
    return np.ones(shape, dtype=bool)


def test_cone_drains_to_border_and_accumulates_everything():
    # Inverted cone: lowest at the edges, peak in the middle. Every cell drains outward.
    y, x = np.mgrid[-10:11, -10:11]
    z = 100.0 - np.hypot(x, y)
    out = route(z, ones(z.shape), cellsize=1.0)
    # Total area reaching all outlets = number of cells.
    outlets = out["rec"] == -1
    assert out["acc"].ravel()[outlets].sum() == pytest.approx(z.size)
    assert np.nanmax(out["fill_depth"]) == 0.0


def test_v_valley_collects_at_outlet():
    # V-shaped valley sloping toward row 0, channel down the middle column.
    nr, nc = 30, 21
    r, c = np.mgrid[0:nr, 0:nc]
    z = r * 1.0 + np.abs(c - nc // 2) * 5.0
    valid = ones(z.shape)
    out = route(z, valid, cellsize=10.0)
    acc = out["acc"]
    # The channel accumulates more downstream; outlet at (0, mid) gets the bulk of the valley.
    ch = acc[:, nc // 2]
    assert np.all(np.diff(ch[::-1]) > 0)
    assert ch[0] > 0.5 * nr * nc * 100.0


def test_pit_is_filled_to_spill_level_and_drains_through_spill():
    # Plateau at 10 with a pit (2) in the middle and a single notch (5) on the left edge.
    z = np.full((9, 9), 10.0)
    z[2:7, 2:7] = 2.0
    z[4, 0:2] = 5.0
    out = route(z, ones(z.shape), cellsize=1.0)
    filled = out["filled"]
    assert np.all(filled[2:7, 2:7] == 5.0)  # pit filled to the notch height
    # The whole pit drains through the notch outlet (4, 0).
    outlet = 4 * 9 + 0
    assert out["rec"][outlet] == -1
    mask = upstream_mask(out["rec"], outlet).reshape(z.shape)
    assert mask[2:7, 2:7].all()


def test_flat_plateau_with_one_spill_point():
    # Perfectly flat plateau surrounded by a rim, one spill gap on the bottom edge.
    z = np.full((12, 12), 50.0)
    z[0, :] = z[-1, :] = z[:, 0] = z[:, -1] = 60.0
    z[-1, 6] = 40.0
    out = route(z, ones(z.shape), cellsize=1.0)
    spill = 11 * 12 + 6
    # All interior plateau cells drain to the spill cell.
    assert out["acc"].ravel()[spill] >= 10 * 10 + 1


def test_flat_valley_floor_drains_down_the_middle():
    # Contour-DEM style valley: sloping sides, a perfectly flat 15-cell-wide floor at z=20,
    # draining out of the bottom row through a lower cell at the floor centre.
    nr, nc = 40, 41
    r, c = np.mgrid[0:nr, 0:nc]
    mid = nc // 2
    z = np.where(np.abs(c - mid) <= 7, 20.0, 20.0 + (np.abs(c - mid) - 7) * 2.0)
    z[0, :] = z[-1, :] = 100.0
    z[-1, mid] = 0.0
    out = route(z, ones(z.shape), cellsize=8.0)
    acc = out["acc"]
    # The strongest flow line runs exactly down the middle of the 15-cell floor. Skip the top rows
    # (lanes from the corners merging) and the last rows (funnel into the one-cell outlet).
    for row in range(10, nr - 8):
        assert int(np.argmax(acc[row])) == mid, row
    assert acc[-1, mid] == pytest.approx(nr * nc * 64.0)  # every cell reaches the single outlet


def test_nodata_acts_as_outlet():
    z = np.arange(25, dtype=float).reshape(5, 5)
    valid = ones(z.shape)
    valid[2, 2] = False
    filled, rec, order = priority_flood(z, valid)
    assert rec[2 * 5 + 2] == -1
    assert len(order) == 24


def test_accumulate_weights_and_truncation():
    # Chain 0 -> 1 -> 2 (outlet); cell 0 is an edge cell.
    rec = np.array([1, 2, -1], dtype=np.int64)
    acc, trunc, n = accumulate(rec, np.array([1.0, 2.0, 3.0]), np.array([True, False, False]))
    assert n == 3
    assert list(acc) == [1.0, 3.0, 6.0]
    assert list(trunc) == [True, True, True]


def test_matches_pysheds_on_random_terrain():
    if not hasattr(np, "in1d"):  # pysheds still calls np.in1d, which NumPy 2.4 removed
        np.in1d = np.isin
    pysheds = pytest.importorskip("pysheds.grid")
    rng = np.random.default_rng(0)
    nr, nc = 60, 80
    r, c = np.mgrid[0:nr, 0:nc]
    z = (r * 0.5 + 20 * np.sin(c / 9.0) + rng.normal(0, 0.3, (nr, nc))).astype(np.float64)
    out = route(z, ones(z.shape), cellsize=1.0)
    ours = out["acc"]

    from affine import Affine
    from pysheds.sview import Raster, ViewFinder

    vf = ViewFinder(affine=Affine(1, 0, 0, 0, -1, nr), shape=z.shape, nodata=-9999.0)
    dem = Raster(z, viewfinder=vf)
    grid = pysheds.Grid(viewfinder=vf)
    filled = grid.resolve_flats(grid.fill_depressions(grid.fill_pits(dem)))
    fdir = grid.flowdir(filled)
    theirs = np.asarray(grid.accumulation(fdir)) + 1  # pysheds excludes the cell itself
    # Compare away from flats/borders: strongly convergent channel cells should agree closely.
    big = theirs > 50
    rel = np.abs(ours[big] - theirs[big]) / theirs[big]
    assert np.median(rel) < 0.05
