"""Score the national tiles: one pooled model for all of NZ, outputs per river-guide region.

1. Merge every tile's core segments (``nz.merge_segments``), stitching downstream links across
   tile edges and reconnecting the rest along REC2 (``relink.py``), and give each segment its
   river-guide region (``national.assign_regions``). On the 1 m rebuild (``national.is_1m``) the merge is done
   already: read ``reaches.parquet`` and ``joins.parquet`` (``reaches_1m``).
2. Split into one frame per region (``down_seg`` re-indexed within the region).
3. Label known runs. For now these are the reference rivers of ``regions/*.toml``, matched by
   name *inside that reference region's box* (the same names recur elsewhere in NZ).
4. Fit the regression on all regions pooled; compute signature and composite scores.
5. Per region: assemble runs, Pareto layers, write ``data/nz/regions/<slug>/``
   (``segments_scored.gpkg``, ``runs.geojson``, ``candidates.csv``), plus ``data/nz/model.json``
   and ``data/nz/candidates.csv`` for the whole country.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import geopandas as gpd
import numpy as np
import pandas as pd
from pyproj import Transformer

from .national import FILTER_ORDER, assign_regions, nz_dir
from .regions import Region, list_regions, load_region
from .scoring import FEATURES, RunParams, assemble_runs, default_ha_ramp, feature_matrix, score_regions


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower().replace("'", "")).strip("-")


def ref_regions() -> list[Region]:
    return [load_region(n) for n in list_regions()]


def _inside(g: gpd.GeoDataFrame, bbox_wgs84) -> np.ndarray:
    t = Transformer.from_crs("EPSG:2193", "EPSG:4326", always_xy=True)
    mid = g.geometry.interpolate(0.5, normalized=True)
    lon, lat = t.transform(mid.x.to_numpy(), mid.y.to_numpy())
    x0, y0, x1, y1 = bbox_wgs84
    return (lon >= x0) & (lon <= x1) & (lat >= y0) & (lat <= y1)


@dataclass
class NationalLabels:
    """Known runs = reference rivers, by name, within their reference region's box."""
    refs: list[Region] = field(default_factory=ref_regions)

    def masks(self, g: gpd.GeoDataFrame) -> tuple[np.ndarray, np.ndarray]:
        listed = np.zeros(len(g), bool)
        held = np.zeros(len(g), bool)
        for r in self.refs:
            inside = _inside(g, r.bbox_wgs84)
            names_l = {k.river for k in r.known_runs if k.status == "listed"}
            names_u = {k.river for k in r.known_runs if k.status != "listed"}
            listed |= inside & g["river"].isin(names_l).to_numpy()
            held |= inside & g["river"].isin(names_u).to_numpy()
        return listed, held

    def labels(self, g, min_area: float, min_grad: float):
        listed, held = self.masks(g)
        sized = (g["area_km2"] >= min_area).to_numpy() & (g["grad_s"] >= min_grad).to_numpy()
        return (listed & sized).astype(int), held


def reaches_1m(root=None, tiles=None, log=print) -> gpd.GeoDataFrame:
    """The 1 m rebuild's national reaches: ``reaches.parquet`` (the merge) with ``joins.parquet`` (``nz1mjoin``:
    geology at the midpoint, LINZ names and distance, LiDAR survey, sinuosity; its ``river`` replaces the merge's
    REC2-only name). ``tiles``: only these (``nz.Tile`` or ids)."""
    root = root or nz_dir()
    g = gpd.read_parquet(root / "reaches.parquet")
    if tiles:
        ids = {getattr(t, "id", t) for t in tiles}
        g = g[g["tile"].isin(ids)].reset_index(drop=True)
    jp = root / "joins.parquet"
    if not jp.exists():
        raise SystemExit(f"no {jp}: run `river-finder nz1m finish --steps joins` first")
    j = pd.read_parquet(jp).drop_duplicates("gid").set_index("gid").reindex(g["gid"].to_numpy())
    if j.index.isna().any() or j["river"].isna().any():
        raise SystemExit(f"{jp} does not cover {root / 'reaches.parquet'}: run the joins again")
    for c in j.columns:
        g[c] = j[c].to_numpy()
    log(f"1 m reaches: {len(g)} from {root / 'reaches.parquet'} + {jp.name}")
    return g


