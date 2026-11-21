"""Rain gauges (rf-t26.4): hourly totals from the councils' Hilltop servers, cached, for gauge scaling.

``river-finder rain gauges [--days N] [--council wcrc ecan tdc] [--sites-only] [--discover]`` reads
``[gauges]`` in ``config/rainfall.toml`` and, per enabled council (councils run in parallel, one
request at a time per server with ``request_gap_s`` between them):

1. **discovery**: the first ``base_url`` x ``hts`` whose file-level MeasurementList names a rainfall
   measurement (``measurements`` hints in order; ``"Rainfall"`` also matches ``"Rainfall [Rainfall]"``);
2. **sites**: ``SiteList&Location=LatLong&Measurement=<m>``. Each gauge is placed: NZTM2000
   (EPSG:2193), the ECMWF 0.1° cell it falls in (``[grids.ecmwf]``, :meth:`.grid.GridSpec.cell_rc`)
   and its scaling region (below);
3. **per gauge**: ``MeasurementList&Site`` (first and last record time, units, the name to ask for),
   then ``GetData`` (``ShowQuality=Yes``) for the wanted hours that are not cached yet plus the
   newest ``refetch_hours``, in requests of at most ``chunk_days``, and the quality-code series
   (``tsType=StdQualSeries``) over the same span;
4. **hours** (:func:`hours_from_records`): records summed into hours ending, with a state and flags
   per hour; merged into the gauge's cache (newer fetch wins, hours older than ``keep_days`` dropped);
5. **output**: ``gauges_hourly.npz`` + ``.json`` over the cached hours of every gauge of the councils run.

``--sites-only`` stops after step 2 (site list + placement); ``--discover`` probes every candidate
file and prints what each server has (measurements, collections, data files, a sample site, how far
its newest record is from now), into ``discover.json``; neither touches the series.

Hours
-----
Hour ``h`` is the whole UTC hour ending at ``h`` (hours since the epoch, :mod:`.grid`), holding the
rain in ``(h - 1, h]``: a record stamped at ``hh:00:00`` belongs to the hour ending ``hh:00``. Hilltop
rainfall records are incremental (the rain since the record before, a tip of 0.2-0.5 mm or a 5-15
minute total), so ``method = "raw"`` sums the records whose time falls in the hour; an hour with no
record inside the record's extent is 0 mm. ``total-end`` / ``total-start`` instead take the server's
``Method=Total&Interval=1 hour&Alignment=00:00``, stamped at the end / start of each hour (which one
a server uses is not documented: check before switching); an hour it does not return is missing.

States and flags (per gauge and hour)
-------------------------------------
``state`` (uint8): 0 **good** (value; quality code >= 500 for the whole hour), 1 **raw** (value; no
quality code yet, or NEMS QC 200 / 0: unverified telemetry, the norm for recent hours), 2 **suspect**
(value kept but not to be used: see flags), 3 **missing** (no value, ``mm`` is NaN). Use hours with
``state <= 1`` (:attr:`GaugeHours.ok`). ``flags`` (uint8 bits) say why:

====  ===========  ====================================================================================
bit   name         meaning
====  ===========  ====================================================================================
1     gap          the hour lies wholly in a gap the server marked (``<Gap/>``): missing
2     partial      part of the hour is in a gap or outside the record: the total may be short: suspect
4     outside      the hour is wholly before the record's first or after its last time: missing
8     no_value     ``total-*`` methods: the server returned no total for the hour: missing
16    range        a record unreadable or negative, or the hour above ``max_mm_per_hour``: suspect
32    low_qc       quality code 300-499 (NEMS: below fair) or not a NEMS code: suspect
64    qc_missing   quality code 100 (NEMS "missing record"): missing
128   not_fetched  no request has covered the hour (before the cache began, or its request failed)
====  ===========  ====================================================================================

``qc`` (int16) is the worst (lowest) quality code that applies to any part of the hour, -1 if none.
Codes come from the quality series when the server has one (a code at time T applies to the period
since the code before it, up to T; time after the last code is not coded yet), else from the
records' own ``Q1``. The hour after a gauge's last record is "outside": a tipping-bucket gauge that
has not tipped since looks the same as one that stopped, so dry spells at the end of a record show as
missing until the gauge reports again (``refetch_hours`` fetches them again).

Regions
-------
A gauge's scaling region is a region of the reach network (``buffer.region_of``, ``Network.regions``,
the river-guide regions of :func:`..national.region_filters`, e.g. ``"West Coast"``), with the site's
id (``"west-coast"``, as ``live.viewer_map``). Placed, in order: point in the region polygons when
``councils.gpkg`` and ``large_catchments.gpkg`` are in the data directory (exactly as the segment
table's ``region``: method ``polygon``); else the region holding most catchment area in the gauge's
ECMWF cell (``weights_ecmwf.npz``: ``cell``); else the council's ``region`` (``council``). A gauge
without a location gets its council's region and cell -1.

Files under ``<rain>/gauges/`` (``cfg.gauges_dir``)
---------------------------------------------------
- ``sites.json``: per council the server, file and measurement found and when; per gauge the
  record below (``id`` = ``"<council>:<site name>"``);
- ``series/<council>/<key>.npz``: one gauge's cached hours (contiguous ``first .. last``: ``mm``,
  ``qc``, ``state``, ``flags``) and its recent fetches;
- ``gauges_hourly.npz`` (the input of rf-t26.5, :class:`GaugeHours`): ``hour`` (T,), ``mm`` float32,
  ``qc`` int16, ``state`` / ``flags`` uint8 (G, T); per gauge ``id``, ``council``, ``name``, ``lon``,
  ``lat`` (WGS84), ``x``, ``y`` (NZTM), ``cell`` (flat index on ``[grids.ecmwf]``, -1 outside),
  ``cell_row``, ``cell_col``, ``region_of`` (index into ``meta.regions``, -1 none); ``meta`` JSON
  (grid, regions and their ids, councils with attribution and licence, state and flag names);
- ``gauges_hourly.json``: the run report: per council (server, requests, errors), per region (gauges,
  how many cover 90 % of the last 30 days), per gauge (place, cell, region, record times, hours per
  state over the cache and the last 30 days, mm in the last 24 h and 30 days, warnings).

For rf-t26.5::

    g = GaugeHours.load(cfg.gauges_dir / "gauges_hourly.npz")
    w = g.window(hours=720)                 # per gauge: mm over ok hours, n_ok, n_missing, n_suspect
    for region, rows in g.region_rows().items():
        rows[w["n_ok"][rows] >= 0.9 * 720]  # gauges with 90 % of the 30 days
    g.cell[rows]                            # their ECMWF cells: compare with the grid totals there
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from .config import CouncilConfig, GaugesConfig, RainConfig
from .fetch import FetchError, Http, NotFound
from .grid import GridSpec, floor_hour, hour_index, hour_time
from .hilltop import (
    HilltopClient,
    HilltopError,
    Pacer,
    Series,
    TimeStats,
    choose_measurement,
    find_source,
    rain_like,
    to_utc,
)

SITES_FORMAT = "river-finder rain gauge sites v1"
SERIES_FORMAT = "river-finder rain gauge series v1"
HOURLY_FORMAT = "river-finder rain gauges hourly v1"

GOOD, RAW, SUSPECT, MISSING = 0, 1, 2, 3
STATES = ("good", "raw", "suspect", "missing")
STATE_DOC = {
    "good": "value; quality code >= 500 (NEMS fair or good) over the whole hour",
    "raw": "value; not quality coded yet (no code, QC 0 or 200): unverified telemetry, usable",
    "suspect": "value kept but not to be used (flags: partial, range, low_qc)",
    "missing": "no value (mm NaN; flags: gap, outside, no_value, qc_missing, not_fetched)",
}
F_GAP, F_PARTIAL, F_OUTSIDE, F_NO_VALUE, F_RANGE, F_LOW_QC, F_QC_MISSING, F_NOT_FETCHED = (1, 2, 4, 8, 16, 32, 64,
                                                                                          128)
FLAGS = {F_GAP: "gap", F_PARTIAL: "partial", F_OUTSIDE: "outside", F_NO_VALUE: "no_value", F_RANGE: "range",
         F_LOW_QC: "low_qc", F_QC_MISSING: "qc_missing", F_NOT_FETCHED: "not_fetched"}
FLAG_DOC = {
    "gap": "the hour lies wholly in a gap the server marked (<Gap/>)",
    "partial": "part of the hour is in a gap or outside the record: the total may be short",
    "outside": "the hour is wholly before the record's first or after its last time",
    "no_value": "total-* methods: the server returned no total for the hour",
    "range": "a record unreadable or negative, or the hour above max_mm_per_hour",
    "low_qc": "quality code 300-499 (NEMS: below fair) or not a NEMS code",
    "qc_missing": "quality code 100 (NEMS: missing record)",
    "not_fetched": "no request has covered the hour",
}
MISSING_FLAGS = F_GAP | F_OUTSIDE | F_NO_VALUE | F_QC_MISSING | F_NOT_FETCHED
SUSPECT_FLAGS = F_PARTIAL | F_RANGE | F_LOW_QC
HOUR_CONVENTION = "hour h = the whole UTC hour ending at h (hours since 1970-01-01T00:00Z): rain in (h - 1, h]"


# --- quality codes and hours ------------------------------------------------------------------------
def qc_state(qc) -> tuple[np.ndarray, np.ndarray]:
    """State and flags implied by quality codes (NEMS National Quality Code Schema): >= 500 good;
    300-499 suspect (low_qc); 100 missing (qc_missing); none (-1), 0 and 200-299 raw (not coded / unverified);
    other codes (1-99, 101-199) suspect (low_qc)."""
    qc = np.asarray(qc, dtype=np.int32)
    state = np.full(qc.shape, RAW, dtype=np.uint8)
    flags = np.zeros(qc.shape, dtype=np.uint8)
    state[qc >= 500] = GOOD
    low = ((qc >= 300) & (qc < 500)) | ((qc > 0) & (qc < 200) & (qc != 100))
    state[low], flags[low] = SUSPECT, F_LOW_QC
    state[qc == 100], flags[qc == 100] = MISSING, F_QC_MISSING
    return state, flags


@dataclass
class HourBlock:
    """One gauge's hours ``first .. first + n - 1`` (hour ending): mm (NaN = missing), qc, state, flags."""

    first: int
    mm: np.ndarray
    qc: np.ndarray
    state: np.ndarray
    flags: np.ndarray

    @property
    def n(self) -> int:
        return len(self.mm)

    @property
    def last(self) -> int:
        return self.first + self.n - 1

    @property
    def hours(self) -> np.ndarray:
        return np.arange(self.first, self.first + self.n, dtype=np.int64)

    @staticmethod
    def empty(first: int, n: int, flag: int = F_NOT_FETCHED) -> HourBlock:
        return HourBlock(int(first), np.full(n, np.nan, np.float32), np.full(n, -1, np.int16),
                         np.full(n, MISSING, np.uint8), np.full(n, flag, np.uint8))

    def slice(self, first: int, last: int) -> HourBlock:
        a, b = max(first, self.first) - self.first, min(last, self.last) - self.first + 1
        if b <= a:
            return HourBlock.empty(first, 0)
        return HourBlock(self.first + a, self.mm[a:b], self.qc[a:b], self.state[a:b], self.flags[a:b])

    def counts(self) -> dict[str, int]:
        out = {s: int(np.count_nonzero(self.state == k)) for k, s in enumerate(STATES)}
        out["not_fetched"] = int(np.count_nonzero(self.flags & F_NOT_FETCHED))
        return out


