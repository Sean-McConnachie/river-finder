"""Reconnect national reaches that stop at a tile edge (rf-t26.8).

``nz.merge_segments`` stitches a reach that leaves its tile's core to the nearest reach of another
tile within ``stitch_m`` (24 m) whose area is 0.7 to 3 times its own. About 640 of the 7,300
national outlets fail that test although the river goes on:

- the two tiles route the river along different lines, mostly across lakes and flat valley floors
  (Lake Wakatipu's Frankton Arm at the head of the Kawarau, the Waikato at Horotiu: 25 m and 500 m);
- the reach joins a much bigger river just across the edge (area ratio above 3);
- the neighbour tile's version of the reach ends in its halo, so no reach of the next tile passes
  near the end point.

The other outlets drain into the sea (DEM nodata, which is all connected to the open sea).

The fix follows REC2. For each outlet that left its tile's core: find the REC2 reach at its end point,
walk REC2 downstream (through lakes and across tile edges), and take the first reach of ours that lies
on that path and drains at least (1 - ``area_tol``) of the outlet's area. When that reach is part of
the river's continuation in the next tile, the link goes to the top of that continuation: the reach
whose area came from the REC2 inflow at the tile edge (its own area minus its upstream reaches' is
about the outlet's area). A link is never made into the outlet's own catchment (no cycles), and every
link and every refusal is logged with its reason.

Areas do not change: each tile already added the REC2 inflow at its edge, so a reach's ``area_km2``
includes its whole catchment. What was wrong is only which reach drains into which.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import shapely
from scipy.spatial import cKDTree


@dataclass(frozen=True)
class RelinkParams:
    match_m: float = 150.0                    # REC2 reach within this of the outlet's end point ...
    match_m_per_sqrt_km2: float = 20.0        # ... or, for big rivers, within this x sqrt(area)
    match_max_m: float = 1_000.0              # (wide flat valleys: our line and REC2's can be far apart)
    match_ratio: tuple[float, float] = (1 / 3, 3.0)   # REC2 cumulative area / outlet area
    snap_m: float = 100.0                     # a reach of ours this close to the REC2 path is on it
    step_m: float = 20.0                      # spacing of the points along the REC2 path
    max_walk_m: float = 10_000.0              # how far down REC2 to look
    area_tol: float = 0.05                    # target area >= (1 - area_tol) x outlet area
    head_excess: tuple[float, float] = (0.5, 1.5)   # top of the continuation: own minus upstream area / outlet
    max_up_m: float = 5_000.0                 # how far up the continuation to look for its top
    max_chain_m: float = 3_000.0              # no REC2 reach at the end point: try the outlet's own river this
                                              # far upstream (our line and REC2's can part on flats)


# ---------------------------------------------------------------------------- network helpers
def roots(down: np.ndarray) -> np.ndarray:
    """The outlet each reach drains to (pointer doubling); -1 for reaches on a cycle."""
    n = len(down)
    r = np.where(down >= 0, down, np.arange(n))
    for _ in range(64):
        nr = r[r]
        if np.array_equal(nr, r):
            break
        r = nr
    else:
        r = r.copy()
    return np.where(down[r] < 0, r, -1)


def local_area(down: np.ndarray, area: np.ndarray) -> np.ndarray:
    """Each reach's area minus the areas of the reaches draining into it."""
    ok = down >= 0
    return area - np.bincount(down[ok], weights=area[ok], minlength=len(down))


def upstream_main(down: np.ndarray, area: np.ndarray, key: np.ndarray) -> np.ndarray:
    """For each reach, the upstream neighbour with the largest area (ties: smallest ``key``)."""
    up = np.full(len(down), -1, dtype=np.int64)
    i = np.flatnonzero(down >= 0)
    o = i[np.lexsort((key[i], -area[i], down[i]))]
    first = np.ones(len(o), dtype=bool)
    first[1:] = down[o[1:]] != down[o[:-1]]
    up[down[o[first]]] = o[first]
    return up


