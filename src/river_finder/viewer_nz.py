"""Region data files for the web app (``river-finder nz viewer``): one set of files per river-guide region, listed in
``regions_index.json`` (which ``webapp.build`` embeds in the page; ``web/README.md``). Choosing a region on the map
fetches:

- ``<slug>.json``: region metadata, REC2 main rivers, reference markers, segment header;
- ``<slug>_seg.txt``: the reach segments, packed (see ``pack_segments``) and base64-encoded (the
  artifact host serves no raw binary), decoded in the browser into the columnar ``Region.segments``;
- ``<slug>_hs.jpg``, ``<slug>_area.png``, ``<slug>_trunc.png``, ``<slug>_catch.png``: hillshade,
  area code, truncation mask and local-catchment labels on one Web-Mercator grid.

The same files come from the 8 m scan (64 km tiles ``x05y07``, ``truncated_core.tif``) and from the 1 m LiDAR rebuild
(32 km tiles ``q10y14``, 10 m local catchments and area code over each tile's box; the truncation mask is painted from
the reaches' ``truncated`` through the local catchments). On the 1 m rebuild the packed reaches also carry
``PACK_1M`` (steps, steepest 100 m, lake, DEM source, LiDAR year).

The packed label status, grade and held-out flag (``sta``, ``gr``, ``lho``) come from every known section, the guide
research included, placed on the national table (``models table``) as the web app's sections are
(``national_labels``, rf-7km).

The three 5 m reference regions are included as ``<id>.json`` files holding their ``viewer.region_payload``
(images as data URIs).

Per-model scores (``<slug>_m_<model>.txt``) come from ``models viewer``, the extras (sections, access, LINZ rivers, ...)
from ``external attach`` and ``web data``. The Leaflet page this module once wrote next to them (``index.html``, from
``assets/viewer_template.html``) was removed in rf-aq6.18.
"""

from __future__ import annotations

import base64
import io
import json
import math
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
from .areacode import CODE_LOG_MIN, CODE_STEP  # shared with nz.encode_area
from .national import FILTER_ORDER, is_1m, merged_gids, nz_dir, region_filters
from .nz import Tile
from .nzscore import region_dir, slug
from .regions import KnownRun, Region, list_regions, load_region

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
# 1 m rebuild only (packed when the segment dict has them; web/README.md): steepest 100 m m/km, bed steps of 1 m or
# more, tallest step m, steps of 5 m or more (waterfall candidates), on a lake, DEM source (0 lidar, 1 mixed, 2 8 m
# fallback, 3 lake fill), LiDAR survey end year (null: none)
PACK_1M = {
    "s100": ("u16", 0.1, 0.0), "stp": ("u16", 1.0, 0.0), "smax": ("u16", 0.1, 0.0), "f1m": ("u16", 1.0, 0.0),
    "lake": ("u8",), "dsrc": ("u8",), "ly": ("u16null", 1.0, 0.0),
}
DEM_SOURCES = ("lidar", "mixed", "8m", "lake")


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

    for k, spec in (PACK | {k: v for k, v in PACK_1M.items() if k in S}).items():
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
    """Tiles (64 km ``x`` or 32 km ``q``) whose box overlaps ``bounds`` and that have their rasters."""
    root = nz_dir() / "tiles"
    out = []
    for p in sorted(root.glob("*/tile.json")):
        try:
            t = Tile.parse(p.parent.name)
        except ValueError:
            continue
        x0, y0, x1, y1 = t.box if t.prefix == "q" else t.core   # 1 m rasters (local catchments) span the box
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
    return Region(name=name, description=f"{name}: {build_label()}", role="search",
                  bbox_wgs84=(0, 0, 0, 0), dem_coarse="", known_runs=runs, root=region_dir(name))


def national_labels(table=None, sections=None) -> dict[str, viewer.ReachLabels]:
    """Label status, grade and held-out flag of the reaches of every river-guide region (``segments_scored.gpkg``
    order), the packed ``sta``, ``gr`` and ``lho``: every known section (known_runs.toml and the guide research in
    ``labels/guide_research.json``, ``models.all_sections``) placed on the national table (``models table``) with
    ``models.map_section``, as ``webdata.write_sections`` places the sections the web app draws (``Region.sections``)
    and the score files flag (bit 2). A section across a region edge labels its reaches on both sides."""
    from .models import national_table, section_labels

    t = national_table() if table is None else table
    status, grade, held, placed = section_labels(t, sections)
    reg = t["region"].to_numpy().astype(str)
    seg = t["seg_id"].to_numpy()
    out = {}
    for name in dict.fromkeys(reg):
        rows = np.flatnonzero(reg == name)
        secs = [m.section for m in placed if (reg[m.rows] == name).any()]
        out[name] = viewer.ReachLabels(status[rows], grade[rows], held[rows], seg[rows], secs)
    return out


