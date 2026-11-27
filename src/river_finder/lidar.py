"""1 m LiDAR DEM coverage: where the LINZ LiDAR DEM exists, which survey and capture year, and how much of each
reach and catchment it covers (bead rf-lxa.1, ``docs/lidar-coverage.md``).

Source: LINZ's public elevation bucket ``s3://nz-elevation`` (read over HTTPS, no key), a STAC catalog with one
``dem_1m`` collection per LiDAR survey and the national mosaic ``new-zealand/new-zealand/dem_1m/2193``, which the
reference areas read today (``stac.py``). The mosaic's items are Topo50 sheets: an item's box is not proof of data.
What is proof is the mosaic's ``capture-dates.geojson`` asset: one footprint per survey, *the area that survey
supplies to the mosaic*, nodata excluded, simplified. Those footprints are disjoint (a newer survey replaces an
older one where both exist), so they form the coverage layer directly. Each survey collection also has its own
``capture-area.geojson``; surveys not (yet) in the mosaic (drafts, new flights, replaced surveys) are added where
they cover ground the mosaic does not, flagged ``in_mosaic = False`` (about 200 km2 in 2026-11: not worth reading).

The STAC has no point density or vertical accuracy (only producer, licensor, flight dates and lifecycle).

Per reach, the reach line is sampled every ``STEP_M`` metres (``aerial.sample_points``) and each point takes the
footprint containing it: ``lidar_share`` is the share of the reach under the mosaic, ``new_share`` the share under
surveys not yet in it, ``lidar_code`` the survey over most of the reach (code into the layer, -1 none). Per local
catchment (``data/nz/tiles/<tile>/local_catchment_core.tif``, 8 m), the cells under each are counted, and
accumulated down the national network: ``catch_lidar_share`` is the share of a reach's whole upstream catchment
under the mosaic (or a new survey: ``catch_any_share``).

Cache and outputs (``data/analysis/lidar/`` by default; gitignored):

- ``stac/``: ``catalog.json``, ``surveys.json`` (one row per ``dem_1m`` survey), ``mosaic_capture_dates.geojson``,
  ``capture/<region>__<slug>.geojson`` (survey footprints);
- ``lidar_coverage.gpkg``: layer ``coverage`` (one row per survey area: code, slug, title, region, flight dates,
  year, producer, licensor, lifecycle, in_mosaic, km2) and layer ``gaps`` (1 km cells of land without LiDAR,
  dissolved into connected gap areas);
- ``reach_coverage.pkl``: one row per national reach, in ``data/nz/segments_table.pkl`` order (key ``gid``), with
  the fields above, the survey slug and years (``lidar_slug``, ``lidar_year``, ``lidar_year_start``), the local and
  upstream catchment shares and ``source`` (``source_flag``: ``lidar`` / ``mixed`` / ``8m``);
  ``reach_coverage_raw.pkl`` and ``catchment_cells.npz`` cache the tile pass (about 6 min for 114 tiles);
- ``gap_grid.npz``: land and no-LiDAR 8 m cell counts per 1 km cell (national grid of ``nz.py``);
- ``summary.json``: national and per-region coverage, capture years, the largest gaps, guide sections without LiDAR.

``river-finder lidar fetch | coverage`` rebuilds them (``--refresh`` fetches the STAC again and redoes the tile
pass). Only the collection documents and footprints are read (about 15 MB), never the item JSON or the DEM.
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from .aerial import _abs, _get, _local_date, footprint_from_geojson, sample_points
from .regions import REPO_ROOT
from .stac import BASE

MOSAIC = "new-zealand/new-zealand/dem_1m/2193"
MOSAIC_SLUG = "new-zealand"
STEP_M = 25.0          # sample spacing along a reach line
MIN_NEW_KM2 = 0.5      # a survey not in the mosaic is kept where it adds at least this much ground
GAP_CELL_M = 1000.0    # gap grid cell
GAP_FRAC = 0.5         # a gap cell: at least this share of its land without LiDAR
SEG_BASE = 1_000_000   # as nz.SEG_BASE: global segment id = tile index * SEG_BASE + local id


def default_dir() -> Path:
    return REPO_ROOT / "data" / "analysis" / "lidar"


# ---------------------------------------------------------------------------- STAC
def _producers(coll: dict, role: str) -> str:
    return ", ".join(p["name"] for p in coll.get("providers", []) if role in p.get("roles", []))


def survey_record(coll: dict, url: str, in_mosaic: set[str] | None = None) -> dict | None:
    """One row of the survey table from a ``dem_1m`` STAC collection (None for anything else)."""
    if coll.get("linz:geospatial_category") != "dem" or "/dem_1m/" not in url:
        return None
    start, end = (coll["extent"]["temporal"]["interval"][0] + [None, None])[:2]
    area = (coll.get("assets") or {}).get("capture_area")
    slug = coll.get("linz:slug") or url.split("/")[-4]
    region = coll.get("linz:region") or url.split("/")[-5]
    title = coll.get("title", slug)
    n_items = sum(1 for ln in coll.get("links", []) if ln.get("rel") == "item")
    s, e = _local_date(start), _local_date(end or start)
    return {"slug": slug, "region": region, "key": f"{region}__{slug}", "title": title,
            "start": s, "end": e, "year_start": int(s[:4]) if s else None, "year": int(e[:4]) if e else None,
            "producer": _producers(coll, "producer"), "licensor": _producers(coll, "licensor"),
            "lifecycle": coll.get("linz:lifecycle", ""), "draft": bool(re.search(r"-\s*Draft\s*$", title)),
            "gsd": float(coll.get("gsd", 1.0)), "items": n_items, "mosaic": slug == MOSAIC_SLUG,
            "in_mosaic": slug == MOSAIC_SLUG or (in_mosaic is not None and f"{region}/{slug}" in in_mosaic),
            "collection": url, "capture": _abs(url, area["href"]) if area else None,
            "bbox": coll["extent"]["spatial"]["bbox"][0]}


def mosaic_sources(capture_dates: dict) -> set[str]:
    """``{"region/slug"}`` of the surveys the national mosaic draws from (its ``capture-dates`` ``source``)."""
    out = set()
    for f in capture_dates.get("features", []):
        src = str(f["properties"].get("source", ""))
        m = re.search(r"s3://nz-elevation/([^/]+)/([^/]+)/dem_1m/", src)
        if m:
            out.add(f"{m.group(1)}/{m.group(2)}")
    return out


def fetch(out: Path | None = None, refresh: bool = False, log=print, workers: int = 8) -> list[dict]:
    """Survey table, survey footprints and the mosaic's capture dates into ``<out>/stac/`` (cached)."""
    d = (out or default_dir()) / "stac"
    (d / "capture").mkdir(parents=True, exist_ok=True)
    p = d / "surveys.json"
    cd = d / "mosaic_capture_dates.geojson"
    if refresh or not cd.exists():
        cd.write_bytes(_get(f"{BASE}/{MOSAIC}/capture-dates.geojson"))
    in_mosaic = mosaic_sources(json.loads(cd.read_text()))
    if refresh or not p.exists():
        cat = json.loads(_get(f"{BASE}/catalog.json"))
        (d / "catalog.json").write_text(json.dumps(cat))
        kids = [_abs(f"{BASE}/catalog.json", ln["href"]) for ln in cat["links"]
                if ln["rel"] == "child" and "/dem_1m/" in ln["href"]]
        log(f"lidar: {len(kids)} dem_1m collections in s3://nz-elevation")

        def one(url):
            try:
                return survey_record(json.loads(_get(url)), url, in_mosaic)
            except Exception as e:  # noqa: BLE001 (one broken collection should not stop the table)
                log(f"lidar: {url} skipped ({type(e).__name__})")
                return None

        with ThreadPoolExecutor(workers) as ex:
            rows = [r for r in ex.map(one, kids) if r]
        rows.sort(key=lambda r: r["key"])
        p.write_text(json.dumps(rows, ensure_ascii=False, indent=0))
    rows = json.loads(p.read_text())

    def grab(r):
        f = d / "capture" / f"{r['key']}.geojson"
        if r.get("capture") and (refresh or not f.exists()):
            f.write_bytes(_get(r["capture"]))

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(grab, [r for r in rows if not r["mosaic"]]))
    log(f"lidar: {len(rows)} surveys ({sum(r['in_mosaic'] and not r['mosaic'] for r in rows)} in the mosaic), "
        f"footprints in {d / 'capture'}")
    return rows


