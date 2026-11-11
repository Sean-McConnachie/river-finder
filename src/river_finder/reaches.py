"""Phase 5: turn the flow graph into a channel network of short reach segments with kayak-relevant metrics.

Network: cells with upstream area >= ``min_area_km2`` are channel cells. A *link* runs from a
channel head (source or confluence) down to the next confluence. Each link is cut into
segments of about ``seg_len_m``. Segments are the unit for scoring and for the viewer.

Per-segment metrics:
- ``gradient_m_per_km``: drop over the segment along the flow path (filled DEM, so the profile
  never rises).
- ``steepest_50m_m_per_km``: steepest 50 m window inside the segment. Picks out falls and
  steps.
- ``ksn``: normalised channel steepness, S * A^0.45 (A in m^2). It compares steepness across
  river sizes; high values mark knickzones.
- ``knick_ratio``: ``ksn`` relative to the segment immediately downstream.
- ``gorge_depth_m``: mean height of the highest ground within ``gorge_radius_m`` above the
  channel.
- ``head_m``: mean catchment elevation minus channel elevation (how far the catchment sits
  above this point).
- ``upstream_flat_frac``: share of the catchment with slope < 4 degrees (plateau tops and
  swamps).
- ``upstream_hard_frac``: share of the catchment on resistant rock (GNS 1:250k).
- ``bed_rock`` / ``bed_hard``: rock group at the segment midpoint, and whether it is resistant.
- ``maf_m3s``: estimated mean annual flood = our area x the specific MAF (m3/s/km2) of the
  nearest NIWA REC1 reach of similar size.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
import rasterio.features
from numba import njit
from scipy import ndimage
from scipy.spatial import cKDTree
from shapely.geometry import LineString

from .regions import Region
from .routing import accumulate, edge_mask

THETA = 0.45


@njit(cache=True)
def extract_links(rec, chan):
    """Return (cells, link_ptr, link_next_cell): the cells of every link in upstream->downstream
    order, packed in CSR form, plus the first cell below each link (-1 if it leaves the grid)."""
    n = rec.size
    ndon = np.zeros(n, dtype=np.int32)
    nchan = 0
    for i in range(n):
        if chan[i]:
            nchan += 1
            r = rec[i]
            if r >= 0 and chan[r]:
                ndon[r] += 1
    cells = np.empty(nchan, dtype=np.int64)
    ptr = np.empty(nchan + 1, dtype=np.int64)
    nxt = np.empty(nchan, dtype=np.int64)
    nl = 0
    k = 0
    for h in range(n):
        if not chan[h] or ndon[h] == 1:
            continue
        ptr[nl] = k
        c = h
        while True:
            cells[k] = c
            k += 1
            r = rec[c]
            if r < 0 or not chan[r] or ndon[r] != 1:
                nxt[nl] = r if (r >= 0 and chan[r]) else -1
                break
            c = r
        nl += 1
    ptr[nl] = k
    return cells[:k], ptr[: nl + 1], nxt[:nl]


def _step_len(a: int, b: int, ncol: int, cellsize: float) -> float:
    ra, ca = divmod(a, ncol)
    rb, cb = divmod(b, ncol)
    return cellsize * (math.sqrt(2.0) if (ra != rb and ca != cb) else 1.0)


def _steepest(dist: np.ndarray, z: np.ndarray, window: float) -> float:
    """Largest drop per km over any ``window``-metre stretch (two-pointer over the profile)."""
    best, j = 0.0, 0
    for i in range(len(dist)):
        while j < len(dist) - 1 and dist[j] - dist[i] < window:
            j += 1
        span = dist[j] - dist[i]
        if span >= 0.6 * window:
            best = max(best, (z[i] - z[j]) / span * 1000.0)
    return best


def build_segments(region: Region, min_area_km2: float = 0.5, seg_len_m: float = 250.0,
                   gorge_radius_m: float = 150.0) -> gpd.GeoDataFrame:
    d = region.data_dir()
    dem_name = (d / "routing_stats.json").exists() and json.loads((d / "routing_stats.json").read_text())["dem"]
    with rasterio.open(d / "area_km2.tif") as ds:
        area = ds.read(1)
        transform, shape2 = ds.transform, ds.shape
    with rasterio.open(d / "filled.tif") as ds:
        filled = ds.read(1)
    with rasterio.open(d / dem_name) as ds:
        z = ds.read(1).astype(np.float32)
    valid = np.isfinite(area)
    cs = transform.a
    ncol = shape2[1]
    rec = np.load(d / "receivers.npy").astype(np.int64)

    # --- per-cell upstream statistics via weighted accumulation -------------------------------
    edge = (edge_mask(shape2) & valid).ravel()
    ones = valid.ravel().astype(np.float64)
    zz = np.where(valid, filled, 0.0)
    gy, gx = np.gradient(np.where(valid, filled, np.nanmean(filled[valid])), cs)
    flat = (np.hypot(gx, gy) < math.tan(math.radians(4.0))) & valid
    del gx, gy
    hard = np.zeros(shape2, dtype=np.uint8)
    geo_path = d / "geology_nztm.geojson"
    geo = gpd.read_file(geo_path) if geo_path.exists() else None
    if geo is not None and len(geo):
        shapes = [(g, 1) for g, h in zip(geo.geometry, geo["hard_rock"], strict=True) if h and g is not None]
        if shapes:
            hard = rasterio.features.rasterize(shapes, out_shape=shape2, transform=transform, fill=0, dtype="uint8")
    n_up, _, _ = accumulate(rec, ones, edge)
    z_up, _, _ = accumulate(rec, zz.ravel().astype(np.float64), edge)
    flat_up, _, _ = accumulate(rec, flat.ravel().astype(np.float64), edge)
    hard_up, _, _ = accumulate(rec, (hard.ravel() & valid.ravel()).astype(np.float64), edge)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean_z_up = z_up / n_up
        flat_frac = flat_up / n_up
        hard_frac = hard_up / n_up
    del z_up, flat_up, hard_up, n_up

    # Gorge depth: highest ground within the radius (square window) above the channel.
    rad = max(1, round(gorge_radius_m / cs))
    zmax = ndimage.maximum_filter(np.where(valid, z, -1e9), size=2 * rad + 1, mode="nearest")

    # --- network ----------------------------------------------------------------------------
    chan = (np.nan_to_num(area, nan=0.0) >= min_area_km2).ravel()
    cells, ptr, nxt = extract_links(rec, chan)
    af, ff, zf, zmf = area.ravel(), filled.ravel(), z.ravel(), zmax.ravel()
    with rasterio.open(d / "truncated.tif") as ds:
        trf = ds.read(1).ravel() == 1
    hard_f = hard.ravel()

    seg_rows, seg_geoms = [], []
    first_seg_of_link = np.full(len(ptr) - 1, -1, dtype=np.int64)
    last_seg_of_link = np.full(len(ptr) - 1, -1, dtype=np.int64)
    cell_link = {}
    for li in range(len(ptr) - 1):
        lc = cells[ptr[li]:ptr[li + 1]]
        cell_link[int(lc[0])] = li
        path = list(lc) + ([int(nxt[li])] if nxt[li] >= 0 else [])
        if len(path) < 2:
            continue
        dist = np.zeros(len(path))
        for k in range(1, len(path)):
            dist[k] = dist[k - 1] + _step_len(path[k - 1], path[k], ncol, cs)
        total = dist[-1]
        nseg = max(1, int(round(total / seg_len_m)))
        bounds = np.searchsorted(dist, np.linspace(0, total, nseg + 1))
        bounds[-1] = len(path) - 1
        for s in range(nseg):
            a, b = int(bounds[s]), int(bounds[s + 1])
            if b <= a:
                continue
            idx = np.array(path[a:b + 1])
            dd, zp = dist[a:b + 1] - dist[a], ff[idx]
            length = dd[-1]
            drop = float(zp[0] - zp[-1])
            grad = drop / length * 1000.0 if length > 0 else 0.0
            # The last segment of a link ends on the confluence cell below; read its area just above.
            ends_on_confluence = b == len(path) - 1 and nxt[li] >= 0
            a_bot = float(af[idx[-2] if ends_on_confluence else idx[-1]])
            a_mid = float(np.nanmean(af[idx[:-1]]))
            mid = idx[len(idx) // 2]
            body = idx[:-1] if len(idx) > 1 else idx
            ksn = (drop / length if length > 0 else 0.0) * (a_mid * 1e6) ** THETA
            rows, cols = np.divmod(idx, ncol)
            xs, ys = rasterio.transform.xy(transform, rows, cols)
            step = max(1, int(round(25.0 / cs)))
            keep = list(range(0, len(xs), step))
            if keep[-1] != len(xs) - 1:
                keep.append(len(xs) - 1)
            geom = LineString([(xs[k], ys[k]) for k in keep])
            seg_id = len(seg_rows)
            if s == 0:
                first_seg_of_link[li] = seg_id
            last_seg_of_link[li] = seg_id
            seg_rows.append({
                "seg_id": seg_id, "link": li, "down_seg": seg_id + 1 if s < nseg - 1 else -1,
                "length_m": round(length, 1), "z_top": round(float(zp[0]), 2), "z_bot": round(float(zp[-1]), 2),
                "gradient_m_per_km": round(grad, 2),
                "steepest_50m_m_per_km": round(_steepest(dd, zp, 50.0), 2),
                "area_km2": round(a_bot, 4), "area_mid_km2": round(a_mid, 4),
                "ksn": round(ksn, 2),
                "gorge_depth_m": round(float(np.mean(zmf[body] - zf[body])), 2),
                "head_m": round(float(mean_z_up[mid] - ff[mid]), 2),
                "upstream_flat_frac": round(float(flat_frac[mid]), 4),
                "upstream_hard_frac": round(float(hard_frac[mid]), 4),
                "bed_hard": int(hard_f[mid]),
                "truncated": bool(trf[mid]),
            })
            seg_geoms.append(geom)
    # Link the last segment of each link to the first segment of the link below.
    for li in range(len(ptr) - 1):
        last = last_seg_of_link[li]
        if last < 0 or nxt[li] < 0:
            continue
        down_link = cell_link.get(int(nxt[li]))
        if down_link is not None and first_seg_of_link[down_link] >= 0:
            seg_rows[last]["down_seg"] = int(first_seg_of_link[down_link])
    gdf = gpd.GeoDataFrame(seg_rows, geometry=seg_geoms, crs="EPSG:2193")
    # knick ratio: ksn relative to the next segment downstream
    ksn = gdf["ksn"].to_numpy()
    down = gdf["down_seg"].to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        kr = np.where(down >= 0, ksn / np.maximum(ksn[np.maximum(down, 0)], 1.0), np.nan)
    gdf["knick_ratio"] = np.round(kr, 3)
    _attach_geology(gdf, geo)
    _attach_flood(gdf, d / "flood_rec1_nztm.geojson")
    _attach_names(gdf, d / "rec2_nztm.geojson")
    return gdf


def _attach_geology(gdf: gpd.GeoDataFrame, geo: gpd.GeoDataFrame | None) -> None:
    gdf["bed_rock"] = ""
    if geo is None or not len(geo):
        return
    mids = gpd.GeoDataFrame(geometry=[g.interpolate(0.5, normalized=True) for g in gdf.geometry], crs=gdf.crs)
    geo = geo.set_crs("EPSG:2193", allow_override=True)
    j = gpd.sjoin(mids, geo[["rockgroup", "geometry"]], how="left", predicate="within")
    j = j[~j.index.duplicated(keep="first")]
    gdf["bed_rock"] = j["rockgroup"].fillna("").str.strip().to_numpy()


def _line_coords(geom: dict) -> list:
    return [geom["coordinates"]] if geom["type"] == "LineString" else geom["coordinates"]


def _match_reaches(gdf: gpd.GeoDataFrame, feats: list[dict], area_key: str, radius_m: float,
                   ratio: tuple[float, float]) -> np.ndarray:
    """For each segment, index of the nearest reach (within ``radius_m``) whose area is within
    ``ratio`` of the segment's area; -1 if none."""
    pts, owner = [], []
    for i, f in enumerate(feats):
        if f["geometry"] is None:
            continue
        for line in _line_coords(f["geometry"]):
            for x, y in line:
                pts.append((x, y))
                owner.append(i)
    if not pts:
        return np.full(len(gdf), -1)
    tree, owner = cKDTree(np.array(pts)), np.array(owner)
    areas = np.array([f["properties"].get(area_key) or np.nan for f in feats], dtype=float)
    out = np.full(len(gdf), -1)
    for k, (g, a) in enumerate(zip(gdf.geometry, gdf["area_mid_km2"], strict=True)):
        m = g.interpolate(0.5, normalized=True)
        cand = tree.query_ball_point((m.x, m.y), radius_m)
        best, bestd = -1, 1e18
        for c in cand:
            i = owner[c]
            r = areas[i] / a if a > 0 else np.nan
            if not (ratio[0] <= r <= ratio[1]):
                continue
            dd = (pts[c][0] - m.x) ** 2 + (pts[c][1] - m.y) ** 2 + (math.log(r) * 100) ** 2
            if dd < bestd:
                best, bestd = i, dd
        out[k] = best
    return out


