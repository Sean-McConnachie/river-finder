"""Reference implementation of mb1997_spec.json (spec_version mb1997-ops-1.0.0).

Usage:
    spec = load_spec("mb1997_spec.json")
    out = classify(reach_dict, spec, track="A_full")
    aucs = ovr_auc(y_true_list, prob_dicts, spec["classes"])

Standard library only.
"""
import json
import math
import random


def load_spec(path):
    with open(path) as fh:
        return json.load(fh)


def _numeric_membership(x, params, sigma_min, neutral):
    if params is None:
        return neutral
    z = (math.log10(x) - params["log10_mu"]) / max(params["log10_sigma"], sigma_min)
    return math.exp(-0.5 * z * z)


def classify(reach, spec, track="A_full", prior=None):
    classes = spec["classes"]
    model = spec["scoring_model"]
    allowed = set(spec["tracks"][track]["features"])
    sigma_min = model["sigma_min_log10"]
    neutral = model["neutral_membership_if_class_param_null"]
    prior = prior or {c: 1.0 / len(classes) for c in classes}
    scores = {c: math.log(prior[c]) for c in classes}
    used, used_weight = [], 0.0

    for name, feat in spec["numeric_features"].items():
        if name not in allowed:
            continue
        x = reach.get(name)
        if x is None or x <= 0:
            continue
        if name == "pool_spacing" and reach.get("pools_present") != "true":
            continue
        for c in classes:
            params = feat["class_params"][c]
            if (name == "slope" and c == "pool_riffle"
                    and reach.get("obstruction_forced") is True):
                lo, hi = math.log10(0.002), math.log10(0.03)
                params = {"log10_mu": (lo + hi) / 2,
                          "log10_sigma": max((hi - lo) / 1.349, sigma_min)}
            m = _numeric_membership(x, params, sigma_min, neutral)
            scores[c] += feat["weight"] * math.log(max(m, 1e-300))
        used.append(name)
        used_weight += feat["weight"]

    for name, feat in spec["categorical_features"].items():
        if name not in allowed:
            continue
        v = reach.get(name)
        if v is None:
            continue
        v = str(v).lower()
        if v not in feat["values"]:
            raise ValueError(f"{name}: '{v}' not in {feat['values']}")
        for c in classes:
            scores[c] += feat["weight"] * math.log(feat["likelihood"][c][v])
        used.append(name)
        used_weight += feat["weight"]

    top = max(scores.values())
    expd = {c: math.exp(s - top) for c, s in scores.items()}
    z = sum(expd.values())
    probs = {c: expd[c] / z for c in classes}
    ranked = sorted(classes, key=lambda c: (-probs[c], classes.index(c)))
    pred, second = ranked[0], ranked[1]
    adj = spec["adjacent_classes"]["map"]
    forced = None
    if reach.get("obstruction_forced") is True:
        forced = {"pool_riffle": "forced_pool_riffle",
                  "step_pool": "forced_step_pool"}.get(pred)
    return {
        "reach_id": reach.get("reach_id"),
        "spec_version": spec["spec_version"],
        "track": track,
        "probabilities": probs,
        "log_scores": scores,
        "predicted_class": pred,
        "flags": {
            "transitional": (probs[pred] - probs[second] < 0.15) and second in adj[pred],
            "forced_variant": forced,
            "out_of_scope": "braided" if reach.get("multithread") is True else None,
            "low_evidence": used_weight < 1.5,
        },
        "features_used": used,
    }


# --------------------------------------------------------------------------- AUC
def binary_auc(labels, scores):
    """Mann-Whitney AUC; ties count 0.5. labels are 0/1."""
    pairs = sorted(zip(scores, labels))
    n_pos = sum(labels)
    n_neg = len(labels) - n_pos
    if n_pos == 0 or n_neg == 0:
        return None
    rank_sum, i = 0.0, 0
    while i < len(pairs):
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            j += 1
        avg_rank = (i + 1 + j) / 2.0
        rank_sum += avg_rank * sum(l for _, l in pairs[i:j])
        i = j
    return (rank_sum - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def ovr_auc(y_true, prob_dicts, classes):
    per_class = {}
    for c in classes:
        labels = [1 if y == c else 0 for y in y_true]
        scores = [p[c] for p in prob_dicts]
        per_class[c] = {"auc": binary_auc(labels, scores),
                        "n_pos": sum(labels), "n_neg": len(labels) - sum(labels)}
    defined = [v for v in per_class.values() if v["auc"] is not None]
    macro = sum(v["auc"] for v in defined) / len(defined) if defined else None
    n = sum(v["n_pos"] for v in defined)
    weighted = sum(v["auc"] * v["n_pos"] for v in defined) / n if n else None
    return {"per_class": per_class, "macro": macro, "weighted": weighted}


def hand_till_m(y_true, prob_dicts, classes):
    present = [c for c in classes if c in set(y_true)]
    total, k = 0.0, 0
    for a in range(len(present)):
        for b in range(a + 1, len(present)):
            ci, cj = present[a], present[b]
            idx = [n for n, y in enumerate(y_true) if y in (ci, cj)]
            lab = [1 if y_true[n] == ci else 0 for n in idx]
            a_ij = binary_auc(lab, [prob_dicts[n][ci] for n in idx])
            a_ji = binary_auc([1 - l for l in lab], [prob_dicts[n][cj] for n in idx])
            total += (a_ij + a_ji) / 2.0
            k += 1
    return total / k if k else None


def bootstrap_macro_auc(y_true, prob_dicts, classes, n_boot=1000, seed=0):
    rng = random.Random(seed)
    by_class = {}
    for n, y in enumerate(y_true):
        by_class.setdefault(y, []).append(n)
    stats = []
    for _ in range(n_boot):
        idx = [rng.choice(members) for members in by_class.values() for _ in members]
        m = ovr_auc([y_true[i] for i in idx], [prob_dicts[i] for i in idx], classes)["macro"]
        if m is not None:
            stats.append(m)
    stats.sort()
    return stats[int(0.025 * len(stats))], stats[int(0.975 * len(stats)) - 1]
