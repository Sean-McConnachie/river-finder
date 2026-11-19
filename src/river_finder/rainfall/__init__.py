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
  metrics, paths, grids, ``[ecmwf]`` reader and leads), validated.
- :mod:`.pipeline`: ``ingest(source, buffer, weights, start, end, factors)``, the hourly step;
  ``run_ecmwf`` (the ``rain ecmwf`` step) and ``status``.
- :mod:`.fetch`: HTTP byte ranges from public buckets with retries (S3 ``503 SlowDown``), mirror
  fallback and a download tally; ``RangeFile`` lets ``omfiles`` read a remote ``.om`` file.
- :mod:`.ecmwf` (rf-t26.3): ECMWF IFS hourly rain on the 0.1° ECMWF grid from either reader,
  ``openmeteo-s3`` (Open-Meteo's hourly copy, O1280 regridded; default) or ``ecmwf-s3`` (0.1° GRIB
  ``tp`` by range request, 3-hourly steps split evenly); the time steps found, the comparison
  behind the choice, which leads make ``ECMWF_OBS`` and how ``ECMWF_FC`` is filled are in its
  docstring.

Data flow
---------
Once per grid: ``river-finder rain weights --grid gsmap|ecmwf|all`` writes
``data/nz/rain/weights_<family>.npz`` (+ ``.json`` with the checks).

ECMWF, every few hours (a run comes every 6 h): ``river-finder rain ecmwf`` fills
``obs_ecmwf.npz`` (``ECMWF_OBS``: leads 1-6 h of the latest run and the 4 before it, the newest
run per hour) and ``fc_ecmwf.npz`` (``ECMWF_FC``: hours now+1 .. now+24 of the newest run that
covers them), each with a ``.json`` report giving the source, run, lead and product of every hour,
the run's age and the download. Re-running replaces hours, never adds to them; decoded fields are
cached under ``data/nz/rain/cache/``. ``--run YYYYMMDDHH`` picks a run, ``--obs`` / ``--fc`` one
buffer, ``--reader`` the other reader.

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
- rf-t26.3 (ECMWF, done): ``EcmwfSource`` (``family = "ecmwf"``, grid ``ecmwf``) yields
  ``ECMWF_OBS`` into the observed ecmwf buffer, next to the satellite buffer (IMERG, replacing
  GSMaP) for comparison through the same interface, and ``ECMWF_FC`` into ``fc_ecmwf.npz``
  (``fc.advance(now)``, then ``fc.ahead(x)`` for x = 1..24, ``.m3(w.area_m2)`` for volumes).
- rf-t26.5 (scaling): build ``ScaleFactors(family, buffer.regions, factor, version)`` and pass them
  to ``ingest``; ``buffer.apply_factors`` re-scales hours already held. Regions are the segment
  table's ``region`` column (river-guide regions, which follow the councils), ``buffer.region_of``.
- rf-t26.6 (upstream): ``WindowTotal.m3(w.area_m2)`` gives local volumes; add them down
  ``w.down`` in topological order; upstream mm = upstream m³ / upstream area (sum of ``w.area_m2``).
"""

from .buffer import DEFAULT_WINDOWS, SLOTS, HourlyBuffer, PutResult, ScaleFactors, WindowTotal
from .config import EcmwfConfig, RainConfig, load_config
from .grid import ECMWF_GLOBAL, GSMAP_GLOBAL, GridSpec, hour_index, hour_time
from .pipeline import ingest, open_buffer
from .sources import PRODUCTS, HourlyField, Product, RainfallSource, SyntheticSource
from .weights import CatchmentWeights, build_weights

__all__ = [
    "DEFAULT_WINDOWS", "ECMWF_GLOBAL", "GSMAP_GLOBAL", "PRODUCTS", "SLOTS", "CatchmentWeights", "EcmwfConfig",
    "GridSpec", "HourlyBuffer", "HourlyField", "Product", "PutResult", "RainConfig", "RainfallSource", "ScaleFactors",
    "SyntheticSource", "WindowTotal", "build_weights", "hour_index", "hour_time", "ingest", "load_config",
    "open_buffer",
]
