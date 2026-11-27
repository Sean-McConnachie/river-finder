# After the national 1 m merge: rebuild everything downstream, then switch (rf-lxa.8)

The tile job ([`nz1m-tile-job.md`](nz1m-tile-job.md)) and the merge ([`nz1m-merge.md`](nz1m-merge.md)) give
`network.parquet`, `reaches.parquet`, `x1.arrow`, `steps.arrow`, `lakes.gpkg` and `merge.json` in `data/nz1m/`.
Everything the site reads is still built from the 8 m scan in `data/nz/`. This page is the step from one to the
other: one resumable command that rebuilds every consumer into `data/nz1m/`, and one command that points the code
at it. Code: `src/river_finder/nz1mfinish.py` (steps, state, switch), `nz1mjoin.py` (joins), `nz1mcompare.py`
(reference comparison, section audit). Tests: `tests/test_nz1mfinish.py`.

```bash
uv run river-finder nz1m finish                     # every step not done yet, into data/nz1m (~3-4 h, < 8 GB)
uv run river-finder nz1m status                     # what is done, how long it took, peak memory
uv run river-finder nz1m finish --steps cv v5       # run these steps again (later steps that need them are reset)
uv run river-finder nz1m switch                     # config/national.toml -> data/nz1m (refused until the site
                                                    # steps are done); --back returns to data/nz
```

Options: `--root` (default `data/nz1m`), `--merge DIR` and `--tiles-dir DIR` (link a merge and its tiles from
elsewhere into the root), `--inputs` (national layers, default `data/nz`), `--models-dir` (model registry, default
`models/`), `--ref8m` (the 8 m scan for the section audit, default `data/nz`), `--repeats` and `--cv-models` (CV),
`--v5-id`, `--v5-recipe` (default `gbm_xsec`), `--refs` (keep the 5 m reference areas in the 1 m viewer),
`--web-only` (some web data steps), `--force` (re-run done steps).

## How the code chooses the data

- `national.nz_dir()` is the national root. It comes from `$RIVER_FINDER_NZ`, else `config/national.toml`
  (`[data] root`, `data/nz` today), else `data/nz`. Every national command reads and writes there: nz score,
  models, ml, external, hydro, lidar coverage, rain (`config/rainfall.toml` no longer sets `nz_data`), nz viewer,
  web data.
- `national.is_1m()` is true when the root holds `network.parquet`. The consumers branch on it (below).
- Viewer data: `data/viewer_nz` for `data/nz`, `<root>/viewer_nz` for any other root (`national.viewer_dir`), so a
  1 m build never writes over the live site's files.
- The finish runs every step with `nz_dir()` set to the root (`national.using_root`). Nothing it does touches
  `data/nz/`, `data/viewer_nz/`, `data/web/` or `data/analysis/lidar/`. It reads `data/nz/` (input layers, hard
  linked into the root by the `inputs` step) and `data/analysis/lidar/lidar_coverage.gpkg` (survey footprints).
- Model cards have a build (`data`: `"8m"` for v1-v4 and every older card, `"1m"`). Only the current build's cards
  are scored, evaluated and offered on the map; the others are listed as `archived` in the registry with their own
  results (`models/evaluations.json` for 8 m, `models/evaluations-1m.json` for 1 m; CV in `models/cv/` and
  `models/cv-1m/`).

## Steps

In the order of [`lidar-extraction-spec.md`](lidar-extraction-spec.md) ("Rebuild order after the pass"), except that
the viewer comes before `external attach` (which writes its viewer files) and the ML steps. A step that is done is
skipped; a step that runs again resets the steps that need it.

