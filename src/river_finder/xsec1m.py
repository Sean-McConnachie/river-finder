"""Cross-sections of every reach on the 1 m LiDAR DEM: channel shape, bankfull, banks, variability, bed roughness
and the flood hydraulics (rf-lxa.4, ``docs/cross-sections-1m.md``).

Method
------
- **Stations** every ``SPACING_M`` (25 m) along each reach line (``xsec.stations``: centred, perpendicular to the
  chord from 40 m above to 40 m below the station).
- **Re-centring, pass 1**: the 1 m DEM is sampled every 1 m along the normal, ``SEARCH_M`` (100 m) to each side of
  the line; the channel is the lowest point (3-sample mean, then the lowest raw sample next to it). The national 8 m
  lines lie 11 m (median) / 70 m (p90) from the 1 m channel; the 2 m lines of the 1 m rebuild 0.7 / 4.4 m, so
  there a window of ~10 m is enough. Flags (``FLAG_*``): ``EDGE`` (a lower point just outside the window: the
  channel is farther away), ``TWIN`` (a second low within ``TWIN_M`` of the lowest, behind a rise of ``RIDGE_M``:
  two channels), ``NODATA``.
- **Pass 2**: the section direction is re-cut perpendicular to the chord between the pass-1 channel points of the
  neighbouring sections (50 m apart, or 25 m), through the pass-1 channel point, and re-centred within ``REFINE_M``
  (12 m; ``FAR`` when that fails). A section whose channel height lies far off the reach's other sections (a robust
  line through the bottoms; ``LOW``) is dropped too: it found another channel.
- **Profile** (what the national pass stores, ``U``): on the final section, centred on the channel, the 1 m DEM
  (bilinear) every 1 m within +-``INNER_M`` (150 m) and a 2 m block mean of the 1 m cells every 2 m from 152 m to
  ``OUTER_M`` (500 m): 651 samples, heights above the channel ``z0``.
- **Measures** (``measure``, from the profile only): on each side, walk out from the channel until the ground first
  rises above the stage ``h``; width ``w(h)`` = both distances, flow area ``A(h)``, wetted perimeter ``P(h)`` (as
  ``xsec.py``), at ``STAGES`` (0.1-20 m) and on a 0.1 m grid to 5 m for the shape fits. No-data samples count as
  ground at ``z0`` (water). From them: the shape exponents (``w ~ h^b``, ``A ~ h^m`` over 0.2-2 m), the form factor
  ``A / (w h)``, the bankfull stage (Wolman: the minimum of ``w(h) / h`` over 0.3-5 m, a break only where the ratio
  rises by 20 % above it), bankfull width, area, perimeter and the flood-prone width at twice the bankfull stage
  (entrenchment ratio), the bank angle of each side (0.25-3 m), the share of the bank rise (0.25-5 m) in 1 m steps
  steeper than 60 degrees, and the highest ground within 50, 150 and 500 m of each side.
- **Long profile**: through the final channel points of a reach, every 1 m, the lowest 1 m cell within 2 m; its
  linear-detrended spread (bed roughness), the share of its drop in the steepest 10 % of its length (running
  minimum) and its gradient.

LiDAR does not see below the water: every stage is a height above the water surface at capture and every area is
the area above it. ``derive`` adds the Manning flood hydraulics on the reach's median section (as ``xsec.hydraulics``)
both as measured and with an estimated under-water rectangle (the water width times the Manning depth of a capture
flow of ``CAPTURE_SHARE`` of the mean annual flood; an estimate, labelled ``_est``).

Inputs and outputs
------------------
The 1 m mosaic (``lidar.MOSAIC``) is read at full resolution only, in the 512 m blocks within ``OUTER_M`` +
``SEARCH_M`` of the stations (corridor reads, ``corridor_jobs``), 16 km chunks at a time, many threads in parallel
(``lidarpipe.stream``). Per tile: ``<out>/tiles/<tile>_sec.parquet`` (one row per section), ``<tile>_reach.pkl``
(one row per reach, key ``gid``), ``<tile>.json`` (stats) and, with ``keep_profiles``, ``<tile>_prof.npz`` (the
stored profiles as the national pass would keep them). ``merge`` joins the tiles into ``<out>/reach1m.pkl``.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

import numpy as np
import pandas as pd
from numba import njit

from . import xsec as XS

SPACING_M = 25.0
TANGENT_M = 40.0
INNER_M = 150          # 1 m samples within this distance of the channel
OUTER_M = 500          # 2 m samples beyond, out to this distance
OUTER_DU = 2
SEARCH_M = 100         # pass 1: re-centring window around the routed line
REFINE_M = 12          # pass 2: window around the pass-1 channel point
EDGE_M = 10            # beyond the window: a lower point there means the channel is farther away
RIDGE_M = 1.5          # a second channel lies behind a rise of at least this ...
TWIN_M = 0.5           # ... and within this height of the lowest point
LOW_M = 2.0            # a bottom more than max(LOW_M, 4 MAD) off the reach's robust line: another channel
MIN_AREA_KM2 = 2.0
MIN_LIDAR_SHARE = 0.5
CHUNK_M = 16_000.0
BLOCK = 512
CAPTURE_SHARE = 0.05   # capture flow / mean annual flood for the under-water estimate (assumed, not measured)
TAN60 = math.tan(math.radians(60.0))
STAGES = (0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.5, 10.0, 15.0, 20.0)
FINE = np.round(0.1 * np.arange(1, 51), 2)       # 0.1 ... 5.0 m
FLAG_NODATA, FLAG_EDGE, FLAG_TWIN, FLAG_LOW, FLAG_FAR = 1, 2, 4, 8, 16
FLAG_FLATW, FLAG_PLANW = 32, 64                   # water at the channel point: hydro-flattened / planar (no drop)
DROP = FLAG_NODATA | FLAG_EDGE | FLAG_TWIN | FLAG_LOW | FLAG_FAR
NODATA_I16 = -32768


def grid() -> np.ndarray:
    """The stored profile's distance grid ``U`` (m from the channel, negative = left of the downstream direction):
    2 m from -500 to -152, 1 m from -150 to 150, 2 m from 152 to 500 (651 samples; ``U[CI] = 0``)."""
    o = np.arange(INNER_M + OUTER_DU, OUTER_M + 1, OUTER_DU, dtype=np.float64)
    return np.concatenate([-o[::-1], np.arange(-INNER_M, INNER_M + 1, dtype=np.float64), o])


U = grid()
CI = int(np.flatnonzero(U == 0)[0])
N_OUT = (OUTER_M - INNER_M) // OUTER_DU


def stage_tag(h: float) -> str:
    return XS.stage_tag(h)


# ---------------------------------------------------------------------------------------------- re-centring
@njit(cache=True)
def _recentre(Z, c, s, edge_n, ridge, twin):
    """Channel index (lowest point within ``s`` samples of the centre ``c``) and flags per profile row."""
    n, K = Z.shape
    j0 = np.full(n, -1, np.int64)
    fl = np.zeros(n, np.uint8)
    for i in range(n):
        best = np.inf
        jb = -1
        for j in range(c - s, c + s + 1):
            b = Z[i, j]
            if b != b:
                continue
            m = b
            k = 1
            if j > 0 and Z[i, j - 1] == Z[i, j - 1]:
                m += Z[i, j - 1]
                k += 1
            if j < K - 1 and Z[i, j + 1] == Z[i, j + 1]:
                m += Z[i, j + 1]
                k += 1
            m /= k
            if m < best:
                best = m
                jb = j
        if jb < 0:
            fl[i] = 1
            continue
        jj = jb
        zz = Z[i, jb]
        for j in (jb - 1, jb + 1):
            if c - s <= j <= c + s:
                v = Z[i, j]
                if v == v and v < zz:
                    zz = v
                    jj = j
        j0[i] = jj
        for j in range(max(0, c - s - edge_n), c - s):
            v = Z[i, j]
            if v == v and v < zz - 0.1:
                fl[i] |= 2
        for j in range(c + s + 1, min(K, c + s + edge_n + 1)):
            v = Z[i, j]
            if v == v and v < zz - 0.1:
                fl[i] |= 2
        for step in (-1, 1):
            mx = zz
            mn = np.inf
            j = jj + step
            while c - s <= j <= c + s:
                v = Z[i, j]
                if v == v:
                    if mx - zz >= ridge:
                        if v < mn:
                            mn = v
                    elif v > mx:
                        mx = v
                j += step
            if mn - zz <= twin:
                fl[i] |= 4
    return j0, fl


@njit(cache=True)
def _water_at(z, r, c, flat_tol=0.002, planar_tol=0.01):
    """``lidarpipe.water_flags`` at single cells: 1 flattened, 2 planar, 0 otherwise (or off the grid)."""
    nr, nc = z.shape
    out = np.zeros(len(r), np.uint8)
    for i in range(len(r)):
        rr, cc = r[i], c[i]
        if rr < 1 or cc < 1 or rr >= nr - 1 or cc >= nc - 1:
            continue
        v = z[rr, cc]
        if not np.isfinite(v):
            continue
        ok = True
        for dr in range(-1, 2):
            for dc in range(-1, 2):
                w = z[rr + dr, cc + dc]
                if not np.isfinite(w) or abs(w - v) >= flat_tol:
                    ok = False
        if ok:
            out[i] = 1
            continue
        l4 = z[rr - 1, cc] + z[rr + 1, cc] + z[rr, cc - 1] + z[rr, cc + 1] - 4 * v
        lx = z[rr - 1, cc - 1] + z[rr + 1, cc + 1] + z[rr - 1, cc + 1] + z[rr + 1, cc - 1] - 4 * v
        if np.isfinite(l4) and np.isfinite(lx) and abs(l4) < planar_tol and abs(lx) < planar_tol:
            out[i] = 2
    return out


@njit(cache=True, nogil=True)
def block_mean2(z):
    """2 m block mean of a 1 m grid (even shape), NaN where fewer than 2 of the 4 cells are valid."""
    H, W = z.shape
    out = np.full((H // 2, W // 2), np.nan, np.float32)
    for r in range(H // 2):
        for c in range(W // 2):
            s = 0.0
            k = 0
            for dr in range(2):
                for dc in range(2):
                    v = z[2 * r + dr, 2 * c + dc]
                    if np.isfinite(v):
                        s += v
                        k += 1
            if k >= 2:
                out[r, c] = s / k
    return out


# ---------------------------------------------------------------------------------------------- measures
@njit(cache=True)
def _prefix(us, zs, nk, T, L, M):
    T[0] = 0.0
    L[0] = 0.0
    M[0] = zs[0]
    for k in range(1, nk):
        du = us[k] - us[k - 1]
        dz = zs[k] - zs[k - 1]
        T[k] = T[k - 1] + du * 0.5 * (zs[k] + zs[k - 1])
        L[k] = L[k - 1] + math.sqrt(du * du + dz * dz)
        M[k] = max(M[k - 1], zs[k])


@njit(cache=True)
def _at(us, zs, nk, T, L, M, h):
    """Distance, area and wetted length out to where the ground first rises above ``h`` (censored at the end)."""
    if M[nk - 1] <= h:
        return us[nk - 1], h * us[nk - 1] - T[nk - 1], L[nk - 1], 1
    lo = 1
    hi = nk - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if M[mid] > h:
            hi = mid
        else:
            lo = mid + 1
    k = lo
    prev = zs[k - 1]
    v = zs[k]
    du = us[k] - us[k - 1]
    f = (h - prev) / (v - prev)
    return (us[k - 1] + f * du, h * us[k - 1] - T[k - 1] + 0.5 * f * du * (h - prev),
            L[k - 1] + f * math.sqrt(du * du + (v - prev) * (v - prev)), 0)


@njit(cache=True)
def _slope_fit(x, y):
    n = x.size
    mx = x.mean()
    my = y.mean()
    sxx = 0.0
    sxy = 0.0
    for i in range(n):
        sxx += (x[i] - mx) ** 2
        sxy += (x[i] - mx) * (y[i] - my)
    return sxy / sxx if sxx > 0 else np.nan


@njit(cache=True)
def _measure(P, U, ci, stages, fine, d_out, a_out, p_out, c_out, sc):
    """Per section (rows of ``P``: heights above the channel on grid ``U``): per side distance, area, perimeter and
    censoring at ``stages``; scalar columns ``sc`` (see ``SCALARS``)."""
    n, K = P.shape
    S = stages.size
    F = fine.size
    us = np.empty(K)
    zs = np.empty(K)
    T = np.empty(K)
    L = np.empty(K)
    M = np.empty(K)
    wf = np.empty(F)
    af = np.empty(F)
    dfs = np.empty((2, F))
    d025 = np.empty(2)
    lh = np.log(fine)
    for i in range(n):
        if P[i, ci] != P[i, ci]:
            continue
        miss = 0
        tot = 0
        for f in range(F):
            wf[f] = 0.0
            af[f] = 0.0
        stp = np.zeros(2)
        for side in range(2):
            nk = 0
            if side == 0:
                rng0, rng1, step = ci, -1, -1
            else:
                rng0, rng1, step = ci, K, 1
            j = rng0
            b50 = 0.0
            b150 = 0.0
            b500 = 0.0
            while j != rng1:
                v = P[i, j]
                if v != v:
                    v = 0.0
                    if j != ci:
                        miss += 1
                tot += 1
                u = abs(U[j])
                us[nk] = u
                zs[nk] = v
                nk += 1
                if u <= 50.0 and v > b50:
                    b50 = v
                if u <= 150.0 and v > b150:
                    b150 = v
                if v > b500:
                    b500 = v
                j += step
            _prefix(us, zs, nk, T, L, M)
            for s in range(S):
                d, a, p, c = _at(us, zs, nk, T, L, M, stages[s])
                d_out[i, side, s] = d
                a_out[i, s] += a
                p_out[i, s] += p
                c_out[i, s] += c << side
            for f in range(F):
                d, a, p, c = _at(us, zs, nk, T, L, M, fine[f])
                wf[f] += d
                af[f] += a
                dfs[side, f] = d
            d025[side] = _at(us, zs, nk, T, L, M, 0.25)[0]
            rise = 0.0
            steep = 0.0
            for k in range(1, nk):
                if M[k - 1] >= 5.0:
                    break
                lo = max(M[k - 1], 0.25)
                hi = min(M[k], 5.0)
                if hi > lo:
                    rise += hi - lo
                    if (zs[k] - zs[k - 1]) / (us[k] - us[k - 1]) > 1.7320508075688772:
                        steep += hi - lo
            stp[side] = steep / rise if rise > 0 else np.nan
            sc[i, 9 + side] = b50
            sc[i, 11 + side] = b150
            sc[i, 13 + side] = b500
        sc[i, 2] = stp[0]
        sc[i, 3] = stp[1]
        # shape exponents over 0.2-2.0 m (fine[1:20])
        sc[i, 4] = _slope_fit(lh[1:20], np.log(np.maximum(wf[1:20], 1e-3)))
        sc[i, 5] = _slope_fit(lh[1:20], np.log(np.maximum(af[1:20], 1e-4)))
        # bankfull: minimum of w / h over 0.3-5 m; a break only where the ratio rises 20 % above it afterwards
        jb = 2
        rb = wf[2] / fine[2]
        for f in range(3, F):
            r = wf[f] / fine[f]
            if r < rb:
                rb = r
                jb = f
        rmax = 0.0
        for f in range(jb + 1, F):
            r = wf[f] / fine[f]
            if r > rmax:
                rmax = r
        brk = fine[jb] <= 4.5 and rmax >= 1.2 * rb
        sc[i, 6] = fine[jb] if brk else np.nan
        sc[i, 7] = 1.0 if brk else 0.0
        sc[i, 8] = miss / max(tot - 2, 1)     # the channel sample is walked from both sides
        # bank angle of each side: from the 0.25 m water edge to the bankfull stage (1-3 m; 3 m without a break)
        jt = min(max(jb if brk else 29, 9), 29)
        for side in range(2):
            sc[i, side] = math.degrees(math.atan2(fine[jt] - 0.25, max(dfs[side, jt] - d025[side], 1e-6)))


SCALARS = ("ang_l", "ang_r", "steep_l", "steep_r", "b", "m", "hb", "brk", "nodata", "bank50_l", "bank50_r",
           "wall150_l", "wall150_r", "wall500_l", "wall500_r")


@njit(cache=True)
def _bankfull_widths(P, U, ci, hb, out):
    """Width, area and perimeter at ``hb`` and the width at ``2 hb`` per section (``out``: n x 4)."""
    n, K = P.shape
    us = np.empty(K)
    zs = np.empty(K)
    T = np.empty(K)
    L = np.empty(K)
    M = np.empty(K)
    for i in range(n):
        h = hb[i]
        if h != h or P[i, ci] != P[i, ci]:
            continue
        for side in range(2):
            nk = 0
            j = ci
            step = -1 if side == 0 else 1
            end = -1 if side == 0 else K
            while j != end:
                v = P[i, j]
                us[nk] = abs(U[j])
                zs[nk] = 0.0 if v != v else v
                nk += 1
                j += step
            _prefix(us, zs, nk, T, L, M)
            d, a, p, c = _at(us, zs, nk, T, L, M, h)
            out[i, 0] += d
            out[i, 1] += a
            out[i, 2] += p
            out[i, 3] += _at(us, zs, nk, T, L, M, 2.0 * h)[0]


def measure(P: np.ndarray, u: np.ndarray = U, ci: int = CI, stages=STAGES) -> dict:
    """Section measures from profiles ``P`` (n, len(u)): heights above the channel (m, NaN = no data) on the grid
    ``u`` with the channel at ``u[ci] = 0``. Returns per side distances ``d`` (n, 2, stages; side 0 = negative
    ``u``), ``w`` = their sum, ``a``, ``p`` (n, stages), ``cens`` (n, stages; bit 1 left, bit 2 right censored), the
    ``SCALARS`` and the bankfull set: ``hb_use`` (the bankfull stage, or 5 m where there is no break), ``wbf``,
    ``abf``, ``pbf``, ``wfp`` (width at 2 ``hb_use``), ``er`` = wfp / wbf, ``ffbf`` = abf / (wbf hb_use),
    ``wd_bf`` = wbf / hb_use, ``rbf`` = abf / pbf, plus ``ff1`` (form factor at 1 m), ``w0`` (width at 0.1 m: the
    water surface), ``asym_w`` (|left - right| / width at 2 m) and ``asym_ang`` (|left - right| bank angle, deg).
    Rows whose channel sample is NaN are NaN."""
    n = P.shape[0]
    st = np.asarray(stages, dtype=np.float64)
    S = len(st)
    P = np.ascontiguousarray(P, dtype=np.float32)
    d = np.zeros((n, 2, S))
    a = np.zeros((n, S))
    p = np.zeros((n, S))
    c = np.zeros((n, S), np.int8)
    sc = np.full((n, len(SCALARS)), np.nan)
    _measure(P, np.asarray(u, np.float64), int(ci), st, FINE.astype(np.float64), d, a, p, c, sc)
    bad = ~np.isfinite(P[:, ci])
    d[bad] = np.nan
    a[bad] = np.nan
    p[bad] = np.nan
    out = {"d": d, "w": d.sum(axis=1), "a": a, "p": p, "cens": c}
    for k, name in enumerate(SCALARS):
        out[name] = sc[:, k]
    hb_use = np.where(out["brk"] == 1, out["hb"], 5.0)
    hb_use[bad] = np.nan
    bw = np.zeros((n, 4))
    _bankfull_widths(P, np.asarray(u, np.float64), int(ci), hb_use, bw)
    bw[bad] = np.nan
    j1, j2, j0 = st.tolist().index(1.0), st.tolist().index(2.0), st.tolist().index(0.1)
    with np.errstate(invalid="ignore", divide="ignore"):
        out.update(hb_use=hb_use, wbf=bw[:, 0], abf=bw[:, 1], pbf=bw[:, 2], wfp=bw[:, 3], er=bw[:, 3] / bw[:, 0],
                   ffbf=bw[:, 1] / (bw[:, 0] * hb_use), wd_bf=bw[:, 0] / hb_use, rbf=bw[:, 1] / bw[:, 2],
                   ff1=a[:, j1] / (out["w"][:, j1] * 1.0), w0=out["w"][:, j0],
                   asym_w=np.abs(d[:, 0, j2] - d[:, 1, j2]) / out["w"][:, j2],
                   asym_ang=np.abs(out["ang_l"] - out["ang_r"]))
    return out


# ---------------------------------------------------------------------------------------------- long profile
def _window_min(z1, tf1, x, y, rad: int) -> np.ndarray:
    """Lowest finite 1 m cell within ``rad`` cells of each point (NaN where none)."""
    r = np.floor((tf1.f - y) / -tf1.e).astype(np.int64)
    c = np.floor((x - tf1.c) / tf1.a).astype(np.int64)
    out = np.full(len(r), np.inf, np.float32)
    for dr in range(-rad, rad + 1):
        for dc in range(-rad, rad + 1):
            rr = np.clip(r + dr, 0, z1.shape[0] - 1)
            cc = np.clip(c + dc, 0, z1.shape[1] - 1)
            v = z1[rr, cc]
            out = np.fmin(out, np.where(np.isfinite(v), v, np.inf))
    return np.where(np.isfinite(out), out, np.nan)


def profile_metrics(s: np.ndarray, z: np.ndarray) -> tuple[float, float, float, float]:
    """(robust spread of the linear-detrended profile m, share of the drop in the steepest 10 % of the length,
    gradient m/km, length m) of a long profile ``z`` at distances ``s`` (downstream)."""
    ok = np.isfinite(z)
    if ok.sum() < 20 or s[ok][-1] - s[ok][0] < 20:
        return (np.nan, np.nan, np.nan, np.nan)
    s, z = s[ok], z[ok]
    A = np.column_stack([np.ones_like(s), s])
    coef = np.linalg.lstsq(A, z, rcond=None)[0]
    res = z - A @ coef
    q75, q25 = np.percentile(res, [75, 25])
    rough = (q75 - q25) / 1.349
    zm = np.minimum.accumulate(z)
    steps = np.clip(-np.diff(zm), 0, None)
    drop = float(zm[0] - zm[-1])
    L = float(s[-1] - s[0])
    k = max(1, int(round(0.1 * len(steps))))
    conc = float(np.sort(steps)[::-1][:k].sum() / drop) if drop >= 0.5 else np.nan
    return (float(rough), conc, drop / L * 1000.0, L)


def long_profiles(z1, tf1, reach: np.ndarray, s: np.ndarray, cx: np.ndarray, cy: np.ndarray, ok: np.ndarray,
                  rad: int = 2) -> pd.DataFrame:
    """Per reach (index ``reach``), the 1 m long profile through its valid channel points in station order, every
    1 m, lowest cell within ``rad`` m: ``rough``, ``dropconc``, ``grad1m`` (m/km), ``prof_len`` (m)."""
    rows = []
    o = np.lexsort((s, reach))
    rr, xx, yy, kk = reach[o], cx[o], cy[o], ok[o]
    bounds = np.flatnonzero(np.r_[True, rr[1:] != rr[:-1], True])
    for b0, b1 in zip(bounds[:-1], bounds[1:], strict=True):
        m = kk[b0:b1]
        if m.sum() < 2:
            continue
        px, py = xx[b0:b1][m], yy[b0:b1][m]
        seg = np.hypot(np.diff(px), np.diff(py))
        cum = np.r_[0.0, np.cumsum(seg)]
        if cum[-1] < 20:
            continue
        sd = np.arange(0.0, cum[-1], 1.0)
        qx, qy = np.interp(sd, cum, px), np.interp(sd, cum, py)
        zp = _window_min(z1, tf1, qx, qy, rad)
        rough, conc, grad, L = profile_metrics(sd, zp.astype(np.float64))
        rows.append((int(rr[b0]), rough, conc, grad, L))
    return pd.DataFrame(rows, columns=["reach", "rough", "dropconc", "grad1m", "prof_len"])


# ---------------------------------------------------------------------------------------------- extraction
def _sample(z, tf, x, y, nx, ny, half: int, du: float) -> np.ndarray:
    return XS.sample_profiles(z, tf, x, y, nx, ny, half, du)


def refine_normals(reach, s, cx, cy, ok, nx, ny, reach_span: float = 50.0, max_turn: float = 75.0):
    """Normals perpendicular to the chord between the valid channel points of the neighbouring sections of the
    same reach (``reach_span`` m apart, else the nearer pair); the original normal where there is no such pair or the
    turn exceeds ``max_turn`` degrees. Sign kept (left stays left)."""
    n = len(reach)
    o = np.lexsort((s, reach))
    nx2, ny2 = nx.copy(), ny.copy()
    pos = np.empty(n, np.int64)
    pos[o] = np.arange(n)
    rs, ss, xs, ys, ks = reach[o], s[o], cx[o], cy[o], ok[o]
    cosmax = math.cos(math.radians(max_turn))
    for q in range(n):
        if not ks[q]:
            continue
        best = None
        for span in (reach_span, reach_span / 2):
            lo = q
            while lo - 1 >= 0 and rs[lo - 1] == rs[q] and ss[q] - ss[lo - 1] <= span + 1e-6:
                lo -= 1
            hi = q
            while hi + 1 < n and rs[hi + 1] == rs[q] and ss[hi + 1] - ss[q] <= span + 1e-6:
                hi += 1
            a = next((k for k in range(lo, q + 1) if ks[k]), None)
            b = next((k for k in range(hi, q - 1, -1) if ks[k]), None)
            if a is not None and b is not None and b > a and ss[b] - ss[a] >= 20.0:
                best = (a, b)
                break
        if best is None:
            continue
        a, b = best
        tx, ty = xs[b] - xs[a], ys[b] - ys[a]
        L = math.hypot(tx, ty)
        if L < 10.0:
            continue
        mx, my = -ty / L, tx / L
        i = o[q]
        dot = mx * nx[i] + my * ny[i]
        if abs(dot) < cosmax:
            continue
        if dot < 0:
            mx, my = -mx, -my
        nx2[i], ny2[i] = mx, my
    return nx2, ny2


def low_flags(reach, s, z0, ok, low_m: float = LOW_M) -> np.ndarray:
    """Sections whose channel height is more than max(``low_m``, 4 MAD) off a least-squares line through the reach's
    valid bottoms (reaches with 4 or more): they found another channel."""
    out = np.zeros(len(reach), bool)
    df = pd.DataFrame({"r": reach, "s": s, "z": z0, "ok": ok})
    for _, g in df[df["ok"]].groupby("r"):
        if len(g) < 4:
            continue
        A = np.column_stack([np.ones(len(g)), g["s"].to_numpy()])
        z = g["z"].to_numpy()
        res = z - A @ np.linalg.lstsq(A, z, rcond=None)[0]
        mad = np.median(np.abs(res - np.median(res)))
        bad = np.abs(res) > max(low_m, 4.0 * 1.4826 * mad)
        out[g.index.to_numpy()[bad]] = True
    return out


def extract(z1, tf1, z2, tf2, reach, s, x, y, nx, ny, search: int = SEARCH_M, refine: bool = True) -> dict:
    """Sections on a 1 m DEM ``z1`` (NaN = no data) and its 2 m block mean ``z2``, from stations on routed lines.

    Returns per section: ``reach``, ``s``, ``x``, ``y`` (station), ``cx``, ``cy`` (final channel point), ``nx``,
    ``ny`` (final normal), ``off_line`` (pass-1 channel offset from the line, m), ``z0``, ``flags`` and ``prof``
    (n, len(U) float32 heights above ``z0``)."""
    k1 = search + EDGE_M + 2
    Z = _sample(z1, tf1, x, y, nx, ny, k1, 1.0)
    j0, fl = _recentre(Z, k1, search, EDGE_M, RIDGE_M, TWIN_M)
    del Z
    off1 = np.where(j0 >= 0, j0 - k1, 0).astype(np.float64)
    cx, cy = x + off1 * nx, y + off1 * ny
    ok1 = (fl & DROP) == 0
    if refine:
        nx2, ny2 = refine_normals(reach, s, cx, cy, ok1, nx, ny)
        k2 = REFINE_M + EDGE_M + 2
        Z2 = _sample(z1, tf1, cx, cy, nx2, ny2, k2, 1.0)
        j2, fl2 = _recentre(Z2, k2, REFINE_M, EDGE_M, RIDGE_M, TWIN_M)
        del Z2
        fl |= np.where((fl2 & (FLAG_NODATA | FLAG_EDGE)) != 0, FLAG_FAR, 0).astype(np.uint8)
        fl |= (fl2 & FLAG_TWIN)
        off2 = np.where(j2 >= 0, j2 - k2, 0).astype(np.float64)
        fx, fy = cx + off2 * nx2, cy + off2 * ny2
    else:
        nx2, ny2, fx, fy = nx, ny, cx, cy
    inner = _sample(z1, tf1, fx, fy, nx2, ny2, INNER_M, 1.0)
    outer = _sample(z2, tf2, fx, fy, nx2, ny2, OUTER_M // OUTER_DU, float(OUTER_DU))
    no = OUTER_M // OUTER_DU
    prof = np.concatenate([outer[:, :N_OUT], inner, outer[:, no + 1 + (INNER_M // OUTER_DU):]], axis=1)
    del inner, outer
    z0 = prof[:, CI].astype(np.float64)
    fl |= np.where(~np.isfinite(z0), FLAG_NODATA, 0).astype(np.uint8)
    prof -= z0[:, None].astype(np.float32)
    ok = (fl & DROP) == 0
    low = low_flags(reach, s, z0, ok)
    fl |= np.where(low, FLAG_LOW, 0).astype(np.uint8)
    r = np.floor((tf1.f - fy) / -tf1.e).astype(np.int64)
    c = np.floor((fx - tf1.c) / tf1.a).astype(np.int64)
    w = _water_at(z1, r, c)
    fl |= np.where(w == 1, FLAG_FLATW, np.where(w == 2, FLAG_PLANW, 0)).astype(np.uint8)
    return {"reach": reach, "s": s, "x": x, "y": y, "cx": fx, "cy": fy, "nx": nx2, "ny": ny2, "off_line": off1,
            "z0": z0, "flags": fl, "prof": prof}


def section_table(sec: dict, m: dict) -> pd.DataFrame:
    """One row per section: position, flags and the measures (stage widths, areas, perimeters, scalars)."""
    cols = {k: sec[k] for k in ("reach", "s", "cx", "cy", "nx", "ny", "off_line", "z0", "flags")}
    for j, h in enumerate(STAGES):
        tg = stage_tag(h)
        cols[f"w{tg}"] = m["w"][:, j]
        cols[f"dl{tg}"] = m["d"][:, 0, j]
        cols[f"a{tg}"] = m["a"][:, j]
        cols[f"p{tg}"] = m["p"][:, j]
        cols[f"c{tg}"] = m["cens"][:, j]
    for k in (*SCALARS, "hb_use", "wbf", "abf", "pbf", "wfp", "er", "ffbf", "wd_bf", "rbf", "ff1", "w0", "asym_w",
              "asym_ang"):
        cols[k] = m[k]
    df = pd.DataFrame(cols)
    for c in df.columns:
        if df[c].dtype == np.float64 and c not in ("cx", "cy", "z0"):
            df[c] = df[c].astype(np.float32)
    return df


# ---------------------------------------------------------------------------------------------- per reach
def reach_table(sec: pd.DataFrame, n_reaches: int, profiles: pd.DataFrame | None = None,
                every: int = 1) -> pd.DataFrame:
    """Per reach summaries of its valid sections (row i = reach i): medians and spreads of the section measures,
    stage tables (median width, area, perimeter; minimum width), the share of dropped sections and of water at the
    channel, and the long-profile metrics. ``every`` = 2 keeps every other section (50 m spacing)."""
    sec = sec.copy()
    if every > 1:
        sec["k"] = sec.groupby("reach").cumcount()
        sec = sec[sec["k"] % every == 0]
    idx = pd.RangeIndex(n_reaches)
    cols: dict[str, np.ndarray] = {}

    def put(name, s, dtype=np.float32):
        cols[name] = s.reindex(idx).to_numpy(dtype=np.float64).astype(dtype)

    fl = sec["flags"].to_numpy()
    ok = (fl & DROP) == 0
    n_all = sec.groupby("reach").size()
    n_ok = sec[ok].groupby("reach").size()
    put("x1_n_all", n_all.reindex(idx).fillna(0), np.int16)
    put("x1_n", n_ok.reindex(idx).fillna(0), np.int16)
    put("x1_drop_share", 1.0 - n_ok.reindex(idx).fillna(0) / n_all.reindex(idx))
    for name, bit in (("edge", FLAG_EDGE), ("twin", FLAG_TWIN), ("low", FLAG_LOW), ("far", FLAG_FAR)):
        put(f"x1_{name}_share", pd.Series((fl & bit) != 0).groupby(sec["reach"].to_numpy()).mean())
    v = sec[ok].copy()
    vf = v["flags"].to_numpy()
    extra = {"flatw": ((vf & FLAG_FLATW) != 0).astype(float), "planw": ((vf & FLAG_PLANW) != 0).astype(float),
             "aoff": np.abs(v["off_line"]), "ang": 0.5 * (v["ang_l"] + v["ang_r"]),
             "ang_hi": np.fmax(v["ang_l"], v["ang_r"]), "ang_lo": np.fmin(v["ang_l"], v["ang_r"]),
             "steep": v[["steep_l", "steep_r"]].mean(axis=1), "steep_hi": np.fmax(v["steep_l"], v["steep_r"]),
             "wall_lo": np.fmin(v["wall150_l"], v["wall150_r"]), "wall_hi": np.fmax(v["wall150_l"], v["wall150_r"]),
             "bank_lo": np.fmin(v["bank50_l"], v["bank50_r"]),
             "vf": 2.0 * v["w2"] / np.maximum(v["wall500_l"] + v["wall500_r"], 1.0)}
    for h in STAGES:
        tg = stage_tag(h)
        extra[f"cz{tg}"] = (v[f"c{tg}"] > 0).astype(float)
    v = pd.concat([v, pd.DataFrame(extra, index=v.index)], axis=1)
    g = v.groupby("reach")
    med = g.median(numeric_only=True)
    mean = g.mean(numeric_only=True)
    for h in STAGES:
        tg = stage_tag(h)
        for k in ("w", "a", "p"):
            put(f"x1_{k}{tg}_med", med[f"{k}{tg}"])
        put(f"x1_w{tg}_min", g[f"w{tg}"].min())
        put(f"x1_cens{tg}", mean[f"cz{tg}"])
    for k in ("w0", "b", "m", "ff1", "ffbf", "hb_use", "wbf", "abf", "pbf", "wd_bf", "rbf", "er", "ang", "ang_hi",
              "ang_lo", "steep", "steep_hi", "asym_w", "asym_ang", "wall_lo", "wall_hi", "bank_lo", "vf", "aoff"):
        put(f"x1_{k}_med", med[k])
    put("x1_hb_med", med["hb"])
    put("x1_brk_frac", mean["brk"])
    put("x1_flatw_frac", mean["flatw"])
    put("x1_planw_frac", mean["planw"])
    put("x1_nodata", mean["nodata"])
    # along-reach variability
    put("x1_b_iqr", g["b"].quantile(0.75) - g["b"].quantile(0.25))
    put("x1_w1_cv", g["w1"].std() / mean["w1"])
    put("x1_hb_cv", g["hb_use"].std() / mean["hb_use"])
    put("x1_pinch_frac", (v["w1"] < 0.5 * v["reach"].map(med["w1"])).groupby(v["reach"]).mean())
    p = profiles.set_index("reach") if profiles is not None and len(profiles) else None
    for k in ("rough", "dropconc", "grad1m", "prof_len"):
        if p is not None:
            put(f"x1_{k}", p[k])
        else:
            cols[f"x1_{k}"] = np.full(n_reaches, np.nan, np.float32)
    out = pd.DataFrame(cols, index=idx)
    return out


# ---------------------------------------------------------------------------------------------- hydraulics
def hydraulics(t: pd.DataFrame, q_col: str = "maf_m3s", s_col: str = "grad_s", estimate: bool = False,
               n: float = XS.MANNING_N) -> pd.DataFrame:
    """Manning flood stage, width, area, velocity and unit stream power on the reach's median 1 m section
    (``xsec.hydraulics`` on the ``x1_*_med`` stage tables, stage floor 0.1 m). With ``estimate``, an under-water
    rectangle is added to every stage's area and perimeter: water width ``w_u = max(x1_w0_med, 1 m)`` times the
    Manning depth of a capture flow of ``CAPTURE_SHARE`` x the flood (an estimate; LiDAR sees only the surface)."""
    tags = [stage_tag(h) for h in STAGES]
    tmp = pd.DataFrame(index=t.index)
    S = np.maximum(t[s_col].to_numpy(dtype=float), 0.1) / 1000.0
    Q = t[q_col].to_numpy(dtype=float)
    if estimate:
        wu = np.maximum(t["x1_w0_med"].to_numpy(dtype=float), 1.0)
        du = (CAPTURE_SHARE * Q * n / (wu * np.sqrt(S))) ** 0.6
        au, pu = wu * du, 2.0 * du
    else:
        au = pu = np.zeros(len(t))
    for g in tags:
        tmp[f"xs_a{g}_med"] = t[f"x1_a{g}_med"].to_numpy(dtype=float) + au
        tmp[f"xs_p{g}_med"] = t[f"x1_p{g}_med"].to_numpy(dtype=float) + pu
        tmp[f"xs_w{g}_med"] = t[f"x1_w{g}_med"].to_numpy(dtype=float)
    tmp["q"] = Q
    tmp["s"] = t[s_col].to_numpy(dtype=float)
    h = XS.hydraulics(tmp, q_col="q", s_col="s", stages=STAGES, n=n, h_min=0.1)
    if estimate:
        h["xs_d_u"] = du
    return h


def derive(f: pd.DataFrame) -> pd.DataFrame:
    """Flood hydraulics from the joined ``x1_*`` columns, ``maf_m3s`` and ``grad_s`` (in place):
    ``x1_h_q``, ``x1_w_q``, ``x1_v_q``, ``x1_omega_q`` as measured and ``..._est`` with the under-water estimate."""
    for est, suf in ((False, ""), (True, "_est")):
        h = hydraulics(f, estimate=est)
        for c, k in (("xs_h_q", "h_q"), ("xs_w_q", "w_q"), ("xs_v_q", "v_q"), ("xs_omega_q", "omega_q")):
            f[f"x1_{k}{suf}"] = h[c].to_numpy(dtype=np.float32)
        if est:
            f["x1_d_u"] = h["xs_d_u"].to_numpy(dtype=np.float32)
    return f


# ---------------------------------------------------------------------------------------------- reading
def out_dir(out: Path | None = None) -> Path:
    from .regions import REPO_ROOT

    d = Path(out) if out is not None else REPO_ROOT / "data" / "analysis" / "lidar-xsec"
    d.mkdir(parents=True, exist_ok=True)
    return d


def corridor_jobs(sheets: list[dict], box, px: np.ndarray, py: np.ndarray, radius: float, win: int = 2048,
                  block: int = BLOCK, cell: int = 64) -> list[tuple]:
    """Read jobs (as ``lidarpipe.sheet_windows``) for the 1 m blocks within ``radius`` of the points ``px, py``
    inside ``box``: per 2048 window of a sheet, the bounding rectangle of its needed 512 blocks."""
    from scipy import ndimage

    bx0, by0, bx1, by1 = (int(round(v)) for v in box)
    H, W = math.ceil((by1 - by0) / cell), math.ceil((bx1 - bx0) / cell)
    m = np.zeros((H, W), bool)
    r = ((by1 - py) // cell).astype(np.int64)
    c = ((px - bx0) // cell).astype(np.int64)
    k = (r >= 0) & (r < H) & (c >= 0) & (c < W)
    m[r[k], c[k]] = True
    need = ndimage.distance_transform_edt(~m) * cell <= radius + cell
    jobs = []
    for sh in sheets:
        sx0, sy0, sx1, sy1 = (int(round(v)) for v in sh["bounds"])
        ix0, iy0, ix1, iy1 = max(bx0, sx0), max(by0, sy0), min(bx1, sx1), min(by1, sy1)
        if ix0 >= ix1 or iy0 >= iy1:
            continue
        c_lo, c_hi = ix0 - sx0, ix1 - sx0
        r_lo, r_hi = sy1 - iy1, sy1 - iy0
        for wr in range((r_lo // win) * win, r_hi, win):
            for wc in range((c_lo // win) * win, c_hi, win):
                rows, cols = [], []
                for br in range(wr, min(wr + win, r_hi), block):
                    for bc in range(wc, min(wc + win, c_hi), block):
                        ra, rb = max(br, r_lo), min(br + block, r_hi)
                        ca, cb = max(bc, c_lo), min(bc + block, c_hi)
                        if ra >= rb or ca >= cb:
                            continue
                        # this block piece in box pixels (rows from the top, columns from the left) -> mask cells
                        mr0, mr1 = (by1 - (sy1 - ra)) // cell, (by1 - (sy1 - rb) - 1) // cell
                        mc0, mc1 = (sx0 + ca - bx0) // cell, (sx0 + cb - 1 - bx0) // cell
                        if need[max(mr0, 0):mr1 + 1, max(mc0, 0):mc1 + 1].any():
                            rows += [ra, rb]
                            cols += [ca, cb]
                if not rows:
                    continue
                ra, rb, ca, cb = min(rows), max(rows), min(cols), max(cols)
                jobs.append((sh["href"], ca, ra, cb - ca, rb - ra, by1 - (sy1 - ra), (sx0 + ca) - bx0))
    return jobs


def read_corridor(sheets, box, px, py, radius: float, threads: int = 32, log=None):
    """The 1 m mosaic over ``box`` (whole metres) read only in the corridor blocks; NaN elsewhere."""
    from rasterio.transform import from_origin

    from . import lidarpipe as LP

    bx0, by0, bx1, by1 = (int(round(v)) for v in box)
    z = np.full((by1 - by0, bx1 - bx0), np.nan, np.float32)
    jobs = corridor_jobs(sheets, box, px, py, radius)

    def consume(w, br, bc):
        z[br:br + w.shape[0], bc:bc + w.shape[1]] = w

    st = LP.stream(jobs, consume, threads=threads, log=log, every=400) if jobs else {"windows": 0, "cells": 0,
                                                                                        "seconds": 0.0}
    st["box_cells"] = int(z.size)
    return z, from_origin(bx0, by1, 1, 1), st


def tile_sheets(tile, out: Path) -> list[dict]:
    """The mosaic sheets intersecting a tile's core plus 1 km (cached in ``<out>/sheets/<tile>.json``)."""
    from . import lidarpipe as LP
    from .regions import REPO_ROOT

    p = out / "sheets" / f"{tile.id}.json"
    if p.exists():
        return json.loads(p.read_text())
    x0, y0, x1, y1 = tile.core
    sh = LP.mosaic_sheets((x0 - 1000, y0 - 1000, x1 + 1000, y1 + 1000), REPO_ROOT / "data" / "_stac")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sh))
    return sh


