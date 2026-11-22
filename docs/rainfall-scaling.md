# Rain gauge scaling (rf-t26.5, rf-t26.13)

The ECMWF rain the site shows (the observed proxy and the next-24-h forecast, `docs/live-rain.md`)
comes from a 9 km model. Over steep terrain it misses part of the orographic rain, and elsewhere it can
over-forecast. Per scaling region, the gauges of the regional councils (`docs/rain-gauges.md`) say by how
much: over the last 30 days, the factor is the gauge rain divided by the ECMWF rain in the gauges' 0.1°
cells. Local totals are scaled by it. The published files are not scaled yet (see "Publishing").

Code: `src/river_finder/rainfall/scaling.py` (its docstring is the reference). Config: `[scaling]` and
`[scaling.ecmwf]` in `config/rainfall.toml`. Tests: `tests/test_rainfall_scaling.py`.

```bash
uv run river-finder rain gauges          # gauge hours (incremental, ~6 min)
uv run river-finder rain scale           # cell store, copies, factors; re-scales the saved buffers
uv run river-finder rain ecmwf           # new buffers, scaled with the current factors
uv run river-finder rain totals          # local upstream totals (scaled)
uv run river-finder rain status          # includes the factors and the cell store
uv run river-finder rain scale --days 30 --now 2026112203 --force --no-fetch   # options
```

## Method

1. **Window.** The 30 x 24 whole UTC hours (hour ending) up to "now", or up to the newest gauge hour
   when the gauge cache is older (`[scaling] days`).
2. **Gauge side.** `gauges_hourly.npz`. A gauge hour counts when its state is good or raw
   (`state <= 1`: suspect and missing hours are left out).
3. **Copies of one station (rf-t26.13).** Some stations are in the files of two councils. Gauges
   within 3 km of each other (`dedupe_km`) are compared over every hour the gauge cache holds. If at least 10 of
   their common usable hours have rain at either gauge (`dedupe_min_wet_hours`), and at least 95 % of those hours
   agree within 0.05 mm (`dedupe_equal_share`, `dedupe_equal_mm`), the two gauges are copies. Groups
   of copies count as one station. The copy kept is the one with more usable hours in the window.
   On a tie, the kept copy is the one whose council's home region (`[gauges.councils.*] region`)
   holds the gauge, then the one with more quality-coded (good) hours, then the lower id. Each group
   records the kept copy, the dropped copies, the reason and the pairwise checks. Distinct gauges
   within 100 m (`colocated_m`) are listed with the hourly lag that matches them best. Both of them
   count.
4. **Grid side.** `<rain>/scaling/ecmwf_cells.npz` holds ECMWF's hourly mm at every gauge cell (189
   cells) for `keep_days` (40). Each run of `rain scale` fills it incrementally with the rule of the
   observed proxy: each hour comes from the newest run that has published it within `[ecmwf]
   obs_leads` (1-18 h). So settled hours come from leads 1-6, and an hour first taken at a longer
   lead (the newest hours) is replaced when the next run has it. For each run, the readers are tried
   in `[scaling.ecmwf] readers` order: Open-Meteo first (the reader of the live buffers), then ECMWF's
   own 0.1° GRIB files on AWS (3-hourly steps split evenly into hours: exact over 30 days). Each
   value records its reader, run and lead. A gauge is a point and a cell is 0.1°, so the factor is
   a regional ratio of sums, never a per-gauge correction.
5. **Factor.** Per region, the usable gauges are the gauges that are not a dropped copy, have a cell
   and have at least 80 % (`min_usable`) of the window's hours both usable at the gauge and valued at
   the cell. Then

       ratio = sum(gauge mm) / sum(ECMWF mm at the gauges' cells)

   over exactly those hours (one sum over all gauges and hours, so the wet gauges weigh most).
6. **Guard rails**, in order:
   - the cell store covers less than `min_usable` of the window (ECMWF unreachable): nothing changes;
   - fewer than 3 usable gauges (`min_gauges`): factor 1, version 0;
   - mean total below 30 mm per gauge at the gauges or in the grid (`min_gauge_mm`, `min_grid_mm`): the
     current factor is held (a dry month says nothing about the bias);
   - the ratio is clamped to [0.5, 2.0] (`clamp`);
   - one update moves the factor by at most x1.25 or /1.25 (`max_step`), so a new ratio takes a few
     days to reach;
   - a change below 2 % (`min_change`) keeps the current factor and version;
   - a region gets at most one new version per 20 h (`min_update_hours`; `--force` overrides), so an
     hourly run does not move the factor every hour.
7. **Checks** reported with every region, but not used: the median and quartiles of the per-gauge
   ratios (gauges with at least half of `min_grid_mm` in their cell), and the ratio of sums without the
   10 % highest and lowest per-gauge ratios (10 or more gauges). Also the sums per council.

Regions are those of the reach network (the river-guide regions of the segment table, which follow
the councils: `buffer.region_of`). The gauges' regions use the same names (`docs/rain-gauges.md`,
"Regions"). Regions with no gauges (the North Island, Otago, Southland, ...) stay at factor 1, version 0.
The factor applies to every catchment of its region, upstream of the gauges or not.

