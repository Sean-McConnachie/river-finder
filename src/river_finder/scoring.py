"""Phases 6-8: feature stack, reference signature, composite score, regression, run assembly.

Everything works on the reach segments from ``reaches.py`` (about 250 m each).

Labels
------
The guides list *sections*, not whole rivers, and we don't have the put-in/take-out points yet.
Until we do, a segment counts as a **known run** (positive) when it lies on a listed reference river
(REC2 name), drains >= ``label_min_area_km2`` and its smoothed gradient is >= ``label_min_gradient``.
Segments on *unlisted* reference rivers (the Mangakarengorengo) are **held out**: they are not
trained on and not counted as negatives. They are the test.

Scores (all reported as 0-100 percentiles within the scored set)
-----------------------------------------------------------------
- ``signature``: similarity to the mean known-run feature vector (diagonal Mahalanobis distance).
- ``regression``: L2-regularised logistic regression, known runs vs everything else.
- ``composite``: z-scored features x user weights (defaults below). The viewer recomputes it live.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import geopandas as gpd
import numpy as np
import pandas as pd

RHO_G = 1000.0 * 9.81

# name -> (column builder, default composite weight, description)
FEATURES: dict[str, tuple] = {
    "gradient": (lambda g: g["grad_s"].clip(0, 150), 1.0, "Smoothed gradient, m/km (capped at 150)"),
    "steep50": (lambda g: np.log1p(g["steepest_50m_m_per_km"].clip(0, 1000)), 0.5, "Steepest 50 m, log"),
    "ksn": (lambda g: np.log1p(g["ksn_s"].clip(0, 1000)), 1.0, "Channel steepness k_sn (size-normalised), log"),
    "power": (lambda g: np.log1p(g["power_kw_m"].clip(0, 500)), 1.0, "Stream power at mean annual flood, log kW/m"),
    "maf": (lambda g: np.log10(g["maf_m3s"].clip(0.05, None)), 0.5, "Mean annual flood, log10 m3/s"),
    "gorge": (lambda g: g["gorge_depth_m"].clip(0, 250), 0.75, "Gorge depth, m"),
    "head": (lambda g: g["head_m"].clip(0, 600), 0.5, "Catchment height above channel, m"),
    "plateau": (lambda g: g["upstream_flat_frac"], 0.25, "Flat (plateau/swamp) share of catchment"),
    "hard_up": (lambda g: g["upstream_hard_frac"], 0.5, "Resistant-rock share of catchment"),
    "bed_hard": (lambda g: g["bed_hard"].astype(float), 0.5, "Channel on resistant rock"),
    "knick": (lambda g: np.log(g["knick_ratio"].fillna(1).clip(0.1, 10)), 0.25, "k_sn jump vs downstream, log"),
    "melton": (lambda g: np.log10(g["melton"].clip(1e-3, None)), 0.5, "Height-to-area: relief / sqrt(area), log"),
    "h_to_w": (lambda g: np.log1p(g["height_to_width"].clip(0, 20)), 0.5,
               "Height-to-width: gorge depth / valley width, log"),
}


@dataclass
class RunParams:
    """Sean's three-parameter runnability model: flow, gradient, height-to-area.

    Each parameter maps to a 0-1 *desirability*:
    - flow and gradient: trapezoid bands (a, b, c, d). Zero at or below a, ramps up to full credit
      from b to c, then ramps back to zero at d. Flow is ramped on a log scale.
    - height-to-area: an increasing ramp (lo, hi). ``None`` means "use the 10th-60th percentile of
      all reaches >= 2 km2 in the scored regions".

    A segment passes when the weighted mean desirability is >= ``threshold``. With ``gate``, every
    parameter must also be above zero. Passing segments are chained downstream into runs.
    """
    flow_band: tuple = (2.0, 8.0, 300.0, 2000.0)       # mean annual flood, m3/s
    gradient_band: tuple = (5.0, 20.0, 80.0, 150.0)    # m/km
    ha_measure: str = "melton"                         # "melton" (relief/sqrt(area)) or "height_to_width"
    ha_ramp: tuple | None = None
    weights: tuple = (1.0, 1.0, 1.0)                   # flow, gradient, height-to-area
    threshold: float = 0.7
    gate: bool = True
    flow_scaled: bool = True     # big rivers count at lower gradients: g * clip((MAF/20)^0.5, 1, 3)
    min_length_km: float = 1.0
    max_gap_segments: int = 1    # pools / flatter bits allowed inside a run


@dataclass
class ModelResult:
    features: list[str]
    coef: dict[str, float]
    intercept: float
    mean: dict[str, float]
    std: dict[str, float]
    holdout: dict = field(default_factory=dict)
    lorocv: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------- preparation
def _upstream_main(g: pd.DataFrame) -> np.ndarray:
    """For each segment, the upstream neighbour with the largest area (-1 if none)."""
    up = np.full(len(g), -1)
    best = np.full(len(g), -1.0)
    down = g["down_seg"].to_numpy()
    area = g["area_km2"].to_numpy()
    for i, dsg in enumerate(down):
        if dsg >= 0 and area[i] > best[dsg]:
            best[dsg], up[dsg] = area[i], i
    return up


def prepare(g: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Add smoothed gradient/ksn (over upstream-main + self + downstream, about 750 m) and stream
    power."""
    g = g.copy()
    up = _upstream_main(g)
    down = g["down_seg"].to_numpy()
    for col, out in (("gradient_m_per_km", "grad_s"), ("ksn", "ksn_s")):
        v = g[col].to_numpy(dtype=float)
        L = g["length_m"].to_numpy(dtype=float)
        num, den = v * L, L.copy()
        for nb in (up, down):
            ok = nb >= 0
            num[ok] += v[nb[ok]] * L[nb[ok]]
            den[ok] += L[nb[ok]]
        g[out] = num / den
    g["power_kw_m"] = RHO_G * g["maf_m3s"] * g["grad_s"] / 1000.0 / 1000.0
    g["up_seg"] = up
    return g


