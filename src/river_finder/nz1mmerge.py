"""National merge of the 1 m tiles (rf-lxa.5, ``docs/nz1m-merge.md``): ``river-finder nz1m merge [qid...]``.

The tiles (``nz1m``, rf-lxa.6) write their core reaches; this stitches them into one national network and takes
every reach's area from the network, not from the tile:

1. **Read** every tile's core reaches (``segments_core.gpkg``, streamed tile by tile; only the line ends, midpoints
   and the lines near the core edge are kept) and the national lake table (``nz1mlakes.build``).
2. **Links**, in order (``stitch``): 0 inside the tile (``down_seg`` is a core reach); 1 at a tile edge (a reach that
   leaves its core links to the reach of another tile whose line holds its end point: the same 2 m cell, whatever
   the in-tile areas; else within ``STITCH_M`` with an area ratio of 0.7-3; else the first reach of the tile it
   crosses into within ``ENTRY_M``, where the two tiles cut the link into reaches differently); 2 through a lake
   node (every lake reach whose path leaves the lake anywhere but the national outlet, and every unstitched reach
   ending in the lake, links to the lake's outlet reach); 3 REC2 relink (``relink.relink_outlets``, the fallback of
   the 8 m merge, for exits into a merged tile); 4 none.
3. **Cycle guard**: a topological sort over ``down_gid``; a cycle is reported and cut at its weakest link (the
   highest ``stitch`` code, then the smallest area).
4. **Network accumulation** of ``local_km2`` and the other ``local_*`` sums (each box cell is counted once, by the
   tile whose core holds its first reach): ``area_km2`` and the other ``merge`` fields of ``lidarspec``.
5. **Joins** that need the final areas: REC2 matched along the network (``rec2_match``: area ratio and line
   distance, kept consistent with the reach below), REC1 MAF (``rec1_match``), the river-guide region.
6. **Write** ``network.parquet`` (the merge fields per gid), ``reaches.parquet`` (every tile field, the merge fields
   and the line: the national reach table), ``x1.arrow`` and ``steps.arrow`` (all tiles), ``lakes.gpkg`` (with the
   outlet reaches) and ``merge.json`` (counts, unresolved outlets, cycles, timings).

Gids never change. ``outlet_kind`` replaces the 8 m ``unresolved_gt200.csv`` and ``stitch`` replaces
``relink_log.csv``.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

import numpy as np
import pandas as pd
from numba import njit

from . import lidarspec as S
from . import nz1m as N
from . import nz1mlakes as LK

STITCH_M = 8.0                  # the neighbour's line passes the exit point within this
STITCH_RATIO = (0.7, 3.0)       # its in-tile area / the exit's
EDGE_BAND_M = 1500.0            # lines kept for stitching: within this of the core edge
ENTRY_M = 600.0                 # second stitch: an entry reach of another tile starts within this of the exit
EXACT_M = 1.0                   # on the line: the same 2 m cell path (lines are simplified at 1 m)
TRUNC_TOL = 0.10                # truncated: the area entering unlinked is more than this share ...
TRUNC_MIN_KM2 = 0.25            # ... and more than this
THETA = 0.45
LAKE_OUTLET_M = 300.0           # an outlet point not held by a tile record: the reach within this
REC2_NAME_M, REC2_NAME_RATIO = 150.0, (0.6, 1.6)     # spec rule for river_rec2
REC1_M, REC1_RATIO = 400.0, (0.4, 2.5)               # spec rule for the MAF match (as reaches._attach_flood)

STITCH_IN, STITCH_EDGE, STITCH_LAKE, STITCH_REC2, STITCH_NONE = 0, 1, 2, 3, 4
OUT_NONE, OUT_SEA, OUT_SINK, OUT_EDGE = 0, 1, 2, 3

READ_COLS = ["gid", "tile", "seg_id", "down_seg", "end_x", "end_y", "exits_core", "inflow", "area_tile_km2",
             "area_tile_mid_km2", "local_km2", "length_m", "z_top", "z_bot", "gradient_m_per_km", "local_z_mean_m",
             "local_z_max_m", "local_flat_km2", "local_hard_km2", "local_lidar_km2", "local_lake_km2", "dem_source",
             "lidar_share", "lake", "lake_share"]
LOCAL_SUMS = {"local_flat_km2": "upstream_flat_frac", "local_hard_km2": "upstream_hard_frac",
              "local_lake_km2": "upstream_lake_frac", "local_lidar_km2": "catch_lidar_share"}


def _rss_gb() -> float:
    """Peak resident memory of the process so far (GB)."""
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6


# ---------------------------------------------------------------------------------------------- reading
def read_tile(tid: str, root: Path | None = None) -> tuple[pd.DataFrame, dict]:
    """One tile's core reaches (``READ_COLS`` + start, middle points) and the vertices of the lines near its core
    edge (``xy``, ``k``: row in the frame, ``last``: the line's last vertex)."""
    import pyogrio
    import shapely

    od = N.tile_dir(tid, root)
    g = pyogrio.read_dataframe(od / "segments_core.gpkg", columns=READ_COLS)
    core = json.loads((od / "tile.json").read_text())["core"]
    if not len(g):   # a tile with no reach in its core (a sliver of land, a small island)
        T = pd.DataFrame(g.drop(columns="geometry"))
        for c in ("start_x", "start_y", "mid_x", "mid_y"):
            T[c] = np.zeros(0)
        return T, {"xy": np.zeros((0, 2)), "k": np.zeros(0, np.int64), "last": np.zeros(0, bool), "core": core}
    geoms = g.geometry.to_numpy()
    xy, ix = shapely.get_coordinates(geoms, return_index=True)
    first = np.r_[0, np.flatnonzero(np.diff(ix)) + 1]
    lastv = np.r_[first[1:] - 1, len(ix) - 1]
    T = pd.DataFrame(g.drop(columns="geometry"))
    T["start_x"], T["start_y"] = xy[first, 0], xy[first, 1]
    mid = shapely.get_coordinates(shapely.line_interpolate_point(geoms, 0.5, normalized=True))
    T["mid_x"], T["mid_y"] = mid[:, 0], mid[:, 1]
    b = shapely.bounds(geoms)
    inner = ((b[:, 0] > core[0] + EDGE_BAND_M) & (b[:, 2] < core[2] - EDGE_BAND_M)
             & (b[:, 1] > core[1] + EDGE_BAND_M) & (b[:, 3] < core[3] - EDGE_BAND_M))
    sel = ~inner[ix]
    last = np.zeros(len(ix), bool)
    last[lastv] = True
    return T, {"xy": xy[sel], "k": ix[sel], "last": last[sel], "core": core}


def read_tiles(tiles: list[str], root: Path | None = None, log=print):
    """All tiles' reaches as one frame (row order: tile by tile) and the edge vertices (rows into the frame)."""
    parts, ex, ek, el, cores = [], [], [], [], {}
    n = 0
    for tid in tiles:
        T, E = read_tile(tid, root)
        parts.append(T)
        ex.append(E["xy"])
        ek.append(E["k"] + n)
        el.append(E["last"])
        cores[tid] = E["core"]
        n += len(T)
    T = pd.concat(parts, ignore_index=True)
    T["tile"] = T["tile"].astype("category")
    edge = {"xy": np.concatenate(ex) if ex else np.zeros((0, 2)), "k": np.concatenate(ek) if ek else np.zeros(0, int),
            "last": np.concatenate(el) if el else np.zeros(0, bool)}
    if not T["gid"].is_unique:
        raise RuntimeError("duplicate gids across tiles")
    log(f"merge: {len(tiles)} tiles, {len(T)} core reaches, {len(edge['xy'])} edge vertices")
    return T, edge, cores


# ---------------------------------------------------------------------------------------------- network
@njit(cache=True)
def kahn(down):
    """Topological order (upstream first) over ``down`` (-1 outlet); nodes on or above a cycle are left out."""
    n = len(down)
    indeg = np.zeros(n, np.int32)
    for i in range(n):
        if down[i] >= 0:
            indeg[down[i]] += 1
    order = np.empty(n, np.int64)
    t = 0
    for i in range(n):
        if indeg[i] == 0:
            order[t] = i
            t += 1
    h = 0
    while h < t:
        v = order[h]
        h += 1
        d = down[v]
        if d >= 0:
            indeg[d] -= 1
            if indeg[d] == 0:
                order[t] = d
                t += 1
    return order[:t]


@njit(cache=True)
def acc_sum(order, down, v):
    a = v.copy()
    for k in range(len(order)):
        i = order[k]
        if down[i] >= 0:
            a[down[i]] += a[i]
    return a


@njit(cache=True)
def acc_max(order, down, v):
    a = v.copy()
    for k in range(len(order)):
        i = order[k]
        d = down[i]
        if d >= 0 and a[i] > a[d]:
            a[d] = a[i]
    return a


def find_cycles(down: np.ndarray, order: np.ndarray) -> list[list[int]]:
    """The cycles of ``down`` (rows), each once, from the nodes the topological sort left out."""
    left = np.ones(len(down), bool)
    left[order] = False
    seen = np.zeros(len(down), np.int8)        # 0 new, 1 on the current walk, 2 done
    out = []
    for s in np.flatnonzero(left):
        if seen[s]:
            continue
        path, x = [], int(s)
        while x >= 0 and seen[x] == 0:
            seen[x] = 1
            path.append(x)
            x = int(down[x])
        if x >= 0 and seen[x] == 1:
            out.append(path[path.index(x):])
        for p in path:
            seen[p] = 2
    return out


def break_cycles(down, stitch, area, log=print) -> list[dict]:
    """Cut every cycle of ``down`` at its weakest link (highest ``stitch`` code, then the smallest area), in place;
    returns one record per cycle."""
    cut = []
    while True:
        order = kahn(down)
        if len(order) == len(down):
            return cut
        for cyc in find_cycles(down, order):
            k = max(cyc, key=lambda i: (int(stitch[i]), -float(area[i])))
            cut.append({"rows": [int(i) for i in cyc], "cut_row": int(k), "cut_stitch": int(stitch[k])})
            down[k], stitch[k] = -1, STITCH_NONE
            log(f"merge: cycle of {len(cyc)} reaches cut at row {k} (stitch {cut[-1]['cut_stitch']})")


def link_in_tile(T: pd.DataFrame, pos: pd.Index) -> tuple[np.ndarray, np.ndarray]:
    """``down`` (rows) and ``stitch`` from the tiles' own links: a core reach whose ``down_seg`` is a core reach."""
    gid = T["gid"].to_numpy(np.int64)
    ds = T["down_seg"].to_numpy(np.int64)
    ok = (ds >= 0) & ~T["exits_core"].to_numpy(bool)
    dg = (gid // S.SEG_BASE) * S.SEG_BASE + ds
    down = np.full(len(T), -1, np.int64)
    j = pos.get_indexer(dg[ok])
    if (j < 0).any():
        raise RuntimeError(f"{int((j < 0).sum())} in-tile links point to no core reach")
    down[ok] = j
    stitch = np.where(ok, STITCH_IN, STITCH_NONE).astype(np.uint8)
    return down, stitch


def crosses_into(x: np.ndarray, y: np.ndarray, tile: np.ndarray, cores: dict) -> np.ndarray:
    """The tile across the side of its own core nearest to each exit's end point (where the reach leaves the core):
    an id ``q<ix>y<iy>``. An unstitched exit into a merged tile is a failed stitch; into another, a tile not merged."""
    out = np.empty(len(x), object)
    for k, (xx, yy, t) in enumerate(zip(x, y, tile, strict=True)):
        c = cores[str(t)]
        d = [abs(xx - c[0]), abs(xx - c[2]), abs(yy - c[1]), abs(yy - c[3])]
        q = N.Tile32.parse(str(t))
        dx, dy = [(-1, 0), (1, 0), (0, -1), (0, 1)][int(np.argmin(d))]
        out[k] = N.Tile32(q.ix + dx, q.iy + dy).id
    return out


def _edge_lines(edge: dict):
    """The edge reaches' lines (rebuilt from the kept vertices) and their rows."""
    import shapely

    k = edge["k"]
    if not len(k):
        return np.zeros(0, object), np.zeros(0, np.int64)
    cut = np.flatnonzero(np.r_[True, k[1:] != k[:-1], True])
    rows = k[cut[:-1]]
    lines = shapely.linestrings(edge["xy"], indices=np.repeat(np.arange(len(rows)), np.diff(cut)))
    return lines, rows


def stitch_edges(T: pd.DataFrame, edge: dict, down: np.ndarray, stitch: np.ndarray, cores: dict | None = None,
                 stitch_m: float = STITCH_M, ratio: tuple[float, float] = STITCH_RATIO) -> dict:
    """Link each reach that leaves its core (and has no link yet) to the reach of another tile whose line passes its
    end point: on the line (within ``EXACT_M``: the same 2 m cell, so the same path from there on) whatever the
    in-tile areas (the receiving tile's REC2 inflow can be off), else within ``stitch_m`` with an in-tile area ratio
    within ``ratio``. A line that only ends at the point is skipped (the point starts the reach below it). Then
    ``stitch_entries`` for the rest. In place; returns counts."""
    import shapely

    ex = np.flatnonzero(T["exits_core"].to_numpy(bool) & (down < 0))
    st = {"exits": int(len(ex)), "stitched": 0, "exact": 0, "no_candidate": 0, "area_ratio": 0, "entry": 0}
    lines, rows = _edge_lines(edge)
    if not len(ex) or not len(lines):
        st["no_candidate"] = int(len(ex))
        return st
    tile = T["tile"].cat.codes.to_numpy()
    area = T["area_tile_km2"].to_numpy(float)
    end = T[["end_x", "end_y"]].to_numpy(float)
    pts = shapely.points(end[ex])
    pi, li = shapely.STRtree(lines).query(pts, predicate="dwithin", distance=stitch_m)
    d = shapely.distance(pts[pi], lines[li])
    sp = shapely.line_locate_point(lines[li], pts[pi])
    at_end = (shapely.length(lines[li]) - sp < 0.5) & (d < 0.5)
    cands: dict[int, list] = {}
    for a, b, dd, e in zip(pi.tolist(), li.tolist(), d.tolist(), at_end.tolist(), strict=True):
        cands.setdefault(a, []).append((int(rows[b]), dd, e))
    for q, i in enumerate(ex):
        best, bd, any_c, exact = -1, np.inf, False, False
        for j, dd, e in cands.get(q, ()):
            if tile[j] == tile[i] or e:
                continue
            any_c = True
            r = area[j] / max(area[i], 1e-9)
            if dd < EXACT_M:
                score = dd - 1e6
            elif ratio[0] <= r <= ratio[1]:
                score = dd * dd + abs(math.log(r))
            else:
                continue
            if score < bd:
                best, bd, exact = j, score, dd < EXACT_M
        if best >= 0:
            down[i], stitch[i] = best, STITCH_EDGE
            st["stitched"] += 1
            st["exact"] += int(exact)
        elif any_c:
            st["area_ratio"] += 1
        else:
            st["no_candidate"] += 1
    st["entry"] = stitch_entries(T, down, stitch, cores, ratio)
    return st


def stitch_entries(T: pd.DataFrame, down: np.ndarray, stitch: np.ndarray, cores: dict | None = None,
                   ratio=STITCH_RATIO, radius_m: float = ENTRY_M) -> int:
    """The second stitch, for exits whose end point lies on no other tile's core reach: the two tiles cut the
    river's link into reaches differently (its head or confluence is outside one box), so the stretch after the end
    point is a halo reach in both. Link to the nearest *entry* of another tile (a core reach no core reach of its
    tile drains into) whose first vertex is within ``radius_m``, not higher than the exit's end (``z_top`` <=
    ``z_bot`` + 0.5 m) and of a similar area. In place; returns the count. The cells that drain first into the
    unowned stretch are counted by neither tile (a small area loss)."""
    from scipy.spatial import cKDTree

    ex = np.flatnonzero(T["exits_core"].to_numpy(bool) & (down < 0))
    if not len(ex):
        return 0
    gid = T["gid"].to_numpy(np.int64)
    ds = T["down_seg"].to_numpy(np.int64)
    inner = ~T["exits_core"].to_numpy(bool) & (ds >= 0)
    fed = set(((gid[inner] // S.SEG_BASE) * S.SEG_BASE + ds[inner]).tolist())
    entry = np.flatnonzero(~np.isin(gid, list(fed)))
    if not len(entry):
        return 0
    tile = T["tile"].cat.codes.to_numpy()
    area = T["area_tile_km2"].to_numpy(float)
    st_xy = T[["start_x", "start_y"]].to_numpy(float)
    end = T[["end_x", "end_y"]].to_numpy(float)
    ztop, zbot = T["z_top"].to_numpy(float), T["z_bot"].to_numpy(float)
    tree = cKDTree(st_xy[entry])
    names = T["tile"].astype(str).to_numpy()
    into = crosses_into(end[ex, 0], end[ex, 1], names[ex], cores) if cores else None
    n = 0
    for q, (i, h) in enumerate(zip(ex, tree.query_ball_point(end[ex], radius_m), strict=True)):
        best, bd = -1, np.inf
        for c in h:
            j = int(entry[c])
            r = area[j] / max(area[i], 1e-9)
            if tile[j] == tile[i] or not ratio[0] <= r <= ratio[1] or ztop[j] > zbot[i] + 0.5:
                continue
            if into is not None and names[j] != into[q]:
                continue
            dd = float(np.hypot(*(st_xy[j] - end[i]))) + 100.0 * abs(math.log(r))
            if dd < bd:
                best, bd = j, dd
        if best >= 0:
            down[i], stitch[i] = best, STITCH_EDGE
            n += 1
    return n


def _walk_to(x: int, target: int, down: np.ndarray, cap: int = 100_000) -> bool:
    n = 0
    while x >= 0 and n < cap:
        if x == target:
            return True
        x = int(down[x])
        n += 1
    return False


def _in_any_core(x: float, y: float, cores: dict) -> bool:
    return any(c[0] <= x < c[2] and c[1] <= y < c[3] for c in cores.values())


def lake_nodes(T: pd.DataFrame, L, R: pd.DataFrame, pos: pd.Index, down: np.ndarray, stitch: np.ndarray,
               cores: dict | None = None, log=print) -> tuple[np.ndarray, dict]:
    """Lakes as single nodes, in place: per national lake (not tidal) with an outlet reach O, every member reach (on
    the lake in some tile's record) whose path leaves the lake anywhere but at O, and every reach without a link
    whose end point lies in the lake, links to O (``stitch`` 2). Members on O's own downstream path are left as they
    are (no cycle). Returns ``lake_id`` per row (member, else the lake its end point lies in, else -1) and a summary
    per lake."""
    import shapely
    from scipy.spatial import cKDTree

    n = len(T)
    lake_id = np.full(n, -1, np.int64)
    kind = dict(zip(L["lake_id"], L["kind"], strict=True))
    for lid, reaches in zip(R["lake_id"], R["reaches"], strict=True):
        if kind.get(int(lid)) in ("hole", "flat") and len(reaches):
            j = pos.get_indexer(np.asarray(reaches, np.int64))
            lake_id[j[j >= 0]] = int(lid)
    member = lake_id.copy()
    end = T[["end_x", "end_y"]].to_numpy(float)
    lakes = L[L["kind"].isin(["hole", "flat"])]
    geoms = lakes.geometry.to_numpy()
    if len(lakes):
        tree = shapely.STRtree(geoms)
        free = np.flatnonzero(lake_id < 0)
        pi, gi = tree.query(shapely.points(end[free]), predicate="intersects")
        lake_id[free[pi]] = lakes["lake_id"].to_numpy()[gi]
    area = T["area_tile_km2"].to_numpy(float)
    gid = T["gid"].to_numpy(np.int64)
    pts = np.vstack([T[["start_x", "start_y"]].to_numpy(float), T[["mid_x", "mid_y"]].to_numpy(float), end])
    ptree = cKDTree(pts)
    summary = {}
    for _, lk in lakes.iterrows():
        lid = int(lk["lake_id"])
        o, how = -1, "none"
        if int(lk["outlet_gid"]) >= 0 and pos.get_indexer([int(lk["outlet_gid"])])[0] >= 0:
            o, how = int(pos.get_indexer([int(lk["outlet_gid"])])[0]), "tile record"
        elif np.isfinite(lk["outlet_x"]):
            c = ptree.query_ball_point((lk["outlet_x"], lk["outlet_y"]), LAKE_OUTLET_M)
            best = None
            for q in c:
                j = q % n
                r = area[j] / max(float(lk["outlet_area_tile_km2"]), 1e-9)
                if member[j] == lid or not 0.5 <= r <= 3.0:
                    continue
                d = float(np.hypot(*(pts[q] - (lk["outlet_x"], lk["outlet_y"]))))
                if best is None or (d, gid[j]) < best[:2]:
                    best = (d, gid[j], j)
            if best is not None:
                o, how = int(best[2]), "nearest reach"
        mem = np.flatnonzero(member == lid)
        outside = bool(o < 0 and np.isfinite(lk["outlet_x"]) and cores is not None
                       and not _in_any_core(float(lk["outlet_x"]), float(lk["outlet_y"]), cores))
        rec = {"lake_id": lid, "kind": lk["kind"], "km2": float(lk["km2"]), "members": int(len(mem)),
               "outlet_row": o, "outlet_gid": int(gid[o]) if o >= 0 else -1, "outlet_from": how, "redirected": 0,
               "enders": 0, "outlet_outside": outside}
        if o < 0:
            summary[lid] = rec
            continue
        below, x = set(), o
        while x >= 0 and x not in below:
            below.add(x)
            x = int(down[x])
        ok_memo: dict[int, bool] = {}
        for r in mem:
            if r == o or r in below:
                continue
            x, chain = int(r), []
            while (x >= 0 and member[x] == lid and x != o and x not in ok_memo and x not in below
                   and len(chain) < 100_000):
                chain.append(x)
                x = int(down[x])
            if x in ok_memo:
                good = ok_memo[x]
            elif x in below and x != o:            # joins the river below the outlet without passing it
                good = False
            else:
                good = x == o or (x >= 0 and _walk_to(x, o, down))
                if x >= 0 and x != o:
                    ok_memo[x] = good
            if not good and chain:
                t = chain[-1]
                down[t], stitch[t] = o, STITCH_LAKE
                rec["redirected"] += 1
                good = True
            for c in chain:
                ok_memo[c] = good
        ex = np.flatnonzero((lake_id == lid) & (down < 0) & T["exits_core"].to_numpy(bool))
        for r in ex:
            if r != o and int(r) not in below:
                down[r], stitch[r] = o, STITCH_LAKE
                rec["enders"] += 1
        summary[lid] = rec
    return lake_id, summary


def relink_rec2(T: pd.DataFrame, edge: dict, down: np.ndarray, stitch: np.ndarray, rec2, cores: dict | None = None,
                log=print) -> dict:
    """The REC2 relink of the 8 m merge (``relink.relink_outlets``) as the fallback for reaches that left their core
    and found no stitch: in-tile areas with the tiles' REC2 inflows say where the continuation took the inflow in.
    The reaches' lines are the edge vertices plus every reach's start, middle and end. Only exits into a merged tile
    are eligible (one into a tile that is not merged has nothing to join; nationally every exit goes into a tile).
    In place; returns counts."""
    from .relink import relink_outlets

    elig = T["exits_core"].to_numpy(bool) & (down < 0)
    if cores and elig.any():
        e = np.flatnonzero(elig)
        into = crosses_into(T["end_x"].to_numpy()[e], T["end_y"].to_numpy()[e], T["tile"].astype(str).to_numpy()[e],
                            cores)
        elig[e[~np.isin(into.astype(str), list(cores))]] = False
    st = {"eligible": int(elig.sum()), "linked": 0, "status": {}}
    if not elig.any() or rec2 is None:
        return st
    n = len(T)
    seg_xy = np.vstack([edge["xy"], T[["start_x", "start_y"]].to_numpy(float), T[["mid_x", "mid_y"]].to_numpy(float),
                        T[["end_x", "end_y"]].to_numpy(float)])
    seg_row = np.r_[edge["k"], np.arange(n), np.arange(n), np.arange(n)]
    new, lg = relink_outlets(down, T["area_tile_km2"].to_numpy(float), T[["end_x", "end_y"]].to_numpy(float), elig,
                             seg_xy, seg_row, rec2, local_km2=T["local_km2"].to_numpy(float),
                             key=T["gid"].to_numpy(np.int64), length=T["length_m"].to_numpy(float))
    ch = (new != down) & elig
    down[ch] = new[ch]
    stitch[ch] = STITCH_REC2
    st["linked"] = int(ch.sum())
    st["status"] = {k: int(v) for k, v in lg["status"].value_counts().items()} if len(lg) else {}
    return st


# ---------------------------------------------------------------------------------------------- fields
def network_fields(T: pd.DataFrame, down: np.ndarray, order: np.ndarray) -> dict:
    """The area and catchment fields of ``lidarspec`` (stage ``merge``) by network accumulation."""
    loc = T["local_km2"].to_numpy(np.float64)
    a = acc_sum(order, down, loc)
    out = {"area_km2": a}
    at = T["area_tile_km2"].to_numpy(np.float64)
    atm = T["area_tile_mid_km2"].to_numpy(np.float64)
    with np.errstate(invalid="ignore", divide="ignore"):
        amid = np.where(at > 0, atm * a / np.where(at > 0, at, 1.0), a)
        amid = np.minimum(np.where(np.isfinite(amid), amid, a), a)
        out["area_mid_km2"] = amid
        out["area_check"] = np.where(a > 0, at / np.where(a > 0, a, 1.0), np.nan)
        zs = np.where(loc > 0, np.nan_to_num(T["local_z_mean_m"].to_numpy(np.float64)) * loc, 0.0)
        zmean = acc_sum(order, down, zs) / np.where(a > 0, a, np.nan)
        zx = acc_max(order, down, np.nan_to_num(T["local_z_max_m"].to_numpy(np.float64), nan=-1e9))
        zmid = 0.5 * (T["z_top"].to_numpy(np.float64) + T["z_bot"].to_numpy(np.float64))
        out["head_m"] = zmean - zmid
        out["relief_m"] = np.where(zx > -1e8, zx - zmid, np.nan)
        out["melton"] = out["relief_m"] / np.sqrt(np.maximum(amid, 1e-6) * 1e6)
        for src, name in LOCAL_SUMS.items():
            out[name] = np.clip(acc_sum(order, down, T[src].to_numpy(np.float64)) / np.where(a > 0, a, np.nan), 0, 1)
        g = T["gradient_m_per_km"].to_numpy(np.float64)
        ksn = g / 1000.0 * (np.maximum(amid, 0.0) * 1e6) ** THETA
        out["ksn"] = ksn
        out["knick_ratio"] = np.where(down >= 0, ksn / np.maximum(ksn[np.maximum(down, 0)], 1.0), np.nan)
    return out


def truncation(T: pd.DataFrame, pos: pd.Index, down: np.ndarray, order: np.ndarray, area: np.ndarray):
    """``truncated`` and the area missing upstream of each reach. A reach takes in area from outside its tile's core
    (``ext_in``: its in-tile area less its local area and its in-tile core upstream reaches' in-tile areas; the
    halo reaches above it and the REC2 inflows). That area must come from another tile's reaches linked into it; when
    none is, the reach is an open entry and ``ext_in`` is missing. Missing areas are summed down the network; a reach
    is truncated when the missing area is over ``TRUNC_MIN_KM2`` and over ``TRUNC_TOL`` of the true area (network area
    + missing). The in-tile areas only say *that* area enters, not how much: a linked entry is complete whatever the
    receiving tile's REC2 inflow said (on plateau divides REC2 and the LiDAR disagree)."""
    gid = T["gid"].to_numpy(np.int64)
    ds = T["down_seg"].to_numpy(np.int64)
    inner = ~T["exits_core"].to_numpy(bool) & (ds >= 0)
    dn0 = np.full(len(T), -1, np.int64)
    dn0[inner] = pos.get_indexer((gid[inner] // S.SEG_BASE) * S.SEG_BASE + ds[inner])
    at = T["area_tile_km2"].to_numpy(np.float64)
    up_tile = np.bincount(dn0[dn0 >= 0], weights=at[dn0 >= 0], minlength=len(T))
    ext_in = np.maximum(at - T["local_km2"].to_numpy(np.float64) - up_tile, 0.0)
    tcode = T["tile"].cat.codes.to_numpy()
    ok = down >= 0
    cross = ok & (tcode != tcode[np.maximum(down, 0)])
    linked = np.zeros(len(T), bool)
    linked[down[cross]] = True
    missing = np.where(~linked & (ext_in >= TRUNC_MIN_KM2), ext_in, 0.0)
    miss = acc_sum(order, down, missing)
    trunc = (miss > TRUNC_MIN_KM2) & (miss > TRUNC_TOL * (area + miss))
    return trunc, miss, missing > 0


def sinuosity_1km(T: pd.DataFrame, down: np.ndarray, area: np.ndarray, half_m: float = 500.0) -> np.ndarray:
    """Channel length / straight distance over ``2 half_m`` of main stem centred on the reach's middle: up the main
    upstream reach (largest area), down ``down_gid``; the ends are interpolated along the reaches' start-end chords."""
    from .relink import upstream_main

    n = len(T)
    up = upstream_main(down, area, T["gid"].to_numpy(np.int64))
    L = np.maximum(T["length_m"].to_numpy(float), 1e-3)
    sx, sy = T["start_x"].to_numpy(float), T["start_y"].to_numpy(float)
    ex, ey = T["end_x"].to_numpy(float), T["end_y"].to_numpy(float)
    return _sinuosity(down, up, L, sx, sy, ex, ey, half_m, n)


@njit(cache=True)
def _sinuosity(down, up, L, sx, sy, ex, ey, half_m, n):
    out = np.full(n, np.nan)
    for i in range(n):
        # downstream end: from the middle of i, half_m along the network
        rem = half_m - 0.5 * L[i]
        x = i
        while rem > 0 and down[x] >= 0:
            x = down[x]
            rem -= L[x]
        f = 1.0 if rem > 0 else 1.0 + rem / L[x]
        dx, dy = sx[x] + f * (ex[x] - sx[x]), sy[x] + f * (ey[x] - sy[x])
        got = half_m - max(rem, 0.0)
        rem = half_m - 0.5 * L[i]
        x = i
        while rem > 0 and up[x] >= 0:
            x = up[x]
            rem -= L[x]
        f = 0.0 if rem > 0 else -rem / L[x]
        ux, uy = sx[x] + f * (ex[x] - sx[x]), sy[x] + f * (ey[x] - sy[x])
        got += half_m - max(rem, 0.0)
        dist = math.hypot(dx - ux, dy - uy)
        if got >= 0.5 * half_m and dist > 1.0:
            out[i] = got / dist
    return out


def rec2_frame(bounds, pad: float = 5000.0):
    """The REC2 reaches over ``bounds`` (+ ``pad``), with ``HydroID`` / ``NextDownID`` and the names."""
    import geopandas as gpd

    from .national import nz_dir

    x0, y0, x1, y1 = bounds
    return gpd.read_file(nz_dir() / "rec2.gpkg", bbox=(x0 - pad, y0 - pad, x1 + pad, y1 + pad))


def rec2_match(T: pd.DataFrame, down: np.ndarray, area: np.ndarray, area_mid: np.ndarray, rec2,
               rounds: int = 2) -> pd.DataFrame:
    """REC2 matched along the network (rf-33a.4): per reach, the REC2 reaches within clip(20 sqrt(area), 100, 400) m
    of its midpoint, with REC2's area at the projection of the midpoint (upstream end + share of its own catchment,
    linear along the line) within 1/3-3x of ``area_mid``, scored by (distance / 100 m)^2 + (ln ratio / ln 1.25)^2.
    Then, ``rounds`` times, a reach whose match is not on the REC2 path of the reach below's match (same REC2 reach
    or up to 4 REC2 reaches upstream of it) takes its best candidate that is, when one scores within 4 of its best.
    Returns per row: ``rec2_nzsegment`` (-1 none), ``rec2_area_km2`` (REC2 CUM_AREA), ``rec2_area_at_km2`` (REC2's
    area at the midpoint), ``rec2_dist_m``, ``river_rec2`` (the name, by the spec's 150 m / 0.6-1.6x rule)."""
    import shapely

    n = len(T)
    out = pd.DataFrame({"rec2_nzsegment": np.full(n, -1, np.int64), "rec2_area_km2": np.full(n, np.nan),
                        "rec2_area_at_km2": np.full(n, np.nan), "rec2_dist_m": np.full(n, np.nan),
                        "river_rec2": np.full(n, "", object)})
    if rec2 is None or not len(rec2):
        return out
    geoms = rec2.geometry.to_numpy()
    cum = rec2["cum_area_km2"].to_numpy(float)
    cat = rec2["cat_area_km2"].to_numpy(float)
    hid = rec2["HydroID"].to_numpy()
    nxt = pd.Series(np.arange(len(rec2)), index=hid).reindex(rec2["NextDownID"].to_numpy()).fillna(-1).astype(
        np.int64).to_numpy()
    names = rec2["name"].fillna("").astype(str).str.strip().to_numpy()
    tree = shapely.STRtree(geoms)
    mid = shapely.points(T[["mid_x", "mid_y"]].to_numpy(float))
    rad = np.clip(20.0 * np.sqrt(np.maximum(area_mid, 0)), 100.0, 400.0)
    pi, gi = tree.query(mid, predicate="dwithin", distance=rad)
    if not len(pi):
        return out
    d = shapely.distance(mid[pi], geoms[gi])
    t = shapely.line_locate_point(geoms[gi], mid[pi], normalized=True)
    at = np.maximum(cum[gi] - cat[gi], 0.0) + t * cat[gi]
    r = at / np.maximum(area_mid[pi], 1e-9)
    ok = (r >= 1 / 3) & (r <= 3)
    pi, gi, d, at, r = pi[ok], gi[ok], d[ok], at[ok], r[ok]
    score = (d / 100.0) ** 2 + (np.log(r) / math.log(1.25)) ** 2
    o = np.lexsort((score, pi))
    pi, gi, d, at, r, score = pi[o], gi[o], d[o], at[o], r[o], score[o]
    start = np.searchsorted(pi, np.arange(n))
    stop = np.searchsorted(pi, np.arange(n), side="right")
    best = np.where(stop > start, start, -1)

    def on_path(c_up: int, c_dn: int) -> bool:
        x = c_up
        for _ in range(5):
            if x == c_dn:
                return True
            if x < 0:
                return False
            x = int(nxt[x])
        return False

    for _ in range(rounds):
        changed = 0
        for i in np.flatnonzero((best >= 0) & (down >= 0)):
            dn = int(down[i])
            if best[dn] < 0 or on_path(int(gi[best[i]]), int(gi[best[dn]])):
                continue
            cdn = int(gi[best[dn]])
            for q in range(start[i], stop[i]):
                if score[q] > score[best[i]] + 4.0:
                    break
                if on_path(int(gi[q]), cdn):
                    best[i] = q
                    changed += 1
                    break
        if not changed:
            break
    m = best >= 0
    b = best[m]
    out.loc[m, "rec2_nzsegment"] = rec2["nzsegment"].to_numpy(np.int64)[gi[b]]
    out.loc[m, "rec2_area_km2"] = cum[gi[b]]
    out.loc[m, "rec2_area_at_km2"] = at[b]
    out.loc[m, "rec2_dist_m"] = d[b]
    nm = (d[b] <= REC2_NAME_M) & (r[b] >= REC2_NAME_RATIO[0]) & (r[b] <= REC2_NAME_RATIO[1])
    rows = np.flatnonzero(m)
    out.loc[rows[nm], "river_rec2"] = names[gi[b[nm]]]
    return out


def rec1_match(T: pd.DataFrame, area_mid: np.ndarray, area: np.ndarray, flood) -> pd.DataFrame:
    """The MAF of the nearest REC1 reach within ``REC1_M`` of the midpoint with area 0.4-2.5x ``area_mid`` (the rule
    of ``reaches._attach_flood``, distance + 100 x ln ratio): ``specific_maf`` (unmatched: the median of the matched),
    ``maf_m3s`` (specific x area_km2, before ``maf.apply``), ``specific_maf_matched``, ``rec1_nzreach``."""
    import shapely

    n = len(T)
    out = pd.DataFrame({"specific_maf": np.full(n, np.nan), "maf_m3s": np.full(n, np.nan),
                        "specific_maf_matched": np.zeros(n, bool), "rec1_nzreach": np.full(n, -1, np.int64)})
    if flood is None or not len(flood):
        return out
    geoms = flood.geometry.to_numpy()
    a1 = flood["Areakm2"].to_numpy(float)
    spec = np.where(a1 > 0, flood["H_C18_MAF"].to_numpy(float) / np.where(a1 > 0, a1, 1.0), np.nan)
    mid = shapely.points(T[["mid_x", "mid_y"]].to_numpy(float))
    pi, gi = shapely.STRtree(geoms).query(mid, predicate="dwithin", distance=REC1_M)
    if len(pi):
        d = shapely.distance(mid[pi], geoms[gi])
        r = a1[gi] / np.maximum(area_mid[pi], 1e-9)
        ok = (r >= REC1_RATIO[0]) & (r <= REC1_RATIO[1]) & np.isfinite(spec[gi])
        pi, gi, d, r = pi[ok], gi[ok], d[ok], r[ok]
        sc = d ** 2 + (np.log(r) * 100) ** 2
        o = np.lexsort((sc, pi))
        first = np.r_[True, pi[o][1:] != pi[o][:-1]] if len(o) else np.zeros(0, bool)
        bi, bg = pi[o][first], gi[o][first]
        out.loc[bi, "specific_maf"] = spec[bg]
        out.loc[bi, "specific_maf_matched"] = True
        out.loc[bi, "rec1_nzreach"] = flood["NZREACH"].to_numpy(np.int64)[bg]
    s = out["specific_maf"].to_numpy(float)
    fill = np.nanmedian(s) if np.isfinite(s).any() else np.nan
    s = np.where(np.isfinite(s), s, fill)
    out["specific_maf"] = np.round(s, 4)
    out["maf_m3s"] = np.round(s * area, 3)
    return out


def karst_up(T: pd.DataFrame, down, order, area) -> np.ndarray:
    """``karst_up_frac``: local area on karst bed rock (GNS main rock at the reach midpoint, ``ml.KARST_ROCKS``)
    accumulated down the network / area_km2. NaN without ``geology.gpkg``."""
    import geopandas as gpd

    from .ml import KARST_ROCKS
    from .national import nz_dir

    p = nz_dir() / "geology.gpkg"
    if not p.exists():
        return np.full(len(T), np.nan)
    x0, y0 = T["mid_x"].min(), T["mid_y"].min()
    x1, y1 = T["mid_x"].max(), T["mid_y"].max()
    geo = gpd.read_file(p, bbox=(x0, y0, x1, y1), columns=["mainrock"])
    pts = gpd.GeoDataFrame(geometry=gpd.points_from_xy(T["mid_x"], T["mid_y"]), crs="EPSG:2193")
    j = gpd.sjoin(pts, geo, predicate="within", how="left")
    j = j[~j.index.duplicated()].reindex(pts.index)
    kb = j["mainrock"].fillna("").astype(str).str.strip().str.lower().isin(KARST_ROCKS).to_numpy()
    k = acc_sum(order, down, T["local_km2"].to_numpy(np.float64) * kb)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.clip(k / np.where(area > 0, area, np.nan), 0, 1)


# ---------------------------------------------------------------------------------------------- the merge
def merge(tiles: list[str] | None = None, root: Path | None = None, out: Path | None = None, rec2=True, flood=True,
          regions: bool = True, geology: bool = True, write: bool = True, log=print) -> dict:
    """Merge the done tiles (default: all) into one network -> ``out`` (default ``data/nz1m``). ``rec2`` / ``flood``:
    True reads ``data/nz/rec2.gpkg`` / ``flood.gpkg`` over the tiles, a frame uses it, None skips (relink, REC2 match,
    MAF). Returns ``{"T": the national table (no lines), "lakes": ..., "summary": ...}``."""
    t0 = time.time()
    root = root or N.root_dir()
    out = out or root
    tiles = tiles or LK.done_tiles(root)
    tim = {}

    def lap(k, t):
        tim[k] = round(time.time() - t, 1)
        return time.time()

    t = time.time()
    T, edge, cores = read_tiles(tiles, root, log)
    t = lap("read", t)
    L, R, rerun = LK.build(tiles, root, windows=True, write=False, log=log)
    t = lap("lakes", t)
    pos = pd.Index(T["gid"].to_numpy(np.int64))
    down, stitch = link_in_tile(T, pos)
    st_edge = stitch_edges(T, edge, down, stitch, cores)
    t = lap("stitch", t)
    lake_id, lake_sum = lake_nodes(T, L, R, pos, down, stitch, cores, log)
    t = lap("lake_nodes", t)
    ext = [min(c[0] for c in cores.values()), min(c[1] for c in cores.values()),
           max(c[2] for c in cores.values()), max(c[3] for c in cores.values())]
    rec2f = rec2_frame(ext) if rec2 is True else (rec2 if rec2 is not None and rec2 is not False else None)
    st_rel = {"eligible": 0, "linked": 0}
    if rec2f is not None and len(rec2f):
        from .relink import Rec2Graph

        st_rel = relink_rec2(T, edge, down, stitch, Rec2Graph.from_frame(rec2f), cores, log)
    t = lap("relink", t)
    cycles = break_cycles(down, stitch, T["area_tile_km2"].to_numpy(float), log)
    order = kahn(down)
    assert len(order) == len(T)
    t = lap("cycles", t)
    F = network_fields(T, down, order)
    t = lap("accumulate", t)
    gid = T["gid"].to_numpy(np.int64)
    exits = T["exits_core"].to_numpy(bool)
    ok = down >= 0
    okind = np.where(ok, OUT_NONE, np.where(~exits & (T["down_seg"].to_numpy() < 0), OUT_SEA, OUT_EDGE))
    sink = {lid for lid, s in lake_sum.items() if s["outlet_row"] < 0 and not s["outlet_outside"]}
    okind = np.where(~ok & exits & np.isin(lake_id, list(sink)), OUT_SINK, okind).astype(np.uint8)
    stitch = np.where(~ok & ~exits & (T["down_seg"].to_numpy() < 0), STITCH_IN, stitch).astype(np.uint8)
    kind = dict(zip(L["lake_id"], L["kind"], strict=True))
    net = pd.DataFrame({"gid": gid, "down_gid": np.where(ok, gid[np.maximum(down, 0)], -1).astype(np.int64),
                        "stitch": stitch, "outlet_kind": okind, "lake_id": lake_id.astype(np.int32)})
    for k in ("area_km2", "area_mid_km2", "area_check", "ksn", "knick_ratio", "head_m", "relief_m", "melton",
              "upstream_flat_frac", "upstream_hard_frac", "upstream_lake_frac", "catch_lidar_share"):
        net[k] = F[k].astype(np.float32)
    trunc, miss, open_ = truncation(T, pos, down, order, F["area_km2"])
    net["truncated"] = trunc
    net["missing_up_km2"] = miss.astype(np.float32)
    net["sinuosity_1km"] = sinuosity_1km(T, down, F["area_km2"]).astype(np.float32)
    net["karst_up_frac"] = (karst_up(T, down, order, F["area_km2"]) if geology else np.full(len(T), np.nan)).astype(
        np.float32)
    # the tile fields the lake table corrects: lake (not on tidal water), dem_source "lake" (tiles before rf-lxa.5)
    on_lake = np.array([kind.get(int(v)) in ("hole", "flat") for v in lake_id])
    net["lake"] = T["lake"].to_numpy(bool) & on_lake
    hole_member = np.isin(lake_id, [k for k, v in kind.items() if v == "hole"]) & T["lake"].to_numpy(bool)
    ds = T["dem_source"].astype(str).to_numpy()
    net["dem_source"] = np.where(hole_member & np.isin(ds, ["8m", "mixed"]) & (T["lidar_share"].to_numpy() <= 0.5),
                                 "lake", ds)
    t = lap("fields", t)
    rm = rec2_match(T, down, F["area_km2"], F["area_mid_km2"], rec2f)          # empty columns without REC2
    for c in rm.columns:
        net[c] = rm[c].to_numpy()
    net["river"] = net["river_rec2"]               # LINZ names (river_linz) are joined later (stage ext)
    t = lap("rec2_match", t)
    fl = None
    if flood is True:
        import geopandas as gpd

        from .national import nz_dir

        p = nz_dir() / "flood.gpkg"
        fl = gpd.read_file(p, bbox=(ext[0] - 5e3, ext[1] - 5e3, ext[2] + 5e3, ext[3] + 5e3)) if p.exists() else None
    elif flood is not None and flood is not False:
        fl = flood
    mf = rec1_match(T, F["area_mid_km2"], F["area_km2"], fl)
    for c in mf.columns:
        net[c] = mf[c].to_numpy()
    t = lap("rec1_match", t)
    if regions:
        import geopandas as gpd

        from .national import assign_regions

        net["region"] = assign_regions(gpd.GeoSeries(gpd.points_from_xy(T["mid_x"], T["mid_y"]), crs="EPSG:2193"))
    else:
        net["region"] = ""
    t = lap("regions", t)
    # lake outlets into the table
    L = L.copy()
    L["outlet_gid"] = [lake_sum.get(int(i), {}).get("outlet_gid", -1) for i in L["lake_id"]]
    L["members"] = [lake_sum.get(int(i), {}).get("members", 0) for i in L["lake_id"]]
    L["outlet_from"] = [lake_sum.get(int(i), {}).get("outlet_from", "") for i in L["lake_id"]]
    summary = _summary(T, net, tiles, st_edge, st_rel, lake_sum, cycles, rerun, L, cores)
    summary["open_entries"] = int(open_.sum())
    summary["open_entries_gt_10km2"] = int((open_ & (T["area_tile_km2"].to_numpy() > 10)).sum())
    if write:
        tw = time.time()
        files = write_outputs(out, tiles, root, net, L, summary)
        tim["write"] = round(time.time() - tw, 1)
        summary["files"] = files
    tim["total"] = round(time.time() - t0, 1)
    summary["timings"] = tim
    summary["peak_rss_gb"] = round(_rss_gb(), 2)
    if write:
        (out / "merge.json").write_text(json.dumps(summary, indent=1, default=N._jsonable))
    log(f"merge: {len(T)} reaches, {summary['links']} -> {out} ({tim['total']} s)")
    return {"T": T, "net": net, "lakes": L, "summary": summary, "down": down}


def _summary(T, net, tiles, st_edge, st_rel, lake_sum, cycles, rerun, L, cores) -> dict:
    a = net["area_km2"].to_numpy()
    st = net["stitch"].to_numpy()
    ok = net["outlet_kind"].to_numpy()
    names = {0: "in_tile", 1: "edge", 2: "lake", 3: "rec2", 4: "none"}
    okn = {0: "not_outlet", 1: "sea", 2: "sink", 3: "edge"}
    out_rows = net[net["outlet_kind"] > 0].assign(tile=T["tile"].astype(str).to_numpy()[net["outlet_kind"] > 0],
                                                  end_x=T["end_x"].to_numpy()[net["outlet_kind"] > 0],
                                                  end_y=T["end_y"].to_numpy()[net["outlet_kind"] > 0])
    unres = out_rows[out_rows["outlet_kind"] == OUT_EDGE].sort_values("area_km2", ascending=False)
    into = crosses_into(unres["end_x"].to_numpy(), unres["end_y"].to_numpy(), unres["tile"].to_numpy(), cores)
    unres = unres.assign(into=into, inside=np.isin(into.astype(str), list(cores)))
    return {
        "tiles": tiles, "reaches": int(len(T)), "km": round(float(T["length_m"].sum()) / 1e3, 1),
        "links": {names[k]: int((st == k).sum()) for k in names},
        "outlets": {okn[k]: int((ok == k).sum()) for k in okn if k},
        "outlets_gt_10km2": {okn[k]: int(((ok == k) & (a > 10)).sum()) for k in okn if k},
        "edge_stitch": st_edge, "rec2_relink": st_rel, "cycles": cycles,
        "unresolved_edge": {"inside": int(unres["inside"].sum()), "outside": int((~unres["inside"]).sum()),
                            "inside_gt_1km2": int((unres["inside"] & (unres["area_km2"] > 1)).sum()),
                            "outside_gt_10km2": int((~unres["inside"] & (unres["area_km2"] > 10)).sum())},
        "unresolved_edge_top": [{"gid": int(r.gid), "tile": r.tile, "area_km2": round(float(r.area_km2), 3),
                                 "end_x": round(float(r.end_x), 1), "end_y": round(float(r.end_y), 1),
                                 "lake_id": int(r.lake_id), "into": r.into, "inside": bool(r.inside)}
                                for r in unres.head(30).itertuples()],
        "truncated": int(net["truncated"].sum()), "truncated_gt_10km2": int((net["truncated"] & (a > 10)).sum()),
        "area_check_gt_1p1": int((net["area_check"] > 1.1).sum()),
        "area_check_lt_0p9": int((net["area_check"] < 0.9).sum()),
        "local_km2": round(float(T["local_km2"].sum()), 1),
        "lakes": {"n": int(len(L)), "by_kind": {k: int(v) for k, v in L["kind"].value_counts().items()},
                  "nodes": [v for v in lake_sum.values() if v["km2"] >= 1 or v["redirected"] or v["enders"]]},
        "lake_rerun": rerun,
    }


def _concat_arrow(paths: list[Path], dest: Path) -> int:
    import pyarrow as pa
    import pyarrow.feather as feather

    tabs = [feather.read_table(p) for p in paths if p.exists()]
    tabs = [t for t in tabs if t.num_rows]
    if not tabs:
        return 0
    tab = pa.concat_tables(tabs, promote_options="default")
    feather.write_feather(tab, dest, compression="zstd", compression_level=9)
    return dest.stat().st_size


def write_outputs(out: Path, tiles: list[str], root: Path, net: pd.DataFrame, L, summary: dict) -> dict:
    """``network.parquet``, ``reaches.parquet`` (streamed tile by tile: every tile column, the merge fields, the
    line as WKB with GeoParquet metadata), ``x1.arrow``, ``steps.arrow``, ``lakes.gpkg``."""
    import geopandas as gpd
    import pyarrow.parquet as pq
    import pyogrio
    from geopandas.io.arrow import _geopandas_to_arrow

    out.mkdir(parents=True, exist_ok=True)
    net.to_parquet(out / "network.parquet", index=False, compression="zstd")
    p = out / "reaches.parquet"
    p.unlink(missing_ok=True)
    nn = net.set_index("gid")
    over = [c for c in nn.columns if c in ("lake", "dem_source")]
    writer, schema = None, None
    try:
        for tid in tiles:
            g = pyogrio.read_dataframe(N.tile_dir(tid, root) / "segments_core.gpkg")
            j = nn.loc[g["gid"].to_numpy()]
            for c in over:
                g[c] = j[c].to_numpy()
            for c in nn.columns:
                if c not in over:
                    g[c] = j[c].to_numpy()
            # GeoParquet: geopandas' own writer path (WKB + "geo" metadata), streamed tile by tile
            tab = _geopandas_to_arrow(gpd.GeoDataFrame(g, geometry="geometry", crs="EPSG:2193"), index=False)
            if writer is None:
                schema = tab.schema
                writer = pq.ParquetWriter(p, schema, compression="zstd")
            writer.write_table(tab.cast(schema))
    finally:
        if writer is not None:
            writer.close()
    files = {"network.parquet": (out / "network.parquet").stat().st_size, "reaches.parquet": p.stat().st_size}
    files["x1.arrow"] = _concat_arrow([N.tile_dir(t, root) / "x1_reach.arrow" for t in tiles], out / "x1.arrow")
    files["steps.arrow"] = _concat_arrow([N.tile_dir(t, root) / "steps.arrow" for t in tiles], out / "steps.arrow")
    lp = out / "lakes.gpkg"
    lp.unlink(missing_ok=True)
    L.to_file(lp, layer="lakes", driver="GPKG")
    files["lakes.gpkg"] = lp.stat().st_size
    return files
