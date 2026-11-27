"""Cross-sections of every reach from the DEM: width, flow area and wetted perimeter at fixed stages,
bank height, and the per-reach summaries the ML features read (rf-zmy.10, ``docs/cross-sections.md``).

Method
------
- **Stations**: every ``SPACING_M`` (50 m) along the reach line, centred (a 250 m reach gets sections at
  25, 75, ..., 225 m; a reach shorter than one spacing gets one at its midpoint).
- **Direction**: the section is perpendicular to the chord of the reach line from ``TANGENT_M`` (40 m) above
  to 40 m below the station, which smooths the D8 zig-zag.
- **Profile**: the DEM (raw, not filled) is sampled bilinearly every half cell along the section, out to
  ``HALF_WIDTH_M`` (500 m) on each side of the channel.
- **Channel bottom**: the lowest sample within ``SNAP_M`` (16 m, two 8 m cells) of the reach line, because the
  routed line can sit a cell or two off the DEM's valley axis. The section is re-centred there and every
  height is measured from that bottom ``z0``. Where a DEM cell in the 3 x 3 window around the station is lower,
  ``z0`` is that cell (``cell_floor``): on a diagonal D8 stretch the station can sit on a cell corner, where bilinear
  sampling averages two channel cells with two bank cells and lifts the bottom by up to half a cell's rise.
- **Stage h** (``STAGES``: 1, 2, 5, 10, 20 m above ``z0``): on each side, walk outward from the bottom until the
  ground first rises above ``z0 + h`` (linear interpolation between samples). Width ``w(h)`` is the sum of the
  two distances, flow area ``A(h)`` the area between the level and the ground over that span (trapezoids), and
  ``P(h)`` the wetted perimeter (ground length). Ground beyond a ridge never counts. A side that does not reach
  the level within 500 m is *censored*: its distance is 500 m. No-data samples (sea) count as flat ground at
  ``z0``, so coastal and lake sections come out wide and censored.
- **Bank and wall height**: the highest ground within ``BANK_M`` (50 m) of the bottom on each side, minus ``z0``;
  ``bank_lo`` is the lower of the two sides (the bank the flood overtops first), ``bank_hi`` the higher. The same
  within ``GORGE_M`` (150 m, the gorge radius of ``reaches.py``) gives the wall heights ``gorge_lo`` / ``gorge_hi``.
  On the 8 m DEM the 50 m bank is nearly flat (median 5-15 % of the LiDAR value), the 150 m wall is not.

Per reach (``reach_table``): number of sections, median and minimum width per stage, median area and wetted
perimeter per stage, the share of sections censored per stage, median (and 90 % / minimum) bank and wall heights,
the share of flat sections (both sides censored at 1 m: lakes, wide flats), the no-data share and the DEM cell size
(``xs_res_m``). ``derive`` adds a valley-scale unit stream power and the Manning flood
hydraulics (``hydraulics``), which read the flood and the gradient of the table they are joined to.

Resolution
----------
The national run samples the 8 m contour DEM (interpolated from 20 m contours; ``docs/data-sources.md``). It has
no channel below the water surface and its floor between the stream line and the next contour is interpolated,
so widths at 1-2 m stages are mostly interpolation; 5-20 m stages are what it resolves. The reference areas also
hold a 5 m LiDAR-derived DEM; ``run_region(..., dem="5m")`` extracts the same sections on it for the resolution
comparison, but the ML features always use the 8 m extraction so that every reach in NZ is measured the same way.

Outputs
-------
National: ``data/nz/xsec/tiles/<tile>.pkl`` per tile (resumable: a tile with a file is skipped) and the merged
``data/nz/xsec.pkl`` (one row per reach, key ``gid``), which ``ml.ml_table`` joins. Reference areas:
``data/<region>/xsec_<res>.pkl`` (key ``seg_id``). ``out_dir`` moves either elsewhere.
"""

from __future__ import annotations

