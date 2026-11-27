"""Hydro-affected reaches (rf-8x2): diverted, regulated or drowned by a hydro scheme.

The flood statistics (NIWA Henderson & Collins, REC1) are *natural* flows. Below a hydro intake most of the
water leaves the river, so the natural mean annual flood (MAF) overstates the flow there, and a dam face or
a lake outlet can look like a steep, big-water run. ``config/hydro.toml`` lists the schemes (cited), each as
points with a role:

| role | what happens |
|---|---|
| ``intake`` | ``take`` (default 1) of the natural flow leaves the river (weir, dam or intake to a canal or tunnel) |
| ``dam`` | a storage dam or lake control structure: the flow below is controlled (power station at the dam toe) |
| ``return`` | a tailrace or outfall: diverted water comes back (``returns``: its intakes, default the scheme's) |

A return also makes the reaches below it regulated unless ``regulates = false`` (a run-of-river bypass: the
river is natural again below the tailrace).

Each point snaps to the nearest reach of its named river (``models._Index.nearest``). Down the network from
it, every reach gets the point's catchment area (natural area at the snapped reach): an intake adds
``take * area`` to the reach's *diverted* area until the water returns (the first reach on a return point's
path), the walk enters the named ``until`` river (a confluence) or ``km`` is reached; a dam or return adds to
the *regulated* area. Shares are these areas over the reach's own catchment, summed over every point upstream,
so several intakes on one river add up (the Tongariro Power Scheme's Western Diversion), and a diversion fades
as tributaries join below it. Per reach:

- ``reservoir``: the lake behind a point with ``reservoir = true`` (reaches upstream of it flatter than
  ``reservoir_grad``, the lake surface in the DEM, within ``reservoir_km``) and the dam itself (reaches
  within ``dam_m`` of the point);
- ``diverted``: diverted share >= ``diverted_share`` (default 0.5: at least half the natural flow is taken);
- ``regulated``: diverted + regulated share >= ``regulated_share`` (the flow is controlled or much reduced);
- ``""``: otherwise.

``hydro_scheme`` / ``hydro_point`` name the scheme and point that contribute most; ``hydro_share`` is the
share behind the class (1 for reservoir reaches).

Files: ``data/nz/hydro.pkl`` (national reaches in ``national_table`` order: ``region``, ``seg_id``, ``hydro``,
``hydro_scheme``, ``hydro_point``, ``hydro_share``) and ``data/nz/hydro_summary.json`` (snapped points and
counts per class and scheme). ``river-finder hydro attach`` writes both; ``hydro check`` prints the snapping.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .regions import REPO_ROOT

CONFIG_PATH = REPO_ROOT / "config" / "hydro.toml"
CLASSES = ("", "diverted", "regulated", "reservoir")
ROLES = ("intake", "dam", "return")
NOTES = {
    "diverted": "Hydro-diverted: most of the natural flow is taken out upstream, so the natural flood "
                "statistic overstates the flow.",
    "regulated": "Flow controlled by a hydro scheme (storage dam, control gates or tailrace): natural flood "
                 "statistics do not describe it.",
    "reservoir": "Hydro lake or dam structure, not a river reach.",
}


# ---------------------------------------------------------------------------- config
@dataclass(frozen=True)
class Point:
    scheme: str
    id: str
    name: str
    river: str
    lon: float
    lat: float
    role: str = "intake"
    take: float = 1.0
    reservoir: bool = False
    reservoir_km: float | None = None
    until: str = ""
    km: float | None = None
    returns: tuple[str, ...] = ()
    regulates: bool = True             # return: false for a run-of-river bypass (the river is natural again below)
    area_km2: float | None = None      # natural catchment at the point when the network's is wrong; return: the
                                       # water released, as catchment area (default: what its intakes took)
    note: str = ""


@dataclass(frozen=True)
class Scheme:
    id: str
    name: str
    operator: str = ""
    note: str = ""
    sources: tuple[str, ...] = ()
    points: tuple[Point, ...] = ()


@dataclass
class HydroConfig:
    schemes: list[Scheme] = field(default_factory=list)
    diverted_share: float = 0.5
    regulated_share: float = 0.5
    snap_named_m: float = 3000.0
    snap_any_m: float = 700.0
    max_km: float = 400.0
    reservoir_grad: float = 0.5
    reservoir_km: float = 30.0
    dam_m: float = 300.0
    dam_up_m: float = 1500.0
    audit_exclude: tuple[str, ...] = ("diverted", "reservoir")
    web: bool = True

    def points(self) -> list[Point]:
        return [p for s in self.schemes for p in s.points]

    def scheme(self, sid: str) -> Scheme:
        return next(s for s in self.schemes if s.id == sid)


def _point(sid: str, d: dict) -> Point:
    role = d.get("role", "intake")
    if role not in ROLES:
        raise ValueError(f"hydro.toml: scheme {sid} point {d.get('id')}: role {role!r} not in {ROLES}")
    return Point(scheme=sid, id=str(d["id"]), name=str(d.get("name", d["id"])), river=str(d.get("river", "")),
                 lon=float(d["lon"]), lat=float(d["lat"]), role=role, take=float(d.get("take", 1.0)),
                 reservoir=bool(d.get("reservoir", False)),
                 reservoir_km=float(d["reservoir_km"]) if "reservoir_km" in d else None,
                 until=str(d.get("until", "")), km=float(d["km"]) if "km" in d else None,
                 returns=tuple(d.get("returns", ())), regulates=bool(d.get("regulates", True)),
                 area_km2=float(d["area_km2"]) if "area_km2" in d else None, note=str(d.get("note", "")))


def parse_config(d: dict) -> HydroConfig:
    st, au, web = d.get("settings", {}), d.get("audit", {}), d.get("web", {})
    schemes, seen = [], set()
    for s in d.get("scheme", []):
        sid = str(s["id"])
        pts = tuple(_point(sid, p) for p in s.get("point", []))
        for p in pts:
            if (sid, p.id) in seen:
                raise ValueError(f"hydro.toml: duplicate point {sid}/{p.id}")
            seen.add((sid, p.id))
        ids = {p.id for p in pts}
        for p in pts:
            bad = [r for r in p.returns if r not in ids]
            if bad:
                raise ValueError(f"hydro.toml: {sid}/{p.id} returns unknown points {bad}")
        schemes.append(Scheme(id=sid, name=str(s.get("name", sid)), operator=str(s.get("operator", "")),
                              note=str(s.get("note", "")), sources=tuple(s.get("sources", ())), points=pts))
    if len({s.id for s in schemes}) != len(schemes):
        raise ValueError("hydro.toml: duplicate scheme ids")
    base = HydroConfig()
    cfg = HydroConfig(schemes=schemes, **{k: type(getattr(base, k))(st[k]) for k in
                                         ("diverted_share", "regulated_share", "snap_named_m", "snap_any_m",
                                          "max_km", "reservoir_grad", "reservoir_km", "dam_m", "dam_up_m") if k in st})
    if "exclude" in au:
        bad = [c for c in au["exclude"] if c not in CLASSES[1:]]
        if bad:
            raise ValueError(f"hydro.toml: [audit] exclude has unknown classes {bad}")
        cfg.audit_exclude = tuple(au["exclude"])
    cfg.web = bool(web.get("enabled", True))
    return cfg


def load_config(path: Path | None = None) -> HydroConfig:
    p = Path(path) if path else CONFIG_PATH
    if not p.exists():
        return HydroConfig()
    with p.open("rb") as fh:
        return parse_config(tomllib.load(fh))


# ---------------------------------------------------------------------------- network
def _ups(down: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """CSR upstream adjacency: reaches flowing into ``d`` are ``order[start[d]:start[d + 1]]``."""
    n = len(down)
    ok = (down >= 0) & (down < n)
    src = np.flatnonzero(ok)
    order = src[np.argsort(down[ok], kind="stable")]
    counts = np.bincount(down[ok], minlength=n)
    start = np.concatenate([[0], np.cumsum(counts)])
    return order, start


def _path(down: np.ndarray, length: np.ndarray, r0: int, max_km: float) -> list[int]:
    out, cur, km = [], r0, 0.0
    while 0 <= cur < len(down) and km <= max_km and len(out) < 100_000:
        out.append(cur)
        km += length[cur] / 1000.0
        cur = int(down[cur])
        if out[-1] == cur:
            break
    return out


@dataclass
class Snap:
    point: Point
    row: int
    dist_m: float
    river: str
    area_km2: float
    named: bool


def _prep(f: pd.DataFrame) -> pd.DataFrame:
    """``f`` with the ``rnorm`` (normalised river name) and ``up`` (main upstream reach) columns the snapping needs."""
    from .models import _upstream_main, norm_river

    if "rnorm" in f and "up" in f:
        return f
    f = f.copy()
    if "rnorm" not in f:
        f["rnorm"] = [norm_river(r) for r in f["river"].fillna("")] if "river" in f else ""
    if "up" not in f:
        f["up"] = _upstream_main(f["down"].to_numpy(), f["area_km2"].to_numpy())
    return f


def snap(f: pd.DataFrame, cfg: HydroConfig, index=None) -> list[Snap]:
    """Nearest reach of each point's named river (within ``snap_named_m``), else the nearest reach of 1 km2
    or more within ``snap_any_m``; ``row`` -1 when neither."""
    from .models import _TO_NZTM, _Index, norm_river

    f = _prep(f)
    idx = index if index is not None else _Index(f)
    x, y = f["x"].to_numpy(), f["y"].to_numpy()
    rn = idx.rnorm   # the names the index matched with (a cached rnorm column can predate norm_river)
    out = []
    for p in cfg.points():
        r = idx.nearest((p.lon, p.lat), p.river, max_m=cfg.snap_any_m, named_m=cfg.snap_named_m)
        if r < 0:
            out.append(Snap(p, -1, float("nan"), "", float("nan"), False))
            continue
        px, py = _TO_NZTM.transform(p.lon, p.lat)
        out.append(Snap(p, r, float(np.hypot(x[r] - px, y[r] - py)), str(f["river"].iat[r] or ""),
                        float(f["area_km2"].iat[r]), bool(p.river) and rn[r] == norm_river(p.river)))
    return out


def classify(f: pd.DataFrame, cfg: HydroConfig | None = None, snaps: list[Snap] | None = None) -> pd.DataFrame:
    """Per-reach ``hydro``, ``hydro_scheme``, ``hydro_point``, ``hydro_share`` (and the raw ``div_share`` /
    ``reg_share``) for a reach frame with ``down``, ``area_km2``, ``length_m``, ``grad_s``, ``x``, ``y``,
    ``river`` and ``rnorm`` (``models.national_table`` or ``models.reference_frame``). Same index as ``f``."""
    from .models import norm_river

    cfg = cfg or load_config()
    f = _prep(f)
    n = len(f)
    down = f["down"].to_numpy().astype(np.int64)
    down = np.where((down >= 0) & (down < n), down, -1)
    area = np.maximum(f["area_km2"].to_numpy(dtype=float), 1e-6)
    length = np.nan_to_num(f["length_m"].to_numpy(dtype=float), nan=0.0)
    grad = np.nan_to_num(f["grad_s"].to_numpy(dtype=float), nan=99.0)
    rn = f["rnorm"].to_numpy()
    snaps = snaps if snaps is not None else snap(f, cfg)
    pts = [s for s in snaps if s.row >= 0]
    sids = [s.id for s in cfg.schemes]
    order, start = _ups(down)

    div, reg = np.zeros(n), np.zeros(n)
    best = {"div": (np.zeros(n), np.full(n, -1)), "reg": (np.zeros(n), np.full(n, -1)),
            "res": (np.zeros(n), np.full(n, -1))}
    paths = {k: _path(down, length, s.row, cfg.max_km) for k, s in enumerate(pts)}
    took: dict[int, float] = {}

    def add(kind: str, rows, val: float, k: int):
        rows = np.asarray(rows, dtype=np.int64)
        if not len(rows):
            return
        arr = {"div": div, "reg": reg}.get(kind)
        if arr is not None:
            np.add.at(arr, rows, val)
        bv, bk = best[kind]
        hit = rows[bv[rows] < val]
        bv[hit], bk[hit] = val, k

    def walk(k: int, stop: set[int]) -> list[int]:
        """The point's downstream path, cut at a return (``stop``), the ``until`` confluence or ``km``."""
        p = pts[k].point
        until = norm_river(p.until) if p.until else ""
        out, km = [], 0.0
        for i, r in enumerate(paths[k]):
            if i and r in stop:
                break
            if i and until:
                prev = paths[k][i - 1]
                feeders = order[start[r]:start[r + 1]]
                if (rn[r] == until and rn[prev] != until) or any(rn[u] == until for u in feeders if u != prev):
                    break
            out.append(r)
            km += length[r] / 1000.0
            if p.km is not None and km >= p.km:
                break
        return out

    # upstream points first, so a point lower on the same river adds only what is not yet taken or controlled
    # (nested catchments are not counted twice)
    on_path = [set(paths[k]) for k in range(len(pts))]
    above = [sum(1 for j in range(len(pts)) if j != k and pts[k].row in on_path[j]) for k in range(len(pts))]
    queue = sorted(range(len(pts)), key=lambda k: above[k])

    def feeds(k: int) -> list[int]:   # the intakes a return carries
        p = pts[k].point
        return [j for j, t in enumerate(pts) if t.point.scheme == p.scheme and t.point.role == "intake"
                and (not p.returns or t.point.id in p.returns)]

    while queue:
        k = queue.pop(0)
        s = pts[k]
        p = s.point
        if p.role == "return" and p.area_km2 is None and any(j not in took for j in feeds(k)):
            queue.append(k)       # an inter-catchment return can sit "above" its intakes: wait for them
            continue
        a0 = p.area_km2 if p.area_km2 is not None and p.role != "return" else s.area_km2
        if p.role == "intake":
            stop: set[int] = set()
            for j, t in enumerate(pts):
                q = t.point
                if q.scheme != p.scheme or q.role != "return" or (q.returns and p.id not in q.returns):
                    continue
                if s.row in on_path[j]:          # the return is upstream of this intake
                    continue
                stop.update(paths[j])
            took[k] = p.take * max(a0 - div[s.row], 0.0)
            add("div", walk(k, stop), took[k], k)
        elif p.role == "dam":
            add("reg", walk(k, set()), p.take * max(a0 - div[s.row] - reg[s.row], 0.0), k)
        elif p.regulates:   # return: the water of its intakes (or area_km2) comes back controlled
            val = p.area_km2 if p.area_km2 is not None else sum(took[j] for j in feeds(k))
            add("reg", walk(k, set()), val, k)
        if p.reservoir:
            add("res", _reservoir(s.row, down, order, start, length, grad, cfg, p), 1.0, k)

    div_share, reg_share = div / area, reg / area
    cls = np.full(n, "", dtype=object)
    regd = (div_share + reg_share) >= cfg.regulated_share
    cls[regd] = "regulated"
    dv = div_share >= cfg.diverted_share
    cls[dv] = "diverted"
    res = best["res"][1] >= 0
    cls[res] = "reservoir"
    who = np.where(res, best["res"][1], np.where(dv, best["div"][1], np.where(best["reg"][0] >= best["div"][0],
                                                                                best["reg"][1], best["div"][1])))
    who = np.where(cls == "", -1, who)
    scheme = np.array([pts[k].point.scheme if k >= 0 else "" for k in who], dtype=object)
    point = np.array([pts[k].point.id if k >= 0 else "" for k in who], dtype=object)
    share = np.clip(np.where(res, 1.0, np.where(dv, div_share, np.where(regd, div_share + reg_share, 0.0))), 0, 1)
    out = pd.DataFrame({"hydro": cls.astype(str), "hydro_scheme": scheme.astype(str),
                        "hydro_point": point.astype(str), "hydro_share": np.round(share, 3),
                        "div_share": np.round(div_share, 3), "reg_share": np.round(reg_share, 3)}, index=f.index)
    assert set(out["hydro_scheme"]) <= set(sids) | {""}
    return out


