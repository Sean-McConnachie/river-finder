# The 1 m tile job (rf-lxa.6)

The per-tile job of the national rebuild on the 1 m LiDAR DEM, as designed in [`lidar-pipeline.md`](lidar-pipeline.md)
(rf-lxa.2) and specified in [`lidar-extraction-spec.md`](lidar-extraction-spec.md) (rf-lxa.3), proved on two real
tiles. Code: `src/river_finder/nz1m.py` (the job), `src/river_finder/nz1mcheck.py` (the checks below), new pieces in
`lidarpipe.py` (`agg2_into`, `window_reader`, `stream(..., in_reader=True)`) and `xsec1m.py` (`corridor_mask`,
`mask_jobs`). Tests: `tests/test_nz1m.py` (a synthetic tile end to end, plus each new piece).

```bash
uv run river-finder nz1m plan                 # the 32 km land tiles -> data/nz1m/plan.json
uv run river-finder nz1m tile q11y15 q24y33   # the job, resumable per tile -> data/nz1m/tiles/<qid>/
uv run river-finder nz1m check q11y15 q24y33  # benchmark, OSM waterfalls, riser rule -> data/nz1m/analysis/<qid>.json
```

## What the job does

One tile is a 32 km core on the national grid (`nz.X0`, `nz.Y0`) with a 3 km halo: a 38 x 38 km box, 361 M cells at
2 m. Its id is `q<ix>y<iy>` (`q11y15` is a quarter of the 64 km tile `x05y07`). In order:

1. **Stream** the box's 1 m mosaic at full resolution (2048 x 2048 windows on the sheets' block grid, 32 threads;
   no overviews) into the 2 m block mean and a one-byte code per 2 m cell: how many of its four 1 m cells are valid,
   hydro-flattened and planar. The aggregation runs inside the reader threads (`lidarpipe.agg2_into`, numba `nogil`),
   so reading and aggregating overlap (rf-lxa.7). The 8 m contour DEM of the box is read at the same time, in memory.
2. **Sea and lakes**: surveyed water outside the 8 m DEM's land and below 10 m (a harbour the survey flattened) is
   set to no data, so the coast drains to it. A no-data hole is a lake when half its rim is hydro-flattened water, one
   rim level (to 2 cm) holds 40 % of it and the hole is at least half land in the 8 m DEM (the sea is not). It is
   filled flat at that level. A hole whose level is below 1 m is tidal water and stays no data, like the sea. When
   the national lake table exists (`data/nz1m/lakes.gpkg`, rf-lxa.5, [`nz1m-merge.md`](nz1m-merge.md)), a hole with
   at least half of its cells in a table lake takes that lake's level and decision: filled at `level_m` (also when
   the tile's own rim test fails on its part of the rim), or left as sea when the table says `tidal`.
3. **Fallback**: the other land holes take the 8 m DEM (bilinear), corrected by the seam rule with the edge blend
   (`seam_fill`: the rule of `lidarpipe.seam_correct(..., edge_m=25)`, its edge part worked in 2 m chunks so that no
   box-sized distance transform is made).
4. A **float32 copy** of this DEM is kept: the router fills in place, and gorge depth and the 152-500 m part of the
   sections read the unfilled DEM.
5. **Route** at 2 m (`lidarpipe.route_lean`, REC2 border outlets), **accumulate** as uint32 cell counts, add the
   REC2 inflows at the box edge along their paths (held sparsely), take channels of 0.5 km2 and cut reaches every
   ~250 m of 20 m-smoothed length.
6. **2 m pass**: each cell's first reach; per core reach the local sums (`local_km2`, mean and highest elevation,
   flat, hard rock, LiDAR and lake area) and the HAND counts at 1, 2, 5, 10 and 20 m; gorge depth (150 m maximum of
   the 10 m block maximum); seam distance; the 10 m local-catchment, HAND and area-code rasters, the 24 m hillshade.
7. **1 m pass**: the 1 m blocks along the reaches re-read in 8 km chunks (within 4 m of the profile stations, within
   200 m of the section stations), the next chunk read while the current one is measured. Long profiles: a station
   every 1 m of smoothed length on every link with a core reach, the lowest 1 m cell within 2 m and its water code;
   running minimum and steps per link (`lidarpipe.profile_steps`, given to the reach holding their middle).
   Sections every 25 m on core reaches of 1 km2 or more (`xsec1m.extract(..., search=10)`).
8. **Write** the files below and `tile.json` last (the done flag).