import json
import math
import resource
import time
from pathlib import Path

import numpy as np
import pandas as pd
from numba import njit

STAGES = (1.0, 2.0, 5.0, 10.0, 20.0)   # m above the section's channel bottom
SPACING_M = 50.0
HALF_WIDTH_M = 500.0
BANK_M = 50.0
GORGE_M = 150.0           # wall height: highest ground within this distance (``reaches.py`` gorge radius)
SNAP_M = 16.0
TANGENT_M = 40.0
MIN_AREA_KM2 = 1.0
DEM_MARGIN_M = 1024.0     # DEM read around a tile's core (128 cells of 8 m; > half-width + snap + tangent)
CHUNK = 20_000            # sections sampled at once
MANNING_N = 0.045         # roughness for the flood-stage hydraulics in ``hydraulics`` (mountain rivers)
RHO_G = 1000.0 * 9.81


def stage_tag(h: float) -> str:
    return f"{h:g}".replace(".", "p")


# ---------------------------------------------------------------------------- stations and profiles
def stations(geoms, spacing: float = SPACING_M, tangent: float = TANGENT_M):
    """Section stations along each line: (reach index, along-line distance, x, y, unit normal nx, ny).

    Lines of zero length get no station."""
    import shapely

    geoms = np.asarray(geoms, dtype=object)
    length = shapely.length(geoms)
    n = np.where(length > 0, np.maximum(1, np.round(length / spacing)), 0).astype(np.int64)
    ri = np.repeat(np.arange(len(geoms)), n)
    k = np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n)
    s = (k + 0.5) * length[ri] / np.maximum(n[ri], 1)
    g = geoms[ri]
    p = shapely.get_coordinates(shapely.line_interpolate_point(g, s))
    a = shapely.get_coordinates(shapely.line_interpolate_point(g, np.maximum(s - tangent, 0.0)))
    b = shapely.get_coordinates(shapely.line_interpolate_point(g, np.minimum(s + tangent, length[ri])))
    t = b - a
    norm = np.hypot(t[:, 0], t[:, 1])
    ok = norm > 0
    t[ok] /= norm[ok, None]
    t[~ok] = (1.0, 0.0)
    return ri, s, p[:, 0], p[:, 1], -t[:, 1], t[:, 0]


def sample_profiles(z: np.ndarray, transform, x, y, nx, ny, half_samples: int, du: float) -> np.ndarray:
    """DEM profiles along the normals, bilinear, ``2 * half_samples + 1`` samples ``du`` apart (NaN off the grid
    or next to no-data)."""
    from scipy.ndimage import map_coordinates

    cs = transform.a
    u = np.arange(-half_samples, half_samples + 1) * du
    px = x[:, None] + u[None, :] * nx[:, None]
    py = y[:, None] + u[None, :] * ny[:, None]
    cols = (px - transform.c) / cs - 0.5
    rows = (transform.f - py) / cs - 0.5
    out = map_coordinates(z, [rows.ravel(), cols.ravel()], order=1, mode="constant", cval=np.nan,
                          prefilter=False, output=np.float32)
    return out.reshape(px.shape)