| step | needs | what it writes (in the root) | sample s / peak GB | country (estimate) |
|---|---|---|---:|---|
| `inputs` | | hard links to `rec2`, `flood`, `geology`, `access` (copies of the small layers and caches: `councils`, `large_catchments`, `waterfalls`, `gauges.json`, `linz/`, `aerial/`, `s2tiles/`, `rain/gauges`, `rain/scaling`, `rain/cache`); with `--merge` / `--tiles-dir` the merge files and tile links; checks every tile of `merge.json` is done | 1.2 / 0.2 | < 1 min |
| `joins` | inputs | `joins.parquet`: `bed_hard`, `bed_rock`, `river_linz`, `linz_dist_m`, `linz_section_id`, `river` (REC2, else LINZ), `lidar_survey`, `lidar_year(_start)`, `sinuosity` | 5.7 / 0.4 | 6-15 min, < 1 GB (one tile at a time) |
| `score` | joins | `regions/<slug>/segments_scored.gpkg` (every tile, merge and join field), `runs.geojson`, `candidates.csv`, `model.json` | 2.1 / 0.5 | 10-20 min, 5-7 GB |
| `table` | score | `segments_table.pkl` with `models.FIELDS_1M` (58 more columns) | 0.5 / 0.5 | 2-4 min, 2-3 GB |
| `viewer` | table | `viewer_nz/`: region files, `regions_index.json` (no 5 m reference areas unless `--refs`) | 40 / 1.5 | 35-50 min, 3 GB |
| `external` | viewer | `external.pkl`, `waterfall_check.json`, `<region>_x.txt`, `_falls.json` | 18 / 1.3 | 10-15 min, 3 GB |
| `hydro` | table | `hydro.pkl`, `hydro_summary.json` | 0.1 / 1.1 | 1 min, 3 GB |
| `lidar` | table | `analysis/lidar/`: coverage per reach and catchment, gap grid, summary | 5.3 / 1.5 | 8-12 min, 3 GB |
| `ml` | external | `ml_table.pkl` with every `x1_*` of `x1.arrow` and the 1 m hydraulics | 0.9 / 1.1 | 5-10 min, 5-7 GB |
| `cv` | ml | `models/cv-1m/results.json`, `explore.json`, `ml_oof.npz`, `viewer_nz/ml.html` | 29 / 1.1 | 20-40 min, 6-8 GB |
| `v5` | ml | `models/v5.json` + `v5.lgb.txt` (`gbm_xsec` on `guides-v1`), `models/baseline-flow-1m.json`, `models/audits/v5_top_runs.csv` | 2.8 / 1.0 | 2-4 min, 6 GB |
| `evaluate` | v5 | `models/evaluations-1m.json` (1 m cards only) | 2.5 / 1.0 | 3-6 min, 6 GB |
| `scores` | v5, viewer | `viewer_nz/<region>_m_<card>.txt` for the 1 m cards | 1.6 / 1.0 | 3-6 min, 6 GB |
| `web` | scores, external, hydro | web data (sections, access, gauges, LINZ, hydro, aerial, satellite) into `viewer_nz/`, then a preview site in `web/` (no Basemaps key, no live rain) | 52 / 1.2 | 20-40 min, 4 GB |
| `rain` | viewer | `rain/weights_gsmap/ecmwf.npz`, `scaling_zones.npz`, `network.npz`; `rain-inputs/` (what the `rain-inputs` branch holds) | 2.8 / 1.0 | 5-8 min, 2-3 GB |
| `export` | score | `gis/`: GeoPackage, VRTs, the truncation mask per tile, the local catchments of the tiles that hold a dropped gid (below) | 6.4 / 1.4 | 15-25 min, 4 GB, ~2 GB disk (+1.2 MB per tile with a dropped gid) |
| `compare` | table | `analysis/reference_compare.json` | 0.4 / 1.1 | 1-2 min, 2 GB |
| `sections` | table | `analysis/sections_audit.json`, `.csv` | 1.5 / 1.1 | 3-6 min, 4 GB |

Sample: the four tiles of x12y16 (21,161 reaches, the merge before the zero-length drop; 21,149 after it),
`data/nz1m_sample/finish_full/`, all 18 steps in one run in
2 min 54 s, at most 1.5 GB (process memory, so later steps also hold what earlier ones left). Country: ~1.2-1.4 M
reaches (57-66 times the sample), scaled from the sample and from today's 8 m runs of the same commands; **about
3-4 hours, peak 7-8 GB** (score, ml, cv). Run it only after the tile run has ended: the tile job peaks at 8 GB.
Disk: about 6.5 GB for the root's tables and viewer files (scored regions ~2.4 GB, ML table ~1.6 GB, segment table
~0.8 GB, viewer and web ~1.3 GB), plus ~2 GB for `export`.

