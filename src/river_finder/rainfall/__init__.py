"""Rainfall accumulation per catchment (epic rf-t26): the pieces and how data flows through them.

Modules
-------
- :mod:`.grid`: ``GridSpec`` (regular lat/lon grid: centre of the first cell, step, shape), the
  native global grids of GSMaP (cell centres on x.x5°) and ECMWF 0.1° (points on x.x0°), the hour
  index (UTC hours since the epoch, hour ending).
- :mod:`.sources`: the product table (``GSMAP_NOW``, ``GSMAP_NRT``, ``ECMWF_OBS``, ``ECMWF_FC``:
  family, kind, priority), ``HourlyField`` (mm grid + valid hour + product + run/lead time) and the
  ``RainfallSource`` interface; ``SyntheticSource`` for tests.
- :mod:`.weights`: ``build_weights`` turns the 8 m local-catchment tiles into one sparse matrix per
  grid (rows = catchments in ``segments_table.pkl`` order, columns = grid cells,
  ``W[c, g]`` = share of catchment ``c``'s area in cell ``g``); ``CatchmentWeights.mm`` /
  ``.m3`` give depth and volume per catchment.
- :mod:`.buffer`: ``HourlyBuffer``, 24 hourly slots per catchment with raw and scaled mm, product,
  source, run/lead, overwrite record and per-region scale factors; ``window(x)`` (past x hours) and
  ``ahead(x)`` (next x hours) report missing hours explicitly. ``ScaleFactors`` per family and
  region.
- :mod:`.config`: ``config/rainfall.toml`` (windows, which family feeds the observed and forecast
  metrics, paths, grids), validated.
- :mod:`.pipeline`: ``ingest(source, buffer, weights, start, end, factors)``, the hourly step.

Data flow
---------
Once per grid: ``river-finder rain weights --grid gsmap|ecmwf|all`` writes
``data/nz/rain/weights_<family>.npz`` (+ ``.json`` with the checks).

Every hour (children 2 and 3 provide the sources)::

    cfg = load_config()
    w = CatchmentWeights.load(cfg.weights_path("gsmap"))
    buf = open_buffer(cfg, "observed", "gsmap", w)
    ingest(GsmapSource(cfg.grids["gsmap"]), buf, w, start, end, factors={"gsmap": current_factors})
    buf.save(cfg.buffer_path("observed", "gsmap"))
    totals = buf.windows(cfg.windows)          # {x: WindowTotal}, mm per catchment + n_missing

- rf-t26.2 (GSMaP): subclass ``RainfallSource`` with ``family = "gsmap"``; crop the global field
  with ``cfg.grids["gsmap"].crop(values, GSMAP_GLOBAL)``; yield ``GSMAP_NOW`` then ``GSMAP_NRT``
  for an hour when both exist (NRT has the higher priority and replaces NOW in the buffer).
- rf-t26.3 (ECMWF): ``family = "ecmwf"``, grid ``ecmwf``. Observed proxy: ``ECMWF_OBS`` fields
  (short-lead hours; for the same hour a newer run replaces an older one) into the observed ecmwf
  buffer. Forecast: ``fc.advance(now_hour)``, then ``ECMWF_FC`` fields with ``run`` set into
  ``fc_ecmwf.npz``; ``fc.ahead(x)``. The 0p1 open data has 3-hourly precipitation steps to 144 h:
  split into hours (document how) or use the hourly Open-Meteo copy.
- rf-t26.5 (scaling): build ``ScaleFactors(family, buffer.regions, factor, version)`` and pass them
  to ``ingest``; ``buffer.apply_factors`` re-scales hours already held. Regions are the segment
  table's ``region`` column (river-guide regions, which follow the councils), ``buffer.region_of``.
- rf-t26.6 (upstream): ``WindowTotal.m3(w.area_m2)`` gives local volumes; add them down
  ``w.down`` in topological order; upstream mm = upstream m³ / upstream area (sum of ``w.area_m2``).
"""

from .buffer import DEFAULT_WINDOWS, SLOTS, HourlyBuffer, PutResult, ScaleFactors, WindowTotal
from .config import RainConfig, load_config
from .grid import ECMWF_GLOBAL, GSMAP_GLOBAL, GridSpec, hour_index, hour_time
from .pipeline import ingest, open_buffer
from .sources import PRODUCTS, HourlyField, Product, RainfallSource, SyntheticSource
from .weights import CatchmentWeights, build_weights

__all__ = [
    "DEFAULT_WINDOWS", "ECMWF_GLOBAL", "GSMAP_GLOBAL", "PRODUCTS", "SLOTS", "CatchmentWeights", "GridSpec",
    "HourlyBuffer", "HourlyField", "Product", "PutResult", "RainConfig", "RainfallSource", "ScaleFactors",
    "SyntheticSource", "WindowTotal", "build_weights", "hour_index", "hour_time", "ingest", "load_config",
    "open_buffer",
]
