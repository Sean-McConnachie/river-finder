"""LINZ Data Service (LDS) vector layers for the web app.

Needs an LDS API key in ``$LINZ_LDS_KEY``. It is sent as an ``Authorization: key ...`` header and is
never written to disk, logs or the built pages.

- NZ River Name Lines (Pilot), LDS layer 103632: NZ Topo50 river centrelines (and canals) with LINZ
  river names (``name``, ``macronated``), split into river sections (``river_section_id``). CC BY 4.0.
  https://data.linz.govt.nz/layer/103632-nz-river-name-lines-pilot/

Per viewer region, ``<vid>_linz.json`` (river-finder web data):
``{"names": [...], "types": [...], "lines": [[type, name | -1, river_section_id, [lon, lat, ...]], ...]}``.
Raw features are cached per region in ``data/nz/linz/<vid>.gpkg``.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

import geopandas as gpd
import numpy as np

WFS = "https://data.linz.govt.nz/services/wfs"
RIVER_NAME_LINES = 103632
LAYER_URL = "https://data.linz.govt.nz/layer/103632-nz-river-name-lines-pilot/"
KEEP_TYPES = ("river_cl", "canal_cl")     # drop farm drains (drain_cl)
ATTRIBUTION = "LINZ NZ River Name Lines (Pilot), CC BY 4.0"


def have_key() -> bool:
    return bool(os.environ.get("LINZ_LDS_KEY"))


def _get(params: dict, tries: int = 5) -> dict:
    url = f"{WFS}?{urllib.parse.urlencode(params)}"
    headers = {"Accept": "application/json"}
    key = os.environ.get("LINZ_LDS_KEY")
    if key:
        headers["Authorization"] = f"key {key}"
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=180) as r:
                return json.loads(r.read())
        except Exception as e:  # noqa: BLE001 (network: retry, then re-raise without the URL)
            if k == tries - 1:
                layer = params.get("typeNames")
                raise RuntimeError(f"LDS request failed for {layer}: {type(e).__name__}") from None
            time.sleep(2 * (k + 1))
    return {}


def wfs_features(layer: int, bbox_nztm, page: int = 5000, log=print) -> list[dict]:
    """Every feature of an LDS layer inside an NZTM box (EPSG:2193 coordinates), paged."""
    x0, y0, x1, y1 = bbox_nztm
    out, start = [], 0
    while True:
        d = _get({"service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": f"layer-{layer}",
                  "outputFormat": "application/json", "srsName": "EPSG:2193", "sortBy": "id",
                  "bbox": f"{x0},{y0},{x1},{y1},EPSG:2193", "count": page, "startIndex": start})
        feats = d.get("features", [])
        out.extend(feats)
        if len(feats) < page:
            return out
        start += page
        log(f"  layer {layer}: {len(out)} features so far")


def river_lines(bbox_nztm, log=print) -> gpd.GeoDataFrame:
    feats = wfs_features(RIVER_NAME_LINES, bbox_nztm, log=log)
    if not feats:
        return gpd.GeoDataFrame({"feat_type": [], "name": [], "river_section_id": []}, geometry=[], crs=2193)
    g = gpd.GeoDataFrame.from_features(feats, crs="EPSG:2193")
    return g[["river_section_id", "feat_type", "name", "macronated", "geometry"]]


def compact(g: gpd.GeoDataFrame, simplify_m: float = 4.0) -> dict:
    """The map's JSON: named lines merged per name and type (labels need long lines), WGS84, ~1 m."""
    import shapely
    from pyproj import Transformer

    to_ll = Transformer.from_crs("EPSG:2193", "EPSG:4326", always_xy=True)
    g = g[g["feat_type"].isin(KEEP_TYPES)].copy()
    g["name"] = g["name"].fillna("")
    names: list[str] = []
    nidx: dict[str, int] = {}
    lines = []
    for (ftype, name), part in g.groupby(["feat_type", "name"], sort=False):
        if name:
            merged = shapely.line_merge(shapely.union_all(part.geometry.to_numpy()))
            geoms = [(x, int(part["river_section_id"].iloc[0])) for x in getattr(merged, "geoms", [merged])]
        else:
            geoms = list(zip(part.geometry, part["river_section_id"].astype(int), strict=True))
        for geom, sid in geoms:
            if geom is None or geom.is_empty or geom.geom_type != "LineString":
                continue
            geom = geom.simplify(simplify_m)
            x, y = to_ll.transform(*np.asarray(geom.coords)[:, :2].T)
            if name and name not in nidx:
                nidx[name] = len(names)
                names.append(name)
            lines.append([KEEP_TYPES.index(ftype), nidx[name] if name else -1, sid,
                          np.round(np.column_stack([x, y]).ravel(), 5).tolist()])
    return {"names": names, "types": list(KEEP_TYPES), "lines": lines, "source": LAYER_URL, "attribution": ATTRIBUTION}


def write_linz(data_dir: Path, refresh: bool = False, log=print) -> dict:
    """``<vid>_linz.json`` for every region in ``regions_index.json``; skipped without a key."""
    from pyproj import Transformer

    from .national import nz_dir

    cache = nz_dir() / "linz"
    cache.mkdir(parents=True, exist_ok=True)
    to_m = Transformer.from_crs("EPSG:4326", "EPSG:2193", always_xy=True)
    counts = {}
    for e in json.loads((data_dir / "regions_index.json").read_text()):
        raw = cache / f"{e['id']}.gpkg"
        if refresh or not raw.exists():
            if not have_key():
                log(f"linz: {e['id']} skipped (set LINZ_LDS_KEY to fetch LINZ river lines)")
                continue
            (la0, lo0), (la1, lo1) = e["bounds"]
            g = river_lines(to_m.transform_bounds(lo0, la0, lo1, la1, densify_pts=21), log=log)
            g.to_file(raw, layer="rivers", driver="GPKG")
        g = gpd.read_file(raw)
        out = compact(g)
        (data_dir / f"{e['id']}_linz.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
        counts[e["id"]] = len(out["lines"])
        log(f"linz: {e['id']} {len(g)} LINZ lines -> {len(out['lines'])} map lines, {len(out['names'])} names")
    return counts
