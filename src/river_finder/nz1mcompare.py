"""Checks of the 1 m rebuild against what it replaces (rf-lxa.8; ``docs/nz1m-finish.md``).

- ``compare_references``: the one comparison of the 1 m national results with the 5 m reference areas
  (``data/<region>/``: kaimai, karangahake, tutamoe) that the user asked for before the per-region pipeline retires.
  Per area, inside the part of its box that the merged tiles cover: channel length by catchment size, reaches of the
  5 m run matched to 1 m lines (area, gradient, steepest 50 m, gorge depth, valley width, MAF), REC2 agreement (the
  5 m ``validation_summary.json`` against the 1 m network's ``rec2_area_at_km2``) and the named runs at their
  measured outlets.
- ``section_audit``: the guide sections placed on the 8 m table and on the 1 m table (``models.map_sections``), per
  section: placed or not, km, method, overlap, and whether the 1 m placement rests on LINZ-only names.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from .regions import REPO_ROOT, list_regions, load_region

MATCH_M = 30.0          # a 5 m reach's midpoint lies within this of the 1 m line it is matched to
RATIO = (0.5, 2.0)      # and their areas are within this ratio
OUTLET_M = 150.0        # a named run's measured outlet: 1 m reaches within this
LENGTH_KM2 = (1.0, 2.0, 10.0, 100.0)
OVERLAP_M = 150.0       # section audit: a placement's reach midpoint within this of the other placement's


def covered(root: Path):
    """The union of the merged tiles' cores (``merge.json``), a shapely geometry in NZTM."""
    import shapely

    from .nz import Tile

    tiles = json.loads((Path(root) / "merge.json").read_text())["tiles"]
    return shapely.union_all([shapely.box(*Tile.parse(t).core) for t in tiles])


def _q(v, qs=(0.1, 0.5, 0.9)) -> list | None:
    v = np.asarray(v, float)
    v = v[np.isfinite(v)]
    return [round(float(x), 3) for x in np.quantile(v, qs)] if len(v) else None


def _mids(geoms):
    import shapely

    p = shapely.line_interpolate_point(np.asarray(geoms, dtype=object), 0.5, normalized=True)
    xy = shapely.get_coordinates(p)
    return xy[:, 0], xy[:, 1]


def reaches_1m_in(root: Path, box) -> gpd.GeoDataFrame:  # noqa: F821
    """The scored 1 m reaches (every region's ``segments_scored.gpkg``) whose lines meet ``box``."""
    import geopandas as gpd
    import pyogrio

    parts = []
    for p in sorted((Path(root) / "regions").glob("*/segments_scored.gpkg")):
        g = pyogrio.read_dataframe(p, bbox=tuple(box))
        if len(g):
            parts.append(g)
    if not parts:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:2193")
    return gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), geometry="geometry", crs="EPSG:2193")


def match_reaches(g5, g1, max_m: float = MATCH_M, ratio=RATIO) -> np.ndarray:
    """For each 5 m reach, the row of the 1 m reach whose line passes within ``max_m`` of its midpoint, with the
    closest area within ``ratio`` (-1: none)."""
    import shapely

    out = np.full(len(g5), -1, np.int64)
    if not len(g5) or not len(g1):
        return out
    x, y = _mids(g5.geometry.values)
    pts = shapely.points(x, y)
    pi, li = shapely.STRtree(g1.geometry.values).query(pts, predicate="dwithin", distance=max_m)
    if not len(pi):
        return out
    a5 = g5["area_km2"].to_numpy(float)[pi]
    a1 = g1["area_km2"].to_numpy(float)[li]
    r = a1 / np.maximum(a5, 1e-9)
    ok = (r >= ratio[0]) & (r <= ratio[1])
    d = pd.DataFrame({"p": pi[ok], "l": li[ok], "e": np.abs(np.log(r[ok]))}).sort_values(["p", "e"])
    d = d.drop_duplicates("p")
    out[d["p"].to_numpy()] = d["l"].to_numpy()
    return out


def _ratio_block(v5, v1) -> dict:
    v5, v1 = np.asarray(v5, float), np.asarray(v1, float)
    ok = np.isfinite(v5) & np.isfinite(v1) & (v5 > 0)
    r = v1[ok] / v5[ok]
    out = {"n": int(ok.sum()), "ratio_q10_q50_q90": _q(r)}
    if ok.sum() >= 3:
        out["spearman"] = round(float(pd.Series(v5[ok]).corr(pd.Series(v1[ok]), method="spearman")), 3)
    return out