# ---------------------------------------------------------------------------- REC2
class Rec2Graph:
    """REC2 reaches as a downstream graph, with a spatial index on the lines."""

    def __init__(self, geoms, hydro_id, next_down_id, cum_area_km2, nzsegment):
        self.geoms = np.asarray(geoms, dtype=object)
        self.cum = np.asarray(cum_area_km2, dtype=float)
        self.nzseg = np.asarray(nzsegment, dtype=np.int64)
        pos = pd.Series(np.arange(len(self.geoms)), index=np.asarray(hydro_id))
        self.next = pos.reindex(np.asarray(next_down_id)).fillna(-1).astype(np.int64).to_numpy()
        self.length = shapely.length(self.geoms)
        self.tree = shapely.STRtree(self.geoms)

    @classmethod
    def from_frame(cls, rec2) -> Rec2Graph:
        return cls(rec2.geometry.to_numpy(), rec2["HydroID"].to_numpy(), rec2["NextDownID"].to_numpy(),
                   rec2["cum_area_km2"].to_numpy(), rec2["nzsegment"].to_numpy())

    @classmethod
    def read(cls, path: Path) -> Rec2Graph:
        import pyogrio

        return cls.from_frame(pyogrio.read_dataframe(path, columns=["nzsegment", "HydroID", "NextDownID",
                                                                     "cum_area_km2"]))

    def match(self, xy: np.ndarray, area: np.ndarray, radius_m, ratio: tuple[float, float]):
        """Index of the REC2 reach at each point (-1 none): within ``radius_m`` (one value or one per
        point), cumulative area within ``ratio`` of ``area``, scored by distance and by how far the areas
        differ."""
        pts = shapely.points(xy)
        pi, gi = self.tree.query(pts, predicate="dwithin", distance=radius_m)
        out = np.full(len(xy), -1, dtype=np.int64)
        if not len(pi):
            return out
        d = shapely.distance(pts[pi], self.geoms[gi])
        r = self.cum[gi] / np.maximum(area[pi], 1e-9)
        ok = (r >= ratio[0]) & (r <= ratio[1])
        score = np.where(ok, d ** 2 + (300.0 * np.log(np.where(ok, r, 1.0))) ** 2, np.inf)
        o = np.lexsort((self.nzseg[gi], score, pi))
        first = np.ones(len(o), dtype=bool)
        first[1:] = pi[o[1:]] != pi[o[:-1]]
        best = o[first & np.isfinite(score[o])]
        out[pi[best]] = gi[best]
        return out

    def path(self, r0: int, start_xy, step_m: float, max_m: float):
        """Points every ``step_m`` down REC2 from the point of reach ``r0`` nearest ``start_xy``, for up to
        ``max_m``. Returns (points (k, 2), distance along (k,), reached the sea: bool)."""
        s = float(shapely.line_locate_point(self.geoms[r0], shapely.Point(start_xy)))
        pts, along, done, r = [], [], 0.0, r0
        while True:
            L = float(self.length[r])
            d = np.append(np.arange(s, L, step_m), L)
            p = shapely.get_coordinates(shapely.line_interpolate_point(self.geoms[r], d))
            pts.append(p)
            along.append(done + d - s)
            done += L - s
            r, s = int(self.next[r]), 0.0
            if r < 0 or done >= max_m:
                break
        pts, along = np.concatenate(pts), np.concatenate(along)
        keep = along <= max_m
        return pts[keep], along[keep], r < 0 and done <= max_m


# ---------------------------------------------------------------------------- relinking
def _radius(p: RelinkParams, area):
    return np.clip(p.match_m_per_sqrt_km2 * np.sqrt(area), p.match_m, p.match_max_m)


