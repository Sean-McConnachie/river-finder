"""The national lake table of the 1 m rebuild (rf-lxa.5, ``docs/nz1m-merge.md``).

One record per lake for the whole country: polygon, one LiDAR water level, kind, one outlet. The tiles fill their
lake holes at that level (``nz1m.read_lake_table``), and the merge (``nz1mmerge``) uses the lake as one network node.

- **Records**: every tile reports its lakes (``tile.json`` ``lakes``: id, kind ``hole`` / ``flat``, level, km2,
  bbox, polygon of its 10 m cells, outlet) and every hole it examined (``holes_list``: rim level and rim cells).
  ``survey`` makes the same records for any window without routing (stream 1 m -> 2 m, sea mask, holes, lake
  labels; about 40 s per tile box): the pre-pass over windows around big lakes before the tiles run, and the
  polygons of tiles that ran before the tile job wrote them.
- **Join** (``build``): records whose polygons meet (within 20 m) and whose levels agree within 0.10 m are one lake,
  whichever tile they come from (union-find). A lake is a ``hole`` lake when any record has a filled hole (its
  interior is no data in the mosaic), else ``flat`` (the survey flattened it). Its level: for a hole lake, the rim
  level of the tile that saw the most flattened rim cells of it (its own level, not one taken from an earlier
  table); for a flat lake, the median of the records' levels weighted by area. A lake whose level is below
  ``nz1m.TIDAL_MAX_M`` (1 m) is **tidal** water (an estuary, a flattened tidal reach, a coastal lagoon): it is no lake
  and the merge treats it as sea.
- **Outlet**: the records that drain ``out`` give outlet points (where the flow leaves the lake); points within
  100 m are one outlet, and the outlet carrying the most in-tile area wins (a tile that sees only part of a lake
  routes that part to its box edge and reports ``sink``). The outlet reach is the record's ``outlet_gid`` from a
  tile whose core holds it; the merge resolves the others.
- **Re-run check** (``check``): a tile must be re-run when one of its holes was filled at a level more than
  ``LEVEL_TOL_M`` (2 cm, the rim tolerance) from the national level, when it left a national hole lake unfilled
  (its part of the rim failed the rim test) or filled a hole that is tidal. Re-run, the tile reads ``lakes.gpkg``
  and takes the national level and decision for every hole that lies mostly in a table lake.

Outputs: ``data/nz1m/lakes.gpkg`` (layer ``lakes``) and ``data/nz1m/lakes.json`` (counts, re-run list); surveys in
``data/nz1m/lakes/<name>.json``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import nz1m as N

LEVEL_TOL_M = 0.02          # re-run a tile whose filled lake level differs from the national one by more
JOIN_LEVEL_M = 0.10         # records of one lake: levels within this ...
JOIN_GAP_M = 20.0           # ... and polygons within this
OUTLET_JOIN_M = 100.0       # outlet points within this are one outlet


def lakes_dir(root: Path | None = None) -> Path:
    return (root or N.root_dir()) / "lakes"


# ---------------------------------------------------------------------------------------------- survey
def survey_box(box, src=None, dem8=None, log=print, threads: int = 32) -> dict:
    """The lake part of the tile job over ``box`` (no routing): stream the 1 m mosaic into the 2 m grid, mask the
    surveyed sea, examine the holes (``nz1m.fill_holes``, no lake table: the tile's own rim levels) and label the
    lakes (``nz1m.label_lakes``). Returns ``{"box", "lakes", "tidal", "holes_list"}`` as ``tile.json`` holds them
    (without outlets). ``src`` / ``dem8`` default to the national mosaic and 8 m DEM."""
    from concurrent.futures import ThreadPoolExecutor

    from rasterio.transform import from_origin

    from . import lidarpipe as P

    box = tuple(float(v) for v in box)
    H, W = int(round((box[3] - box[1]) / N.CELL)), int(round((box[2] - box[0]) / N.CELL))
    if H % (N.F8 * N.F10) or W % (N.F8 * N.F10) or any(round(v) % 40 for v in box):
        raise ValueError("box must lie on the 40 m grid")
    t0 = time.time()
    src = src or N.MosaicSource(box)
    ex = ThreadPoolExecutor(1)
    f8 = ex.submit(dem8 or (lambda: N.fetch_dem8(box)))
    z = np.full((H, W), np.nan, np.float32)
    wc = np.zeros((H, W), np.uint8)
    st = P.stream(src.box_jobs(box), N._into2(z, wc), threads=threads, in_reader=True, read=src.read, log=log,
                  every=200)
    z8 = f8.result()
    ex.shutdown()
    tf = from_origin(box[0], box[3], N.CELL, N.CELL)
    N._mask_sea(z, wc, z8, N.F8, N.SEA_MAX_Z)
    holes = N.fill_holes(z, wc, z8, tf)
    del z8
    z10, _, cnt1, flat1, _, nlake10, _ = N._blocks(z, wc, N.F10)
    del z, wc
    flatw = (cnt1 > 0) & (2 * flat1 >= cnt1)
    _, _, lakes, tidal = N.label_lakes(flatw, nlake10, z10, holes, box)
    out = {"box": list(box), "lakes": lakes, "tidal": tidal, "seconds": round(time.time() - t0, 1),
           "read_cells": int(st["cells"]),
           "holes_list": [{k: v for k, v in h.items() if k not in ("mask", "slice")} for h in holes
                          if h["filled"] or h["km2"] >= 0.01]}
    log(f"lake survey {box}: {len(lakes)} lakes, {len(tidal)} tidal, {sum(h['filled'] for h in holes)} holes filled "
        f"in {out['seconds']} s")
    return out


def survey(names: list[str], root: Path | None = None, log=print) -> list[Path]:
    """``survey_box`` over tile boxes (``q<ix>y<iy>``) or windows (``x0,y0,x1,y1`` in NZTM, rounded out to the 40 m
    grid) -> ``data/nz1m/lakes/<name>.json``."""
    out = []
    d = lakes_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    for name in names:
        if name.startswith("q"):
            t = N.Tile32.parse(name)
            box, rec = t.box, {"tile": t.id}
        else:
            v = [float(s) for s in name.split(",")]
            box = (np.floor(v[0] / 40) * 40, np.floor(v[1] / 40) * 40, np.ceil(v[2] / 40) * 40,
                   np.ceil(v[3] / 40) * 40)
            name = "w{:.0f}_{:.0f}_{:.0f}_{:.0f}".format(*box)
            rec = {"window": name}
        r = rec | survey_box(box, log=log)
        p = d / f"{name}.json"
        p.write_text(json.dumps(r, indent=1, default=N._jsonable))
        out.append(p)
    return out


# ---------------------------------------------------------------------------------------------- records
def _poly(rec: dict):
    import shapely

    if rec.get("wkt"):
        return shapely.from_wkt(rec["wkt"])
    return shapely.box(*rec["bbox"])


def tile_records(tid: str, root: Path | None = None) -> dict:
    """A done tile's lake, tidal and hole records (``tile.json``). Tiles that ran before the tile job wrote lake
    polygons take them from their survey (``lakes/<qid>.json``, matched by bbox and level; ``polygon_from``)."""
    p = N.tile_dir(tid, root) / "tile.json"
    t = json.loads(p.read_text())
    lakes = [dict(lk) for lk in t.get("lakes", [])]
    tidal = [dict(x) for x in t.get("tidal", [])]
    holes = [dict(h) for h in t.get("holes_list", [])]
    sv = lakes_dir(root) / f"{tid}.json"
    if any(not lk.get("wkt") for lk in lakes) and sv.exists():
        s = json.loads(sv.read_text())
        cands = [(x, "lake") for x in s["lakes"]] + [(x, "tidal") for x in s["tidal"]]
        for lk in lakes:
            if lk.get("wkt"):
                continue
            for x, kind in cands:
                if x["bbox"] == lk["bbox"] and abs(x["level"] - lk["level"]) < 0.01:
                    lk["wkt"], lk["polygon_from"] = x["wkt"], "survey"
                    if kind == "tidal":
                        lk["kind"] = "tidal"
                    lk.setdefault("touches_box", x.get("touches_box"))
                    break
    for lk in lakes:                       # tiles before the tidal rule: flattened water below 1 m is tidal
        if lk["kind"] == "flat" and lk["level"] < N.TIDAL_MAX_M:
            lk["kind"] = "tidal"
    return {"tile": tid, "box": t["box"], "core": t["core"], "lakes": lakes, "tidal": tidal, "holes": holes,
            "lake_table": bool(t.get("lake_table", False))}


def _records(recs: list[dict]) -> pd.DataFrame:
    rows = []
    for r in recs:
        for lk in r["lakes"]:
            rows.append({"tile": r["tile"], "lake": lk["lake"], "kind": lk["kind"], "level": lk["level"],
                         "km2": lk["km2"], "geometry": _poly(lk), "touches_box": bool(lk.get("touches_box", False)),
                         "drains": lk.get("drains", ""), "outlet_x": lk.get("outlet_x", np.nan),
                         "outlet_y": lk.get("outlet_y", np.nan),
                         "outlet_area_tile_km2": lk.get("outlet_area_tile_km2", np.nan),
                         "outlet_gid": int(lk.get("outlet_gid", -1)), "holes": list(lk.get("holes", [])),
                         "reaches": list(lk.get("reaches", []))})
        for x in r["tidal"]:
            rows.append({"tile": r["tile"], "lake": -1, "kind": "tidal", "level": x["level"], "km2": x["km2"],
                         "geometry": _poly(x), "touches_box": bool(x.get("touches_box", False)), "drains": "",
                         "outlet_x": np.nan, "outlet_y": np.nan, "outlet_area_tile_km2": np.nan, "outlet_gid": -1,
                         "holes": [], "reaches": []})
    return pd.DataFrame(rows)


def _union_find(n: int, pairs) -> np.ndarray:
    parent = np.arange(n)

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    return np.array([find(i) for i in range(n)])


def _outlet(g: pd.DataFrame) -> dict:
    """One outlet from the records of a lake that drain out: points within ``OUTLET_JOIN_M`` are one outlet; the one
    carrying the most in-tile area wins; its gid from a record whose tile core holds the outlet reach."""
    o = g[(g["drains"] == "out") & np.isfinite(g["outlet_x"].astype(float))]
    if not len(o):
        return {"drains": "sink" if (g["drains"] != "").any() else "", "outlet_x": np.nan, "outlet_y": np.nan,
                "outlet_area_tile_km2": np.nan, "outlet_gid": -1, "outlet_tile": "", "n_outlets": 0}
    xy = o[["outlet_x", "outlet_y"]].to_numpy(float)
    lab = _union_find(len(o), [(i, j) for i in range(len(o)) for j in range(i + 1, len(o))
                               if np.hypot(*(xy[i] - xy[j])) <= OUTLET_JOIN_M])
    o = o.assign(cl=lab)
    best = o.groupby("cl")["outlet_area_tile_km2"].max().idxmax()
    c = o[o["cl"] == best].sort_values("outlet_area_tile_km2", ascending=False)
    ing = c[c["outlet_gid"] >= 0]
    r = ing.iloc[0] if len(ing) else c.iloc[0]
    return {"drains": "out", "outlet_x": float(r["outlet_x"]), "outlet_y": float(r["outlet_y"]),
            "outlet_area_tile_km2": float(c["outlet_area_tile_km2"].max()), "outlet_gid": int(r["outlet_gid"]),
            "outlet_tile": str(r["tile"]), "n_outlets": int(o["cl"].nunique())}


def join(recs: list[dict]):
    """The national lakes from tile (or survey) records: GeoDataFrame ``lakes`` (one row per lake) and the record
    table with each record's ``lake_id``."""
    import geopandas as gpd
    import shapely

    R = _records(recs)
    cols = ["lake_id", "kind", "level_m", "km2", "n_records", "tiles", "drains", "outlet_x", "outlet_y",
            "outlet_area_tile_km2", "outlet_gid", "outlet_tile", "n_outlets", "level_from"]
    if not len(R):
        return gpd.GeoDataFrame({c: [] for c in cols}, geometry=[], crs="EPSG:2193"), R
    geoms = R["geometry"].to_numpy()
    tree = shapely.STRtree(geoms)
    a, b = tree.query(geoms, predicate="dwithin", distance=JOIN_GAP_M)
    lev = R["level"].to_numpy(float)
    keep = (a < b) & (np.abs(lev[a] - lev[b]) <= JOIN_LEVEL_M)
    R["root"] = _union_find(len(R), zip(a[keep], b[keep], strict=True))
    holes = {r["tile"]: {h["hole"]: h for h in r["holes"]} for r in recs}
    rows, lake_of = [], {}
    for _, g in R.groupby("root", sort=False):
        poly = shapely.union_all(g["geometry"].to_numpy()).simplify(10.0)
        kinds = set(g["kind"])
        # the level: a hole lake's rim level from the tile that saw the most flattened rim cells of it
        best, level_from = None, ""
        for _, r in g[g["kind"] == "hole"].iterrows():
            for h in r["holes"]:
                hr = holes.get(r["tile"], {}).get(h)
                if hr is None or not hr.get("filled"):
                    continue
                own = hr.get("own_level", None if hr.get("level_from") == "lake_table" else hr.get("level"))
                if own is None:
                    continue
                n = hr.get("rim_flat_cells", hr.get("rim_cells", 0))
                if best is None or n > best[0]:
                    best = (n, float(own), f"{r['tile']} hole {h}")
        if best is not None:
            level, level_from = best[1], best[2]
        else:
            w = g["km2"].to_numpy(float)
            o = np.argsort(g["level"].to_numpy(float))
            cw = np.cumsum(w[o])
            level = float(g["level"].to_numpy(float)[o][np.searchsorted(cw, 0.5 * cw[-1])])
            level_from = "median of flat records"
        kind = "tidal" if level < N.TIDAL_MAX_M or kinds == {"tidal"} else ("hole" if "hole" in kinds else "flat")
        rec = {"kind": kind, "level_m": level, "km2": round(float(poly.area) / 1e6, 3), "n_records": len(g),
               "tiles": ",".join(sorted(set(g["tile"]))), "level_from": level_from}
        rec |= _outlet(g) if kind != "tidal" else {"drains": "", "outlet_x": np.nan, "outlet_y": np.nan,
                                                    "outlet_area_tile_km2": np.nan, "outlet_gid": -1,
                                                    "outlet_tile": "", "n_outlets": 0}
        rec["geometry"] = poly
        rows.append(rec)
        for i in g.index:
            lake_of[i] = len(rows) - 1
    L = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:2193")
    # stable ids: north to south, then west to east
    b = L.geometry.bounds
    order = np.lexsort((b["minx"].to_numpy(), -b["maxy"].to_numpy()))
    new = np.empty(len(L), np.int64)
    new[order] = np.arange(len(L))
    L["lake_id"] = new
    L = L.iloc[order].reset_index(drop=True)
    R["lake_id"] = [int(new[lake_of[i]]) for i in R.index]
    return L[cols + ["geometry"]], R


def check(L, recs: list[dict]) -> list[dict]:
    """Tiles to re-run because their lake fills differ from the national table ``L`` (``lakes.gpkg``): a filled hole
    whose level is more than ``LEVEL_TOL_M`` off, a filled hole that is tidal, a national hole lake left unfilled."""
    import shapely

    holes = L[L["kind"].isin(["hole", "tidal"])]
    geoms = holes.geometry.to_numpy()
    tree = shapely.STRtree(geoms) if len(holes) else None
    out = []
    for r in recs:
        for h in r["holes"]:
            if tree is None:
                break
            pt = shapely.Point(h["x"], h["y"])
            hit = tree.query(pt, predicate="dwithin", distance=JOIN_GAP_M)
            if not len(hit):
                continue
            lk = holes.iloc[int(hit[0])]
            row = {"tile": r["tile"], "hole": h["hole"], "lake_id": int(lk["lake_id"]), "km2": h["km2"],
                   "national_kind": lk["kind"], "national_level_m": round(float(lk["level_m"]), 3),
                   "tile_level_m": h.get("level"), "filled": bool(h["filled"])}
            if h["filled"] and lk["kind"] == "tidal":
                out.append(row | {"reason": "filled a tidal hole (it is sea)"})
            elif h["filled"] and abs(float(h["level"]) - float(lk["level_m"])) > LEVEL_TOL_M:
                out.append(row | {"reason": f"level off by {float(h['level']) - float(lk['level_m']):+.3f} m"})
            elif not h["filled"] and lk["kind"] == "hole" and h["km2"] >= 0.01:
                out.append(row | {"reason": "national hole lake left unfilled"})
    return out


def done_tiles(root: Path | None = None) -> list[str]:
    d = (root or N.root_dir()) / "tiles"
    return sorted(p.parent.name for p in d.glob("*/tile.json") if N.is_done(p.parent.name, root))


def build(tiles: list[str] | None = None, root: Path | None = None, windows: bool = True, write: bool = True,
          log=print):
    """The national lake table from the done tiles' records (and the window surveys, ``lakes/w*.json``) ->
    ``lakes.gpkg`` + ``lakes.json`` (counts and the re-run check). Returns (lakes, records, re-run list)."""
    root = root or N.root_dir()
    tiles = tiles or done_tiles(root)
    recs = [tile_records(t, root) for t in tiles]
    if windows:
        for p in sorted(lakes_dir(root).glob("w*.json")):
            s = json.loads(p.read_text())
            recs.append({"tile": s["window"], "box": s["box"], "core": s["box"], "lakes": s["lakes"],
                         "tidal": s["tidal"], "holes": s["holes_list"], "lake_table": False})
    L, R = join(recs)
    rerun = check(L, [r for r in recs if not r["tile"].startswith("w")])
    if write:
        p = root / "lakes.gpkg"
        p.unlink(missing_ok=True)
        L.to_file(p, layer="lakes", driver="GPKG")
        summary = {"tiles": tiles, "lakes": int(len(L)),
                   "by_kind": {k: int(v) for k, v in L["kind"].value_counts().items()},
                   "km2_by_kind": {k: round(float(v), 1) for k, v in L.groupby("kind")["km2"].sum().items()},
                   "multi_tile": int((L["n_records"] > 1).sum()), "rerun": rerun,
                   "rerun_tiles": sorted({r["tile"] for r in rerun})}
        (root / "lakes.json").write_text(json.dumps(summary, indent=1, default=N._jsonable))
        log(f"lakes: {len(L)} ({summary['by_kind']}), {summary['multi_tile']} in more than one record, "
            f"re-run: {summary['rerun_tiles'] or 'none'} -> {p}")
    return L, R, rerun