def rec2_agreement(g1) -> dict:
    """REC2 agreement of 1 m reaches: area_mid_km2 against REC2's area where the midpoint projects onto the matched
    REC2 line (``rec2_area_at_km2``), by REC2 area class (as ``validation_summary.json``'s ``strict`` blocks)."""
    if not len(g1) or "rec2_area_at_km2" not in g1:
        return {}
    ra = pd.to_numeric(g1["rec2_area_at_km2"], errors="coerce").to_numpy(float)
    am = g1["area_mid_km2"].to_numpy(float)
    ok = np.isfinite(ra) & (ra > 0) & (am >= 1.0)
    e = np.abs(am / np.where(ok, ra, 1.0) - 1)
    out = {}
    for name, lo in (("all", 1.0), ("gt_5km2", 5.0), ("gt_20km2", 20.0)):
        m = ok & (ra >= lo)
        if m.any():
            out[name] = {"n": int(m.sum()), "median_abs_rel_err": round(float(np.median(e[m])), 4),
                         "within_10pct": round(float((e[m] <= 0.10).mean()), 3),
                         "within_25pct": round(float((e[m] <= 0.25).mean()), 3)}
    return out


def named_runs(v5: dict, g1, cov=None) -> list[dict]:
    """The 5 m validation's named runs, at their measured outlets, on the 1 m reaches (``cov``: the area the merged
    tiles cover; an outlet outside it is not looked for)."""
    import shapely
    from pyproj import Transformer

    to = Transformer.from_crs("EPSG:4326", "EPSG:2193", always_xy=True)
    out = []
    if not len(g1):
        return out
    tree = shapely.STRtree(g1.geometry.values)
    for r in v5.get("named", []):
        at = r.get("measured_at_wgs84")
        rec = {k: r.get(k) for k in ("river", "status", "rec2_km2", "model_km2", "rel_err")}
        rec["rel_err_5m"] = rec.pop("rel_err")
        rec["model_km2_5m"] = rec.pop("model_km2")
        if not at or not r.get("rec2_km2"):
            out.append(rec | {"area_1m_km2": None})
            continue
        x, y = to.transform(*at)
        if cov is not None and not cov.contains(shapely.Point(x, y)):
            out.append(rec | {"area_1m_km2": None, "note": "outlet outside the merged tiles"})
            continue
        cand = tree.query(shapely.Point(x, y), predicate="dwithin", distance=OUTLET_M)
        if not len(cand):
            out.append(rec | {"area_1m_km2": None, "note": f"no 1 m reach within {OUTLET_M:.0f} m"})
            continue
        a = g1["area_km2"].to_numpy(float)[cand]
        k = cand[np.argmin(np.abs(np.log(np.maximum(a, 1e-9) / r["rec2_km2"])))]
        a1 = float(g1["area_km2"].iloc[k])
        out.append(rec | {"area_1m_km2": round(a1, 2), "rel_err_1m": round(a1 / r["rec2_km2"] - 1, 3),
                          "river_1m": str(g1["river"].iloc[k] or ""),
                          "truncated_1m": bool(g1["truncated"].iloc[k]) if "truncated" in g1 else None})
    return out