def _reservoir(r0: int, down, order, start, length, grad, cfg: HydroConfig, p: Point) -> list[int]:
    """The dam (reaches within ``dam_m`` below the point and ``dam_up_m`` above it: the smoothed gradient spreads
    the dam's drop over its neighbours) and the lake behind it: reaches upstream flatter than ``reservoir_grad``
    within ``reservoir_km`` (path distance)."""
    lim = (p.reservoir_km if p.reservoir_km is not None else cfg.reservoir_km) * 1000.0
    out, seen = [r0], {r0}
    cur, d = int(down[r0]), length[r0]
    while cur >= 0 and d <= cfg.dam_m and cur not in seen:   # dam face and spillway below the point
        out.append(cur)
        seen.add(cur)
        d += length[cur]
        cur = int(down[cur])
    stack = [(r0, 0.0)]
    while stack:
        r, dist = stack.pop()
        for u in order[start[r]:start[r + 1]]:
            u = int(u)
            if u in seen:
                continue
            du = dist + length[u]
            if du <= cfg.dam_up_m or (grad[u] <= cfg.reservoir_grad and du <= lim):
                seen.add(u)
                out.append(u)
                stack.append((u, du))
    return out


# ---------------------------------------------------------------------------- national table
def table_path() -> Path:
    from .national import nz_dir

    return nz_dir() / "hydro.pkl"