# ---------------------------------------------------------------------------- coverage layer
LAYER_COLS = ["code", "slug", "region", "title", "start", "end", "year_start", "year", "producer", "licensor",
              "lifecycle", "draft", "in_mosaic", "km2"]


def _valid(g):
    import shapely

    g = shapely.make_valid(g)
    if g.geom_type == "GeometryCollection":
        g = shapely.union_all([p for p in shapely.get_parts(g) if p.geom_type in ("Polygon", "MultiPolygon")])
    return g


def build_layer(rows: list[dict], capture_dates: dict, footprints: dict, min_new_km2: float = MIN_NEW_KM2):
    """The coverage layer: the mosaic's survey areas (disjoint, from ``capture_dates``), then the surveys not in
    the mosaic, newest first, each where it adds ground nothing before it covers (``footprints``: ``{key:
    geometry in EPSG:2193}``). Returns a GeoDataFrame in EPSG:2193 with ``LAYER_COLS``; ``code`` = row number."""
    import geopandas as gpd
    import shapely

    by_key = {r["key"]: r for r in rows}
    recs, geoms = [], []
    for f in capture_dates["features"]:
        pr = f["properties"]
        m = re.search(r"s3://nz-elevation/([^/]+)/([^/]+)/dem_1m/", str(pr.get("source", "")))
        key = f"{m.group(1)}__{m.group(2)}" if m else pr.get("linz:slug", "")
        r = by_key.get(key, {})
        g = footprint_from_geojson({"type": "Feature", "geometry": f["geometry"]}, None)
        s, e = pr.get("flown_from", r.get("start", "")), pr.get("flown_to", r.get("end", ""))
        recs.append({"slug": pr.get("linz:slug", r.get("slug", "")), "region": pr.get("linz:region", ""),
                     "title": pr.get("title", r.get("title", "")), "start": s, "end": e,
                     "year_start": int(s[:4]) if s else None, "year": int(e[:4]) if e else None,
                     "producer": pr.get("producer", ""), "licensor": pr.get("licensor", ""),
                     "lifecycle": pr.get("linz:lifecycle", ""), "draft": bool(r.get("draft", False)),
                     "in_mosaic": True})
        geoms.append(_valid(g))
    have = shapely.union_all(geoms) if geoms else shapely.Polygon()
    extra = sorted((r for r in rows if not r["in_mosaic"] and r["key"] in footprints),
                   key=lambda r: (r["end"], r["start"]), reverse=True)
    for r in extra:
        g = footprints[r["key"]]
        if g is None or g.is_empty:
            continue
        add = _valid(shapely.difference(_valid(g), have))
        if add.is_empty or add.area / 1e6 < min_new_km2:
            continue
        recs.append({k: r[k] for k in ("slug", "region", "title", "start", "end", "year_start", "year", "producer",
                                       "licensor", "lifecycle", "draft")} | {"in_mosaic": False})
        geoms.append(add)
        have = shapely.union(have, add)
    gdf = gpd.GeoDataFrame(recs, geometry=geoms, crs="EPSG:2193")
    gdf["km2"] = gdf.geometry.area / 1e6
    gdf.insert(0, "code", np.arange(len(gdf), dtype=np.int64))
    return gdf[LAYER_COLS + ["geometry"]]


