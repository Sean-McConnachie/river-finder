"""LINZ aerial photo coverage per reach, for the map's "Aerial (LINZ)" base map (bead rf-aq6.29).

Source: LINZ's public imagery bucket ``s3://nz-imagery`` (read over HTTPS, no key): a STAC catalog with one
collection per survey. Each collection has ``linz:geospatial_category`` (``rural-aerial-photos``,
``urban-aerial-photos``, ``ancillary-aerial-photos`` for storm and flood flights, satellite, historic scans),
``gsd`` (m), a capture interval and a ``capture-area.geojson`` asset, the survey's footprint. Only the RGB
collections are read (not the near-infrared copies), never satellite or historic imagery. CC BY 4.0.

What the map shows is the LINZ Basemaps ``aerial`` tile set: one mosaic of these surveys over a 10 m satellite
background, rural photos from zoom 13, urban photos from zoom 14, later layers drawn on top (usually the newest).
Its layer list (``linz/basemaps-config`` on GitHub, ``config/tileset/aerial.json``) names the surveys it draws
and their order. With that list, a reach's survey is the one Basemaps draws on top, at zoom ``ZOOM``, over most
of the reach, so the card describes the photo people see. Without it (``rank = "gsd"``) the finest rural or urban
survey, then the newest, is used.

Each reach is sampled every ``STEP_M`` metres (sample points at the middle of equal pieces); each point takes the
top survey whose footprint contains it. ``share`` = covered points / all points of the reach.

Cache (``data/nz/aerial/``, gitignored): ``surveys.json`` (the survey table), ``basemaps_aerial.json`` (the
Basemaps layer list), ``capture/<slug>.geojson`` (footprints, WGS84). ``river-finder web data --only aerial``
fetches what is missing (about 115 MB of STAC JSON and 14 MB of footprints the first time, about a minute)
and then works offline; ``--refresh`` fetches the tables again.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

BASE = "https://nz-imagery.s3-ap-southeast-2.amazonaws.com"
CATEGORIES = ("rural-aerial-photos", "urban-aerial-photos", "ancillary-aerial-photos")
FALLBACK_CATS = ("rural", "urban")   # used without the Basemaps layer list
BASEMAPS_CONFIG = "https://raw.githubusercontent.com/linz/basemaps-config/master/config/tileset/aerial.json"
ZOOM = 16       # the zoom whose top survey a reach records (rural photos start at 13, urban at 14)
MIN_ZOOM = 13   # Basemaps aerial draws photos from this zoom; below it, 10 m satellite
STEP_M = 25.0
LICENCE = "CC BY 4.0"
ATTRIBUTION = "LINZ aerial photos (s3://nz-imagery; LINZ Basemaps aerial), CC BY 4.0"
# Historic scans are titled by survey number ("SN8298", "SNC50347"): not current photos.
_HISTORIC = re.compile(r"\bSNC?\d+[A-Za-z]?\b")
_NZ = ZoneInfo("Pacific/Auckland")


def cache_dir() -> Path:
    from .national import nz_dir

    d = nz_dir() / "aerial"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get(url: str, tries: int = 4, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "river-finder"})
    for k in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception:  # noqa: BLE001 (network: retry, then re-raise)
            if k == tries - 1:
                raise
            time.sleep(2 * (k + 1))
    return b""


def _abs(base_url: str, href: str) -> str:
    return href if href.startswith("http") else base_url.rsplit("/", 1)[0] + "/" + href.removeprefix("./")


def _local_date(t: str | None) -> str:
    """STAC times are UTC (NZ midnight is 11:00 or 12:00 Z the day before): the NZ calendar date."""
    if not t:
        return ""
    return datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(_NZ).date().isoformat()


def candidate(link: dict) -> bool:
    """A catalog child worth opening: RGB, not a historic scan and not satellite imagery (by title)."""
    t, h = link.get("title", ""), link.get("href", "")
    return "/rgb/" in h and not _HISTORIC.search(t) and "satellite" not in t.lower()


def short_name(title: str) -> str:
    """"Waikato 0.3m Rural Aerial Photos (2021-2024)" -> "Waikato Rural Aerial Photos"."""
    t = re.sub(r"\s*-\s*Draft$", "", title.strip())
    t = re.sub(r"\s*\([^)]*\)\s*$", "", t)
    return re.sub(r"\s+", " ", re.sub(r"\b\d+(\.\d+)?m\b", "", t)).strip()


def survey_record(coll: dict, url: str) -> dict | None:
    """One row of the survey table from a STAC collection, or None when it is not an aerial photo survey."""
    cat = coll.get("linz:geospatial_category", "")
    if cat not in CATEGORIES:
        return None
    start, end = (coll["extent"]["temporal"]["interval"][0] + [None, None])[:2]
    area = (coll.get("assets") or {}).get("capture_area")
    slug = coll.get("linz:slug") or url.split("/")[-4]
    title = coll.get("title", slug)
    return {"slug": slug, "title": title, "name": short_name(title), "gsd": float(coll["gsd"]),
            "start": _local_date(start), "end": _local_date(end or start), "cat": cat.split("-")[0],
            "region": coll.get("linz:region", ""), "lifecycle": coll.get("linz:lifecycle", ""),
            "collection": url, "capture": _abs(url, area["href"]) if area else None,
            "bbox": coll["extent"]["spatial"]["bbox"][0]}


def fetch_surveys(log=print, workers: int = 8) -> list[dict]:
    """The survey table from the STAC catalog (rural, urban and ancillary aerial photo collections)."""
    cat = json.loads(_get(f"{BASE}/catalog.json"))
    kids = [ln for ln in cat["links"] if ln["rel"] == "child" and candidate(ln)]
    log(f"aerial: {len(kids)} candidate collections in s3://nz-imagery")

    def one(ln):
        url = _abs(f"{BASE}/catalog.json", ln["href"])
        try:
            return survey_record(json.loads(_get(url)), url)
        except Exception as e:  # noqa: BLE001 (one broken collection should not stop the table)
            log(f"aerial: {url} skipped ({type(e).__name__})")
            return None

    with ThreadPoolExecutor(workers) as ex:
        rows = [r for r in ex.map(one, kids) if r]
    rows.sort(key=lambda r: r["slug"])
    log(f"aerial: {len(rows)} aerial photo surveys")
    return rows


def surveys(refresh: bool = False, log=print) -> list[dict]:
    """The cached survey table (``data/nz/aerial/surveys.json``), fetched when missing or with ``refresh``."""
    p = cache_dir() / "surveys.json"
    if refresh or not p.exists():
        p.write_text(json.dumps(fetch_surveys(log), ensure_ascii=False, indent=0))
    return json.loads(p.read_text())


def basemaps_layers(refresh: bool = False, log=print) -> list[dict] | None:
    """The layers of the LINZ Basemaps ``aerial`` tile set, bottom to top: ``[{name, title, category, minZoom?,
    maxZoom?}]`` (cached ``basemaps_aerial.json``), or None when the list was never fetched and cannot be."""
    p = cache_dir() / "basemaps_aerial.json"
    if refresh or not p.exists():
        try:
            cfg = json.loads(_get(BASEMAPS_CONFIG, tries=2, timeout=60))
            rows = [{k: v for k, v in ln.items() if k not in ("2193", "3857")} for ln in cfg["layers"]]
            p.write_text(json.dumps(rows, ensure_ascii=False, indent=0))
        except Exception as e:  # noqa: BLE001
            log(f"aerial: Basemaps layer list not fetched ({type(e).__name__})")
            if not p.exists():
                return None
    return json.loads(p.read_text())


def overlaps(a, b) -> bool:
    """Whether two ``[x0, y0, x1, y1]`` boxes overlap (or touch)."""
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "-", s.lower()).strip("-")


def rank(rows: list[dict], layers: list[dict] | None = None, zoom: int = ZOOM, log=print) -> tuple[list[dict], str]:
    """The surveys that can be on top at ``zoom``, best first, and the rule used (``"basemaps"`` or ``"gsd"``).

    With the Basemaps layer list: the surveys it draws at that zoom (matched by slug or title), top layer first.
    Without it: rural and urban surveys by GSD (finest first), then end and start date (newest first)."""
    if layers:
        by_slug = {_norm(r["slug"]): r for r in rows}
        by_title = {r["title"]: r for r in rows}
        out, missing = [], []
        for i, ln in enumerate(layers):
            if "aerial" not in str(ln.get("category", "")).lower() and ln.get("category") != "Event":
                continue
            if not ln.get("minZoom", 0) <= zoom <= ln.get("maxZoom", 99):
                continue
            r = by_slug.get(_norm(ln["name"])) or by_title.get(ln.get("title", ""))
            if r is None:
                missing.append(ln["name"])
                continue
            out.append(dict(r, layer=ln["name"], order=i))
        if missing:
            log(f"aerial: {len(missing)} Basemaps layers have no STAC collection (left out): {', '.join(missing)}")
        out.sort(key=lambda r: -r["order"])
        return out, "basemaps"
    keep = [dict(r) for r in rows if r["cat"] in FALLBACK_CATS]
    keep.sort(key=lambda r: (r["gsd"], tuple(-int(x) for x in re.findall(r"\d+", r["end"] + r["start"]))))
    return keep, "gsd"


def footprint(row: dict, refresh: bool = False):
    """The survey's capture area as a shapely geometry in EPSG:2193 (cached ``capture/<slug>.geojson``), or None."""
    from pyproj import Transformer

    p = cache_dir() / "capture" / f"{row['slug']}.geojson"
    if refresh or not p.exists():
        if not row.get("capture"):
            return None
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(_get(row["capture"]))
    return footprint_from_geojson(json.loads(p.read_text()), Transformer)


