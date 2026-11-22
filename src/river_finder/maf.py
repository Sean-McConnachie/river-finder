"""Mean annual flood (MAF) sanity correction (rf-xyk.6; findings in ``docs/audits/maf-outliers-2026-11.md``).

``reaches._attach_flood`` sets ``maf_m3s`` = our area x the specific MAF (m3/s/km2: Henderson & Collins 2018
MAF / REC1 area) of the nearest REC1 reach within 400 m whose area is 0.4-2.5 times ours. Two things go wrong:

- **Unmatched reaches** (``specific_maf_matched`` False: 11 % of the reaches of 2 km2 or more, 30 % of those
  over 1,000 km2) get the median specific MAF of their whole tile, whatever their size. Specific MAF falls
  with area, so big rivers get floods several times too large (the lower Buller: 45,000 m3/s against 4,900
  gauged). ``refill`` gives such a reach the specific MAF of the nearest matched reaches on our own network
  (up the main stem and downstream), scaled to its area by ``area ** -b`` and weighted by closeness in log
  area. On held-out runs of matched reaches this is within 1.5x of the REC1 value 98 % of the time; the
  tile median manages 41 %.
- **Values above anything gauged in NZ.** H&C extrapolates to 15-45 m3/s/km2 on small, steep, wet
  catchments (Mt Taranaki, the Southern Alps, the Tararua and Raukumara ranges). ``cap`` limits specific
  MAF to the envelope ``c * area ** -b`` of the 634 gauges behind H&C 2018 (``c``: the highest
  ``MAF / A * A ** b`` among gauges of 1 km2 or more, set by Whataroa at SH bridge; Cropp at Gorge and
  Hokitika at Gorge come next).

``maf_flag`` records what changed and why: ``refill``, ``cap`` (``refill+cap`` for both), ``drop`` (flag
only: the MAF falls by more than ``drop_tol`` to the reach below) and ``monotone`` (only with ``monotone =
true``: lowered to the MAF of the reach below). ``maf_raw_m3s`` keeps the uncorrected value, so applying
again, with other settings, or reverting always starts from it.

The switch is ``[correction] apply`` in ``config/maf.toml`` (default off) or ``--maf-correction on|off`` on
the command line (``set_override``). When on, the correction runs in ``scoring.prepare`` (reference and
national scoring: ``segments_scored.gpkg``, the viewer and the run finder) and on the whole-NZ network in
``ml.ml_table`` (features, stream power, reach styles, CV and "gbm" cards).
"""

from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

import numba
import numpy as np
import pandas as pd

from .regions import REPO_ROOT

CONFIG_PATH = REPO_ROOT / "config" / "maf.toml"
FLAGS = ("refill", "cap", "drop", "monotone")
_OVERRIDE: bool | None = None


@dataclass(frozen=True)
class MafConfig:
    apply: bool = False            # default off: on changes maf_m3s, stream power, reach styles and every score
    refill: bool = True            # unmatched reaches: network neighbours instead of the tile median
    cap: bool = True               # specific MAF <= envelope_margin * envelope_c * area ** -envelope_b
    monotone: bool = False         # also lower a reach's MAF to that of the reach below it
    envelope_c: float = 27.2       # m3/s/km2 at 1 km2 (fit_envelope over the H&C gauges of 1 km2 or more)
    envelope_b: float = 0.25       # specific MAF ~ area ** -b (MAF ~ area ** 0.75)
    envelope_margin: float = 1.0
    drop_tol: float = 0.10         # "drop": MAF of the reach below < (1 - drop_tol) x this reach's

    def __post_init__(self):
        if not self.envelope_c > 0 or not self.envelope_margin > 0:
            raise ValueError("maf config: envelope_c and envelope_margin must be > 0")
        if not 0.0 <= self.envelope_b < 1.0:
            raise ValueError("maf config: envelope_b must be in [0, 1)")
        if not 0.0 <= self.drop_tol < 1.0:
            raise ValueError("maf config: drop_tol must be in [0, 1)")


def set_override(apply: bool | None) -> None:
    """Force ``apply`` on (True) or off (False) for this process, whatever the config file says (the
    ``--maf-correction`` command-line switch); None goes back to the file."""
    global _OVERRIDE
    _OVERRIDE = apply


