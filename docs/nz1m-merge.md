# The national lake table and the 1 m merge (rf-lxa.5)

The step after the tile job ([`nz1m-tile-job.md`](nz1m-tile-job.md), rf-lxa.6): one water level and one outlet per
lake, and one national river network whose reach areas come from the network, not from the tiles. Design:
[`lidar-pipeline.md`](lidar-pipeline.md) ("Tiles, halo and edges"); fields and files:
[`lidar-extraction-spec.md`](lidar-extraction-spec.md). Code: `src/river_finder/nz1mlakes.py` (lake table),
`src/river_finder/nz1mmerge.py` (merge), the lake pieces of `nz1m.py` (`fill_holes`, `label_lakes`,
`read_lake_table`). Tests: `tests/test_nz1mmerge.py`. Next: [`nz1m-finish.md`](nz1m-finish.md) (everything
downstream of the merge, `river-finder nz1m finish`, and the switch of the site to the 1 m root).

```bash
uv run river-finder nz1m lakes survey q24y33 1880000,5770000,1895000,5790000   # pre-pass: tile boxes or windows
uv run river-finder nz1m lakes build              # -> data/nz1m/lakes.gpkg, lakes.json (with the re-run list)
uv run river-finder nz1m merge [qid ...] [--out DIR]   # all done tiles by default -> network.parquet, reaches.parquet, ...
```

## The lake table

- **Records.** Every tile reports its lakes in `tile.json`: kind `hole` (a no-data interior filled at the rim's LiDAR
  level) or `flat` (water the survey flattened), level, km2, the polygon of its 10 m cells, whether it touches the
  box edge, the outlet and the core reaches on it. It also reports every hole it examined (rim level, rim cells).
  `nz1m lakes survey` makes the same records for any window without routing (about 45 s and up to 7.4 GB for a
  tile box, `q25y32` with Lake Rotorua): the pre-pass before the tiles run, and the polygons of tiles that ran before
  the tile job wrote them. Keep a window within the size of a tile box (38 x 38 km).
- **Join.** Records whose polygons meet (within 20 m) and whose levels agree within 0.10 m are one lake, whatever
  tile they come from.
- **Level.** A hole lake takes the rim level of the tile that saw the most flattened rim cells of it. A flat lake
  takes the area-weighted median of its records.
- **Tidal water.** A lake whose level is below 1 m is tidal (an estuary, a flattened tidal reach, a coastal lagoon
  such as Te Waihora at 0.5 m). It is not a lake: the tile job leaves such holes as no data (sea), the merge does
  not use it as a node, and its reaches get `lake` False and `lake_id` -1.
- **Outlet.** Records that drain `out` give outlet points; points within 100 m are one outlet; the one with the
  most in-tile area wins. A tile that sees only part of a lake routes that part to its box edge and reports `sink`.
- **Fill in the tiles.** When `lakes.gpkg` exists, the tile job gives a hole that lies mostly (half of its cells) in a
  table lake that lake's level and decision: filled at `level_m`, also when the tile's own rim test fails on its part
  of the rim, or left as sea when the table says `tidal`.
- **When a tile must be re-run** (`lakes build` lists them in `lakes.json`): one of its holes was filled at a level
  more than 2 cm (the rim tolerance) from the national level, it left a national hole lake unfilled, or it filled a
  hole that is tidal. Below 2 cm the fill is the same surface for routing. National order: survey windows around the
  big lake holes of [`lidar-coverage.md`](lidar-coverage.md) (Taupo, Te Anau, Pukaki, Manapouri, Ohau; Taupo and
  Te Anau are wider than a tile box, Te Anau needs two overlapping windows), `lakes build`, run the
  tiles, `lakes build` again, re-run the listed tiles, merge. Te Waihora (0.5 m) is tidal: sea.

## The merge

1. **Read** each tile's core reaches, one tile at a time. Only the line ends, midpoints and the lines within 1.5 km
   of the core edge stay in memory.
