"""Joins to the 1 m reach lines after the merge (stage ``ext`` of ``docs/lidar-extraction-spec.md``, rf-lxa.8).

The merge (``nz1mmerge.py``) already joins REC2 (``river_rec2``, ``rec2_*``) and REC1 (MAF). This adds, per reach of
``reaches.parquet`` (same row order, key ``gid``), into ``<root>/joins.parquet``:

- ``bed_hard``, ``bed_rock``: GNS 250K geology at the line midpoint (``hard_rock``, ``rockgroup``; as the 8 m tiles).
- ``river_linz``: the LINZ river-line name (LDS 103632) within ``LINZ_NEAR_M`` of more than half of the reach's
  25 m points.
- ``linz_dist_m``: median distance of those points to the nearest LINZ line, capped at ``LINZ_CAP_M``.
- ``linz_section_id``: the LINZ ``river_section_id`` nearest to most of those points (within ``LINZ_NEAR_M``), -1 none.
- ``river``: ``river_rec2``, else ``river_linz`` (the merge's ``river`` is REC2 only).
- ``lidar_survey``, ``lidar_year``, ``lidar_year_start``: the LiDAR survey over most of the line
  (``lidar.line_coverage`` on the survey layer of ``data/analysis/lidar``).
- ``sinuosity``: ``length_m`` / straight distance between the line's ends.

LINZ lines come from the web app's per-region cache (``<root>/linz/<vid>.gpkg``, ``river-finder web data --only
linz`` fetches them; the ``inputs`` step of ``nz1m finish`` links today's). One tile (row group of
``reaches.parquet``) at a time: about 5,000 reaches, well under 1 GB.
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd

LINZ_NEAR_M = 30.0     # a point is on a LINZ line within this
LINZ_CAP_M = 500.0     # linz_dist_m cap (and search radius)
STEP_M = 25.0          # sample spacing along the reach line
PAD_M = 1000.0         # LINZ and geology read around each tile's reaches
COLUMNS = ["gid", "bed_hard", "bed_rock", "river_linz", "linz_dist_m", "linz_section_id", "river", "lidar_survey",
           "lidar_year", "lidar_year_start", "sinuosity"]


def line_arrays(geoms) -> list[np.ndarray]:
    import shapely

    return [np.asarray(c) for c in (shapely.get_coordinates(g) for g in geoms)]


def midpoints(geoms) -> tuple[np.ndarray, np.ndarray]:
    import shapely

    p = shapely.line_interpolate_point(np.asarray(geoms, dtype=object), 0.5, normalized=True)
    xy = shapely.get_coordinates(p)
    return xy[:, 0], xy[:, 1]


def sinuosity(geoms, length_m: np.ndarray) -> np.ndarray:
    import shapely

    g = np.asarray(geoms, dtype=object)
    a, b = shapely.get_point(g, 0), shapely.get_point(g, -1)
    chord = shapely.distance(a, b)
    with np.errstate(divide="ignore", invalid="ignore"):
        s = np.where(chord > 1.0, np.asarray(length_m, float) / chord, np.nan)
    return np.round(np.maximum(s, 1.0), 4).astype(np.float32)


def join_geology(x: np.ndarray, y: np.ndarray, geo) -> tuple[np.ndarray, np.ndarray]:
    """``bed_hard`` (int8) and ``bed_rock`` (str) of the geology polygon holding each point."""
    import shapely

    hard = np.zeros(len(x), np.int8)
    rock = np.full(len(x), "", dtype=object)
    if geo is None or not len(geo) or not len(x):
        return hard, rock
    pts = shapely.points(x, y)
    geoms = geo.geometry.to_numpy()
    pi, gi = shapely.STRtree(geoms).query(pts, predicate="within")
    first = np.unique(pi, return_index=True)[1]
    pi, gi = pi[first], gi[first]
    hard[pi] = geo["hard_rock"].fillna(0).astype(bool).to_numpy()[gi].astype(np.int8)
    rock[pi] = geo["rockgroup"].fillna("").astype(str).str.strip().to_numpy()[gi]
    return hard, rock


def _mode(keys: np.ndarray, groups: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray]:
    """Per group 0..n-1: the most common key and its count (key -1 where the group has none)."""
    best = np.full(n, -1, np.int64)
    cnt = np.zeros(n, np.int64)
    if not len(keys):
        return best, cnt
    df = pd.DataFrame({"g": groups, "k": keys}).value_counts().reset_index(name="c")
    df = df.sort_values(["g", "c", "k"], ascending=[True, False, True]).drop_duplicates("g")
    best[df["g"].to_numpy()] = df["k"].to_numpy()
    cnt[df["g"].to_numpy()] = df["c"].to_numpy()
    return best, cnt


def join_linz(lines: list[np.ndarray], linz, step_m: float = STEP_M, near_m: float = LINZ_NEAR_M,
              cap_m: float = LINZ_CAP_M) -> pd.DataFrame:
    """``river_linz``, ``linz_dist_m``, ``linz_section_id`` per line (see the module doc). ``linz``: LINZ river
    lines (``name``, ``river_section_id``, LineString geometry, EPSG:2193); None or empty: no LINZ data."""
    import shapely

    from .aerial import sample_points

    n = len(lines)
    out = pd.DataFrame({"river_linz": np.full(n, "", dtype=object), "linz_dist_m": np.full(n, np.nan, np.float32),
                        "linz_section_id": np.full(n, -1, np.int64)})
    if linz is None or not len(linz) or not n:
        return out
    x, y, idx, _ = sample_points(lines, step_m)
    pts = shapely.points(x, y)
    geoms = linz.geometry.to_numpy()
    pi, li = shapely.STRtree(geoms).query_nearest(pts, max_distance=cap_m, all_matches=False)
    d = np.full(len(x), cap_m)
    lk = np.full(len(x), -1, np.int64)
    d[pi] = np.minimum(shapely.distance(pts[pi], geoms[li]), cap_m)
    lk[pi] = li
    out["linz_dist_m"] = pd.Series(d).groupby(idx).median().reindex(range(n)).to_numpy(np.float32).round(1)
    npts = np.bincount(idx, minlength=n)
    near = d <= near_m
    sec = linz["river_section_id"].fillna(-1).astype(np.int64).to_numpy()
    names = linz["name"].fillna("").astype(str).to_numpy(object)
    # the section nearest to most points within near_m
    best, cnt = _mode(sec[lk[near]], idx[near], n)
    ok = 2 * cnt > npts
    out.loc[ok, "linz_section_id"] = best[ok]
    # the name: codes of the non-empty names of the near points
    named = near.copy()
    named[near] = names[lk[near]] != ""
    if named.any():
        uniq, code = np.unique(names[lk[named]].astype(str), return_inverse=True)
        bn, cn = _mode(code.astype(np.int64), idx[named], n)
        okn = (2 * cn > npts) & (bn >= 0)
        out.loc[okn, "river_linz"] = uniq[bn[okn]]
    return out


def join_survey(lines: list[np.ndarray], layer, step_m: float = STEP_M) -> pd.DataFrame:
    """``lidar_survey`` (slug), ``lidar_year``, ``lidar_year_start`` of the survey over most of each line."""
    from .lidar import line_coverage

    n = len(lines)
    out = pd.DataFrame({"lidar_survey": np.full(n, "", dtype=object),
                        "lidar_year": pd.array([pd.NA] * n, dtype="Int16"),
                        "lidar_year_start": pd.array([pd.NA] * n, dtype="Int16")})
    if layer is None or not len(layer) or not n:
        return out
    cov = line_coverage(lines, list(layer.geometry.values), layer["in_mosaic"].to_numpy(bool), step_m)
    code = cov["lidar_code"]
    has = code >= 0
    pick = np.maximum(code, 0)
    out.loc[has, "lidar_survey"] = layer["slug"].to_numpy(object)[pick[has]]
    for col in ("year", "year_start"):
        out[f"lidar_{col}"] = pd.array(np.where(has, layer[col].to_numpy(float)[pick], np.nan), dtype="Int16")
    return out


class LinzLines:
    """The cached LINZ river lines (``<dir>/*.gpkg``, one file per viewer region; overlapping regions hold the same
    lines), read per box and de-duplicated."""

    def __init__(self, d: Path):
        self.paths = sorted(Path(d).glob("*.gpkg")) if Path(d).exists() else []
        self.bounds = {}
        import pyogrio

        for p in self.paths:
            try:
                self.bounds[p] = pyogrio.read_info(p)["total_bounds"]
            except Exception:  # noqa: BLE001 (a broken cache file is skipped)
                continue

    def read(self, box):
        import geopandas as gpd
        import pyogrio
        import shapely

        x0, y0, x1, y1 = box
        parts = []
        for p, b in self.bounds.items():
            if b[0] > x1 or b[2] < x0 or b[1] > y1 or b[3] < y0:
                continue
            g = pyogrio.read_dataframe(p, bbox=(x0, y0, x1, y1), columns=["river_section_id", "name"])
            if len(g):
                parts.append(g)
        if not parts:
            return None
        g = pd.concat(parts, ignore_index=True)
        key = shapely.to_wkb(g.geometry.to_numpy())
        g = g[~pd.Series(key).duplicated().to_numpy()].reset_index(drop=True)
        g = g[g.geometry.geom_type.isin(["LineString", "MultiLineString"])].reset_index(drop=True)
        return gpd.GeoDataFrame(g, geometry="geometry", crs="EPSG:2193")


def run(root: Path, reaches: Path | None = None, linz_dir: Path | None = None, layer_dir: Path | None = None,
        out: Path | None = None, log=print) -> dict:
    """``<root>/joins.parquet``: one row per reach of ``reaches.parquet`` (same order). Returns a summary."""
    import geopandas as gpd
    import pyarrow.parquet as pq
    import pyogrio

    from .lidar import coverage_layer, default_dir

    root = Path(root)
    reaches = reaches or root / "reaches.parquet"
    out = out or root / "joins.parquet"
    t0 = time.time()
    linz = LinzLines(linz_dir or root / "linz")
    if not linz.paths:
        log(f"joins: no LINZ lines in {linz_dir or root / 'linz'}: river_linz and linz_dist_m are left empty")
    lp = (layer_dir or default_dir()) / "lidar_coverage.gpkg"
    layer = coverage_layer(layer_dir or default_dir(), log=log) if lp.exists() else None
    if layer is None:
        log(f"joins: no {lp}: the LiDAR survey and year are left empty (river-finder lidar fetch)")
    geo_path = root / "geology.gpkg"
    pf = pq.ParquetFile(reaches)
    cols = ["gid", "length_m", "river_rec2", "river", "geometry"]
    parts, rec2_flags = [], []
    for k in range(pf.metadata.num_row_groups):
        tab = pf.read_row_group(k, columns=cols)
        f = tab.to_pandas()
        g = gpd.GeoSeries.from_wkb(f["geometry"].to_numpy(), crs="EPSG:2193")
        b = g.total_bounds
        box = (b[0] - PAD_M, b[1] - PAD_M, b[2] + PAD_M, b[3] + PAD_M)
        lines = line_arrays(g.values)
        x, y = midpoints(g.values)
        geo = pyogrio.read_dataframe(geo_path, bbox=box, columns=["rockgroup", "hard_rock"]) if geo_path.exists() \
            else None
        hard, rock = join_geology(x, y, geo)
        lz = join_linz(lines, linz.read(box) if linz.paths else None)
        sv = join_survey(lines, layer)
        rec2 = f["river_rec2"].fillna("").astype(str).to_numpy(object)
        river = np.where(rec2 != "", rec2, lz["river_linz"].to_numpy(object))
        rec2_flags.append(rec2 != "")
        parts.append(pd.concat([pd.DataFrame({"gid": f["gid"].to_numpy(np.int64), "bed_hard": hard,
                                              "bed_rock": rock}), lz, pd.DataFrame({"river": river}), sv,
                                pd.DataFrame({"sinuosity": sinuosity(g.values, f["length_m"].to_numpy())})], axis=1))
        log(f"joins: row group {k + 1}/{pf.metadata.num_row_groups}: {len(f)} reaches ({time.time() - t0:.0f} s)")
    j = pd.concat(parts, ignore_index=True)[COLUMNS]
    j["bed_rock"] = j["bed_rock"].astype(str)
    j["river_linz"] = j["river_linz"].astype(str)
    j["river"] = j["river"].astype(str)
    j["lidar_survey"] = j["lidar_survey"].astype(str)
    j.to_parquet(out, index=False, compression="zstd")
    rec2_named = np.concatenate(rec2_flags) if rec2_flags else np.zeros(0, bool)
    agree = rec2_named & (j["river_linz"].to_numpy() != "")
    s = {"reaches": int(len(j)), "seconds": round(time.time() - t0, 1),
         "named_rec2": int(rec2_named.sum()), "named_linz_only": int(((j["river"] != "") & ~rec2_named).sum()),
         "unnamed": int((j["river"] == "").sum()), "linz_named": int((j["river_linz"] != "").sum()),
         "rec2_and_linz_named": int(agree.sum()),
         "rec2_linz_same_name": int((agree & (j["river_linz"].to_numpy() == j["river"].to_numpy())).sum()),
         "linz_dist_m_median": None if j["linz_dist_m"].isna().all() else round(float(j["linz_dist_m"].median()), 1),
         "linz_within_30m_share": round(float((j["linz_dist_m"] <= LINZ_NEAR_M).mean()), 4),
         "bed_hard_share": round(float(j["bed_hard"].mean()), 4),
         "survey_share": round(float((j["lidar_survey"] != "").mean()), 4), "out": str(out)}
    log(f"joins: {s['reaches']} reaches -> {out}: {s['named_linz_only']} named from LINZ only, "
        f"{s['unnamed']} unnamed, median LINZ distance {s['linz_dist_m_median']} m ({s['seconds']} s)")
    return s


def read(root: Path) -> pd.DataFrame | None:
    p = Path(root) / "joins.parquet"
    return pd.read_parquet(p) if p.exists() else None
