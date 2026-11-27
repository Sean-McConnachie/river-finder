"""The ML page: data exploration, separation, CV metrics for every model, and documentation.

``explore`` summarises the national reach table into ``models/cv/explore.json`` (histograms,
quantiles, univariate separation, label statistics); ``ml.run`` writes ``models/cv/results.json``.
Both are committed, so ``build`` can render the page from the repository alone:

    uv run river-finder ml explore     # needs data/nz (national scan); ~2 min
    uv run river-finder ml cv          # repeated 5-fold OOF + leave-one-region-out; ~1 h
    uv run river-finder ml page        # data/viewer_nz/ml.html (+ ml_fragment.html for the artifact)
"""

from __future__ import annotations

import datetime as dt
import json
from importlib import resources
from pathlib import Path

import numpy as np
import pandas as pd

from . import ml
from . import reachtype as RT
from .regions import REPO_ROOT

PDF = "MB1997_reach_classification_spec.pdf"
NBINS = 30

# Features shown on the page: name -> (label, unit). Values use the model transforms (``ml.column``)
# except ``area``.
SHOW = {
    "gradient": ("Gradient", "m/km, smoothed over ~750 m, capped at 150"),
    "steep50": ("Steepest 50 m", "ln(1 + m/km)"),
    "ksn": ("Channel steepness k_sn", "ln(1 + k_sn)"),
    "power": ("Stream power at MAF", "ln(1 + kW/m)"),
    "maf": ("Mean annual flood", "log10 m³/s"),
    "area": ("Catchment area", "log10 km²"),
    "gorge": ("Gorge depth", "m, capped at 250"),
    "head": ("Head (catchment height above channel)", "m, capped at 600"),
    "plateau": ("Plateau share of catchment", "fraction"),
    "hard_up": ("Resistant rock share of catchment", "fraction"),
    "bed_hard": ("Channel on resistant rock", "0/1"),
    "knick": ("Knickpoint ratio", "ln(k_sn / k_sn downstream)"),
    "melton": ("Melton ruggedness (height-to-area)", "log10"),
    "h_to_w": ("Height-to-width", "ln(1 + gorge depth / valley width)"),
    "valley_w": ("Valley-floor width", "log10 m"),
    "confine": ("Confinement ratio", "log10(valley width / bankfull width)"),
    "karst_bed": ("Channel on karst", "0/1"),
    "karst_up": ("Karst share of catchment", "fraction"),
    "rock_age": ("Bed rock age", "log10 Ma"),
    "xs_w10": ("Cross-section width at 10 m stage", "log10 m (8 m DEM, median of sections)"),
    "xs_w20": ("Cross-section width at 20 m stage", "log10 m"),
    "xs_wall": ("Lower valley wall (150 m)", "m, capped at 300"),
    "xs_h2a": ("Cross-sectional height-to-area", "log10(lower wall / sqrt(area at 20 m stage))"),
    "xs_pinch": ("Constriction (pinch point)", "log10(narrowest / median section width at 20 m stage)"),
    "xs_omega10": ("Unit stream power over the 10 m-stage width", "log10 W/m²"),
    "xs_vel": ("Mean velocity at MAF (Manning)", "log10 m/s"),
    "xs_omega_q": ("Unit stream power at MAF (Manning width)", "log10 W/m²"),
    "x1_w10": ("1 m section width at 10 m stage", "log10 m (1 m LiDAR, median of sections)"),
    "x1_w20": ("1 m section width at 20 m stage", "log10 m"),
    "x1_wall": ("Lower valley wall (150 m, 1 m sections)", "m, capped at 300"),
    "x1_vf": ("Valley floor width-to-height", "log10"),
    "x1_b": ("Channel shape exponent b (w ~ h^b)", "0 box, 0.5 parabola, 1 V"),
    "x1_ffbf": ("Form factor at bankfull", "A / (w h)"),
    "x1_bank": ("Bank angle to bankfull", "deg"),
    "x1_steep": ("Bank share in > 60° steps", "fraction"),
    "x1_rough": ("Bed roughness (1 m long profile)", "log10 m"),
    "x1_dropconc": ("Drop in the steepest 10 % of the reach", "fraction"),
    "x1_vel": ("Mean velocity at MAF (Manning, 1 m section)", "log10 m/s"),
    "falls": ("OSM waterfalls on reach", "ln(1 + count)"),
    "falls_h": ("Tallest OSM waterfall", "m"),
    "rapids": ("OSM rapids near reach", "ln(1 + count)"),
}