def feature_matrix(g: pd.DataFrame, names: list[str] | None = None) -> tuple[np.ndarray, list[str]]:
    names = names or list(FEATURES)
    X = np.column_stack([np.asarray(FEATURES[n][0](g), dtype=float) for n in names])
    return np.nan_to_num(X, nan=0.0), names


def label(g: pd.DataFrame, region, label_min_area_km2: float = 2.0, label_min_gradient: float = 10.0):
    """Return (y, holdout) arrays: y=1 known run, 0 other; holdout=True for unlisted reference rivers.
    A ``region`` with a ``labels(g)`` method (the national labeller) supplies its own."""
    if hasattr(region, "labels"):
        return region.labels(g, label_min_area_km2, label_min_gradient)
    listed = {r.river for r in region.known_runs if r.status == "listed"}
    unlisted = {r.river for r in region.known_runs if r.status != "listed"}
    on_listed = g["river"].isin(listed).to_numpy()
    sized = (g["area_km2"] >= label_min_area_km2).to_numpy() & (g["grad_s"] >= label_min_gradient).to_numpy()
    y = (on_listed & sized).astype(int)
    holdout = g["river"].isin(unlisted).to_numpy()
    return y, holdout


# ---------------------------------------------------------------------------- models
def _standardise(X, mean=None, std=None):
    mean = X.mean(0) if mean is None else mean
    std = X.std(0) if std is None else std
    std = np.where(std > 1e-9, std, 1.0)
    return (X - mean) / std, mean, std


def logistic_fit(X: np.ndarray, y: np.ndarray, l2: float = 1.0, iters: int = 50,
                 class_weight: bool = True) -> tuple[np.ndarray, float]:
    """L2-regularised logistic regression by Newton/IRLS. ``X`` should be standardised."""
    n, p = X.shape
    Xb = np.column_stack([np.ones(n), X])
    w = np.ones(n)
    if class_weight and 0 < y.sum() < n:
        w = np.where(y == 1, 0.5 * n / y.sum(), 0.5 * n / (n - y.sum()))
    beta = np.zeros(p + 1)
    R = np.eye(p + 1) * l2
    R[0, 0] = 0.0
    for _ in range(iters):
        eta = np.clip(Xb @ beta, -30, 30)
        mu = 1 / (1 + np.exp(-eta))
        W = w * mu * (1 - mu)
        grad = Xb.T @ (w * (y - mu)) - R @ beta
        H = Xb.T @ (Xb * W[:, None]) + R
        step = np.linalg.solve(H, grad)
        beta += step
        if np.abs(step).max() < 1e-8:
            break
    return beta[1:], float(beta[0])


def percentile_rank(scores: np.ndarray, ref: np.ndarray | None = None) -> np.ndarray:
    """Percentile (0-100) of each score within ``ref`` (default: itself)."""
    ref = np.sort(scores if ref is None else ref)
    return 100.0 * np.searchsorted(ref, scores, side="right") / len(ref)


