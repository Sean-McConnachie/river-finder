"""Scaling zones (rf-t26.16): the units that gauge scale factors are computed for and applied to.

Without ``[[scaling.zones]]`` a zone is a network region (rf-t26.5: the segment table's ``region``,
``weights.region_of``), and its name is the region's. A region listed in ``[[scaling.zones]]`` is
split in two by distance to the **main divide**: catchments and gauges within ``divide_km`` of it are
zone ``near``, the others zone ``far`` (Canterbury: ``Canterbury divide`` within 15 km, ``Canterbury
east`` beyond: foothills, basins and plains). Zone names are the ids of the factor history
(:mod:`.scaling`): a new zone starts at version 1, and the factor of a region that is split is
closed with a history record.

**The main divide** is the border of the regions ``[scaling.divide] west`` (West Coast, Tasman) with
the regions ``east`` (Canterbury, Otago): the part of the west regions' outline within 50 m of an east
region, from the polygons the segment table's regions come from (:func:`..national.region_filters`:
council boundaries). That border follows the main divide from the Spenser Mountains past Lewis,
Arthur's and Haast passes to Mt Aspiring (about 600 km). Distances are in NZTM (EPSG:2193), to a line
simplified to 50 m.

**Catchments** are placed by their reach's point in the segment table (``x``, ``y``); **gauges** by
their NZTM position and network region (:meth:`ZoneMap.gauge_zones`); both use the same line, which
the zone file keeps, so ``rain scale`` can place gauges anywhere the file is.

``river-finder rain zones`` writes ``<rain>/scaling_zones.npz`` (``[scaling] zones_file``) and a
``.json`` report: ``zones`` (names), ``parent`` (the network region of each zone), ``zone_of`` (int16
zone index per catchment row, -1 none), ``divide_km`` (float32 distance of each catchment of a split
region to the divide, NaN elsewhere), the divide line (WKB) and in the metadata the rows digest of the
weights, the splits and the divide's regions, its length, and catchments and area per zone. The
hourly job reads it from the ``rain-inputs`` branch (``rain inputs`` copies it); without a file,
:func:`load_zones` falls back to one zone per region, unless gauge factors would then be applied with
the wrong zones (``rain scale``; ``rain ecmwf`` when a factor file is there).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .config import RainConfig, ZoneSplit

FORMAT = "river-finder rain scaling zones v1"
SHARED_TOL_M = 50.0     # the west regions' outline within this of an east region is the divide
SIMPLIFY_M = 50.0


@dataclass
class ZoneMap:
    """Catchment rows -> scaling zones, and the rule that places gauges in the same zones."""

    names: tuple[str, ...]          # zone names: the ids of the factor history
    parent: tuple[str, ...]         # network region of each zone
    zone_of: np.ndarray             # int16 zone index per catchment row (-1 none)
    rows_digest: str
    splits: tuple[ZoneSplit, ...] = ()
    divide_west: tuple[str, ...] = ()
    divide_east: tuple[str, ...] = ()
    divide_wkb: bytes = b""         # the main divide (NZTM), empty without splits
    divide_km: np.ndarray | None = None   # float32 per row: distance to the divide (split regions), NaN elsewhere
    meta: dict = field(default_factory=dict)

    def __post_init__(self):
        self.names, self.parent = tuple(self.names), tuple(self.parent)
        self.zone_of = np.asarray(self.zone_of, dtype=np.int16)
        if len(self.parent) != len(self.names) or len(set(self.names)) != len(self.names):
            raise ValueError("zones need distinct names, each with a parent region")
        if self.zone_of.size and self.zone_of.max(initial=-1) >= len(self.names):
            raise ValueError("zone_of names a zone that is not listed")

    @property
    def n(self) -> int:
        return int(self.zone_of.size)

    @property
    def identity(self) -> bool:
        """One zone per region (no split)."""
        return not self.splits

    def rule(self) -> dict:
        return {"splits": [{"region": s.region, "divide_km": s.divide_km, "near": s.near, "far": s.far}
                           for s in self.splits],
                "divide": {"west": list(self.divide_west), "east": list(self.divide_east)} if self.splits else None}

    def matches(self, cfg: RainConfig) -> bool:
        """Built with the zones of ``cfg`` (its splits of this network's regions, and the divide's regions)."""
        return self.rule() == rule_of(cfg, self.parent)

    def zone_index(self, name: str) -> int:
        return self.names.index(name)

    @property
    def divide(self):
        import shapely

        return shapely.from_wkb(self.divide_wkb) if self.divide_wkb else None

    def gauge_zones(self, regions, x, y) -> tuple[np.ndarray, np.ndarray]:
        """Zone index (-1: none) and distance to the divide (km, NaN when not measured) of points in network
        regions ``regions`` (names, "" = none) at NZTM ``x``, ``y``. A point without a position in a split
        region goes to the ``far`` zone (it has no cell either, so no comparison uses it)."""
        import shapely

        regions = [str(r) for r in regions]
        x, y = np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)
        zi = np.full(len(regions), -1, dtype=np.int16)
        km = np.full(len(regions), np.nan)
        by = {s.region: s for s in self.splits}
        line = self.divide
        for i, r in enumerate(regions):
            s = by.get(r)
            if s is None:
                zi[i] = self.names.index(r) if r in self.names else -1
                continue
            if line is not None and np.isfinite(x[i]) and np.isfinite(y[i]):
                km[i] = float(shapely.distance(line, shapely.Point(x[i], y[i]))) / 1000.0
                zi[i] = self.names.index(s.near if km[i] <= s.divide_km else s.far)
            else:
                zi[i] = self.names.index(s.far)
        return zi, km

    # --- files ----------------------------------------------------------------------------------
    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = dict(self.meta, format=FORMAT, zones=list(self.names), parent=list(self.parent),
                    rows_digest=self.rows_digest, **self.rule())
        dk = self.divide_km if self.divide_km is not None else np.full(self.n, np.nan, np.float32)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "wb") as fh:
            np.savez_compressed(fh, meta=np.array(json.dumps(meta, ensure_ascii=False)), zone_of=self.zone_of,
                                divide_km=dk.astype(np.float32),
                                divide_wkb=np.frombuffer(self.divide_wkb, dtype=np.uint8))
        os.replace(tmp, path)
        return path

    @classmethod
    def load(cls, path: Path) -> ZoneMap:
        with np.load(path, allow_pickle=False) as z:
            meta = json.loads(str(z["meta"]))
            if meta.get("format") != FORMAT:
                raise ValueError(f"{path}: not a {FORMAT} file")
            splits = tuple(ZoneSplit(s["region"], float(s["divide_km"]), s["near"], s["far"]) for s in meta["splits"])
            dv = meta.get("divide") or {}
            return cls(tuple(meta["zones"]), tuple(meta["parent"]), z["zone_of"], meta["rows_digest"], splits,
                       tuple(dv.get("west", ())), tuple(dv.get("east", ())), z["divide_wkb"].tobytes(),
                       z["divide_km"], {k: v for k, v in meta.items()
                                        if k not in ("format", "zones", "parent", "rows_digest", "splits", "divide")})


def active_splits(cfg: RainConfig, regions) -> tuple[ZoneSplit, ...]:
    """The ``[[scaling.zones]]`` of regions that the network has (a split of a region it lacks changes nothing)."""
    have = set(regions)
    return tuple(s for s in cfg.scaling.zones if s.region in have)


def rule_of(cfg: RainConfig, regions) -> dict:
    sc = cfg.scaling
    splits = active_splits(cfg, regions)
    return {"splits": [{"region": s.region, "divide_km": s.divide_km, "near": s.near, "far": s.far} for s in splits],
            "divide": {"west": list(sc.divide_west), "east": list(sc.divide_east)} if splits else None}


def identity(regions, region_of, rows_digest: str) -> ZoneMap:
    """One zone per network region, named after it (rf-t26.5)."""
    return ZoneMap(tuple(regions), tuple(regions), region_of, rows_digest)


def weights_regions(path: Path) -> tuple[tuple[str, ...], np.ndarray, str, np.ndarray]:
    """``regions``, ``region_of``, ``rows_digest`` and ``npix`` of a weights file, without its matrix."""
    with np.load(path, allow_pickle=False) as z:
        meta = json.loads(str(z["meta"]))
        return tuple(meta["regions"]), z["region_of"].astype(np.int16), meta["rows_digest"], z["npix"]


# --- building ---------------------------------------------------------------------------------------
def divide_line(base: Path, west, east):
    """The main divide (NZTM): the outline of the ``west`` regions where it runs within 50 m of an ``east``
    region, simplified to 50 m."""
    import shapely

    from ..national import region_filters

    rf = region_filters(base).set_index("region")
    missing = [r for r in (*west, *east) if r not in rf.index]
    if missing:
        raise SystemExit(f"[scaling.divide]: no region polygon for {', '.join(missing)} (have {', '.join(rf.index)})")
    w = shapely.union_all([rf.geometry[r] for r in west])
    e = shapely.union_all([rf.geometry[r] for r in east])
    line = shapely.line_merge(shapely.intersection(shapely.boundary(w), e.buffer(SHARED_TOL_M)))
    if line.is_empty:
        raise SystemExit(f"[scaling.divide]: {', '.join(west)} and {', '.join(east)} do not touch")
    return shapely.simplify(line, SIMPLIFY_M)


def build_zones(splits, west, east, table_xy: np.ndarray, regions, region_of, rows_digest: str, line,
                npix: np.ndarray | None = None, pixel_area_m2: float = 64.0) -> ZoneMap:
    """Zones of every catchment row: ``regions``/``region_of`` as the weights, ``table_xy`` (n, 2) NZTM points."""
    import shapely

    regions = tuple(regions)
    region_of = np.asarray(region_of, dtype=np.int16)
    by = {s.region: s for s in splits}
    bad = [s.region for s in splits if s.region not in regions]
    if bad:
        raise SystemExit(f"[[scaling.zones]]: {', '.join(bad)} is not a region of the network "
                         f"(have {', '.join(regions)})")
    names, parent, old_to_new = [], [], {}
    for k, r in enumerate(regions):
        s = by.get(r)
        if s is None:
            old_to_new[k] = (len(names),)
            names.append(r)
            parent.append(r)
        else:
            old_to_new[k] = (len(names), len(names) + 1)
            names += [s.near, s.far]
            parent += [r, r]
    zone_of = np.full(region_of.size, -1, dtype=np.int16)
    divide_km = np.full(region_of.size, np.nan, dtype=np.float32)
    stats = {}
    for k, r in enumerate(regions):
        rows = np.flatnonzero(region_of == k)
        s = by.get(r)
        if s is None:
            zone_of[rows] = old_to_new[k][0]
            continue
        pts = shapely.points(table_xy[rows, 0], table_xy[rows, 1])
        km = shapely.distance(line, pts) / 1000.0
        divide_km[rows] = np.round(km, 2)   # 10 m: compresses well
        near = km <= s.divide_km
        zone_of[rows] = np.where(near, old_to_new[k][0], old_to_new[k][1])
    area = npix.astype(np.float64) * pixel_area_m2 / 1e6 if npix is not None else None
    for j, name in enumerate(names):
        rows = zone_of == j
        stats[name] = {"region": parent[j], "catchments": int(rows.sum())}
        if area is not None:
            stats[name]["area_km2"] = round(float(area[rows].sum()), 1)
    meta = {"built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "divide_length_km": round(line.length / 1e3, 1), "zones_stats": stats}
    return ZoneMap(tuple(names), tuple(parent), zone_of, rows_digest, tuple(splits), tuple(west), tuple(east),
                   shapely.to_wkb(line), divide_km, meta)


def run_zones(cfg: RainConfig, family: str = "ecmwf", log=print) -> ZoneMap:
    """``rain zones``: the zone file for the rows of ``weights_<family>.npz`` from the segment table."""
    import pandas as pd

    sc = cfg.scaling
    wpath = cfg.weights_path(family)
    if not wpath.exists():
        raise SystemExit(f"no {wpath} (river-finder rain weights --grid {family})")
    regions, region_of, digest, npix = weights_regions(wpath)
    with np.load(wpath, allow_pickle=False) as z:
        px = float(json.loads(str(z["meta"]))["pixel_area_m2"])
    from ..national import FILTER_ORDER

    unknown = [s.region for s in sc.zones if s.region not in FILTER_ORDER]
    if unknown:
        raise SystemExit(f"[[scaling.zones]]: {', '.join(unknown)} is not a river-guide region "
                         f"({', '.join(FILTER_ORDER)})")
    splits = active_splits(cfg, regions)
    for s in sc.zones:
        if s not in splits:
            log(f"note: [[scaling.zones]] {s.region}: the network has no such region; not split")
    if not splits:
        zm = identity(regions, region_of, digest)
        zm.meta.update(built=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       zones_stats={r: {"region": r, "catchments": int((region_of == k).sum()),
                                        "area_km2": round(float(npix[region_of == k].sum() * px / 1e6), 1)}
                                    for k, r in enumerate(regions)})
        log("no [[scaling.zones]]: one zone per region")
    else:
        table = pd.read_pickle(cfg.segments_path)[["gid", "x", "y", "region"]]
        import hashlib

        if hashlib.sha1(np.ascontiguousarray(table["gid"].to_numpy(np.int64)).tobytes()).hexdigest()[:16] != digest:
            raise SystemExit(f"{cfg.segments_path.name} rows differ from {wpath.name}: rebuild the weights first")
        t0 = time.time()
        line = divide_line(cfg.nz_data, sc.divide_west, sc.divide_east)
        log(f"main divide: border of {', '.join(sc.divide_west)} with {', '.join(sc.divide_east)}, "
            f"{line.length / 1e3:.0f} km ({time.time() - t0:.1f} s)")
        zm = build_zones(splits, sc.divide_west, sc.divide_east, table[["x", "y"]].to_numpy(np.float64), regions,
                         region_of, digest, line, npix, px)
        log(f"zones of {zm.n} catchments in {time.time() - t0:.1f} s")
    path = zm.save(cfg.zones_path)
    rep = dict(zm.meta, format=FORMAT, file=path.name, zones=list(zm.names), parent=list(zm.parent),
               rows_digest=zm.rows_digest, bytes=path.stat().st_size, **zm.rule())
    path.with_suffix(".json").write_text(json.dumps(rep, indent=1, ensure_ascii=False))
    for name, st in zm.meta.get("zones_stats", {}).items():
        if name != st["region"]:
            log(f"  {name:<20} ({st['region']}): {st['catchments']:>7} catchments, {st.get('area_km2', 0):>9,.0f} km2")
    log(f"zones -> {path} ({path.stat().st_size / 1e3:.0f} kB): {len(zm.names)} zones")
    return zm


def load_zones(cfg: RainConfig, family: str = "ecmwf", strict: bool | None = None, log=print) -> ZoneMap:
    """The scaling zones for the rows of ``weights_<family>.npz``.

    No ``[[scaling.zones]]`` for a region of the network: one zone per region (no file needed). With
    splits: ``<rain>/<zones_file>`` when it is for these rows and these splits. Otherwise ``strict``
    (default: when a factor file of ``family`` exists and ``[scaling] apply`` is on, i.e. factors would be
    applied with the wrong zones) raises; else one zone per region is used and a note logged (the hourly
    job until the rain-inputs branch has the file: it has no factors, so nothing is scaled either way)."""
    wpath = cfg.weights_path(family)
    if not wpath.exists():
        raise SystemExit(f"no {wpath}: the scaling zones follow its rows (river-finder rain weights)")
    regions, region_of, digest, _ = weights_regions(wpath)
    if not active_splits(cfg, regions):
        return identity(regions, region_of, digest)
    p = cfg.zones_path
    why = None
    if not p.exists():
        why = f"no {p.name}"
    else:
        zm = ZoneMap.load(p)
        if zm.rows_digest != digest:
            why = f"{p.name} is for other rows than {wpath.name}"
        elif not zm.matches(cfg):
            why = f"{p.name} was built for other [[scaling.zones]] than the config's"
        else:
            return zm
    if strict is None:
        strict = cfg.scaling.apply and cfg.factors_path(family).exists()
    if strict:
        raise SystemExit(f"{why}: run river-finder rain zones (the scaling zones of config/rainfall.toml)")
    log(f"note: {why}: one scaling zone per region (river-finder rain zones builds the zones)")
    return identity(regions, region_of, digest)