def summary(f: pd.DataFrame, h: pd.DataFrame, snaps: list[Snap], cfg: HydroConfig, domain_km2: float = 2.0) -> dict:
    """Reaches and km per class and per scheme (all reaches, and the ML domain of ``domain_km2`` or more),
    and where each point snapped."""
    km = f["length_m"].to_numpy(dtype=float) / 1000.0
    dom = (f["area_km2"].to_numpy(dtype=float) >= domain_km2)

    def counts(mask):
        return {"reaches": int(mask.sum()), "km": round(float(km[mask].sum()), 1),
                "domain_reaches": int((mask & dom).sum()), "domain_km": round(float(km[mask & dom].sum()), 1)}

    cls = h["hydro"].to_numpy()
    sch = h["hydro_scheme"].to_numpy()
    out = {"classes": {c: counts(cls == c) for c in CLASSES[1:]}, "schemes": {}, "points": []}
    for s in cfg.schemes:
        m = sch == s.id
        out["schemes"][s.id] = {"name": s.name, **{c: counts(m & (cls == c)) for c in CLASSES[1:]}}
    for s in snaps:
        p = s.point
        row = {"scheme": p.scheme, "id": p.id, "name": p.name, "role": p.role, "river": p.river,
               "lat": p.lat, "lon": p.lon, "row": s.row}
        if s.row >= 0:
            row |= {"snapped_river": s.river, "named_match": s.named, "dist_m": round(s.dist_m),
                    "area_km2": round(s.area_km2, 1), "seg_id": int(f["seg_id"].iat[s.row]) if "seg_id" in f else None,
                    "region": str(f["region"].iat[s.row]) if "region" in f else "",
                    "hydro_here": str(h["hydro"].iat[s.row])}
        out["points"].append(row)
    return out


