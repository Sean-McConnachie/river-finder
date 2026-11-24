"""Known runnability labels (labels/known_runs.toml) mapped onto reach segments."""

from __future__ import annotations

import functools
import math
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from pyproj import Transformer

from .regions import REPO_ROOT

UNKNOWN, RUNNABLE, NOT_RUNNABLE = 0, 1, 2
_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6}


@dataclass
class Section:
    region: str
    river: str
    status: str
    grade: str | None = None
    held_out: bool = False
    put_in: tuple | None = None
    take_out: tuple | None = None
    outlet: tuple | None = None
    upstream_km: float | None = None
    source: str = ""
    bbox: tuple | None = None  # WGS84 box limiting the name-only rule (national frames)
    coords: str = "sourced"    # put-in/take-out provenance: sourced | inferred | unstated | none
    extra: dict = field(default_factory=dict)


def parse_grade(text: str | None) -> float:
    """'3' -> 3, '4-4+' -> 4.17, 'III+' -> 3.33, 'V' -> 5. NaN if missing or unreadable."""
    if not text:
        return math.nan
    vals = []
    for part in re.split(r"\s*[-/–]\s*", text.strip().upper()):
        m = re.fullmatch(r"(VI|V|IV|III|II|I|\d(?:\.\d)?)\s*([+-]?)", part)
        if not m:
            continue
        base = float(_ROMAN.get(m.group(1), m.group(1)))
        vals.append(base + {"+": 1 / 3, "-": -1 / 3, "": 0.0}[m.group(2)])
    return float(np.mean(vals)) if vals else math.nan


def load_sections(path: Path | None = None) -> list[Section]:
    path = path or REPO_ROOT / "labels" / "known_runs.toml"
    if not path.exists():
        return []
    out = []
    for d in tomllib.loads(path.read_text()).get("section", []):
        known = {f for f in Section.__dataclass_fields__ if f not in ("extra", "bbox", "coords")}
        kw = {k: (tuple(v) if isinstance(v, list) else v) for k, v in d.items() if k in known}
        s = Section(**kw, extra={k: v for k, v in d.items() if k not in known})
        s.coords = "sourced" if (s.put_in or s.take_out or s.outlet) else "none"
        out.append(s)
    return out


# ---------------------------------------------------------------------------- name-rule pieces
# A section placed by river name picks up every steep reach with that name, which can be several pieces:
# steep bits of one river split by flatter or smaller reaches, but also a stream with the same name in
# another valley, or the other fork of a river whose branches share its name. ``one_path`` joins the
# pieces that follow each other down the river and keeps one downstream path.
GAP_REACHES = 2          # a gap of at most this many reaches joins two pieces, whatever the gap reaches are,
GAP_M = 1000.0           # and so does a gap of at most this length;
RIVER_GAP_M = 10_000.0   # a longer gap joins them only while it stays on the river (same name or unnamed)


def _links(rows: np.ndarray, down, length_m, names, name: str) -> np.ndarray:
    """For each reach of ``rows``, the position in ``rows`` of the first reach of ``rows`` below it, if the
    reaches in between pass the gap rule (``GAP_REACHES``, ``GAP_M``, ``RIVER_GAP_M``); -1 otherwise."""
    pos = {int(r): i for i, r in enumerate(rows)}
    par = np.full(len(rows), -1, dtype=np.int64)
    for i, r in enumerate(rows):
        cur, n, m, river = int(down[r]), 0, 0.0, True
        while cur >= 0 and cur not in pos:
            n += 1
            m += float(length_m[cur])
            river = river and (not names[cur] or names[cur] == name)
            if not (n <= GAP_REACHES or m <= GAP_M or (river and m <= RIVER_GAP_M)) or n > 5000:
                cur = -1
                break
            cur = int(down[cur])
        if cur >= 0:
            par[i] = pos[cur]
    return par


def _components(par: np.ndarray) -> list[np.ndarray]:
    parent = np.arange(len(par))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, j in enumerate(par):
        if j >= 0:
            parent[find(i)] = find(j)
    roots = np.array([find(i) for i in range(len(par))])
    return [np.flatnonzero(roots == r) for r in np.unique(roots)]


