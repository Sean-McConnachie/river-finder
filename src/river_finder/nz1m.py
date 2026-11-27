"""Per-tile job of the national 1 m LiDAR rebuild (rf-lxa.6, ``docs/nz1m-tile-job.md``).

One 32 km tile (``q<ix>y<iy>``, a quarter of a 64 km tile of ``nz.py``) with a 3 km halo, in one run:

1. **Stream** the box's 1 m mosaic at full resolution (block-aligned windows, many threads; overviews never read) into
   the 2 m block mean and a per-cell code of valid, hydro-flattened and planar 1 m cells
   (``lidarpipe.agg2_into``, run inside the reader threads: rf-lxa.7). The 8 m contour DEM of the box is fetched at
   the same time (memory only).
2. **Sea and lakes**: surveyed water outside the 8 m DEM's land (harbours the survey flattened) becomes no data, so
   it is an outlet (``_mask_sea``); no-data holes whose rim is hydro-flattened water and which are land in the 8 m
   DEM (the sea is not) are filled flat at the rim's LiDAR water level (``lidarpipe.fill_lakes`` rule,
   ``fill_holes``).
3. **Fallback**: other land holes from the 8 m DEM (bilinear), corrected towards the LiDAR by the seam rule with the
   edge blend (``seam_fill``: ``lidarpipe.seam_correct(..., edge_m=25)`` worked in 2 m chunks).
4. A float32 copy of this DEM is kept (the router fills in place): gorge depth and the 2 m part of the sections
   read it.
5. **Route** at 2 m (``lidarpipe.route_lean``; REC2 border outlets), **accumulate** (uint32 cell counts) with REC2
   inflows at the box edge (held sparsely, only along their paths), **channels** of 0.5 km2 and **reaches** cut every
   ~250 m of 20 m-smoothed length.
6. **2 m pass**: each cell's first reach (local catchment), per reach the local sums (area, mean / highest elevation,
   flat, hard rock, LiDAR, lake) and the HAND counts; the 10 m local-catchment, area-code and HAND rasters; gorge
   depth, seam distance, lake shares.
7. **1 m pass**: the 1 m blocks along the reaches re-read in 8 km chunks; long profiles (a station every 1 m of
   smoothed length, lowest 1 m cell within 2 m), steps, and the rf-lxa.4 cross-sections every 25 m on reaches of
   1 km2 or more (``xsec1m.extract(..., search=10)``, the 2 m DEM for the 152-500 m part).
8. **Write** the per-tile files of ``lidarspec.TILE_FILES`` and ``tile.json`` (the done flag, last).

Areas here are in-tile (local accumulation plus REC2 inflows, as the benchmark): they pick the channels and the
section reaches; the real areas come from the national merge (rf-lxa.5) from ``local_km2``. Nothing at 1 m or 2 m is
written to disk. Outputs: ``data/nz1m/tiles/<qid>/``; ``plan`` writes ``data/nz1m/plan.json``.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from numba import njit

from . import lidarpipe as P
from . import lidarspec as S
from . import xsec1m as X1
from .regions import REPO_ROOT
from .routing import DC, DR

CELL = 2.0
F8 = 4                      # 2 m cells per 8 m cell
F10 = 5                     # 2 m cells per 10 m cell
TILE_M = S.TILE_M           # 32 km core
HALO_M = S.HALO_M           # 3 km halo
NX, NY = 32, 46             # the 32 km grid on nz.X0, nz.Y0 (twice nz.NX, nz.NY)
MIN_AREA_KM2 = 0.5          # channel initiation (in-tile area)
SEG_LEN_M = 250.0           # reach length (smoothed)
SMOOTH_M = 20.0
SEC_MIN_KM2 = 1.0           # sections on reaches of this in-tile area or more
SEC_SEARCH_M = 10           # section re-centring window on the 2 m lines (rf-lxa.4)
SEC_READ_M = 200.0          # 1 m read radius around section stations (150 m + search + refine + margin)
PROF_READ_M = 4.0           # ... around profile stations (2 m window + rounding)
PROF_RAD = 2                # lowest 1 m cell within 2 m of a station (5 x 5)
CHUNK_M = 8000.0            # 1 m re-read chunks
HAND_TH = (1.0, 2.0, 5.0, 10.0, 20.0)
GORGE_M = 150.0
SEAM_CAP_M = 5000.0
LAKE_MIN_KM2 = 0.1          # hydro-flattened water bodies kept as lakes
SEA_MAX_Z = 10.0            # surveyed water outside the 8 m land below this is sea
BUILD = "lidar1m-dev"
W_LAKE = int(P.W2_LAKE)
W_FALLBACK = int(P.W2_FALLBACK)


def root_dir() -> Path:
    return REPO_ROOT / "data" / "nz1m"


# ---------------------------------------------------------------------------------------------- tiles
@dataclass(frozen=True)
class Tile32:
    ix: int
    iy: int

    @property
    def id(self) -> str:
        return S.tile_id(self.ix, self.iy)

    @property
    def index(self) -> int:
        return S.tile_index(self.ix, self.iy)

    @property
    def core(self) -> tuple[float, float, float, float]:
        from .nz import X0, Y0

        x, y = X0 + self.ix * TILE_M, Y0 + self.iy * TILE_M
        return (x, y, x + TILE_M, y + TILE_M)

    @property
    def box(self) -> tuple[float, float, float, float]:
        x0, y0, x1, y1 = self.core
        return (x0 - HALO_M, y0 - HALO_M, x1 + HALO_M, y1 + HALO_M)

    @property
    def parent(self) -> str:
        """The 64 km tile of ``nz.py`` this tile is a quarter of."""
        return f"x{self.ix // 2:02d}y{self.iy // 2:02d}"

    @staticmethod
    def parse(s: str) -> Tile32:
        if not (len(s) == 6 and s[0] == "q" and s[3] == "y"):
            raise ValueError(f"not a 32 km tile id: {s!r} (q<ix>y<iy>, e.g. q11y15)")
        return Tile32(int(s[1:3]), int(s[4:6]))


def plan(root: Path | None = None, log=print) -> dict:
    """The land tiles of the 32 km grid (a REC2 reach ends in the core, as ``nz.land_tiles``), north to south, with
    their land and LiDAR area from the 1 km gap grid of ``lidar coverage`` when it exists -> ``plan.json``."""
    import pyogrio

    from .national import nz_dir
    from .nz import X0, Y0

    rec = pyogrio.read_dataframe(nz_dir() / "rec2.gpkg", columns=["downcoordX", "downcoordY"], read_geometry=False)
    kx = ((rec["downcoordX"].to_numpy() - X0) // TILE_M).astype(int)
    ky = ((rec["downcoordY"].to_numpy() - Y0) // TILE_M).astype(int)
    keys, counts = np.unique(np.column_stack([kx, ky]), axis=0, return_counts=True)
    gp = REPO_ROOT / "data" / "analysis" / "lidar" / "gap_grid.npz"
    grid = np.load(gp) if gp.exists() else None
    tiles = []
    for (ix, iy), n in zip(keys, counts, strict=True):
        if not (0 <= ix < NX and 0 <= iy < NY):
            continue
        t = Tile32(int(ix), int(iy))
        rec_t = {"id": t.id, "ix": t.ix, "iy": t.iy, "parent": t.parent, "core": list(t.core), "box": list(t.box),
                 "rec2_reaches": int(n)}
        if grid is not None:
            nb = int(TILE_M / float(grid["cell_m"]))
            r0 = int(round((float(grid["y1"]) - t.core[3]) / float(grid["cell_m"])))
            c0 = int(round((t.core[0] - float(grid["x0"])) / float(grid["cell_m"])))
            cm2 = float(grid["dem_cell_m"]) ** 2 / 1e6
            land = float(grid["land"][r0:r0 + nb, c0:c0 + nb].sum()) * cm2
            gap = float(grid["gap"][r0:r0 + nb, c0:c0 + nb].sum()) * cm2
            rec_t.update(land_km2=round(land, 1), lidar_km2=round(land - gap, 1))
        tiles.append(rec_t)
    tiles.sort(key=lambda r: (-r["iy"], r["ix"]))
    out = {"build": BUILD, "tile_m": TILE_M, "halo_m": HALO_M, "cell_m": CELL, "tiles": tiles, "n_tiles": len(tiles)}
    if grid is not None:
        out["land_km2"] = round(sum(r.get("land_km2", 0) for r in tiles), 1)
        out["lidar_km2"] = round(sum(r.get("lidar_km2", 0) for r in tiles), 1)
    d = root or root_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "plan.json").write_text(json.dumps(out, indent=1))
    log(f"nz1m plan: {len(tiles)} land tiles of 32 km -> {d / 'plan.json'}")
    return out


# ---------------------------------------------------------------------------------------------- resources
def _rss() -> int:
    with open("/proc/self/statm") as f:
        return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")


def trim() -> None:
    """Free unreachable arrays now (``gc.collect``: big arrays caught in reference cycles otherwise wait for the
    next collection, 3 GB on the first tile) and give freed heap memory back to the system (glibc ``malloc_trim``:
    the reader threads' arenas otherwise keep the freed 1 m windows, ~2 GB after the stream)."""
    import gc

    gc.collect()
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


def _written() -> int:
    try:
        with open("/proc/self/io") as f:
            for line in f:
                if line.startswith("write_bytes:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


class Meter:
    """Wall time, peak resident memory (sampled every 50 ms by a thread) and bytes written to disk per step."""

    def __init__(self, log=None):
        self.steps: dict[str, dict] = {}
        self.peak = 0
        self._step_peak = 0
        self._run = True
        self.log = log
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while self._run:
            r = _rss()
            self._step_peak = max(self._step_peak, r)
            self.peak = max(self.peak, r)
            time.sleep(0.05)

    @contextmanager
    def step(self, name: str):
        self._step_peak = _rss()
        t, w = time.time(), _written()
        yield
        r = _rss()
        self.peak = max(self.peak, r)
        self.steps[name] = {"s": round(time.time() - t, 1), "peak_gb": round(max(self._step_peak, r) / 1e9, 2),
                            "end_gb": round(r / 1e9, 2), "written_mb": round((_written() - w) / 1e6, 1)}
        if self.log:
            v = self.steps[name]
            self.log(f"  {name}: {v['s']} s, peak {v['peak_gb']} GB, written {v['written_mb']} MB")

    def close(self) -> dict:
        self._run = False
        return {"peak_gb": round(self.peak / 1e9, 2), "seconds": round(sum(v["s"] for v in self.steps.values()), 1)}


# ---------------------------------------------------------------------------------------------- inputs
class MosaicSource:
    """The national 1 m mosaic over a box: sheets, read jobs and the window reader."""

    def __init__(self, box, cache: Path | None = None):
        self.sheets = P.mosaic_sheets(box, cache or REPO_ROOT / "data" / "_stac")
        self.read = P.window_reader()

    def box_jobs(self, box) -> list[tuple]:
        return P.sheet_windows(self.sheets, box, win=2048)

    def mask_jobs(self, box, need, cell: int = 64) -> list[tuple]:
        return X1.mask_jobs(self.sheets, box, need, cell=cell)


class ArraySource(MosaicSource):
    """A 1 m array over a box acting as one mosaic sheet (tests, synthetic runs)."""

    def __init__(self, z1: np.ndarray, box):
        self.z1 = z1
        self.sheets = [{"id": "array", "href": "array", "bounds": tuple(float(v) for v in box)}]

    def read(self, job):
        _href, c, r, w, h, br, bc = job
        return self.z1[r:r + h, c:c + w].astype(np.float32), br, bc


def fetch_dem8(box, cache: Path | None = None) -> np.ndarray:
    """The 8 m contour DEM on the box's 8 m grid (float32, NaN = no data: the sea), read into memory."""
    import rasterio
    from pyproj import Transformer
    from rasterio.enums import Resampling
    from rasterio.merge import merge

    from .nz import DEM_8M
    from .stac import GDAL_ENV, NODATA, tiles_for_bbox

    bb = Transformer.from_crs(2193, 4326, always_xy=True).transform_bounds(*box, densify_pts=21)
    tiles = tiles_for_bbox(DEM_8M, bb, cache or REPO_ROOT / "data" / "_stac")
    H, W = int(round((box[3] - box[1]) / 8)), int(round((box[2] - box[0]) / 8))
    if not tiles:
        return np.full((H, W), np.nan, np.float32)
    with rasterio.Env(**GDAL_ENV):
        arr, tf = merge(["/vsicurl/" + t["href"] for t in tiles], bounds=box, res=8.0, nodata=NODATA,
                        dtype="float32", resampling=Resampling.bilinear)
    z = arr[0]
    if z.shape != (H, W) or abs(tf.c - box[0]) > 1e-6 or abs(tf.f - box[3]) > 1e-6:
        raise RuntimeError(f"8 m DEM grid {z.shape} {tf} does not match the box {box}")
    z[z <= NODATA + 1] = np.nan
    return np.ascontiguousarray(z, np.float32)


def rec2_near(box, pad: float = 5e4):
    import geopandas as gpd

    from .national import nz_dir

    x0, y0, x1, y1 = box
    return gpd.read_file(nz_dir() / "rec2.gpkg", bbox=(x0 - pad, y0 - pad, x1 + pad, y1 + pad))


def geology_near(box):
    import geopandas as gpd

    from .national import nz_dir

    p = nz_dir() / "geology.gpkg"
    return gpd.read_file(p, bbox=tuple(box)) if p.exists() else None


# ---------------------------------------------------------------------------------------------- kernels
@njit(cache=True)
def _bilin(a, r, c, f):
    """Value of the coarse grid ``a`` (cells ``f`` fine cells wide, aligned at the origin) at the centre of fine cell
    ``r, c``: bilinear over its valid neighbours (weights renormalised), NaN when none."""
    R, C = a.shape
    u = (r + 0.5) / f - 0.5
    v = (c + 0.5) / f - 0.5
    r0 = int(math.floor(u))
    c0 = int(math.floor(v))
    wu = u - r0
    wv = v - c0
    s = 0.0
    ws = 0.0
    for dr in range(2):
        rr = min(max(r0 + dr, 0), R - 1)
        a_r = wu if dr == 1 else 1.0 - wu
        for dc in range(2):
            cc = min(max(c0 + dc, 0), C - 1)
            w = a_r * (wv if dc == 1 else 1.0 - wv)
            x = a[rr, cc]
            if w > 0 and np.isfinite(x):
                s += w * x
                ws += w
    return s / ws if ws > 1e-9 else np.nan


@njit(cache=True)
def _mask_sea(z, wc, z8, f8, max_z):
    """Surveyed sea to no data, in place: cells outside the 8 m DEM's land (it has no sea) whose 1 m cells are
    mostly hydro-flattened or planar water and which lie below ``max_z`` (harbours and coastal water the survey
    flattened: left as data, they are not an outlet and the channels run on across them). Returns the count."""
    H, W = z.shape
    n = 0
    for r in range(H):
        for c in range(W):
            v = z[r, c]
            if not np.isfinite(v) or v >= max_z or np.isfinite(z8[r // f8, c // f8]):
                continue
            w = wc[r, c]
            if w >= 125:
                continue
            k = w // 25
            if k > 0 and 2 * ((w % 25) // 5 + w % 5) >= k:
                z[r, c] = np.nan
                wc[r, c] = 252
                n += 1
    return n


@njit(cache=True)
def _bilin_block(a, f, r0, r1, c0, c1):
    out = np.empty((r1 - r0, c1 - c0), np.float32)
    for r in range(r0, r1):
        for c in range(c0, c1):
            out[r - r0, c - c0] = _bilin(a, r, c, f)
    return out


@njit(cache=True)
def _gap_counts(z, wc, z8, f8, fw):
    """Per work cell (``fw`` x ``fw`` 2 m cells): gap cells (no data where the nearest 8 m cell is land) and data
    cells (LiDAR or lake fill; lake fills count as data, as in the benchmark)."""
    H, W = z.shape
    Hc, Wc = (H + fw - 1) // fw, (W + fw - 1) // fw
    ngap = np.zeros((Hc, Wc), np.int32)
    nlid = np.zeros((Hc, Wc), np.int32)
    for r in range(H):
        R = r // fw
        for c in range(W):
            if np.isfinite(z[r, c]):
                if wc[r, c] != 251:
                    nlid[R, c // fw] += 1
            elif np.isfinite(z8[r // f8, c // f8]):
                ngap[R, c // fw] += 1
    return ngap, nlid


@njit(cache=True)
def _seam_d(z, wc, z8, f8, fw, near):
    """Sum and count of data minus 8 m (bilinear) per work cell, on the work cells marked in ``near``."""
    H, W = z.shape
    Hc, Wc = near.shape
    dsum = np.zeros((Hc, Wc), np.float64)
    dcnt = np.zeros((Hc, Wc), np.int32)
    for R in range(Hc):
        for C in range(Wc):
            if not near[R, C]:
                continue
            for r in range(R * fw, min(R * fw + fw, H)):
                for c in range(C * fw, min(C * fw + fw, W)):
                    v = z[r, c]
                    if not np.isfinite(v) or wc[r, c] == 251:
                        continue
                    zc = _bilin(z8, r, c, f8)
                    if np.isfinite(zc):
                        dsum[R, C] += v - zc
                        dcnt[R, C] += 1
    return dsum, dcnt


@njit(cache=True)
def _blocks(z, wc, f):
    """Per ``f`` x ``f`` block of the 2 m grid: mean and maximum of ``z`` (NaN where no data), and counts of valid 1 m
    cells, flattened 1 m cells, LiDAR 2 m cells, lake-filled and fallback 2 m cells."""
    H, W = z.shape
    Hb, Wb = (H + f - 1) // f, (W + f - 1) // f
    zm = np.full((Hb, Wb), np.nan, np.float32)
    zx = np.full((Hb, Wb), np.nan, np.float32)
    cnt1 = np.zeros((Hb, Wb), np.int32)
    flat1 = np.zeros((Hb, Wb), np.int32)
    nlid = np.zeros((Hb, Wb), np.int32)
    nlake = np.zeros((Hb, Wb), np.int32)
    nfb = np.zeros((Hb, Wb), np.int32)
    for R in range(Hb):
        for C in range(Wb):
            s = 0.0
            k = 0
            mx = -np.inf
            for r in range(R * f, min(R * f + f, H)):
                for c in range(C * f, min(C * f + f, W)):
                    v = z[r, c]
                    w = wc[r, c]
                    if w == 250:
                        nlake[R, C] += 1
                    elif w == 251:
                        nfb[R, C] += 1
                    elif w < 125:
                        n1 = w // 25
                        cnt1[R, C] += n1
                        flat1[R, C] += (w % 25) // 5
                        if n1 >= 2:
                            nlid[R, C] += 1
                    if np.isfinite(v):
                        s += v
                        k += 1
                        if v > mx:
                            mx = v
            if k > 0:
                zm[R, C] = s / k
                zx[R, C] = mx
    return zm, zx, cnt1, flat1, nlid, nlake, nfb


@njit(cache=True)
def _block_mean(z, f, need):
    H, W = z.shape
    Hb, Wb = H // f, W // f
    out = np.full((Hb, Wb), np.nan, np.float32)
    for R in range(Hb):
        for C in range(Wb):
            s = 0.0
            k = 0
            for r in range(R * f, R * f + f):
                for c in range(C * f, C * f + f):
                    v = z[r, c]
                    if np.isfinite(v):
                        s += v
                        k += 1
            if k >= need:
                out[R, C] = s / k
    return out


@njit(cache=True)
def _accumulate_u32(d):
    """Upstream cell counts (uint32, the cell itself included; 0 on nodata) without a queue, as
    ``lidarpipe._accumulate_lean``. Returns (counts, cells done)."""
    nr, nc = d.shape
    n = nr * nc
    df = d.ravel()
    off = np.empty(8, np.int64)
    for k in range(8):
        off[k] = DR[k] * nc + DC[k]
    indeg = np.zeros(n, np.uint8)
    acc = np.zeros(n, np.uint32)
    for i in range(n):
        k = df[i]
        if k == 9:
            continue
        acc[i] = 1
        if k < 8:
            indeg[i + off[k]] += 1
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
            indeg[rcv] -= 1
            if indeg[rcv] != 0:
                break
            v = rcv
    return acc.reshape(nr, nc), done


@njit(cache=True)
def _walk(d, start, cap):
    nr, nc = d.shape
    df = d.ravel()
    out = np.empty(cap, np.int64)
    k = 0
    v = start
    while k < cap:
        out[k] = v
        k += 1
        c = df[v]
        if c >= 8:
            break
        v = v + DR[c] * nc + DC[c]
    return out[:k]


@njit(cache=True)
def _chan_mask(d, cnt, thr, ext_c, ext_v):
    """Channel cells: in-tile cell count plus the inflow cells along their paths (``ext_c`` sorted) >= ``thr``."""
    n = d.size
    df = d.ravel()
    cf = cnt.ravel()
    ch = np.zeros(n, np.bool_)
    j = 0
    m = len(ext_c)
    for i in range(n):
        if df[i] == 9:
            continue
        a = np.float64(cf[i])
        while j < m and ext_c[j] < i:
            j += 1
        if j < m and ext_c[j] == i:
            a += ext_v[j]
        if a >= thr:
            ch[i] = True
    return ch.reshape(d.shape)


@njit(cache=True)
def _links_mask(d, chan):
    """``lidarpipe._links`` on a channel mask: links upstream to downstream (heads in raster order), CSR packed, and
    the first channel cell below each link (-1 none)."""
    nr, nc = d.shape
    n = nr * nc
    df = d.ravel()
    cf = chan.ravel()
    ndon = np.zeros(n, np.uint8)
    nchan = 0
    for i in range(n):
        if cf[i]:
            nchan += 1
            k = df[i]
            if k < 8:
                j = i + DR[k] * nc + DC[k]
                if cf[j]:
                    ndon[j] += 1
    cells = np.empty(nchan, np.int64)
    ptr = np.empty(nchan + 1, np.int64)
    nxt = np.empty(nchan, np.int64)
    nl = 0
    kk = 0
    for h in range(n):
        if not cf[h] or ndon[h] == 1:
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
            if not cf[r]:
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
def _area10(cnt, f):
    H, W = cnt.shape
    Hb, Wb = H // f, W // f
    out = np.zeros((Hb, Wb), np.uint32)
    for r in range(Hb * f):
        R = r // f
        for c in range(Wb * f):
            v = cnt[r, c]
            if v > out[R, c // f]:
                out[R, c // f] = v
    return out


@njit(cache=True)
def _pass2m(d, zf, soc, keep, wc, flat10, hard10, lakew10, f10, hand_th, nseg, lc10, hand10):
    """The 2 m pass: each cell's first reach cell downstream (path compression), then per kept reach the local sums
    and HAND counts, and the 10 m local-catchment (reach row, -1) and HAND (0.25 m steps, 255 = higher or none)
    values at the 10 m cell centres. Returns (n, zsum, zmax, nlidar, nlake, nflat, nhard, hand counts, cells with no
    reach, cells of reaches not kept)."""
    nr, nc = d.shape
    n = nr * nc
    df = d.ravel()
    zz = zf.ravel()
    sc = soc.ravel()
    wf = wc.ravel()
    off = np.empty(8, np.int64)
    for k in range(8):
        off[k] = DR[k] * nc + DC[k]
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
            v = v + off[df[v]]
        if sc[v] >= 0:
            t = v
        elif tgt[v] != -2:
            t = tgt[v]
        else:
            t = -1
        tgt[v] = t
        for j in range(k):
            tgt[stack[j]] = t
    nth = len(hand_th)
    cnt = np.zeros(nseg, np.int64)
    zsum = np.zeros(nseg, np.float64)
    zmax = np.full(nseg, -np.inf, np.float32)
    nlid = np.zeros(nseg, np.int64)
    nlake = np.zeros(nseg, np.int64)
    nflat = np.zeros(nseg, np.int64)
    nhard = np.zeros(nseg, np.int64)
    hc = np.zeros((nseg, nth), np.int64)
    lost = 0
    other = 0
    half = f10 // 2
    R10, C10 = lc10.shape
    for i in range(n):
        if df[i] == 9:
            continue
        r = i // nc
        c = i - r * nc
        R = r // f10
        C = c // f10
        t = tgt[i]
        s = sc[t] if t >= 0 else -1
        if r % f10 == half and c % f10 == half and R < R10 and C < C10:
            lc10[R, C] = s if (s >= 0 and keep[s]) else -1
            if t >= 0:
                q = (zz[i] - zz[t]) / 0.25
                hand10[R, C] = 255 if q > 254.0 else np.uint8(max(0.0, round(q)))
        if t < 0:
            lost += 1
            continue
        if not keep[s]:
            other += 1
            continue
        cnt[s] += 1
        zv = zz[i]
        zsum[s] += zv
        if zv > zmax[s]:
            zmax[s] = zv
        w = wf[i]
        if w < 125 and w // 25 >= 2:
            nlid[s] += 1
        if R < R10 and C < C10:
            if w == 250 or lakew10[R, C]:
                nlake[s] += 1
            if flat10[R, C]:
                nflat[s] += 1
            if hard10[R, C]:
                nhard[s] += 1
        h = zv - zz[t]
        for q in range(nth):
            if h <= hand_th[q]:
                hc[s, q] += 1
    return cnt, zsum, zmax, nlid, nlake, nflat, nhard, hc, lost, other


@njit(cache=True)
def _station_heights(z, x0, y1, xs, ys, rad):
    """Lowest finite 1 m cell within ``rad`` cells of each station and the water code of that cell (``W_*`` of
    ``lidarpipe``; 3 when no 1 m cell is near)."""
    nr, nc = z.shape
    n = len(xs)
    zo = np.full(n, np.nan, np.float32)
    fo = np.full(n, 3, np.uint8)
    for i in range(n):
        r = int(math.floor(y1 - ys[i]))
        c = int(math.floor(xs[i] - x0))
        best, br, bc = np.inf, -1, -1
        for dr in range(-rad, rad + 1):
            rr = r + dr
            if rr < 0 or rr >= nr:
                continue
            for dc in range(-rad, rad + 1):
                cc = c + dc
                if cc < 0 or cc >= nc:
                    continue
                v = z[rr, cc]
                if np.isfinite(v) and v < best:
                    best, br, bc = v, rr, cc
        if br < 0:
            continue
        zo[i] = best
        fo[i] = P._wflag(z, br, bc, 0.002, 0.01)
    return zo, fo


# ---------------------------------------------------------------------------------------------- lakes and fallback
def fill_holes(z: np.ndarray, wc: np.ndarray, z8: np.ndarray, tf, min_cells: int = 4, rim_flat: float = 0.5,
               rim_mode: float = 0.4, tol: float = 0.02, min_land: float = 0.5, lake_table=None) -> list[dict]:
    """``lidarpipe.fill_lakes`` on the 2 m grid, in place: a no-data hole is a lake when at least ``rim_flat`` of its
    rim is hydro-flattened (half the rim cell's 1 m cells), the modal rim level (to ``tol``) holds ``rim_mode`` of
    those cells and at least ``min_land`` of the hole is land in the 8 m DEM (``z8``, 4 x 4 cells per 8 m cell; the
    sea is not). Lakes are filled flat at that level (or at the level of the ``lake_table`` lake holding the hole:
    ``[(polygon, level), ...]``, the national table of rf-lxa.5) and coded ``W2_LAKE``. Returns one record per hole of
    ``min_cells`` or more, the filled ones with their mask (``mask``: packed bits over ``slice``)."""
    from scipy import ndimage

    H, W = z.shape
    if H % F8 or W % F8:
        raise ValueError("the 2 m grid must be a whole number of 8 m cells")
    land8 = np.isfinite(z8)
    lab, n = ndimage.label(~np.isfinite(z), structure=np.ones((3, 3), bool))
    out = []
    if n == 0:
        return out
    objs = ndimage.find_objects(lab)
    sizes = np.bincount(lab.ravel(), minlength=n + 1)
    for k, sl in enumerate(objs, start=1):
        if sl is None or sizes[k] < min_cells:
            continue
        a0 = max(0, (sl[0].start - 1) // F8 * F8)
        a1 = min(H, -(-(sl[0].stop + 1) // F8) * F8)
        b0 = max(0, (sl[1].start - 1) // F8 * F8)
        b1 = min(W, -(-(sl[1].stop + 1) // F8) * F8)
        s2 = (slice(a0, a1), slice(b0, b1))
        m = lab[s2] == k
        cnt = int(sizes[k])
        sub = z[s2]
        rim = ndimage.binary_dilation(m, structure=np.ones((3, 3), bool)) & ~m & np.isfinite(sub)
        m4 = m.reshape((a1 - a0) // F8, F8, (b1 - b0) // F8, F8).sum(axis=(1, 3))
        land = float((m4 * land8[a0 // F8:a1 // F8, b0 // F8:b1 // F8]).sum() / max(m4.sum(), 1))
        rr, cc = np.unravel_index(int(np.argmax(m)), m.shape)
        rec = {"hole": k, "cells": cnt, "km2": round(cnt * CELL * CELL / 1e6, 4), "land_share": round(land, 3),
               "rim_cells": int(rim.sum()), "row": int(a0 + rr), "col": int(b0 + cc),
               "x": float(tf.c + (b0 + cc + 0.5) * CELL), "y": float(tf.f - (a0 + rr + 0.5) * CELL),
               "bbox": [float(tf.c + b0 * CELL), float(tf.f - a1 * CELL), float(tf.c + b1 * CELL),
                        float(tf.f - a0 * CELL)]}
        if not rim.any():
            out.append(dict(rec, filled=False, rim_flat_share=0.0))
            continue
        _, nf, _ = P.w2_counts(wc[s2][rim])
        nv, _, _ = P.w2_counts(wc[s2][rim])
        fr = (nv > 0) & (2 * nf >= nv)
        rec["rim_flat_share"] = round(float(fr.mean()), 3)
        if fr.mean() >= rim_flat and land >= min_land:
            zr = sub[rim][fr]
            q = np.round(zr / tol).astype(np.int64)
            vals, cts = np.unique(q, return_counts=True)
            j = int(np.argmax(cts))
            share = cts[j] / fr.sum()
            level = float(np.median(zr[q == vals[j]]))
            rec.update(level=round(level, 3), rim_mode_share=round(float(share), 3))
            if lake_table is not None:
                from shapely.geometry import Point

                pt = Point(rec["x"], rec["y"])
                for poly, lev in lake_table:
                    if poly.contains(pt):
                        rec.update(level=round(float(lev), 3), level_from="lake_table")
                        level = float(lev)
                        break
            if share >= rim_mode:
                sub[m] = np.float32(level)
                wc[s2][m] = W_LAKE
                rec.update(filled=True, slice=[a0, a1, b0, b1], mask=np.packbits(m))
        rec.setdefault("filled", False)
        out.append(rec)
    return out


def seam_fill(z: np.ndarray, wc: np.ndarray, z8: np.ndarray, band_m: float = 300.0, sigma_m: float = 150.0,
              taper_m: float = 1000.0, work_m: float = 10.0, edge_m: float = 25.0, edge_band_m: float = 150.0,
              chunk: int = 2048) -> dict:
    """Fill the land holes of the 2 m grid ``z`` (no data where the 8 m DEM has land) in place from the 8 m DEM
    (bilinear), corrected by ``lidarpipe.seam_correct``'s rule with the edge blend: d = LiDAR - 8 m on the LiDAR cells
    within ``band_m`` of the gap, smoothed into the gap (``sigma_m``) on a ``work_m`` grid with a weight falling to 0
    at ``taper_m``; within ``edge_band_m`` of the LiDAR the correction is the d of the nearest LiDAR cell, blended
    into the smooth field with weight exp(-distance / ``edge_m``). The edge part is worked on the 2 m grid in
    ``chunk`` x ``chunk`` windows, so no box-sized distance transform is made. Filled cells are coded
    ``W2_FALLBACK``."""
    from scipy import ndimage

    H, W = z.shape
    fw = max(1, int(round(work_m / CELL)))
    land8 = np.isfinite(z8)
    ngap, nlid = _gap_counts(z, wc, z8, F8, fw)
    gap_c = ngap > 0
    stats = {"gap_cells": int(ngap.sum()), "gap_km2": round(float(ngap.sum()) * CELL * CELL / 1e6, 3)}
    if not gap_c.any():
        return stats

    def gap_of(sub, R0, R1, C0, C1):
        rr = np.arange(R0, R1) // F8
        cc = np.arange(C0, C1) // F8
        return ~np.isfinite(sub) & land8[rr[:, None], cc[None, :]]

    if not (nlid > 0).any():                  # no LiDAR at all: the 8 m DEM as it is
        for r0 in range(0, H, chunk):
            r1 = min(H, r0 + chunk)
            zc = _bilin_block(z8, F8, r0, r1, 0, W)
            g = gap_of(z[r0:r1], r0, r1, 0, W)
            z[r0:r1][g] = zc[g]
            wc[r0:r1][g] = W_FALLBACK
        return stats | {"corrected": False}
    have_c = nlid >= 0.5 * fw * fw
    band = have_c & (ndimage.distance_transform_edt(~gap_c) * work_m <= band_m)
    dsum, dcnt = _seam_d(z, wc, z8, F8, fw, band)
    with np.errstate(invalid="ignore", divide="ignore"):
        dmean = np.where(dcnt > 0, dsum / np.maximum(dcnt, 1), 0.0)
    wt = (band & (dcnt > 0)).astype(np.float64)
    sg = sigma_m / work_m
    num = ndimage.gaussian_filter(dmean * wt, sg, mode="nearest")
    den = ndimage.gaussian_filter(wt, sg, mode="nearest")
    with np.errstate(invalid="ignore", divide="ignore"):
        dext = np.where(den > 1e-6, num / den, 0.0).astype(np.float32)
    dist_gap = ndimage.distance_transform_edt(gap_c) * work_m
    taper = np.clip(1.0 - np.maximum(dist_gap - work_m, 0.0) / taper_m, 0.0, 1.0).astype(np.float32)
    del num, den, wt, dist_gap, dmean
    hb = int(math.ceil(edge_band_m / CELL)) + 2
    nband = 0
    corr_max = 0.0
    for r0 in range(0, H, chunk):
        for c0 in range(0, W, chunk):
            r1, c1 = min(H, r0 + chunk), min(W, c0 + chunk)
            if not gap_c[r0 // fw:(r1 + fw - 1) // fw, c0 // fw:(c1 + fw - 1) // fw].any():
                continue
            R0, R1, C0, C1 = max(0, r0 - hb), min(H, r1 + hb), max(0, c0 - hb), min(W, c1 + hb)
            sub = z[R0:R1, C0:C1]
            zc = _bilin_block(z8, F8, R0, R1, C0, C1)
            gap = gap_of(sub, R0, R1, C0, C1) & np.isfinite(zc)
            inner = np.zeros(gap.shape, bool)
            inner[r0 - R0:r1 - R0, c0 - C0:c1 - C0] = True
            gi = gap & inner
            if not gi.any():
                continue
            src = np.isfinite(sub) & np.isfinite(zc) & (wc[R0:R1, C0:C1] != W_FALLBACK)
            de = _bilin_block(dext, fw, R0, R1, C0, C1)
            tp = _bilin_block(taper, fw, R0, R1, C0, C1)
            corr = de.copy()
            if src.any():
                dist, (ir, ic) = ndimage.distance_transform_edt(~src, return_indices=True)
                dm = dist * CELL
                near = gi & (dm <= edge_band_m)
                if near.any():
                    a = np.exp(-np.maximum(dm[near] - CELL, 0.0) / edge_m)
                    dn = sub[ir[near], ic[near]] - zc[ir[near], ic[near]]
                    corr[near] = (a * dn + (1.0 - a) * de[near]).astype(np.float32)
                    nband += int(near.sum())
                del dist, ir, ic, dm
            val = zc[gi] + tp[gi] * corr[gi]
            corr_max = max(corr_max, float(np.abs(tp[gi] * corr[gi]).max()))
            sub[gi] = val
            wc[R0:R1, C0:C1][gi] = W_FALLBACK
    stats.update(corrected=True, edge_cells=nband, corr_abs_max=round(corr_max, 3),
                 d_band_median=round(float(np.median((dsum / np.maximum(dcnt, 1))[band & (dcnt > 0)])), 3)
                 if (band & (dcnt > 0)).any() else None)
    return stats


# ---------------------------------------------------------------------------------------------- reaches
def inflows_sparse(d: np.ndarray, cnt: np.ndarray, tf, inflows, radius_cells: int):
    """REC2 inflows ``[(x, y, area_km2, id)]``: each snapped to the cell of largest in-tile count within
    ``radius_cells``, its area (in cells) added along its downstream path. Returns the path cells (sorted, unique),
    their added cells, and the inflows used."""
    import rasterio

    nr, nc = d.shape
    cells, vals, used = [], [], []
    for x, y, a, sid in inflows:
        r, c = rasterio.transform.rowcol(tf, x, y)
        r0, r1 = max(0, r - radius_cells), min(nr, r + radius_cells + 1)
        c0, c1 = max(0, c - radius_cells), min(nc, c + radius_cells + 1)
        if r0 >= r1 or c0 >= c1:
            continue
        win = cnt[r0:r1, c0:c1]
        k = int(np.argmax(win))
        if win.flat[k] == 0:
            continue
        rr, cc = r0 + k // win.shape[1], c0 + k % win.shape[1]
        path = _walk(d, rr * nc + cc, d.size)
        cells.append(path)
        vals.append(np.full(len(path), a * 1e6 / (CELL * CELL)))
        used.append({"id": int(sid), "area_km2": round(float(a), 3), "x": round(float(x), 1), "y": round(float(y), 1),
                     "row": int(rr), "col": int(cc), "path_cells": int(len(path))})
    if not cells:
        return np.zeros(0, np.int64), np.zeros(0, np.float64), used
    u, inv = np.unique(np.concatenate(cells), return_inverse=True)
    return u, np.bincount(inv, weights=np.concatenate(vals)), used


def _ext_at(cells: np.ndarray, ext_c: np.ndarray, ext_v: np.ndarray) -> np.ndarray:
    if not len(ext_c):
        return np.zeros(len(cells))
    j = np.clip(np.searchsorted(ext_c, cells), 0, len(ext_c) - 1)
    return np.where(ext_c[j] == cells, ext_v[j], 0.0)


def build_reaches(d, cnt, ext_c, ext_v, tf, core, min_area_km2: float = MIN_AREA_KM2, seg_len_m: float = SEG_LEN_M):
    """Channel links (in-tile area plus inflows >= ``min_area_km2``) cut into reaches of about ``seg_len_m`` of
    20 m-smoothed length (the last reach of a link ends on the confluence cell below). Returns (links, reaches):
    ``links`` a list of dicts (``path`` cell indices, ``xy`` cell centres, ``sm`` smoothed distance, ``reaches``
    seg ids), ``reaches`` a DataFrame (seg_id, link, a, b: indices into the link path, down_seg, length_m,
    area_tile_km2, area_tile_mid_km2, inflow, core, mid_x, mid_y)."""
    ca = CELL * CELL
    chan = _chan_mask(d, cnt, min_area_km2 * 1e6 / ca, ext_c, ext_v)
    cells, ptr, nxt = _links_mask(d, chan)
    del chan
    nc = d.shape[1]
    cf = cnt.ravel()
    links, rows, head = [], [], {}
    first, last = {}, {}
    for li in range(len(ptr) - 1):
        lc = cells[ptr[li]:ptr[li + 1]]
        head[int(lc[0])] = li
        path = np.concatenate([lc, [nxt[li]]]) if nxt[li] >= 0 else lc
        if len(path) < 2:
            links.append(None)
            continue
        r, c = np.divmod(path, nc)
        xy = np.column_stack([tf.c + (c + 0.5) * CELL, tf.f - (r + 0.5) * CELL])
        sm = P.smooth_distance(xy, SMOOTH_M, CELL)
        total = float(sm[-1])
        nseg = max(1, int(round(total / seg_len_m)))
        bnd = np.searchsorted(sm, np.linspace(0, total, nseg + 1))
        bnd[0], bnd[-1] = 0, len(path) - 1
        bnd = np.unique(bnd)
        area = (cf[path].astype(np.float64) + _ext_at(path, ext_c, ext_v)) * ca / 1e6
        ext = _ext_at(path, ext_c, ext_v) > 0
        conf = nxt[li] >= 0
        dd = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
        sids = []
        for a, b in zip(bnd[:-1], bnd[1:], strict=True):
            a, b = int(a), int(b)
            sid = len(rows)
            ends_conf = b == len(path) - 1 and conf
            half = dd[a] + 0.5 * (dd[b] - dd[a])
            mi = min(b, a + int(np.searchsorted(dd[a:b + 1], half)))
            mx, my = float(np.interp(half, dd, xy[:, 0])), float(np.interp(half, dd, xy[:, 1]))
            rows.append({"seg_id": sid, "link": li, "a": a, "b": b, "down_seg": sid + 1,
                         "length_m": float(sm[b] - sm[a]),
                         "area_tile_km2": float(area[b - 1] if ends_conf else area[b]),
                         "area_tile_mid_km2": float(area[a:b].mean()),
                         "inflow": bool(ext[mi]),
                         "core": bool(core[0] <= mx < core[2] and core[1] <= my < core[3]),
                         "mid_x": mx, "mid_y": my})
            sids.append(sid)
        rows[sids[-1]]["down_seg"] = -1
        first[li], last[li] = sids[0], sids[-1]
        links.append({"li": li, "path": path, "xy": xy, "sm": sm, "reaches": sids, "nxt": int(nxt[li])})
    for li, sid in last.items():
        h = int(nxt[li])
        if h >= 0 and h in head and head[h] in first:
            rows[sid]["down_seg"] = first[head[h]]
    return links, pd.DataFrame(rows)


def seg_of_cell(shape, links, R: pd.DataFrame) -> np.ndarray:
    """int32 raster of the reach row whose body holds each channel cell (all its cells but the bottom one; -1 off
    the network), as ``lidarpipe._seg_of_cell``."""
    soc = np.full(shape, -1, np.int32)
    flat = soc.ravel()
    a, b, li = R["a"].to_numpy(), R["b"].to_numpy(), R["link"].to_numpy()
    for k in range(len(R)):
        p = links[li[k]]["path"]
        flat[p[a[k]:b[k]] if b[k] > a[k] else p[a[k]:a[k] + 1]] = k
    return soc


def body_cells(links, R: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    a, b, li = R["a"].to_numpy(), R["b"].to_numpy(), R["link"].to_numpy()
    cells = [links[li[k]]["path"][a[k]:max(b[k], a[k] + 1)] for k in range(len(R))]
    return np.concatenate(cells), np.repeat(np.arange(len(R)), [len(c) for c in cells])


def link_stations(L: dict, R: pd.DataFrame, step: float = 1.0):
    """Stations every ``step`` m of smoothed distance along a link, from its top: x, y, reach row, station number
    within the link."""
    sm, xy = L["sm"], L["xy"]
    total = float(sm[-1])
    sk = np.arange(0.0, max(total, step), step)
    x = np.interp(sk, sm, xy[:, 0])
    y = np.interp(sk, sm, xy[:, 1])
    sids = np.asarray(L["reaches"])
    starts = sm[R["a"].to_numpy()[sids]]
    own = sids[np.clip(np.searchsorted(starts, sk, side="right") - 1, 0, len(sids) - 1)]
    return x, y, own


# ---------------------------------------------------------------------------------------------- writing
def _profile(bounds, cell: float, dtype: str, nodata, **kw) -> dict:
    from rasterio.transform import from_origin

    x0, y0, x1, y1 = bounds
    p = dict(driver="GTiff", width=int(round((x1 - x0) / cell)), height=int(round((y1 - y0) / cell)), count=1,
             dtype=dtype, crs="EPSG:2193", transform=from_origin(x0, y1, cell, cell), nodata=nodata, tiled=True,
             blockxsize=512, blockysize=512, compress="deflate")
    if np.dtype(dtype).itemsize > 1:
        p["predictor"] = 2
    p.update(kw)
    return p


def write_raster(path: Path, a: np.ndarray, bounds, cell: float, nodata, **tags) -> None:
    import rasterio

    p = _profile(bounds, cell, str(a.dtype), nodata)
    if (p["height"], p["width"]) != a.shape:
        raise ValueError(f"{path.name}: shape {a.shape} does not match {bounds} at {cell} m")
    with rasterio.open(path, "w", **p) as dst:
        dst.write(a, 1)
        if tags:
            dst.update_tags(**{k: str(v) for k, v in tags.items()})


def write_arrow(path: Path, cols: dict) -> int:
    import pyarrow as pa
    import pyarrow.feather as feather

    feather.write_feather(pa.table(cols), path, compression="zstd", compression_level=9)
    return path.stat().st_size


def _fsl(a: np.ndarray):
    import pyarrow as pa

    return pa.FixedSizeListArray.from_arrays(pa.array(np.ascontiguousarray(a).ravel()), a.shape[1])


def _into1(z1):
    def put(w, br, bc):
        z1[br:br + w.shape[0], bc:bc + w.shape[1]] = w

    return put


def _into2(z, wc):
    def put(w, br, bc):
        P.agg2_into(w, br, bc, z, wc)

    return put


def _jsonable(o):
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    return str(o)


# ---------------------------------------------------------------------------------------------- the job
def process(tile: Tile32, src, dem8, rec2, geology, od: Path, threads: int = 32, log=print, lake_table=None,
            core=None, box=None) -> dict:
    """The tile job on a 1 m source (``MosaicSource`` / ``ArraySource``), ``dem8`` (a callable returning the 8 m DEM
    of the box, run in a thread while the 1 m data streams), the REC2 reaches near the box (or None: no border
    outlets, no inflows) and the geology (or None). ``core`` / ``box`` override the tile's (tests). Writes the tile
    files into ``od``; returns the ``tile.json`` record."""
    from rasterio.transform import from_origin
    from scipy import ndimage

    from .areacode import CODE_TAG, encode_area
    from .products import hillshade

    core = tuple(core or tile.core)
    box = tuple(box or tile.box)
    H, W = int(round((box[3] - box[1]) / CELL)), int(round((box[2] - box[0]) / CELL))
    if H % (F8 * F10) or W % (F8 * F10) or any(round(v) % 40 for v in box) or any(round(v) % 40 for v in core):
        raise ValueError("box and core must lie on the 40 m grid (2, 8 and 10 m cells aligned)")
    tf = from_origin(box[0], box[3], CELL, CELL)
    cr0, cc0 = int(round((box[3] - core[3]) / CELL)), int(round((core[0] - box[0]) / CELL))
    ch, cw = int(round((core[3] - core[1]) / CELL)), int(round((core[2] - core[0]) / CELL))
    od.mkdir(parents=True, exist_ok=True)
    meter = Meter(log)
    t0 = time.time()
    info: dict = {"tile": tile.id, "build": BUILD, "box": list(box), "core": list(core), "cell_m": CELL,
                  "shape": [H, W], "cells": H * W}
    ex = ThreadPoolExecutor(1)
    pool = ThreadPoolExecutor(threads)          # one reader pool for the tile: its threads keep their open sheets
    f8 = ex.submit(dem8)

    # 1. stream the 1 m mosaic into the 2 m block mean and the water code (in the reader threads)
    with meter.step("stream"):
        z = np.full((H, W), np.nan, np.float32)
        wc = np.zeros((H, W), np.uint8)
        jobs = src.box_jobs(box)
        if any((br % 2) or (bc % 2) or (w % 2) or (h % 2) for _, _, _, w, h, br, bc in jobs):
            raise RuntimeError("1 m windows are not aligned to the 2 m grid")
        st = P.stream(jobs, _into2(z, wc), threads=threads, in_reader=True, read=src.read, log=log, every=100,
                      executor=pool)
        info["stream"] = st | {"sheets": [s.get("id", s["href"]) for s in src.sheets]}
        info["lidar_cells_2m"] = int(np.isfinite(z).sum())
        trim()
    log(f"[{tile.id}] streamed {st['cells'] / 1e9:.2f} G 1 m cells in {st['seconds']} s ({st['mcells_s']} M/s)")
    with meter.step("dem8_wait"):
        z8 = f8.result()
        ex.shutdown()
    # 2. surveyed sea to no data, lakes, 3. fallback
    with meter.step("lakes"):
        info["sea_masked_km2"] = round(_mask_sea(z, wc, z8, F8, SEA_MAX_Z) * CELL * CELL / 1e6, 3)
        holes = fill_holes(z, wc, z8, tf, lake_table=lake_table)
        info["holes"] = {"examined": len(holes), "filled": int(sum(h["filled"] for h in holes)),
                         "filled_km2": round(sum(h["km2"] for h in holes if h["filled"]), 3)}
        trim()
    with meter.step("fallback"):
        info["fallback"] = seam_fill(z, wc, z8)
        info["nodata_cells"] = int((~np.isfinite(z)).sum())
    del z8
    trim()
    # 4. the raw (unfilled) DEM and the 10 m grids
    with meter.step("grids10"):
        zraw = z.copy()
        z10, zx10, cnt1, flat1, nlid10, nlake10, nfb10 = _blocks(zraw, wc, F10)
        with np.errstate(invalid="ignore"):
            fill = np.nanmean(z10) if np.isfinite(z10).any() else 0.0
            gy, gx = np.gradient(np.where(np.isfinite(z10), z10, fill), F10 * CELL)
        flat10 = (np.hypot(gx, gy) < math.tan(math.radians(4.0))) & np.isfinite(z10)
        del gx, gy
        zmax150 = ndimage.maximum_filter(np.where(np.isfinite(zx10), zx10, -1e9).astype(np.float32),
                                         size=2 * int(round(GORGE_M / (F10 * CELL))) + 1, mode="nearest")
        del zx10
        hard10 = np.zeros(z10.shape, np.uint8)
        if geology is not None and len(geology):
            from rasterio.features import rasterize

            shp = [(g, 1) for g, h in zip(geology.geometry, geology["hard_rock"], strict=True) if h and g is not None]
            if shp:
                hard10 = rasterize(shp, out_shape=z10.shape, transform=from_origin(box[0], box[3], 10, 10), fill=0,
                                   dtype="uint8")
        # lakes: filled holes and hydro-flattened water bodies (>= half of the 1 m cells flattened, >= 0.1 km2)
        flatw = (cnt1 > 0) & (2 * flat1 >= cnt1)
        lab, nl = ndimage.label(flatw | (nlake10 > 0), structure=np.ones((3, 3), bool))
        has_hole = np.bincount(lab.ravel(), weights=(nlake10 > 0).ravel(), minlength=nl + 1) > 0
        size = np.bincount(lab.ravel(), minlength=nl + 1)
        keep = has_hole | (size * 100e-6 >= LAKE_MIN_KM2)
        keep[0] = False
        ids = np.full(nl + 1, -1, np.int32)
        ids[keep] = np.arange(int(keep.sum()), dtype=np.int32)
        lake10 = ids[lab]
        lakew10 = ((lake10 >= 0) & flatw).astype(np.uint8)
        lakes = []
        objs = ndimage.find_objects(lab)
        hole_of = {}
        for h in holes:
            if h["filled"]:
                hole_of.setdefault(int(lake10[h["row"] // F10, h["col"] // F10]), []).append(h)
        for k in np.flatnonzero(keep):
            sl = objs[k - 1]
            m = lab[sl] == k
            lid = int(ids[k])
            hs = hole_of.get(lid, [])
            lev = max(hs, key=lambda q: q["cells"])["level"] if hs else float(np.nanmedian(z10[sl][m & flatw[sl]]))
            lakes.append({"lake": lid, "kind": "hole" if hs else "flat", "level": round(lev, 3),
                          "km2": round(float((m & flatw[sl]).sum() * 100e-6 + sum(q["km2"] for q in hs)), 3),
                          "holes": [q["hole"] for q in hs],
                          "bbox": [box[0] + sl[1].start * 10.0, box[3] - sl[0].stop * 10.0,
                                   box[0] + sl[1].stop * 10.0, box[3] - sl[0].start * 10.0]})
        del lab, flatw, cnt1, flat1
        # seam distance (10 m grid): from the 10 m cells where fallback meets LiDAR
        fb10 = nfb10 > 0
        if fb10.any():
            seam = fb10 & ndimage.binary_dilation(nlid10 > 0, structure=np.ones((3, 3), bool))
            seamd = np.minimum(ndimage.distance_transform_edt(~seam) * F10 * CELL, SEAM_CAP_M).astype(np.float32)
        else:
            seamd = None
        del nlid10, nfb10, nlake10
        # hillshade of the 8 m block mean of the core, averaged to 24 m
        z8m = _block_mean(np.ascontiguousarray(zraw[cr0:cr0 + ch, cc0:cc0 + cw]), F8, F8 * F8 // 2)
        hs8 = hillshade(z8m, np.isfinite(z8m), F8 * CELL)
        m3 = (hs8.shape[0] // 3) * 3
        hs24 = hs8[:m3, :m3].reshape(m3 // 3, 3, m3 // 3, 3).astype(np.float32).mean(axis=(1, 3))
        hs24 = np.where(hs8[:m3, :m3].reshape(m3 // 3, 3, m3 // 3, 3).max(axis=(1, 3)) > 0, np.clip(hs24, 1, 255), 0)
        hs24 = hs24.astype(np.uint8)
        del z8m, hs8

    # 5. route
    with meter.step("route"):
        from .nz import border_outlets, inflow_points

        seed = border_outlets(rec2, box, tf, z.shape, half_cells=max(1, round(96 / CELL))) if rec2 is not None \
            else None
        if seed is not None and not seed.any() and np.isfinite(z).all():
            seed = None
        info["outlet_border_cells"] = int(seed.sum()) if seed is not None else -1
        tim: dict = {}
        d = P.route_lean(z, CELL, border_seed=seed, timings=tim)
        del seed
        info["routing"] = tim
    # 6. accumulate with REC2 inflows; channels and reaches
    with meter.step("accumulate"):
        cnt, done = _accumulate_u32(d)
        if done != d.size:
            raise RuntimeError(f"flow graph has a cycle: {done} of {d.size} cells")
        infl = inflow_points(rec2, box) if rec2 is not None else []
        ext_c, ext_v, used = inflows_sparse(d, cnt, tf, infl, radius_cells=max(1, round(48 / CELL)))
        info["inflows"] = used
        # area code (10 m maximum of the in-tile area with inflows), core only
        a10 = _area10(cnt, F10).astype(np.float64)
        if len(ext_c):
            er, ec = np.divmod(ext_c, W)
            np.maximum.at(a10, (er // F10, ec // F10), cnt.ravel()[ext_c].astype(np.float64) + ext_v)
        a10 *= CELL * CELL / 1e6
        dv = (d != P.NODATA)
        any10 = dv.reshape(H // F10, F10, W // F10, F10).any(axis=(1, 3))
        del dv
        a10 = np.where(any10, a10, np.nan)
        c10r, c10c = cr0 // F10, cc0 // F10
        area_code = encode_area(a10[c10r:c10r + ch // F10, c10c:c10c + cw // F10])
        del a10, any10
        # lake outlets: from the lake cell of largest area, downstream until the flow leaves the lake
        for lk in lakes:
            x0, y0, x1, y1 = lk["bbox"]
            r0, r1 = int(round((box[3] - y1) / CELL)), int(round((box[3] - y0) / CELL))
            q0, q1 = int(round((x0 - box[0]) / CELL)), int(round((x1 - box[0]) / CELL))
            l10 = lake10[r0 // F10:r1 // F10, q0 // F10:q1 // F10] == lk["lake"]
            w10 = lakew10[r0 // F10:r1 // F10, q0 // F10:q1 // F10].astype(bool)
            mem = np.repeat(np.repeat(l10, F10, 0), F10, 1) & (
                (wc[r0:r1, q0:q1] == W_LAKE) | np.repeat(np.repeat(w10 & l10, F10, 0), F10, 1))
            if not mem.any():
                continue
            cw_ = np.where(mem, cnt[r0:r1, q0:q1], 0)
            j = int(np.argmax(cw_))
            v = (r0 + j // mem.shape[1]) * W + (q0 + j % mem.shape[1])
            path = _walk(d, v, d.size)
            pr, pc = np.divmod(path, W)
            inside = (pr >= r0) & (pr < r1) & (pc >= q0) & (pc < q1)
            inside[inside] = mem[pr[inside] - r0, pc[inside] - q0]
            out_i = int(np.argmax(~inside)) if (~inside).any() else -1
            ocell = int(path[out_i]) if out_i >= 0 else int(path[-1])
            orr, occ = divmod(ocell, W)
            lk.update(outlet_cell=ocell, outlet_x=round(float(tf.c + (occ + 0.5) * CELL), 1),
                      outlet_y=round(float(tf.f - (orr + 0.5) * CELL), 1),
                      outlet_area_tile_km2=round(float(cnt.ravel()[ocell] * CELL * CELL / 1e6
                                                       + _ext_at(np.array([ocell]), ext_c, ext_v)[0] * CELL * CELL
                                                       / 1e6), 3),
                      drains="out" if out_i >= 0 else ("sink" if d.ravel()[path[-1]] >= 8 else "edge"))
            del mem, cw_
    with meter.step("reaches"):
        links, R = build_reaches(d, cnt, ext_c, ext_v, tf, core)
        del cnt
        trim()
        nseg = len(R)
        R["gid"] = [S.gid(tile.ix, tile.iy, int(s)) for s in R["seg_id"]]
        inr = R["core"].to_numpy()
        ds = R["down_seg"].to_numpy()
        R["exits_core"] = inr & (ds >= 0) & ~inr[np.maximum(ds, 0)]
        info["reaches_box"], info["reaches_core"], info["links"] = nseg, int(inr.sum()), len(links)
    log(f"[{tile.id}] {nseg} reaches ({int(inr.sum())} in the core), {len(used)} inflows")

    # 7. the 2 m pass: local catchments, local sums, HAND; 10 m rasters
    with meter.step("pass2m"):
        soc = seg_of_cell(d.shape, links, R)
        lc10 = np.full((H // F10, W // F10), -1, np.int32)
        hand10 = np.full((H // F10, W // F10), 255, np.uint8)
        n_, zsum, zmax, nlid, nlake, nflat, nhard, hc, lost, other = _pass2m(
            d, z, soc, inr, wc, flat10, hard10, lakew10, F10, np.asarray(HAND_TH, np.float64), nseg, lc10, hand10)
        ca = CELL * CELL / 1e6
        R["local_km2"] = n_ * ca
        with np.errstate(invalid="ignore", divide="ignore"):
            R["local_z_mean_m"] = np.where(n_ > 0, zsum / np.maximum(n_, 1), np.nan)
        R["local_z_max_m"] = np.where(n_ > 0, zmax, np.nan)
        R["local_flat_km2"], R["local_hard_km2"] = nflat * ca, nhard * ca
        R["local_lidar_km2"], R["local_lake_km2"] = nlid * ca, nlake * ca
        Lm = np.maximum(R["length_m"].to_numpy(), CELL)
        for q, th in enumerate(HAND_TH):
            wq = np.maximum(hc[:, q] * CELL * CELL / Lm, CELL)
            name = "valley_width_m" if th == 5.0 else f"hand_w{th:g}_m"
            R[name] = wq
        info["lost_km2"], info["halo_owned_km2"] = round(lost * ca, 3), round(other * ca, 3)
        # lake outlet reaches
        socf = soc.ravel()
        for lk in lakes:
            if "outlet_cell" not in lk:
                continue
            path = _walk(d, lk.pop("outlet_cell"), d.size)
            hit = socf[path]
            k = int(np.argmax(hit >= 0)) if (hit >= 0).any() else -1
            sid = int(hit[k]) if k >= 0 else -1
            lk.update(outlet_seg=sid, outlet_gid=int(R.at[sid, "gid"]) if sid >= 0 and inr[sid] else -1,
                      outlet_in_core=bool(sid >= 0 and inr[sid]))
        del soc, socf
        # gorge depth: mean over the reach body of the highest ground within +-150 m minus the channel cell
        bc_, br_ = body_cells(links, R)
        rr, cc = np.divmod(bc_, W)
        gd = zmax150[rr // F10, cc // F10] - zraw.ravel()[bc_]
        ok = np.isfinite(gd) & (gd > -1e8)
        R["gorge_depth_m"] = np.bincount(br_[ok], weights=gd[ok], minlength=nseg) / np.maximum(
            np.bincount(br_[ok], minlength=nseg), 1)
        R["height_to_width"] = R["gorge_depth_m"] / R["valley_width_m"]
        del bc_, br_, rr, cc, gd, zmax150
        lc_gid = np.where(lc10 >= 0, R["gid"].to_numpy()[np.maximum(lc10, 0)], -1).astype(np.int64)
        del lc10
        write_raster(od / "local_catchment_core.tif", lc_gid, box, 10.0, -1,
                     description="gid of the reach each 10 m cell (its centre 2 m cell) first drains into, over the "
                                 "cells this tile owns; -1 elsewhere")
        del lc_gid
        write_raster(od / "hand_10m.tif", np.ascontiguousarray(hand10[c10r:c10r + ch // F10, c10c:c10c + cw // F10]),
                     core, 10.0, None, description="HAND of the filled 2 m DEM at the 10 m cell centre, 0.25 m steps "
                                                   "to 63.5 m, 255 = higher or no channel")
        del hand10
        write_raster(od / "area_code.tif", area_code, core, 10.0, 0, code=CODE_TAG,
                     description="in-tile 2 m accumulation with REC2 inflows, maximum per 10 m cell")
        write_raster(od / "hillshade_24m.tif", hs24, (core[0], core[3] - hs24.shape[0] * 24.0,
                                                      core[0] + hs24.shape[1] * 24.0, core[3]), 24.0, 0)
        del d, z
        trim()
    # 8. the 1 m pass
    with meter.step("extract1m"):
        res1 = extract_1m(tile.id, src, links, R, zraw, wc, lake10, lakew10, seamd, box, tf, threads, log, pool)
    pool.shutdown()
    del zraw
    trim()
    with meter.step("profiles"):
        prof = reach_profiles(R, links, res1)
    with meter.step("write"):
        files = write_outputs(od, tile.id, R, links, prof, res1)
    info.update(res1["stats"])
    info["lakes"] = lakes
    for lk in lakes:
        lk["reaches"] = prof["lake_reaches"].get(lk["lake"], [])
    info["holes_list"] = [{k: v for k, v in h.items() if k not in ("mask", "slice")} for h in holes
                          if h["filled"] or h["km2"] >= 0.01]
    info["edges"] = {"exits_core": int(R.loc[R["core"], "exits_core"].sum()),
                     "inflow_reaches": int(R.loc[R["core"], "inflow"].sum())}
    info["counts"] = prof["counts"]
    info["files"] = files
    m = meter.close()
    info["timings"] = meter.steps
    info["peak_gb"] = m["peak_gb"]
    info["seconds"] = round(time.time() - t0, 1)
    info["disk_bytes"] = int(sum(files.values()))
    info["done"] = True
    (od / "tile.json").write_text(json.dumps(info, indent=1, default=_jsonable))
    log(f"[{tile.id}] done in {info['seconds']} s, peak {info['peak_gb']} GB, {info['disk_bytes'] / 1e6:.1f} MB")
    return info


def extract_1m(tid, src, links, R, zraw, wc, lake10, lakew10, seamd, box, tf, threads, log, pool=None) -> dict:
    """The 1 m pass: stations of every link with a core reach and sections of the core reaches of 1 km2 or more,
    sampled from the 1 m blocks along them, re-read in ``CHUNK_M`` chunks (by reach midpoint; the next chunk is read
    while the current one is measured)."""
    import shapely
    from rasterio.transform import from_origin

    from . import xsec as XS

    W = zraw.shape[1]
    inr = R["core"].to_numpy()
    plinks = sorted({int(li) for li in R.loc[inr, "link"]})
    sx, sy, sown = [], [], []
    for li in plinks:
        x, y, own = link_stations(links[li], R)
        sx.append(x)
        sy.append(y)
        sown.append(own)
    sx, sy, sown = np.concatenate(sx), np.concatenate(sy), np.concatenate(sown)
    sec_rows = np.flatnonzero(inr & (R["area_tile_km2"].to_numpy() >= SEC_MIN_KM2))
    geoms = []
    for k in sec_rows:
        L = links[R.at[k, "link"]]
        geoms.append(shapely.LineString(L["xy"][R.at[k, "a"]:R.at[k, "b"] + 1]))
    ri, ss, xx, yy, nx, ny = XS.stations(np.array(geoms, dtype=object), X1.SPACING_M, X1.TANGENT_M) if geoms else (
        np.zeros(0, int), *(np.zeros(0) for _ in range(5)))
    srow = sec_rows[ri] if len(ri) else np.zeros(0, int)
    nchx = max(1, int(math.ceil((box[2] - box[0]) / CHUNK_M)))
    nchy = max(1, int(math.ceil((box[3] - box[1]) / CHUNK_M)))
    kx = np.clip(((R["mid_x"].to_numpy() - box[0]) // CHUNK_M).astype(int), 0, nchx - 1)
    ky = np.clip(((box[3] - R["mid_y"].to_numpy()) // CHUNK_M).astype(int), 0, nchy - 1)
    chunk_of = ky * nchx + kx
    pch, sch = chunk_of[sown], chunk_of[srow]
    chunks = sorted(set(pch.tolist()) | set(sch.tolist()))

    def chunk_box(k):
        mp, ms = pch == k, sch == k
        ex = np.concatenate([sx[mp] - PROF_READ_M, sx[mp] + PROF_READ_M, xx[ms] - SEC_READ_M, xx[ms] + SEC_READ_M])
        ey = np.concatenate([sy[mp] - PROF_READ_M, sy[mp] + PROF_READ_M, yy[ms] - SEC_READ_M, yy[ms] + SEC_READ_M])
        b = (max(box[0], math.floor(ex.min()) - 2), max(box[1], math.floor(ey.min()) - 2),
             min(box[2], math.ceil(ex.max()) + 2), min(box[3], math.ceil(ey.max()) + 2))
        return b, mp, ms

    def load(k):
        b, mp, ms = chunk_box(k)
        need = X1.corridor_mask(b, [(sx[mp], sy[mp], PROF_READ_M), (xx[ms], yy[ms], SEC_READ_M)])
        jobs = src.mask_jobs(b, need)
        z1 = np.full((int(b[3] - b[1]), int(b[2] - b[0])), np.nan, np.float32)

        st = P.stream(jobs, _into1(z1), threads=threads, in_reader=True, read=src.read, executor=pool) if jobs else {
            "cells": 0, "seconds": 0.0}
        return k, b, mp, ms, z1, st

    zst = np.full(len(sx), np.nan, np.float32)
    code = np.full(len(sx), P.W_COARSE, np.uint8)
    secs, encs, longs = [], [], []
    t_read = t_meas = 0.0
    cells = 0
    with ThreadPoolExecutor(1) as ex:
        nxt = ex.submit(load, chunks[0]) if chunks else None
        for q in range(len(chunks)):
            t = time.time()
            k, b, mp, ms, z1, st = nxt.result()
            t_read += time.time() - t
            cells += st["cells"]
            nxt = ex.submit(load, chunks[q + 1]) if q + 1 < len(chunks) else None
            t = time.time()
            tf1 = from_origin(b[0], b[3], 1, 1)
            ip = np.flatnonzero(mp)
            if len(ip):
                zz, ff = _station_heights(z1, float(b[0]), float(b[3]), sx[ip], sy[ip], PROF_RAD)
                zst[ip], code[ip] = zz, ff
            isel = np.flatnonzero(ms)
            if len(isel):
                sec = X1.extract(z1, tf1, zraw, tf, ri[isel], ss[isel], xx[isel], yy[isel], nx[isel], ny[isel],
                                 search=SEC_SEARCH_M)
                m = X1.measure(sec["prof"])
                tab = X1.section_table(sec, m)
                tab["sec"] = isel
                secs.append(tab)
                dq, msk = X1.encode(X1.quantise(sec["prof"]))
                encs.append((isel, dq, msk))
                ok = (sec["flags"] & X1.DROP) == 0
                longs.append(X1.long_profiles(z1, tf1, sec["reach"], sec["s"], sec["cx"], sec["cy"], ok))
                del sec, m
            del z1
            t_meas += time.time() - t
            log(f"[{tid}] 1 m chunk {q + 1}/{len(chunks)}: {int(mp.sum())} stations, {int(ms.sum())} sections, read "
                f"{st['cells'] / 1e6:.0f} M cells in {st['seconds']:.0f} s")
    # stations with no 1 m cell near: the 2 m grid (fallback or lake fill), code 3
    rr = np.clip(((box[3] - sy) // CELL).astype(np.int64), 0, zraw.shape[0] - 1)
    cc = np.clip(((sx - box[0]) // CELL).astype(np.int64), 0, W - 1)
    miss = ~np.isfinite(zst)
    zst[miss] = zraw[rr[miss], cc[miss]]
    code[miss] = P.W_COARSE
    in_lake = (wc[rr, cc] == W_LAKE) | (lakew10[rr // F10, cc // F10] > 0)
    lake_id = np.where(in_lake, lake10[rr // F10, cc // F10], -1)
    sdist = seamd[rr // F10, cc // F10] if seamd is not None else np.full(len(sx), SEAM_CAP_M, np.float32)
    sec = pd.concat(secs, ignore_index=True).sort_values("sec").reset_index(drop=True) if secs else None
    if encs:
        idx = np.concatenate([e[0] for e in encs])
        o = np.argsort(idx)
        dprof = np.concatenate([e[1] for e in encs])[o]
        nodata = np.concatenate([e[2] for e in encs])[o]
    else:
        dprof = np.zeros((0, len(X1.U) - 1), np.int16)
        nodata = np.zeros((0, (len(X1.U) + 7) // 8), np.uint8)
    lp = pd.concat(longs, ignore_index=True) if longs else None
    stats = {"stations": int(len(sx)), "stations_no_1m": int(miss.sum()), "sections": int(len(ri)),
             "sections_kept": int(((sec["flags"].to_numpy() & X1.DROP) == 0).sum()) if sec is not None else 0,
             "chunks": len(chunks), "read_1m_cells": int(cells), "read_1m_wait_s": round(t_read, 1),
             "measure_1m_s": round(t_meas, 1), "box_1m_cells": int((box[2] - box[0]) * (box[3] - box[1]))}
    return {"sx": sx, "sy": sy, "own": sown, "z": zst, "code": code, "in_lake": in_lake, "lake_id": lake_id,
            "seam": sdist, "sec": sec, "sec_rows": sec_rows, "dprof": dprof, "nodata": nodata, "long": lp,
            "sec_station": (ri, ss, xx, yy), "stats": stats}


def reach_profiles(R: pd.DataFrame, links, r1: dict) -> dict:
    """Per core reach the 1 m profile fields (``lidarpipe.reach_profile``, running minimum along the link), the steps
    (``profile_steps`` per link, given to the reach holding their middle), lake and seam fields, and the x1 section
    summaries (``xsec1m.reach_table``)."""
    inr = R["core"].to_numpy()
    own, z, w = r1["own"], r1["z"].astype(np.float64), r1["code"]
    order = np.arange(len(own))
    rows, steps = {}, []
    # stations are grouped by link (link_stations order) and in station order within a link
    link_of = R["link"].to_numpy()[own]
    cut = np.flatnonzero(np.r_[True, link_of[1:] != link_of[:-1], True])
    for a0, a1 in zip(cut[:-1], cut[1:], strict=True):
        zl, wl, ol = z[a0:a1], w[a0:a1], own[a0:a1]
        zm = P.running_min(zl)
        starts = {}
        for i, s in enumerate(ol):
            starts.setdefault(int(s), i)
        mine = {s: [] for s in starts}
        for stp in P.profile_steps(zl, wl):
            mid = (stp["i0"] + stp["i1"]) // 2
            s = int(ol[mid])
            if not inr[s]:
                continue
            g = a0 + mid
            stp.update(seg=s, s0r=stp["s0"] - starts[s], x=float(r1["sx"][g]), y=float(r1["sy"][g]))
            steps.append(stp)
            mine[s].append(dict(stp, s0=stp["s0r"]))
        for s, i0 in starts.items():
            if not inr[s]:
                continue
            m = np.flatnonzero(ol == s)
            i1 = m[-1] + 1
            row = P.reach_profile(zl[m], zm[i0:min(i1 + 1, len(zm))], wl[m])
            row.update(P.reach_steps(mine[s], row.get("z_top", np.nan) - row.get("z_bot", np.nan)))
            g = a0 + m
            ls = float(np.mean(r1["in_lake"][g]))
            ids = r1["lake_id"][g]
            row.update(lake_share=ls, lake=bool(ls >= 0.5), seam_dist_m=float(np.min(r1["seam"][g])),
                       lake_mode=int(np.bincount(ids[ids >= 0]).argmax()) if (ids >= 0).any() else -1,
                       n_stations=int(len(m)), st0=int(g[0]))
            rows[s] = row
    del order
    P_ = pd.DataFrame.from_dict(rows, orient="index").sort_index()
    core_rows = np.flatnonzero(inr)
    P_ = P_.reindex(core_rows)
    # x1 section summaries of the section reaches, then every core reach
    sec_rows = r1["sec_rows"]
    x1 = None
    if r1["sec"] is not None and len(sec_rows):
        x1 = X1.reach_table(r1["sec"], len(sec_rows), r1["long"])
        x1.index = sec_rows
    lake_reaches: dict[int, list[int]] = {}
    gids = R["gid"].to_numpy()
    for s, row in P_.iterrows():
        if row.get("lake") and row.get("lake_mode", -1) >= 0:
            lake_reaches.setdefault(int(row["lake_mode"]), []).append(int(gids[s]))
    kinds = {P.STEP_BED: "bed", P.STEP_RISER: "riser", P.STEP_PLANAR: "planar", P.STEP_COARSE: "coarse"}
    counts = {"steps": len(steps), "steps_by_kind": {kinds[k]: int(sum(s["kind"] == k for s in steps))
                                                      for k in kinds}}
    return {"P": P_, "x1": x1, "steps": steps, "lake_reaches": lake_reaches, "counts": counts}


PROFILE_FIELDS = ("z_top", "z_bot", "steepest_50m_m_per_km", "steepest_100m_m_per_km", "steepest_200m_m_per_km",
                  "steep50_water", "longest_flat_m", "steps_n", "steps_drop_m", "step_max_m", "step_max_s_m",
                  "steps_share", "drops_n", "falls1m_n", "risers_n", "dem_source", "lidar_share", "dem_res_m",
                  "water_flat_share", "water_planar_share", "lake", "lake_share", "seam_dist_m")
_DT = {"float32": np.float32, "float64": np.float64, "int16": np.int16, "int32": np.int32, "int64": np.int64,
       "uint8": np.uint8, "int8": np.int8}


def reach_frame(tid: str, R: pd.DataFrame, links, prof: dict):
    """The core reaches with every ``segments_core.gpkg`` column (``lidarspec.tile_columns``), in spec order and
    dtype, and the line (2 m cell-centre path simplified at 1 m) as geometry."""
    import geopandas as gpd
    import shapely

    inr = R["core"].to_numpy()
    C = R[inr].copy()
    Pf = prof["P"]
    for k in PROFILE_FIELDS:
        C[k] = Pf[k].to_numpy()
    C["gradient_m_per_km"] = (C["z_top"] - C["z_bot"]) / np.maximum(C["length_m"], 1.0) * 1000.0
    C["tile"] = tid
    geoms, ends = [], []
    for k in C.index:
        xy = links[R.at[k, "link"]]["xy"][R.at[k, "a"]:R.at[k, "b"] + 1]
        geoms.append(shapely.simplify(shapely.LineString(xy), 1.0))
        ends.append(xy[-1])
    ends = np.asarray(ends).reshape(-1, 2)
    C["end_x"], C["end_y"] = ends[:, 0], ends[:, 1]
    cols = S.tile_columns()["segments_core.gpkg"]
    out = {}
    for name in cols:
        f = S.BY_NAME[name]
        v = C[name]
        if f.dtype.startswith("float"):
            v = v.astype(np.float64).astype(_DT[f.dtype])
        elif f.dtype in _DT:
            v = v.fillna(0).astype(_DT[f.dtype])
        elif f.dtype == "bool":
            v = v.fillna(False).astype(bool)
        else:
            v = v.fillna("").astype(str)
        out[name] = v.to_numpy()
    return gpd.GeoDataFrame(out, geometry=geoms, crs="EPSG:2193")


def x1_frame(R: pd.DataFrame, prof: dict) -> pd.DataFrame:
    """One row per core reach: ``gid`` and every ``x1_*`` field (``lidarspec.tile_columns``); the section summaries
    of ``xsec1m.reach_table`` (NaN and x1_n = 0 without sections) and the bed roughness of the reach's 1 m profile
    (``lidarpipe.reach_profile``)."""
    core_rows = np.flatnonzero(R["core"].to_numpy())
    cols = S.tile_columns()["x1_reach.arrow"]
    x1 = prof["x1"].reindex(core_rows) if prof["x1"] is not None else pd.DataFrame(index=core_rows)
    out = {"gid": R["gid"].to_numpy()[core_rows].astype(np.int64)}
    for name in cols[1:]:
        f = S.BY_NAME[name]
        if name in ("x1_rough", "x1_dropconc"):
            v = prof["P"][name].to_numpy(np.float64)
        elif name in x1:
            v = x1[name].to_numpy(np.float64)
        else:
            v = np.full(len(core_rows), np.nan)
        if f.dtype == "int16":
            v = np.nan_to_num(v, nan=0.0)
        out[name] = v.astype(_DT[f.dtype])
    return pd.DataFrame(out)


def write_outputs(od: Path, tid: str, R: pd.DataFrame, links, prof: dict, r1: dict) -> dict:
    """The per-tile files of ``lidarspec.TILE_FILES`` for the core reaches (rasters are written by ``process``)."""
    import pyogrio

    inr = R["core"].to_numpy()
    gids = R["gid"].to_numpy()
    g = reach_frame(tid, R, links, prof)
    p = od / "segments_core.gpkg"
    p.unlink(missing_ok=True)
    pyogrio.write_dataframe(g, p, layer="segments", driver="GPKG")
    x1 = x1_frame(R, prof)
    write_arrow(od / "x1_reach.arrow", {c: x1[c].to_numpy() for c in x1.columns})
    # 1 m long profiles: the stations of every core reach, in order (a reach's stations are consecutive)
    sel = inr[r1["own"]]
    enc = P.encode_profiles(gids[r1["own"][sel]], r1["z"][sel], r1["code"][sel])
    import pyarrow as pa

    write_arrow(od / "profiles.arrow", {"gid": enc["gid"], "n": enc["n"], "z0_cm": enc["z0_cm"],
                                        "dz": pa.array(enc["dz"], type=pa.list_(pa.int16())),
                                        "w4": pa.array(enc["w4"], type=pa.list_(pa.uint8()))})
    st = prof["steps"]
    write_arrow(od / "steps.arrow", {
        "gid": np.array([gids[s["seg"]] for s in st], np.int64), "s0": np.array([s["s0r"] for s in st], np.float32),
        "x_cm": np.round(np.array([s["x"] for s in st], np.float64) * 100).astype(np.int64),
        "y_cm": np.round(np.array([s["y"] for s in st], np.float64) * 100).astype(np.int64),
        **{k: np.array([s[k] for s in st], np.float32) for k in ("h", "h_excess", "len", "slope_max", "z_top")},
        "kind": np.array([s["kind"] for s in st], np.uint8)})
    sec = r1["sec"]
    if sec is not None:
        sg = gids[r1["sec_rows"][sec["reach"].to_numpy()]]
        cols = {"gid": sg.astype(np.int64), "s": sec["s"].to_numpy(np.float32),
                "x": np.round(sec["cx"].to_numpy() * 100).astype(np.int32),
                "y": np.round(sec["cy"].to_numpy() * 100).astype(np.int32),
                "nx": sec["nx"].to_numpy(np.float32), "ny": sec["ny"].to_numpy(np.float32),
                "z0": sec["z0"].to_numpy(np.float32), "flags": sec["flags"].to_numpy(np.uint8),
                "dprof": _fsl(r1["dprof"]), "nodata": _fsl(r1["nodata"])}
    else:
        cols = {"gid": np.zeros(0, np.int64)}
    write_arrow(od / "xsec.arrow", cols)
    return {f.name: f.stat().st_size for f in sorted(od.iterdir()) if f.is_file() and f.name != "tile.json"}


# ---------------------------------------------------------------------------------------------- runs
def tile_dir(tid: str, root: Path | None = None) -> Path:
    return (root or root_dir()) / "tiles" / tid


def is_done(tid: str, root: Path | None = None) -> bool:
    p = tile_dir(tid, root) / "tile.json"
    return p.exists() and bool(json.loads(p.read_text()).get("done"))


def run_tile(tid: str, root: Path | None = None, threads: int = 32, force: bool = False, log=print) -> dict | None:
    """One 32 km tile on the national mosaic (resumable: a tile whose ``tile.json`` says done is skipped). A failed
    run leaves no ``tile.json``; the next run starts the tile again."""
    t = Tile32.parse(tid)
    od = tile_dir(tid, root)
    if is_done(tid, root) and not force:
        log(f"[{tid}] done, skipped")
        return None
    (od / "tile.json").unlink(missing_ok=True)
    log(f"[{tid}] box {t.box} (quarter of {t.parent})")
    src = MosaicSource(t.box)
    rec2 = rec2_near(t.box)
    geo = geology_near(t.box)
    lake_table = None
    lp = (root or root_dir()) / "lakes.gpkg"
    if lp.exists():                         # the national lake table (rf-lxa.5): one level per lake
        import geopandas as gpd

        lk = gpd.read_file(lp, bbox=t.box)
        lake_table = list(zip(lk.geometry, lk["level_m"], strict=True)) if len(lk) else None
    return process(t, src, lambda: fetch_dem8(t.box), rec2, geo, od, threads=threads, log=log, lake_table=lake_table)


def run_tiles(tids: list[str], root: Path | None = None, threads: int = 32, force: bool = False, log=print) -> None:
    for tid in tids:
        try:
            run_tile(tid, root, threads=threads, force=force, log=log)
        except Exception as e:  # keep going; a failed tile is re-run next time
            log(f"[{tid}] FAILED: {e!r}")
            raise
