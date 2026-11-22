# Rain gauge scaling (rf-t26.5, rf-t26.13, rf-t26.16)

The ECMWF rain the site shows (the observed proxy and the next-24-h forecast, `docs/live-rain.md`)
comes from a 9 km model. Over steep terrain it misses part of the orographic rain, and elsewhere it can
over-forecast. Per scaling zone, the gauges of the regional councils (`docs/rain-gauges.md`) say by how
much: over the last 30 days, the factor is the gauge rain divided by the ECMWF rain in the gauges' 0.1°
cells. Local totals are scaled by it. The published files are not scaled yet (see "Publishing").

A scaling zone is a network region, or part of one: Canterbury is split at 15 km from the main
divide into **Canterbury divide** and **Canterbury east** (rf-t26.16, "Zones" below). Gauges that are
probably down (dry while their neighbours and ECMWF are wet) are left out ("Likely outages").

Code: `src/river_finder/rainfall/scaling.py` (its docstring is the reference) and
`src/river_finder/rainfall/zones.py` (the zones). Config: `[scaling]`, `[scaling.ecmwf]`,
`[scaling.divide]` and `[[scaling.zones]]` in `config/rainfall.toml`. Tests:
`tests/test_rainfall_scaling.py`.

```bash
uv run river-finder rain gauges          # gauge hours (incremental, ~6 min)
uv run river-finder rain zones           # catchment -> scaling zone (after a national rebuild or a zone change)
uv run river-finder rain scale           # cell store, copies, outages, factors; re-scales the saved buffers
uv run river-finder rain ecmwf           # new buffers, scaled with the current factors
uv run river-finder rain totals          # local upstream totals (scaled)
uv run river-finder rain status          # includes the factors and the cell store
uv run river-finder rain scale --days 30 --now 2026112203 --force --no-fetch   # options
```

## Method

1. **Window.** The 30 x 24 whole UTC hours (hour ending) up to "now", or up to the newest gauge hour
   when the gauge cache is older (`[scaling] days`).
2. **Gauge side.** `gauges_hourly.npz`. A gauge hour counts when its state is good or raw
   (`state <= 1`: suspect and missing hours are left out), and is not in a likely outage (step 4).
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
4. **Likely outages (rf-t26.16).** A gauge that is down reads dry. A spell of at least 24 dry (or
   unusable) hours is left out when at least 2 other gauges within 25 km have a median of at least
   20 mm over its dry hours and ECMWF has at least 20 mm in its cell ("Likely outages" below).
5. **Zones (rf-t26.16).** Each gauge is placed in the zone of its network region, or, in a split
   region, in the near or far zone by its distance to the main divide ("Zones" below).
