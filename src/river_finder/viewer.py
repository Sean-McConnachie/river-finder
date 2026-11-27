"""Region data for the web app: the 5 m reference regions' payloads (``region_payload``, written as ``<id>.json`` by
``river-finder nz viewer``) and the columnar reach table of every region pack (``_segments``, also used by
``viewer_nz`` for the river-guide regions).

Each reference region is warped to Web Mercator (so the map can place it as one image) and stored as:
- a JPEG hillshade,
- an 8-bit PNG of log10(catchment area) codes (max-pooled when downsampling, so 1-cell-wide
  channels survive),
- a PNG mask of cells whose upstream area touches the box edge.
The page colours the heat map in the browser, so the threshold and opacity can be changed live.

The single-file Leaflet page this module once built (``data/viewer/viewer.html``) was removed in rf-aq6.18.
"""

from __future__ import annotations

import base64
import io
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
from PIL import Image
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.warp import calculate_default_transform, reproject

from .areacode import CODE_LOG_MIN, CODE_STEP, encode_area
from .regions import Region

# Pixel codes in the area PNG: see areacode.py (every land cell has an area, down to one cell)
LOG_MIN, STEP = CODE_LOG_MIN, CODE_STEP

TITLES = {"kaimai": "Kaimai Range", "tutamoe": "Tutamoe plateau", "karangahake": "Karangahake Gorge"}


def _warp(src_path: Path, max_px: int, resampling: Resampling, dst_nodata):
    with rasterio.open(src_path) as src:
        tr, w, h = calculate_default_transform(src.crs, "EPSG:3857", src.width, src.height, *src.bounds)
        scale = max(w, h) / max_px
        if scale > 1:
            w, h = int(math.ceil(w / scale)), int(math.ceil(h / scale))
            tr = tr * tr.scale(scale, scale)
        dst = np.full((h, w), dst_nodata, dtype=src.dtypes[0])
        reproject(rasterio.band(src, 1), dst, dst_transform=tr, dst_crs="EPSG:3857",
                  resampling=resampling, dst_nodata=dst_nodata)
        return dst, tr


def _data_uri(img: Image.Image, fmt: str, **kw) -> str:
    buf = io.BytesIO()
    img.save(buf, format=fmt, optimize=True, **kw)
    mime = "jpeg" if fmt == "JPEG" else "png"
    return f"data:image/{mime};base64," + base64.b64encode(buf.getvalue()).decode()


def _rec2_features(path: Path, min_order: int) -> list[dict]:
    """REC2 lines for the viewer, each oriented upstream -> downstream (so the viewer can draw
    flow arrows): the end nearest REC2's recorded downstream node comes last."""
    fc = json.loads(path.read_text())
    to_ll = Transformer.from_crs("EPSG:2193", "EPSG:4326", always_xy=True)
    out = []
    for f in fc["features"]:
        p = f["properties"]
        if p["StreamOrde"] < min_order:
            continue
        g = f["geometry"]
        lines = [g["coordinates"]] if g["type"] == "LineString" else g["coordinates"]
        if p.get("downcoordX") is not None:
            dx, dy = to_ll.transform(p["downcoordX"], p["downcoordY"])
            k = math.cos(math.radians(dy))

            def d2(pt, dx=dx, dy=dy, k=k):
                return ((pt[0] - dx) * k) ** 2 + (pt[1] - dy) ** 2

            lines = [ln if d2(ln[-1]) <= d2(ln[0]) else ln[::-1] for ln in lines]
        out.append({
            "n": p.get("name") or "", "o": p["StreamOrde"], "a": round(p["cum_area_km2"], 2),
            "c": [[[round(x, 5), round(y, 5)] for x, y in line] for line in lines],
        })
    return out


@dataclass
class ReachLabels:
    """Label status (0 unknown, 1 runnable, 2 not runnable), grade (NaN unknown) and held-out flag of every reach of a
    region's ``segments_scored.gpkg`` (file order, ``seg_id`` to check it), and the sections on them."""
    status: np.ndarray
    grade: np.ndarray
    held: np.ndarray
    seg_id: np.ndarray
    sections: list = field(default_factory=list)


def _section_info(s) -> dict:
    """``S.sections`` entry of a section: a research section (it has an ``id``) names its primary source."""
    ex = s.extra or {}
    source = (ex.get("primary_source") or "") if ex.get("id") else s.source
    return {"river": s.river, "section": ex.get("section") or "", "status": s.status, "grade": s.grade,
            "source": source}


