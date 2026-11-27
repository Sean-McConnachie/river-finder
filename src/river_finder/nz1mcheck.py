"""Checks of done 1 m tiles (rf-lxa.6, ``docs/nz1m-tile-job.md``): ``river-finder nz1m check <qid...>`` ->
``data/nz1m/analysis/<qid>.json``.

- **Network and steps**: reaches, km, steps per km by catchment size and kind, water on the profiles, zero-gradient
  reaches (rf-a33).
- **OSM waterfalls** in the core (``data/nz/waterfalls.gpkg``): distance to the nearest reach of 0.5 km2 or more and
  to the nearest step, its height and kind.
- **Benchmark** (tiles inside x05y07, rf-lxa.2): offsets of the reach lines from the 1 m channel (the bench's 1 m
  routing on its sub-area) and their height above it (1 m mosaic re-read at the points), zero-gradient shares
  against the bench's 5 m / 8 m variants on the same core, steps against the rf-lxa.3 dry run.
- **Riser rule** on guide sections in the core whose path has hydro-flattened water (the Wairoa below McLaren
  Falls): every step on the path with its kind and the water codes above and below.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from . import lidarpipe as P
from . import nz1m as N
from .regions import REPO_ROOT

AREA_BINS = [(0.5, 2), (2, 10), (10, 100), (100, 1e9)]
KINDS = {P.STEP_BED: "bed", P.STEP_RISER: "riser", P.STEP_PLANAR: "planar", P.STEP_COARSE: "coarse"}
BENCH = REPO_ROOT / "data" / "analysis" / "lidar-bench"
SPEC = REPO_ROOT / "data" / "analysis" / "lidar-spec"
SUB = (1_437_000, 5_229_000, 1_457_000, 5_250_000)      # the rf-lxa.2 1 m / 2 m sub-area


def _q(a, qs=(50, 90)) -> dict:
    a = np.asarray(a, float)
    a = a[np.isfinite(a)]
    return ({f"p{q}": round(float(np.percentile(a, q)), 2) for q in qs} | {"n": int(len(a))}) if len(a) else {"n": 0}


def _in(x, y, box):
    return (x >= box[0]) & (x < box[2]) & (y >= box[1]) & (y < box[3])


def _samples(geoms, step: float):
    import shapely

    xs, own = [], []
    for i, ln in enumerate(geoms):
        s = np.arange(step / 2, ln.length, step) if ln.length > step else np.array([ln.length / 2])
        xs.append(shapely.get_coordinates(shapely.line_interpolate_point(ln, s)))
        own.append(np.full(len(s), i))
    return np.vstack(xs), np.concatenate(own)


def load(tid: str, root: Path | None = None):
    import pyarrow.feather as feather
    import pyogrio

    od = N.tile_dir(tid, root)
    g = pyogrio.read_dataframe(od / "segments_core.gpkg")
    st = feather.read_table(od / "steps.arrow").to_pandas()
    x1 = feather.read_table(od / "x1_reach.arrow").to_pandas()
    info = json.loads((od / "tile.json").read_text())
    return g, st, x1, info


def network(g: pd.DataFrame, st: pd.DataFrame) -> dict:
    a = g.set_index("gid")["area_tile_km2"]
    st = st.assign(area=a.reindex(st["gid"]).to_numpy())
    out = {"reaches": int(len(g)), "km": round(float(g["length_m"].sum() / 1000), 1),
           "steps_by_kind": {KINDS[k]: int((st["kind"] == k).sum()) for k in KINDS}}
    rows = {}
    for lo, hi in AREA_BINS:
        m = (g["area_tile_km2"] >= lo) & (g["area_tile_km2"] < hi)
        km = float(g.loc[m, "length_m"].sum() / 1000)
        s = st[(st["area"] >= lo) & (st["area"] < hi)]
        bed = s[s["kind"] == P.STEP_BED]
        rows[f"{lo:g}-{hi:g}"] = {
            "reaches": int(m.sum()), "km": round(km, 1),
            "gradient_p50": round(float(g.loc[m, "gradient_m_per_km"].median()), 1) if m.any() else None,
            "bed_steps_per_km": round(len(bed) / max(km, 1e-9), 2),
            "ge2m_per_km": round(int((bed["h"] >= 2).sum()) / max(km, 1e-9), 2),
            "ge5m": int((bed["h"] >= 5).sum()), "risers": int((s["kind"] == P.STEP_RISER).sum()),
            "planar": int((s["kind"] == P.STEP_PLANAR).sum()),
            "bed_h_p50": round(float(bed["h"].median()), 2) if len(bed) else None,
            "with_steps_share": round(float((g.loc[m, "steps_n"] > 0).mean()), 3) if m.any() else None,
            "water_flat_share": round(float(g.loc[m, "water_flat_share"].mean()), 4) if m.any() else None,
            "water_planar_share": round(float(g.loc[m, "water_planar_share"].mean()), 4) if m.any() else None}
    out["by_area"] = rows
    big = g["area_tile_km2"] >= 2
    out["zero_gradient_share_ge2km2"] = round(float(
        g.loc[big & ((g["z_top"] - g["z_bot"]) < 0.01), "length_m"].sum() / max(g.loc[big, "length_m"].sum(), 1)), 4)
    out["steep50_water_share"] = round(float(g["steep50_water"].mean()), 4)
    out["dem_source"] = g["dem_source"].value_counts().to_dict()
    out["lake_reaches"] = int(g["lake"].sum())
    return out


def osm_falls(g, st, core) -> dict:
    """Every OSM waterfall in the core: the nearest reach (>= 0.5 km2) and the nearest step of any kind."""
    import geopandas as gpd
    from scipy.spatial import cKDTree

    from .national import nz_dir

    w = gpd.read_file(nz_dir() / "waterfalls.gpkg", bbox=tuple(core)).to_crs(2193)
    w = w[_in(w.geometry.x.to_numpy(), w.geometry.y.to_numpy(), core) & (w["kind"] == "waterfall")]
    if not len(w):
        return {"n": 0}
    xy, own = _samples(g.geometry.values, 2.0)
    tree = cKDTree(xy)
    sxy = np.column_stack([st["x_cm"] / 100.0, st["y_cm"] / 100.0])
    stree = cKDTree(sxy) if len(sxy) else None
    rows = []
    for _, f in w.iterrows():
        p = (f.geometry.x, f.geometry.y)
        dr, k = tree.query(p)
        rec = {"name": f["name"] or "", "height_m": None if pd.isna(f["height_m"]) else float(f["height_m"]),
               "x": round(p[0]), "y": round(p[1]), "reach_dist_m": round(float(dr), 1),
               "reach_km2": round(float(g["area_tile_km2"].iat[own[k]]), 2)}
        if stree is not None:
            ds, j = stree.query(p, k=min(5, len(sxy)))
            ds, j = np.atleast_1d(ds), np.atleast_1d(j)
            near = [(d_, i) for d_, i in zip(ds, j, strict=True) if d_ <= 60]
            if near:
                d_, i = near[0]
                tall = max(near, key=lambda t: st["h"].iat[t[1]])
                rec.update(step_dist_m=round(float(d_), 1), step_h=round(float(st["h"].iat[i]), 2),
                           step_kind=KINDS[int(st["kind"].iat[i])], tallest_within_60m=round(float(
                               st["h"].iat[tall[1]]), 2))
        rows.append(rec)
    r = pd.DataFrame(rows)
    on = r["reach_dist_m"] <= 30
    return {"n": int(len(r)), "on_a_reach_30m": int(on.sum()),
            "step_within_20m": int((r.get("step_dist_m", pd.Series(dtype=float)) <= 20).sum()),
            "step_within_60m": int(r.get("step_dist_m", pd.Series(dtype=float)).notna().sum()),
            "on_reach_with_step_20m": int((on & (r.get("step_dist_m", pd.Series(np.nan, index=r.index)) <= 20)).sum()),
            "falls": rows}


# ---------------------------------------------------------------------------------------------- benchmark
def bench(g, st, core) -> dict:
    """Against rf-lxa.2 (x05y07): the 1 m channel of the bench sub-area, the 5 m / 8 m variants, the dry run."""
    import geopandas as gpd
    from scipy.spatial import cKDTree

    out: dict = {}
    box = (max(core[0], SUB[0]), max(core[1], SUB[1]), min(core[2], SUB[2]), min(core[3], SUB[3]))
    out["overlap_box"] = list(box)
    s1p = BENCH / "variants" / "s1" / "segments.gpkg"
    if s1p.exists() and box[0] < box[2] and box[1] < box[3]:
        ref = gpd.read_file(s1p)
        rxy, rown = _samples(ref.geometry.values, 1.0)
        ra = ref["area_mid_km2"].to_numpy()[rown]
        rtree = cKDTree(rxy)
        rows = {}
        pts_all = {}
        for name, frame, acol in (("lidar1m_2m_lines", g, "area_tile_mid_km2"),
                                  ("bench_s2mean", gpd.read_file(BENCH / "variants" / "s2mean" / "segments.gpkg"),
                                   "area_mid_km2"),
                                  ("bench_l5mean", gpd.read_file(BENCH / "variants" / "l5mean" / "segments.gpkg",
                                                                 bbox=tuple(box)), "area_mid_km2"),
                                  ("national_8m", gpd.read_file(REPO_ROOT / "data" / "nz" / "tiles" / "x05y07"
                                                                / "segments_core.gpkg", bbox=tuple(box)), "area_km2")):
            xy, own = _samples(frame.geometry.values, 10.0)
            a = frame[acol].to_numpy()[own]
            m = _in(xy[:, 0], xy[:, 1], box) & (a >= 2)
            q, aa = xy[m], a[m]
            dd, ii = rtree.query(q, k=24, distance_upper_bound=600)
            best = np.full(len(q), np.nan)
            bi = np.full(len(q), -1)
            for j in range(24):
                ok = np.isfinite(dd[:, j]) & np.isnan(best)
                jj = np.minimum(ii[:, j], len(rxy) - 1)
                ratio = ra[jj] / np.maximum(aa, 1e-6)
                ok &= (ratio >= 0.5) & (ratio <= 2.0)
                best[ok] = dd[ok, j]
                bi[ok] = jj[ok]
            rows[name] = {"all_ge2": _q(best), "ge100": _q(best[aa >= 100]),
                          "unmatched": round(float(np.isnan(best).mean()), 3)}
            pts_all[name] = (q, bi)
        # height above the 1 m channel: the 1 m mosaic at the points and at their 1 m channel points
        try:
            from . import xsec1m as X1

            sheets = P.mosaic_sheets(box, REPO_ROOT / "data" / "_stac")
            allp = np.vstack([np.vstack([q, rxy[bi[bi >= 0]]]) for q, bi in pts_all.values()])
            z1, tf1, _ = X1.read_corridor(sheets, box, allp[:, 0], allp[:, 1], 3.0, threads=32)

            def at(p):
                r = np.clip(np.floor(tf1.f - p[:, 1]).astype(int), 0, z1.shape[0] - 1)
                c = np.clip(np.floor(p[:, 0] - tf1.c).astype(int), 0, z1.shape[1] - 1)
                return z1[r, c]

            for name, (q, bi) in pts_all.items():
                ok = bi >= 0
                dz = at(q[ok]) - at(rxy[bi[ok]])
                rows[name]["height_above"] = _q(dz)
                rows[name]["share_gt1m_above"] = round(float((dz[np.isfinite(dz)] > 1.0).mean()), 4)
            del z1
        except Exception as e:  # network trouble: keep the distances
            rows["height_error"] = repr(e)
        out["to_1m_channel"] = rows
    # zero-gradient reaches in this core, every variant clipped to it (by midpoint)
    flats = {"lidar1m_2m": _zero(g, "area_tile_km2", core)}
    for v in ("l5mean", "l8", "c8"):
        p = BENCH / "variants" / v / "segments.gpkg"
        if p.exists():
            flats[f"bench_{v}"] = _zero(gpd.read_file(p, bbox=tuple(core)), "area_km2", core)
    p8 = REPO_ROOT / "data" / "nz" / "tiles" / "x05y07" / "segments_core.gpkg"
    if p8.exists():
        f8 = gpd.read_file(p8, bbox=tuple(core))
        f8["z_bot"] = f8["z_top"] - f8["gradient_m_per_km"] * f8["length_m"] / 1000
        flats["national_8m"] = _zero(f8, "area_km2", core)
    out["zero_gradient"] = flats
    # steps against the rf-lxa.3 dry run (2 m bench lines of the sub-area), in the overlap
    dr = SPEC / "dryrun_reaches.csv"
    ds = SPEC / "dryrun_steps.csv"
    if dr.exists() and ds.exists():
        R = pd.read_csv(dr)
        Sx = pd.read_csv(ds)
        s2 = gpd.read_file(BENCH / "variants" / "s2mean" / "segments.gpkg")
        mid = s2.geometry.interpolate(0.5, normalized=True)
        inside = set(s2.loc[_in(mid.x.to_numpy(), mid.y.to_numpy(), box), "seg_id"])
        R = R[R["seg_id"].isin(inside)]
        Sx = Sx[Sx["seg"].isin(inside)]
        gm = g.geometry.interpolate(0.5, normalized=True)
        gi = g[_in(gm.x.to_numpy(), gm.y.to_numpy(), box)]
        si = st[st["gid"].isin(gi["gid"])]
        comp = {}
        for lo, hi in AREA_BINS:
            a = R[(R["area_km2"] >= lo) & (R["area_km2"] < hi)]
            b = gi[(gi["area_tile_km2"] >= lo) & (gi["area_tile_km2"] < hi)]
            sa = Sx[Sx["seg"].isin(a["seg_id"]) & (Sx["kind"] == 0)]
            sb = si[si["gid"].isin(b["gid"]) & (si["kind"] == 0)]
            ka, kb = a["length_profile_m"].sum() / 1000, b["length_m"].sum() / 1000
            comp[f"{lo:g}-{hi:g}"] = {"dryrun_km": round(ka, 1), "tile_km": round(kb, 1),
                                      "dryrun_bed_per_km": round(len(sa) / max(ka, 1e-9), 2),
                                      "tile_bed_per_km": round(len(sb) / max(kb, 1e-9), 2),
                                      "dryrun_ge5m": int((sa["h"] >= 5).sum()), "tile_ge5m": int((sb["h"] >= 5).sum())}
        out["steps_vs_dryrun"] = comp
    return out


def _zero(f, acol: str, core) -> dict:
    mid = f.geometry.interpolate(0.5, normalized=True)
    m = _in(mid.x.to_numpy(), mid.y.to_numpy(), core) & (f[acol].to_numpy() >= 2)
    L = f["length_m"].to_numpy()
    zero = (f["z_top"] - f["z_bot"]).to_numpy() < 0.01
    km = L[m].sum() / 1000
    return {"km_ge2": round(float(km), 1), "zero_share": round(float(L[m & zero].sum() / 1000 / max(km, 1e-9)), 4),
            "reaches_ge2": int(m.sum())}


# ---------------------------------------------------------------------------------------------- riser rule
def risers(tid: str, g, st, core, root: Path | None = None) -> dict:
    """Guide sections with a put-in in the core: the path of reaches from the put-in to the take-out (down_seg),
    every step on it with its kind, and the water codes of its stations (decoded profiles)."""
    import pyarrow.feather as feather
    from pyproj import Transformer
    from scipy.spatial import cKDTree

    from .models import all_sections

    t = Transformer.from_crs(4326, 2193, always_xy=True)
    pr = feather.read_table(N.tile_dir(tid, root) / "profiles.arrow")
    prof = {int(gg): (z0, dz, w4, n) for gg, z0, dz, w4, n in zip(
        pr["gid"].to_numpy(), pr["z0_cm"].to_numpy(), pr["dz"].to_pylist(), pr["w4"].to_pylist(),
        pr["n"].to_numpy(), strict=True)}
    xy, own = _samples(g.geometry.values, 5.0)
    tree = cKDTree(xy)
    by_seg = {int(s): i for i, s in enumerate(g["seg_id"])}
    out = []
    seen = set()
    for s in all_sections():
        if s.status != "runnable" or not s.put_in or not s.take_out:
            continue
        pi, to = t.transform(*s.put_in), t.transform(*s.take_out)
        if not _in(np.array([pi[0]]), np.array([pi[1]]), core)[0] or (s.river, pi) in seen:
            continue
        seen.add((s.river, pi))
        cand = tree.query_ball_point(pi, 150)
        if not cand:
            continue
        i0 = max({own[c] for c in cand}, key=lambda i: g["area_tile_km2"].iat[i])
        path, i, best, bd = [], i0, 0, np.inf
        while i is not None and len(path) < 2000:
            path.append(i)
            e = g.geometry.iat[i].coords[-1]
            dd = math.dist(e, to)
            if dd < bd:
                best, bd = len(path), dd
            elif dd > bd + 2000:
                i = by_seg.get(int(g["down_seg"].iat[i]))
                break
            i = by_seg.get(int(g["down_seg"].iat[i]))
        while i is not None and len(path) < 2000:             # on down to the end of the core network
            path.append(i)
            i = by_seg.get(int(g["down_seg"].iat[i]))

        def summary(pp):
            gids = g["gid"].to_numpy()[pp]
            codes = []
            for gg in gids:
                _z0, _dz, w4, n = prof[int(gg)]
                b = np.asarray(w4, np.uint8)
                codes.append(np.stack([b & 3, (b >> 2) & 3, (b >> 4) & 3, (b >> 6) & 3], axis=1).ravel()[:n])
            codes = np.concatenate(codes) if codes else np.zeros(0, np.uint8)
            ss = st[st["gid"].isin(gids)].copy()
            ss["reach_pos"] = ss["gid"].map({int(x): k for k, x in enumerate(gids)})
            return {"reaches": int(len(pp)), "km": round(float(g["length_m"].to_numpy()[pp].sum() / 1000), 2),
                    "drop_m": round(float(g["z_top"].iat[pp[0]] - g["z_bot"].iat[pp[-1]]), 1) if pp else None,
                    "area_km2": round(float(g["area_tile_km2"].iat[pp[-1]]), 1) if pp else None,
                    "end": [round(v) for v in g.geometry.iat[pp[-1]].coords[-1]] if pp else None,
                    "station_codes": {k: round(float((codes == c).mean()), 4) for k, c in
                                      (("bed", 0), ("flattened", 1), ("planar", 2), ("coarse", 3))},
                    "steps_by_kind": {KINDS[k]: int((ss["kind"] == k).sum()) for k in KINDS},
                    "steps": ss.sort_values(["reach_pos", "s0"])[["gid", "s0", "h", "h_excess", "len", "kind",
                                                                  "x_cm", "y_cm"]]
                    .assign(kind=lambda f: f["kind"].map(KINDS), x=lambda f: (f["x_cm"] / 100).round(1),
                            y=lambda f: (f["y_cm"] / 100).round(1)).drop(columns=["x_cm", "y_cm"])
                    .round(2).to_dict("records")}

        out.append({"river": s.river, "grade": s.grade, "put_in": [round(v) for v in pi],
                    "take_out": [round(v) for v in to], "end_gap_m": round(float(bd), 1),
                    "section": summary(path[:best]), "to_end_of_core": summary(path)})
    return {"sections": out}


def check_tile(tid: str, root: Path | None = None, log=print) -> dict:
    g, st, x1, info = load(tid, root)
    core = tuple(info["core"])
    res = {"tile": tid, "network": network(g, st), "osm_falls": osm_falls(g, st, core),
           "riser_rule": risers(tid, g, st, core, root),
           "x1": {"reaches_with_sections": int((x1["x1_n"] > 0).sum()), "sections_kept": int(x1["x1_n"].sum()),
                  "w1_med_p50": round(float(x1["x1_w1_med"].median()), 1)}}
    if N.Tile32.parse(tid).parent == "x05y07":
        res["bench"] = bench(g, st, core)
    d =(root or N.root_dir()) / "analysis"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{tid}.json").write_text(json.dumps(res, indent=1, default=N._jsonable))
    log(f"nz1m check {tid}: -> {d / f'{tid}.json'}")
    return res


def estimate(tids: list[str], root: Path | None = None) -> dict:
    """National cost from the measured tiles and ``plan.json``: per step, time = fixed + per-km2 x land km2 of the
    core (fixed: the steps that do not scale with land: the 8 m fetch, the 10 m grids, routing of a box of sea is
    cheap too, so routing and the 2 m pass scale with land); disk scales with land; memory is the largest peak (the
    arrays are box sized)."""
    root = root or N.root_dir()
    plan = json.loads((root / "plan.json").read_text())
    land = {t["id"]: t.get("land_km2", 1024.0) for t in plan["tiles"]}
    recs = [json.loads((N.tile_dir(t, root) / "tile.json").read_text()) for t in tids]
    per_km2 = np.mean([r["seconds"] / max(land.get(r["tile"], 1024.0), 1.0) for r in recs])
    disk_km2 = np.mean([r["disk_bytes"] / max(land.get(r["tile"], 1024.0), 1.0) for r in recs])
    fixed = 20.0                                  # sheets, 8 m DEM, numba, writes: not proportional to land
    tot_land = sum(land.values())
    secs = len(land) * fixed + (per_km2 - fixed / 1024.0) * tot_land
    steps = {}
    for k in recs[0]["timings"]:
        steps[k] = round(float(np.mean([r["timings"][k]["s"] / max(land.get(r["tile"], 1024.0), 1.0)
                                        for r in recs])) * tot_land / 3600, 2)
    return {"tiles": len(land), "land_km2": round(tot_land), "full_tile_equivalents": round(tot_land / 1024, 1),
            "hours": round(secs / 3600, 1), "hours_by_step": steps,
            "peak_gb": max(r["peak_gb"] for r in recs), "disk_gb": round(disk_km2 * tot_land / 1e9, 2),
            "measured": {r["tile"]: {"s": r["seconds"], "peak_gb": r["peak_gb"], "mb": round(r["disk_bytes"] / 1e6, 1),
                                     "land_km2": land.get(r["tile"])} for r in recs}}


def run(tids: list[str], root: Path | None = None, log=print) -> list[dict]:
    tids = tids or sorted(p.name for p in ((root or N.root_dir()) / "tiles").glob("q*") if N.is_done(p.name, root))
    out = [check_tile(t, root, log) for t in tids]
    if tids and ((root or N.root_dir()) / "plan.json").exists():
        est = estimate(tids, root)
        p = (root or N.root_dir()) / "analysis" / "estimate.json"
        p.write_text(json.dumps(est, indent=1))
        log(f"nz1m national estimate: {json.dumps(est)}")
    return out