def load_config(path: Path | None = None) -> MafConfig:
    """``[correction]`` of ``config/maf.toml`` (defaults when the file or a key is missing)."""
    p = Path(path) if path else CONFIG_PATH
    d = {}
    if p.exists():
        with open(p, "rb") as fh:
            d = tomllib.load(fh).get("correction", {})
    known = {f.name for f in fields(MafConfig)}
    if set(d) - known:
        raise ValueError(f"maf config {p}: unknown keys {sorted(set(d) - known)}")
    cfg = MafConfig(**d)
    return replace(cfg, apply=_OVERRIDE) if _OVERRIDE is not None else cfg


# ---------------------------------------------------------------------------- envelope
def envelope(area_km2, c: float = MafConfig.envelope_c, b: float = MafConfig.envelope_b) -> np.ndarray:
    """Ceiling on specific MAF (m3/s/km2) at each area: ``c * area ** -b`` (area floored at 0.001 km2)."""
    return c * np.maximum(np.asarray(area_km2, dtype=float), 1e-3) ** -b


def fit_envelope(area_km2, maf_m3s, b: float = MafConfig.envelope_b, min_area_km2: float = 1.0) -> float:
    """The smallest ``c`` with ``MAF / A <= c * A ** -b`` at every gauge of ``min_area_km2`` or more."""
    a, q = np.asarray(area_km2, dtype=float), np.asarray(maf_m3s, dtype=float)
    ok = np.isfinite(a) & np.isfinite(q) & (a >= min_area_km2) & (q > 0)
    if not ok.any():
        raise ValueError("fit_envelope: no gauge of min_area_km2 or more with a positive MAF")
    return float(np.max(q[ok] / a[ok] * a[ok] ** b))


# ---------------------------------------------------------------------------- network
@numba.njit(cache=True)
def _order(down):
    """Rows upstream to downstream (Kahn); rows on a cycle are left out."""
    n = len(down)
    indeg = np.zeros(n, np.int64)
    for i in range(n):
        if down[i] >= 0:
            indeg[down[i]] += 1
    order = np.empty(n, np.int64)
    head, tail = 0, 0
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
def _upstream_main(down, area):
    up = np.full(len(down), -1, np.int64)
    best = np.full(len(down), -1.0)
    for i in range(len(down)):
        d = down[i]
        if d >= 0 and area[i] > best[d]:
            best[d] = area[i]
            up[d] = i
    return up


@numba.njit(cache=True)
def _nearest_ok(down, up, order, ok):
    """For every row, the nearest ``ok`` row up the main stem (``rup``) and downstream (``rdn``), -1 if none.
    A row that is ``ok`` is its own nearest."""
    n = len(down)
    rup = np.full(n, -1, np.int64)
    rdn = np.full(n, -1, np.int64)
    for k in range(len(order)):
        i = order[k]
        if ok[i]:
            rup[i] = i
        elif up[i] >= 0:
            rup[i] = rup[up[i]]
    for k in range(len(order) - 1, -1, -1):
        i = order[k]
        if ok[i]:
            rdn[i] = i
        elif down[i] >= 0:
            rdn[i] = rdn[down[i]]
    return rup, rdn


@numba.njit(cache=True)
def _clamp_down(down, order, q):
    """Lower each row's value to that of the row below, from the outlets up. Returns the changed rows."""
    changed = np.zeros(len(q), np.bool_)
    for k in range(len(order) - 1, -1, -1):
        i = order[k]
        d = down[i]
        if d >= 0 and np.isfinite(q[d]) and q[i] > q[d] * (1.0 + 1e-9):
            q[i] = q[d]
            changed[i] = True
    return changed


