"""National (all-NZ) processing on a grid of tiles.

The country is cut into 64 km x 64 km tiles in NZTM. Each tile is processed with a 3 km halo on
the 8 m contour DEM (Priority-Flood, D8, flat resolution, Kahn accumulation, then the reach
segments of ``reaches.py``), and only the tile's core is kept.

Rivers that enter a tile from outside get their upstream area from REC2: where a REC2 reach
crosses the box boundary inward, its catchment area at the crossing is added as a point inflow
to the channel cell just inside the box, before accumulation. So the area grid, the flood
estimate and the run finder see the full catchment of big rivers such as the Waikato, not only
the part inside the tile. Cells whose catchment touches the box edge with no REC2 inflow are
still flagged ``truncated``.

Per tile, only small products are kept (``data/nz/tiles/<id>/``): ``segments.gpkg`` (core
segments with global ids), ``area_code.tif`` (8 m, uint8 log-area code), ``hillshade_24m.tif``,
``local_catchment.tif`` (8 m, global segment id) and ``tile.json``. The DEM, filled DEM and flow
graph are deleted afterwards, which keeps a whole-country run inside ~10 GB of disk.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from pyproj import Transformer
from rasterio.windows import Window
from shapely.geometry import LineString, Point, box

from .areacode import CODE_LOG_MIN, CODE_STEP, CODE_TAG, encode_area
from .national import nz_dir
from .products import _write, hillshade
from .regions import Region
from .routing import accumulate, edge_mask, route
from .stac import mosaic, read_dem

RES = 8.0
TILE = 64_000.0
HALO = 3_000.0
X0, Y0 = 1_072_000.0, 4_736_000.0  # grid origin (NZTM); tiles cover x0 + i*TILE ...
NX, NY = 16, 23
DEM_8M = "new-zealand/new-zealand-contour/dem_8m/2193"
SEG_BASE = 1_000_000  # global segment id = tile_index * SEG_BASE + local seg_id


@dataclass(frozen=True)
class Tile:
    ix: int
    iy: int

    @property
    def id(self) -> str:
        return f"x{self.ix:02d}y{self.iy:02d}"

    @property
    def index(self) -> int:
        return self.iy * 100 + self.ix

    @property
    def core(self) -> tuple[float, float, float, float]:
        x, y = X0 + self.ix * TILE, Y0 + self.iy * TILE
        return (x, y, x + TILE, y + TILE)

    @property
    def box(self) -> tuple[float, float, float, float]:
        x0, y0, x1, y1 = self.core
        return (x0 - HALO, y0 - HALO, x1 + HALO, y1 + HALO)

    def dir(self) -> Path:
        d = nz_dir() / "tiles" / self.id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def region(self) -> Region:
        t = Transformer.from_crs("EPSG:2193", "EPSG:4326", always_xy=True)
        bb = t.transform_bounds(*self.box, densify_pts=21)
        return Region(name=f"nz_{self.id}", description="national tile", role="search", bbox_wgs84=tuple(bb),
                      dem_coarse=DEM_8M, routing_res=RES, nztm_box=self.box, root=self.dir())

    @staticmethod
    def parse(s: str) -> Tile:
        return Tile(int(s[1:3]), int(s[4:6]))


def land_tiles(rec2: gpd.GeoDataFrame) -> list[Tile]:
    """Tiles whose core holds at least one REC2 reach, north to south."""
    x = (rec2["downcoordX"].to_numpy() - X0) // TILE
    y = (rec2["downcoordY"].to_numpy() - Y0) // TILE
    keys = sorted({(int(a), int(b)) for a, b in zip(x, y, strict=True)}, key=lambda k: (-k[1], k[0]))
    return [Tile(a, b) for a, b in keys if 0 <= a < NX and 0 <= b < NY]


# --- national layers, loaded once per process ---------------------------------------------------
class Layers:
    def __init__(self):
        d = nz_dir()
        self.rec2 = gpd.read_file(d / "rec2.gpkg")
        self.flood = gpd.read_file(d / "flood.gpkg") if (d / "flood.gpkg").exists() else None
        self.geology = gpd.read_file(d / "geology.gpkg") if (d / "geology.gpkg").exists() else None

    @staticmethod
    def _clip(g: gpd.GeoDataFrame | None, bb, pad: float = 2000.0) -> gpd.GeoDataFrame | None:
        if g is None:
            return None
        x0, y0, x1, y1 = bb
        return g.cx[x0 - pad:x1 + pad, y0 - pad:y1 + pad]

    def write_tile_inputs(self, tile: Tile) -> None:
        """The per-region files ``reaches.py`` reads, clipped from the national layers."""
        d = tile.dir()
        for g, name in ((self._clip(self.rec2, tile.box), "rec2_nztm.geojson"),
                        (self._clip(self.flood, tile.box), "flood_rec1_nztm.geojson"),
                        (self._clip(self.geology, tile.box, 0.0), "geology_nztm.geojson")):
            if g is not None:
                (d / name).write_text(g.to_json(drop_id=True))


# --- inflows from REC2 ----------------------------------------------------------------------------
def inflow_points(rec2: gpd.GeoDataFrame, bb, inset_m: float = 160.0) -> list[tuple[float, float, float, int]]:
    """(x, y, area_km2, nzsegment) where REC2 reaches enter the box from outside.

    The area at the crossing is the reach's cumulative area minus the share of its own local
    catchment that lies inside the box (by length). The point is moved ``inset_m`` along the reach
    into the box so it lands on a well-defined channel.
    """
    x0, y0, x1, y1 = bb
    poly = box(*bb)
    ux, uy = rec2["upcoordX"].to_numpy(), rec2["upcoordY"].to_numpy()
    dx, dy = rec2["downcoordX"].to_numpy(), rec2["downcoordY"].to_numpy()
    up_in = (ux > x0) & (ux < x1) & (uy > y0) & (uy < y1)
    dn_in = (dx > x0) & (dx < x1) & (dy > y0) & (dy < y1)
    out = []
    for i in np.flatnonzero(dn_in & ~up_in):
        r = rec2.iloc[i]
        line = r.geometry
        if line is None or line.is_empty:
            continue
        if line.geom_type == "MultiLineString":
            line = max(line.geoms, key=lambda g: g.length)
        inside = line.intersection(poly)
        if inside.is_empty:
            continue
        parts = list(inside.geoms) if hasattr(inside, "geoms") else [inside]
        down = Point(dx[i], dy[i])
        part = min(parts, key=lambda p: p.distance(down))
        if part.geom_type != "LineString" or part.length < 1:
            continue
        a, b = Point(part.coords[0]), Point(part.coords[-1])
        if a.distance(down) < b.distance(down):  # orient from the boundary towards the down end
            part = LineString(part.coords[::-1])
        frac_in = min(1.0, inside.length / max(line.length, 1e-9))
        area = float(r["cum_area_km2"]) - float(r["cat_area_km2"]) * frac_in
        if area <= 0:
            continue
        p = part.interpolate(min(inset_m, part.length * 0.5))
        out.append((p.x, p.y, area, int(r["nzsegment"])))
    return out


def border_outlets(rec2: gpd.GeoDataFrame, bb, transform, shape, half_cells: int = 12) -> np.ndarray:
    """2-D bool mask of grid-border cells that may act as outlets: within ``half_cells`` of where a
    REC2 reach leaves the box. Elsewhere the box edge is not an outlet, so a lake or flat valley
    that touches the edge still drains to its real exit instead of out of the nearest edge."""
    x0, y0, x1, y1 = bb
    ring = box(*bb).boundary
    ux, uy = rec2["upcoordX"].to_numpy(), rec2["upcoordY"].to_numpy()
    dx, dy = rec2["downcoordX"].to_numpy(), rec2["downcoordY"].to_numpy()
    up_in = (ux > x0) & (ux < x1) & (uy > y0) & (uy < y1)
    dn_in = (dx > x0) & (dx < x1) & (dy > y0) & (dy < y1)
    nr, nc = shape
    seed = np.zeros(shape, dtype=bool)
    for i in np.flatnonzero(up_in & ~dn_in):
        geom = rec2.geometry.iloc[i]
        if geom is None or geom.is_empty:
            continue
        hit = geom.intersection(ring)
        pts = [hit] if hit.geom_type == "Point" else list(getattr(hit, "geoms", []))
        for p in pts:
            if p.geom_type != "Point":
                continue
            r, c = rasterio.transform.rowcol(transform, p.x, p.y)
            r, c = min(max(r, 0), nr - 1), min(max(c, 0), nc - 1)
            h = half_cells
            if abs(p.y - y1) < 1:
                seed[0, max(0, c - h):c + h + 1] = True
            if abs(p.y - y0) < 1:
                seed[nr - 1, max(0, c - h):c + h + 1] = True
            if abs(p.x - x0) < 1:
                seed[max(0, r - h):r + h + 1, 0] = True
            if abs(p.x - x1) < 1:
                seed[max(0, r - h):r + h + 1, nc - 1] = True
    return seed


def _snap(acc: np.ndarray, transform, x: float, y: float, radius_cells: int) -> tuple[int, int] | None:
    r, c = rasterio.transform.rowcol(transform, x, y)
    nr, nc = acc.shape
    r0, r1 = max(0, r - radius_cells), min(nr, r + radius_cells + 1)
    c0, c1 = max(0, c - radius_cells), min(nc, c + radius_cells + 1)
    if r0 >= r1 or c0 >= c1:
        return None
    win = np.nan_to_num(acc[r0:r1, c0:c1], nan=-1.0)
    k = int(np.argmax(win))
    if win.flat[k] <= 0:
        return None
    return r0 + k // win.shape[1], c0 + k % win.shape[1]


def route_tile(tile: Tile, rec2: gpd.GeoDataFrame) -> dict:
    """Route the tile's DEM with REC2 inflows; writes the files ``reaches.py`` expects."""
    d = tile.dir()
    z, valid, profile = read_dem(d / "dem_8m.tif")
    cs = profile["transform"].a
    t = time.time()
    near = rec2.cx[tile.box[0] - 5e4:tile.box[2] + 5e4, tile.box[1] - 5e4:tile.box[3] + 5e4]
    seed = border_outlets(near, tile.box, profile["transform"], z.shape)
    sea = not valid.all()
    if not seed.any() and not sea:
        seed = None  # no known exit: fall back to the whole border
    out = route(z, valid, cs, border_seed=seed)
    acc0 = out["acc"]
    rec = out["rec"]
    inflows = inflow_points(near, tile.box)
    w = np.where(valid, cs * cs, 0.0).ravel()
    inj = np.zeros(rec.size, dtype=np.bool_)
    used = []
    for x, y, a, seg in inflows:
        rc = _snap(acc0, profile["transform"], x, y, radius_cells=6)
        if rc is None:
            continue
        k = rc[0] * z.shape[1] + rc[1]
        w[k] += a * 1e6
        inj[k] = True
        used.append({"nzsegment": seg, "area_km2": round(a, 3), "x": round(x), "y": round(y)})
    is_edge = edge_mask(z.shape).ravel() & valid.ravel()
    acc, trunc, _ = accumulate(rec, w, is_edge)
    _, inj_up, _ = accumulate(rec, np.zeros(rec.size), inj)
    trunc = (trunc & ~inj_up).reshape(z.shape) & valid
    area = np.where(valid, acc.reshape(z.shape) / 1e6, np.nan).astype(np.float32)
    elapsed = time.time() - t
    _write(d / "area_km2.tif", area, profile, "float32", np.nan)
    _write(d / "truncated.tif", trunc.astype(np.uint8), profile, "uint8", 255)
    _write(d / "filled.tif", np.where(valid, out["filled"], np.nan).astype(np.float32), profile, "float32", np.nan)
    np.save(d / "receivers.npy", rec.astype(np.int32))
    hs = hillshade(z, valid, cs)
    _write(d / "hillshade.tif", hs, profile, "uint8", 0)
    stats = {"tile": tile.id, "dem": "dem_8m.tif", "shape": list(z.shape), "cellsize_m": cs,
             "valid_cells": int(valid.sum()), "routing_seconds": round(elapsed, 1),
             "inflows": len(used), "outlet_border_cells": int(seed.sum()) if seed is not None else -1,
             "max_area_km2": float(np.nanmax(area)) if valid.any() else 0.0}
    (d / "routing_stats.json").write_text(json.dumps(stats, indent=1))
    (d / "inflows.json").write_text(json.dumps(used))
    return stats