## What each consumer does on the 1 m root

The ten items of the spec's "What consumers lose or must change":

1. **Sections.** `ml.ml_table` joins `x1.arrow` by `gid` (`ml.join_x1`) and derives the 1 m flood hydraulics.
   The rf-lxa.4 keep list is in `ml.EXTRA` (`x1_w10`, `x1_w20`, `x1_wall`, `x1_vf`, `x1_b`, `x1_ffbf`,
   `x1_bank`, `x1_steep`, `x1_rough`, `x1_dropconc`; `x1_vel` registry only); `xsec1mstudy.FEATURES1M` takes those
   builders from there. `gbm_xsec` and `logit_xsec` now read the keep list; the 8 m versions stay as
   `gbm_xsec8` / `logit_xsec8` and run only where `xs_*` exist. `nz xsec`, `nz tiles` and `nz relink` refuse a
   1 m root.
2. **Truncation mask.** `viewer_nz` paints `_trunc.png` from the local catchments of the reaches with
   `truncated`; `nz export` writes one 10 m mask per tile (`gis/truncated/`) from `network.parquet`. VRTs of the
   1 m tiles (their rasters span the box) draw no-data as transparent.
3. **Rain network.** `rainfall.upstream.network_from_files` reads `outlet_kind` (3: an unresolved box edge, flag
   `tile_edge_outlet`) and `truncated` (flag 16, `area_short`) from the table, and records the `stitch` counts; the
   relink logs are not read. The weights read the land mask on the box grid of the 1 m local catchments.
4. **Reference areas.** Not rebuilt: the national run covers them. The `compare` step is the one comparison; the
   1 m viewer leaves the three areas out (`--refs` keeps them; a 5 m frame has no `x1_*`, so v5 cannot score it).
5. **Tile size.** `nz.Tile` has a size: `x05y07` is 64 km, `q10y14` 32 km (`core`, `box`, `index`,
   `Tile.parse`, `Tile.of_index`). `viewer_nz._tiles_for` and `lidar._tile_index` use it. `lidar coverage` takes
   the cell area from each raster (8 m: 64 m², 1 m tiles: 10 m cells, 100 m²) and sums the gap grid over the
   cells each tile owns; on a 1 m root it writes `<root>/analysis/lidar/`.
6. **National table.** `models.national_table` carries `models.FIELDS_1M` when the scored regions have them
   (steps, steepest 100 and 200 m, HAND widths, lake and water shares, DEM source, `stitch`, `outlet_kind`,
   `truncated`, REC2 and LINZ matches, survey year, ...). No relink on a 1 m root.
7. **Cards.** v1-v4 keep their 8 m scores and results and are archived on the 1 m site; `v5` is trained by the
   finish and is the default there; `baseline-flow-1m` is the untrained flood-size yardstick with 1 m quantiles.
8. **More reaches.** Measured in x12y16: 21,161 reaches (5,233 km) against 19,336 (4,774 km) on 8 m: +9 %; viewer
   reaches of 2 km² or more +7.5 %. The packed reaches take 141-146 bytes per reach against 114-115 (+25 %: the
   seven new columns and longer lines), score files 4 bytes per reach and card as before (two 1 m cards against
   six 8 m ones), rain weights 17 bytes per reach. Country: ~92 MB of packed reaches against 68 MB.
9. **Section placements.** The `sections` step maps every guide section on both tables and lists what changed.
   x12y16: of 17 sections, 11 the same, 5 changed (shorter or longer by up to 2 km, the Mangakarengorengo now in one
   piece), 1 moved (Waihou: the name-near-coordinates rule found another stretch 7 km away); the Pokaiwhenua is
   now placed by LINZ-only names (2.5 km shift). Review the national list before the switch.