@njit(cache=True)
def _measure(Z, zfloor, du, snap, side_n, bank_n, gorge_n, stages, w, a, p, cens, z0o, offo, blo, bhi, glo, ghi,
             nod):
    """Per section: channel bottom, then width, area, wetted perimeter and censoring per stage, bank heights."""
    n, K = Z.shape
    c = K // 2
    S = stages.size
    for i in range(n):
        j0 = -1
        z0 = np.inf
        for j in range(c - snap, c + snap + 1):
            v = Z[i, j]
            if v == v and v < z0:
                z0 = v
                j0 = j
        if j0 < 0:
            continue
        if zfloor[i] < z0:      # a DEM cell next to the station is lower than the interpolated profile
            z0 = zfloor[i]
        z0o[i] = z0
        offo[i] = (j0 - c) * du
        lo = np.inf
        hi = -np.inf
        glo_ = np.inf
        ghi_ = -np.inf
        miss = 0
        for side in (-1, 1):
            m = -np.inf
            mg = -np.inf
            for k in range(1, side_n + 1):
                v = Z[i, j0 + side * k]
                if v != v:
                    miss += 1
                    continue
                if k <= bank_n and v > m:
                    m = v
                if k <= gorge_n and v > mg:
                    mg = v
            b = m - z0 if m > -np.inf else 0.0
            bg = mg - z0 if mg > -np.inf else 0.0
            lo = min(lo, b)
            hi = max(hi, b)
            glo_ = min(glo_, bg)
            ghi_ = max(ghi_, bg)
        blo[i] = lo
        bhi[i] = hi
        glo[i] = glo_
        ghi[i] = ghi_
        nod[i] = miss / (2.0 * side_n)
        for s in range(S):
            lvl = z0 + stages[s]
            width = 0.0
            area = 0.0
            per = 0.0
            cs_ = 0
            for side in (-1, 1):
                prev = z0
                k = 1
                hit = False
                while k <= side_n:
                    v = Z[i, j0 + side * k]
                    if v != v:
                        v = z0
                    if v > lvl:
                        f = (lvl - prev) / (v - prev)
                        width += (k - 1 + f) * du
                        area += 0.5 * f * du * (lvl - prev)
                        per += f * math.sqrt(du * du + (v - prev) * (v - prev))
                        hit = True
                        break
                    area += 0.5 * du * ((lvl - prev) + (lvl - v))
                    per += math.sqrt(du * du + (v - prev) * (v - prev))
                    prev = v
                    k += 1
                if not hit:
                    width += side_n * du
                    cs_ += 1
            w[i, s] = width
            a[i, s] = area
            p[i, s] = per
            cens[i, s] = cs_


def measure(Z: np.ndarray, du: float, snap_samples: int, side_samples: int, bank_samples: int,
            gorge_samples: int, stages=STAGES, zfloor: np.ndarray | None = None) -> dict:
    """Section geometry from profiles ``Z`` (n, 2 * (side + snap) + 1), centred on the reach line.

    The channel bottom ``z0`` is the lowest profile sample within ``snap_samples`` of the centre, or ``zfloor`` (the
    lowest DEM cell next to the station, ``cell_floor``) where that is lower.

    Returns arrays: ``w``, ``a``, ``p`` (n, stages; m, m2, m), ``cens`` (n, stages; sides censored, 0-2), ``z0``,
    ``offset`` (bottom position relative to the line, m), ``bank_lo``, ``bank_hi`` (lower and higher side's highest
    ground within ``bank_samples``, m), ``gorge_lo``, ``gorge_hi`` (the same within ``gorge_samples``) and ``nodata``
    (share of no-data samples). Sections with no valid sample near the line are NaN."""
    n = Z.shape[0]
    st = np.asarray(stages, dtype=np.float64)
    if Z.shape[1] != 2 * (side_samples + snap_samples) + 1:
        raise ValueError("profile length does not match side + snap samples")
    out = {k: np.full((n, len(st)), np.nan) for k in ("w", "a", "p")}
    out["cens"] = np.zeros((n, len(st)), np.int8)
    for k in ("z0", "offset", "bank_lo", "bank_hi", "gorge_lo", "gorge_hi", "nodata"):
        out[k] = np.full(n, np.nan)
    zf = np.full(n, np.inf) if zfloor is None else np.where(np.isfinite(zfloor), zfloor, np.inf).astype(np.float64)
    _measure(np.ascontiguousarray(Z, dtype=np.float32), zf, float(du), int(snap_samples), int(side_samples),
             int(bank_samples), int(gorge_samples), st, out["w"], out["a"], out["p"], out["cens"], out["z0"],
             out["offset"], out["bank_lo"], out["bank_hi"], out["gorge_lo"], out["gorge_hi"], out["nodata"])
    return out


