"""Multi-file browser viewer for the national run: one set of files per river-guide region.

The page itself (``index.html``) only holds a small index. Choosing a region fetches:

- ``<slug>.json``: region metadata, REC2 main rivers, reference markers, segment header;
- ``<slug>_seg.bin``: the reach segments, packed (see ``pack_segments``), decoded in the browser
  into the same columnar object the single-file viewer embeds;
- ``<slug>_hs.jpg``, ``<slug>_area.png``, ``<slug>_trunc.png``, ``<slug>_catch.png``: hillshade,
  area code, truncation mask and local-catchment labels on one Web-Mercator grid.

The three 5 m reference regions are included as ``<id>.json`` files holding their full
single-file payload (images as data URIs).
"""

from __future__ import annotations

import io
import json
import math
from dataclasses import replace
from importlib import resources
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from PIL import Image
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.warp import reproject

from . import viewer
from .labels import load_sections
from .national import FILTER_ORDER, nz_dir, region_filters
from .nz import Tile
from .nzscore import region_dir, slug
from .regions import KnownRun, Region, list_regions, load_region

CODE_LOG_MIN = math.log10(0.05)
CODE_STEP = (math.log10(30000.0) - CODE_LOG_MIN) / 253.0

# --- segment packing ---------------------------------------------------------------------------
# kind: "u16" linear (q * scale + off), "u16log" (10 ** (q * scale + off)), "u16null" (0 = null),
#       "i32", "u8"
PACK = {
    "d": ("i32",), "u": ("i32",), "r": ("i32",),
    "len": ("u16", 1.0, 0.0),
    "g": ("u16", 0.1, 0.0), "s50": ("u16", 0.1, 0.0), "gorge": ("u16", 0.1, 0.0),
    "zt": ("u16", 0.1, -100.0), "zb": ("u16", 0.1, -100.0), "head": ("u16", 0.1, -100.0),
    "mel": ("u16", 1e-4, 0.0), "hw": ("u16", 1e-3, 0.0),
    "reg": ("u16", 0.01, 0.0), "sig": ("u16", 0.01, 0.0),
    "a": ("u16log", 2e-4, -3.0), "maf": ("u16log", 2e-4, -3.0),
    "ksn": ("u16log", 2e-4, -3.0), "pow": ("u16log", 2e-4, -3.0),
    "gr": ("u16null", 0.01, 0.0),
    "hard": ("u8",), "sta": ("u8",), "lho": ("u8",), "lab": ("u8",), "ho": ("u8",),
}


def pack_segments(S: dict) -> tuple[bytes, dict]:
    """Pack the viewer's columnar segment dict into one little-endian buffer plus a header."""
    n = len(S["g"])
    buf = io.BytesIO()
    fields = []

    def put(arr: np.ndarray) -> int:
        pad = (-buf.tell()) % 4
        buf.write(b"\0" * pad)
        off = buf.tell()
        buf.write(np.ascontiguousarray(arr).tobytes())
        return off

    for k, spec in PACK.items():
        v = S[k]
        kind = spec[0]
        if kind == "i32":
            arr = np.asarray(v, dtype="<i4")
        elif kind == "u8":
            arr = np.asarray(v, dtype=np.uint8)
        elif kind == "u16":
            x = np.nan_to_num(np.asarray(v, dtype=float), nan=spec[2])
            arr = np.clip(np.round((x - spec[2]) / spec[1]), 0, 65535).astype("<u2")
        elif kind == "u16log":
            x = np.log10(np.maximum(np.nan_to_num(np.asarray(v, dtype=float), nan=0.0), 10 ** spec[2]))
            arr = np.clip(np.round((x - spec[2]) / spec[1]), 0, 65535).astype("<u2")
        else:  # u16null
            x = np.array([np.nan if e is None else e for e in v], dtype=float)
            arr = np.where(np.isfinite(x), np.clip(np.round((np.nan_to_num(x) - spec[2]) / spec[1]) + 1, 1, 65535),
                           0).astype("<u2")
        fields.append({"k": k, "kind": kind, "off": put(arr), "scale": spec[1] if len(spec) > 1 else 1,
                       "add": spec[2] if len(spec) > 2 else 0})
    nf = len(S["features"])
    z = np.clip(np.round(np.asarray(S["z"], dtype=float).reshape(n, nf) * 10), -127, 127).astype(np.int8)
    z_off = put(z.ravel())
    counts = np.array([len(c) for c in S["c"]], dtype="<u2")
    pts = [np.round(np.asarray(c, dtype=float) * 1e5).astype(np.int64) for c in S["c"]]
    first = np.array([p[0] for p in pts], dtype="<i4").reshape(-1, 2) if n else np.zeros((0, 2), "<i4")
    deltas = np.concatenate([np.diff(p, axis=0) for p in pts]) if n else np.zeros((0, 2), np.int64)
    if len(deltas) and np.abs(deltas).max() > 32767:
        raise ValueError("segment vertex step too long for int16 deltas")
    c_off = put(counts)
    f_off = put(first.ravel())
    d_off = put(deltas.astype("<i2").ravel())
    head = {"n": n, "fields": fields, "z": {"off": z_off, "nf": nf, "scale": 0.1},
            "coords": {"count": c_off, "first": f_off, "delta": d_off, "scale": 1e-5, "ndelta": int(len(deltas))}}
    return buf.getvalue(), head