def load_footprints(rows: list[dict], out: Path | None = None) -> dict:
    d = (out or default_dir()) / "stac" / "capture"
    fps = {}
    for r in rows:
        f = d / f"{r['key']}.geojson"
        if not r["mosaic"] and f.exists():
            fps[r["key"]] = footprint_from_geojson(json.loads(f.read_text()), None)
    return fps


def coverage_layer(out: Path | None = None, refresh: bool = False, log=print):
    """The cached coverage layer (``lidar_coverage.gpkg``, layer ``coverage``), built when missing."""
    import geopandas as gpd

    out = out or default_dir()
    p = out / "lidar_coverage.gpkg"
    if p.exists() and not refresh:
        return gpd.read_file(p, layer="coverage")
    rows = fetch(out, refresh=refresh, log=log)
    cd = json.loads((out / "stac" / "mosaic_capture_dates.geojson").read_text())
    layer = build_layer(rows, cd, load_footprints(rows, out))
    p.unlink(missing_ok=True)
    layer.to_file(p, layer="coverage", driver="GPKG")
    log(f"lidar: coverage layer {len(layer)} survey areas ({int(layer['in_mosaic'].sum())} from the mosaic), "
        f"{layer['km2'].sum():.0f} km2 -> {p}")
    return layer


# ---------------------------------------------------------------------------- per reach
def point_codes(x: np.ndarray, y: np.ndarray, geoms: list) -> np.ndarray:
    """For each point, the index of the first geometry containing it (-1: none)."""
    import shapely

    top = np.full(len(x), -1, np.int64)
    for k, g in enumerate(geoms):
        if g is None or g.is_empty:
            continue
        x0, y0, x1, y1 = g.bounds
        todo = np.flatnonzero((top < 0) & (x >= x0) & (x <= x1) & (y >= y0) & (y <= y1))
        if not len(todo):
            continue
        shapely.prepare(g)
        top[todo[shapely.contains_xy(g, x[todo], y[todo])]] = k
    return top


def line_coverage(lines, geoms: list, in_mosaic: np.ndarray, step_m: float = STEP_M) -> dict:
    """Per line (``[(n, 2)]`` NZTM arrays): share under the mosaic, under surveys not in it, and the code of the
    survey over most of the line (-1: none)."""
    x, y, idx, _ = sample_points(lines, step_m)
    n, ns = len(lines), len(geoms)
    top = point_codes(x, y, geoms) if len(x) else np.zeros(0, np.int64)
    counts = np.zeros((n, ns + 1), np.int64)
    np.add.at(counts, (idx, top + 1), 1)
    total = np.maximum(counts.sum(axis=1), 1)
    mos = counts[:, 1:][:, in_mosaic].sum(axis=1)
    new = counts[:, 1:][:, ~in_mosaic].sum(axis=1)
    hit = mos + new
    code = np.where(hit > 0, np.argmax(counts[:, 1:], axis=1) if ns else 0, -1)
    return {"lidar_share": mos / total, "new_share": new / total, "lidar_code": code.astype(np.int64)}


def _tile_index(tile_id: str) -> int:
    """``gid // SEG_BASE`` of a tile's reaches: 64 km ``x05y07`` (8 m scan) or 32 km ``q10y14`` (1 m rebuild)."""
    from .nz import Tile

    return Tile.parse(tile_id).index


def catchment_counts(lc: np.ndarray, code: np.ndarray, base: int, n_local: int, n_codes: int) -> np.ndarray:
    """``(n_local, n_codes + 1)`` cell counts per local catchment (``lc``: global segment id per cell, < 0 none)
    and coverage code (``code``: per cell, -1 none -> column 0)."""
    m = lc >= 0
    loc = lc[m] - base
    ok = (loc >= 0) & (loc < n_local)
    flat = loc[ok] * (n_codes + 1) + (code[m][ok] + 1)
    return np.bincount(flat, minlength=n_local * (n_codes + 1)).reshape(n_local, n_codes + 1)