def _quality_per_hour(q_s: np.ndarray, q_code: np.ndarray, hstart: np.ndarray, hend: np.ndarray):
    """Worst code over each hour from a quality series (a code at T covers the time since the code before it,
    up to T), and whether part of the hour lies after the last code (not coded yet)."""
    n = len(hstart)
    qc = np.full(n, -1, dtype=np.int32)
    if not len(q_s):
        return qc, np.ones(n, dtype=bool)
    j0 = np.searchsorted(q_s, hstart, side="right")      # first code with T > hour start
    j1 = np.searchsorted(q_s, hend, side="left")         # first code with T >= hour end: covers the hour end
    uncoded = j1 >= len(q_s)
    at_end = np.where(uncoded, np.iinfo(np.int32).max, q_code[np.minimum(j1, len(q_s) - 1)])
    worst = at_end.copy()
    for i in np.flatnonzero(j1 > j0):                    # codes whose period ends inside the hour (rare)
        worst[i] = min(int(worst[i]), int(q_code[j0[i]:j1[i]].min()))
    has = worst != np.iinfo(np.int32).max
    qc[has] = worst[has]
    return qc, uncoded


def hours_from_records(t_s: np.ndarray, values: np.ndarray, first: int, last: int, *,
                       gap_before: np.ndarray | None = None, gap_at_end: bool = False,
                       rec_start: float | None = None, rec_end: float | None = None,
                       rec_qc: np.ndarray | None = None, qual: tuple[np.ndarray, np.ndarray] | None = None,
                       method: str = "raw", max_mm: float = 200.0) -> HourBlock:
    """Hours ``first .. last`` (hour ending) of one gauge from its records.

    ``t_s``: record times (epoch seconds, UTC, in record order); ``values``: mm per record (incremental for
    ``raw``, hourly totals for ``total-end`` / ``total-start``); ``gap_before[i]``: a ``<Gap/>`` came right before
    record ``i`` (before the span start for ``i = 0``), ``gap_at_end``: one after the last record (to the span
    end); ``rec_start`` / ``rec_end``: the record's first and last time (seconds; None = unknown: the record is
    taken to start before the span and to end at its last record, or nowhere when it has none); ``rec_qc``:
    the records' own quality codes (-1 none); ``qual``: the quality series (times, codes), used instead
    when given.
    """
    n = last - first + 1
    if n <= 0:
        return HourBlock.empty(first, 0)
    t_s = np.asarray(t_s, dtype=np.int64)
    values = np.asarray(values, dtype=np.float64)
    gap_before = np.zeros(len(t_s), bool) if gap_before is None else np.asarray(gap_before, bool)
    span0, span1 = (first - 1) * 3600, last * 3600
    hours = np.arange(first, last + 1, dtype=np.int64)
    hstart, hend = (hours - 1) * 3600, hours * 3600
    flags = np.zeros(n, dtype=np.uint8)

    if method == "raw":
        h_of = -(-t_s // 3600)                            # ceil: a record at hh:00:00 is in the hour ending hh
    elif method == "total-end":
        h_of = -(-t_s // 3600)
    elif method == "total-start":
        h_of = t_s // 3600 + 1
    else:
        raise ValueError(f"unknown method {method!r}")
    inside = (h_of >= first) & (h_of <= last)
    idx = (h_of[inside] - first).astype(np.intp)
    v = values[inside]
    bad = ~np.isfinite(v) | (v < 0)
    if method == "raw":
        mm = np.bincount(idx, weights=np.where(bad, 0.0, v), minlength=n)
    else:
        mm = np.full(n, np.nan)
        got = np.zeros(n, bool)
        mm[idx], got[idx] = np.where(bad, np.nan, v), True  # duplicates: the last one wins
        flags[~got] |= F_NO_VALUE
    nbad = np.bincount(idx, weights=bad.astype(np.float64), minlength=n)
    flags[nbad > 0] |= F_RANGE
    flags[np.nan_to_num(mm, nan=0.0) > max_mm] |= F_RANGE

    # gaps the server marked
    gaps = []
    for i in np.flatnonzero(gap_before):
        gaps.append((t_s[i - 1] if i > 0 else span0, t_s[i]))
    if gap_at_end:
        gaps.append((t_s[-1] if len(t_s) else span0, span1))
    # the record's extent (never narrower than the records themselves; a start-stamped total covers the hour after)
    lo = float(t_s.min()) if len(t_s) else math.inf
    hi = float(t_s.max()) + (3600 if method == "total-start" else 0) if len(t_s) else -math.inf
    r0 = -math.inf if rec_start is None else min(float(rec_start), lo)
    r1 = hi if rec_end is None else max(float(rec_end), hi)
    for (a, b), bit in [(g, F_GAP) for g in gaps] + [((-math.inf, r0), F_OUTSIDE), ((r1, math.inf), F_OUTSIDE)]:
        if not b > a:
            continue
        whole = (hstart >= a) & (hend <= b)
        part = (hend > a) & (hstart < b) & ~whole
        flags[whole] |= bit
        flags[part] |= F_PARTIAL

    # quality codes
    uncoded = np.ones(n, dtype=bool)
    if qual is not None and len(qual[0]):
        qc, uncoded = _quality_per_hour(np.asarray(qual[0], np.int64), np.asarray(qual[1], np.int32), hstart, hend)
    else:
        qc = np.full(n, -1, dtype=np.int32)
        if rec_qc is not None and len(idx):
            rq = np.asarray(rec_qc, dtype=np.int32)[inside]
            big = np.iinfo(np.int32).max
            w = np.full(n, big, dtype=np.int64)
            np.minimum.at(w, idx[rq >= 0], rq[rq >= 0])
            qc = np.where(w == big, -1, w).astype(np.int32)
            uncoded = qc < 0
    state, qflags = qc_state(qc)
    flags |= qflags
    state[(state == GOOD) & uncoded] = RAW
    state = np.where(flags & SUSPECT_FLAGS, np.maximum(state, SUSPECT), state).astype(np.uint8)
    state[(flags & MISSING_FLAGS) != 0] = MISSING
    mm = np.where(state == MISSING, np.nan, mm).astype(np.float32)
    return HourBlock(first, mm, qc.astype(np.int16), state, flags)


def merge(old: HourBlock | None, new: HourBlock, keep_from: int | None = None) -> HourBlock:
    """``old`` with ``new``'s hours written over it (the newer fetch wins), hours before ``keep_from`` dropped;
    hours between the two that neither has are missing / not_fetched."""
    if old is None or old.n == 0:
        out = new
    else:
        first, last = min(old.first, new.first), max(old.last, new.last)
        out = HourBlock.empty(first, last - first + 1)
        for b in (old, new):
            a = b.first - first
            out.mm[a:a + b.n], out.qc[a:a + b.n] = b.mm, b.qc
            out.state[a:a + b.n], out.flags[a:a + b.n] = b.state, b.flags
    if keep_from is not None and out.first < keep_from:
        out = out.slice(keep_from, out.last)
    return out


def plan(cached: HourBlock | None, first: int, last: int, refetch_from: int) -> list[tuple[int, int]]:
    """Ranges of hours (inclusive, hour ending) to ask for: the wanted hours the cache lacks (or never fetched)
    plus every hour from ``refetch_from`` on."""
    n = last - first + 1
    if n <= 0:
        return []
    need = np.ones(n, dtype=bool)
    if cached is not None and cached.n:
        a, b = max(first, cached.first), min(last, cached.last)
        if b >= a:
            held = (cached.flags[a - cached.first:b - cached.first + 1] & F_NOT_FETCHED) == 0
            need[a - first:b - first + 1] = ~held
    need[max(0, refetch_from - first):] = True
    out, i = [], 0
    while i < n:
        if not need[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and need[j + 1]:
            j += 1
        out.append((first + i, first + j))
        i = j + 1
    return out


def chunks(ranges: list[tuple[int, int]], max_hours: int) -> list[tuple[int, int]]:
    out = []
    for a, b in ranges:
        while a <= b:
            out.append((a, min(b, a + max_hours - 1)))
            a += max_hours
    return out


# --- gauges, placement ----------------------------------------------------------------------------
def slug(name: str) -> str:
    """Region or site name -> id (as ``live._slug`` / ``nzscore.slug``: "Hawke's Bay" -> "hawkes-bay")."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower().replace("'", "")).strip("-")


def site_key(name: str) -> str:
    """File name stem of a site: its slug and 6 hex digits of its name's sha1 (names that slug alike)."""
    return f"{slug(name)[:60] or 'site'}-{hashlib.sha1(name.encode()).hexdigest()[:6]}"


@dataclass
class Gauge:
    id: str                      # "<council>:<site name>"
    council: str
    name: str
    measurement: str = ""        # the name asked for in GetData
    lon: float = math.nan        # WGS84 (the server's NZGD2000)
    lat: float = math.nan
    x: float = math.nan          # NZTM2000 (EPSG:2193)
    y: float = math.nan
    cell: int = -1               # flat index on [grids.ecmwf] (row * nx + col), -1 outside or unplaced
    cell_row: int = -1
    cell_col: int = -1
    region: str = ""             # network region name ("West Coast")
    region_method: str = ""      # polygon | cell | council
    units: str = ""
    interpolation: str = ""
    record_first: str | None = None   # first and last record time, UTC ISO (from MeasurementList)
    record_last: str | None = None

    @property
    def key(self) -> str:
        return site_key(self.name)

    @property
    def region_id(self) -> str:
        return slug(self.region) if self.region else ""

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("lon", "lat", "x", "y"):
            d[k] = None if not math.isfinite(d[k]) else round(d[k], 6 if k in ("lon", "lat") else 1)
        d["region_id"] = self.region_id
        return d

    @staticmethod
    def from_dict(d: dict) -> Gauge:
        kw = {k: d.get(k) for k in Gauge.__dataclass_fields__ if k in d}
        for k in ("lon", "lat", "x", "y"):
            kw[k] = math.nan if kw.get(k) is None else float(kw[k])
        return Gauge(**kw)


def to_nztm(lon: np.ndarray, lat: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    from pyproj import Transformer

    t = Transformer.from_crs("EPSG:4326", "EPSG:2193", always_xy=True)
    x, y = t.transform(np.asarray(lon, np.float64), np.asarray(lat, np.float64))
    return np.asarray(x, np.float64), np.asarray(y, np.float64)


def cell_regions(weights_path: Path, cells: np.ndarray) -> list[str]:
    """Per cell: the region with the most catchment pixels in it (``weights_<grid>.npz``), "" for none."""
    with np.load(weights_path, allow_pickle=False) as z:
        meta = json.loads(str(z["meta"]))
        import scipy.sparse as sp

        counts = sp.csr_matrix((z["counts"], z["indices"], z["indptr"]), shape=tuple(z["shape"])).tocsc()
        region_of = z["region_of"].astype(np.int64)
    regions = meta["regions"]
    out = []
    for c in np.asarray(cells, np.int64):
        if c < 0 or c >= counts.shape[1]:
            out.append("")
            continue
        col = counts[:, int(c)]
        r = region_of[col.indices]
        ok = r >= 0
        if not ok.any():
            out.append("")
            continue
        out.append(regions[int(np.argmax(np.bincount(r[ok], weights=col.data[ok], minlength=len(regions))))])
    return out


def place(gauges: list[Gauge], grid: GridSpec, cfg: RainConfig, log=print) -> dict[str, int]:
    """NZTM, ECMWF cell and scaling region of each gauge, in place; returns how many by region method."""
    if not gauges:
        return {}
    lon = np.array([g.lon for g in gauges])
    lat = np.array([g.lat for g in gauges])
    ok = np.isfinite(lon) & np.isfinite(lat)
    x, y = np.full(len(gauges), np.nan), np.full(len(gauges), np.nan)
    if ok.any():
        x[ok], y[ok] = to_nztm(lon[ok], lat[ok])
    row, col = grid.cell_rc(np.where(ok, lon, 0.0), np.where(ok, lat, 0.0))
    inside = ok & (row >= 0) & (row < grid.ny) & (col >= 0) & (col < grid.nx)
    regions = [""] * len(gauges)
    methods = [""] * len(gauges)
    nz = cfg.nz_data
    if ok.any() and (nz / "councils.gpkg").exists() and (nz / "large_catchments.gpkg").exists():
        import geopandas as gpd

        from ..national import assign_regions

        pts = gpd.GeoSeries(gpd.points_from_xy(x[ok], y[ok]), crs="EPSG:2193")
        for i, r in zip(np.flatnonzero(ok), assign_regions(pts, base=nz), strict=True):
            if r:
                regions[i], methods[i] = str(r), "polygon"
    elif inside.any() and cfg.weights_path("ecmwf").exists():
        idx = np.flatnonzero(inside)
        for i, r in zip(idx, cell_regions(cfg.weights_path("ecmwf"), row[idx] * grid.nx + col[idx]), strict=True):
            if r:
                regions[i], methods[i] = r, "cell"
    by_council = {c.id: c.region for c in cfg.gauges.councils}
    for i, g in enumerate(gauges):
        g.x, g.y = float(x[i]), float(y[i])
        g.cell_row, g.cell_col = (int(row[i]), int(col[i])) if inside[i] else (-1, -1)
        g.cell = int(row[i] * grid.nx + col[i]) if inside[i] else -1
        g.region, g.region_method = (regions[i], methods[i]) if regions[i] else (by_council.get(g.council, ""),
                                                                                  "council")
    out: dict[str, int] = {}
    for g in gauges:
        out[g.region_method] = out.get(g.region_method, 0) + 1
    return out


# --- cache files ----------------------------------------------------------------------------------
def _write_json(path: Path, d: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False))
    os.replace(tmp, path)


def series_path(gdir: Path, g: Gauge) -> Path:
    return gdir / "series" / g.council / f"{g.key}.npz"


def save_series(path: Path, block: HourBlock, meta: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        np.savez_compressed(fh, meta=np.array(json.dumps(dict(meta, format=SERIES_FORMAT, first=block.first))),
                            mm=block.mm, qc=block.qc, state=block.state, flags=block.flags)
    os.replace(tmp, path)
    return path


def load_series(path: Path) -> tuple[HourBlock, dict] | tuple[None, dict]:
    if not path.exists():
        return None, {}
    with np.load(path, allow_pickle=False) as z:
        meta = json.loads(str(z["meta"]))
        if meta.get("format") != SERIES_FORMAT:
            raise ValueError(f"{path}: not a {SERIES_FORMAT} file")
        return HourBlock(int(meta["first"]), z["mm"].astype(np.float32), z["qc"].astype(np.int16),
                         z["state"].astype(np.uint8), z["flags"].astype(np.uint8)), meta


def load_sites(gdir: Path) -> dict:
    p = gdir / "sites.json"
    if not p.exists():
        return {"format": SITES_FORMAT, "councils": {}, "gauges": []}
    d = json.loads(p.read_text())
    if d.get("format") != SITES_FORMAT:
        raise ValueError(f"{p}: not a {SITES_FORMAT} file")
    return d


# --- combined output ------------------------------------------------------------------------------
@dataclass
class GaugeHours:
    """Every gauge's hours on one time axis: the input of the gauge scale factors (rf-t26.5)."""

    hour: np.ndarray        # int64 (T,): hour ending, hours since the epoch, consecutive
    mm: np.ndarray          # float32 (G, T), NaN = missing
    qc: np.ndarray          # int16 (G, T), -1 = no quality code
    state: np.ndarray       # uint8 (G, T): GOOD RAW SUSPECT MISSING
    flags: np.ndarray       # uint8 (G, T): FLAGS bits
    gauges: dict[str, np.ndarray]   # per gauge: id council name lon lat x y cell cell_row cell_col region_of
    regions: tuple[str, ...]
    meta: dict = field(default_factory=dict)

    GAUGE_FIELDS = ("id", "council", "name", "lon", "lat", "x", "y", "cell", "cell_row", "cell_col", "region_of")

    @property
    def n(self) -> int:
        return int(self.mm.shape[0])

    @property
    def ok(self) -> np.ndarray:
        """Hours with a usable value (good or raw)."""
        return self.state <= RAW

    def __getattr__(self, k):
        g = self.__dict__.get("gauges")
        if g is not None and k in g:
            return g[k]
        raise AttributeError(k)

    def region_rows(self) -> dict[str, np.ndarray]:
        ro = self.gauges["region_of"]
        return {r: np.flatnonzero(ro == k) for k, r in enumerate(self.regions)}

    def cols(self, end_hour: int | None, hours: int) -> np.ndarray:
        end = int(self.hour[-1]) if end_hour is None else int(end_hour)
        return np.flatnonzero((self.hour > end - hours) & (self.hour <= end))

    def window(self, end_hour: int | None = None, hours: int = 720) -> dict[str, np.ndarray]:
        """Per gauge over the ``hours`` ending ``end_hour`` (default: the last hour held): ``mm`` summed over usable
        hours, ``n_ok``, ``n_suspect``, ``n_missing`` (hours of the window the axis does not reach count missing)."""
        c = self.cols(end_hour, hours)
        ok = self.ok[:, c]
        outside = hours - len(c)
        return {"mm": np.where(ok, np.nan_to_num(self.mm[:, c]), 0.0).sum(axis=1),
                "n_ok": ok.sum(axis=1), "n_suspect": (self.state[:, c] == SUSPECT).sum(axis=1),
                "n_missing": (self.state[:, c] == MISSING).sum(axis=1) + outside}

    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = dict(self.meta, format=HOURLY_FORMAT, regions=list(self.regions))
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "wb") as fh:
            np.savez_compressed(fh, meta=np.array(json.dumps(meta, ensure_ascii=False)), hour=self.hour, mm=self.mm,
                                qc=self.qc, state=self.state, flags=self.flags, **self.gauges)
        os.replace(tmp, path)
        return path

    @classmethod
    def load(cls, path: Path) -> GaugeHours:
        with np.load(path, allow_pickle=False) as z:
            meta = json.loads(str(z["meta"]))
            if meta.get("format") != HOURLY_FORMAT:
                raise ValueError(f"{path}: not a {HOURLY_FORMAT} file")
            return cls(z["hour"], z["mm"], z["qc"], z["state"], z["flags"], {k: z[k] for k in cls.GAUGE_FIELDS},
                       tuple(meta["regions"]), meta)


def build_hourly(gdir: Path, gauges: list[Gauge], last: int, keep_from: int) -> GaugeHours:
    """The cached hours of ``gauges`` on one axis from the earliest hour any holds (not before ``keep_from``)
    to ``last``; hours a gauge's cache lacks are missing / not_fetched."""
    blocks = [load_series(series_path(gdir, g))[0] for g in gauges]
    starts = [b.first for b in blocks if b is not None and b.n]
    first = max(keep_from, min(starts)) if starts else last
    first = min(first, last)
    T = last - first + 1
    G = len(gauges)
    mm = np.full((G, T), np.nan, np.float32)
    qc = np.full((G, T), -1, np.int16)
    state = np.full((G, T), MISSING, np.uint8)
    flags = np.full((G, T), F_NOT_FETCHED, np.uint8)
    for i, b in enumerate(blocks):
        if b is None:
            continue
        s = b.slice(first, last)
        if s.n:
            a = s.first - first
            mm[i, a:a + s.n], qc[i, a:a + s.n], state[i, a:a + s.n], flags[i, a:a + s.n] = (s.mm, s.qc, s.state,
                                                                                            s.flags)
    regions = tuple(sorted({g.region for g in gauges if g.region}))
    ridx = {r: k for k, r in enumerate(regions)}
    cols = {"id": np.array([g.id for g in gauges], dtype=str), "council": np.array([g.council for g in gauges], str),
            "name": np.array([g.name for g in gauges], dtype=str),
            "lon": np.array([g.lon for g in gauges], np.float64), "lat": np.array([g.lat for g in gauges], np.float64),
            "x": np.array([g.x for g in gauges], np.float64), "y": np.array([g.y for g in gauges], np.float64),
            "cell": np.array([g.cell for g in gauges], np.int32),
            "cell_row": np.array([g.cell_row for g in gauges], np.int32),
            "cell_col": np.array([g.cell_col for g in gauges], np.int32),
            "region_of": np.array([ridx.get(g.region, -1) for g in gauges], np.int16)}
    return GaugeHours(np.arange(first, last + 1, dtype=np.int64), mm, qc, state, flags, cols, regions)


# --- fetching -------------------------------------------------------------------------------------
def _iso(epoch_s: float | None) -> str | None:
    if epoch_s is None or not math.isfinite(epoch_s):
        return None
    return datetime.fromtimestamp(int(epoch_s), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str | None) -> float | None:
    return None if not s else datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _hour(h: int) -> str:
    return hour_time(h).strftime("%Y-%m-%d %H:00Z")


@dataclass
class CouncilRun:
    council: str
    status: str = "not run"           # ok | partial | failed: <why> | sites only
    base_url: str = ""
    hts: str = ""
    measurement: str = ""
    gauges: list[Gauge] = field(default_factory=list)
    fetched: int = 0                  # gauges whose series this run updated
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    times: TimeStats = field(default_factory=TimeStats)
    http: dict = field(default_factory=dict)
    seconds: float = 0.0


def _client(c: CouncilConfig, base: str, hts: str, http: Http, pacer: Pacer) -> HilltopClient:
    return HilltopClient(base, hts, c.timezone, http, pacer)


def find_file(c: CouncilConfig, http: Http, pacer: Pacer, log=print) -> tuple[HilltopClient, str, list[str]]:
    """The first server root x .hts file that answers with a rainfall measurement: (client, measurement,
    what was tried). Raises FetchError (nothing answered) or HilltopError (answers without rain)."""
    tried, answered = [], False
    for base in c.base_urls:
        for hts in c.hts:
            cl = _client(c, base, hts, http, pacer)
            try:
                names = cl.measurement_names()
            except (FetchError, NotFound) as e:
                tried.append(f"{cl.url('MeasurementList')}: {e}")
                continue
            except HilltopError as e:
                answered = True
                tried.append(f"{cl.url('MeasurementList')}: Hilltop error {e}")
                continue
            answered = True
            m = choose_measurement(names, c.measurements)
            if m:
                return cl, m, tried
            tried.append(f"{cl.url('MeasurementList')}: {len(names)} measurements, none of {list(c.measurements)}"
                         + (f" (rain-like: {rain_like(names)[:8]})" if rain_like(names) else ""))
    msg = "; ".join(tried)
    raise (HilltopError(msg) if answered else FetchError(msg))


def _record_extent(src, tz: str) -> tuple[float | None, float | None, TimeStats]:
    st = TimeStats()
    out = []
    for t in (src.first if src else None, src.last if src else None):
        if t is None:
            out.append(None)
            continue
        s, ts = to_utc([t], tz)
        st.add(ts)
        out.append(float(s[0]))
    return out[0], out[1], st


def fetch_gauge(cl: HilltopClient, c: CouncilConfig, gc: GaugesConfig, g: Gauge, gdir: Path, first: int,
                last: int, refetch_from: int, run: CouncilRun, now_s: float, log=print) -> bool:
    """Fetch what ``g``'s cache lacks in ``first .. last`` (plus the newest hours) and merge it; True if updated."""
    try:
        sources = cl.site_measurements(g.name)
    except (FetchError, NotFound, HilltopError) as e:
        run.errors.append(f"{g.name}: MeasurementList: {e}")
        return False
    src = find_source(sources, run.measurement)
    g.measurement = src.request_name if src else run.measurement
    if src:
        g.units, g.interpolation = src.units, src.interpolation
    rec0, rec1, st = _record_extent(src, c.timezone)
    run.times.add(st)
    g.record_first, g.record_last = _iso(rec0), _iso(rec1)
    if rec1 is not None and rec1 > now_s + 600:
        run.warnings.append(f"{g.name}: last record {_iso(rec1)} is {(rec1 - now_s) / 60:.0f} min in the future "
                            f"(read as {c.timezone}): is the server on another time zone?")
    if src and src.units and src.units.lower() not in ("mm", "millimetres", "millimeters"):
        run.warnings.append(f"{g.name}: units {src.units!r}, expected mm")
    path = series_path(gdir, g)
    cached, meta = load_series(path)
    todo = chunks(plan(cached, first, last, refetch_from), gc.chunk_days * 24)
    if not todo:
        return False
    margin = 7200 if c.timezone == "Pacific/Auckland" else 3600
    total = c.method != "raw"
    span0, span1 = (todo[0][0] - 1) * 3600, todo[-1][1] * 3600
    qual = None
    if c.quality:
        try:
            # to now: the code that covers the newest hours may be stamped after them (a code ends its period)
            q = cl.quality_series(g.name, g.measurement, span0 - margin, max(span1, int(now_s)) + margin)
            if len(q):
                qs, st = to_utc(q.times, c.timezone)
                run.times.add(st)
                qual = (qs, np.where(np.isfinite(q.values), q.values, -1).astype(np.int32))
        except (FetchError, NotFound, HilltopError) as e:
            run.warnings.append(f"{g.name}: no quality series ({e})")
    block, fetches = cached, list(meta.get("fetches", []))
    done = 0
    for a, b in todo:
        try:
            s: Series = cl.get_data(g.name, g.measurement, (a - 1) * 3600 - margin, b * 3600 + margin, total=total,
                                    quality=c.quality)
        except (FetchError, NotFound, HilltopError) as e:
            run.errors.append(f"{g.name}: GetData {_hour(a)} .. {_hour(b)}: {e}")
            continue
        t_s, st = to_utc(s.times, c.timezone)
        run.times.add(st)
        order = np.argsort(t_s, kind="stable")
        if np.any(order != np.arange(len(order))):
            run.warnings.append(f"{g.name}: records out of time order after conversion to UTC")
        hb = hours_from_records(t_s[order], s.values[order], a, b, gap_before=s.gap_before[order],
                                gap_at_end=s.gap_at_end, rec_start=rec0, rec_end=rec1, rec_qc=s.quality[order],
                                qual=qual, method=c.method, max_mm=gc.max_mm_per_hour)
        block = merge(block, hb)
        fetches.append({"at": _iso(now_s), "first": _hour(a), "last": _hour(b), "records": len(s)})
        done += 1
    if not done:
        return False
    keep_from = last - gc.keep_days * 24 + 1
    if block.first < keep_from:
        block = block.slice(keep_from, block.last)
    save_series(path, block, {"id": g.id, "council": g.council, "site": g.name, "measurement": g.measurement,
                              "timezone": c.timezone, "method": c.method, "units": g.units,
                              "record_first": g.record_first, "record_last": g.record_last,
                              "fetches": fetches[-24:]})
    return True


def run_council(cfg: RainConfig, c: CouncilConfig, first: int, last: int, refetch_from: int, now_s: float,
                sites_only: bool = False, http: Http | None = None, log=print) -> CouncilRun:
    gc = cfg.gauges
    t0 = time.monotonic()
    http = http or Http(timeout=gc.timeout_s, retries=gc.retries, log=log)
    pacer = Pacer(gc.request_gap_s)
    run = CouncilRun(c.id)
    try:
        cl, m, tried = find_file(c, http, pacer, log)
    except (FetchError, HilltopError) as e:
        run.status = f"failed: {e}"
        run.http, run.seconds = http.stats.to_dict(), round(time.monotonic() - t0, 1)
        log(f"{c.id}: no server answered with a rainfall measurement: {e}")
        return run
    run.base_url, run.hts, run.measurement = cl.base_url, cl.hts, m
    for t in tried:
        run.warnings.append(f"tried {t}")
    try:
        sites = cl.sites(m)
    except (FetchError, NotFound, HilltopError) as e:
        run.status = f"failed: SiteList: {e}"
        run.http, run.seconds = http.stats.to_dict(), round(time.monotonic() - t0, 1)
        log(f"{c.id}: SiteList failed: {e}")
        return run
    names = {}
    for s in sites:
        names.setdefault(s.name, s)  # a site listed twice keeps its first entry
    run.gauges = [Gauge(f"{c.id}:{s.name}", c.id, s.name, m, s.lon, s.lat) for s in names.values()]
    unplaced = sum(1 for s in names.values() if not s.located)
    log(f"{c.id}: {cl.url('SiteList', Measurement=m)} -> {len(run.gauges)} sites with {m!r}"
        + (f" ({unplaced} without a location)" if unplaced else ""))
    if sites_only:
        run.status = "sites only"
    else:
        gdir = cfg.gauges_dir
        for i, g in enumerate(run.gauges):
            if fetch_gauge(cl, c, gc, g, gdir, first, last, refetch_from, run, now_s, log):
                run.fetched += 1
            if (i + 1) % 25 == 0:
                log(f"{c.id}: {i + 1}/{len(run.gauges)} sites, {http.stats.requests} requests, "
                    f"{http.stats.bytes / 1e6:.1f} MB, {len(run.errors)} errors")
        run.status = "ok" if not run.errors else "partial"
    run.http, run.seconds = http.stats.to_dict(), round(time.monotonic() - t0, 1)
    log(f"{c.id}: {run.status}; {run.fetched}/{len(run.gauges)} series updated; {http.stats.requests} requests, "
        f"{http.stats.bytes / 1e6:.1f} MB, {http.stats.retries} retries, {run.seconds} s"
        + (f"; {len(run.errors)} errors (first: {run.errors[0]})" if run.errors else ""))
    return run


def _selected(cfg: RainConfig, councils) -> list[CouncilConfig]:
    if councils:
        return [cfg.gauges.council(c) for c in councils]
    return [c for c in cfg.gauges.councils if c.enabled]


def run_gauges(cfg: RainConfig, councils: list[str] | None = None, days: int | None = None, sites_only: bool = False,
               now: datetime | None = None, refetch_hours: int | None = None, http: Http | None = None,
               workers: int | None = None, log=print) -> dict:
    """Fetch and cache hourly gauge totals, then write ``gauges_hourly.npz`` + ``.json`` (see the module doc).
    Returns the report; ``report["answered"]`` is False when no council's server answered."""
    gc = cfg.gauges
    sel = _selected(cfg, councils)
    if not sel:
        raise SystemExit("no enabled [gauges.councils.*] in the rainfall config")
    now_h = hour_index(floor_hour(now or datetime.now(UTC)))
    now_s = (now or datetime.now(UTC)).timestamp()
    days = days or gc.days
    if days > gc.keep_days:
        raise SystemExit(f"--days {days} exceeds gauges.keep_days {gc.keep_days}")
    first = now_h - days * 24 + 1
    refetch_from = now_h - (gc.refetch_hours if refetch_hours is None else refetch_hours) + 1
    gdir = cfg.gauges_dir
    grid = cfg.grids["ecmwf"]
    log(f"gauges: {', '.join(c.id for c in sel)}; hours {_hour(first)} .. {_hour(now_h)} ({days} days), "
        f"refetch from {_hour(refetch_from)}; cache {gdir}")

    def one(c: CouncilConfig) -> CouncilRun:
        def clog(m):
            log(m if m.startswith(c.id) else f"{c.id}: {m}")
        return run_council(cfg, c, first, now_h, refetch_from, now_s, sites_only, http, clog)

    if (workers or len(sel)) > 1 and http is None:
        with ThreadPoolExecutor(max_workers=workers or len(sel)) as ex:
            runs = list(ex.map(one, sel))
    else:
        runs = [one(c) for c in sel]

    # sites.json: this run's gauges replace their council's; councils not reached keep their last list
    sites = load_sites(gdir)
    old = {g["id"]: Gauge.from_dict(g) for g in sites.get("gauges", [])}
    by_council: dict[str, list[Gauge]] = {}
    for g in old.values():
        by_council.setdefault(g.council, []).append(g)
    fresh = [g for r in runs for g in r.gauges]
    methods = place(fresh, grid, cfg, log)
    if fresh:
        log(f"placed {len(fresh)} gauges: regions by {methods}")
    for r in runs:
        if r.gauges:
            for g in r.gauges:  # keep the record times of a gauge this run did not reach
                o = old.get(g.id)
                if o is not None and g.record_last is None:
                    g.record_first, g.record_last, g.units = o.record_first, o.record_last, o.units
            by_council[r.council] = r.gauges
            sites["councils"][r.council] = {"base_url": r.base_url, "hts": r.hts, "measurement": r.measurement,
                                            "found": _iso(now_s), "sites": len(r.gauges)}
    sites.update(format=SITES_FORMAT, updated=_iso(now_s),
                 gauges=[g.to_dict() for cid in sorted(by_council) for g in by_council[cid]])
    _write_json(gdir / "sites.json", sites)

    gauges = [g for c in sel for g in by_council.get(c.id, [])]
    keep_from = now_h - gc.keep_days * 24 + 1
    gh = build_hourly(gdir, gauges, now_h, keep_from)
    rep = report(cfg, sel, runs, gauges, gh, now_h, now_s, sites_only)
    gh.meta = {k: rep[k] for k in ("created", "now", "hour_convention", "grid", "states", "flags", "region_ids")}
    gh.meta["councils"] = {cid: {k: v[k] for k in ("name", "base_url", "hts", "measurement", "timezone", "method",
                                                   "attribution", "licence")} for cid, v in rep["councils"].items()}
    if not sites_only:
        path = gh.save(gdir / "gauges_hourly.npz")
        _write_json(path.with_suffix(".json"), rep)
        s = rep["summary"]
        log(f"gauges -> {path} ({path.stat().st_size / 1e6:.2f} MB): {s['gauges']} gauges x {s['hours']} hours "
            f"({rep['hours']['first']} .. {rep['hours']['last']}); usable {s['usable_frac']:.1%}, suspect "
            f"{s['suspect_frac']:.1%}, missing {s['missing_frac']:.1%}; {s['gauges_90pct_30d']} gauges with >= 90 % "
            f"of the last 30 days")
        for r, v in rep["regions"].items():
            log(f"  {r}: {v['gauges']} gauges, {v['gauges_90pct_30d']} with >= 90 % of 30 days")
    return rep


def report(cfg: RainConfig, sel: list[CouncilConfig], runs: list[CouncilRun], gauges: list[Gauge],
           gh: GaugeHours, now_h: int, now_s: float, sites_only: bool) -> dict:
    grid = cfg.grids["ecmwf"]
    w30, w24 = gh.window(now_h, 720), gh.window(now_h, 24)
    cols30 = gh.cols(now_h, 720)
    site_rows = []
    for i, g in enumerate(gauges):
        hb = HourBlock(int(gh.hour[0]) if len(gh.hour) else now_h, gh.mm[i], gh.qc[i], gh.state[i], gh.flags[i])
        ok = np.flatnonzero(gh.ok[i])
        lon_c = lat_c = None
        if g.cell >= 0:
            lon_c, lat_c = round(float(grid.lons[g.cell_col]), 4), round(float(grid.lats[g.cell_row]), 4)
        site_rows.append(dict(g.to_dict(), cell_lon=lon_c, cell_lat=lat_c, series=str(series_path(Path(""), g)),
                              hours=hb.counts(), hours_30d={"ok": int(w30["n_ok"][i]),
                                                            "suspect": int(w30["n_suspect"][i]),
                                                            "missing": int(w30["n_missing"][i])},
                              coverage_30d=round(float(w30["n_ok"][i]) / 720, 3),
                              mm_24h=round(float(w24["mm"][i]), 1), mm_30d=round(float(w30["mm"][i]), 1),
                              last_value=_hour(int(gh.hour[ok[-1]])) if len(ok) else None))
    regions = {}
    good = w30["n_ok"] >= 0.9 * 720
    for k, r in enumerate(gh.regions):
        rows = np.flatnonzero(gh.gauges["region_of"] == k)
        regions[r] = {"id": slug(r), "gauges": len(rows), "gauges_90pct_30d": int(good[rows].sum()),
                      "councils": sorted({gauges[i].council for i in rows})}
    st = gh.state
    tot = max(1, st.size)
    councils = {}
    for c in sel:
        r = next(x for x in runs if x.council == c.id)
        councils[c.id] = {"name": c.name, "status": r.status, "base_url": r.base_url, "hts": r.hts,
                          "measurement": r.measurement, "timezone": c.timezone, "method": c.method,
                          "attribution": c.attribution, "licence": c.licence, "source": c.source,
                          "gauges": sum(1 for g in gauges if g.council == c.id), "series_updated": r.fetched,
                          "errors": r.errors[:50], "n_errors": len(r.errors), "warnings": r.warnings[:50],
                          "times": r.times.to_dict(), "http": r.http, "seconds": r.seconds}
    return {
        "format": HOURLY_FORMAT, "created": _iso(time.time()), "now": _hour(now_h), "sites_only": sites_only,
        "hour_convention": HOUR_CONVENTION,
        "hours": {"first": _hour(int(gh.hour[0])) if len(gh.hour) else None,
                  "last": _hour(int(gh.hour[-1])) if len(gh.hour) else None, "n": int(len(gh.hour)),
                  "last_30d_from": _hour(int(gh.hour[cols30[0]])) if len(cols30) else None},
        "grid": dict(grid.to_dict(), cell="row * nx + col; row 0 = lat0 (north), col 0 = lon0"),
        "states": {s: STATE_DOC[s] for s in STATES}, "flags": {str(b): {"name": n, "doc": FLAG_DOC[n]}
                                                               for b, n in FLAGS.items()},
        "region_ids": {r: slug(r) for r in gh.regions},
        "summary": {"gauges": gh.n, "hours": int(len(gh.hour)),
                    "usable_frac": round(float((st <= RAW).sum()) / tot, 4),
                    "suspect_frac": round(float((st == SUSPECT).sum()) / tot, 4),
                    "missing_frac": round(float((st == MISSING).sum()) / tot, 4),
                    "gauges_90pct_30d": int(good.sum()),
                    "councils_answered": sorted(r.council for r in runs if not r.status.startswith("failed"))},
        "answered": any(not r.status.startswith("failed") for r in runs),
        "councils": councils, "regions": regions, "sites": site_rows,
    }


# --- discovery ------------------------------------------------------------------------------------
def discover(cfg: RainConfig, councils: list[str] | None = None, http: Http | None = None, now: datetime | None = None,
             log=print) -> dict:
    """Probe every server root x candidate file of the councils; write and return ``discover.json``."""
    gc = cfg.gauges
    now_s = (now or datetime.now(UTC)).timestamp()
    out = {"created": _iso(time.time()), "councils": {}}
    for c in ([cfg.gauges.council(x) for x in councils] if councils else list(cfg.gauges.councils)):
        h = http or Http(timeout=gc.timeout_s, retries=min(gc.retries, 2), log=log)
        pacer = Pacer(gc.request_gap_s)
        probes = []
        for base in c.base_urls:
            for hts in c.hts:
                cl = _client(c, base, hts, h, pacer)
                p: dict = {"base_url": base, "hts": hts}
                for what, fn in (("status", cl.status), ("collections", cl.collections)):
                    try:
                        v = fn()
                        p[what] = v if what == "status" else [x for x in v if "rain" in x.lower()] or v[:20]
                    except (FetchError, NotFound, HilltopError) as e:
                        p[f"{what}_error"] = str(e)
                try:
                    names = cl.measurement_names()
                except (FetchError, NotFound, HilltopError) as e:
                    p["measurements_error"] = str(e)
                    probes.append(p)
                    log(f"{c.id}: {cl.url('MeasurementList')}: {e}")
                    continue
                m = choose_measurement(names, c.measurements)
                p.update(measurements=len(names), rain_like=rain_like(names), chosen=m)
                log(f"{c.id}: {cl.url('MeasurementList')}: {len(names)} measurements; rain-like {rain_like(names)}; "
                    f"chosen {m!r}")
                if m:
                    try:
                        sites = cl.sites(m)
                        p["sites"] = len(sites)
                        p["sites_located"] = sum(s.located for s in sites)
                        p["site_sample"] = [s.name for s in sites[:10]]
                        if sites:
                            p["sample"] = _sample(cl, c, sites[0].name, m, now_s)
                        log(f"{c.id}: {len(sites)} sites ({p['sites_located']} located); sample {p.get('sample')}")
                    except (FetchError, NotFound, HilltopError) as e:
                        p["sites_error"] = str(e)
                probes.append(p)
                if m:
                    break
            if probes and probes[-1].get("chosen"):
                break
        out["councils"][c.id] = {"name": c.name, "timezone": c.timezone, "probes": probes, "http": h.stats.to_dict()}
    _write_json(cfg.gauges_dir / "discover.json", out)
    log(f"discover -> {cfg.gauges_dir / 'discover.json'}")
    return out


def _sample(cl: HilltopClient, c: CouncilConfig, site: str, m: str, now_s: float) -> dict:
    """One site's data sources and its last 6 hours of records, with how far its newest record is from now."""
    srcs = cl.site_measurements(site)
    src = find_source(srcs, m)
    d = {"site": site, "sources": [{"datasource": s.datasource, "measurement": s.measurement,
                                    "request_as": s.request_as, "units": s.units, "tstype": s.tstype,
                                    "data_type": s.data_type, "interpolation": s.interpolation,
                                    "from": s.first.isoformat() if s.first else None,
                                    "to": s.last.isoformat() if s.last else None} for s in srcs[:12]]}
    end = src.last if src and src.last else None
    if end is not None:
        for tz in ("NZST", "Pacific/Auckland", "UTC"):
            s, _ = to_utc([end], tz)
            d[f"last_record_minus_now_min_as_{tz}"] = round((float(s[0]) - now_s) / 60, 1)
    name = src.request_name if src else m
    try:
        ser = cl.get_data(site, name, int(now_s) - 6 * 3600, int(now_s) + 3600, quality=True)
        d["records_6h"] = len(ser)
        d["records_tail"] = [[t.isoformat(), float(v), int(q)] for t, v, q in
                             zip(ser.times[-5:], ser.values[-5:], ser.quality[-5:], strict=True)]
        d["gaps"] = int(ser.gap_before.sum()) + int(ser.gap_at_end)
        d["data_type"], d["interpolation"], d["units"] = ser.data_type, ser.interpolation, ser.units
    except (FetchError, NotFound, HilltopError) as e:
        d["data_error"] = str(e)
    return d


# --- status ---------------------------------------------------------------------------------------
def status_line(cfg: RainConfig) -> str:
    """One line on the gauge cache for ``rain status``."""
    p = cfg.gauges_dir / "gauges_hourly.json"
    enabled = [c.id for c in cfg.gauges.councils if c.enabled]
    if not p.exists():
        return (f"gauges: none yet ({cfg.gauges_dir}; river-finder rain gauges; councils {enabled})")
    r = json.loads(p.read_text())
    s, h = r["summary"], r["hours"]
    per = ", ".join(f"{cid} {v['gauges']} ({v['status'].split(':')[0]})" for cid, v in r["councils"].items())
    return (f"gauges {r['created']}: {s['gauges']} gauges ({per}); hours {h['first']} .. {h['last']}; usable "
            f"{s['usable_frac']:.1%}, missing {s['missing_frac']:.1%}; {s['gauges_90pct_30d']} with >= 90 % of 30 days")