# --- rasters ----------------------------------------------------------------------------------
def _grid(bounds_nztm, max_px: int):
    to_m = Transformer.from_crs("EPSG:2193", "EPSG:3857", always_xy=True)
    x0, y0, x1, y1 = to_m.transform_bounds(*bounds_nztm, densify_pts=21)
    px = max(x1 - x0, y1 - y0) / max_px
    w, h = int(math.ceil((x1 - x0) / px)), int(math.ceil((y1 - y0) / px))
    return from_origin(x0, y1, px, px), w, h, (x0, y1 - h * px, x0 + w * px, y1)


def _tiles_for(bounds) -> list[Tile]:
    root = nz_dir() / "tiles"
    out = []
    for p in sorted(root.glob("*/tile.json")):
        t = Tile.parse(p.parent.name)
        x0, y0, x1, y1 = t.core
        overlaps = x1 > bounds[0] and x0 < bounds[2] and y1 > bounds[1] and y0 < bounds[3]
        if overlaps and (p.parent / "area_code.tif").exists():
            out.append(t)
    return out


def _mosaic(tiles, name, dst_tr, w, h, resampling, dtype, combine="max", transform_fn=None):
    out = np.zeros((h, w), dtype=dtype)
    for t in tiles:
        with rasterio.open(t.dir() / name) as ds:
            src = ds.read(1)
            src_tr, crs = ds.transform, ds.crs
        if transform_fn is not None:
            src = transform_fn(t, src)
            if src is None:
                continue
        tmp = np.zeros((h, w), dtype=src.dtype)
        reproject(src, tmp, src_transform=src_tr, src_crs=crs, dst_transform=dst_tr, dst_crs="EPSG:3857",
                  resampling=resampling, src_nodata=0, dst_nodata=0)
        if combine == "max":
            np.maximum(out, tmp.astype(dtype), out=out)
        else:
            m = tmp != 0
            out[m] = tmp[m]
    return out


def _png(arr: np.ndarray, mode: str, path: Path) -> None:
    Image.fromarray(arr, mode).save(path, format="PNG", optimize=True)


# --- one region --------------------------------------------------------------------------------
def national_region(name: str) -> Region:
    """Pseudo-Region for a river-guide region, carrying the reference runs that lie inside it."""
    poly = region_filters().set_index("region").loc[name, "geometry"]
    to_nztm = Transformer.from_crs("EPSG:4326", "EPSG:2193", always_xy=True)
    from shapely.geometry import Point

    runs = []
    for r in (load_region(n) for n in list_regions()):
        for k in r.known_runs:
            if all(map(math.isfinite, k.outlet_wgs84)) and poly.contains(Point(*to_nztm.transform(*k.outlet_wgs84))):
                runs.append(KnownRun(k.river, k.status, k.outlet_wgs84, k.rec2_cum_area_km2))
    return Region(name=name, description=f"{name}: national 8 m scan", role="search",
                  bbox_wgs84=(0, 0, 0, 0), dem_coarse="", known_runs=runs, root=region_dir(name))


def national_sections(name: str):
    """Label sections that apply to a river-guide region, limited to their reference box."""
    poly = region_filters().set_index("region").loc[name, "geometry"]
    to_nztm = Transformer.from_crs("EPSG:4326", "EPSG:2193", always_xy=True)
    from shapely.geometry import Point

    refs = {n: load_region(n) for n in list_regions()}
    out = []
    for s in load_sections():
        ref = refs.get(s.region)
        if ref is None:
            continue
        x0, y0, x1, y1 = ref.bbox_wgs84
        if poly.intersects(Point(*to_nztm.transform((x0 + x1) / 2, (y0 + y1) / 2)).buffer(30_000)):
            out.append(replace(s, region=name, bbox=ref.bbox_wgs84))
    return out


