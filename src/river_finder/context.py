"""Context layers: GNS geology and NIWA flood statistics.

- Geology: GNS 1:250k Geological Map of NZ (2023), layer 55 ``geological_units`` (polygons).
- Hydrology: Henderson & Collins (2018) mean annual flood (MAF) per REC1 reach. We use the specific
  MAF (m3/s per km2) as a rain-intensity proxy. The MfE annual-rainfall grid would be an
  alternative, but data.govt.nz blocks scripted downloads and the MfE portal needs an API key.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path

GEOLOGY = ("https://gis.gns.cri.nz/server/rest/services/NZL_GNS_250K_Geology_2023/"
           "NZL_GNS_250K_geology_FeatureService_NZ/FeatureServer/55/query")
GEOLOGY_FIELDS = "mapsymbol,mapname,simplename,keygrpname,rockclass,rockgroup,mainrock,lithology,absmin_ma,absmax_ma"
FLOOD = ("https://services3.arcgis.com/fp1tibNcN9mbExhG/ArcGIS/rest/services/"
         "NZ_Flood_Statistics_Henderson_Collins_V2_REC1_Layer_WFL1/FeatureServer/0/query")
FLOOD_FIELDS = "NZREACH,Rivername,Areakm2,H_C18_MAF,H_C18_10y,H_C18_100y"

# Rock that forms resistant caprock / bedrock gorges (the reference-region hypothesis).
HARD_ROCKGROUPS = {"basalt", "andesite", "dacite", "rhyolite", "ignimbrite", "greywacke", "granite",
                   "diorite", "gabbro", "schist", "gneiss", "volcanic breccia", "lava"}


def hard_rock(p: dict) -> int:
    """1 when a GNS geology unit is resistant rock (volcanic, plutonic, metamorphic, greywacke, ...)."""
    return int((p.get("rockgroup") or "").strip().lower() in HARD_ROCKGROUPS
               or (p.get("rockclass") or "").strip().lower() in {"volcanic", "volcanic rock", "plutonic", "metamorphic"}
               or "ignimbrite" in (p.get("lithology") or "").lower())


def _query_all(url: str, params: dict) -> list[dict]:
    feats, off = [], 0
    while True:
        p = dict(params, resultOffset=off, resultRecordCount=2000, f="geojson")
        with urllib.request.urlopen(url, urllib.parse.urlencode(p).encode(), timeout=180) as r:
            out = json.load(r)
        if "error" in out:
            raise RuntimeError(out["error"])
        feats += out["features"]
        off += len(out["features"])
        if not out.get("properties", {}).get("exceededTransferLimit") and not out.get("exceededTransferLimit"):
            if len(out["features"]) < 2000:
                break
        if not out["features"]:
            break
    return feats


def _envelope(bbox_wgs84) -> dict:
    return {"geometry": ",".join(map(str, bbox_wgs84)), "geometryType": "esriGeometryEnvelope", "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects", "outSR": 2193, "returnGeometry": "true"}


def fetch_geology(bbox_wgs84, out_dir: Path) -> Path:
    feats = _query_all(GEOLOGY, dict(_envelope(bbox_wgs84), outFields=GEOLOGY_FIELDS))
    for f in feats:
        f["properties"]["hard_rock"] = hard_rock(f["properties"])
    path = out_dir / "geology_nztm.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": feats,
                                "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::2193"}}}))
    return path


def fetch_flood(bbox_wgs84, out_dir: Path) -> Path:
    feats = _query_all(FLOOD, dict(_envelope(bbox_wgs84), outFields=FLOOD_FIELDS))
    path = out_dir / "flood_rec1_nztm.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": feats,
                                "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::2193"}}}))
    return path


def fetch_region(region, out_dir: Path) -> None:
    fetch_geology(region.bbox_wgs84, out_dir)
    fetch_flood(region.bbox_wgs84, out_dir)