6. **Grid side.** `<rain>/scaling/ecmwf_cells.npz` holds ECMWF's hourly mm at every gauge cell (189
   cells) for `keep_days` (40). Each run of `rain scale` fills it incrementally with the rule of the
   observed proxy: each hour comes from the newest run that has published it within `[ecmwf]
   obs_leads` (1-18 h). So settled hours come from leads 1-6, and an hour first taken at a longer
   lead (the newest hours) is replaced when the next run has it. For each run, the readers are tried
   in `[scaling.ecmwf] readers` order: Open-Meteo first (the reader of the live buffers), then ECMWF's
   own 0.1° GRIB files on AWS (3-hourly steps split evenly into hours, which only matters at the
   window's edges and at gauge hours left out). Each
   value records its reader, run and lead. A gauge is a point and a cell is 0.1°, so the factor is
   a regional ratio of sums, never a per-gauge correction.
7. **Factor.** Per zone, the usable gauges are the gauges that are not a dropped copy, have a cell
   and have at least 80 % (`min_usable`) of the window's hours both usable at the gauge and valued at
   the cell. Then

       ratio = sum(gauge mm) / sum(ECMWF mm at the gauges' cells)

   over exactly those hours (one sum over all gauges and hours, so the wet gauges weigh most).
8. **Guard rails**, in order:
   - the cell store covers less than `min_usable` of the window (ECMWF unreachable): nothing changes;
   - fewer than 3 usable gauges in the zone (`min_gauges`): factor 1, version 0;
   - mean total below 30 mm per gauge at the gauges or in the grid (`min_gauge_mm`, `min_grid_mm`): the
     current factor is held (a dry month says nothing about the bias);
   - the ratio is clamped to [0.5, 2.0] (`clamp`);
   - one update moves the factor by at most x1.25 or /1.25 (`max_step`), so a new ratio takes a few
     days to reach;
   - a change below 2 % (`min_change`) keeps the current factor and version;
   - a zone gets at most one new version per 20 h (`min_update_hours`; `--force` overrides), so an
     hourly run does not move the factor every hour.
9. **Checks** reported with every region, but not used: the median and quartiles of the per-gauge
   ratios (gauges with at least half of `min_grid_mm` in their cell), and the ratio of sums without the
   10 % highest and lowest per-gauge ratios (10 or more gauges). Also the sums per council.

Zones are the regions of the reach network (the river-guide regions of the segment table, which follow
the councils), except a region listed in `[[scaling.zones]]`, which is two zones. The gauges' regions
use the same names (`docs/rain-gauges.md`, "Regions"). Zones without gauge councils yet (the North
Island, Southland, ...) and zones with fewer than 3 gauges (Marlborough and Otago now) stay at factor
1, version 0. The factor applies to every catchment of its zone, upstream of the gauges or not.

## Zones (rf-t26.16)

**The rule.** `[[scaling.zones]]` splits a region in two by distance to the main divide. Catchments
(by their reach's point in the segment table) and gauges within `divide_km` of the divide are zone
`near`, the others zone `far`:

```toml
[scaling.divide]                 # the main divide: the border of the west regions with the east regions
west = ["West Coast", "Tasman"]
east = ["Canterbury", "Otago"]

[[scaling.zones]]
region = "Canterbury"
divide_km = 15.0
near = "Canterbury divide"
far = "Canterbury east"
```

The divide is the outline of the West Coast and Tasman where it runs within 50 m of Canterbury or
Otago, from the council polygons the network's regions come from (`national.region_filters`),
simplified to 50 m: one line of 609 km from the Spenser Mountains (172.72° E, 42.10° S, where Tasman,
Canterbury and Marlborough meet) past Lewis, Harper, Arthur's, Whitcombe and Haast passes to the
Otago-Southland boundary (168.34° E, 44.50° S). Distances are in NZTM.

**Why.** One Canterbury factor (0.72 on 2026-11-22) mixed two regimes. By distance to the divide
(30 days to 2026-11-22 03:00Z, likely outages left out; the halves are the first and last 15 days):

| distance to the divide | gauges | gauge / ECMWF (ratio of sums) | 1st / 2nd half | median per gauge | median elevation |
|---|---|---|---|---|---|
| <= 15 km | 11 | 1.20 | 1.19 / 1.21 | 1.04 | 810 m |
| 15-30 km | 8 | 0.58 | 0.63 / 0.52 | 0.55 | 738 m |
| 30-50 km | 14 | 0.44 | 0.49 / 0.38 | 0.40 | 552 m |
| 50-80 km | 32 | 0.49 | 0.40 / 0.55 | 0.41 | 418 m |
| 80-120 km | 33 | 0.53 | 0.38 / 0.63 | 0.47 | 181 m |
| > 120 km | 10 | 0.57 | 0.62 / 0.52 | 0.59 | 128 m |

ECMWF's 9 km orography spreads the divide's rain east, into the foothills and basins: there the gauges
see half of the model's rain. Close to the divide the model is about right or low. It is not the
gauges' elevation: at 700-1200 m, the 9 divide gauges see 1.10 x ECMWF and the 19 east gauges 0.61 x.
The 11 divide gauges are Mount Byrne (0.1 km from the divide, 2.33), Clough Memorial (1.47),
Carrington (1.41), Arthur's Pass EWS and Arthur's Pass (1.01, 1.04), Maling Pass (0.93), Aoraki/Mt
Cook (1.06), Mistake Flats (0.81), Ranger Stream (1.04), Clyde (0.57) and Cassinia Moraine (11.2 km,
1.00). The nearest east gauges are Bull Creek (17.3 km, 0.83) and Grasmere (17.5 km, 0.47), so any
threshold from 11.3 to 17.3 km gives the same gauges; 15 km is under two grid lengths of the model.

**Candidates compared.** Each split was scored by how well the zone factors predict each gauge's
total: the RMS of log(gauge / (factor x ECMWF)) over the gauges with >= 30 mm in their cell, in sample,
with the factors from one 15-day half applied to the other half (half CV), and leaving each gauge out
of its zone's factor (LOGO). Gauge elevations are from the LINZ 8 m DEM at each gauge, the flood
climate is the median specific mean annual flood (H&C 2018) of the 0.5-30 km² REC1 reaches in the
gauge's cell. Canterbury, 108 gauges (distances to a 200 m strip along the border; the stored line
differs by at most 0.22 km, which moves one gauge across 30 km):

| zones | gauges: ratio (1st / 2nd half) | in sample | half CV | LOGO |
|---|---|---|---|---|
| one zone | 108: 0.72 (0.75 / 0.70) | 0.614 | 0.715 | 0.620 |
| **<= 15 km of the divide / beyond** (also 12 km) | **11: 1.20 (1.19 / 1.21); 97: 0.51 (0.47 / 0.54)** | **0.472** | **0.575** | **0.479** |
| <= 10 km / beyond | 10: 1.21; 98: 0.52 | 0.478 | 0.580 | 0.485 |
| <= 20 km / beyond | 13: 1.15; 95: 0.50 | 0.477 | 0.582 | 0.484 |
| <= 30 km / beyond | 20: 1.03; 88: 0.49 | 0.498 | 0.607 | 0.506 |
| ECMWF cell >= 250 mm in the 30 days | 11: 1.17; 97: 0.51 | 0.484 | 0.584 | 0.492 |
| long-term flood climate: NIWA specific mean annual flood of the cell >= 2.5 m³/s/km² | 9: 1.21; 99: 0.52 | 0.482 | 0.582 | 0.489 |
| gauge elevation >= 800 m (8 m DEM) | 20: 1.03; 88: 0.59 | 0.558 | 0.657 | 0.568 |
| three zones: <= 15 / 15-60 / > 60 km | 11: 1.20; 33: 0.46 (0.51 / 0.42); 64: 0.55 (0.43 / 0.63) | 0.464 | 0.621 | 0.475 |

The split by distance to the divide is the best of the two-zone splits on all three scores, and it
is a fixed line (no 30-day window or rain band decides the zones, so they do not drift). A third zone
fits the window a little better but predicts the other half worse: the middle and outer bands swap
order between the halves. The MfE mean annual rainfall grid is not reachable from here
(`docs/data-sources.md`); the NIWA flood statistics are the long-term climate at hand, and they give
nearly the same split.

**West Coast and Tasman stay one zone each.** West Coast (41 gauges): the gauges within 12 km of the
divide see 1.25 x ECMWF (6 gauges) and the others 0.92 x, but that is the gauges' elevation: the
high ones (Mt Browning, Ripplerock, Cropp, Tuke Hut: 950-1370 m) see 1.23-1.59, the valley gauges
just as close to the divide 0.67-1.15 (Haast at Moa Creek, Waiho at SH6, Roaring Billy, Cron Creek).
A cell holds both, so a cell factor cannot follow it, and the scores improve by 4-9 % only (one zone:
0.356 half CV; <= 12 km: 0.332; elevation >= 600 m: 0.325). Tasman (67 gauges): the inland basins
25-55 km from the divide (Matakitaki, Maruia (2), Tiraumea, Howard, Gowan, Rotoiti, Murchison, Buller at
Longford, Motupiko, Motueka Gorge, Matiri: 12 gauges) see 0.73 x (0.75 / 0.71 in the halves), the
rest 1.12 x; the half-CV score improves by 10 % (0.331 to 0.297). That is a real pattern (the lee of
the West Coast ranges), but the line would be fitted to this window: to be checked with a second
window before it gets a zone (one more `[[scaling.zones]]` table, `divide_km = 60`).

**Zone map.** `rain zones` writes `<rain>/scaling_zones.npz` (+ `.json`, 0.5 MB): the zone of every
catchment row (`zone_of`), each zone's region, the catchments' distance to the divide in split
regions, the divide line (to place gauges) and the rule. Live: 17 zones; **Canterbury divide**
19,481 catchments, 5,388 km² (12 % of Canterbury): a band along the divide from the Spenser Mountains
(upper Waiau, Boyle, Lewis, Hope, Clarence headwaters) past the Hurunui South Branch, the Poulter, the
upper Waimakariri (Arthur's Pass, Edwards), the Wilberforce, the upper Rangitata (Havelock, Clyde,
Lawrence) and the Godley to Aoraki/Mt Cook and the Hopkins and Dobson headwaters; **Canterbury east**
182,165 catchments, 38,929 km²: the foothills, the high country basins (Mackenzie, Lees Valley) and
the plains. A reach whose catchment crosses the line has both zones upstream: the lower Waitaki
(11,785 km² upstream) gets Canterbury divide, Canterbury east and Otago.

**History.** Zone names are the ids of the factor history. On the first `rain scale` with the
split, the Canterbury factor was closed: history record 4 (`status: closed`, factor 1 version 0,
previous 0.800 v1, `replaced_by` the two zones, the reason), and `factors_ecmwf.json` keeps it under
`closed` with its last factor, versions and record. The new zones start from factor 1, version 0, so
their first factor is version 1 (records 5 and 6). A zone that is listed again continues above the
versions it had (`max_version + 1`). The zone of every gauge in a split region and its distance to the
divide are in `last_update.gauge_zones`.

**When the zones change** (a new `[[scaling.zones]]`, another `divide_km`) or the network does: run
`rain zones`, then `rain scale`, and refresh the `rain-inputs` branch (`docs/live-rain.md`): the job's
buffers keep their factors per zone, so `rain inputs` copies the zone file. `rain scale` and `rain
ecmwf` (with a factor file) refuse a missing or stale zone file; without factors (the hourly job
today) a missing one means one zone per region and a note. A saved buffer of other zones is moved to
the current ones (`HourlyBuffer.rezone`: unscaled until the factors are applied again).

## Likely outages (rf-t26.16)

A tipping-bucket gauge that is down reads dry: a record that does not arrive is a dry hour. ECan's
event gauges have no row for a dry hour, so a dead gauge whose site still reports looks the same as a
dry one (`docs/rain-gauges.md`, "Dry hours have no row"); the portal's "last sample" only catches a
site that stopped reporting (the hours after it are already missing). Glynn Wye's last sample was an
hour old, but it had recorded nothing for 24 days. WCRC's Hilltop records have the same blind spot.

The rule (`scaling.find_outages`, over every hour the gauge cache holds): a **spell** is a run of
hours that are dry (usable, 0 mm) or unusable at a gauge, with at least `outage_min_hours` (24) dry
hours. Its neighbours are the other gauges within `outage_km` (25 km; dropped copies left out) with a
value in at least 80 % of the spell's dry hours. The spell is a likely outage when at least
`outage_min_neighbours` (2) neighbours qualify, their median rain over those hours is at least
`outage_neighbour_mm` (20 mm), and ECMWF has at least `outage_grid_mm` (20 mm) in the gauge's cell.
Both must be wet: a gauge that is dry next to one wet gauge, or that only the model calls wet, is not
marked. Its dry hours are then left out like missing hours (the gauge cache is not changed), so a
gauge that loses more than 20 % of the window drops out by `min_usable`. `last_update.outages` lists
every spell with its neighbours; the used gauges carry `outage_hours`.

Live, 2026-11-22 (1617 dry spells of >= 24 h checked): 7 spells at 5 gauges, 2433 gauge-hours of the
cache and **2181 gauge-hours of the window** (WCRC 1623, ECan 558, TDC 0):

| gauge | dry | neighbours: median (nearest) | ECMWF in the cell |
|---|---|---|---|
| wcrc:Kokatahi Rv @ Above Crawford Junction | 629 h to 2026-11-13 | 7: 968 mm (Styx at Mt Browning 9.6 km, 1109 mm) | 855 mm |
| wcrc:Wanganui Rv @ Above Smyth Hut | 629 h to 2026-11-13 | 6: 1168 mm (Ivory at Ripplerock 10.5 km, 1151 mm) | 843 mm |
| wcrc:Hokitika Rv @ Colliers Ck | 162 + 129 + 296 h from 2026-10-28 | 9: 204, 136, 492 mm (Hokitika Gorge 4.3 km) | 290, 213, 491 mm |
| wcrc:Taipo Rv @ SHB | 30 h, 2-4 November | 8: 33 mm (Tuke Hut, the same site, 82 mm) | 36 mm |
| ecan:Glynn Wye | 558 h from 2026-10-29 | 3: 38 mm (Marble Point 22 km, 38 mm) | 99 mm |

Kokatahi, Smyth Hut, Colliers Creek and Glynn Wye drop out (< 80 % of the window); Taipo stays with
30 hours fewer. Effect: **West Coast 0.906 -> 1.007** (44 -> 41 gauges: three gauges that read almost
nothing while their neighbours had a metre of rain), Canterbury as one zone 0.720 -> 0.724 (Glynn Wye
only). So ECan's event gauges do not explain the low Canterbury ratio. The thresholds are not
critical: neighbours and ECMWF >= 10 mm mark 12 spells at 9 gauges (Canterbury east 0.512, West Coast
1.009); >= 40 mm over >= 48 h mark 5 spells at 3 gauges (0.504, 1.006). Unmarked borderline spells:
Grays Hills (292 h, 2 neighbours with a median of 16 mm, ECMWF 25 mm), Poyntzs Rd (132 h, 13 mm, 19
mm), Staveley Camp (83 h, 10 mm, 35 mm). To be checked with the councils: Kokatahi and Smyth Hut
recorded nothing from the start of the cache (17 October) to 13 November, and Colliers Creek nothing
since 28 October, while WCRC's unplaced "Hokitika Rv @ Colliers Ck 2" recorded 1322 mm in 30 days.

## Versions, storage and traceability

- `<rain>/scaling/factors_ecmwf.json`: under `regions`, per **zone** the current `factor`, `version`,
  `set` (when), `status` (`scaled`, `updated`, `held`, `unity`, `reset`), `reason`, `record` (the
  history line behind it) and `parent` (its network region). `closed` holds the zones no longer
  listed (Canterbury since rf-t26.16) with their last factor, versions and closing record; `zones` the
  rule and each zone's region. `last_update` holds the newest evaluation of every zone: the gauges
  used with their cell, hours, gauge and ECMWF sums and ratio, the gauges left out and why, the sums
  per council, the checks, the guard rails that acted, the window, the config, the input files (gauge
  file time, cell store span and readers, zone file), the copy groups, the likely outages and the
  zone of each gauge in a split region (`gauge_zones`).
- `<rain>/scaling/factors_ecmwf.history.jsonl`: append-only, one line per new version: `seq`, `region`
  (the zone), `parent`, factor, version, the previous factor and version, status, reason, window
  (first and last hour), the full evaluation (every gauge id with its cell, hours and sums), the
  copies dropped and the likely outages in the zone, the config and the inputs. So any factor traces
  back to its gauges and hours. A closed zone gets one line with `status: closed`, factor 1, version
  0, the previous factor and version, `replaced_by` and the reason.
- Versions change only with the factor. Version 0 always means unscaled (factor 1). A zone reset to
  unity gets version 0, and its next factor gets `max_version + 1`, so versions above 0 are unique per
  zone (also for a zone closed and listed again).
- Each buffer slot stores raw mm, scaled mm, and the factor and version per zone
  (`HourlyBuffer.regions` are the zone names, `.region_of` the zone of each catchment; `.factor`,
  `.factor_version`; `slot_table()` lists the versions per hour). The upstream totals carry the scale
  bits per reach (1 raw, 2 scaled, 3 mixed), the factor versions per window and, per kind, the zones
  and a bit per zone upstream (`up_zones`), so `provenance()` names the zones whose factors reached a
  reach.

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

Live run of 2026-11-22 04:41Z, the first with zones (window 2026-10-23 03:00Z .. 2026-11-22 03:00Z, 720
h; cell store complete: 189 cells x 720 h, 99.6 % of the cell-hours from leads 1-6; likely outages
left out). History records 4-6: Canterbury closed, the two Canterbury zones at version 1. Not forced
(`--force` was not needed: the new zones had no version yet).

| zone | gauges listed | used | left out: copy / no location / < 80 % hours | gauge mm | ECMWF mm | ratio | median per gauge (q25-q75) | trimmed | factor | version |
|---|---|---|---|---|---|---|---|---|---|---|
| Canterbury divide | 12 | 11 | 1 / 0 / 0 | 6 878 | 5 755 | 1.195 | 1.036 (0.97-1.24) | 1.148 | **1.195** | 1 |
| Canterbury east | 99 | 97 | 0 / 1 / 1 (outage) | 6 346 | 12 500 | 0.508 | 0.480 (0.36-0.62) | 0.493 | **0.800** (max step; target 0.508) | 1 |
| West Coast | 57 | 41 | 3 / 8 / 5 (3 outages) | 24 305 | 24 130 | 1.007 | 0.933 (0.75-1.06) | 0.965 | **0.906** (held: v1 set 0.5 h before; 1.007 at the next daily run) | 1 |
| Tasman | 80 | 67 | 4 / 9 / 0 | 11 199 | 10 796 | 1.037 | 0.993 (0.84-1.14) | 1.005 | **1.037** | 1 |
| Marlborough | 2 | 2 | - | 328 | 262 | 1.251 | | | 1 (2 gauges < 3) | 0 |
| Otago | 1 | 1 | - | 671 | 919 | 0.730 | | | 1 (1 gauge) | 0 |
| 11 other zones | 0 | | | | | | | | 1 | 0 |
| ~~Canterbury~~ (closed) | | | | | | 0.720 | | | was 0.800 v1 | closed |

"No location" means gauges that the site lists give without coordinates (placed in their council's region
in `docs/rain-gauges.md`, with no cell to compare).

Reading the table:

- **Canterbury divide: 1.195.** The alpine headwaters (5,388 km²) went from 0.80 (the one Canterbury
  factor) to 1.195, x1.49. Their gauges see about what ECMWF has, or more (median 1.04, Mount Byrne
  2.33).
- **Canterbury east: target 0.508**, factor 0.800 now (the old Canterbury factor), then 0.64 and 0.512
  in the next two daily runs (0.508 is within 2 % of 0.512). Half of the east gauges see less than 48 %
  of their cell's ECMWF rain.
- **West Coast: 1.007 without the outages** (median 0.93); the factor stays at 0.906 until the next
  daily run (20 h rule). These gauges are mostly in valleys and on the coastal plain, and the cells
  average over the ranges, so this is not a statement about the alpine rain ("Zones": the high West
  Coast gauges see 1.2-1.6 x).
- **Tasman agrees within 4 %** (median 0.99, trimmed 1.00).
- Marlborough (2 gauges) and Otago (1) stay at version 0. The North Island and Southland have no
  gauge councils yet.

The first run (04:11Z, records 1-3, one zone per region, no outage rule): West Coast 0.906 (ratio
0.906), Tasman 1.037, Canterbury 0.800 (target 0.720). These factors come from one 30-day window that
the West Coast storm validation (rf-t26.7) has not checked yet. They are local only: `[scaling]
publish = false`.

Per zone, `factors_ecmwf.json` -> `last_update.regions.<zone>.used` lists each gauge with its hours,
sums and ratio (and `outage_hours`), and `by_council` gives the sums per council (Tasman: TDC 66
gauges, ratio 1.042; one WCRC gauge, 0.79).

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
  versions). `rain ecmwf` applies the current factors on every run. `rain zones` after a national
  rebuild or a change of `[[scaling.zones]]`, then `rain inputs` for the hourly job.
- **The hourly GitHub job** does not scale yet. It has no gauge cache, no cell store and no factor
  file, so everything is version 0. Publishing scaled values also waits for rf-t26.14.
  `docs/live-rain.md`, "Gauge scaling in the job", has the design: a daily workflow that keeps the
  gauge cache and `rain/scaling/` on a `rain-scaling` branch (~1.5 MB), and a step in the hourly job
  that downloads `factors_ecmwf.json` (~0.1 MB, under a second).
- **Sizes**: `ecmwf_cells.npz` 92 kB (189 cells x 720 h), `factors_ecmwf.json` ~90 kB, history ~13 kB
  per new version, `scaling_zones.npz` 0.5 MB (an input of the hourly job: `docs/live-rain.md`).

## Not done here

- A Tasman zone for the inland basins (0.73 x vs 1.12 x, "Zones"): to be checked with a second window.
- The outage rule marks hours left out of the comparison only; the gauge cache and rf-t26.7's storm
  checks still see the dry hours (`scaling.find_outages` gives the mask).
- The West Coast storm validation of rf-t26.7: do the scaled hourly totals beat raw ECMWF at gauges
  that were left out (or in a cross-validation by gauge)?
- Satellite rain (IMERG) is deferred. The design is per source family: a `[scaling.<family>]` table,
  `<family>_cells.npz` and `factors_<family>.json`, and buffers scaled by family. A satellite source
  needs a filler for its grid in place of `fill_ecmwf`.
- Gauges without coordinates (18 in the three councils) are not compared. Their site pages may give
  a location.
