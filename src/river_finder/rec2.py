"""NIWA River Environment Classification v2 (REC2) reaches, used to validate and give context.

Two public ArcGIS FeatureServers:
- REC2_Layers/0: reach polylines with topology (HydroID -> NextDownID) and CUM_AREA (m^2).
- NZ_River_Names_REC2_V1/0: nzsegment -> RivName.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path

ORG = "https://services3.arcgis.com/fp1tibNcN9mbExhG/ArcGIS/rest/services"
LINES = f"{ORG}/REC2_Layers/FeatureServer/0/query"
NAMES = f"{ORG}/NZ_River_Names_REC2_V1/FeatureServer/0/query"
FIELDS = "nzsegment,HydroID,NextDownID,CUM_AREA,StreamOrde,upElev,downElev,segslpmean,Headwater,downcoordX,downcoordY"


def _query(url: str, params: dict) -> dict:
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(url, data, timeout=120) as r:
        out = json.load(r)
    if "error" in out:
        raise RuntimeError(out["error"])
    return out


def fetch_reaches(bbox_wgs84, out_sr: int = 2193) -> list[dict]:
    """All REC2 reaches intersecting the bbox, with names. Returns ArcGIS JSON features."""
    feats, off = [], 0
    while True:
        r = _query(LINES, {
            "geometry": ",".join(map(str, bbox_wgs84)), "geometryType": "esriGeometryEnvelope", "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects", "outFields": FIELDS, "returnGeometry": "true",
            "outSR": out_sr, "f": "json", "resultOffset": off, "resultRecordCount": 2000,
        })
        feats += r["features"]
        off += len(r["features"])
        if not r.get("exceededTransferLimit") or not r["features"]:
            break
    segs = [f["attributes"]["nzsegment"] for f in feats]
    names: dict[int, str] = {}
    for i in range(0, len(segs), 500):
        r = _query(NAMES, {"where": f"nzsegment IN ({','.join(map(str, segs[i:i + 500]))})",
                           "outFields": "nzsegment,RivName", "returnGeometry": "false", "f": "json"})
        names.update({f["attributes"]["nzsegment"]: f["attributes"]["RivName"] for f in r["features"]})
    for f in feats:
        f["attributes"]["name"] = names.get(f["attributes"]["nzsegment"])
    return feats


def to_geojson(feats: list[dict], epsg: int) -> dict:
    out = []
    for f in feats:
        paths = f["geometry"]["paths"]
        geom = ({"type": "LineString", "coordinates": paths[0]} if len(paths) == 1
                else {"type": "MultiLineString", "coordinates": paths})
        a = dict(f["attributes"])
        a["cum_area_km2"] = a.pop("CUM_AREA") / 1e6
        out.append({"type": "Feature", "geometry": geom, "properties": a})
    fc = {"type": "FeatureCollection", "features": out}
    if epsg != 4326:
        fc["crs"] = {"type": "name", "properties": {"name": f"urn:ogc:def:crs:EPSG::{epsg}"}}
    return fc


def fetch_region(bbox_wgs84, out_dir: Path) -> tuple[Path, Path]:
    """Write rec2_nztm.geojson (EPSG:2193, for analysis/GIS) and rec2_wgs84.geojson (viewer)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    p1, p2 = out_dir / "rec2_nztm.geojson", out_dir / "rec2_wgs84.geojson"
    p1.write_text(json.dumps(to_geojson(fetch_reaches(bbox_wgs84, 2193), 2193)))
    p2.write_text(json.dumps(to_geojson(fetch_reaches(bbox_wgs84, 4326), 4326)))
    return p1, p2


def upstream_complete(features: list[dict], bbox_nztm) -> set[int]:
    """HydroIDs whose whole REC2 upstream network lies inside the bbox, so their CUM_AREA is a fair
    comparison for a grid clipped to that bbox."""
    x0, y0, x1, y1 = bbox_nztm
    by_id = {f["properties"]["HydroID"]: f for f in features}
    donors: dict[int, list[int]] = {}
    for f in features:
        donors.setdefault(f["properties"]["NextDownID"], []).append(f["properties"]["HydroID"])

    def inside(f) -> bool:
        g = f["geometry"]
        lines = [g["coordinates"]] if g["type"] == "LineString" else g["coordinates"]
        return all(x0 <= x <= x1 and y0 <= y <= y1 for line in lines for x, y in line)

    ok: dict[int, bool] = {}

    def check(h: int) -> bool:
        # iterative post-order to avoid recursion limits
        stack = [(h, False)]
        while stack:
            node, expanded = stack.pop()
            if node in ok:
                continue
            if not expanded:
                stack.append((node, True))
                stack.extend((d, False) for d in donors.get(node, []) if d not in ok)
            else:
                f = by_id[node]
                ok[node] = inside(f) and all(ok.get(d, False) for d in donors.get(node, []))
        return ok[h]

    # A reach whose upstream reaches lie outside the fetched set looks like a headwater here. The REC2
    # Headwater flag tells true headwaters apart: a non-headwater reach with no donors is incomplete.
    for f in features:
        p = f["properties"]
        if p["Headwater"] == 0 and not donors.get(p["HydroID"]):
            ok[p["HydroID"]] = False
    return {h for h in by_id if check(h)}