def attach(cfg: HydroConfig | None = None, log=print) -> dict:
    """``data/nz/hydro.pkl`` and ``hydro_summary.json`` for the national table. Returns the summary."""
    from .models import national_table
    from .national import nz_dir

    cfg = cfg or load_config()
    t = national_table(log=log)
    snaps = snap(t, cfg)
    h = classify(t, cfg, snaps)
    keep = [c for c in ("region", "seg_id", "gid", "vi") if c in t]
    out = pd.concat([t[keep].reset_index(drop=True), h.reset_index(drop=True)], axis=1)
    for c in ("region", "hydro", "hydro_scheme", "hydro_point"):
        out[c] = out[c].astype("category")
    out.to_pickle(table_path())
    s = summary(t, h, snaps, cfg)
    (nz_dir() / "hydro_summary.json").write_text(json.dumps(s, indent=1, ensure_ascii=False))
    missed = [f"{x.point.scheme}/{x.point.id}" for x in snaps if x.row < 0 or (x.point.river and not x.named)]
    log(f"hydro: {len(cfg.schemes)} schemes, {len(snaps)} points ({len(missed)} not on their named river: "
        f"{', '.join(missed) or 'none'}); " + ", ".join(f"{c} {v['reaches']} reaches / {v['km']:.0f} km"
                                                      for c, v in s["classes"].items()))
    return s


