"""ML evaluation: extended reach features, catchment-grouped repeated k-fold OOF and leave-one-region-out.

Everything here works on the national reach table (``models.national_table``) restricted to reaches
draining 2 km2 or more (the *domain*, about 580k reaches).

Labels (positive-unlabelled)
----------------------------
A reach is **positive** when a runnable guide section covers it (``models.map_sections`` over
``labels/known_runs.toml`` + ``labels/guide_research.json``; ``models._positive_rows``). Sections
marked ``held_out`` (the Mangakarengorengo) are scored but never trained on. Every other reach of a
known section is left out (neither positive nor background). All remaining domain reaches are
**background**: unlabelled, used as negatives in training, and the reference ranking in evaluation.
Background still holds unknown runs, so every AUC here is a lower bound.

Only the section extents and ``status`` / ``held_out`` / ``grade`` (for reporting) are used from
the guides. Guide fields such as tags, portages, deaths, popularity, commercially rafted, access
type, gauge, flow notes, length and stated gradient are never features.

Cross-validation
----------------
Folds hold whole **outlet catchments** (every reach that drains to the same sea outlet), so a
river, its tributaries and its neighbours downstream are never split between training and test.
Every reach in the domain, positive or background, gets an out-of-fold (OOF) score from the model
that did not see its catchment. Catchments with positives are dealt to folds greedily so each fold
gets a similar number of positive rivers; the rest are dealt at random. ``REPEATS`` repeats with
different seeds give a spread. Leave-one-region-out (LORO) holds out every catchment whose reaches
lie mostly in one guide region, trains on the rest and evaluates inside that region.

Metrics (see ``metrics``)
-------------------------
- ``auc``: positives vs all background (pooled OOF scores).
- ``auc_style``: **headline**. Each positive is ranked only against background reaches of the same
  paddling style (Montgomery & Buffington reach type, ``reachtype.py``); the mean of those
  percentiles. It is the AUC within style, weighted by positives.
- ``auc_size``: the same with 10 bins of log mean annual flood (the earlier "size-matched").
- ``auc_style_size``: within style x size quintile.
- ``recall_top10`` / ``recall_top5``: share of positive reaches in the top 10% / 5% of background.
- per region, per style, per grade band, per river.

Model inputs never include access (roads, tracks): whether a run can be reached is the paddler's
call, and access correlates with being in a guide, which would reward documentation over terrain.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import time
from dataclasses import dataclass, field

import numba
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

from . import reachtype as RT
from .models import MODELS_DIR, Mapped, _positive_rows, all_sections, grade_band, map_sections, national_table
from .national import nz_dir
from .scoring import FEATURES, _standardise, logistic_fit

CV_DIR = MODELS_DIR / "cv"
DOMAIN_KM2 = 2.0
FOLDS = 5
REPEATS = 5
SEED = 20261118
KARST_ROCKS = {"limestone", "marble", "dolomite", "calcarenite", "coquina", "calcareous sandstone"}
RAPIDS_SNAP_M = 150.0
ROCKCLASS_KEEP = ("clastic sediment", "schist", "felsic extrusive", "mafic extrusive", "felsic intrusive",
                  "intermediate extrusive", "mafic intrusive", "chemical sediment", "gneiss", "intermediate intrusive")


# ---------------------------------------------------------------------------- network helpers
@numba.njit(cache=True)
def _kahn(down):
    n = len(down)
    indeg = np.zeros(n, np.int64)
    for i in range(n):
        if down[i] >= 0:
            indeg[down[i]] += 1
    order = np.empty(n, np.int64)
    head = 0
    tail = 0
    for i in range(n):
        if indeg[i] == 0:
            order[tail] = i
            tail += 1
    while head < tail:
        i = order[head]
        head += 1
        d = down[i]
        if d >= 0:
            indeg[d] -= 1
            if indeg[d] == 0:
                order[tail] = d
                tail += 1
    return order[:tail]


@numba.njit(cache=True)
def _accumulate(down, order, v):
    acc = v.copy()
    for k in range(len(order)):
        i = order[k]
        if down[i] >= 0:
            acc[down[i]] += acc[i]
    return acc


@numba.njit(cache=True)
def _outlets(down, order):
    out = np.arange(len(down))
    for k in range(len(order) - 1, -1, -1):
        i = order[k]
        if down[i] >= 0:
            out[i] = out[down[i]]
    return out


def local_area(t: pd.DataFrame) -> np.ndarray:
    """Area draining directly to each reach: its area minus the areas of the reaches above it."""
    down = t["down"].to_numpy()
    a = t["area_km2"].to_numpy(dtype=float)
    inflow = np.zeros(len(t))
    ok = down >= 0
    np.add.at(inflow, down[ok], a[ok])
    return np.clip(a - inflow, 0.0, None)


# ---------------------------------------------------------------------------- feature table
def ml_table(rebuild: bool = False, log=print) -> pd.DataFrame:
    """``national_table`` plus geology, karst, OSM waterfalls/rapids, catchment and reach type.

    Cached in ``data/nz/ml_table.pkl``.
    """
    cache = nz_dir() / "ml_table.pkl"
    if cache.exists() and not rebuild:
        return pd.read_pickle(cache)
    import geopandas as gpd
    import pyogrio

    t = national_table(log=log).copy()
    t0 = time.time()
    down = t["down"].to_numpy().astype(np.int64)
    order = _kahn(down)
    if len(order) < len(t):
        log(f"ml: {len(t) - len(order)} reaches in cycles; they keep their own catchment")
    t["outlet"] = _outlets(down, order)
    # geology at the reach midpoint
    geo = pyogrio.read_dataframe(nz_dir() / "geology.gpkg",
                                 columns=["rockclass", "rockgroup", "mainrock", "absmin_ma", "hard_rock"])
    pts = gpd.GeoDataFrame(geometry=gpd.points_from_xy(t["x"], t["y"]), crs="EPSG:2193")
    j = gpd.sjoin(pts, geo, predicate="within", how="left")
    j = j[~j.index.duplicated()].reindex(t.index)
    for c in ("rockclass", "rockgroup", "mainrock"):
        t[c] = j[c].fillna("unknown").astype(str).str.strip().str.lower().to_numpy()
    t["rock_age_ma"] = pd.to_numeric(j["absmin_ma"], errors="coerce").to_numpy()
    t["karst_bed"] = t["mainrock"].isin(KARST_ROCKS).astype(np.int8)
    la = local_area(t)
    kacc = _accumulate(down, order, la * t["karst_bed"].to_numpy())
    aacc = _accumulate(down, order, la)
    t["karst_up_frac"] = np.where(aacc > 0, kacc / np.maximum(aacc, 1e-9), 0.0).clip(0, 1)
    log(f"ml: geology + karst ({time.time() - t0:.0f} s), {int(t['karst_bed'].sum())} karst reaches")
    # OSM waterfalls (snapped to the reach line by external.attach) and rapids (nearest midpoint)
    xp = nz_dir() / "external.pkl"
    if xp.exists():
        x = pd.read_pickle(xp)
        if len(x) == len(t) and (x["seg_id"].to_numpy() == t["seg_id"].to_numpy()).all():
            t["falls_n"] = x["falls_n"].to_numpy()
            t["falls_max_h_m"] = x["falls_max_h_m"].to_numpy()
        else:
            log("ml: external.pkl does not line up with the table; run `river-finder external attach`")
    wf_path = nz_dir() / "waterfalls.gpkg"
    if wf_path.exists():
        wf = pyogrio.read_dataframe(wf_path)
        rp = wf[wf["kind"] != "waterfall"]
        tree = cKDTree(np.column_stack([t["x"], t["y"]]))
        d, i = tree.query(np.column_stack([rp.geometry.x, rp.geometry.y]), distance_upper_bound=RAPIDS_SNAP_M)
        ok = np.isfinite(d)
        n = np.zeros(len(t), np.int16)
        np.add.at(n, i[ok], 1)
        t["rapids_n"] = n
    for c in ("falls_n", "falls_max_h_m", "rapids_n"):
        if c not in t:
            t[c] = np.nan
    rt = RT.classify_frame(t)
    t = pd.concat([t, rt], axis=1)
    t.to_pickle(cache)
    log(f"ml: table {len(t)} reaches -> {cache}")
    return t


# name -> (builder, description, group)
EXTRA: dict[str, tuple] = {
    "valley_w": (lambda g: np.log10(g["valley_width_m"].clip(8, 20000)), "Valley-floor width (HAND <= 5 m), log10 m",
                 "terrain"),
    "confine": (lambda g: np.log10(g["confinement_ratio"].clip(0.05, 1000)),
                "Valley width / estimated bankfull width, log10", "terrain"),
    "karst_bed": (lambda g: g["karst_bed"].astype(float), "Channel on karst rock (limestone, marble, dolomite)",
                  "geology"),
    "karst_up": (lambda g: g["karst_up_frac"], "Karst share of the catchment (reach-midpoint geology)", "geology"),
    "rock_age": (lambda g: np.log10(g["rock_age_ma"].fillna(1.0).clip(0.01, 3000)),
                 "Minimum age of the bed rock unit, log10 Ma", "geology"),
    "falls": (lambda g: np.log1p(g["falls_n"].fillna(0)), "OSM waterfalls on the reach (snapped within 100 m), log",
              "osm"),
    "falls_h": (lambda g: g["falls_max_h_m"].fillna(0).clip(0, 200), "Tallest OSM waterfall on the reach, m", "osm"),
    "rapids": (lambda g: np.log1p(g["rapids_n"].fillna(0)), "OSM rapids within 150 m of the reach midpoint, log",
               "osm"),
    "x_km": (lambda g: g["x"] / 1000.0, "NZTM easting, km", "xy"),
    "y_km": (lambda g: g["y"] / 1000.0, "NZTM northing, km", "xy"),
}
CATEGORICAL = {"rockclass": "GNS rock class at the reach midpoint", "rockgroup": "GNS rock group at the reach midpoint"}
BASE = list(FEATURES)
TERRAIN = ["valley_w", "confine", "karst_bed", "karst_up", "rock_age"]
OSM = ["falls", "falls_h", "rapids"]


def describe(name: str) -> str:
    if name in FEATURES:
        return FEATURES[name][2]
    if name in EXTRA:
        return EXTRA[name][1]
    if name in CATEGORICAL:
        return CATEGORICAL[name]
    if name.startswith("rc_"):
        return f"Rock class is {name[3:].replace('_', ' ')} (one-hot)"
    return name


def column(t: pd.DataFrame, name: str) -> np.ndarray:
    if name in FEATURES:
        return np.asarray(FEATURES[name][0](t), dtype=float)
    if name in EXTRA:
        return np.asarray(EXTRA[name][0](t), dtype=float)
    if name.startswith("rc_"):
        return (t["rockclass"].to_numpy() == name[3:].replace("_", " ")).astype(float)
    raise KeyError(name)


def matrix(t: pd.DataFrame, names: list[str]) -> np.ndarray:
    return np.nan_to_num(np.column_stack([column(t, n) for n in names]), nan=0.0)


ROCK_ONEHOT = [f"rc_{c.replace(' ', '_')}" for c in ROCKCLASS_KEEP]


@dataclass
class Spec:
    id: str
    kind: str                     # "logistic" | "gbm"
    title: str
    features: list[str]
    categorical: list[str] = field(default_factory=list)
    description: str = ""


ZOO = [
    Spec("flow", "logistic", "Flood size only", ["maf"],
         description="Logistic regression on log mean annual flood. The 'big rivers are in guides' baseline."),
    Spec("grad_flow", "logistic", "Gradient + flood", ["gradient", "maf"],
         description="Logistic regression on smoothed gradient and log mean annual flood: Sean's two main levers."),
    Spec("logit13", "logistic", "Logistic, 13 terrain features", BASE,
         description="The v2 recipe (13 terrain, hydrology and geology features) under the new CV."),
    Spec("logit_plus", "logistic", "Logistic, all features", BASE + TERRAIN + OSM + ROCK_ONEHOT,
         description="13 features + valley width, confinement, karst, rock age, rock-class one-hots and OSM "
                     "waterfalls/rapids."),
    Spec("gbm13", "gbm", "LightGBM, 13 terrain features", BASE,
         description="Gradient-boosted trees on the v2 features: same inputs as logit13, but interactions allowed."),
    Spec("gbm_terrain", "gbm", "LightGBM, terrain + geology", BASE + TERRAIN, ["rockclass", "rockgroup"],
         description="13 features + valley width, confinement, karst, rock age, rock class and group (categorical)."),
    Spec("gbm_osm", "gbm", "LightGBM, terrain + geology + OSM", BASE + TERRAIN + OSM, ["rockclass", "rockgroup"],
         description="gbm_terrain + OSM waterfalls and rapids. OSM rapids are often mapped by paddlers on known "
                     "runs, so a gain here can be documentation rather than terrain."),
    Spec("gbm_xy", "gbm", "LightGBM, all + coordinates", BASE + TERRAIN + OSM + ["x_km", "y_km"],
         ["rockclass", "rockgroup"],
         description="gbm_osm + NZTM coordinates. Here to show how much location alone inflates CV; compare its "
                     "leave-one-region-out scores."),
]
GBM_PARAMS = dict(objective="binary", learning_rate=0.05, n_estimators=400, num_leaves=31, min_data_in_leaf=200,
                  bagging_fraction=0.8, bagging_freq=1, feature_fraction=0.8, lambda_l2=1.0, verbose=-1, num_threads=3)
NEG_SAMPLE = 0.3   # GBM trains on this share of background (weighted back up)


# ---------------------------------------------------------------------------- labels
@dataclass
class Labels:
    domain: np.ndarray            # bool, area >= DOMAIN_KM2
    pos: np.ndarray               # bool, positive (trainable)
    pos_eval: np.ndarray          # bool, positive for evaluation (includes held-out sections)
    bg: np.ndarray                # bool, background
    neg_known: np.ndarray         # bool, labelled not runnable
    river: np.ndarray             # object, river key of positive reaches ("" otherwise)
    grade: np.ndarray             # object, grade band of positive reaches
    mapped: list[Mapped]


def labels(t: pd.DataFrame, mapped: list[Mapped] | None = None) -> Labels:
    mapped = mapped if mapped is not None else map_sections(t, all_sections())
    n = len(t)
    domain = (t["area_km2"] >= DOMAIN_KM2).to_numpy()
    known = np.zeros(n, bool)
    pos, pos_eval, neg = np.zeros(n, bool), np.zeros(n, bool), np.zeros(n, bool)
    river, grade = np.full(n, "", dtype=object), np.full(n, "", dtype=object)
    for m in mapped:
        if not len(m.rows):
            continue
        known[m.rows] = True
        if m.section.status != "runnable":
            neg[m.rows] = True
            continue
        rows = _positive_rows(m, t)
        rows = rows[domain[rows]]
        pos_eval[rows] = True
        if not m.section.held_out:
            pos[rows] = True
        river[rows] = m.key
        grade[rows] = grade_band(m.section)
    neg &= domain & ~pos_eval
    return Labels(domain, pos, pos_eval, domain & ~known, neg, river, grade, mapped)


# ---------------------------------------------------------------------------- folds
def catchment_region(t: pd.DataFrame) -> pd.Series:
    """Guide region holding most of each outlet catchment's domain reaches."""
    d = t.loc[t["area_km2"] >= DOMAIN_KM2, ["outlet", "guide_region"]]
    return d.groupby("outlet")["guide_region"].agg(lambda s: s.value_counts().index[0])


