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
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import numba
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

from . import reachtype as RT
from . import xsec as XS
from .models import (
    MODELS_DIR,
    Mapped,
    _positive_rows,
    _quantiles,
    all_sections,
    grade_band,
    list_cards,
    map_sections,
    national_table,
    save_card,
    section_dict,
)
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

    Cached in ``data/nz/ml_table.pkl``. The MAF correction (``maf.py``, ``config/maf.toml`` or
    ``--maf-correction``) is applied or undone to match the current setting, on the whole-NZ network, with
    stream power and reach styles recomputed (``sync_maf``); the cache keeps what it was built with.
    """
    cache = nz_dir() / "ml_table.pkl"
    if cache.exists() and not rebuild:
        return sync_xsec(sync_maf(pd.read_pickle(cache), log=log), log=log)
    import pyogrio

    t = sync_maf(national_table(log=log).copy(), log=log, reclassify=False)
    t0 = time.time()
    down = t["down"].to_numpy().astype(np.int64)
    order = _kahn(down)
    if len(order) < len(t):
        log(f"ml: {len(t) - len(order)} reaches in cycles; they keep their own catchment")
    t["outlet"] = _outlets(down, order)
    add_geology(t)
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
    if not XS.join(t, log=log):
        log(f"ml: no {XS.national_table_path()}; the cross-section features (xs_*) are left out")
    t.to_pickle(cache)
    log(f"ml: table {len(t)} reaches -> {cache}")
    return t


def sync_xsec(t: pd.DataFrame, log=print) -> pd.DataFrame:
    """Cross-section columns of a cached table: joined when the table predates ``data/nz/xsec.pkl``, and the
    derived ones (constriction, stream power, flood hydraulics) recomputed, because they read the MAF that
    ``sync_maf`` may have changed. In place; returns ``t``."""
    if "xs_n" in t.columns:
        XS.derive(t)
    elif XS.national_table_path().exists():
        XS.join(t, log=log)
    return t


def sync_maf(t: pd.DataFrame, cfg=None, log=print, reclassify: bool = True) -> pd.DataFrame:
    """Bring a national table's MAF in line with the MAF-correction setting (``maf.load_config()`` unless
    ``cfg``): correct it (from ``maf_raw_m3s`` when it was corrected before) when ``apply`` is on, undo a
    correction when it is off. With ``reclassify``, the reach-type columns (bankfull width comes from MAF)
    are recomputed when MAF changed. In place; returns ``t``."""
    from . import maf as MAF

    cfg = cfg or MAF.load_config()
    if not cfg.apply and "maf_raw_m3s" not in t.columns:
        return t
    before = t["maf_m3s"].to_numpy(dtype=float).copy()
    if cfg.apply:
        if "specific_maf_matched" not in t.columns:
            if (nz_dir() / "tiles").exists() and "gid" in t.columns:
                t["specific_maf_matched"] = MAF.matched_from_tiles(t, nz_dir() / "tiles")
            else:
                log("maf: no specific_maf_matched and no tiles; unmatched reaches are not refilled")
        MAF.apply(t, cfg, down="down")
        log(f"maf: correction on: {MAF.summary(t)}")
    else:
        MAF.revert(t)
        log("maf: correction off: raw MAF restored")
    if reclassify and "style" in t.columns and not np.array_equal(before, t["maf_m3s"].to_numpy(dtype=float),
                                                                    equal_nan=True):
        rt = RT.classify_frame(t)
        for c in rt.columns:
            t[c] = rt[c]
    return t


GEO_COLS = ("rockclass", "rockgroup", "rock_age_ma", "karst_bed", "karst_up_frac")   # what the features read


def add_geology(t: pd.DataFrame) -> pd.DataFrame:
    """GNS geology at each reach midpoint (``x``, ``y``, EPSG:2193) and karst: ``karst_bed`` (bed on
    limestone, marble, dolomite ...) and ``karst_up_frac`` (karst share of the area draining to the
    reach, accumulated down ``down``). In place; returns ``t``."""
    import geopandas as gpd
    import pyogrio

    down = t["down"].to_numpy().astype(np.int64)
    down = np.where((down >= 0) & (down < len(t)), down, -1)   # a cut-out frame: links that leave it end there
    order = _kahn(down)
    geo = pyogrio.read_dataframe(nz_dir() / "geology.gpkg",
                                 columns=["rockclass", "rockgroup", "mainrock", "absmin_ma", "hard_rock"])
    pts = gpd.GeoDataFrame(geometry=gpd.points_from_xy(t["x"], t["y"]), crs="EPSG:2193")
    j = gpd.sjoin(pts, geo, predicate="within", how="left")
    j = j[~j.index.duplicated()].reindex(pts.index)
    for c in ("rockclass", "rockgroup", "mainrock"):
        t[c] = j[c].fillna("unknown").astype(str).str.strip().str.lower().to_numpy()
    t["rock_age_ma"] = pd.to_numeric(j["absmin_ma"], errors="coerce").to_numpy()
    t["karst_bed"] = t["mainrock"].isin(KARST_ROCKS).astype(np.int8)
    la = local_area(t.assign(down=down))
    kacc = _accumulate(down, order, la * t["karst_bed"].to_numpy())
    aacc = _accumulate(down, order, la)
    t["karst_up_frac"] = np.where(aacc > 0, kacc / np.maximum(aacc, 1e-9), 0.0).clip(0, 1)
    return t


def enrich(f: pd.DataFrame, xsec: bool = False) -> pd.DataFrame:
    """The columns the extended features need (geology, karst, reach type and confinement) for any reach
    frame with ``down``, ``x``, ``y``, ``area_km2``, ``grad_s``, ``maf_m3s`` and ``valley_width_m``:
    a national region, a 5 m reference area or the national table. A frame that has them already is
    returned unchanged. Karst share upstream only sees the frame's own network, so score national reaches
    from ``ml_table`` (whole-NZ network) rather than from a cut-out region.

    With ``xsec``, a frame without the cross-section columns gets them from the stored table (``xsec.join``:
    national reaches by ``gid``, a reference area from its ``xsec_8m.pkl``); a frame with them keeps them."""
    need_geo = any(c not in f for c in GEO_COLS)
    need_rt = "confinement_ratio" not in f or "style" not in f
    need_xs = xsec and "xs_n" not in f
    if not need_geo and not need_rt and not need_xs:
        return f
    f = f.copy()
    if need_xs and not XS.join(f):
        raise KeyError("no cross-section table for this frame: run `river-finder nz xsec` (national) or "
                       "`river-finder xsec <region>` (docs/cross-sections.md)")
    if need_geo:
        add_geology(f)
    if need_rt:
        rt = RT.classify_frame(f)
        f = pd.concat([f.drop(columns=[c for c in rt.columns if c in f]), rt.set_index(f.index)], axis=1)
    return f


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
    # cross-sections on the 8 m DEM (xsec.py, docs/cross-sections.md); a reach with none counts as wide open
    "xs_w10": (lambda g: np.log10(g["xs_w10_med"].fillna(1000.0).clip(8, 1000)),
               "Cross-section width 10 m above the channel bottom (median of sections every 50 m, 8 m DEM), log10 m",
               "xsec"),
    "xs_w20": (lambda g: np.log10(g["xs_w20_med"].fillna(1000.0).clip(8, 1000)),
               "Cross-section width 20 m above the channel bottom (median of sections), log10 m", "xsec"),
    "xs_wall": (lambda g: g["xs_gorge_lo_med"].fillna(0.0).clip(0, 300),
                "Lower valley wall: the lower side's highest ground within 150 m of the channel, m (median of "
                "sections)", "xsec"),
    "xs_h2a": (lambda g: np.log10(g["xs_gorge_lo_med"].fillna(0.0).clip(0.5, 300)
                                  / np.sqrt(g["xs_a20_med"].fillna(20000.0).clip(50, 20000))),
               "Cross-sectional height-to-area: lower wall height / sqrt(flow area at the 20 m stage), log10",
               "xsec"),
    "xs_pinch": (lambda g: np.log10((g["xs_w20_min"] / g["xs_w20_med"]).fillna(1.0).clip(0.05, 1.0)),
                 "Constriction index: narrowest section's width / the reach's median width, both at the 20 m stage, "
                 "log10 (0 = no pinch point)", "xsec"),
    "xs_omega10": (lambda g: np.log10(g["xs_omega10_wm2"].fillna(0.1).clip(0.1, 1e5)),
                   "Unit stream power at the mean annual flood over the 10 m-stage width, rho g Q S / w10, log10 W/m2",
                   "xsec"),
    "xs_vel": (lambda g: np.log10(g["xs_v_q"].fillna(0.1).clip(0.05, 20)),
               "Mean velocity at the mean annual flood, Q / A on the median section (Manning stage, n = 0.045; mostly "
               "below what the 8 m DEM resolves), log10 m/s", "xsec"),
    "xs_omega_q": (lambda g: np.log10(g["xs_omega_q"].fillna(0.1).clip(0.1, 1e5)),
                   "Unit stream power at the mean annual flood over the Manning flood width, log10 W/m2", "xsec"),
    "x_km": (lambda g: g["x"] / 1000.0, "NZTM easting, km", "xy"),
    "y_km": (lambda g: g["y"] / 1000.0, "NZTM northing, km", "xy"),
}
CATEGORICAL = {"rockclass": "GNS rock class at the reach midpoint", "rockgroup": "GNS rock group at the reach midpoint"}
BASE = list(FEATURES)
TERRAIN = ["valley_w", "confine", "karst_bed", "karst_up", "rock_age"]
OSM = ["falls", "falls_h", "rapids"]
XSEC = ["xs_w10", "xs_w20", "xs_wall", "xs_h2a", "xs_pinch", "xs_omega10"]   # what the 8 m DEM resolves
XSEC_HYD = ["xs_vel", "xs_omega_q"]    # Manning flood hydraulics; in the registry, not in a recipe (see the doc)
XSEC_ALL = XSEC + XSEC_HYD


def available(t: pd.DataFrame, spec) -> bool:
    """Whether ``t`` has what ``spec``'s features read (cross-section features need the ``xs_*`` columns)."""
    return not any(f in XSEC_ALL for f in spec.features) or "xs_n" in t.columns


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
    Spec("gbm_xsec", "gbm", "LightGBM, terrain + geology + cross-sections", BASE + TERRAIN + XSEC,
         ["rockclass", "rockgroup"],
         description="gbm_terrain + 8 m DEM cross-sections: width at 10 and 20 m stages, lower wall height, "
                     "cross-sectional height-to-area, constriction (pinch points) and unit stream power over the "
                     "10 m-stage width."),
    Spec("logit_xsec", "logistic", "Logistic, all features + cross-sections", BASE + TERRAIN + OSM + ROCK_ONEHOT + XSEC,
         description="logit_plus + the gbm_xsec cross-section features. A linear model cannot form width-to-flood "
                     "or wall-to-width ratios itself, so explicit section features help it more than LightGBM."),
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
    b = _gbm_fit(spec, X, Xcat, train, y, rng)
    s = b.predict(_gbm_frame(spec, X, Xcat, np.flatnonzero(score)), raw_score=True)
    return {"score": s, "gain": _gain(b, spec)}