def relink_outlets(down: np.ndarray, area: np.ndarray, end_xy: np.ndarray, eligible: np.ndarray,
                   seg_xy: np.ndarray, seg_row: np.ndarray, rec2: Rec2Graph, key: np.ndarray | None = None,
                   length: np.ndarray | None = None, params: RelinkParams | None = None
                   ) -> tuple[np.ndarray, pd.DataFrame]:
    """New ``down`` (row indices, -1 outlet) with eligible outlets reconnected, and a log with one row per
    eligible outlet (``status`` "linked" or why not).

    ``end_xy``: each reach's downstream end (NZTM). ``seg_xy`` / ``seg_row``: the vertices of every reach
    line and the row each belongs to. ``key``: a stable id per row (``gid``) so the result does not depend
    on row order. ``length``: reach lengths in m (for how far up a continuation to look).
    """
    p = params or RelinkParams()
    down = np.asarray(down, dtype=np.int64)
    area = np.asarray(area, dtype=float)
    n = len(down)
    key = np.arange(n) if key is None else np.asarray(key)
    length = np.full(n, 250.0) if length is None else np.asarray(length, dtype=float)
    new = down.copy()
    root = roots(down)
    parent = np.arange(n)            # union-find over outlets: the outlet a former outlet now drains to

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    excess = local_area(down, area)
    up = upstream_main(down, area, key)
    tree = cKDTree(seg_xy)
    todo = np.flatnonzero(np.asarray(eligible, bool) & (down < 0))
    todo = todo[np.lexsort((key[todo], -area[todo]))]          # biggest first: they get the best targets
    # Where each outlet is on REC2: at its end point or, failing that, further up its own river.
    chain_o, chain_x, chain_m = [], [], []
    for o in todo:
        x, m = int(o), 0.0
        while x >= 0 and m <= p.max_chain_m:
            chain_o.append(o)
            chain_x.append(x)
            chain_m.append(m)
            m += length[x]
            x = int(up[x])
    chain_o, chain_x, chain_m = np.array(chain_o), np.array(chain_x, dtype=np.int64), np.array(chain_m)
    hit = rec2.match(end_xy[chain_x], area[chain_x], _radius(p, area[chain_x]), p.match_ratio)
    start = {}
    for o, x, m, r in zip(chain_o.tolist(), chain_x.tolist(), chain_m.tolist(), hit.tolist(), strict=True):
        if r >= 0 and o not in start:
            start[o] = (x, m, r)
    lo = 1.0 - p.area_tol
    recs = []
    for o in todo.tolist():
        A = area[o]
        x0, m0, r0 = start.get(o, (-1, np.nan, -1))
        rec = {"row": int(o), "area_km2": float(A), "end_x": float(end_xy[o, 0]), "end_y": float(end_xy[o, 1]),
               "rec2_nzsegment": int(rec2.nzseg[r0]) if r0 >= 0 else -1, "rec2_from_m": m0, "status": "",
               "target": -1, "first_hit": -1, "along_m": np.nan, "snap_m": np.nan, "up_m": 0.0, "via_head": False,
               "area_ratio": np.nan, "rec2_sea_m": np.nan, "largest_too_small_km2": np.nan, "head_off_path": -1}
        recs.append(rec)
        if r0 < 0:
            rec["status"] = "no REC2 reach near the outlet's river"
            continue
        pts, along, sea = rec2.path(int(r0), end_xy[x0], p.step_m, p.max_walk_m + m0)
        if sea:
            rec["rec2_sea_m"] = float(along[-1] - m0)
        # Only path points at or below the outlet's end: the walk may start upstream, on the outlet's river.
        k0 = int(np.argmin(np.hypot(pts[:, 0] - end_xy[o, 0], pts[:, 1] - end_xy[o, 1]))) if x0 != o else 0
        hits = tree.query_ball_point(pts, p.snap_m)
        chosen, cycle, small = None, False, 0.0
        for k, h in enumerate(hits):
            if not h or k < k0:
                continue
            h = np.asarray(h)
            rows = seg_row[h]
            d = np.hypot(seg_xy[h, 0] - pts[k, 0], seg_xy[h, 1] - pts[k, 1])
            best = {}
            for j, dj in zip(rows.tolist(), d.tolist(), strict=True):
                if j != o and dj < best.get(j, np.inf):
                    best[j] = dj
            cands = []
            for j, dj in best.items():
                if area[j] < lo * A:
                    small = max(small, area[j])
                    continue
                if find(root[j]) == o:      # in the outlet's own catchment
                    cycle |= root[j] != o       # ... only through an earlier link
                    continue
                cands.append((dj + 100.0 * abs(math.log(area[j] / A)), key[j], j, dj))
            if cands:
                chosen = (k, *min(cands))
                break
        rec["largest_too_small_km2"] = small if small > 0 else np.nan
        if chosen is None:
            # too small: the next tile has the river but with much less area (its REC2 inflow went astray)
            rec["status"] = ("would make a cycle" if cycle else
                             "reaches on the REC2 path too small" if small >= 0.1 * A else
                             "REC2 reaches the sea first" if sea else "no reach on the REC2 path")
            continue
        k, _, _, j, dj = chosen
        # Climb the continuation to its top: the reach that got the REC2 inflow at the tile edge.
        target, x, walked = j, j, 0.0
        if not p.head_excess[0] <= excess[j] / A <= p.head_excess[1]:
            while walked < p.max_up_m:
                u = up[x]
                if u < 0 or area[u] < lo * A or new[u] != down[u]:   # not into a river linked here
                    break
                x = int(u)
                walked += length[x]
                if p.head_excess[0] <= excess[x] / A <= p.head_excess[1]:
                    # still along the REC2 path (as far off it as a match may be)
                    if np.hypot(*(pts - end_xy[x]).T).min() <= max(2 * p.snap_m, _radius(p, A)):
                        target = x
                    else:
                        rec["head_off_path"] = int(x)
                    break
        new[o] = target
        parent[o] = find(root[target])
        excess[target] -= A
        if up[target] < 0 or area[o] > area[up[target]] or (area[o] == area[up[target]] and key[o] < key[up[target]]):
            up[target] = o
        rec.update(status="linked", target=int(target), first_hit=int(j), along_m=float(along[k] - along[k0]),
                   snap_m=float(dj), up_m=float(walked) if target != j else 0.0, via_head=bool(target != j),
                   area_ratio=float(area[target] / A))
    return new, pd.DataFrame(recs)


