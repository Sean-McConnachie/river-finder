"""The hourly step: source fields -> mm per catchment -> buffer, and the files it keeps."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .buffer import HourlyBuffer, PutResult, ScaleFactors
from .config import RainConfig
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
            log(f"{kind} {fam}: {span}, {len(held)}/{len(rows)} hours held {prods}, "
                f"missing {[r['hour'] for r in rows if not r['product']]}")