def columns(f: pd.DataFrame, cfg: HydroConfig | None = None) -> pd.DataFrame:
    """The hydro columns for ``f``: from ``data/nz/hydro.pkl`` when it lines up with ``f`` (same reaches, same
    order), else computed from the config now (a few seconds for the national table)."""
    p = table_path()
    if cfg is None and p.exists() and "seg_id" in f:
        h = pd.read_pickle(p)
        same = len(h) == len(f) and (h["seg_id"].to_numpy() == f["seg_id"].to_numpy()).all()
        if same and "region" in f:
            same = (h["region"].astype(str).to_numpy() == f["region"].astype(str).to_numpy()).all()
        if same:
            cols = h[["hydro", "hydro_scheme", "hydro_point", "hydro_share"]].set_index(f.index)
            return cols.astype({c: str for c in ("hydro", "hydro_scheme", "hydro_point")})
    return classify(f, cfg)[["hydro", "hydro_scheme", "hydro_point", "hydro_share"]]


def excluded(f: pd.DataFrame, classes: tuple[str, ...] | None = None) -> np.ndarray:
    """Reaches the audit and candidate lists leave out (``[audit] exclude``, default diverted and reservoir)."""
    classes = load_config().audit_exclude if classes is None else tuple(classes)
    if not classes:
        return np.zeros(len(f), dtype=bool)
    return columns(f)["hydro"].isin(classes).to_numpy()