def _segments(region: Region, min_area_km2: float = 1.0, labels: ReachLabels | None = None) -> dict | None:
    """Columnar segment table for the run finder and score overlays (WGS84, simplified). ``labels``: the reaches'
    label status and grade (``viewer_nz.national_labels``: every known section, guide research included); without,
    the region's known_runs.toml sections (``labels.segment_labels``: the 5 m reference areas)."""
    import geopandas as gpd

    path = region.data_dir() / "segments_scored.gpkg"
    if not path.exists():
        return None
    from .labels import segment_labels

    g = gpd.read_file(path)
    if labels is None:
        status, grade, held, _, secs = segment_labels(g, region.name)
    else:
        if len(labels.status) != len(g) or not np.array_equal(labels.seg_id, g["seg_id"].to_numpy()):
            raise ValueError(f"{region.name}: the reach labels come from another build of {path.name} "
                             "(rebuild the national table: river-finder models table)")
        status, grade, held, secs = labels.status, labels.grade, labels.held, labels.sections
    g["lab_status"], g["lab_grade"], g["lab_held"] = status, grade, held
    g = g[g["area_km2"] >= min_area_km2].copy()
    num = ["length_m", "grad_s", "steepest_50m_m_per_km", "area_km2", "maf_m3s", "gorge_depth_m", "z_top", "z_bot",
           "ksn_s", "power_kw_m", "head_m", "melton", "height_to_width", "score_regression", "score_signature"]
    g[num] = g[num].fillna(0.0)   # the 1 m rebuild has a few reaches without a profile (zero length)
    idx = {int(s): i for i, s in enumerate(g["seg_id"])}
    g["geometry"] = g.geometry.simplify(6.0)
    g = g.to_crs(4326)
    rivers = sorted({r for r in g["river"] if r})
    rix = {r: i for i, r in enumerate(rivers)}
    zcols = [c for c in g.columns if c.startswith("z_")]
    listed = {r.river for r in region.known_runs if r.status == "listed"}
    return {
        "rivers": rivers,
        "listed": sorted(listed & set(rivers)),
        "features": [c[2:] for c in zcols],
        "d": [idx.get(int(v), -1) for v in g["down_seg"]],
        "u": [idx.get(int(v), -1) for v in g["up_seg"]],
        "len": [round(float(v)) for v in g["length_m"]],
        "g": [round(float(v), 1) for v in g["grad_s"]],
        "s50": [round(float(v)) for v in g["steepest_50m_m_per_km"]],
        "a": [round(float(v), 2) for v in g["area_km2"]],
        "maf": [round(float(v), 1) for v in g["maf_m3s"]],
        "gorge": [round(float(v)) for v in g["gorge_depth_m"]],
        "zt": [round(float(v), 1) for v in g["z_top"]],
        "zb": [round(float(v), 1) for v in g["z_bot"]],
        "hard": [int(v) for v in g["bed_hard"]],
        "ksn": [round(float(v), 1) for v in g["ksn_s"]],
        "pow": [round(float(v), 3) for v in g["power_kw_m"]],
        "head": [round(float(v)) for v in g["head_m"]],
        "sta": [int(v) for v in g["lab_status"]],
        "gr": [None if not np.isfinite(v) else round(float(v), 2) for v in g["lab_grade"]],
        "lho": [int(v) for v in g["lab_held"]],
        "sections": [_section_info(x) for x in secs],
        "mel": [round(float(v), 4) for v in g["melton"]],
        "hw": [round(float(v), 3) for v in g["height_to_width"]],
        "r": [rix.get(v, -1) for v in g["river"]],
        "lab": [int(v) for v in g["label"]],
        "ho": [int(v) for v in g["holdout"]],
        "reg": [round(float(v), 1) for v in g["score_regression"]],
        "sig": [round(float(v), 1) for v in g["score_signature"]],
        "z": [[round(float(v), 2) for v in row] for row in g[zcols].to_numpy()],
        "c": [[[round(x, 5), round(y, 5)] for x, y in geom.coords] for geom in g.geometry],
    } | _segments_1m(g)


def _segments_1m(g) -> dict:
    """The 1 m rebuild's extra reach columns (``viewer_nz.PACK_1M``; none for the 8 m scan or a 5 m area)."""
    if "steps_n" not in g:
        return {}
    num = lambda c: np.nan_to_num(g[c].to_numpy(dtype=float))   # noqa: E731
    src = {"lidar": 0, "mixed": 1, "8m": 2, "lake": 3}
    yr = list(g["lidar_year"]) if "lidar_year" in g else [None] * len(g)
    return {"s100": [round(float(v), 1) for v in num("steepest_100m_m_per_km")],
            "stp": [int(v) for v in num("steps_n")],
            "smax": [round(float(v), 1) for v in num("step_max_m")],
            "f1m": [int(v) for v in num("falls1m_n")],
            "lake": [int(bool(v)) for v in g["lake"].fillna(False)] if "lake" in g else [0] * len(g),
            "dsrc": [src.get(str(v), 2) for v in g["dem_source"]] if "dem_source" in g else [2] * len(g),
            "ly": [None if v is None or pd.isna(v) else int(v) for v in yr]}