def _refill_values(area, spec, rup, rdn, b: float) -> np.ndarray:
    """Specific MAF from the two network neighbours, each scaled by ``(A / A_ref) ** -b`` and weighted by
    ``1 / (|ln(A / A_ref)| + 0.05)`` (geometric mean). NaN where neither exists."""
    num, den = np.zeros(len(area)), np.zeros(len(area))
    for ref in (rup, rdn):
        ok = ref >= 0
        r = np.maximum(ref, 0)
        la = np.log(np.maximum(area, 1e-6) / np.maximum(area[r], 1e-6))
        w = np.where(ok, 1.0 / (np.abs(la) + 0.05), 0.0)
        ls = np.where(ok, np.log(np.where(ok, spec[r], 1.0)) - b * la, 0.0)
        num += w * ls
        den += w
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(den > 0, np.exp(num / np.where(den > 0, den, 1.0)), np.nan)


# ---------------------------------------------------------------------------- correction
@dataclass
class Correction:
    maf: np.ndarray            # corrected MAF, m3/s
    specific: np.ndarray       # corrected specific MAF, m3/s/km2
    flag: np.ndarray           # "" or "+"-joined FLAGS
    bits: dict                 # flag name -> bool array


def correct(area_km2, maf_m3s, matched=None, down=None, cfg: MafConfig | None = None) -> Correction:
    """Corrected MAF for reaches with area ``area_km2`` and raw MAF ``maf_m3s``.

    ``matched``: whether each reach's specific MAF came from a REC1 reach (None: all did, so nothing is
    refilled). ``down``: row index of the reach below (-1 none; None: no network, so no refill and no drop
    flags). A matched reach with a zero or missing MAF is refilled as well. ``cfg.apply`` is ignored: this
    always corrects (``apply`` decides whether the pipeline calls it)."""
    cfg = cfg or MafConfig()
    area = np.asarray(area_km2, dtype=float)
    q_raw = np.asarray(maf_m3s, dtype=float)
    n = len(area)
    with np.errstate(invalid="ignore", divide="ignore"):
        spec = np.where(area > 0, q_raw / np.where(area > 0, area, 1.0), np.nan)
    good = np.isfinite(spec) & (spec > 0)
    src = (np.ones(n, bool) if matched is None else np.asarray(matched, dtype=bool)) & good
    if down is None:
        dn = np.full(n, -1, np.int64)
    else:
        dn = np.asarray(down, dtype=np.int64)
        dn = np.where((dn >= 0) & (dn < n), dn, -1)
    E = envelope(area, cfg.envelope_c * cfg.envelope_margin, cfg.envelope_b)
    s = spec.copy()
    bits = {k: np.zeros(n, bool) for k in FLAGS}
    if cfg.cap:   # cap the sources first, so a refill never borrows an implausible value
        over = src & (s > E)
        s[over] = E[over]
        bits["cap"] |= over
    order = _order(dn) if down is not None else np.arange(n, dtype=np.int64)
    if cfg.refill and down is not None and (~src).any():
        rup, rdn = _nearest_ok(dn, _upstream_main(dn, area), order, src)
        v = _refill_values(area, s, rup, rdn, cfg.envelope_b)
        fill = ~src & np.isfinite(v)
        s[fill] = v[fill]
        bits["refill"] = fill
    if cfg.cap:
        over = np.isfinite(s) & (s > E) & ~bits["cap"]
        s[over] = E[over]
        bits["cap"] |= over
    q = s * area
    q = np.where(np.isfinite(q), q, q_raw)
    if down is not None:
        has = dn >= 0
        d = np.maximum(dn, 0)
        bits["drop"] = has & (area[d] >= area) & (q[d] < (1.0 - cfg.drop_tol) * q)
        if cfg.monotone:
            bits["monotone"] = _clamp_down(dn, order, q)
    with np.errstate(invalid="ignore", divide="ignore"):
        s = np.where(area > 0, q / np.where(area > 0, area, 1.0), spec)
    flag = np.full(n, "", dtype=object)
    for k in FLAGS:
        m = bits[k]
        flag[m] = np.where(flag[m] == "", k, flag[m] + "+" + k)
    return Correction(q, s, flag, bits)


def _power(f: pd.DataFrame) -> None:
    if "grad_s" in f.columns and "power_kw_m" in f.columns:
        from .scoring import RHO_G

        f["power_kw_m"] = RHO_G * f["maf_m3s"] * f["grad_s"] / 1000.0 / 1000.0


