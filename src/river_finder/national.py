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
import os
import time
import tomllib
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
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


NZ_ENV = "RIVER_FINDER_NZ"                       # overrides the national data root for one process
ROOT_CONFIG = REPO_ROOT / "config" / "national.toml"   # [data] root = "data/nz" (the 8 m scan) until the switch
DEFAULT_ROOT = "data/nz"
VIEWER_8M = REPO_ROOT / "data" / "viewer_nz"     # the 8 m scan's viewer data (the live site today)


def _resolve(p: str | Path) -> Path:
    p = Path(p)
    return p if p.is_absolute() else REPO_ROOT / p


def root_setting() -> tuple[Path, str]:
    """The national data root and where it was set: ``$RIVER_FINDER_NZ``, ``config/national.toml`` or the default."""
    env = os.environ.get(NZ_ENV)
    if env:
        return _resolve(env), NZ_ENV
    if ROOT_CONFIG.exists():
        with open(ROOT_CONFIG, "rb") as fh:
            root = tomllib.load(fh).get("data", {}).get("root")
        if root:
            return _resolve(root), str(ROOT_CONFIG)
    return _resolve(DEFAULT_ROOT), "default"


def nz_dir() -> Path:
    """The national data root: ``data/nz`` (the 8 m scan) until ``river-finder nz1m switch`` points
    ``config/national.toml`` at ``data/nz1m`` (the 1 m LiDAR rebuild); ``$RIVER_FINDER_NZ`` overrides both."""
    d = root_setting()[0]
    d.mkdir(parents=True, exist_ok=True)
    return d


def is_1m(d: Path | None = None) -> bool:
    """Whether a national root holds the 1 m LiDAR rebuild (the merge's ``network.parquet``), not the 8 m scan."""
    return ((d or nz_dir()) / "network.parquet").exists()


def data_kind(d: Path | None = None) -> str:
    """``"1m"`` for the 1 m LiDAR rebuild, ``"8m"`` for the 8 m scan (model cards and CV results carry it)."""
    return "1m" if is_1m(d) else "8m"


def viewer_dir_for(d: Path) -> Path:
    """Viewer data files of a national root: ``data/viewer_nz`` for the 8 m scan in ``data/nz``, else
    ``<root>/viewer_nz`` (so a 1 m build never writes over the live site's files before the switch)."""
    return VIEWER_8M if Path(d).resolve() == _resolve(DEFAULT_ROOT).resolve() else Path(d) / "viewer_nz"


def viewer_dir() -> Path:
    """``viewer_dir_for`` the current root."""
    return viewer_dir_for(root_setting()[0])


@contextmanager
def using_root(d: Path):
    """Point ``nz_dir()`` at ``d`` for the duration (sets ``$RIVER_FINDER_NZ`` in this process)."""
    old = os.environ.get(NZ_ENV)
    os.environ[NZ_ENV] = str(Path(d).resolve())
    try:
        yield Path(d)
    finally:
        if old is None:
            os.environ.pop(NZ_ENV, None)
        else:
            os.environ[NZ_ENV] = old


