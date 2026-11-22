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


def open_buffer(cfg: RainConfig, kind: str, family: str, weights: CatchmentWeights, zones=None,
                log=print) -> HourlyBuffer:
    """The saved buffer of ``kind`` (observed | forecast) and ``family``, or a new empty one; with ``zones``
    (a :class:`.zones.ZoneMap`) a saved buffer of other zones is moved to them (:meth:`HourlyBuffer.rezone`)."""
    p = cfg.buffer_path(kind, family)
    if p.exists():
        b = HourlyBuffer.load(p)
        b.check_rows(weights)
        if zones is not None and b.rezone(zones.names, zones.zone_of):
            new = ", ".join(z for z in zones.names if z not in weights.regions) or "one per region"
            log(f"{kind} {family} buffer: moved to the scaling zones ({len(zones.names)}; new: {new}); its hours "
                f"are re-scaled below")
        return b
    return HourlyBuffer.for_weights(weights, kind, zones)


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
    from .gauges import status_line
    from .scaling import status_lines

    log(status_line(cfg))
    for line in status_lines(cfg):
        log(line)
    tj = cfg.totals_path.with_suffix(".json")
    if not tj.exists():
        log(f"upstream totals: none yet ({cfg.totals_path.name}; river-finder rain totals)")
        return
    m = json.loads(tj.read_text())
    parts = []
    for kind, k in m.get("kinds", {}).items():
        when = k.get("window_end") if kind == "observed" else k.get("start")
        parts.append(f"{kind} {k.get('family', '')} windows {k['windows']} at {when}")
    log(f"upstream totals {m['created']}: {m['rows']} reaches; " + "; ".join(parts))


# --- ECMWF (rf-t26.3) ------------------------------------------------------------------------------
class SourceUnavailable(SystemExit):
    """No published run covers what was asked (the source is late or down), as opposed to a real error.
    The CLI exits with :data:`EXIT_UNAVAILABLE` for it and for downloads that fail after their retries."""