def one_path(rows, down, length_m, area, names, name: str, xy=None, points=(), outlet=None
             ) -> tuple[np.ndarray, dict]:
    """Keep one downstream path of a name-rule match.

    ``rows`` are the name-matched reaches (frame row indices); ``down``, ``length_m``, ``area`` and
    ``names`` are whole-frame arrays (``names`` as compared with ``name``; "" for unnamed reaches).
    Reaches are joined into pieces down the network (gap rule in ``_links``). The piece kept is the one
    nearest the section's ``points`` (NZTM x, y; needs ``xy``, the reach midpoints, shape (n, 2)); then,
    if the river's mouth is known (``outlet``, NZTM x, y), one whose water flows past it (so a stream
    with the same name in another catchment loses); then the longest (ties: larger catchment). If that
    piece forks (two branches with the name), only one branch is kept: the path from one top reach down
    to the piece's bottom, chosen by the points, then length. Returns the kept rows (sorted) and
    ``{"pieces", "branches", "dropped_reaches", "dropped_km", "gap_rows"}``: pieces and branches as
    found, before dropping; ``gap_rows`` are the reaches between kept reaches that the gap rule bridged
    (on the path, not in the match).
    """
    rows = np.unique(np.asarray(rows, dtype=np.int64))
    info = {"pieces": 0, "branches": 0, "dropped_reaches": 0, "dropped_km": 0.0,
            "gap_rows": np.array([], dtype=np.int64)}
    if not len(rows):
        return rows, info
    L = np.asarray(length_m, dtype=float)
    A = np.asarray(area, dtype=float)
    par = _links(rows, down, L, names, name)
    pts = np.asarray(points, dtype=float).reshape(-1, 2) if xy is not None else np.zeros((0, 2))

    def rank(members: np.ndarray, top: int | None = None):
        r = rows[members]
        near = mouth = 0.0
        if len(pts):   # summed distance from each point to its nearest reach, to 50 m
            d = np.hypot(xy[r, 0][:, None] - pts[None, :, 0], xy[r, 1][:, None] - pts[None, :, 1]).min(axis=0)
            near = float(np.round(d / 50.0).sum())
        if outlet is not None and xy is not None and top is None:
            # how close the piece's water passes to the river's mouth (the walk down to the network's end), to 100 m
            walk = [int(r[np.argmax(A[r])])]
            cur = int(down[walk[0]])
            while cur >= 0 and len(walk) < 50_000:
                walk.append(cur)
                cur = int(down[cur])
            mouth = float(np.round(np.hypot(xy[walk, 0] - outlet[0], xy[walk, 1] - outlet[1]).min() / 100.0))
        return (near, mouth, -float(L[r].sum()), -float(A[rows[top]] if top is not None else A[r].max()))

    pieces = _components(par)
    best = min(pieces, key=rank)
    inside = set(best.tolist())
    fed = {int(par[i]) for i in best if par[i] >= 0}
    heads = [int(i) for i in best if int(i) not in fed]
    paths = []
    for h in heads:
        p, j, seen = [], h, set()
        while j >= 0 and j in inside and j not in seen:
            seen.add(j)
            p.append(j)
            j = int(par[j])
        paths.append((np.array(p, dtype=np.int64), h))
    if len(paths) > 1:
        keep = min(paths, key=lambda ph: rank(ph[0], ph[1]))[0]
    else:
        keep = best   # one top reach: the piece is one path already (or a loop in a broken network)
    kept = np.sort(rows[keep])
    gone = np.setdiff1d(rows, kept)
    # the reaches the gap rule bridged between kept reaches (on the path, but not part of the match)
    within, gap = set(keep.tolist()), []
    for i in keep:
        if par[i] >= 0 and int(par[i]) in within:
            cur = int(down[rows[i]])
            while cur >= 0 and cur != rows[par[i]] and len(gap) < 100_000:
                gap.append(cur)
                cur = int(down[cur])
    info.update(pieces=len(pieces), branches=max(len(heads), 1), dropped_reaches=int(len(gone)),
                dropped_km=round(float(L[gone].sum()) / 1000.0, 2), gap_rows=np.unique(np.array(gap, dtype=np.int64)))
    return kept, info


@functools.cache
def _river_outlets() -> dict:
    from .regions import list_regions, load_region

    out = {}
    for rid in list_regions():
        for k in load_region(rid).known_runs:
            if len(k.outlet_wgs84) == 2 and all(math.isfinite(v) for v in k.outlet_wgs84):
                out[(rid, k.river)] = tuple(float(v) for v in k.outlet_wgs84)
    return out


def river_outlet(s: Section) -> tuple | None:
    """NZTM (x, y) of the river's mouth for a reference river (``regions/<region>.toml`` known_runs
    ``outlet_wgs84``; ``extra["reference_region"]`` when the section was moved to a national region), else
    None. The name rule uses it to tell the river from same-name streams in other catchments."""
    p = _river_outlets().get(((s.extra or {}).get("reference_region", s.region), s.river))
    return Transformer.from_crs("EPSG:4326", "EPSG:2193", always_xy=True).transform(*p) if p else None