def cell_floor(z: np.ndarray, transform, x, y, radius: int = 1) -> np.ndarray:
    """Lowest DEM cell in the (2 radius + 1)^2 window around each point (NaN if none). Bilinear sampling lifts the
    bottom of a diagonal D8 channel at cell corners (half the neighbours are bank cells); the cells do not."""
    nr, nc = z.shape
    cs = transform.a
    r0 = np.floor((transform.f - np.asarray(y)) / cs).astype(np.int64)
    c0 = np.floor((np.asarray(x) - transform.c) / cs).astype(np.int64)
    out = np.full(len(r0), np.inf)
    for dr in range(-radius, radius + 1):
        for dc in range(-radius, radius + 1):
            r, c = r0 + dr, c0 + dc
            ok = (r >= 0) & (r < nr) & (c >= 0) & (c < nc)
            v = np.full(len(r0), np.inf)
            v[ok] = z[r[ok], c[ok]]
            out = np.fmin(out, np.where(np.isfinite(v), v, np.inf))
    return np.where(np.isfinite(out), out, np.nan)


def extract(z: np.ndarray, transform, geoms, spacing: float = SPACING_M, half_width: float = HALF_WIDTH_M,
            snap: float = SNAP_M, bank: float = BANK_M, gorge: float = GORGE_M, stages=STAGES,
            chunk: int = CHUNK) -> dict:
    """Every section of every line in ``geoms`` on DEM ``z`` (2-D, NaN = no data; ``transform`` its affine).

    Returns the ``measure`` arrays plus ``reach`` (index into ``geoms``) and ``s`` (distance along the line)."""
    cs = float(transform.a)
    du = cs / 2.0
    side_n = int(round(half_width / du))
    snap_n = max(1, int(round(snap / du)))
    bank_n = max(1, int(round(bank / du)))
    gorge_n = max(1, int(round(gorge / du)))
    ri, s, x, y, nx, ny = stations(geoms, spacing)
    parts = []
    for i0 in range(0, len(ri), chunk):
        sl = slice(i0, i0 + chunk)
        Z = sample_profiles(z, transform, x[sl], y[sl], nx[sl], ny[sl], side_n + snap_n, du)
        zf = cell_floor(z, transform, x[sl], y[sl])
        parts.append(measure(Z, du, snap_n, side_n, bank_n, gorge_n, stages, zf))
    if parts:
        sec = {k: np.concatenate([q[k] for q in parts]) for k in parts[0]}
    else:
        sec = measure(np.zeros((0, 2 * (side_n + snap_n) + 1), np.float32), du, snap_n, side_n, bank_n, gorge_n,
                      stages)
    sec.update(reach=ri, s=s, x=x, y=y)
    return sec