def footprint_from_geojson(gj: dict, transformer=None):
    """A (Multi)Polygon in EPSG:2193 from a capture-area GeoJSON (WGS84 unless its ``crs`` says 2193)."""
    import shapely
    import shapely.geometry

    if transformer is None:
        from pyproj import Transformer as transformer
    feats = gj.get("features") if gj.get("type") == "FeatureCollection" else [gj]
    geoms = [shapely.geometry.shape(f.get("geometry", f)) for f in feats if f.get("geometry", f)]
    if not geoms:
        return None
    g = shapely.make_valid(shapely.union_all(geoms))
    crs = str((gj.get("crs") or {}).get("properties", {}).get("name", "EPSG:4326"))
    if "2193" not in crs:
        tr = transformer.from_crs("EPSG:4326", "EPSG:2193", always_xy=True)
        g = shapely.transform(g, lambda xy: np.column_stack(tr.transform(xy[:, 0], xy[:, 1])))
    return g


def sample_points(lines, step_m: float = STEP_M):
    """Points along each NZTM line (``[(n, 2) arrays]``): the middles of ``max(1, round(length / step_m))`` equal
    pieces. Returns ``(x, y, reach index, lengths)``."""
    import shapely

    n = len(lines)
    if not n:
        z = np.zeros(0)
        return z, z, np.zeros(0, np.int64), z
    geoms = np.array([shapely.LineString(c if len(c) >= 2 else [c[0], c[0]]) for c in lines], dtype=object)
    length = shapely.length(geoms)
    k = np.maximum(1, np.round(length / step_m)).astype(np.int64)
    idx = np.repeat(np.arange(n), k)
    first = np.r_[0, np.cumsum(k)[:-1]]
    frac = (np.arange(len(idx)) - first[idx] + 0.5) / k[idx]
    pts = shapely.line_interpolate_point(geoms[idx], frac, normalized=True)
    xy = shapely.get_coordinates(pts)
    return xy[:, 0], xy[:, 1], idx, length


