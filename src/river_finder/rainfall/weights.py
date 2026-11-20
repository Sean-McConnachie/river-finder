"""Catchment weight matrix: grid cells -> local catchments, built once per grid.

**Definition.** Rows are the national reaches' *local* catchments (the ground draining directly into
each reach, ``local_catchment_core.tif``), in ``segments_table.pkl`` row order; columns are the grid
cells in C order (``grid_mm.ravel()``). With ``n[c, g]`` the 8 m pixels of catchment ``c`` whose
centre lies in cell ``g``::

    W[c, g] = n[c, g] / sum_g n[c, g]            (share of the catchment's area in cell g)
    mm[c]   = W @ grid_mm.ravel()                (area-weighted mean depth over the catchment)
    m3[c]   = mm[c] / 1000 * area_m2[c]          (area_m2 = pixels x pixel area)

The epic's "share of each cell's area inside the catchment" read literally would give
``n[c, g] / n[g]`` (rows of each column sum to 1), which distributes a cell's *volume*; the row
normalisation above gives a *depth* directly and the same volume once multiplied by the catchment
area (``sum_g mm_g * cellshare * cell_area = mm[c] * area[c]``). Rows of W sum to 1.

Areas are NZTM map areas (8 m x 8 m = 64 m² per pixel), the same as ``area_km2`` in the segment
table. True ground area differs by the projection's scale (k² = 0.9992 on the central meridian
173°E, up to ~1.005 at the far east and west of NZ); that is far below the rain products' error.

**Pixel -> cell.** Each tile is read in strips of rows (GDAL block cache capped; a few hundred MB
in all). Pixel centres are not transformed one by one: lon/lat are computed exactly (pyproj) on a
lattice every ``lattice_step`` pixels (64 px = 512 m) and interpolated bilinearly in between. A
lattice block whose four corners fall in one cell lies wholly in it (a bilinear patch stays in
the convex hull of its corners), so its pixels take that cell directly; only blocks cut by a cell
edge (~11 %) are interpolated per pixel, in a numba kernel. The build measures the interpolation
error on random pixels of every tile against the exact transform and stores the maximum
(millimetres; a 0.1° cell is 8-11 km wide) and how many of them would change cell (expected 0).

**Gaps.** A catchment with no pixel (its local area lies in a neighbouring tile's halo, see
``nz.finish_tile``) gets weight 1 on the cell holding its midpoint (table ``lon``/``lat``) and area 0,
flagged ``fallback``: its depth is the local cell's, its volume 0. Land pixels draining into no
kept segment (coastal slivers, tile edges) are counted in the build stats.

Upstream totals (rf-t26.6, :mod:`.upstream`) use ``area_m2`` and the segment table's ``down`` (row
index of the downstream catchment, -1 at an outlet; ``down`` here is the copy taken when the weights
were built): upstream m³ = sum of local m³ upstream, upstream mm = upstream m³ / upstream area.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
import scipy.sparse as sp
from numba import njit
from pyproj import Transformer
from rasterio.windows import Window

from .grid import GridSpec, metres_per_degree
from .sources import HourlyField

FORMAT = "river-finder rain weights v1"
SEG_BASE = 1_000_000  # gid = tile_index * SEG_BASE + local seg id (as river_finder.nz.SEG_BASE)


@dataclass
class CatchmentWeights:
    grid: GridSpec
    counts: sp.csr_matrix        # int32 pixels per (catchment, cell); fallback rows hold a single 1
    gid: np.ndarray              # int64 global segment id per row
    npix: np.ndarray             # int64 pixels of each local catchment (0 = fallback)
    pixel_area_m2: float
    down: np.ndarray             # int64 row of the downstream catchment, -1 at an outlet
    region_of: np.ndarray        # int16 region index per row (-1 none); factor regions of rf-t26.5
    regions: tuple[str, ...]
    table_area_km2: np.ndarray   # float32 upstream area at the reach's bottom end (segment table)
    table_local_km2: np.ndarray  # float32 area_km2 minus the upstream reaches' area_km2
    meta: dict = field(default_factory=dict)

    @property
    def n(self) -> int:
        return self.counts.shape[0]

    @property
    def fallback(self) -> np.ndarray:
        return self.npix == 0

    @property
    def area_m2(self) -> np.ndarray:
        return self.npix.astype(np.float64) * self.pixel_area_m2

    @cached_property
    def W(self) -> sp.csr_matrix:
        """Row-normalised weights (float64 CSR, n x grid.size)."""
        c = self.counts
        rs = np.asarray(c.sum(axis=1)).ravel().astype(np.float64)
        data = c.data.astype(np.float64) / np.repeat(np.where(rs > 0, rs, 1.0), np.diff(c.indptr))
        return sp.csr_matrix((data, c.indices, c.indptr), shape=c.shape)

    @cached_property
    def rows_digest(self) -> str:
        return hashlib.sha1(np.ascontiguousarray(self.gid, dtype=np.int64).tobytes()).hexdigest()[:16]

    def mm(self, values: HourlyField | np.ndarray) -> np.ndarray:
        """Area-weighted mean depth (mm) per catchment of one grid field; NaN if it touches a NaN cell."""
        if isinstance(values, HourlyField):
            if not values.grid.same_cells(self.grid):
                raise ValueError(f"field is on grid {values.grid.name}, weights are for {self.grid.name}")
            values = values.mm
        v = np.asarray(values, dtype=np.float64).ravel()
        if v.size != self.grid.size:
            raise ValueError(f"grid has {v.size} cells, weights expect {self.grid.size}")
        return (self.W @ v).astype(np.float32)

    def m3(self, mm: np.ndarray) -> np.ndarray:
        """Volume (m³) per catchment from depth (mm)."""
        return np.asarray(mm, dtype=np.float64) / 1000.0 * self.area_m2

    # --- files ----------------------------------------------------------------------------------
    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = dict(self.meta, format=FORMAT, grid=self.grid.to_dict(), pixel_area_m2=self.pixel_area_m2,
                    regions=list(self.regions), rows_digest=self.rows_digest)
        c = self.counts
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "wb") as fh:
            np.savez_compressed(fh, meta=np.array(json.dumps(meta)), counts=c.data.astype(np.int32),
                                indices=c.indices.astype(np.int32), indptr=c.indptr.astype(np.int64),
                                shape=np.array(c.shape, dtype=np.int64), gid=self.gid, npix=self.npix,
                                down=self.down, region_of=self.region_of, table_area_km2=self.table_area_km2,
                                table_local_km2=self.table_local_km2)
        tmp.replace(path)
        return path

    @classmethod
    def load(cls, path: Path) -> CatchmentWeights:
        with np.load(path, allow_pickle=False) as z:
            meta = json.loads(str(z["meta"]))
            if meta.get("format") != FORMAT:
                raise ValueError(f"{path}: not a {FORMAT} file")
            counts = sp.csr_matrix((z["counts"], z["indices"], z["indptr"]), shape=tuple(z["shape"]))
            return cls(GridSpec.from_dict(meta["grid"]), counts, z["gid"], z["npix"], float(meta["pixel_area_m2"]),
                       z["down"], z["region_of"], tuple(meta["regions"]), z["table_area_km2"],
                       z["table_local_km2"], meta)


# --- building -----------------------------------------------------------------------------------
def _lattice(transform, h: int, w: int, step: int, to_ll: Transformer) -> tuple[np.ndarray, np.ndarray]:
    """Exact lon/lat at the centres of pixels (i * step, j * step), with the last node at or beyond the edge."""
    pr = np.arange((h - 1) // step + 2) * step + 0.5
    pc = np.arange((w - 1) // step + 2) * step + 0.5
    cc, rr = np.meshgrid(pc, pr)
    x = transform.c + cc * transform.a + rr * transform.b
    y = transform.f + cc * transform.d + rr * transform.e
    lon, lat = to_ll.transform(x, y)
    return np.asarray(lon), np.asarray(lat)


def _interp_points(node: np.ndarray, step: int, r: np.ndarray, c: np.ndarray) -> np.ndarray:
    """Bilinear interpolation of lattice values at pixels (r, c)."""
    nr, nc = node.shape
    u, v = r / step, c / step
    i0, j0 = np.minimum(u.astype(np.int64), nr - 2), np.minimum(v.astype(np.int64), nc - 2)
    fr, fc = u - i0, v - j0
    return (node[i0, j0] * (1 - fr) * (1 - fc) + node[i0 + 1, j0] * fr * (1 - fc)
            + node[i0, j0 + 1] * (1 - fr) * fc + node[i0 + 1, j0 + 1] * fr * fc)


def _row_lookup(gid: np.ndarray, seg_base: int) -> dict[int, tuple[np.ndarray, np.ndarray]]:
    """tile index -> (lut: local seg id -> tile-row position or -1, tile rows: global row per position)."""
    order = np.argsort(gid, kind="stable")
    g = gid[order]
    tiles = g // seg_base
    out = {}
    for t in np.unique(tiles):
        a, b = np.searchsorted(tiles, [t, t + 1])
        local = g[a:b] - t * seg_base
        lut = np.full(int(local.max()) + 1, -1, dtype=np.int64)
        lut[local] = np.arange(b - a)
        out[int(t)] = (lut, order[a:b])
    return out


@dataclass
class _GridAcc:
    grid: GridSpec
    rows: list = field(default_factory=list)
    cells: list = field(default_factory=list)
    counts: list = field(default_factory=list)
    outside_px: int = 0
    max_err_m: float = 0.0
    mismatched: int = 0
    mixed_blocks: int = 0
    blocks: int = 0


class _TileGrid:
    """One grid seen from one tile: its cell window (one-cell margin) and the cell of every lattice block.

    Block (i, j) holds the pixels whose lattice coordinates lie in [i, i+1) x [j, j+1). Pixel lon/lat
    is the bilinear interpolation of the four corner nodes, so it lies in their convex hull: when the
    four corners share a cell (a lon/lat rectangle), so does every pixel of the block. Only blocks
    whose corners straddle a cell edge need per-pixel coordinates.
    """

    def __init__(self, grid: GridSpec, lon_n: np.ndarray, lat_n: np.ndarray):
        r, c = grid.cell_rc(lon_n, lat_n)
        self.r_lo, self.c_lo = int(r.min()) - 1, int(c.min()) - 1
        self.nr, self.nc = int(r.max()) + 2 - self.r_lo, int(c.max()) + 2 - self.c_lo
        self.grid = grid
        node = (r - self.r_lo) * self.nc + (c - self.c_lo)
        a = node[:-1, :-1]
        same = (a == node[1:, :-1]) & (a == node[:-1, 1:]) & (a == node[1:, 1:])
        self.block = np.where(same, a, -1)

    @property
    def ncell(self) -> int:
        return self.nr * self.nc

    def local_cells(self, lon, lat) -> np.ndarray:
        r, c = self.grid.cell_rc(lon, lat)
        r -= self.r_lo
        c -= self.c_lo
        ok = (r >= 0) & (r < self.nr) & (c >= 0) & (c < self.nc)
        return np.where(ok, r * self.nc + c, -1)


@njit(cache=True)
def _count_strip(g, r0, base_off, lut, block, step, lon_n, lat_n, lon_e0, dlon, lat_e0, dlat, r_lo, c_lo, nr, nc,
                 counts):  # pragma: no cover - compiled
    """Add one strip's pixels to ``counts[pos * ncell + cell]``; returns (assigned, unknown gid, outside).

    The same arithmetic as ``_interp_points`` + ``GridSpec.cell_rc``, one pass, no temporaries.
    """
    ncell = nr * nc
    nlr, nlc = lon_n.shape
    assigned, unknown, outside = 0, 0, 0
    for i in range(g.shape[0]):
        r = r0 + i
        bi = r // step
        u = r / step
        i0 = min(int(u), nlr - 2)
        fr = u - i0
        for j in range(g.shape[1]):
            v = g[i, j]
            if v < 0:
                continue
            loc = v - base_off
            if loc < 0 or loc >= lut.size or lut[loc] < 0:
                unknown += 1
                continue
            cell = block[bi, j // step]
            if cell < 0:
                w = j / step
                j0 = min(int(w), nlc - 2)
                fc = w - j0
                lon = (lon_n[i0, j0] * (1 - fr) * (1 - fc) + lon_n[i0 + 1, j0] * fr * (1 - fc)
                       + lon_n[i0, j0 + 1] * (1 - fr) * fc + lon_n[i0 + 1, j0 + 1] * fr * fc)
                lat = (lat_n[i0, j0] * (1 - fr) * (1 - fc) + lat_n[i0 + 1, j0] * fr * (1 - fc)
                       + lat_n[i0, j0 + 1] * (1 - fr) * fc + lat_n[i0 + 1, j0 + 1] * fr * fc)
                cr = int(math.floor((lat - lat_e0) / dlat)) - r_lo
                cc = int(math.floor((lon - lon_e0) / dlon)) - c_lo
                if cr < 0 or cr >= nr or cc < 0 or cc >= nc:
                    outside += 1
                    continue
                cell = cr * nc + cc
            counts[lut[loc] * ncell + cell] += 1
            assigned += 1
    return assigned, unknown, outside


def _tile(path: Path, luts: dict, accs: list[_GridAcc], stats: dict, seg_base: int, strip_rows: int, step: int,
          check_points: int, rng) -> float:
    """Count pixels per (catchment, cell) of one tile into ``accs``; returns the pixel area (m²)."""
    land_path = Path(path).parent / "area_code.tif"
    lds = rasterio.open(land_path) if land_path.exists() else None
    with rasterio.open(path) as ds:
        h, w, tr = ds.height, ds.width, ds.transform
        to_ll = Transformer.from_crs(ds.crs, "EPSG:4326", always_xy=True)
        lon_n, lat_n = _lattice(tr, h, w, step, to_ll)
        tgs = [_TileGrid(acc.grid, lon_n, lat_n) for acc in accs]
        sums: list[np.ndarray | None] = [None] * len(accs)
        base, lut, tile_rows = None, None, None
        for r0 in range(0, h, strip_rows):
            nrows = min(strip_rows, h - r0)
            g = ds.read(1, window=Window(0, r0, w, nrows))
            valid = g >= 0
            if lds is not None:
                land = lds.read(1, window=Window(0, r0, w, nrows)) > 0
                stats["land_pixels"] += int(land.sum())
                stats["land_unassigned"] += int((land & ~valid).sum())
            if not valid.any():
                continue
            gv = g[valid]
            t_lo, t_hi = int(gv.min()) // seg_base, int(gv.max()) // seg_base
            del gv
            if t_lo != t_hi or (base is not None and t_lo != base):
                raise ValueError(f"{path}: segment ids of several tiles in one raster")
            if base is None:
                base = t_lo
                lut, tile_rows = luts.get(base, (np.full(1, -1, dtype=np.int64), np.zeros(0, dtype=np.int64)))
                sums = [np.zeros(tile_rows.size * tg.ncell, dtype=np.int64) for tg in tgs]
            for j, (acc, tg) in enumerate(zip(accs, tgs, strict=True)):
                gr = acc.grid
                done, unknown, outside = _count_strip(
                    g, r0, base * seg_base, lut, tg.block, step, lon_n, lat_n, gr.lon0 - gr.dlon / 2, gr.dlon,
                    gr.lat0 - gr.dlat / 2, gr.dlat, tg.r_lo, tg.c_lo, tg.nr, tg.nc, sums[j])
                acc.outside_px += outside
                if j == 0:
                    stats["pixels_assigned"] += done
                    stats["pixels_unknown_gid"] += unknown
        # interpolation error against the exact transform, on random pixels of the tile
        rr, cc = rng.integers(0, h, check_points), rng.integers(0, w, check_points)
        lon_x, lat_x = to_ll.transform(tr.c + (cc + 0.5) * tr.a + (rr + 0.5) * tr.b,
                                       tr.f + (cc + 0.5) * tr.d + (rr + 0.5) * tr.e)
        lon_i, lat_i = _interp_points(lon_n, step, rr, cc), _interp_points(lat_n, step, rr, cc)
        mx, my = metres_per_degree(lat_x)
        err = float(np.hypot((lon_i - lon_x) * mx, (lat_i - lat_x) * my).max())
        for acc, tg, cnt in zip(accs, tgs, sums, strict=True):
            acc.max_err_m = max(acc.max_err_m, err)
            acc.mismatched += int((acc.grid.cell_index(lon_i, lat_i) != acc.grid.cell_index(lon_x, lat_x)).sum())
            acc.blocks += tg.block.size
            acc.mixed_blocks += int((tg.block < 0).sum())
            if cnt is None:
                continue
            nz = np.flatnonzero(cnt)
            p, cell = np.divmod(nz, tg.ncell)
            gr, gc = tg.r_lo + cell // tg.nc, tg.c_lo + cell % tg.nc
            inside = (gr >= 0) & (gr < acc.grid.ny) & (gc >= 0) & (gc < acc.grid.nx)
            acc.outside_px += int(cnt[nz[~inside]].sum())
            acc.rows.append(tile_rows[p[inside]])
            acc.cells.append((gr * acc.grid.nx + gc)[inside])
            acc.counts.append(cnt[nz[inside]])
    if lds is not None:
        lds.close()
    return abs(tr.a * tr.e)


def build_weights(tile_paths: list[Path], table: pd.DataFrame, grids: list[GridSpec], *, seg_base: int = SEG_BASE,
                  strip_rows: int = 512, lattice_step: int = 64, check_points: int = 4096,
                  log=print) -> tuple[dict[str, CatchmentWeights], dict]:
    """Weight matrices of every grid in one pass over the local-catchment tiles.

    ``table`` needs ``gid`` and ``down`` (row index downstream, -1 none); ``area_km2``, ``region``,
    ``lon``/``lat`` are used when present (checks, factor regions, fallback cells). Returns the
    weights per grid name and the build statistics. Memory stays at a few hundred MB: one strip of
    ``strip_rows`` rows at a time, and GDAL's block cache is capped (each block is read once).
    """
    t0 = time.time()
    n = len(table)
    gid_all = table["gid"].to_numpy(np.int64)
    luts = _row_lookup(gid_all, seg_base)
    accs = [_GridAcc(g) for g in grids]
    stats = {"tiles": 0, "pixels_assigned": 0, "pixels_unknown_gid": 0, "land_pixels": 0, "land_unassigned": 0,
             "strip_rows": strip_rows, "lattice_step_px": lattice_step}
    rng = np.random.default_rng(20261119)
    px_area = None
    with rasterio.Env(GDAL_CACHEMAX=64):
        for k, path in enumerate(tile_paths):
            tt = time.time()
            a = _tile(path, luts, accs, stats, seg_base, strip_rows, lattice_step, check_points, rng)
            if px_area is not None and abs(a - px_area) > 1e-9:
                raise ValueError(f"{path}: pixel area {a} m2 differs from {px_area} m2")
            px_area = a
            stats["tiles"] += 1
            log(f"  [{k + 1}/{len(tile_paths)}] {Path(path).parent.name}: {time.time() - tt:.1f}s")
    stats["build_seconds"] = round(time.time() - t0, 1)
    stats["pixel_area_m2"] = px_area

    down = table["down"].to_numpy(np.int64) if "down" in table else np.full(n, -1, dtype=np.int64)
    area = table["area_km2"].to_numpy(np.float64) if "area_km2" in table else np.zeros(n)
    up = down >= 0
    local_tab = area - np.bincount(down[up], weights=area[up], minlength=n)
    if "region" in table:
        regions, region_of = np.unique(table["region"].astype(str).to_numpy(), return_inverse=True)
        regions, region_of = tuple(str(r) for r in regions), region_of.astype(np.int16)
    else:
        regions, region_of = ("",), np.zeros(n, dtype=np.int16)

    out = {}
    for acc in accs:
        rows = np.concatenate(acc.rows) if acc.rows else np.zeros(0, dtype=np.int64)
        cells = np.concatenate(acc.cells) if acc.cells else np.zeros(0, dtype=np.int64)
        cnts = np.concatenate(acc.counts) if acc.counts else np.zeros(0, dtype=np.int64)
        npix = np.bincount(rows, weights=cnts, minlength=n).astype(np.int64)
        fb = np.flatnonzero(npix == 0)
        no_cell = 0
        if len(fb) and {"lon", "lat"} <= set(table.columns):
            fcell = acc.grid.cell_index(table["lon"].to_numpy()[fb], table["lat"].to_numpy()[fb])
            ok = fcell >= 0
            no_cell = int((~ok).sum())
            rows = np.concatenate([rows, fb[ok]])
            cells = np.concatenate([cells, fcell[ok]])
            cnts = np.concatenate([cnts, np.ones(int(ok.sum()), dtype=np.int64)])
        else:
            no_cell = len(fb)
        counts = sp.csr_matrix((cnts.astype(np.int32), (rows, cells)), shape=(n, acc.grid.size))
        counts.sum_duplicates()
        meta = {"built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "build": dict(stats),
                "interp_max_error_m": acc.max_err_m, "interp_checked_pixels": stats["tiles"] * check_points,
                "interp_cell_mismatches": acc.mismatched, "pixels_outside_grid": acc.outside_px,
                "lattice_blocks": acc.blocks, "lattice_blocks_split_by_cell_edge": acc.mixed_blocks,
                "fallback_rows_without_cell": no_cell}
        cw = CatchmentWeights(acc.grid, counts, gid_all, npix, float(px_area or 0.0), down, region_of, regions,
                              area.astype(np.float32), local_tab.astype(np.float32), meta)
        cw.meta["checks"] = check_weights(cw)
        out[acc.grid.name] = cw
    return out, stats


def check_weights(cw: CatchmentWeights) -> dict:
    """Row sums, catchment counts and pixel area against the segment table."""
    W = cw.W
    rs = np.asarray(W.sum(axis=1)).ravel()
    filled = np.diff(W.indptr) > 0
    cells_per_row = np.diff(W.indptr)
    pix_km2 = cw.area_m2 / 1e6
    tab = cw.table_local_km2.astype(np.float64)
    big = tab >= 0.01  # >= ~156 pixels: ratios of smaller areas are dominated by rounding
    ratio = pix_km2[big] / tab[big]
    q = np.quantile(ratio, [0.05, 0.25, 0.5, 0.75, 0.95]) if big.any() else np.full(5, np.nan)
    outlets = cw.down < 0
    return {
        "rows": int(cw.n), "nnz": int(W.nnz), "grid_cells": int(cw.grid.size),
        "rows_empty": int((~filled).sum()),
        "weight_sum_max_abs_dev": float(np.abs(rs[filled] - 1.0).max()) if filled.any() else None,
        "cells_per_catchment_mean": round(float(cells_per_row[filled].mean()), 3) if filled.any() else None,
        "cells_per_catchment_max": int(cells_per_row.max()) if cw.n else 0,
        "catchments_in_one_cell": int((cells_per_row == 1).sum()),
        "missing_no_pixels": int(cw.fallback.sum()),
        "tiny_lt_0.001_km2": int(((cw.npix > 0) & (pix_km2 < 0.001)).sum()),
        "small_lt_0.01_km2": int(((cw.npix > 0) & (pix_km2 < 0.01)).sum()),
        "pixel_area_km2_total": round(float(pix_km2.sum()), 1),
        "table_local_km2_total": round(float(tab.sum()), 1),
        "table_outlet_area_km2_total": round(float(cw.table_area_km2[outlets].astype(np.float64).sum()), 1),
        "table_local_negative": int((tab < 0).sum()),
        "area_ratio_rows_compared": int(big.sum()),
        "area_ratio_q05_q25_q50_q75_q95": [round(float(v), 4) for v in q],
        "area_ratio_within_10pct": round(float((np.abs(ratio - 1) <= 0.10).mean()), 4) if big.any() else None,
        "area_ratio_within_25pct": round(float((np.abs(ratio - 1) <= 0.25).mean()), 4) if big.any() else None,
    }


def tile_paths(tiles_dir: Path) -> list[Path]:
    return sorted(Path(tiles_dir).glob("*/local_catchment_core.tif"))
