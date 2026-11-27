"""Storm validation of the gauge scale factors (rf-t26.7): ECMWF raw and scaled against the rain gauges, for
one past event, at the gauges and over whole catchments.

``river-finder rain validate --event START END [--region "West Coast" ...] [--catchment NAME ...]
[--no-fetch] [--out DIR]`` (START, END: UTC hours ``YYYYMMDDHH``; the event is the rain in (START, END])
writes ``<rain>/validation/validate_<regions>_<START>_<END>.json`` and ``.md`` (the tables of
``docs/rainfall-validation-westcoast-2026-11.md``). Steps:

1. **ECMWF over the event** (:func:`event_store`): the observed proxy (each hour from the newest run
   that has it within ``[ecmwf] obs_leads``, so leads 1-6 for a past event: :func:`.scaling.fill_ecmwf`)
   on every cell of the NZ window, over the event and ``margin`` hours either side (for the lags), in
   ``<rain>/validation/ecmwf_grid_<from>_<to>.npz`` (a :class:`.scaling.CellStore`; ~64 MB from
   Open-Meteo or ~35 MB of GRIB per 96 h, read once). Readers as ``[scaling.ecmwf] readers``:
   Open-Meteo keeps about 8 days of runs, ECMWF's AWS bucket the 0.1° runs since 2026-10-20 06z.
2. **Gauges** as the scaling cleans them: copies of one station counted once
   (:func:`.scaling.find_copies`, the kept copy chosen on the event hours), likely outages and under-read
   events left out (:func:`.scaling.find_outages`, :func:`.scaling.find_underreads` over every cached hour;
   ``underread=False``, ``--no-underread``: the outages only, as before rf-t26.20), each gauge in its
   scaling zone. A gauge that under-read the storm itself (Colliers Ck: 2 mm against 203 mm in its cell on
   24-27 October) has the storm's hours left out and drops out by ``min_usable``; the rule needs less than
   ``underread_fraction`` of both its neighbours and ECMWF, so it leaves out broken gauges, not those that
   ECMWF alone gets wrong. Per gauge:
   the event total over its usable hours and ECMWF at its 0.1° cell over the same hours
   (:func:`.scaling.evaluate_region` on the event hours: at least ``min_usable`` of them). The cell
   values come from the scaling cell store (the numbers the factor was fitted on) where it has them,
   else from the event store; ``ecmwf_check`` compares the two at the gauge cells.
3. **Factors** per zone (:func:`factor_variants`): ``in_use`` (``factors_ecmwf.json``: the factor and
   version applied now), ``window`` (the ratio of sums over the window of the factor file's last
   update, recomputed: in sample when the event lies inside it), ``leo`` (the same window without the
   event's hours: out of sample in time), and per gauge ``logo`` (``leo`` without the gauge itself) and
   per catchment ``loco`` (``leo`` without the gauges inside the catchment). Each is a ratio of sums
   clamped to ``[scaling] clamp``; fewer than ``min_gauges`` gauges give 1 and a dry window keeps the
   factor in use, as in :func:`.scaling.decide` (``max_step`` and ``min_change`` are rules of the
   version history and are not applied).
4. **Catchments** (:func:`catchment_cells`): for each outlet reach, every local catchment upstream
   (the network of :mod:`.upstream`), so the ECMWF total is ``rain totals``' upstream mean depth
   (``sum(pixel area x local depth) / upstream pixel area``) for a window of the event's length; the
   scaled total applies each upstream catchment's zone factor. Three gauge-based estimates, none of
   them the truth (gauges are points, mostly in valleys): ``inside`` (mean of the gauges whose 8 m
   local catchment, ``local_catchment_core.tif``, lies upstream), ``idw`` (gauge totals interpolated
   to the catchment's 0.1° cells with inverse-distance weights, power 2, gauges within 30 km and at
   least the 3 nearest, weighted by the catchment's area in each cell; gauges of the zones upstream
   only, so no gauge from the other side of the main divide) and ``guided`` (ECMWF's own pattern times
   the gauges' local ratio: each cell's ECMWF total x the interpolated gauge / ECMWF ratio, in logs).
5. **Timing** (:func:`timing`): hourly correlation at lag 0, the lag with the best correlation
   (``k > 0``: ECMWF later than the gauges), the correlation of 3-hour sums (hours ending 03, 06, ...
   UTC: the GRIB steps), the peak hour and the rain centroid (hours), per gauge, for the zone's mean
   over the gauges with every event hour usable, and per catchment (ECMWF over the catchment against
   the ``idw`` hourly series).
6. **Metrics** (:func:`error_metrics`) per variant: bias (mm), ECMWF / gauge (ratio of sums), median
   ratio, RMSE and MAE (mm) and the RMS of log(gauge / ECMWF), the score of the zone choice
   (``docs/rainfall-scaling.md``).

The gauge cache (``rain gauges``) keeps 60 days; an older storm needs the gauges' history first.
"""

from __future__ import annotations

import json
import math
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from .config import RainConfig
from .grid import hour_index, hour_time

FORMAT = "river-finder rain storm validation v1"
DEFAULT_CATCHMENTS = {
    "West Coast": ("haast", "waiho", "whataroa", "wanganui", "hokitika", "arahura", "taramakau", "grey", "ahaura",
                   "buller", "karamea"),
}
MAX_LAG = 6
IDW_POWER = 2.0
IDW_RADIUS_KM = 30.0
IDW_MIN_GAUGES = 3
IDW_MIN_KM = 1.0         # distances below this count as 1 km (a gauge inside a cell does not take all its weight)
GUIDED_MIN_MM = 5.0      # guided estimate: gauges with at least this much ECMWF in their cell give a ratio ...
GUIDED_CLIP = (0.2, 5.0)  # ... clipped to this range
VARIANTS = {
    "raw": "ECMWF raw",
    "in_use": "x the factor in use",
    "window": "x the 30-day ratio (window of the factor file)",
    "leo": "x the 30-day ratio without the event",
    "event": "x the event's own ratio (in sample, the best one factor can do)",
    "logo": "x the 30-day ratio without the event and the gauge",
    "loco": "x the 30-day ratio without the event and the catchment's gauges",
}


def _hour(h: int) -> str:
    return hour_time(int(h)).strftime("%Y-%m-%d %H:00Z")


def _label(h: int) -> str:
    return hour_time(int(h)).strftime("%Y%m%d%H")


def _overlaps(x: dict, first: int, last: int) -> bool:
    """Whether a spell or event of :mod:`.scaling` (``from``, ``to``: "YYYY-MM-DD HH:00Z", from = the hour before
    its first) holds any of the hours ending ``first .. last``."""
    def h(v: str) -> int:
        return hour_index(datetime.strptime(v, "%Y-%m-%d %H:%MZ").replace(tzinfo=UTC))

    return h(x["from"]) + 1 <= last and h(x["to"]) >= first


def _r(v, p: int = 3):
    return None if v is None or not np.isfinite(v) else round(float(v), p)


def parse_event(start: str, end: str) -> tuple[int, int]:
    """The hours ending ``first .. last`` of an event given as UTC hours ``YYYYMMDDHH``: the rain in (START, END]."""
    from .ecmwf import parse_hour

    a, b = hour_index(parse_hour(start)), hour_index(parse_hour(end))
    if b <= a:
        raise SystemExit(f"--event {start} {end}: the end must be after the start")
    return a + 1, b


