"""Benchmark of the 1 m LiDAR pipeline on one national tile (rf-lxa.2, ``docs/lidar-pipeline.md``).

``river-finder lidar bench stream | speed | route | compare | tiling | seam | lakes``, outputs in
``data/analysis/lidar-bench/``.

- **stream**: the tile's box (core + 3 km halo) read at full 1 m resolution from the national mosaic in parallel
  windows and aggregated on the fly: 5 m block mean and block minimum, 8 m block mean, the flattened / interpolated
  water share per cell; the 1 m DEM is kept only for the sub-area ``SUB`` (and 2 m block mean / minimum made from
  it). Also the 8 m contour DEM of the current national product for the same box.
- **route**: each variant (``VARIANTS``: 8 m contour = today's product, LiDAR at 8 / 5 / 2 / 1 m by block mean or
  minimum, LINZ lines burnt in) routed with ``lidarpipe.route_lean`` exactly as ``nz.route_tile`` routes a tile
  (REC2 border outlets and inflows, lake fill, 8 m fallback with the seam correction), cut into ~250 m reaches,
  validated against REC2 and traced along the guide sections. Time and peak memory per step.
- **compare**: per variant, channel offsets from the LINZ river lines and from the 1 m channel, distances to the guide
  put-ins and take-outs, REC2 catchment agreement, flats, and the guide sections' long profiles sampled on the
  full-resolution 1 m DEM (read again in windows along the routed lines, as the extraction pass would).
- **tiling**: the 5 m tile cut into 2 x 2 sub-tiles of 32 km with a 3 km halo, each routed with REC2 inflows, and
  compared with the whole tile at its internal edges.
- **seam**: a block of the tile treated as a LiDAR gap filled from the 8 m DEM, with and without the seam
  correction, against the true LiDAR; and a lake interior removed and filled back at the rim's water level.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pandas as pd

from . import lidarpipe as P
from .regions import REPO_ROOT

TILE_ID = "x05y07"
SUB = (1_437_000, 5_229_000, 1_457_000, 5_250_000)  # 1 m / 2 m sub-area: Hokitika gorge, Toaroha, Kokatahi
MIN_AREA_KM2 = 0.5
SEG_LEN_M = 250.0
# variant -> (source raster, cell m, box: tile | sub, LINZ burn depth m)
VARIANTS = {
    "c8": ("dem8_contour", 8, "tile", 0.0),     # the current national product's DEM
    "l8": ("dem8_mean", 8, "tile", 0.0),        # LiDAR, 8 m block mean
    "l5mean": ("dem5_mean", 5, "tile", 0.0),
    "l5min": ("dem5_min", 5, "tile", 0.0),
    "l5min_b1": ("dem5_min", 5, "tile", 1.0),   # LINZ river lines burnt 1 m
    "l5min_b3": ("dem5_min", 5, "tile", 3.0),
    "s5min": ("dem5_min", 5, "sub", 0.0),       # 5 m on the sub-area box (same box as the 1-2 m runs)
    "s2mean": ("s2_mean", 2, "sub", 0.0),
    "s2min": ("s2_min", 2, "sub", 0.0),
    "s1": ("s1_dem", 1, "sub", 0.0),
}


def out_dir() -> Path:
    d = REPO_ROOT / "data" / "analysis" / "lidar-bench"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _tile():
    from .nz import Tile

    return Tile.parse(TILE_ID)


def box_of(which: str) -> tuple[float, float, float, float]:
    return _tile().box if which == "tile" else SUB


# ---------------------------------------------------------------------------------------------- resources
def _rss() -> int:
    with open("/proc/self/statm") as f:
        return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")


class Meter:
    """Wall time and peak resident memory per named step (RSS sampled every 50 ms by a thread)."""

    def __init__(self):
        self.steps: dict[str, dict] = {}
        self._peak = 0
        self._run = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while self._run:
            self._peak = max(self._peak, _rss())
            time.sleep(0.05)

    @contextmanager
    def step(self, name: str):
        self._peak = _rss()
        t = time.time()
        yield
        self.steps[name] = {"s": round(time.time() - t, 1), "peak_gb": round(max(self._peak, _rss()) / 1e9, 2)}

    def close(self):
        self._run = False


def _du(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0


# ---------------------------------------------------------------------------------------------- rasters
def _profile(box, cell: float, dtype="float32", nodata=np.nan, compress="zstd"):
    from rasterio.transform import from_origin

    x0, y0, x1, y1 = box
    p = dict(driver="GTiff", width=int(round((x1 - x0) / cell)), height=int(round((y1 - y0) / cell)), count=1,
             dtype=dtype, crs="EPSG:2193", transform=from_origin(x0, y1, cell, cell), nodata=nodata,
             tiled=True, blockxsize=512, blockysize=512, compress=compress, BIGTIFF="IF_SAFER")
    if dtype.startswith("float"):
        p["predictor"] = 3
    return p


def write(path: Path, a: np.ndarray, box, cell: float, nodata=np.nan, **tags) -> None:
    import rasterio

    p = _profile(box, cell, str(a.dtype), nodata)
    with rasterio.open(path, "w", **p) as dst:
        dst.write(a, 1)
        if tags:
            dst.update_tags(**{k: str(v) for k, v in tags.items()})


def read(path: Path, box=None) -> tuple[np.ndarray, object]:
    import rasterio
    from rasterio.windows import from_bounds

    with rasterio.open(path) as ds:
        w = None if box is None else from_bounds(*box, ds.transform).round_offsets().round_lengths()
        a = ds.read(1, window=w)
        tf = ds.transform if w is None else ds.window_transform(w)
        nd = ds.nodata
    if nd is not None and np.isfinite(nd) and a.dtype.kind == "f":
        a[a <= nd + 1 if nd < -1000 else a == nd] = np.nan
    return a, tf


def rasters() -> Path:
    d = out_dir() / "rasters"
    d.mkdir(exist_ok=True)
    return d


# ---------------------------------------------------------------------------------------------- stream
def run_stream(threads: int = 32, log=print) -> dict:
    """Stream the tile box at 1 m into the 5 m / 8 m aggregates and the sub-area's 1 m / 2 m rasters."""
    from .nz import DEM_8M, Tile
    from .stac import mosaic

    box = _tile().box
    bx0, by0, bx1, by1 = (int(v) for v in box)
    H, W = by1 - by0, bx1 - bx0
    meter = Meter()
    rd = rasters()
    cache = REPO_ROOT / "data" / "_stac"
    with meter.step("sheets"):
        sheets = P.mosaic_sheets(box, cache)
        jobs = P.sheet_windows(sheets, box, win=2048)
    log(f"bench: {len(sheets)} sheets, {len(jobs)} windows over {box}")
    sx0, sy0, sx1, sy1 = SUB
    sr0, sc0, sh, sw = by1 - sy1, sx0 - bx0, sy1 - sy0, sx1 - sx0
    a5 = P.Agg((H, W), 5)
    a8 = P.Agg((H, W), 8)
    z1 = np.full((sh, sw), np.nan, np.float32)
    n_nan = [0]

    def consume(z, br, bc):
        fl = P.water_flags(z)
        a5.add(z, br, bc, fl)
        a8.add(z, br, bc, fl)
        n_nan[0] += int(np.isnan(z).sum())
        h, w = z.shape
        r0, r1 = max(br, sr0), min(br + h, sr0 + sh)
        c0, c1 = max(bc, sc0), min(bc + w, sc0 + sw)
        if r0 < r1 and c0 < c1:
            z1[r0 - sr0:r1 - sr0, c0 - sc0:c1 - sc0] = z[r0 - br:r1 - br, c0 - bc:c1 - bc]

    with meter.step("stream_1m"):
        st = P.stream(jobs, consume, threads=threads, log=log, every=400)
    log(f"bench: streamed {st['cells'] / 1e9:.2f} G cells in {st['seconds']} s ({st['mcells_s']} M cells/s)")
    with meter.step("write_5m_8m"):
        write(rd / "dem5_mean.tif", a5.mean(), box, 5)
        write(rd / "dem5_min.tif", a5.minimum(), box, 5)
        write(rd / "flat5.tif", np.round(a5.share("flat") * 100).astype(np.uint8), box, 5, nodata=None)
        write(rd / "planar5.tif", np.round(a5.share("planar") * 100).astype(np.uint8), box, 5, nodata=None)
        write(rd / "dem8_mean.tif", a8.mean(), box, 8)
        write(rd / "flat8.tif", np.round(a8.share("flat") * 100).astype(np.uint8), box, 8, nodata=None)
        valid5 = int((a5.cnt > 0).sum())
        a5 = a8 = None  # free before the sub-area step
    with meter.step("sub_1m_2m"):
        write(rd / "s1_dem.tif", z1, SUB, 1)
        f1 = P.water_flags(z1)
        write(rd / "s1_water.tif", f1, SUB, 1, nodata=None)
        a2 = P.Agg((sh, sw), 2)
        a2.add(z1, 0, 0, f1)
        z1 = f1 = None
        write(rd / "s2_mean.tif", a2.mean(), SUB, 2)
        write(rd / "s2_min.tif", a2.minimum(), SUB, 2)
        write(rd / "flat2.tif", np.round(a2.share("flat") * 100).astype(np.uint8), SUB, 2, nodata=None)
        del a2
    with meter.step("contour_8m"):
        p = rd / "dem8_contour.tif"
        if not p.exists():
            mosaic(DEM_8M, Tile.parse(TILE_ID).region().bbox_wgs84, box, 8.0, p, cache)
    meter.close()
    res = {"box": list(box), "sub": list(SUB), "sheets": [s["id"] for s in sheets], "stream": st,
           "nan_cells_1m": n_nan[0], "valid_cells_5m": valid5, "steps": meter.steps,
           "disk_bytes": {f.name: f.stat().st_size for f in sorted(rd.glob("*.tif"))}}
    (out_dir() / "stream.json").write_text(json.dumps(res, indent=1))
    return res