def build_label() -> str:
    return "national 1 m LiDAR rebuild" if is_1m() else "national 8 m scan"


def gid_lookup(gids: np.ndarray, values: np.ndarray, gid_map=None):
    """A raster transform for ``_mosaic``: each cell's gid (local catchment raster) -> ``values`` of that gid, 0
    where the gid is not in ``gids`` (or no data); ``None`` for a tile with no hit. ``gid_map``
    (``national.merged_gids``): a cell of a gid the 1 m merge dropped takes the values of the reach that took it."""
    order = np.argsort(gids)
    gid_s, val_s = np.asarray(gids)[order], np.asarray(values)[order]

    def fn(t, src):
        src = src.astype(np.int64)
        if gid_map:
            src = gid_map(src)
        if not len(gid_s):
            return None
        k = np.clip(np.searchsorted(gid_s, src), 0, len(gid_s) - 1)
        hit = (src >= 0) & (gid_s[k] == src)
        out = np.where(hit, val_s[k], 0).astype(val_s.dtype)
        return out if out.any() else None

    return fn


def _tile_stats(stats: list[dict]) -> dict:
    """``valid_cells`` and ``routing_seconds`` of the tiles (8 m ``tile.json``, or the 1 m tile job's)."""
    if stats and "valid_cells" in stats[0]:
        return {"valid_cells": sum(s["valid_cells"] for s in stats), "cellsize_m": 8.0,
                "routing_seconds": round(sum(s["routing_seconds"] for s in stats))}
    return {"valid_cells": int(sum(s.get("cells", 0) - s.get("nodata_cells", 0) for s in stats)),
            "cellsize_m": float(stats[0].get("cell_m", 2.0)) if stats else 2.0,
            "routing_seconds": round(sum(v for s in stats for k, v in s.get("routing", {}).items()
                                         if k.endswith("_s")))}