def slug(names) -> str:
    return "-".join("".join(ch if ch.isalnum() else "-" for ch in str(n).lower()).strip("-") for n in names)


# --- metrics ---------------------------------------------------------------------------------------
def error_metrics(model, gauge) -> dict:
    """Model (ECMWF) totals against gauge totals (mm), one value per gauge or catchment: ``bias_mm`` = mean of
    model - gauge, ``model_over_gauge`` = ratio of sums, ``median_ratio`` and ``geo_mean_ratio`` of model / gauge,
    ``rmse_mm``, ``mae_mm``, ``rms_log`` = RMS of log(gauge / model) (pairs with both > 0)."""
    m, o = np.asarray(model, dtype=np.float64), np.asarray(gauge, dtype=np.float64)
    ok = np.isfinite(m) & np.isfinite(o)
    m, o = m[ok], o[ok]
    if not m.size:
        return {"n": 0}
    d = m - o
    pos = (m > 0) & (o > 0)
    lr = np.log(m[pos] / o[pos])
    return {"n": int(m.size), "gauge_mean_mm": _r(o.mean(), 1), "model_mean_mm": _r(m.mean(), 1),
            "bias_mm": _r(d.mean(), 1), "model_over_gauge": _r(m.sum() / o.sum()) if o.sum() > 0 else None,
            "median_ratio": _r(np.median(m[pos] / o[pos])) if pos.any() else None,
            "geo_mean_ratio": _r(math.exp(lr.mean())) if lr.size else None,
            "rmse_mm": _r(math.sqrt(float((d ** 2).mean())), 1), "mae_mm": _r(np.abs(d).mean(), 1),
            "rms_log": _r(math.sqrt(float((lr ** 2).mean()))) if lr.size else None}


def _corr(a: np.ndarray, b: np.ndarray) -> float | None:
    ok = np.isfinite(a) & np.isfinite(b)
    a, b = a[ok].astype(np.float64), b[ok].astype(np.float64)
    if a.size < 3 or np.ptp(a) == 0 or np.ptp(b) == 0:
        return None
    return float(np.corrcoef(a, b)[0, 1])


def timing(x, y, first: int, pad: int, max_lag: int = MAX_LAG) -> dict:
    """Timing of model ``y`` against gauge ``x`` (hourly mm, NaN = no value). ``x`` covers hours ending ``first ..
    first + T - 1``; ``y`` the same hours with ``pad`` more on each side (``y[pad + t]`` is the hour of ``x[t]``).

    ``r0``: correlation at lag 0; ``r_by_lag`` / ``best_lag``: correlation of ``x[t]`` with ``y[t + k]`` for
    ``|k| <= max_lag`` (``k > 0``: the model is late); ``r3``: correlation of 3-hour sums (hours ending 03, 06, ...
    UTC, blocks with all 3 hours valued); peak hours and mm; ``centroid_lag_h``: model minus gauge rain centroid."""
    x, y = np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)
    T = x.size
    if y.size != T + 2 * pad:
        raise ValueError("y must cover the hours of x and pad more on each side")
    ym = y[pad:pad + T]
    by_lag = {}
    for k in range(-max_lag, max_lag + 1):
        lo, hi = max(0, -(pad + k)), min(T, y.size - (pad + k))
        if hi - lo < 3:
            continue
        xs = np.full(T, np.nan)
        xs[lo:hi] = x[lo:hi]
        ys = np.full(T, np.nan)
        ys[lo:hi] = y[pad + k + lo:pad + k + hi]
        by_lag[k] = _corr(xs, ys)
    valid = {k: v for k, v in by_lag.items() if v is not None}
    best = max(valid, key=lambda k: (valid[k], -abs(k))) if valid else None
    both = np.isfinite(x) & np.isfinite(ym)
    hours = first + np.arange(T)
    blk = (hours + 2) // 3                 # hour ending 03 UTC closes a block: (h + 2) // 3 is the block id
    r3 = None
    if both.any():
        ids = np.unique(blk[both])
        full = [b for b in ids if (both & (blk == b)).sum() == 3]
        if len(full) >= 3:
            sx = np.array([x[blk == b].sum() for b in full])
            sy = np.array([ym[blk == b].sum() for b in full])
            r3 = _corr(sx, sy)
    out = {"hours": int(both.sum()), "r0": _r(by_lag.get(0)), "best_lag_h": best,
           "r_best": _r(valid.get(best)) if best is not None else None, "r3": _r(r3),
           "r_by_lag": {str(k): _r(v) for k, v in by_lag.items()}}
    if both.any() and x[both].sum() > 0 and ym[both].sum() > 0:
        xb, yb = np.where(both, x, 0.0), np.where(both, ym, 0.0)
        px, py = int(np.argmax(xb)), int(np.argmax(yb))
        t = np.arange(T)
        cx, cy = float((t * xb).sum() / xb.sum()), float((t * yb).sum() / yb.sum())
        out.update(gauge_peak_mm=_r(xb[px], 1), gauge_peak_hour=_hour(hours[px]), model_peak_mm=_r(yb[py], 1),
                   model_peak_hour=_hour(hours[py]), peak_lag_h=py - px, gauge_centroid=_hour(hours[0] + round(cx)),
                   model_centroid=_hour(hours[0] + round(cy)), centroid_lag_h=_r(cy - cx, 2))
    return out


# --- ECMWF over the event ------------------------------------------------------------------------------
def store_path(cfg: RainConfig, first: int, last: int, family: str = "ecmwf") -> Path:
    """``<rain>/validation/<family>_grid_<from>_<to>.npz``: hours ending ``first .. last`` (from = first - 1)."""
    return cfg.rain_dir / "validation" / f"{family}_grid_{_label(first - 1)}_{_label(last)}.npz"


def event_store(cfg: RainConfig, first: int, last: int, fetch: bool = True, readers=None, http=None, log=print):
    """The ECMWF observed proxy on every cell of the NZ window for hours ending ``first .. last``: the saved store,
    filled where it lacks hours when ``fetch`` (:func:`.scaling.fill_ecmwf`, readers of ``[scaling.ecmwf]``)."""
    from .scaling import CellStore, fill_ecmwf, make_readers

    grid = cfg.grids["ecmwf"]
    path = store_path(cfg, first, last)
    store = CellStore.load(path) if path.exists() else CellStore.empty("ecmwf", grid)
    if not store.grid.same_cells(grid):
        raise SystemExit(f"{path}: another grid than [grids.ecmwf]")
    cells = np.arange(grid.size)
    if fetch:
        if readers is None:
            src = cfg.scaling.source("ecmwf")
            readers = make_readers(cfg, src.readers if src else ("openmeteo-s3", "ecmwf-s3"), http=http, log=log)
        log(f"ECMWF over the event: {path.name}, {_hour(first)} .. {_hour(last)} on {cells.size} cells")
        try:
            stats = fill_ecmwf(store, readers, cells, first, last, cfg.ecmwf.obs_leads, log=log,
                               checkpoint=lambda: store.save(path), every=12)
        finally:
            store.meta.setdefault("note", "rf-t26.7 storm validation: ECMWF observed proxy on the whole NZ window")
            store.save(path)
        log(f"  {stats['written']} hours read, {stats['replaced']} replaced, {stats['settled']} held; by reader "
            f"{stats['by_reader']}")
    elif not path.exists():
        raise SystemExit(f"no {path}: run rain validate without --no-fetch first")
    store.ensure(cells, first, last)
    return store, path