def national_frames(tiles=None, log=print) -> dict[str, gpd.GeoDataFrame]:
    from .national import is_1m

    if is_1m():
        g = reaches_1m(tiles=tiles, log=log)   # regions come with the merge
    else:
        from .nz import merge_segments

        g = merge_segments(tiles, log=log)
        g["region"] = assign_regions(g.geometry.interpolate(0.5, normalized=True))
    frames = {}
    for name in FILTER_ORDER:
        f = g[g["region"] == name].reset_index(drop=True)
        if not len(f):
            continue
        pos = pd.Series(np.arange(len(f)), index=f["gid"].to_numpy())
        f["seg_id"] = np.arange(len(f))
        f["down_seg"] = f["down_gid"].map(pos).fillna(-1).astype(int).to_numpy()
        frames[name] = gpd.GeoDataFrame(f, geometry="geometry", crs="EPSG:2193")
    return frames


def region_dir(name: str):
    d = nz_dir() / "regions" / slug(name)
    d.mkdir(parents=True, exist_ok=True)
    return d


def score_national(tiles=None, params: RunParams | None = None, log=print) -> dict:
    params = params or RunParams()
    frames = national_frames(tiles, log=log)
    log(f"segments: {sum(len(f) for f in frames.values())} in {len(frames)} regions")
    ha_defaults = {m: default_ha_ramp(list(frames.values()), m) for m in ("melton", "height_to_width")}
    if params.ha_ramp is None:
        params.ha_ramp = ha_defaults[params.ha_measure]
    lab = NationalLabels()
    scored, model = score_regions(frames, {k: lab for k in frames})
    log(f"model fitted; hold-out {json.dumps(model.holdout.get('regression'))}")
    summary = {"holdout": model.holdout, "leave_one_river_out": model.lorocv, "coef": model.coef, "regions": {}}
    allc = []
    for name, g in scored.items():
        d = region_dir(name)
        runs = assemble_runs(g, params)
        Z = (feature_matrix(g, model.features)[0] - np.array([model.mean[f] for f in model.features])) / np.array(
            [model.std[f] if model.std[f] > 1e-9 else 1.0 for f in model.features])
        for i, f in enumerate(model.features):
            g[f"z_{f}"] = np.round(Z[:, i], 3)
        listed, held = lab.masks(g)
        out = d / "segments_scored.gpkg"
        out.unlink(missing_ok=True)
        g.to_file(out, layer="segments", driver="GPKG")
        if len(runs):
            sc = g.groupby("run_id")[["score_regression", "score_composite"]].max()
            runs = runs.join(sc, on="run_id")
            rid = g["run_id"].to_numpy()
            on_l = pd.Series(listed).groupby(rid).any()
            on_h = pd.Series(held).groupby(rid).any()
            runs["status"] = np.where(runs["run_id"].map(on_l).fillna(False), "listed",
                                      np.where(runs["run_id"].map(on_h).fillna(False), "reference (unlisted)",
                                               "candidate"))
            runs["region"] = name
            geoms = [g.geometry.iloc[np.flatnonzero(rid == r)].union_all() for r in runs["run_id"]]
            rg = gpd.GeoDataFrame(runs, geometry=geoms, crs=g.crs)
            rg.to_crs(4326).to_file(d / "runs.geojson", driver="GeoJSON")
            cand = runs[runs["status"] != "listed"].sort_values(["pareto_layer", "wscore"], ascending=[True, False])
            cand.to_csv(d / "candidates.csv", index=False)
            allc.append(cand)
        summary["regions"][name] = {"segments": len(g), "positives": int(g["label"].sum()), "runs": len(runs)}
        log(f"[{name}] {len(g)} segments, {len(runs)} runs")
    if allc:
        pd.concat(allc).sort_values(["pareto_layer", "score_regression"], ascending=[True, False]).to_csv(
            nz_dir() / "candidates.csv", index=False)
    (nz_dir() / "model.json").write_text(json.dumps(
        {**model.__dict__, "run_params": params.__dict__, "ha_defaults": ha_defaults,
         "feature_descriptions": {k: v[2] for k, v in FEATURES.items()}}, indent=2))
    (nz_dir() / "score_summary.json").write_text(json.dumps(summary, indent=1, default=str))
    return summary