# ---------------------------------------------------------------------------- per reach
def reach_table(sec: dict, n_reaches: int, stages=STAGES, res_m: float = np.nan) -> pd.DataFrame:
    """Per reach summaries of its sections (row i = line i). Reaches with no valid section are NaN, with
    ``xs_n`` 0."""
    ok = np.isfinite(sec["z0"])
    ri = sec["reach"][ok]
    cols = {"reach": ri, "bank_lo": sec["bank_lo"][ok], "bank_hi": sec["bank_hi"][ok],
            "gorge_lo": sec["gorge_lo"][ok], "gorge_hi": sec["gorge_hi"][ok],
            "flat": (sec["cens"][ok, 0] == 2).astype(float), "nodata": sec["nodata"][ok]}
    for j, h in enumerate(stages):
        tg = stage_tag(h)
        cols[f"w{tg}"] = sec["w"][ok, j]
        cols[f"a{tg}"] = sec["a"][ok, j]
        cols[f"p{tg}"] = sec["p"][ok, j]
        cols[f"c{tg}"] = (sec["cens"][ok, j] > 0).astype(float)
    df = pd.DataFrame(cols)
    g = df.groupby("reach", sort=True)
    med = g.median()
    out = pd.DataFrame(index=pd.RangeIndex(n_reaches))
    out["xs_n"] = g.size().reindex(out.index).fillna(0).astype(np.int16)
    out["xs_res_m"] = np.float32(res_m)
    for h in stages:
        tg = stage_tag(h)
        out[f"xs_w{tg}_med"] = med[f"w{tg}"]
        out[f"xs_w{tg}_min"] = g[f"w{tg}"].min()
        out[f"xs_a{tg}_med"] = med[f"a{tg}"]
        out[f"xs_p{tg}_med"] = med[f"p{tg}"]
        out[f"xs_cens{tg}"] = g[f"c{tg}"].mean()
    out["xs_bank_lo_med"] = med["bank_lo"]
    out["xs_bank_lo_p90"] = g["bank_lo"].quantile(0.9)
    out["xs_bank_hi_med"] = med["bank_hi"]
    out["xs_gorge_lo_med"] = med["gorge_lo"]
    out["xs_gorge_lo_min"] = g["gorge_lo"].min()
    out["xs_gorge_hi_med"] = med["gorge_hi"]
    out["xs_flat_frac"] = g["flat"].mean()
    out["xs_nodata"] = g["nodata"].mean()
    for c in out.columns:
        if out[c].dtype == np.float64:
            out[c] = out[c].astype(np.float32)
    return out