Areas in the tile are in-tile areas: local accumulation plus REC2 inflows at the box edge, as in the benchmark. They
pick the channels (0.5 km2) and the section reaches (1 km2), and are written as `area_tile_km2` (a check). No
area-dependent field is computed here.

## Outputs per tile (`data/nz1m/tiles/<qid>/`)

The files of `lidarspec.TILE_FILES` (the optional 10 m DEM is not written, as decided). Sizes on the three real tiles
(all about 1,010 km2 of land in the core):

| file | content | q11y15 MB | q24y33 MB | q25y32 MB |
|---|---|---:|---:|---:|
| `segments_core.gpkg` | core reaches: every `1m` / `2m` field but the `x1_*` (`lidarspec.tile_columns`), line simplified at 1 m | 4.6 | 6.1 | 5.2 |
| `x1_reach.arrow` | `gid` + every `x1_*` field, one row per core reach | 1.1 | 1.5 | 1.3 |
| `xsec.arrow` | raw sections, `lidarspec.SECTION_STORE` (~700 B per section) | 23.2 | 28.0 | 22.4 |
| `profiles.arrow` | 1 m long profiles, `PROFILE_STORE` (0.8 B per station) | 0.8 | 1.0 | 0.8 |
| `steps.arrow` | every step of 1 m or more, `STEP_STORE` | 0.23 | 0.04 | 0.05 |
| `local_catchment_core.tif` | 10 m, int64 gid of the first reach, over the box cells the tile owns | 1.1 | 1.2 | 1.2 |
| `area_code.tif` | 10 m, maximum of the in-tile area code, core | 8.1 | 8.1 | 7.3 |
| `hand_10m.tif` | 10 m, HAND in 0.25 m steps, core | 2.2 | 6.2 | 5.7 |
| `hillshade_24m.tif` | 24 m, core | 1.7 | 1.4 | 1.3 |
| `tile.json` | done flag, build id, counts, costs per step, inflows, lakes, holes, edges | 0.01 | 0.01 | 0.03 |
| **total** | | **43.1** | **53.6** | **45.1** |

Nothing else reaches the disk: the 8 m DEM is read into memory, the 1 m and 2 m data never leave it. The area code is
larger than the spec's 5.5 MB (deflate is already the best codec for it: every 10 m cell has its own area).

`tile.json` also holds what the merge needs: `inflows` (REC2 inflows used: position, area, REC2 id, snapped cell),
`lakes` (id, kind `hole` or `flat`, LiDAR level, km2, bbox, outlet point, outlet reach gid, `drains` = out / sink /
edge, the core reaches mostly on the lake), `holes_list` (every hole examined, filled or not, with its rim shares),
`edges` (core reaches that leave the core, reaches with an inflow), `lost_km2` (cells that reach no reach) and
`halo_owned_km2` (cells that drain to halo reaches: counted by the neighbour tile).

## The test tiles

- **`q11y15`** (a quarter of `x05y07`, West Coast: Hokitika gorge, Toaroha, Kokatahi, Whitcombe): the rf-lxa.2
  benchmark area. All land, all LiDAR, no hole. 4,018 core reaches (983 km).
- **`q24y33`** (a quarter of `x12y16`, Bay of Plenty: Kaimai Range, the Wairoa below McLaren Falls, Wairere Falls, the
  inner Tauranga Harbour). 5,664 core reaches (1,400 km).
- **`q25y32`** (the third tile allowed, `x12y16` again): Tutea Falls on the Kaituna is not in `q24y33`, so this quarter
  was run for it. It also holds Lake Rotorua, a 62 km2 no-data hole, the worst case for the lake fill and the flats.
  5,380 core reaches (1,330 km).

`q11y15` ran before two fixes (one reader pool per tile, memory freed early). Its outputs are the same, but its times
and memory are the older ones. `q24y33` was run again after the sea fix (below); its numbers are from that run.

## Measured cost per tile

