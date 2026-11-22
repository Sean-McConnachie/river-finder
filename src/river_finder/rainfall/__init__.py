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
- :mod:`.upstream` (rf-t26.6): ``Network`` (the reach forest in topological order, pixel areas,
  viewer index, flags for outlets the tile edges cut off) and ``UpstreamTotals``: per reach and
  window, upstream volume (m³) and mean depth (mm) summed over every local catchment upstream, with
  the missing hours, products and scale factors that reached it; method, flags and checks in its
  docstring.
- :mod:`.hilltop` / :mod:`.ecanportal` / :mod:`.gauges` (rf-t26.4): Hilltop Server client (site
  lists, measurement discovery, the per-site rain series nearest to now, GetData, quality series;
  NZST / NZ wall-clock times to UTC; WCRC, TDC), ECan's Open Data Portal (site list, hourly totals)
  and the gauge ingestion: records summed into hours ending with a state (good / raw / suspect / missing) and flags per
  hour, each gauge's NZTM position, ECMWF cell and scaling region, a per-site cache under
  ``<rain>/gauges/`` and ``gauges_hourly.npz`` (``GaugeHours``) for rf-t26.5; ``docs/rain-gauges.md``.
- :mod:`.scaling` (rf-t26.5, rf-t26.13): gauge scale factors per source family and region: the
  grid's hourly mm at the gauge cells (``CellStore``, backfilled by the observed proxy's rule),
  copies of one station counted once (``find_copies``), ratio of sums over 30 days with guard rails
  (``evaluate_region``, ``decide``), ``factors_<family>.json`` + an append-only version history, and
  ``factors_for(cfg, product, regions)`` for the buffers; ``docs/rainfall-scaling.md``.
- :mod:`.live` (rf-t26.9): the hourly GitHub Actions job's fixed inputs (``rain inputs``: weights,
  network, viewer map, manifest; the ``rain-inputs`` branch) and the files it publishes (``rain
  publish``: ``index.json`` + one ``<region>.bin.gz`` per site region; the ``rain`` branch). The
  file format, the contract for the map layer, is in its docstring; ``docs/live-rain.md`` has the job.

Data flow
---------
Once per grid: ``river-finder rain weights --grid gsmap|ecmwf|all`` writes
``data/nz/rain/weights_<family>.npz`` (+ ``.json`` with the checks).

ECMWF, every few hours (a run comes every 6 h): ``river-finder rain ecmwf`` fills
``obs_ecmwf.npz`` (``ECMWF_OBS``: the 24 h up to now, each hour from the newest of the latest 6 runs
that has it within leads 1-18 h: leads 1-6 of the older runs, the latest run's own leads up to now)
and ``fc_ecmwf.npz`` (``ECMWF_FC``: hours now+1 .. now+24 of the newest run that covers them), each
with a ``.json`` report giving the source, run, lead and product of every hour, the run's age and
the download. Re-running replaces hours, never adds to them; decoded fields are cached under
``data/nz/rain/cache/``. ``--run YYYYMMDDHH`` picks a run, ``--obs`` / ``--fc`` one buffer,
``--reader`` the other reader.

After each fill: ``river-finder rain totals`` adds the observed and forecast windows down the river
network into ``totals.npz`` (+ ``.json``; ``--windows`` for any of 1..24 h, ``--raw`` for unscaled
values), from ``network.npz``, which it builds from ``segments_table.pkl`` when the table changes
(~10 s in all, ~1.5 s of it computing)::

    t = UpstreamTotals.load(cfg.totals_path)
    t.mm("observed", 24); t.m3("forecast", 6); t.at(row); t.for_gids(gids)
    for region, rows in t.viewer_rows().items():   # vi >= 0, sorted by vi
        t.frame(rows)                              # obs_mm_24h, fc_miss_hours_6h, ... one row per reach

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
- rf-t26.4 (gauges): ``river-finder rain gauges`` fills ``<rain>/gauges/``; ``GaugeHours.load(...)``
  gives hours x gauges (``mm``, ``state``, ``ok``), ``window(hours=720)``, ``region_rows()`` (the
  network's region names) and each gauge's ``cell`` on the ECMWF grid.
- rf-t26.5 (scaling, done): ``river-finder rain scale`` writes ``<rain>/scaling/factors_ecmwf.json``;
  ``rain ecmwf`` passes ``factors_for(cfg, tag, buffer.regions)`` (a ``ScaleFactors``) to ``ingest``
  and re-scales the hours held with ``buffer.apply_factors``. Regions are the segment table's
  ``region`` column (river-guide regions, which follow the councils), ``buffer.region_of``; the
  gauges' ``region_of`` uses the same names. ``rain publish`` publishes scaled values only with
  ``[scaling] publish = true`` (rf-t26.14).
- rf-t26.6 (upstream, done): ``compute_totals(net, {"observed": obs, "forecast": fc}, windows)``
  sums ``WindowTotal.m3(area_m2)`` down ``net.down`` in topological order; upstream mm = upstream m³ /
  upstream pixel area. Tile-edge outlets (rf-t26.11) end their totals and are flagged.
- rf-t26.9 (publishing, done): per region, ``rows = t.viewer_rows()[region]`` lists the viewer
  reaches in ``vi`` order, so ``t.mm(kind, x)[rows]`` is indexed by ``vi`` directly; ``live.publish``
  packs them per region; ``.github/workflows/rain.yml`` runs ``rain ecmwf``, ``rain totals`` and ``rain
  publish`` every hour from the ``rain-inputs`` branch (``rain ecmwf`` exits 75 when ECMWF is late or
  unreachable, and the job then keeps the last files).
"""

from .buffer import DEFAULT_WINDOWS, SLOTS, HourlyBuffer, PutResult, ScaleFactors, WindowTotal
from .config import EcmwfConfig, RainConfig, load_config
from .grid import ECMWF_GLOBAL, GSMAP_GLOBAL, GridSpec, hour_index, hour_time
from .pipeline import ingest, open_buffer
from .sources import PRODUCTS, HourlyField, Product, RainfallSource, SyntheticSource
from .upstream import KindTotals, Network, UpstreamTotals, compute_totals, topo_order
from .weights import CatchmentWeights, build_weights

__all__ = [
    "DEFAULT_WINDOWS", "ECMWF_GLOBAL", "GSMAP_GLOBAL", "PRODUCTS", "SLOTS", "CatchmentWeights", "EcmwfConfig",
    "GridSpec", "HourlyBuffer", "HourlyField", "KindTotals", "Network", "Product", "PutResult", "RainConfig",
    "RainfallSource", "ScaleFactors", "SyntheticSource", "UpstreamTotals", "WindowTotal", "build_weights",
    "compute_totals", "hour_index", "hour_time", "ingest", "load_config", "open_buffer", "topo_order",
]
