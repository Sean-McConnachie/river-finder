"""Find and mosaic LINZ elevation tiles from the public s3://nz-elevation bucket (COG + STAC)."""

from __future__ import annotations

import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.merge import merge

BASE = "https://nz-elevation.s3-ap-southeast-2.amazonaws.com"
NODATA = -9999.0

GDAL_ENV = dict(
    GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tiff,.tif",
    GDAL_HTTP_MAX_RETRY="5",
    GDAL_HTTP_RETRY_DELAY="2",
    GDAL_HTTP_MULTIRANGE="YES",
    GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
    VSI_CACHE="TRUE",
)


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)


def collection_index(collection: str, cache_dir: Path) -> list[dict]:
    """Return [{id, bbox (WGS84), href}] for every item in a collection, cached locally."""
    collection = collection.strip("/")
    cache = cache_dir / (collection.replace("/", "__") + ".json")
    if cache.exists():
        return json.loads(cache.read_text())
    root = f"{BASE}/{collection}"
    coll = _get_json(f"{root}/collection.json")
    item_urls = [f"{root}/{lnk['href'].removeprefix('./')}" for lnk in coll["links"] if lnk["rel"] == "item"]

    def one(url: str) -> dict:
        item = _get_json(url)
        asset = next(iter(item["assets"].values()))
        href = asset["href"]
        if not href.startswith("http"):
            href = url.rsplit("/", 1)[0] + "/" + href.removeprefix("./")
        return {"id": item["id"], "bbox": item["bbox"], "href": href}

    with ThreadPoolExecutor(max_workers=8) as ex:
        items = list(ex.map(one, item_urls))
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(items))
    return items


def tiles_for_bbox(collection: str, bbox_wgs84, cache_dir: Path) -> list[dict]:
    x0, y0, x1, y1 = bbox_wgs84
    return [
        it
        for it in collection_index(collection, cache_dir)
        if not (it["bbox"][2] < x0 or it["bbox"][0] > x1 or it["bbox"][3] < y0 or it["bbox"][1] > y1)
    ]


def mosaic(collection: str, bbox_wgs84, bbox_nztm, res: float, out_path: Path, cache_dir: Path) -> Path:
    """Mosaic all tiles of ``collection`` that intersect the bbox onto a ``res``-metre NZTM grid.

    Reads only the needed windows of the remote COGs (and their overviews when ``res`` is
    coarser than the native resolution).
    """
    tiles = tiles_for_bbox(collection, bbox_wgs84, cache_dir)
    if not tiles:
        raise ValueError(f"no tiles of {collection} intersect {bbox_wgs84}")
    paths = ["/vsicurl/" + t["href"] for t in tiles]
    with rasterio.Env(**GDAL_ENV):
        arr, transform = merge(
            paths, bounds=bbox_nztm, res=res, nodata=NODATA, dtype="float32", resampling=Resampling.bilinear
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    profile = dict(
        driver="GTiff", height=arr.shape[1], width=arr.shape[2], count=1, dtype="float32",
        crs="EPSG:2193", transform=transform, nodata=NODATA,
        compress="deflate", predictor=3, tiled=True, blockxsize=512, blockysize=512,
    )
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(arr[0], 1)
        dst.update_tags(source=f"s3://nz-elevation/{collection}", tiles=",".join(t["id"] for t in tiles))
    return out_path


def read_dem(path: Path):
    with rasterio.open(path) as ds:
        z = ds.read(1).astype(np.float64)
        valid = ds.read_masks(1) > 0
        valid &= np.isfinite(z) & (z > NODATA + 1)
        return z, valid, ds.profile.copy()