| step | q11y15 s | GB | q24y33 s | GB | q25y32 s | GB |
|---|---:|---:|---:|---:|---:|---:|
| stream 1 m -> 2 m (1.44 G cells, 69-82 M cells/s) | 18.7 | 4.0 | 22.3 | 4.1 | 20.1 | 4.2 |
| 8 m DEM (fetched during the stream; the wait) | 3.6 | 3.7 | 8.2 | 3.2 | 6.5 | 3.2 |
| sea and lakes | 2.4 | 5.4 | 6.9 | 7.5 | 6.9 | 7.5 |
| fallback | 1.9 | 4.0 | 1.7 | 3.5 | 1.5 | 3.5 |
| raw copy, 10 m grids, hillshade | 6.6 | 6.3 | 9.6 | 5.9 | 7.5 | 5.8 |
| route (flood 157-170 s) | 181.6 | 5.8 | 177.7 | 5.3 | 194.9 | 5.4 |
| accumulate, inflows, area code, lake outlets | 11.4 | 7.6 | 11.9 | 7.1 | 11.3 | 7.1 |
| reaches | 2.8 | 7.5 | 2.9 | 7.0 | 2.2 | 7.1 |
| 2 m pass, rasters | 34.8 | 7.5 | 34.6 | 7.0 | 31.8 | 6.8 |
| 1 m pass (1.0-1.2 G cells re-read, 71-82 % of the box) | 87.4 | 8.0 | 65.0 | 4.9 | 60.3 | 4.9 |
| profiles, steps, x1 tables | 3.3 | 6.1 | 3.8 | 2.2 | 3.5 | 2.1 |
| write | 2.9 | 6.1 | 3.7 | 2.3 | 2.9 | 2.2 |
| **total** | **357** | **8.0** | **349** | **7.5** | **349** | **7.5** |

- **About 6 minutes and 7.5 GB per full tile.** The peak is below the ~10 GB limit, so the 32 km tile fits; no smaller
  tile is needed. The peak comes when the filled and raw 2 m DEMs, the codes, the directions and the uint32 counts
  are in memory together (accumulate), or when the hole labels and the sea or Lake Rotorua masks are (lakes).
  Disk written: only the outputs (no transient files).
- **Routing is half the time** (157-170 s of priority flood, single-threaded, as rf-lxa.2 measured: ~0.47 us per
  cell). Lake Rotorua's 60 M flat cells add only ~8 s (flats 19 s).
- **The stream** (rf-lxa.7): with the 2 x 2 block mean computed inside the 32 reader threads, the whole 38 km box
  reads in 18-22 s, against the benchmark's 81 s for 4.9 G cells. The target of < 60 s per full tile is met.
- **The 1 m re-read** touches 71-82 % of the box's cells (the corridor blocks are 512 m), about 1.1 G cells in 50-55 s.
  The first tile was slower (73 s): each 8 km chunk started a new thread pool and reopened every sheet. One pool now
  serves the whole tile. One read on `q24y33`'s first run hung for 115 s; reads now time out after 60 s and retry.
- Two memory fixes after the first tile: the cyclic garbage collector freed 3 GB of arrays (filled DEM, directions,
  reach raster) only at the end, and the reader threads' heaps kept ~2 GB. `nz1m.trim` (gc + `malloc_trim`) after
  the big frees brought the peak from 8.0 to 7.5 GB and the 1 m pass from 8.0 to 4.9 GB.

### National estimate

`nz1m plan` finds **378 land tiles** of 32 km (a REC2 reach ends in the core), holding 258,000 km2 of land: **252
full-tile equivalents**. The design notes counted ~200 tiles; coastal tiles with little land are cheap, because
routing and reading scale with the valid cells. From the three tiles (`data/nz1m/analysis/estimate.json`: time and
disk per km2 of land, plus 20 s per tile that does not scale):

| | country |
|---|---:|
| time on this machine, one tile at a time | **~26 h** (routing ~13 h, 1 m pass ~5 h, 2 m pass ~2.4 h, stream ~1.5 h) |
| peak memory | **~7.5 GB** (box sized: the same on every tile; Rotorua-sized lakes add little) |
| kept outputs | **~12 GB** (sections ~6.2 GB, area code ~2 GB, HAND ~1.2 GB, reach lines ~1.3 GB) |
| transient disk | none |

That is about 1.7x the earlier ~15 h, mostly the single-threaded flood on more tiles. Two tiles at a time would need
~15 GB, which this 15 GB machine does not have; a 32 GB machine could run three or four at once (~7-9 h).

## Results

### Against the rf-lxa.2 benchmark (`q11y15`)