def _values(stores, cells, first: int, last: int) -> tuple[np.ndarray, list[int]]:
    """mm (hours ``first .. last``, cells) from the first store that has each value; how many values each gave."""
    cells = np.asarray(cells, dtype=np.int64)
    out = np.full((last - first + 1, cells.size), np.nan, np.float32)
    used = []
    for s in stores:
        if s is None or not s.n_hours:
            used.append(0)
            continue
        v = s.values(cells, first, last)
        take = ~np.isfinite(out) & np.isfinite(v)
        out[take] = v[take]
        used.append(int(take.sum()))
    return out, used


# --- factors ---------------------------------------------------------------------------------------------
def _target(ev: dict, sc, fallback: float) -> tuple[float, str]:
    """A factor from an evaluation as :func:`.scaling.decide` would aim for it: 1 below ``min_gauges``, the factor
    in use when too dry, else the ratio clamped."""
    if ev["gauges"] < sc.min_gauges:
        return 1.0, f"{ev['gauges']} gauges (< {sc.min_gauges}): factor 1"
    if (ev["mean_gauge_mm"] or 0) < sc.min_gauge_mm or (ev["mean_grid_mm"] or 0) < sc.min_grid_mm:
        return fallback, "too dry for a ratio: the factor in use"
    lo, hi = sc.clamp
    return float(min(max(ev["ratio"], lo), hi)), f"ratio {ev['ratio']:.3f}"


def _window_hours(doc: dict | None, g) -> tuple[int, int]:
    """First and last hour of the factor file's last update (else the last 30 days of the gauge cache)."""
    w = (doc or {}).get("last_update", {}).get("window")
    if w:
        f = datetime.strptime(w["first_hour"], "%Y-%m-%d %H:%MZ").replace(tzinfo=UTC)
        t = datetime.strptime(w["last_hour"], "%Y-%m-%d %H:%MZ").replace(tzinfo=UTC)
        return hour_index(f), hour_index(t)
    return int(g.hour[-1]) - 719, int(g.hour[-1])


def factor_variants(g, keep, outage, gzone, zone_names, doc, stores, sc, event: tuple[int, int],
                    underread=None) -> dict:
    """Per zone: the factor in use, the window ratio and the ratio without the event (see the module docstring),
    with the evaluation behind each (``used``: gauges with their sums, for the leave-out variants). ``outage`` and
    ``underread`` mark the hours the scaling leaves out (:func:`.scaling.evaluate_region`)."""
    from .scaling import evaluate_region

    wf, wl = _window_hours(doc, g)
    wcols = np.flatnonzero((g.hour >= wf) & (g.hour <= wl))
    out = {"window": {"first_hour": _hour(wf), "last_hour": _hour(wl), "hours": int(wcols.size)}, "zones": {}}
    if not wcols.size:
        return out
    cells = np.maximum(np.asarray(g.cell), 0)
    gv, _ = _values(stores, cells, int(g.hour[wcols[0]]), int(g.hour[wcols[-1]]))
    gv[:, np.asarray(g.cell) < 0] = np.nan
    in_event = (g.hour[wcols] >= event[0]) & (g.hour[wcols] <= event[1])
    out["window"]["event_hours_inside"] = int(in_event.sum())
    cur = (doc or {}).get("regions", {})
    last = (doc or {}).get("last_update", {}).get("regions", {})
    for zi, z in enumerate(zone_names):
        rows = np.flatnonzero(gzone == zi)
        if not rows.size:
            continue
        st = cur.get(z, {})
        f_use = float(st.get("factor", 1.0))
        ev_w = evaluate_region(rows, g, wcols, gv, keep, sc, outage, underread)
        ev_l = evaluate_region(rows, g, wcols[~in_event], gv[~in_event], keep, sc, outage, underread)
        f_w, why_w = _target(ev_w, sc, f_use)
        f_l, why_l = _target(ev_l, sc, f_use)
        out["zones"][z] = {
            "in_use": {"factor": f_use, "version": int(st.get("version", 0)), "set": st.get("set"),
                       "status": st.get("status"), "file_ratio": (last.get(z) or {}).get("ratio")},
            "window": {"factor": round(f_w, 4), "why": why_w, "ratio": ev_w["ratio"], "gauges": ev_w["gauges"],
                       "gauge_mm": ev_w["gauge_mm"], "grid_mm": ev_w["grid_mm"]},
            "leo": {"factor": round(f_l, 4), "why": why_l, "ratio": ev_l["ratio"], "gauges": ev_l["gauges"],
                    "gauge_mm": ev_l["gauge_mm"], "grid_mm": ev_l["grid_mm"], "hours": int((~in_event).sum())},
            "_leo_used": ev_l["used"]}
    return out


def leave_out_factor(fz: dict, ids, sc) -> float:
    """The zone's no-event factor without the gauges ``ids`` (ratio of the remaining sums, clamped); 1 when fewer
    than ``min_gauges`` remain, the no-event factor when ``ids`` held none of its gauges."""
    used = fz["_leo_used"]
    ids = set(ids)
    rest = [u for u in used if u["id"] not in ids]
    if len(rest) == len(used):
        return fz["leo"]["factor"]
    if len(rest) < sc.min_gauges:
        return 1.0
    gs, es = sum(u["gauge_mm"] for u in rest), sum(u["grid_mm"] for u in rest)
    if es <= 0:
        return fz["leo"]["factor"]
    lo, hi = sc.clamp
    return round(float(min(max(gs / es, lo), hi)), 4)


# --- catchments ------------------------------------------------------------------------------------------
def upstream_mask(net, row: int) -> np.ndarray:
    """Rows of ``net`` (:class:`.upstream.Network`) that drain to ``row``, ``row`` included."""
    from .upstream import _outlet_of

    down = np.asarray(net.down, dtype=np.int64).copy()
    down[row] = -1          # the order stays valid: one link fewer
    return _outlet_of(net.order, down) == row


def catchment_cells(w, mask: np.ndarray, zone_of: np.ndarray, n_zones: int) -> dict:
    """Area weights of the catchment ``mask`` (rows of the weights ``w``) per 0.1° cell, per zone of its rows:
    ``u[z, c] = pixel area of the catchment's zone-z rows in cell c / its upstream pixel area``, so that
    ``u.sum(0) @ grid_mm`` is the catchment's mean depth, as :mod:`.upstream` sums it (rows without pixels
    count 0)."""
    rows = np.flatnonzero(mask & (np.asarray(w.npix) > 0))
    area = float(np.asarray(w.npix)[rows].sum()) * w.pixel_area_m2
    u = np.zeros((n_zones, w.counts.shape[1]))
    if area <= 0:
        return {"u": u, "area_km2": 0.0, "rows": int(mask.sum()), "zone_share": np.zeros(n_zones)}
    zo = np.asarray(zone_of)[rows]
    for z in np.unique(zo[zo >= 0]):
        sub = w.counts[rows[zo == z]]
        u[z] = np.asarray(sub.sum(axis=0)).ravel() * w.pixel_area_m2 / area
    return {"u": u, "area_km2": area / 1e6, "rows": int(mask.sum()), "zone_share": u.sum(axis=1)}