def _gbm_frame(spec: Spec, X: np.ndarray, Xcat: pd.DataFrame | None, rows: np.ndarray) -> pd.DataFrame:
    f = pd.DataFrame(X[rows], columns=spec.features)
    for c in spec.categorical:
        f[c] = pd.Categorical(Xcat[c].iloc[rows].to_numpy(), categories=Xcat[c].cat.categories)
    return f[spec.features + spec.categorical]


def _gbm_fit(spec: Spec, X: np.ndarray, Xcat: pd.DataFrame | None, train: np.ndarray, y: np.ndarray, rng,
             **extra):
    """LightGBM on the ``train`` rows: every positive and ``NEG_SAMPLE`` of the rest, weighted back up."""
    import lightgbm as lgb

    tr = np.flatnonzero(train)
    keep = (y[tr] == 1) | (rng.random(len(tr)) < NEG_SAMPLE)
    tr = tr[keep]
    w = np.where(y[tr] == 1, 1.0, 1.0 / NEG_SAMPLE)
    params = {k: v for k, v in GBM_PARAMS.items() if k != "n_estimators"}
    params["seed"] = int(rng.integers(1 << 30))
    params.update(extra)
    ds = lgb.Dataset(_gbm_frame(spec, X, Xcat, tr), y[tr], weight=w, categorical_feature=spec.categorical or "auto",
                     free_raw_data=True)
    return lgb.train(params, ds, num_boost_round=GBM_PARAMS["n_estimators"])