def tile_reaches(tile, min_area_km2: float = MIN_AREA_KM2, min_lidar: float = MIN_LIDAR_SHARE):
    """The tile's core reaches of ``min_area_km2`` or more with at least ``min_lidar`` of the line under LiDAR
    (``data/analysis/lidar/reach_coverage.pkl``)."""
    import pyogrio

    from .national import nz_dir
    from .regions import REPO_ROOT

    g = pyogrio.read_dataframe(nz_dir() / "tiles" / tile.id / "segments_core.gpkg",
                               columns=["gid", "area_km2", "length_m"])
    g = g[g["area_km2"] >= min_area_km2].reset_index(drop=True)
    cov = REPO_ROOT / "data" / "analysis" / "lidar" / "reach_coverage.pkl"
    if cov.exists():
        c = pd.read_pickle(cov).set_index("gid")["lidar_share"]
        g["lidar_share"] = c.reindex(g["gid"].to_numpy()).fillna(0.0).to_numpy()
        g = g[g["lidar_share"] >= min_lidar].reset_index(drop=True)
    return g


def _maxrss_gb() -> float:
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6


def run_tile(tile_id: str, out: Path | None = None, threads: int = 32, keep_profiles: bool = False,
             chunk_m: float = CHUNK_M, refine: bool = True, log=print) -> dict | None:
    """Sections of one national tile's LiDAR-covered core reaches of 2 km2 or more (see the module doc). Skips a
    tile whose ``<tile>_reach.pkl`` exists."""
    from concurrent.futures import ThreadPoolExecutor

    import shapely

    from .nz import Tile

    od = out_dir(out) / "tiles"
    od.mkdir(parents=True, exist_ok=True)
    dst = od / f"{tile_id}_reach.pkl"
    if dst.exists():
        return None
    tile = Tile.parse(tile_id)
    t0 = time.time()
    g = tile_reaches(tile)
    stats = {"tile": tile_id, "reaches": int(len(g))}
    if not len(g):
        pd.DataFrame({"gid": []}).to_pickle(dst)
        (od / f"{tile_id}.json").write_text(json.dumps(stats))
        log(f"[{tile_id}] no LiDAR-covered reaches")
        return stats
    sheets = tile_sheets(tile, out_dir(out))
    geoms = g.geometry.to_numpy()
    ri, s, x, y, nx, ny = XS.stations(geoms, SPACING_M, TANGENT_M)
    mid = shapely.get_coordinates(shapely.line_interpolate_point(geoms, 0.5, normalized=True))
    cx0, cy0, _, _ = tile.core
    nch = int(round((tile.core[2] - tile.core[0]) / chunk_m))
    kx = np.clip(((mid[:, 0] - cx0) // chunk_m).astype(int), 0, nch - 1)
    ky = np.clip(((mid[:, 1] - cy0) // chunk_m).astype(int), 0, nch - 1)
    chunk_of = (ky * nch + kx)[ri]
    reach_r = OUTER_M + SEARCH_M + 15.0
    chunks = [k for k in range(nch * nch) if (chunk_of == k).any()]

    def chunk_box(sel):
        ex = np.concatenate([x[sel] - reach_r, x[sel] + reach_r])
        ey = np.concatenate([y[sel] - reach_r, y[sel] + reach_r])
        bx0, by0 = 2 * math.floor(ex.min() / 2) - 2, 2 * math.floor(ey.min() / 2) - 2
        bx1, by1 = 2 * math.ceil(ex.max() / 2) + 2, 2 * math.ceil(ey.max() / 2) + 2
        return (bx0, by0, bx1, by1)

    def load(k):
        sel = chunk_of == k
        box = chunk_box(sel)
        shs = [sh for sh in sheets if not (sh["bounds"][2] <= box[0] or sh["bounds"][0] >= box[2]
                                           or sh["bounds"][3] <= box[1] or sh["bounds"][1] >= box[3])]
        return (sel, box) + read_corridor(shs, box, x[sel], y[sel], reach_r, threads=threads)

    secs, profs, longs, rd = [], [], [], []
    t_read = t_meas = 0.0
    with ThreadPoolExecutor(1) as ex:
        nxt = ex.submit(load, chunks[0])
        for q in range(len(chunks)):
            tr = time.time()
            sel, box, z1, tf1, st = nxt.result()
            t_read += time.time() - tr
            nxt = ex.submit(load, chunks[q + 1]) if q + 1 < len(chunks) else None
            tm = time.time()
            from rasterio.transform import from_origin

            z2 = block_mean2(z1)
            tf2 = from_origin(tf1.c, tf1.f, 2, 2)
            idx = np.flatnonzero(sel)
            sec = extract(z1, tf1, z2, tf2, ri[idx], s[idx], x[idx], y[idx], nx[idx], ny[idx], refine=refine)
            del z2
            m = measure(sec["prof"])
            tab = section_table(sec, m)
            tab["sec_id"] = idx
            secs.append(tab)
            ok = (sec["flags"] & DROP) == 0
            longs.append(long_profiles(z1, tf1, sec["reach"], sec["s"], sec["cx"], sec["cy"], ok))
            if keep_profiles:
                profs.append((idx, sec["z0"], sec["flags"], sec["prof"]))
            del z1, sec, m
            t_meas += time.time() - tm
            rd.append(st)
            log(f"[{tile_id}] chunk {q + 1}/{len(chunks)}: {int(sel.sum())} sections, read {st['cells'] / 1e6:.0f} "
                f"of {st['box_cells'] / 1e6:.0f} M cells in {st['seconds']:.0f} s, max RSS {_maxrss_gb():.1f} GB")
    sec = pd.concat(secs, ignore_index=True).sort_values("sec_id").reset_index(drop=True)
    lp = pd.concat(longs, ignore_index=True)
    rt = reach_table(sec, len(g), lp)
    rt.insert(0, "gid", g["gid"].to_numpy())
    sec.insert(0, "gid", g["gid"].to_numpy()[sec["reach"].to_numpy()])
    sec.to_parquet(od / f"{tile_id}_sec.parquet", compression="zstd")
    if keep_profiles:
        write_profiles(od / f"{tile_id}_prof.npz", profs, sec, g)
    rt.to_pickle(dst)
    stats.update(sections=int(len(sec)), valid=int(((sec["flags"].to_numpy() & DROP) == 0).sum()),
                 cells_read=int(sum(r["cells"] for r in rd)), read_s=round(sum(r["seconds"] for r in rd), 1),
                 wait_read_s=round(t_read, 1), measure_s=round(t_meas, 1), total_s=round(time.time() - t0, 1),
                 maxrss_gb=round(_maxrss_gb(), 2), chunks=len(chunks))
    (od / f"{tile_id}.json").write_text(json.dumps(stats, indent=1))
    log(f"[{tile_id}] {stats['reaches']} reaches, {stats['sections']} sections ({stats['valid']} valid): read "
        f"{stats['cells_read'] / 1e9:.2f} G cells in {stats['read_s']:.0f} s, total {stats['total_s']:.0f} s")
    return stats


def quantise(prof: np.ndarray) -> np.ndarray:
    """Heights above the channel as int16 centimetres (clipped to +-327.67 m; no data = -32768): the stored form."""
    q = np.clip(np.round(prof * 100.0), -32767, 32767)
    q[~np.isfinite(prof)] = NODATA_I16
    return q.astype(np.int16)


def dequantise(q: np.ndarray) -> np.ndarray:
    p = q.astype(np.float32) / 100.0
    p[q == NODATA_I16] = np.nan
    return p


def _outward(q: np.ndarray, ci: int) -> tuple[np.ndarray, np.ndarray]:
    return q[:, :ci + 1][:, ::-1], q[:, ci:]


def encode(q: np.ndarray, ci: int = CI) -> tuple[np.ndarray, np.ndarray]:
    """Stored int16 profiles (``quantise``) -> (per-side differences walking out from the channel, left side first,
    ``ci`` values per side, int16; the no-data mask packed to bits). No-data samples repeat the last valid height
    outward (difference 0), so the differences stay small and compress ~1.6x better than the heights (zstd)."""
    nd = q == NODATA_I16
    out = []
    for side, m in zip(_outward(q.astype(np.int32), ci), _outward(nd, ci), strict=True):
        idx = np.where(~m, np.arange(m.shape[1])[None, :], 0)
        np.maximum.accumulate(idx, axis=1, out=idx)
        v = np.take_along_axis(side, idx, axis=1)
        v[:, 0] = np.where(m[:, 0], 0, v[:, 0])
        v = np.where(m[:, :1] & (idx == 0), 0, v)
        out.append(np.diff(v, axis=1))
    d = np.clip(np.concatenate(out, axis=1), -32767, 32767).astype(np.int16)
    return d, np.packbits(nd, axis=1)


def decode(d: np.ndarray, mask: np.ndarray, ci: int = CI) -> np.ndarray:
    """Inverse of ``encode``: the int16 profiles (channel sample 0, no data = ``NODATA_I16``)."""
    n = len(d)
    left = np.cumsum(d[:, :ci].astype(np.int32), axis=1)[:, ::-1]
    right = np.cumsum(d[:, ci:].astype(np.int32), axis=1)
    q = np.concatenate([left, np.zeros((n, 1), np.int32), right], axis=1).astype(np.int16)
    nd = np.unpackbits(mask, axis=1, count=q.shape[1]).astype(bool)
    q[nd] = NODATA_I16
    return q


def write_profiles(path: Path, parts, sec: pd.DataFrame, g) -> None:
    idx = np.concatenate([p[0] for p in parts])
    o = np.argsort(idx)
    prof = np.concatenate([p[3] for p in parts])[o]
    np.savez_compressed(path, gid=g["gid"].to_numpy()[sec["reach"].to_numpy()], s=sec["s"].to_numpy(np.float32),
                        cx=sec["cx"].to_numpy(), cy=sec["cy"].to_numpy(), nx=sec["nx"].to_numpy(np.float32),
                        ny=sec["ny"].to_numpy(np.float32), z0=sec["z0"].to_numpy(np.float32),
                        flags=sec["flags"].to_numpy(np.uint8), prof=quantise(prof), u=U.astype(np.float32))


def run_tiles(tiles: list[str], out: Path | None = None, threads: int = 32, keep_profiles: list[str] | None = None,
              log=print) -> Path:
    for t in tiles:
        try:
            run_tile(t, out, threads=threads, keep_profiles=t in (keep_profiles or []), log=log)
        except Exception as e:  # keep going; a failed tile is re-run next time
            log(f"[{t}] FAILED: {e!r}")
    return merge(out, log=log)


def merge(out: Path | None = None, log=print) -> Path:
    od = out_dir(out)
    paths = sorted((od / "tiles").glob("*_reach.pkl"))
    parts = [pd.read_pickle(p) for p in paths]
    df = pd.concat([p for p in parts if len(p)], ignore_index=True) if parts else pd.DataFrame()
    dst = od / "reach1m.pkl"
    df.to_pickle(dst)
    log(f"xsec1m: {len(df)} reaches from {len(paths)} tiles -> {dst}")
    return dst
