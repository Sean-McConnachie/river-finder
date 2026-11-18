"""CV plumbing on a synthetic network: catchment folds, OOF scores and stratified AUC."""

import numpy as np
import pandas as pd

from river_finder import ml
from river_finder import reachtype as RT


def _network(n_catch=60, length=12, seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    for c in range(n_catch):
        base = len(rows)
        for k in range(length):    # a single chain per catchment, outlet last
            rows.append({"down": base + k + 1 if k < length - 1 else -1, "area_km2": 1.0 + 3.0 * k,
                         "grad_s": float(rng.uniform(1, 120)), "maf_m3s": float(rng.uniform(1, 500)),
                         "valley_width_m": float(rng.uniform(8, 2000)), "guide_region": f"R{c % 4}"})
    return pd.DataFrame(rows)


def test_outlets_and_local_area():
    t = _network(3, 5)
    down = t["down"].to_numpy()
    order = ml._kahn(down)
    assert len(order) == len(t)
    out = ml._outlets(down, order)
    assert list(out) == [4] * 5 + [9] * 5 + [14] * 5
    la = ml.local_area(t)
    assert np.allclose(la[[0, 1, 2]], [1.0, 3.0, 3.0])
    acc = ml._accumulate(down, order, la)
    assert np.allclose(acc[4], t["area_km2"][4])


def test_folds_keep_catchments_whole():
    t = _network()
    t["outlet"] = ml._outlets(t["down"].to_numpy(), ml._kahn(t["down"].to_numpy()))
    n = len(t)
    pos = np.zeros(n, bool)
    pos[t.index[((t["outlet"] // 12) % 3 == 0) & (t["area_km2"] >= 20)]] = True
    river = np.where(pos, t["outlet"].astype(str), "").astype(object)
    L = ml.Labels(domain=np.ones(n, bool), pos=pos, pos_eval=pos, bg=~pos, neg_known=np.zeros(n, bool),
                  river=river, grade=np.where(pos, "3-4", "").astype(object), mapped=[])
    f = ml.make_folds(t, L, k=5, seed=1)
    assert (t.assign(f=f).groupby("outlet")["f"].nunique() == 1).all()
    per = pd.Series(river[pos]).groupby(f[pos]).nunique()
    assert per.max() - per.min() <= 1          # positive rivers balanced over folds


def test_pct_within_and_metrics():
    t = _network(80, 10, seed=3)
    t = pd.concat([t, RT.classify_frame(t)], axis=1)
    n = len(t)
    rng = np.random.default_rng(5)
    pos = rng.random(n) < 0.1
    river = np.where(pos, np.arange(n).astype(str), "").astype(object)
    L = ml.Labels(domain=np.ones(n, bool), pos=pos, pos_eval=pos, bg=~pos, neg_known=np.zeros(n, bool),
                  river=river, grade=np.where(pos, "≤3", "").astype(object), mapped=[])
    st = ml.strata(t, L.bg)
    perfect = pos.astype(float) + rng.random(n) * 0.1
    m = ml.metrics(perfect, t, L, st)
    assert m["auc"] == 1.0 and m["auc_style"] == 1.0 and m["recall_top10"] == 1.0
    rnd = rng.random(n)
    m = ml.metrics(rnd, t, L, st, detail=False)
    assert 0.4 < m["auc"] < 0.6
    # positives drawn at random from the steep styles: "steeper is better" wins overall, not within style
    steep = t["grad_s"].to_numpy()
    steep_style = (t["style"] == "bedrock_slides_and_gorges").to_numpy()
    pos2 = steep_style & (rng.random(n) < 0.5)
    L2 = ml.Labels(np.ones(n, bool), pos2, pos2, ~pos2, np.zeros(n, bool),
                   np.where(pos2, "r", "").astype(object), np.where(pos2, "?", "").astype(object), [])
    m2 = ml.metrics(steep, t, L2, ml.strata(t, L2.bg), detail=False)
    assert m2["auc"] > 0.6 and abs(m2["auc_style"] - 0.5) < 0.1
    ties = np.zeros(n)
    assert abs(ml.metrics(ties, t, L, st, detail=False)["auc"] - 0.5) < 1e-9


def test_cv_explore_and_page_end_to_end(tmp_path, monkeypatch):
    from river_finder import mlpage
    from river_finder.labels import Section
    from river_finder.models import Mapped

    rng = np.random.default_rng(11)
    t = _network(120, 12, seed=11)
    n = len(t)
    for c, v in {"steepest_50m_m_per_km": t["grad_s"] * 2, "ksn_s": t["grad_s"] * 3, "power_kw_m": t["maf_m3s"] * 0.1,
                 "gorge_depth_m": rng.uniform(5, 100, n), "head_m": rng.uniform(10, 400, n),
                 "upstream_flat_frac": rng.random(n), "upstream_hard_frac": rng.random(n),
                 "bed_hard": (rng.random(n) < 0.3).astype(int), "knick_ratio": rng.uniform(0.5, 2, n),
                 "melton": rng.uniform(0.05, 1, n), "height_to_width": rng.uniform(0, 3, n), "length_m": 250.0,
                 "x": rng.uniform(1.6e6, 2.0e6, n), "y": rng.uniform(5.0e6, 6.0e6, n),
                 "rockclass": rng.choice(["schist", "clastic sediment"], n),
                 "rockgroup": rng.choice(["greywacke", "mudstone"], n),
                 "rock_age_ma": rng.uniform(1, 200, n), "karst_bed": (rng.random(n) < 0.05).astype(np.int8),
                 "karst_up_frac": rng.random(n) * 0.1, "falls_n": (rng.random(n) < 0.02).astype(int),
                 "falls_max_h_m": 0.0, "rapids_n": 0}.items():
        t[c] = v
    t["outlet"] = ml._outlets(t["down"].to_numpy(), ml._kahn(t["down"].to_numpy()))
    t = pd.concat([t, RT.classify_frame(t)], axis=1)
    mapped = []
    for c in range(0, 120, 4):
        rows = np.arange(c * 12 + 6, c * 12 + 11)
        s = Section(region=f"R{c % 4}", river=f"River {c}", status="runnable", grade="3", held_out=c == 0)
        mapped.append(Mapped(s, f"River {c} | R{c % 4}", rows, "name"))
    L = ml.labels(t, mapped)
    assert L.pos_eval.sum() > L.pos.sum() > 0           # the held-out river is evaluation-only
    monkeypatch.setitem(ml.GBM_PARAMS, "n_estimators", 10)
    monkeypatch.setattr(ml, "CV_DIR", tmp_path)
    zoo = [s for s in ml.ZOO if s.id in ("flow", "logit_plus", "gbm_terrain")]
    cv = ml.run_cv(t, L, zoo, repeats=2, log=lambda m: None)
    for s in zoo:
        S = cv["oof"][s.id]
        assert np.isfinite(S[:, L.domain]).all()
    lo = ml.run_loro(t, L, zoo, log=lambda m: None)
    res = ml.summarise(t, L, cv, lo, zoo)
    assert [m["id"] for m in res["models"]] == ["flow", "logit_plus", "gbm_terrain"]
    assert 0 <= res["models"][0]["cv"]["auc_style"]["mean"] <= 1
    import json
    (tmp_path / "results.json").write_text(json.dumps(res))
    e = mlpage.explore(t, L, log=lambda m: None)
    (tmp_path / "explore.json").write_text(json.dumps(e, allow_nan=False))
    page = mlpage.build(tmp_path / "page", log=lambda m: None)
    html = page.read_text()
    assert "River Finder ML" in html and "%%DATA%%" not in html
    assert (tmp_path / "page" / mlpage.PDF).exists()