def make_folds(t: pd.DataFrame, L: Labels, k: int = FOLDS, seed: int = SEED) -> np.ndarray:
    """Fold (0..k-1) per reach; whole outlet catchments, balanced by positive rivers."""
    rng = np.random.default_rng(seed)
    outlet = t["outlet"].to_numpy()
    cats = np.unique(outlet)
    fold_of = dict(zip(cats.tolist(), rng.integers(0, k, len(cats)).tolist(), strict=True))
    pr = pd.DataFrame({"outlet": outlet[L.pos_eval], "river": L.river[L.pos_eval]})
    per = pr.groupby("outlet")["river"].nunique()
    labelled = per.index.to_numpy().copy()
    rng.shuffle(labelled)
    labelled = sorted(labelled.tolist(), key=lambda c: -per[c])   # stable: big ones first, random ties
    load = np.zeros(k)
    for c in labelled:
        f = int(np.argmin(load + rng.random(k) * 1e-6))
        fold_of[c] = f
        load[f] += per[c]
    return pd.Series(outlet).map(fold_of).to_numpy()


# ---------------------------------------------------------------------------- fitting
def _fit_predict(spec: Spec, t: pd.DataFrame, X: np.ndarray, Xcat: pd.DataFrame | None, train: np.ndarray,
                 y: np.ndarray, score: np.ndarray, rng) -> dict:
    """Fit on ``train`` rows and return scores for ``score`` rows, plus importances."""
    if spec.kind == "logistic":
        Z, mean, std = _standardise(X[train])
        coef, b0 = logistic_fit(Z, y[train], l2=1.0)
        s = ((X[score] - mean) / std) @ coef + b0
        return {"score": s, "coef": dict(zip(spec.features, coef.round(4).tolist(), strict=True))}
    import lightgbm as lgb

    tr = np.flatnonzero(train)
    keep = (y[tr] == 1) | (rng.random(len(tr)) < NEG_SAMPLE)
    tr = tr[keep]
    w = np.where(y[tr] == 1, 1.0, 1.0 / NEG_SAMPLE)
    cols = spec.features + spec.categorical

    def frame(rows):
        f = pd.DataFrame(X[rows], columns=spec.features)
        for c in spec.categorical:
            f[c] = Xcat[c].iloc[rows].to_numpy()
            f[c] = pd.Categorical(f[c], categories=Xcat[c].cat.categories)
        return f[cols]

    params = {k: v for k, v in GBM_PARAMS.items() if k != "n_estimators"}
    params["seed"] = int(rng.integers(1 << 30))
    ds = lgb.Dataset(frame(tr), y[tr], weight=w, categorical_feature=spec.categorical or "auto", free_raw_data=True)
    b = lgb.train(params, ds, num_boost_round=GBM_PARAMS["n_estimators"])
    s = b.predict(frame(np.flatnonzero(score)), raw_score=True)
    gain = b.feature_importance(importance_type="gain")
    return {"score": s, "gain": dict(zip(cols, (gain / max(gain.sum(), 1e-9)).round(5).tolist(), strict=True))}


