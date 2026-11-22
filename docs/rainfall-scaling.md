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
   within 3 km of each other (`dedupe_km`) are compared over every hour the gauge cache holds. If at
   least 10 of their common usable hours have rain at either gauge (`dedupe_min_wet_hours`), and at
   least 95 % of those hours agree within 0.05 mm (`dedupe_equal_share`, `dedupe_equal_mm`), the two
   gauges are copies. Groups
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
   own 0.1° GRIB files on AWS (3-hourly steps split evenly into hours, which only matters at the
   window's edges and at gauge hours left out). Each
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
"Regions"). Regions without gauge councils yet (the North Island, Southland, ...) and regions with
fewer than 3 gauges (Marlborough and Otago now) stay at factor 1, version 0. The factor applies to
every catchment of its region, upstream of the gauges or not.

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
  unity gets version 0, and its next factor gets `max_version + 1`, so versions above 0 are unique per
  region.
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
`mixed`, and `scaling.factors.ecmwf` lists the factor, version and time per region with the window.
The region files' scale bits are 2 in scaled regions, 1 in regions at version 0, and 3 (mixed) where
a reach's upstream catchment crosses from one into the other (live: 610 West Coast reaches, 1631
Canterbury, 1590 Otago). The councils whose gauges are behind a published
factor are added to `licences` and to the attribution.

## Duplicates and co-located gauges (rf-t26.13)

Live, 2026-11-22 (gauge cache to 03:00Z, series compared over all 846 cached hours): 22 pairs within
3 km were compared and 8 stations have two copies. The 7 gauges that WCRC and TDC both publish and
WCRC's Arthurs Pass EWS (ECan's ESNZ station) are each counted once:

| kept | dropped | region | apart (m) | wet hours | equal | totals (mm) | why |
|---|---|---|---|---|---|---|---|
| ecan:Arthur's Pass EWS (ESNZ) | wcrc:Arthurs Pass EWS | Canterbury | 0.5 | 226 | 100 % | 621.4 / 621.4 | more usable hours in the window (720 vs 719) |
| tdc:HY Aorere at Perry Saddle | wcrc:Aorere Rv @ Perry Saddle | Tasman | 2392 | 205 | 100 % | 533.0 / 533.0 | same usable hours; TDC is the home council of Tasman |
| tdc:HY Cobb at Trilobite | wcrc:Cobb @ Trilobite | Tasman | 419 | 117 | 100 % | 212.5 / 212.5 | same usable hours; TDC's home region |
| tdc:HY Matakitaki at Horse Terrace Br | wcrc:Matakitaki @ Horse Terrace Br | Tasman | 47 | 97 | 99 % | 176.5 / 173.5 | same usable hours; TDC's home region |
| wcrc:Karamea Rv @ Garibaldi | tdc:HY Karamea at Garibaldi | West Coast | 15 | 178 | 100 % | 440.0 / 440.0 | more usable hours (720 vs 719) |
| wcrc:Maruia Rv @ Shenandoah Cableway | tdc:HY Maruia at Shenandoah | Tasman | 97 | 84 | 100 % | 163.0 / 163.0 | more usable hours (720 vs 717) |
| wcrc:Mokihinui Rv @ Stoney Ck | tdc:HY Mokihinui at Stoney Ck | West Coast | 1047 | 160 | 100 % | 353.5 / 353.5 | same usable hours; WCRC is the home council of the West Coast |
| wcrc:Wangapeka @ Biggs Top | tdc:HY Wangapeka at Biggs Tops | West Coast | 260 | 159 | 100 % | 384.0 / 384.0 | same usable hours; WCRC's home region |

"apart" is the distance between the two councils' coordinates for the same gauge: up to 2.4 km, so
a distance test alone would need a large radius. The test on the series separates the cases cleanly.
Copies agree in 99-100 % of their wet hours. Distinct gauges at the same site agree in at most 53 %.
The next-closest distinct gauges in the cache (Richmond Weather at Race Course and at TDC Roof, 1.6
km apart, r 0.99) agree in 11 %.

Distinct gauges within 100 m (`last_update.copies.colocated`), all counted:

