"""Known runnability labels (labels/known_runs.toml) mapped onto reach segments."""

from __future__ import annotations

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
        known = {f for f in Section.__dataclass_fields__ if f not in ("extra", "bbox")}
        kw = {k: (tuple(v) if isinstance(v, list) else v) for k, v in d.items() if k in known}
        out.append(Section(**kw, extra={k: v for k, v in d.items() if k not in known}))
    return out


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


def section_segments(g: pd.DataFrame, s: Section) -> list[int]:
    """Row indices (positional == seg_id) of the segments a section covers."""
    down = g["down_seg"].to_numpy()
    up = g["up_seg"].to_numpy() if "up_seg" in g else None
    if s.put_in and s.take_out:
        a, b = _nearest_segment(g, s.put_in), _nearest_segment(g, s.take_out)
        out, cur = [], a
        while 0 <= cur and len(out) < 2000:
            out.append(cur)
            if cur == b:
                return out
            cur = down[cur]
        return []
    if s.outlet and s.upstream_km and up is not None:
        a = _nearest_segment(g, s.outlet, s.river)
        out, cur, km = [], a, 0.0
        L = g["length_m"].to_numpy()
        while cur >= 0 and km < s.upstream_km:
            out.append(cur)
            km += L[cur] / 1000.0
            cur = up[cur]
        return out
    grad = g["grad_s"] if "grad_s" in g else g["gradient_m_per_km"]
    m = ((g["river"] == s.river) & (grad >= 10.0) & (g["area_km2"] >= 2.0)).to_numpy()
    if s.bbox is not None and m.any():
        mid = g.geometry.interpolate(0.5, normalized=True)
        lon, lat = Transformer.from_crs("EPSG:2193", "EPSG:4326", always_xy=True).transform(
            mid.x.to_numpy(), mid.y.to_numpy())
        x0, y0, x1, y1 = s.bbox
        m &= (lon >= x0) & (lon <= x1) & (lat >= y0) & (lat <= y1)
    return list(np.flatnonzero(m))


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