def _attach_flood(gdf: gpd.GeoDataFrame, path: Path) -> None:
    gdf["specific_maf"] = np.nan
    gdf["maf_m3s"] = np.nan
    if not path.exists():
        return
    feats = json.loads(path.read_text())["features"]
    idx = _match_reaches(gdf, feats, "Areakm2", 400.0, (0.4, 2.5))
    spec = np.array([f["properties"]["H_C18_MAF"] / f["properties"]["Areakm2"]
                     if f["properties"].get("Areakm2") else np.nan for f in feats])
    s = np.where(idx >= 0, spec[np.maximum(idx, 0)], np.nan)
    fill = np.nanmedian(s) if np.isfinite(s).any() else np.nan
    gdf["specific_maf_matched"] = np.isfinite(s)
    s = np.where(np.isfinite(s), s, fill)
    gdf["specific_maf"] = np.round(s, 4)
    gdf["maf_m3s"] = np.round(s * gdf["area_km2"].to_numpy(), 2)


def _attach_names(gdf: gpd.GeoDataFrame, path: Path) -> None:
    gdf["river"] = ""
    if not path.exists():
        return
    feats = json.loads(path.read_text())["features"]
    idx = _match_reaches(gdf, feats, "cum_area_km2", 150.0, (0.6, 1.6))
    names = [(f["properties"].get("name") or "").strip() for f in feats]
    gdf["river"] = [names[i] if i >= 0 else "" for i in idx]


def run(region: Region, **kw) -> Path:
    gdf = build_segments(region, **kw)
    d = region.data_dir()
    out = d / "segments.gpkg"
    out.unlink(missing_ok=True)
    gdf.to_file(out, layer="segments", driver="GPKG")
    return out