- **ECan's own `Arthur's Pass` runs 1 h ahead of the ESNZ station** 14 m away (r 0.99 with the
  ESNZ series shifted 1 h, 0.92 as stamped; 636.5 vs 621.4 mm). This is the open point of
  `docs/rain-gauges.md`. A 1 h offset does not change a 30-day total. The two are different
  instruments (2 % of wet hours agree), so both count. It matters to hourly comparisons only, for
  example rf-t26.7's storm checks.
- `wcrc:Haast Rv @ Roaring Billy` and `wcrc:Haast Rv @ Roaring Billy WCRC`, 10 m apart: r 1.00 at
  0 h, but only 53 % of the wet hours agree and the totals differ by 2 % (390 vs 381 mm). So they are
  two gauges, or one gauge processed twice. The WCRC one ended on 2026-11-02 and is left out by
  `min_usable` anyway.
- `wcrc:Taipo Rv @ SHB` and `wcrc:Tuke Rv @ Tuke Hut`, 46 m apart in WCRC's site list, with totals
  that differ 3-fold (604 vs 1807 mm). Gauges 46 m apart do not differ 3-fold over a month, so one of
  the two locations is probably wrong. Both are in the same 0.1° cell, so the factor is not
  affected. To be checked with WCRC.

## Current factors

First live run, 2026-11-22 04:11Z. Window 2026-10-23 03:00Z .. 2026-11-22 03:00Z (720 h). Cell store
complete: 189 cells x 720 h, all hours valued; 99.6 % of the cell-hours from leads 1-6, 525 hours
from ECMWF GRIB and 195 from Open-Meteo. History records 1-3.

| region | gauges listed | used | left out: copy / no location / < 80 % hours | gauge mm | ECMWF mm | ratio | median per gauge (q25-q75) | trimmed | factor | version |
|---|---|---|---|---|---|---|---|---|---|---|
| West Coast | 57 | 44 | 3 / 8 / 2 | 25 086 | 27 693 | 0.906 | 0.904 (0.71-1.06) | 0.929 | **0.906** | 1 |
| Tasman | 80 | 67 | 4 / 9 / 0 | 11 199 | 10 796 | 1.037 | 0.993 (0.84-1.14) | 1.005 | **1.037** | 1 |
| Canterbury | 111 | 109 | 1 / 1 / 0 | 13 251 | 18 396 | 0.720 | 0.507 (0.37-0.75) | 0.593 | **0.800** (max step; target 0.720) | 1 |
| Marlborough | 2 | 2 | - | 328 | 262 | 1.251 | | | 1 (2 gauges < 3) | 0 |
| Otago | 1 | 1 | - | 671 | 919 | 0.730 | | | 1 (1 gauge) | 0 |
| 11 other regions | 0 | | | | | | | | 1 | 0 |

"No location" means gauges that the site lists give without coordinates (placed in their council's region
in `docs/rain-gauges.md`, with no cell to compare).

Reading the table:

- **ECMWF is too wet at the Canterbury gauges, but not on the main divide.** In all, their cells
  have 1.4 times the gauge rain, and half of the gauges see less than 51 % of their cell's rain.
  By the cell's 30-day ECMWF total, the gauges see this share of it:

  | ECMWF in the cell | gauges | ratio of sums | median |
  |---|---|---|---|
  | >= 250 mm (main divide: Clough Memorial, Carrington, Mt Byrne, Arthur's Pass, Aoraki/Mt Cook, ...) | 11 | 1.17 | 1.04 |
  | 120-250 mm (foothills, high plains) | 53 | 0.45 | 0.41 |
  | 60-120 mm | 37 | 0.66 | 0.56 |
  | < 60 mm | 8 | 0.74 | 0.58 |

  So ECMWF spreads the divide's rain too far east into the foothills, which fits a 9 km model's
  smoothed orography. One Canterbury factor (0.72) mixes these two regimes. It cuts the rain of the
  alpine headwaters, where ECMWF is about right or low, and the headwaters are the catchments that
  matter most for the runs. Smaller scaling regions (for example the divide and the foothills, or
  bands of ECMWF rain) would fix this, and the code takes any region list. Part of the difference may
  also come from the gauges: an ECan event gauge that is down reads as dry (`docs/rain-gauges.md`,
  "Dry hours have no row"). The factor takes the step limit: 0.8 now, 0.72 after the next daily
  update.
- **The West Coast gauges see 9 % less than ECMWF** (median 0.90, agreeing with the sum). These gauges
  are mostly in valleys and on the coastal plain, and the cells average over the ranges, so this is
  not a statement about the alpine rain.
- **Tasman agrees within 4 %** (median 0.99, trimmed 1.00).
- Marlborough (2 gauges) and Otago (1) stay at version 0. The North Island and Southland have no
  gauge councils yet.

These factors come from one 30-day window that the West Coast storm validation (rf-t26.7) has not
checked yet. They are local only: `[scaling] publish = false`.

Per region, `factors_ecmwf.json` -> `last_update.regions.<region>.used` lists each gauge with its
hours, sums and ratio, and `by_council` gives the sums per council (Tasman: TDC 66 gauges, ratio
1.042; one WCRC gauge, 0.79).

## The grid side: how far back the sources go

Checked 2026-11-22 by listing the buckets:

- **Open-Meteo** (`s3://openmeteo/data_spatial/ecmwf_ifs/`) keeps about 8 days of runs. The
  oldest run listed at 03:40Z was 2026-11-13 18Z (8.4 days), and the days before it were gone.
