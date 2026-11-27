# One-pass extraction spec for the 1 m rebuild (rf-lxa.3)

The rule: before any national computation on the 1 m LiDAR DEM, settle everything, so that **one pass over the 1 m
data extracts all the information we need**. This page lists every per-reach field the national pass produces,
where each one is computed, the raw data each tile stores so that later features need no second pass, the files each
tile keeps, the reach ids, the national size, and what today's consumers read and would lose.

Settled design, not re-opened here: [`lidar-pipeline.md`](lidar-pipeline.md) and its decisions of 2026-11-26 (rf-lxa.2:
route at 2 m from the block mean of full-resolution 1 m cells, ~200 tiles of 32 km + 3 km halo, lakes filled at the
rim level, 8 m fallback with seam correction, profiles sampled at 1 m, LiDAR lines with LINZ names, areas by network
accumulation after the merge, reference areas in the national run, mosaic only), [`cross-sections-1m.md`](cross-sections-1m.md)
(rf-lxa.4: section keep list and storage) and [`lidar-coverage.md`](lidar-coverage.md) (rf-lxa.1: LiDAR fields and water
caveats).

Code: `src/river_finder/lidarspec.py` (the spec as data: fields, stages, stored layouts, files, reach ids, consumers;
`python -m river_finder.lidarspec` prints the field tables below), new profile pieces in `lidarpipe.py`
(`reach_profile`, `profile_steps`, `reach_steps`, `encode_profiles`, `decode_profiles`). Tests:
`tests/test_lidarspec.py` checks that every field and file today's consumers read is produced or listed as replaced
or dropped; `tests/test_lidarpipe.py` checks the profile pieces. Dry run outputs (gitignored):
`data/analysis/lidar-spec/` (`dryrun.json`, `dryrun_reaches.csv`, `dryrun_steps.csv`, `profiles_sub.arrow`,
`steps_sub.arrow`, `scripts/dryrun.py`).

## Summary

- **263 per-reach fields**: 141 per tile from the 1 m DEM (119 of them cross-section summaries, 75 of those the stage
  table), 29 per tile from the 2 m grid, 28 after the national merge, 28 from external data joined to the stored
  lines, and 37 derived where they are used (scoring, MAF correction, reach types). Every field that today's
  consumers read is produced under its name, or replaced by a named field (the 8 m `xs_*` sections by `x1_*`; the
  relink logs by `stitch` and `outlet_kind`), or dropped (5 `xs_*` columns no consumer outside `xsec.py` reads).
- **What must happen inside the tile job** is only what reads the 1 m or the 2 m data: the long profiles, steps,
  sections, LiDAR and water shares, valley widths, gorge depth, and the local catchment sums. Everything that needs
  the network or the final areas (areas, ksn, MAF, head, relief, Melton, upstream shares) is done after the merge
  from per-reach local sums. Everything from other data (REC1/REC2, LINZ names, geology, OSM, hydro, aerial, survey
  years) is joined to the stored lines and can be redone at any time.
- **Stored raw data**: the 1 m long profile of every reach (int16 cm differences + 2-bit station codes; **830 bytes per
  km**, ~0.3 GB for the country), the raw cross-sections of rf-lxa.4 (25 m on reaches of 1 km2 or more, ~620 bytes per
  section, **~5.4-6.3 GB**), and a step table (15 bytes per step, ~0.05 GB). With these, every profile and section
  feature here, and new ones, can be computed without reading the LiDAR again.
- **National size**: ~10 GB of tile outputs (sections ~6 GB, lines ~1.5 GB, area code ~1.1 GB) plus ~4 GB of
  national tables and viewer files: **~14 GB**, +0.7 GB with an optional 10 m HAND raster, +2.8 GB with an optional
  10 m DEM. It does not fit next to today's data in this session (8 GB free).
- **Per-tile cost of the extraction**: ~65-85 s (about half of it the 1 m re-read and the sections) and ~6-7.6 GB
  of 2 m arrays, inside rf-lxa.6's ~9 GB budget: **~3.5-4.5 h for the country** on top of streaming and routing.
- **Dry run** on the rf-lxa.2 sub-area (Hokitika / Toaroha / Kokatahi, 2 m lines, 422 km): 422,000 1 m stations in
  1.7 s after a 6 s re-read, 3.5 GB peak; profile store 830 B/km, exact to 1 cm; 5,250 steps (12.5 per km, median
  3.0 m high, 4 m long). 3 of the 5 OSM waterfalls in the box have a 15-38 m step within 20 m; the other two are 48 m
  and 330 m from the nearest channel of 0.5 km2.