def _col(t: pd.DataFrame, name: str) -> np.ndarray:
    if name == "area":
        return np.log10(t["area_km2"].clip(lower=0.01).to_numpy(dtype=float))
    return np.nan_to_num(ml.column(t, name), nan=0.0)


def _auc(pos: np.ndarray, neg: np.ndarray) -> float | None:
    if not len(pos) or not len(neg):
        return None
    ns = np.sort(neg)
    lo, hi = np.searchsorted(ns, pos, "left"), np.searchsorted(ns, pos, "right")
    return float(np.mean((lo + hi) / 2.0) / len(ns))


def _med(s: pd.Series, mask: np.ndarray) -> float | None:
    v = s.to_numpy(dtype=float)[mask]
    v = v[np.isfinite(v)]
    return round(float(np.median(v)), 2) if len(v) else None


def _q(v: np.ndarray) -> list[float] | None:
    if not len(v):
        return None
    return [round(float(x), 4) for x in np.quantile(v, [0.05, 0.25, 0.5, 0.75, 0.95])]


def explore(t: pd.DataFrame | None = None, L: ml.Labels | None = None, log=print) -> dict:
    t = ml.ml_table(log=log) if t is None else t
    L = ml.labels(t) if L is None else L
    dom, bg, pos = L.domain, L.bg, L.pos_eval & L.domain
    reg = t["guide_region"].fillna("").to_numpy()
    regions = sorted(set(reg[dom]) - {""})
    styles = list(t["style"].cat.categories)
    sc = t["style"].cat.codes.to_numpy()
    feats = [f for f in SHOW if (f == "area" or f in ml.FEATURES or f in ml.EXTRA)
             and ml.available(t, ml.Spec(f, "", "", [f]))]
    cols = {f: _col(t, f) for f in feats}
    out: dict = {"created": dt.datetime.now().isoformat(timespec="minutes"), "regions": regions, "styles": styles,
                 "style_labels": RT.STYLE_LABELS, "reach_types": RT.classes(),
                 "features": [{"id": f, "label": SHOW[f][0], "unit": SHOW[f][1], "desc": ml.describe(f)
                               if f != "area" else "Catchment area at the reach's lower end"} for f in feats],
                 "domain_km2": ml.DOMAIN_KM2, "bankfull_a": RT.BANKFULL_A,
                 "spec_version": RT.load_spec()["spec_version"]}
    # --- totals and per-region / per-style summaries
    L_km = t["length_m"].to_numpy(dtype=float) / 1000.0

    def summary(mask_bg, mask_pos):
        rows = L.river[mask_pos]
        return {"background": int(mask_bg.sum()), "positives": int(mask_pos.sum()),
                "rivers": int(len(set(rows) - {""})), "km": round(float(L_km[mask_bg | mask_pos].sum()), 0),
                "km_pos": round(float(L_km[mask_pos].sum()), 1),
                "median_grad_bg": _med(t["grad_s"], mask_bg), "median_grad_pos": _med(t["grad_s"], mask_pos),
                "median_maf_bg": _med(t["maf_m3s"], mask_bg), "median_maf_pos": _med(t["maf_m3s"], mask_pos),
                "style_bg": [int((mask_bg & (sc == j)).sum()) for j in range(len(styles))],
                "style_pos": [int((mask_pos & (sc == j)).sum()) for j in range(len(styles))],
                "karst_bg": round(float(t["karst_bed"].to_numpy()[mask_bg].mean()), 4) if mask_bg.any() else None,
                "falls_bg": int((t["falls_n"].fillna(0).to_numpy()[mask_bg] > 0).sum()),
                "falls_pos": int((t["falls_n"].fillna(0).to_numpy()[mask_pos] > 0).sum()),
                "rapids_pos": int((t["rapids_n"].fillna(0).to_numpy()[mask_pos] > 0).sum()),
                "rapids_bg": int((t["rapids_n"].fillna(0).to_numpy()[mask_bg] > 0).sum())}

    out["total"] = summary(bg, pos)
    out["by_region"] = [{"region": r, **summary(bg & (reg == r), pos & (reg == r))} for r in regions]
    # --- histograms: fixed edges per feature (background 0.5-99.5 %), All + each region + each style
    hist = {}
    groups = {"All": np.ones(len(t), bool), **{r: reg == r for r in regions},
              **{f"style:{s}": sc == j for j, s in enumerate(styles)}}
    for f in feats:
        v = cols[f]
        lo, hi = np.quantile(v[bg], [0.005, 0.995])
        if f in ("bed_hard", "karst_bed"):
            lo, hi = -0.5, 1.5
        if hi <= lo:
            hi = lo + 1.0
        edges = np.linspace(lo, hi, NBINS + 1)
        cl = np.clip(v, lo, hi)
        h = {"edges": [round(float(e), 4) for e in edges], "groups": {}}
        for g, m in groups.items():
            h["groups"][g] = {"bg": np.histogram(cl[bg & m], edges)[0].tolist(),
                              "pos": np.histogram(cl[pos & m], edges)[0].tolist(),
                              "q_bg": _q(v[bg & m]), "q_pos": _q(v[pos & m])}
        hist[f] = h
    out["hist"] = hist
    log("explore: histograms")
    # --- univariate separation: AUC overall, within style (stratified), within each region
    st = ml.strata(t, bg)
    sep = []
    P = np.flatnonzero(pos)
    for f in feats:
        v = cols[f]
        within_style = ml._pct_within(v, bg, st["style"], P)
        within_size = ml._pct_within(v, bg, st["size"], P)
        sep.append({"feature": f, "auc": _auc(v[pos], v[bg]), "auc_style": float(np.nanmean(within_style)),
                    "auc_size": float(np.nanmean(within_size)),
                    "by_region": {r: _auc(v[pos & (reg == r)], v[bg & (reg == r)]) for r in regions},
                    "by_style": {s: _auc(v[pos & (sc == j)], v[bg & (sc == j)]) for j, s in enumerate(styles)}})
    out["separation"] = sep
    # --- Spearman correlation on a sample
    rng = np.random.default_rng(1)
    samp = np.flatnonzero(bg)
    samp = np.concatenate([rng.choice(samp, min(60000, len(samp)), replace=False), P])
    R = pd.DataFrame({f: cols[f][samp] for f in feats}).rank().corr().to_numpy()
    out["corr"] = {"features": feats, "matrix": np.round(np.nan_to_num(R), 3).tolist()}
    log("explore: separation + correlation")
    # --- 2D: gradient x log10 MAF
    gx = np.clip(t["grad_s"].fillna(0).to_numpy(), 0, 150)
    gy = np.clip(np.log10(t["maf_m3s"].clip(lower=0.05).fillna(0.05).to_numpy()), -1.3, 4.0)
    xe, ye = np.linspace(0, 150, 51), np.linspace(-1.3, 4.0, 37)
    two = {"x": {"label": "Gradient, m/km", "edges": xe.round(3).tolist()},
           "y": {"label": "Mean annual flood, log10 m³/s", "edges": ye.round(3).tolist()}, "groups": {}}
    for g, m in {"All": np.ones(len(t), bool), **{r: reg == r for r in regions}}.items():
        hb = np.histogram2d(gx[bg & m], gy[bg & m], [xe, ye])[0].astype(int)
        two["groups"][g] = {"bg": hb.T.tolist(), "pos_x": np.round(gx[pos & m], 1).tolist(),
                            "pos_y": np.round(gy[pos & m], 3).tolist(),
                            "pos_style": sc[pos & m].tolist()}
    out["grad_maf"] = two
    # --- reach types
    pcols = [f"p_{c}" for c in RT.classes()]
    out["reach_type"] = {
        "counts_bg": [int((bg & (t["reach_type"] == c).to_numpy()).sum()) for c in RT.classes()],
        "counts_pos": [int((pos & (t["reach_type"] == c).to_numpy()).sum()) for c in RT.classes()],
        "mean_p_bg": [round(float(x), 4) for x in t.loc[bg, pcols].mean().to_numpy()],
        "mean_p_pos": [round(float(x), 4) for x in t.loc[pos, pcols].mean().to_numpy()],
        "transitional_bg": round(float(t["transitional"].to_numpy()[bg].mean()), 4),
        "transitional_pos": round(float(t["transitional"].to_numpy()[pos].mean()), 4),
        "style_p_hist_bg": np.histogram(t["style_p"].to_numpy()[bg], 20, (0, 1))[0].tolist(),
        "style_p_hist_pos": np.histogram(t["style_p"].to_numpy()[pos], 20, (0, 1))[0].tolist(),
        "confinement": {k: [int(((t["confinement_ratio"].to_numpy() < 2) & m).sum()),
                            int(((t["confinement_ratio"].to_numpy() >= 2) & (t["confinement_ratio"].to_numpy() <= 4)
                                 & m).sum()),
                            int(((t["confinement_ratio"].to_numpy() > 4) & m).sum())] for k, m in
                        (("bg", bg), ("pos", pos))},
    }
    # --- geology
    rc = t["rockclass"].fillna("unknown").to_numpy()
    top = pd.Series(rc[bg]).value_counts().index[:14].tolist()
    out["rockclass"] = [{"rockclass": c, "bg": int((bg & (rc == c)).sum()), "pos": int((pos & (rc == c)).sum())}
                        for c in top]
    out["rockclass"].append({"rockclass": "other", "bg": int((bg & ~np.isin(rc, top)).sum()),
                             "pos": int((pos & ~np.isin(rc, top)).sum())})
    # --- labels
    secs = []
    for m in L.mapped:
        s = m.section
        rows = m.rows
        secs.append({"river": s.river, "region": s.region, "status": s.status, "grade": s.grade or "",
                     "band": ml.grade_band(s), "coords": s.coords, "method": m.method, "held_out": bool(s.held_out),
                     "reaches": int(len(rows)), "domain_reaches": int(dom[rows].sum()) if len(rows) else 0,
                     "key": m.key,
                     "section": (s.extra or {}).get("section") or "",
                     "style": (pd.Series(t["style"].to_numpy()[rows[dom[rows]]]).astype(str).mode().iloc[0]
                               if len(rows) and dom[rows].any() else "")})
    out["sections"] = secs
    out["grade_by_style"] = {s: {g: int(((L.grade == g) & pos & (sc == j)).sum()) for g in ("≤3", "3-4", "4+", "?")}
                             for j, s in enumerate(styles)}
    log("explore: done")
    return out


