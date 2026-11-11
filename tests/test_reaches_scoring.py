import numpy as np
import pandas as pd

from river_finder.reaches import _steepest, extract_links
from river_finder.scoring import RunParams, assemble_runs, logistic_fit, passes, percentile_rank


def test_extract_links_splits_at_confluence():
    # Two branches (0->1->2 and 3->4->2) join at 2, which flows 2->5->6 (outlet).
    rec = np.array([1, 2, 5, 4, 2, 6, -1], dtype=np.int64)
    chan = np.ones(7, dtype=np.bool_)
    cells, ptr, nxt = extract_links(rec, chan)
    links = [list(cells[ptr[i]:ptr[i + 1]]) for i in range(len(ptr) - 1)]
    assert sorted(map(tuple, links)) == [(0, 1), (2, 5, 6), (3, 4)]
    by_head = {lk[0]: nx for lk, nx in zip(links, nxt, strict=True)}
    assert by_head[0] == 2 and by_head[3] == 2 and by_head[2] == -1


def test_extract_links_ignores_non_channel_donors():
    rec = np.array([1, 2, -1, 1], dtype=np.int64)  # cell 3 drains into 1 but is not a channel
    chan = np.array([True, True, True, False])
    cells, ptr, _ = extract_links(rec, chan)
    assert len(ptr) == 2 and list(cells) == [0, 1, 2]


def test_steepest_window_finds_waterfall():
    dist = np.arange(0, 250, 5.0)
    z = 100 - dist * 0.02  # 20 m/km background
    z[dist >= 120] -= 10  # a 10 m fall at 120 m
    assert _steepest(dist, z, 50.0) > 150


def _chain(grads, mafs, length=250.0):
    n = len(grads)
    z = np.cumsum([0] + [-g * length / 1000 for g in grads])[:-1] + 500
    return pd.DataFrame({
        "seg_id": range(n), "down_seg": list(range(1, n)) + [-1], "up_seg": [-1] + list(range(n - 1)),
        "length_m": length, "grad_s": grads, "gradient_m_per_km": grads, "maf_m3s": mafs,
        "z_top": z, "z_bot": z - np.array(grads) * length / 1000, "steepest_50m_m_per_km": grads,
        "area_km2": np.linspace(5, 10, n), "gorge_depth_m": 50.0, "bed_hard": 1, "river": "Test Creek",
    })


def test_assemble_runs_chains_and_allows_one_gap():
    g = _chain([5, 30, 35, 10, 40, 30, 5, 5, 45, 5], [10] * 10)
    runs = assemble_runs(g, RunParams(flow_scaled=False, min_length_km=0.5))
    # segments 1-5 form one run (segment 3 is a one-segment gap); segment 8 alone is too short
    assert len(runs) == 1
    assert runs.iloc[0]["n_segments"] == 5
    assert list(np.flatnonzero(g["run_id"] == 0)) == [1, 2, 3, 4, 5]


def test_flow_scaling_lowers_the_bar_for_big_rivers():
    g = _chain([15, 15, 15, 15, 15], [80, 80, 80, 80, 80])
    assert not passes(g, RunParams(flow_scaled=False)).any()
    assert passes(g, RunParams(flow_scaled=True)).all()  # 20 * (80/20)^-0.5 = 10 m/km


def test_logistic_fit_separates_and_ranks():
    rng = np.random.default_rng(1)
    X = rng.normal(size=(400, 2))
    y = (X[:, 0] + 0.2 * rng.normal(size=400) > 1.0).astype(int)
    coef, b0 = logistic_fit(X, y, l2=0.1)
    assert coef[0] > 1.0 and abs(coef[1]) < 0.5
    s = X @ coef + b0
    assert percentile_rank(s[y == 1], s).mean() > 85
