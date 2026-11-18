"""Waterfalls, rapids, roads and tracks for every reach, from Overture Maps (OpenStreetMap data).

LINZ Topo50 needs a Data Service key, and the OSM download servers (Geofabrik, Overpass) are not
reachable from here, so this reads the Overture Maps GeoParquet release on public S3 instead.
Overture passes OSM tags through (``source_tags``), and most NZ waterfalls in OSM come from the
LINZ Topo50 import (``ref:linz:place_id``, ``height``). Licence: ODbL, "© OpenStreetMap
contributors" (Overture Maps Foundation).

Files in ``data/nz/``:

| file | contents |
|---|---|
| ``waterfalls.gpkg`` | waterfall points and rapids (``kind``), name, height in m where mapped |
| ``access.gpkg`` | road, 4WD/private and foot-track lines (``cls``) |
| ``external.pkl`` | per reach, in ``national_table`` row order: distance to each access class, falls |
| ``waterfall_check.json`` | do the terrain features find the mapped waterfalls? |

Viewer files, per region (reach order = the viewer's): ``<id>_x.txt`` (base64 of three uint16
distances in 10 m units: road, 4WD or private road, foot track; then uint8 waterfall count and
uint8 tallest mapped height in m) and ``<id>_falls.json`` (points for the map).
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

from .national import FILTER_ORDER, nz_dir
from .regions import list_regions, load_region

RELEASE = "2026-10-21.0"
BUCKET = f"overturemaps-us-west-2/release/{RELEASE}"
NZ_BBOX = (166.0, -47.6, 179.0, -34.0)   # main islands and Stewart Island (no Chathams)
ATTRIBUTION = "© OpenStreetMap contributors (ODbL), via Overture Maps"

ROAD = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential",
        "living_street", "service", "unknown"}
TRACK = {"track"}
FOOT = {"path", "footway", "bridleway", "steps", "cycleway", "pedestrian"}
CLASSES = ("road", "track", "foot")      # track = 4WD tracks and roads closed to the public
MAX_DIST = 20_000.0                       # m; further than this is stored as MAX_DIST
SNAP_M = 100.0                            # waterfall to reach centreline
NEAR_M = 2000.0                           # falls and rapids this close to a region's reaches go on its map
_MOTOR = {None, "motor_vehicle", "vehicle", "car", "motorcar"}


def _read(typ: str, columns: list[str], log=print):
    import pyarrow.compute as pc
    import pyarrow.dataset as ds
    import pyarrow.fs as pafs

    s3 = pafs.S3FileSystem(anonymous=True, region="us-west-2")
    d = ds.dataset(f"{BUCKET}/{typ}", filesystem=s3, format="parquet")
    x0, y0, x1, y1 = NZ_BBOX
    b = lambda k: pc.field("bbox", k)   # noqa: E731
    flt = (b("xmin") < x1) & (b("xmax") > x0) & (b("ymin") < y1) & (b("ymax") > y0)
    t0 = time.time()
    t = d.to_table(filter=flt, columns=columns)
    log(f"overture {typ}: {t.num_rows} rows in {time.time() - t0:.0f} s")
    return t


def _geoms(t) -> gpd.GeoSeries:
    return gpd.GeoSeries.from_wkb(t["geometry"].to_numpy(zero_copy_only=False), crs=4326).to_crs(2193)


def _record(sources) -> str:
    return next((s.get("record_id") or "" for s in (sources or []) if s.get("dataset") == "OpenStreetMap"), "")


def _height(v) -> float:
    try:
        return float(str(v).lower().replace("m", "").replace("~", "").split(";")[0].strip())
    except ValueError:
        return float("nan")


def falls_frame(t) -> gpd.GeoDataFrame:
    """Waterfall points and rapids from an Overture ``base/water`` table."""
    cls = t["class"].to_pylist()
    tags = [dict(x or []) for x in t["source_tags"].to_pylist()]
    keep = [i for i, (c, g) in enumerate(zip(cls, tags, strict=False))
            if c == "waterfall" or g.get("water") == "rapids" or g.get("waterway") == "rapids"
            or any(k.startswith("whitewater") for k in g)]
    names = t["names"].to_pylist()
    srcs = t["sources"].to_pylist()
    geom = _geoms(t.take(keep)).representative_point()
    rows = []
    for i in keep:
        g = tags[i]
        kind = "waterfall" if cls[i] == "waterfall" else "rapids"
        rows.append({"kind": kind, "name": (names[i] or {}).get("primary") or "",
                     "height_m": _height(g["height"]) if "height" in g else np.nan,
                     "grade": g.get("whitewater:rapid_grade") or g.get("rapids") or "",
                     "linz": bool(g.get("ref:linz:place_id") or "LINZ" in g.get("source", "")),
                     "osm": _record(srcs[i])})
    return gpd.GeoDataFrame(rows, geometry=geom.to_numpy(), crs=2193)


def _private(restr) -> bool:
    for a in restr or []:
        w = a.get("when") or {}
        modes = w.get("mode") or [None]
        if (a.get("access_type") == "denied" and not w.get("during") and not w.get("heading")
                and any(m in _MOTOR for m in modes)):
            return True
    return False


def access_frame(t) -> gpd.GeoDataFrame:
    """Road / track / foot lines from an Overture ``transportation/segment`` table."""
    cls = t["class"].to_pylist()
    sub = t["subtype"].to_pylist()
    restr = t["access_restrictions"].to_pylist()
    out = []
    for c, s, r in zip(cls, sub, restr, strict=False):
        if s != "road":
            out.append(None)
        elif c in ROAD:
            out.append("track" if _private(r) else "road")
        elif c in TRACK:
            out.append("track")
        elif c in FOOT:
            out.append("foot")
        else:
            out.append(None)
    keep = [i for i, c in enumerate(out) if c]
    names = t["names"].to_pylist()
    g = gpd.GeoDataFrame({"cls": [out[i] for i in keep], "osm_class": [cls[i] for i in keep],
                          "name": [(names[i] or {}).get("primary") or "" for i in keep]},
                         geometry=_geoms(t.take(keep)).to_numpy(), crs=2193)
    return g


def fetch(log=print) -> dict:
    """Download the NZ waterfalls/rapids and access lines into ``data/nz``."""
    w = _read("theme=base/type=water", ["class", "names", "source_tags", "sources", "geometry", "bbox"], log)
    falls = falls_frame(w)
    a = _read("theme=transportation/type=segment",
              ["subtype", "class", "names", "access_restrictions", "geometry", "bbox"], log)
    acc = access_frame(a)
    for g, name in ((falls, "waterfalls"), (acc, "access")):
        p = nz_dir() / f"{name}.gpkg"
        p.unlink(missing_ok=True)
        g.to_file(p, layer=name, driver="GPKG")
    out = {"release": RELEASE, "waterfalls": int((falls["kind"] == "waterfall").sum()),
           "with_height": int(falls["height_m"].notna().sum()), "rapids": int((falls["kind"] == "rapids").sum()),
           "access": acc["cls"].value_counts().to_dict()}
    log(json.dumps(out))
    return out


def load(name: str) -> gpd.GeoDataFrame:
    import pyogrio

    return pyogrio.read_dataframe(nz_dir() / f"{name}.gpkg")


# ---------------------------------------------------------------------------- per reach
def reach_access(lines: gpd.GeoSeries, access: gpd.GeoDataFrame) -> pd.DataFrame:
    """Distance (m) from each reach line to the nearest road, 4WD/private road and foot track."""
    out = {}
    lines = lines.to_numpy()
    for c in CLASSES:
        tree = shapely.STRtree(access.geometry[access["cls"] == c].to_numpy())
        (src, _), dist = tree.query_nearest(lines, max_distance=MAX_DIST, return_distance=True,
                                             all_matches=False)
        d = np.full(len(lines), MAX_DIST)
        np.minimum.at(d, src, dist)
        out[f"d_{c}_m"] = np.round(d, 1)
    return pd.DataFrame(out)


def _near(lines: gpd.GeoSeries, pts: gpd.GeoDataFrame, max_m: float):
    """Nearest reach (index, distance) of each point within ``max_m``; -1 / NaN otherwise."""
    tree = shapely.STRtree(lines.to_numpy())
    (src, tgt), dist = tree.query_nearest(pts.geometry.to_numpy(), max_distance=max_m, return_distance=True,
                                          all_matches=False)
    idx, d = np.full(len(pts), -1), np.full(len(pts), np.nan)
    idx[src], d[src] = tgt, dist
    return idx, d


def reach_falls(lines: gpd.GeoSeries, falls: gpd.GeoDataFrame, snap_m: float = SNAP_M, near_m: float = NEAR_M):
    """Snap waterfalls to the nearest reach within ``snap_m``.

    Returns the per-reach frame (``falls_n``, ``falls_max_h_m``) and every fall and rapid within
    ``near_m`` of these reaches (so a region's map shows its own), with ``reach`` (-1 if not snapped)
    and ``snap_m``.
    """
    idx, d = _near(lines, falls, near_m)
    keep = idx >= 0
    out = falls[keep].reset_index(drop=True)
    idx, d = idx[keep], d[keep]
    snapped = (out["kind"] == "waterfall").to_numpy() & (d <= snap_m)
    reach = np.where(snapped, idx, -1)
    n = np.zeros(len(lines), np.int16)
    h = np.zeros(len(lines))
    np.add.at(n, reach[snapped], 1)
    np.maximum.at(h, reach[snapped], np.nan_to_num(out["height_m"].to_numpy()[snapped], nan=0.0))
    return pd.DataFrame({"falls_n": n, "falls_max_h_m": h}), out.assign(reach=reach, snap_m=d)


def _frames():
    """(viewer id, national region name or None, segments GeoDataFrame, viewer min area)."""
    import pyogrio

    from .nzscore import region_dir

    for name in FILTER_ORDER:
        p = region_dir(name) / "segments_scored.gpkg"
        if p.exists():
            yield region_dir(name).name, name, pyogrio.read_dataframe(
                p, columns=["seg_id", "area_km2", "steepest_50m_m_per_km", "knick_ratio", "grad_s"]), 2.0
    for rid in list_regions():
        p = load_region(rid).data_dir() / "segments_scored.gpkg"
        if p.exists():
            yield rid, None, pyogrio.read_dataframe(
                p, columns=["seg_id", "area_km2", "steepest_50m_m_per_km", "knick_ratio", "grad_s"]), 1.0


def attach(out_dir: Path | None = None, log=print) -> dict:
    """Per-reach access distances and waterfalls for every national region and reference area.

    Writes ``data/nz/external.pkl`` (national reaches, ``national_table`` row order) and, with
    ``out_dir``, the viewer files. Returns the waterfall check summary.
    """
    falls, access = load("waterfalls"), load("access")
    nat, checks = [], []
    for vid, name, g, min_a in _frames():
        t0 = time.time()
        per, wf = reach_falls(g.geometry, falls)
        x = pd.concat([reach_access(g.geometry, access), per], axis=1)
        for c in ("area_km2", "steepest_50m_m_per_km", "knick_ratio", "grad_s"):
            x[c] = g[c].to_numpy()
        if name is not None:
            nat.append(x.assign(region=name, seg_id=g["seg_id"].to_numpy()))
            checks.append(x)
        if out_dir is not None:
            write_viewer(out_dir, vid, g, x, wf, min_a)
        log(f"external: {vid} {len(g)} reaches, {int((x['falls_n'] > 0).sum())} with waterfalls, "
            f"median road distance {np.median(x['d_road_m']):.0f} m ({time.time() - t0:.0f} s)")
    t = pd.concat(nat, ignore_index=True)
    t.drop(columns=["steepest_50m_m_per_km", "knick_ratio", "grad_s"]).to_pickle(nz_dir() / "external.pkl")
    summary = waterfall_check(pd.concat(checks, ignore_index=True), falls)
    (nz_dir() / "waterfall_check.json").write_text(json.dumps(summary, indent=1))
    return summary


def external_table() -> pd.DataFrame:
    """``external.pkl`` aligned with ``models.national_table`` rows (same region / file order)."""
    return pd.read_pickle(nz_dir() / "external.pkl")


# ---------------------------------------------------------------------------- validation
def _auc(pos: np.ndarray, neg: np.ndarray) -> float:
    from scipy.stats import rankdata

    pos, neg = pos[np.isfinite(pos)], neg[np.isfinite(neg)]
    if not len(pos) or not len(neg):
        return float("nan")
    r = rankdata(np.concatenate([pos, neg]))
    return float((r[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def waterfall_check(x: pd.DataFrame, falls: gpd.GeoDataFrame) -> dict:
    """How well do the terrain features pick out reaches with a mapped waterfall?

    AUC of each feature for 'reach has a waterfall' against all other reaches, and against reaches of
    the same size (area deciles), which is the fairer test because falls sit on small, steep creeks.
    """
    has = (x["falls_n"] > 0).to_numpy()
    big = has & (x["falls_max_h_m"] >= 5).to_numpy()
    feats = {"steepest_50m_m_per_km": x["steepest_50m_m_per_km"].to_numpy(dtype=float),
             "grad_s": x["grad_s"].to_numpy(dtype=float),
             "knick_ratio": x["knick_ratio"].to_numpy(dtype=float)}
    bins = pd.qcut(np.log10(x["area_km2"].to_numpy(dtype=float)), 10, labels=False, duplicates="drop")
    out = {"reaches": len(x), "reaches_with_falls": int(has.sum()), "with_falls_ge_5m": int(big.sum()),
           "waterfalls": int((falls["kind"] == "waterfall").sum()), "features": {}}
    for k, v in feats.items():
        aucs = [_auc(v[has & (bins == b)], v[~has & (bins == b)]) for b in np.unique(bins)]
        w = [int((has & (bins == b)).sum()) for b in np.unique(bins)]
        ok = [(a, n) for a, n in zip(aucs, w, strict=False) if np.isfinite(a) and n]
        out["features"][k] = {
            "auc": round(_auc(v[has], v[~has]), 3),
            "auc_size_matched": round(sum(a * n for a, n in ok) / max(sum(n for _, n in ok), 1), 3),
            "auc_falls_ge_5m": round(_auc(v[big], v[~has]), 3),
            "median_with_falls": round(float(np.nanmedian(v[has])), 2),
            "median_without": round(float(np.nanmedian(v[~has])), 2)}
    return out


# ---------------------------------------------------------------------------- viewer
def _u16(d: np.ndarray) -> np.ndarray:
    return np.clip(np.round(np.asarray(d, float) / 10.0), 0, 65535).astype("<u2")


def write_viewer(out_dir: Path, vid: str, g: gpd.GeoDataFrame, x: pd.DataFrame, wf: gpd.GeoDataFrame,
                 min_area: float) -> None:
    """``<vid>_x.txt`` and ``<vid>_falls.json`` (see the module docstring) for one viewer region."""
    vis = (g["area_km2"] >= min_area).to_numpy()
    vi = np.where(vis, np.cumsum(vis) - 1, -1)
    xv = x[vis]
    blob = b"".join([*(_u16(xv[f"d_{c}_m"]).tobytes() for c in CLASSES),
                     np.clip(xv["falls_n"].to_numpy(), 0, 255).astype(np.uint8).tobytes(),
                     np.clip(np.round(xv["falls_max_h_m"].to_numpy()), 0, 255).astype(np.uint8).tobytes()])
    (out_dir / f"{vid}_x.txt").write_text(base64.b64encode(blob).decode())
    ll = wf.to_crs(4326)
    pts = [[round(p.x, 5), round(p.y, 5), r["name"], None if np.isnan(r["height_m"]) else round(float(r["height_m"])),
            int(vi[r["reach"]]) if r["reach"] >= 0 else -1, "w" if r["kind"] == "waterfall" else "r"]
           for p, (_, r) in zip(ll.geometry, wf.iterrows(), strict=False)]
    (out_dir / f"{vid}_falls.json").write_text(json.dumps(pts, ensure_ascii=False, separators=(",", ":")))