def _cats(t: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(index=t.index)
    for c in CATEGORICAL:
        v = t[c].astype(str)
        top = v[t["area_km2"] >= DOMAIN_KM2].value_counts()
        keep = set(top.index[:30])
        out[c] = pd.Categorical(np.where(v.isin(keep), v, "other"))
    return out


def run_cv(t: pd.DataFrame, L: Labels, specs: list[Spec] = ZOO, repeats: int = REPEATS, k: int = FOLDS,
           log=print) -> dict:
    """Repeated grouped k-fold OOF scores per model: {model: (repeats, n) float32, NaN outside domain}."""
    n = len(t)
    y = L.pos.astype(int)
    trainable = L.domain & (L.pos | L.bg | L.neg_known)
    Xcat = _cats(t)
    folds = [make_folds(t, L, k, SEED + r) for r in range(repeats)]
    oof, imp = {}, {}
    for spec in specs:
        X = matrix(t, spec.features)
        S = np.full((repeats, n), np.nan, np.float32)
        imps = []
        t0 = time.time()
        for r in range(repeats):
            rng = np.random.default_rng(SEED + 1000 * r)
            for f in range(k):
                test = L.domain & (folds[r] == f)
                train = trainable & (folds[r] != f)
                out = _fit_predict(spec, t, X, Xcat, train, y, test, rng)
                S[r, test] = out["score"]
                imps.append(out.get("coef") or out.get("gain"))
        oof[spec.id] = S
        imp[spec.id] = {c: round(float(np.mean([d[c] for d in imps])), 5) for c in imps[0]}
        log(f"cv: {spec.id} {repeats}x{k} folds in {time.time() - t0:.0f} s")
    return {"oof": oof, "importance": imp, "folds": folds}


def run_loro(t: pd.DataFrame, L: Labels, specs: list[Spec] = ZOO, log=print) -> dict:
    """Leave one guide region out: {model: {region: scores over that region's catchments}}."""
    n = len(t)
    y = L.pos.astype(int)
    trainable = L.domain & (L.pos | L.bg | L.neg_known)
    Xcat = _cats(t)
    creg = t["outlet"].map(catchment_region(t)).fillna("").to_numpy()
    regions = sorted({r for r in creg[L.pos_eval] if r})
    out = {}
    for spec in specs:
        X = matrix(t, spec.features)
        S = np.full(n, np.nan, np.float32)
        t0 = time.time()
        rng = np.random.default_rng(SEED + 7)
        for reg in regions:
            test = L.domain & (creg == reg)
            train = trainable & (creg != reg)
            S[test] = _fit_predict(spec, t, X, Xcat, train, y, test, rng)["score"]
        out[spec.id] = S
        log(f"loro: {spec.id} {len(regions)} regions in {time.time() - t0:.0f} s")
    return {"scores": out, "regions": regions, "catchment_region": creg}


# ---------------------------------------------------------------------------- metrics
def _pct_within(score: np.ndarray, ref_mask: np.ndarray, strata: np.ndarray | None, rows: np.ndarray) -> np.ndarray:
    """Percentile (0-1, ties half) of ``score[rows]`` among ``score[ref_mask]`` in the same stratum."""
    out = np.full(len(rows), np.nan)
    if strata is None:
        strata = np.zeros(len(score), np.int64)
    for s in np.unique(strata[rows]):
        sel = strata[rows] == s
        ref = np.sort(score[ref_mask & (strata == s)])
        if not len(ref):
            continue
        v = score[rows[sel]]
        lo, hi = np.searchsorted(ref, v, "left"), np.searchsorted(ref, v, "right")
        out[sel] = (lo + hi) / 2.0 / len(ref)
    return out


def strata(t: pd.DataFrame, bg: np.ndarray) -> dict[str, np.ndarray]:
    lq = np.log10(t["maf_m3s"].clip(lower=0.05).fillna(0.05).to_numpy())
    size10 = np.searchsorted(np.quantile(lq[bg], np.linspace(0, 1, 11)[1:-1]), lq)
    size5 = np.searchsorted(np.quantile(lq[bg], np.linspace(0, 1, 6)[1:-1]), lq)
    style = t["style"].cat.codes.to_numpy().astype(np.int64)
    return {"size": size10, "style": style, "style_size": style * 10 + size5}


def metrics(score: np.ndarray, t: pd.DataFrame, L: Labels, st: dict, rows_mask: np.ndarray | None = None,
            detail: bool = True) -> dict:
    """Ranking metrics of positives against background (both limited to ``rows_mask``)."""
    m = np.isfinite(score) & (rows_mask if rows_mask is not None else True)
    bg = L.bg & m
    P = np.flatnonzero(L.pos_eval & m)
    if not len(P) or not bg.any():
        return {"positives": int(len(P))}
    pct = _pct_within(score, bg, None, P)
    out = {"positives": int(len(P)), "rivers": int(len(set(L.river[P]))), "background": int(bg.sum()),
           "auc": float(np.mean(pct))}
    for k in ("style", "size", "style_size"):
        out[f"auc_{k}"] = float(np.nanmean(_pct_within(score, bg, st[k], P)))
    out["recall_top10"] = float(np.mean(pct >= 0.90))
    out["recall_top5"] = float(np.mean(pct >= 0.95))
    out["median_pct"] = float(np.median(pct) * 100)
    if L.neg_known[m].any():
        N = np.flatnonzero(L.neg_known & m)
        out["not_runnable_reaches"] = int(len(N))
        out["auc_vs_not_runnable"] = float(np.mean(_pct_within(score, L.neg_known & m, None, P)))
    if not detail:
        return out
    spct = _pct_within(score, bg, st["style"], P)
    styles = t["style"].cat.categories
    sc = st["style"]
    out["by_style"] = []
    for j, s in enumerate(styles):
        sel = sc[P] == j
        out["by_style"].append({"style": s, "positives": int(sel.sum()), "background": int((bg & (sc == j)).sum()),
                                "auc": float(spct[sel].mean()) if sel.any() else None})
    out["by_grade"] = []
    for g in ("≤3", "3-4", "4+", "?"):
        sel = L.grade[P] == g
        if sel.any():
            out["by_grade"].append({"grade": g, "positives": int(sel.sum()), "rivers": int(len(set(L.river[P][sel]))),
                                    "auc": float(pct[sel].mean()), "auc_style": float(np.nanmean(spct[sel])),
                                    "recall_top10": float(np.mean(pct[sel] >= 0.9))})
    reg = t["guide_region"].to_numpy()
    out["by_region"] = []
    for r in sorted(set(reg[P])):
        sel = reg[P] == r
        rb = bg & (reg == r)
        within = _pct_within(score, rb, None, P[sel])
        out["by_region"].append({"region": r, "positives": int(sel.sum()), "rivers": int(len(set(L.river[P][sel]))),
                                 "auc_national": float(pct[sel].mean()), "auc_within": float(np.mean(within)),
                                 "auc_style": float(np.nanmean(spct[sel])),
                                 "recall_top10": float(np.mean(pct[sel] >= 0.9))})
    # ROC (pooled), 101 points on the FPR grid
    neg_s = np.sort(score[bg])
    thr = np.quantile(neg_s, 1 - np.linspace(0, 1, 101))
    ps = score[P]
    out["roc"] = [round(float(np.mean(ps >= th)), 4) for th in thr]
    out["pct_hist"] = np.histogram(pct, bins=20, range=(0, 1))[0].tolist()
    out["style_pct_hist"] = np.histogram(spct[np.isfinite(spct)], bins=20, range=(0, 1))[0].tolist()
    return out


def _river_table(score_rows: list[np.ndarray], t: pd.DataFrame, L: Labels, st: dict) -> list[dict]:
    """Per river, OOF percentiles averaged over repeats."""
    P = np.flatnonzero(L.pos_eval & L.domain)
    pct = np.zeros(len(P))
    spct = np.zeros(len(P))
    for s in score_rows:
        pct += _pct_within(s, L.bg & np.isfinite(s), None, P)
        spct += _pct_within(s, L.bg & np.isfinite(s), st["style"], P)
    pct /= len(score_rows)
    spct /= len(score_rows)
    df = pd.DataFrame({"river": L.river[P], "pct": pct, "spct": spct, "grade": L.grade[P],
                       "region": t["guide_region"].to_numpy()[P], "style": t["style"].to_numpy()[P].astype(str)})
    out = []
    for k, g in df.groupby("river"):
        out.append({"river": k, "region": g["region"].mode().iloc[0], "grade": g["grade"].mode().iloc[0],
                    "reaches": int(len(g)), "median_pct": round(float(g["pct"].median() * 100), 1),
                    "median_style_pct": round(float(g["spct"].median() * 100), 1),
                    "best_pct": round(float(g["pct"].max() * 100), 1),
                    "share_top10": round(float((g["pct"] >= 0.9).mean()), 2),
                    "style": g["style"].mode().iloc[0]})
    return sorted(out, key=lambda r: -r["median_style_pct"])


def _round(o, nd=4):
    if isinstance(o, float):
        return round(o, nd)
    if isinstance(o, dict):
        return {k: _round(v, nd) for k, v in o.items()}
    if isinstance(o, list):
        return [_round(v, nd) for v in o]
    return o


def summarise(t: pd.DataFrame, L: Labels, cv: dict, loro: dict, specs: list[Spec] = ZOO) -> dict:
    st = strata(t, L.bg)
    res = {"created": dt.datetime.now().isoformat(timespec="minutes"), "folds": FOLDS, "repeats": REPEATS,
           "seed": SEED, "domain_km2": DOMAIN_KM2, "neg_sample_gbm": NEG_SAMPLE, "gbm_params": GBM_PARAMS,
           "bankfull_a": RT.BANKFULL_A, "spec_version": RT.load_spec()["spec_version"], "models": []}
    keys = ["auc", "auc_style", "auc_size", "auc_style_size", "recall_top10", "recall_top5", "median_pct"]
    for spec in specs:
        S = cv["oof"][spec.id]
        reps = [metrics(S[r], t, L, st, detail=False) for r in range(S.shape[0])]
        folds = []
        for r in range(S.shape[0]):
            for f in range(FOLDS):
                mf = metrics(S[r], t, L, st, rows_mask=cv["folds"][r] == f, detail=False)
                if "auc" in mf:
                    folds.append(mf)
        pooled = metrics(S[0], t, L, st)
        entry = {"id": spec.id, "kind": spec.kind, "title": spec.title, "description": spec.description,
                 "features": spec.features, "categorical": spec.categorical,
                 "cv": {k: {"mean": float(np.mean([m[k] for m in reps])), "sd": float(np.std([m[k] for m in reps])),
                            "fold_sd": float(np.std([m[k] for m in folds])) if folds else None} for k in keys},
                 "cv_detail": {k: v for k, v in pooled.items() if k not in keys},
                 "importance": cv["importance"][spec.id],
                 "rivers": _river_table([S[r] for r in range(S.shape[0])], t, L, st)}
        lo = loro["scores"][spec.id]
        creg = loro["catchment_region"]
        entry["loro"] = []
        for reg in loro["regions"]:
            m = metrics(lo, t, L, st, rows_mask=creg == reg, detail=False)
            mc = metrics(S[0], t, L, st, rows_mask=creg == reg, detail=False)   # CV, same rows
            if "auc" in m:
                entry["loro"].append({"region": reg, **{k: m[k] for k in ("positives", "rivers", "background", "auc",
                                                                           "auc_style", "recall_top10")},
                                      "cv_auc_style_same_rows": mc.get("auc_style")})
        res["models"].append(entry)
    return _round(res)


def run(log=print, specs: list[Spec] = ZOO, repeats: int = REPEATS) -> dict:
    t = ml_table(log=log)
    L = labels(t)
    log(f"labels: {int(L.pos.sum())} positive reaches ({len(set(L.river[L.pos]))} rivers), "
        f"{int(L.bg.sum())} background, {int(L.neg_known.sum())} labelled not runnable")
    cv = run_cv(t, L, specs, repeats=repeats, log=log)
    loro = run_loro(t, L, specs, log=log)
    res = summarise(t, L, cv, loro, specs)
    CV_DIR.mkdir(parents=True, exist_ok=True)
    (CV_DIR / "results.json").write_text(json.dumps(res, ensure_ascii=False, separators=(",", ":")) + "\n")
    np.savez_compressed(nz_dir() / "ml_oof.npz", **{f"cv_{k}": v for k, v in cv["oof"].items()},
                        **{f"loro_{k}": v for k, v in loro["scores"].items()})
    log(f"ml: results -> {CV_DIR / 'results.json'}")
    return res


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