# ---------------------------------------------------------------------------- checks
def check_network(down: np.ndarray, area: np.ndarray, local_km2: np.ndarray | None = None,
                  tol: float = 0.01) -> dict:
    """Outlets by size, cycles, reaches that reach no outlet, area decreases downstream and, with
    ``local_km2`` (pixel local areas), local areas summed down the network against ``area``."""
    from .reaches import upstream_order

    down = np.asarray(down, dtype=np.int64)
    area = np.asarray(area, dtype=float)
    n = len(down)
    order = upstream_order(down)
    out = down < 0
    bins = [0, 1, 10, 100, 200, 1000, 1e9]
    sizes = pd.cut(pd.Series(area[out]), bins, right=False).value_counts().sort_index()
    ok = down >= 0
    dec = np.flatnonzero(ok & (area[np.maximum(down, 0)] < area * (1 - tol)))
    res = {
        "reaches": n, "outlets": int(out.sum()),
        "outlets_by_area_km2": {f"{int(b.left)}-{int(b.right) if b.right < 1e9 else 'inf'}": int(v)
                                for b, v in sizes.items()},
        "outlet_area_km2_total": round(float(area[out].sum()), 1),
        "in_cycles": int(n - len(order)),
        "reach_no_outlet": int((roots(down) < 0).sum()),
        "decreases": int(len(dec)),
        "local_km2_total": round(float(local_area(down, area).sum()), 1),
        "local_km2_negative": int((local_area(down, area) < -1e-6).sum()),
    }
    if local_km2 is not None:
        acc = np.asarray(local_km2, dtype=float).copy()
        for i in order:
            if down[i] >= 0:
                acc[down[i]] += acc[i]
        big = area >= 10.0
        ratio = acc[big] / area[big]
        q = np.quantile(ratio, [0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99])
        res["summed_local_vs_area"] = {
            "reaches_ge_10km2": int(big.sum()),
            "ratio_q01_q05_q25_q50_q75_q95_q99": [round(float(v), 4) for v in q],
            "within_5pct": round(float((np.abs(ratio - 1) <= 0.05).mean()), 4),
            "within_10pct": round(float((np.abs(ratio - 1) <= 0.10).mean()), 4),
            "below_50pct": int((ratio < 0.5).sum()),
        }
        res["_summed_local_km2"] = acc
    res["_decreases"] = dec
    return res