- **ECMWF open data on AWS** (`ecmwf-forecasts`) lists days back to 2023-01-18. The 0.1°
  `ifs/0p1/oper` files start with the 2026-10-20 06z run (only 0.25° before), so since 2026-11-19 a
  whole 30-day window can be backfilled from GRIB.

The first backfill of an empty store (720 h x 189 cells) costs about 380 MB and 10 min. Measured: 672
hours in 8.8 min and 346 MB in 850 requests, of which 525 hours came from GRIB (two `tp` messages
per run, steps 3 and 6, ~1.1 MB each) and 147 from Open-Meteo (~0.74 MB per hour). The other 48
hours came from a 2-day trial run before it (36 MB, 54 s). Later runs read only the new hours and
the hours whose run was not the newest possible one: about 24 hours and ~18 MB a day, or nothing when
`rain ecmwf` has the fields in its cache (the backfill reads that cache but does not write old runs
to it). A download that fails keeps what was read: the store is saved every 48 hours read and on
any error.

**Reader mix.** Over the 195 hours that came from Open-Meteo, the same hours from the GRIB reader at
the 189 cells give Open-Meteo / GRIB = 0.996 in all (Canterbury 0.989, Tasman 0.989, West Coast 1.002,
Otago 0.994, Marlborough 0.987; 80 MB). Open-Meteo is 16 % lower than GRIB over the whole NZ window,
all of it in drizzle (`ecmwf.py`), but at the gauge cells drizzle is only 2.4 % of the rain. So mixing
the two readers in the store biases a factor by about 1 % at most, which is below `min_change`. Each
cell-hour records its reader (`src`), and `last_update.inputs.cells.by_reader` gives the mix. After
about 30 days of daily updates, the window is all Open-Meteo.

## Update schedule and the hourly job

- **Locally**: `rain gauges` and then `rain scale` once a day (the 20 h `min_update_hours` makes a daily
  run the cadence; more frequent runs update the cell store and the evaluation but hold the
  versions). `rain ecmwf` applies the current factors on every run.
- **The hourly GitHub job** does not scale yet. It has no gauge cache, no cell store and no factor
  file, so everything is version 0. Publishing scaled values also waits for rf-t26.14.
  `docs/live-rain.md`, "Gauge scaling in the job", has the design: a daily workflow that keeps the
  gauge cache and `rain/scaling/` on a `rain-scaling` branch (~1.5 MB), and a step in the hourly job
  that downloads `factors_ecmwf.json` (~0.1 MB, under a second).
- **Sizes**: `ecmwf_cells.npz` 92 kB (189 cells x 720 h), `factors_ecmwf.json` 72 kB, history ~13 kB
  per new version.

## Not done here

- The West Coast storm validation of rf-t26.7: do the scaled hourly totals beat raw ECMWF at gauges
  that were left out (or in a cross-validation by gauge)?
- Satellite rain (IMERG) is deferred. The design is per source family: a `[scaling.<family>]` table,
  `<family>_cells.npz` and `factors_<family>.json`, and buffers scaled by family. A satellite source
  needs a filler for its grid in place of `fill_ecmwf`.
- Gauges without coordinates (18 in the three councils) are not compared. Their site pages may give
  a location.