- **Consumers that lose something or must change**: the 8 m sections (`xs_*`, `gbm_xsec`, `logit_xsec`),
  `truncated_core.tif`, the relink logs, the reference-area pipeline (`data/<region>/`), the 64 km tile class, the
  column list of `models.national_table`, `lidar coverage`'s 8 m pixel size, and cards v1-v4 (same column names, new
  definitions). See [What consumers lose](#what-consumers-lose-or-must-change).
- **Open questions for the user**: 10, listed at the end (reference areas, cards v1-v4, optional rasters, section
  spacing, the 1 m DSM, reach length, output root, line tolerance, the proposed extra fields, the step definition).

## What runs where (stages)

| stage | where | reads | examples |
|---|---|---|---|
| `1m` | tile job (rf-lxa.6), after routing | 1 m blocks re-read along the reaches; the 2 m grid | long profile, steps, sections, `lidar_share`, water shares |
| `2m` | tile job, while the 2 m grid is in memory | 2 m block-mean DEM, filled DEM, directions, accumulation | area in the tile, local sums, HAND widths, gorge depth, seam distance, lake share |
| `merge` | after the national merge (rf-lxa.5) | the tiles' reach tables, the lake table | `area_km2`, `down_gid`, `truncated`, ksn, head, relief, Melton, upstream shares, MAF, names |
| `ext` | any time after the merge | stored reach lines and midpoints; external layers | geology, OSM, hydro, LINZ distance, survey and year, REC2 match |
| `use` | where used (`scoring.prepare`, `maf.apply`, `reachtype`, `xsec1m.derive`, nz score) | the national table | `grad_s`, `power_kw_m`, styles, scores, `x1_v_q` |

"(store)" in the tables means the value can be recomputed from the stored raw data. Nothing at 2 m is kept, so a
`2m` field can only be changed by re-running the tile; that is why the cheap 2 m extras are proposed now.

Area-dependent fields wait for the merge: with 32 km tiles the in-tile areas are wrong near edges (183 truncated
reaches in the x05y07 core at 32 km, rf-lxa.2), so ksn, Melton, MAF matching (area ratio), REC2 name matching (area
ratio) and every upstream share are computed from the network-accumulated `area_km2` and the accumulated local sums.

## Consumer inventory (today)

What each consumer reads from `data/nz/` (or `data/<region>/`) and writes, from the code. `lidarspec.CONSUMERS` and
`FILE_CONSUMERS` hold the same lists; the feature builders (`scoring.FEATURES`, `ml.EXTRA`, the rf-lxa.4 keep list,
`reachtype.nz_inputs`) are read from the code by `lidarspec.consumer_fields`.

| consumer | reads | writes |
|---|---|---|
| `nz.py` (tiles, merge, GIS) | per tile: 8 m DEM, clipped `rec2.gpkg`, `flood.gpkg`, `geology.gpkg`; merge: `tiles/*/segments_core.gpkg` (`gid`, `down_gid`, `exits_core`, `end_x`, `end_y`, `tile`, `area_km2`, `length_m`, line), `rec2.gpkg`, `local_catchment_core.tif` (relink); GIS: `regions/*/segments_scored.gpkg`, `runs.geojson`, tile rasters | `tiles/<t>/segments_core.gpkg`, `area_code.tif` (8 m), `hillshade_24m.tif`, `truncated_core.tif`, `local_catchment_core.tif` (8 m, int64 gid), `tile.json`, `inflows.json`; `relink_log.csv`, `unresolved_gt200.csv`; `gis/` |
| `reaches.py` | `area_km2.tif`, `filled.tif`, DEM, `receivers.npy`, `truncated.tif`, `geology_nztm.geojson`, `flood_rec1_nztm.geojson`, `rec2_nztm.geojson` | `segments.gpkg` (33 fields), `local_catchment.tif` |
| `nzscore.py`, `scoring.py` | merged reaches; the 13 `FEATURES` columns, `gradient_m_per_km`, `ksn`, `length_m`, `down_seg`, `z_top`, `z_bot`, `river`, `maf_m3s`; `regions/*.toml` | `regions/<slug>/segments_scored.gpkg` (+ `grad_s`, `ksn_s`, `power_kw_m`, `up_seg`, `label`, `holdout`, `score_*`, `run_id`, `z_*`), `runs.geojson`, `candidates.csv`, `model.json` |
| `models.py` | `regions/*/segments_scored.gpkg` (`seg_id`, `gid`, `down_gid`, `river`, `area_km2`, `length_m`, `score_regression`, `label`, `holdout`, the 13 raw features, `valley_width_m`, MAF columns); reference `data/<region>/segments_scored.gpkg` | `segments_table.pkl` (+ `x`, `y`, `lon`, `lat`, `down`, `up`, `vi`, `rnorm`, `guide_region`), cards, `<region>_m_<model>.txt` |
| `ml.py`, `mlpage.py` | `segments_table.pkl`, `geology.gpkg`, `external.pkl`, `waterfalls.gpkg`, `xsec.pkl`, `hydro.pkl` (audit), `tiles/*/segments_core.gpkg` (`specific_maf_matched`) | `ml_table.pkl` (+ `outlet`, geology, karst, OSM, reach types, `xs_*`), `ml_oof.npz`, `models/cv/`, audits |
| `reachtype.py` | `grad_s`, `maf_m3s`, `valley_width_m` | `p_*`, `reach_type`, `style`, `confinement_ratio` |
| `maf.py` | `area_km2`, `maf_m3s`, `maf_raw_m3s`, `specific_maf`, `specific_maf_matched`, `down`, `grad_s` | corrected `maf_m3s`, `maf_flag`, `power_kw_m` |
| `labels.py` | `down_seg`, `up_seg`, `length_m`, `area_km2`, `grad_s`, `river`, line | section rows (viewer labels) |
| `hydro.py` | `segments_table.pkl` (`x`, `y`, `river`, `area_km2`, `down`, `up`, `length_m`, `grad_s`, `region`, `seg_id`, `gid`, `vi`) | `hydro.pkl`, `hydro_summary.json` |
| `external.py` | `regions/*/segments_scored.gpkg` (`seg_id`, `area_km2`, `steepest_50m_m_per_km`, `knick_ratio`, `grad_s`, line), `waterfalls.gpkg`, `access.gpkg` | `external.pkl`, `waterfall_check.json`, `<vid>_x.txt`, `<vid>_falls.json` |
| `lidar.py` | `tiles/*/segments_core.gpkg` (`gid`, line), `tiles/*/local_catchment_core.tif` (assumes 8 m: 64 m2 per cell), `segments_table.pkl` (`gid`, `down`) | `analysis/lidar/reach_coverage.pkl`, `catchment_cells.npz`, `summary.json` |
| `xsec.py`, `xsec1m.py` | `tiles/*/segments_core.gpkg` (`gid`, `area_km2`, `length_m`, line), 8 m DEM / 1 m mosaic, `reach_coverage.pkl` (`lidar_share`) | `xsec.pkl` (`xs_*`), `analysis/lidar-xsec/` |
| `rainfall/` | `segments_table.pkl` (`gid`, `down`, `area_km2`, `region`, `lon`, `lat`, `x`, `y`, `vi`), `tiles/*/local_catchment_core.tif` (pixels per gid; one tile's gids per raster), `tiles/*/area_code.tif` (land), `relink_log.csv` (status), `unresolved_gt200.csv`, `councils.gpkg`, `large_catchments.gpkg` | `rain/weights_*.npz`, `network.npz`, `scaling_zones.npz`, `totals.npz`; the `rain-inputs` branch (refresh after a rebuild) |
| `viewer_nz.py`, `viewer.py` | `regions/*/segments_scored.gpkg` (25 columns + `z_*`), `tiles/*/area_code.tif`, `truncated_core.tif`, `hillshade_24m.tif`, `local_catchment_core.tif`, `tile.json`, `rec2.gpkg`; reference `data/<region>/` | `data/viewer_nz/<slug>.json`, `_seg.txt`, `_hs.jpg`, `_area.png`, `_trunc.png`, `_catch.png`, `regions_index.json` |
| `webdata.py`, `aerial.py`, `linz.py` | `segments_table.pkl` (sections, hydro), `hydro.pkl`, the viewer's reach lines (aerial) and area grid (linz), `access.gpkg`, `linz/<vid>.gpkg` | `<vid>_sections.json`, `_hydro.json`, `_aerial.json`, `_access.json`, `_linz.json` |
| `validate.py`, `export.py` | reference areas only: `data/<region>/area_km2.tif`, `rec2_nztm.geojson`, rasters | `validation_*`, `gis/` |
| web app (`web/core`) | `Region.segments`: `c d u len g s50 a maf zt zb mel hw gorge head ksn pow hard r reg sig lab ho sta gr lho z`, `dRoad dTrack dFoot fallsN fallsH` | - |

Side tables keyed by row order (`external.pkl`, `hydro.pkl`: `national_table` order) or by viewer index (`vi`: score
blobs, rain files, `_aerial.json`, sections' `m`) are all recomputed after the rebuild; none can be carried over.

## The tile job's extraction (in order)

After routing (rf-lxa.6), with the 2 m grid in memory:

1. **Reaches**: links and ~250 m reaches from the directions (`lidarpipe.segments`), `area_tile_km2`, lines.
2. **2 m pass** (one traversal of the 361 M cells): each cell's first reach (the local catchment), and per reach the
   sums `local_km2`, `local_z_mean_m`, `local_z_max_m`, `local_flat_km2`, `local_hard_km2`, `local_lidar_km2`,
   `local_lake_km2`, and the HAND counts (cells within 1, 2, 5, 10, 20 m above their channel cell). Gorge depth from
   a 150 m maximum of the 10 m block maximum, read at the channel cells. Seam distance and lake share.
3. **1 m re-read** along the reaches in 8 km chunks: the blocks within ~165 m of the reaches of 1 km2 or more
   (sections) and within 4 m of all reaches (profiles).
4. **Profiles**: per link, stations every 1 m of smoothed distance; lowest 1 m cell within 2 m; station codes; per
   reach `lidarpipe.reach_profile`; steps per link (`profile_steps`) given to the reach holding their middle
   (`reach_steps`).
5. **Sections** (rf-lxa.4): cast on the 2 m lines with a 10 m search, measured, encoded.
6. **Products and files** (below), `tile.json` done flag.

The router fills the DEM in place: keep a float32 copy of the raw 2 m block mean (+1.4 GB) for gorge depth and the
2 m part of the sections, or they read the filled DEM (pits and lakes flattened).

| step | per full tile (s) | memory |
|---|---:|---|
| 2 m pass: local catchment, local sums, HAND counts | 15-25 | int32 local catchment 1.4 GB, target height 1.4 GB |
| gorge depth, seam distance, slope < 4 deg on 10 m | ~5 | small |
| 1 m re-read (8 km chunks) | 15-25 | 0.3 GB per chunk |
| profiles: sampling and codes | ~1 | small |
| per-reach profile fields, steps | ~5 | small |
| sections (~40,000-50,000 at 0.32 ms) | ~15 | small |
| products and writes | ~10 | small |
| **total** | **~65-85** | filled + raw DEM, directions, local catchment, target heights: ~6 GB (~7.6 GB while the accumulation is still held) |

The dry run read 89 % of the sub-area's blocks for a ±4 m corridor (the blocks are 512 m and the West Coast network is
dense), so in steep terrain the re-read is close to a second full read of the box (~1.4 G cells, ~20 s at 70 M
cells/s). Country: ~200 tiles x ~75 s = **~4 h** on top of the ~15 h of streaming and routing.

## Reach ids and tiles

- **Tiles**: 32 km cores on the national grid (`nz.X0`, `nz.Y0`), each a quarter of a 64 km tile, with a 3 km halo.
  Id `q{ix:02d}y{iy:02d}` (`lidarspec.tile_id`): `q10y14` is a quarter of today's `x05y07`. `nz.Tile.parse` reads it
  as (10, 14), but `Tile.core` and `Tile.box` assume 64 km: the tile class needs a size (consumer change).
- **gid** = tile index x 1e6 + local id, tile index = 10,000 + 100 iy + ix (`lidarspec.gid`). New gids are at least
  1e10, so they never equal an 8 m gid (below 2.3e9) and a stale table cannot join by accident. `gid // 1e6` still
  gives the tile, as the rain weights expect. int64 everywhere (it already is).
- **Local ids** are given in a fixed order (links in raster order of their head cell, reaches down each link), so a
  tile re-run on the same data and code gives the same ids. A change of data or code in a tile changes its ids; the
  tile job writes all of a tile's id-keyed outputs at once, and the merge and the joins are re-run after any re-run.
- **Ownership**: a reach belongs to the tile whose core holds its line midpoint (as today). Halo reaches are dropped
  after stitching. Each box cell is counted, in `local_km2` and in the local-catchment raster, only by the tile that
  owns the cell's first reach, so local areas add up across tiles with no double counting.
- **Merge** (rf-lxa.5) never changes a gid. It sets `down_gid` (stitch at the edge, lakes as nodes, REC2 relink as
  the fallback), `stitch`, `outlet_kind`, `lake_id`, `area_km2`, `truncated`. nz score re-numbers `seg_id` and
  `down_seg` per region, and `vi`, as today.
- **Build id**: every `tile.json` and national table carries a build id (e.g. `lidar1m-2026-12`); consumers that keep
  their own copies (rain inputs, web data) check it, not only the reach counts.

## Field tables

#### Identity and network

| field | unit | dtype | stage | definition | today (8 m) | used by |
|---|---|---|---|---|---|---|
| `gid` |  | int64 | 2m | reach id: tile index x 1e6 + local id (``gid``); never changed by the merge | tile index x 1e6 + seg_id (64 km tiles) | every keyed table, rain, xsec, lidar |
| `tile` |  | str | 2m | 32 km tile id (``tile_id``) | 64 km tile id | merge, lidar |
| `seg_id` |  | int32 | 2m | local id in the tile; nz score re-numbers it per region (row order) | same | scoring, hydro, external, models |
| `link` |  | int32 | 2m | channel link (head or confluence to the next confluence) in the tile; profiles and steps are measured per link | same | steps |
| `down_seg` |  | int32 | 2m | downstream reach (tile-local row; region-local after nz score) | same | scoring, labels, viewer |
| `down_gid` |  | int64 | merge | downstream reach after stitching across tile edges, through lake nodes (rf-lxa.5); -1 at an outlet | stitch 24 m + REC2 relink | nz score, models, rain |
| `end_x` | m | float64 | 2m | NZTM x of the line's last vertex | same | merge |
| `end_y` | m | float64 | 2m | NZTM y of the line's last vertex | same | merge |
| `exits_core` |  | bool | 2m | the reach drains out of the tile's core | same | merge |
| `inflow` |  | bool | 2m | a REC2 point inflow enters on the reach (in-tile area only) | lidarpipe.segments | area check |
| `truncated` |  | bool | merge | part of the upstream network ends at a box edge with no stitch or inflow (its area is too small) | cell flag at the reach middle (truncated.tif) | viewer, rain flag 16 |
| `outlet_kind` |  | uint8 | merge | 0 not an outlet, 1 sea, 2 inland sink or lake without an outlet, 3 unresolved box edge | relink_log.csv / unresolved_gt200.csv | rain, QA |
| `stitch` |  | uint8 | merge | how down_gid was set: 0 inside the tile, 1 stitched at a tile edge, 2 through a lake node, 3 REC2 relink (fallback), 4 none | relink_log.csv status | rain, QA |
| `lake_id` |  | int32 | merge | the national lake table's lake the reach lies in or ends in (-1) | new | merge, hydro, viewer |
| `geometry` | m | line | 2m | the reach's 2 m cell-centre path (upstream to downstream, ending on the next reach's first cell), simplified at 1 m | 8 m D8 path, a vertex every ~25 m | viewer, external, aerial, lidar, labels, sections |
| `region` |  | str | merge | river-guide region of the line midpoint (national.assign_regions) | same | nz score, viewer, rain, hydro |
| `vi` |  | int32 | merge | index in the region's viewer arrays (reaches of 2 km2 or more), -1 not shown | same | viewer scores, rain, aerial, sections |

#### Catchment area

| field | unit | dtype | stage | definition | today (8 m) | used by |
|---|---|---|---|---|---|---|
| `area_km2` | km2 | float32 | merge | area at the reach bottom: network accumulation of local_km2 over the stitched national network, lakes as single nodes (rf-lxa.5) | flow accumulation in the tile + REC2 inflows at the box edge | everything |
| `area_mid_km2` | km2 | float32 | merge | mean area along the reach: area_tile_mid_km2 x area_km2 / area_tile_km2 | in-tile mean | ksn, melton, MAF and name matching |
| `area_tile_km2` | km2 | float32 | 2m | in-tile area at the reach bottom with REC2 inflows (today's method; kept as a check) | area_km2 | area_check |
| `area_tile_mid_km2` | km2 | float32 | 2m | in-tile mean area along the reach body | area_mid_km2 | area_mid_km2 |
| `local_km2` | km2 | float32 | 2m | area of the box cells (core and halo) that first drain into the reach; each cell is counted once, by the tile whose core holds the reach (lidarpipe.local_areas) | relink.tile_local_km2 (pixels of the core only) | merge, relink, rain |
| `area_check` | ratio | float32 | merge | area_tile_km2 / area_km2 (flag reaches off by > 10 %) | new | QA |

#### 1 m long profile

| field | unit | dtype | stage | definition | today (8 m) | used by |
|---|---|---|---|---|---|---|
| `length_m` | m | float32 | 1m (store) | length of the 2 m line after a 20 m moving average (lidarpipe.smooth_distance): no D8 staircase | D8 cell-step length on 8 m | scoring weights, runs, labels, hydro |
| `z_top` | m | float32 | 1m (store) | 1 m profile (running minimum along the link) at the reach's first station, NZVD2016 | filled 8 m DEM at the path's top | runs, viewer |
| `z_bot` | m | float32 | 1m (store) | the same at the reach's end (the next reach's first station) | filled 8 m DEM at the path's end | runs, viewer |
| `gradient_m_per_km` | m/km | float32 | 1m (store) | (z_top - z_bot) / length_m | filled 8 m drop / D8 length | scoring (grad_s), labels |
| `steepest_50m_m_per_km` | m/km | float32 | 1m (store) | steepest 50 m of the running-minimum profile inside the reach (the reach gradient when shorter than 45 m) | the same on the filled 8 m profile | FEATURES steep50, viewer s50, external check |
| `steepest_100m_m_per_km` | m/km | float32 | 1m (store) | the same over 100 m (robust over flattened treads, rf-lxa.1) | new | ML candidate, web |
| `steepest_200m_m_per_km` | m/km | float32 | 1m (store) | the same over 200 m | new | ML candidate, web |
| `steep50_water` |  | bool | 1m (store) | the steepest 50 m lies mostly on flattened or planar water (a survey riser or interpolation, not a fall) | new | QA, ML |
| `longest_flat_m` | m | float32 | 1m (store) | longest stretch dropping less than 5 cm (rf-a33) | new | QA |
| `ksn` | m^0.9 | float32 | merge | gradient / 1000 x (area_mid_km2 x 1e6) ^ 0.45 | same (in the tile) | ksn_s, knick_ratio |
| `knick_ratio` | ratio | float32 | merge | ksn / max(ksn of the reach below, 1) | same (in the tile) | FEATURES knick, external check |
| `sinuosity` | ratio | float32 | ext (store) | length_m / straight distance between the line's ends | new | ML candidate |
| `sinuosity_1km` | ratio | float32 | merge (store) | channel length / straight distance over 1 km of main stem centred on the reach (stored lines) | new | ML candidate |

#### Steps and drops (waterfall and rapid candidates)

| field | unit | dtype | stage | definition | today (8 m) | used by |
|---|---|---|---|---|---|---|
| `steps_n` |  | int16 | 1m (store) | bed steps of 1 m or more (lidarpipe.profile_steps; not risers on flattened water, not planar water) | OSM falls_n (mapped falls only) | ML candidate, web |
| `steps_drop_m` | m | float32 | 1m (store) | sum of the bed steps' heights | new | ML candidate |
| `step_max_m` | m | float32 | 1m (store) | tallest bed step | OSM falls_max_h_m | ML candidate, web |
| `step_max_s_m` | m | float32 | 1m (store) | position of the tallest step, m from the reach top | new | web |
| `steps_share` | share | float32 | 1m (store) | steps_drop_m / reach drop (drop >= 0.5 m) | new | ML candidate |
| `drops_n` |  | int16 | 1m (store) | bed steps of 2 m or more | new | ML candidate, web |
| `falls1m_n` |  | int16 | 1m (store) | bed steps of 5 m or more (waterfall candidates) | new | ML candidate, web, OSM check |
| `risers_n` |  | int16 | 1m (store) | steps between hydro-flattened treads (survey breaklines) | new | QA |

#### Valley and gorge (2 m grid)

| field | unit | dtype | stage | definition | today (8 m) | used by |
|---|---|---|---|---|---|---|
| `gorge_depth_m` | m | float32 | 2m | mean over the reach cells of the highest 2 m cell within +-150 m (square) minus the channel cell | same on the 8 m DEM | FEATURES gorge, runs, viewer, audit |
| `valley_width_m` | m | float32 | 2m | area of the cells within 5 m above their own channel cell (HAND on the filled 2 m DEM) per metre of reach | same on the 8 m DEM | EXTRA valley_w, reachtype, audit |
| `height_to_width` | ratio | float32 | 2m | gorge_depth_m / valley_width_m | same | FEATURES h_to_w, runs, viewer hw |
| `hand_w1_m` | m | float32 | 2m | as valley_width_m with HAND <= 1 m (proposed: cheap, needs the 2 m grid) | new | ML candidate |
| `hand_w2_m` | m | float32 | 2m | the same with HAND <= 2 m (proposed) | new | ML candidate |
| `hand_w10_m` | m | float32 | 2m | the same with HAND <= 10 m (proposed) | new | ML candidate |
| `hand_w20_m` | m | float32 | 2m | the same with HAND <= 20 m (proposed) | new | ML candidate |

#### Catchment statistics (local sums per tile, accumulated after the merge)

| field | unit | dtype | stage | definition | today (8 m) | used by |
|---|---|---|---|---|---|---|
| `local_z_mean_m` | m | float32 | 2m | mean filled 2 m elevation of the reach's local catchment | new | head_m |
| `local_z_max_m` | m | float32 | 2m | highest 2 m cell of the local catchment | new | relief_m |
| `local_flat_km2` | km2 | float32 | 2m | local area with slope < 4 deg (slope of the 10 m block mean, close to today's 8 m) | new | upstream_flat_frac |
| `local_hard_km2` | km2 | float32 | 2m | local area on resistant rock (GNS 1:250k rasterised per tile) | new | upstream_hard_frac |
| `local_lidar_km2` | km2 | float32 | 2m | local area whose 2 m cells are at least half valid 1 m cells | new | catch_lidar_share |
| `local_lake_km2` | km2 | float32 | 2m | local area on filled lake holes or flattened lakes (proposed) | new | upstream_lake_frac |
| `head_m` | m | float32 | merge | mean elevation of the whole upstream catchment (local means weighted by local_km2) minus the profile at the reach's middle | same on 8 m (per-cell accumulation) | FEATURES head, viewer |
| `relief_m` | m | float32 | merge | highest cell of the whole upstream catchment minus the profile at the reach's middle | same on 8 m | melton |
| `melton` |  | float32 | merge | relief_m / sqrt(area_mid_km2 x 1e6) | same | FEATURES melton, run finder H/A, viewer mel |
| `upstream_flat_frac` | share | float32 | merge | accumulated local_flat_km2 / area_km2 | same on the 8 m filled DEM | FEATURES plateau |
| `upstream_hard_frac` | share | float32 | merge | accumulated local_hard_km2 / area_km2 | same | FEATURES hard_up |
| `upstream_lake_frac` | share | float32 | merge | accumulated local_lake_km2 / area_km2 (proposed: lakes damp floods) | new | ML candidate, hydro |
| `catch_lidar_share` | share | float32 | merge | accumulated local_lidar_km2 / area_km2 | lidar.py (footprints on 8 m local catchments) | QA, card v5 strata |
| `karst_up_frac` | share | float32 | merge | local_km2 x karst_bed accumulated / area_km2 (ml.add_geology, midpoint geology) | same (local area from area differences) | EXTRA karst_up |

#### LiDAR source, lakes and water surfaces

| field | unit | dtype | stage | definition | today (8 m) | used by |
|---|---|---|---|---|---|---|
| `dem_source` |  | category | 1m (store) | lidar (>= 95 % of the stations on 1 m data), mixed, 8m (< 5 %) | lidar.source_flag (footprints) | QA, card v5 |
| `lidar_share` | share | float32 | 1m (store) | share of the reach's stations with a 1 m cell within 2 m (the rest are on the 2 m grid: 8 m fallback or lake fill) | footprint share along the line (lidar.py) | QA, section domain |
| `dem_res_m` | m | float32 | 1m (store) | resolution of the data the profile was measured on: 1 (lidar) or 8 (mixed, 8m) | new | QA, card v5 |
| `seam_dist_m` | m | float32 | 2m | distance from the reach line to the nearest LiDAR / 8 m fallback seam (10 m work grid, capped at 5 km) | new | QA |
| `lake` |  | bool | 2m | at least half of the reach's stations on a filled lake hole or a flattened lake of the national lake table | new | QA, viewer, hydro, scoring domain |
| `lake_share` | share | float32 | 2m | share of the reach's stations on a lake | new | QA |
| `water_flat_share` | share | float32 | 1m (store) | share of the stations on hydro-flattened 1 m cells | new | QA, gradient and step reliability |
| `water_planar_share` | share | float32 | 1m (store) | share of the stations on planar (interpolated) 1 m cells | new | QA |
| `lidar_survey` |  | category | ext | survey slug over most of the reach (mosaic capture-dates footprints) | lidar_slug (reach_coverage.pkl) | QA, web |
| `lidar_year` | year | int16 | ext | end year of that survey's flights | same | QA, web |
| `lidar_year_start` | year | int16 | ext | start year of that survey's flights | same | QA |

#### Names, lines and REC2

| field | unit | dtype | stage | definition | today (8 m) | used by |
|---|---|---|---|---|---|---|
| `river` |  | str | merge | REC2 name (nearest REC2 reach within 150 m, area 0.6-1.6x), else river_linz | REC2 name only | labels, scoring, models, hydro, viewer |
| `river_rec2` |  | str | merge | the REC2 name alone | new |  |
| `river_linz` |  | str | ext | name of the LINZ river line (LDS 103632) within 30 m over most of the reach | new | river |
| `linz_dist_m` | m | float32 | ext | median distance from the reach's 25 m sample points to the nearest LINZ river line, capped at 500 m | new | QA, rf-3ri.7 check |
| `linz_section_id` |  | int64 | ext | river_section_id of that LINZ line | new | web |
| `rec2_nzsegment` |  | int64 | merge | REC2 reach matched along the network (area ratio + line distance, rf-33a.4) | not kept (validate.py snaps at outlets) | validation, relink, names |
| `rec2_area_km2` | km2 | float32 | ext | REC2 CUM_AREA of that reach | validate.py (reference areas) | validation |
| `rec2_dist_m` | m | float32 | ext | distance from the reach midpoint to that REC2 line | new | validation |

#### Flood (MAF)

| field | unit | dtype | stage | definition | today (8 m) | used by |
|---|---|---|---|---|---|---|
| `specific_maf` | m3/s/km2 | float32 | merge | H&C 2018 MAF / area of the nearest REC1 reach within 400 m with area 0.4-2.5x area_mid_km2 | same (unmatched: tile median) | maf, scoring |
| `maf_m3s` | m3/s | float32 | merge | specific_maf x area_km2 (corrected by maf.apply when on) | same | everything |
| `specific_maf_matched` |  | bool | merge | a REC1 reach was matched | same | maf refill |
| `rec1_nzreach` |  | int64 | merge | the matched REC1 reach (NZREACH), so the MAF can be redone without matching | new | maf |

#### Geology

| field | unit | dtype | stage | definition | today (8 m) | used by |
|---|---|---|---|---|---|---|
| `bed_hard` | 0/1 | int8 | ext | resistant rock at the reach midpoint | same | FEATURES bed_hard, runs, viewer |
| `bed_rock` |  | str | ext | GNS rock group at the midpoint | same | GIS export |
| `rockclass` |  | category | ext | GNS rock class at the midpoint (ml.add_geology) | same | ML categorical |
| `rockgroup` |  | category | ext | GNS rock group at the midpoint | same | ML categorical |
| `mainrock` |  | category | ext | GNS main rock at the midpoint | same | karst_bed |
| `rock_age_ma` | Ma | float32 | ext | minimum age of the bed rock unit | same | EXTRA rock_age |
| `karst_bed` | 0/1 | int8 | ext | bed on limestone, marble, dolomite ... | same | EXTRA karst_bed |

#### Cross-sections (1 m sections, rf-lxa.4)

| field | unit | dtype | stage | definition | today (8 m) | used by |
|---|---|---|---|---|---|---|
| `x1_n` |  | int16 | 1m (store) | kept sections | xs_n (8 m) | ML, QA |
| `x1_n_all` |  | int16 | 1m (store) | all sections | new | ML, QA |
| `x1_drop_share` |  | float32 | 1m (store) | share of sections dropped | new | ML, QA |
| `x1_edge_share` |  | float32 | 1m (store) | share flagged EDGE | new | ML, QA |
| `x1_twin_share` |  | float32 | 1m (store) | share flagged TWIN | new | ML, QA |
| `x1_low_share` |  | float32 | 1m (store) | share LOW | new | ML, QA |
| `x1_far_share` |  | float32 | 1m (store) | share FAR | new | ML, QA |
| `x1_flatw_frac` |  | float32 | 1m (store) | kept sections on flattened water | new | ML, QA |
| `x1_planw_frac` |  | float32 | 1m (store) | kept sections on planar water | new | ML, QA |
| `x1_nodata` |  | float32 | 1m (store) | no-data share of the profiles | new | ML, QA |
| `x1_w0_med` |  | float32 | 1m (store) | median of the kept sections: width at the 0.1 m stage: the water surface width at capture | new | ML candidates |
| `x1_b_med` |  | float32 | 1m (store) | median of the kept sections: shape exponent w ~ h^b (keep: x1_b) | new | ML (keep list) |
| `x1_m_med` |  | float32 | 1m (store) | median of the kept sections: shape exponent A ~ h^m | new | ML candidates |
| `x1_ff1_med` |  | float32 | 1m (store) | median of the kept sections: form factor at 1 m | new | ML candidates |
| `x1_ffbf_med` |  | float32 | 1m (store) | median of the kept sections: form factor at bankfull (keep: x1_ffbf) | new | ML (keep list) |
| `x1_hb_use_med` |  | float32 | 1m (store) | median of the kept sections: bankfull stage used (5 m without a break) | new | ML candidates |
| `x1_wbf_med` |  | float32 | 1m (store) | median of the kept sections: bankfull width | new | ML candidates |
| `x1_abf_med` |  | float32 | 1m (store) | median of the kept sections: bankfull area | new | ML candidates |
| `x1_pbf_med` |  | float32 | 1m (store) | median of the kept sections: bankfull perimeter | new | ML candidates |
| `x1_wd_bf_med` |  | float32 | 1m (store) | median of the kept sections: bankfull width / stage | new | ML candidates |
| `x1_rbf_med` |  | float32 | 1m (store) | median of the kept sections: hydraulic radius at bankfull | new | ML candidates |
| `x1_er_med` |  | float32 | 1m (store) | median of the kept sections: entrenchment ratio | new | ML candidates |
| `x1_ang_med` |  | float32 | 1m (store) | median of the kept sections: bank angle, both sides (keep: x1_bank) | new | ML (keep list) |
| `x1_ang_hi_med` |  | float32 | 1m (store) | median of the kept sections: steeper bank angle | new | ML candidates |
| `x1_ang_lo_med` |  | float32 | 1m (store) | median of the kept sections: gentler bank angle | new | ML candidates |
| `x1_steep_med` |  | float32 | 1m (store) | median of the kept sections: share of the bank in > 60 deg steps (keep: x1_steep) | new | ML (keep list) |
| `x1_steep_hi_med` |  | float32 | 1m (store) | median of the kept sections: the steeper side's share | new | ML candidates |
| `x1_asym_w_med` |  | float32 | 1m (store) | median of the kept sections: width asymmetry at 2 m | new | ML candidates |
| `x1_asym_ang_med` |  | float32 | 1m (store) | median of the kept sections: bank-angle asymmetry | new | ML candidates |
| `x1_wall_lo_med` |  | float32 | 1m (store) | median of the kept sections: lower side's highest ground within 150 m (keep: x1_wall) | new | ML (keep list) |
| `x1_wall_hi_med` |  | float32 | 1m (store) | median of the kept sections: higher side's | new | ML candidates |
| `x1_bank_lo_med` |  | float32 | 1m (store) | median of the kept sections: lower side's highest ground within 50 m | new | ML candidates |
| `x1_vf_med` |  | float32 | 1m (store) | median of the kept sections: valley floor width-to-height (keep: x1_vf) | new | ML (keep list) |
| `x1_aoff_med` |  | float32 | 1m (store) | median of the kept sections: distance of the channel point from the line | new | ML candidates |
| `x1_hb_med` |  | float32 | 1m (store) | median of the kept sections: bankfull stage (break only) | new | ML candidates |
| `x1_brk_frac` |  | float32 | 1m (store) | share of sections with a bankfull break | new | ML |
| `x1_b_iqr` |  | float32 | 1m (store) | spread of b along the reach | new | ML |
| `x1_w1_cv` |  | float32 | 1m (store) | variation of the 1 m width along the reach | new | ML |
| `x1_hb_cv` |  | float32 | 1m (store) | variation of the bankfull stage | new | ML |
| `x1_pinch_frac` |  | float32 | 1m (store) | share of pinch points (narrower than half the median at 1 m) | new | ML |
| `x1_rough` |  | float32 | 1m (store) | bed roughness: robust spread of the detrended 1 m long profile (keep) | new | ML |
| `x1_dropconc` |  | float32 | 1m (store) | share of the drop in the steepest 10 % of the 1 m long profile (keep) | new | ML |
| `x1_grad1m` |  | float32 | 1m (store) | gradient of the 1 m long profile through the section points | new | ML |
| `x1_prof_len` |  | float32 | 1m (store) | length of that profile | new | ML |
| `x1_{w,a,p}<s>_med`, `x1_w<s>_min`, `x1_cens<s>` | m, m2, m, m, share | float32 | 1m (store) | stage table: median width, area, perimeter, narrowest width and censored share at the 15 stages <s> = 0p1, 0p25, 0p5, 0p75, 1, 1p5, 2, 2p5, 3, 4, 5, 7p5, 10, 15, 20 | `xs_*` at 1, 2, 5, 10, 20 m (8 m) | hydraulics after the merge, ML |

#### Other external data

| field | unit | dtype | stage | definition | today (8 m) | used by |
|---|---|---|---|---|---|---|
| `d_road_m` | m | float32 | ext | distance to the nearest road (OSM via Overture) | same | viewer, audit (never a model input) |
| `d_track_m` | m | float32 | ext | distance to the nearest 4WD or private road | same | viewer |
| `d_foot_m` | m | float32 | ext | distance to the nearest foot track | same | viewer |
| `falls_n` |  | int16 | ext | OSM waterfalls snapped within 100 m | same | EXTRA falls, viewer |
| `falls_max_h_m` | m | float32 | ext | tallest mapped OSM waterfall | same | EXTRA falls_h, viewer |
| `rapids_n` |  | int16 | ext | OSM rapids within 150 m of the midpoint | same | EXTRA rapids |
| `hydro` |  | category | ext | '', diverted, regulated, reservoir (hydro.py, config/hydro.toml) | same | web, audit |
| `hydro_scheme` |  | str | ext | the scheme that contributes most | same | web, audit |
| `hydro_point` |  | str | ext | the point that contributes most | same | web |
| `hydro_share` | share | float32 | ext | share behind the class | same | web |
| `div_share` | share | float32 | ext | diverted share | same | hydro |
| `reg_share` | share | float32 | ext | regulated share | same | hydro |

#### Derived where used (not stored by the pass)

| field | unit | dtype | stage | definition | today (8 m) | used by |
|---|---|---|---|---|---|---|
| `up_seg` |  | int32 | use | main upstream reach (largest area) | same | runs, viewer |
| `down` |  | int64 | use | downstream row in the national table | same | models, ml, rain, hydro |
| `up` |  | int64 | use | main upstream row in the national table | same | models, hydro |
| `x` | m | float64 | use | NZTM x of the line midpoint | same | models, ml, hydro, zones |
| `y` | m | float64 | use | NZTM y of the line midpoint | same | models, ml, hydro, zones |
| `lon` | deg | float64 | use | longitude of the midpoint | same | models, rain |
| `lat` | deg | float64 | use | latitude of the midpoint | same | models, rain |
| `rnorm` |  | str | use | normalised river name (models.norm_river) | same | labels, hydro |
| `guide_region` |  | str | use | region used for labels and LORO (= region) | same | ml, models |
| `outlet` |  | int64 | use | row of the reach's sea outlet (CV folds) | same | ml |
| `grad_s` | m/km | float32 | use | gradient over the main upstream reach, the reach and the one below (scoring.prepare) | same | everything |
| `ksn_s` |  | float32 | use | ksn smoothed the same way | same | FEATURES ksn, viewer |
| `power_kw_m` | kW/m | float32 | use | rho g MAF grad_s | same | FEATURES power, viewer |
| `maf_raw_m3s` | m3/s | float32 | use | MAF before maf.apply | same | maf |
| `maf_flag` |  | str | use | what maf.apply changed | same | maf |
| `confinement_ratio` | ratio | float32 | use | valley_width_m / (4 MAF^0.5) (reachtype) | same | EXTRA confine |
| `reach_type` |  | category | use | Montgomery & Buffington type (reachtype) | same | ml strata |
| `style` |  | category | use | paddling style (reachtype) | same | auc_style |
| `style_p` |  | float32 | use | probability of that style | same | ml |
| `transitional` |  | bool | use | between two adjacent types | same | ml |
| `label` |  | int8 | use | pooled-regression training label (nz score) | same | models, viewer |
| `holdout` |  | bool | use | held out of the pooled regression | same | models, viewer |
| `score_regression` | pct | float32 | use | pooled regression percentile | same | viewer reg |
| `score_signature` | pct | float32 | use | similarity to known runs, percentile | same | viewer sig |
| `score_composite` | pct | float32 | use | composite score, percentile | same | runs |
| `run_id` |  | int32 | use | run of the default run finder (-1 none) | same | runs.geojson |
| `p_colluvial` |  | float32 | use | reach-type probability (reachtype) | same | ml |
| `p_bedrock` |  | float32 | use | reach-type probability (reachtype) | same | ml |
| `p_cascade` |  | float32 | use | reach-type probability (reachtype) | same | ml |
| `p_step_pool` |  | float32 | use | reach-type probability (reachtype) | same | ml |
| `p_plane_bed` |  | float32 | use | reach-type probability (reachtype) | same | ml |
| `p_pool_riffle` |  | float32 | use | reach-type probability (reachtype) | same | ml |
| `p_dune_ripple` |  | float32 | use | reach-type probability (reachtype) | same | ml |
| `x1_h_q` |  | float32 | use | Manning flood stage on the median section (xsec1m.derive: MAF, grad_s) | xs_h_q (8 m) | registry, web |
| `x1_w_q` |  | float32 | use | flood width on the median section (xsec1m.derive: MAF, grad_s) | xs_w_q (8 m) | registry, web |
| `x1_v_q` |  | float32 | use | flood velocity (x1_vel, registry only) on the median section (xsec1m.derive: MAF, grad_s) | xs_v_q (8 m) | registry, web |
| `x1_omega_q` |  | float32 | use | flood unit stream power (dropped from recipes) on the median section (xsec1m.derive: MAF, grad_s) | xs_omega_q (8 m) | registry, web |

## Stored raw data

### 1 m long profile (per reach)

`tiles/<t>/profiles.arrow`: Feather v2 (Arrow IPC), zstd level 9, one row per core reach. `lidarspec.PROFILE_STORE`,
codec `lidarpipe.encode_profiles` / `decode_profiles`.

| column | type | content |
|---|---|---|
| `gid` | int64 | reach id |
| `n` | int32 | stations |
| `z0_cm` | int32 | height of the first station, cm NZVD2016 |
| `dz` | list<int16> | n - 1 differences to the station before, cm |
| `w4` | list<uint8> | station codes, 2 bits, four per byte: 0 bed or dry, 1 hydro-flattened water, 2 planar water, 3 not on 1 m data |

- **Stations**: every 1 m of 20 m-smoothed distance along the reach's 2 m line, from its top (position by linear
  interpolation along the cell path). Height: the lowest 1 m cell within 2 m (5 x 5 cells), the rf-lxa.4 rule for 2 m
  lines. Where no 1 m cell is near (8 m fallback, lake fill), the height is the 2 m grid's and the code is 3, so every
  station has a height. The code is that of the 1 m cell the height comes from (`lidarpipe.water_flags` rule).
- The **raw** heights are stored, not the running minimum: bridges, logs and bank hits stay visible, and any later
  rule can be applied. Profiles are run along the link (reaches in order) for the running minimum and the steps.
- **Size**: 0.83 bytes per station in the dry run (West Coast mountains; 94 % of the bytes are the height
  differences), **830 bytes per km**. With ~340,000 km of reach line (today's 271,000 km x 1.25: the 2 m lines are
  longer and the LiDAR network denser) the country is **~0.3 GB**.

### Cross-sections (per section)

`tiles/<t>/xsec.arrow`, exactly the rf-lxa.4 storage spec (`lidarspec.SECTION_STORE`): per section `gid`, station,
channel point (cm), normal, `z0`, flags, 651 heights above `z0` (1 m within ±150 m, 2 m to ±500 m) as int16 cm outward
differences plus 82 bytes of no-data bits; ~620 bytes per section. Every 25 m on reaches of 1 km2 or more. With the
2 m lines (about 23 % more km of reaches of 1 km2 or more than today's 8 m lines in the dry-run box): ~216,000 km under
LiDAR = **5.4 GB**, plus the fallback reaches (+17 %) = **6.3 GB**; at 50 m half of that. Per reach,
`tiles/<t>/x1_reach.arrow` holds every `x1_*` field of `xsec1m.reach_table`.

### Steps (per step)

`tiles/<t>/steps.arrow` (`lidarspec.STEP_STORE`): `gid`, `s0` (m from the reach top), `x_cm`, `y_cm`, `h`, `h_excess`,
`len`, `slope_max`, `z_top`, `kind`. 15 bytes per step; the dry run's 12.5 steps per km is steep country, so the
country is ~2-4 M steps, **~0.05 GB**. The steps are recomputable from the profiles, so the table is a convenience for
the map and the models.

**Step definition** (`lidarpipe.profile_steps`, on the running minimum of a link's profile): a 4 m window is steep
where the profile drops at least 1 m in it (25 %) and at least 1 m more than the local gradient over 100 m predicts;
steep windows are trimmed to their metres steeper than max(12.5 %, 1.5 x the local gradient) and merged across gaps
of up to 3 m; a step drops at least 1 m. 1 m is about the smallest step the DEM resolves: the 2 m lowest-cell window
spreads a vertical face over ~4 m. The "excess" rule keeps evenly steep headwaters from becoming one long step (the
first version, without it, called a 428 m drop over 987 m one step). Kinds: `riser` (mostly hydro-flattened water 10
m above and below: a survey breakline, not a fall), `planar` (on interpolated water), `coarse` (touches stations not
on 1 m data), `bed` (the rest). Per reach: `steps_n` (bed steps >= 1 m), `drops_n` (>= 2 m), `falls1m_n` (>= 5 m),
`step_max_m`, `steps_drop_m`, `steps_share`, `risers_n`.

## Files

### Per tile (`data/<root>/tiles/<t>/`; sizes per full 32 km tile)

| file | res | dtype | kept | MB | content | used by |
|---|---:|---|---|---:|---|---|
| `segments_core.gpkg` | | vector | yes | ~7.5 | core reaches, every `1m` and `2m` field, line simplified at 1 m | merge, lidar, maf, xsec1m |
| `local_catchment_core.tif` | 10 m | int64 | yes | ~1.5 | gid of the reach each 10 m cell first drains into (its centre 2 m cell), over the cells the tile owns (core, and halo cells that drain to its core reaches), -1 elsewhere | rain weights, viewer `catch.png`, GIS, later catchment features |
| `area_code.tif` | 10 m | uint8 | yes | ~5.5 | `areacode` of the in-tile 2 m accumulation, maximum per 10 m cell (keeps one-cell channels) | viewer `area.png`, rain land mask, GIS |
| `hillshade_24m.tif` | 24 m | uint8 | yes | ~1.5 | hillshade of the 8 m block mean, averaged to 24 m (as today) | viewer, GIS |
| `profiles.arrow` | 1 m | Feather | yes | ~1.4 | long profiles | later profile features |
| `xsec.arrow` | 1 / 2 m | Feather | yes | ~30 | raw sections | later section features |
| `x1_reach.arrow` | | Feather | yes | ~1.5 | `x1_*` per reach | national `x1.arrow` |
| `steps.arrow` | | Feather | yes | ~0.25 | steps | web map layer, ML |
| `tile.json` | | json | yes | | done flag, build id, counts, timings, memory, inflows, lakes filled | resume, viewer |
| `hand_10m.tif` | 10 m | uint8 | **optional** | ~3.5 | HAND at the 10 m cell centre, 0.25 m steps to 63.5 m | flood and valley map layers, later valley features |
| `dem_10m.tif` | 10 m | float32 LERC 1 cm | **optional** | ~14 | 1 m block mean at 10 m (fallback in the gaps) | later catchment features (slope, relief, aspect per local catchment) without the 1 m data |
| 2 m DEM, filled DEM, directions, accumulation | 2 m | | no | | memory only (rf-lxa.2) | |

The local-catchment raster spans the tile's box where the tile owns the cells (not only the core), so the rain
weights and the viewer see every cell once; each raster still holds only its own tile's gids, as the rain weights
require. With it, any later catchment statistic (rain, land cover, a 10 m DEM's slope) can be summed per local
catchment and accumulated down the network without re-routing.

### National (after the merge)

| file | content | MB |
|---|---|---:|
| `lakes.gpkg` | lake table: polygon, LiDAR level, outlet gid, km2, tiles (rf-lxa.5; needed before the tiles run) | ~20 |
| `network.parquet` | the `merge` fields per gid | ~60 |
| `x1.arrow` | all `x1_reach.arrow` (replaces `xsec.pkl`) | ~300 |
| `steps.arrow` | all tiles' steps | ~55 |
| `regions/<slug>/segments_scored.gpkg`, `segments_table.pkl`, `ml_table.pkl`, `external.pkl`, `hydro.pkl`, `model.json`, `candidates.csv` | as today, with the new columns | ~3,500 |

### National size estimate

| item | country GB |
|---|---:|
| reach lines and tile fields (`segments_core.gpkg`; 2 m simplification: ~1.0) | ~1.5 |
| local catchments 10 m | ~0.3 |
| area code 10 m | ~1.1 |
| hillshade 24 m | ~0.3 |
| 1 m long profiles | ~0.3 |
| sections, 25 m, reaches >= 1 km2, with the fallback (LiDAR only: 5.4; 50 m: 3.1) | ~6.3 |
| `x1` summaries, steps | ~0.4 |
| national tables (scored regions, segments, ML, external, hydro, network) | ~3.5 |
| viewer and web data (~+30 % reaches) | ~0.6 |
| **total** | **~14** |
| optional: HAND 10 m / DEM 10 m | +0.7 / +2.8 |

Today `data/nz/` holds 6.5 GB and the machine has 8 GB free: run the rebuild in a fresh session with at least 20 GB
free (as decided), into its own root.

## Dry run (rf-lxa.2 sub-area)

`data/analysis/lidar-spec/scripts/dryrun.py`: the 2 m bench reaches of the sub-area (1,437,000-1,457,000 E,
5,229,000-5,250,000 N; `variants/s2mean`), the 1 m blocks within 4 m of their paths re-read (32 threads), profiles,
steps and the store as specified.

| | value |
|---|---|
| reaches (>= 0.5 km2), links | 1,955, 451 (today's 8 m network in the box: 1,369 reaches, 336 km) |
| reach length: D8 path / smoothed | 481 km / 422 km (smoothed / D8 median 0.88) |
| 1 m stations | 421,980 (no station without a 1 m cell) |
| re-read | 372 M of 420 M cells (89 % of the blocks), 5.1 s at 72.6 M cells/s |
| sampling / per-reach fields and steps / store | 0.2 s / 1.5 s / 0.3 s |
| peak memory | 3.5 GB (a box-sized 1 m array; the tile job uses 8 km chunks) |
| profile store | 349 KB = 0.83 B per station, 830 B per km; round trip exact to 1 cm |
| line vertices per km of D8 path: raw / 0.5 m / 1 m / 2 m simplification | 424 / 219 / 137 / 66 |
| water on the profiles: flattened / planar | 1.1 % / 5.0 % of the stations; steepest 50 m on water in 5.6 % of the reaches |
| 1 m gradient / 2 m own filled gradient (reaches >= 2 km2, median) | 1.16 (smoothed length 12 % shorter than the D8 path) |
| 1 m steepest 50 m / 2 m own (median, p10-p90) | 1.09 (0.93-1.22) |

Steps by catchment size:

| area km2 | reaches | km | gradient m/km (median) | bed steps per km | >= 2 m per km | >= 5 m | median height m | reaches with steps | share of the drop in steps (median) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.5-2 | 948 | 207 | 203 | 16.7 | 14.0 | 955 | 3.2 | 67 % | 0.36 |
| 2-10 | 521 | 113 | 104 | 12.4 | 8.9 | 286 | 2.7 | 58 % | 0.35 |
| 10-100 | 379 | 78 | 32 | 5.1 | 2.9 | 58 | 2.1 | 35 % | 0.31 |
| >= 100 | 107 | 23 | 10 | 0.2 | 0.1 | 1 | 2.4 | 3 % | 0.51 |

- The five OSM waterfalls in the box are 39-67 m (mapped heights). Three lie on a reach of 0.56-1.13 km2, each with a
  detected step 11-20 m away (15-38 m tall: the profile splits a big fall at its ledges). The other two are 48 m and
  330 m from the nearest reach of 0.5 km2 or more: they are on smaller side channels.
- The tallest steps (40-65 m over 13-33 m) are falls on steep side creeks; on the rivers of 10-100 km2 the tallest
  are a chain of 12-28 m steps in one gorge (reaches 1820-1822). No riser was found: this survey interpolates its
  water (planar) rather than flattening it. Check the riser rule on `x12y16` (Wairoa below McLaren Falls, Tutea Falls)
  in the first rf-lxa.6 tile.

## What consumers lose or must change

1. **8 m cross-sections**: `xsec.pkl` and the `xs_*` columns are not produced. `lidarspec.REPLACED` maps each `xs_*`
   column to its `x1_*` field (same stage tags 1, 2, 5, 10, 20 m; `xs_gorge_lo_med` -> `x1_wall_lo_med`), so the
   `ml.EXTRA` builders `xs_w10`, `xs_w20`, `xs_wall`, `xs_h2a`, `xs_pinch`, `xs_omega10`, `xs_vel`, `xs_omega_q` and
   the recipes `gbm_xsec` / `logit_xsec` must be re-pointed or retired; rf-lxa.4 says the keep list replaces them.
   Dropped (no consumer outside `xsec.py`, recomputable from the stored sections): `xs_bank_lo_p90`,
   `xs_bank_hi_med`, `xs_gorge_lo_min`, `xs_flat_frac`, `xs_a_q`.
2. **`truncated_core.tif`** is not kept: `viewer_nz.region_files` and `nz.export_gis` paint the truncation mask from
   the reaches' `truncated` and the local-catchment raster.
3. **`relink_log.csv`, `unresolved_gt200.csv`**: `rainfall.upstream.network_from_files` reads them for rain flag 16;
   it must read `stitch` and `outlet_kind` (or `truncated`).
4. **Reference areas** (`data/kaimai`, `karangahake`, `tutamoe`): with the areas in the national run, their 5 m
   products, `xsec_8m.pkl`, `validate.py`'s `area_km2.tif` and the three reference viewer entries have no 1 m
   successor. `models.reference_frame`, `write_viewer_scores`, `webdata`, `hydro` and `external` handle reference
   frames: those paths go idle. REC2 validation moves to `rec2_nzsegment` / `rec2_area_km2` (rf-33a.4).
5. **Tiles**: `nz.Tile` (64 km core and box), `viewer_nz._tiles_for`, `xsec.run_tiles`, `lidar._tile_index` and
   `nz.run_tiles` assume 64 km tiles; `lidar.tile_coverage` assumes 8 m pixels (64 m2). The tile class needs a size;
   `lidar coverage`'s tile pass is replaced by the in-tile `lidar_share` and `local_lidar_km2`.
6. **`models.national_table`** reads a fixed column list from the scored regions: the new fields reach
   `segments_table.pkl` and `ml_table.pkl` only if it is extended.
7. **Cards v1-v4** read the same column names, but the values change (1 m profiles, 2 m HAND, network areas, LINZ
   names). Their frozen `pool_quantiles` no longer match, and the style strata of the headline metric (`auc_style`)
   shift with the narrower 2 m valley floors. Needs a decision (question 2).
8. **More reaches**: the dry-run box has 43 % more reaches than today (more channel at 2 m; reaches cut at 250 m of
   the longer D8 path). Viewer files, score blobs, rain files and the ML table grow by that much; cutting at 250 m of
   smoothed length brings it to ~+25 % (question 7).
9. **Labels**: `river` adds LINZ names where REC2 has none, and the lines move onto the channel, so name-rule
   placements (`labels.one_path`, `models.map_section`) change. Re-run the placement audit after the rebuild.
10. **Web contract**: `Region.segments` has no slot for the new fields (steps, survey year, lake, `dem_source`);
    adding them is the core owner's call (rf-aq6). `viewer_nz` says "national scan on the LINZ 8 m DEM" and
    `cellsize_m: 8.0` in two places.

Nothing a consumer reads is lost without a named replacement.

## Gaps: 1 m information not in the spec

- **Canopy** (DSM minus DEM): trees over the channel, forest on the walls, logjam hazard. It needs the 1 m DSM, a
  second LINZ dataset; reading it later is a second pass over 1 m data (question 5). Never for routing.
- **Water width on small reaches**: only sections give it (`x1_w0_med`, reaches of 1 km2 or more). The 1 m water
  flags could give a planimetric water width for every reach during the re-read; not specified.
- **Capture date per reach**: the footprints give the survey and its years, not the month. If the post-flood state
  matters (Hawke's Bay, Gisborne after Gabrielle), the item metadata may hold flight dates; joinable later (`ext`).
- **Point density and vertical accuracy**: not in the STAC (rf-lxa.1); only in LINZ survey reports.
- **Lake table before the tiles**: rf-lxa.5 needs one level per lake before the tiles run (lakes split by tiles must
  be filled at one level). That needs the rim cells of the 1 m data: a small pre-pass, not part of the tile pass.
- **Bridges, pools, braids**: not fields, but recomputable from the stored raw profiles (rises above the running
  minimum; flat stretches; several lows in a section, already the `TWIN` flag).
- **Steepest windows across reach ends**: kept inside the reach, as today; steps are found per link, so a fall at a
  reach boundary is not split. Cross-boundary windows are recomputable from the profiles.

## Rebuild order after the pass

1. Tiles (rf-lxa.6). 2. Merge, lake nodes, network areas (rf-lxa.5) -> `network.parquet`. 3. Joins: REC1 MAF, REC2
match and names, LINZ names and `linz_dist_m`, geology, survey and year, `sinuosity`. 4. `nz score`. 5. `models table`.
6. `external attach`, `hydro attach`. 7. `ml table`, CV, card v5. 8. `nz viewer`, `models viewer`, `web data`. 9. `rain
weights`, `rain zones`, network, `rain inputs` (refresh the `rain-inputs` branch). 10. `web build`, Pages.

## Open questions for the user

1. **Reference areas**: retire the per-region 5 m pipeline (`river-finder run kaimai ...`, `data/<region>/`, the three
   reference viewer entries) after the national run, or keep it as a regression check?
2. **Cards v1-v4**: after the rebuild, drop them from the site (they stay with the archived 8 m rows), or re-score
   them on the new rows (same weights, shifted percentiles)?
3. **Optional rasters**: keep a 10 m HAND raster (+0.7 GB) and/or a 10 m DEM (+2.8 GB), so later valley and catchment
   features need no 1 m re-read?
4. **Sections**: every 25 m (5.4 GB LiDAR reaches, 6.3 GB with the fallback) or 50 m (half; seven eighths of the
   gain, rf-lxa.4)? Store sections on fallback reaches?
5. **1 m DSM**: read the DSM in the same pass for canopy fields? It adds a second dataset's reads near the channels.
6. **Proposed extras** (cheap, only possible in the tile job): HAND widths at 1, 2, 10 and 20 m, `local_lake_km2` /
   `upstream_lake_frac`. Keep them?
7. **Reach length**: cut reaches at 250 m of smoothed length (~+25 % reaches against today) or of D8 path as today
   (~+43 % in the dry-run box)?
8. **Output root**: build into `data/nz1m/` and switch `nz_dir()` when it is ready (today's site keeps working), or
   replace `data/nz/`?
9. **Line tolerance**: 1 m (~1.5 GB of lines) or 2 m (~1.0 GB)?
10. **Steps**: is the definition above right for whitewater (1 m minimum, 4 m window, 1 m above a 100 m gradient)? It
    is recomputable from the profiles, so this can also be settled after the pass.

## Reproduce

```bash
uv run python -m river_finder.lidarspec          # the field tables above and the counts per stage
uv run pytest -q tests/test_lidarspec.py tests/test_lidarpipe.py
uv run python data/analysis/lidar-spec/scripts/dryrun.py   # dry run (~15 s, 3.5 GB; needs the rf-lxa.2 bench)
```