def _catchment_labels(region: Region, shape: tuple[int, int], max_px: int, min_area_km2: float = 1.0) -> str | None:
    """RGB-encoded PNG (value = viewer segment index + 1, 0 = none) on the heat-map grid: for each
    pixel, the viewer segment its ground first drains into. Segments too small for the viewer are
    folded into the first viewer segment downstream of them."""
    import geopandas as gpd

    d = region.data_dir()
    if not (d / "local_catchment.tif").exists() or not (d / "segments_scored.gpkg").exists():
        return None
    g = gpd.read_file(d / "segments_scored.gpkg", columns=["seg_id", "down_seg", "area_km2"], ignore_geometry=True)
    n = len(g)
    down = g["down_seg"].to_numpy()
    keep = (g["area_km2"] >= min_area_km2).to_numpy()
    view = np.full(n, -1, dtype=np.int64)
    view[np.flatnonzero(keep)] = np.arange(int(keep.sum()))
    for i in range(n):  # fold small segments into the first kept segment downstream
        if view[i] >= 0:
            continue
        chain, cur = [], i
        while cur >= 0 and view[cur] < 0 and len(chain) < 10000:
            chain.append(cur)
            cur = down[cur]
        tgt = view[cur] if cur >= 0 else -1
        view[chain] = tgt
    lab, _ = _warp(d / "local_catchment.tif", max_px, Resampling.nearest, -1)
    lab = lab[: shape[0], : shape[1]].astype(np.int64)
    v = np.where(lab >= 0, view[np.clip(lab, 0, n - 1)] + 1, 0)
    rgb = np.stack([(v >> 16) & 255, (v >> 8) & 255, v & 255], axis=-1).astype(np.uint8)
    return _data_uri(Image.fromarray(rgb, "RGB"), "PNG")


def region_payload(region: Region, max_px: int = 1800, rec2_min_order: int = 3) -> dict:
    d = region.data_dir()
    area, tr = _warp(d / "area_km2.tif", max_px, Resampling.max, np.nan)
    hs, _ = _warp(d / "hillshade.tif", max_px, Resampling.average, 0)
    # A display pixel is "truncated" when its largest-area cell is: max-pool the area of truncated
    # cells separately and compare with the overall max (pooling the 0/1 mask itself would let a
    # neighbouring edge-fed stream flag the pixel).
    with rasterio.open(d / "area_km2.tif") as a_ds, rasterio.open(d / "truncated.tif") as t_ds:
        tmp = d / "_area_truncated.tif"
        prof = a_ds.profile
        with rasterio.open(tmp, "w", **prof) as dst:
            a = a_ds.read(1)
            dst.write(np.where(t_ds.read(1) == 1, a, np.nan).astype("float32"), 1)
    t_area, _ = _warp(tmp, max_px, Resampling.max, np.nan)
    tmp.unlink()
    h, w = area.shape
    hs = hs[:h, :w]
    trunc = (np.isfinite(t_area[:h, :w]) & (t_area[:h, :w] >= area * 0.999)).astype(np.uint8) * 255
    x0, y1 = tr.c, tr.f
    x1, y0 = x0 + w * tr.a, y1 + h * tr.e
    to_ll = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    lon0, lat0 = to_ll.transform(x0, y0)
    lon1, lat1 = to_ll.transform(x1, y1)

    runs = []
    summary_path = d / "validation_summary.json"
    named = {n["river"]: n for n in json.loads(summary_path.read_text())["named"]} if summary_path.exists() else {}
    for r in region.known_runs:
        v = named.get(r.river, {})
        lon, lat = v.get("measured_at_wgs84", r.outlet_wgs84)
        runs.append({"river": r.river, "status": r.status, "lon": lon, "lat": lat,
                     "rec2_km2": v.get("rec2_km2", r.rec2_cum_area_km2), "model_km2": v.get("model_km2")})
    stats = json.loads((d / "routing_stats.json").read_text())
    validation = json.loads(summary_path.read_text()) if summary_path.exists() else None
    return {
        "id": region.name, "title": TITLES.get(region.name, region.name.title()),
        "description": region.description,
        "bounds": [[lat0, lon0], [lat1, lon1]], "merc": [x0, y0, x1, y1], "width": w, "height": h,
        "pixel_m": round(tr.a, 1),
        "hillshade": _data_uri(Image.fromarray(np.where(hs == 0, 0, hs).astype(np.uint8), "L"), "JPEG", quality=82),
        "area": _data_uri(Image.fromarray(encode_area(area), "L"), "PNG"),
        "trunc": _data_uri(Image.fromarray(trunc, "L"), "PNG"),
        "code": {"log_min": LOG_MIN, "step": STEP},
        "rec2": _rec2_features(d / "rec2_wgs84.geojson", rec2_min_order) if (d / "rec2_wgs84.geojson").exists() else [],
        "runs": runs,
        "stats": stats,
        "validation": validation,
        "segments": _segments(region),
        "catch": _catchment_labels(region, (h, w), max_px),
    }

