"""Per-region files the web app reads in addition to the viewer files (``viewer_nz``, ``models
viewer``, ``external attach``). Written into the viewer data directory (default ``data/viewer_nz``):

| file | contents |
|---|---|
| ``<vid>_sections.json`` | known whitewater sections placed on the region's viewer reaches |
| ``<vid>_access.json`` | roads, 4WD/private roads and foot tracks in the region (OpenStreetMap via Overture) |
| ``gauges.json`` | national river-flow gauges (NIWA flood-statistics sites) and rain gauges (NIWA CliFlo) |
| ``<vid>_hydro.json`` | hydro-diverted, regulated and reservoir reaches (``hydro.py``, ``config/hydro.toml``) |
| ``<vid>_aerial.json`` | LINZ aerial photo survey over each viewer reach (``aerial.py``; ``write_aerial``) |

Sections come from ``labels/known_runs.toml`` (local knowledge) and ``labels/guide_research.json``
(NZ RiverGuide, packraftingtrips.nz and others), placed with ``models.map_section``. Each record:
``id, river, section, grade, gnum`` (stated grade as a number), ``gmin, gmax`` (incl. hardest
rapid), ``src`` (riverguide | packrafting | other | local),
``srcName, desc, putIn, takeOut`` ([lat, lon]), ``putInDesc, takeOutDesc, coords`` (sourced |
inferred | none), ``gauge, flow, len`` (km), ``time, hazards, deaths, popularity, links``
([{t, u}]), ``how`` (placement method, e.g. "put-in to take-out" or "name (1 of 3 pieces)"),
``dropped``, ``gap`` and ``m`` (viewer reach indices).

``dropped`` flags a section placed by river name whose name match was not one downstream path: null
when nothing was dropped, else ``{pieces, branches, km}``: how many other pieces (same-name reaches
that do not join this one down the river, e.g. a stream with the same name in another valley) and
other branches (the other fork, where the river's branches share its name) were left out, and their
length. The section is the one path kept (``labels.one_path``); ``gap`` (viewer reach indices, often
empty) are the flatter or smaller reaches between its steep pieces along that path: not part of the
section, but they join its pieces into one line down the river.
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


def dropped_record(info: dict | None) -> dict | None:
    """``{pieces, branches, km}`` left out by the name rule (``labels.one_path``), None if nothing was."""
    if not info or not info.get("dropped_reaches"):
        return None
    return {"pieces": max(int(info.get("pieces", 1)) - 1, 0), "branches": max(int(info.get("branches", 1)) - 1, 0),
            "km": round(float(info.get("dropped_km", 0.0)), 2)}


def section_record(s: Section, members: list[int], how: str, research: dict | None = None,
                   info: dict | None = None, gap: list[int] | None = None) -> dict:
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
        "how": how, "dropped": dropped_record(info), "gap": [int(v) for v in (gap or [])],
        "m": [int(v) for v in members],
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
        info: dict = {}
        rows, how = map_section(frame, s, idx, info)
        members = [int(vi[r]) for r in rows if vi[r] >= 0]
        gap = [int(vi[r]) for r in info.get("gap_rows", []) if vi[r] >= 0]
        if members:
            out.append(section_record(s, members, how, research, info, gap))
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
        infos: list[dict] = [{} for _ in sections]
        mapped = [(s, *map_section(t, s, idx, info), info) for s, info in zip(sections, infos, strict=True)]
        reg, vi = t["region"].to_numpy(), t["vi"].to_numpy()
        by_vid: dict[str, list[dict]] = {}
        for s, rows, how, info in mapped:
            for name in sorted({str(reg[r]) for r in rows}):
                members = [int(vi[r]) for r in rows if reg[r] == name and vi[r] >= 0]
                gap = [int(vi[r]) for r in info.get("gap_rows", []) if reg[r] == name and vi[r] >= 0]
                if members:
                    by_vid.setdefault(slug(name), []).append(section_record(s, members, how, research, info, gap))
        for vid in national:
            recs = by_vid.get(vid, [])
            (data_dir / f"{vid}_sections.json").write_text(json.dumps(recs, ensure_ascii=False, separators=(",", ":")))
            counts[vid] = len(recs)
            log(f"sections: {vid} {len(recs)}")
    return counts


# ---------------------------------------------------------------------------- hydro-affected reaches
def hydro_record(h, vi, cfg) -> dict:
    """``<vid>_hydro.json`` for one region: ``h`` the ``hydro.classify`` columns of its reaches, ``vi`` their viewer
    index (-1 not shown). Layout: ``{"classes": ["", "diverted", "regulated", "reservoir"], "exclude": [classes the
    lists leave out of new leads], "notes": {class: text}, "schemes": [{id, name, operator, note, src: [url]}],
    "r": [[viewer reach, class index, scheme index, share %], ...]}``, flagged reaches only."""
    from . import hydro as H

    cls = h["hydro"].astype(str).to_numpy()
    sch = h["hydro_scheme"].astype(str).to_numpy()
    share = h["hydro_share"].to_numpy(dtype=float)
    vi = np.asarray(vi)
    rows = np.flatnonzero((cls != "") & (vi >= 0))
    ids = sorted({sch[r] for r in rows})
    sidx = {s: i for i, s in enumerate(ids)}
    by_id = {s.id: s for s in cfg.schemes}
    rows = rows[np.argsort(vi[rows])]
    schemes = [{"id": s, "name": by_id[s].name, "operator": by_id[s].operator, "note": " ".join(by_id[s].note.split()),
                "src": list(by_id[s].sources)} for s in ids]
    return {"classes": list(H.CLASSES), "exclude": list(cfg.audit_exclude), "notes": dict(H.NOTES), "schemes": schemes,
            "r": [[int(vi[r]), H.CLASSES.index(cls[r]), sidx[sch[r]], round(100 * min(float(share[r]), 1.0))]
                  for r in rows]}


def write_hydro(data_dir: Path, log=print) -> dict:
    """``<vid>_hydro.json`` (see ``hydro_record``) for every region in ``regions_index.json``: national regions from
    ``data/nz/hydro.pkl`` (``hydro attach``; computed now if it is missing or stale), 5 m reference areas
    classified on their own network. Off with ``[web] enabled = false`` in ``config/hydro.toml``."""
    from . import hydro as H
    from .models import national_table, reference_frame
    from .nzscore import slug

    cfg = H.load_config()
    if not cfg.web:
        log("hydro: [web] enabled = false in config/hydro.toml; no <region>_hydro.json written")
        return {}
    index = json.loads((data_dir / "regions_index.json").read_text())
    refs = set(list_regions())
    counts = {}
    nat = [e["id"] for e in index if e["id"] not in refs]
    if nat:
        t = national_table()
        h = H.columns(t)
        reg = t["region"].map({r: slug(r) for r in t["region"].unique()}).to_numpy()
        for vid in nat:
            sel = np.flatnonzero(reg == vid)
            rec = hydro_record(h.iloc[sel], t["vi"].to_numpy()[sel], cfg)
            (data_dir / f"{vid}_hydro.json").write_text(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
            counts[vid] = len(rec["r"])
    for e in index:
        if e["id"] in refs:
            f = reference_frame(e["id"], guide_regions=False)
            rec = hydro_record(H.classify(f, cfg), f["vi"].to_numpy(), cfg)
            (data_dir / f"{e['id']}_hydro.json").write_text(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
            counts[e["id"]] = len(rec["r"])
    log("hydro: " + ", ".join(f"{k} {v}" for k, v in counts.items()) + " flagged viewer reaches")
    return counts


# ---------------------------------------------------------------------------- aerial photo coverage
def unpack_coords(blob: bytes, head: dict) -> list[np.ndarray]:
    """The reach lines (``[[lon, lat], ...]`` arrays, viewer order) of a ``viewer_nz.pack_segments`` buffer."""
    n, C = int(head["n"]), head["coords"]
    cnt = np.frombuffer(blob, "<u2", n, C["count"]).astype(np.int64)
    first = np.frombuffer(blob, "<i4", 2 * n, C["first"]).reshape(n, 2).astype(np.int64)
    d = np.frombuffer(blob, "<i2", 2 * int(C["ndelta"]), C["delta"]).reshape(-1, 2).astype(np.int64)
    out, p = [], 0
    for i in range(n):
        k = int(cnt[i]) - 1
        out.append(np.vstack([first[i], first[i] + np.cumsum(d[p:p + k], axis=0)]) * C["scale"])
        p += k
    return out


def reach_lines(data_dir: Path, entry: dict) -> list[np.ndarray] | None:
    """A region's viewer reaches as ``[[lon, lat], ...]`` arrays in viewer order (``Region.segments.c``): inline
    in ``<vid>.json`` (5 m reference areas) or packed in ``<vid>_seg.txt``; None when the files are missing."""
    import base64

    src = data_dir / entry.get("src", f"{entry['id']}.json")
    if not src.exists():
        return None
    meta = json.loads(src.read_text())
    S = meta.get("segments")
    if isinstance(S, dict) and "c" in S:
        return [np.asarray(c, dtype=float).reshape(-1, 2) for c in S["c"]]
    if meta.get("segB64") and meta.get("segHead") and (data_dir / meta["segB64"]).exists():
        return unpack_coords(base64.b64decode((data_dir / meta["segB64"]).read_text()), meta["segHead"])
    return None


def _to_nztm(lines: list[np.ndarray]) -> list[np.ndarray]:
    from pyproj import Transformer

    if not lines:
        return []
    tr = Transformer.from_crs("EPSG:4326", "EPSG:2193", always_xy=True)
    allc = np.concatenate(lines)
    x, y = tr.transform(allc[:, 0], allc[:, 1])
    return np.split(np.column_stack([x, y]), np.cumsum([len(c) for c in lines])[:-1])


def write_aerial(data_dir: Path, refresh: bool = False, log=print) -> dict:
    """``<vid>_aerial.json`` for every region in ``regions_index.json``: the LINZ aerial photo survey over each
    viewer reach (``aerial.py``; the file format is in ``web/README.md``). Needs no key; the STAC survey table,
    the Basemaps layer list and the footprints are cached in ``data/nz/aerial/`` (``refresh`` fetches the tables
    again)."""
    from . import aerial as A

    ranked, rule = A.rank(A.surveys(refresh, log), A.basemaps_layers(refresh, log), log=log)
    log(f"aerial: {len(ranked)} surveys can be on top at zoom {A.ZOOM} (ranked by {rule})")
    index = json.loads((data_dir / "regions_index.json").read_text())
    boxes = [[e["bounds"][0][1], e["bounds"][0][0], e["bounds"][1][1], e["bounds"][1][0]] for e in index]
    need = [r for r in ranked if any(A.overlaps(r["bbox"], b) for b in boxes)]
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(8) as ex:
        fps = dict(zip([r["slug"] for r in need], ex.map(A.footprint, need), strict=True))
    counts = {}
    for e, box in zip(index, boxes, strict=True):
        lines = reach_lines(data_dir, e)
        if lines is None:
            log(f"aerial: {e['id']} skipped (no viewer reaches)")
            continue
        sub = [r for r in need if A.overlaps(r["bbox"], box)]
        rec = A.region_record(_to_nztm(lines), sub, [fps.get(r["slug"]) for r in sub], rule)
        path = data_dir / f"{e['id']}_aerial.json"
        path.write_text(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
        st = rec["stats"]
        counts[e["id"]] = st
        log(f"aerial: {e['id']} {st['covered']}/{st['reaches']} reaches ({st['kmCovered']:.0f} of {st['km']:.0f} km) "
            f"under {len(rec['surveys'])} surveys, {path.stat().st_size / 1e3:.0f} kB")
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


# ---------------------------------------------------------------------------- gauges
ARC = "https://services3.arcgis.com/fp1tibNcN9mbExhG/ArcGIS/rest/services"
FLOW_GAUGES = f"{ARC}/NZ_Flood_Statistics_Henderson_Collins_V2_FlowGauges_Layer_WFL2/FeatureServer/0"
RAIN_GAUGES = f"{ARC}/Cliflo_Rain_Stations_WFL1/FeatureServer/0"


def fetch_gauges(log=print) -> dict:
    """River-flow gauges (the 634 sites behind NIWA's Henderson & Collins flood statistics, with
    operator and flood quantiles) and NIWA CliFlo rain stations, as compact lists.

    ``flow``: ``[site no, name, lat, lon, council, operator, funder, area km2, years, mean annual flood,
    Q10, Q100]``; ``rain``: ``[agent no, name, lat, lon, owner]``."""
    from pyproj import Transformer

    from .national import fetch_layer

    to_ll = Transformer.from_crs("EPSG:2193", "EPSG:4326", always_xy=True)
    flow = []
    for f in fetch_layer(f"{FLOW_GAUGES}/query", "Siteno,Name,NZTM_E,NZTM_N,Region,Operator,Funder,Area_km2,No_years,"
                         "Data_2_33y,Data_10y,Data_100y", geometry=False, log=log):
        a = f["properties"]
        lon, lat = to_ll.transform(a["NZTM_E"], a["NZTM_N"])
        flow.append([a["Siteno"], a["Name"], round(lat, 5), round(lon, 5), a["Region"] or "", a["Operator"] or "",
                     a["Funder"] or "", _num(a["Area_km2"]), a["No_years"], _num(a["Data_2_33y"]), _num(a["Data_10y"]),
                     _num(a["Data_100y"])])
    rain = [[a["AGENT_NO"], a["NAME"], round(a["LAT"], 5), round(a["LONGT"], 5), a["OWNER"] or ""]
            for a in (f["properties"] for f in fetch_layer(f"{RAIN_GAUGES}/query", "AGENT_NO,NAME,LAT,LONGT,OWNER",
                                                           geometry=False, log=log))]
    out = {"flow": flow, "rain": rain, "sources": {"flow": FLOW_GAUGES, "rain": RAIN_GAUGES}}
    log(f"gauges: {len(flow)} river-flow gauges, {len(rain)} rain gauges")
    return out


def write_gauges(data_dir: Path, refresh: bool = False, log=print) -> int:
    """``gauges.json`` into the viewer data directory (cached in ``data/nz/gauges.json``)."""
    from .national import nz_dir

    cache = nz_dir() / "gauges.json"
    if refresh or not cache.exists():
        cache.write_text(json.dumps(fetch_gauges(log), ensure_ascii=False, separators=(",", ":")))
    (data_dir / "gauges.json").write_text(cache.read_text())
    return len(json.loads(cache.read_text())["flow"])


# ---------------------------------------------------------------------------- satellite
# EOxCloudless (Sentinel-2 cloudless) by EOX, the 2025 edition: the newest cloud-free mosaic, ~10 m (zoom 14),
# CC BY-NC-SA 4.0 (attribution, non-commercial, share-alike; 2016 is CC BY but too old for rivers that move).
# Tiles are Web Mercator, {z}/{row}/{col}.
S2_LAYER = "s2cloudless-2025_3857"
S2_TILES = "https://tiles.maps.eox.at/wmts/1.0.0/{layer}/default/g/{z}/{y}/{x}.jpg"
S2_ATTRIBUTION = ("EOxCloudless 2025 (https://cloudless.eox.at) by EOX IT Services GmbH, contains modified "
                  "Copernicus Sentinel data 2025, CC BY-NC-SA 4.0")
_ORIGIN = 20037508.342789244


def _tile(z: int, x: int, y: int, layer: str, cache: Path):
    import urllib.request

    from PIL import Image

    p = cache / layer / str(z) / str(x) / f"{y}.jpg"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        url = S2_TILES.format(layer=layer, z=z, x=x, y=y)
        req = urllib.request.Request(url, headers={"User-Agent": "river-finder"})
        for k in range(4):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    p.write_bytes(r.read())
                break
            except Exception:  # noqa: BLE001 (retry, then leave the tile blank)
                if k == 3:
                    return None
    try:
        return Image.open(p).convert("RGB")
    except OSError:
        return None


def satellite_image(merc, max_px: int = 4000, layer: str = S2_LAYER, max_zoom: int = 14, cache: Path | None = None,
                    workers: int = 6):
    """A satellite mosaic exactly covering a Web-Mercator box ``(x0, y0, x1, y1)``, at most ``max_px``
    on its long side (tiles at the zoom whose pixels are just finer than the output's)."""
    import math
    from concurrent.futures import ThreadPoolExecutor

    from PIL import Image

    from .national import nz_dir

    cache = cache or nz_dir() / "s2tiles"
    x0, y0, x1, y1 = merc
    scale = max(x1 - x0, y1 - y0) / max_px
    out = (max(1, round((x1 - x0) / scale)), max(1, round((y1 - y0) / scale)))
    z = int(min(max_zoom, max(0, math.ceil(math.log2(2 * _ORIGIN / 256 / scale)))))
    ts = 2 * _ORIGIN / 2 ** z
    tx0, tx1 = int((x0 + _ORIGIN) // ts), int((x1 + _ORIGIN) // ts)
    ty0, ty1 = int((_ORIGIN - y1) // ts), int((_ORIGIN - y0) // ts)
    mosaic = Image.new("RGB", ((tx1 - tx0 + 1) * 256, (ty1 - ty0 + 1) * 256))
    jobs = [(x, y) for x in range(tx0, tx1 + 1) for y in range(ty0, ty1 + 1)]
    with ThreadPoolExecutor(workers) as ex:
        for (x, y), im in zip(jobs, ex.map(lambda j: _tile(z, j[0], j[1], layer, cache), jobs), strict=True):
            if im is not None:
                mosaic.paste(im, ((x - tx0) * 256, (y - ty0) * 256))
    px = 256 / ts
    box = ((x0 + _ORIGIN) * px - tx0 * 256, (_ORIGIN - y1) * px - ty0 * 256,
           (x1 + _ORIGIN) * px - tx0 * 256, (_ORIGIN - y0) * px - ty0 * 256)
    return mosaic.resize(out, Image.Resampling.LANCZOS, box=box), z, len(jobs)


def write_satellite(data_dir: Path, max_px: int = 4000, log=print) -> dict:
    """``<vid>_sat.jpg`` per region: the satellite base map for hosts that block tile servers (the
    claude.ai artifact), on the same Web-Mercator box as the region's terrain images."""
    counts = {}
    for e in json.loads((data_dir / "regions_index.json").read_text()):
        meta = json.loads((data_dir / e.get("src", f"{e['id']}.json")).read_text())
        img, z, n = satellite_image(meta["merc"], max_px)
        img.save(data_dir / f"{e['id']}_sat.jpg", format="JPEG", quality=80, optimize=True, progressive=True)
        counts[e["id"]] = img.size
        log(f"satellite: {e['id']} {img.size[0]}x{img.size[1]} px from {n} tiles at zoom {z}")
    return counts