def run_class(h: pd.DataFrame, rows, length: np.ndarray) -> tuple[str, str, float]:
    """(class, scheme, share of length) for a run: the hydro class covering most of its flagged length,
    ("", "", 0) when less than half the run is flagged."""
    rows = np.asarray(rows)
    w = length[rows]
    cls = h["hydro"].to_numpy()[rows]
    flagged = cls != ""
    frac = float(w[flagged].sum() / max(w.sum(), 1e-9))
    if frac < 0.5:
        return "", "", round(frac, 2)
    by = pd.Series(w[flagged]).groupby(cls[flagged]).sum()
    c = str(by.idxmax())
    sch = pd.Series(w[flagged & (cls == c)]).groupby(h["hydro_scheme"].to_numpy()[rows][flagged & (cls == c)]).sum()
    return c, str(sch.idxmax()), round(frac, 2)


def check(cfg: HydroConfig | None = None, log=print) -> pd.DataFrame:
    """Where every point snaps and how far each point's marking runs (for hand checks)."""
    from .models import national_table

    cfg = cfg or load_config()
    t = national_table(log=log)
    snaps = snap(t, cfg)
    h = classify(t, cfg, snaps)
    rows = []
    for s in snaps:
        p = s.point
        if s.row < 0:
            rows.append({"point": f"{p.scheme}/{p.id}", "role": p.role, "river": p.river, "snapped": "(none)"})
            continue
        mine = (h["hydro_scheme"].to_numpy() == p.scheme) & (h["hydro_point"].to_numpy() == p.id)
        rows.append({"point": f"{p.scheme}/{p.id}", "role": p.role, "river": p.river, "snapped": s.river,
                     "named": s.named, "dist_m": round(s.dist_m), "area_km2": round(s.area_km2, 1),
                     "class_here": h["hydro"].iat[s.row], "reaches": int(mine.sum()),
                     "km": round(float(t["length_m"].to_numpy()[mine].sum() / 1000.0), 1)})
    out = pd.DataFrame(rows)
    log(out.to_string(index=False))
    return out