def _gain(b, spec: Spec) -> dict:
    gain = b.feature_importance(importance_type="gain")
    return dict(zip(spec.features + spec.categorical, (gain / max(gain.sum(), 1e-9)).round(5).tolist(), strict=True))


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
    skip = [s.id for s in specs if not available(t, s)]
    if skip:
        log(f"cv: {', '.join(skip)} left out: no cross-section columns (run `river-finder nz xsec`, then `ml table`)")
        specs = [s for s in specs if s.id not in skip]
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


# ---------------------------------------------------------------------------- registry cards (kind "gbm")
def spec_by_id(sid: str) -> Spec:
    return next(s for s in ZOO if s.id == sid)


def train_card(t: pd.DataFrame, mapped: list[Mapped], split: dict, mid: str, title: str, description: str,
               spec_id: str = "gbm_terrain", order: int | None = None) -> tuple[dict, str]:
    """A frozen registry card of kind "gbm", trained like ``models.train`` (variant "all") but with the
    CV recipe of ``spec_id``: positives are the runnable sections of the split's training rivers,
    every reach of a test river is left out, other known reaches are left out, and every other domain
    reach (>= 2 km2) is background. Returns the card and the LightGBM model text.

    ``t`` is the national table with the ML columns (``ml_table``); ``mapped`` its section mapping."""
    spec = spec_by_id(spec_id)
    t = enrich(t, xsec=any(f in XSEC_ALL for f in spec.features))
    n = len(t)
    domain = (t["area_km2"] >= DOMAIN_KM2).to_numpy()
    train_keys, test_keys = set(split["train"]), set(split["test"])
    known, neg, y = np.zeros(n, bool), np.zeros(n, bool), np.zeros(n, np.int64)
    used = []
    for m in mapped:
        if not len(m.rows):
            continue
        known[m.rows] = True
        if m.key in test_keys:
            continue
        if m.section.status != "runnable":
            neg[m.rows] = True
            used.append(m)
        elif m.key in train_keys and not m.section.held_out:
            rows = _positive_rows(m, t)
            y[rows[domain[rows]]] = 1
            used.append(m)
    neg &= domain & (y == 0)
    trainable = domain & ((y == 1) | neg | ~known)
    X = matrix(t, spec.features)
    Xcat = _cats(t)
    t0 = time.time()
    b = _gbm_fit(spec, X, Xcat, trainable, y, np.random.default_rng(SEED), deterministic=True,
                 force_row_wise=True)
    text = b.model_to_string()
    order = order or (max((c.get("order", 0) for c in list_cards()), default=0) + 1)
    card = {
        "id": mid, "order": order, "kind": "gbm", "title": title, "description": description,
        "created": dt.date.today().isoformat(), "features": spec.features, "categorical": spec.categorical,
        "categories": {c: [str(v) for v in Xcat[c].cat.categories] for c in spec.categorical},
        "recipe": spec.id, "gbm_params": {**GBM_PARAMS, "deterministic": True, "force_row_wise": True},
        "neg_sample": NEG_SAMPLE, "seed": SEED,
        "booster_file": f"{mid}.lgb.txt", "booster_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "importance": _gain(b, spec), "coef": {}, "intercept": 0.0, "mean": {}, "std": {},
        "labels": {"source": "labels/known_runs.toml + labels/guide_research.json", "variant": "all",
                   "split": split["name"], "positives": int(y[trainable].sum()),
                   "training_reaches": int(trainable.sum()),
                   "train_sections": [section_dict(m.section) for m in used], "dropped_sections": [],
                   "test_sections": [section_dict(m.section) for m in mapped if m.key in test_keys],
                   "train_rivers": sorted({m.key for m in used if m.section.status == "runnable"}),
                   "test_rivers": sorted(test_keys)},
        "train_seconds": round(time.time() - t0, 1),
    }
    _BOOSTERS[card["booster_sha256"]] = b
    card["pool_quantiles"] = _quantiles(gbm_logits(card, t), t)
    return card, text


