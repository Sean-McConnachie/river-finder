"""Run flow routing for a region and write analysis rasters (NZTM GeoTIFFs)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import rasterio

from .regions import Region
from .routing import route
from .stac import read_dem


def hillshade(z: np.ndarray, valid: np.ndarray, cellsize: float, azimuth=315.0, altitude=45.0) -> np.ndarray:
    zz = np.where(valid, z, np.nanmean(np.where(valid, z, np.nan)))
    gy, gx = np.gradient(zz, cellsize)
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    az, alt = np.radians(360.0 - azimuth + 90.0), np.radians(altitude)
    hs = np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect)
    out = np.clip(hs * 255, 1, 255).astype(np.uint8)
    out[~valid] = 0
    return out


def _write(path: Path, arr: np.ndarray, profile: dict, dtype: str, nodata, **tags):
    p = dict(profile, dtype=dtype, nodata=nodata, count=1, compress="deflate", tiled=True,
             blockxsize=512, blockysize=512, driver="GTiff")
    p.pop("predictor", None)
    with rasterio.open(path, "w", **p) as dst:
        dst.write(arr.astype(dtype), 1)
        if tags:
            dst.update_tags(**tags)


def routing_dem(region: Region) -> str:
    """Prefer the 5 m LiDAR/8 m hybrid when it has been fetched."""
    return "dem_5m.tif" if (region.data_dir() / "dem_5m.tif").exists() else "dem_8m.tif"


def run_routing(region: Region, dem_name: str | None = None) -> dict:
    d = region.data_dir()
    dem_name = dem_name or routing_dem(region)
    z, valid, profile = read_dem(d / dem_name)
    cellsize = profile["transform"].a
    t = time.time()
    out = route(z, valid, cellsize)
    elapsed = time.time() - t
    area_km2 = (out["acc"] / 1e6).astype(np.float32)
    _write(d / "area_km2.tif", np.where(valid, area_km2, np.nan), profile, "float32", np.nan,
           description="Upstream catchment area (km2), D8 on Priority-Flood filled DEM")
    _write(d / "truncated.tif", out["truncated"].astype(np.uint8), profile, "uint8", 255,
           description="1 = upstream area touches the grid edge; catchment may be incomplete")
    _write(d / "fill_depth_m.tif", np.where(valid, out["fill_depth"], np.nan).astype(np.float32), profile,
           "float32", np.nan, description="Depth of depression filling (m)")
    _write(d / "hillshade.tif", hillshade(z, valid, cellsize), profile, "uint8", 0)
    _write(d / "filled.tif", np.where(valid, out["filled"], np.nan).astype(np.float32), profile, "float32", np.nan,
           description="Depression-filled DEM used for flow routing (m)")
    np.save(d / "receivers.npy", out["rec"].astype(np.int32 if out["rec"].size < 2**31 else np.int64))
    stats = {
        "region": region.name, "dem": dem_name, "shape": list(z.shape), "cellsize_m": cellsize,
        "valid_cells": int(valid.sum()), "routing_seconds": round(elapsed, 1),
        "max_area_km2": float(np.nanmax(area_km2)),
        "filled_cells": int(np.nansum(out["fill_depth"] > 0)),
        "max_fill_depth_m": float(np.nanmax(out["fill_depth"])),
    }
    (d / "routing_stats.json").write_text(json.dumps(stats, indent=2))
    return stats