def region_files(name: str, out_dir: Path, max_px: int = 4000, min_area_km2: float = 2.0) -> dict:
    sl = slug(name)
    reg = national_region(name)
    d = region_dir(name)
    S = viewer._segments(reg, min_area_km2=min_area_km2, sections=national_sections(name))
    g = gpd.read_file(d / "segments_scored.gpkg", columns=["gid", "down_seg", "area_km2"])
    b = g.total_bounds
    bounds = (b[0] - 2000, b[1] - 2000, b[2] + 2000, b[3] + 2000)
    tiles = _tiles_for(bounds)
    tr, w, h, merc = _grid(bounds, max_px)

    area = _mosaic(tiles, "area_code.tif", tr, w, h, Resampling.max, np.uint8)

    def trunc_code(t, src):
        with rasterio.open(t.dir() / "area_code.tif") as ds:
            code = ds.read(1)
        return np.where(src == 1, code, 0).astype(np.uint8)

    tcode = _mosaic(tiles, "truncated_core.tif", tr, w, h, Resampling.max, np.uint8, transform_fn=trunc_code)
    trunc = ((tcode > 0) & (tcode >= area)).astype(np.uint8) * 255
    hs = _mosaic(tiles, "hillshade_24m.tif", tr, w, h, Resampling.average, np.uint8, combine="set")

    # catchment labels: global segment id -> viewer index (+1), small segments folded downstream
    keep = (g["area_km2"] >= min_area_km2).to_numpy()
    view = np.full(len(g), -1, dtype=np.int64)
    view[np.flatnonzero(keep)] = np.arange(int(keep.sum()))
    down = g["down_seg"].to_numpy()
    for i in range(len(g)):
        if view[i] >= 0:
            continue
        chain, cur = [], i
        while cur >= 0 and view[cur] < 0 and len(chain) < 100000:
            chain.append(cur)
            cur = down[cur]
        view[chain] = view[cur] if cur >= 0 else -1
    gid = g["gid"].to_numpy()
    order = np.argsort(gid)
    gid_s, view_s = gid[order], view[order] + 1

    def labels(t, src):
        src = src.astype(np.int64)
        k = np.clip(np.searchsorted(gid_s, src), 0, len(gid_s) - 1)
        hit = (src >= 0) & (gid_s[k] == src)
        out = np.where(hit, view_s[k], 0).astype(np.int32)
        return out if out.any() else None

    lab = _mosaic(tiles, "local_catchment_core.tif", tr, w, h, Resampling.nearest, np.int32, combine="set",
                  transform_fn=labels)
    rgb = np.stack([(lab >> 16) & 255, (lab >> 8) & 255, lab & 255], axis=-1).astype(np.uint8)

    Image.fromarray(hs, "L").save(out_dir / f"{sl}_hs.jpg", format="JPEG", quality=78, optimize=True)
    _png(area, "L", out_dir / f"{sl}_area.png")
    _png(trunc, "L", out_dir / f"{sl}_trunc.png")
    _png(rgb, "RGB", out_dir / f"{sl}_catch.png")
    blob, head = pack_segments(S)
    (out_dir / f"{sl}_seg.bin").write_bytes(blob)

    to_ll = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    lon0, lat0 = to_ll.transform(merc[0], merc[1])
    lon1, lat1 = to_ll.transform(merc[2], merc[3])
    stats = [json.loads((t.dir() / "tile.json").read_text()) for t in tiles]
    rec2 = _rec2_lines(bounds)
    meta = {
        "id": sl, "title": name,
        "description": f"{name}: national scan on the LINZ 8 m DEM, {len(tiles)} tiles of 64 km",
        "bounds": [[lat0, lon0], [lat1, lon1]], "merc": list(merc), "width": w, "height": h,
        "pixel_m": round(tr.a, 1),
        "hillshade": f"{sl}_hs.jpg", "area": f"{sl}_area.png", "trunc": f"{sl}_trunc.png", "catch": f"{sl}_catch.png",
        "code": {"log_min": CODE_LOG_MIN, "step": CODE_STEP},
        "rec2": rec2,
        "runs": [{"river": r.river, "status": r.status, "lon": r.outlet_wgs84[0], "lat": r.outlet_wgs84[1],
                  "rec2_km2": r.rec2_cum_area_km2, "model_km2": None} for r in reg.known_runs],
        "stats": {"valid_cells": sum(s["valid_cells"] for s in stats), "cellsize_m": 8.0,
                  "routing_seconds": round(sum(s["routing_seconds"] for s in stats))},
        "validation": None,
        "segBin": f"{sl}_seg.bin", "segHead": head,
        "segInfo": {k: S[k] for k in ("rivers", "listed", "features", "sections")},
    }
    (out_dir / f"{sl}.json").write_text(json.dumps(meta, separators=(",", ":")))
    return {"id": sl, "title": name, "group": "River-guide regions (8 m national scan)", "src": f"{sl}.json",
            "bounds": meta["bounds"], "segments": head["n"]}