def hydraulics(t: pd.DataFrame, q_col: str = "maf_m3s", s_col: str = "grad_s", stages=STAGES,
               n: float = MANNING_N, h_min: float = 0.25, h_max: float = 60.0) -> pd.DataFrame:
    """Flood stage, width, mean velocity and unit stream power at the reach's flood ``q_col`` (m3/s) on its median
    section, by Manning: ``Q(h) = A(h) R(h)^(2/3) S^(1/2) / n`` with ``R = A / P`` from the median area and wetted
    perimeter per stage, ``S`` = gradient (``s_col``, m/km; floored at 0.1 m/km). The stage where ``Q(h)`` reaches the
    flood is found by log-log interpolation between stages (extrapolated beyond them, clipped to
    ``h_min``-``h_max``, default 0.25-60 m).
    Returns ``xs_h_q`` (m), ``xs_w_q`` (m), ``xs_a_q`` (m2), ``xs_v_q`` = Q / A (m/s) and ``xs_omega_q`` =
    rho g Q S / w (W/m2). On the 8 m DEM most floods sit below its resolvable stages, so these are indices of how
    confined the section is at the flood, not field hydraulics."""
    hs = np.log(np.asarray(stages, dtype=float))
    tags = [stage_tag(h) for h in stages]
    A = np.column_stack([t[f"xs_a{g}_med"].to_numpy(dtype=float) for g in tags])
    P = np.column_stack([t[f"xs_p{g}_med"].to_numpy(dtype=float) for g in tags])
    W = np.column_stack([t[f"xs_w{g}_med"].to_numpy(dtype=float) for g in tags])
    S = np.maximum(t[s_col].to_numpy(dtype=float), 0.1) / 1000.0
    Q = t[q_col].to_numpy(dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        K = A * (A / P) ** (2.0 / 3.0) / n
        lq = np.log(np.maximum.accumulate(np.maximum(K, 1e-9), axis=1) * np.sqrt(S)[:, None])
        lq = lq + np.arange(len(tags)) * 1e-9          # strictly increasing for the interpolation
        lQ = np.log(np.maximum(Q, 1e-6))
        # segment j between stages j and j+1 (j = 0 below the first, last beyond the last)
        j = np.clip((lq < lQ[:, None]).sum(axis=1) - 1, 0, len(tags) - 2)
        r = np.arange(len(t))
        f = (lQ - lq[r, j]) / (lq[r, j + 1] - lq[r, j])
        lh = hs[j] + f * (hs[j + 1] - hs[j])
        lh = np.clip(lh, math.log(h_min), math.log(h_max))
        f = (lh - hs[j]) / (hs[j + 1] - hs[j])

        def at(M):
            lm = np.log(np.maximum(M, 1e-6))
            return np.exp(lm[r, j] + f * (lm[r, j + 1] - lm[r, j]))

        a_q, w_q = at(A), at(W)
        out = pd.DataFrame({"xs_h_q": np.exp(lh), "xs_w_q": w_q, "xs_a_q": a_q, "xs_v_q": Q / a_q,
                            "xs_omega_q": RHO_G * Q * S / w_q}, index=t.index)
    bad = ~np.isfinite(A).all(axis=1) | ~np.isfinite(P).all(axis=1) | ~np.isfinite(Q)
    out.loc[bad] = np.nan
    return out


# ---------------------------------------------------------------------------- drivers
def _maxrss_gb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6


def _read_dem(path: Path):
    import rasterio

    from .stac import NODATA

    with rasterio.open(path) as ds:
        z = ds.read(1).astype(np.float32)
        m = ds.read_masks(1) > 0
        tr = ds.transform
    z[~m | ~np.isfinite(z) | (z <= NODATA + 1)] = np.nan
    return z, tr


def reaches_frame(path: Path, key: str, min_area_km2: float):
    import pyogrio

    g = pyogrio.read_dataframe(path, columns=[key, "area_km2"])
    return g[g["area_km2"] >= min_area_km2].reset_index(drop=True)


def measure_reaches(dem_path: Path, g, key: str) -> tuple[pd.DataFrame, dict]:
    t0 = time.time()
    z, tr = _read_dem(dem_path)
    t1 = time.time()
    sec = extract(z, tr, g.geometry.to_numpy())
    del z
    rt = reach_table(sec, len(g), res_m=float(tr.a))
    rt.insert(0, key, g[key].to_numpy())
    stats = {"reaches": int(len(g)), "sections": int(len(sec["reach"])),
             "valid_sections": int(np.isfinite(sec["z0"]).sum()), "read_s": round(t1 - t0, 1),
             "extract_s": round(time.time() - t1, 1), "res_m": float(tr.a)}
    return rt, stats


def xsec_dir(out_dir: Path | None = None) -> Path:
    from .national import nz_dir

    return Path(out_dir) if out_dir is not None else nz_dir() / "xsec"


def fetch_tile_dem(tile, dest: Path) -> Path:
    """The 8 m DEM over the tile's core plus ``DEM_MARGIN_M``, on the routing grid (same origin and cell)."""
    from pyproj import Transformer

    from .nz import DEM_8M, RES
    from .regions import REPO_ROOT
    from .stac import mosaic

    if dest.exists():
        return dest
    x0, y0, x1, y1 = tile.core
    m = DEM_MARGIN_M
    bb = (x0 - m, y0 - m, x1 + m, y1 + m)
    ll = Transformer.from_crs("EPSG:2193", "EPSG:4326", always_xy=True).transform_bounds(*bb, densify_pts=21)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part.tif")
    mosaic(DEM_8M, ll, bb, RES, tmp, REPO_ROOT / "data" / "_stac")
    tmp.rename(dest)
    return dest


def run_tile(tile, out_dir: Path | None = None, min_area_km2: float = MIN_AREA_KM2, keep_dem: bool = False,
             log=print) -> dict | None:
    """Sections of one national tile's core reaches -> ``<out>/tiles/<tile>.pkl`` (+ ``.json`` stats). Skips a
    tile whose file exists or that has no reaches."""
    from .national import nz_dir

    out = xsec_dir(out_dir)
    dst = out / "tiles" / f"{tile.id}.pkl"
    if dst.exists():
        return None
    seg = nz_dir() / "tiles" / tile.id / "segments_core.gpkg"
    if not seg.exists():
        return None
    t0 = time.time()
    g = reaches_frame(seg, "gid", min_area_km2)
    dem = out / "dem" / f"{tile.id}.tif"
    fetch_tile_dem(tile, dem)
    t1 = time.time()
    rt, st = measure_reaches(dem, g, "gid")
    dst.parent.mkdir(parents=True, exist_ok=True)
    rt.to_pickle(dst.with_suffix(".part.pkl"))
    dst.with_suffix(".part.pkl").rename(dst)
    if not keep_dem:
        dem.unlink(missing_ok=True)
    st.update(tile=tile.id, fetch_s=round(t1 - t0, 1), total_s=round(time.time() - t0, 1),
              maxrss_gb=round(_maxrss_gb(), 2))
    dst.with_suffix(".json").write_text(json.dumps(st, indent=1))
    log(f"[{tile.id}] {st['reaches']} reaches, {st['sections']} sections: dem {st['fetch_s']:.0f} s, read "
        f"{st['read_s']:.0f} s, sections {st['extract_s']:.0f} s (max RSS {st['maxrss_gb']} GB)")
    return st


def run_tiles(tiles=None, out_dir: Path | None = None, min_area_km2: float = MIN_AREA_KM2, keep_dem: bool = False,
              log=print) -> Path:
    """Every done national tile (or ``tiles``), fetching the next tile's DEM while the current one is measured,
    then ``merge``. Resumable: tiles with a file are skipped."""
    from concurrent.futures import ThreadPoolExecutor

    from .national import nz_dir
    from .nz import Tile

    out = xsec_dir(out_dir)
    if not tiles:
        tiles = sorted((Tile.parse(p.parent.name) for p in (nz_dir() / "tiles").glob("*/segments_core.gpkg")),
                       key=lambda t: (-t.iy, t.ix))
    todo = [t for t in tiles if not (out / "tiles" / f"{t.id}.pkl").exists()]
    log(f"xsec: {len(tiles)} tiles, {len(todo)} to do -> {out}")

    def prefetch(t):
        return fetch_tile_dem(t, out / "dem" / f"{t.id}.tif")

    with ThreadPoolExecutor(1) as ex:
        nxt = ex.submit(prefetch, todo[0]) if todo else None
        for i, t in enumerate(todo):
            try:
                nxt.result()
            except Exception as e:
                log(f"[{t.id}] DEM prefetch failed: {e!r}")
            nxt = ex.submit(prefetch, todo[i + 1]) if i + 1 < len(todo) else None
            try:
                run_tile(t, out, min_area_km2, keep_dem, log)
            except Exception as e:  # keep going; a failed tile is re-run next time
                log(f"[{t.id}] FAILED: {e!r}")
        if nxt is not None:
            nxt.result()
    return merge(out, log=log)


def merge(out_dir: Path | None = None, log=print) -> Path:
    """Every tile file in ``<out>/tiles`` -> ``<out>.pkl`` (``data/nz/xsec.pkl`` by default), so re-running one
    tile keeps the others."""
    out = xsec_dir(out_dir)
    paths = [p for p in sorted((out / "tiles").glob("*.pkl")) if not p.name.endswith(".part.pkl")]
    df = pd.concat([pd.read_pickle(p) for p in paths], ignore_index=True) if paths else pd.DataFrame()
    dst = out.parent / f"{out.name}.pkl"
    df.to_pickle(dst)
    log(f"xsec: {len(df)} reaches from {len(paths)} tiles -> {dst}")
    return dst


def run_region(region, dem: str = "8m", out_dir: Path | None = None, min_area_km2: float = 0.0, log=print) -> Path:
    """Sections of a reference area's reaches (``segments.gpkg``, key ``seg_id``) on its 8 m contour DEM
    (default, comparable with the national run) or its 5 m LiDAR DEM -> ``xsec_<dem>.pkl``."""
    d = region.data_dir()
    g = reaches_frame(d / "segments.gpkg", "seg_id", min_area_km2)
    rt, st = measure_reaches(d / f"dem_{dem}.tif", g, "seg_id")
    out = Path(out_dir) if out_dir is not None else d
    out.mkdir(parents=True, exist_ok=True)
    dst = out / (f"xsec_{dem}.pkl" if out_dir is None else f"{region.name}_xsec_{dem}.pkl")
    rt.to_pickle(dst)
    st.update(region=region.name, dem=dem, maxrss_gb=round(_maxrss_gb(), 2))
    dst.with_suffix(".json").write_text(json.dumps(st, indent=1))
    log(f"[{region.name}] {dem}: {st['reaches']} reaches, {st['sections']} sections in {st['extract_s']:.0f} s "
        f"-> {dst}")
    return dst


# ---------------------------------------------------------------------------- joins and derived columns
DERIVED = ("xs_omega10_wm2", "xs_h_q", "xs_w_q", "xs_a_q", "xs_v_q", "xs_omega_q")


def attach(f: pd.DataFrame, table: pd.DataFrame, key: str) -> pd.DataFrame:
    """``f`` with the ``xs_*`` columns of ``table`` joined on ``key`` (NaN where a reach has none). In place."""
    cols = [c for c in table.columns if c.startswith("xs_")]
    x = table.drop_duplicates(key).set_index(key)[cols]
    j = x.reindex(f[key].to_numpy())
    for c in cols:
        f[c] = j[c].to_numpy()
    f["xs_n"] = f["xs_n"].fillna(0).astype(np.int16)
    return f


def derive(f: pd.DataFrame) -> pd.DataFrame:
    """The ``DERIVED`` columns from the joined ``xs_*`` columns, the flood ``maf_m3s`` and the gradient ``grad_s``.
    In place; call again after the MAF changes.

    - ``xs_omega10_wm2``: unit stream power at the mean annual flood spread over the 10 m-stage width,
      rho g Q S / w10 (W/m2, S floored at 0.1 m/km). A valley-scale index: the real flood is narrower.
    - ``xs_h_q`` ... ``xs_omega_q``: Manning flood stage, width, area, velocity and unit stream power
      (``hydraulics``)."""
    with np.errstate(divide="ignore", invalid="ignore"):
        S = np.maximum(f["grad_s"].to_numpy(dtype=float), 0.1) / 1000.0
        f["xs_omega10_wm2"] = (RHO_G * f["maf_m3s"].to_numpy(dtype=float) * S
                               / f["xs_w10_med"].to_numpy(dtype=float)).astype(np.float32)
    h = hydraulics(f)
    for c in h.columns:
        f[c] = h[c].to_numpy(dtype=np.float32)
    return f


def national_table_path() -> Path:
    from .national import nz_dir

    return nz_dir() / "xsec.pkl"


def region_table_path(region: str, dem: str = "8m") -> Path:
    from .regions import REPO_ROOT

    return REPO_ROOT / "data" / region / f"xsec_{dem}.pkl"


def join(f: pd.DataFrame, path: Path | None = None, log=print) -> bool:
    """Join the stored cross-sections into ``f`` (national reaches by ``gid`` from ``data/nz/xsec.pkl``; a reference
    area's frame, one ``region`` and no ``gid``, by ``seg_id`` from ``data/<region>/xsec_8m.pkl``) and ``derive``.
    In place; False (and ``f`` unchanged) when there is no table for it."""
    if path is None:
        if "gid" in f:
            path = national_table_path()
        elif "region" in f and f["region"].nunique() == 1:
            path = region_table_path(str(f["region"].iloc[0]))
        else:
            return False
    if not Path(path).exists():
        return False
    table = pd.read_pickle(path)
    key = "gid" if "gid" in table.columns else "seg_id"
    attach(f, table, key)
    derive(f)
    log(f"xsec: {int((f['xs_n'] > 0).sum())} of {len(f)} reaches have cross-sections ({path})")
    return True
