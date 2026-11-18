"""Montgomery & Buffington (1997) channel-reach types, used to compare like with like.

The specification is ``docs/MB1997_reach_classification_spec.pdf`` (spec version
``mb1997-ops-1.0.0``). Its machine-readable Appendix A is ``assets/mb1997/mb1997_spec.json``,
copied unchanged from the PDF attachment; the reference implementation (Appendix B) is
``tests/mb_classifier_ref.py`` and the tests check this vectorised version against it.

What we can measure on a national 8 m DEM
-----------------------------------------
The spec's Track B ("quantitative proxies only") lists slope, width/depth, relative submergence,
excess Shields stress, bed material class and confinement. We have no grain size or bankfull
depth anywhere in NZ, so four of the six are missing. The spec says a missing feature is skipped
for every class, so we run **Track B with the two inputs we have**:

- ``slope`` (m/m) = the reach's smoothed gradient ``grad_s`` / 1000, floored at 1e-4 (flat
  DEM reaches still get a value);
- ``confinement`` from the ratio valley-floor width / bankfull width, with the spec's operational
  thresholds (< 2 confined, 2-4 moderately confined, > 4 unconfined). Valley-floor width is
  ``valley_width_m`` from ``reaches.py`` (ground within 5 m height of its channel, per metre of
  channel). Bankfull width is not measured, so we estimate it from downstream hydraulic geometry,
  ``w_bf = BANKFULL_A * MAF ** 0.5`` with the mean annual flood standing in for bankfull flow.
  ``BANKFULL_A`` = 4.0 is our operational choice (recorded in every report).

The resulting type is a slope-and-confinement class with Montgomery & Buffington names, not a
field classification. In particular "bedrock" with slope alone becomes the band between
step-pool and cascade slopes, because the bedrock slope range is the widest.

Styles
------
Section 9 of the spec maps reach types to paddling styles (a heuristic, not part of MB1997). A
reach's style score is the sum of its mapped types' probabilities and its style is the argmax.
The evaluation ranks each test reach only against background reaches of the same style
("style-matched AUC"), so a model earns nothing for knowing that a steep creek is steeper than a
lowland river.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources

import numpy as np
import pandas as pd

SPEC_FILE = "mb1997_spec.json"
TRACK = "B_proxy"
BANKFULL_A = 4.0          # w_bf = A * MAF^0.5 (m, m3/s); operational
SLOPE_FLOOR = 1e-4        # m/m


@lru_cache(maxsize=1)
def load_spec() -> dict:
    return json.loads((resources.files("river_finder") / "assets" / "mb1997" / SPEC_FILE).read_text())


def classes() -> list[str]:
    return list(load_spec()["classes"])


def styles() -> list[str]:
    return list(load_spec()["paddling_style_mapping"]["map"])


def classify_arrays(inputs: dict[str, np.ndarray], track: str = TRACK, prior: dict | None = None,
                    spec: dict | None = None) -> dict:
    """Vectorised ``classify`` from the spec's reference implementation.

    ``inputs``: feature name -> array (numeric: float, NaN or <= 0 = missing; categorical: object
    or str, None / "" = missing). Optional ``obstruction_forced`` (bool array) and ``multithread``.
    Returns ``probs`` (n, 7), ``log_scores``, ``pred`` (class index), ``transitional``,
    ``low_evidence``, ``used_weight`` and ``out_of_scope`` (braided).
    """
    spec = spec or load_spec()
    cls = spec["classes"]
    model = spec["scoring_model"]
    allowed = set(spec["tracks"][track]["features"])
    sigma_min = model["sigma_min_log10"]
    neutral = model["neutral_membership_if_class_param_null"]
    n = len(next(iter(inputs.values())))
    prior = prior or {c: 1.0 / len(cls) for c in cls}
    S = np.tile(np.log([prior[c] for c in cls]), (n, 1)).astype(float)
    used = np.zeros(n)
    forced = np.asarray(inputs.get("obstruction_forced", np.zeros(n, bool)), dtype=object) == True  # noqa: E712
    pools = inputs.get("pools_present")
    for name, feat in spec["numeric_features"].items():
        if name not in allowed or name not in inputs:
            continue
        x = np.asarray(inputs[name], dtype=float)
        ok = np.isfinite(x) & (x > 0)
        if name == "pool_spacing":
            ok &= np.asarray(pools if pools is not None else np.full(n, None), dtype=object) == "true"
        lx = np.log10(np.where(ok, x, 1.0))
        for j, c in enumerate(cls):
            p = feat["class_params"][c]
            if p is None:
                m = np.full(n, neutral)
            else:
                z = (lx - p["log10_mu"]) / max(p["log10_sigma"], sigma_min)
                m = np.exp(-0.5 * z * z)
                if name == "slope" and c == "pool_riffle" and forced.any():
                    lo, hi = np.log10(0.002), np.log10(0.03)
                    zf = (lx - (lo + hi) / 2) / max((hi - lo) / 1.349, sigma_min)
                    m = np.where(forced, np.exp(-0.5 * zf * zf), m)
            S[ok, j] += feat["weight"] * np.log(np.maximum(m[ok], 1e-300))
        used[ok] += feat["weight"]
    for name, feat in spec["categorical_features"].items():
        if name not in allowed or name not in inputs:
            continue
        v = pd.Series(inputs[name], dtype=object).str.lower().to_numpy()
        ok = pd.notna(v) & (v != "")
        bad = set(v[ok]) - set(feat["values"])
        if bad:
            raise ValueError(f"{name}: {sorted(bad)} not in {feat['values']}")
        for j, c in enumerate(cls):
            lk = feat["likelihood"][c]
            S[ok, j] += feat["weight"] * np.log(np.array([lk[k] for k in v[ok]], dtype=float))
        used[ok] += feat["weight"]
    top = S.max(axis=1, keepdims=True)
    e = np.exp(S - top)
    P = e / e.sum(axis=1, keepdims=True)
    order = np.argsort(-P, axis=1, kind="stable")   # ties: order in ``classes``
    pred, second = order[:, 0], order[:, 1]
    adj = spec["adjacent_classes"]["map"]
    A = np.array([[cls[b] in adj[cls[a]] for b in range(len(cls))] for a in range(len(cls))])
    rows = np.arange(n)
    trans = (P[rows, pred] - P[rows, second] < 0.15) & A[pred, second]
    multi = np.asarray(inputs.get("multithread", np.zeros(n, bool)), dtype=object) == True  # noqa: E712
    return {"probs": P, "log_scores": S, "pred": pred, "transitional": trans,
            "low_evidence": used < 1.5, "used_weight": used, "out_of_scope": multi}


def nz_inputs(f: pd.DataFrame, bankfull_a: float = BANKFULL_A) -> dict[str, np.ndarray]:
    """Track B inputs from a reach table (``grad_s``, ``maf_m3s``, ``valley_width_m``)."""
    slope = np.maximum(np.nan_to_num(f["grad_s"].to_numpy(dtype=float), nan=0.0) / 1000.0, SLOPE_FLOOR)
    ratio = confinement_ratio(f, bankfull_a)
    conf = np.where(~np.isfinite(ratio), None,
                    np.where(ratio < 2, "confined", np.where(ratio <= 4, "moderately_confined", "unconfined")))
    return {"slope": slope, "confinement": conf.astype(object)}


def bankfull_width(f: pd.DataFrame, bankfull_a: float = BANKFULL_A) -> np.ndarray:
    q = f["maf_m3s"].to_numpy(dtype=float)
    return np.where(np.isfinite(q) & (q > 0), bankfull_a * np.sqrt(np.maximum(q, 0.0)), np.nan)


def confinement_ratio(f: pd.DataFrame, bankfull_a: float = BANKFULL_A) -> np.ndarray:
    vw = f["valley_width_m"].to_numpy(dtype=float)
    return vw / np.maximum(bankfull_width(f, bankfull_a), 1.0)


def classify_frame(f: pd.DataFrame, bankfull_a: float = BANKFULL_A) -> pd.DataFrame:
    """Per reach: probability of each type (``p_<type>``), ``reach_type``, ``style`` and flags."""
    spec = load_spec()
    out = classify_arrays(nz_inputs(f, bankfull_a), spec=spec)
    cls = spec["classes"]
    smap = spec["paddling_style_mapping"]["map"]
    d = {f"p_{c}": out["probs"][:, j].astype(np.float32) for j, c in enumerate(cls)}
    sp = np.column_stack([sum(out["probs"][:, cls.index(c)] for c in members) for members in smap.values()])
    names = list(smap)
    d["reach_type"] = pd.Categorical.from_codes(out["pred"], categories=cls)
    d["style"] = pd.Categorical.from_codes(np.argmax(sp, axis=1), categories=names)
    d["style_p"] = sp.max(axis=1).astype(np.float32)
    d["transitional"] = out["transitional"]
    d["confinement_ratio"] = confinement_ratio(f, bankfull_a).astype(np.float32)
    return pd.DataFrame(d, index=f.index)


STYLE_LABELS = {
    "too_small_or_unrunnable": "Too small / unrunnable (colluvial)",
    "bedrock_slides_and_gorges": "Bedrock slides & gorges (bedrock)",
    "boulder_garden_creek": "Boulder-garden creek (cascade)",
    "pool_drop_steep_creek": "Pool-drop steep creek (step-pool)",
    "continuous_read_and_run": "Continuous read-and-run (plane-bed)",
    "easy_pool_riffle": "Easy pool-riffle (pool-riffle, dune-ripple)",
}