def cell_xy(grid) -> tuple[np.ndarray, np.ndarray]:
    """NZTM x, y of every cell centre of ``grid`` (C order)."""
    from .gauges import to_nztm

    lon, lat = np.meshgrid(grid.lons, grid.lats)
    return to_nztm(lon.ravel(), lat.ravel())


def idw_weights(px, py, gx, gy, power: float = IDW_POWER, radius_km: float = IDW_RADIUS_KM,
                min_gauges: int = IDW_MIN_GAUGES, min_km: float = IDW_MIN_KM) -> np.ndarray:
    """(points, gauges) inverse-distance weights, rows summing to 1: gauges within ``radius_km`` and at least the
    ``min_gauges`` nearest, weight ``max(d, min_km) ** -power``."""
    px, py = np.asarray(px, np.float64), np.asarray(py, np.float64)
    gx, gy = np.asarray(gx, np.float64), np.asarray(gy, np.float64)
    if not gx.size:
        return np.zeros((px.size, 0))
    d = np.hypot(px[:, None] - gx[None, :], py[:, None] - gy[None, :]) / 1000.0
    rank = np.argsort(np.argsort(d, axis=1), axis=1)
    use = (d <= radius_km) | (rank < min_gauges)
    wt = np.where(use, np.maximum(d, min_km) ** -power, 0.0)
    return wt / wt.sum(axis=1, keepdims=True)


def tile_bounds(tiles_dir: Path) -> list[tuple[Path, tuple[float, float, float, float]]]:
    import rasterio

    out = []
    for p in sorted(Path(tiles_dir).glob("*/local_catchment_core.tif")):
        with rasterio.open(p) as ds:
            b = ds.bounds
            out.append((p, (b.left, b.bottom, b.right, b.top)))
    return out


def locate_gauges(tiles_dir: Path, x, y, search_px: int = 6, gid_map=None) -> np.ndarray:
    """gid of the 8 m local catchment holding each point (NZTM ``x``, ``y``), from the tiles'
    ``local_catchment_core.tif``; a point on a pixel of no catchment takes the nearest one within ``search_px``
    pixels; -1 when none (or no position). Gids the 1 m merge dropped are read as the reach that took them
    (``gid_map``, default ``national.merged_gids`` of the tiles' root)."""
    import rasterio
    from rasterio.windows import Window

    from ..national import merged_gids

    gmap = merged_gids(Path(tiles_dir).parent) if gid_map is None else gid_map
    x, y = np.asarray(x, np.float64), np.asarray(y, np.float64)
    out = np.full(x.size, -1, dtype=np.int64)
    tiles = tile_bounds(tiles_dir)
    for i in range(x.size):
        if not (np.isfinite(x[i]) and np.isfinite(y[i])):
            continue
        for p, (left, bottom, right, top) in tiles:
            if not (left <= x[i] < right and bottom < y[i] <= top):
                continue
            with rasterio.open(p) as ds:
                r, c = ds.index(x[i], y[i])
                r0, c0 = max(0, r - search_px), max(0, c - search_px)
                win = gmap(ds.read(1, window=Window(c0, r0, 2 * search_px + 1, 2 * search_px + 1)))
            ok = win >= 0
            if ok.any():
                rr, cc = np.nonzero(ok)
                k = int(np.argmin((rr + r0 - r) ** 2 + (cc + c0 - c) ** 2))
                out[i] = int(win[rr[k], cc[k]])
            break
    return out


def resolve_catchments(specs, table, regions) -> list[dict]:
    """Outlet rows for catchment specs: ``GID``, ``NAME=GID``, or a river name (``rnorm`` or ``river`` of the
    segment table, case-insensitive): the reach of that name with the largest ``area_km2`` in ``regions``."""
    import re

    gid = table["gid"].to_numpy(np.int64)
    reg = table["region"].astype(str).to_numpy()
    in_reg = np.isin(reg, list(regions)) if regions else np.ones(gid.size, bool)
    names = table["rnorm"].fillna("").astype(str).str.lower().to_numpy() if "rnorm" in table else None
    river = table["river"].fillna("").astype(str).str.lower().to_numpy() if "river" in table else None
    area = table["area_km2"].to_numpy(np.float64)
    out = []
    for s in specs:
        s = str(s).strip()
        m = re.fullmatch(r"(?:(.+?)=)?(\d+)", s)
        if m:
            g = int(m.group(2))
            rows = np.flatnonzero(gid == g)
            if not rows.size:
                raise SystemExit(f"--catchment {s}: no reach with gid {g}")
            row = int(rows[0])
            label = m.group(1) or (river[row].title() if river is not None and river[row] else str(g))
        else:
            key = s.lower()
            hit = np.zeros(gid.size, bool)
            if names is not None:
                hit |= names == key
            if river is not None:
                hit |= (river == key) | (river == f"{key} river")
            hit &= in_reg
            if not hit.any():
                raise SystemExit(f"--catchment {s}: no reach of that name in {', '.join(regions) or 'the table'}")
            row = int(np.flatnonzero(hit)[np.argmax(area[hit])])
            label = s.title()
        out.append({"name": label, "spec": s, "row": row, "gid": int(gid[row]), "region": str(reg[row]),
                    "table_area_km2": _r(area[row], 1)})
    return out


# --- the whole step --------------------------------------------------------------------------------------
def _selected_zones(zm, regions) -> list[int]:
    if not regions:
        return list(range(len(zm.names)))
    sel = [i for i, (z, p) in enumerate(zip(zm.names, zm.parent, strict=True)) if z in regions or p in regions]
    if not sel:
        raise SystemExit(f"--region {' '.join(regions)}: no such scaling zone or region ({', '.join(zm.names)})")
    return sel