def coverage(lines, ranked: list[dict], fps: list, step_m: float = STEP_M):
    """Per line: the index into ``ranked`` of the survey on top over most of it (-1: none) and the share of it
    under any of them (0-1). ``fps[k]``: footprint of ``ranked[k]`` in EPSG:2193, or None."""
    import shapely

    x, y, idx, _ = sample_points(lines, step_m)
    n, ns = len(lines), len(ranked)
    top = np.full(len(x), -1, np.int64)
    for k, fp in enumerate(fps):
        if fp is None or fp.is_empty:
            continue
        x0, y0, x1, y1 = fp.bounds
        todo = np.flatnonzero((top < 0) & (x >= x0) & (x <= x1) & (y >= y0) & (y <= y1))
        if not len(todo):
            continue
        shapely.prepare(fp)
        top[todo[shapely.contains_xy(fp, x[todo], y[todo])]] = k
    counts = np.zeros((n, ns + 1), np.int64)
    np.add.at(counts, (idx, top + 1), 1)
    total = counts.sum(axis=1)
    hit = total - counts[:, 0]
    best = np.where(hit > 0, np.argmax(counts[:, 1:], axis=1) if ns else 0, -1)
    share = np.divide(hit, total, out=np.zeros(n), where=total > 0)
    return best, share


def years(r: dict) -> str:
    a, b = r["start"][:4], r["end"][:4]
    return a if a == b or not b else f"{a}-{b}"


def region_record(lines, ranked: list[dict], fps: list, rule: str, step_m: float = STEP_M) -> dict:
    """``<vid>_aerial.json`` for one region (lines: its viewer reaches in NZTM, viewer order). See ``web/README.md``."""
    import shapely

    best, share = coverage(lines, ranked, fps, step_m)
    used = sorted({int(b) for b in best if b >= 0})
    remap = {k: i for i, k in enumerate(used)}
    s = [remap.get(int(b), -1) for b in best]
    pct = np.where(share > 0, np.maximum(np.round(100 * share), 1), 0).astype(int)
    part = [[i, int(p)] for i, (b, p) in enumerate(zip(s, pct, strict=True)) if b >= 0 and p < 100]
    km = np.array([shapely.length(shapely.LineString(c)) if len(c) >= 2 else 0.0 for c in lines]) / 1000
    by = {}
    for i, b in enumerate(s):
        if b >= 0:
            by[b] = by.get(b, 0.0) + km[i] * share[i]
    table = []
    for k in used:
        r = ranked[k]
        table.append({"id": r["slug"], "title": r["title"], "name": r["name"], "gsd": r["gsd"], "years": years(r),
                      "start": r["start"], "end": r["end"], "cat": r["cat"], "km": round(by.get(remap[k], 0.0), 1)})
    stats = {"reaches": len(lines), "covered": int(((share >= 0.5) & (best >= 0)).sum()),
             "km": round(float(km.sum()), 1), "kmCovered": round(float((km * share).sum()), 1)}
    return {"v": 1, "rank": rule, "zoom": ZOOM, "minZoom": MIN_ZOOM, "step": step_m, "attribution": ATTRIBUTION,
            "licence": LICENCE, "surveys": table, "s": s, "part": part, "stats": stats}