def write_explore(log=print) -> Path:
    e = explore(log=log)
    ml.cv_dir().mkdir(parents=True, exist_ok=True)
    p = ml.cv_dir() / "explore.json"
    p.write_text(json.dumps(e, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")
    log(f"explore -> {p} ({p.stat().st_size / 1e6:.1f} MB)")
    return p


def _docs_features() -> list[dict]:
    rows = []
    for name, (_, _, desc) in ml.FEATURES.items():
        rows.append({"id": name, "group": "terrain (v2)", "desc": desc})
    for name, (_, desc, grp) in ml.EXTRA.items():
        rows.append({"id": name, "group": grp, "desc": desc})
    for name, desc in ml.CATEGORICAL.items():
        rows.append({"id": name, "group": "geology", "desc": desc + " (top 30 groups, rest 'other'; LightGBM only)"})
    return rows


def page_data() -> dict:
    def load(p: Path):
        return json.loads(p.read_text()) if p.exists() else None

    from .models import evaluations_path

    return {"explore": load(ml.cv_dir() / "explore.json"), "results": load(ml.cv_dir() / "results.json"),
            "registry": load(evaluations_path()),
            "zoo": [{"id": s.id, "kind": s.kind, "title": s.title, "features": s.features,
                     "categorical": s.categorical, "description": s.description} for s in ml.ZOO],
            "feature_docs": _docs_features(), "pdf": PDF, "spec": _spec_digest()}


def _spec_digest() -> dict:
    s = RT.load_spec()
    nf = s["numeric_features"]
    return {"version": s["spec_version"], "classes": s["classes"],
            "slope_iqr": {c: nf["slope"]["class_params"][c] for c in s["classes"]},
            "confinement": s["categorical_features"]["confinement"]["likelihood"],
            "style_map": s["paddling_style_mapping"]["map"],
            "weights": {"slope": nf["slope"]["weight"],
                        "confinement": s["categorical_features"]["confinement"]["weight"]}}


def build(out_dir: Path, log=print) -> Path:
    """``ml.html`` (full document, for local use) and ``ml_fragment.html`` (for the artifact host),
    plus a copy of the MB1997 spec PDF next to them."""
    out_dir.mkdir(parents=True, exist_ok=True)
    tpl = (resources.files("river_finder") / "assets" / "ml_template.html").read_text()
    data = json.dumps(page_data(), ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = tpl.replace("%%DATA%%", data)
    (out_dir / "ml_fragment.html").write_text(page)
    head, _, body = page.partition("<!--BODY-->")
    (out_dir / "ml.html").write_text("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
                                     "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, "
                                     "viewport-fit=cover\">\n" + head + "\n</head>\n<body>\n" + body
                                     + "\n</body>\n</html>\n")
    (out_dir / PDF).write_bytes((REPO_ROOT / "docs" / PDF).read_bytes())
    log(f"ml page -> {out_dir / 'ml.html'} ({(out_dir / 'ml.html').stat().st_size / 1e6:.1f} MB)")
    return out_dir / "ml.html"