# --- keep the core, delete the rest -------------------------------------------------------------
def _core_window(ds, core) -> Window:
    x0, y0, x1, y1 = core
    return rasterio.windows.from_bounds(x0, y0, x1, y1, ds.transform).round_offsets().round_lengths()


def finish_tile(tile: Tile, keep_full: bool = False) -> dict:
    d = tile.dir()
    base = tile.index * SEG_BASE
    core = tile.core
    seg = gpd.read_file(d / "segments.gpkg")
    mid = seg.geometry.interpolate(0.5, normalized=True)
    in_core = ((mid.x >= core[0]) & (mid.x < core[2]) & (mid.y >= core[1]) & (mid.y < core[3])).to_numpy()
    ids = seg["seg_id"].to_numpy()
    keep_ids = set(ids[in_core].tolist())
    down = seg["down_seg"].to_numpy()
    seg["gid"] = base + ids
    seg["down_gid"] = np.where([x in keep_ids for x in down], base + down, -1)
    end = [g.coords[-1] for g in seg.geometry]
    seg["end_x"] = [e[0] for e in end]
    seg["end_y"] = [e[1] for e in end]
    seg["exits_core"] = (down >= 0) & (seg["down_gid"].to_numpy() < 0)
    seg["tile"] = tile.id
    seg = seg[in_core].reset_index(drop=True)
    seg.to_file(d / "segments_core.gpkg", layer="segments", driver="GPKG")

    with rasterio.open(d / "area_km2.tif") as ds:
        win = _core_window(ds, core)
        area = ds.read(1, window=win)
        prof = ds.profile.copy()
        tr = ds.window_transform(win)
    prof.update(height=area.shape[0], width=area.shape[1], transform=tr, compress="deflate", tiled=True,
                blockxsize=512, blockysize=512)
    prof.pop("predictor", None)
    with rasterio.open(d / "area_code.tif", "w", **dict(prof, dtype="uint8", nodata=0)) as dst:
        dst.write(encode_area(area), 1)
        dst.update_tags(code=CODE_TAG)
    with rasterio.open(d / "truncated.tif") as ds:
        tr8 = ds.read(1, window=_core_window(ds, core))
    with rasterio.open(d / "truncated_core.tif", "w", **dict(prof, dtype="uint8", nodata=255)) as dst:
        dst.write(tr8, 1)
    with rasterio.open(d / "local_catchment.tif") as ds:
        lc = ds.read(1, window=_core_window(ds, core)).astype(np.int64)
    lc = np.where(np.isin(lc, list(keep_ids)), base + lc, -1)
    with rasterio.open(d / "local_catchment_core.tif", "w", **dict(prof, dtype="int64", nodata=-1, predictor=2)) as dst:
        dst.write(lc, 1)
    with rasterio.open(d / "hillshade.tif") as ds:
        w = _core_window(ds, core)
        f = 3
        hs = ds.read(1, window=w, out_shape=(int(w.height) // f, int(w.width) // f),
                     resampling=rasterio.enums.Resampling.average)
    hprof = dict(prof, dtype="uint8", nodata=0, height=hs.shape[0], width=hs.shape[1],
                 transform=tr * tr.scale(f, f))
    with rasterio.open(d / "hillshade_24m.tif", "w", **hprof) as dst:
        dst.write(hs, 1)

    stats = json.loads((d / "routing_stats.json").read_text())
    stats.update(segments=int(len(seg)), core=list(core), done=True)
    (d / "tile.json").write_text(json.dumps(stats, indent=1))
    if not keep_full:
        for name in ("dem_8m.tif", "filled.tif", "receivers.npy", "area_km2.tif", "truncated.tif",
                     "local_catchment.tif", "hillshade.tif", "segments.gpkg", "rec2_nztm.geojson",
                     "flood_rec1_nztm.geojson", "geology_nztm.geojson", "fill_depth_m.tif"):
            (d / name).unlink(missing_ok=True)
    return stats


def process_tile(tile: Tile, layers: Layers, log=print, keep_full: bool = False) -> dict | None:
    from .reaches import run as run_reaches

    d = tile.dir()
    if (d / "tile.json").exists() and json.loads((d / "tile.json").read_text()).get("done"):
        return None
    t0 = time.time()
    reg = tile.region()
    fetch_dem(tile)
    t1 = time.time()
    layers.write_tile_inputs(tile)
    st = route_tile(tile, layers.rec2)
    t2 = time.time()
    if st["valid_cells"] == 0 or st["max_area_km2"] < 1.0:  # sea, or islets with no streams
        for f in d.iterdir():
            if f.name != "routing_stats.json":
                f.unlink()
        (d / "tile.json").write_text(json.dumps(dict(st, done=True, segments=0, core=list(tile.core))))
        return st
    run_reaches(reg)
    t3 = time.time()
    st = finish_tile(tile, keep_full=keep_full)
    log(f"[{tile.id}] dem {t1 - t0:.0f}s, route {t2 - t1:.0f}s, reaches {t3 - t2:.0f}s, "
        f"{st['segments']} segments, {st['inflows']} inflows, max {st['max_area_km2']:.0f} km2")
    return st


def fetch_dem(tile: Tile) -> None:
    d = tile.dir()
    if not (d / "dem_8m.tif").exists() and not (d / "tile.json").exists():
        tmp = d / "dem_8m.part.tif"
        mosaic(DEM_8M, tile.region().bbox_wgs84, tile.box, RES, tmp, nz_dir().parent / "_stac")
        tmp.rename(d / "dem_8m.tif")


def run_tiles(tiles: list[Tile] | None = None, log=print, keep_full: bool = False) -> None:
    """Process tiles in order, downloading the next tile's DEM while the current one is routed."""
    from concurrent.futures import ThreadPoolExecutor

    layers = Layers()
    tiles = tiles or land_tiles(layers.rec2)
    log(f"{len(tiles)} tiles")
    with ThreadPoolExecutor(1) as ex:
        nxt = ex.submit(fetch_dem, tiles[0]) if tiles else None
        for i, t in enumerate(tiles):
            try:
                nxt.result()
            except Exception as e:
                log(f"[{t.id}] DEM prefetch failed: {e!r}")
            nxt = ex.submit(fetch_dem, tiles[i + 1]) if i + 1 < len(tiles) else None
            try:
                process_tile(t, layers, log=log, keep_full=keep_full)
            except Exception as e:  # keep going; a failed tile can be re-run
                log(f"[{t.id}] FAILED: {e!r}")
            if (i + 1) % 10 == 0:
                log(f"{i + 1}/{len(tiles)} tiles, disk free {shutil.disk_usage(nz_dir()).free / 1e9:.1f} GB")
        if nxt is not None:
            nxt.result()


def merge_segments(tiles: list[Tile] | None = None, stitch_m: float = 24.0) -> gpd.GeoDataFrame:
    """All tiles' core segments, with downstream links stitched across tile edges."""
    from scipy.spatial import cKDTree

    root = nz_dir() / "tiles"
    paths = [root / t.id / "segments_core.gpkg" for t in tiles] if tiles else sorted(root.glob("*/segments_core.gpkg"))
    g = pd.concat([gpd.read_file(p) for p in paths if p.exists()], ignore_index=True)
    g = gpd.GeoDataFrame(g, geometry="geometry", crs="EPSG:2193")
    ex = np.flatnonzero(g["exits_core"].to_numpy())
    if len(ex):
        # A segment that leaves its tile's core continues in the neighbour: link it to the nearest
        # segment of another tile, of similar area, whose line passes the exit point.
        pts, owner = [], []
        for i, geom in enumerate(g.geometry):
            for x, y in geom.coords:
                pts.append((x, y))
                owner.append(i)
        tree, owner = cKDTree(np.array(pts)), np.array(owner)
        tile_arr, area = g["tile"].to_numpy(), g["area_km2"].to_numpy()
        down = g["down_gid"].to_numpy().copy()
        gid = g["gid"].to_numpy()
        for i in ex:
            cand = tree.query_ball_point((g.at[i, "end_x"], g.at[i, "end_y"]), stitch_m)
            best, bd = -1, 1e18
            for c in cand:
                j = owner[c]
                if tile_arr[j] == tile_arr[i]:
                    continue
                ratio = area[j] / max(area[i], 1e-6)
                if not 0.7 <= ratio <= 3.0:
                    continue
                dd = (pts[c][0] - g.at[i, "end_x"]) ** 2 + (pts[c][1] - g.at[i, "end_y"]) ** 2 + abs(math.log(ratio))
                if dd < bd:
                    best, bd = j, dd
            if best >= 0:
                down[i] = gid[best]
        g["down_gid"] = down
    return g


# --- GIS export -----------------------------------------------------------------------------------
def _vrt(paths: list[Path], out: Path, band_type: str, nodata, resampling: str = "nearest") -> None:
    """A GDAL VRT mosaic of same-resolution NZTM tiles (QGIS opens it like one raster)."""
    infos = []
    for p in paths:
        with rasterio.open(p) as ds:
            infos.append((p, ds.bounds, ds.width, ds.height, ds.res[0]))
    res = infos[0][4]
    x0 = min(i[1].left for i in infos)
    y1 = max(i[1].top for i in infos)
    x1 = max(i[1].right for i in infos)
    y0 = min(i[1].bottom for i in infos)
    W, H = round((x1 - x0) / res), round((y1 - y0) / res)
    srs = rasterio.crs.CRS.from_epsg(2193).to_wkt().replace("&", "&amp;").replace('"', "&quot;")
    src = []
    for p, b, w, h, _ in infos:
        xo, yo = round((b.left - x0) / res), round((y1 - b.top) / res)
        src.append(f"""    <SimpleSource resampling="{resampling}">
      <SourceFilename relativeToVRT="1">{os.path.relpath(p, out.parent)}</SourceFilename>
      <SourceBand>1</SourceBand>
      <SrcRect xOff="0" yOff="0" xSize="{w}" ySize="{h}" />
      <DstRect xOff="{xo}" yOff="{yo}" xSize="{w}" ySize="{h}" />
    </SimpleSource>""")
    out.write_text(f"""<VRTDataset rasterXSize="{W}" rasterYSize="{H}">
  <SRS>{srs}</SRS>
  <GeoTransform>{x0}, {res}, 0, {y1}, 0, {-res}</GeoTransform>
  <VRTRasterBand dataType="{band_type}" band="1">
    <NoDataValue>{nodata}</NoDataValue>
{chr(10).join(src)}
  </VRTRasterBand>
</VRTDataset>
""")


def _area_code_qml() -> str:
    from .export import RAMP, STOPS

    items = []
    for v, c in zip(STOPS, RAMP, strict=True):
        code = 2 + (math.log10(v) - CODE_LOG_MIN) / CODE_STEP
        items.append(f'<item alpha="230" value="{code:.1f}" label="{v} km²" color="{c}"/>')
    return f"""<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.34.0" styleCategories="AllStyleCategories">
  <pipe>
    <rasterrenderer type="singlebandpseudocolor" band="1" opacity="1" alphaBand="-1">
      <rastershader>
        <colorrampshader colorRampType="INTERPOLATED" classificationMode="1" clip="1">
          <item alpha="0" value="1" label="&lt; 2e-5 km²" color="#cde2fb"/>
          {chr(10).join(items)}
        </colorrampshader>
      </rastershader>
    </rasterrenderer>
  </pipe>
</qgis>
"""


def export_gis(log=print) -> Path:
    """``data/nz/gis/``: GeoPackage (segments >= 1 km2, runs, region filters) and VRT mosaics."""
    from .national import region_filters

    out = nz_dir() / "gis"
    out.mkdir(exist_ok=True)
    gpkg = out / "river_finder_nz.gpkg"
    gpkg.unlink(missing_ok=True)
    region_filters().to_file(gpkg, layer="guide_regions", driver="GPKG")
    runs = []
    for d in sorted((nz_dir() / "regions").glob("*/")):
        if (d / "segments_scored.gpkg").exists():
            g = gpd.read_file(d / "segments_scored.gpkg")
            g = g[g["area_km2"] >= 1.0]
            g.to_file(gpkg, layer="reach_segments", driver="GPKG", mode="a" if gpkg.exists() else "w")
        if (d / "runs.geojson").exists():
            runs.append(gpd.read_file(d / "runs.geojson").to_crs(2193))
    if runs:
        pd.concat(runs, ignore_index=True).pipe(gpd.GeoDataFrame, crs="EPSG:2193").to_file(gpkg, layer="runs",
                                                                                          driver="GPKG")
    root = nz_dir() / "tiles"
    for name, typ, nodata, rs in (("area_code", "Byte", 0, "nearest"), ("hillshade_24m", "Byte", 0, "average"),
                                  ("truncated_core", "Byte", 255, "nearest"),
                                  ("local_catchment_core", "Int64", -1, "nearest")):
        paths = sorted(root.glob(f"*/{name}.tif"))
        if paths:
            _vrt(paths, out / f"{name}.vrt", typ, nodata, rs)
    (out / "area_code.qml").write_text(_area_code_qml())
    (out / "README.txt").write_text(
        "river-finder national GIS export. NZTM2000 (EPSG:2193). Open the .vrt files in QGIS; they\n"
        "read the per-tile GeoTIFFs in ../tiles/ (keep the folder layout).\n\n"
        "area_code.vrt        8 m catchment area as a code: 0 sea, 2..255 = log10(km2) from 2e-5 to 30 000 km2\n"
        f"                     (area = 10 ** ({CODE_LOG_MIN:.4f} + (code - 2) * {CODE_STEP:.6f}));\n"
        "                     styled by area_code.qml\n"
        "hillshade_24m.vrt    hillshade\n"
        "truncated_core.vrt   1 = catchment reaches a tile edge without a REC2 inflow\n"
        "local_catchment_core.vrt  global id (gid) of the reach segment each cell first drains into\n"
        "river_finder_nz.gpkg layers: guide_regions, reach_segments (>= 1 km2, all features and\n"
        "                     scores, gid / region), runs (default run-finder settings)\n"
        "../candidates.csv    candidate runs not on known rivers, all regions\n")
    log(f"GIS export -> {out}")
    return out