_BOOSTERS: dict = {}


def booster(card: dict):
    """The card's LightGBM model (``models/<booster_file>``), checked against ``booster_sha256``."""
    key = card["booster_sha256"]
    if key not in _BOOSTERS:
        import lightgbm as lgb

        text = (MODELS_DIR / card["booster_file"]).read_text()
        if hashlib.sha256(text.encode()).hexdigest() != key:
            raise ValueError(f"models/{card['booster_file']} does not match card {card['id']}")
        _BOOSTERS[key] = lgb.Booster(model_str=text)
    return _BOOSTERS[key]


def gbm_logits(card: dict, f: pd.DataFrame) -> np.ndarray:
    """Raw score (log-odds) of a "gbm" card for every reach of ``f`` (any reach frame; see ``enrich``)."""
    f = enrich(f, xsec=any(n in XSEC_ALL for n in card["features"]))
    X = matrix(f, card["features"])
    df = pd.DataFrame(X, columns=card["features"])
    for c in card["categorical"]:
        cats = card["categories"][c]
        v = f[c].astype(str).to_numpy()
        df[c] = pd.Categorical(np.where(np.isin(v, cats), v, "other"), categories=cats)
    return booster(card).predict(df[card["features"] + card["categorical"]], raw_score=True)


def save_gbm(card: dict, text: str):
    """Write the booster next to the card, then the card (both frozen: an id is never reused)."""
    p = MODELS_DIR / card["booster_file"]
    if p.exists() and hashlib.sha256(p.read_text().encode()).hexdigest() != card["booster_sha256"]:
        raise FileExistsError(f"{p} is a frozen checkpoint with a different model; pick a new model id.")
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return save_card(card)