_REC2 = None


def _rec2_lines(bounds, min_order: int = 4) -> list[dict]:
    global _REC2
    if _REC2 is None:
        r = gpd.read_file(nz_dir() / "rec2.gpkg", columns=["name", "StreamOrde", "cum_area_km2", "downcoordX",
                                                           "downcoordY"])
        _REC2 = r[r["StreamOrde"] >= min_order]
    x0, y0, x1, y1 = bounds
    r = _REC2.cx[x0:x1, y0:y1].copy()
    r["geometry"] = r.geometry.simplify(20.0)
    ll = r.to_crs(4326)
    to_ll = Transformer.from_crs("EPSG:2193", "EPSG:4326", always_xy=True)
    dlon, dlat = to_ll.transform(r["downcoordX"].to_numpy(), r["downcoordY"].to_numpy())
    out = []
    for geom, n, o, a, dx, dy in zip(ll.geometry, r["name"], r["StreamOrde"], r["cum_area_km2"], dlon, dlat,
                                     strict=True):
        lines = [geom] if geom.geom_type == "LineString" else list(geom.geoms)
        k = math.cos(math.radians(dy))
        cs = []
        for ln in lines:
            c = list(ln.coords)
            if ((c[0][0] - dx) * k) ** 2 + (c[0][1] - dy) ** 2 < ((c[-1][0] - dx) * k) ** 2 + (c[-1][1] - dy) ** 2:
                c = c[::-1]
            cs.append([[round(x, 4), round(y, 4)] for x, y in c])
        out.append({"n": n or "", "o": int(o), "a": round(float(a), 1), "c": cs})
    return out


def build(out_dir: Path, regions: list[str] | None = None, refs: list[str] | None = None, max_px: int = 4000):
    out_dir.mkdir(parents=True, exist_ok=True)
    index = []
    for name in regions or FILTER_ORDER:
        if (region_dir(name) / "segments_scored.gpkg").exists():
            index.append(region_files(name, out_dir, max_px))
            print(f"viewer: {name} ({index[-1]['segments']} segments)", flush=True)
    for rid in refs if refs is not None else list_regions():
        reg = load_region(rid)
        if not (reg.data_dir() / "segments_scored.gpkg").exists():
            continue
        p = viewer.region_payload(reg)
        (out_dir / f"{rid}.json").write_text(json.dumps(p, separators=(",", ":")))
        index.append({"id": rid, "title": p["title"], "group": "Reference areas (5 m LiDAR)", "src": f"{rid}.json",
                      "bounds": p["bounds"], "segments": len(p["segments"]["g"]) if p["segments"] else 0})
    model = None
    mp = nz_dir() / "model.json"
    if mp.exists():
        from .scoring import FEATURES

        m = json.loads(mp.read_text())
        model = {"coef": m["coef"], "holdout": m["holdout"], "lorocv": m["lorocv"], "run_params": m["run_params"],
                 "descriptions": m["feature_descriptions"],
                 "default_weights": {k: v[1] for k, v in FEATURES.items()}, "ha_defaults": m.get("ha_defaults", {})}
    tpl = resources.files("river_finder").joinpath("assets/viewer_template.html").read_text()
    css = resources.files("river_finder").joinpath("assets/leaflet-1.9.4.css").read_text()
    data = json.dumps({"regions": index, "model": model}, separators=(",", ":")).replace("</", "<\\/")
    page = tpl.replace("/*%%LEAFLET_CSS%%*/", css).replace("%%DATA%%", data)
    (out_dir / "index_fragment.html").write_text(page)  # for a claude.ai artifact (it adds the skeleton)
    (out_dir / "index.html").write_text(
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">\n"
        "</head>\n<body>\n" + page + "\n</body>\n</html>\n")
    return out_dir / "index.html"