# ---------------------------------------------------------------------------- the national table
def tile_segments(tiles_dir: Path, gids: np.ndarray, log=print):
    """End points, ``exits_core``, lengths and line vertices of the tiles' core segments, aligned to
    ``gids``. Returns (end_xy, exits_core, length_m, seg_xy, seg_row)."""
    import pyogrio

    n = len(gids)
    pos = pd.Series(np.arange(n), index=np.asarray(gids))
    end = np.full((n, 2), np.nan)
    exits = np.zeros(n, dtype=bool)
    length = np.full(n, np.nan)
    xs, rows = [], []
    paths = sorted(Path(tiles_dir).glob("*/segments_core.gpkg"))
    for p in paths:
        g = pyogrio.read_dataframe(p, columns=["gid", "end_x", "end_y", "exits_core", "length_m"])
        r = pos.reindex(g["gid"].to_numpy()).to_numpy()
        ok = ~np.isnan(r)
        r = r[ok].astype(np.int64)
        end[r, 0], end[r, 1] = g["end_x"].to_numpy()[ok], g["end_y"].to_numpy()[ok]
        exits[r] = g["exits_core"].to_numpy(dtype=bool)[ok]
        length[r] = g["length_m"].to_numpy()[ok]
        xy, ix = shapely.get_coordinates(g.geometry.to_numpy()[ok], return_index=True)
        xs.append(xy)
        rows.append(r[ix])
    missing = int(np.isnan(end[:, 0]).sum())
    if missing:
        raise ValueError(f"{missing} reaches of the table are in no tile's segments_core.gpkg")
    log(f"relink: {len(paths)} tiles, {n} reaches")
    return end, exits, length, np.concatenate(xs), np.concatenate(rows)


def relink_table(t: pd.DataFrame, tiles_dir: Path, rec2_path: Path, params: RelinkParams | None = None,
                 log=print) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The national table with outlets that left their tile reconnected. Only ``down``, ``down_gid`` and
    ``up`` change; rows, gids and every other column stay as they are. Returns (table, log)."""
    from .models import _upstream_main

    gid = t["gid"].to_numpy(np.int64)
    end, exits, length, seg_xy, seg_row = tile_segments(tiles_dir, gid, log=log)
    rec2 = Rec2Graph.read(rec2_path)
    down0 = t["down"].to_numpy(np.int64)
    down, lg = relink_outlets(down0, t["area_km2"].to_numpy(float), end, exits, seg_xy, seg_row, rec2, key=gid,
                              length=length, params=params)
    t = t.copy()
    t["down"] = down
    t["down_gid"] = np.where(down >= 0, gid[np.maximum(down, 0)], -1)
    t["up"] = _upstream_main(down, t["area_km2"].to_numpy())
    lg = describe_log(lg, t)
    log(f"relink: {int((lg['status'] == 'linked').sum())} of {len(lg)} outlets that left their tile reconnected")
    return t, lg


def describe_log(lg: pd.DataFrame, t: pd.DataFrame) -> pd.DataFrame:
    """Add gids, names and coordinates to a ``relink_outlets`` log."""
    lg = lg.copy()
    gid = t["gid"].to_numpy()
    r = lg["row"].to_numpy()
    lg.insert(0, "gid", gid[r])
    tg = lg["target"].to_numpy()
    lg["target_gid"] = np.where(tg >= 0, gid[np.maximum(tg, 0)], -1)
    for c in ("river", "region", "lon", "lat"):
        if c in t:
            lg[c] = t[c].to_numpy()[r]
    if "area_km2" in t:
        lg["target_area_km2"] = np.where(tg >= 0, t["area_km2"].to_numpy()[np.maximum(tg, 0)], np.nan)
    return lg
