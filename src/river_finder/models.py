"""Versioned models: checkpoint, train, evaluate, and per-model score files for the viewer.

Every model is one JSON *card* in ``models/<id>.json`` (committed). A card is frozen once written:
``save_card`` refuses to replace a card whose weights differ. A card holds

- the weights: feature list, coefficients, intercept, standardisation mean/std;
- ``pool_quantiles``: 1001 quantiles of the model's logit over every reach of 2 km2 or more in NZ,
  so any reach (national 8 m or reference-area 5 m) gets a 0-100 national percentile;
- the labels it saw: ``train_sections`` (used to fit), ``test_sections`` (held out) and
  ``train_rivers`` / ``test_rivers`` (river keys "River name | region").

Splits (``models/splits/<name>.json``) hold whole rivers, never single reaches, so a river is
never in both sets. ``evaluate`` scores a card on a split's test rivers. Results for every card
on every split go to ``models/evaluations.json``, which the viewer's Models page shows.

Labels are positive-unlabelled: a reach not on a known run is "unlabelled", not "not runnable".
Training uses unlabelled reaches as negatives (as v1 did). Evaluation ranks test rivers against
the unlabelled background, which still holds unknown runs, so AUC and recall are lower bounds.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from pyproj import Transformer
from scipy.spatial import cKDTree

from .labels import Section, load_sections, one_path, parse_grade, pieces_note, river_outlet
from .national import FILTER_ORDER, data_kind, is_1m, nz_dir
from .regions import REPO_ROOT, list_regions, load_region
from .scoring import FEATURES, _standardise, logistic_fit

MODELS_ENV = "RIVER_FINDER_MODELS"     # another registry directory (a sample rebuild must not write models/)
MODELS_DIR = Path(os.environ.get(MODELS_ENV) or REPO_ROOT / "models")
SPLITS_DIR = MODELS_DIR / "splits"
PCT_GRID = np.linspace(0.0, 100.0, 1001)
SEED = 20261113

_RAW = ["grad_s", "steepest_50m_m_per_km", "ksn_s", "power_kw_m", "maf_m3s", "gorge_depth_m", "head_m",
        "upstream_flat_frac", "upstream_hard_frac", "bed_hard", "knick_ratio", "melton", "height_to_width"]
_MAF_COLS = ["specific_maf_matched", "maf_raw_m3s", "maf_flag"]   # kept when present (maf.py, rf-xyk.6)
# the 1 m rebuild's fields (docs/lidar-extraction-spec.md) that reach segments_table.pkl when the scored regions have
# them; the x1_* section summaries join in ml_table (x1.arrow)
FIELDS_1M = ["tile", "z_top", "z_bot", "gradient_m_per_km", "ksn", "relief_m", "area_mid_km2", "local_km2",
             "area_tile_km2", "area_check", "steepest_100m_m_per_km", "steepest_200m_m_per_km", "steep50_water",
             "longest_flat_m", "steps_n", "steps_drop_m", "step_max_m", "step_max_s_m", "steps_share", "drops_n",
             "falls1m_n", "risers_n", "hand_w1_m", "hand_w2_m", "hand_w10_m", "hand_w20_m", "upstream_lake_frac",
             "catch_lidar_share", "karst_up_frac", "sinuosity", "sinuosity_1km", "dem_source", "lidar_share",
             "dem_res_m", "seam_dist_m", "lake", "lake_share", "lake_id", "water_flat_share", "water_planar_share",
             "stitch", "outlet_kind", "truncated", "missing_up_km2", "rec2_nzsegment", "rec2_area_km2",
             "rec2_area_at_km2", "rec2_dist_m", "river_rec2", "river_linz", "linz_dist_m", "linz_section_id",
             "rec1_nzreach", "specific_maf", "bed_rock", "lidar_survey", "lidar_year", "lidar_year_start"]
_TO_LL = Transformer.from_crs("EPSG:2193", "EPSG:4326", always_xy=True)
_TO_NZTM = Transformer.from_crs("EPSG:4326", "EPSG:2193", always_xy=True)


# ---------------------------------------------------------------------------- reach frames
def _rslug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower().replace("'", "")).strip("-")


def norm_river(name) -> str:
    """'Kaituna River' / 'kaituna' / 'Kaitūna R.' -> 'kaituna' (for matching research names to REC2)."""
    s = str(name or "").lower()
    for a, b in zip("āēīōū", "aeiou", strict=True):
        s = s.replace(a, b)
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"[^a-z ]", " ", s)
    s = re.sub(r"\b(river|stream|creek|burn|r|str|ck|awa|the|branch|north|south|east|west|left|right|true|"
               r"upper|lower|middle)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _upstream_main(down: np.ndarray, area: np.ndarray) -> np.ndarray:
    up, best = np.full(len(down), -1), np.full(len(down), -1.0)
    for i, d in enumerate(down):
        if d >= 0 and area[i] > best[d]:
            best[d], up[d] = area[i], i
    return up


def _finish(f: pd.DataFrame, x, y) -> pd.DataFrame:
    f = f.reset_index(drop=True)
    f["x"], f["y"] = x, y
    f["lon"], f["lat"] = _TO_LL.transform(np.asarray(x), np.asarray(y))
    f["up"] = _upstream_main(f["down"].to_numpy(), f["area_km2"].to_numpy())
    f["rnorm"] = [norm_river(r) for r in f["river"].fillna("")]
    return f


def national_table(rebuild: bool = False, log=print) -> pd.DataFrame:
    """Every national reach (no geometry, midpoint x/y), cached in ``data/nz/segments_table.pkl``.

    ``down`` / ``up`` are row indices across all of NZ; ``vi`` is the reach's index in its region's
    viewer arrays (reaches of 2 km2 or more, in file order), -1 if not shown.
    """
    import pyogrio

    cache = nz_dir() / "segments_table.pkl"
    if cache.exists() and not rebuild:
        return pd.read_pickle(cache)
    cols = ["seg_id", "gid", "down_gid", "river", "area_km2", "length_m", "score_regression", "label", "holdout",
            *_RAW, "valley_width_m"]
    parts, xs, ys = [], [], []
    for name in FILTER_ORDER:
        p = nz_dir() / "regions" / _rslug(name) / "segments_scored.gpkg"
        if not p.exists():
            continue
        have = set(pyogrio.read_info(p)["fields"])
        g = pyogrio.read_dataframe(p, columns=cols + [c for c in _MAF_COLS + FIELDS_1M if c in have])
        mid = g.geometry.interpolate(0.5, normalized=True)
        f = pd.DataFrame(g.drop(columns="geometry"))
        f["region"] = name
        big = (f["area_km2"] >= 2.0).to_numpy()
        f["vi"] = np.where(big, np.cumsum(big) - 1, -1)
        parts.append(f)
        xs.append(mid.x.to_numpy())
        ys.append(mid.y.to_numpy())
        log(f"table: {name} {len(f)} reaches")
    t = pd.concat(parts, ignore_index=True)
    pos = pd.Series(np.arange(len(t)), index=t["gid"].to_numpy())
    pos = pos[~pos.index.duplicated()]
    t["down"] = t["down_gid"].map(pos).fillna(-1).astype(np.int64).to_numpy()
    t = _finish(t, np.concatenate(xs), np.concatenate(ys))
    t["guide_region"] = t["region"]
    for c in ("steep50_water", "lake", "truncated"):   # GeoPackage booleans come back as int or object
        if c in t:
            t[c] = t[c].fillna(False).astype(bool)
    if is_1m():
        log("table: 1 m rebuild: the merge's network is final (stitch, outlet_kind, truncated); no relink")
    elif (nz_dir() / "tiles").exists() and (nz_dir() / "rec2.gpkg").exists():
        # ``nz score`` already relinks (nz.merge_segments); this catches segments_scored.gpkg files written
        # before it did. A second pass links nothing new.
        from .relink import relink_table

        t, lg = relink_table(t, nz_dir() / "tiles", nz_dir() / "rec2.gpkg", log=log)
        lg.to_csv(nz_dir() / "segments_table_relink_log.csv", index=False)
    t.to_pickle(cache)
    return t


def reference_frame(rid: str, guide_regions: bool = True) -> pd.DataFrame:
    """A 5 m reference area's reaches in the same layout (``vi``: viewer index, reaches >= 1 km2).
    ``guide_regions=False`` skips the guide-region lookup (it needs the national council layer)."""
    import pyogrio

    reg = load_region(rid)
    g = pyogrio.read_dataframe(reg.data_dir() / "segments_scored.gpkg",
                               columns=["seg_id", "down_seg", "river", "area_km2", "length_m", *_RAW,
                                        "valley_width_m"])
    mid = g.geometry.interpolate(0.5, normalized=True)
    f = pd.DataFrame(g.drop(columns="geometry"))
    f["down"] = f["down_seg"].astype(np.int64)
    big = (f["area_km2"] >= 1.0).to_numpy()
    f["vi"] = np.where(big, np.cumsum(big) - 1, -1)
    f["region"] = rid
    f = _finish(f, mid.x.to_numpy(), mid.y.to_numpy())
    if guide_regions:
        from .national import assign_regions

        f["guide_region"] = assign_regions(mid.reset_index(drop=True))
    else:
        f["guide_region"] = ""
    return f


def features(f: pd.DataFrame, names: list[str]) -> np.ndarray:
    X = np.column_stack([np.asarray(FEATURES[n][0](f), dtype=float) for n in names])
    return np.nan_to_num(X, nan=0.0)


# ---------------------------------------------------------------------------- section mapping
@dataclass
class Mapped:
    section: Section
    key: str            # river key: "River | region"
    rows: np.ndarray    # rows of the frame the section covers
    method: str         # "put-in to take-out", "outlet + length", "name", "name (1 of 3 pieces)", ...
    info: dict = field(default_factory=dict)   # name rule: what ``labels.one_path`` dropped


def river_key(s: Section) -> str:
    return f"{s.river} | {s.region}"


class _Index:
    def __init__(self, f: pd.DataFrame):
        self.f = f
        self.ok = np.flatnonzero((f["area_km2"] >= 1.0).to_numpy())
        self.tree = cKDTree(np.column_stack([f["x"].to_numpy()[self.ok], f["y"].to_numpy()[self.ok]]))
        self.down = f["down"].to_numpy()
        self.up = f["up"].to_numpy()
        self.rnorm = f["rnorm"].to_numpy()
        self.xy = np.column_stack([f["x"].to_numpy(), f["y"].to_numpy()])
        codes, uniq = pd.factorize(f["rnorm"])
        order = np.argsort(codes, kind="stable")
        bounds = np.searchsorted(codes[order], np.arange(len(uniq) + 1))
        self.by_name = {u: order[bounds[i]:bounds[i + 1]] for i, u in enumerate(uniq) if u}

    def nearest(self, lonlat, river: str, max_m: float = 700.0, named_m: float = 3000.0) -> int:
        x, y = _TO_NZTM.transform(*lonlat)
        named = self.by_name.get(norm_river(river)) if river else None
        if named is not None and len(named):
            named = named[self.f["area_km2"].to_numpy()[named] >= 1.0]
            d = np.hypot(self.f["x"].to_numpy()[named] - x, self.f["y"].to_numpy()[named] - y)
            if len(d) and d.min() <= named_m:
                return int(named[np.argmin(d)])
        d, k = self.tree.query([x, y], k=1)
        return int(self.ok[k]) if d <= max_m else -1

    def walk(self, a: int, b: int, limit: int = 4000) -> list[int]:
        out, cur = [], a
        while cur >= 0 and len(out) < limit:
            out.append(cur)
            if cur == b:
                return out
            cur = self.down[cur]
        return []

    def walk_towards(self, a: int, lonlat, tol_m: float, limit: int = 4000) -> list[int]:
        """Downstream from ``a``, stopping at the reach closest to the point (if within ``tol_m``).
        For take-outs placed only roughly, or snapped to a side creek."""
        x, y = _TO_NZTM.transform(*lonlat)
        fx, fy = self.f["x"].to_numpy(), self.f["y"].to_numpy()
        path, cur = [], a
        while cur >= 0 and len(path) < limit:
            path.append(cur)
            cur = self.down[cur]
        d = np.hypot(fx[path] - x, fy[path] - y)
        k = int(np.argmin(d))
        return path[:k + 1] if d[k] <= tol_m and k > 0 else []


def _name_rows(f: pd.DataFrame, s: Section, idx: _Index) -> np.ndarray:
    r = idx.by_name.get(norm_river(s.river), np.array([], dtype=np.int64))
    r = r[(f["grad_s"].to_numpy()[r] >= 10.0) & (f["area_km2"].to_numpy()[r] >= 2.0)]
    if s.bbox is not None:
        x0, y0, x1, y1 = s.bbox
        lon, lat = f["lon"].to_numpy()[r], f["lat"].to_numpy()[r]
        r = r[(lon >= x0) & (lon <= x1) & (lat >= y0) & (lat <= y1)]
    elif s.region in FILTER_ORDER:
        r = r[f["guide_region"].to_numpy()[r] == s.region]
    else:
        return np.array([], dtype=np.int64)
    return np.sort(r)


def snap_river(s: Section, point: str) -> str:
    """The river name a section's ``point`` ("put_in", "take_out" or "outlet") snaps to: the section's river,
    unless the label overrides it with ``<point>_river`` (another name, e.g. the main river a take-out sits on,
    or "" for the nearest reach whatever its name: where REC2 leaves the guide's reach unnamed)."""
    v = (s.extra or {}).get(f"{point}_river")
    return s.river if v is None else str(v)


def map_section(f: pd.DataFrame, s: Section, idx: _Index, info: dict | None = None) -> tuple[np.ndarray, str]:
    """The frame rows a section covers and how they were placed. Put-in and take-out (or outlet and
    length) follow the network; without a path, the name rule takes the steep reaches with the river's
    name and keeps one downstream path of them (``labels.one_path``: "name (1 of 3 pieces)"). ``info``,
    if given, receives what the name rule dropped (pieces, branches, dropped reaches and km). Points snap
    to the nearest reach with the river's name within 3 km, or to another name (``snap_river``)."""
    if s.put_in and s.take_out:
        a, b = idx.nearest(s.put_in, snap_river(s, "put_in")), idx.nearest(s.take_out, snap_river(s, "take_out"))
        length = s.extra.get("length_km") if s.extra else None
        path = (idx.walk(a, b) or idx.walk(b, a)) if a >= 0 and b >= 0 else []
        if path and (len(path) > 1 or not length):
            return np.array(path, dtype=np.int64), "put-in to take-out"
        if not path:
            x0, y0 = _TO_NZTM.transform(*s.put_in)
            x1, y1 = _TO_NZTM.transform(*s.take_out)
            tol = max(1500.0, 0.35 * float(np.hypot(x1 - x0, y1 - y0)))
            path = (idx.walk_towards(a, s.take_out, tol) if a >= 0 else []) or (
                idx.walk_towards(b, s.put_in, tol) if b >= 0 else [])
            if path:
                return np.array(path, dtype=np.int64), "put-in towards take-out"
    length = s.extra.get("length_km") if s.extra else None
    if s.put_in and length:
        a = idx.nearest(s.put_in, snap_river(s, "put_in"))
        if a >= 0:
            out, km, cur = [], 0.0, a
            L = f["length_m"].to_numpy()
            while cur >= 0 and km < float(length):
                out.append(cur)
                km += L[cur] / 1000.0
                cur = idx.down[cur]
            return np.array(out, dtype=np.int64), "put-in + length"
    if s.outlet and s.upstream_km:
        a = idx.nearest(s.outlet, snap_river(s, "outlet"))
        if a >= 0:
            out, km, cur = [], 0.0, a
            L = f["length_m"].to_numpy()
            while cur >= 0 and km < s.upstream_km:
                out.append(cur)
                km += L[cur] / 1000.0
                cur = idx.up[cur]
            return np.array(out, dtype=np.int64), "outlet + length"
    rows = _name_rows(f, s, idx)
    pts = [p for p in (s.put_in, s.take_out, s.outlet) if p]
    xy = np.array([_TO_NZTM.transform(*p) for p in pts]).reshape(-1, 2)
    how = "name"
    if pts:
        # coordinates given but the reaches between them did not join up: keep the name rule, but
        # only near the points given
        d = np.min(np.hypot(f["x"].to_numpy()[rows, None] - xy[None, :, 0],
                            f["y"].to_numpy()[rows, None] - xy[None, :, 1]), axis=1) if len(rows) else []
        rows = rows[np.asarray(d) <= 15_000] if len(rows) else rows
        how = "name near the coordinates (path did not join)"
    # one downstream path: same-name reaches in other valleys or on another fork are dropped
    rows, got = one_path(rows, idx.down, f["length_m"].to_numpy(), f["area_km2"].to_numpy(), idx.rnorm,
                         norm_river(s.river), xy=idx.xy, points=xy, outlet=river_outlet(s))
    if info is not None:
        info.update(got)
    note = pieces_note(got)
    if note:
        how = f"{how[:-1]}; {note})" if how.endswith(")") else f"{how} ({note})"
    return rows, how


def map_sections(f: pd.DataFrame, sections: list[Section], group: bool = True) -> list[Mapped]:
    """Map sections onto reaches. With ``group``, sections are gathered into whole rivers: the same
    river name in the same guide region, or any shared reach, gives one key ("River | region"), so a
    river listed in several sections (or in both the reference labels and the research) can never be
    split across train and test."""
    idx = _Index(f)
    out = []
    for s in sections:
        info: dict = {}
        rows, how = map_section(f, s, idx, info)
        out.append(Mapped(s, river_key(s), rows, how, info))
    if not group or not out:
        return out
    gr = f["guide_region"].to_numpy()
    parent = list(range(len(out)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def region_of(m):
        if len(m.rows):
            v, c = np.unique(gr[m.rows], return_counts=True)
            return str(v[np.argmax(c)])
        return m.section.region

    regs = [region_of(m) for m in out]
    first: dict = {}
    owner = {}
    for i, m in enumerate(out):
        k = (norm_river(m.section.river), regs[i])
        if k in first:
            parent[find(i)] = find(first[k])
        else:
            first[k] = i
        for r in m.rows.tolist():
            if r in owner:
                parent[find(i)] = find(owner[r])
            else:
                owner[r] = i
    for i, m in enumerate(out):
        root = find(i)
        m.key = f"{out[root].section.river} | {regs[root]}"
    return out


def _same(a: Section, b: Section) -> bool:
    return a.river == b.river and (a.extra or {}).get("id") == (b.extra or {}).get("id") and a.region == b.region


def _orig_key(s: Section, tm: list[Mapped]) -> str:
    return next((m.key for m in tm if _same(m.section, s)), river_key(s))


# ---------------------------------------------------------------------------- label sets
def reference_sections() -> list[Section]:
    """The v1 labels: reference rivers by name inside their reference box (what v1 trained on)."""
    out = []
    for rid in list_regions():
        r = load_region(rid)
        for k in r.known_runs:
            out.append(Section(region=rid, river=k.river, status="runnable", held_out=k.status != "listed",
                               bbox=tuple(r.bbox_wgs84), coords="none", source=f"regions/{rid}.toml"))
    return out


def all_sections(research: Path | None = None) -> list[Section]:
    """Reference sections (known_runs.toml, limited to their reference box) + guide research."""
    from .research import load_research

    refs = {rid: load_region(rid) for rid in list_regions()}
    out = []
    for s in load_sections():
        if s.region in refs:
            s.bbox = tuple(refs[s.region].bbox_wgs84)
        out.append(s)
    res = load_research(research)
    pts = [s.put_in or s.take_out for s in res]
    has = [i for i, p in enumerate(pts) if p]
    if has:
        import geopandas as gpd

        from .national import assign_regions

        xy = [_TO_NZTM.transform(*pts[i]) for i in has]
        reg = assign_regions(gpd.GeoSeries(gpd.points_from_xy([a for a, _ in xy], [b for _, b in xy]),
                                           crs="EPSG:2193"))
        for i, r in zip(has, reg, strict=True):
            if r:
                res[i].region = r
    return out + res


def variant_sections(sections: list[Section], variant: str) -> list[Section]:
    """'all': as given. 'sourced': published coordinates only (see ``research.sourced_only``)."""
    if variant == "all":
        return sections
    from .research import sourced_only

    return [v for v in (sourced_only(s) for s in sections) if v is not None]


def section_dict(s: Section) -> dict:
    d = {k: v for k, v in asdict(s).items() if k != "extra" and v not in (None, "", False)}
    for k in ("put_in", "take_out", "outlet", "bbox"):
        if k in d:
            d[k] = [round(float(v), 6) for v in d[k]]
    if s.extra:
        d["extra"] = {k: v for k, v in s.extra.items() if k in ("section", "gradient_m_per_km", "length_km")
                      and v is not None}
    return d


def section_from(d: dict) -> Section:
    d = dict(d)
    for k in ("put_in", "take_out", "outlet", "bbox"):
        if k in d:
            d[k] = tuple(d[k])
    return Section(**d)


def grade_band(s: Section) -> str:
    g = (s.extra or {}).get("grade_max")
    g = float(g) if isinstance(g, int | float) else parse_grade(s.grade)
    return "?" if not np.isfinite(g) else "≤3" if g < 3.5 else "3-4" if g < 4.5 else "4+"


def make_split(mapped: list[Mapped], name: str, seed: int = SEED, test_frac: float = 0.25,
               force_test: tuple = ("Mangakarengorengo River",)) -> dict:
    """Whole rivers to train or test, stratified by region and grade band, with a fixed seed."""
    rivers: dict[str, dict] = {}
    for m in mapped:
        if m.section.status != "runnable" or not len(m.rows):
            continue
        r = rivers.setdefault(m.key, {"region": m.section.region, "bands": []})
        r["bands"].append(grade_band(m.section))
    strata: dict[tuple, list[str]] = {}
    for k, r in sorted(rivers.items()):
        band = max(r["bands"], key=r["bands"].count)
        strata.setdefault((r["region"], band), []).append(k)
    rng = np.random.default_rng(seed)
    test = {k for k in rivers if k.split(" | ")[0] in force_test}
    carry = 0.0
    for key in sorted(strata):
        ks = [k for k in strata[key] if k not in test]
        rng.shuffle(ks)
        want = len(ks) * test_frac + carry   # carry fractions across small strata
        n = int(want)
        carry = want - n
        test.update(ks[:n])
    split = {"name": name, "seed": seed, "test_frac": test_frac, "created": dt.date.today().isoformat(),
             "strata": "guide region x grade band (≤3, 3-4, 4+, unknown)",
             "train": sorted(set(rivers) - test), "test": sorted(test & set(rivers))}
    return split


def save_split(split: dict) -> Path:
    SPLITS_DIR.mkdir(parents=True, exist_ok=True)
    p = SPLITS_DIR / f"{split['name']}.json"
    if p.exists():
        old = json.loads(p.read_text())
        if (old["train"], old["test"]) != (split["train"], split["test"]):
            raise FileExistsError(f"{p} exists with a different split; splits are frozen. Use a new name.")
        return p
    p.write_text(json.dumps(split, indent=1, ensure_ascii=False) + "\n")
    return p


def load_split(name: str) -> dict:
    return json.loads((SPLITS_DIR / f"{name}.json").read_text())


# ---------------------------------------------------------------------------- cards
def set_models_dir(d: Path) -> None:
    """Use another registry directory in this process (``nz1m finish --models-dir``; also ``$RIVER_FINDER_MODELS``)."""
    global MODELS_DIR, SPLITS_DIR
    from . import ml

    MODELS_DIR, SPLITS_DIR = Path(d), Path(d) / "splits"
    ml.MODELS_DIR, ml.CV_DIR, ml.CV1M_DIR, ml.AUDIT_DIR = (MODELS_DIR, MODELS_DIR / "cv", MODELS_DIR / "cv-1m",
                                                         MODELS_DIR / "audits")


def card_path(mid: str) -> Path:
    return MODELS_DIR / f"{mid}.json"


def card_data(card: dict) -> str:
    """The national build a card was trained and scored on: ``"8m"`` (v1-v4, cards without the field) or ``"1m"``."""
    return card.get("data", "8m")


def evaluations_path(kind: str | None = None) -> Path:
    """``models/evaluations.json`` (8 m cards on the 8 m table) or ``models/evaluations-1m.json``."""
    return MODELS_DIR / ("evaluations.json" if (kind or data_kind()) == "8m" else "evaluations-1m.json")


def list_cards(kind: str | None = None) -> list[dict]:
    """Every card (``kind`` None), or the cards of one national build (``"8m"``, ``"1m"``). Cards of another build
    than the current root's are archived there: they keep their own scores and results and are not re-scored."""
    if not MODELS_DIR.exists():
        return []
    cards = [json.loads(p.read_text()) for p in MODELS_DIR.glob("*.json") if not p.name.startswith("evaluations")]
    if kind is not None:
        cards = [c for c in cards if card_data(c) == kind]
    return sorted(cards, key=lambda c: c.get("order", 0))


def load_card(mid: str) -> dict:
    return json.loads(card_path(mid).read_text())


def save_card(card: dict) -> Path:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    card.setdefault("data", data_kind())   # the build it was trained on (1 m cards are never scored on the 8 m table)
    p = card_path(card["id"])
    if p.exists():
        old = json.loads(p.read_text())
        same = (old.get("coef"), old.get("intercept"), old.get("booster_sha256"))
        if same != (card.get("coef"), card.get("intercept"), card.get("booster_sha256")):
            raise FileExistsError(f"{p} is a frozen checkpoint with different weights; pick a new model id.")
    p.write_text(json.dumps(card, indent=1, ensure_ascii=False) + "\n")
    return p


def logits(card: dict, f: pd.DataFrame) -> np.ndarray:
    """The card's raw score (log-odds) for every reach of ``f``. Kind "gbm" (LightGBM, ``ml.gbm_logits``)
    needs the ML columns, which it adds to a frame that lacks them (``ml.enrich``)."""
    if card.get("kind") == "gbm":
        from .ml import gbm_logits

        return gbm_logits(card, f)
    names = card["features"]
    X = features(f, names)
    mean = np.array([card["mean"][n] for n in names])
    std = np.array([card["std"][n] if card["std"][n] > 1e-9 else 1.0 for n in names])
    return ((X - mean) / std) @ np.array([card["coef"][n] for n in names]) + card["intercept"]


def percentiles(card: dict, lg: np.ndarray) -> np.ndarray:
    """National percentile (0-100) of each logit among all reaches of 2 km2 or more."""
    q = np.asarray(card["pool_quantiles"])
    return np.interp(lg, q, PCT_GRID, left=0.0, right=100.0)


def _quantiles(lg: np.ndarray, f: pd.DataFrame) -> list[float]:
    big = (f["area_km2"] >= 2.0).to_numpy()
    q = np.quantile(lg[big], PCT_GRID / 100.0)
    return [round(float(v), 5) for v in np.maximum.accumulate(q)]


def _positive_rows(m: Mapped, f: pd.DataFrame) -> np.ndarray:
    """Name-only extents already apply the size and gradient rule; coordinate extents keep every
    reach of 1 km2 or more between the put-in and take-out (pools included)."""
    return m.rows[(f["area_km2"].to_numpy()[m.rows] >= 1.0)]


def checkpoint_v1(table: pd.DataFrame) -> dict:
    """Freeze the model the first national run fitted (``data/nz/model.json``) as ``models/v1.json``."""
    m = json.loads((nz_dir() / "model.json").read_text())
    secs = reference_sections()
    card = {
        "id": "v1", "order": 1, "kind": "logistic",
        "title": "v1: reference rivers",
        "description": "The first national model (2026-11-13). Logistic regression on 13 terrain features, "
                       "trained on six reference rivers in the Kaimai, Karangahake and Tutamoe areas, named "
                       "reaches of 2 km² or more and 10 m/km or steeper. The Mangakarengorengo was held out.",
        "created": "2026-11-13", "features": m["features"], "coef": m["coef"], "intercept": m["intercept"],
        "mean": m["mean"], "std": m["std"], "l2": 1.0,
        "labels": {"source": "regions/*.toml known_runs (name inside each reference box)", "variant": "all",
                   "split": "reference",
                   "train_sections": [section_dict(s) for s in secs if not s.held_out],
                   "test_sections": [section_dict(s) for s in secs if s.held_out]},
        "run_params": m.get("run_params"),
    }
    card["labels"]["train_rivers"] = sorted({river_key(s) for s in secs if not s.held_out})
    card["labels"]["test_rivers"] = sorted({river_key(s) for s in secs if s.held_out})
    lg = logits(card, table)
    card["pool_quantiles"] = _quantiles(lg, table)
    # it must reproduce the scores the national run wrote
    big = (table["area_km2"] >= 2.0).to_numpy()
    diff = np.abs(percentiles(card, lg)[big] - table["score_regression"].to_numpy()[big])
    card["checkpoint_check"] = {"reaches": int(big.sum()), "max_abs_pct_diff": round(float(diff.max()), 2),
                                "mean_abs_pct_diff": round(float(diff.mean()), 3)}
    split = {"name": "reference", "seed": None, "test_frac": None, "created": "2026-11-13",
             "strata": "fixed: the Mangakarengorengo was always the test",
             "train": card["labels"]["train_rivers"], "test": card["labels"]["test_rivers"],
             "sections": [section_dict(s) for s in secs]}
    save_split(split)
    return card


def baseline_card(table: pd.DataFrame, mid: str = "baseline-flow") -> dict:
    """A one-feature yardstick: rank reaches by flood size (mean annual flood) alone. Guide runs sit on
    bigger rivers than most reaches of 2 km2, so a model has to beat this to show it learnt more. Not trained, so
    each national build has its own (``baseline-flow`` 8 m, ``baseline-flow-1m``: its pool quantiles differ)."""
    names = list(FEATURES)
    _, mean, std = _standardise(features(table, names))
    card = {"id": mid, "order": 0, "kind": "baseline", "title": "Baseline: flood size only",
            "description": "Not trained. Ranks every reach by its mean annual flood alone. Any model "
                           "should beat this; the gap is what the terrain features add.",
            "created": dt.date.today().isoformat(), "features": names,
            "coef": {n: (1.0 if n == "maf" else 0.0) for n in names}, "intercept": 0.0,
            "mean": {k: float(v) for k, v in zip(names, mean, strict=True)},
            "std": {k: float(v) for k, v in zip(names, std, strict=True)},
            "labels": {"source": "none", "variant": "-", "split": "-", "train_sections": [], "test_sections": [],
                       "train_rivers": [], "test_rivers": []}}
    card["pool_quantiles"] = _quantiles(logits(card, table), table)
    return card


def train(table: pd.DataFrame, mapped: list[Mapped], split: dict, mid: str, title: str, description: str,
          variant: str = "all", l2: float = 1.0, order: int | None = None) -> dict:
    """Fit a logistic model on the split's training rivers.

    ``variant``: "all" uses every training section as given; "sourced" uses published coordinates
    only (``variant_sections``). Known reaches a variant cannot place are left out of training, never
    turned into negatives: every reach that the "all" mapping puts on a known river is excluded
    unless it is a positive of the variant.
    """
    names = list(FEATURES)
    X = features(table, names)
    Z, mean, std = _standardise(X)
    n = len(table)
    y = np.zeros(n, dtype=int)
    use = np.ones(n, dtype=bool)
    train_keys, test_keys = set(split["train"]), set(split["test"])
    for m in mapped:                      # every known reach out, unless re-admitted below
        if m.section.status == "runnable" or m.key in test_keys:
            use[m.rows] = False
    var = mapped if variant == "all" else map_sections(
        table, variant_sections([m.section for m in mapped if m.key in train_keys or
                                 m.section.status != "runnable"], variant), group=False)
    if variant != "all":
        for m in var:    # mapped without grouping: give each section its river key back
            m.key = _orig_key(m.section, mapped)
    used = []
    for m in var:
        if m.key in test_keys or not len(m.rows):
            continue
        if m.section.status != "runnable":   # labelled not runnable: a real negative
            use[m.rows] = True
        elif m.key in train_keys:
            rows = _positive_rows(m, table)
            y[rows], use[rows] = 1, True
        else:
            continue
        used.append(m)
    dropped = [m for m in mapped if variant != "all" and m.key in train_keys and m.section.status == "runnable"
               and not any(_same(u.section, m.section) for u in used)]
    coef, b0 = logistic_fit(Z[use], y[use], l2=l2)
    order = order or (max((c.get("order", 0) for c in list_cards()), default=0) + 1)
    card = {
        "id": mid, "order": order, "kind": "logistic", "title": title, "description": description,
        "created": dt.date.today().isoformat(), "features": names,
        "coef": {k: round(float(c), 4) for k, c in zip(names, coef, strict=True)}, "intercept": round(b0, 4),
        "mean": {k: float(v) for k, v in zip(names, mean, strict=True)},
        "std": {k: float(v) for k, v in zip(names, std, strict=True)}, "l2": l2,
        "labels": {"source": "labels/known_runs.toml + labels/guide_research.json", "variant": variant,
                   "split": split["name"], "positives": int(y[use].sum()), "training_reaches": int(use.sum()),
                   "train_sections": [section_dict(m.section) for m in used],
                   "dropped_sections": [section_dict(m.section) for m in dropped],
                   "test_sections": [section_dict(m.section) for m in mapped if m.key in test_keys],
                   "train_rivers": sorted({m.key for m in used if m.section.status == "runnable"}),
                   "test_rivers": sorted(test_keys)},
    }
    lg = Z @ np.array([card["coef"][k] for k in names]) + card["intercept"]
    card["pool_quantiles"] = _quantiles(lg, table)
    return card


# ---------------------------------------------------------------------------- evaluation
def _pct_strata(lg: np.ndarray, bg: np.ndarray, strata: np.ndarray) -> np.ndarray:
    """Percentile (0-100) of every reach among the background reaches of its own stratum."""
    out = np.zeros(len(lg))
    for b in np.unique(strata):
        sel = strata == b
        ref = np.sort(lg[bg & sel])
        out[sel] = 100.0 * np.searchsorted(ref, lg[sel], side="right") / max(len(ref), 1)
    return out


def _auc(pos: np.ndarray, neg_sorted: np.ndarray) -> float:
    lo = np.searchsorted(neg_sorted, pos, side="left")
    hi = np.searchsorted(neg_sorted, pos, side="right")
    return float(np.mean((lo + hi) / 2.0) / len(neg_sorted))


def evaluate(card: dict, table: pd.DataFrame, mapped: list[Mapped], split: dict, coords: str = "any") -> dict:
    """Rank the split's test rivers against the unlabelled background.

    ``coords``: "any" counts every test section; "sourced" only those whose coordinates were not
    inferred. Rivers the card was trained on are left out (named in ``excluded``).
    """
    trained_rows = roles(card, table) == 1
    lg = logits(card, table)
    big = (table["area_km2"] >= 2.0).to_numpy()
    known = trained_rows.copy()
    for m in mapped:
        known[m.rows] = True
    bg = big & ~known
    regions = table["guide_region"].to_numpy()
    bg_sorted = np.sort(lg[bg])
    bg_by_region = {r: np.sort(lg[bg & (regions == r)]) for r in np.unique(regions[bg])}
    pct = 100.0 * np.searchsorted(bg_sorted, lg, side="right") / max(len(bg_sorted), 1)
    # size-matched: rank each reach only among background reaches of similar flood size (10 bins of
    # log mean annual flood), so "it is a big river" earns nothing
    lq = np.nan_to_num(np.log10(table["maf_m3s"].clip(lower=0.05).to_numpy()), nan=np.log10(0.05))
    mpct = _pct_strata(lg, bg, np.searchsorted(np.quantile(lq[bg], np.linspace(0, 1, 11)[1:-1]), lq))
    # style-matched: rank each reach among background reaches of the same paddling style
    # (Montgomery & Buffington reach type from slope and confinement, see reachtype.py); style x size
    # also within the same flood quintile (as ml.strata), so neither "steep" nor "big" earns anything
    spct = sspct = None
    if "valley_width_m" in table:
        from .reachtype import classify_frame

        st = table["style"] if "style" in table else classify_frame(table)["style"]
        style = st.cat.codes.to_numpy().astype(np.int64)
        spct = _pct_strata(lg, bg, style)
        q5 = np.searchsorted(np.quantile(lq[bg], np.linspace(0, 1, 6)[1:-1]), lq)
        sspct = _pct_strata(lg, bg, style * 10 + q5)
    test = set(split["test"])
    # a test river is left out for this card if any of its reaches were in the card's training data
    # (v1 trained on rivers that the guide research lists again, under other region names)
    trained = {m.key for m in mapped if m.key in test and trained_rows[m.rows].any()}
    if coords == "sourced":
        tm = [m for m in mapped if m.key in test]
        remapped = map_sections(table, variant_sections([m.section for m in tm], "sourced"), group=False)
        for m in remapped:   # keep each section's river group key
            m.key = _orig_key(m.section, tm)
        mapped = [m for m in mapped if m.key not in test] + remapped
    per, excluded, pos_rows, neg_rows = {}, sorted(trained), [], []
    for m in mapped:
        if m.key not in test or m.key in trained or not len(m.rows):
            continue
        rows = m.rows[big[m.rows]]
        if not len(rows):
            continue
        if m.section.status != "runnable":
            neg_rows.append(rows)
            continue
        pos_rows.append(rows)
        r = per.setdefault(m.key, {"river": m.section.river, "region": m.section.region, "rows": [],
                                   "grade": m.section.grade, "coords": set(), "method": set()})
        r["rows"].append(rows)
        r["coords"].add(m.section.coords)
        r["method"].add(m.method)
    out = {"model": card["id"], "split": split["name"], "coords": coords, "excluded_trained": excluded,
           "background_reaches": int(bg.sum())}
    if not pos_rows:
        out.update({"test_rivers": 0, "test_reaches": 0})
        return out
    P = np.unique(np.concatenate(pos_rows))
    p = pct[P]
    out.update({
        "test_rivers": len(per), "test_reaches": int(len(P)),
        "auc": round(_auc(lg[P], bg_sorted), 4),
        "median_pct": round(float(np.median(p)), 1),
        "auc_size_matched": round(float(np.mean(mpct[P]) / 100.0), 4),
        **({"auc_style_matched": round(float(np.mean(spct[P]) / 100.0), 4),
            "auc_style_size_matched": round(float(np.mean(sspct[P]) / 100.0), 4)} if spct is not None else {}),
        "recall_top10": round(float((p >= 90).mean()), 3),
        "recall_top5": round(float((p >= 95).mean()), 3),
        "lift_top10": round(float((p >= 90).mean() / 0.10), 2),
    })
    if neg_rows:  # labelled not-runnable test reaches give a real precision
        N = np.unique(np.concatenate(neg_rows))
        tp, fp = int((pct[P] >= 90).sum()), int((pct[N] >= 90).sum())
        out["precision_top10_labelled"] = round(tp / max(tp + fp, 1), 3)
        out["not_runnable_reaches"] = int(len(N))
    rivers = []
    for k, r in sorted(per.items()):
        rows = np.unique(np.concatenate(r["rows"]))
        rp = pct[rows]
        rivers.append({"key": k, "river": r["river"], "region": r["region"], "grade": r["grade"],
                       "reaches": int(len(rows)), "best_pct": round(float(rp.max()), 1),
                       "median_pct": round(float(np.median(rp)), 1),
                       "median_pct_size_matched": round(float(np.median(mpct[rows])), 1),
                       **({"median_pct_style_matched": round(float(np.median(spct[rows])), 1),
                           "median_pct_style_size_matched": round(float(np.median(sspct[rows])), 1)}
                          if spct is not None else {}),
                       "share_top10": round(float((rp >= 90).mean()), 2),
                       "coords": "/".join(sorted(r["coords"])), "extent": "/".join(sorted(r["method"]))})
    out["rivers"] = sorted(rivers, key=lambda r: -r["median_pct"])
    reg = {}
    for r in rivers:
        reg.setdefault(r["region"], []).append(r)
    out["regions"] = []
    for name, rs in sorted(reg.items()):
        rows = np.unique(np.concatenate([np.concatenate(per[r["key"]]["rows"]) for r in rs]))
        gr = table["guide_region"].to_numpy()[rows]
        ref = bg_by_region.get(gr[0]) if len(set(gr)) == 1 else None
        out["regions"].append({
            "region": name, "rivers": len(rs), "reaches": int(len(rows)),
            "median_pct": round(float(np.median(pct[rows])), 1),
            "recall_top10": round(float((pct[rows] >= 90).mean()), 3),
            "auc_within_region": round(_auc(lg[rows], ref), 4) if ref is not None and len(ref) else None})
    return out


def evaluate_all(table: pd.DataFrame, mapped: list[Mapped], log=print) -> dict:
    """Every card on every split, with and without inferred-coordinate test sections."""
    splits = [json.loads(p.read_text()) for p in sorted(SPLITS_DIR.glob("*.json"))]
    table = scoring_table(table)
    res = []
    ref_mapped = None
    for card in list_cards(data_kind()):   # cards of another build keep their own results
        for sp in splits:
            if sp["name"] == "reference":
                ref_mapped = ref_mapped or map_sections(table, reference_sections(), group=False)
                mp = ref_mapped
            else:
                mp = mapped
            for coords in ("any", "sourced"):
                e = evaluate(card, table, mp, sp, coords)
                res.append(e)
                log(f"eval {card['id']} on {sp['name']} ({coords}): auc {e.get('auc')} "
                    f"median {e.get('median_pct')} rivers {e.get('test_rivers')}")
    out = {"created": dt.datetime.now().isoformat(timespec="minutes"), "evaluations": res,
           "splits": [{k: v for k, v in sp.items() if k != "sections"} for sp in splits]}
    evaluations_path().write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    return out


# ---------------------------------------------------------------------------- viewer files
def roles(card: dict, f: pd.DataFrame) -> np.ndarray:
    """Per reach: 1 on a section the model trained on, 2 on a held-out test section, 0 otherwise."""
    out = np.zeros(len(f), dtype=np.uint8)
    lab = card["labels"]
    idx = _Index(f)
    for code, key in ((2, "test_sections"), (1, "train_sections")):
        for d in lab.get(key, []):
            s = section_from(d)
            if s.status != "runnable":
                continue
            rows, _ = map_section(f, s, idx)
            out[rows] = code
    return out


def guide_mask(f: pd.DataFrame, sections: list[Section] | None = None) -> np.ndarray:
    """Reaches on any known section (guide research or known_runs.toml), whatever the model."""
    out = np.zeros(len(f), dtype=bool)
    for m in map_sections(f, sections if sections is not None else all_sections(), group=False):
        if m.section.status == "runnable" and not m.section.held_out:   # held_out = local knowledge only
            out[m.rows] = True
    return out


def score_blob(card: dict, f: pd.DataFrame, guides: np.ndarray | None = None) -> str:
    """Base64 of [uint16 percentile x 100] + [uint8 flags] for the reaches the viewer shows.
    Flags: bits 0-1 role (1 trained on, 2 test river), bit 2 on a guide-listed section."""
    vis = np.flatnonzero(f["vi"].to_numpy() >= 0)
    vis = vis[np.argsort(f["vi"].to_numpy()[vis])]
    pct = percentiles(card, logits(card, f)[vis])   # the whole frame: features may follow the network
    q = np.round(pct * 100).astype("<u2")
    r = roles(card, f)[vis].astype(np.uint8)
    if guides is not None:
        r |= (guides[vis].astype(np.uint8) << 2)
    return base64.b64encode(q.tobytes() + r.tobytes()).decode("ascii")


def write_viewer_scores(out_dir: Path, table: pd.DataFrame | None = None, log=print) -> list[dict]:
    """``<region>_m_<model>.txt`` for every region in the viewer index and every card."""
    index = json.loads((out_dir / "regions_index.json").read_text())
    cards = list_cards(data_kind())   # archived cards (another build) keep their old blobs and are not re-scored
    by_slug = {_rslug(n): n for n in FILTER_ORDER}
    if table is None and any(e["id"] in by_slug for e in index):
        table = national_table()   # only national regions need it; reference regions have their own frame
    if table is not None:
        table = scoring_table(table)   # ML columns for "gbm" cards (whole-NZ network)
    secs = all_sections()
    for e in index:
        if e["id"] in by_slug:
            sel = np.flatnonzero((table["region"] == by_slug[e["id"]]).to_numpy())
            local = np.full(len(table), -1, dtype=np.int64)
            local[sel] = np.arange(len(sel))
            f = table.iloc[sel].reset_index(drop=True)
            d = f["down"].to_numpy()
            f["down"] = np.where(d >= 0, local[np.maximum(d, 0)], -1)   # downstream links inside the region
            f["up"] = _upstream_main(f["down"].to_numpy(), f["area_km2"].to_numpy())
        else:
            f = reference_frame(e["id"])
        n = int((f["vi"] >= 0).sum())
        if n != e["segments"]:
            raise ValueError(f"{e['id']}: {n} reaches here but {e['segments']} in the viewer; rebuild the viewer")
        guides = guide_mask(f, secs)
        for c in cards:
            try:
                blob = score_blob(c, f, guides)
            except KeyError as err:   # a 5 m reference area has no 1 m sections for a card that reads x1_*
                log(f"scores: {e['id']}: {c['id']} left out ({err})")
                continue
            (out_dir / f"{e['id']}_m_{c['id']}.txt").write_text(blob)
        log(f"scores: {e['id']} ({n} reaches, {len(cards)} models)")
    return cards


def scoring_table(table: pd.DataFrame) -> pd.DataFrame:
    """``table`` with the ML columns when a card needs them (kind "gbm"): the cached ``ml_table`` when it
    lines up with ``table`` (whole-NZ karst accumulation), else ``ml.enrich(table)``."""
    if not any(c.get("kind") == "gbm" for c in list_cards(data_kind())):
        return table
    from . import ml

    cache = nz_dir() / "ml_table.pkl"
    if cache.exists() and "seg_id" in table:
        t = ml.ml_table()
        if len(t) == len(table) and (t["seg_id"].to_numpy() == table["seg_id"].to_numpy()).all():
            return t
    return ml.enrich(table)


def cv_summary() -> dict | None:
    """The cross-validated model zoo (``models/cv/results.json``), headline numbers only."""
    p = MODELS_DIR / ("cv-1m" if data_kind() == "1m" else "cv") / "results.json"   # as ml.cv_dir
    if not p.exists():
        return None
    r = json.loads(p.read_text())
    keys = ("auc", "auc_style", "auc_size", "auc_style_size", "recall_top10", "recall_top5", "median_pct")
    models = []
    for m in r["models"]:
        lo = [x["auc_style"] for x in m.get("loro", []) if x.get("auc_style") is not None]
        models.append({"id": m["id"], "kind": m["kind"], "title": m["title"], "description": m["description"],
                       "inputs": len(m["features"]) + len(m.get("categorical", [])),
                       "cv": {k: m["cv"][k] for k in keys if k in m["cv"]},
                       "loro_auc_style": round(float(np.mean(lo)), 4) if lo else None})
    return {"created": r["created"], "folds": r["folds"], "repeats": r["repeats"], "domain_km2": r["domain_km2"],
            "spec_version": r.get("spec_version"), "models": models}


def _card_summary(c: dict) -> dict:
    lab = c["labels"]
    return ({k: c[k] for k in ("id", "title", "description", "created", "coef", "intercept", "kind")}
            | {"variant": lab.get("variant"), "split": lab.get("split"),
               "train_rivers": lab.get("train_rivers", []), "test_rivers": lab.get("test_rivers", []),
               "positives": lab.get("positives"), "training_reaches": lab.get("training_reaches"),
               "checkpoint_check": c.get("checkpoint_check"), "data": card_data(c)}
            | ({"features": c["features"] + c.get("categorical", []), "importance": c.get("importance"),
                "recipe": c.get("recipe")} if c["kind"] == "gbm" else {}))


def viewer_models() -> dict:
    """What the page embeds: card summaries (no quantiles or sections), the held-out evaluations and the
    cross-validation summary. The default model is the newest card trained on every guide section
    ("all" variant). Only the current build's cards are in ``models`` (they have score files); cards of another
    build (v1-v4 on the 1 m rebuild) are listed in ``archived``, their results in ``archived_evaluations``."""
    kind = data_kind()
    other = "8m"   # the 8 m cards are history on the 1 m site; 1 m cards are not shown on the 8 m site at all
    arch = [_card_summary(c) for c in list_cards(other)] if kind == "1m" else []
    aev = None
    if arch:
        ep = evaluations_path(other)
        ids = {c["id"] for c in arch}
        if ep.exists():
            aev = json.loads(ep.read_text())
            aev = {**aev, "evaluations": [e for e in aev["evaluations"] if e.get("model") in ids]}
    cards = [_card_summary(c) for c in list_cards(kind)]
    ev = evaluations_path(kind)
    full = [c for c in cards if c["kind"] in ("logistic", "gbm") and c["variant"] == "all"]
    out = {"models": cards, "default": (full or cards)[-1]["id"] if cards else None,
           "evaluations": json.loads(ev.read_text()) if ev.exists() else None,
           "cv": cv_summary(),
           "descriptions": _descriptions(any(c["kind"] == "gbm" for c in cards + arch))}
    if arch:
        out |= {"archived": arch, "archived_evaluations": aev, "data": kind}
    return out


def _descriptions(extended: bool) -> dict:
    """Feature descriptions keyed by name; with ``extended``, the ML features of "gbm" cards too."""
    out = {k: v[2] for k, v in FEATURES.items()}
    if extended:
        from .ml import CATEGORICAL, EXTRA

        out |= {k: v[1] for k, v in EXTRA.items()} | dict(CATEGORICAL)
    return out
