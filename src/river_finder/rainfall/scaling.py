"""Gauge scale factors per source family and region (rf-t26.5), each gauge station counted once (rf-t26.13).

``river-finder rain scale [--days 30] [--now YYYYMMDDHH] [--force] [--no-fetch]`` compares a source's
grid with the rain gauges over a rolling window and keeps one factor per scaling region:

1. **gauges** (:class:`.gauges.GaugeHours`, ``<rain>/gauges/gauges_hourly.npz``): the window is the
   ``[scaling] days`` x 24 whole hours ending at "now" (or at the newest gauge hour, when the gauge
   cache is older); a gauge hour counts when its state is good or raw (``state <= 1``);
2. **copies of one station** (:func:`find_copies`): gauges within ``dedupe_km`` whose common usable
   hours with rain agree within ``dedupe_equal_mm`` for at least ``dedupe_equal_share`` of them are
   copies of one record (WCRC and TDC both publish 7 gauges; WCRC's Arthurs Pass EWS is ECan's ESNZ
   station). One copy is kept: the one with more usable hours in the window, then the one whose
   council's home region (``[gauges.councils.*] region``) it lies in, then the one with more
   quality-coded (good) hours, then the lower id; every group records what was kept and why.
   Distinct gauges within ``colocated_m`` are listed with the hourly lag that matches them best
   (ECan's own Arthur's Pass gauge runs an hour ahead of the ESNZ station 14 m away: r 0.99 at -1 h,
   0.92 at 0 h); a lag does not change a 30-day total, so they both count;
3. **the grid side** (:class:`CellStore`, ``<rain>/scaling/<family>_cells.npz``): the source's hourly
   mm at every gauge cell, kept for ``keep_days`` and filled incrementally. ECMWF
   (:func:`fill_ecmwf`) follows the observed proxy's rule (:func:`.ecmwf.obs_plan`): each hour from the
   newest run that has published it within ``[ecmwf] obs_leads``, so settled hours come from leads
   1-6, and an hour first taken at a longer lead (the newest hours) is replaced once a newer run has
   it. Readers are tried in ``[scaling.ecmwf] readers`` order for each run: Open-Meteo (the reader
   the live buffers use) keeps about 8 days of runs, so older hours of a first backfill come from
   ECMWF's 0.1° GRIB files on AWS (kept since 2026-10-20 06z); each value records its reader, run
   and lead. Cells are 0.1° and gauges are points: the factor is a regional ratio of sums, never a
   per-gauge correction;
4. **factor maths** (:func:`evaluate`, :func:`decide`), per region: the usable gauges (not a dropped
   copy, on the grid, with at least ``min_usable`` of the window's hours both usable at the gauge and
   valued at its cell) give ``ratio = sum(gauge mm) / sum(grid mm at their cells)`` over exactly those
   hours. Guard rails, in order: fewer than ``min_gauges`` usable gauges -> factor 1, version 0;
   mean gauge or grid total per gauge below ``min_gauge_mm`` / ``min_grid_mm`` -> the current factor
   is held (a dry month says nothing about the bias); the ratio is clamped to ``clamp``; one update
   moves the factor by at most ``max_step`` either way; a change below ``min_change`` keeps the
   current factor and version; a region gets a new version at most once per ``min_update_hours``
   (``--force`` overrides). If the grid store covers less than ``min_usable`` of the window, nothing
   changes. Checks reported with every region, not used: the median and quartiles of the per-gauge
   ratios and the ratio of sums without the 10 % highest and lowest per-gauge ratios;
5. **storage** (``<rain>/scaling/factors_<family>.json``): per region the current factor, version,
   when it was set, its status and the history record behind it; ``last_update`` holds the newest
   evaluation of every region (gauges used with their hours and sums, guard rails), the window, the
   input files and the copy groups. ``factors_<family>.history.jsonl`` is append-only: one line per
   new version (``seq``, region, factor, version, previous factor and version, window, every gauge
   used with its cell, hours and sums, the config), so each factor traces back to its gauges and
   hours. Versions only change with the factor; version 0 always means unscaled (factor 1): a region
   reset to unity gets version 0, the next factor ``max_version + 1``;
6. **applying** (:func:`factors_for`): ``rain ecmwf`` scales each product listed in ``[scaling.<family>]
   products`` (ECMWF: the observed proxy and the forecast, one model) with the current factors, when
   ``[scaling] apply`` is true, and re-scales the hours it holds, so each slot stores raw, scaled,
   factor and version per region; ``rain scale`` re-scales the saved buffers the same way. No factor
   file, an unlisted product or ``apply = false``: factor 1, version 0. ``rain publish`` publishes
   scaled values only with ``[scaling] publish = true`` (rf-t26.14: WCRC and TDC have not stated a
   licence); otherwise it publishes the raw sums.

``docs/rainfall-scaling.md`` has the method, the thresholds, the current factors and the hourly job.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from .buffer import HourlyBuffer, ScaleFactors
from .config import RainConfig, ScalingConfig
from .grid import GridSpec, floor_hour, hour_index, hour_time
from .sources import product

CELLS_FORMAT = "river-finder rain scaling cells v1"
FACTORS_FORMAT = "river-finder rain scale factors v1"
HISTORY_FORMAT = "river-finder rain scale factor record v1"


def _hour(h: int) -> str:
    return hour_time(int(h)).strftime("%Y-%m-%d %H:00Z")


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _write_json(path: Path, d: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False))
    os.replace(tmp, path)


# --- the grid side: hourly mm at the gauge cells -----------------------------------------------------
@dataclass
class CellStore:
    """One source family's hourly mm at a set of grid cells, hours ``first .. first + T - 1`` (hour ending).

    Per hour and cell: ``mm`` (NaN = no value), ``src`` (0 none, k = ``readers[k - 1]``), ``run_hour``
    and ``lead`` (forecast-type sources; -1 otherwise)."""

    family: str
    grid: GridSpec
    cells: np.ndarray       # int32 (C,): flat cell index (row * nx + col), sorted, unique
    first: int              # hour of row 0 (-1: empty)
    mm: np.ndarray          # float32 (T, C)
    src: np.ndarray         # uint8 (T, C)
    run_hour: np.ndarray    # int64 (T, C)
    lead: np.ndarray        # int16 (T, C)
    readers: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    @classmethod
    def empty(cls, family: str, grid: GridSpec) -> CellStore:
        z = np.zeros((0, 0))
        return cls(family, grid, np.zeros(0, np.int32), -1, z.astype(np.float32), z.astype(np.uint8),
                   z.astype(np.int64), z.astype(np.int16))

    @property
    def n_hours(self) -> int:
        return int(self.mm.shape[0])

    @property
    def last(self) -> int:
        return self.first + self.n_hours - 1 if self.n_hours else -1

    @property
    def hours(self) -> np.ndarray:
        return np.arange(self.first, self.first + self.n_hours, dtype=np.int64)

    def reader_code(self, name: str) -> int:
        if name not in self.readers:
            if len(self.readers) >= 255:
                raise ValueError("too many readers for one cell store")
            self.readers.append(name)
        return self.readers.index(name) + 1

    def col(self, cells) -> np.ndarray:
        cells = np.asarray(cells, dtype=np.int64)
        i = np.searchsorted(self.cells, cells)
        if np.any(i >= self.cells.size) or np.any(self.cells[np.minimum(i, self.cells.size - 1)] != cells):
            raise KeyError("cells not in the store")
        return i

    def ensure(self, cells, first: int, last: int) -> None:
        """Make room for ``cells`` and hours ``first .. last`` (what is held stays)."""
        cells = np.unique(np.asarray(cells, dtype=np.int64))
        if np.any((cells < 0) | (cells >= self.grid.size)):
            raise ValueError("cells outside the grid")
        new_cells = np.union1d(self.cells, cells).astype(np.int32)
        lo = first if self.n_hours == 0 else min(first, self.first)
        hi = last if self.n_hours == 0 else max(last, self.last)
        if new_cells.size == self.cells.size and lo == self.first and hi == self.last:
            return
        T, C = hi - lo + 1, new_cells.size
        mm = np.full((T, C), np.nan, np.float32)
        src = np.zeros((T, C), np.uint8)
        run = np.full((T, C), -1, np.int64)
        lead = np.full((T, C), -1, np.int16)
        if self.n_hours and self.cells.size:
            r0 = self.first - lo
            c = np.searchsorted(new_cells, self.cells)
            rows = slice(r0, r0 + self.n_hours)
            mm[rows, c], src[rows, c], run[rows, c], lead[rows, c] = self.mm, self.src, self.run_hour, self.lead
        self.cells, self.first, self.mm, self.src, self.run_hour, self.lead = new_cells, lo, mm, src, run, lead

    def trim(self, keep_from: int) -> int:
        """Drop hours before ``keep_from``; returns how many."""
        if not self.n_hours or keep_from <= self.first:
            return 0
        k = min(keep_from - self.first, self.n_hours)
        self.mm, self.src, self.run_hour, self.lead = (a[k:] for a in (self.mm, self.src, self.run_hour, self.lead))
        self.first = self.first + k if self.n_hours else -1
        return k

    def values(self, cells, first: int, last: int) -> np.ndarray:
        """mm (hours ``first .. last``, cells), NaN where the store has no value."""
        cells = np.asarray(cells, dtype=np.int64)
        T = last - first + 1
        out = np.full((T, cells.size), np.nan, np.float32)
        if not self.n_hours or not cells.size:
            return out
        a, b = max(first, self.first), min(last, self.last)
        if a > b:
            return out
        known = np.isin(cells, self.cells)
        c = np.searchsorted(self.cells, cells[known])
        out[a - first:b - first + 1, np.flatnonzero(known)] = self.mm[a - self.first:b - self.first + 1][:, c]
        return out

    def coverage(self, cells, first: int, last: int) -> float:
        """Share of the hours ``first .. last`` with a value at every one of ``cells``."""
        v = self.values(cells, first, last)
        return float(np.isfinite(v).all(axis=1).mean()) if v.size else 0.0

    def summary(self) -> dict:
        have = self.src > 0
        by = {name: int((self.src == k + 1).sum()) for k, name in enumerate(self.readers)}
        settled = int(((self.lead >= 1) & (self.lead <= 6) & have).sum())
        return {"cells": int(self.cells.size), "first": _hour(self.first) if self.n_hours else None,
                "last": _hour(self.last) if self.n_hours else None, "hours": self.n_hours,
                "values": int(have.sum()), "missing": int((~have).sum()), "by_reader": by,
                "lead_1_6": settled, "longer_lead": int(have.sum()) - settled}

    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = dict(self.meta, format=CELLS_FORMAT, family=self.family, grid=self.grid.to_dict(),
                    first=self.first, readers=list(self.readers), saved=_iso(datetime.now(UTC)))
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "wb") as fh:
            np.savez_compressed(fh, meta=np.array(json.dumps(meta)), cells=self.cells, mm=self.mm, src=self.src,
                                run_hour=self.run_hour, lead=self.lead)
        os.replace(tmp, path)
        return path

    @classmethod
    def load(cls, path: Path) -> CellStore:
        with np.load(path, allow_pickle=False) as z:
            meta = json.loads(str(z["meta"]))
            if meta.get("format") != CELLS_FORMAT:
                raise ValueError(f"{path}: not a {CELLS_FORMAT} file")
            return cls(meta["family"], GridSpec.from_dict(meta["grid"]), z["cells"], int(meta["first"]), z["mm"],
                       z["src"], z["run_hour"], z["lead"], list(meta["readers"]),
                       {k: v for k, v in meta.items() if k not in ("format", "family", "grid", "first", "readers")})


def open_store(cfg: RainConfig, family: str) -> CellStore:
    """The saved cell store of ``family``, or an empty one (also when its grid is not the config's)."""
    grid = cfg.grids[family]
    p = cfg.cells_path(family)
    if p.exists():
        s = CellStore.load(p)
        if s.grid.same_cells(grid) and s.family == family:
            return s
    return CellStore.empty(family, grid)


def ecmwf_pick(readers, h: int, leads: tuple[int, int]):
    """The observed proxy's choice for hour ``h``: the newest run with ``h`` among its published leads in
    ``leads``; for one run, the first reader that has it. Returns (reader index, run, lead) or None."""
    from .ecmwf import RUN_EVERY, floor_run

    lo, hi = leads
    run = floor_run(hour_time(h - lo))
    while h - hour_index(run) <= hi:
        lead = h - hour_index(run)
        for k, rd in enumerate(readers):
            if lead in rd.leads(run):
                return k, run, lead
        run -= timedelta(hours=RUN_EVERY)
    return None


def fill_ecmwf(store: CellStore, readers, cells, first: int, last: int, leads: tuple[int, int],
               log=print) -> dict:
    """Fill hours ``first .. last`` at ``cells`` from ECMWF ``readers`` (in order of preference).

    An hour is left alone when every cell has a value from the newest run that can have it (leads
    from ``leads[0]``); otherwise :func:`ecmwf_pick` names the run and reader, and cells with no value
    or one from an older run take the new value."""
    from .ecmwf import floor_run, run_label

    store.ensure(cells, first, last)
    ci = store.col(cells)
    cells = np.asarray(cells, dtype=np.int64)
    lo = leads[0]
    stats = {"hours": last - first + 1, "settled": 0, "written": 0, "replaced": 0, "no_run": [], "by_reader": {},
             "runs": set()}
    t0 = time.monotonic()
    for h in range(first, last + 1):
        t = h - store.first
        src, run = store.src[t, ci], store.run_hour[t, ci]
        newest = hour_index(floor_run(hour_time(h - lo)))
        if np.all(src > 0) and np.all(run >= newest):
            stats["settled"] += 1
            continue
        pick = ecmwf_pick(readers, h, leads)
        if pick is None:
            stats["no_run"].append(h)
            continue
        k, r, lead = pick
        rh = hour_index(r)
        upd = (src == 0) | (run < rh)
        if not upd.any():
            continue
        field_mm, _ = readers[k].hourly(r, lead)
        vals = np.asarray(field_mm, dtype=np.float32).ravel()[cells]
        c = ci[upd]
        stats["replaced" if np.any(src[upd] > 0) else "written"] += 1
        store.mm[t, c] = vals[upd]
        store.src[t, c] = store.reader_code(readers[k].name)
        store.run_hour[t, c], store.lead[t, c] = rh, lead
        name = readers[k].name
        stats["by_reader"][name] = stats["by_reader"].get(name, 0) + 1
        stats["runs"].add(run_label(r))
    stats["runs"] = sorted(stats["runs"])
    stats["seconds"] = round(time.monotonic() - t0, 1)
    if stats["no_run"]:
        log(f"  {len(stats['no_run'])} hours no reader has within leads {leads[0]}-{leads[1]} h: "
            f"{_hour(stats['no_run'][0])} .. {_hour(stats['no_run'][-1])}")
    stats["no_run"] = [_hour(h) for h in stats["no_run"]]
    return stats


# --- copies of one station (rf-t26.13) ---------------------------------------------------------------
def _home_regions(cfg: RainConfig) -> dict[str, str]:
    return {c.id: c.region for c in cfg.gauges.councils if c.region}


def _corr(a: np.ndarray, b: np.ndarray) -> float | None:
    a, b = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    if a.size < 3 or np.ptp(a) == 0 or np.ptp(b) == 0:  # a constant series has no correlation
        return None
    return round(float(np.corrcoef(a, b)[0, 1]), 3)


def _lag_corr(a: np.ndarray, b: np.ndarray, oka: np.ndarray, okb: np.ndarray, k: int) -> tuple[float | None, int]:
    """r of ``a[h]`` against ``b[h + k]`` over hours where both are usable; (r, hours)."""
    n = a.size
    if k >= 0:
        ia, ib = slice(0, n - k), slice(k, n)
    else:
        ia, ib = slice(-k, n), slice(0, n + k)
    m = oka[ia] & okb[ib]
    return _corr(a[ia][m].astype(np.float64), b[ib][m].astype(np.float64)), int(m.sum())


def find_copies(g, cols: np.ndarray, sc: ScalingConfig, home: dict[str, str] | None = None,
                detect_cols: np.ndarray | None = None) -> dict:
    """Gauges of ``g`` (:class:`.gauges.GaugeHours`) that are copies of one station.

    Series are compared over ``detect_cols`` (default: every hour held, so that a dry window does not
    hide a copy); the copy kept is chosen on the window columns ``cols``. Returns ``keep`` (bool per
    gauge), ``groups`` (one per station with several copies: kept, dropped, why, the pairwise checks)
    and ``colocated`` (distinct gauges within ``colocated_m``, with the hourly lag that matches them
    best)."""
    from .gauges import GOOD

    G = g.n
    home = home or {}
    keep = np.ones(G, dtype=bool)
    out = {"keep": keep, "groups": [], "colocated": [], "pairs_checked": 0,
           "rule": {"distance_km": sc.dedupe_km, "min_wet_hours": sc.dedupe_min_wet_hours,
                    "equal_share": sc.dedupe_equal_share, "equal_mm": sc.dedupe_equal_mm,
                    "colocated_m": sc.colocated_m}}
    if G < 2:
        return out
    x, y = np.asarray(g.x, dtype=np.float64), np.asarray(g.y, dtype=np.float64)
    placed = np.isfinite(x) & np.isfinite(y)
    d = np.hypot(x[:, None] - x[None, :], y[:, None] - y[None, :])
    near = placed[:, None] & placed[None, :] & (d <= max(sc.dedupe_km * 1000.0, sc.colocated_m))
    near &= np.triu(np.ones((G, G), dtype=bool), 1)
    dc = np.arange(g.hour.size) if detect_cols is None else np.asarray(detect_cols)
    ok = g.ok[:, dc]
    mm = np.where(ok, np.nan_to_num(g.mm[:, dc]), 0.0).astype(np.float32)
    usable = g.ok[:, cols].sum(axis=1)
    good = (g.state[:, cols] == GOOD).sum(axis=1)
    out["compared"] = {"first_hour": _hour(g.hour[dc[0]]) if dc.size else None,
                       "last_hour": _hour(g.hour[dc[-1]]) if dc.size else None, "hours": int(dc.size)}
    parent = list(range(G))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    pair_info: dict[tuple[int, int], dict] = {}
    for i, j in np.argwhere(near):
        i, j = int(i), int(j)
        out["pairs_checked"] += 1
        both = ok[i] & ok[j]
        a, b = mm[i, both], mm[j, both]
        wet = (a > 0) | (b > 0)
        n_wet = int(wet.sum())
        share = float((np.abs(a - b)[wet] <= sc.dedupe_equal_mm + 1e-6).mean()) if n_wet else None
        info = {"a": str(g.id[i]), "b": str(g.id[j]), "distance_m": round(float(d[i, j]), 1),
                "common_hours": int(both.sum()), "wet_hours": n_wet,
                "equal_share": round(share, 3) if share is not None else None,
                "sum_a_mm": round(float(a.sum()), 1), "sum_b_mm": round(float(b.sum()), 1), "r": _corr(a, b)}
        copy = (d[i, j] <= sc.dedupe_km * 1000.0 and n_wet >= sc.dedupe_min_wet_hours
                and share is not None and share >= sc.dedupe_equal_share)
        if copy:
            pair_info[(i, j)] = info
            ri, rj = root(i), root(j)
            if ri != rj:
                parent[max(ri, rj)] = min(ri, rj)
        elif d[i, j] <= sc.colocated_m:
            lags = {}
            for k in range(-3, 4):
                lags[k] = _lag_corr(g.mm[i, dc], g.mm[j, dc], ok[i], ok[j], k)[0]
            valid = {k: v for k, v in lags.items() if v is not None}
            best = max(valid, key=valid.get) if valid else None
            info.update(r_by_lag={str(k): v for k, v in lags.items()}, best_lag_h=best,
                        why_not_copy=("too few wet hours to compare" if n_wet < sc.dedupe_min_wet_hours
                                      else f"{share:.0%} of wet hours agree (< {sc.dedupe_equal_share:.0%})"))
            notes = []
            if best is not None and best != 0 and valid[best] >= 0.9:
                # r(k) pairs a[h] with b[h + k]: k < 0 means b records the same rain |k| h earlier
                ahead, behind = (info["b"], info["a"]) if best < 0 else (info["a"], info["b"])
                notes.append(f"{ahead} runs {abs(best)} h ahead of {behind} (r {valid[best]:.2f} shifted "
                             f"{abs(best)} h, {valid.get(0, float('nan')):.2f} as stamped); a 30-day total is "
                             "not affected")
            sa, sb = info["sum_a_mm"], info["sum_b_mm"]
            if min(sa, sb) > 0 and max(sa, sb) / min(sa, sb) > 1.5:
                notes.append(f"totals differ {max(sa, sb) / min(sa, sb):.1f}-fold {d[i, j]:.0f} m apart: one "
                             "location may be wrong")
            if notes:
                info["note"] = "; ".join(notes)
            info["rows"] = (i, j)
            out["colocated"].append(info)
    groups: dict[int, list[int]] = {}
    for i in range(G):
        groups.setdefault(root(i), []).append(i)
    def region_name(i) -> str | None:
        return g.regions[g.region_of[i]] if g.region_of[i] >= 0 else None

    def is_home(i) -> bool:
        return home.get(str(g.council[i])) == region_name(i)

    def key(i):  # kept first: more usable hours, home council, more quality-coded hours, lower id
        return (-int(usable[i]), not is_home(i), -int(good[i]), str(g.id[i]))

    def why(k, i) -> str:
        if usable[k] != usable[i]:
            return f"more usable hours in the window ({usable[k]} vs {usable[i]})"
        if is_home(k) != is_home(i):
            return f"same usable hours ({usable[k]}); {g.council[k]} is the home council of {region_name(k)}"
        if good[k] != good[i]:
            return f"same usable hours ({usable[k]}); more quality-coded (good) hours ({good[k]} vs {good[i]})"
        return f"same usable ({usable[k]}) and quality-coded hours; lower id"

    for members in groups.values():
        if len(members) < 2:
            continue
        members = sorted(members, key=key)
        k = members[0]
        for i in members[1:]:
            keep[i] = False
        pairs = [v for (i, j), v in pair_info.items() if i in members and j in members]
        out["groups"].append({
            "kept": str(g.id[k]), "dropped": [str(g.id[i]) for i in members[1:]],
            "why": {str(g.id[i]): why(k, i) for i in members[1:]},
            "region": region_name(k), "cell": int(g.cell[k]),
            "usable_hours": {str(g.id[i]): int(usable[i]) for i in members}, "pairs": pairs})
    out["groups"].sort(key=lambda r: r["kept"])
    # co-located distinct gauges, between the copies kept
    out["colocated"] = [{k: v for k, v in c.items() if k != "rows"} for c in out["colocated"]
                        if keep[c["rows"][0]] and keep[c["rows"][1]]]
    return out


# --- factor maths ---------------------------------------------------------------------------------------
def _r4(v) -> float | None:
    return None if v is None or not np.isfinite(v) else round(float(v), 4)


def evaluate_region(rows: np.ndarray, g, cols: np.ndarray, grid_vals: np.ndarray, keep: np.ndarray,
                    sc: ScalingConfig) -> dict:
    """The comparison for one region: gauges ``rows`` of ``g``; ``grid_vals`` (hours, gauges) is the grid's
    mm at each gauge's cell over the window hours (NaN = no value)."""
    H = cols.size
    used, excluded = [], {"copy": [], "no_cell": [], "few_hours": []}
    gsum = esum = 0.0
    hours = 0
    by_council: dict[str, dict] = {}
    for r in rows:
        gid = str(g.id[r])
        if not keep[r]:
            excluded["copy"].append(gid)
            continue
        if g.cell[r] < 0:
            excluded["no_cell"].append(gid)
            continue
        e = grid_vals[:, r]
        ok = g.ok[r, cols] & np.isfinite(e)
        n = int(ok.sum())
        share = n / H if H else 0.0
        if share < sc.min_usable:
            excluded["few_hours"].append({"id": gid, "usable": round(share, 3)})
            continue
        gm = float(np.nan_to_num(g.mm[r, cols])[ok].sum())
        em = float(e[ok].sum())
        used.append({"id": gid, "council": str(g.council[r]), "cell": int(g.cell[r]), "hours": n,
                     "usable": round(share, 3), "gauge_mm": round(gm, 2), "grid_mm": round(em, 2),
                     "ratio": _r4(gm / em) if em > 0 else None})
        gsum += gm
        esum += em
        hours += n
        c = by_council.setdefault(str(g.council[r]), {"gauges": 0, "gauge_mm": 0.0, "grid_mm": 0.0})
        c["gauges"] += 1
        c["gauge_mm"] += gm
        c["grid_mm"] += em
    for c in by_council.values():
        c["ratio"] = _r4(c["gauge_mm"] / c["grid_mm"]) if c["grid_mm"] > 0 else None
        c["gauge_mm"], c["grid_mm"] = round(c["gauge_mm"], 1), round(c["grid_mm"], 1)
    n = len(used)
    ev = {"gauges_listed": int(len(rows)), "gauges": n, "excluded": excluded, "hours": hours,
          "gauge_mm": round(gsum, 1), "grid_mm": round(esum, 1),
          "mean_gauge_mm": round(gsum / n, 1) if n else None, "mean_grid_mm": round(esum / n, 1) if n else None,
          "ratio": _r4(gsum / esum) if esum > 0 else None, "by_council": by_council, "used": used}
    ratios = np.array([u["ratio"] for u in used if u["ratio"] is not None and u["grid_mm"] >= 0.5 * sc.min_grid_mm])
    check = {"gauges": int(ratios.size)}
    if ratios.size:
        q = np.quantile(ratios, [0.25, 0.5, 0.75])
        check.update(median_ratio=_r4(q[1]), q25=_r4(q[0]), q75=_r4(q[2]))
    if n >= 10:  # ratio of sums without the 10 % highest and lowest per-gauge ratios
        rr = np.array([u["gauge_mm"] / u["grid_mm"] if u["grid_mm"] > 0 else np.inf for u in used])
        order = np.argsort(rr)
        cut = max(1, int(round(0.1 * n)))
        mid = order[cut:n - cut]
        gs, es = sum(used[i]["gauge_mm"] for i in mid), sum(used[i]["grid_mm"] for i in mid)
        check["trimmed_ratio"] = _r4(gs / es) if es > 0 else None
    ev["check"] = check
    return ev


def decide(prev: dict, ev: dict, sc: ScalingConfig, now: datetime, force: bool = False,
           grid_ok: bool = True) -> dict:
    """The region's next state from its current one (``factor``, ``version``, ``set``, ``max_version``)
    and its evaluation: ``status`` (scaled | updated | held | unity | reset), ``factor``, ``version``,
    ``changed``, ``reason`` and the guard rails that acted."""
    f0, v0 = float(prev.get("factor", 1.0)), int(prev.get("version", 0))
    vmax = int(prev.get("max_version", v0))
    lo, hi = sc.clamp
    out = {"factor": f0, "version": v0, "changed": False, "guards": [], "target": None, "stepped": None}

    def hold(status, reason, guard):
        out.update(status=status, reason=reason)
        out["guards"].append(guard)
        return out

    if not grid_ok:
        return hold("held" if v0 else "unity", "the grid store does not cover the window: nothing changes",
                    "grid_coverage")
    if ev["gauges"] < sc.min_gauges:
        reason = (f"{ev['gauges']} usable gauge{'s' if ev['gauges'] != 1 else ''} (< {sc.min_gauges}): "
                  "factor 1, version 0")
        out["guards"].append("min_gauges")
        if (f0, v0) == (1.0, 0):
            out.update(status="unity", reason=reason)
            return out
        out.update(status="reset", reason=reason, factor=1.0, version=0, changed=True)
        return out
    if ev["mean_gauge_mm"] < sc.min_gauge_mm or ev["mean_grid_mm"] < sc.min_grid_mm:
        return hold("held" if v0 else "unity",
                    f"too dry for a ratio: mean {ev['mean_gauge_mm']:.1f} mm per gauge, {ev['mean_grid_mm']:.1f} mm "
                    f"in the grid (< {sc.min_gauge_mm:g} / {sc.min_grid_mm:g} mm): the factor is held", "min_rain")
    ratio = ev["ratio"]
    target = min(max(ratio, lo), hi)
    if target != ratio:
        out["guards"].append("clamp")
    stepped = min(max(target, f0 / sc.max_step), f0 * sc.max_step)
    if abs(stepped - target) > 1e-9:
        out["guards"].append("max_step")
    stepped = round(stepped, 4)
    out.update(target=round(target, 4), stepped=stepped)
    if abs(stepped / f0 - 1.0) < sc.min_change:
        return hold("held" if v0 else "unity",
                    f"ratio {ratio:.3f}: a change to {stepped:.3f} is below {sc.min_change:.0%} of {f0:.3f}",
                    "min_change")
    since = _parse_iso(prev.get("set"))
    if since is not None and not force and (now - since) < timedelta(hours=sc.min_update_hours):
        age = (now - since).total_seconds() / 3600
        return hold("held" if v0 else "unity",
                    f"ratio {ratio:.3f} -> {stepped:.3f}, but version {v0} was set {age:.1f} h ago "
                    f"(< {sc.min_update_hours} h)", "min_update_hours")
    steps = [g for g in out["guards"] if g in ("clamp", "max_step")]
    out.update(status="updated" if v0 else "scaled", factor=stepped, version=vmax + 1, changed=True,
               reason=f"ratio {ratio:.3f}" + (f" ({', '.join(steps)}: {stepped:.4f})" if steps else ""))
    return out


# --- the factor store ------------------------------------------------------------------------------
def history_path(cfg: RainConfig, family: str) -> Path:
    p = cfg.factors_path(family)
    return p.with_name(p.stem + ".history.jsonl")


def load_factors(cfg: RainConfig, family: str) -> dict | None:
    p = cfg.factors_path(family)
    if not p.exists():
        return None
    d = json.loads(p.read_text())
    if d.get("format") != FACTORS_FORMAT or d.get("family") != family:
        raise ValueError(f"{p}: not a {FACTORS_FORMAT} file for {family}")
    return d


def load_history(cfg: RainConfig, family: str) -> list[dict]:
    p = history_path(cfg, family)
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def factors_for(cfg: RainConfig, tag: str, regions) -> ScaleFactors:
    """The scale factors for product ``tag`` on ``regions`` (a buffer's): the current factors of its
    family when ``[scaling] apply`` is on and ``[scaling.<family>] products`` lists it, else unity."""
    p = product(tag)
    regions = tuple(regions)
    src = cfg.scaling.source(p.family)
    d = load_factors(cfg, p.family) if cfg.scaling.apply and src and tag in src.products else None
    if d is None:
        return ScaleFactors.unity(p.family, regions)
    cur = d.get("regions", {})
    return ScaleFactors(p.family, regions, [float(cur.get(r, {}).get("factor", 1.0)) for r in regions],
                        [int(cur.get(r, {}).get("version", 0)) for r in regions])


def describe_factors(f: ScaleFactors) -> dict:
    """For reports: the regions with a factor (version > 0)."""
    return {r: {"factor": round(float(f.factor[k]), 4), "version": int(f.version[k])}
            for k, r in enumerate(f.regions) if f.version[k] > 0}


def apply_to_buffers(cfg: RainConfig, family: str, log=print) -> dict:
    """Re-scale the saved buffers of ``family`` with the current factors (unity when off); returns hours per kind."""
    from .sources import family_products

    done = {}
    for kind in ("observed", "forecast"):
        p = cfg.buffer_path(kind, family)
        if not p.exists():
            continue
        buf = HourlyBuffer.load(p)
        tags = family_products(family, kind)
        if not tags:
            continue
        f = factors_for(cfg, tags[0], buf.regions)
        n = buf.apply_factors(f)
        buf.save(p)
        done[kind] = n
        log(f"{kind} {family} buffer re-scaled: {n} hours, factors {describe_factors(f) or 'none (version 0)'}")
    return done


# --- the whole step ----------------------------------------------------------------------------------
def _network_regions(cfg: RainConfig, family: str) -> tuple[str, ...]:
    p = cfg.weights_path(family)
    if p.exists():
        with np.load(p, allow_pickle=False) as z:
            return tuple(json.loads(str(z["meta"]))["regions"])
    for kind in ("observed", "forecast"):
        b = cfg.buffer_path(kind, family)
        if b.exists():
            with np.load(b, allow_pickle=False) as z:
                return tuple(json.loads(str(z["meta"]))["regions"])
    raise SystemExit(f"no {p.name} or buffers in {cfg.rain_dir}: the scaling regions come from them "
                     f"(river-finder rain weights)")


def make_readers(cfg: RainConfig, names, http=None, log=print) -> list:
    from .ecmwf import make_reader
    from .fetch import Http

    ec = cfg.ecmwf
    http = http or Http(log=log)
    out = []
    for name in names:
        rd = make_reader(name, cfg.grids["ecmwf"], http=http, cache_dir=cfg.cache_dir, keep_days=ec.keep_days,
                         ecmwf_urls=ec.ecmwf_urls, openmeteo_url=ec.openmeteo_url, log=log)
        rd.cache_write = False  # read what rain ecmwf cached; old runs would only be pruned again
        out.append(rd)
    return out


def run_scale(cfg: RainConfig, family: str = "ecmwf", days: int | None = None, now: datetime | None = None,
              force: bool = False, fetch: bool = True, readers=None, http=None, log=print) -> dict:
    """Update the cell store, find copies of one station, compute and store the factors of ``family``,
    re-scale the saved buffers (``[scaling] apply``). See the module docstring."""
    from .gauges import GaugeHours

    sc = cfg.scaling
    src = sc.source(family)
    if src is None:
        raise SystemExit(f"no [scaling.{family}] (scaling sources: {', '.join(s.family for s in sc.sources)})")
    days = int(days or sc.days)
    if days * 24 > sc.keep_days * 24:
        raise SystemExit(f"--days {days} is longer than [scaling] keep_days ({sc.keep_days})")
    clock = (now or datetime.now(UTC)).astimezone(UTC)
    now_h = hour_index(floor_hour(clock))
    gpath = cfg.gauges_dir / "gauges_hourly.npz"
    if not gpath.exists():
        raise SystemExit(f"no {gpath}: run river-finder rain gauges first")
    g = GaugeHours.load(gpath)
    grid = cfg.grids[family]
    gg = g.meta.get("grid")
    if gg and not GridSpec(**{k: gg[k] for k in ("name", "lon0", "lat0", "dlon", "dlat", "nx", "ny")}).same_cells(grid):
        raise SystemExit(f"{gpath.name} places gauges on another grid than [grids.{family}]: run rain gauges again")
    regions = _network_regions(cfg, family)
    end = min(now_h, int(g.hour[-1]))
    first = end - days * 24 + 1
    cols = np.flatnonzero((g.hour >= first) & (g.hour <= end))
    log(f"gauges {gpath} ({g.meta.get('created')}): {g.n} gauges, hours {_hour(g.hour[0])} .. {_hour(g.hour[-1])}; "
        f"window {days} d: {_hour(first - 1)} .. {_hour(end)}" + (f" ({now_h - end} h before now)" if end < now_h
                                                                  else ""))
    if end < now_h - 24:
        log(f"  note: the gauge cache ends {now_h - end} h before now (river-finder rain gauges)")

    # copies of one station
    cp = find_copies(g, cols, sc, _home_regions(cfg))
    keep = cp["keep"]
    log(f"copies of one station: {len(cp['groups'])} stations with copies, {int((~keep).sum())} copies dropped "
        f"({cp['pairs_checked']} pairs within {sc.dedupe_km:g} km compared over {cp['compared']['hours']} h, "
        f"{cp['compared']['first_hour']} .. {cp['compared']['last_hour']})")
    for grp in cp["groups"]:
        log(f"  kept {grp['kept']}; dropped {', '.join(grp['dropped'])}: {next(iter(grp['why'].values()))}")
    for c in cp["colocated"]:
        if c.get("note"):
            log(f"  co-located, not copies ({c['distance_m']:.0f} m): {c['note']}")

    # the grid side
    store = open_store(cfg, family)
    trimmed = store.trim(now_h - sc.keep_days * 24 + 1)
    cells = np.unique(g.cell[g.cell >= 0]).astype(np.int64)
    fill = None
    if fetch:
        if family != "ecmwf":
            raise SystemExit(f"no grid filler for {family} yet")
        if readers is None:
            readers = make_readers(cfg, src.readers, http=http, log=log)
        http = http or (readers[0].bucket.http if readers and hasattr(readers[0], "bucket") else None)
        b0, q0 = (http.stats.bytes, http.stats.requests) if http else (0, 0)
        log(f"cell store {cfg.cells_path(family)}: {cells.size} cells; filling {_hour(first)} .. {_hour(end)} "
            f"from {', '.join(r.name for r in readers)} (leads {cfg.ecmwf.obs_leads[0]}-{cfg.ecmwf.obs_leads[1]} h)")
        fill = fill_ecmwf(store, readers, cells, first, end, cfg.ecmwf.obs_leads, log=log)
        if http:
            fill["download"] = {"bytes": http.stats.bytes - b0, "requests": http.stats.requests - q0}
        log(f"  {fill['written']} hours written, {fill['replaced']} replaced by a newer run, {fill['settled']} "
            f"settled, {len(fill['no_run'])} with no run; by reader {fill['by_reader']}"
            + (f"; {fill['download']['bytes'] / 1e6:.1f} MB in {fill['download']['requests']} requests"
               if http else "") + f", {fill['seconds']} s")
        store.save(cfg.cells_path(family))
    else:
        store.ensure(cells, first, end)
    coverage = store.coverage(cells, first, end)
    grid_ok = coverage >= sc.min_usable
    summ = store.summary()
    log(f"cell store: {summ['cells']} cells, {summ['first']} .. {summ['last']}, by reader {summ['by_reader']}; "
        f"window coverage {coverage:.1%}" + ("" if grid_ok else f" (< {sc.min_usable:.0%}: factors held)"))

    # evaluate and decide
    grid_vals = store.values(np.maximum(g.cell, 0), first, end)   # (hours, gauges): each gauge's cell
    grid_vals[:, np.asarray(g.cell) < 0] = np.nan
    prev = (load_factors(cfg, family) or {}).get("regions", {})
    hist = load_history(cfg, family)
    seq = (hist[-1]["seq"] + 1) if hist else 1
    when, created = clock, _iso(clock)
    gidx = {r: k for k, r in enumerate(g.regions)}
    rows_by = g.region_rows()
    window = {"from": _hour(first - 1), "to": _hour(end), "first_hour": _hour(first), "last_hour": _hour(end),
              "hours": int(end - first + 1), "days": days}
    cfg_snap = {k: getattr(sc, k) for k in ("days", "min_gauges", "min_usable", "min_gauge_mm", "min_grid_mm",
                                            "clamp", "max_step", "min_change", "min_update_hours", "dedupe_km",
                                            "dedupe_min_wet_hours", "dedupe_equal_share", "dedupe_equal_mm")}
    cfg_snap["clamp"] = list(sc.clamp)
    inputs = {"gauges": {"file": str(gpath.relative_to(cfg.rain_dir)), "created": g.meta.get("created"),
                         "hours": [_hour(g.hour[0]), _hour(g.hour[-1])]},
              "cells": {"file": str(cfg.cells_path(family).relative_to(cfg.rain_dir)), **summ,
                        "window_coverage": round(coverage, 4)}}
    states, evals, records = {}, {}, []
    for r in regions:
        rows = rows_by.get(r, np.zeros(0, np.int64)) if r in gidx else np.zeros(0, np.int64)
        ev = evaluate_region(rows, g, cols, grid_vals, keep, sc)
        p = prev.get(r, {"factor": 1.0, "version": 0, "set": None, "max_version": 0})
        dec = decide(p, ev, sc, when, force=force, grid_ok=grid_ok)
        ev["decision"] = {k: dec[k] for k in ("status", "reason", "guards", "target", "stepped")}
        evals[r] = ev
        st = {"factor": dec["factor"], "version": dec["version"], "status": dec["status"], "reason": dec["reason"],
              "set": p.get("set"), "record": p.get("record"),
              "max_version": max(int(p.get("max_version", 0)), dec["version"])}
        if dec["changed"]:
            rec = {"format": HISTORY_FORMAT, "seq": seq, "family": family, "products": list(src.products),
                   "region": r, "factor": dec["factor"], "version": dec["version"],
                   "previous": {"factor": float(p.get("factor", 1.0)), "version": int(p.get("version", 0))},
                   "status": dec["status"], "reason": dec["reason"], "created": created, "window": window,
                   "evaluation": {k: v for k, v in ev.items() if k != "decision"}, "guards": dec["guards"],
                   "copies_dropped": [x for grp in cp["groups"] for x in grp["dropped"] if grp["region"] == r],
                   "config": cfg_snap, "inputs": inputs}
            records.append(rec)
            st.update(set=created, record=seq)
            seq += 1
        states[r] = st
    if records:
        hp = history_path(cfg, family)
        hp.parent.mkdir(parents=True, exist_ok=True)
        with open(hp, "a", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    councils = g.meta.get("councils", {})
    doc = {"format": FACTORS_FORMAT, "family": family, "products": list(src.products), "updated": created,
           "version_doc": "0 = unscaled (factor 1); versions > 0 are unique per region and only change with the factor",
           "regions": states,
           "councils": {cid: {k: c.get(k) for k in ("name", "attribution", "licence", "base_url")}
                        for cid, c in councils.items()},
           "last_update": {"created": created, "now": _hour(now_h), "window": window, "config": cfg_snap,
                           "inputs": inputs, "fill": fill, "trimmed_hours": trimmed, "forced": force,
                           "copies": {k: v for k, v in cp.items() if k != "keep"}, "regions": evals,
                           "new_records": [rec["seq"] for rec in records]}}
    _write_json(cfg.factors_path(family), doc)
    log_table(doc, log)
    log(f"factors -> {cfg.factors_path(family)}; {len(records)} new version records -> "
        f"{history_path(cfg, family).name}")
    if sc.apply:
        doc["applied"] = apply_to_buffers(cfg, family, log=log)
    else:
        log("[scaling] apply = false: the buffers are left as they are (rain ecmwf writes them unscaled)")
    return doc


def log_table(doc: dict, log=print) -> None:
    """The per-region table of a factors document."""
    ev = doc["last_update"]["regions"]
    log(f"{'region':<14} {'gauges':>6} {'gauge mm':>9} {'grid mm':>9} {'ratio':>6} {'median':>6} {'target':>6} "
        f"{'factor':>6} {'ver':>3}  status")
    quiet = []
    for r, st in doc["regions"].items():
        e = ev.get(r, {})
        if not e.get("gauges_listed"):
            quiet.append(r)
            continue
        d = e.get("decision", {})

        def f(v, w=6, p=3):
            return f"{v:>{w}.{p}f}" if isinstance(v, int | float) else f"{'-':>{w}}"
        log(f"{r:<14} {e['gauges']:>3}/{e['gauges_listed']:<2} {f(e['gauge_mm'], 9, 1)} {f(e['grid_mm'], 9, 1)} "
            f"{f(e['ratio'])} {f(e['check'].get('median_ratio'))} {f(d.get('target'))} {st['factor']:>6.3f} "
            f"{st['version']:>3}  {st['status']}: {st['reason']}")
    if quiet:
        log(f"{len(quiet)} regions without gauges: factor 1, version 0 ({', '.join(quiet)})")


def status_lines(cfg: RainConfig) -> list[str]:
    """Lines for ``rain status``: the current factors and the cell store of each scaling source."""
    out = [f"scaling: apply {str(cfg.scaling.apply).lower()}, publish {str(cfg.scaling.publish).lower()} "
           f"(window {cfg.scaling.days} d; {cfg.scaling_dir})"]
    for src in cfg.scaling.sources:
        d = load_factors(cfg, src.family)
        if d is None:
            out.append(f"  {src.family}: no factors yet (river-finder rain scale): factor 1, version 0 everywhere")
            continue
        sc = [f"{r} {s['factor']:.3f} v{s['version']}" for r, s in d["regions"].items() if s["version"] > 0]
        w = d["last_update"]["window"]
        out.append(f"  {src.family} {d['updated']} (window {w['from']} .. {w['to']}; products "
                   f"{', '.join(d['products'])}): " + ("; ".join(sc) if sc else "no region scaled")
                   + f"; {sum(1 for s in d['regions'].values() if s['version'] == 0)} regions at version 0")
        cp = cfg.cells_path(src.family)
        if cp.exists():
            s = CellStore.load(cp).summary()
            out.append(f"  {src.family} cells: {s['cells']} cells, {s['first']} .. {s['last']}, by reader "
                       f"{s['by_reader']}, {s['missing']} cell-hours missing")
    return out