def region_files(name: str, out_dir: Path, max_px: int = 4000, min_area_km2: float = 2.0,
                 labels: viewer.ReachLabels | None = None) -> dict:
    """One river-guide region's files. ``labels``: its entry of ``national_labels()`` (computed when not given)."""
    sl = slug(name)
    reg = national_region(name)
    d = region_dir(name)
    if labels is None:
        labels = national_labels()[name]
    S = viewer._segments(reg, min_area_km2=min_area_km2, labels=labels)
    import pyogrio

    have = set(pyogrio.read_info(d / "segments_scored.gpkg")["fields"])
    g = gpd.read_file(d / "segments_scored.gpkg", columns=["gid", "down_seg", "area_km2"]
                      + (["truncated"] if "truncated" in have else []))
    b = g.total_bounds
    bounds = (b[0] - 2000, b[1] - 2000, b[2] + 2000, b[3] + 2000)
    tiles = _tiles_for(bounds)
    tr, w, h, merc = _grid(bounds, max_px)
    gmap = merged_gids()    # the 1 m merge's dropped gids (still in local_catchment_core.tif) -> the reach below

    area = _mosaic(tiles, "area_code.tif", tr, w, h, Resampling.max, np.uint8)

    def trunc_code(t, src):
        with rasterio.open(t.dir() / "area_code.tif") as ds:
            code = ds.read(1)
        return np.where(src == 1, code, 0).astype(np.uint8)

    if tiles and all((t.dir() / "truncated_core.tif").exists() for t in tiles):   # the 8 m scan
        tcode = _mosaic(tiles, "truncated_core.tif", tr, w, h, Resampling.max, np.uint8, transform_fn=trunc_code)
        trunc = ((tcode > 0) & (tcode >= area)).astype(np.uint8) * 255
    else:   # the 1 m rebuild: the local catchments of the reaches the merge marks truncated
        tg = g["gid"].to_numpy(np.int64)[g["truncated"].fillna(False).astype(bool).to_numpy()] if "truncated" in g \
            else np.zeros(0, np.int64)
        trunc = _mosaic(tiles, "local_catchment_core.tif", tr, w, h, Resampling.nearest, np.uint8, combine="set",
                        transform_fn=gid_lookup(tg, np.full(len(tg), 255, np.uint8), gmap))
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
    lab = _mosaic(tiles, "local_catchment_core.tif", tr, w, h, Resampling.nearest, np.int32, combine="set",
                  transform_fn=gid_lookup(g["gid"].to_numpy(np.int64), (view + 1).astype(np.int32), gmap))
    rgb = np.stack([(lab >> 16) & 255, (lab >> 8) & 255, lab & 255], axis=-1).astype(np.uint8)

    Image.fromarray(hs, "L").save(out_dir / f"{sl}_hs.jpg", format="JPEG", quality=78, optimize=True)
    _png(area, "L", out_dir / f"{sl}_area.png")
    _png(trunc, "L", out_dir / f"{sl}_trunc.png")
    _png(rgb, "RGB", out_dir / f"{sl}_catch.png")
    blob, head = pack_segments(S)
    (out_dir / f"{sl}_seg.txt").write_text(base64.b64encode(blob).decode("ascii"))

    to_ll = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    lon0, lat0 = to_ll.transform(merc[0], merc[1])
    lon1, lat1 = to_ll.transform(merc[2], merc[3])
    stats = [json.loads((t.dir() / "tile.json").read_text()) for t in tiles]
    rec2 = _rec2_lines(bounds)
    one = is_1m()
    meta = {
        "id": sl, "title": name,
        "description": (f"{name}: national rebuild on the LINZ 1 m LiDAR DEM (routed at 2 m), {len(tiles)} tiles of "
                        f"32 km" if one else f"{name}: national scan on the LINZ 8 m DEM, {len(tiles)} tiles of 64 km"),
        "bounds": [[lat0, lon0], [lat1, lon1]], "merc": list(merc), "width": w, "height": h,
        "pixel_m": round(tr.a, 1),
        "hillshade": f"{sl}_hs.jpg", "area": f"{sl}_area.png", "trunc": f"{sl}_trunc.png", "catch": f"{sl}_catch.png",
        "code": {"log_min": CODE_LOG_MIN, "step": CODE_STEP},
        "rec2": rec2,
        "runs": [{"river": r.river, "status": r.status, "lon": r.outlet_wgs84[0], "lat": r.outlet_wgs84[1],
                  "rec2_km2": r.rec2_cum_area_km2, "model_km2": None} for r in reg.known_runs],
        "stats": _tile_stats(stats),
        "validation": None,
        "segB64": f"{sl}_seg.txt", "segHead": head,
        "segInfo": {k: S[k] for k in ("rivers", "listed", "features", "sections")},
    }
    (out_dir / f"{sl}.json").write_text(json.dumps(meta, separators=(",", ":")))
    group = "River-guide regions (1 m LiDAR)" if one else "River-guide regions (8 m national scan)"
    return {"id": sl, "title": name, "group": group, "src": f"{sl}.json",
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


def build(out_dir: Path, regions: list[str] | None = None, refs: list[str] | None = None,
          max_px: int = 4000) -> Path:
    """Write every region's files and ``regions_index.json`` (returned). ``regions``: river-guide regions (default:
    every scored one); ``refs``: 5 m reference regions (default: every one with ``segments_scored.gpkg``)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    index = []
    names = [n for n in regions or FILTER_ORDER if (region_dir(n) / "segments_scored.gpkg").exists()]
    labels = national_labels() if names else {}   # loads the national table once (freed before the rasters)
    for name in names:
        index.append(region_files(name, out_dir, max_px, labels=labels[name]))
        print(f"viewer: {name} ({index[-1]['segments']} segments)", flush=True)
    for rid in refs if refs is not None else list_regions():
        reg = load_region(rid)
        if not (reg.data_dir() / "segments_scored.gpkg").exists():
            continue
        p = viewer.region_payload(reg)
        (out_dir / f"{rid}.json").write_text(json.dumps(p, separators=(",", ":")))
        index.append({"id": rid, "title": p["title"], "group": "Reference areas (5 m LiDAR)", "src": f"{rid}.json",
                      "bounds": p["bounds"], "segments": len(p["segments"]["g"]) if p["segments"] else 0})
    path = out_dir / "regions_index.json"
    path.write_text(json.dumps(index))
    return path