def run_validate(cfg: RainConfig, start: str, end: str, regions=("West Coast",), catchments=None,
                 fetch: bool = True, margin: int = 12, out_dir: Path | None = None, exclude=(), underread: bool = True,
                 readers=None, http=None, log=print) -> dict:
    """Validate the gauge-scaled ECMWF against the gauges for the event (START, END]; see the module docstring.
    ``exclude``: gauge ids left out of everything (factors included), for a sensitivity run; ``underread=False``:
    no under-read rule (rf-t26.20), only the outages. Writes
    ``validate_<regions>_<START>_<END>[_without-<n>][_no-underread].json`` and ``.md`` into ``out_dir`` (default
    ``<rain>/validation``)."""
    from .gauges import GaugeHours
    from .scaling import (
        _home_regions,
        describe_underread,
        evaluate_region,
        find_copies,
        find_outages,
        find_underreads,
        load_factors,
        open_store,
    )
    from .zones import load_zones

    t0 = time.monotonic()
    sc = cfg.scaling
    first, last = parse_event(start, end)
    T = last - first + 1
    regions = tuple(regions or ())
    gpath = cfg.gauges_dir / "gauges_hourly.npz"
    if not gpath.exists():
        raise SystemExit(f"no {gpath}: run river-finder rain gauges first")
    g = GaugeHours.load(gpath)
    if first < g.hour[0] or last > g.hour[-1]:
        raise SystemExit(f"the gauge cache holds {_hour(g.hour[0])} .. {_hour(g.hour[-1])}: the event "
                         f"{_hour(first - 1)} .. {_hour(last)} is not inside (rain gauges --days N)")
    cols = np.flatnonzero((g.hour >= first) & (g.hour <= last))
    zm = load_zones(cfg, log=log)
    zones = zm.names
    sel = _selected_zones(zm, regions)
    doc = load_factors(cfg, "ecmwf")
    if doc is None:
        log("note: no factor file: the factor in use is 1 everywhere")

    # ECMWF: the scaling cell store first (what the factors saw), the event store for the rest and the catchments
    ev_store, ev_path = event_store(cfg, first - margin, last + margin, fetch=fetch, readers=readers, http=http,
                                    log=log)
    sc_store = open_store(cfg, "ecmwf")
    stores = [sc_store, ev_store]
    cells_g = np.maximum(np.asarray(g.cell), 0)
    no_cell = np.asarray(g.cell) < 0
    grid_all, _ = _values(stores, cells_g, int(g.hour[0]), int(g.hour[-1]))
    grid_all[:, no_cell] = np.nan
    eg, by_store = _values(stores, cells_g, first - margin, last + margin)   # event +- margin at the gauge cells
    eg[:, no_cell] = np.nan
    ev_vals = eg[margin:margin + T]

    # gauges, cleaned as the scaling does
    cp = find_copies(g, cols, sc, _home_regions(cfg))
    keep = cp["keep"].copy()
    ids = np.asarray(g.id).astype(str)
    exclude = sorted(set(exclude or ()))
    unknown = [x for x in exclude if x not in set(ids.tolist())]
    if unknown:
        raise SystemExit(f"--exclude-gauge: no gauge {', '.join(unknown)}")
    keep &= ~np.isin(ids, exclude)    # counted as a dropped copy: out of the factors and the estimates
    out = find_outages(g, grid_all, keep, sc)
    outage = out["mask"]
    ur = (find_underreads(g, grid_all, keep, sc, outage) if underread else
          {"mask": np.zeros_like(outage), "events": [], "rule": None})
    under = ur["mask"]
    gregion = [g.regions[k] if k >= 0 else "" for k in g.region_of]
    gzone, gdiv = zm.gauge_zones(gregion, g.x, g.y)
    usable = g.ok & ~outage & ~under
    row_of = {gid: k for k, gid in enumerate(ids)}
    ur_in = [x | {"zone": zones[gzone[row_of[x["id"]]]] if gzone[row_of[x["id"]]] >= 0 else None}
             for x in ur["events"] if _overlaps(x, first, last)]
    log(f"gauges {gpath.name} ({g.meta.get('created')}): {g.n} gauges; event {_hour(first - 1)} .. {_hour(last)} "
        f"({T} h); {int((~keep).sum())} copies dropped, {len(out['spells'])} likely outages "
        f"({int(outage[:, cols].sum())} gauge-hours of the event), "
        + (f"{len(ur['events'])} under-read events ({describe_underread(sc)}; {len(ur_in)} in the event, "
           f"{int(under[:, cols].sum())} gauge-hours)" if underread else "under-read rule off"))
    for x in ur_in:
        log(f"  under-read: {x['id']} {x['gauge_mm']:.1f} mm in {x['hours']} h ({x['from']} .. {x['to']}) against a "
            f"neighbour median of {x['neighbour_median_mm']:.1f} mm and {x['grid_mm']:.1f} mm in ECMWF")

    # per-gauge totals over the event, every zone (the catchments use gauges of any zone upstream)
    evals = {z: evaluate_region(np.flatnonzero(gzone == zi), g, cols, ev_vals, keep, sc, outage, under)
             for zi, z in enumerate(zones) if (gzone == zi).any()}
    fv = factor_variants(g, keep, outage, gzone, zones, doc, stores, sc, (first, last), under)
    fz = fv["zones"]
    for z, ev in evals.items():   # the event's own ratio: what one factor per zone could do at best
        if z in fz:
            f_ev = float(min(max(ev["ratio"], sc.clamp[0]), sc.clamp[1])) if ev["ratio"] else 1.0
            fz[z]["event"] = {"factor": round(f_ev, 4), "ratio": ev["ratio"], "gauges": ev["gauges"],
                              "gauge_mm": ev["gauge_mm"], "grid_mm": ev["grid_mm"]}

    def fac(z, kind):
        return fz[z][kind]["factor"] if z in fz else 1.0

    gauges = []
    for z, ev in evals.items():
        for u in ev["used"]:
            i = row_of[u["id"]]
            x = np.where(usable[i, cols], g.mm[i, cols], np.nan)
            tm = timing(x, eg[:, i], first, margin)
            gm, em = u["gauge_mm"], u["grid_mm"]
            f_logo = leave_out_factor(fz[z], [u["id"]], sc) if z in fz else 1.0
            row = {"id": u["id"], "name": str(g.name[i]), "council": u["council"], "zone": z, "cell": u["cell"],
                   "hours": u["hours"], "gauge_mm": round(gm, 1), "raw_mm": round(em, 1),
                   "gauge_over_raw": _r(gm / em) if em > 0 else None,
                   "lon": _r(g.lon[i], 4), "lat": _r(g.lat[i], 4),
                   "gauge_peak_mm": _r(np.nanmax(x), 1) if np.isfinite(x).any() else None,
                   "model": {"raw": round(em, 1), "in_use": round(em * fac(z, "in_use"), 1),
                             "window": round(em * fac(z, "window"), 1), "leo": round(em * fac(z, "leo"), 1),
                             "logo": round(em * f_logo, 1), "event": round(em * fac(z, "event"), 1)},
                   "logo_factor": f_logo, "timing": tm}
            if np.isfinite(gdiv[i]):
                row["divide_km"] = round(float(gdiv[i]), 1)
            gauges.append(row)
    gauges.sort(key=lambda r: -r["gauge_mm"])

    zone_rep = {}
    for zi in sel:
        z = zones[zi]
        rs = [r for r in gauges if r["zone"] == z]
        ev = evals.get(z, {"gauges_listed": 0, "excluded": {}})
        rep = {"parent": zm.parent[zi], "gauges_listed": ev["gauges_listed"], "gauges": len(rs),
               "excluded": ev.get("excluded", {}), "factors": {k: v for k, v in fz.get(z, {}).items()
                                                               if not k.startswith("_")}}
        if rs:
            obs = np.array([r["gauge_mm"] for r in rs])
            rep["metrics"] = {k: error_metrics([r["model"][k] for r in rs], obs) for k in
                              ("raw", "in_use", "window", "leo", "logo", "event")}
            rep["summary"] = _event_summary(g, rs, row_of, usable, cols, first, ev_vals)
            full = [row_of[r["id"]] for r in rs if r["hours"] == T]
            if full:
                xm = np.where(usable[full][:, cols], g.mm[full][:, cols], np.nan).mean(axis=0)
                ym = eg[:, full].mean(axis=1)
                rep["timing"] = timing(xm, ym, first, margin) | {"gauges": len(full)}
            lags = [r["timing"]["best_lag_h"] for r in rs if r["timing"]["best_lag_h"] is not None]
            r0 = [r["timing"]["r0"] for r in rs if r["timing"]["r0"] is not None]
            rep["timing_per_gauge"] = {"median_r0": _r(np.median(r0)) if r0 else None,
                                       "best_lag_counts": {str(k): lags.count(k) for k in sorted(set(lags))}}
        zone_rep[z] = rep

    # catchments
    specs = list(catchments) if catchments else [c for r in (regions or ()) for c in DEFAULT_CATCHMENTS.get(r, ())]
    catch, cmetrics = [], {}
    if specs:
        catch = _catchments(cfg, specs, regions, zm, g, gzone, gauges, row_of, usable, cols, first, margin, ev_store,
                            fz, sc, log)
        for est in ("inside", "idw", "guided"):
            have = [c for c in catch if c["gauge"].get(est) is not None]
            if have:
                cmetrics[est] = {k: error_metrics([c["model"][k] for c in have], [c["gauge"][est] for c in have])
                                 for k in ("raw", "in_use", "window", "leo", "loco", "event")}

    # the two ECMWF stores at the gauge cells, over the event
    gc = np.unique(cells_g[~no_cell])
    a, _ = _values([sc_store], gc, first, last)
    b, _ = _values([ev_store], gc, first, last)
    both = np.isfinite(a) & np.isfinite(b)
    check = {"cells": int(gc.size), "cell_hours_both": int(both.sum()),
             "scaling_store_mm": _r(a[both].sum(), 1), "event_store_mm": _r(b[both].sum(), 1),
             "event_over_scaling": _r(b[both].sum() / a[both].sum(), 4) if a[both].sum() > 0 else None,
             "max_abs_mm": _r(np.abs(a[both] - b[both]).max(), 2) if both.any() else None,
             "values_from": {"scaling_store": by_store[0], "event_store": by_store[1]},
             "event_store_readers": ev_store.summary()["by_reader"]}
    report = {
        "format": FORMAT, "created": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": {"start": _hour(first - 1), "end": _hour(last), "first_hour": _hour(first), "last_hour": _hour(last),
                  "hours": T, "margin_h": margin},
        "regions": list(regions), "zones": [zones[i] for i in sel], "excluded_gauges": exclude,
        "underread_rule": bool(underread),
        "inputs": {"gauges": {"file": str(gpath.relative_to(cfg.rain_dir)), "created": g.meta.get("created"),
                              "hours": [_hour(g.hour[0]), _hour(g.hour[-1])]},
                   "factors": {"updated": (doc or {}).get("updated"), "window": fv["window"]},
                   "event_store": {"file": str(ev_path.relative_to(cfg.rain_dir)), **ev_store.summary()},
                   "scaling_store": sc_store.summary() if sc_store.n_hours else None},
        "ecmwf_check": check,
        "cleaning": {"copies_dropped": sorted(x for grp in cp["groups"] for x in grp["dropped"]),
                     "outages_in_event": [s for s in out["spells"] if _overlaps(s, first, last)],
                     "underreads_in_event": ur_in,
                     "rule": {"min_usable": sc.min_usable, "outage": out["rule"], "underread": ur["rule"]}},
        "zone_reports": zone_rep, "gauges": [r for r in gauges if r["zone"] in {zones[i] for i in sel}],
        "catchments": catch, "catchment_metrics": cmetrics,
        "method": {"idw": {"power": IDW_POWER, "radius_km": IDW_RADIUS_KM, "min_gauges": IDW_MIN_GAUGES,
                           "min_km": IDW_MIN_KM}, "guided": {"min_cell_mm": GUIDED_MIN_MM, "clip": list(GUIDED_CLIP)},
                   "max_lag_h": MAX_LAG, "variants": VARIANTS},
        "seconds": round(time.monotonic() - t0, 1)}
    out_dir = Path(out_dir) if out_dir else cfg.rain_dir / "validation"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = (f"validate_{slug(regions or ['all'])}_{_label(first - 1)}_{_label(last)}"
            + (f"_without-{len(exclude)}" if exclude else "") + ("" if underread else "_no-underread"))
    (out_dir / f"{stem}.json").write_text(json.dumps(report, indent=1, ensure_ascii=False))
    md = render_markdown(report)
    (out_dir / f"{stem}.md").write_text(md)
    for line in md.splitlines():
        log(line)
    log(f"report -> {out_dir / stem}.json (+ .md), {report['seconds']} s")
    return report