def signature_scores(Xs: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Similarity to the known-run centroid: -(diagonal Mahalanobis distance)^2."""
    pos = Xs[y == 1]
    mu, sd = pos.mean(0), np.where(pos.std(0) > 1e-6, pos.std(0), 1.0)
    return -(((Xs - mu) / sd) ** 2).sum(1)


def composite_scores(Xs: np.ndarray, names: list[str], weights: dict[str, float] | None = None) -> np.ndarray:
    w = np.array([(weights or {}).get(n, FEATURES[n][1]) for n in names])
    return Xs @ w


# ---------------------------------------------------------------------------- run assembly
def trapezoid(x, band) -> np.ndarray:
    a, b, c, d = band
    x = np.asarray(x, dtype=float)
    up = np.clip((x - a) / max(b - a, 1e-9), 0, 1)
    down = np.clip((d - x) / max(d - c, 1e-9), 0, 1)
    return np.minimum(up, down)


def effective_gradient(g: pd.DataFrame, p: RunParams) -> np.ndarray:
    grad = g["grad_s"].to_numpy(dtype=float)
    if not p.flow_scaled:
        return grad
    return grad * np.clip((np.maximum(g["maf_m3s"].to_numpy(dtype=float), 1e-3) / 20.0) ** 0.5, 1.0, 3.0)


def default_ha_ramp(frames, measure: str) -> tuple[float, float]:
    v = np.concatenate([f.loc[f["area_km2"] >= 2.0, measure].to_numpy(dtype=float) for f in frames])
    v = v[np.isfinite(v)]
    return float(np.percentile(v, 10)), float(np.percentile(v, 60))


def desirability(g: pd.DataFrame, p: RunParams) -> pd.DataFrame:
    """Per-segment desirabilities d_flow, d_gradient, d_ha and their weighted mean ``wscore``."""
    maf = np.maximum(g["maf_m3s"].to_numpy(dtype=float), 1e-3)
    d_flow = trapezoid(np.log10(maf), tuple(np.log10(p.flow_band)))
    d_grad = trapezoid(effective_gradient(g, p), p.gradient_band)
    lo, hi = p.ha_ramp if p.ha_ramp is not None else default_ha_ramp([g], p.ha_measure)
    d_ha = np.clip((g[p.ha_measure].to_numpy(dtype=float) - lo) / max(hi - lo, 1e-9), 0, 1)
    w = np.asarray(p.weights, dtype=float)
    wscore = (w[0] * d_flow + w[1] * d_grad + w[2] * d_ha) / max(w.sum(), 1e-9)
    return pd.DataFrame({"d_flow": d_flow, "d_gradient": d_grad, "d_ha": d_ha, "wscore": wscore}, index=g.index)


def passes(g: pd.DataFrame, p: RunParams) -> np.ndarray:
    d = desirability(g, p)
    ok = d["wscore"].to_numpy() >= p.threshold
    if p.gate:
        ok &= (d[["d_flow", "d_gradient", "d_ha"]].to_numpy() > 0).all(1)
    return ok


def pareto_layers(points: np.ndarray) -> np.ndarray:
    """Non-dominated sorting (all objectives maximised). Layer 1 is the Pareto frontier."""
    n = len(points)
    layer = np.zeros(n, dtype=int)
    remaining = np.arange(n)
    k = 0
    while remaining.size:
        k += 1
        P = points[remaining]
        dominated = np.zeros(len(remaining), dtype=bool)
        for i in range(len(remaining)):
            ge = (P >= P[i]).all(1) & (P > P[i]).any(1)
            dominated[i] = ge.any()
        layer[remaining[~dominated]] = k
        remaining = remaining[dominated]
    return layer


def assemble_runs(g: pd.DataFrame, p: RunParams) -> pd.DataFrame:
    """Chain passing segments downstream into runs (allowing short gaps). Returns a run table and
    sets ``g['run_id']`` (-1 where none)."""
    ok = passes(g, p)
    down = g["down_seg"].to_numpy()
    up = g["up_seg"].to_numpy() if "up_seg" in g else _upstream_main(g)
    L = g["length_m"].to_numpy()
    run_id = np.full(len(g), -1)
    runs = []
    for s in np.flatnonzero(ok):
        if run_id[s] >= 0:
            continue
        # only start at a passing segment whose main upstream neighbour does not pass
        if up[s] >= 0 and ok[up[s]]:
            continue
        members, gap, cur = [], [], s
        while cur >= 0 and run_id[cur] < 0:
            if ok[cur]:
                members += gap + [cur]
                gap = []
            else:
                gap.append(cur)
                if len(gap) > p.max_gap_segments:
                    break
            cur = down[cur]
        length = L[members].sum() / 1000.0
        if length < p.min_length_km:
            continue
        rid = len(runs)
        run_id[members] = rid
        sub = g.iloc[members]
        runs.append({
            "run_id": rid, "n_segments": len(members), "length_km": round(length, 2),
            "top_seg": int(members[0]), "bottom_seg": int(members[-1]),
            "drop_m": round(float(sub["z_top"].iloc[0] - sub["z_bot"].iloc[-1]), 1),
            "mean_gradient": round(float((sub["gradient_m_per_km"] * sub["length_m"]).sum()
                                         / sub["length_m"].sum()), 1),
            "max_steep50": round(float(sub["steepest_50m_m_per_km"].max()), 1),
            "area_top_km2": round(float(sub["area_km2"].iloc[0]), 2),
            "area_bottom_km2": round(float(sub["area_km2"].iloc[-1]), 2),
            "maf_bottom_m3s": round(float(sub["maf_m3s"].iloc[-1]), 1),
            "gorge_depth_m": round(float(sub["gorge_depth_m"].mean()), 1),
            "melton": round(float(sub["melton"].median()), 4) if "melton" in sub else np.nan,
            "height_to_width": round(float(sub["height_to_width"].median()), 3) if "height_to_width" in sub else np.nan,
            "eff_gradient": round(float(np.average(effective_gradient(sub, p), weights=sub["length_m"])), 1),
            "wscore": round(float(np.average(desirability(sub, p)["wscore"], weights=sub["length_m"])), 3)
            if p.ha_ramp is not None else np.nan,
            "hard_bed_frac": round(float(sub["bed_hard"].mean()), 2),
            "river": (sub["river"][sub["river"] != ""].mode().iloc[0] if (sub["river"] != "").any() else ""),
        })
    g["run_id"] = run_id
    out = pd.DataFrame(runs)
    if len(out):
        ha = out[p.ha_measure].to_numpy(dtype=float)
        pts = np.column_stack([np.log10(np.maximum(out["maf_bottom_m3s"], 1e-3)), out["eff_gradient"], ha])
        out["pareto_layer"] = pareto_layers(np.nan_to_num(pts, nan=-1e9))
    return out


# ---------------------------------------------------------------------------- end-to-end
def score_regions(frames: dict[str, gpd.GeoDataFrame], regions: dict, l2: float = 1.0) -> tuple[dict, ModelResult]:
    """Fit the regression on all regions pooled (holding out unlisted rivers), score every segment
    with all three methods, and run the hold-out and leave-one-river-out tests."""
    prepped, ys, hos, Xs_all = {}, {}, {}, []
    names = list(FEATURES)
    for k, g in frames.items():
        g = prepare(g)
        y, ho = label(g, regions[k])
        prepped[k], ys[k], hos[k] = g, y, ho
        Xs_all.append(feature_matrix(g, names)[0])
    X_all = np.vstack(Xs_all)
    y_all = np.concatenate([ys[k] for k in frames])
    ho_all = np.concatenate([hos[k] for k in frames])
    river_all = np.concatenate([prepped[k]["river"].to_numpy() for k in frames])
    Z, mean, std = _standardise(X_all)
    train = ~ho_all
    coef, b0 = logistic_fit(Z[train], y_all[train], l2=l2)
    reg = Z @ coef + b0
    sig = signature_scores(Z, np.where(train, y_all, 0))
    comp = composite_scores(Z, names)

    # Rank among "candidate-sized" segments that are not training positives.
    big = np.concatenate([(prepped[k]["area_km2"] >= 2.0).to_numpy() for k in frames])
    pool = big & (y_all == 0)

    def ho_report(score):
        out = {}
        for rv in sorted(set(river_all[ho_all])):
            m = (river_all == rv) & ho_all & big
            if not m.any():
                continue
            pct = percentile_rank(score[m], score[pool])
            out[rv] = {"segments": int(m.sum()), "best_pct": round(float(pct.max()), 1),
                       "median_pct": round(float(np.median(pct)), 1),
                       "share_top10pct": round(float((pct >= 90).mean()), 2)}
        return out

    holdout = {"regression": ho_report(reg), "signature": ho_report(sig), "composite": ho_report(comp)}

    # Leave-one-river-out: refit without each listed river, rank its positives against the pool.
    loro = {}
    for rv in sorted(set(river_all[y_all == 1])):
        out_m = river_all == rv
        tr = train & ~out_m
        c, b = logistic_fit(Z[tr], y_all[tr], l2=l2)
        s = Z @ c + b
        m = out_m & (y_all == 1)
        pct = percentile_rank(s[m], s[pool & ~out_m])
        loro[rv] = {"segments": int(m.sum()), "median_pct": round(float(np.median(pct)), 1),
                    "share_top10pct": round(float((pct >= 90).mean()), 2)}

    methods = (("regression", reg), ("signature", sig), ("composite", comp))
    pct = {name: percentile_rank(s, s[big]) for name, s in methods}
    i0 = 0
    for k in frames:
        n = len(prepped[k])
        g = prepped[k]
        g["label"] = y_all[i0:i0 + n]
        g["holdout"] = ho_all[i0:i0 + n]
        for name, v in pct.items():
            g[f"score_{name}"] = np.round(v[i0:i0 + n], 1)
        i0 += n
        prepped[k] = g
    model = ModelResult(names, {n: round(float(c), 4) for n, c in zip(names, coef, strict=True)}, round(b0, 4),
                        {n: float(m) for n, m in zip(names, mean, strict=True)},
                        {n: float(s) for n, s in zip(names, std, strict=True)}, holdout, loro)
    return prepped, model


def model_json(m: ModelResult) -> str:
    return json.dumps(m.__dict__, indent=2)


def score_and_write(regions: list, data_root, params: RunParams | None = None) -> dict:
    """Score all regions together (pooled regression) and write per-region outputs:
    ``segments_scored.gpkg``, ``runs.geojson``, ``candidates.csv``; plus ``data/model.json``."""
    from pathlib import Path

    params = params or RunParams()
    data_root = Path(data_root)
    frames = {r.name: gpd.read_file(r.data_dir() / "segments.gpkg") for r in regions}
    ha_defaults = {m: default_ha_ramp(list(frames.values()), m) for m in ("melton", "height_to_width")}
    if params.ha_ramp is None:
        params.ha_ramp = ha_defaults[params.ha_measure]
    regmap = {r.name: r for r in regions}
    scored, model = score_regions(frames, regmap)
    summary = {"holdout": model.holdout, "leave_one_river_out": model.lorocv, "coef": model.coef, "regions": {}}
    listed = {r.river for reg in regions for r in reg.known_runs if r.status == "listed"}
    for name, g in scored.items():
        d = regmap[name].data_dir()
        runs = assemble_runs(g, params)
        Z = (feature_matrix(g, model.features)[0] - np.array([model.mean[f] for f in model.features])) / np.array(
            [model.std[f] if model.std[f] > 1e-9 else 1.0 for f in model.features])
        for i, f in enumerate(model.features):
            g[f"z_{f}"] = np.round(Z[:, i], 3)
        out = d / "segments_scored.gpkg"
        out.unlink(missing_ok=True)
        g.to_file(out, layer="segments", driver="GPKG")
        if len(runs):
            sc = g.groupby("run_id")[["score_regression", "score_composite"]].max()
            runs = runs.join(sc, on="run_id")
            runs["status"] = np.where(runs["river"].isin(listed), "listed",
                                      np.where(runs["river"].isin({r.river for r in regmap[name].known_runs}),
                                               "reference (unlisted)", "candidate"))
            geoms = [g.geometry.iloc[g.index[g["run_id"] == rid]].union_all() for rid in runs["run_id"]]
            rg = gpd.GeoDataFrame(runs, geometry=geoms, crs=g.crs)
            rg.to_crs(4326).to_file(d / "runs.geojson", driver="GeoJSON")
            cand = runs[runs["status"] != "listed"].sort_values(["pareto_layer", "wscore"], ascending=[True, False])
            cand.to_csv(d / "candidates.csv", index=False)
        summary["regions"][name] = {
            "segments": len(g), "positives": int(g["label"].sum()), "runs": len(runs),
            "runs_on_listed_rivers": int((runs["status"] == "listed").sum()) if len(runs) else 0,
            "holdout_runs": runs[runs["status"] == "reference (unlisted)"][
                ["river", "length_km", "mean_gradient", "maf_bottom_m3s", "score_regression"]].to_dict("records")
            if len(runs) else [],
        }
    (data_root / "model.json").write_text(json.dumps(
        {**model.__dict__, "run_params": params.__dict__, "ha_defaults": ha_defaults,
         "feature_descriptions": {k: v[2] for k, v in FEATURES.items()}}, indent=2))
    return summary
