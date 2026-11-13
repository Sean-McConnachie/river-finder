"""National (all-NZ) vector data: rivers, flood/rainfall statistics, geology and region boundaries.

Everything comes from public ArcGIS FeatureServers and is written to ``data/nz/`` as GeoPackages in
NZTM (EPSG:2193). Pages are requested by object id in parallel, which is faster and steadier than
offset paging on layers with ~600k features.

| file | source | contents |
|---|---|---|
| rec2.gpkg | NIWA REC2_Layers/0 + NZ_River_Names_REC2_V1 | 593k reaches, topology, CUM_AREA, names |
| flood.gpkg | NIWA Henderson & Collins 2018 (REC1) + HIRDS v3 rational method | MAF, Q10, Q100 and the
  rainfall-intensity 10-year flood per REC1 reach |
| geology.gpkg | GNS 250K geology 2023, layer 55 | 55k units with the hard_rock flag |
| councils.gpkg | Stats NZ regional council boundaries 2022 (generalised), NIWA mirror | 17 polygons |
| large_catchments.gpkg | NIWA REC2 large catchments | 90 basins with names |
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from .context import GEOLOGY, GEOLOGY_FIELDS, hard_rock
from .regions import REPO_ROOT

ORG = "https://services3.arcgis.com/fp1tibNcN9mbExhG/ArcGIS/rest/services"
REC2 = f"{ORG}/REC2_Layers/FeatureServer/0/query"
NAMES = f"{ORG}/NZ_River_Names_REC2_V1/FeatureServer/0/query"
FLOOD = f"{ORG}/NZ_Flood_Statistics_Henderson_Collins_V2_REC1_Layer_WFL1/FeatureServer/0/query"
HIRDS = f"{ORG}/NZ_Flood_Statistics_Henderson_Collins_V2_HIRDSV3_REC1_Layer_WFL3/FeatureServer/0/query"
COUNCILS = f"{ORG}/Regional_Council_Boundaries__gen_/FeatureServer/2/query"
LARGE = f"{ORG}/NZ_Large_Catchments/FeatureServer/0/query"

REC2_FIELDS = ("nzsegment,HydroID,NextDownID,CUM_AREA,CATAREA,StreamOrde,upElev,downElev,segslpmean,Headwater,"
               "upcoordX,upcoordY,downcoordX,downcoordY,LENGTHDOWN")


def nz_dir() -> Path:
    d = REPO_ROOT / "data" / "nz"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _post(url: str, params: dict, tries: int = 5) -> dict:
    data = urllib.parse.urlencode(params).encode()
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, data, timeout=300) as r:
                out = json.load(r)
            if "error" in out:
                raise RuntimeError(out["error"])
            return out
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 * 2 ** i)
    raise AssertionError


def _ids(url: str, where: str = "1=1") -> list[int]:
    return sorted(_post(url, {"where": where, "returnIdsOnly": "true", "f": "json"})["objectIds"])


def fetch_layer(url: str, fields: str, geometry: bool = True, page: int = 2000, workers: int = 6,
                where: str = "1=1", log=print) -> list[dict]:
    """All features of a layer as GeoJSON-like dicts (geometry in EPSG:2193, 1 m precision)."""
    ids = _ids(url, where)
    batches = [ids[i:i + page] for i in range(0, len(ids), page)]
    done = [0]

    def one(b):
        p = {"objectIds": ",".join(map(str, b)), "outFields": fields, "f": "geojson",
             "returnGeometry": "true" if geometry else "false"}
        if geometry:
            p.update(outSR=2193, geometryPrecision=1)
        out = _post(url, p)["features"]
        done[0] += 1
        if done[0] % 25 == 0 or done[0] == len(batches):
            log(f"  {url.split('/services/')[1].split('/')[0]}: {done[0]}/{len(batches)} pages")
        return out

    with ThreadPoolExecutor(workers) as ex:
        return [f for part in ex.map(one, batches) for f in part]


def _frame(feats: list[dict]) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame.from_features(feats, crs="EPSG:2193")


def fetch_rec2(log=print) -> Path:
    out = nz_dir() / "rec2.gpkg"
    g = _frame(fetch_layer(REC2, REC2_FIELDS, log=log))
    names = pd.DataFrame([f["properties"] for f in fetch_layer(NAMES, "nzsegment,RivName", geometry=False, log=log)])
    names = names.dropna(subset=["RivName"]).drop_duplicates("nzsegment").set_index("nzsegment")["RivName"]
    g["name"] = g["nzsegment"].map(names)
    g["cum_area_km2"] = g.pop("CUM_AREA") / 1e6
    g["cat_area_km2"] = g.pop("CATAREA") / 1e6
    g.to_file(out, layer="rec2", driver="GPKG")
    log(f"rec2: {len(g)} reaches, {g['name'].notna().sum()} named -> {out}")
    return out


def fetch_floods(log=print) -> Path:
    out = nz_dir() / "flood.gpkg"
    g = _frame(fetch_layer(FLOOD, "NZREACH,Rivername,Areakm2,H_C18_MAF,H_C18_10y,H_C18_100y", log=log))
    q = pd.DataFrame([f["properties"] for f in fetch_layer(HIRDS, "NZREACH,QIA_10y,QIA_100y", geometry=False,
                                                           log=log)])
    q = q.drop_duplicates("NZREACH").set_index("NZREACH")
    g["QIA_10y"] = g["NZREACH"].map(q["QIA_10y"])
    g["QIA_100y"] = g["NZREACH"].map(q["QIA_100y"])
    g.to_file(out, layer="flood", driver="GPKG")
    log(f"flood: {len(g)} REC1 reaches -> {out}")
    return out


def fetch_geology(log=print) -> Path:
    out = nz_dir() / "geology.gpkg"
    g = _frame(fetch_layer(GEOLOGY, GEOLOGY_FIELDS, page=1000, workers=4, log=log))
    g["hard_rock"] = [hard_rock(r) for r in g.to_dict("records")]
    g.to_file(out, layer="geology", driver="GPKG")
    log(f"geology: {len(g)} units -> {out}")
    return out


def fetch_boundaries(log=print) -> tuple[Path, Path]:
    c = _frame(fetch_layer(COUNCILS, "REGC2022_V1_00,REGC2022_V1_00_NAME_ASCII,LAND_AREA_SQ_KM", log=log))
    c = c.rename(columns={"REGC2022_V1_00": "regc", "REGC2022_V1_00_NAME_ASCII": "council"})
    p1 = nz_dir() / "councils.gpkg"
    c.to_file(p1, layer="councils", driver="GPKG")
    lc = _frame(fetch_layer(LARGE, "nzsegment,RivName,CUM_AREA", log=log))
    p2 = nz_dir() / "large_catchments.gpkg"
    lc.to_file(p2, layer="large_catchments", driver="GPKG")
    log(f"boundaries: {len(c)} councils, {len(lc)} large catchments")
    return p1, p2


def fetch_all(log=print) -> None:
    fetch_boundaries(log)
    fetch_rec2(log)
    fetch_floods(log)
    fetch_geology(log)


# --- region filters ---------------------------------------------------------------------------
# The river guide (riverguide.co.nz) files sections under these regions. They follow the regional
# councils, except that Manawatu-Whanganui is split into "Wanganui" (Whanganui, Whangaehu,
# Turakina, Rangitikei, Waitotara and the coast north of the Rangitikei mouth) and "Manawatu"
# (the rest), and Nelson is filed under Tasman. Auckland has no guide sections but is kept.
COUNCIL_TO_FILTER = {
    "01": "Northland", "02": "Auckland", "03": "Waikato", "04": "Bay of Plenty", "05": "Gisborne",
    "06": "Hawke's Bay", "07": "Taranaki", "09": "Wellington", "12": "West Coast", "13": "Canterbury",
    "14": "Otago", "15": "Southland", "16": "Tasman", "17": "Tasman", "18": "Marlborough",
}
WANGANUI_BASINS = ("Whanganui River", "Whangaehu River", "Turakina River", "Rangitikei River", "Waitotara River")
RANGITIKEI_MOUTH_Y = 5_538_000.0  # NZTM northing of the Rangitikei mouth
WANGANUI_COAST_MAX_X = 1_790_000.0
FILTER_ORDER = ["Northland", "Auckland", "Waikato", "Bay of Plenty", "Gisborne", "Hawke's Bay", "Taranaki",
                "Wanganui", "Manawatu", "Wellington", "Tasman", "Marlborough", "West Coast", "Canterbury",
                "Otago", "Southland"]


def region_filters() -> gpd.GeoDataFrame:
    """Polygons (EPSG:2193) of the river-guide regions, built from the council boundaries."""
    from shapely.geometry import box as sbox
    from shapely.ops import unary_union

    c = gpd.read_file(nz_dir() / "councils.gpkg")
    lc = gpd.read_file(nz_dir() / "large_catchments.gpkg")
    parts: dict[str, list] = {}
    for code, geom in zip(c["regc"], c.geometry, strict=True):
        if code in COUNCIL_TO_FILTER:
            parts.setdefault(COUNCIL_TO_FILTER[code], []).append(geom)
    horizons = unary_union(c.loc[c["regc"] == "08"].geometry)
    basins = unary_union(lc.loc[lc["RivName"].isin(WANGANUI_BASINS)].geometry)
    manawatu_basin = unary_union(lc.loc[lc["RivName"] == "Manawatu River"].geometry)
    x0, y0, x1, y1 = horizons.bounds
    coast = sbox(x0, RANGITIKEI_MOUTH_Y, WANGANUI_COAST_MAX_X, y1)
    wang = horizons.intersection(unary_union([basins, coast])).difference(manawatu_basin)
    parts["Wanganui"] = [wang]
    parts["Manawatu"] = [horizons.difference(wang)]
    rows = [{"region": k, "geometry": unary_union(parts[k]).buffer(0)} for k in FILTER_ORDER]
    return gpd.GeoDataFrame(rows, crs="EPSG:2193")


def assign_regions(points: gpd.GeoSeries) -> np.ndarray:
    """River-guide region name for each point (EPSG:2193); nearest region for points offshore."""
    rf = region_filters()
    pts = gpd.GeoDataFrame(geometry=points.reset_index(drop=True), crs="EPSG:2193")
    j = gpd.sjoin_nearest(pts, rf, how="left", max_distance=20_000)
    j = j[~j.index.duplicated(keep="first")]
    return j["region"].fillna("").to_numpy()