10. **Web contract.** `Region.segments` gets seven optional columns (`s100`, `stp`, `smax`, `f1m`, `lake`,
    `dsrc`, `ly`; `web/README.md`), and the registry `archived` / `archived_evaluations`. `core/` is unchanged:
    exposing the new columns on the map is the core owner's call (rf-aq6). The viewer texts say "1 m LiDAR".

Also changed: `nzscore` reads `reaches.parquet` + `joins.parquet` (the merge has the regions); `scoring.prepare`
and the viewer leave reaches without a profile out of the smoothed gradient; `ml.add_geology` takes the karst share
from `local_km2`.

**Zero-length reaches** (rf-lxa.12, rf-lxa.13). Tiles run before 4cc040a hold reaches of 20 m-smoothed length 0: a
channel link of 3-6 path cells, whose moving average put every vertex on one point (12 in x12y16: 9 heads just
above a confluence, 3 links between two confluences). The merge now drops them into the reach below
([`nz1m-merge.md`](nz1m-merge.md)), so no national file holds them. The tile job of 4cc040a makes none (a 3-6
cell link takes its cell length); `lidarpipe.smooth_distance` now does the same for every caller (a path of at
most k + 1 vertices, k being half the window in cells, keeps its cell-step length; longer paths are smoothed as
before). The tiles' `local_catchment_core.tif` still name the dropped gids. Every reader maps them with
`national.merged_gids(root)`: a `GidMap` from `merge.json` `zero_length.merged`, read once per file, the identity
for the 8 m scan and for older merges. The cells of a dropped gid count for the gid that took its area (none,
`-1`, if the merge lost the area with it):

- rain weights (`rainfall.weights._row_lookup`: the dropped gid's lut entry points at the receiving reach's row, in
  any tile; `build_weights(gid_map=...)`, passed by `pipeline.run_weights`) and the gauge locator
  (`rainfall.validate.locate_gauges`);
- viewer `_catch.png` and `_trunc.png` (`viewer_nz.gid_lookup`);
- lidar coverage (`lidar.tile_coverage`; `catchment_cells.npz` has one row per gid, summed over the tiles; the
  land and gap grid count every cell);
- GIS: the truncation mask (`nz.truncation_rasters`) and `local_catchment_core.vrt`, which reads a mapped copy
  (`gis/local_catchment/<tile>.tif`, `nz.local_catchment_rasters`) of each tile that holds a dropped gid;
- the 8 m relink's pixel areas (`relink.tile_local_km2`; nothing to map on the 8 m scan).

## Sample results (x12y16)

- **Joins.** 8,321 reaches have a REC2 name; LINZ names 8,201, the same name on 5,998 of the 6,940 that have both.
  1,261 reaches get their name from LINZ alone. Median distance to a LINZ line 10.4 m; 73 % within 30 m. LiDAR
  survey on 98 % of reaches, resistant bed rock on 61 %.