2. **Links** (`stitch`): 0 inside the tile; 1 at a tile edge; 2 through a lake node; 3 REC2 relink; 4 none.
   - **Edge.** A reach that leaves its core links to the reach of the next tile whose line holds its end point (the
     same 2 m cell: the same path from there on, whatever the two in-tile areas say), or else passes within 8 m
     with an in-tile area ratio of 0.7-3. A line that only ends at the point is skipped (the point starts the reach
     below it). If nothing passes the point, the two tiles cut the river's link into reaches differently; the reach
     links to the first reach of the tile it crosses into that starts within 600 m, not higher and of a similar area.
   - **Lake node.** For each lake with an outlet reach O: every lake reach whose path leaves the lake anywhere but
     at O, and every unlinked reach that ends in the lake, links to O. Lake reaches below O are left alone.
   - **REC2 relink** (the 8 m merge's `relink.relink_outlets`): only for exits into a merged tile that are still
     unlinked.
   - **The receiving tile's routing** (`stitch_catchments`, rf-lxa.10; `stitch` 1). For exits still unlinked: the
     tile they cross into names, in `local_catchment_core.tif`, the core reach each of its 10 m cells first drains
     into. From the core side nearest the end point outwards, 10 m at a time up to 250 m, along it within
     clip(30 m + 2 x the end point's distance to the side, 30, 300 m): at the first depth that names an acceptable
     reach, the link. Acceptable: a core reach of that tile whose bottom is at most 1 m above the exit's end (20 m
     when either is on the 8 m fallback: each tile fits the fallback to the LiDAR around its own box, so two tiles'
     heights there differ by metres), and no cycle. Those that do not leave their core straight back into the exit's
     tile come first, then the largest in-tile area. No area test: where the first stitches fail, the receiving
     tile's in-tile areas are what went wrong (its REC2 inflow took another path: across the plain beside a braided
     bed, or across a flat its box edge cut).
   - **REC2 path** (`stitch_rec2_path`; `stitch` 1). For exits the receiving tile's routing gave nothing for (its
     cells past the edge drain into reaches it does not own, e.g. back across the edge on a flat): the REC2 reach at
     the exit's end (the relink's match, on the larger of the in-tile and the network area so far), then points every
     20 m down REC2 for up to 10 km; at the first point with an acceptable reach of the receiving tile within 100 m,
     the link (same order). `merge.json` `fallback_stitch` has the counts and the 60 biggest links of both.
3. **Cycle guard**: a topological sort; a cycle is reported in `merge.json` and cut at its weakest link (highest
   `stitch` code, then smallest area).
   - **Zero-length reaches** (rf-lxa.12) then leave the network. The tile job gives a channel link of 3-6 path cells
     (its cells and the confluence cell below) one reach whose 20 m-smoothed length is 0: the moving average
     (+-5 cells, shrinking at the ends) puts every vertex on the path's centroid. Such a reach has one profile
     station and no profile. In practice it is a stream that reaches the 0.5 km2 threshold 2-5 cells above a
     confluence, or two confluences 2-5 cells apart. Everything that drains into it now drains into the first reach
     of non-zero length below it (`stitch`: the weakest link on the way). That reach takes its `local_km2` and
     other `local_*` sums (`local_z_mean_m` weighted by area, `local_z_max_m` the higher), so its area and all areas
     below stay the same. A reach whose `down_seg` named it takes its `down_seg` and `exits_core`. If the path ends at
     an outlet first, the reaches above become outlets of that kind and the biggest takes the sums; a lone reach
     (head and outlet) is dropped with its area (`lost_km2`). Lake outlets move to the reach that took them.
     The reaches leave `network.parquet`, `reaches.parquet`, `x1.arrow` and `steps.arrow`. `merge.json`
     `zero_length.merged` lists `[dropped gid, gid that took it]`, for readers of the tiles'
     `local_catchment_core.tif`, whose cells still name the dropped gid: they map them with
     `national.merged_gids` (rf-lxa.13, [`nz1m-finish.md`](nz1m-finish.md)).
4. **Accumulate** `local_km2` and the other `local_*` sums down the network: `area_km2`, `area_mid_km2`,
   `area_check`, `head_m`, `relief_m`, `melton`, `ksn`, `knick_ratio`, `upstream_*_frac`, `catch_lidar_share`,
   `karst_up_frac`, `sinuosity_1km`.