# ---------------------------------------------------------------------------- audits
AUDIT_DIR = MODELS_DIR / "audits"


def candidate_runs(card: dict, t: pd.DataFrame | None = None, min_pct: float = 99.0, min_len_km: float = 1.0,
                   max_gap: int = 1, hydro_exclude: tuple[str, ...] | None = None) -> pd.DataFrame:
    """Runs that a card ranks in the top ``100 - min_pct`` % of *unlisted* reaches (domain reaches that no
    known section covers). A trained model puts its own training rivers at the very top nationally,
    so the cut is taken among unlisted reaches only.

    A run is a chain of such reaches followed down the main stem, with at most ``max_gap`` reaches
    below the cut inside it, at least ``min_len_km`` long. One row per run, best first (median
    percentile, then length). ``oof_pct`` is the same percentile from the out-of-fold CV scores of the
    card's recipe (mean over repeats, ``data/nz/ml_oof.npz``), a check that the run is not an
    in-sample or single-fit artefact. For hand audits, the guide status of the river name, terrain,
    reach style, OSM waterfalls and road distance are listed for the reader; access never changes the
    order (it is the paddler's call).

    Hydro-affected reaches (``hydro.py``, ``config/hydro.toml``): reaches of the ``hydro_exclude`` classes (default
    ``[audit] exclude``: diverted and reservoir, where the natural flood overstates the flow) never start, join or
    bridge a run; they stay in the percentile reference, so the other reaches keep their percentiles. ``hydro``,
    ``hydro_scheme`` and ``hydro_frac`` label what is left (mostly regulated reaches below storage dams); the
    reaches and rivers left out are in ``out.attrs["hydro_excluded"]``."""
    from . import hydro as H
    from .models import all_sections, guide_mask, logits, norm_river, percentiles

    t = t if t is not None else ml_table()
    lg = logits(card, t)
    nat = percentiles(card, lg)
    domain = (t["area_km2"] >= DOMAIN_KM2).to_numpy()
    secs = all_sections()
    gm = guide_mask(t, secs)
    unl = domain & ~gm

    def within(score):
        ref = np.sort(score[unl])
        return 100.0 * np.searchsorted(ref, score, side="right") / len(ref)

    pct = within(lg)
    oof = None
    op = nz_dir() / "ml_oof.npz"
    if op.exists() and card.get("recipe"):
        with np.load(op) as z:
            k = f"cv_{card['recipe']}"
            if k in z.files and z[k].shape[1] == len(t):
                oof = within(np.nan_to_num(z[k].mean(axis=0), nan=-1e9))
    hy = H.columns(t)
    ex_cls = H.load_config().audit_exclude if hydro_exclude is None else tuple(hydro_exclude)
    excl = hy["hydro"].isin(ex_cls).to_numpy() if ex_cls else np.zeros(len(t), dtype=bool)
    dropped = unl & (pct >= min_pct) & excl
    hot = unl & (pct >= min_pct) & ~excl
    # a river is "in the guides" if a guide names it or a guide section lies on it (REC2 spellings differ:
    # Manganui-a-te-ao / Manganui-o-te-ao, Routeburn / Route Burn)
    listed = ({norm_river(s.river) for s in secs if s.status == "runnable"}
              | {norm_river(r) for r in t.loc[gm, "river"].dropna().unique()}) - {""}
    down, up = t["down"].to_numpy(), t["up"].to_numpy()
    length = t["length_m"].to_numpy(dtype=float)
    seen = np.zeros(len(t), bool)
    road = None
    xp = nz_dir() / "external.pkl"
    if xp.exists():
        x = pd.read_pickle(xp)
        if len(x) == len(t) and (x["seg_id"].to_numpy() == t["seg_id"].to_numpy()).all():
            road = x["d_road_m"].to_numpy(dtype=float)
    rows = []
    for s in np.flatnonzero(hot):
        if seen[s] or (up[s] >= 0 and hot[up[s]]):
            continue
        members, gap, cur = [], [], s
        while cur >= 0 and not seen[cur] and not excl[cur]:
            if hot[cur]:
                members += gap + [cur]
                gap = []
            else:
                gap.append(cur)
                if len(gap) > max_gap:
                    break
            cur = down[cur]
        if not members:
            continue
        m = np.asarray(members)
        seen[m] = True
        km = length[m].sum() / 1000.0
        if km < min_len_km:
            continue
        sub = t.iloc[m]
        names = sub["river"].fillna("")
        name = names[names != ""].mode().iloc[0] if (names != "").any() else ""
        w = length[m]
        rows.append({
            "river": name, "region": sub["region"].iloc[-1], "length_km": round(km, 2), "reaches": len(m),
            "median_pct": round(float(np.median(pct[m])), 2), "min_pct": round(float(pct[m].min()), 2),
            "oof_pct": round(float(np.median(oof[m])), 2) if oof is not None else None,
            "national_pct": round(float(np.median(nat[m])), 2),
            "grad_m_per_km": round(float(np.average(sub["grad_s"], weights=w)), 1),
            "steep50_max": round(float(sub["steepest_50m_m_per_km"].max()), 0),
            "drop_m": round(float((sub["grad_s"] * w).sum() / 1000.0), 0),
            "area_top_km2": round(float(sub["area_km2"].iloc[0]), 1),
            "area_bottom_km2": round(float(sub["area_km2"].iloc[-1]), 1),
            "maf_bottom_m3s": round(float(sub["maf_m3s"].iloc[-1]), 1),
            "gorge_m": round(float(sub["gorge_depth_m"].mean()), 0),
            "valley_w_m": round(float(sub["valley_width_m"].median()), 0),
            "style": str(sub["style"].astype(str).mode().iloc[0]) if "style" in sub else "",
            "rockgroup": str(sub["rockgroup"].mode().iloc[0]) if "rockgroup" in sub else "",
            "falls_n": int(np.nansum(sub["falls_n"])) if "falls_n" in sub else 0,
            "falls_max_h_m": float(np.nanmax(sub["falls_max_h_m"])) if "falls_max_h_m" in sub and
            np.isfinite(sub["falls_max_h_m"]).any() else None,
            "road_min_m": round(float(np.nanmin(road[m])), 0) if road is not None else None,
            "river_in_guides": bool(norm_river(name) in listed) if name else False,
            **dict(zip(("hydro", "hydro_scheme", "hydro_frac"), H.run_class(hy, m, length), strict=True)),
            "lat_top": round(float(sub["lat"].iloc[0]), 5), "lon_top": round(float(sub["lon"].iloc[0]), 5),
            "lat_bottom": round(float(sub["lat"].iloc[-1]), 5), "lon_bottom": round(float(sub["lon"].iloc[-1]), 5),
            "top_seg_id": int(sub["seg_id"].iloc[0]),
        })
    out = pd.DataFrame(rows)
    if len(out):
        out = out.sort_values(["median_pct", "length_km"], ascending=False).reset_index(drop=True)
        out.insert(0, "rank", np.arange(1, len(out) + 1))
    names = t["river"].fillna("").to_numpy()[dropped]
    out.attrs["hydro_excluded"] = {
        "classes": list(ex_cls), "reaches": int(dropped.sum()),
        "km": round(float(length[dropped].sum() / 1000.0), 1),
        "by_scheme": {k: int(v) for k, v in pd.Series(hy["hydro_scheme"].to_numpy()[dropped]).value_counts().items()},
        "rivers": sorted({str(r) for r in names if r})}
    return out


def write_audit(model_id: str, n: int = 60, log=print) -> Path:
    """``models/audits/<model>_top_runs.csv``: the best run of each of the ``n`` top unlisted rivers (unnamed
    reaches count one river per run), from ``candidate_runs``."""
    from .models import load_card

    runs = candidate_runs(load_card(model_id))
    key = np.where(runs["river"] != "", runs["river"] + " | " + runs["region"], "#" + runs["rank"].astype(str))
    best = runs[~pd.Series(key).duplicated().to_numpy()].head(n)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    p = AUDIT_DIR / f"{model_id}_top_runs.csv"
    best.to_csv(p, index=False)
    log(f"audit: {len(runs)} runs in the top 1% of unlisted reaches; best of {len(best)} rivers -> {p}")
    ex = runs.attrs.get("hydro_excluded") or {}
    if ex.get("reaches"):
        log(f"audit: hydro ({', '.join(ex['classes'])}) left out {ex['reaches']} top-1% reaches ({ex['km']} km): "
            f"{', '.join(f'{k} {v}' for k, v in ex['by_scheme'].items())}; rivers: {', '.join(ex['rivers']) or '-'}")
    return p