## Versions, storage and traceability

- `<rain>/scaling/factors_ecmwf.json`: per region the current `factor`, `version`, `set` (when),
  `status` (`scaled`, `updated`, `held`, `unity`, `reset`), `reason` and `record` (the history line
  behind it). `last_update` holds the newest evaluation of every region: the gauges used with their
  cell, hours, gauge and ECMWF sums and ratio, the gauges left out and why, the sums per council, the
  checks, the guard rails that acted, the window, the config, the input files (gauge file time, cell
  store span and readers) and the copy groups.
- `<rain>/scaling/factors_ecmwf.history.jsonl`: append-only, one line per new version: `seq`, region,
  factor, version, the previous factor and version, status, reason, window (first and last hour),
  the full evaluation (every gauge id with its cell, hours and sums), the copies dropped in the
  region, the config and the inputs. So any factor traces back to its gauges and hours.
- Versions change only with the factor. Version 0 always means unscaled (factor 1). A region reset to
  unity gets version 0, and its next factor gets `max_version + 1`, so versions above 0 are unique per region.
- Each buffer slot stores raw mm, scaled mm, and the factor and version per region
  (`HourlyBuffer.factor`, `.factor_version`; `slot_table()` lists the versions per hour). The upstream
  totals carry the scale bits per reach (1 raw, 2 scaled, 3 mixed) and the factor versions per window.

## Applying

- `rain ecmwf` scales every product listed in `[scaling.ecmwf] products` with the current factors,
  when `[scaling] apply = true` (the default). It also re-scales the hours that the buffer kept from
  earlier runs, so all slots of a buffer carry the same versions. The `.json` report gives `scale`
  (the factor file, the factors used, hours re-scaled).
- `rain scale` re-scales the saved buffers when it is done, so `rain totals` sees new factors at once.
- No factor file, a product that is not listed, or `apply = false`: factor 1, version 0 (raw).
- **Forecasts get the same factor.** `ECMWF_FC` is the same model as the observed proxy. The bias that
  the gauges measure is mostly the model's grid and orography, and that does not depend on the lead
  at these short leads. `products = ["ECMWF_OBS"]` limits the factor to the observed proxy. The factor
  file lists the products it applies to, and each buffer's report says if it was scaled.

## Publishing (rf-t26.14)

WCRC and TDC have not stated a licence for their gauge data (ECan's is CC BY 4.0). So
`[scaling] publish = false` until they do. `rain publish` then publishes the raw sums. If the local
totals are scaled, it computes the raw sums again from the same buffers (it refuses buffers that
changed after `rain totals`). `index.json` says so:

```json
"observed": {"scaled": false, "scale": "raw", ...},
"totals": {"scaled": false, "values": "raw", ...},
"scaling": {"publish": false, "apply": true, "scaled": false, "withheld": true,
            "note": "gauge-scaled values are computed locally but not published: ..."}
```

With `publish = true`, the scaled sums are published. `scaled` is `true`, `scale` is `scaled` or
`mixed`, the region files' scale bits are 2 in scaled regions, and `scaling.factors.ecmwf` lists the
factor, version and time per region with the window. The councils whose gauges are behind a published
factor are added to `licences` and to the attribution.

## Duplicates and co-located gauges (rf-t26.13)

PLACEHOLDER_DEDUPE

## Current factors

PLACEHOLDER_FACTORS

## The grid side: how far back the sources go

PLACEHOLDER_SOURCES

## Update schedule and the hourly job

PLACEHOLDER_SCHEDULE
