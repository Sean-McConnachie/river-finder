"""Building blocks of the 1 m LiDAR national pipeline (rf-lxa.2, ``docs/lidar-pipeline.md``).

The national 1 m mosaic (``lidar.MOSAIC``: 424 Topo50 COG sheets, 24 x 36 km, 512 x 512 LERC blocks, 377 GB) is
read at full resolution only (the overviews are quantised to 0.1 m, ``docs/lidar-coverage.md``), in block-aligned
windows with many threads in parallel (the reads are latency bound: 16-48 threads ran 40-60 M cells/s here, against
2-4 M cells/s with 1-8), and aggregated on the fly to the routing grid. Nothing at 1 m is kept on disk.

Pieces:

- **Streaming** (``sheet_windows``, ``stream``): every 1 m window of the mosaic inside a box, read by a pool of
  threads (one dataset handle per thread and sheet), handed to a consumer in the calling thread.
- **Aggregation** (``Agg``): block mean, block minimum and valid-cell count onto a coarser grid aligned to the box,
  plus the share of each coarse cell that is hydro-flattened or interpolated water (``water_flags``).
- **Lean routing** (``route_lean``): the algorithm of ``routing.py`` (Priority-Flood with FIFO pits, D8, flat
  resolution of Barnes et al. 2014b, Kahn accumulation) with the same receivers, on a float32 DEM filled in place,
  uint8 direction codes and no n-sized int64 arrays: about 11-15 bytes per cell instead of ~66, so a 64 km tile with
  a 3 km halo fits at 5 m (196 M cells, ~3 GB) and a 20 km window at 1 m (~6 GB).
- **Inflows** (``inject``): point inflows (REC2 areas where rivers enter the box) added along the downstream path.
- **Channels** (``channel_links``, ``segments``): links and ~250 m reaches from the direction codes, as ``reaches.py``.
- **Lakes, seams, burning** (``fill_lakes``, ``seam_correct``, ``burn``): lake holes filled flat at the LiDAR water
  level of their rim; the 8 m fallback corrected towards the LiDAR across a seam; LINZ lines burnt into the DEM.
- **Profiles** (``running_min``, ``steepest``, ``longest_flat``): long-profile metrics on the 1 m water surface.

Direction codes (``uint8``): 0-7 receiver in the ``routing.DR/DC`` order (receiver = cell + ``offsets(nc)[k]``),
``OUT`` (8) outlet, ``NODATA`` (9), ``FLAT`` bit (16) while a flat is unresolved, ``UNSEEN`` (255) during the flood.
"""

from __future__ import annotations

import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from numba import njit

from .routing import DC, DIST, DR

OUT = np.uint8(8)
NODATA = np.uint8(9)
FLAT = np.uint8(16)
UNSEEN = np.uint8(255)
SQ2 = math.sqrt(2.0)


def offsets(nc: int) -> np.ndarray:
    """Flat-index offset of each direction code 0-7 on a grid with ``nc`` columns."""
    return (DR * nc + DC).astype(np.int64)


# ---------------------------------------------------------------------------------------------- aggregation
@njit(cache=True, nogil=True)
def _agg_into(z, r0, c0, f, s, cnt, mn):
    """Add the valid (finite) cells of window ``z`` (top-left at 1 m row/col ``r0, c0`` of the box) to the coarse
    sums ``s``, counts ``cnt`` and minima ``mn`` of factor ``f``. Cells outside the coarse grid are skipped."""
    nr, nc = z.shape
    R, C = s.shape
    for r in range(nr):
        rr = (r0 + r) // f
        if rr < 0 or rr >= R:
            continue
        for c in range(nc):
            v = z[r, c]
            if not np.isfinite(v):
                continue
            cc = (c0 + c) // f
            if cc < 0 or cc >= C:
                continue
            s[rr, cc] += v
            cnt[rr, cc] += 1
            if v < mn[rr, cc]:
                mn[rr, cc] = v


@njit(cache=True, nogil=True)
def _count_into(flags, r0, c0, f, nflat, nplanar):
    nr, nc = flags.shape
    R, C = nflat.shape
    for r in range(nr):
        rr = (r0 + r) // f
        if rr < 0 or rr >= R:
            continue
        for c in range(nc):
            g = flags[r, c]
            if g == 0:
                continue
            cc = (c0 + c) // f
            if cc < 0 or cc >= C:
                continue
            if g & 1:
                nflat[rr, cc] += 1
            elif g & 2:
                nplanar[rr, cc] += 1


@njit(cache=True, nogil=True)
def water_flags(z, flat_tol=0.002, planar_tol=0.01):
    """Per cell: 1 = flattened (all 8 neighbours within ``flat_tol``: hydro-flattened water), 2 = planar (both
    Laplacians below ``planar_tol``: inside an interpolation triangle), 0 otherwise. Border cells are 0."""
    nr, nc = z.shape
    out = np.zeros((nr, nc), np.uint8)
    for r in range(1, nr - 1):
        for c in range(1, nc - 1):
            v = z[r, c]
            if not np.isfinite(v):
                continue
            ok = True
            for dr in range(-1, 2):
                for dc in range(-1, 2):
                    w = z[r + dr, c + dc]
                    if not np.isfinite(w) or abs(w - v) >= flat_tol:
                        ok = False
            if ok:
                out[r, c] = 1
                continue
            l4 = z[r - 1, c] + z[r + 1, c] + z[r, c - 1] + z[r, c + 1] - 4 * v
            lx = z[r - 1, c - 1] + z[r + 1, c + 1] + z[r - 1, c + 1] + z[r + 1, c - 1] - 4 * v
            if np.isfinite(l4) and np.isfinite(lx) and abs(l4) < planar_tol and abs(lx) < planar_tol:
                out[r, c] = 2
    return out