def _event_summary(g, rs, row_of, usable, cols, first, ev_vals) -> dict:
    """The event at the zone's gauges: totals, the wettest gauge and hour, the gauge-mean hourly peak."""
    idx = [row_of[r["id"]] for r in rs]
    x = np.where(usable[idx][:, cols], g.mm[idx][:, cols], np.nan)
    tot = np.array([r["gauge_mm"] for r in rs])
    k = int(np.nanargmax(np.nan_to_num(x, nan=-1).max(axis=1)))
    t = int(np.nanargmax(np.nan_to_num(x[k], nan=-1)))
    mean_h = np.nanmean(x, axis=0)
    tm = int(np.nanargmax(np.nan_to_num(mean_h, nan=-1)))
    e = ev_vals[:, idx]
    wet = np.flatnonzero(np.nan_to_num(mean_h) >= 1.0)
    return {"gauges": len(rs), "mean_mm": _r(tot.mean(), 1), "median_mm": _r(np.median(tot), 1),
            "max_mm": _r(tot.max(), 1), "max_gauge": rs[int(np.argmax(tot))]["id"],
            "over_100mm": int((tot > 100).sum()), "over_200mm": int((tot > 200).sum()),
            "peak_hour_mm": _r(x[k, t], 1), "peak_hour_gauge": rs[k]["id"], "peak_hour": _hour(first + t),
            "mean_peak_mm": _r(mean_h[tm], 1), "mean_peak_hour": _hour(first + tm),
            "wet_hours_mean_ge_1mm": int(wet.size),
            "wet_from": _hour(first + wet[0] - 1) if wet.size else None,
            "wet_to": _hour(first + wet[-1]) if wet.size else None,
            "ecmwf_mean_mm": _r(np.nansum(e, axis=0).mean(), 1),
            "ecmwf_peak_cell_hour_mm": _r(np.nanmax(e), 1) if np.isfinite(e).any() else None}