def compare_region(rid: str, root: Path, cov, log=print) -> dict:
    import geopandas as gpd
    import shapely

    reg = load_region(rid)
    d = REPO_ROOT / "data" / rid
    p5 = d / "segments_scored.gpkg"
    if not p5.exists():
        return {"status": "no 5 m outputs", "dir": str(d)}
    box = shapely.box(*reg.bbox_nztm(5.0))
    part = box.intersection(cov)
    share = part.area / box.area
    if share < 0.01:
        return {"status": "outside the merged tiles", "box_share_covered": round(share, 4)}
    g5 = gpd.read_file(p5)
    x5, y5 = _mids(g5.geometry.values)
    shapely.prepare(part)
    g5 = g5[shapely.contains_xy(part, x5, y5)].reset_index(drop=True)
    g1 = reaches_1m_in(root, part.bounds)
    if len(g1):
        x1, y1 = _mids(g1.geometry.values)
        g1 = g1[shapely.contains_xy(part, x1, y1)].reset_index(drop=True)
    res = {"status": "compared" if share > 0.999 else "compared (part of the box)", "box_share_covered":
           round(share, 4), "reaches_5m": len(g5), "reaches_1m": len(g1)}
    lengths = {}
    for a in LENGTH_KM2:
        k5 = float(g5.loc[g5["area_km2"] >= a, "length_m"].sum() / 1000)
        k1 = float(g1.loc[g1["area_km2"] >= a, "length_m"].sum() / 1000) if len(g1) else 0.0
        lengths[f"ge_{a:g}km2"] = {"km_5m": round(k5, 1), "km_1m": round(k1, 1),
                                   "ratio": round(k1 / k5, 3) if k5 else None,
                                   "reaches_5m": int((g5["area_km2"] >= a).sum()),
                                   "reaches_1m": int((g1["area_km2"] >= a).sum()) if len(g1) else 0}
    res["lengths"] = lengths
    big = g5[g5["area_km2"] >= 1.0].reset_index(drop=True)
    m = match_reaches(big, g1)
    ok = m >= 0
    res["matched"] = {"reaches_ge_1km2": len(big), "matched": int(ok.sum()),
                      "share": round(float(ok.mean()), 3) if len(big) else None,
                      "share_ge_10km2": round(float(ok[(big["area_km2"] >= 10).to_numpy()].mean()), 3)
                      if (big["area_km2"] >= 10).any() else None}
    a, b = big[ok].reset_index(drop=True), g1.iloc[m[ok]].reset_index(drop=True)
    cols = {"area_km2": "area_km2", "grad_s": "grad_s", "gradient_m_per_km": "gradient_m_per_km",
            "steepest_50m_m_per_km": "steepest_50m_m_per_km", "gorge_depth_m": "gorge_depth_m",
            "valley_width_m": "valley_width_m", "height_to_width": "height_to_width", "maf_m3s": "maf_m3s",
            "head_m": "head_m", "melton": "melton"}
    res["ratios_1m_over_5m"] = {k: _ratio_block(a[k], b[v]) for k, v in cols.items() if k in a and v in b}
    if len(a):
        e = np.abs(b["area_km2"].to_numpy(float) / np.maximum(a["area_km2"].to_numpy(float), 1e-9) - 1)
        res["ratios_1m_over_5m"]["area_km2"] |= {"within_5pct": round(float((e <= 0.05).mean()), 3),
                                                  "within_10pct": round(float((e <= 0.10).mean()), 3)}
        same = (a["river"].fillna("").to_numpy() == b["river"].fillna("").to_numpy())
        named5 = a["river"].fillna("").to_numpy() != ""
        res["names"] = {"5m_named": int(named5.sum()), "same_name": int((same & named5).sum()),
                        "1m_named_where_5m_unnamed": int(((b["river"].fillna("") != "").to_numpy() & ~named5).sum())}
    vp = d / "validation_summary.json"
    v5 = json.loads(vp.read_text()) if vp.exists() else {}
    res["rec2"] = {"5m_strict": v5.get("strict"), "1m_along_network": rec2_agreement(g1),
                   "note": "5 m: reach outlets snapped to REC2 (validate.py); 1 m: area_mid_km2 against REC2's area "
                           "at the midpoint's projection on the network-matched REC2 reach (nz1mmerge)"}
    res["named_runs"] = named_runs(v5, g1, part)
    res["truncated_1m"] = int(g1["truncated"].astype(bool).sum()) if len(g1) and "truncated" in g1 else None
    log(f"compare {rid}: {res['status']}, {len(g5)} 5 m / {len(g1)} 1 m reaches, matched "
        f"{res['matched']['share']} of 5 m reaches >= 1 km2")
    return res


def compare_references(root: Path, log=print) -> dict:
    """The comparison for every reference area (see the module doc)."""
    cov = covered(root)
    out = {"root": str(root), "tiles": json.loads((Path(root) / "merge.json").read_text())["tiles"],
           "match": {"max_m": MATCH_M, "area_ratio": list(RATIO)}, "regions": {}}
    for rid in list_regions():
        out["regions"][rid] = compare_region(rid, root, cov, log=log)
    return out


# ---------------------------------------------------------------------------------------------- sections
def subset(t: pd.DataFrame, keep: np.ndarray) -> pd.DataFrame:
    """Rows ``keep`` of a national table, ``down`` / ``up`` renumbered (links out of the subset end there)."""
    idx = np.flatnonzero(keep)
    new = np.full(len(t), -1, np.int64)
    new[idx] = np.arange(len(idx))
    f = t.iloc[idx].reset_index(drop=True).copy()
    for c in ("down", "up"):
        if c in f:
            v = f[c].to_numpy(np.int64)
            f[c] = np.where(v >= 0, new[np.maximum(v, 0)], -1)
    return f