def stream_speed(threads=(8, 16, 32, 48), win=2048, cells_m: int = 300, log=print) -> list[dict]:
    """Read throughput of full-resolution windows for several thread counts (each on a fresh part of the tile)."""
    box = _tile().box
    sheets = P.mosaic_sheets(box, REPO_ROOT / "data" / "_stac")
    jobs = P.sheet_windows(sheets, box, win=win)
    per = max(1, int(cells_m * 1e6 // (win * win)))
    out, k = [], len(jobs) // 2
    for t in threads:
        part = jobs[k:k + per]
        k += per
        st = P.stream(part, lambda z, br, bc: None, threads=t)
        st["win"] = win
        out.append(st)
        log(f"bench: {t} threads x {win}: {st['mcells_s']} M cells/s")
    return out


# ---------------------------------------------------------------------------------------------- inputs
def linz_lines(box, kinds=("river_cl",)):
    """LINZ NZ River Name Lines (Topo50 centrelines; cached per region in data/nz/linz/) inside a box."""
    import geopandas as gpd
    import shapely

    from .national import nz_dir

    parts = []
    for name in ("west-coast", "canterbury"):
        p = nz_dir() / "linz" / f"{name}.gpkg"
        if p.exists():
            g = gpd.read_file(p, bbox=tuple(box))
            parts.append(g[g["feat_type"].isin(kinds)])
    g = pd.concat(parts, ignore_index=True)
    g = gpd.GeoDataFrame(g, geometry="geometry", crs="EPSG:2193")
    g = g[~g["river_section_id"].duplicated()].reset_index(drop=True)
    g["geometry"] = shapely.clip_by_rect(g.geometry.values, *box)
    return g[~g.geometry.is_empty].reset_index(drop=True)


def rec2_near(box, pad: float = 5e4):
    import geopandas as gpd

    from .national import nz_dir

    x0, y0, x1, y1 = box
    return gpd.read_file(nz_dir() / "rec2.gpkg", bbox=(x0 - pad, y0 - pad, x1 + pad, y1 + pad))


def guide_sections(box) -> list[dict]:
    """Runnable guide sections with a put-in or take-out inside ``box``: river, grade, put-in / take-out in NZTM."""
    from pyproj import Transformer

    from .models import all_sections

    t = Transformer.from_crs(4326, 2193, always_xy=True)
    out = []
    for s in all_sections():
        if s.status != "runnable":
            continue
        pi = t.transform(*s.put_in) if s.put_in else None
        to = t.transform(*s.take_out) if s.take_out else None
        if not any(p and box[0] <= p[0] < box[2] and box[1] <= p[1] < box[3] for p in (pi, to)):
            continue
        out.append({"river": s.river, "grade": s.grade or "", "put_in": pi, "take_out": to,
                    "key": f"{s.river} {s.grade} ({s.put_in[0]:.3f},{s.put_in[1]:.3f})" if s.put_in else s.river})
    return out


# ---------------------------------------------------------------------------------------------- route
def prepare_dem(name: str, log=print) -> tuple[np.ndarray, object, dict]:
    """The variant's routing DEM: LiDAR aggregate, lake holes filled at the rim's water level, the remaining land
    holes from the 8 m contour DEM with the seam correction, LINZ lines burnt when the variant says so."""
    import rasterio
    from rasterio.warp import Resampling, reproject

    src, cell, which, depth = VARIANTS[name]
    box = box_of(which)
    rd = rasters()
    z, tf = read(rd / f"{src}.tif", box)
    z = np.ascontiguousarray(z, np.float32)
    info: dict = {"cells": int(z.size), "nan_before": int(np.isnan(z).sum())}
    if src != "dem8_contour" and np.isnan(z).any():
        flat_src = {5: "flat5", 8: "flat8", 2: "flat2"}.get(cell)
        if flat_src:
            fs, _ = read(rd / f"{flat_src}.tif", box)
            fs = fs.astype(np.float32) / 100.0
        else:
            w, _ = read(rd / "s1_water.tif", box)
            fs = (w == 1).astype(np.uint8)
            del w
        z8 = np.full(z.shape, np.nan, np.float32)
        with rasterio.open(rd / "dem8_contour.tif") as ds:
            reproject(rasterio.band(ds, 1), z8, dst_transform=tf, dst_crs="EPSG:2193",
                      resampling=Resampling.bilinear, src_nodata=ds.nodata, dst_nodata=np.nan)
        info["lakes"] = [r for r in P.fill_lakes(z, fs, land=np.isfinite(z8)) if r["cells"] >= 20]
        del fs
        hole = np.isnan(z)
        if hole.any():
            land = hole & np.isfinite(z8)
            info["fallback_cells"] = int(land.sum())
            if land.any():
                z, info["seam"] = P.seam_correct(z, z8, float(cell))
                z = np.ascontiguousarray(z, np.float32)
        del z8
    if src != "dem8_contour":
        info["nan_after"] = int(np.isnan(z).sum())
    if depth:
        info["burnt_cells"] = P.burn(z, list(linz_lines(box).geometry), tf, depth)
    return z, tf, info


def trace(d: np.ndarray, acc: np.ndarray, tf, p0, p1, search_m: float = 150.0, min_km2: float = 1.0):
    """Flow path from the channel cell of largest area within ``search_m`` of ``p0`` down to the closest approach to
    ``p1`` (None when ``p0`` has no channel nearby). Returns flat cell indices."""
    nr, nc = d.shape
    cs = tf.a
    r, c = int((tf.f - p0[1]) // cs), int((p0[0] - tf.c) // cs)
    k = int(round(search_m / cs))
    r0, r1, c0, c1 = max(0, r - k), min(nr, r + k + 1), max(0, c - k), min(nc, c + k + 1)
    if r0 >= r1 or c0 >= c1:
        return None
    rr, cc = np.mgrid[r0:r1, c0:c1]
    win = np.where(np.hypot(rr - r, cc - c) <= k, acc[r0:r1, c0:c1], 0.0)
    j = int(np.argmax(win))
    if win.flat[j] < min_km2 * 1e6:
        return None
    v = (r0 + j // win.shape[1]) * nc + (c0 + j % win.shape[1])
    off = P.offsets(nc)
    df = d.ravel()
    path, best, bd = [v], 0, np.inf
    straight = math.dist(p0, p1)
    while df[v] < 8 and len(path) * cs < 3 * straight + 5000:
        v = v + off[df[v]]
        path.append(v)
        x, y = tf.c + (v % nc + 0.5) * cs, tf.f - (v // nc + 0.5) * cs
        dd = math.hypot(x - p1[0], y - p1[1])
        if dd < bd:
            best, bd = len(path) - 1, dd
        elif dd > bd + 2000:
            break
    return np.asarray(path[:best + 1], np.int64), bd


class _Km2:
    """km2 view of an m2 accumulation grid for ``validate.sample_max`` (no full-size copy)."""

    def __init__(self, acc_m2):
        self.a = acc_m2
        self.shape = acc_m2.shape

    def __getitem__(self, k):
        return self.a[k] / 1e6


def rec2_check(area_km2: np.ndarray, tf, rec2, boxes: dict, min_km2: float = 1.0) -> pd.DataFrame:
    """REC2 cumulative area against the modelled area 100 m above each REC2 reach's down end (strict: max within
    32 m; tolerant: growing radius until half the REC2 area is found, as ``validate.py``)."""
    from .validate import _lines, _point_upstream, sample_max, snap_tolerant

    rows = []
    shp = area_km2.shape
    x0, y1 = tf.c, tf.f
    x1, y0 = x0 + shp[1] * tf.a, y1 - shp[0] * tf.a
    for f in rec2.iterfeatures(na="null"):
        p = f["properties"]
        if p["cum_area_km2"] < min_km2 or f["geometry"] is None:
            continue
        line = max(_lines(f["geometry"]), key=len)
        length = sum(math.dist(a, b) for a, b in zip(line, line[1:], strict=False))
        x, y = _point_upstream(line, (p["downcoordX"], p["downcoordY"]), min(100.0, 0.3 * length))
        if not (x0 + 50 < x < x1 - 50 and y0 + 50 < y < y1 - 50):
            continue
        inside = {k: b[0] <= x < b[2] and b[1] <= y < b[3] for k, b in boxes.items()}
        if not any(inside.values()):
            continue
        strict = sample_max(area_km2, tf, x, y, max(1, round(32.0 / tf.a)))
        tol, rad = snap_tolerant(area_km2, tf, x, y, p["cum_area_km2"])
        rows.append({"nzsegment": int(p["nzsegment"]), "name": p.get("name") or "", "rec2_km2": p["cum_area_km2"],
                     "model_km2": strict, "model_tolerant_km2": tol, "tolerant_radius_m": rad, **inside})
    return pd.DataFrame(rows)


def product_sizes(d, acc, zf, tf, seg, od: Path) -> dict:
    """Compressed size of the per-tile rasters a 5 m national tile would keep (written to the core, measured, then
    deleted): area code (uint8), D8 directions (uint8), filled DEM (float32, LERC 1 mm and lossless), local
    catchment (int32) and hillshade 24 m; plus the reach lines at full 5 m vertex density."""
    import rasterio

    from .areacode import encode_area
    from .products import hillshade

    core = _tile().core
    r0, c0 = int(round((tf.f - core[3]) / tf.a)), int(round((core[0] - tf.c) / tf.a))
    n = int(round((core[2] - core[0]) / tf.a))
    win = (slice(r0, r0 + n), slice(c0, c0 + n))
    valid = d[win] != P.NODATA
    tmp = od / "_sizes"
    tmp.mkdir(exist_ok=True)
    out = {}

    def w(name, a, nodata, **kw):
        p = dict(_profile(core, tf.a, str(a.dtype), nodata), **kw)
        if not str(a.dtype).startswith("float") or p["compress"] == "lerc":
            p.pop("predictor", None)
            if a.dtype.itemsize > 1 and p["compress"] != "lerc":
                p["predictor"] = 2
        with rasterio.open(tmp / f"{name}.tif", "w", **p) as dst:
            dst.write(a, 1)
        out[name] = (tmp / f"{name}.tif").stat().st_size

    code = np.zeros(valid.shape, np.uint8)
    for k in range(0, n, 1024):                                     # in row blocks: no full-size float64 copies
        code[k:k + 1024] = encode_area(np.where(valid[k:k + 1024], acc[win][k:k + 1024] / 1e6, np.nan))
    w("area_code_deflate", code, 0, compress="deflate")
    del code
    w("dir_uint8_deflate", d[win].copy(), 255, compress="deflate")
    w("dir_uint8_zstd", d[win].copy(), 255, compress="zstd")
    zc = np.where(valid, zf[win], np.nan).astype(np.float32)
    w("filled_lerc_1mm", zc, np.nan, compress="lerc", max_z_error=0.001)
    w("filled_zstd", zc, np.nan, compress="zstd")
    del zc
    lc = P.local_catchment(d, seg)[win].copy()
    w("local_catchment_int32", lc, -1, compress="deflate")
    del lc
    f = 5                                                            # hillshade of the 25 m block mean
    m = (n // f) * f
    z25 = np.where(valid, zf[win], np.nan)[:m, :m].reshape(m // f, f, m // f, f)
    with np.errstate(all="ignore"):
        z25 = np.nanmean(z25, axis=(1, 3))
    hs = hillshade(z25, np.isfinite(z25), tf.a * f)
    w("hillshade_25m", hs, 0, compress="deflate", transform=rasterio.transform.from_origin(core[0], core[3], tf.a * f,
                                                                                         tf.a * f),
      width=m // f, height=m // f)
    del hs, z25
    mid = seg.geometry.interpolate(0.5, normalized=True)
    inc = ((mid.x >= core[0]) & (mid.x < core[2]) & (mid.y >= core[1]) & (mid.y < core[3])).to_numpy()
    seg[inc].drop(columns=["cells"]).to_file(tmp / "segments_core.gpkg", driver="GPKG")
    out["segments_core_gpkg_5m_vertices"] = (tmp / "segments_core.gpkg").stat().st_size
    for f_ in tmp.iterdir():
        f_.unlink()
    tmp.rmdir()
    return out


def run_route(name: str, log=print) -> dict:
    """Route one variant; writes ``variants/<name>/`` (segments.gpkg, rec2.csv, paths.npz, route.json)."""
    from .nz import border_outlets, inflow_points

    src, cell, which, depth = VARIANTS[name]
    box = box_of(which)
    od = out_dir() / "variants" / name
    od.mkdir(parents=True, exist_ok=True)
    meter = Meter()
    with meter.step("load"):
        z, tf, info = prepare_dem(name, log)
    near = rec2_near(box)
    with meter.step("route"):
        seed = border_outlets(near, box, tf, z.shape, half_cells=max(1, round(96 / cell)))
        if not seed.any() and not np.isnan(z).any():
            seed = None
        tim: dict = {}
        d = P.route_lean(z, float(cell), border_seed=seed, timings=tim)
        del seed
    with meter.step("accumulate"):
        acc, fl = P.accumulate_lean(d, float(cell))
        used = P.inject(d, acc, fl, tf, inflow_points(near, box), radius_cells=max(1, round(48 / cell)))
    with meter.step("rec2"):
        boxes = {"core": _tile().core, "sub": SUB}
        rc = rec2_check(_Km2(acc), tf, near, boxes)
        rc.to_csv(od / "rec2.csv", index=False)
    with meter.step("segments"):
        seg = P.segments(d, acc, z, tf, MIN_AREA_KM2, SEG_LEN_M, trunc=fl)
        seg.drop(columns=["cells"]).to_file(od / "segments.gpkg", layer="segments", driver="GPKG")
    if name == "l5min":
        with meter.step("products"):
            sizes = product_sizes(d, acc, z, tf, seg, od)
    with meter.step("paths"):
        paths = {}
        for s in guide_sections(box):
            if not (s["put_in"] and s["take_out"]):
                continue
            t = trace(d, acc, tf, s["put_in"], s["take_out"])
            if t is None:
                continue
            cells, gap = t
            r, c = np.divmod(cells, d.shape[1])
            xy = np.column_stack([tf.c + (c + 0.5) * cell, tf.f - (r + 0.5) * cell])
            paths[s["key"]] = {"xy": xy, "z": z.ravel()[cells].astype(np.float64),
                               "area": acc.ravel()[cells] / 1e6, "end_gap_m": gap}
        np.savez_compressed(od / "paths.npz", **{f"{k}|{f}": v[f] for k, v in paths.items()
                                                for f in ("xy", "z", "area")})
        (od / "paths.json").write_text(json.dumps({k: {"end_gap_m": round(v["end_gap_m"], 1)}
                                                   for k, v in paths.items()}, indent=1))
    meter.close()
    res = {"variant": name, "source": src, "cell_m": cell, "box": list(box), "burn_m": depth, "dem": info,
           "product_bytes": sizes if name == "l5min" else None,
           "routing": tim, "inflows": len(used), "segments": len(seg),
           "truncated_segments": int(seg["truncated"].sum()),
           "steps": meter.steps, "peak_gb": max(v["peak_gb"] for v in meter.steps.values()),
           "seconds": round(sum(v["s"] for v in meter.steps.values()), 1), "disk_bytes": _du(od)}
    (od / "route.json").write_text(json.dumps(res, indent=1, default=str))
    log(f"bench: {name}: {res['seconds']} s, peak {res['peak_gb']} GB, {len(seg)} segments, {len(used)} inflows, "
        f"{tim}")
    return res


# ---------------------------------------------------------------------------------------------- evaluation
def _samples(g, step: float = 10.0):
    """Points every ``step`` m along each line: (xy, owner row)."""
    import shapely

    xs, own = [], []
    for i, ln in enumerate(g.geometry.values):
        L = ln.length
        s = np.arange(step / 2, L, step) if L > step else np.array([L / 2])
        xs.append(shapely.get_coordinates(shapely.line_interpolate_point(ln, s)))
        own.append(np.full(len(s), i))
    return np.vstack(xs), np.concatenate(own)


def _densify(lines, step: float = 2.0):
    import shapely

    pts = []
    for ln in lines:
        L = ln.length
        pts.append(shapely.get_coordinates(shapely.line_interpolate_point(ln, np.arange(0, L + step, step))))
    return np.vstack(pts)


AREA_BINS = [(0.5, 2), (2, 10), (10, 100), (100, 1e9)]


def _q(a, qs=(50, 90)):
    a = np.asarray(a, float)
    a = a[np.isfinite(a)]
    return {f"p{q}": round(float(np.percentile(a, q)), 1) for q in qs} | {"n": int(len(a))} if len(a) else {"n": 0}


def _in(xy, box):
    return (xy[:, 0] >= box[0]) & (xy[:, 0] < box[2]) & (xy[:, 1] >= box[1]) & (xy[:, 1] < box[3])


def _sample_raster(a, tf, xy, order=0):
    from scipy.ndimage import map_coordinates

    c = (xy[:, 0] - tf.c) / tf.a - 0.5
    r = (xy[:, 1] - tf.f) / tf.e - 0.5
    return map_coordinates(a, [r, c], order=order, mode="constant", cval=np.nan)


def offsets(names: list[str], log=print) -> dict:
    """Channel offsets per variant: from the LINZ lines (core and sub-area), from the 1 m channel and height above
    it (sub-area), and from the guide put-ins / take-outs."""
    import geopandas as gpd
    from scipy.spatial import cKDTree

    core = _tile().core
    lz = linz_lines(_tile().box)
    ltree = cKDTree(_densify(lz.geometry.values, 2.0))
    ref = gpd.read_file(out_dir() / "variants" / "s1" / "segments.gpkg") if "s1" in names else None
    z1, tf1 = read(rasters() / "s1_dem.tif")
    if ref is not None:
        rxy, rown = _samples(ref, 1.0)
        rarea = ref["area_mid_km2"].to_numpy()[rown]
        rtree = cKDTree(rxy)
    secs = guide_sections(core)
    gpts = np.array([p for s in secs for p in (s["put_in"], s["take_out"]) if p])
    out = {}
    for n in names + ["national_8m", "linz"]:
        if n == "linz":
            g = lz.assign(area_mid_km2=np.nan)
        elif n == "national_8m":
            from .national import nz_dir

            g = gpd.read_file(nz_dir() / "tiles" / TILE_ID / "segments_core.gpkg")
            g = g.rename(columns={"area_km2": "area_km2"}).assign(area_mid_km2=g["area_km2"])
        else:
            g = gpd.read_file(out_dir() / "variants" / n / "segments.gpkg")
        xy, own = _samples(g, 10.0)
        a = g["area_mid_km2"].to_numpy()[own]
        rec: dict = {}
        if n != "linz":
            dl, _ = ltree.query(xy, distance_upper_bound=2000)
            dl[~np.isfinite(dl)] = 2000
            for box_name, box in (("core", core), ("sub", SUB)):
                m = _in(xy, box)
                rec[f"linz_{box_name}"] = {f"{lo:g}-{hi:g}": _q(dl[m & (a >= lo) & (a < hi)]) for lo, hi in AREA_BINS}
                rec[f"linz_{box_name}"]["all>=2"] = _q(dl[m & (a >= 2)])
                rec[f"linz_{box_name}_share_gt20m_ge2"] = round(float((dl[m & (a >= 2)] > 20).mean()), 3) \
                    if (m & (a >= 2)).any() else None
        if ref is not None:
            m = _in(xy, SUB) & (np.isfinite(a) | (n == "linz"))
            q = xy[m]
            k = 24
            dd, ii = rtree.query(q, k=k, distance_upper_bound=600)
            best = np.full(len(q), np.nan)
            bz = np.full(len(q), np.nan)
            aa = a[m]
            for j in range(k):
                ok = np.isfinite(dd[:, j]) & np.isnan(best)
                ii_j = np.minimum(ii[:, j], len(rxy) - 1)
                if n != "linz":
                    ratio = rarea[ii_j] / np.maximum(aa, 1e-6)
                    ok &= (ratio >= 0.5) & (ratio <= 2.0)
                else:
                    ok &= rarea[ii_j] >= 0.5
                best[ok] = dd[ok, j]
                bz[ok] = ii_j[ok]
            have = np.isfinite(bz)
            dz = np.full(len(q), np.nan)
            dz[have] = (_sample_raster(z1, tf1, q[have]) - _sample_raster(z1, tf1, rxy[bz[have].astype(int)]))
            aa = aa if n != "linz" else np.full(len(q), 10.0)
            rec["to_1m"] = {f"{lo:g}-{hi:g}": _q(best[(aa >= lo) & (aa < hi)]) for lo, hi in AREA_BINS}
            rec["to_1m"]["all>=2"] = _q(best[aa >= 2])
            rec["to_1m_unmatched_share"] = round(float(np.isnan(best[aa >= 2]).mean()), 3) if (aa >= 2).any() else None
            rec["dz_above_1m"] = {f"{lo:g}-{hi:g}": _q(dz[(aa >= lo) & (aa < hi)], (50, 90)) for lo, hi in AREA_BINS}
            rec["dz_above_1m"]["all>=2"] = _q(dz[aa >= 2])
            rec["dz_gt1m_share_ge2"] = round(float((dz[(aa >= 2) & np.isfinite(dz)] > 1.0).mean()), 3) \
                if ((aa >= 2) & np.isfinite(dz)).any() else None
        vb = box_of(VARIANTS[n][2]) if n in VARIANTS else core
        gp = gpts[_in(gpts, vb)] if len(gpts) else gpts
        if len(gp):
            big = (a >= 1.0) if n != "linz" else np.ones(len(a), bool)
            t = cKDTree(xy[big])
            dg, _ = t.query(gp)
            rec["guide_points"] = _q(dg) | {"share_le25m": round(float((dg <= 25).mean()), 3), "n_points": len(gp)}
        out[n] = rec
        log(f"bench: offsets {n} done")
    return out


def rec2_summary(names: list[str]) -> dict:
    out = {}
    for n in names:
        p = out_dir() / "variants" / n / "rec2.csv"
        if not p.exists():
            continue
        r = pd.read_csv(p)
        rec = {}
        for box_name in ("core", "sub"):
            if box_name not in r:
                continue
            for th in (5.0, 20.0):
                m = r[box_name].astype(bool) & (r["rec2_km2"] >= th)
                e = ((r["model_km2"] - r["rec2_km2"]) / r["rec2_km2"]).abs()[m]
                et = ((r["model_tolerant_km2"] - r["rec2_km2"]) / r["rec2_km2"]).abs()[m]
                rec[f"{box_name}_ge{th:g}"] = {"n": int(m.sum()), "median_abs_err": round(float(e.median()), 3),
                                               "within_10pct": round(float((e <= 0.1).mean()), 3),
                                               "within_10pct_tolerant": round(float((et <= 0.1).mean()), 3),
                                               "within_25pct_tolerant": round(float((et <= 0.25).mean()), 3)}
        out[n] = rec
    return out


def flats_summary(names: list[str]) -> dict:
    """Zero-gradient reaches (rf-a33): share of channel km (>= 2 km2, core / sub-area) on reaches with less than 1 cm
    of drop, and km in stretches of such reaches longer than 1 km along the network."""
    import geopandas as gpd

    out = {}
    for n in names + ["national_8m"]:
        if n == "national_8m":
            from .national import nz_dir

            g = gpd.read_file(nz_dir() / "tiles" / TILE_ID / "segments_core.gpkg")
            g["drop"] = g["gradient_m_per_km"] * g["length_m"] / 1000
            g["area_mid_km2"] = g["area_km2"]
            down = g["down_seg"].to_numpy()
            ids = g["seg_id"].to_numpy()
            pos = pd.Series(np.arange(len(g)), index=ids)
            down = pd.Series(down).map(pos).fillna(-1).astype(int).to_numpy()
        else:
            p = out_dir() / "variants" / n / "segments.gpkg"
            if not p.exists():
                continue
            g = gpd.read_file(p)
            g["drop"] = g["z_top"] - g["z_bot"]
            down = g["down_seg"].to_numpy()
        mid = g.geometry.interpolate(0.5, normalized=True)
        rec = {}
        for box_name, box in (("core", _tile().core), ("sub", SUB)):
            m = ((mid.x >= box[0]) & (mid.x < box[2]) & (mid.y >= box[1]) & (mid.y < box[3])).to_numpy()
            big = m & (g["area_km2"].to_numpy() >= 2)
            if not big.any():
                continue
            zero = (g["drop"].to_numpy() < 0.01)
            L = g["length_m"].to_numpy()
            # stretch length: consecutive zero reaches down the network
            run = np.zeros(len(g))
            up_n = np.zeros(len(g), int)
            for i in range(len(g)):
                if down[i] >= 0:
                    up_n[down[i]] += 1
            order = [i for i in range(len(g)) if up_n[i] == 0]
            indeg = up_n.copy()
            k = 0
            while k < len(order):
                i = order[k]
                k += 1
                j = down[i]
                if j >= 0:
                    indeg[j] -= 1
                    if indeg[j] == 0:
                        order.append(j)
            for i in order:
                if zero[i]:
                    run[i] += L[i]
                    j = down[i]
                    if j >= 0 and zero[j]:
                        run[j] = max(run[j], run[i])
            # propagate run maxima back up each stretch so every reach knows its stretch length
            stretch = run.copy()
            for i in reversed(order):
                j = down[i]
                if zero[i] and j >= 0 and zero[j]:
                    stretch[i] = max(stretch[i], stretch[j])
            km = L[big].sum() / 1000
            rec[box_name] = {"km_ge2": round(km, 1), "zero_share": round(float(L[big & zero].sum() / 1000 / km), 4),
                             "zero_km_in_stretches_gt1km": round(float(L[big & zero & (stretch > 1000)].sum() / 1000),
                                                                 2),
                             "reaches_ge2": int(big.sum())}
        out[n] = rec
    return out


def read_1m_window(bounds, threads: int = 16) -> tuple[np.ndarray, object]:
    """Full-resolution 1 m mosaic inside whole-metre bounds (re-read along a routed line, as the extraction pass
    would)."""
    from rasterio.transform import from_origin

    x0, y0, x1, y1 = (int(math.floor(bounds[0])), int(math.floor(bounds[1])), int(math.ceil(bounds[2])),
                      int(math.ceil(bounds[3])))
    box = (x0, y0, x1, y1)
    z = np.full((y1 - y0, x1 - x0), np.nan, np.float32)
    sheets = P.mosaic_sheets(box, REPO_ROOT / "data" / "_stac")

    def consume(w, br, bc):
        z[br:br + w.shape[0], bc:bc + w.shape[1]] = w

    P.stream(P.sheet_windows(sheets, box, 1024), consume, threads=threads)
    return z, from_origin(x0, y1, 1, 1)


def _window_min(z1, tf1, xy, rad: int) -> np.ndarray:
    """Lowest finite 1 m cell within ``rad`` cells of each point (NaN where none)."""
    r = np.floor((tf1.f - xy[:, 1]) / -tf1.e).astype(np.int64)
    c = np.floor((xy[:, 0] - tf1.c) / tf1.a).astype(np.int64)
    dr, dc = np.mgrid[-rad:rad + 1, -rad:rad + 1]
    rr = np.clip(r[:, None] + dr.ravel()[None], 0, z1.shape[0] - 1)
    cc = np.clip(c[:, None] + dc.ravel()[None], 0, z1.shape[1] - 1)
    v = z1[rr, cc]
    with np.errstate(all="ignore"):
        out = np.nanmin(np.where(np.isfinite(v), v, np.inf), axis=1)
    out[~np.isfinite(out)] = np.nan
    return out


def profile_metrics(xy: np.ndarray, z1, tf1, w1, cell: float, z_own=None) -> dict:
    """Long profile of a routed path on the 1 m DEM: each path point takes the lowest 1 m cell within half a routing
    cell (+1 m) of it (the 1 m channel bottom), the profile its downstream running minimum."""
    s_raw = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    # distances along the line smoothed over 20 m (no D8 staircase): used for every length and gradient below
    s = P.smooth_distance(xy, 20.0, cell)
    L_s = float(s[-1])
    rad = int(math.ceil(cell / 2 + 1))
    zb = _window_min(z1, tf1, xy, rad)
    zm = P.running_min(zb)
    wf = _sample_raster(w1, tf1, xy) if w1 is not None else np.zeros(len(xy))
    drop = float(zm[0] - zm[-1]) if np.isfinite(zm[[0, -1]]).all() else float("nan")
    rec = {"length_m": round(float(s_raw[-1])), "length_smooth_m": round(L_s), "drop_m": round(drop, 2),
           "grad_m_per_km": round(drop / L_s * 1000, 2) if L_s > 0 else None,
           "steep50_raw": round(P.steepest(s, np.where(np.isfinite(zb), zb, zm), 50.0), 1),
           "steep50_rmin": round(P.steepest(s, zm, 50.0), 1),
           "steep100_rmin": round(P.steepest(s, zm, 100.0), 1),
           "steep200_rmin": round(P.steepest(s, zm, 200.0), 1),
           "longest_flat_1m_m": round(P.longest_flat(s, zm, 0.05)),
           "flat_share": round(float(np.mean(wf == 1)), 3), "planar_share": round(float(np.mean(wf == 2)), 3),
           "uphill_gt0p3": int((np.diff(np.nan_to_num(zb, nan=0)) > 0.3).sum())}
    if z_own is not None:
        rec["drop_own_m"] = round(float(z_own[0] - z_own[-1]), 2)
        rec["steep50_own"] = round(P.steepest(s, z_own, 50.0), 1)
        rec["longest_flat_own_m"] = round(P.longest_flat(s, z_own, 0.05))
    return rec


def profiles(names: list[str], log=print) -> dict:
    """Guide-section long profiles per variant, sampled on the 1 m DEM (the sub-area raster, or re-read)."""
    import rasterio

    paths = {}
    for n in names:
        p = out_dir() / "variants" / n / "paths.npz"
        if not p.exists():
            continue
        f = np.load(p)
        for key in f.files:
            sec, fld = key.rsplit("|", 1)
            paths.setdefault(sec, {}).setdefault(n, {})[fld] = f[key]
    z1s, tf1s = read(rasters() / "s1_dem.tif")
    with rasterio.open(rasters() / "s1_water.tif") as ds:
        w1s = ds.read(1)
    out = {}
    t_read, cells_read = 0.0, 0
    for sec, by in paths.items():
        allxy = np.vstack([v["xy"] for v in by.values()])
        bb = (allxy[:, 0].min() - 30, allxy[:, 1].min() - 30, allxy[:, 0].max() + 30, allxy[:, 1].max() + 30)
        inside = bb[0] >= SUB[0] and bb[1] >= SUB[1] and bb[2] <= SUB[2] and bb[3] <= SUB[3]
        if inside:
            z1, tf1, w1 = z1s, tf1s, w1s
        else:
            t = time.time()
            z1, tf1 = read_1m_window(bb)
            w1 = P.water_flags(z1)
            t_read += time.time() - t
            cells_read += z1.size
        rec = {"in_sub": inside}
        ref_end = by["l5min"]["xy"][-1] if "l5min" in by else None
        for n, v in by.items():
            rec[n] = profile_metrics(v["xy"], z1, tf1, w1, VARIANTS[n][1], v["z"])
            rec[n]["area_end_km2"] = round(float(v["area"][-1]), 1)
            rec[n]["same_end"] = bool(ref_end is None or math.dist(v["xy"][-1], ref_end) <= 300)
        out[sec] = rec
        log(f"bench: profile {sec}: " + ", ".join(f"{n} {rec[n]['drop_m']} m/{rec[n]['length_smooth_m']} m"
                                                    for n in by))
    out["_read"] = {"seconds": round(t_read, 1), "cells": int(cells_read)}
    return out


def corridor_cost(variant: str = "l5min", block: float = 512.0) -> dict:
    """Share of the tile's 1 m COG blocks (``block`` m, on a grid aligned to the box) that a second, extraction-time
    read along the channels would fetch: blocks touched by a corridor of +-``buf`` m around the reaches of at least
    ``min_km2``."""
    import geopandas as gpd
    import shapely
    from rasterio.features import rasterize
    from rasterio.transform import from_origin

    g = gpd.read_file(out_dir() / "variants" / variant / "segments.gpkg")
    box = _tile().core
    n = int(math.ceil((box[2] - box[0]) / block))
    tf = from_origin(box[0], box[3], block, block)
    land, _ = read(rasters() / "dem5_mean.tif", box)
    f = int(round(block / 5))
    hh = (land.shape[0] // f) * f
    lb = np.isfinite(land[:hh, :hh]).reshape(hh // f, f, hh // f, f).any(axis=(1, 3))
    out = {"blocks_land": int(lb.sum())}
    for min_km2 in (0.5, 2.0, 10.0):
        lines = g.geometry.values[g["area_km2"].to_numpy() >= min_km2]
        km = float(sum(ln.length for ln in lines) / 1000)
        for buf in (50.0, 150.0, 500.0):
            poly = shapely.union_all(shapely.buffer(lines, buf, quad_segs=2))
            m = rasterize([(poly, 1)], out_shape=(n, n), transform=tf, all_touched=True, dtype="uint8") > 0
            m = m[:lb.shape[0], :lb.shape[1]] & lb
            area_share = float(shapely.area(shapely.clip_by_rect(poly, *box)) / ((box[2] - box[0]) ** 2))
            out[f"ge{min_km2:g}_buf{buf:g}"] = {"km": round(km), "block_share": round(float(m.sum() / lb.sum()), 3),
                                               "area_share": round(area_share, 3)}
    return out


def linz_quality() -> dict:
    """The LINZ river lines themselves against the 1 m channel (sub-area): named LINZ lines sampled every 10 m, the
    nearest 1 m reach of >= 2 km2 (``s1``) and the height of the LINZ point above that channel point."""
    import geopandas as gpd
    from scipy.spatial import cKDTree

    lz = linz_lines(SUB)
    lz = lz[lz["name"].fillna("").str.len() > 0].reset_index(drop=True)
    ref = gpd.read_file(out_dir() / "variants" / "s1" / "segments.gpkg")
    ref = ref[ref["area_km2"] >= 2].reset_index(drop=True)
    rxy, rown = _samples(ref, 1.0)
    ra = ref["area_mid_km2"].to_numpy()[rown]
    z1, tf1 = read(rasters() / "s1_dem.tif")
    xy, _ = _samples(lz, 10.0)
    xy = xy[_in(xy, SUB)]
    dd, ii = cKDTree(rxy).query(xy, distance_upper_bound=1000)
    ok = np.isfinite(dd)
    dz = _sample_raster(z1, tf1, xy[ok]) - _sample_raster(z1, tf1, rxy[ii[ok]])
    a = ra[ii[ok]]
    out = {"samples": int(len(xy)), "within_1km": int(ok.sum())}
    for lo, hi in ((2, 10), (10, 100), (100, 1e9), (2, 1e9)):
        m = (a >= lo) & (a < hi)
        out[f"{lo:g}-{hi:g}"] = {"dist": _q(dd[ok][m]), "dz": _q(dz[m]),
                                 "share_gt20m": round(float((dd[ok][m] > 20).mean()), 3) if m.any() else None,
                                 "dz_gt1m": round(float((dz[m] > 1).mean()), 3) if m.any() else None}
    return out


def run_eval(names: list[str] | None = None, log=print) -> dict:
    names = [n for n in (names or list(VARIANTS)) if (out_dir() / "variants" / n / "segments.gpkg").exists()]
    res = {"variants": names, "route": {n: json.loads((out_dir() / "variants" / n / "route.json").read_text())
                                        for n in names},
           "rec2": rec2_summary(names), "flats": flats_summary(names), "offsets": offsets(names, log),
           "profiles": profiles(names, log), "corridor": corridor_cost() if "l5min" in names else None,
           "linz_quality": linz_quality() if "s1" in names else None}
    (out_dir() / "eval.json").write_text(json.dumps(res, indent=1, default=str))
    return res


# ---------------------------------------------------------------------------------------------- tiling
def _stitch(g, core_of: dict, tol: float = 8.0) -> np.ndarray:
    """Downstream links across sub-tile edges: a core segment whose downstream neighbour is not kept (it left the
    core) links to the segment of another sub-tile whose line passes within ``tol`` m of its end point and whose
    area is similar. Returns the down index (-1 none) over ``g`` (rows of all sub-tiles)."""
    import shapely
    from scipy.spatial import cKDTree

    down = g["down_row"].to_numpy().copy()
    xy, own = shapely.get_coordinates(g.geometry.values, return_index=True)
    tree = cKDTree(xy)
    sub = g["sub"].to_numpy()
    area = g["area_km2"].to_numpy()
    ends = np.array([ln.coords[-1] for ln in g.geometry.values])
    for i in np.flatnonzero(g["exits"].to_numpy()):
        best, bd = -1, np.inf
        for k in tree.query_ball_point(ends[i], tol):
            j = own[k]
            if sub[j] == sub[i]:
                continue
            ratio = area[j] / max(area[i], 1e-6)
            if not 0.7 <= ratio <= 3.0:
                continue
            dd = math.dist(xy[k], ends[i]) + abs(math.log(ratio)) * 10
            if dd < bd:
                best, bd = j, dd
        down[i] = best
    return down


def _net_accumulate(local: np.ndarray, down: np.ndarray) -> np.ndarray:
    n = len(local)
    indeg = np.zeros(n, int)
    for j in down[down >= 0]:
        indeg[j] += 1
    tot = local.astype(float).copy()
    q = list(np.flatnonzero(indeg == 0))
    k = 0
    while k < len(q):
        i = q[k]
        k += 1
        j = down[i]
        if j >= 0:
            tot[j] += tot[i]
            indeg[j] -= 1
            if indeg[j] == 0:
                q.append(j)
    if k != n:
        raise RuntimeError("cycle in the stitched network")
    return tot


def run_tiling(log=print, halo: float = 3000.0, sub_tile: float = 32_000.0, variant: str = "l5min") -> dict:
    """The 5 m tile routed as 2 x 2 sub-tiles (``sub_tile`` cores, ``halo``) and compared with the whole tile:

    - *inflow* areas: each sub-tile with REC2 inflows and border outlets, as ``nz.route_tile`` does today;
    - *network* areas: each sub-tile counts the cells of its box (core and halo) whose first reach is one of its
      core reaches (each cell then counts once: in the overlap both sub-tiles route it the same way), reaches are
      stitched across the core edges, and areas are accumulated down the stitched network (no REC2).
    """
    import geopandas as gpd
    from rasterio.transform import from_origin
    from scipy.spatial import cKDTree

    from .nz import border_outlets, inflow_points

    cell = float(VARIANTS[variant][1])
    core = _tile().core
    zt, tft, _ = prepare_dem(variant)      # the same DEM as the whole-tile run (lake fill, 8 m fallback)
    near = rec2_near(_tile().box)
    meter = Meter()
    # the whole tile again, with the same local-area rule (cells whose first reach is in the core), as the reference
    # for the network areas: halo cells that drain to halo reaches belong to the neighbour tiles in both
    with meter.step("full"):
        z = zt.copy()
        d = P.route_lean(z, cell, border_seed=border_outlets(near, _tile().box, tft, z.shape,
                                                             half_cells=max(1, round(96 / cell))))
        acc, fl = P.accumulate_lean(d, cell)
        P.inject(d, acc, fl, tft, inflow_points(near, _tile().box), radius_cells=max(1, round(48 / cell)))
        full = P.segments(d, acc, z, tft, MIN_AREA_KM2, SEG_LEN_M, trunc=fl)
        floc, _ = P.local_areas(d, full, cell)
        del z, d, acc, fl
    fmid = full.geometry.interpolate(0.5, normalized=True)
    fkeep = ((fmid.x >= core[0]) & (fmid.x < core[2]) & (fmid.y >= core[1]) & (fmid.y < core[3])).to_numpy()
    fds = full["down_seg"].to_numpy()
    fdown = np.where((fds >= 0) & fkeep[np.maximum(fds, 0)], fds, -1)
    fdown[~fkeep] = -1
    full["area_net_km2"] = _net_accumulate(np.where(fkeep, floc, 0.0), fdown)
    full = full.drop(columns=["cells"])
    parts = []
    n = int(round((core[2] - core[0]) / sub_tile))
    offset = 0
    for i in range(n):
        for j in range(n):
            c = (core[0] + i * sub_tile, core[1] + j * sub_tile, core[0] + (i + 1) * sub_tile,
                 core[1] + (j + 1) * sub_tile)
            b = (c[0] - halo, c[1] - halo, c[2] + halo, c[3] + halo)
            r0, c0 = int(round((tft.f - b[3]) / cell)), int(round((b[0] - tft.c) / cell))
            h, w = int(round((b[3] - b[1]) / cell)), int(round((b[2] - b[0]) / cell))
            z = np.ascontiguousarray(zt[r0:r0 + h, c0:c0 + w], np.float32).copy()
            tf = from_origin(b[0], b[3], cell, cell)
            with meter.step(f"sub_{i}{j}"):
                seed = border_outlets(near, b, tf, z.shape, half_cells=max(1, round(96 / cell)))
                d = P.route_lean(z, cell, border_seed=seed if seed.any() else None)
                acc, fl = P.accumulate_lean(d, cell)
                used = P.inject(d, acc, fl, tf, inflow_points(near, b), radius_cells=max(1, round(48 / cell)))
                g = P.segments(d, acc, z, tf, MIN_AREA_KM2, SEG_LEN_M, trunc=fl)
                loc, lost = P.local_areas(d, g, cell)
            g["local_km2"] = loc
            mid = g.geometry.interpolate(0.5, normalized=True)
            keep = ((mid.x >= c[0]) & (mid.x < c[2]) & (mid.y >= c[1]) & (mid.y < c[3])).to_numpy()
            ds = g["down_seg"].to_numpy()
            newpos = np.full(len(g), -1)
            newpos[keep] = offset + np.arange(keep.sum())
            g["down_row"] = np.where(ds >= 0, newpos[np.maximum(ds, 0)], -1)
            g["exits"] = (ds < 0) | ~keep[np.maximum(ds, 0)]
            g.loc[g["exits"], "down_row"] = -1
            g = g[keep].drop(columns=["cells"]).assign(sub=f"{i}{j}", inflows=len(used), lost_km2=lost)
            offset += len(g)
            parts.append(g)
            del z, d, acc, fl
    meter.close()
    sub = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), geometry="geometry", crs="EPSG:2193")
    sub["down_net"] = _stitch(sub, {})
    sub["area_net_km2"] = _net_accumulate(sub["local_km2"].to_numpy(), sub["down_net"].to_numpy())
    sub.to_file(out_dir() / "tiling_segments.gpkg", layer="segments", driver="GPKG")
    # whole-tile reaches (core) matched to sub-tile reaches by line proximity
    fm = full.geometry.interpolate(0.5, normalized=True)
    fxy = np.column_stack([fm.x, fm.y])
    infull = _in(fxy, core)
    sxy, sown = _samples(sub, 5.0)
    tree = cKDTree(sxy)
    dd, ii = tree.query(fxy[infull], k=8, distance_upper_bound=15.0)
    fa = full["area_km2"].to_numpy()[infull]
    cand = {"inflow": sub["area_km2"].to_numpy(), "network": sub["area_net_km2"].to_numpy()}
    mid_x, mid_y = core[0] + sub_tile, core[1] + sub_tile
    dist_edge = np.minimum(np.abs(fxy[infull, 0] - mid_x), np.abs(fxy[infull, 1] - mid_y))
    rec = {"variant": variant, "sub_tile_m": sub_tile, "halo_m": halo, "steps": meter.steps,
           "full_reaches_core": int(infull.sum()), "sub_reaches": int(len(sub)),
           "exits": int(sub["exits"].sum()), "stitched": int((sub["exits"] & (sub["down_net"] >= 0)).sum()),
           "lost_km2_per_sub": [round(float(x), 2) for x in sub.groupby("sub")["lost_km2"].first()]}
    for how, sa in cand.items():
        # network areas against the whole tile under the same rule; inflow areas against the whole-tile run
        ref = full["area_net_km2"].to_numpy()[infull] if how == "network" else fa
        best = np.full(len(fa), np.nan)
        for k in range(8):
            ok = np.isfinite(dd[:, k]) & np.isnan(best)
            jj = sown[np.minimum(ii[:, k], len(sown) - 1)]
            ratio = sa[jj] / np.maximum(ref, 1e-9)
            ok &= (ratio > 0.3) & (ratio < 3.0)
            best[ok] = sa[jj[ok]]
        err = np.abs(best - ref) / np.maximum(ref, 1e-9)
        r = {}
        base = np.ones(len(fa), bool)
        r["reaches_compared"] = int(base.sum())
        for lo in (2.0, 10.0, 100.0):
            m = (fa >= lo) & base
            ne = m & (dist_edge < 2000)
            r[f"ge{lo:g}km2"] = {"n": int(m.sum()), "unmatched": round(float(np.isnan(best[m]).mean()), 4),
                                 "err_gt1pct": round(float((err[m] > 0.01).mean()), 4),
                                 "err_gt5pct": round(float((err[m] > 0.05).mean()), 4),
                                 "near_edge_n": int(ne.sum()),
                                 "near_edge_err_gt5pct": round(float((err[ne] > 0.05).mean()), 4) if ne.any()
                                 else None,
                                 "max_err": round(float(np.nanmax(err[m])), 3) if m.any() else None}
        rec[how] = r
    rec["truncated_core"] = {"full": int(full["truncated"].to_numpy()[infull].sum()),
                             "sub_inflow": int(sub["truncated"].sum())}
    (out_dir() / "tiling.json").write_text(json.dumps(rec, indent=1))
    log(f"bench: tiling {json.dumps(rec)}")
    return rec


# ---------------------------------------------------------------------------------------------- seam and lake
SEAM_GAP = (1_440_000, 5_195_000, 1_452_000, 5_215_000)  # pretend gap: Whitcombe / upper Hokitika headwaters
SEAM_BOX = (1_430_000, 5_185_000, 1_459_000, 5_225_000)


SEAM_RULES = {"hard": None, "smooth150": dict(sigma_m=150.0, work_m=25.0),
              "smooth50_5m": dict(sigma_m=50.0, work_m=5.0), "edge25_5m": dict(sigma_m=150.0, work_m=5.0, edge_m=25.0)}


def run_seam(log=print) -> dict:
    """Pretend ``SEAM_GAP`` has no LiDAR: fill it from the 8 m contour DEM by a hard switch or one of the seam
    corrections (``SEAM_RULES``), route at 5 m and compare with the true LiDAR on the same box: the elevation step
    across the seam (merged minus true difference between the two cells either side), depressions the seam makes,
    and the reaches crossing it."""
    import rasterio
    from rasterio.warp import Resampling, reproject

    zt, tf = read(rasters() / "dem5_min.tif", SEAM_BOX)
    zt = np.ascontiguousarray(zt, np.float32)
    z8 = np.full(zt.shape, np.nan, np.float32)
    with rasterio.open(rasters() / "dem8_contour.tif") as ds:
        reproject(rasterio.band(ds, 1), z8, dst_transform=tf, dst_crs="EPSG:2193", resampling=Resampling.bilinear,
                  src_nodata=ds.nodata, dst_nodata=np.nan)
    r0, c0 = int(round((tf.f - SEAM_GAP[3]) / 5)), int(round((SEAM_GAP[0] - tf.c) / 5))
    r1, c1 = int(round((tf.f - SEAM_GAP[1]) / 5)), int(round((SEAM_GAP[2] - tf.c) / 5))
    gap = np.zeros(zt.shape, bool)
    gap[r0:r1, c0:c1] = True
    fine = zt.copy()
    fine[gap] = np.nan
    near = rec2_near(SEAM_BOX)
    # cell pairs across the seam: (inside, outside) on the four sides of the rectangle
    pairs = [((slice(r0, r1), c0), (slice(r0, r1), c0 - 1)), ((slice(r0, r1), c1 - 1), (slice(r0, r1), c1)),
             ((r0, slice(c0, c1)), (r0 - 1, slice(c0, c1))), ((r1 - 1, slice(c0, c1)), (r1, slice(c0, c1)))]
    ring = np.zeros(zt.shape, bool)
    ring[r0 - 2:r1 + 2, c0 - 2:c1 + 2] = True
    ring[r0 + 2:r1 - 2, c0 + 2:c1 - 2] = False
    true = route_box(zt.copy(), tf, near, SEAM_BOX)
    chan = true["acc"] >= 2e6
    out = {"gap": list(SEAM_GAP), "box": list(SEAM_BOX),
           "d_seam_median_m": round(float(np.nanmedian((z8 - zt)[ring])), 2),
           "d_seam_abs_p90_m": round(float(np.nanpercentile(np.abs(z8 - zt)[ring], 90)), 2),
           "lidar": {"fill_cells_gt0p5m_ring": int((true["fill"][ring] > 0.5).sum()),
                     "reaches": len(true["seg"])}}
    segs = {"lidar": true["seg"]}
    for name, kw in SEAM_RULES.items():
        t = time.time()
        if kw is None:
            z = np.where(gap, z8, zt).astype(np.float32)
            st = {}
        else:
            z, st = P.seam_correct(fine, z8, 5.0, **kw)
        dt = time.time() - t
        steps, steps_ch = [], []
        for a, b in pairs:
            dd = np.abs((z[a] - z[b]) - (zt[a] - zt[b]))
            steps.append(dd)
            steps_ch.append(dd[chan[a] | chan[b]])
        steps, steps_ch = np.concatenate(steps), np.concatenate(steps_ch)
        r = route_box(np.ascontiguousarray(z, np.float32), tf, near, SEAM_BOX)
        g = r["seg"]
        cross = np.array([bool(ring.ravel()[c].any()) for c in g["cells"]])
        big = cross & (g["area_km2"].to_numpy() >= 2)
        rec = {"seconds": round(dt, 1), "stats": st,
               "seam_step_m": {"p50": round(float(np.median(steps)), 2),
                               "p90": round(float(np.percentile(steps, 90)), 2),
                               "share_gt1m": round(float((steps > 1).mean()), 3)},
               "seam_step_channels_m": {"p50": round(float(np.median(steps_ch)), 2),
                                        "p90": round(float(np.percentile(steps_ch, 90)), 2), "n": int(len(steps_ch))},
               "fill_cells_gt0p5m_ring": int((r["fill"][ring] > 0.5).sum()),
               "crossing_reaches_ge2": int(big.sum()),
               "crossing_steep50_median": round(float(np.median(g["steepest_50m_m_per_km"][big])), 1) if big.any()
               else None, "reaches": len(g)}
        out[name] = rec
        segs[name] = g
        log(f"bench: seam {name}: {rec}")
    import geopandas as gpd

    tg = true["seg"]
    tb = np.array([bool(ring.ravel()[c].any()) for c in tg["cells"]]) & (tg["area_km2"].to_numpy() >= 2)
    out["lidar"]["crossing_reaches_ge2"] = int(tb.sum())
    out["lidar"]["crossing_steep50_median"] = round(float(np.median(tg["steepest_50m_m_per_km"][tb])), 1)
    gpd.GeoDataFrame(pd.concat([v.drop(columns=["cells"]).assign(run=k) for k, v in segs.items()], ignore_index=True),
                     geometry="geometry", crs="EPSG:2193").to_file(out_dir() / "seam_segments.gpkg", driver="GPKG")
    (out_dir() / "seam.json").write_text(json.dumps(out, indent=1, default=str))
    return out


def route_box(z: np.ndarray, tf, near, box, cell: float = 5.0) -> dict:
    """Route a DEM over a box as a national tile is routed (REC2 border outlets and inflows); filled-minus-raw depth,
    accumulation and the segments with their cells."""
    from .nz import border_outlets, inflow_points

    zin = z.copy()
    seed = border_outlets(near, box, tf, z.shape, half_cells=max(1, round(96 / cell)))
    d = P.route_lean(z, cell, border_seed=seed if seed.any() else None)
    acc, fl = P.accumulate_lean(d, cell)
    P.inject(d, acc, fl, tf, inflow_points(near, box), radius_cells=max(1, round(48 / cell)))
    g = P.segments(d, acc, z, tf, MIN_AREA_KM2, SEG_LEN_M, trunc=fl)
    return {"fill": z - zin, "acc": acc, "seg": g}


def _leave_points(d: np.ndarray, zf: np.ndarray, cells: np.ndarray, level: float, tol: float = 0.01) -> np.ndarray:
    """For each of ``cells``, the first cell downstream whose filled elevation is below ``level - tol`` (where the
    flow leaves the lake), or -1 when it reaches an outlet first."""
    nc = d.shape[1]
    off = P.offsets(nc)
    df, zz = d.ravel(), zf.ravel()
    memo: dict[int, int] = {}
    out = np.empty(len(cells), np.int64)
    for k, c in enumerate(cells):
        path, v = [], int(c)
        while True:
            if v in memo:
                res = memo[v]
                break
            if zz[v] < level - tol:
                res = v
                break
            if df[v] >= 8:
                res = -1
                break
            path.append(v)
            v = v + int(off[df[v]])
        for u in path:
            memo[u] = res
        out[k] = res
    return out


def lake_runs(z: np.ndarray, fs: np.ndarray, z8: np.ndarray, tf) -> dict:
    """The largest no-data lake hole of ``z`` (5 m; ``fs`` flattened share; ``z8`` the 8 m contour DEM on the same
    grid) filled two ways, flat at the rim's LiDAR water level (``fill_lakes``) and from the 8 m DEM, then routed:
    where the lake drains, how much of it reaches the outlet, and the steps each fill leaves at the shore."""
    from pyproj import Transformer
    from scipy import ndimage

    to_ll = Transformer.from_crs(2193, 4326, always_xy=True)
    land8 = np.isfinite(z8)
    hole = np.isnan(z)
    lab, _ = ndimage.label(hole, structure=np.ones((3, 3), bool))
    sizes = np.bincount(lab.ravel(), weights=land8.ravel().astype(float))   # land (8 m) cells per hole: not the sea
    sizes[0] = 0
    lake = lab == int(np.argmax(sizes))
    res = {"hole_km2": round(lake.sum() * 25e-6, 2), "z8_in_hole_median": round(float(np.nanmedian(z8[lake])), 2)}
    z_l = z.copy()
    recs = P.fill_lakes(z_l, fs, land=land8)
    res["fill"] = max(recs, key=lambda r: r["cells"] * (r.get("land_share", 1) >= 0.5))
    z_8 = z.copy()
    z_8[lake] = z8[lake]
    # the whole lake: the hole and the hydro-flattened water connected to it
    wl, _ = ndimage.label(lake | (fs >= 0.5), structure=np.ones((3, 3), bool))
    region = wl == np.bincount(wl[lake]).argmax()
    res["lake_km2"] = round(region.sum() * 25e-6, 2)
    runs = {}
    for name, zz in (("lidar_level", z_l), ("fill_8m", z_8)):
        zz = np.ascontiguousarray(zz, np.float32)
        other = np.isnan(zz) & land8
        zz[other] = z8[other]                                     # other land gaps (no seam correction here)
        zin = zz.copy()
        d = P.route_lean(zz, 5.0)
        acc, _ = P.accumulate_lean(d, 5.0)
        a = np.where(lake, acc, 0)
        j = int(np.argmax(a))
        r, c = divmod(j, z.shape[1])
        off = P.offsets(z.shape[1])
        dfl = d.ravel()
        idx = np.flatnonzero(lake.ravel())
        nxt = idx + off[np.minimum(dfl[idx], 7)]
        leaving = idx[(dfl[idx] < 8) & ~lake.ravel()[np.clip(nxt, 0, lake.size - 1)]]
        lon, lat = to_ll.transform(tf.c + (c + .5) * 5, tf.f - (r + .5) * 5)
        ridx = np.flatnonzero(region.ravel())
        rn = ridx + off[np.minimum(dfl[ridx], 7)]
        ex = ridx[(dfl[ridx] < 8) & ~region.ravel()[np.clip(rn, 0, region.size - 1)]]
        ex_acc = acc.ravel()[ex]
        jo = ex[int(np.argmax(ex_acc))] if len(ex) else j
        olon, olat = to_ll.transform(tf.c + (jo % z.shape[1] + .5) * 5, tf.f - (jo // z.shape[1] + .5) * 5)
        gy, gx = np.gradient(np.where(np.isfinite(zin), zin, 0.0), 5.0)
        step = np.hypot(gx, gy) * 5.0
        shore = ndimage.binary_dilation(lake, iterations=3) & ~ndimage.binary_erosion(lake, iterations=3)
        shore &= np.isfinite(zin) & ndimage.binary_erosion(np.isfinite(zin), iterations=2)
        lev = float(res["fill"].get("level", np.nanmedian(zin[lake])))   # the LiDAR water level, both runs
        lp = _leave_points(d, zz, ridx, lev, tol=0.25)
        u, cnt = np.unique(lp, return_counts=True)
        top = u[int(np.argmax(cnt))]
        ur, uc = np.divmod(np.maximum(u, 0), z.shape[1])
        same = (u >= 0) & (np.hypot(ur - top // z.shape[1], uc - top % z.shape[1]) * 5 <= 100)   # one outlet: 100 m
        tl = to_ll.transform(tf.c + (top % z.shape[1] + .5) * 5, tf.f - (top // z.shape[1] + .5) * 5) if top >= 0 \
            else (np.nan, np.nan)
        runs[name] = {"lake_level": round(float(np.nanmedian(zin[lake])), 2),
                      "leaves_lake_at": [round(tl[0], 4), round(tl[1], 4)],
                      "lake_share_leaving_there": round(float(cnt[same].sum() / len(lp)), 4),
                      "leave_points": int(len(u)),
                      "leave_area_km2": round(float(acc.ravel()[u[same]].sum() / 1e6), 1) if top >= 0 else None,
                      "lake_outlet_lonlat": [round(olon, 4), round(olat, 4)],
                      "lake_outlet_area_km2": round(float(acc.ravel()[jo] / 1e6), 1),
                      "lake_exits_gt1km2": int((ex_acc > 1e6).sum()),
                      "hole_max_lonlat": [round(lon, 4), round(lat, 4)],
                      "hole_max_area_km2": round(float(acc.ravel()[j] / 1e6), 1),
                      "lake_cells_draining_out": int(len(leaving)),
                      "lake_share_reaching_outlet": round(float(min(1.0, acc.ravel()[j] / 25.0 / lake.sum())), 3),
                      "shore_cells_step_gt0p5m": int((step[shore] > 0.5).sum()),
                      "fill_depth_gt0p5m_km2": round(float(((zz - zin) > 0.5).sum() * 25e-6), 3)}
        del d, acc, zz, zin
    res["runs"] = runs
    return res


def lake_test(log=print) -> dict:
    """The benchmark tile's own lake hole (the largest no-data hole over 8 m land: Lake Ianthe's interior)."""
    import rasterio
    from rasterio.transform import from_origin
    from rasterio.warp import Resampling, reproject
    from scipy import ndimage

    zt, tf = read(rasters() / "dem5_min.tif")
    z8 = np.full(zt.shape, np.nan, np.float32)
    with rasterio.open(rasters() / "dem8_contour.tif") as ds:
        reproject(rasterio.band(ds, 1), z8, dst_transform=tf, dst_crs="EPSG:2193", resampling=Resampling.bilinear,
                  src_nodata=ds.nodata, dst_nodata=np.nan)
    lab, _ = ndimage.label(np.isnan(zt), structure=np.ones((3, 3), bool))
    sizes = np.bincount(lab.ravel(), weights=np.isfinite(z8).ravel().astype(float))
    sizes[0] = 0
    sl = ndimage.find_objects(lab)[int(np.argmax(sizes)) - 1]
    pad = 1200                                                      # 6 km around the hole
    sl = (slice(max(0, sl[0].start - pad), sl[0].stop + pad), slice(max(0, sl[1].start - pad), sl[1].stop + pad))
    fs, _ = read(rasters() / "flat5.tif")
    tfw = from_origin(tf.c + sl[1].start * 5, tf.f - sl[0].start * 5, 5, 5)
    res = lake_runs(np.ascontiguousarray(zt[sl]), (fs[sl] / 100).astype(np.float32), np.ascontiguousarray(z8[sl]), tfw)
    res["window"] = [tfw.c, tfw.f - (sl[0].stop - sl[0].start) * 5, tfw.c + (sl[1].stop - sl[1].start) * 5, tfw.f]
    (out_dir() / "lake.json").write_text(json.dumps(res, indent=1, default=str))
    log(f"bench: lake {json.dumps(res, default=str)}")
    return res


ROTORUA_BOX = (1_876_000, 5_770_000, 1_896_000, 5_792_000)  # Lake Rotorua (tile x12y16): a no-data lake interior


def rotorua_test(log=print) -> dict:
    """A big real lake hole: Lake Rotorua's interior is no data in the mosaic. The box is streamed at 1 m into 5 m
    and the hole filled and routed as in ``lake_runs`` (outlet: the Ohau Channel to Lake Rotoiti)."""
    import rasterio
    from pyproj import Transformer
    from rasterio.transform import from_origin
    from rasterio.warp import Resampling, reproject

    from .nz import DEM_8M
    from .stac import mosaic

    box = ROTORUA_BOX
    H, W = box[3] - box[1], box[2] - box[0]
    a5 = P.Agg((H, W), 5)
    sheets = P.mosaic_sheets(box, REPO_ROOT / "data" / "_stac")
    t = time.time()
    st = P.stream(P.sheet_windows(sheets, box, 2048), lambda z, br, bc: a5.add(z, br, bc, P.water_flags(z)),
                  threads=32)
    z = np.ascontiguousarray(a5.minimum(), np.float32)
    fs = a5.share("flat")
    a5 = None
    tf = from_origin(box[0], box[3], 5, 5)
    p8 = rasters() / "rotorua_dem8_contour.tif"
    if not p8.exists():
        bb = Transformer.from_crs(2193, 4326, always_xy=True).transform_bounds(*box, densify_pts=21)
        mosaic(DEM_8M, bb, box, 8.0, p8, REPO_ROOT / "data" / "_stac")
    z8 = np.full(z.shape, np.nan, np.float32)
    with rasterio.open(p8) as ds:
        reproject(rasterio.band(ds, 1), z8, dst_transform=tf, dst_crs="EPSG:2193", resampling=Resampling.bilinear,
                  src_nodata=ds.nodata, dst_nodata=np.nan)
    res = {"box": list(box), "stream": st, "read_s": round(time.time() - t, 1)} | lake_runs(z, fs, z8, tf)
    (out_dir() / "rotorua.json").write_text(json.dumps(res, indent=1, default=str))
    log(f"bench: rotorua {json.dumps(res, default=str)}")
    return res


# ---------------------------------------------------------------------------------------------- report
LABEL = {"c8": "8 m contour (today)", "l8": "LiDAR 8 m mean", "l5mean": "LiDAR 5 m mean", "l5min": "LiDAR 5 m min",
         "l5min_b1": "5 m min + LINZ burn 1 m", "l5min_b3": "5 m min + LINZ burn 3 m", "s5min": "LiDAR 5 m min (sub)",
         "s2mean": "LiDAR 2 m mean (sub)", "s2min": "LiDAR 2 m min (sub)", "s1": "LiDAR 1 m (sub)",
         "national_8m": "national 8 m reaches", "linz": "LINZ lines"}


def _md(rows: list[list], head: list[str]) -> str:
    out = ["| " + " | ".join(head) + " |", "|" + "|".join("---" if i == 0 else "---:" for i in range(len(head))) + "|"]
    out += ["| " + " | ".join("" if v is None else str(v) for v in r) + " |" for r in rows]
    return "\n".join(out)


def _pq(d: dict | None) -> str:
    return f"{d['p50']:g} / {d['p90']:g}" if d and d.get("n") else "-"


def report() -> str:
    """Markdown tables of the benchmark (``tables.md``) from the step outputs."""
    D = out_dir()
    E = json.loads((D / "eval.json").read_text())
    names = E["variants"]
    parts = []
    st = json.loads((D / "stream.json").read_text())
    sp = json.loads((D / "speed.json").read_text()) if (D / "speed.json").exists() else []
    parts.append("### Reading the 1 m mosaic\n\n" + _md(
        [[f"{r['threads']} threads x {r['win']}", f"{r['cells'] / 1e6:.0f}", r["seconds"], r["mcells_s"]] for r in sp]
        + [[f"whole tile box, {st['stream']['threads']} threads x 2048", f"{st['stream']['cells'] / 1e6:.0f}",
            st["stream"]["seconds"], st["stream"]["mcells_s"]]], ["read", "M cells", "s", "M cells/s"]))
    rows = []
    for n in names:
        r = E["route"][n]
        cells = r["dem"]["cells"]
        rows.append([LABEL[n], r["cell_m"], f"{cells / 1e6:.0f}", r["routing"]["flood_s"], r["routing"]["d8_s"],
                     r["routing"]["flats_s"], r["seconds"], round(r["seconds"] / cells * 1e6, 2), r["peak_gb"],
                     round(r["peak_gb"] * 1e9 / cells, 1), r["segments"]])
    parts.append("### Routing cost\n\n" + _md(rows, ["variant", "cell m", "M cells", "flood s", "D8 s", "flats s",
                                                    "total s", "us / cell", "peak GB", "bytes / cell", "reaches"]))
    o = E["offsets"]
    rows = []
    for n in names + ["national_8m"]:
        r = o.get(n, {})
        t1 = r.get("to_1m", {})
        dz = r.get("dz_above_1m", {})
        rows.append([LABEL[n], _pq(t1.get("all>=2")), _pq(t1.get("100-1e+09")), _pq(dz.get("all>=2")),
                     r.get("dz_gt1m_share_ge2"), _pq(r.get("linz_sub", {}).get("all>=2")),
                     _pq(r.get("linz_sub", {}).get("100-1e+09")), r.get("linz_sub_share_gt20m_ge2"),
                     _pq(r.get("linz_core", {}).get("all>=2")), r.get("linz_core_share_gt20m_ge2")])
    parts.append("### Channel position (reaches >= 2 km2; p50 / p90 in m)\n\n" + _md(
        rows, ["variant", "to 1 m channel (sub)", "to 1 m, >= 100 km2", "height above 1 m channel",
               "share > 1 m above", "to LINZ line (sub)", "to LINZ, >= 100 km2", "share > 20 m from LINZ",
               "to LINZ (core)", "share > 20 m (core)"]))
    lq = E.get("linz_quality")
    if lq:
        parts.append("### The LINZ lines against the 1 m channel (named lines, sub-area)\n\n" + _md(
            [[k, _pq(v["dist"]), v["share_gt20m"], _pq(v["dz"]), v["dz_gt1m"]] for k, v in lq.items()
             if isinstance(v, dict)], ["1 m channel km2", "distance p50 / p90", "share > 20 m", "height p50 / p90",
                                       "share > 1 m above"]))
    rows = []
    for n in names + ["national_8m", "linz"]:
        g = o.get(n, {}).get("guide_points")
        if g:
            rows.append([LABEL[n], g["n_points"], _pq(g), g["share_le25m"]])
    parts.append("### Guide put-ins / take-outs: distance to the nearest reach >= 1 km2\n\n" + _md(
        rows, ["variant", "points", "p50 / p90 m", "share <= 25 m"]))
    rows = []
    for n in names:
        r = E["rec2"].get(n, {})
        c, sb = r.get("core_ge5", {}), r.get("sub_ge5", {})
        rows.append([LABEL[n], c.get("n"), c.get("median_abs_err"), c.get("within_10pct"),
                     c.get("within_10pct_tolerant"), sb.get("n"), sb.get("within_10pct"),
                     sb.get("within_10pct_tolerant"), r.get("sub_ge20", {}).get("within_10pct")])
    parts.append("### Catchment area vs REC2 (reaches >= 5 km2)\n\n" + _md(
        rows, ["variant", "core n", "median abs err", "within 10 %", "within 10 % tolerant", "sub n", "within 10 %",
               "tolerant", "sub >= 20 km2 within 10 %"]))
    rows = []
    for n in names + ["national_8m"]:
        f = E["flats"].get(n, {})
        rows.append([LABEL[n], f.get("core", {}).get("km_ge2"), f.get("core", {}).get("zero_share"),
                     f.get("core", {}).get("zero_km_in_stretches_gt1km"), f.get("sub", {}).get("zero_share"),
                     f.get("sub", {}).get("zero_km_in_stretches_gt1km")])
    parts.append("### Zero-gradient reaches (drop < 1 cm, reaches >= 2 km2)\n\n" + _md(
        rows, ["variant", "core km", "core zero share", "core km in zero stretches > 1 km", "sub zero share",
               "sub km > 1 km"]))
    P_ = E["profiles"]
    rows, rows2 = [], []
    for sec, r in P_.items():
        if sec.startswith("_"):
            continue
        ok = {n: m for n, m in r.items() if n != "in_sub" and m.get("same_end", True)}
        if "s1" in ok:
            for n in ("s1", "s2min", "s2mean", "s5min", "l5min", "l8", "c8"):
                if n in ok:
                    m = ok[n]
                    rows.append([sec, LABEL[n], m["length_smooth_m"], m["drop_m"], m["grad_m_per_km"],
                                 m["steep100_rmin"], m["steep200_rmin"], m["steep50_raw"],
                                 m.get("steep50_own"), m.get("longest_flat_own_m"), m["flat_share"],
                                 m["planar_share"]])
        if "l5min" in ok and "c8" in ok:
            a, b = ok["c8"], ok["l5min"]
            rows2.append([sec, b["area_end_km2"], b["length_smooth_m"], b["drop_m"], a["drop_own_m"], a["drop_m"],
                          b["steep200_rmin"], a["steep50_own"], b["steep50_own"], b["steep50_rmin"],
                          a["longest_flat_own_m"], b["longest_flat_own_m"], b["flat_share"], b["planar_share"]])
    parts.append("### Guide sections with the 1 m truth (sub-area): long profiles\n\nlength smoothed (m), drop on "
                 "the 1 m DEM along the routed line (lowest 1 m cell within half a routing cell + 1 m, running "
                 "minimum), steepest stretches in m/km (rmin: on the running minimum), own: on the routing DEM's "
                 "filled profile; flat / planar: share of the path on hydro-flattened / interpolated 1 m water.\n\n"
                 + _md(rows, ["section", "variant", "length", "drop", "m/km", "steep 100", "steep 200",
                              "steep 50 raw", "steep 50 own", "longest flat own m", "flat", "planar"]))
    parts.append("### All guide sections: today's 8 m profile vs the 5 m line sampled at 1 m\n\n" + _md(
        rows2, ["section", "km2", "length 5 m", "drop 1 m", "drop 8 m own", "drop 8 m line on 1 m",
                "steep 200 (5 m line, 1 m)", "steep 50 8 m own", "steep 50 5 m own", "steep 50 1 m rmin",
                "longest flat 8 m", "longest flat 5 m", "flat", "planar"]))
    if E.get("corridor"):
        c = E["corridor"]
        parts.append("### Second read along the channels: share of the core's 512 m blocks touched\n\n" + _md(
            [[k.replace("_buf", " km2, +-").replace("ge", ">= ") + " m", v["km"], v["block_share"], v["area_share"]]
             for k, v in c.items() if isinstance(v, dict)], ["corridor", "reach km", "block share", "area share"]))
    if (D / "tiling.json").exists():
        t = json.loads((D / "tiling.json").read_text())
        rows = []
        for how in ("inflow", "network"):
            for k in ("ge2km2", "ge10km2", "ge100km2"):
                v = t[how][k]
                rows.append([how, k, v["n"], v["unmatched"], v["err_gt1pct"], v["err_gt5pct"], v["near_edge_n"],
                             v["near_edge_err_gt5pct"], v["max_err"]])
        parts.append("### Tiling: 2 x 2 sub-tiles of 32 km (3 km halo) against the whole tile\n\n" + _md(
            rows, ["areas", "reaches", "n", "unmatched", "err > 1 %", "err > 5 %", "n within 2 km of an edge",
                   "err > 5 % there", "max err"]) + f"\n\nexits {t['exits']}, stitched {t['stitched']}; "
            f"reaches whole tile {t['full_reaches_core']}, sub-tiles {t['sub_reaches']}; truncated "
            f"{t['truncated_core']}")
    if (D / "seam.json").exists():
        sm = json.loads((D / "seam.json").read_text())
        rows = [[k, v["seam_step_m"]["p50"], v["seam_step_m"]["p90"], v["seam_step_m"]["share_gt1m"],
                 v["seam_step_channels_m"]["p50"], v["seam_step_channels_m"]["p90"], v["fill_cells_gt0p5m_ring"],
                 v["crossing_steep50_median"], v["seconds"]] for k, v in sm.items() if isinstance(v, dict)
                and "seam_step_m" in v]
        rows.append(["true LiDAR", 0, 0, 0, 0, 0, sm["lidar"]["fill_cells_gt0p5m_ring"],
                     sm["lidar"].get("crossing_steep50_median"), ""])
        parts.append(f"### Seam: a {SEAM_GAP} block filled from the 8 m DEM\n\n8 m minus LiDAR at the seam: median "
                     f"{sm['d_seam_median_m']} m, abs p90 {sm['d_seam_abs_p90_m']} m.\n\n" + _md(
                         rows, ["rule", "step p50 m", "step p90 m", "share > 1 m", "on channels p50", "p90",
                                "fill cells > 0.5 m at the seam", "steepest 50 m of crossing reaches (median)",
                                "s"]))
    rows = []
    for name in ("lake", "rotorua"):
        if (D / f"{name}.json").exists():
            lk = json.loads((D / f"{name}.json").read_text())
            for k, v in lk["runs"].items():
                rows.append([name, lk["hole_km2"], lk.get("lake_km2"), lk["fill"].get("level"),
                             lk["z8_in_hole_median"], k, v["leaves_lake_at"], v["lake_share_leaving_there"],
                             v["shore_cells_step_gt0p5m"]])
    if rows:
        parts.append("### Lake holes\n\n" + _md(rows, ["lake", "hole km2", "lake km2", "LiDAR level", "8 m level",
                                                       "fill", "leaves the lake at", "share of the lake there",
                                                       "shore cells with a step > 0.5 m"]))
    r5 = E["route"].get("l5min", {}).get("product_bytes")
    if r5:
        parts.append("### Per-tile products at 5 m (64 km core)\n\n" + _md(
            [[k, round(v / 1e6, 1)] for k, v in r5.items()], ["file", "MB"]))
    text = "\n\n".join(parts) + "\n"
    (D / "tables.md").write_text(text)
    return text


def run(step: str, variants: list[str] | None = None, threads: int = 32, log=print):
    if step == "stream":
        return run_stream(threads=threads, log=log)
    if step == "speed":
        r = stream_speed(log=log)
        (out_dir() / "speed.json").write_text(json.dumps(r, indent=1))
        return r
    if step == "route":
        return [run_route(n, log) for n in (variants or list(VARIANTS))]
    if step == "compare":
        return run_eval(variants, log)
    if step == "tiling":
        return run_tiling(log)
    if step == "seam":
        return {"seam": run_seam(log), "lake": lake_test(log), "rotorua": rotorua_test(log)}
    if step == "report":
        return report()
    if step == "profiles":       # recompute only the profiles of eval.json
        p = out_dir() / "eval.json"
        E = json.loads(p.read_text())
        E["profiles"] = profiles(E["variants"], log)
        p.write_text(json.dumps(E, indent=1, default=str))
        return report()
    if step == "lakes":
        return {"lake": lake_test(log), "rotorua": rotorua_test(log)}
    raise ValueError(step)