5. **Outlets and truncation.** `outlet_kind`: 1 sea (the reach ends at no data in its tile), 2 lake without an
   outlet, 3 box edge (no link into the next tile, or that tile is not merged). A reach is an open entry when area
   enters its tile's core there (from the halo or a REC2 inflow, 0.25 km2 or more) and no reach of another tile
   links in, unless (rf-lxa.10) it is a reach of a lake with an outlet (its in-tile area is the lake's catchment
   routed across the flat surface; the lake node brings that to the outlet from every member), or the tile across its
   core's nearest side is merged and no unresolved exit ends within 3 km (every cell of a merged core is counted once,
   so area from there can only go missing at an unresolved exit; otherwise the network brought the river in through
   another reach and the in-tile area would count it twice). `missing_up_km2` sums those areas down the network;
   `truncated` is set where the missing area is more than 10 % of the true area and REC2 does not say the area is
   there (`area_mid_km2` at least 90 % of `rec2_area_at_km2`).
6. **Joins**: REC2 matched along the network (`rec2_nzsegment`, `rec2_area_km2`, `rec2_area_at_km2`, `rec2_dist_m`,
   `river_rec2`, `river`; below), REC1 MAF (`specific_maf`, `maf_m3s`, `specific_maf_matched`, `rec1_nzreach`), the
   river-guide `region`.
7. **Write** (to `data/nz1m/` or `--out`): `network.parquet` (every merge field per gid), `reaches.parquet` (every
   tile field, the merge fields and the line; GeoParquet written tile by tile), `x1.arrow`, `steps.arrow`,
   `lakes.gpkg` (with the outlet reaches) and `merge.json` (links and outlets by kind, unresolved exits, cycles,
   lake nodes, timings). Gids never change.

**REC2 matching** (rf-33a.4): per reach, the REC2 lines within clip(20 sqrt(area), 100, 400) m of its midpoint,
with REC2's area at the projection of the midpoint (the REC2 reach's upstream end plus the share of its own
catchment) within 1/3-3x of `area_mid_km2`, scored by distance and area ratio. A reach whose match is not on the
REC2 path of the reach below's match then takes its best candidate that is. This replaces the outlet point snap that
picked the bigger river at confluences.

## Results on x12y16 (Bay of Plenty, four 32 km tiles)

`q24y32` and `q25y33` were run for this (364 s and 7.0 GB, 319 s and 7.4 GB; 56 and 49 MB). The three tiles that
ran before the tile job wrote lake polygons got them from `lakes survey` (31-50 s each). The single-box reference is
the tile job on a 32 km core centred on the corner where the four tiles meet, so every internal edge crosses its
core (337 s, 7.3 GB). Checks: `data/nz1m/analysis/merge_x12y16.json` (scripts in `data/nz1m/analysis/scripts/`).

### Links across the edges

| | x12y16 |
|---|---|
| core reaches | 21,161 (5,233 km) |
| reaches that leave their core | 169: 78 into another of the four tiles, 91 out of x12y16 |
| links across the internal edges | **78 of 78**: 75 on the same 2 m cell, 3 to the next tile's first reach (10-26 m on), no REC2 relink, none left |
| lake links | 6, in three lakes with no surface outlet (by level and size probably Okataina, Tikitapu and Rotomahana) |
| cycles | 0 |
| zero-length reaches (rf-lxa.12) | 12 dropped, all into the reach below: 9 heads of 0.50-0.80 km2, 3 links between two confluences; 3 on flattened lakes. 21,149 reaches are left |
| outlets | 23 sea, 90 box edge (all where a river leaves x12y16), 0 at an internal edge |
| truncated | 945 reaches (392 of 10 km2 or more) below 61 open entries; 57 entries lie within 3.5 km of x12y16's outer edge (rivers from tiles not merged); 4 are interior streams of 0.6-1.4 km2 with 0.25-0.34 km2 of in-tile area that their local sums do not hold |
| merge time, memory | 3.3 s, 0.5 GB |

