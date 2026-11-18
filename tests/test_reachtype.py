"""The vectorised MB1997 classifier against the spec's reference implementation and its tests."""

import math
import random
import sys
from importlib import resources
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from river_finder import reachtype as RT

sys.path.insert(0, str(Path(__file__).parent))
import mb_classifier_ref as ref  # noqa: E402

SPEC = RT.load_spec()
CLASSES = SPEC["classes"]
EXAMPLE = {"reach_id": "example-01", "slope": 0.045, "width_depth_ratio": 11, "relative_submergence": 4.5,
           "pools_present": "true", "pool_spacing": 2.2, "bedform_pattern": "vertically_oscillatory",
           "bed_material_class": "boulder", "confinement": "confined", "substrate_origin": "alluvial",
           "obstruction_forced": False, "multithread": False}


def _vec(reaches: list[dict], track: str) -> dict:
    keys = {k for r in reaches for k in r if k != "reach_id"}
    inputs = {}
    for k in keys:
        vals = [r.get(k) for r in reaches]
        if k in SPEC["numeric_features"]:
            inputs[k] = np.array([np.nan if v is None else float(v) for v in vals])
        else:
            inputs[k] = np.array(vals, dtype=object)
    return RT.classify_arrays(inputs, track=track)


def test_spec_is_the_pdf_attachment():
    assert SPEC["spec_version"] == "mb1997-ops-1.0.0"
    assert resources.files("river_finder").joinpath("assets/mb1997/mb1997_spec.json").is_file()


@pytest.mark.parametrize("track,expected", [
    ("A_full", {"colluvial": 0.0, "bedrock": 0.0, "cascade": 0.009, "step_pool": 0.991, "plane_bed": 0.0,
                "pool_riffle": 0.0, "dune_ripple": 0.0}),
    ("B_proxy", {"colluvial": 0.011, "bedrock": 0.041, "cascade": 0.353, "step_pool": 0.561, "plane_bed": 0.034,
                 "pool_riffle": 0.001, "dune_ripple": 0.0}),
])
def test_worked_example(track, expected):
    """Spec test 2: the worked example to 3 decimals, in both implementations."""
    out = ref.classify(EXAMPLE, SPEC, track=track)
    assert {c: round(p, 3) for c, p in out["probabilities"].items()} == expected
    v = _vec([EXAMPLE], track)
    assert {c: round(float(v["probs"][0, j]), 3) for j, c in enumerate(CLASSES)} == expected
    assert CLASSES[v["pred"][0]] == "step_pool"


def _canonical(c: str) -> dict:
    r = {"reach_id": c}
    for name, feat in SPEC["numeric_features"].items():
        p = feat["class_params"][c]
        if p is not None:
            r[name] = math.sqrt(p["q25"] * p["q75"])
    for name, feat in SPEC["categorical_features"].items():
        dv = feat["diagnostic_values"][c]
        if isinstance(dv, list) and dv:
            r[name] = dv[0]
    r["pools_present"] = "false" if c == "plane_bed" else r.get("pools_present", "true")
    return r


def test_canonical_reaches():
    """Spec test 1: each canonical reach is its own type under Track A."""
    reaches = [_canonical(c) for c in CLASSES]
    for c, r in zip(CLASSES, reaches, strict=True):
        assert ref.classify(r, SPEC, "A_full")["predicted_class"] == c
    v = _vec(reaches, "A_full")
    assert [CLASSES[i] for i in v["pred"]] == CLASSES


def test_rows_sum_to_one():
    """Spec test 3."""
    for feat in SPEC["categorical_features"].values():
        for row in feat["likelihood"].values():
            assert abs(sum(row.values()) - 1.0) < 1e-4
    v = _vec([EXAMPLE, _canonical("bedrock")], "A_full")
    assert np.allclose(v["probs"].sum(axis=1), 1.0)


def test_random_reaches_match_reference():
    rng = random.Random(7)
    reaches = []
    for i in range(300):
        r = {"reach_id": str(i), "slope": 10 ** rng.uniform(-4, 0), "confinement": rng.choice(
            ["confined", "moderately_confined", "unconfined", None])}
        if rng.random() < 0.5:
            r["width_depth_ratio"] = 10 ** rng.uniform(0, 2)
        if rng.random() < 0.3:
            r["pools_present"] = rng.choice(["true", "false"])
            r["pool_spacing"] = 10 ** rng.uniform(-1, 1)
            r["bedform_pattern"] = rng.choice(SPEC["categorical_features"]["bedform_pattern"]["values"])
        r["obstruction_forced"] = rng.random() < 0.2
        reaches.append(r)
    for track in ("A_full", "B_proxy"):
        v = _vec(reaches, track)
        for n, r in enumerate(reaches):
            o = ref.classify(r, SPEC, track=track)
            assert np.allclose([o["probabilities"][c] for c in CLASSES], v["probs"][n], atol=1e-9)
            assert CLASSES[v["pred"][n]] == o["predicted_class"]
            assert bool(v["transitional"][n]) == o["flags"]["transitional"]
            assert bool(v["low_evidence"][n]) == o["flags"]["low_evidence"]


def test_nz_frame():
    f = pd.DataFrame({"grad_s": [0.0, 5.0, 45.0, 150.0, 400.0], "maf_m3s": [500.0, 100.0, 20.0, 5.0, 1.0],
                      "valley_width_m": [2000.0, 300.0, 16.0, 8.0, 8.0]})
    out = RT.classify_frame(f)
    assert list(out["reach_type"]) == ["dune_ripple", "pool_riffle", "step_pool", "cascade", "colluvial"]
    assert set(out["style"].cat.categories) == set(RT.styles())
    assert np.allclose(out[[f"p_{c}" for c in CLASSES]].sum(axis=1), 1.0, atol=1e-5)
