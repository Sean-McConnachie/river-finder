"""Compare modelled catchment area with NIWA REC2 CUM_AREA at reach outlets."""

from __future__ import annotations

import csv
import json
import math

import numpy as np
import rasterio
from pyproj import Transformer
from scipy import ndimage

from .rec2 import upstream_complete
from .regions import Region


def _lines(geom):
    return [geom["coordinates"]] if geom["type"] == "LineString" else geom["coordinates"]


def _point_upstream(line, down_xy, dist):
    """Walk ``dist`` metres upstream along ``line`` from its downstream end."""
    pts = line if math.dist(line[-1], down_xy) <= math.dist(line[0], down_xy) else line[::-1]
    pts = pts[::-1]  # now ordered downstream -> upstream
    remaining = dist
    for (x0, y0), (x1, y1) in zip(pts, pts[1:], strict=False):
        seg = math.hypot(x1 - x0, y1 - y0)
        if seg >= remaining:
            f = remaining / seg
            return x0 + f * (x1 - x0), y0 + f * (y1 - y0)
        remaining -= seg
    return pts[-1]


def sample_max(area, transform, x, y, radius_cells):
    r, c = rasterio.transform.rowcol(transform, x, y)
    r0, r1 = max(r - radius_cells, 0), min(r + radius_cells + 1, area.shape[0])
    c0, c1 = max(c - radius_cells, 0), min(c + radius_cells + 1, area.shape[1])
    if r0 >= r1 or c0 >= c1:
        return math.nan
    win = area[r0:r1, c0:c1]
    return float(np.nanmax(win)) if np.isfinite(win).any() else math.nan


def snap_tolerant(area, transform, x, y, target_km2, radii_m=(32, 64, 128, 256)):
    """Grow the search radius until a cell carrying >= half the REC2 area is found.

    Measures catchment-area agreement independently of small channel-position offsets (REC2
    lines and our contour-DEM channels can sit 100-300 m apart on low-relief floodplains).
    Returns (model_km2, radius_used_m).
    """
    v = math.nan
    for rad in radii_m:
        v = sample_max(area, transform, x, y, max(1, round(rad / transform.a)))
        if math.isfinite(v) and v >= 0.5 * target_km2:
            return v, rad
    return v, radii_m[-1]


def validate(region: Region, min_area_km2: float = 1.0, radius_m: float = 32.0,
             steep_m_per_km: float = 10.0) -> dict:
    d = region.data_dir()
    with rasterio.open(d / "area_km2.tif") as ds:
        area, transform, b = ds.read(1), ds.transform, ds.bounds
    feats = json.loads((d / "rec2_nztm.geojson").read_text())["features"]
    complete = upstream_complete(feats, (b.left, b.bottom, b.right, b.top))
    rad = max(1, round(radius_m / transform.a))
    rows = []
    for f in feats:
        p = f["properties"]
        if p["HydroID"] not in complete or p["cum_area_km2"] < min_area_km2:
            continue
        line = max(_lines(f["geometry"]), key=len)
        length = sum(math.dist(a, b_) for a, b_ in zip(line, line[1:], strict=False))
        x, y = _point_upstream(line, (p["downcoordX"], p["downcoordY"]), min(100.0, 0.3 * length))
        model = sample_max(area, transform, x, y, rad)
        tol, tol_rad = snap_tolerant(area, transform, x, y, p["cum_area_km2"])
        grad = (p["upElev"] - p["downElev"]) / length * 1000 if length > 0 else math.nan
        rows.append({
            "nzsegment": p["nzsegment"], "name": p.get("name") or "", "order": p["StreamOrde"],
            "gradient_m_per_km": round(grad, 2), "rec2_km2": round(p["cum_area_km2"], 3),
            "model_km2": round(model, 3),
            "rel_err": round((model - p["cum_area_km2"]) / p["cum_area_km2"], 4) if math.isfinite(model) else "",
            "model_tolerant_km2": round(tol, 3), "tolerant_radius_m": tol_rad,
            "rel_err_tolerant": round((tol - p["cum_area_km2"]) / p["cum_area_km2"], 4) if math.isfinite(tol) else "",
        })
    with open(d / "validation_rec2.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["nzsegment"])
        w.writeheader()
        w.writerows(rows)

    def summ(th, key="rel_err", steep=None):
        e = np.array([abs(r[key]) for r in rows if r[key] != "" and r["rec2_km2"] >= th and (
            steep is None or (r["gradient_m_per_km"] >= steep_m_per_km) == steep)])
        if not e.size:
            return {"n": 0}
        return {"n": int(e.size), "median_abs_rel_err": round(float(np.median(e)), 4),
                "within_10pct": round(float((e <= 0.10).mean()), 3),
                "within_25pct": round(float((e <= 0.25).mean()), 3)}

    # Named reference rivers. Coastal mouths sit on dune and estuary flats, where a contour DEM
    # cannot place the channel, so measure at the river's lowest REC2 reach >= inland_m from
    # the sea / nodata.
    inland_m = 500.0
    dist_sea = ndimage.distance_transform_edt(np.isfinite(area)) * transform.a
    to_ll = Transformer.from_crs("EPSG:2193", "EPSG:4326", always_xy=True)
    named = []
    for run in region.known_runs:
        cand = [f for f in feats if f["properties"].get("name") == run.river]
        if not cand:
            named.append({"river": run.river, "status": run.status, "found_in_rec2": False})
            continue

        def inland(f):
            px, py = f["properties"]["downcoordX"], f["properties"]["downcoordY"]
            if not (b.left < px < b.right and b.bottom < py < b.top):
                return False
            r, c = rasterio.transform.rowcol(transform, px, py)
            return dist_sea[r, c] >= inland_m

        ok = [f for f in cand if inland(f)]
        best = max(ok or cand, key=lambda f: f["properties"]["cum_area_km2"])
        p = best["properties"]
        line = max(_lines(best["geometry"]), key=len)
        x, y = _point_upstream(line, (p["downcoordX"], p["downcoordY"]), 100.0)
        model = sample_max(area, transform, x, y, rad)
        if not (math.isfinite(model) and model >= 0.5 * p["cum_area_km2"]):
            model, _ = snap_tolerant(area, transform, x, y, p["cum_area_km2"])
        lon, lat = to_ll.transform(x, y)
        named.append({
            "river": run.river, "status": run.status, "found_in_rec2": True,
            "measured_at_wgs84": [round(lon, 5), round(lat, 5)],
            "rec2_km2": round(p["cum_area_km2"], 2), "model_km2": round(model, 2),
            "rel_err": round((model - p["cum_area_km2"]) / p["cum_area_km2"], 3),
            "upstream_complete": p["HydroID"] in complete,
        })
    summary = {
        "region": region.name, "min_area_km2": min_area_km2, "snap_radius_m": radius_m,
        "steep_threshold_m_per_km": steep_m_per_km,
        "strict": {"all": summ(min_area_km2), "gt_5km2": summ(5.0), "gt_20km2": summ(20.0),
                   "gt_5km2_steep": summ(5.0, steep=True), "gt_5km2_low_relief": summ(5.0, steep=False)},
        "tolerant": {"all": summ(min_area_km2, "rel_err_tolerant"), "gt_5km2": summ(5.0, "rel_err_tolerant"),
                     "gt_5km2_steep": summ(5.0, "rel_err_tolerant", True),
                     "gt_5km2_low_relief": summ(5.0, "rel_err_tolerant", False)},
        "named": named,
    }
    (d / "validation_summary.json").write_text(json.dumps(summary, indent=2))
    return summary
