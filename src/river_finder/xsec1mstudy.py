"""Which 1 m cross-section features add information (rf-lxa.4, ``docs/cross-sections-1m.md``).

The protocol of rf-zmy.10 (``docs/cross-sections.md``) on the LiDAR-covered domain reaches of its 14 sample tiles:

- ``check``: on the rf-lxa.2 benchmark sub-area (Hokitika / Toaroha / Kokatahi), the re-centred sections of the
  national 8 m reaches against the 1 m-routed channel (``data/analysis/lidar-bench/variants/s1``): how far the
  channel points are from it, and how well per-reach features from them agree with features from sections cast
  along the 1 m lines themselves (what the 2 m lines of the rebuild will give).
- ``study``: per feature, AUC alone (pooled, within style, within style x size), Spearman with the existing terrain
  features and the 8 m ``xs_*`` features, the variance a linear fit on the ``gbm_terrain`` inputs explains and the
  residual's AUC, then grouped repeated 5-fold CV (``ml.run_cv``, same folds for every model: paired gains) and
  leave-one-region-out for LightGBM and the logistic model, with and without the 1 m features (each group alone, the
  keep list ``VALLEY`` + ``KEEP_LOW``, all of them).
- ``select``: backward elimination by group on top of the terrain features and ``VALLEY`` (``PRUNED`` groups, then
  ``ROUND2``); ``spacing``: the keep list with sections every 50 m against every 25 m; ``storage``: bytes per section.

The 1 m features are registered in ``ml.EXTRA`` only while a CV runs (``registered``); no production recipe changes.
Outputs in ``data/analysis/lidar-xsec/``: ``check_s1.json``, ``study.json``, ``select_*.json``, ``spacing.json``,
``storage.json`` and ``tables.md``.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pandas as pd

from . import ml as ML
from . import xsec as XS
from . import xsec1m as X

SAMPLE_TILES = ("x02y04 x02y05 x03y05 x04y06 x05y07 x06y08 x07y09 x07y10 x07y11 x11y11 x11y14 x12y16 x12y17 "
                "x09y20").split()
SUB = (1_437_000, 5_229_000, 1_457_000, 5_250_000)


def _log10(s, lo, hi, fill):
    return np.log10(s.fillna(fill).clip(lo, hi))


# name -> (builder, description, group); reaches without valid sections are not in the sample, so fills are rare
FEATURES1M: dict[str, tuple] = {
    "x1_vel": (lambda g: _log10(g["x1_v_q"], 0.05, 20, 0.1),
               "Mean velocity at the mean annual flood, Q / A on the median 1 m section (Manning stage from the water "
               "surface, area above it only), log10 m/s", "hyd"),
    "x1_omega_q": (lambda g: _log10(g["x1_omega_q"], 0.1, 1e5, 0.1),
                   "Unit stream power at the mean annual flood over the Manning flood width (1 m), log10 W/m2", "hyd"),
    "x1_vel_est": (lambda g: _log10(g["x1_v_q_est"], 0.05, 20, 0.1),
                   "As x1_vel with an estimated under-water rectangle (capture flow 5 % of MAF), log10 m/s", "hyd_est"),
    "x1_omega_q_est": (lambda g: _log10(g["x1_omega_q_est"], 0.1, 1e5, 0.1),
                       "As x1_omega_q with the under-water estimate, log10 W/m2", "hyd_est"),
    "x1_b": (lambda g: g["x1_b_med"].fillna(0.5).clip(-0.5, 3), "Shape exponent b of w ~ h^b over 0.2-2 m (1 V, "
             "0.5 parabola, 0 box), median of sections", "shape"),
    "x1_ff1": (lambda g: g["x1_ff1_med"].fillna(0.7).clip(0, 1.2), "Form factor A / (w h) at the 1 m stage (0.5 V, "
               "0.67 parabola, 1 box)", "shape"),
    "x1_ffbf": (lambda g: g["x1_ffbf_med"].fillna(0.7).clip(0, 1.2), "Form factor at the bankfull stage", "shape"),
    "x1_wd": (lambda g: _log10(g["x1_wd_bf_med"], 1, 1000, 30), "Bankfull width / bankfull stage, log10", "wd"),
    "x1_rbf": (lambda g: _log10(g["x1_rbf_med"], 0.01, 20, 0.5), "Hydraulic radius A / P at bankfull, log10 m", "wd"),
    "x1_w1": (lambda g: _log10(g["x1_w1_med"], 0.5, 1000, 30), "Width 1 m above the water surface, log10 m", "wd"),
    "x1_w0": (lambda g: _log10(g["x1_w0_med"], 0.1, 1000, 10), "Width 0.1 m above the water surface (the water "
              "surface at capture), log10 m", "wd"),
    "x1_hb": (lambda g: g["x1_hb_use_med"].fillna(5).clip(0.3, 5), "Bankfull stage above the water surface (width "
              "break, Wolman; 5 m where none), m", "bankfull"),
    "x1_wbf": (lambda g: _log10(g["x1_wbf_med"], 0.5, 1000, 30), "Bankfull width, log10 m", "bankfull"),
    "x1_brk": (lambda g: g["x1_brk_frac"].fillna(0.5), "Share of sections with a bankfull width break below 4.5 m",
               "bankfull"),
    "x1_er": (lambda g: _log10(g["x1_er_med"], 1, 200, 2), "Entrenchment ratio: width at twice the bankfull stage "
              "/ bankfull width, log10", "entrench"),
    "x1_bank": (lambda g: g["x1_ang_med"].fillna(20).clip(0, 90), "Bank angle from the water edge to bankfull, mean "
                "of both sides, deg", "banks"),
    "x1_bank_hi": (lambda g: g["x1_ang_hi_med"].fillna(20).clip(0, 90), "Steeper bank's angle, deg", "banks"),
    "x1_steep": (lambda g: g["x1_steep_med"].fillna(0).clip(0, 1), "Share of the bank rise (0.25-5 m) in 1 m steps "
                 "steeper than 60 deg (bedrock walls)", "banks"),
    "x1_asym_ang": (lambda g: g["x1_asym_ang_med"].fillna(0).clip(0, 90), "|left - right| bank angle, deg", "asym"),
    "x1_asym_w": (lambda g: g["x1_asym_w_med"].fillna(0).clip(0, 1), "|left - right| / width at the 2 m stage",
                  "asym"),
    "x1_w1_cv": (lambda g: g["x1_w1_cv"].fillna(0.3).clip(0, 3), "Coefficient of variation of the 1 m-stage width "
                 "along the reach", "var"),
    "x1_hb_cv": (lambda g: g["x1_hb_cv"].fillna(0.3).clip(0, 3), "Coefficient of variation of the bankfull stage",
                 "var"),
    "x1_pinchf": (lambda g: g["x1_pinch_frac"].fillna(0).clip(0, 1), "Share of sections narrower than half the reach "
                  "median width at 1 m (pinch points)", "var"),
    "x1_pinch": (lambda g: np.log10((g["x1_w1_min"] / g["x1_w1_med"]).fillna(1).clip(0.05, 1)),
                 "Constriction index at 1 m: narrowest / median width, log10", "var"),
    "x1_rough": (lambda g: _log10(g["x1_rough"], 0.01, 20, 0.3), "Bed roughness: robust spread of the linear-"
                 "detrended 1 m long profile, log10 m", "rough"),
    "x1_dropconc": (lambda g: g["x1_dropconc"].fillna(0.3).clip(0, 1), "Share of the reach drop in its steepest 10 % "
                    "of length (1 m long profile)", "rough"),
    "x1_w10": (lambda g: _log10(g["x1_w10_med"], 2, 1000, 1000), "Width at the 10 m stage (2 m DEM beyond 150 m), "
               "log10 m", "valley"),
    "x1_w20": (lambda g: _log10(g["x1_w20_med"], 2, 1000, 1000), "Width at the 20 m stage, log10 m", "valley"),
    "x1_wall": (lambda g: g["x1_wall_lo_med"].fillna(0).clip(0, 300), "Lower side's highest ground within 150 m, m",
                "valley"),
    "x1_vf": (lambda g: _log10(g["x1_vf_med"], 0.001, 100, 1), "Valley floor width-to-height: 2 w(2 m) / (left + "
              "right highest ground within 500 m), log10", "valley"),
}
GROUPS: dict[str, list[str]] = {}
for _k, _v in FEATURES1M.items():
    GROUPS.setdefault(_v[2], []).append(_k)
REF = ["gradient", "maf", "power", "gorge", "h_to_w", "valley_w", "confine", "xs_w10", "xs_wall", "xs_h2a",
       "xs_pinch", "xs_vel", "xs_omega_q"]
TERRAIN_NUM = ML.BASE + ML.TERRAIN


@contextmanager
def registered():
    """The 1 m features in ``ml.EXTRA`` for the duration (so ``ml.run_cv`` can build them); removed afterwards."""
    added = [k for k in FEATURES1M if k not in ML.EXTRA]
    ML.EXTRA.update({k: FEATURES1M[k] for k in added})
    try:
        yield
    finally:
        for k in added:
            ML.EXTRA.pop(k, None)


def col(t: pd.DataFrame, name: str) -> np.ndarray:
    if name in FEATURES1M:
        return np.asarray(FEATURES1M[name][0](t), dtype=float)
    return ML.column(t, name)


# ---------------------------------------------------------------------------------------------- table
def attach(t: pd.DataFrame, table: pd.DataFrame) -> pd.DataFrame:
    """``t`` with the ``x1_*`` columns of the 1 m reach table joined on ``gid`` and the hydraulics derived."""
    cols = [c for c in table.columns if c.startswith("x1_")]
    j = table.drop_duplicates("gid").set_index("gid")[cols].reindex(t["gid"].to_numpy())
    t = pd.concat([t.drop(columns=[c for c in cols if c in t]), pd.DataFrame(
        {c: j[c].to_numpy() for c in cols}, index=t.index)], axis=1)
    t["x1_n"] = t["x1_n"].fillna(0).astype(np.int16)
    X.derive(t)
    return t


def tile_of(gid: np.ndarray) -> np.ndarray:
    k = np.asarray(gid) // 1_000_000
    return np.array([f"x{a % 100:02d}y{a // 100:02d}" for a in k])


def sample_frame(reach_path: Path | None = None, every: int = 1, log=print):
    """The ML table restricted to the sample (14 tiles, domain, 1 m sections), with the 8 m sections and the 1 m
    features joined, and its labels. ``every`` = 2 rebuilds the 1 m reach table from every other section."""
    od = X.out_dir()
    t = ML.ml_table(log=log)
    xs8 = Path(X.out_dir()).parent / "xsec" / "nz.pkl"
    XS.join(t, xs8, log=log)
    if every == 1:
        r1 = pd.read_pickle(reach_path or od / "reach1m.pkl")
    else:
        r1 = rebuild_reaches(every)
    t = attach(t, r1)
    L = ML.labels(t)
    t["tile"] = tile_of(t["gid"].to_numpy())
    keep = t["tile"].isin(SAMPLE_TILES).to_numpy() & L.domain & (t["x1_n"].to_numpy() >= 1)
    idx = np.flatnonzero(keep)
    s = t.iloc[idx].reset_index(drop=True)
    Ls = ML.Labels(L.domain[idx], L.pos[idx], L.pos_eval[idx], L.bg[idx], L.neg_known[idx], L.river[idx],
                   L.grade[idx], [])
    in_tiles = t["tile"].isin(SAMPLE_TILES).to_numpy() & L.domain
    cov = {"domain_reaches_in_tiles": int(in_tiles.sum()), "positives_in_tiles": int((in_tiles & L.pos_eval).sum()),
           "sample_reaches": int(len(s)), "sample_positives": int(Ls.pos_eval.sum()),
           "sample_rivers": int(len(set(Ls.river[Ls.pos_eval]))), "national_positives": int(L.pos_eval.sum()),
           "per_tile": {k: {"domain": int((in_tiles & (t["tile"].to_numpy() == k)).sum()),
                            "with_1m": int((keep & (t["tile"].to_numpy() == k)).sum()),
                            "positives_1m": int((keep & L.pos_eval & (t["tile"].to_numpy() == k)).sum())}
                        for k in SAMPLE_TILES}}
    return s, Ls, cov


def rebuild_reaches(every: int) -> pd.DataFrame:
    """The 1 m reach table rebuilt from the stored per-section tables keeping every ``every``-th section."""
    od = X.out_dir() / "tiles"
    parts = []
    for p in sorted(od.glob("*_sec.parquet")):
        sec = pd.read_parquet(p)
        r = pd.read_pickle(p.with_name(p.name.replace("_sec.parquet", "_reach.pkl")))
        rt = X.reach_table(sec, len(r), every=every)
        for c in ("x1_rough", "x1_dropconc", "x1_grad1m", "x1_prof_len"):
            rt[c] = r[c].to_numpy()
        rt.insert(0, "gid", r["gid"].to_numpy())
        parts.append(rt)
    return pd.concat(parts, ignore_index=True)


# ---------------------------------------------------------------------------------------------- single features
def separation(t: pd.DataFrame, L: ML.Labels, names: list[str]) -> pd.DataFrame:
    st = ML.strata(t, L.bg)
    rows = []
    for n in names:
        m = ML.metrics(col(t, n), t, L, st, detail=False)
        rows.append({"feature": n, "auc": m["auc"], "auc_style": m["auc_style"], "auc_style_size": m["auc_style_size"]})
    return pd.DataFrame(rows).set_index("feature")


def spearman(t: pd.DataFrame, names: list[str], refs: list[str]) -> pd.DataFrame:
    A = pd.DataFrame({n: col(t, n) for n in names + refs})
    R = A.rank()
    c = R.corr()
    return c.loc[names, refs]


def residual(t: pd.DataFrame, L: ML.Labels, names: list[str], base: list[str]) -> pd.DataFrame:
    """Linear fit of each feature on the ``base`` numeric inputs (domain): R2, and the residual's AUC (pooled and
    within style)."""
    st = ML.strata(t, L.bg)
    B = np.column_stack([np.ones(len(t))] + [col(t, b) for b in base])
    B = np.nan_to_num(B)
    rows = []
    for n in names:
        y = np.nan_to_num(col(t, n))
        coef = np.linalg.lstsq(B, y, rcond=None)[0]
        r = y - B @ coef
        r2 = 1 - r.var() / max(y.var(), 1e-12)
        m = ML.metrics(r, t, L, st, detail=False)
        rows.append({"feature": n, "r2": r2, "resid_auc": m["auc"], "resid_auc_style": m["auc_style"],
                     "resid_auc_style_size": m["auc_style_size"]})
    return pd.DataFrame(rows).set_index("feature")


# ---------------------------------------------------------------------------------------------- CV
def spec(sid: str, kind: str, features: list[str], cat: bool = True) -> ML.Spec:
    return ML.Spec(sid, kind, sid, features, ["rockclass", "rockgroup"] if kind == "gbm" and cat else [])


def cv_table(t: pd.DataFrame, L: ML.Labels, specs: list[ML.Spec], pairs: dict[str, str], repeats: int = 10,
             log=print) -> tuple[pd.DataFrame, dict, dict]:
    """``ml.run_cv`` (same folds for every model) -> mean, sd and the paired gain against the model in ``pairs``
    (mean, sd, repeats better) for every metric; the importances; the per-repeat metrics (for ``paired``)."""
    with registered():
        cv = ML.run_cv(t, L, specs, repeats=repeats, log=log)
    st = ML.strata(t, L.bg)
    keys = ["auc", "auc_style", "auc_size", "auc_style_size", "recall_top10", "recall_top5"]
    per = {}
    for s in specs:
        S = cv["oof"][s.id]
        per[s.id] = {k: np.array([ML.metrics(S[r], t, L, st, detail=False)[k] for r in range(repeats)]) for k in keys}
    rows = []
    for s in specs:
        row = {"model": s.id}
        for k in keys:
            row[k] = per[s.id][k].mean()
            row[f"{k}_sd"] = per[s.id][k].std()
            b = pairs.get(s.id)
            if b:
                d = per[s.id][k] - per[b][k]
                row[f"d_{k}"] = d.mean()
                row[f"d_{k}_sd"] = d.std()
                row[f"d_{k}_n"] = int((d > 0).sum())
                row["vs"] = b
        rows.append(row)
    return pd.DataFrame(rows).set_index("model"), {s.id: cv["importance"][s.id] for s in specs}, per


def paired(per: dict, comparisons: list[tuple[str, str]], keys=("auc", "auc_style", "auc_style_size",
                                                                 "recall_top10")) -> pd.DataFrame:
    """Paired gains (model a - model b on the same folds and seeds): mean, sd and repeats where a is better."""
    rows = []
    for a, b in comparisons:
        row = {"model": a, "vs": b}
        for k in keys:
            d = per[a][k] - per[b][k]
            row[f"d_{k}"] = d.mean()
            row[f"d_{k}_sd"] = d.std()
            row[f"d_{k}_n"] = int((d > 0).sum())
        rows.append(row)
    return pd.DataFrame(rows).set_index("model")


def loro_table(t: pd.DataFrame, L: ML.Labels, specs: list[ML.Spec], log=print) -> pd.DataFrame:
    with registered():
        lo = ML.run_loro(t, L, specs, log=log)
    st = ML.strata(t, L.bg)
    rows = []
    for reg in lo["regions"]:
        mask = lo["catchment_region"] == reg
        row = {"region": reg, "positives": int((L.pos_eval & mask).sum())}
        for s in specs:
            m = ML.metrics(lo["scores"][s.id], t, L, st, rows_mask=mask, detail=False)
            row[s.id] = m.get("auc_style", np.nan)
            row[f"{s.id}_ss"] = m.get("auc_style_size", np.nan)
        rows.append(row)
    return pd.DataFrame(rows).set_index("region")


# ---------------------------------------------------------------------------------------------- resolution, spacing
def _partial_rank(u: np.ndarray, v: np.ndarray, Z: np.ndarray) -> float:
    """Spearman of ``u`` and ``v`` after removing a linear fit on ``Z`` from both ranks."""
    ok = np.isfinite(u) & np.isfinite(v) & np.isfinite(Z).all(axis=1)
    ru, rv = pd.Series(u[ok]).rank().to_numpy(), pd.Series(v[ok]).rank().to_numpy()
    B = np.column_stack([np.ones(ok.sum()), Z[ok]])
    eu = ru - B @ np.linalg.lstsq(B, ru, rcond=None)[0]
    ev = rv - B @ np.linalg.lstsq(B, rv, rcond=None)[0]
    return float(np.corrcoef(eu, ev)[0, 1])


def resolution(t: pd.DataFrame) -> dict:
    """1 m against the 8 m sections on the same reaches: rank agreement (raw and after log MAF and log gradient),
    median ratio, the Manning flood stage on both, how much of the 1 m velocity the flood and the gradient explain,
    and the water at the channel points."""
    Z = np.column_stack([np.log10(t["maf_m3s"].clip(0.05)), np.log10(t["grad_s"].clip(0.1))])
    pairs = {"w10": ("x1_w10_med", "xs_w10_med"), "w20": ("x1_w20_med", "xs_w20_med"),
             "w5": ("x1_w5_med", "xs_w5_med"), "w2": ("x1_w2_med", "xs_w2_med"), "w1": ("x1_w1_med", "xs_w1_med"),
             "wall150_lo": ("x1_wall_lo_med", "xs_gorge_lo_med"), "flood_stage": ("x1_h_q", "xs_h_q"),
             "velocity": ("x1_v_q", "xs_v_q"), "omega_q": ("x1_omega_q", "xs_omega_q"),
             "velocity_est": ("x1_v_q_est", "xs_v_q")}
    out = {}
    for k, (a, b) in pairs.items():
        u, v = t[a].to_numpy(float), t[b].to_numpy(float)
        ok = np.isfinite(u) & np.isfinite(v)
        out[k] = {"spearman": float(pd.Series(u[ok]).rank().corr(pd.Series(v[ok]).rank())),
                  "partial": _partial_rank(u, v, Z),
                  "median_1m": float(np.median(u[ok])), "median_8m": float(np.median(v[ok])),
                  "median_ratio_8m_1m": float(np.median(v[ok] / np.where(u[ok] > 0, u[ok], np.nan)))}
    hq = t["x1_h_q"].to_numpy(float)
    a = t["area_km2"].to_numpy(float)
    out["flood_stage_1m"] = {"median": float(np.nanmedian(hq)), "p10": float(np.nanpercentile(hq, 10)),
                             "p90": float(np.nanpercentile(hq, 90)), "at_floor_0p1": float(np.mean(hq <= 0.1001)),
                             "above_5m": float(np.mean(hq > 5)),
                             "median_ge50km2": float(np.nanmedian(hq[a >= 50])),
                             "median_8m": float(np.nanmedian(t["xs_h_q"])),
                             "at_floor_8m": float(np.mean(t["xs_h_q"].to_numpy(float) <= 0.2501))}
    for k in ("x1_v_q", "x1_omega_q", "x1_v_q_est", "xs_v_q", "xs_omega_q"):
        y = np.log10(t[k].to_numpy(float).clip(1e-3))
        ok = np.isfinite(y) & np.isfinite(Z).all(axis=1)
        B = np.column_stack([np.ones(ok.sum()), Z[ok]])
        r = y[ok] - B @ np.linalg.lstsq(B, y[ok], rcond=None)[0]
        out[f"r2_{k}_on_q_s"] = float(1 - r.var() / y[ok].var())
    out["water"] = {"flattened_at_channel": float(np.average(t["x1_flatw_frac"], weights=t["x1_n"])),
                    "planar_at_channel": float(np.average(t["x1_planw_frac"], weights=t["x1_n"])),
                    "reaches_mostly_flattened": float(np.mean(t["x1_flatw_frac"] > 0.5)),
                    "water_width_median_m": float(np.median(t["x1_w0_med"])),
                    "dropped_sections": float(1 - t["x1_n"].sum() / t["x1_n_all"].sum()),
                    "under_water_depth_est_median_m": float(np.nanmedian(t["x1_d_u"]))}
    return out


def spacing(keep: list[str], repeats: int = 5, log=print) -> dict:
    """The keep list with sections every 50 m (every other 25 m section) against every 25 m: rank agreement of the
    features and the CV gain of ``gbm_terrain`` + keep over ``gbm_terrain`` (same folds) at both spacings."""
    s25, L25, _ = sample_frame(log=log)
    s50, L50, _ = sample_frame(every=2, log=log)
    j = s50.set_index("gid").reindex(s25["gid"].to_numpy())
    rho = {}
    for k in keep:
        u = col(s25, k)
        v = col(j.reset_index(), k)
        ok = np.isfinite(u) & np.isfinite(v)
        rho[k] = float(pd.Series(u[ok]).rank().corr(pd.Series(v[ok]).rank()))
    base = ML.BASE + ML.TERRAIN
    out = {"spearman_25_50": rho}
    for tag, (s, L) in (("25m", (s25, L25)), ("50m", (s50, L50))):
        specs = [spec("gbm_terrain", "gbm", base), spec("gbm_t+keep", "gbm", base + keep),
                 spec("logit_plus", "logistic", base + ML.OSM + ML.ROCK_ONEHOT),
                 spec("logit_p+keep", "logistic", base + ML.OSM + ML.ROCK_ONEHOT + keep)]
        cvt, _, _ = cv_table(s, L, specs, {"gbm_t+keep": "gbm_terrain", "logit_p+keep": "logit_plus"}, repeats, log)
        out[tag] = cvt.round(5).to_dict(orient="index")
        log(f"spacing {tag}: " + cvt[["auc_style", "d_auc_style", "d_auc_style_size"]].round(4).to_string())
    (X.out_dir() / "spacing.json").write_text(json.dumps(out, indent=1, default=float))
    return out


# ---------------------------------------------------------------------------------------------- selection
VALLEY = ["x1_w10", "x1_w20", "x1_wall", "x1_vf"]
# one or two features per concept (rank-duplicates dropped: omega_q ~ vel 1.00, _est ~ measured 1.00, bank_hi ~ bank
# 0.97, rbf ~ hb 0.91, w1 ~ wd 0.90, brk ~ hb -0.87, pinchf ~ pinch -0.79)
# kept after the backward elimination (docs/cross-sections-1m.md): the valley set and these channel features
ROUND2 = ("banks", "rough", "hyd", "shape")      # groups that cost something when dropped in round 1
KEEP_LOW = ["x1_bank", "x1_steep", "x1_rough", "x1_dropconc", "x1_b", "x1_ffbf"]   # round 2: hyd dropped
PRUNED = {"hyd": ["x1_vel"], "shape": ["x1_b", "x1_ffbf"], "size": ["x1_w0", "x1_wd"], "bankfull": ["x1_hb", "x1_er"],
          "banks": ["x1_bank", "x1_steep"], "asym": ["x1_asym_ang"], "var": ["x1_w1_cv", "x1_hb_cv", "x1_pinch"],
          "rough": ["x1_rough", "x1_dropconc"]}


def select(groups: dict[str, list[str]] | None = None, base_extra: list[str] | None = None, repeats: int = 10,
           log=print) -> pd.DataFrame:
    """Backward elimination by group: the full model (terrain + ``base_extra`` + every group) against the same model
    without each group, paired on the same folds, for LightGBM and the logistic model. A group whose removal costs
    nothing adds nothing beyond the others."""
    groups = groups or PRUNED
    base_extra = VALLEY if base_extra is None else base_extra
    s, L, _ = sample_frame(log=log)
    allf = [f for g in groups.values() for f in g]
    bg, bl = ML.BASE + ML.TERRAIN + base_extra, ML.BASE + ML.TERRAIN + ML.OSM + ML.ROCK_ONEHOT + base_extra
    specs, pairs = [], {}
    for kind, base, tag in (("gbm", bg, "gbm"), ("logistic", bl, "logit")):
        specs += [spec(f"{tag}_base", kind, base), spec(f"{tag}_full", kind, base + allf)]
        pairs[f"{tag}_full"] = f"{tag}_base"
        for g, feats in groups.items():
            specs.append(spec(f"{tag}_full-{g}", kind, base + [f for f in allf if f not in feats]))
            pairs[f"{tag}_full-{g}"] = f"{tag}_full"
    cvt, imp, per = cv_table(s, L, specs, pairs, repeats=repeats, log=log)
    out = X.out_dir() / f"select_{'_'.join(groups)}.json"
    out.write_text(json.dumps({"groups": groups, "base_extra": base_extra, "cv": cvt.round(5).to_dict(orient="index"),
                               "importance": imp.get("gbm_full"),
                               "per_repeat": {k: {m: v.round(5).tolist() for m, v in d.items()}
                                              for k, d in per.items()}}, indent=1, default=float))
    log(cvt[["auc_style", "d_auc_style", "d_auc_style_n", "d_auc_style_size", "d_auc_style_size_n"]].round(4)
        .to_string())
    return cvt


# ---------------------------------------------------------------------------------------------- driver
def _md(df: pd.DataFrame, fmt: str = "{:.3f}") -> str:
    cols = list(df.columns)
    out = ["| " + " | ".join([df.index.name or ""] + cols) + " |", "|" + "---|" * (len(cols) + 1)]
    for i, r in df.iterrows():
        vals = []
        for c in cols:
            v = r[c]
            vals.append(fmt.format(v) if isinstance(v, (float, np.floating)) and np.isfinite(v) else str(v))
        out.append("| " + " | ".join([str(i)] + vals) + " |")
    return "\n".join(out)


def study(repeats: int = 10, keep_low: list[str] | None = None, log=print) -> dict:
    """The whole protocol on the sample; writes ``study.json`` and ``tables.md``. The final models use ``VALLEY`` +
    ``keep_low`` (default ``KEEP_LOW``, the channel features kept by ``select``)."""
    t0 = time.time()
    od = X.out_dir()
    keep_low = KEEP_LOW if keep_low is None else keep_low
    keep = VALLEY + keep_low
    s, L, cov = sample_frame(log=log)
    log(f"sample: {cov['sample_reaches']} reaches, {cov['sample_positives']} positives on {cov['sample_rivers']} "
        f"rivers ({time.time() - t0:.0f} s)")
    names = list(FEATURES1M)
    res = {"coverage": cov, "keep": keep}
    sep = separation(s, L, names + REF)
    sp = spearman(s, names, REF)
    rs = residual(s, L, names, TERRAIN_NUM)
    rs8 = residual(s, L, names, TERRAIN_NUM + ML.XSEC)
    res["separation"] = sep.round(4).to_dict(orient="index")
    res["spearman"] = sp.round(3).to_dict(orient="index")
    res["residual_terrain"] = rs.round(4).to_dict(orient="index")
    res["residual_terrain_xsec8"] = rs8.round(4).to_dict(orient="index")
    res["resolution"] = resolution(s)
    log(f"single features done ({time.time() - t0:.0f} s)")
    specs, pairs, comp = [], {}, []
    for kind, tag, base, b0 in (("gbm", "gbm", ML.BASE + ML.TERRAIN, "gbm_terrain"),
                                ("logistic", "logit", ML.BASE + ML.TERRAIN + ML.OSM + ML.ROCK_ONEHOT, "logit_plus")):
        x8, t1 = f"{tag}_xsec", f"{tag}_t+"
        specs += [spec(b0, kind, base), spec(x8, kind, base + ML.XSEC)]
        pairs[x8] = b0
        for gname, feats in GROUPS.items():
            specs.append(spec(t1 + gname, kind, base + feats))
            pairs[t1 + gname] = b0
        specs += [spec(t1 + "x1all", kind, base + names), spec(t1 + "keep", kind, base + keep),
                  spec(f"{x8}+low", kind, base + ML.XSEC + keep_low)]
        pairs.update({t1 + "x1all": b0, t1 + "keep": b0, f"{x8}+low": x8})
        comp += [(x8, b0), (t1 + "valley", b0), (t1 + "valley", x8), (t1 + "keep", b0), (t1 + "keep", x8),
                 (t1 + "keep", t1 + "valley"), (t1 + "x1all", t1 + "keep"), (f"{x8}+low", x8)]
    cvt, imp, per = cv_table(s, L, specs, pairs, repeats=repeats, log=log)
    cmp_ = paired(per, comp)
    res["cv"] = cvt.round(5).to_dict(orient="index")
    res["paired"] = cmp_.reset_index().round(5).to_dict(orient="records")
    res["importance"] = {k: v for k, v in imp.items() if k in ("gbm_t+x1all", "gbm_t+keep", "gbm_xsec+low")}
    res["per_repeat"] = {k: {m: v.round(5).tolist() for m, v in d.items()} for k, d in per.items()}
    log(cmp_[["vs", "d_auc_style", "d_auc_style_n", "d_auc_style_size", "d_auc_style_size_n"]].round(4).to_string())
    log(f"cv done ({time.time() - t0:.0f} s)")
    lo_ids = ("gbm_terrain", "gbm_xsec", "gbm_t+valley", "gbm_t+keep", "gbm_xsec+low", "logit_plus", "logit_xsec",
              "logit_t+keep")
    lo = loro_table(s, L, [sp_ for sp_ in specs if sp_.id in lo_ids], log=log)
    res["loro"] = lo.round(4).to_dict(orient="index")
    res["loro_mean"] = lo[[c for c in lo.columns if c != "positives"]].mean().round(4).to_dict()
    res["loro_better"] = {f"{a} vs {b}": int((lo[a] > lo[b]).sum()) for a, b in
                          (("gbm_t+keep", "gbm_terrain"), ("gbm_t+keep", "gbm_xsec"), ("gbm_xsec+low", "gbm_xsec"),
                           ("gbm_t+valley", "gbm_xsec"), ("logit_t+keep", "logit_plus"),
                           ("logit_t+keep", "logit_xsec"))}
    (od / "study.json").write_text(json.dumps(res, indent=1, default=float))
    md = ["## separation", _md(sep), "## spearman", _md(sp, "{:.2f}"), "## residual on gbm_terrain inputs", _md(rs),
          "## residual on gbm_terrain + 8 m xsec inputs", _md(rs8), "## cv", _md(cvt, "{:.4f}"), "## paired",
          _md(cmp_, "{:.4f}"), "## loro", _md(lo, "{:.4f}"), "## loro mean",
          json.dumps(res["loro_mean"], indent=1), json.dumps(res["loro_better"], indent=1)]
    (od / "tables.md").write_text("\n\n".join(md) + "\n")
    log(f"study -> {od / 'study.json'} ({time.time() - t0:.0f} s)")
    return res


# ---------------------------------------------------------------------------------------------- storage
def storage(tiles=("x05y07", "x12y16"), log=print) -> dict:
    """Bytes per section of the storage options, from the profiles kept by the tile run (``--keep-profiles``)."""
    import pyarrow as pa

    def zst(a: np.ndarray) -> int:
        return len(pa.Codec("zstd", compression_level=9).compress(np.ascontiguousarray(a).tobytes(), asbytes=True))

    od = X.out_dir() / "tiles"
    res = {}
    for tile in tiles:
        p = od / f"{tile}_prof.npz"
        if not p.exists():
            continue
        f = np.load(p)
        q = f["prof"]
        n = len(q)
        sec = pd.read_parquet(od / f"{tile}_sec.parquet")
        inner = q[:, X.CI - 150:X.CI + 151]
        o = np.r_[0:X.N_OUT, X.CI + 151:q.shape[1]]
        qd = q.copy()
        v = qd[:, o]
        qd[:, o] = np.where(v == X.NODATA_I16, v, np.round(v / 10.0) * 10).astype(np.int16)
        head = (zst(f["gid"]) + zst(f["s"]) + zst(np.column_stack([f["cx"], f["cy"]])) + zst(f["nx"]) + zst(f["ny"])
                + zst(f["z0"]) + zst(f["flags"])) / n
        tags = [X.stage_tag(h) for h in X.STAGES]
        dl = sec[[f"dl{t}" for t in tags]].to_numpy()
        dr = sec[[f"w{t}" for t in tags]].to_numpy() - dl
        st = np.nan_to_num(np.round(np.concatenate([dl, dr], axis=1) * 10)).clip(0, 65535).astype(np.uint16)
        ap = sec[[f"a{t}" for t in tags] + [f"p{t}" for t in tags]].to_numpy(np.float32)
        d, msk = X.encode(q)
        dd, mskd = X.encode(qd)
        di, mski = X.encode(inner, 150)
        if not np.array_equal(X.decode(d, msk), q):
            raise AssertionError("encode / decode is not exact")
        r = {"sections": n, "header": head, "heights_int16_cm": q.nbytes / n, "heights_zstd": zst(q) / n,
             "delta_zstd": (zst(d) + zst(msk)) / n, "delta_zstd_outer_dm": (zst(dd) + zst(mskd)) / n,
             "inner_only_delta_zstd": (zst(di) + zst(mski)) / n, "stage_table_zstd": (zst(st) + zst(ap)) / n}
        # the recommended layout as files: Arrow IPC (Feather v2, whole-buffer zstd) and Parquet
        import pyarrow.feather as pf
        import pyarrow.parquet as pq

        tbl = pa.table({"gid": f["gid"], "s": f["s"], "x": np.round(f["cx"] * 100).astype(np.int32),
                        "y": np.round(f["cy"] * 100).astype(np.int32), "nx": f["nx"], "ny": f["ny"], "z0": f["z0"],
                        "flags": f["flags"],
                        "dprof": pa.FixedSizeListArray.from_arrays(pa.array(d.ravel()), d.shape[1]),
                        "nodata": pa.FixedSizeListArray.from_arrays(pa.array(msk.ravel()), msk.shape[1])})
        tmp = X.out_dir() / f"_{tile}_layout"
        pf.write_feather(tbl, tmp.with_suffix(".arrow"), compression="zstd", compression_level=9)
        pq.write_table(tbl, tmp.with_suffix(".parquet"), compression="zstd", compression_level=9)
        r["file_arrow_zstd"] = tmp.with_suffix(".arrow").stat().st_size / n
        r["file_parquet_zstd"] = tmp.with_suffix(".parquet").stat().st_size / n
        back = pf.read_table(tmp.with_suffix(".arrow"))
        d2 = np.asarray(back["dprof"].combine_chunks().flatten()).reshape(d.shape)
        if not np.array_equal(d2, d):
            raise AssertionError("arrow round trip changed the profiles")
        tmp.with_suffix(".arrow").unlink()
        tmp.with_suffix(".parquet").unlink()
        sec_km = sec.groupby("gid")["s"].size().sum() / (sec.groupby("gid")["s"].max() + X.SPACING_M / 2).sum() * 1000
        r["sections_per_km"] = float(sec_km)
        res[tile] = {k: round(v, 1) if isinstance(v, float) else v for k, v in r.items()}
        log(f"{tile}: " + json.dumps(res[tile]))
    (X.out_dir() / "storage.json").write_text(json.dumps(res, indent=1))
    return res


# ---------------------------------------------------------------------------------------------- check on the sub-area
def _line_points(geoms, areas, step: float = 1.0):
    import shapely

    pts, area = [], []
    for geom, a in zip(geoms, areas, strict=True):
        d = np.arange(0, geom.length, step)
        pts.append(shapely.get_coordinates(shapely.line_interpolate_point(geom, d)))
        area.append(np.full(len(d), a))
    return np.vstack(pts), np.concatenate(area)


def _similar_dist(tree, area_pts, xy, a, ratio=(0.5, 2.0), k=50, radius=200.0):
    d, i = tree.query(xy, k=k, distance_upper_bound=radius)
    best = np.full(len(xy), np.inf)
    for j in range(k):
        ok = np.isfinite(d[:, j])
        ii = np.where(ok, i[:, j], 0)
        r = area_pts[ii] / a
        sim = ok & (r >= ratio[0]) & (r <= ratio[1])
        best = np.where(sim & (d[:, j] < best), d[:, j], best)
    return best


def check(log=print) -> dict:
    """Re-centred 8 m sections against the 1 m channel on the benchmark sub-area (needs the x05y07 tile run and
    ``data/analysis/lidar-bench/variants/s1``)."""
    import pyogrio
    import shapely
    from rasterio.transform import from_origin
    from scipy.spatial import cKDTree

    from .national import nz_dir
    from .nz import Tile
    from .regions import REPO_ROOT

    od = X.out_dir()
    sec = pd.read_parquet(od / "tiles" / "x05y07_sec.parquet")
    seg = pyogrio.read_dataframe(nz_dir() / "tiles" / "x05y07" / "segments_core.gpkg", columns=["gid", "area_km2"])
    mid = shapely.get_coordinates(shapely.line_interpolate_point(seg.geometry.to_numpy(), 0.5, normalized=True))
    m = 1000.0
    inside = ((mid[:, 0] > SUB[0] + m) & (mid[:, 0] < SUB[2] - m) & (mid[:, 1] > SUB[1] + m)
              & (mid[:, 1] < SUB[3] - m))
    area_of = dict(zip(seg["gid"], seg["area_km2"], strict=True))
    gids = set(seg["gid"].to_numpy()[inside])
    sec = sec[sec["gid"].isin(gids)].reset_index(drop=True)
    sec["area_km2"] = sec["gid"].map(area_of)
    s1 = pyogrio.read_dataframe(REPO_ROOT / "data" / "analysis" / "lidar-bench" / "variants" / "s1" / "segments.gpkg",
                                columns=["area_km2", "area_mid_km2"])
    s1 = s1[s1["area_km2"] >= 0.5].reset_index(drop=True)
    pts, apts = _line_points(s1.geometry.to_numpy(), s1["area_mid_km2"].to_numpy())
    tree = cKDTree(pts)
    xy = np.column_stack([sec["cx"], sec["cy"]])
    a = sec["area_km2"].to_numpy()
    fl = sec["flags"].to_numpy()
    kept = (fl & X.DROP) == 0
    dsim = _similar_dist(tree, apts, xy, a)
    out = {"sections": int(len(sec)), "reaches": int(sec["gid"].nunique()), "dropped": float(1 - kept.mean())}
    rows = []
    for lab, mm in (("kept", kept), ("dropped", ~kept), ("edge", (fl & X.FLAG_EDGE) != 0),
                    ("twin", (fl & X.FLAG_TWIN) != 0), ("far", (fl & X.FLAG_FAR) != 0), ("low", (fl & X.FLAG_LOW) != 0),
                    ("kept 2-10 km2", kept & (a < 10)), ("kept 10-100 km2", kept & (a >= 10) & (a < 100)),
                    ("kept >= 100 km2", kept & (a >= 100))):
        if not mm.any():
            continue
        b = np.where(np.isfinite(dsim[mm]), dsim[mm], 999.0)
        rows.append({"sections": lab, "n": int(mm.sum()), "share": float(mm.mean()), "p50_m": float(np.median(b)),
                     "p90_m": float(np.percentile(b, 90)), "within_5m": float(np.mean(b <= 5)),
                     "within_10m": float(np.mean(b <= 10)),
                     "line_off_p50": float(np.median(np.abs(sec["off_line"][mm]))),
                     "line_off_p90": float(np.percentile(np.abs(sec["off_line"][mm]), 90))})
    out["recentre"] = rows
    log(pd.DataFrame(rows).round(3).to_string())
    # twin sections along the 1 m lines (search 10 m, as for the 2 m lines of the rebuild)
    g1 = s1[s1["area_km2"] >= 1.0].reset_index(drop=True)
    ri, s, x, y, nx, ny = XS.stations(g1.geometry.to_numpy(), X.SPACING_M, X.TANGENT_M)
    ins = (x > SUB[0] + 700) & (x < SUB[2] - 700) & (y > SUB[1] + 700) & (y < SUB[3] - 700)
    ri, s, x, y, nx, ny = ri[ins], s[ins], x[ins], y[ins], nx[ins], ny[ins]
    R = X.OUTER_M + 25.0
    box = (2 * int(np.floor(x.min() / 2)) - int(R) - 2, 2 * int(np.floor(y.min() / 2)) - int(R) - 2,
           2 * int(np.ceil(x.max() / 2)) + int(R) + 2, 2 * int(np.ceil(y.max() / 2)) + int(R) + 2)
    sheets = [sh for sh in X.tile_sheets(Tile.parse("x05y07"), od)
              if not (sh["bounds"][2] <= box[0] or sh["bounds"][0] >= box[2] or sh["bounds"][3] <= box[1]
                      or sh["bounds"][1] >= box[3])]
    z1, tf1, _ = X.read_corridor(sheets, box, x, y, R)
    z2 = X.block_mean2(z1)
    tf2 = from_origin(tf1.c, tf1.f, 2, 2)
    tw = X.extract(z1, tf1, z2, tf2, ri, s, x, y, nx, ny, search=10)
    del z1, z2
    tt = X.section_table(tw, X.measure(tw["prof"]))
    tt["area_km2"] = g1["area_mid_km2"].to_numpy()[tt["reach"].to_numpy()]
    tk = (tt["flags"].to_numpy() & X.DROP) == 0
    out["s1_sections"] = int(len(tt))
    out["s1_dropped"] = float(1 - tk.mean())
    # pair each kept national section with the nearest kept twin (<= 15 m, area 0.5-2x)
    tt = tt[tk].reset_index(drop=True)
    tr = cKDTree(np.column_stack([tt["cx"], tt["cy"]]))
    d, i = tr.query(xy, k=8, distance_upper_bound=15.0)
    pair = np.full(len(sec), -1)
    for j in range(8):
        ok = np.isfinite(d[:, j]) & (pair < 0) & kept
        ii = np.where(ok, i[:, j], 0)
        r = tt["area_km2"].to_numpy()[ii] / a
        ok &= (r >= 0.5) & (r <= 2.0)
        pair[ok] = ii[ok]
    out["paired_share_of_kept"] = float((pair >= 0)[kept].mean())
    P = np.flatnonzero(pair >= 0)
    meas = ["w0", "w1", "w2", "w5", "b", "ff1", "hb_use", "wbf", "er", "ang_l", "steep_l", "asym_w", "w10", "w20",
            "wall150_l"]
    sp = {}
    A_, B_ = sec.iloc[P], tt.iloc[pair[P]]
    for k in meas:
        u, v = A_[k].to_numpy(float), B_[k].to_numpy(float)
        if k in ("ang_l", "steep_l", "wall150_l"):     # sides are not matched: compare the mean of both sides
            k2 = k.replace("_l", "_r")
            u = 0.5 * (u + A_[k2].to_numpy(float))
            v = 0.5 * (v + B_[k2].to_numpy(float))
        ok = np.isfinite(u) & np.isfinite(v)
        diff = k in ("b", "ang_l", "steep_l", "asym_w")
        with np.errstate(invalid="ignore", divide="ignore"):
            rel = u[ok] - v[ok] if diff else u[ok] / v[ok]
        sp[k] = {"spearman": float(pd.Series(u[ok]).rank().corr(pd.Series(v[ok]).rank())),
                 "median_diff" if diff else "median_ratio": float(np.nanmedian(rel))}
    out["section_agreement"] = sp
    # per reach: the national reach's kept sections vs the twins paired to them
    nat_idx = {g: k for k, g in enumerate(sorted(gids))}
    sec["reach"] = sec["gid"].map(nat_idx)
    twin = tt.iloc[pair[P]].copy()
    twin["reach"] = sec["reach"].to_numpy()[P]
    twin["s"] = sec["s"].to_numpy()[P]
    nr = len(nat_idx)
    ra = X.reach_table(sec[kept].assign(flags=0), nr)
    rb = X.reach_table(twin.assign(flags=0), nr)
    okr = (ra["x1_n"] >= 3) & (rb["x1_n"] >= 3)
    rr = {}
    for c in ("x1_w0_med", "x1_w1_med", "x1_b_med", "x1_ff1_med", "x1_hb_use_med", "x1_wbf_med", "x1_er_med",
              "x1_ang_med", "x1_steep_med", "x1_asym_w_med", "x1_w1_cv", "x1_pinch_frac", "x1_w10_med",
              "x1_wall_lo_med", "x1_vf_med"):
        u, v = ra.loc[okr, c].to_numpy(float), rb.loc[okr, c].to_numpy(float)
        ok = np.isfinite(u) & np.isfinite(v)
        rr[c] = float(pd.Series(u[ok]).rank().corr(pd.Series(v[ok]).rank()))
    out["reach_agreement"] = {"reaches": int(okr.sum()), "spearman": rr}
    log(json.dumps({k: out[k] for k in ("s1_sections", "s1_dropped", "paired_share_of_kept")}))
    log(pd.DataFrame(sp).T.round(3).to_string())
    log(pd.Series(rr).round(3).to_string())
    (od / "check_s1.json").write_text(json.dumps(out, indent=1))
    return out
