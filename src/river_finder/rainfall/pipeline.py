"""The hourly step: source fields -> mm per catchment -> buffer, and the files it keeps."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from .buffer import SLOTS, HourlyBuffer, PutResult, ScaleFactors
from .config import RainConfig
from .grid import floor_hour, hour_index, hour_time
from .sources import RainfallSource
from .weights import CatchmentWeights


def ingest(source: RainfallSource, buffer: HourlyBuffer, weights: CatchmentWeights, start: datetime, end: datetime,
           factors: dict[str, ScaleFactors] | None = None) -> list[PutResult]:
    """Put every field of ``source`` with valid hour in ``(start, end]`` into ``buffer``.

    One sparse mat-vec per hour (``weights.mm``); ``factors[family]`` scales the raw values (unity
    if absent). A forecast buffer must already be anchored (``buffer.advance(now)``).
    """
    if not source.grid.same_cells(weights.grid):
        raise ValueError(f"source grid {source.grid.name} != weights grid {weights.grid.name}")
    buffer.check_rows(weights)
    out = []
    for f in source.fields(start, end):
        fam = f.info.family
        out.append(buffer.put(f.hour, weights.mm(f), f.product, f.source, run_hour=f.run_hour,
                              lead=f.lead_hours if f.lead_hours is not None else -1,
                              factors=(factors or {}).get(fam)))
    return out


def open_buffer(cfg: RainConfig, kind: str, family: str, weights: CatchmentWeights) -> HourlyBuffer:
    """The saved buffer of ``kind`` (observed | forecast) and ``family``, or a new empty one."""
    p = cfg.buffer_path(kind, family)
    if p.exists():
        b = HourlyBuffer.load(p)
        b.check_rows(weights)
        return b
    return HourlyBuffer.for_weights(weights, kind)


# --- CLI steps ----------------------------------------------------------------------------------
TABLE_COLUMNS = ["gid", "down", "area_km2", "region", "lon", "lat"]


def run_weights(cfg: RainConfig, families: list[str], log=print) -> dict[str, Path]:
    """Build the weight matrices of ``families`` in one pass over the tiles; write .npz + .json."""
    import pandas as pd

    from .weights import build_weights, tile_paths

    table = pd.read_pickle(cfg.segments_path)[TABLE_COLUMNS]
    paths = tile_paths(cfg.tiles_dir)
    if not paths:
        raise SystemExit(f"no */local_catchment_core.tif under {cfg.tiles_dir}")
    log(f"{len(table)} catchments, {len(paths)} tiles, grids {', '.join(families)}")
    built, stats = build_weights(paths, table, [cfg.grids[f] for f in families], log=log)
    out = {}
    for name, cw in built.items():
        p = cw.save(cfg.weights_path(name))
        cw.meta["file_bytes"] = p.stat().st_size
        p.with_suffix(".json").write_text(json.dumps(cw.meta, indent=1))
        c = cw.meta["checks"]
        log(f"{name}: {p} ({p.stat().st_size / 1e6:.1f} MB), {c['rows']} rows, nnz {c['nnz']}, "
            f"max |row sum - 1| {c['weight_sum_max_abs_dev']:.1e}, {c['missing_no_pixels']} without pixels, "
            f"interpolation error <= {cw.meta['interp_max_error_m'] * 1000:.1f} mm")
        out[name] = p
    log(f"build {stats['build_seconds']} s")
    return out


def status(cfg: RainConfig, log=print) -> None:
    """Config, weight files and buffers at a glance."""
    log(f"windows {list(cfg.windows)} h; observed <- {cfg.observed_source} (compare {list(cfg.compare)}); "
        f"forecast <- {cfg.forecast_source}; data {cfg.rain_dir}")
    for name, g in cfg.grids.items():
        p = cfg.weights_path(name)
        head = f"grid {name}: {g.nx}x{g.ny} cells from ({g.lon0}, {g.lat0}) step {g.dlon}"
        if not p.with_suffix(".json").exists():
            log(f"{head}; weights not built (river-finder rain weights --grid {name})")
            continue
        m = json.loads(p.with_suffix(".json").read_text())
        c = m["checks"]
        log(f"{head}; weights {m['built']}: {c['rows']} rows, nnz {c['nnz']}, {m.get('file_bytes', 0) / 1e6:.1f} MB, "
            f"{c['missing_no_pixels']} catchments without pixels")
    for kind, fams in (("observed", cfg.observed_families), ("forecast", (cfg.forecast_source,))):
        for fam in fams:
            p = cfg.buffer_path(kind, fam)
            if not p.exists():
                log(f"{kind} {fam}: no buffer yet ({p.name})")
                continue
            b = HourlyBuffer.load(p)
            rows = b.slot_table()
            held = [r for r in rows if r["product"]]
            prods = {}
            for r in held:
                prods[r["product"]] = prods.get(r["product"], 0) + 1
            span = f"{rows[0]['hour']} .. {rows[-1]['hour']}" if rows else "empty"
            runs = sorted({r["run"] for r in held if r.get("run")})
            age = ""
            if runs and kind == "forecast":
                newest = max(int(h) for h in b.run_hour[b.hour >= 0])
                age = f", {hour_index(floor_hour(datetime.now(UTC))) - newest} h old now"
            log(f"{kind} {fam}: {span}, {len(held)}/{len(rows)} hours held {prods}"
                + (f", runs {runs}{age}" if runs else "")
                + f", missing {[r['hour'] for r in rows if not r['product']]}")


# --- ECMWF (rf-t26.3) ------------------------------------------------------------------------------
def _hour(h: int) -> str:
    return hour_time(h).strftime("%Y-%m-%d %H:00Z")


def window_summary(buf: HourlyBuffer, w: CatchmentWeights, xs) -> dict:
    """Per window: national area-weighted mean and max depth, volume, completeness and products."""
    out = {}
    area = w.area_m2
    for x, t in buf.windows(xs).items():
        m3 = t.m3(area)
        out[str(x)] = {"first": _hour(t.first_hour - 1), "last": _hour(t.last_hour),
                       "mean_mm": round(float(m3.sum() / area.sum() * 1000.0), 3),
                       "max_mm": round(float(np.nanmax(t.mm)), 2) if t.mm.size else 0.0,
                       "volume_m3": float(m3.sum()), "catchments_incomplete": int(np.count_nonzero(t.n_missing)),
                       "missing_hours": [_hour(h) for h in t.missing_hours], "products": t.products}
    return out


def run_ecmwf(cfg: RainConfig, run: str = "latest", obs: bool = True, fc: bool = True, reader=None,
              now: datetime | None = None, cache: bool = True, http=None, log=print) -> dict:
    """Fill the observed (``ECMWF_OBS``) and forecast (``ECMWF_FC``) ECMWF buffers from one run.

    ``run`` is ``latest`` (the newest published run that covers what is asked) or ``YYYYMMDDHH``.
    The observed window takes the short leads of that run and the ``obs_runs - 1`` runs before it;
    the forecast holds hours ``now+1 .. now+24`` of that run. Buffers go to
    ``<rain>/obs_ecmwf.npz`` and ``<rain>/fc_ecmwf.npz``, each with a ``.json`` report of the last
    ingest (source, run, lead and product of every hour, missing hours, windows, download).
    """
    from .ecmwf import EcmwfSource, Reader, fc_plan, find_run, make_reader, obs_plan, parse_run, run_label
    from .fetch import Http

    ec = cfg.ecmwf
    w = CatchmentWeights.load(cfg.weights_path("ecmwf"))
    if isinstance(reader, Reader):  # tests pass a ready reader
        rd, name = reader, reader.name
        http = reader.bucket.http
    else:
        name = reader or ec.reader
        http = http or Http(log=log)
        rd = make_reader(name, cfg.grids["ecmwf"], http=http, cache_dir=cfg.cache_dir if cache else None,
                         keep_days=ec.keep_days, ecmwf_urls=ec.ecmwf_urls, openmeteo_url=ec.openmeteo_url, log=log)
    now = floor_hour(now or datetime.now(UTC))
    now_h = hour_index(now)
    explicit = None if run == "latest" else parse_run(run)
    if explicit is not None and not rd.leads(explicit):
        raise SystemExit(f"run {run_label(explicit)} is not published on {name}")
    lo, hi = ec.obs_leads
    log(f"ECMWF reader {name}; now {_hour(now_h)}; run {run}; weights {w.n} catchments")
    report = {}

    def ingest_plan(kind: str, tag: str, buf: HourlyBuffer, plan, missing, start_h: int, end_h: int, extra: dict):
        b0, s0, t0 = http.stats.bytes, http.stats.requests, time.monotonic()
        res = ingest(EcmwfSource(rd, tag, plan), buf, w, hour_time(start_h), hour_time(end_h))
        hours = []
        for r in res:
            r_run, lead = plan[r.hour]
            how = rd.hourly(r_run, lead)[1]  # cached: no download
            hours.append({"hour": _hour(r.hour), "product": tag, "source": name, "run": run_label(r_run),
                          "lead": lead, "method": how, "status": r.status})
            log(f"  {_hour(r.hour)} {tag} {name} run {run_label(r_run)} lead {lead:3d} ({how}): {r.status}"
                + (f" (was {r.previous})" if r.previous and r.status != "updated" else ""))
        for h in missing:
            log(f"  {_hour(h)} {tag}: no run covers this hour")
        path = buf.save(cfg.buffer_path(kind, "ecmwf"))
        rep = {"product": tag, "reader": name, "written": datetime.now(UTC).isoformat(timespec="seconds"),
               "now": _hour(now_h), **extra, "hours": hours, "missing": [_hour(h) for h in missing],
               "windows": window_summary(buf, w, cfg.windows), "buffer": buf.slot_table(),
               "download": {"bytes": http.stats.bytes - b0, "requests": http.stats.requests - s0,
                            "seconds": round(time.monotonic() - t0, 1)}}
        path.with_suffix(".json").write_text(json.dumps(rep, indent=1))
        d = rep["download"]
        log(f"{kind} -> {path} ({len(hours)} hours written, {len(missing)} missing; "
            f"{d['bytes'] / 1e6:.1f} MB in {d['requests']} requests, {d['seconds']} s)")
        for x, s in rep["windows"].items():
            log(f"  {x:>2} h {s['first']} .. {s['last']}: mean {s['mean_mm']:.2f} mm, max {s['max_mm']:.1f} mm, "
                f"{s['volume_m3'] / 1e6:.1f} Mm3, {s['catchments_incomplete']} catchments incomplete")
        return rep

    if obs:
        latest = explicit or find_run(rd, now, lambda r: range(lo, hi + 1), ec.search_hours)
        if latest is None:
            raise SystemExit(f"no {name} run with leads {lo}-{hi} h published in the last {ec.search_hours} h")
        plan, missing, end = obs_plan(rd, latest, (lo, hi), ec.obs_runs)
        buf = open_buffer(cfg, "observed", "ecmwf", w)
        log(f"observed: ECMWF_OBS from leads {lo}-{hi} h of {ec.obs_runs} runs up to {run_label(latest)}; "
            f"window ends {_hour(end)} ({now_h - end} h before now)")
        report["observed"] = ingest_plan(
            "observed", "ECMWF_OBS", buf, plan, missing, end - SLOTS, end,
            {"latest_run": run_label(latest), "runs": sorted({run_label(r) for r, _ in plan.values()}),
             "leads": [lo, hi], "window_end": _hour(end), "hours_behind_now": now_h - end})

    if fc:
        def need(r):
            first = max(1, now_h + 1 - hour_index(r))
            return range(first, now_h + SLOTS + 1 - hour_index(r))
        frun = explicit or find_run(rd, now, need, ec.search_hours)
        if frun is None:
            raise SystemExit(f"no {name} run covering {_hour(now_h + 1)} .. {_hour(now_h + SLOTS)} "
                             f"published in the last {ec.search_hours} h")
        plan, missing = fc_plan(rd, frun, now_h)
        buf = open_buffer(cfg, "forecast", "ecmwf", w)
        held = {int(r) for r in buf.run_hour[buf.hour >= 0]}
        if buf.anchor > now_h or any(r > hour_index(frun) for r in held):
            log("forecast: the saved buffer is ahead of this request (later 'now' or newer run); starting afresh")
            buf = HourlyBuffer.for_weights(w, "forecast")
        buf.advance(now_h)
        age = (now - frun) / timedelta(hours=1)
        log(f"forecast: ECMWF_FC from run {run_label(frun)} ({age:.0f} h old), hours {_hour(now_h + 1)} .. "
            f"{_hour(now_h + SLOTS)} = leads {now_h + 1 - hour_index(frun)}-{now_h + SLOTS - hour_index(frun)}")
        report["forecast"] = ingest_plan("forecast", "ECMWF_FC", buf, plan, missing, now_h, now_h + SLOTS,
                                         {"run": run_label(frun), "run_age_hours": age})
    report["download"] = http.stats.to_dict()
    log(f"download total: {http.stats.bytes / 1e6:.1f} MB in {http.stats.requests} requests "
        f"({http.stats.retries} retries), {http.stats.seconds:.1f} s on the wire; by host {http.stats.by_host}")
    return report