def pieces_note(info: dict) -> str:
    """'' for a match that was one path, else e.g. '1 of 3 pieces' or '1 of 3 pieces, 1 of 2 branches'."""
    parts = []
    if info.get("pieces", 0) > 1:
        parts.append(f"1 of {info['pieces']} pieces")
    if info.get("branches", 0) > 1:
        parts.append(f"1 of {info['branches']} branches")
    return ", ".join(parts)


def _nearest_segment(g: pd.DataFrame, lonlat, river: str | None = None, max_m: float = 400.0) -> int:
    x, y = Transformer.from_crs("EPSG:4326", "EPSG:2193", always_xy=True).transform(*lonlat)
    cand = g[(g["area_km2"] >= 1.0) & ((g["river"] == river) if river else True)]
    if cand.empty:
        return -1
    from shapely.geometry import Point

    p = Point(x, y)
    d = cand.geometry.distance(p)
    i = d.idxmin()
    return int(i) if d[i] <= max_m else -1


def section_placement(g: pd.DataFrame, s: Section) -> tuple[list[int], str, dict]:
    """Row indices (positional == seg_id) of the segments a section covers, how they were placed ("put-in
    to take-out", "outlet + length", "name" or e.g. "name (1 of 3 pieces)") and, for the name rule, what
    ``one_path`` dropped."""
    down = g["down_seg"].to_numpy()
    up = g["up_seg"].to_numpy() if "up_seg" in g else None
    if s.put_in and s.take_out:
        a, b = _nearest_segment(g, s.put_in), _nearest_segment(g, s.take_out)
        out, cur = [], a
        while 0 <= cur and len(out) < 2000:
            out.append(cur)
            if cur == b:
                return out, "put-in to take-out", {}
            cur = down[cur]
        return [], "put-in to take-out", {}
    if s.outlet and s.upstream_km and up is not None:
        a = _nearest_segment(g, s.outlet, s.river)
        out, cur, km = [], a, 0.0
        L = g["length_m"].to_numpy()
        while cur >= 0 and km < s.upstream_km:
            out.append(cur)
            km += L[cur] / 1000.0
            cur = up[cur]
        return out, "outlet + length", {}
    grad = g["grad_s"] if "grad_s" in g else g["gradient_m_per_km"]
    m = ((g["river"] == s.river) & (grad >= 10.0) & (g["area_km2"] >= 2.0)).to_numpy().copy()
    pts = [p for p in (s.put_in, s.take_out, s.outlet) if p]
    mouth = river_outlet(s)
    need_xy = bool(pts) or mouth is not None
    mid = g.geometry.interpolate(0.5, normalized=True) if m.any() and (s.bbox is not None or need_xy) else None
    if s.bbox is not None and m.any():
        lon, lat = Transformer.from_crs("EPSG:2193", "EPSG:4326", always_xy=True).transform(
            mid.x.to_numpy(), mid.y.to_numpy())
        x0, y0, x1, y1 = s.bbox
        m &= (lon >= x0) & (lon <= x1) & (lat >= y0) & (lat <= y1)
    to_m = Transformer.from_crs("EPSG:4326", "EPSG:2193", always_xy=True)
    xy = np.column_stack([mid.x.to_numpy(), mid.y.to_numpy()]) if need_xy and mid is not None else None
    rows, info = one_path(np.flatnonzero(m), down, g["length_m"].to_numpy(), g["area_km2"].to_numpy(),
                          g["river"].fillna("").to_numpy(), s.river, xy=xy,
                          points=[to_m.transform(*p) for p in pts], outlet=mouth)
    note = pieces_note(info)
    return [int(r) for r in rows], f"name ({note})" if note else "name", info


def section_segments(g: pd.DataFrame, s: Section) -> list[int]:
    """Row indices (positional == seg_id) of the segments a section covers (``section_placement``)."""
    return section_placement(g, s)[0]


def segment_labels(g: pd.DataFrame, region: str, sections: list[Section] | None = None):
    """Per-segment arrays: status (0 unknown / 1 runnable / 2 not runnable), grade (NaN if unknown),
    held_out flag, and label index into the region's sections (-1 none)."""
    g = g.reset_index(drop=True)
    secs = [s for s in (sections if sections is not None else load_sections()) if s.region == region]
    status = np.zeros(len(g), dtype=int)
    grade = np.full(len(g), np.nan)
    held = np.zeros(len(g), dtype=bool)
    lab = np.full(len(g), -1)
    for k, s in enumerate(secs):
        idx = section_segments(g, s)
        code = NOT_RUNNABLE if s.status == "not_runnable" else RUNNABLE
        status[idx] = np.maximum(status[idx], code)
        gr = parse_grade(s.grade)
        if math.isfinite(gr):
            grade[idx] = np.fmax(grade[idx], gr)
        held[idx] |= s.held_out
        lab[idx] = k
    return status, grade, held, lab, secs