EXIT_UNAVAILABLE = 75  # sysexits EX_TEMPFAIL: try again later (the hourly job keeps the previous files)


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
    The observed window ends at "now": each hour from the newest of that run and the ``obs_runs - 1``
    runs before it that has it within ``obs_leads`` (see :func:`.ecmwf.obs_plan`); the forecast holds
    hours ``now+1 .. now+24`` of one run (``latest``: the newest that has them all). Buffers go to
    ``<rain>/obs_ecmwf.npz`` and ``<rain>/fc_ecmwf.npz``, each with a ``.json`` report of the last
    ingest (source, run, lead and product of every hour, missing hours, windows, download).

    Gauge scale factors (rf-t26.5, :func:`.scaling.factors_for`): with ``[scaling] apply`` on, each
    product listed in ``[scaling.ecmwf] products`` is scaled with the current ``factors_ecmwf.json``
    (unity without one), and every hour the buffer holds is re-scaled with them, so all its slots
    carry the same factor versions; the reports say which (``scale``).
    """
    from .ecmwf import EcmwfSource, Reader, fc_plan, find_run, make_reader, obs_plan, parse_run, run_label
    from .fetch import Http
    from .scaling import describe_factors, factors_for
    from .zones import load_zones

    ec = cfg.ecmwf
    w = CatchmentWeights.load(cfg.weights_path("ecmwf"))
    zones = load_zones(cfg, "ecmwf", log=log)
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
        f = factors_for(cfg, tag, buf.regions)
        res = ingest(EcmwfSource(rd, tag, plan), buf, w, hour_time(start_h), hour_time(end_h), {f.family: f})
        rescaled = buf.apply_factors(f)  # hours kept from earlier runs take the current factors too
        scale = {"apply": cfg.scaling.apply, "factors_file": cfg.factors_path("ecmwf").name
                 if cfg.factors_path("ecmwf").exists() else None, "regions": describe_factors(f),
                 "hours_rescaled": rescaled, "zones": None if zones.identity else
                 {"file": cfg.zones_path.name, "zones": list(zones.names), "rule": zones.rule()}}
        if scale["regions"]:
            log(f"  {tag} scaled with gauge factors {scale['regions']}")
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
        table = buf.slot_table()
        versions = sorted({v for r in table if r["product"] for v in r["factor_versions"]})
        rep = {"product": tag, "reader": name, "written": datetime.now(UTC).isoformat(timespec="seconds"),
               "now": _hour(now_h), **extra, "hours": hours, "missing": [_hour(h) for h in missing],
               # scaled: some hour held used gauge scale factors (version > 0, rf-t26.5); version 0 = raw
               "scaled": any(v > 0 for v in versions), "factor_versions": versions, "scale": scale,
               "windows": window_summary(buf, w, cfg.windows), "buffer": table,
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
        # the newest run whose first observed lead is out and over, however much of it is converted yet:
        # obs_plan takes the leads it lists and fills the hours after them from the runs before
        latest = explicit or find_run(rd, now, lambda r: (lo,) if hour_index(r) + lo <= now_h else (),
                                      ec.search_hours)
        if latest is None:
            raise SourceUnavailable(f"no {name} run with lead {lo} h published in the last {ec.search_hours} h")
        plan, missing, end = obs_plan(rd, latest, (lo, hi), ec.obs_runs, now_hour=now_h)
        buf = open_buffer(cfg, "observed", "ecmwf", w, zones, log=log)
        newest = sorted(lead for r, lead in plan.values() if r == latest)
        log(f"observed: ECMWF_OBS from leads {lo}-{hi} h of {ec.obs_runs} runs up to {run_label(latest)} "
            f"({f'leads {newest[0]}-{newest[-1]} of it' if newest else 'none of its leads'}); window ends "
            f"{_hour(end)} ({now_h - end} h before now)")
        if end < now_h:
            log(f"  {_hour(end + 1)} .. {_hour(now_h)}: no run publishes these hours within leads {lo}-{hi} h yet")
        report["observed"] = ingest_plan(
            "observed", "ECMWF_OBS", buf, plan, missing, end - SLOTS, end,
            {"latest_run": run_label(latest), "runs": sorted({run_label(r) for r, _ in plan.values()}),
             "leads": [lo, hi], "latest_run_leads": [newest[0], newest[-1]] if newest else None,
             "window_end": _hour(end), "hours_behind_now": now_h - end,
             "not_published": [_hour(h) for h in range(end + 1, now_h + 1)]})

    if fc:
        def need(r):
            first = max(1, now_h + 1 - hour_index(r))
            return range(first, now_h + SLOTS + 1 - hour_index(r))
        frun = explicit or find_run(rd, now, need, ec.search_hours)
        if frun is None:
            raise SourceUnavailable(f"no {name} run covering {_hour(now_h + 1)} .. {_hour(now_h + SLOTS)} "
                                    f"published in the last {ec.search_hours} h")
        plan, missing = fc_plan(rd, frun, now_h)
        buf = open_buffer(cfg, "forecast", "ecmwf", w, zones, log=log)
        held = {int(r) for r in buf.run_hour[buf.hour >= 0]}
        if buf.anchor > now_h or any(r > hour_index(frun) for r in held):
            log("forecast: the saved buffer is ahead of this request (later 'now' or newer run); starting afresh")
            buf = HourlyBuffer.for_weights(w, "forecast", zones)
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


# --- upstream totals (rf-t26.6) ---------------------------------------------------------------------
def _weights_file(cfg: RainConfig) -> Path:
    for fam in (cfg.observed_source, cfg.forecast_source, *cfg.grids):
        p = cfg.weights_path(fam)
        if p.exists():
            return p
    raise SystemExit(f"no weights_<grid>.npz in {cfg.rain_dir} (river-finder rain weights)")


def load_network(cfg: RainConfig, rebuild: bool = False, log=print):
    """``<rain>/network.npz``, rebuilt from the segment table, weights and relink logs when it is missing,
    older than the table (size or mtime differ) or for other rows than the weights. Without the table
    (an hourly job elsewhere) the saved network is used as long as its rows match the weights."""
    from .upstream import Network, network_from_files

    wpath = _weights_file(cfg)
    with np.load(wpath, allow_pickle=False) as z:
        w_digest = json.loads(str(z["meta"]))["rows_digest"]
    path, table = cfg.network_path, cfg.segments_path
    why = "rebuild asked" if rebuild else None
    if why is None and path.exists():
        net = Network.load(path)
        if net.rows_digest != w_digest:
            why = f"{path.name} rows differ from {wpath.name}"
        elif table.exists():
            st = table.stat()
            if (net.meta.get("table_size"), net.meta.get("table_mtime")) != (st.st_size, st.st_mtime):
                why = f"{table.name} changed since {path.name} was built"
        if why is None:
            log(f"network: {path} ({net.n} reaches, built {net.meta.get('built')})")
            return net
    elif why is None:
        why = f"no {path.name}"
    if not table.exists():
        raise SystemExit(f"{why}, and no {table} to build it from")
    log(f"network: building from {table.name} ({why})")
    net = network_from_files(wpath, table, cfg.nz_data / "relink_log.csv", cfg.nz_data / "unresolved_gt200.csv",
                             log=log)
    if net.rows_digest != w_digest:
        raise SystemExit(f"{table} rows differ from {wpath}: rebuild the weights (river-finder rain weights)")
    net.save(path)
    log(f"network -> {path} ({path.stat().st_size / 1e6:.1f} MB)")
    return net


def _log_area_check(c: dict, log) -> None:
    log(f"area check (upstream pixel area / table area_km2): pixel {c['pixel_km2_total']:,.0f} km2 at outlets vs "
        f"table {c['table_km2_at_outlets']:,.0f} km2; {c['edge_outlets']} tile-edge outlets hold "
        f"{c['edge_outlets_pixel_km2']:,.0f} km2 of pixels; {c['area_short_reaches']} reaches area_short "
        f"({c['area_short_reaches_gt100km2']} over 100 km2)")
    for k, v in c["by_size"].items():
        q = v["ratio_q01_q05_q25_q50_q75_q95_q99"]
        log(f"  {k:>14}: {v['reaches']:>7} reaches, ratio q01 {q[0]:.3f} q05 {q[1]:.3f} median {q[3]:.4f} "
            f"q95 {q[5]:.3f} q99 {q[6]:.3f}; within 1/5/10 %: {v['within_1pct']:.1%} / {v['within_5pct']:.1%} / "
            f"{v['within_10pct']:.1%}")


def raw_totals(cfg: RainConfig, like, log=print):
    """The unscaled sums of the current buffers, for the same windows, anchors and runs as ``like`` (an
    :class:`.upstream.UpstreamTotals` of scaled values): what ``rain publish`` publishes while
    ``[scaling] publish`` is off. Refuses buffers that changed since ``like`` was computed."""
    from .upstream import compute_totals

    net = load_network(cfg, log=log)
    if net.rows_digest != like.meta.get("rows_digest"):
        raise SystemExit(f"{cfg.network_path.name} rows differ from {cfg.totals_path.name}: run rain totals again")
    bufs = {}
    for kind, k in like.kinds.items():
        fam = k.info.get("family") or (cfg.observed_source if kind == "observed" else cfg.forecast_source)
        p = cfg.buffer_path(kind, fam)
        if not p.exists():
            raise SystemExit(f"no {p.name} to sum unscaled for {kind}: run rain ecmwf and rain totals again")
        b = HourlyBuffer.load(p)
        runs = sorted({_hour(int(r)) for r in b.run_hour[b.hour >= 0] if r >= 0})
        if _hour(b.anchor) != k.info.get("anchor") or runs != k.info.get("runs"):
            raise SystemExit(f"{p.name} changed since {cfg.totals_path.name} was computed: run rain totals again")
        bufs[kind] = b
    now = like.meta.get("now")
    now_h = hour_index(datetime.strptime(now, "%Y-%m-%d %H:00Z").replace(tzinfo=UTC)) if now else None
    tot = compute_totals(net, bufs, like.windows, scaled=False, now_hour=now_h)
    for kind, k in like.kinds.items():
        tot.kinds[kind].info.update(family=k.info.get("family"), buffer=k.info.get("buffer"))
    tot.meta["now"] = now
    tot.meta["scaled_totals"] = {"created": like.meta.get("created"), "values": like.meta.get("values")}
    return tot


def run_totals(cfg: RainConfig, windows=None, scaled: bool = True, rebuild_network: bool = False,
               now: datetime | None = None, log=print):
    """Upstream totals from the current observed and forecast buffers -> ``<rain>/totals.npz`` + ``.json``."""
    from .upstream import compute_totals

    t0 = time.perf_counter()
    net = load_network(cfg, rebuild_network, log=log)
    t_net = time.perf_counter() - t0
    flags = net.meta.get("flags") or net.flag_counts()
    log(f"network flags: {flags}")
    if "area_check" in net.meta:
        _log_area_check(net.meta["area_check"], log)
    t1 = time.perf_counter()
    bufs = {}
    for kind, fam in (("observed", cfg.observed_source), ("forecast", cfg.forecast_source)):
        p = cfg.buffer_path(kind, fam)
        if not p.exists():
            log(f"{kind}: no buffer {p.name} (river-finder rain ecmwf); skipped")
            continue
        bufs[kind] = HourlyBuffer.load(p)
    if not bufs:
        raise SystemExit(f"no buffers in {cfg.rain_dir}: run river-finder rain ecmwf first")
    t_buf = time.perf_counter() - t1
    now_h = hour_index(floor_hour(now or datetime.now(UTC)))
    xs = tuple(windows) if windows else cfg.windows
    tot = compute_totals(net, bufs, xs, scaled=scaled, now_hour=now_h)
    for kind, fam in (("observed", cfg.observed_source), ("forecast", cfg.forecast_source)):
        if kind in tot.kinds:
            tot.kinds[kind].info.update(family=fam, buffer=str(cfg.buffer_path(kind, fam)))
    t2 = time.perf_counter()
    tot.meta["seconds"].update(network_s=round(t_net, 2), buffers_s=round(t_buf, 2))
    tot.meta["now"] = _hour(now_h)
    path = tot.save(cfg.totals_path)
    t_save = time.perf_counter() - t2
    tot.meta["seconds"]["save_s"] = round(t_save, 2)
    path.with_suffix(".json").write_text(json.dumps(tot.meta, indent=1))
    sec = tot.meta["seconds"]
    for kind, k in tot.kinds.items():
        i = k.info
        when = (f"window end {i['window_end']} ({i.get('hours_behind_now')} h before now)" if kind == "observed"
                else f"from {i['start']} (run {i.get('run')}, {i.get('hours_since_start')} h ago)")
        log(f"{kind} ({i.get('family')}, products {list(k.product_tags)}, scaled={i['scaled']} (scale bits "
            f"{i.get('scale')}{', --raw' if tot.meta['values'] == 'raw' else ''})): {when}")
        for x, w in i["windows_info"].items():
            c = w["checks"]
            area = float(net.up_area_m2[net.down < 0].sum())
            mean = c["outlets_m3"] / area * 1000 if area else 0.0
            mx, mb = w["max_mm"], w["max_mm_ge_100km2"] or {}
            log(f"  {x:>2} h {w['start']} .. {w['end']}: {c['outlets_m3'] / 1e6:,.1f} Mm3 at outlets (mean "
                f"{mean:.2f} mm), max {mx['mm']:.1f} mm (gid {mx['gid']}, {mx['up_area_km2']:.2f} km2), max over "
                f">= 100 km2 {mb.get('mm', float('nan')):.1f} mm (gid {mb.get('gid')}, {mb.get('up_area_km2')} km2); "
                f"{w['reaches_incomplete']} reaches incomplete, missing hours {len(w['missing_hours'])}; "
                f"balance err {c['mass_balance_max_rel_err']:.1e}")
    log(f"totals -> {path} ({path.stat().st_size / 1e6:.1f} MB): network {sec['network_s']} s, buffers "
        f"{sec['buffers_s']} s, compute {sec['total_s']} s (accumulate {sec['accumulate_s']} s), save {t_save:.1f} s")
    return tot
