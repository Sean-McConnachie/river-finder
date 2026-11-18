"""Per-region files the web app reads in addition to the viewer files (``viewer_nz``, ``models
viewer``, ``external attach``). Written into the viewer data directory (default ``data/viewer_nz``):

| file | contents |
|---|---|
| ``<vid>_sections.json`` | known whitewater sections placed on the region's viewer reaches |
| ``<vid>_access.json`` | roads, 4WD/private roads and foot tracks in the region (OpenStreetMap via Overture) |

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


# ---------------------------------------------------------------------------- roads and tracks
ACCESS_CLASSES = ("road", "track", "foot")
MAJOR = {"motorway", "trunk", "primary", "secondary"}


TRAIL = {"path", "bridleway", "track"}   # OSM classes kept for unnamed foot lines (walking tracks, not footpaths)


def access_lines(g, simplify_m: float = 4.0) -> dict:
    """Compact JSON for the map. Unnamed service roads and unnamed urban footways are dropped;
    connected lines with the same class and name are merged (longer lines label better). Layout:
    ``{"classes", "names", "osm", "lines": [[class, name | -1, major 0/1, osm | -1, [lon, lat, ...]]]}``."""
    import shapely
    from pyproj import Transformer

    to_ll = Transformer.from_crs("EPSG:2193", "EPSG:4326", always_xy=True)
    names: list[str] = []
    nidx: dict[str, int] = {}
    osm: list[str] = []
    lines = []
    g = g.assign(major=g["osm_class"].isin(MAJOR).astype(int), osm=g.get("osm", ""))
    named = g["name"].astype(bool)
    service = (g["cls"] == "road") & (g["osm_class"] == "service")
    footpath = (g["cls"] == "foot") & ~g["osm_class"].isin(TRAIL)
    drop = ~named & (service | footpath)
    g = g[~drop]
    for (cls, name, major), part in g.groupby(["cls", "name", "major"], sort=False):
        merged = shapely.line_merge(shapely.union_all(part.geometry.to_numpy()))
        geoms = list(getattr(merged, "geoms", [merged]))
        first_osm = next((o for o in part["osm"] if o), "")
        for geom in geoms:
            if geom is None or geom.is_empty or geom.geom_type != "LineString":
                continue
            geom = geom.simplify(simplify_m)
            x, y = to_ll.transform(*np.asarray(geom.coords).T)
            flat = np.round(np.column_stack([x, y]).ravel(), 5).tolist()
            if name and name not in nidx:
                nidx[name] = len(names)
                names.append(name)
            oi = -1
            if first_osm:
                oi = len(osm)
                osm.append(first_osm)
            lines.append([ACCESS_CLASSES.index(cls), nidx[name] if name else -1, int(major), oi, flat])
    return {"classes": list(ACCESS_CLASSES), "names": names, "osm": osm, "lines": lines}


def write_access(data_dir: Path, log=print) -> dict:
    """``<vid>_access.json`` for every region in ``regions_index.json`` (needs ``external fetch``)."""
    import pyogrio
    from pyproj import Transformer

    from .national import nz_dir

    src = nz_dir() / "access.gpkg"
    if not src.exists():
        log("access: data/nz/access.gpkg missing (run river-finder external fetch)")
        return {}
    to_m = Transformer.from_crs("EPSG:4326", "EPSG:2193", always_xy=True)
    counts = {}
    for e in json.loads((data_dir / "regions_index.json").read_text()):
        (la0, lo0), (la1, lo1) = e["bounds"]
        box = to_m.transform_bounds(lo0, la0, lo1, la1, densify_pts=21)
        g = pyogrio.read_dataframe(src, bbox=box)
        out = access_lines(g)
        (data_dir / f"{e['id']}_access.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
        counts[e["id"]] = len(out["lines"])
        log(f"access: {e['id']} {len(g)} OSM lines -> {len(out['lines'])} map lines")
    return counts