- **Zero-length reaches** (re-merge with rf-lxa.12 into `merge_x12y16_lxa12/` of the sample, against the merge
  before). Every other reach keeps its gid, line and network area (bit for bit). The local sums, the km and every
  count in `merge.json` stay the same, except `links.in_tile` (12 fewer). What changes: `down_gid` of the 6 reaches
  that drained into a dropped one, and their `knick_ratio` (NaN before). `sinuosity_1km` changes on 3 reaches whose
  biggest upstream branch was a zero-length head. The median MAF that fills unmatched reaches changes too (0.4722
  -> 0.4727).
- **No gaps, no duplicates.** Points every 10 m on the reference's lines within 1 km of an internal edge: 99.95 % lie
  on exactly one merged line, none on none, 0.05 % on two (at confluences). Farther from the edges: 99.86 %, 0.11 %,
  0.02 %.
- **Same-cell stitches**: the exit's end is the next reach's first vertex in 97 %; otherwise up to 37 m of the next
  reach lies above the exit's end (the tiles cut the link differently). The 3 entry stitches leave 10-26 m of channel
  that neither tile owns.
- **Area continuity**: at every one of the 78 links the reach below has at least the area of the reach above (median
  ratio 1.05). The receiving tile's own in-tile area was more than 5 % off at 17 % of them (its REC2 inflow); the
  network area is not. The Waiomou's crossing on the Mamaku plateau is one: the receiving tile had 30 km2 from REC2
  where 43 km2 come in, so the spec's area ratio (0.7-3) refused the stitch; the same-cell rule takes it.

### Areas against the single box

Reference reaches whose whole in-tile catchment its own core owns (no REC2 inflow in their area):

| reaches | n | network within 1 % | network max error | in-tile within 1 % | in-tile max error |
|---|---:|---:|---:|---:|---:|
| 2-10 km2 | 1,715 | 98.8 % | 73 % (one stream) | 95.1 % | 73 % |
| 10-100 km2 | 892 | **100 %** | 0.33 % | 78.3 % | 57 % |
| 100 km2 or more | 13 | **100 %** | 0.00 % | 15.4 % | 6.7 % |