Distance from the reach lines (reaches of 2 km2 or more, points every 10 m in the overlap with the benchmark's 1 m
sub-area) to the 1 m channel (the benchmark's 1 m routing), and the line's height above that channel (1 m mosaic
re-read at both points):

| lines | p50 / p90 m | >= 100 km2 | height above p50 / p90 m | share > 1 m above |
|---|---:|---:|---:|---:|
| **this tile job (2 m, simplified at 1 m)** | **0.77 / 4.95** | 2.1 / 15.5 | **0.00 / 0.14** | **1.7 %** |
| benchmark 2 m mean, sub-area box | 0.74 / 4.33 | 1.6 / 10.9 | 0.00 / 0.13 | 0.9 % |
| benchmark 5 m mean | 1.48 / 6.24 | 3.7 / 19.8 | 0.01 / 0.31 | 2.9 % |
| today's national 8 m reaches | 10.1 / 48.4 | 20.0 / 111 | 1.46 / 9.28 | 56.8 % |

The tile job reproduces the benchmark's 2 m lines. The 1 m simplification is not the cause of the small difference
(simplifying the benchmark lines the same way moves them 0.05 m). It is on the wide gravel beds, half on interpolated
water: there the D8 path in the flats depends on the box's outlets, and the reference 1 m channel was routed on the
same sub-area box as the benchmark's 2 m lines.

- **Zero-gradient reaches** (2 km2 or more, drop < 1 cm) in the `q11y15` core: 0.8 % of the length, against 0.6 % (5 m
  benchmark), 0.7 % (8 m LiDAR) and 4.8 % (today's 8 m DEM). In `q24y33` 4.0 % (lowland drains, tidal reaches) and in
  `q25y32` 29 % (the lakes: 1,219 core reaches lie on a lake).
- **Steps** against the rf-lxa.3 dry run (same lines, same rule) in the overlap: bed steps per km 18.5 / 14.8 / 6.6 /
  0.14 for 0.5-2 / 2-10 / 10-100 / >= 100 km2, against 18.5 / 14.5 / 6.1 / 0.19.

### Steps and OSM waterfalls

| tile | OSM falls in the core | within 30 m of a reach | with a step within 20 m | bed steps per km (0.5-2 / 2-10 / 10-100 km2) |
|---|---:|---:|---:|---|
| `q11y15` | 15 | 11 | 9 | 18.7 / 12.8 / 5.1 |
| `q24y33` | 15 | 15 | 10 | 2.2 / 1.6 / 0.6 |
| `q25y32` | 10 | 9 | 4 | 2.7 / 1.6 / 0.3 |

Big falls come out as tall steps: Wairere Falls (153 m) as a 73 m step 8 m away (the profile splits a fall at its
ledges), two West Coast falls of 81 m and 201 m as 82 m and 179 m steps, Price Falls (75 m) as 39 m, McLaren
Falls as a 9.8 m step. Falls the 1 m DEM smooths are missed: **Tutea Falls (7 m) is a 5.2 m drop spread over ~35 m**
(at most 0.8 m in any 4 m), so it is not a step; the reach's `steepest_50m_m_per_km` (115 m/km) shows it. The falls
far from any reach (Michele Falls 0.9 km; three others 49-329 m) are probably on side streams below 0.5 km2.

### The step riser rule on hydro-flattened water

The Bay of Plenty surveys flatten their rivers: on the **Wairoa below McLaren Falls** (grade 5 section, 2.4 km) 41 % of
the 1 m stations are hydro-flattened and 19 % planar; down to the core edge (10.5 km, the tidal reach) 62 % flattened.
The Kaituna from Okere to Trout Pool: 24 % flattened, 14 % planar.

- The flattened water comes as **treads separated by unflattened stretches**, not as treads joined by vertical risers.
  On the Wairoa there are 45 treads of 10 m or more; the median gap between two treads is 77 m of bed or planar
  stations, and the median level change between treads is 0.05 m. The Kaituna from Okere is a staircase of treads at
  269.02, 268.73, 267.68 and 266.99 m, each joined by a 20-40 m ramp.
- So the rule has little to do: **1 riser in `q24y33`, 0 in `q25y32` and `q11y15`** (the West Coast survey interpolates
  its water: 7-24 planar steps per tile). On the Wairoa the steps are 2 bed steps of 1.3 and 1.5 m on the section and
  4 down to the core edge; with a 0.5 m threshold the rule finds 1 riser there.
- **Near misses**: only 5 bed steps per tile have flattened water on one side (half of the 10 m above or below). One
  on the Wairoa (1.5 m: 70 % flattened below, 30 % above) is at a tread edge and may be a survey riser. If the rule is
  tuned after the pass (decision 10), "flattened on one side and the step's own stations bed" is the case to look at.
  The stored profiles allow it.

### The sea (`q24y33`)

The Bay of Plenty survey has data on Tauranga Harbour (flattened at -0.8 m) instead of leaving it empty, so on the
first run the box had no no-data cell: the harbour was no outlet and 19 core reaches ran across it. Setting surveyed
water outside the 8 m DEM's land to no data (1.8 km2 here) fixed it: the coast drains to the harbour (cells that reach
no reach 10.9 -> 17.9 km2), the network is otherwise unchanged (5,661 -> 5,664 core reaches). A tidal reach at -0.83 m
that the 8 m DEM counts as land stays as data; it is a `flat` lake record and its 22 reaches carry `lake`.

### Lakes (`q25y32`)

Lake Rotorua's 62.2 km2 hole is filled at 279.50 m (rim 99.7 % flattened, one level for 97 % of it), the same level
as the benchmark; with its flattened ring the lake record is 80.8 km2. Its outlet, (1,891,689, 5,783,831), carries
the whole catchment (504 km2 in the tile) and lies 33 m from the benchmark's Ohau Channel point. Lake Rotoiti (33.9
km2, flattened, no hole) drains at Okere into the Kaituna. 11 lakes in the tile, 1,219 core reaches on them.
Reaches on a filled hole have no 1 m station nearby, so `lidar_share` is low. Since rf-lxa.5 their `dem_source` is
`lake` (at least half of the stations on the fill; `dem_res_m` 2). The three tiles above ran before that and read
`8m` there; the merge sets `lake` for them in the national table (348 reaches in `q25y32`). Each lake record in
`tile.json` now also holds its polygon (`wkt`, the 10 m cells) and `touches_box`; flattened water below 1 m is listed
under `tidal`, not as a lake. `nz1m lakes survey` gives the polygons of tiles that ran before (rf-lxa.5).

