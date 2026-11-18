"""Per-region files the web app reads in addition to the viewer files (``viewer_nz``, ``models
viewer``, ``external attach``). Written into the viewer data directory (default ``data/viewer_nz``):

| file | contents |
|---|---|
| ``<vid>_sections.json`` | known whitewater sections placed on the region's viewer reaches |

Sections come from ``labels/known_runs.toml`` (local knowledge) and ``labels/guide_research.json``
(NZ RiverGuide, packraftingtrips.nz and others), placed with ``models.map_section``. Each record:
``id, river, section, grade, gnum`` (stated grade as a number), ``gmin, gmax`` (incl. hardest
rapid), ``src`` (riverguide | packrafting | other | local),
``srcName, desc, putIn, takeOut`` ([lat, lon]), ``putInDesc, takeOutDesc, coords`` (sourced |
inferred | none), ``gauge, flow, len`` (km), ``time, hazards, deaths, popularity, links``
([{t, u}]), ``how`` (placement method) and ``m`` (viewer reach indices).
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import numpy as np

from .labels import Section, load_sections, parse_grade
from .regions import REPO_ROOT, list_regions, load_region

RESEARCH = REPO_ROOT / "labels" / "guide_research.json"


def source_group(s: Section) -> str:
    """riverguide | packrafting | local | other: the Known-rivers layer's "by source" groups."""
    p = str((s.extra or {}).get("primary_source") or "").lower()
    if not s.extra:
        return "local"
    if "riverguide" in p:
        return "riverguide"
    if "packrafting" in p:
        return "packrafting"
    return "other"


def _research_records() -> dict:
    if not RESEARCH.exists():
        return {}
    return {r["id"]: r for r in json.loads(RESEARCH.read_text())["sections"]}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 2) if np.isfinite(f) else None


def section_record(s: Section, members: list[int], how: str, research: dict | None = None) -> dict:
    ex = s.extra or {}
    rec = (research or {}).get(ex.get("id"), {})
    grp = source_group(s)
    latlon = lambda p: [round(p[1], 6), round(p[0], 6)] if p else None  # noqa: E731 (lon, lat) -> [lat, lon]
    return {
        "id": ex.get("id") or f"local:{s.region}:{_slug(s.river)}",
        "river": s.river, "section": ex.get("section") or "", "grade": s.grade or "",
        "gnum": _num(parse_grade(s.grade)),
        "gmin": _num(ex.get("grade_min")), "gmax": _num(ex.get("grade_max")),
        "src": grp,
        "srcName": ex.get("primary_source") or ("Local knowledge (known_runs.toml)" if grp == "local" else "Other"),
        "desc": ex.get("description") or (s.source if grp == "local" else ""),
        "putIn": latlon(s.put_in), "takeOut": latlon(s.take_out),
        "putInDesc": ex.get("put_in_description") or "", "takeOutDesc": ex.get("take_out_description") or "",
        "coords": s.coords, "gauge": ex.get("gauge") or "", "flow": ex.get("flow_notes") or "",
        "len": _num(ex.get("length_km")), "time": ex.get("time") or "",
        "hazards": list(ex.get("hazards") or []), "deaths": len(ex.get("deaths") or []),
        "popularity": ex.get("popularity") or "", "status": s.status,
        "links": [{"t": x.get("title") or x.get("url"), "u": x["url"]} for x in rec.get("sources", []) if x.get("url")],
        "how": how, "m": [int(v) for v in members],
    }


def _in_box(s: Section, box, pad: float = 0.02) -> bool:
    x0, y0, x1, y1 = box
    pts = [p for p in (s.put_in, s.take_out, s.outlet) if p]
    return any(x0 - pad <= p[0] <= x1 + pad and y0 - pad <= p[1] <= y1 + pad for p in pts)


def reference_sections(rid: str) -> list[Section]:
    """Sections that belong to a 5 m reference area: its known_runs.toml entries (name rule limited
    to its box) and research sections with a point inside its box."""
    from .research import load_research

    box = tuple(load_region(rid).bbox_wgs84)
    local = [replace(s, bbox=box) for s in load_sections() if s.region == rid]
    research = [replace(s, bbox=box) for s in load_research() if _in_box(s, box)]
    return local + research


def _records(frame, sections: list[Section], research: dict) -> list[dict]:
    from .models import _Index, map_section

    idx = _Index(frame)
    vi = frame["vi"].to_numpy()
    out = []
    for s in sections:
        rows, how = map_section(frame, s, idx)
        members = [int(vi[r]) for r in rows if vi[r] >= 0]
        if members:
            out.append(section_record(s, members, how, research))
    return out


def write_sections(data_dir: Path, log=print) -> dict:
    """``<vid>_sections.json`` for every region in ``regions_index.json``."""
    from .models import all_sections, national_table, reference_frame
    from .nzscore import slug

    index = json.loads((data_dir / "regions_index.json").read_text())
    ids = {e["id"] for e in index}
    research = _research_records()
    counts = {}
    refs = [r for r in list_regions() if r in ids]
    for rid in refs:
        recs = _records(reference_frame(rid, guide_regions=False), reference_sections(rid), research)
        (data_dir / f"{rid}_sections.json").write_text(json.dumps(recs, ensure_ascii=False, separators=(",", ":")))
        counts[rid] = len(recs)
        log(f"sections: {rid} {len(recs)}")
    national = [e["id"] for e in index if e["id"] not in refs]
    if national:
        t = national_table()
        sections = all_sections()
        from .models import _Index, map_section

        idx = _Index(t)
        mapped = [(s, *map_section(t, s, idx)) for s in sections]
        reg, vi = t["region"].to_numpy(), t["vi"].to_numpy()
        by_vid: dict[str, list[dict]] = {}
        for s, rows, how in mapped:
            for name in sorted({str(reg[r]) for r in rows}):
                members = [int(vi[r]) for r in rows if reg[r] == name and vi[r] >= 0]
                if members:
                    by_vid.setdefault(slug(name), []).append(section_record(s, members, how, research))
        for vid in national:
            recs = by_vid.get(vid, [])
            (data_dir / f"{vid}_sections.json").write_text(json.dumps(recs, ensure_ascii=False, separators=(",", ":")))
            counts[vid] = len(recs)
            log(f"sections: {vid} {len(recs)}")
    return counts