def _catchments(cfg, specs, regions, zm, g, gzone, gauges, row_of, usable, cols, first, margin, ev_store, fz, sc,
                log) -> list[dict]:
    import pandas as pd

    from .upstream import FLAG_NAMES, Network
    from .weights import CatchmentWeights

    w = CatchmentWeights.load(cfg.weights_path("ecmwf"))
    net = Network.load(cfg.network_path)
    if net.rows_digest != w.rows_digest or zm.rows_digest != w.rows_digest:
        raise SystemExit("network.npz, the zone file and weights_ecmwf.npz are for different rows: rain totals "
                         "--rebuild-network, rain zones")
    table = pd.read_pickle(cfg.segments_path)
    cols_needed = [c for c in ("gid", "region", "rnorm", "river", "area_km2") if c in table]
    table = table[cols_needed]
    targets = resolve_catchments(specs, table, [zm.parent[i] for i in range(len(zm.names))] if not regions
                                 else sorted({p for z, p in zip(zm.names, zm.parent, strict=True)
                                              if z in regions or p in regions}))
    T = cols.size
    grid = cfg.grids["ecmwf"]
    E = ev_store.values(np.arange(grid.size), first - margin, first + T - 1 + margin).astype(np.float64)
    Eev = E[margin:margin + T]
    Etot = Eev.sum(axis=0)
    px, py = cell_xy(grid)
    # gauges: their local catchment (8 m tiles), their totals over usable hours, ECMWF (event store) the same hours
    used = {r["id"]: r for r in gauges}
    gid_of = {}
    if cfg.tiles_dir.exists():
        uid = list(used)
        idx = [row_of[u] for u in uid]
        loc = locate_gauges(cfg.tiles_dir, np.asarray(g.x)[idx], np.asarray(g.y)[idx])
        gid_of = dict(zip(uid, loc.tolist(), strict=True))
    else:
        log(f"note: no {cfg.tiles_dir}: gauges are not placed in catchments (no 'inside' estimate)")
    net_row = {int(v): k for k, v in enumerate(net.gid)}
    gx = np.array([g.x[row_of[u]] for u in used])
    gy = np.array([g.y[row_of[u]] for u in used])
    gtot = np.array([used[u]["gauge_mm"] for u in used])
    uid = list(used)
    gz = np.array([zm.names.index(used[u]["zone"]) for u in uid])
    ok_h = np.stack([usable[row_of[u], cols] for u in uid]) if uid else np.zeros((0, T), bool)
    xh = np.stack([np.where(ok_h[k], g.mm[row_of[u], cols], 0.0) for k, u in enumerate(uid)]) if uid else ok_h * 0.0
    e_g = np.array([float(Eev[ok_h[k], int(g.cell[row_of[u]])].sum()) for k, u in enumerate(uid)])
    out = []
    for t in targets:
        mask = upstream_mask(net, t["row"])
        cc = catchment_cells(w, mask, zm.zone_of, len(zm.names))
        u = cc["u"]
        ucell = u.sum(axis=0)
        zs = {zm.names[z]: _r(s, 3) for z, s in enumerate(cc["zone_share"]) if s > 1e-4}
        zsel = np.flatnonzero(cc["zone_share"] > 1e-4)
        raw = float(ucell @ Etot)

        def scaled(kind, factors=None, u=u):
            f = np.array([(factors or {}).get(zm.names[z], fz[zm.names[z]][kind]["factor"] if zm.names[z] in fz
                                              else 1.0) for z in range(len(zm.names))])
            return float((f[:, None] * u).sum(axis=0) @ Etot)

        inside = [k for k, uu in enumerate(uid) if gid_of.get(uu, -1) in net_row and mask[net_row[gid_of[uu]]]]
        cand = np.flatnonzero(np.isin(gz, zsel))
        cells = np.flatnonzero(ucell > 0)
        est = {"inside": _r(gtot[inside].mean(), 1) if inside else None, "idw": None, "guided": None}
        tm = None
        loco = {zm.names[z]: leave_out_factor(fz[zm.names[z]], [uid[k] for k in inside], sc)
                for z in zsel if zm.names[z] in fz}
        if cand.size and cells.size:
            W = idw_weights(px[cells], py[cells], gx[cand], gy[cand])
            share = ucell[cells] / ucell[cells].sum()
            v = share @ W                                   # weight of each candidate gauge in the catchment
            est["idw"] = _r(float(v @ gtot[cand]), 1)
            rg = (gtot[cand] > 0) & (e_g[cand] >= GUIDED_MIN_MM)
            if rg.any():
                W2 = idw_weights(px[cells], py[cells], gx[cand][rg], gy[cand][rg])
                lr = np.log(np.clip(gtot[cand][rg] / e_g[cand][rg], *GUIDED_CLIP))
                est["guided"] = _r(float(ucell[cells] @ (Etot[cells] * np.exp(W2 @ lr))), 1)
            num = (v[:, None] * xh[cand]).sum(axis=0)
            den = (v[:, None] * ok_h[cand]).sum(axis=0)
            gh = np.where(den > 0, num / np.where(den > 0, den, 1), np.nan)
            tm = timing(gh, E @ ucell, first, margin)
            top = np.argsort(-v)[:5]
            idw_gauges = [{"id": uid[cand[k]], "weight": _r(v[k], 3), "gauge_mm": _r(gtot[cand[k]], 1)}
                          for k in top if v[k] > 0.005]
        else:
            idw_gauges = []
        out.append(t | {
            "area_km2": _r(cc["area_km2"], 1), "zones": zs, "cells": int(cells.size),
            "gauges_inside": [{"id": uid[k], "gauge_mm": _r(gtot[k], 1), "raw_mm": used[uid[k]]["raw_mm"]}
                              for k in inside],
            "idw_gauges": idw_gauges,
            "flags": [n for b, n in FLAG_NAMES.items() if net.flags[t["row"]] & b],
            "model": {"raw": _r(raw, 1), "in_use": _r(scaled("in_use"), 1), "window": _r(scaled("window"), 1),
                      "leo": _r(scaled("leo"), 1), "loco": _r(scaled("leo", loco), 1),
                      "event": _r(scaled("event"), 1)},
            "loco_factors": loco, "gauge": est, "timing": tm})
        log(f"catchment {t['name']} (gid {t['gid']}, {cc['area_km2']:.0f} km2): ECMWF {raw:.0f} mm, in use "
            f"{scaled('in_use'):.0f}; gauges inside {len(inside)}: {est['inside']}, idw {est['idw']}, guided "
            f"{est['guided']}")
    return out


# --- markdown ------------------------------------------------------------------------------------------------
def _f(v, p: int = 1) -> str:
    return "-" if v is None else (f"{v:.{p}f}" if isinstance(v, float) else str(v))


def _metric_rows(metrics: dict, keys) -> list[str]:
    rows = ["| variant | n | ECMWF / gauge | median ratio | bias (mm) | RMSE (mm) | MAE (mm) | RMS log |",
            "|---|---|---|---|---|---|---|---|"]
    for k in keys:
        m = metrics.get(k)
        if not m or not m.get("n"):
            continue
        rows.append(f"| {VARIANTS[k]} | {m['n']} | {_f(m['model_over_gauge'], 3)} | {_f(m['median_ratio'], 3)} | "
                    f"{_f(m['bias_mm'])} | {_f(m['rmse_mm'])} | {_f(m['mae_mm'])} | {_f(m['rms_log'], 3)} |")
    return rows


def _marks(x: dict) -> str:
    """Why a gauge lost hours: " (likely outage 72 h, under-read 40 h)", or ""."""
    m = [f"{k} {x[f]} h" for k, f in (("likely outage", "outage_hours"), ("under-read", "underread_hours")) if x.get(f)]
    return f" ({', '.join(m)})" if m else ""