def gap_blocks(land: np.ndarray, covered: np.ndarray, block: int) -> tuple[np.ndarray, np.ndarray]:
    """Land cells and land cells without coverage per ``block x block`` cells (shape must divide)."""
    h, w = land.shape
    lb = land.reshape(h // block, block, w // block, block).sum(axis=(1, 3))
    gb = (land & ~covered).reshape(h // block, block, w // block, block).sum(axis=(1, 3))
    return lb, gb


def tile_coverage(tile_dir: Path, layer, step_m: float = STEP_M, cell_m: float = GAP_CELL_M):
    """One national tile: per-reach line coverage, per-local-catchment cell counts by code, and the 1 km gap blocks
    of the cells the tile owns. Returns ``(reaches DataFrame, counts (n, ncodes+1), gid array, (land blocks, gap
    blocks, x0, y1, cell m2))``. The cell size is the raster's (8 m tiles: 64 m2 over the core; 1 m rebuild tiles:
    10 m cells, 100 m2, over the box, each cell owned by one tile)."""
    import pyogrio
    import rasterio
    import shapely
    from rasterio.features import rasterize

    tid = tile_dir.name
    base = _tile_index(tid) * SEG_BASE
    g = pyogrio.read_dataframe(tile_dir / "segments_core.gpkg", columns=["gid"])
    lines = [np.asarray(c) for c in (shapely.get_coordinates(ln) for ln in g.geometry.values)]
    geoms = list(layer.geometry.values)
    in_mosaic = layer["in_mosaic"].to_numpy(bool)
    cov = line_coverage(lines, geoms, in_mosaic, step_m)
    rec = pd.DataFrame({"gid": g["gid"].to_numpy(np.int64), **cov})
    with rasterio.open(tile_dir / "local_catchment_core.tif") as ds:
        lc = ds.read(1)
        tf, shape, (x0, y0, x1, y1) = ds.transform, ds.shape, ds.bounds
    box = shapely.box(x0, y0, x1, y1)
    shapes = [(shapely.clip_by_rect(gg, x0, y0, x1, y1), int(c)) for gg, c in zip(geoms, layer["code"], strict=True)
              if gg is not None and gg.intersects(box)]
    shapes = [(s, c) for s, c in shapes if not s.is_empty]
    code = (rasterize(shapes, out_shape=shape, transform=tf, fill=-1, dtype="int16") if shapes
            else np.full(shape, -1, np.int16)).astype(np.int64)
    n_local = int(max(rec["gid"].max() - base + 1 if len(rec) else 0, lc.max() - base + 1 if lc.max() >= 0 else 0))
    counts = catchment_counts(lc, code, base, n_local, len(layer))
    block = int(round(cell_m / abs(tf.a)))
    aligned = abs(x0 / cell_m - round(x0 / cell_m)) < 1e-6 and abs(y1 / cell_m - round(y1 / cell_m)) < 1e-6
    land, gap = (gap_blocks(lc >= 0, code >= 0, block) if aligned and shape[0] % block == 0 and shape[1] % block == 0
                 else (None, None))
    gids = base + np.arange(n_local, dtype=np.int64)
    return rec, counts, gids, (land, gap, x0, y1, float(abs(tf.a * tf.e)))


def reach_coverage(layer, tiles_dir: Path, out: Path | None = None, step_m: float = STEP_M, log=print):
    """Per-reach coverage over every national tile, cached in ``<out>/reach_coverage.pkl``, plus the 1 km gap
    grid (``<out>/gap_grid.npz``). Local catchment cells per survey code go to ``<out>/catchment_cells.npz``."""
    out = out or default_dir()
    tiles = sorted(p for p in tiles_dir.iterdir() if (p / "segments_core.gpkg").exists())
    nc = len(layer)
    recs, cnts, gids, blocks, cell_m2 = [], [], [], [], set()
    t0 = time.time()
    for i, td in enumerate(tiles):
        rec, counts, gid, blk = tile_coverage(td, layer, step_m)
        recs.append(rec)
        keep = counts.sum(axis=1) > 0
        cnts.append(counts[keep])
        gids.append(gid[keep])
        blocks.append(blk[:4])
        cell_m2.add(blk[4])
        if (i + 1) % 10 == 0 or i + 1 == len(tiles):
            log(f"lidar: {i + 1}/{len(tiles)} tiles ({time.time() - t0:.0f}s)")
    r = pd.concat(recs, ignore_index=True)
    r = r[~r["gid"].duplicated()].reset_index(drop=True)
    cg = np.concatenate(gids)
    cc = np.concatenate(cnts).astype(np.int64) if cnts else np.zeros((0, nc + 1), np.int64)
    if len(cell_m2) > 1:
        raise ValueError(f"local catchment rasters of several cell sizes under {tiles_dir}: {sorted(cell_m2)} m2")
    cm2 = cell_m2.pop() if cell_m2 else 64.0
    np.savez_compressed(out / "catchment_cells.npz", gid=cg, counts=cc, cell_m2=np.float64(cm2))
    _save_gap_grid(blocks, out / "gap_grid.npz", dem_cell_m=float(np.sqrt(cm2)))
    r.to_pickle(out / "reach_coverage_raw.pkl")
    return r, cg, cc


def _save_gap_grid(blocks, path: Path, dem_cell_m: float = 8.0) -> None:
    """The 1 km land and gap counts on the national grid, summed over the tiles' rasters (``(lb, gb, x0, y1)``
    per tile: a 64 km core, or a 32 km tile's box whose cells each belong to one tile)."""
    from .nz import NX, NY, TILE, X0, Y0

    nb = int(TILE / GAP_CELL_M)
    land = np.zeros((NY * nb, NX * nb), np.int32)
    gap = np.zeros_like(land)
    y_top = Y0 + NY * TILE
    for lb, gb, x0, y1 in blocks:
        if lb is None:
            continue
        r0, c0 = int(round((y_top - y1) / GAP_CELL_M)), int(round((x0 - X0) / GAP_CELL_M))
        h, w = lb.shape
        rs, cs = max(r0, 0), max(c0, 0)
        re_, ce = min(r0 + h, land.shape[0]), min(c0 + w, land.shape[1])
        if re_ <= rs or ce <= cs:
            continue
        land[rs:re_, cs:ce] += lb[rs - r0:re_ - r0, cs - c0:ce - c0]
        gap[rs:re_, cs:ce] += gb[rs - r0:re_ - r0, cs - c0:ce - c0]
    np.savez_compressed(path, land=land, gap=gap, x0=np.float64(X0), y1=np.float64(y_top),
                        cell_m=np.float64(GAP_CELL_M), dem_cell_m=np.float64(dem_cell_m))


def upstream_shares(down: np.ndarray, local_cells: np.ndarray, local_lidar: np.ndarray, local_any: np.ndarray):
    """Share of each reach's whole upstream catchment under the mosaic and under any LiDAR (network order)."""
    from .ml import _accumulate, _kahn

    order = _kahn(down)
    tot = _accumulate(down, order, local_cells.astype(np.float64))
    lid = _accumulate(down, order, local_lidar.astype(np.float64))
    anyc = _accumulate(down, order, local_any.astype(np.float64))
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(tot > 0, lid / tot, np.nan), np.where(tot > 0, anyc / tot, np.nan)


def attach(table: pd.DataFrame, reach: pd.DataFrame, cg: np.ndarray, cc: np.ndarray, layer,
           cell_m2: float = 64.0) -> pd.DataFrame:
    """The per-reach coverage table aligned to the national ``table`` (key ``gid``), with catchment shares and the
    survey fields of ``lidar_code``. ``cell_m2``: the local-catchment cell area (``catchment_cells.npz``)."""
    f = pd.DataFrame({"gid": table["gid"].to_numpy(np.int64)})
    f = f.merge(reach, on="gid", how="left")
    miss = f["lidar_share"].isna().to_numpy()
    f["lidar_share"] = f["lidar_share"].fillna(0.0)
    f["new_share"] = f["new_share"].fillna(0.0)
    f["lidar_code"] = f["lidar_code"].fillna(-1).astype(np.int64)
    in_mosaic = layer["in_mosaic"].to_numpy(bool)
    pos = pd.Series(np.arange(len(cg)), index=cg)
    pos = pos[~pos.index.duplicated()]
    k = f["gid"].map(pos).fillna(-1).astype(np.int64).to_numpy()
    have = k >= 0
    local = np.zeros((len(f), cc.shape[1]), np.int64)
    local[have] = cc[k[have]]
    tot = local.sum(axis=1)
    lid = local[:, 1:][:, in_mosaic].sum(axis=1)
    anyc = local[:, 1:].sum(axis=1)
    f["local_km2"] = tot * cell_m2 / 1e6
    f["local_lidar_share"] = np.where(tot > 0, lid / np.maximum(tot, 1), np.nan)
    down = table["down"].to_numpy(np.int64)
    f["catch_lidar_share"], f["catch_any_share"] = upstream_shares(down, tot, lid, anyc)
    lc = f["lidar_code"].to_numpy()
    has = lc >= 0
    pick = np.maximum(lc, 0)
    f["lidar_slug"] = pd.Categorical(np.where(has, layer["slug"].to_numpy(object)[pick], None))
    for col in ("year", "year_start"):
        f[f"lidar_{col}"] = pd.array(np.where(has, layer[col].to_numpy(float)[pick], np.nan), dtype="Int16")
    f["lidar_in_mosaic"] = pd.array(np.where(has, layer["in_mosaic"].to_numpy(bool)[pick], False), dtype="boolean")
    f.loc[~has, "lidar_in_mosaic"] = pd.NA
    for col in ("lidar_share", "new_share", "local_km2", "local_lidar_share", "catch_lidar_share", "catch_any_share"):
        f[col] = f[col].astype(np.float32)
    f["lidar_code"] = f["lidar_code"].astype(np.int16)
    f["lidar_missing_line"] = miss
    return f


# ---------------------------------------------------------------------------- source flag for the national pass
def source_flag(lidar_share: np.ndarray, new_share: np.ndarray, full: float = 0.95, some: float = 0.05) -> np.ndarray:
    """The per-reach DEM source class the 1 m pass would record: ``lidar`` (>= ``full`` of the line under LiDAR),
    ``mixed`` (a seam crosses the reach), ``8m`` (less than ``some``)."""
    s = np.asarray(lidar_share, float) + np.asarray(new_share, float)
    return np.where(s >= full, "lidar", np.where(s >= some, "mixed", "8m"))


# ---------------------------------------------------------------------------- gaps
def gap_components(land: np.ndarray, gap: np.ndarray, capacity: float, frac: float = GAP_FRAC,
                   min_land: float = 0.2):
    """Connected (8-neighbour) groups of gap cells: grid cells with land (DEM cell counts ``land``, at least
    ``min_land`` of ``capacity``) of which at least ``frac`` has no LiDAR (``gap``). Returns (labels, number)."""
    from scipy import ndimage

    is_gap = (land >= min_land * capacity) & (gap >= frac * np.maximum(land, 1))
    lab, n = ndimage.label(is_gap, structure=np.ones((3, 3), bool))
    return lab, n


def _grid_index(x, y, x0, y1, cell, shape):
    c = np.floor((np.asarray(x) - x0) / cell).astype(np.int64)
    r = np.floor((y1 - np.asarray(y)) / cell).astype(np.int64)
    ok = (r >= 0) & (r < shape[0]) & (c >= 0) & (c < shape[1])
    return np.where(ok, r, 0), np.where(ok, c, 0), ok


def gap_layer(grid_path: Path, min_km2: float = 5.0):
    """Gap areas as polygons (EPSG:2193): ``gap_id``, land and no-LiDAR km2, from ``gap_grid.npz``."""
    import geopandas as gpd
    import shapely
    from rasterio.features import shapes
    from rasterio.transform import from_origin

    g = np.load(grid_path)
    land, gap, cell, dc = g["land"], g["gap"], float(g["cell_m"]), float(g["dem_cell_m"])
    lab, n = gap_components(land, gap, (cell / dc) ** 2)
    a = dc * dc          # m2 per counted DEM cell
    land_km2 = np.bincount(lab.ravel(), weights=land.ravel() * a / 1e6, minlength=n + 1)
    gap_km2 = np.bincount(lab.ravel(), weights=gap.ravel() * a / 1e6, minlength=n + 1)
    tf = from_origin(float(g["x0"]), float(g["y1"]), cell, cell)
    polys: dict[int, list] = {}
    for geom, v in shapes(lab.astype(np.int32), mask=lab > 0, transform=tf):
        polys.setdefault(int(v), []).append(shapely.geometry.shape(geom))
    rows = [{"gap_id": k, "land_km2": float(land_km2[k]), "gap_km2": float(gap_km2[k]),
             "geometry": shapely.union_all(v)} for k, v in polys.items() if gap_km2[k] >= min_km2]
    return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:2193"), lab, gap_km2


# ---------------------------------------------------------------------------- summaries
AGE_BINS = [(0, 2015, "2008-2015"), (2016, 2019, "2016-2019"), (2020, 2022, "2020-2022"), (2023, 2100, "2023-2026")]


def _age(year) -> str:
    if year is None or pd.isna(year):
        return "none"
    y = int(year)
    return next(lab for a, b, lab in AGE_BINS if a <= y <= b)


def _cov_stats(km: np.ndarray, share: np.ndarray, anys: np.ndarray, flag: np.ndarray) -> dict:
    tot = float(km.sum())
    return {"reaches": int(len(km)), "km": round(tot, 1),
            "km_lidar": round(float((km * share).sum()), 1),
            "km_share": round(float((km * share).sum() / tot), 4) if tot else None,
            "km_share_any": round(float((km * anys).sum() / tot), 4) if tot else None,
            "reaches_lidar": int((flag == "lidar").sum()), "reaches_mixed": int((flag == "mixed").sum()),
            "reaches_8m": int((flag == "8m").sum())}


def section_rows(table: pd.DataFrame, sections=None) -> list[dict]:
    """Runnable guide sections mapped onto the national table: ``[{river, region, grade, held_out, rows}]``."""
    from .models import all_sections, map_sections

    out = []
    for m in map_sections(table, sections if sections is not None else all_sections(), group=False):
        if m.section.status != "runnable" or not len(m.rows):
            continue
        out.append({"river": m.section.river, "region": m.section.region, "grade": m.section.grade or "",
                    "held_out": bool(m.section.held_out), "rows": np.asarray(m.rows, np.int64)})
    return out


def summarise(table: pd.DataFrame, cov: pd.DataFrame, layer, sections: list[dict], grid_path: Path | None = None,
              domain_km2: float = 2.0, top: int = 25) -> dict:
    """National and per-region coverage, capture years, surveys, gaps and guide sections (see module doc)."""
    km = table["length_m"].to_numpy(float) / 1000.0
    share = cov["lidar_share"].to_numpy(float)
    anys = share + cov["new_share"].to_numpy(float)
    flag = source_flag(share, cov["new_share"].to_numpy(float))
    region = table["region"].to_numpy(object)
    dom = table["area_km2"].to_numpy(float) >= domain_km2
    year = pd.to_numeric(cov["lidar_year"], errors="coerce").to_numpy(dtype=float, na_value=np.nan)
    guide = np.zeros(len(table), bool)
    for s in sections:
        if not s["held_out"]:
            guide[s["rows"]] = True
    local = cov["local_km2"].to_numpy(float)
    loc_lid = np.nan_to_num(cov["local_lidar_share"].to_numpy(float)) * local
    catch = cov["catch_lidar_share"].to_numpy(float)

    def block(m: np.ndarray) -> dict:
        d = {"all": _cov_stats(km[m], share[m], anys[m], flag[m]),
             "domain": _cov_stats(km[m & dom], share[m & dom], anys[m & dom], flag[m & dom]),
             "guide": _cov_stats(km[m & guide], share[m & guide], anys[m & guide], flag[m & guide]),
             "land_km2": round(float(local[m].sum()), 0),
             "land_share": round(float(loc_lid[m].sum() / max(local[m].sum(), 1e-9)), 4)}
        c = catch[m & dom]
        c = c[np.isfinite(c)]
        d["domain"]["catch_share_median"] = round(float(np.median(c)), 4) if len(c) else None
        d["domain"]["catch_full_share"] = round(float((c >= 0.95).mean()), 4) if len(c) else None
        ages: dict[str, float] = {}
        for a, k_, s_ in zip(map(_age, year[m & dom]), km[m & dom], share[m & dom], strict=True):
            ages[a] = ages.get(a, 0.0) + float(k_ * s_)
        tot = sum(ages.values()) or 1.0
        d["domain"]["km_by_capture"] = {a: round(ages.get(a, 0.0), 1) for _, _, a in AGE_BINS}
        d["domain"]["share_by_capture"] = {a: round(ages.get(a, 0.0) / tot, 4) for _, _, a in AGE_BINS}
        return d

    out = {"national": block(np.ones(len(table), bool)), "regions": {}}
    for r in sorted(set(region)):
        out["regions"][r] = block(region == r)
    # capture years by domain km, and surveys
    yr = pd.Series(km[dom] * share[dom]).groupby(np.where(np.isnan(year[dom]), -1, year[dom]).astype(int)).sum()
    out["km_by_year"] = {int(k): round(float(v), 1) for k, v in yr.items() if k >= 0}
    code = cov["lidar_code"].to_numpy(np.int64)
    by_s = pd.Series(km[dom] * (share + cov["new_share"].to_numpy(float))[dom]).groupby(code[dom]).sum()
    out["surveys"] = [{"code": int(c), "slug": str(layer["slug"].iloc[c]), "title": str(layer["title"].iloc[c]),
                       "start": str(layer["start"].iloc[c]), "end": str(layer["end"].iloc[c]),
                       "in_mosaic": bool(layer["in_mosaic"].iloc[c]), "km2": round(float(layer["km2"].iloc[c]), 1),
                       "domain_km": round(float(v), 1)}
                      for c, v in by_s.sort_values(ascending=False).items() if c >= 0]
    out["layer"] = {"areas": int(len(layer)), "km2": round(float(layer["km2"].sum()), 0),
                    "km2_mosaic": round(float(layer.loc[layer["in_mosaic"], "km2"].sum()), 0),
                    "km2_new": round(float(layer.loc[~layer["in_mosaic"], "km2"].sum()), 1),
                    "years": [int(layer["year_start"].min()), int(layer["year"].max())]}
    # gaps by named river (domain reaches without LiDAR)
    miss = dom & (anys < 0.5)
    river = table["river"].fillna("").to_numpy(object)
    g = pd.DataFrame({"river": river[miss], "region": region[miss], "km": km[miss],
                      "area": table["area_km2"].to_numpy(float)[miss]})
    g = g[g["river"] != ""].groupby(["river", "region"]).agg(km=("km", "sum"), max_area_km2=("area", "max"))
    out["gap_rivers"] = [{"river": a, "region": b, "km": round(float(r.km), 1),
                          "max_area_km2": round(float(r.max_area_km2), 1)}
                         for (a, b), r in g.sort_values("km", ascending=False).head(top).iterrows()]
    # guide sections not fully under LiDAR
    secs = []
    for s in sections:
        rws = s["rows"]
        k_ = km[rws].sum()
        sh = float((km[rws] * anys[rws]).sum() / k_) if k_ else 0.0
        yrs = sorted({int(v) for v in year[rws] if v is not None and not pd.isna(v)})
        secs.append({"river": s["river"], "region": s["region"], "grade": s["grade"], "km": round(float(k_), 1),
                     "lidar_share": round(sh, 3), "years": yrs, "held_out": s["held_out"]})
    out["sections"] = {"n": len(secs), "km": round(sum(s["km"] for s in secs), 1),
                       "full": sum(s["lidar_share"] >= 0.95 for s in secs),
                       "partial": sum(0.05 <= s["lidar_share"] < 0.95 for s in secs),
                       "none": sum(s["lidar_share"] < 0.05 for s in secs),
                       "not_full": sorted([s for s in secs if s["lidar_share"] < 0.95], key=lambda s: s["lidar_share"])}
    sy: dict[str, float] = {}
    for s in secs:
        for y in s["years"][-1:] or ["none"]:
            sy[str(y)] = sy.get(str(y), 0.0) + s["km"]
    out["sections"]["km_by_latest_year"] = {k: round(v, 1) for k, v in sorted(sy.items())}
    if grid_path is not None and grid_path.exists():
        gl, lab, gap_km2 = gap_layer(grid_path)
        gg = np.load(grid_path)
        r, c, ok = _grid_index(table["x"].to_numpy(), table["y"].to_numpy(), float(gg["x0"]), float(gg["y1"]),
                               float(gg["cell_m"]), lab.shape)
        comp = np.where(ok, lab[r, c], 0)
        rows = []
        for gid_ in gl.sort_values("gap_km2", ascending=False)["gap_id"].head(top):
            m = (comp == gid_) & dom
            rv = pd.Series(km[m] * (1 - anys[m])).groupby(river[m]).sum().drop("", errors="ignore")
            rv = rv.sort_values(ascending=False).head(4)
            cen = gl.loc[gl["gap_id"] == gid_].geometry.iloc[0].representative_point()
            from pyproj import Transformer

            lon, lat = Transformer.from_crs("EPSG:2193", "EPSG:4326", always_xy=True).transform(cen.x, cen.y)
            regs = pd.Series(km[m]).groupby(region[m]).sum().sort_values(ascending=False)
            rows.append({"gap_id": int(gid_), "gap_km2": round(float(gap_km2[gid_]), 0),
                         "domain_km_no_lidar": round(float((km[m] * (1 - anys[m])).sum()), 1),
                         "guide_km": round(float(km[m & guide].sum()), 1), "lon": round(lon, 3), "lat": round(lat, 3),
                         "regions": list(regs.index[:2]), "rivers": [str(k) for k in rv.index]})
        out["gap_areas"] = rows
    return out


def out_dir() -> Path:
    """Where ``run`` writes: ``data/analysis/lidar`` for the 8 m scan, ``<root>/analysis/lidar`` for the 1 m rebuild
    (which reads the survey layer of ``data/analysis/lidar`` and never writes there)."""
    from .national import is_1m, nz_dir

    return nz_dir() / "analysis" / "lidar" if is_1m() else default_dir()


def run(out: Path | None = None, refresh: bool = False, log=print) -> dict:
    """Coverage layer, per-reach coverage, the attached table (``reach_coverage.pkl`` aligned to the national
    table) and ``summary.json``. Reads ``<root>/tiles`` and ``<root>/segments_table.pkl`` (``nz_dir()``); the survey
    layer comes from ``<out>/lidar_coverage.gpkg``, else ``data/analysis/lidar`` (built there when missing)."""
    from .models import national_table
    from .national import nz_dir

    out = out or out_dir()
    out.mkdir(parents=True, exist_ok=True)
    own = (out / "lidar_coverage.gpkg").exists() or out.resolve() == default_dir().resolve() or refresh
    layer = coverage_layer(out if own else default_dir(), refresh=refresh, log=log)
    t0 = time.time()
    raw = out / "reach_coverage_raw.pkl"
    if raw.exists() and not refresh and (out / "catchment_cells.npz").exists():
        reach = pd.read_pickle(raw)
        z = np.load(out / "catchment_cells.npz")
        cg, cc = z["gid"], z["counts"]
        cm2 = float(z["cell_m2"]) if "cell_m2" in z else 64.0
    else:
        reach, cg, cc = reach_coverage(layer, nz_dir() / "tiles", out, log=log)
        cm2 = float(np.load(out / "catchment_cells.npz")["cell_m2"])
    table = national_table(log=log)
    cov = attach(table, reach, cg, cc, layer, cm2)
    cov["source"] = pd.Categorical(source_flag(cov["lidar_share"].to_numpy(), cov["new_share"].to_numpy()),
                                   categories=["lidar", "mixed", "8m"])
    cov.to_pickle(out / "reach_coverage.pkl")
    log(f"lidar: per-reach coverage {len(cov)} reaches ({time.time() - t0:.0f}s) -> {out / 'reach_coverage.pkl'}")
    secs = section_rows(table)
    s = summarise(table, cov, layer, secs, out / "gap_grid.npz")
    gl, _, _ = gap_layer(out / "gap_grid.npz")
    gl.to_file(out / ("lidar_coverage.gpkg" if own else "lidar_gaps.gpkg"), layer="gaps", driver="GPKG")
    (out / "summary.json").write_text(json.dumps(s, indent=1, ensure_ascii=False, default=str))
    n = s["national"]
    log(f"lidar: national {n['all']['km_share']:.1%} of reach km, {n['domain']['km_share']:.1%} of reaches >= 2 km2, "
        f"{n['guide']['km_share']:.1%} of guide km, {n['land_share']:.1%} of land under the 1 m mosaic -> "
        f"{out / 'summary.json'}")
    return s