def _placement(t: pd.DataFrame, rows) -> dict:
    rows = np.asarray(rows, np.int64)
    if not len(rows):
        return {"n": 0, "km": 0.0}
    L = t["length_m"].to_numpy(float)[rows]
    return {"n": int(len(rows)), "km": round(float(L.sum() / 1000), 2),
            "x": float(np.average(t["x"].to_numpy(float)[rows], weights=L + 1e-9)),
            "y": float(np.average(t["y"].to_numpy(float)[rows], weights=L + 1e-9))}


def _overlap(ta, ra, tb, rb) -> float | None:
    """Share of placement ``a``'s length whose reach midpoints lie within ``OVERLAP_M`` of a midpoint of ``b``."""
    from scipy.spatial import cKDTree

    ra, rb = np.asarray(ra, np.int64), np.asarray(rb, np.int64)
    if not len(ra) or not len(rb):
        return None
    tree = cKDTree(np.column_stack([tb["x"].to_numpy(float)[rb], tb["y"].to_numpy(float)[rb]]))
    d, _ = tree.query(np.column_stack([ta["x"].to_numpy(float)[ra], ta["y"].to_numpy(float)[ra]]))
    L = ta["length_m"].to_numpy(float)[ra]
    return round(float(L[d <= OVERLAP_M].sum() / max(L.sum(), 1e-9)), 3)


def section_audit(root: Path, ref8m: Path, log=print) -> tuple[dict, pd.DataFrame]:
    """The placement of every guide section on the 8 m table (``ref8m/segments_table.pkl``, only its reaches inside
    the merged tiles) and on the 1 m table (the current root). Returns the summary and one row per section."""
    import shapely

    from .models import all_sections, map_sections, national_table

    t1 = national_table(log=log)
    t8 = pd.read_pickle(Path(ref8m) / "segments_table.pkl")
    cov = covered(root)
    shapely.prepare(cov)
    t8 = subset(t8, shapely.contains_xy(cov, t8["x"].to_numpy(float), t8["y"].to_numpy(float)))
    secs = all_sections()
    m8 = map_sections(t8, secs, group=False)
    m1 = map_sections(t1, secs, group=False)
    rec2 = t1["river_rec2"].fillna("").to_numpy(object) if "river_rec2" in t1 else None
    rows = []
    for s, a, b in zip(secs, m8, m1, strict=True):
        if not len(a.rows) and not len(b.rows):
            continue
        p8, p1 = _placement(t8, a.rows), _placement(t1, b.rows)
        o81, o18 = _overlap(t8, a.rows, t1, b.rows), _overlap(t1, b.rows, t8, a.rows)
        if not p8["n"]:
            cat = "new"
        elif not p1["n"]:
            cat = "lost"
        elif (o81 or 0) >= 0.8 and (o18 or 0) >= 0.8 and 0.8 <= p1["km"] / max(p8["km"], 1e-9) <= 1.25:
            cat = "same"
        elif (o81 or 0) >= 0.3 or (o18 or 0) >= 0.3:
            cat = "changed"
        else:
            cat = "moved"
        linz_only = None
        if rec2 is not None and p1["n"]:
            br = np.asarray(b.rows, np.int64)
            named = t1["river"].fillna("").to_numpy(object)[br] != ""
            linz_only = round(float((named & (rec2[br] == "")).mean()), 3)
        dist = (math.hypot(p8["x"] - p1["x"], p8["y"] - p1["y"]) if p8["n"] and p1["n"] else None)
        rows.append({"river": s.river, "section": str(s.extra.get("section") or s.extra.get("name") or ""),
                     "region": s.region, "source": s.source,
                     "status": s.status, "grade": s.grade or "", "category": cat,
                     "km_8m": p8["km"], "km_1m": p1["km"], "reaches_8m": p8["n"], "reaches_1m": p1["n"],
                     "how_8m": a.method, "how_1m": b.method, "overlap_8m_on_1m": o81, "overlap_1m_on_8m": o18,
                     "centre_shift_m": None if dist is None else round(dist), "linz_only_name_share": linz_only})
    df = pd.DataFrame(rows)
    summ = {"sections_considered": len(secs), "placed_either": len(df)}
    if len(df):
        summ["by_category"] = df["category"].value_counts().to_dict()
        summ["km_8m"] = round(float(df["km_8m"].sum()), 1)
        summ["km_1m"] = round(float(df["km_1m"].sum()), 1)
        summ["how_changed"] = int((df["how_8m"] != df["how_1m"]).sum())
        summ["placed_by_linz_names"] = int((df["linz_only_name_share"].fillna(0) > 0.5).sum())
        summ["rivers"] = df.sort_values(["category", "river"]).head(400).to_dict("records")
    log(f"sections: {summ.get('placed_either')} placed on either table: {summ.get('by_category')}")
    return summ, df