def apply(f: pd.DataFrame, cfg: MafConfig | None = None, down: str = "down", matched=None) -> pd.DataFrame:
    """Correct ``f`` in place and return it: ``maf_m3s`` (from ``maf_raw_m3s`` when ``f`` was corrected
    before), ``specific_maf`` if present, ``maf_flag``, ``maf_raw_m3s`` and ``power_kw_m`` (if present).
    ``down`` names the downstream-row column (``down`` in the national table, ``down_seg`` in a region
    frame). ``matched`` defaults to ``f["specific_maf_matched"]`` (all matched if absent)."""
    cfg = cfg or load_config()
    raw = (f["maf_raw_m3s"] if "maf_raw_m3s" in f.columns else f["maf_m3s"]).to_numpy(dtype=float).copy()
    if matched is None and "specific_maf_matched" in f.columns:
        matched = f["specific_maf_matched"].fillna(True).to_numpy(dtype=bool)
    dn = f[down].to_numpy() if down in f.columns else None
    r = correct(f["area_km2"].to_numpy(dtype=float), raw, matched, dn, cfg)
    f["maf_raw_m3s"] = raw
    f["maf_m3s"] = np.round(r.maf, 2)
    if "specific_maf" in f.columns:
        f["specific_maf"] = np.round(r.specific, 4)
    f["maf_flag"] = pd.Categorical(r.flag)
    _power(f)
    return f


def revert(f: pd.DataFrame) -> pd.DataFrame:
    """Undo ``apply`` in place (no-op on a frame that was never corrected) and return ``f``."""
    if "maf_raw_m3s" not in f.columns:
        return f
    f["maf_m3s"] = f["maf_raw_m3s"].to_numpy(dtype=float)
    if "specific_maf" in f.columns:
        a = f["area_km2"].to_numpy(dtype=float)
        with np.errstate(invalid="ignore", divide="ignore"):
            f["specific_maf"] = np.round(np.where(a > 0, f["maf_m3s"].to_numpy() / np.where(a > 0, a, 1.0), np.nan), 4)
    for c in ("maf_raw_m3s", "maf_flag"):
        if c in f.columns:
            del f[c]
    _power(f)
    return f


def summary(f: pd.DataFrame, min_area_km2: float = 2.0) -> dict:
    """Counts of each flag among reaches of ``min_area_km2`` or more, and how far MAF moved."""
    if "maf_flag" not in f.columns:
        return {"corrected": False}
    big = (f["area_km2"] >= min_area_km2).to_numpy()
    fl = f["maf_flag"].astype(str).to_numpy()
    out = {"corrected": True, "reaches": int(big.sum())}
    for k in FLAGS:
        out[k] = int(sum(1 for v in fl[big] if k in v.split("+")))
    raw, new = f["maf_raw_m3s"].to_numpy(dtype=float)[big], f["maf_m3s"].to_numpy(dtype=float)[big]
    ch = np.isfinite(raw) & np.isfinite(new) & (raw > 0) & (np.abs(new - raw) > 0.011)
    out["changed"] = int(ch.sum())
    if ch.any():
        r = new[ch] / raw[ch]
        out["ratio_new_raw"] = {"p05": round(float(np.quantile(r, 0.05)), 3), "median": round(float(np.median(r)), 3),
                                "p95": round(float(np.quantile(r, 0.95)), 3)}
    return out


def config_dict(cfg: MafConfig | None = None) -> dict:
    return asdict(cfg or load_config())


def matched_from_tiles(t: pd.DataFrame, tiles_dir: Path) -> np.ndarray:
    """``specific_maf_matched`` for a national table that lacks it (tables cached before rf-xyk.6), read from
    ``<tiles_dir>/*/segments_core.gpkg`` by ``gid``. Reaches not found count as matched (left alone)."""
    import pyogrio

    parts = [pyogrio.read_dataframe(p, columns=["gid", "specific_maf_matched"], read_geometry=False)
             for p in sorted(Path(tiles_dir).glob("*/segments_core.gpkg"))]
    if not parts:
        return np.ones(len(t), bool)
    m = pd.concat(parts, ignore_index=True).drop_duplicates("gid").set_index("gid")["specific_maf_matched"]
    return t["gid"].map(m).fillna(True).to_numpy(dtype=bool)