- **Lidar coverage.** 98.2 % of reach km and 98.4 % of the land under the 1 m mosaic.
- **Rain network.** 113 outlets: 90 at an unresolved box edge (`outlet_kind` 3, all where rivers leave x12y16;
  3 over 200 km²), 945 reaches flagged `area_short` (the merge's `truncated`). 495 reaches have no local cells
  (they use the cell of their midpoint).
- **Reference area (Kaimai, 90 % of its box inside x12y16).** 89 % of the 5 m reaches of 1 km² or more match a
  1 m line within 30 m (area within 0.5-2x). Areas: median ratio 1.000, 90 % within 10 %. Smoothed gradient
  median 1.08 (q10-q90 0.87-1.47, Spearman 0.98); steepest 50 m 1.03 (0.47-1.72, 0.86); gorge depth 1.02; valley
  width 1.05; MAF 1.00. Channel length of reaches of 1 km² or more 0.94 of the 5 m, 10 km² or more 1.03, 100 km² or
  more 1.31 (the 5 m box gets its big rivers from REC2 inflows at its edge). REC2: the 1 m network is within 10 %
  of REC2 on 80 % of reaches of 5 km² or more (n 2,719), the 5 m outlet snap on 72 % (n 564). Named runs:
  Tuakopae 8.19 km² (REC2 8.49, 5 m 9.28), Mangakarengorengo 109.7 (REC2 108.4, 5 m 109.6); the Wairoa outlet is
  outside x12y16. Karangahake and Tutamoe are outside the sample.
- **Dropped gids** (rf-lxa.13; `joins` to `export` again on the re-merge `merge_x12y16_lxa12/`, 21,149 reaches,
  into `data/nz1m_sample/finish_lxa13/`; each reader against the same computation with an empty map). The 12
  dropped gids hold 53,839 of the 10 m cells, 5.384 km² (the merge moved 5.386 km² of 2 m cells). Rain weights:
  53,839 pixels of unknown gid before, 0 now; pixel area 3,985.24 -> 3,990.62 km² against 3,990.63 km² of local
  area in the table; the 12 receiving reaches hold 0.30 -> 0.9985 of their local area (each within 0.01 km²), every
  other row is unchanged. Lidar: the receivers' local area 2.29 -> 7.68 km² (7.69 in `reaches.parquet`). GIS: all
  4 tiles hold a dropped gid (4 mapped copies, none left in them); the truncation mask paints all 53,839 cells (no
  data before). Viewer: 3,438 (Waikato) and 11,609 (Bay of Plenty) more labelled `_catch.png` pixels of 22 m, no
  other label changed; Waikato's `_trunc.png` 1,657 pixels more. Export 10.4 s (6.4 s before: the 4 copies).
- **Guide research in the packs** (rf-7km; `inputs` to `viewer` on the same re-merge into
  `data/nz1m_sample/finish_rf7km/`, against the packs of `finish_lxa13/`). The packed `sta`, `gr` and `lho` now come
  from every known section (known_runs.toml and the guide research) placed on the national table as the web app's
  sections are (`viewer_nz.national_labels`), no longer from known_runs.toml alone. Bay of Plenty: labelled reaches
  34 -> 367 (8.1 -> 90.3 km), graded 11 -> 366 (II 17, III 147, IV 48, V 154), runnable without a grade 23 -> 1,
  held out 12 -> 12; 15 sections (12 research). Waikato: 0 -> 14 reaches (two RiverGuide sections, II and III-IV).
  Every other packed column is byte-identical, and the labelled reaches and their grades are exactly those of
  `<region>_sections.json` (`webdata.write_sections`). In the legacy viewer's 3D separation (Bay of Plenty, every
  reach) "Runnable, graded" goes from 11 to 366 points and "Runnable, grade unknown" from 23 to 1.
- **Legacy page removed** (rf-aq6.18; `inputs` to `web` but `lidar`, `evaluate`, `rain`, `export`, `compare`,
  `sections`, CV with one repeat, on the same re-merge into `data/nz1m_sample/finish_aq618/`, against the same
  steps run with the code before the removal). `viewer` and `scores` no longer write `index.html` and
  `index_fragment.html`; every region file, score file and web data file is byte-identical, and so are the ML page
  and the built site but for v5's `created` time. `nz viewer`, `models viewer`, `external attach` and `web data` from the command
  line (into `finish_aq618/viewer_cli/`, the 5 m reference areas included) give the same region files.
- **CV and v5 on the sample** only prove the steps run (12 rivers, 411 positive reaches); their numbers say
  nothing about the national recipes.

## Not tested, or only in part

- Every step at national size (memory and time above are estimates).
- `cv` and `v5` on the national table; the choice of `gbm_xsec` for v5 rests on the rf-lxa.4 sample study.
- `web`: LINZ lines only from the cache (no LDS key), satellite from the cache and EOX; `web pages` (publishing)
  not run.
- `rain`: the weights, zones, network and inputs are built; the hourly job (`rain ecmwf`, `totals`, `publish`) on
  the 1 m inputs and the refresh of the `rain-inputs` branch are not.
- `compare`: Kaimai only (Karangahake and Tutamoe are outside the sample).
- `switch`: unit test only (on a copy of the config).

## The switch

`river-finder nz1m switch` refuses until `score`, `table`, `viewer`, `external`, `hydro`, `ml`, `v5`, `scores`,
`web` and `rain` are done (`--force` overrides). It only writes `config/national.toml`. Then, by hand:

1. Commit `config/national.toml`, `models/v5.json`, `v5.lgb.txt`, `baseline-flow-1m.json`,
   `evaluations-1m.json`, `models/cv-1m/`, `models/audits/v5_top_runs.csv`.
2. `uv run river-finder web build` (it now reads `data/nz1m/viewer_nz`), check it, `web pages --push`.
3. Refresh the `rain-inputs` branch from `data/nz1m/rain-inputs/` ([`live-rain.md`](live-rain.md)): the hourly
   job checks the reach counts, so live rain fails ("mismatch") between the site switch and this refresh.
4. `nz1m switch --back` returns to the 8 m scan (and the 8 m site files are still in `data/viewer_nz`, `data/web`).

## Retired later (after the comparison, not yet)

- The per-region 5 m pipeline: `river-finder run | score | xsec <region>`, `validate.py`, `export.py`,
  `data/kaimai/`, `data/karangahake/`, `data/tutamoe/`, `models.reference_frame` and the reference branches of
  `write_viewer_scores`, `webdata`, `hydro`, `external`.
- The 8 m national path once the switch has held: `nz tiles`, `nz relink`, `relink.py`, `nz xsec` / `xsec.py`
  (the `xs_*`, `gbm_xsec8`, `logit_xsec8`), `truncated_core.tif`, the relink logs, `data/nz/`.

## Decisions for the user

- v5's recipe: `gbm_xsec` (gbm_terrain + the 1 m keep list), as rf-lxa.4 proposed. The national CV in the `cv`
  step runs before `v5`; if it disagrees, re-run `--steps v5 --v5-recipe <id>` with another id.
- `baseline-flow-1m`: the finish adds the untrained flood-size yardstick for the 1 m build (the 8 m one stays with
  v1-v4). Drop it if the Models page should only show v5.
- The 5 m reference areas are left out of the 1 m viewer (v5 cannot score them). `--refs` keeps them.
- Disk: ~6.5 GB for the finish (+2 GB for `export`); `segments_scored.gpkg` keeps every field (also in
  `reaches.parquet`); dropping the tile-only columns there would save ~20 %.

## Reproduce (sample)

```bash
cp -r models data/nz1m_sample/finish_full/models     # a registry of its own: the sample must not write models/
uv run river-finder nz1m finish --root data/nz1m_sample/finish_full --merge data/nz1m_sample/merge_x12y16 \
    --tiles-dir data/nz1m_sample/tiles --models-dir data/nz1m_sample/finish_full/models
uv run river-finder nz1m status --root data/nz1m_sample/finish_full
# rf-lxa.13: the readers of the local catchments on the re-merge with the zero-length drop (inputs from finish_full)
uv run river-finder nz1m finish --root data/nz1m_sample/finish_lxa13 --merge data/nz1m_sample/merge_x12y16_lxa12 \
    --tiles-dir data/nz1m_sample/tiles --inputs data/nz1m_sample/finish_full --models-dir <a copy of models/> \
    --steps inputs joins score table viewer lidar rain export
# rf-7km: the guide research in the packed labels (input layers symlinked from finish_full first, as in finish_lxa13)
uv run river-finder nz1m finish --root data/nz1m_sample/finish_rf7km --merge data/nz1m_sample/merge_x12y16_lxa12 \
    --tiles-dir data/nz1m_sample/tiles --inputs data/nz1m_sample/finish_full --models-dir <a copy of models/> \
    --steps inputs joins score table viewer
# rf-aq6.18: the steps that write viewer data and the site, without the legacy page (input layers as for rf-7km)
uv run river-finder nz1m finish --root data/nz1m_sample/finish_aq618 --merge data/nz1m_sample/merge_x12y16_lxa12 \
    --tiles-dir data/nz1m_sample/tiles --inputs data/nz1m_sample/finish_full --models-dir <a copy of models/> \
    --repeats 1 --steps inputs joins score table viewer external hydro ml cv v5 scores web
uv run pytest -q tests/test_nz1mfinish.py
```