class Agg:
    """Coarse grid of factor ``f`` (cell ``f`` m) over a box of ``shape1`` 1 m cells: block sum (float64), count,
    minimum, and counts of flattened / planar 1 m cells (when ``water``)."""

    def __init__(self, shape1: tuple[int, int], f: int, water: bool = True):
        if shape1[0] % f or shape1[1] % f:
            raise ValueError("box must be a whole number of coarse cells")
        self.f = f
        shp = (shape1[0] // f, shape1[1] // f)
        self.sum = np.zeros(shp, np.float64)
        ct = np.uint8 if f * f < 255 else np.uint16
        self.cnt = np.zeros(shp, ct)
        self.min = np.full(shp, np.inf, np.float32)
        self.nflat = np.zeros(shp, ct) if water else None
        self.nplanar = np.zeros(shp, ct) if water else None

    def add(self, z: np.ndarray, r0: int, c0: int, flags: np.ndarray | None = None) -> None:
        _agg_into(z, r0, c0, self.f, self.sum, self.cnt, self.min)
        if flags is not None and self.nflat is not None:
            _count_into(flags, r0, c0, self.f, self.nflat, self.nplanar)

    def mean(self, min_share: float = 0.5) -> np.ndarray:
        """Block mean, NaN where fewer than ``min_share`` of the cell's 1 m cells are valid."""
        need = max(1, int(math.ceil(min_share * self.f * self.f)))
        with np.errstate(invalid="ignore", divide="ignore"):
            m = (self.sum / np.maximum(self.cnt, 1)).astype(np.float32)
        m[self.cnt < need] = np.nan
        return m

    def minimum(self, min_share: float = 0.5) -> np.ndarray:
        need = max(1, int(math.ceil(min_share * self.f * self.f)))
        m = self.min.copy()
        m[self.cnt < need] = np.nan
        return m

    def share(self, which: str) -> np.ndarray:
        a = self.nflat if which == "flat" else self.nplanar
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(self.cnt > 0, a / np.maximum(self.cnt, 1), 0.0).astype(np.float32)


# ---------------------------------------------------------------------------------------------- streaming
def sheet_windows(sheets: list[dict], box: tuple[float, float, float, float], win: int = 2048) -> list[tuple]:
    """Read jobs ``(href, col_off, row_off, width, height, box_row0, box_col0)`` covering ``box`` (NZTM, whole
    metres) with windows aligned to each sheet's 512-cell blocks. ``sheets``: ``[{href, bounds (x0, y0, x1, y1)}]``
    (1 m grids with whole-metre origins)."""
    bx0, by0, bx1, by1 = (int(round(v)) for v in box)
    jobs = []
    for s in sheets:
        sx0, sy0, sx1, sy1 = (int(round(v)) for v in s["bounds"])
        ix0, iy0, ix1, iy1 = max(bx0, sx0), max(by0, sy0), min(bx1, sx1), min(by1, sy1)
        if ix0 >= ix1 or iy0 >= iy1:
            continue
        c_lo, c_hi = ix0 - sx0, ix1 - sx0          # sheet columns
        r_lo, r_hi = sy1 - iy1, sy1 - iy0          # sheet rows
        for r in range((r_lo // win) * win, r_hi, win):
            for c in range((c_lo // win) * win, c_hi, win):
                ca, cb = max(c, c_lo), min(c + win, c_hi)
                ra, rb = max(r, r_lo), min(r + win, r_hi)
                if ca >= cb or ra >= rb:
                    continue
                jobs.append((s["href"], ca, ra, cb - ca, rb - ra, by1 - (sy1 - ra), (sx0 + ca) - bx0))
    return jobs


class _Handles(threading.local):
    def __init__(self):
        self.ds = {}
        self.env = None


def stream(jobs: list[tuple], consume, threads: int = 24, ahead: int = 2, gdal_env: dict | None = None,
           log=None, every: int = 200) -> dict:
    """Read every job of ``sheet_windows`` with ``threads`` threads and call ``consume(z, box_row0, box_col0)`` in
    the calling thread (``z`` float32 with NaN for nodata), in job order. At most ``ahead * threads`` windows are in
    memory at once. Returns timing counters."""
    import rasterio
    from rasterio.windows import Window

    from .stac import GDAL_ENV
    from .stac import NODATA as NODATA_Z

    env = dict(GDAL_ENV, GDAL_CACHEMAX=64, VSI_CACHE="FALSE")
    env.update(gdal_env or {})
    tl = _Handles()

    def read(job):
        href, c, r, w, h, br, bc = job
        if tl.env is None:
            tl.env = rasterio.Env(**env)
            tl.env.__enter__()
        ds = tl.ds.get(href)
        if ds is None:
            ds = tl.ds[href] = rasterio.open(href if href.startswith("/vsi") else "/vsicurl/" + href)
        z = ds.read(1, window=Window(c, r, w, h))
        z[z <= NODATA_Z + 1] = np.nan
        return z, br, bc

    t0 = time.time()
    t_consume = 0.0
    cells = 0
    with ThreadPoolExecutor(threads) as ex:
        pending = []
        it = iter(jobs)
        for job in it:
            pending.append(ex.submit(read, job))
            if len(pending) >= ahead * threads:
                break
        k = 0
        while pending:
            z, br, bc = pending.pop(0).result()
            nxt = next(it, None)
            if nxt is not None:
                pending.append(ex.submit(read, nxt))
            t = time.time()
            consume(z, br, bc)
            t_consume += time.time() - t
            cells += z.size
            k += 1
            if log and k % every == 0:
                dt = time.time() - t0
                log(f"  stream: {k}/{len(jobs)} windows, {cells / 1e6:.0f} M cells, {cells / dt / 1e6:.1f} M cells/s")
    dt = time.time() - t0
    return {"windows": len(jobs), "cells": int(cells), "seconds": round(dt, 1),
            "mcells_s": round(cells / max(dt, 1e-9) / 1e6, 1), "consume_seconds": round(t_consume, 1),
            "threads": threads}


def mosaic_sheets(box, cache_dir: Path) -> list[dict]:
    """The 1 m mosaic sheets intersecting an NZTM box: ``[{id, href, bounds}]`` (bounds from the Topo50 grid of the
    sheet: read from the COG header)."""
    import rasterio
    from pyproj import Transformer

    from .lidar import MOSAIC
    from .stac import GDAL_ENV, tiles_for_bbox

    bb = Transformer.from_crs(2193, 4326, always_xy=True).transform_bounds(*box, densify_pts=21)
    out = []
    with rasterio.Env(**GDAL_ENV):
        for it in tiles_for_bbox(MOSAIC, bb, cache_dir):
            with rasterio.open("/vsicurl/" + it["href"]) as ds:
                b = ds.bounds
            if b.right <= box[0] or b.left >= box[2] or b.top <= box[1] or b.bottom >= box[3]:
                continue
            out.append({"id": it["id"], "href": it["href"], "bounds": (b.left, b.bottom, b.right, b.top)})
    return out


# ---------------------------------------------------------------------------------------------- lean routing
@njit(cache=True)
def _grow_f(a, n):
    b = np.empty(n, a.dtype)
    b[:a.size] = a
    return b


@njit(cache=True)
def _pf_lean(z, bseed):
    """Priority-Flood (``routing._priority_flood``) on float32 ``z`` (NaN = nodata), filled in place. Returns uint8
    codes: receiver direction 0-7 (the neighbour the flood came from), OUT for seeds, NODATA."""
    nr, nc = z.shape
    n = nr * nc
    zf = z.ravel()
    bs = bseed.ravel()
    d = np.full(n, UNSEEN, np.uint8)
    off = np.empty(8, np.int64)
    for k in range(8):
        off[k] = DR[k] * nc + DC[k]
    nseed = 0
    for i in range(n):
        if not np.isfinite(zf[i]):
            d[i] = NODATA
    cap = 1024 + n // 64
    hk = np.empty(cap, np.float32)
    hs = np.empty(cap, np.int64)
    hv = np.empty(cap, np.int64)
    size = 0
    seq = 0
    pcap = 1024 + n // 64
    pit = np.empty(pcap, np.int64)
    ph = 0
    pn = 0
    for i in range(n):
        if d[i] == NODATA:
            continue
        r = i // nc
        c = i - r * nc
        seed = (r == 0 or c == 0 or r == nr - 1 or c == nc - 1) and bs[i]
        if not seed:
            for k in range(8):
                rr = r + DR[k]
                cc = c + DC[k]
                if rr < 0 or cc < 0 or rr >= nr or cc >= nc:
                    continue
                if d[rr * nc + cc] == NODATA:
                    seed = True
                    break
        if seed:
            d[i] = OUT
            nseed += 1
            if size == cap:
                cap *= 2
                hk = _grow_f(hk, cap)
                hs = _grow_f(hs, cap)
                hv = _grow_f(hv, cap)
            # push
            j = size
            hk[j] = zf[i]
            hs[j] = seq
            hv[j] = i
            while j > 0:
                p = (j - 1) >> 1
                if hk[j] < hk[p] or (hk[j] == hk[p] and hs[j] < hs[p]):
                    hk[j], hk[p] = hk[p], hk[j]
                    hs[j], hs[p] = hs[p], hs[j]
                    hv[j], hv[p] = hv[p], hv[j]
                    j = p
                else:
                    break
            size += 1
            seq += 1
    while pn > 0 or size > 0:
        if pn > 0:
            ci = pit[ph]
            ph += 1
            if ph == pcap:
                ph = 0
            pn -= 1
        else:
            ci = hv[0]
            size -= 1
            hk[0] = hk[size]
            hs[0] = hs[size]
            hv[0] = hv[size]
            j = 0
            while True:
                lft = 2 * j + 1
                if lft >= size:
                    break
                m = lft
                rgt = lft + 1
                if rgt < size and (hk[rgt] < hk[lft] or (hk[rgt] == hk[lft] and hs[rgt] < hs[lft])):
                    m = rgt
                if hk[m] < hk[j] or (hk[m] == hk[j] and hs[m] < hs[j]):
                    hk[m], hk[j] = hk[j], hk[m]
                    hs[m], hs[j] = hs[j], hs[m]
                    hv[m], hv[j] = hv[j], hv[m]
                    j = m
                else:
                    break
        r = ci // nc
        c = ci - r * nc
        zc = zf[ci]
        for k in range(8):
            rr = r + DR[k]
            cc = c + DC[k]
            if rr < 0 or cc < 0 or rr >= nr or cc >= nc:
                continue
            jn = rr * nc + cc
            if d[jn] != UNSEEN:
                continue
            d[jn] = 7 - k   # neighbour jn drains to ci: direction from jn to ci
            if zf[jn] <= zc:
                zf[jn] = zc
                if pn == pcap:
                    npit = np.empty(pcap * 2, np.int64)
                    for t in range(pn):
                        q = ph + t
                        if q >= pcap:
                            q -= pcap
                        npit[t] = pit[q]
                    pit = npit
                    ph = 0
                    pcap *= 2
                q = ph + pn
                if q >= pcap:
                    q -= pcap
                pit[q] = jn
                pn += 1
            else:
                if size == cap:
                    cap *= 2
                    hk = _grow_f(hk, cap)
                    hs = _grow_f(hs, cap)
                    hv = _grow_f(hv, cap)
                j = size
                hk[j] = zf[jn]
                hs[j] = seq
                hv[j] = jn
                while j > 0:
                    p = (j - 1) >> 1
                    if hk[j] < hk[p] or (hk[j] == hk[p] and hs[j] < hs[p]):
                        hk[j], hk[p] = hk[p], hk[j]
                        hs[j], hs[p] = hs[p], hs[j]
                        hv[j], hv[p] = hv[p], hv[j]
                        j = p
                    else:
                        break
                size += 1
                seq += 1
    return d.reshape(nr, nc)


@njit(cache=True)
def _d8_lean(z, d, cellsize):
    """D8 on the filled DEM, in place: steepest strictly lower valid neighbour, else keep the flood's receiver
    marked ``FLAT`` (resolved by ``_flats_lean``); seeds without a lower neighbour stay ``OUT``."""
    nr, nc = z.shape
    zf = z.ravel()
    df = d.ravel()
    nflat = 0
    for i in range(nr * nc):
        if df[i] == NODATA:
            continue
        r = i // nc
        c = i - r * nc
        best = 0.0
        bk = -1
        zi = np.float64(zf[i])
        for k in range(8):
            rr = r + DR[k]
            cc = c + DC[k]
            if rr < 0 or cc < 0 or rr >= nr or cc >= nc:
                continue
            j = rr * nc + cc
            if df[j] == NODATA:
                continue
            s = (zi - np.float64(zf[j])) / (DIST[k] * cellsize)
            if s > best:
                best = s
                bk = k
        if bk >= 0:
            df[i] = bk
        elif df[i] != OUT:
            df[i] = FLAT | df[i]
            nflat += 1
    return nflat


@njit(cache=True)
def _flats_lean(z, d, nflat):
    """``routing.resolve_flats`` on the cells marked ``FLAT``, with arrays sized to the flats (plus one int32
    cell -> flat map). Same receivers."""
    nr, nc = z.shape
    n = nr * nc
    zf = z.ravel()
    df = d.ravel()
    fid = np.full(n, -1, np.int32)
    cells = np.empty(nflat, np.int64)
    k_ = 0
    for i in range(n):
        if df[i] != NODATA and (df[i] & FLAT):
            fid[i] = k_
            cells[k_] = i
            k_ += 1
    q = np.empty(nflat, np.int64)
    dlow = np.full(nflat, -1, np.int32)
    dhigh = np.zeros(nflat, np.int32)
    # 1. distance from low edges: flats next to an equal-height drained cell start at 1
    qt = 0
    for f in range(nflat):
        i = cells[f]
        r = i // nc
        c = i - r * nc
        for k in range(8):
            rr = r + DR[k]
            cc = c + DC[k]
            if rr < 0 or cc < 0 or rr >= nr or cc >= nc:
                continue
            j = rr * nc + cc
            if df[j] == NODATA or fid[j] >= 0:
                continue
            if zf[j] == zf[i]:
                dlow[f] = 1
                q[qt] = f
                qt += 1
                break
    qh = 0
    while qh < qt:
        f = q[qh]
        qh += 1
        i = cells[f]
        r = i // nc
        c = i - r * nc
        for k in range(8):
            rr = r + DR[k]
            cc = c + DC[k]
            if rr < 0 or cc < 0 or rr >= nr or cc >= nc:
                continue
            j = rr * nc + cc
            g = fid[j]
            if g >= 0 and dlow[g] < 0 and zf[j] == zf[i]:
                dlow[g] = dlow[f] + 1
                q[qt] = g
                qt += 1
    # 2. distance from high edges
    qt = 0
    for f in range(nflat):
        i = cells[f]
        r = i // nc
        c = i - r * nc
        for k in range(8):
            rr = r + DR[k]
            cc = c + DC[k]
            if rr < 0 or cc < 0 or rr >= nr or cc >= nc:
                continue
            j = rr * nc + cc
            if df[j] != NODATA and zf[j] > zf[i]:
                dhigh[f] = 1
                q[qt] = f
                qt += 1
                break
    qh = 0
    while qh < qt:
        f = q[qh]
        qh += 1
        i = cells[f]
        r = i // nc
        c = i - r * nc
        for k in range(8):
            rr = r + DR[k]
            cc = c + DC[k]
            if rr < 0 or cc < 0 or rr >= nr or cc >= nc:
                continue
            j = rr * nc + cc
            g = fid[j]
            if g >= 0 and dhigh[g] == 0 and zf[j] == zf[i]:
                dhigh[g] = dhigh[f] + 1
                q[qt] = g
                qt += 1
    # 3. labels and the largest high-edge distance per flat
    label = np.full(nflat, -1, np.int32)
    hmax = np.zeros(nflat, np.int32)
    nl = 0
    for s in range(nflat):
        if label[s] >= 0:
            continue
        qh = 0
        qt = 1
        q[0] = s
        label[s] = nl
        m = dhigh[s]
        while qh < qt:
            f = q[qh]
            qh += 1
            if dhigh[f] > m:
                m = dhigh[f]
            i = cells[f]
            r = i // nc
            c = i - r * nc
            for k in range(8):
                rr = r + DR[k]
                cc = c + DC[k]
                if rr < 0 or cc < 0 or rr >= nr or cc >= nc:
                    continue
                j = rr * nc + cc
                g = fid[j]
                if g >= 0 and label[g] < 0 and zf[j] == zf[i]:
                    label[g] = nl
                    q[qt] = g
                    qt += 1
        hmax[nl] = m
        nl += 1
    # 4. mask and receivers (computed for all flats before any code changes)
    big = np.int64(1) << 40
    mask = np.full(nflat, big, np.int64)
    for f in range(nflat):
        if dlow[f] >= 0:
            mask[f] = 2 * np.int64(dlow[f])
            if dhigh[f] > 0:
                mask[f] += hmax[label[f]] - dhigh[f]
    newd = np.empty(nflat, np.uint8)
    for f in range(nflat):
        i = cells[f]
        r = i // nc
        c = i - r * nc
        best = mask[f]
        bk = -1
        for k in range(8):
            rr = r + DR[k]
            cc = c + DC[k]
            if rr < 0 or cc < 0 or rr >= nr or cc >= nc:
                continue
            j = rr * nc + cc
            if df[j] == NODATA or zf[j] != zf[i]:
                continue
            g = fid[j]
            if g >= 0:
                m = mask[g]
            else:
                m = -1
            if m < best:
                best = m
                bk = k
        newd[f] = bk if bk >= 0 else (df[i] & 7)
    for f in range(nflat):
        df[cells[f]] = newd[f]


@njit(cache=True)
def _accumulate_lean(d, cellarea, edge_truncates):
    """Accumulation without a queue: every cell whose donors are all done passes its total down at once. Each valid
    cell weighs ``cellarea``. Returns (acc float64, flags uint8: bit 0 = upstream touches the grid border when
    ``edge_truncates``, n_done)."""
    nr, nc = d.shape
    n = nr * nc
    df = d.ravel()
    off = np.empty(8, np.int64)
    for k in range(8):
        off[k] = DR[k] * nc + DC[k]
    indeg = np.zeros(n, np.uint8)
    acc = np.zeros(n, np.float64)
    fl = np.zeros(n, np.uint8)
    for i in range(n):
        k = df[i]
        if k == NODATA:
            continue
        acc[i] = cellarea
        if k < 8:
            indeg[i + off[k]] += 1
        if edge_truncates:
            r = i // nc
            c = i - r * nc
            if r == 0 or c == 0 or r == nr - 1 or c == nc - 1:
                fl[i] = 1
    done = 0
    for i in range(n):
        if indeg[i] != 0:
            continue
        v = i
        while True:
            indeg[v] = 255
            done += 1
            k = df[v]
            if k >= 8:
                break
            rcv = v + off[k]
            acc[rcv] += acc[v]
            fl[rcv] |= fl[v]
            indeg[rcv] -= 1
            if indeg[rcv] != 0:
                break
            v = rcv
    return acc.reshape(nr, nc), fl.reshape(nr, nc), done


def route_lean(z: np.ndarray, cellsize: float, border_seed: np.ndarray | None = None,
               timings: dict | None = None) -> np.ndarray:
    """Fill ``z`` (float32, NaN = nodata; modified in place) and return uint8 direction codes (resolved, no flats).
    Same receivers as ``routing.route`` on the same DEM."""
    if z.dtype != np.float32 or not z.flags.c_contiguous:
        raise TypeError("z must be a C-contiguous float32 array (filled in place)")
    bs = np.ones(z.shape, np.bool_) if border_seed is None else np.ascontiguousarray(border_seed, np.bool_)
    t = time.time()
    d = _pf_lean(z, bs)
    t1 = time.time()
    nflat = _d8_lean(z, d, float(cellsize))
    t2 = time.time()
    if nflat:
        _flats_lean(z, d, nflat)
    t3 = time.time()
    if timings is not None:
        timings.update(flood_s=round(t1 - t, 1), d8_s=round(t2 - t1, 1), flats_s=round(t3 - t2, 1), flat_cells=nflat)
    return d


def accumulate_lean(d: np.ndarray, cellsize: float, edge_truncates: bool = True):
    """Upstream area (m2, float64 2-D, 0 on nodata) and flags (uint8, bit 0: the upstream area touches the grid
    border)."""
    acc, fl, done = _accumulate_lean(d, float(cellsize) ** 2, edge_truncates)
    if done != d.size:
        raise RuntimeError(f"flow graph has a cycle: processed {done} of {d.size} cells")
    return acc, fl


def receivers(d: np.ndarray) -> np.ndarray:
    """int64 receivers (-1 outlet / nodata), as ``routing.route`` returns (for tests and small grids)."""
    nc = d.shape[1]
    off = offsets(nc)
    df = d.ravel()
    rec = np.full(df.size, -1, np.int64)
    m = df < 8
    rec[m] = np.flatnonzero(m) + off[df[m]]
    return rec


@njit(cache=True)
def _walk_add(d, acc, fl, start, amount, bit):
    nr, nc = d.shape
    df = d.ravel()
    af = acc.ravel()
    ff = fl.ravel()
    v = start
    steps = 0
    while True:
        af[v] += amount
        ff[v] |= bit
        k = df[v]
        if k >= 8 or steps > df.size:
            break
        v = v + DR[k] * nc + DC[k]
        steps += 1
    return steps


def inject(d: np.ndarray, acc: np.ndarray, fl: np.ndarray, transform, inflows, radius_cells: int) -> list[dict]:
    """Add point inflows ``[(x, y, area_km2, id)]`` (as ``nz.inflow_points``): each is snapped to the cell of largest
    accumulated area within ``radius_cells`` and its area added along the downstream path, which also gets flag bit
    1 (``fl & 2``: the catchment includes an inflow, so touching the edge does not truncate it)."""
    import rasterio

    used = []
    nr, nc = d.shape
    for x, y, a, sid in inflows:
        r, c = rasterio.transform.rowcol(transform, x, y)
        r0, r1 = max(0, r - radius_cells), min(nr, r + radius_cells + 1)
        c0, c1 = max(0, c - radius_cells), min(nc, c + radius_cells + 1)
        if r0 >= r1 or c0 >= c1:
            continue
        win = acc[r0:r1, c0:c1]
        k = int(np.argmax(win))
        if win.flat[k] <= 0:
            continue
        rr, cc = r0 + k // win.shape[1], c0 + k % win.shape[1]
        _walk_add(d, acc, fl, rr * nc + cc, a * 1e6, np.uint8(2))
        used.append({"id": int(sid), "area_km2": round(float(a), 3), "row": int(rr), "col": int(cc)})
    return used


# ---------------------------------------------------------------------------------------------- channels
@njit(cache=True)
def _links(d, acc, thr):
    """Links of the channel network (cells with ``acc >= thr``), upstream to downstream, CSR packed, and the first
    channel cell below each link (-1 none)."""
    nr, nc = d.shape
    n = nr * nc
    df = d.ravel()
    af = acc.ravel()
    ndon = np.zeros(n, np.uint8)
    nchan = 0
    for i in range(n):
        if af[i] >= thr and df[i] != NODATA:
            nchan += 1
            k = df[i]
            if k < 8:
                j = i + DR[k] * nc + DC[k]
                if af[j] >= thr:
                    ndon[j] += 1
    cells = np.empty(nchan, np.int64)
    ptr = np.empty(nchan + 1, np.int64)
    nxt = np.empty(nchan, np.int64)
    nl = 0
    kk = 0
    for h in range(n):
        if af[h] < thr or df[h] == NODATA or ndon[h] == 1:
            continue
        ptr[nl] = kk
        c = h
        while True:
            cells[kk] = c
            kk += 1
            k = df[c]
            if k >= 8:
                nxt[nl] = -1
                break
            r = c + DR[k] * nc + DC[k]
            if af[r] < thr:
                nxt[nl] = -1
                break
            if ndon[r] != 1:
                nxt[nl] = r
                break
            c = r
        nl += 1
    ptr[nl] = kk
    return cells[:kk], ptr[:nl + 1], nxt[:nl]


@njit(cache=True)
def _targets(d, seg_of_cell):
    """For every cell, the segment of the first segment cell downstream (itself included; -1 none). Walks down with
    path compression, so each cell is visited once."""
    nr, nc = d.shape
    n = nr * nc
    df = d.ravel()
    sc = seg_of_cell.ravel()
    tgt = np.full(n, -2, np.int32)
    cap = 4096
    stack = np.empty(cap, np.int64)
    for i in range(n):
        if tgt[i] != -2:
            continue
        v = i
        k = 0
        while tgt[v] == -2 and sc[v] < 0 and df[v] < 8:
            if k == cap:
                cap *= 2
                st2 = np.empty(cap, np.int64)
                st2[:k] = stack[:k]
                stack = st2
            stack[k] = v
            k += 1
            v = v + DR[df[v]] * nc + DC[df[v]]
        if sc[v] >= 0:
            t = sc[v]
        elif tgt[v] != -2:
            t = tgt[v]
        else:
            t = -1
        tgt[v] = t
        for j in range(k):
            tgt[stack[j]] = t
    for i in range(n):
        if df[i] == NODATA:
            tgt[i] = -1
    return tgt.reshape(nr, nc)


@njit(cache=True)
def _local_counts(d, seg_of_cell, nseg, mask):
    """Cells (where ``mask``) per segment whose first segment cell downstream belongs to that segment. Returns
    (counts, cells with no segment)."""
    tgt = _targets(d, seg_of_cell).ravel()
    df = d.ravel()
    mk = mask.ravel()
    cnt = np.zeros(nseg, np.int64)
    none = 0
    for i in range(df.size):
        if not mk[i] or df[i] == NODATA:
            continue
        t = tgt[i]
        if t >= 0:
            cnt[t] += 1
        else:
            none += 1
    return cnt, none


def _seg_of_cell(d: np.ndarray, seg) -> np.ndarray:
    soc = np.full(d.shape, -1, np.int32)
    flat = soc.ravel()
    for k, cells in enumerate(seg["cells"]):
        flat[cells[:-1] if len(cells) > 1 else cells] = k
    return soc


def local_catchment(d: np.ndarray, seg) -> np.ndarray:
    """int32 raster: the row of ``seg`` (``segments(...)``) each cell first drains into (-1 none), as
    ``reaches.py``'s ``local_catchment.tif``."""
    return _targets(d, _seg_of_cell(d, seg))


def local_areas(d: np.ndarray, seg, cellsize: float, mask: np.ndarray | None = None) -> tuple[np.ndarray, float]:
    """Local catchment area (km2) of each segment of ``segments(...)`` (its ``cells`` column): the cells that first
    drain into its body (all its cells but the bottom one, as ``reaches.py``). ``mask`` limits the cells counted.
    Returns (km2 per segment, km2 that reaches no segment)."""
    soc = _seg_of_cell(d, seg)
    m = np.ones(d.shape, np.bool_) if mask is None else np.ascontiguousarray(mask, np.bool_)
    cnt, none = _local_counts(d, soc, len(seg), m)
    return cnt * cellsize * cellsize / 1e6, none * cellsize * cellsize / 1e6


def channel_links(d: np.ndarray, acc_m2: np.ndarray, min_area_km2: float):
    return _links(d, acc_m2, min_area_km2 * 1e6)


def _steepest(dist: np.ndarray, z: np.ndarray, window: float) -> float:
    if len(dist) < 2:
        return 0.0
    j = np.searchsorted(dist, dist + window, side="left")
    j = np.minimum(j, len(dist) - 1)
    span = dist[j] - dist
    ok = span >= 0.6 * window
    if not ok.any():
        return 0.0
    return float(max(0.0, np.max((z[ok] - z[j[ok]]) / span[ok]) * 1000.0))


def segments(d: np.ndarray, acc_m2: np.ndarray, zf: np.ndarray, transform, min_area_km2: float = 0.5,
             seg_len_m: float = 250.0, trunc: np.ndarray | None = None):
    """Reach segments as ``reaches.build_segments`` cuts them (links cut into ~``seg_len_m`` pieces; the last segment
    of a link ends on the confluence cell below), with the full cell path as geometry. Columns: seg_id, link,
    down_seg, length_m, z_top, z_bot, gradient_m_per_km, steepest_50m_m_per_km, area_km2, area_mid_km2,
    truncated (the upstream area touches the grid border, no inflow), inflow (it holds a point inflow), and ``cells``
    (flat cell indices, object column)."""
    import geopandas as gpd
    from shapely.geometry import LineString

    cs = transform.a
    nc = d.shape[1]
    cells, ptr, nxt = channel_links(d, acc_m2, min_area_km2)
    af, zz = acc_m2.ravel(), zf.ravel()
    tr = trunc.ravel() if trunc is not None else None
    rows, geoms = [], []
    first, last, head = {}, {}, {}
    for li in range(len(ptr) - 1):
        lc = cells[ptr[li]:ptr[li + 1]]
        head[int(lc[0])] = li
        path = np.concatenate([lc, [nxt[li]]]) if nxt[li] >= 0 else lc
        if len(path) < 2:
            continue
        r, c = np.divmod(path, nc)
        step = np.where((np.diff(r) != 0) & (np.diff(c) != 0), SQ2, 1.0) * cs
        dist = np.r_[0.0, np.cumsum(step)]
        total = dist[-1]
        nseg = max(1, int(round(total / seg_len_m)))
        bnd = np.searchsorted(dist, np.linspace(0, total, nseg + 1))
        bnd[-1] = len(path) - 1
        xs = transform.c + (c + 0.5) * cs
        ys = transform.f - (r + 0.5) * cs
        for s in range(nseg):
            a, b = int(bnd[s]), int(bnd[s + 1])
            if b <= a:
                continue
            idx = path[a:b + 1]
            dd = dist[a:b + 1] - dist[a]
            zp = zz[idx].astype(np.float64)
            length = float(dd[-1])
            drop = float(zp[0] - zp[-1])
            conf = b == len(path) - 1 and nxt[li] >= 0
            a_bot = float(af[idx[-2] if conf else idx[-1]]) / 1e6
            a_mid = float(np.mean(af[idx[:-1]])) / 1e6
            sid = len(rows)
            if s == 0:
                first[li] = sid
            last[li] = sid
            mid = idx[len(idx) // 2]
            rows.append({"seg_id": sid, "link": li, "down_seg": sid + 1 if s < nseg - 1 else -1,
                         "length_m": round(length, 1), "z_top": float(zp[0]), "z_bot": float(zp[-1]),
                         "gradient_m_per_km": round(drop / length * 1000.0, 3) if length > 0 else 0.0,
                         "steepest_50m_m_per_km": round(_steepest(dd, zp, 50.0), 2),
                         "area_km2": round(a_bot, 4), "area_mid_km2": round(a_mid, 4),
                         "truncated": bool(tr[mid] & 1 and not tr[mid] & 2) if tr is not None else False,
                         "inflow": bool(tr[mid] & 2) if tr is not None else False,
                         "cells": idx})
            geoms.append(LineString(np.column_stack([xs[a:b + 1], ys[a:b + 1]])))
    for li, sid in last.items():
        if nxt[li] >= 0 and int(nxt[li]) in head and head[int(nxt[li])] in first:
            rows[sid]["down_seg"] = first[head[int(nxt[li])]]
    return gpd.GeoDataFrame(rows, geometry=geoms, crs="EPSG:2193")


# ---------------------------------------------------------------------------------------------- lakes, seams, burn
def fill_lakes(z: np.ndarray, flat_share: np.ndarray, land: np.ndarray | None = None, min_cells: int = 4,
               rim_flat: float = 0.5, rim_mode: float = 0.4, tol: float = 0.02, min_land: float = 0.5) -> list[dict]:
    """Fill no-data holes that are lake interiors, in place, flat at the LiDAR water level of their rim.

    A hole (8-connected NaN cells) is a lake when at least ``rim_flat`` of its rim (valid cells touching it) is
    hydro-flattened water (``flat_share`` of the cell >= 0.5) and the modal rim level (to ``tol`` m) holds at least
    ``rim_mode`` of those flattened rim cells. The sea is a hole with a flattened rim too (the surveys flatten the
    coastal water), so with ``land`` (where the fallback DEM has data: the 8 m contour DEM has lakes but no sea) a
    hole must also be at least ``min_land`` land. Returns one dict per hole examined (cells, level, shares, filled)."""
    from scipy import ndimage

    hole = ~np.isfinite(z)
    lab, n = ndimage.label(hole, structure=np.ones((3, 3), bool))
    out = []
    if n == 0:
        return out
    objs = ndimage.find_objects(lab)
    for k, sl in enumerate(objs, start=1):
        sl2 = tuple(slice(max(0, s.start - 1), s.stop + 1) for s in sl)
        m = lab[sl2] == k
        cnt = int(m.sum())
        if cnt < min_cells:
            continue
        rim = ndimage.binary_dilation(m, structure=np.ones((3, 3), bool)) & ~m & np.isfinite(z[sl2])
        if not rim.any():
            continue
        zr = z[sl2][rim]
        fr = flat_share[sl2][rim] >= 0.5
        rec = {"hole": k, "cells": cnt, "rim_cells": int(rim.sum()), "rim_flat_share": round(float(fr.mean()), 3)}
        if land is not None:
            rec["land_share"] = round(float(land[sl2][m].mean()), 3)
        if fr.mean() >= rim_flat and (land is None or rec["land_share"] >= min_land):
            q = np.round(zr[fr] / tol).astype(np.int64)
            vals, cts = np.unique(q, return_counts=True)
            j = int(np.argmax(cts))
            share = cts[j] / fr.sum()
            level = float(np.median(zr[fr][q == vals[j]]))
            rec.update(level=round(level, 3), rim_mode_share=round(float(share), 3))
            if share >= rim_mode:
                sub = z[sl2]
                sub[m] = level
                rec["filled"] = True
        rec.setdefault("filled", False)
        out.append(rec)
    return out


def seam_correct(z_fine: np.ndarray, z_coarse: np.ndarray, cell: float, band_m: float = 300.0,
                 sigma_m: float = 150.0, taper_m: float = 1000.0, work_m: float = 25.0,
                 edge_m: float = 0.0) -> tuple[np.ndarray, dict]:
    """Merge a fine DEM with gaps (NaN) and a fallback DEM on the same grid, correcting the fallback at the seam
    (``docs/lidar-coverage.md``): d = fine - coarse on fine cells within ``band_m`` of the gap, extended into the
    gap by normalised Gaussian smoothing (``sigma_m``) and added to the fallback with a weight falling from 1 at the
    seam to 0 at ``taper_m``. The fine cells are never changed. The correction field is computed on a ``work_m``
    grid and interpolated back. With ``edge_m`` > 0, next to the seam the correction is the d of the nearest LiDAR
    cell, blended into the smooth field with weight exp(-distance / ``edge_m``), so the merged surface is continuous
    cell by cell (at ``work_m`` = ``cell``), not only at the smoothing scale. Returns (merged float32, stats)."""
    from scipy import ndimage

    have = np.isfinite(z_fine)
    gap = ~have & np.isfinite(z_coarse)
    out = np.where(have, z_fine, z_coarse).astype(np.float32)
    if not gap.any() or not have.any():
        return out, {"gap_cells": int(gap.sum())}
    f = max(1, int(round(work_m / cell)))
    H, W = (z_fine.shape[0] + f - 1) // f, (z_fine.shape[1] + f - 1) // f

    def coarse(a, fill, how):
        p = np.full((H * f, W * f), fill, a.dtype)
        p[:a.shape[0], :a.shape[1]] = a
        p = p.reshape(H, f, W, f)
        return p.mean(axis=(1, 3)) if how == "mean" else p.max(axis=(1, 3))

    wc = work_m if f > 1 else cell
    gap_c = coarse(gap.astype(np.float32), 0, "max") > 0
    have_c = coarse(have.astype(np.float32), 0, "mean") >= 0.5
    dist_in = ndimage.distance_transform_edt(~gap_c) * wc        # distance from the gap (for the LiDAR band)
    band = have_c & (dist_in <= band_m)
    diff = np.where(have & np.isfinite(z_coarse), z_fine - z_coarse, np.nan)
    dsum = coarse(np.nan_to_num(diff, nan=0.0).astype(np.float64), 0, "mean")
    dcnt = coarse(np.isfinite(diff).astype(np.float64), 0, "mean")
    with np.errstate(invalid="ignore", divide="ignore"):
        dmean = np.where(dcnt > 0, dsum / dcnt, 0.0)
    wt = (band & (dcnt > 0)).astype(np.float64)
    sg = sigma_m / wc
    num = ndimage.gaussian_filter(dmean * wt, sg, mode="nearest")
    den = ndimage.gaussian_filter(wt, sg, mode="nearest")
    with np.errstate(invalid="ignore", divide="ignore"):
        dext = np.where(den > 1e-6, num / den, 0.0)
    dist_gap = ndimage.distance_transform_edt(gap_c) * wc         # distance into the gap from the seam
    if edge_m > 0:
        src = band & (dcnt > 0)
        _, (ir, ic) = ndimage.distance_transform_edt(~src, return_indices=True)
        a = np.exp(-np.maximum(dist_gap - wc, 0.0) / edge_m)       # 1 on the first gap cell
        dext = a * dmean[ir, ic] + (1.0 - a) * dext
        del ir, ic, a
    w = np.clip(1.0 - np.maximum(dist_gap - wc, 0.0) / taper_m, 0.0, 1.0)
    corr_c = (dext * w).astype(np.float32)
    if f > 1:
        zoom = ndimage.zoom(corr_c, f, order=1)[:z_fine.shape[0], :z_fine.shape[1]]
        corr = np.zeros(z_fine.shape, np.float32)
        corr[:zoom.shape[0], :zoom.shape[1]] = zoom
    else:
        corr = corr_c
    out[gap] += corr[gap]
    stats = {"gap_cells": int(gap.sum()), "band_cells_work": int(band.sum()),
             "d_band_median": round(float(np.median(dmean[band & (dcnt > 0)])), 3) if (band & (dcnt > 0)).any()
             else None, "corr_abs_max": round(float(np.abs(corr[gap]).max()), 3)}
    return out, stats


def burn(z: np.ndarray, lines, transform, depth: float) -> int:
    """Lower ``z`` in place by ``depth`` m on the cells the lines cross (``rasterio.features.rasterize``, centre
    rule). Returns the number of cells burnt."""
    from rasterio.features import rasterize

    shapes = [(g, 1) for g in lines if g is not None and not g.is_empty]
    if not shapes:
        return 0
    m = rasterize(shapes, out_shape=z.shape, transform=transform, fill=0, dtype="uint8", all_touched=False) > 0
    m &= np.isfinite(z)
    z[m] -= np.float32(depth)
    return int(m.sum())


# ---------------------------------------------------------------------------------------------- profiles
def smooth_distance(xy: np.ndarray, window_m: float = 20.0, cell: float = 1.0) -> np.ndarray:
    """Distance along a D8 cell path after a moving average of its coordinates over ``window_m`` (shrinking at the
    ends), one value per vertex: removes the D8 staircase (~8 % at any resolution) before lengths and gradients."""
    n = len(xy)
    k = max(1, int(round(window_m / 2 / cell)))
    if n < 3:
        return np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))] if n > 1 else np.zeros(n)
    c = np.vstack([np.zeros((1, 2)), np.cumsum(xy, axis=0)])
    lo = np.clip(np.arange(n) - k, 0, n)
    hi = np.clip(np.arange(n) + k + 1, 0, n)
    sm = (c[hi] - c[lo]) / (hi - lo)[:, None]
    return np.r_[0.0, np.cumsum(np.hypot(*np.diff(sm, axis=0).T))]


def running_min(z: np.ndarray) -> np.ndarray:
    """Downstream running minimum (a water-surface profile never rises): removes bridges and bank hits."""
    zz = np.where(np.isfinite(z), z, np.inf)
    out = np.minimum.accumulate(zz)
    out[~np.isfinite(out)] = np.nan
    return out


def steepest(s: np.ndarray, z: np.ndarray, win: float) -> float:
    """Largest drop per km over any ``win``-metre stretch of the profile (``s`` distance, ``z`` elevation)."""
    if len(s) < 2 or s[-1] - s[0] < 0.9 * win:
        return float("nan")
    j = np.searchsorted(s, s + win, side="right") - 1
    ok = (s[j] - s) >= 0.9 * win
    if not ok.any():
        return float("nan")
    return float(np.max((z[ok] - z[j[ok]]) / (s[j[ok]] - s[ok])) * 1000.0)


def longest_flat(s: np.ndarray, z: np.ndarray, tol: float = 0.05) -> float:
    """Longest stretch (m) over which the profile drops less than ``tol`` m."""
    if len(s) < 2:
        return 0.0
    best, i = 0.0, 0
    for j in range(len(s)):
        while z[i] - z[j] >= tol:
            i += 1
        best = max(best, s[j] - s[i])
    return float(best)


# ---------------------------------------------------------------------------------------------- 1 m long profiles
# Code of a profile station (rf-lxa.3, ``lidarspec.PROFILE_STORE``): the 1 m cell the station's height comes from
# is bed or dry ground, hydro-flattened water or planar (interpolated) water; or there is no 1 m cell within 2 m and
# the height is the 2 m grid's (the seam-corrected 8 m fallback, or a lake fill).
W_BED, W_FLAT, W_PLANAR, W_COARSE = 0, 1, 2, 3
STEP_BED, STEP_RISER, STEP_PLANAR, STEP_COARSE = 0, 1, 2, 3   # step kinds (``profile_steps``)


def _ffill(z: np.ndarray) -> np.ndarray:
    """NaN replaced by the last finite value before it (leading NaN by the first finite one); all NaN stays NaN."""
    z = np.asarray(z, np.float64)
    ok = np.isfinite(z)
    if not ok.any():
        return z.copy()
    idx = np.where(ok, np.arange(len(z)), 0)
    np.maximum.accumulate(idx, out=idx)
    out = z[idx]
    out[:np.argmax(ok)] = z[np.argmax(ok)]
    return out


def profile_steps(z: np.ndarray, w: np.ndarray | None = None, ds: float = 1.0, win: float = 4.0,
                  min_slope: float = 0.25, excess: float = 1.0, trend: float = 100.0, min_h: float = 1.0,
                  gap: float = 3.0, ctx: float = 10.0) -> list[dict]:
    """Steps (falls, ledges, steep chutes) in a 1 m long profile ``z`` (downstream order, one station every ``ds``
    m; the downstream running minimum is applied here) with water codes ``w`` (``W_*``).

    A ``win`` m window (4 m: the lowest-cell window spreads a vertical face over ~4 m) is steep where the profile
    drops at least ``min_slope * win`` in it (1 m) and at least ``excess`` m more than the local gradient over
    ``trend`` m predicts: a step stands out from its own channel, so an evenly steep headwater has none. Steep
    windows are trimmed to their metres steeper than max(``min_slope`` / 2, 1.5 x the local gradient), runs closer
    than ``gap`` m are merged, and a step is a run that drops at least ``min_h``. Kind: ``STEP_RISER`` where both
    ``ctx`` m above and below are mostly hydro-flattened water (a survey breakline between treads, not a fall),
    ``STEP_PLANAR`` where the step itself is mostly interpolated water, ``STEP_COARSE`` where a station of it is not
    on 1 m data (fallback, lake fill, a seam), else ``STEP_BED``. Returns one dict per step: station range
    ``i0``-``i1``, ``s0``/``s1`` (m from the first
    station), ``h`` (m), ``h_excess`` (m above the local gradient), ``len`` (m), ``slope_max`` (steepest 2 m, m/m),
    ``z_top`` (m), ``kind``."""
    zf = _ffill(running_min(_ffill(z)))
    n = len(zf)
    if n < 2 or not np.isfinite(zf).all():
        return []
    w = np.zeros(n, np.uint8) if w is None else np.asarray(w, np.uint8)
    d = np.maximum(zf[:-1] - zf[1:], 0.0)
    c = np.r_[0.0, np.cumsum(d)]
    k = max(1, int(round(win / ds)))
    i = np.arange(n - 1)
    j = np.minimum(i + k, n - 1)
    ht = max(1, int(round(trend / 2 / ds)))
    lo, hi = np.maximum(i - ht, 0), np.minimum(i + ht, n - 1)
    grad = (c[hi] - c[lo]) / ((hi - lo) * ds)                              # local gradient, m/m
    drop = c[j] - c[i]
    steep = (drop >= min_slope * win) & (drop - grad * (j - i) * ds >= excess)
    cover = np.zeros(n + 1, np.int64)
    np.add.at(cover, i[steep], 1)
    np.add.at(cover, j[steep], -1)
    on = (np.cumsum(cover)[:n - 1] > 0) & (d >= np.maximum(0.5 * min_slope, 1.5 * grad) * ds)
    idx = np.flatnonzero(on)
    if not len(idx):
        return []
    g = max(1, int(round(gap / ds)))
    cut = np.flatnonzero(np.diff(idx) > g + 1)
    starts = np.r_[idx[0], idx[cut + 1]]
    ends = np.r_[idx[cut], idx[-1]] + 1
    kc = max(1, int(round(ctx / ds)))
    k2 = max(1, int(round(2.0 / ds)))
    out = []
    for a, b in zip(starts, ends, strict=True):
        h = float(zf[a] - zf[b])
        if h < min_h:
            continue
        x = np.arange(a, b)
        sm = float(np.max(c[np.minimum(x + k2, b)] - c[x]) / (k2 * ds))
        up, dn, mid = w[max(0, a - kc):a], w[b + 1:b + 1 + kc], w[a:b + 1]
        if (mid == W_COARSE).any():
            kind = STEP_COARSE
        elif len(up) and len(dn) and (up == W_FLAT).mean() >= 0.5 and (dn == W_FLAT).mean() >= 0.5:
            kind = STEP_RISER
        elif (mid == W_PLANAR).mean() >= 0.5:
            kind = STEP_PLANAR
        else:
            kind = STEP_BED
        he = h - float(grad[(a + b) // 2]) * (b - a) * ds
        out.append({"i0": int(a), "i1": int(b), "s0": a * ds, "s1": b * ds, "h": round(h, 2),
                    "h_excess": round(he, 2), "len": round((b - a) * ds, 1), "slope_max": round(sm, 3),
                    "z_top": round(float(zf[a]), 2), "kind": kind})
    return out


def reach_profile(z: np.ndarray, zm: np.ndarray, w: np.ndarray, ds: float = 1.0) -> dict:
    """Per-reach long-profile fields (``lidarspec``, stage ``1m``) from its stations: raw heights ``z`` (lowest 1 m
    cell within 2 m), the running minimum ``zm`` along the link, station codes ``w``. ``zm`` may hold one more value
    than ``z``: the next reach's first station, so the drops of a link's reaches add up. For windows longer than the
    reach, the steepest stretch is the reach gradient."""
    from .xsec1m import profile_metrics

    zm = np.asarray(zm, np.float64)
    w = np.asarray(w, np.uint8)
    n = len(z)
    s = np.arange(len(zm)) * ds
    L = max(float(s[-1]), ds)
    ok = np.isfinite(zm)
    lid = float((w != W_COARSE).mean()) if n else 0.0
    out = {"length_profile_m": round(L, 1), "lidar_share": round(lid, 4),
           "dem_source": "lidar" if lid >= 0.95 else ("8m" if lid < 0.05 else "mixed"),
           "dem_res_m": 1.0 if lid >= 0.95 else 8.0}
    if ok.sum() < 2:
        return out | {"z_top": np.nan, "z_bot": np.nan, "gradient_m_per_km": np.nan}
    zt, zb = float(zm[ok][0]), float(zm[ok][-1])
    grad = (zt - zb) / L * 1000.0
    zz = _ffill(zm)
    out.update(z_top=round(zt, 2), z_bot=round(zb, 2), gradient_m_per_km=round(grad, 2))
    for win in (50, 100, 200):
        v = steepest(s, zz, win) if L >= 0.9 * win else grad
        out[f"steepest_{win}m_m_per_km"] = round(float(v if np.isfinite(v) else grad), 2)
    k = int(round(50 / ds))                         # is the steepest 50 m on water (riser, interpolation)?
    i = int(np.argmax(zz[:-k] - zz[k:])) if len(zz) > k else 0
    ww = w[i:min(i + k, n)]
    out["steep50_water"] = bool(len(ww) and np.isin(ww, (W_FLAT, W_PLANAR)).mean() >= 0.5)
    out["longest_flat_m"] = round(longest_flat(s, zz, 0.05), 1)
    for name, code in (("water_flat_share", W_FLAT), ("water_planar_share", W_PLANAR)):
        out[name] = round(float((w == code).mean()), 4) if n else np.nan
    rough, conc, _, _ = profile_metrics(s[:n], np.asarray(z, np.float64))
    out["x1_rough"], out["x1_dropconc"] = rough, conc
    return out


def reach_steps(steps: list[dict], drop_m: float) -> dict:
    """Per-reach step fields (``lidarspec``) from the steps assigned to the reach (``profile_steps``, ``s0`` from
    the reach's top). Counts and heights are of bed steps; ``risers_n`` counts survey breaklines on flattened water."""
    bed = [s for s in steps if s["kind"] == STEP_BED]
    h = np.array([s["h"] for s in bed], float)
    top = bed[int(np.argmax(h))] if len(bed) else None
    return {"steps_n": int(len(h)), "steps_drop_m": round(float(h.sum()), 2),
            "step_max_m": round(float(h.max()), 2) if len(h) else 0.0,
            "step_max_s_m": round(float(top["s0"]), 1) if top else np.nan,
            "steps_share": round(float(h.sum() / drop_m), 4) if drop_m > 0.5 else np.nan,
            "drops_n": int((h >= 2.0).sum()), "falls1m_n": int((h >= 5.0).sum()),
            "risers_n": int(sum(s["kind"] == STEP_RISER for s in steps))}


def encode_profiles(reach: np.ndarray, z: np.ndarray, w: np.ndarray) -> dict:
    """The stored form of 1 m long profiles (``lidarspec.PROFILE_STORE``). Stations grouped by ``reach`` (station
    order within a reach) -> one row per reach: ``gid``, ``n`` stations, ``z0_cm`` (int32, first height in cm),
    ``dz`` (int16 cm differences to the station before, n - 1 values) and ``w4`` (the 2-bit station codes ``W_*``,
    four per byte). Heights are kept to 1 cm. Every station on a reach has a height (the 2 m grid's where no 1 m cell
    is near, code ``W_COARSE``); a missing one (NaN) repeats the height before it and is coded ``W_COARSE``."""
    reach = np.asarray(reach)
    cut = np.flatnonzero(np.r_[True, reach[1:] != reach[:-1], True])
    gid, n, z0, dz, w4 = [], [], [], [], []
    for a, b in zip(cut[:-1], cut[1:], strict=True):
        q = np.nan_to_num(_ffill(np.round(np.asarray(z[a:b], np.float64) * 100.0)), nan=0.0).astype(np.int64)
        ww = np.where(np.isfinite(z[a:b]), w[a:b], W_COARSE).astype(np.uint8)
        p = np.r_[ww, np.zeros((-len(ww)) % 4, np.uint8)].reshape(-1, 4)
        gid.append(int(reach[a]))
        n.append(b - a)
        z0.append(int(q[0]))
        dz.append(np.clip(np.diff(q), -32767, 32767).astype(np.int16))
        w4.append((p[:, 0] | (p[:, 1] << 2) | (p[:, 2] << 4) | (p[:, 3] << 6)).astype(np.uint8))
    return {"gid": np.array(gid, np.int64), "n": np.array(n, np.int32), "z0_cm": np.array(z0, np.int32),
            "dz": dz, "w4": w4}


def decode_profiles(t: dict) -> dict:
    """Inverse of ``encode_profiles``: per station ``reach``, ``z`` (float32 m) and ``w``."""
    rs, zs, ws = [np.zeros(0, np.int64)], [np.zeros(0, np.float32)], [np.zeros(0, np.uint8)]
    for g, n, z0, dz, w4 in zip(t["gid"], t["n"], t["z0_cm"], t["dz"], t["w4"], strict=True):
        q = np.r_[np.int64(z0), np.int64(z0) + np.cumsum(np.asarray(dz, np.int64))]
        b = np.asarray(w4, np.uint8)
        w = np.stack([b & 3, (b >> 2) & 3, (b >> 4) & 3, (b >> 6) & 3], axis=1).ravel()[:n].astype(np.uint8)
        z = (q / 100.0).astype(np.float32)
        rs.append(np.full(n, g, np.int64))
        zs.append(z)
        ws.append(w)
    return {"reach": np.concatenate(rs), "z": np.concatenate(zs), "w": np.concatenate(ws)}