class GidMap:
    """Gids the 1 m merge dropped -> the gid that took their local area (rf-lxa.12, rf-lxa.13).

    The merge takes the zero-length reaches out of the network (``nz1mmerge.drop_zero_length``): the first reach of
    non-zero length below takes each one's local area, or none (``-1``: lost with it). ``merge.json``
    ``zero_length.merged`` lists ``[dropped gid, gid that took it]``. The tiles' ``local_catchment_core.tif`` still
    name the dropped gids, so every reader of those rasters maps its cells with this (``gmap(a)``): a dropped gid
    becomes the gid that took it (``-1``, no catchment, when lost), every other value stays as it is. Empty (the
    identity) for the 8 m scan and for merges before rf-lxa.12. Chains (a target that is itself dropped) are
    followed to their end."""

    def __init__(self, pairs=()):
        d = {int(a): int(b) for a, b in pairs}
        for a in list(d):
            b, seen = d[a], {a}
            while b in d and b not in seen:
                seen.add(b)
                b = d[b]
            d[a] = -1 if b in seen else b
        k = np.array(sorted(d), np.int64)
        self.src = k
        self.dst = np.array([d[int(x)] for x in k], np.int64)

    def __len__(self) -> int:
        return int(self.src.size)

    def __bool__(self) -> bool:
        return bool(self.src.size)

    def as_dict(self) -> dict[int, int]:
        return dict(zip(self.src.tolist(), self.dst.tolist(), strict=True))

    def __call__(self, a: np.ndarray) -> np.ndarray:
        """``a`` (gids, any shape; negative = none) with every dropped gid replaced. Returns ``a`` itself when no
        cell holds one, else a new int64 array."""
        a = np.asarray(a)
        if not self.src.size or not a.size:
            return a
        hi = a.max()
        if hi < self.src[0]:
            return a
        lo = np.min(a, where=a >= 0, initial=hi)
        i0, i1 = int(np.searchsorted(self.src, lo, "left")), int(np.searchsorted(self.src, hi, "right"))
        if i0 >= i1:
            return a
        k, v = self.src[i0:i1], self.dst[i0:i1]
        m = (a >= k[0]) & (a <= k[-1])
        sub = a[m].astype(np.int64)
        j = np.minimum(np.searchsorted(k, sub), k.size - 1)
        hit = k[j] == sub
        if not hit.any():
            return a
        out = a.astype(np.int64, copy=True)
        sub[hit] = v[j[hit]]
        out[m] = sub
        return out


_GIDMAP_CACHE: dict[tuple[str, int], GidMap] = {}


def merged_gids(root: Path | None = None) -> GidMap:
    """The :class:`GidMap` of a national root (default ``nz_dir()``) from its ``merge.json`` ``zero_length.merged``;
    the identity when the file or the section is missing (the 8 m scan, older merges). Read once per file version."""
    p = Path(root if root is not None else nz_dir()) / "merge.json"
    try:
        key = (str(p.resolve()), p.stat().st_mtime_ns)
    except OSError:
        return GidMap()
    if key not in _GIDMAP_CACHE:
        pairs = (json.loads(p.read_text()).get("zero_length") or {}).get("merged") or []
        _GIDMAP_CACHE[key] = GidMap(pairs)
    return _GIDMAP_CACHE[key]


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


def region_filters(base: Path | None = None) -> gpd.GeoDataFrame:
    """Polygons (EPSG:2193) of the river-guide regions, built from the council boundaries in ``base``
    (default ``data/nz``: ``councils.gpkg`` and ``large_catchments.gpkg``)."""
    from shapely.geometry import box as sbox
    from shapely.ops import unary_union

    d = Path(base) if base is not None else nz_dir()
    c = gpd.read_file(d / "councils.gpkg")
    lc = gpd.read_file(d / "large_catchments.gpkg")
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
    rows = [{"region": k, "geometry": unary_union(parts[k]).buffer(0)} for k in FILTER_ORDER if k in parts]
    return gpd.GeoDataFrame(rows, crs="EPSG:2193")


def assign_regions(points: gpd.GeoSeries, base: Path | None = None) -> np.ndarray:
    """River-guide region name for each point (EPSG:2193); nearest region for points offshore.

    Point-in-polygon first (prepared geometries, vectorised); only the few points left over
    (on the coast, in estuaries) go through the much slower nearest-polygon search. ``base``: as
    :func:`region_filters`.
    """
    import shapely

    rf = region_filters(base)
    pts = points.reset_index(drop=True)
    x, y = pts.x.to_numpy(), pts.y.to_numpy()
    out = np.full(len(pts), "", dtype=object)
    for name, geom in zip(rf["region"], rf.geometry, strict=True):
        shapely.prepare(geom)
        todo = out == ""
        hit = shapely.contains_xy(geom, x[todo], y[todo])
        out[np.flatnonzero(todo)[hit]] = name
    rest = np.flatnonzero(out == "")
    if len(rest):
        simple = rf.assign(geometry=rf.geometry.simplify(50.0))
        left = gpd.GeoDataFrame(geometry=pts.iloc[rest].reset_index(drop=True), crs="EPSG:2193")
        j = gpd.sjoin_nearest(left, simple, how="left", max_distance=20_000)
        j = j[~j.index.duplicated(keep="first")]
        out[rest] = j["region"].fillna("").to_numpy()
    return out.astype(str)