Within 2 km of an internal edge: all 566 such reaches of 2-100 km2 within 1 %. Over all 41 reference reaches of
100 km2 or more (also those whose reference area rests on the reference's own REC2 inflows): 71 % within 1 %, at most
3.0 % off; the worst (181 km2 in the reference, 187 km2 in the merge) is the reference's inflow, not the merge (the
four tiles' in-tile area there is 187 km2 too).

### REC2 agreement along the network (reaches of 5 km2 or more)

| areas | matched | within 10 % | tolerant |
|---|---:|---:|---:|
| 1 m network, not truncated | 6,449 of 7,134 | **83.1 %** | **86.6 %** |
| 1 m network, all (incl. rivers from tiles not merged) | 6,794 of 7,685 | 79.1 % | 82.6 % |
| 1 m in-tile (the tile job, with REC2 inflows) | 7,017 of 7,863 | 84.5 % | 88.1 % |
| 8 m national today | 5,846 of 7,193 | 75.7 % | 79.9 % |

"Tolerant": anywhere along the matched REC2 reach, +-10 %. The median distance from a reach's midpoint to its REC2
line is 13 m.

Named rivers at their lowest REC2 reach in x12y16 (our area / REC2's):

| river | REC2 km2 | 1 m network | 8 m today |
|---|---:|---:|---:|
| Waiomou Stream (Mamaku plateau) | 218 | **1.05** | 0.66 |
| Mangapapa River (Mamaku plateau) | 39 | 0.89 | 0.96 |
| Mangorewa River | 205 | 0.98 | 1.00 |
| Oraka Stream | 260 | 0.98 | 1.03 |
| Raparapahoe Stream | 79 | 0.94 | 1.42 |
| Waimapu Stream | 107 | 0.99 | - |
| Kopurererua Stream | 74 | 0.99 | 0.99 |
| Ohau Channel | 503 | 1.00 | 1.01 |
| Waihou River (leaves x12y16) | 891 | 0.98 | 1.01 |
| Kaituna River (at the sea) | 1,205 | 0.96 | - |

**The plateau divides** (rf-33a.4 b). The 8 m network loses a ~50 km2 tributary of the Waiomou across the plateau
(0.46-0.66 of REC2 below the 134 km2 junction); the 1 m keeps it and ends within 5 % of REC2. Higher up, the 1 m gives
the Waiomou 30-50 % more than REC2 at 25-45 km2: it takes ~10 km2 of plateau that REC2 sends elsewhere. On the
Mangapapa the 1 m and the 8 m agree at its head (0.45 and 0.46 of REC2's 4.8 km2) and the 1 m stays 3-4 % under the
8 m to its mouth (0.89). On the flat ignimbrite plateau REC2 (30 m DEM) is no reference: these divides need a check
against drains and aerial photos, not against REC2. The Kaituna lacks the part of Lake Rotoiti's catchment that lies
in `q26y32` (not merged).

### Lakes and rivers to the sea

| lake | kind | level m | km2 | lake reaches | outlet |
|---|---|---:|---:|---|---|
| Rotorua | hole | 279.50 | 80.8 | 515, all drain to the outlet | **Ohau Channel**: 32 m from the top of REC2's Ohau Channel, 503.3 km2 (REC2 501.7-503.3) |
| Rotoiti | flat | 278.655 | 33.9 | 234, all | Okere, 590 km2, into the Kaituna |
| Tarawera | flat | 296.50 | 41.3 | 236 | in `q26y32` (not merged): its reaches end at the box edge |

- The table from the five tiles holds 20 lakes: 1 hole lake, 16 flat, 3 tidal (Tauranga Harbour's flattened tidal
  reaches at -0.8 to 0.2 m). Four lakes were seen by two tiles and joined. **Re-run: none**: no hole lake crosses a
  tile box here (Rotorua's hole lies inside `q25y32`'s box).
- **To the sea**: the Ohau Channel, Lake Rotoiti and the Kaituna reach the sea at Maketu (228 reaches, 57 km, one tile
  edge, 1,192 km2); the Mangorewa crosses three tiles (4 edge links) into the Kaituna; the Raparapahoe, Waimapu and
  Kopurererua reach the sea. The Wairoa (across `q24y33` -> `q25y33`), the Mangapapa and the Waiomou / Waihou leave
  x12y16 to the north before the harbour; the Waiari leaves to the east (`q26y33`).

### One tile alone (`q11y15`)

4,018 reaches, all 51 exits end at the box edge (none of the four tiles around is merged), 16 open entries (4 of 10
km2 or more) make 218 reaches truncated (106 of 10 km2 or more): the merge has nothing to stitch and nothing fails. The
Hokitika leaves to the north with 429 km2. 1.4 s.

## National estimate

The merge itself is light. On x12y16 (21,161 reaches) it took 3.3 s and 0.5 GB; `q11y15` alone 1.4 s. Reading the
national REC2 takes 2.5 s (0.6 GB) and REC1 7.5 s (0.55 GB); the REC2 and REC1 matches take 0.3-0.4 s per 21,000
reaches against the national layers (`data/nz1m/analysis/national_cost.json`). For ~1.4 M national reaches (252
full-tile equivalents of ~5,300 reaches): **about 4-6 minutes and 3-4 GB** (the edge lines, the relink's point
index and the REC layers; `x1.arrow` is concatenated in memory, ~1 GB). Outputs: `reaches.parquet` ~0.5 GB,
`x1.arrow` ~0.4 GB, `network.parquet` ~0.15 GB. The lake surveys before the run add ~10 windows of 1-3 minutes.

## The national run (rf-lxa.10, 2026-11-28)

378 tiles (`data/nz1m/tiles/`, 11 GB), the lake table (838 lakes: 633 flat, 193 tidal, 12 holes; 186 nodes), one
tile re-run for the lakes (`q08y11`), then the merge: **1,238,220 reaches, 304,473 km** (1,456 zero-length reaches
dropped, 19 of them with their 10.9 km2), 135 s, 4.1 GB. Run log and script: `run.sh` / `run.log` of the run directory
(a frozen worktree at 5a06945, which has the empty-tile fixes); the merge was re-run on `main` with the rules below.

| | first merge | with the rf-lxa.10 rules |
|---|---:|---:|
| exits | 10,752 | 10,752 |
| linked at the edge (same cell / 8 m / entry) | 10,380 (10,305 / 75 / 245 entry) | the same |
| REC2 relink | 48 | 48 |
| receiving tile's routing / REC2 path | - | 40 / 5 |
| unresolved inside the country (over 1 km2) | 55 (36) | **10 (6)** |
| truncated (10 km2 or more) | 8,452 (4,756) | **62 (0)** |
| open entries (10 km2 or more) | 998 (281) | 6 (0) |
| cycles | 0 | 0 |

- **What the first merge left.** 36 exits of 1 km2 or more ended at an edge, among them the Rakaia (2,778 km2, at
  SH1: the tile below took its REC2 inflow beside the braided bed and sent it across the plain into the Selwyn and
  Te Waihora, so no reach of that tile carried the river where the tile above left it), the Pomahaka (1,117), the
  upper Taieri on the Maniototo scroll plain (160 + 159 + 52 across one edge, each tile sending it into the other),
  the Mangapu (189) and Stewart Island's Freshwater (95). All but the Rakaia and the Mangapu are on the 8 m fallback.
- **Truncation was mostly double counting.** Of the 8,452 truncated reaches, the 6,193 with a REC2 match had at least
  90 % of REC2's area in 94 %: Waikato (missing 1,844 km2 from Taupo's lake reaches), Waitaki (10,416), Waiau, Clutha
  (Hawea: the tile below spills the lake 2 km east of where the lake's tile does), lower Taieri. The rules above
  leave 62 truncated reaches, all under 10 km2 (55 of them on the Te Waihora shore, `q15y12`).
- **Left unresolved (6 of 1 km2 or more).** The Pomahaka at the `q07y05` / `q07y04` edge (1,270 km2): on the 8 m
  fallback `q07y04` puts the valley 5-10 m higher than `q07y05` and drains it back north for more than 10 km, so every
  reach of `q07y04` near the river or REC2 leads back into the exit (a cycle). The river below the edge carries only
  its own area and is not flagged (`q07y04` has no open entry for it). Two flows on the Te Waihora shore at 0.5 m (147
  and 59 km2, into the tidal lake), two coastal streams at `q08y04` (13 and 2 km2), and four of under 1 km2. Fixing
  the Pomahaka needs the fallback fitted the same way in both tiles (rf-lxa.14).
- **Spot checks** against REC2 (area at the reach): Waikato at the Taupo Control Gates 3,461 km2 (3,439), at the sea
  14,524 (14,473); Taramakau at Inchbonnie 467 (469); Gowan 413 (414); Monowai at the weir 249 (278), down the Monowai
  to the Waiau (the 8 m network sent it into the Borland Burn, rf-73f); Rakaia at the sea 2,989 (2,831); Taieri 5,706
  (5,705); Whanganui 7,133 (7,131); Waiau 8,205 (8,212); Kawarau 5,756 (5,738); Hawea 1,467 (1,476); Ruamahanga 3,354
  (3,313); Clutha (Matau branch) 19,292 (20,571: the Koau branch takes the rest). Along every one of these the
  network's median area is 1.00 x REC2's; the Selwyn has 1,171 km2 against 983 (the tile across the Rakaia sends some
  of the plain north of the Rakaia into it).

## The issues this closes

- **rf-t26.11** (big rivers ending at tile edges): built. One level and one outlet per lake, lakes as single nodes,
  areas from the network, same-cell stitching. On x12y16 every crossing of an internal edge links (78 of 78, none by
  REC2) and every area of 10 km2 or more matches the single box within 0.33 %. It closes when the national run lands:
  then check the 26 outlets of `unresolved_gt200.csv` (Whanganui, Taupo, Ruamahanga, Hawea, Wakatipu, Te Anau) in
  `merge.json` (`unresolved_edge.inside` must be 0, `lakes.nodes`). Before the run: survey windows around Taupo and
  Te Anau, whose holes are wider than a tile box.
- **rf-33a.4** (REC2 agreement): (a) done: REC2 is matched along the network (`rec2_nzsegment`, `rec2_area_at_km2`),
  not snapped at the outlet: 83 % within 10 % (87 % tolerant) on x12y16, against 76 % (80 %) for today's 8 m with
  the same matcher. (b) Waiomou fixed (-34 % -> +5 %); Mangapapa -11 % (8 m -4 %), where REC2 disagrees with both DEMs
  at its head. (c) gone with LiDAR (rf-lxa.2). The 90 % target looks out of reach against REC2 itself. Left: a user
  decision on the target, and validation reading `rec2_nzsegment` after the national run (the reference-area
  `validate.py` retires).
- **rf-3ri.6** (truncation flag): `truncated` now comes from the network: set only where area enters a tile and no
  reach of the next tile links in (`missing_up_km2`). With every tile merged it marks missing tiles and failed links
  only (none inside x12y16). It closes with the national run (the reference areas are folded into it).

## Deviations from the spec

- **`vi`** is not written by the merge: it is the viewer index, given by `nz score` per region, as today.
- **`river`** is the REC2 name for now; `river_linz` is joined later (stage `ext`), then `river` falls back to it.
- **`rec2_area_km2`, `rec2_dist_m`** (stage `ext`) come with the REC2 match here, and **`rec2_area_at_km2`** is new:
  REC2's area at the midpoint's projection, the value the agreement compares (added to `lidarspec`).
- **`missing_up_km2`** is new (added to `lidarspec`): how much area is missing upstream; `truncated` is defined on it
  (the spec's "ends at a box edge with no stitch or inflow" made exact).
- **`dem_source = lake`** is new (tile job and spec): a reach with at least half of its stations on a filled lake hole
  (`dem_res_m` 2). Tiles that ran before read `8m` or `mixed` there; the merge sets `lake` in the national table for
  hole-lake reaches with at most half of their stations on 1 m data (380 reaches in `q25y32`).
- **`lake`** in the national table is False on tidal water (the older tiles counted flattened water below 1 m as a
  lake).
- **Zero-length reaches are not in the national files** (rf-lxa.12; above). The tiles still hold them.
  `merge.json` `zero_length` gives their count, the area moved and `merged` (dropped gid -> gid); the readers of
  `local_catchment_core.tif` map their cells to the receiving gid (rf-lxa.13). Tiles run with 4cc040a or later have
  none (a 3-6 cell link takes its cell length).
- **The stitch accepts an exact cell match whatever the areas.** The spec's area ratio (0.7-3) refused the Waiomou's
  crossing on the Mamaku plateau, where the receiving tile's REC2 inflow (30 km2) was far below the real 43 km2.
- **Files**: `reaches.parquet` (the national reach table) and `merge.json` are added to `lidarspec.NATIONAL_FILES`;
  the tile's `tile.json` lists lake polygons and tidal water.

## Reproduce

```bash
uv run pytest -q tests/test_nz1mmerge.py
uv run river-finder nz1m tile q24y32 q25y33                  # the two missing quarters of x12y16 (~6 min each)
uv run river-finder nz1m lakes survey q24y33 q25y32 q11y15   # polygons for the tiles run before rf-lxa.5
uv run river-finder nz1m lakes build
uv run river-finder nz1m merge q24y32 q24y33 q25y32 q25y33 --out data/nz1m/merge_x12y16
uv run river-finder nz1m merge q11y15 --out data/nz1m/merge_q11y15
uv run python data/nz1m/analysis/ref_corner/ref_tile.py data/nz1m/analysis/ref_corner/qref     # reference (~6 min)
uv run python data/nz1m/analysis/scripts/check_x12y16.py data/nz1m/merge_x12y16 \
    data/nz1m/analysis/ref_corner/qref data/nz1m/analysis/merge_x12y16.json
```