def render_markdown(rep: dict) -> str:
    """The report's tables (Markdown)."""
    ev = rep["event"]
    L = [f"## Storm {ev['start']} .. {ev['end']} ({ev['hours']} h)", ""]
    ck = rep["ecmwf_check"]
    L += [f"ECMWF at the {ck['cells']} gauge cells: event store / scaling store = {_f(ck['event_over_scaling'], 4)} "
          f"(max difference {_f(ck['max_abs_mm'], 2)} mm in an hour); event store readers "
          f"{ck['event_store_readers']}.", ""]
    cl = rep.get("cleaning", {})
    zs = set(rep.get("zones", []))
    if not rep.get("underread_rule", True):
        L += ["Under-read rule (rf-t26.20) off: only the likely outages are left out.", ""]
    elif any(x.get("zone") in zs for x in cl.get("underreads_in_event", [])):
        L += ["Under-read events (rf-t26.20) in the storm, left out: " + "; ".join(
            f"{x['id']} {x['gauge_mm']:.1f} mm in {x['hours']} h ({x['from']} .. {x['to']}) against a neighbour "
            f"median of {x['neighbour_median_mm']:.1f} mm and {x['grid_mm']:.1f} mm in ECMWF"
            for x in cl["underreads_in_event"] if x.get("zone") in zs) + ".", ""]
    for z, zr in rep["zone_reports"].items():
        L += [f"### {z}", ""]
        s = zr.get("summary")
        if s:
            L += [f"{s['gauges']} gauges: mean {_f(s['mean_mm'])} mm, median {_f(s['median_mm'])}, max "
                  f"{_f(s['max_mm'])} ({s['max_gauge']}); {s['over_100mm']} over 100 mm, {s['over_200mm']} over "
                  f"200 mm. "
                  f"Wettest hour {_f(s['peak_hour_mm'])} mm ({s['peak_hour_gauge']}, hour ending {s['peak_hour']}); "
                  f"gauge mean peaks at {_f(s['mean_peak_mm'])} mm/h ({s['mean_peak_hour']}); "
                  f"{s['wet_hours_mean_ge_1mm']} h with a gauge mean >= 1 mm/h ({s['wet_from']} .. {s['wet_to']}). "
                  f"ECMWF at the same cells: mean {_f(s['ecmwf_mean_mm'])} mm, wettest cell-hour "
                  f"{_f(s['ecmwf_peak_cell_hour_mm'])} mm.", ""]
        f = zr.get("factors")
        if f:
            iu, wi, le = f["in_use"], f["window"], f["leo"]
            L += ["| factor | value | from |", "|---|---|---|",
                  f"| in use | {iu['factor']:.3f} (version {iu['version']}) | factor file, set {iu['set']} |",
                  f"| 30-day window | {wi['factor']:.3f} | {wi['why']}, {wi['gauges']} gauges |",
                  f"| window without the event | {le['factor']:.3f} | {le['why']}, {le['gauges']} gauges, "
                  f"{le['hours']} h |"]
            if "event" in f:
                L.append(f"| the event alone | {f['event']['factor']:.3f} | ratio {_f(f['event']['ratio'], 3)}, "
                         f"{f['event']['gauges']} gauges |")
            L.append("")
        ex = zr.get("excluded") or {}
        if ex:
            few = ", ".join(f"{x['id']}" + _marks(x) for x in ex.get("few_hours", []))
            L += [f"Left out: {len(ex.get('copy', []))} copies, {len(ex.get('no_cell', []))} without a location, "
                  f"{len(ex.get('few_hours', []))} with < 80 % of the hours usable" + (f" ({few})" if few else "")
                  + ".", ""]
        if zr.get("metrics"):
            L += _metric_rows(zr["metrics"], ("raw", "in_use", "window", "leo", "logo", "event")) + [""]
        tmz = zr.get("timing")
        if tmz:
            lag = "-" if tmz["best_lag_h"] is None else f"{tmz['best_lag_h']:+d}"
            L += [f"Timing, mean of {tmz['gauges']} gauges with every hour: r {_f(tmz['r0'], 2)} at lag 0, best lag "
                  f"{lag} h (r {_f(tmz['r_best'], 2)}), 3-hour sums r {_f(tmz['r3'], 2)}; peak "
                  f"{_f(tmz.get('gauge_peak_mm'))} mm/h at {tmz.get('gauge_peak_hour')} (gauges) vs "
                  f"{_f(tmz.get('model_peak_mm'))} at {tmz.get('model_peak_hour')} (ECMWF); centroid lag "
                  f"{_f(tmz.get('centroid_lag_h'), 2)} h. Per gauge: median r "
                  f"{_f(zr['timing_per_gauge']['median_r0'], 2)}, best lags "
                  f"{zr['timing_per_gauge']['best_lag_counts']}.", ""]
        rows = [r for r in rep["gauges"] if r["zone"] == z]
        if rows:
            L += ["| gauge | hours | gauge mm | ECMWF raw | gauge / raw | x in use | x no event | x no event, no gauge "
                  "| peak mm/h | r (lag 0) | best lag |", "|---|---|---|---|---|---|---|---|---|---|---|"]
            for r in rows:
                t = r["timing"]
                lag = "-" if t["best_lag_h"] is None else f"{t['best_lag_h']:+d}"
                L.append(f"| {r['id']} | {r['hours']} | {r['gauge_mm']:.1f} | {r['raw_mm']:.1f} | "
                         f"{_f(r['gauge_over_raw'], 2)} | {r['model']['in_use']:.1f} | {r['model']['leo']:.1f} | "
                         f"{r['model']['logo']:.1f} | {_f(r['gauge_peak_mm'])} | {_f(t['r0'], 2)} | {lag} |")
            L.append("")
    if rep["catchments"]:
        L += ["### Catchments", "",
              "| catchment | gid | km² | zones | gauges inside | ECMWF raw | x in use | x no event | x no event, "
              "no own gauges | inside mean | IDW | guided | r (lag 0) | best lag |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for c in rep["catchments"]:
            t = c["timing"] or {}
            zs = ", ".join(f"{k} {v:.0%}" if v >= 0.005 else f"{k} < 1 %" for k, v in c["zones"].items())
            lag = t.get("best_lag_h")
            short = (f" (area short: {_f(c['table_area_km2'], 0)} in the table)" if "area_short" in c["flags"]
                     else "")
            L.append(f"| {c['name']} | {c['gid']} | {_f(c['area_km2'], 0)}{short} | {zs} | {len(c['gauges_inside'])} | "
                     f"{_f(c['model']['raw'])} | {_f(c['model']['in_use'])} | {_f(c['model']['leo'])} | "
                     f"{_f(c['model']['loco'])} | {_f(c['gauge']['inside'])} | {_f(c['gauge']['idw'])} | "
                     f"{_f(c['gauge']['guided'])} | {_f(t.get('r0'), 2)} | {'-' if lag is None else f'{lag:+d}'} |")
        L.append("")
        for est, m in rep["catchment_metrics"].items():
            L += [f"Against the {est} estimate:", ""] + _metric_rows(m, ("raw", "in_use", "window", "leo", "loco",
                                                                           "event"))
            L.append("")
    return "\n".join(L)