## Deviations from the spec, and why

- **`segments_core.gpkg` does not repeat the `x1_*` fields.** The 119 section summaries are in `x1_reach.arrow`
  (one row per core reach, key `gid`), as `lidarspec.TILE_FILES` lists that file; repeating them in the GeoPackage
  would double the file for nothing. `lidarspec.tile_columns()` is the contract and the test checks it.
- **`x1_rough` and `x1_dropconc`** come from the reach's stored 1 m profile (`lidarpipe.reach_profile`, every core
  reach), as rf-lxa.4 says for the national pass; `x1_grad1m` and `x1_prof_len` from the section points
  (`xsec1m.long_profiles`), as in the study.
- **`down_gid` is not written**: it is a merge field. The merge sets it from `down_seg`, `exits_core`, `end_x`,
  `end_y` and the lake records.
- **Lakes before the national lake table**: each tile fills its holes at its own rim level and reports them. A lake
  split by tiles can then get two levels until rf-lxa.5's table exists; the job reads it when it does, and
  `nz1m lakes build` lists the tiles to re-run ([`nz1m-merge.md`](nz1m-merge.md)).
- **Hydro-flattened lakes** (no hole) are found per tile: 10 m cells with at least half of their 1 m cells
  flattened, in components of 0.1 km2 or more, or touching a filled hole. Wide flattened river treads can pass this
  test; they are `kind = flat` in the lake records, so the merge can drop them.
- **The fallback's smooth part** is worked on a 10 m grid (the edge part on the 2 m grid). Against
  `seam_correct` worked at 2 m it differs by less than 0.12 m within 150 m of the seam (test).
- **The sea mask is new** (not in the spec): surveyed water outside the 8 m DEM's land and below 10 m becomes no
  data (`nz1m._mask_sea`, code `W2_SEA`), see "The sea" above. Without it a flattened harbour is no outlet.
- **Section reaches are chosen by the in-tile area** (`area_tile_km2` >= 1 km2), the only area the tile has. Near the
  box edge, where an in-tile area can be short, a reach of just over 1 km2 can be missed; after the merge they can be
  added from a re-read if needed (rare: inflows cover the rivers entering the box).
- **`seam_dist_m`** sees only seams inside the box: a seam more than 3 km outside the core is reported as farther
  than it is (capped at 5 km anyway).

## The merge (rf-lxa.5)

The national lake table and the merge that use these outputs are in [`nz1m-merge.md`](nz1m-merge.md): reaches that
leave a core are stitched to the next tile's reach on the same 2 m cell, lakes are single nodes, and every reach's
area comes from network accumulation of `local_km2` (`area_tile_km2` is the check). Cells that drain to halo
reaches are counted by the neighbour (`halo_owned_km2`); cells that reach no reach before the sea or the box edge are
`lost_km2`.

## Reproduce

```bash
uv run pytest -q tests/test_nz1m.py
uv run river-finder nz1m plan
uv run river-finder nz1m tile q11y15 q24y33 q25y32     # ~6 min and < 8 GB each, ~50 MB each
uv run river-finder nz1m check q11y15 q24y33 q25y32    # + data/nz1m/analysis/estimate.json
```
