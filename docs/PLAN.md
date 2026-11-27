# river-finder — plan

## Goal

Find steep, runnable whitewater in New Zealand that is not in any guidebook. Method:

1. Describe two **reference regions** that already hold good, known creeks.
2. Learn what their terrain, geology and rainfall have in common.
3. Search elsewhere for places that share that signature.
4. Rank candidate reaches with simple, intuitive parameters (gradient, flow, length, access).

## Reference regions

Boxes come from tracing each river's REC2 network upstream (see `regions/*.toml`).

| Region | Known runs | Box (WGS84) | Working hypothesis |
|---|---|---|---|
| **Kaimai Range** (BoP/Waikato) | Tuakopae (Tuakopai) → **Mangakarengorengo** → Wairoa. The Wairoa and Tuakopae are in guides; the Mangakarengorengo (the flood run) is **not**. | lon 175.80–176.19, lat −38.04 to −37.66 | Miocene–Pliocene andesite/dacite (Coromandel Gp, Kaimai Subgroup) under Pleistocene welded ignimbrite sheets (e.g. Waiteariki, Mamaku). A high plateau tilts toward the Bay of Plenty. Streams cut deep gorges and falls through the welded sheets. |
| **Tutamoe / Waima plateau** (Northland, inland of Omapere, west of Kaikohe) | Waima, Waipoua, upper Mangakahia | lon 173.45–173.95, lat −35.75 to −35.35 | Miocene basalt lava plateau (Waipoua/Tutamoe basalts) over weaker sedimentary/allochthon rocks. High rainfall on the plateau. Rivers drop off the plateau edges in every direction. |

**Hold-out test.** The Mangakarengorengo is exactly the kind of river this tool exists to find.
With the Wairoa and Tuakopae marked as known, the ranking must flag the Mangakarengorengo
without being told about it.

Shared signature, to be tested: **a hard volcanic caprock plateau + high rainfall + an
escarpment edge that rivers cut through.** In data terms, that means:

- a large flat-ish upland feeding a few outlets (high catchment area at the plateau rim),
- then a sharp knickzone (high `k_sn`, gradient jump),
- on a volcanic lithology,
- with mean annual rainfall ≥ ~1800 mm.

(The geology above comes from memory. It gets checked against QMAP in rf issue *geology
profile*.)

## Approach

See [`data-sources.md`](data-sources.md) and
[`flow-routing-algorithms.md`](flow-routing-algorithms.md) for the research behind each step.

```
LINZ DEM (COG/STAC) ──► clip/mosaic/reproject (EPSG:2193)
                         │
                         ▼
               Priority-Flood (fill + flow dir)        ◄── our own impl, cross-checked vs RichDEM
                         │
                         ▼
               D8 flow accumulation (Kahn topo sort)
               ├── weight = cell area        → catchment area raster
               └── weight = area × rainfall  → mean-flow proxy raster
                         │
                         ▼
               Heat-map overlay (log10 area) on hillshade/topo   ◄── PHASE 1 DELIVERABLE
                         │
                         ▼
               Validate vs REC2 reaches & known rivers
                         │
                         ▼
               Stream network → reaches → gradient, k_sn, knickpoints, gorge depth
                         │
                         ▼
               Geology + rainfall + terrain "signature" of reference regions
                         │
                         ▼
               Similarity search over wider region → ranked candidate reaches
                         │
                         ▼
               Filter out known runs (NZRCA guide) → map + table for checking by eye
                         │
                         ▼
               Composite overlay: per-point feature stack → weighted score or
               linear/logistic regression trained on known runs → composite heat map
```

## Tech stack

- **Python 3.12+**, managed with `uv`.
- `rasterio`, `numpy`, `numba` (the core algorithms), `pystac-client`/plain STAC JSON for tile
  discovery, `geopandas`/`shapely` (vectors), `pyproj`.
- Validation: `pysheds` as a reference implementation in the tests; `pytest`.
- Output: a self-contained **Leaflet HTML viewer** (browser first), plus a **GIS export** per
  region: COG rasters with QGIS `.qml` styles and a GeoPackage of vectors.
- CLI: `river-finder fetch|route|validate|export|viewer|run [regions...]`.
- Region definitions live in `regions/*.toml` (bbox, DEM collections, known runs).

## Resolution strategy

| Stage | DEM | Kaimai box ≈ 27×39 km |
|---|---|---|
| Regional catchment heat map | 8 m contour DEM (or LiDAR resampled to 5–10 m) | 23.6 M cells, routed in ~25 s |
| Reach gradients, knickpoints | 1–2 m LiDAR, clipped to a buffer around candidate streams | tiled |

Rule: compute catchment area at coarse resolution and gradient at fine resolution.
Accumulation is robust to coarse resolution. Waterfalls are not.

**National scale.** See *National scan* below: 64 km tiles at 8 m, with REC2 supplying the
flow that enters each tile from outside.

## Phases (tracked in beads — `bd ready`, `bd list`)

0. **Scaffold**: repo, Python package, CLI skeleton, region config, tests, CI.
1. **Data fetch**: STAC tile discovery and download/mosaic for a bbox. Rainfall grid. REC2 clip.
2. **Flow routing**: Priority-Flood, D8, Kahn accumulation (area and rainfall-weighted), with
   tests on synthetic DEMs and a RichDEM cross-check.
3. **Heat map**: log-scaled catchment-area overlay and HTML viewer, for both reference regions.
4. **Validation**: compare our accumulated area with REC2 `CATCHAREA` at REC2 reach outlets, and
   our streams with REC2/Topo50 lines. Make sure the Wairoa, Waima and Mangakahia show up.
5. **Reach metrics**: stream extraction, reach segmentation, gradient (m/km), `k_sn`/χ,
   knickpoints (checked against Topo50 waterfalls), gorge depth.
6. **Signature + search**: geology profile (QMAP), rainfall, terrain stats of the reference runs,
   then a similarity score over wider search areas (all of Northland, Coromandel–Kaimai–Mamaku).
7. **Candidate ranking**: user-tunable parameters (min flow, gradient band, min length, max road
   distance). Exclude known runs. Export candidates (GeoJSON + viewer).
8. **Composite overlay**: build a feature stack per cell/reach (log area, flow proxy, gradient,
   `k_sn`, relief, rainfall, geology class, road distance). Combine it two ways:
   - user-set weights (sliders in the viewer);
   - a simple linear/logistic regression trained on labelled reaches (known runs = positive,
     sampled other reaches = negative). The Mangakarengorengo is held out, and its coefficients
     are reported so the model can be read.

   The output is a composite heat map.

## Intuitive parameters (draft, for phase 7)

| Parameter | Default | Meaning |
|---|---|---|
| `min_catchment_km2` | 5 | Enough water to float a boat after rain. |
| `min_mean_flow_m3s` | 0.5 | From area × rainfall × runoff coefficient. |
| `gradient_band_m_per_km` | 20–80 (adjustable) | Roughly grade 3–5 creeking. Above ~100 is mostly waterfalls/portages. |
| `min_reach_length_km` | 1 | A run worth the walk-in. |
| `max_road_distance_km` | 3 | Access. |
| `lithology` | from reference | Must match the reference geology class. |

## Validation (Phase 3/4): status 2026-11-11

Target: within ±10% of REC2 for ≥90% of reaches > 5 km². Results on the 8 m contour DEM, at
REC2 reaches whose whole catchment is inside the box:

| | Kaimai | Tutamoe |
|---|---|---|
| Named reference rivers | all 3 within 1.1% | all 3 within 3.2% |
| Steep reaches (≥10 m/km), within 10% | 75% | 82% |
| All reaches > 5 km², median error | 3.8% | 2.0% |
| All reaches > 5 km², within 10% | 70% | 75% |

The 90% target is **not met yet**. The remaining misses fall into three groups:
1. Snaps that land on the bigger river at a confluence (a validation artefact).
2. Channel-position offsets of 100–300 m on low-relief floodplains, where the contour DEM is flat
   between the 20 m contours. Flat resolution (Barnes 2014b) fixed the routing, but the position
   remains uncertain.
3. Real divide disagreements on flat plateau tops (Mangapapa −20%, Waiomou −35%). These matter
   for the plateau hypothesis. Next step: re-route with LiDAR resampled to 8 m.

River mouths on dune and estuary flats are ambiguous (the Waipoua reaches the sea 700 m from
REC2's mouth), so named rivers are measured ≥ 500 m inland.

## Phases 5-8: status 2026-11-11 (reference regions)

**Terrain.** Both regions are now routed on LiDAR at 5 m: the national 1 m mosaic, read through
its 4 m overviews, with gaps filled from the 8 m DEM (coverage was 100% in both). REC2 agreement
improved (within 10%, reaches > 5 km²): Kaimai 70% → 76%, Tutamoe 75% → 88%. All six named
rivers are within 1-9%.

**Reaches (phase 5).** `reaches.py` splits the channel network (≥ 0.5 km²) into ~250 m
segments. Each gets gradient, steepest 50 m, `k_sn`, a knick ratio, gorge depth, catchment
height above channel, plateau share upstream, resistant-rock share upstream, bed rock (GNS),
mean annual flood (NIWA Henderson & Collins) and stream power. Kaimai has 8,743 segments,
Tutamoe 10,220.

**Hydrology.** The MfE rainfall grid could not be fetched (data.govt.nz blocks scripts; the MfE
portal needs an API key). NIWA's mean annual flood per REC1 reach is used instead: specific
MAF from the nearest REC1 reach of similar size, times our catchment area. For flood runs this
is arguably the better measure. Kaimai pumice catchments have a much lower specific MAF (median
0.7 m³/s/km²) than Northland clay ones (about 3), so per-region flow thresholds matter.

**Geology profile (phase 6).** Known-run segments sit on resistant rock (ignimbrite, rhyolite,
andesite, basalt) far more often than other reaches: Kaimai 97% vs 60%, Tutamoe 73% vs 37%.
They are in deeper gorges (73 vs 46 m, 63 vs 37 m) and are steeper. The plateau share upstream
is *not* distinctive at reach level. The plateau signal comes through catchment height above
channel and stream power instead.

**Scoring (phases 6-8).** `scoring.py` provides three scores, all as percentiles:
- similarity to the known-run feature centroid (the reference signature);
- a user-weighted composite;
- an L2 logistic regression.

Labels are a stop-gap: steep (≥ 10 m/km), ≥ 2 km² segments on listed reference rivers count as
known runs. The real guidebook sections are needed (beads issue). The Mangakarengorengo is held
out entirely.

**Hold-out test: passed.** With default settings the Mangakarengorengo is **#1 and #2 of 47** runs
not in the guides in the Kaimai box. Its best segment is at the 99.9th percentile (regression),
99.1th (signature) and 96.8th (default composite). Leaving each listed river out in turn still
ranks it highly (median 81st-99th percentile). The Tuakopae is weakest (small creeks are
under-represented in the labels).
Caveat: the "big rivers count at lower gradients" rule was added after seeing the
Mangakarengorengo profile (15-22 m/km at a mean annual flood near 70 m³/s). The regression
ranking does not depend on it.

**Run finder (phase 7).** Segments that pass the gradient band, minimum flood flow and length
are chained downstream into runs, allowing one-segment gaps. Optionally the minimum gradient is
scaled by (MAF/20)^-0.5 so big rivers count at lower gradients. The same logic runs live in the
viewer with sliders.

**Not done yet:** the NZRCA guide as the "known" layer (needs the guide or your list), North Island
scaling. Road/access distance: done on 2026-11-18, see "Waterfalls and access".

## Phase 9: three-parameter runnability model (2026-11-11)

Sean's framing: **flow, gradient and height-to-area** (or height-to-width) are the three main
signs that a reach is runnable.

**Measures (per ~250 m reach)**
- Flow: NIWA mean annual flood (m³/s).
- Gradient: smoothed over ~750 m. With "big rivers count at lower gradients" on, it is multiplied
  by clip(√(MAF/20), 1, 3).
- Height-to-area: Melton ruggedness, catchment relief / √catchment area (relief = highest point
  upstream minus channel elevation).
- Height-to-width (alternative): gorge depth / valley-floor width. The width is the ground within
  5 m of its own channel (HAND), per metre of channel.

**Scoring.** Flow and gradient get trapezoid bands. The defaults give full credit at 8-300 m³/s
and 20-80 m/km, falling to zero at 2 / 2000 m³/s and 5 / 150 m/km. Height-to-area gets an
increasing ramp from the 10th to the 60th percentile of all reaches. The weighted mean must reach
the threshold (default 0.7). With the gate on, every parameter must also be above 0. Passing
reaches chain into runs, and runs are put into Pareto layers by non-dominated sorting on
(log flow, effective gradient, height-to-area). Layer 1 is the frontier. Everything is live in the
viewer (bands, weights, threshold, measure), with a flow-vs-gradient scatter plot of all runs.

**What the data says**
- Percentile of each known run's median among all reaches (Kaimai): Tuakopae 76th (Melton) /
  80th (height-to-width), Mangakarengorengo 54th / 58th, Wairoa 38th / 76th. Height-to-area
  separates the known runs only weakly.
- In the trained model, once steepness and stream power are included, both height measures get
  *negative* weights (Melton -0.66, height-to-width -0.42). The labelled runs are mostly large
  rivers, and Melton falls with size.
- The Pareto frontier collects the extremes: small, very steep, rugged creeks and the biggest
  rivers. Big-volume flood runs sit inside it. The Mangakarengorengo ranks **#25 of 90** by
  "Pareto, then weighted score" (layer 2), but **#2 of 90** by the trained model.

**Next (beads rf-zmy.4)**: once real guide sections exist, fit the weights (logistic, or XGBoost
with monotone constraints) on the three parameters plus context, and report where known runs sit
on the frontier.

**Karangahake Gorge** was added as a third reference region: the Ohinemuri above Karangahake,
208 km² (306 km² with the Waitawheta), routed on 5 m LiDAR (REC2 within 10%: 88%). The model
finds the K Gorge as a run. Leaving it out of training, it still ranks at the 78th percentile
(median), with half its reaches in the top 10%.

## 3D separation viewer (2026-11-11)

The viewer has a "3D separation" tab. It plots any three of 11 measures (default: flood flow,
gradient, height-to-area) for runs at the current run-finder settings, or for every reach over
2 km², in one region or all three. Points are coloured by what we know:
- runnable with a known grade: ordinal blue ramp, II-VI;
- runnable, grade unknown: green;
- unknown: yellow;
- not runnable: red. None are labelled yet.

The Mangakarengorengo is drawn as a diamond (held out). Clicking a point jumps to it on the map.
A table gives each axis's AUC (runnable vs unknown).

Labels live in `labels/known_runs.toml`. Each entry has a status and an optional grade, and its
extent is one of: put-in/take-out, an outlet plus a distance upstream, or the whole steep river.
Current grades: Wairoa 5 (rafting listings), Ohinemuri / K Gorge IV-IV+ (flood), Mangakahia 3.

First reading (runs, all regions, default settings): flood flow separates strongly (AUC 88%;
runnable median 73 vs unknown 14 m³/s), gradient not at all (48%), and height-to-area separates
*inversely* (14%: known runs have lower relief/√area). The labelled runs are mostly big rivers,
so this mostly measures size. We need more labels, especially small creeks and no-go reaches,
before these numbers mean much.

## National scan (2026-11-13)

`river-finder nz fetch|tiles|score|viewer`. Code: `national.py`, `nz.py`, `nzscore.py`,
`viewer_nz.py`.

**Data (`data/nz/`, ~0.7 GB).** All from public ArcGIS FeatureServers, fetched by object id in
parallel (about 15 minutes):

| file | contents |
|---|---|
| `rec2.gpkg` | 593,517 REC2 reaches with topology, CUM_AREA, CATAREA and names |
| `flood.gpkg` | 580,293 REC1 reaches: Henderson & Collins MAF/Q10/Q100 plus the HIRDS v3 rational-method floods (rainfall-intensity based) |
| `geology.gpkg` | 55,211 GNS 250K units with the `hard_rock` flag |
| `councils.gpkg`, `large_catchments.gpkg` | 2022 regional council boundaries; 90 named large basins |

The MfE annual-rainfall grid is still unreachable (bot protection / API key), so rainfall enters
through the two flood models, which are both driven by rainfall.

**Tiles.** NZ is cut into 64 km × 64 km tiles in NZTM (115 hold REC2 rivers). Each tile is
routed on the 8 m DEM with a 3 km halo, and only the core is kept. Two fixes make tile edges
behave:

- *Inflows*: where a REC2 reach enters the box, its area at the crossing (cum area minus the
  inside share of its own local area) is added as a point inflow before accumulation. The
  Waikato at Hamilton gets 8,633 km² (REC2: 8,405 km²) instead of 1,755 km².
- *Outlets*: the box edge is an outlet only within 100 m of where a REC2 reach leaves it (plus
  the sea). Otherwise a lake touching the edge (the Waikato hydro lakes) drains out of the
  nearest edge.

A tile takes 1-3.5 minutes and up to 12 GB of memory, so tiles run one at a time (the next
tile's DEM downloads during routing). Per tile only ~20 MB is kept: core segments, an 8 m
log-area code raster, a 24 m hillshade, local catchments and the truncation mask.

Agreement with REC2 on the Hamilton tile (lowland, 8 m contour DEM): 52% of reaches > 5 km²
within 10% (60% for hill-country reaches), lower than the 76-88% we get from 5 m LiDAR. The 8 m
scan is for finding candidates; refine candidates on LiDAR.

**Regions.** Filters follow the river guide's regions (riverguide.co.nz): the regional
councils, with Nelson filed under Tasman and Manawatū-Whanganui split into *Wanganui*
(Whanganui, Whangaehu, Turakina, Rangitīkei, Waitotara and the coast north of the Rangitīkei
mouth) and *Manawatu* (the rest). Auckland is kept although the guide has no sections there.

**Scoring.** One regression pooled over the country. Known runs are still the reference rivers,
matched by name only inside their reference box (names such as Wairoa recur). Runs, Pareto layers
and candidates are made per region: `data/nz/regions/<slug>/`, plus `data/nz/candidates.csv`.

**First full run (2026-11-13).** 115 tiles in 219 minutes, peak memory ~12 GB, 1.4 GB of tile
outputs kept. 1,104,794 reach segments, 22,638 runs. Scoring takes 3 minutes.

| region | segments | runs | | region | segments | runs |
|---|---:|---:|---|---|---:|---:|
| Northland | 47,615 | 1,037 | | Wellington | 34,060 | 1,050 |
| Auckland | 16,255 | 263 | | Tasman | 38,848 | 1,384 |
| Waikato | 108,292 | 1,339 | | Marlborough | 38,100 | 1,083 |
| Bay of Plenty | 53,610 | 1,016 | | West Coast | 90,178 | 3,206 |
| Gisborne | 32,793 | 1,499 | | Canterbury | 201,646 | 2,889 |
| Hawke's Bay | 60,842 | 1,347 | | Otago | 132,069 | 1,701 |
| Taranaki | 33,487 | 680 | | Southland | 119,632 | 2,112 |
| Wanganui | 63,019 | 1,197 | | | | |
| Manawatu | 34,348 | 835 | | | | |

Hold-out Mangakarengorengo: best reach 99.9th percentile nationally, median 88th, half its
reaches in the top 10%. Leave-one-river-out medians 94-99.7. The national top candidates include
well-known runs that are not yet labels (Whakapapa, Tongariro, Karangarua, Tutoko, Manganui), which
is a good sign the reference signature transfers; rf-zmy.7 turns the guide research into labels.

**Viewer.** `data/viewer_nz/`: `index.html` holds only an index; picking a region fetches its
JSON, packed binary segments (~85 bytes each, shipped as base64 text because the artifact host
serves no raw binary) and four images (≤ 4000 px, the iOS canvas limit). Run strokes thin out at
regional zoom. `nz viewer --page-only` re-renders only the page after a template change.
The three 5 m reference areas stay available in the same drop-down.

Since the UI redesign (epic rf-aq6) the site is the web app (`src/river_finder/web/`, `river-finder web build`; its
README), which reads these same region files. The Leaflet pages (`data/viewer_nz/index.html` and the reference
areas' `data/viewer/viewer.html`, from `assets/viewer_template.html`) are legacy, kept until the web app also has
what only they still show (rf-aq6.18, checked 2026-11-27): ranking runs by Pareto layer, weighted score, similarity
or one's own feature weights, with the flow-vs-gradient scatter of runs; every reach coloured by its score; the 3D
separation by label status and grade, with runs as points and several regions at once; the Models view's
per-region table and per-river size-matched, style-matched and best percentiles; the reference areas' REC2
agreement table and reference-river areas (model vs REC2). The NIWA REC2 line overlay is replaced by the LINZ river
lines by design.

## Model versions and the guide research (2026-11-13)

`labels/guide_research.json`: 339 sections of grade 2 and up compiled from the NZ RiverGuide API
(269), packraftingtrips.nz (62) and four other sources. Licence CC BY-SA 3.0 NZ (RiverGuide content;
attribution in the file's `metadata`). 274 put-ins are published, but 301 take-outs were inferred
(from directions or geography), so provenance matters.

**Import** (`research.py`): tolerant of layout; `[lat, lon]` points; per-point provenance from
`inferred_put_in_coords` / `inferred_take_out_coords`. Guide region comes from the put-in point
(the research's "Manawatū-Whanganui" spans our Wanganui and Manawatu filters).

**Placing sections on reaches** (`models.map_section`), in order:
put-in to take-out down the flow path (236); put-in walked downstream to the reach nearest the
take-out when the two snapped to different branches (31); put-in + stated length (24); the river's
steep reaches near the points (47, mostly sections with no take-out); 11 unmapped (points only
rounded to 0.1°). Snapping prefers a reach with the same (normalised) name within 3 km.

**Rivers, not reaches.** Sections are grouped into rivers (same normalised name in the same guide
region, or any shared reach), so a river with several sections, or listed both in
`known_runs.toml` and the research, is either all train or all test.
Split `models/splits/guides-v1.json`: 144 train / 49 test rivers, stratified by guide region x
grade band, seed 20261113, the Mangakarengorengo forced into test. Splits and cards are frozen.

**Models** (`models/*.json`, one card each, never overwritten):

| id | trained on | notes |
|---|---|---|
| baseline-flow | nothing | flood size (MAF) only: the yardstick |
| v1 | 6 reference rivers | checkpoint of the first national run; reproduces its scores within 0.09 percentile points |
| v2 | 144 guide rivers, all coordinates | 9,418 positive reaches |
| v2-published | same rivers, published coordinates only | inferred take-outs -> put-in + stated length; 6,289 positives |

Unlabelled reaches are negatives (positive-unlabelled); known reaches a variant cannot place are
left out of training, never used as negatives.

**Results on the 49 held-out guide rivers** (`models/evaluations.json`; background = 594k reaches of
2 km2 or more not on any known section):

| model | AUC | size-matched AUC | top-10% recall | AUC, published-coordinate test sections |
|---|---|---|---|---|
| baseline-flow | 0.917 | 0.548 | 81% | 0.929 |
| v1 | 0.530 | 0.477 | 17% | 0.520 |
| v2 | 0.931 | 0.695 | 81% | 0.949 (size-matched 0.733) |
| v2-published | 0.925 | 0.670 | 78% | 0.945 (size-matched 0.705) |

Readings: (1) v1's 99th-percentile reference hold-out did not carry over: on guide rivers nationally it
is at chance. Six rivers in three small areas taught it their local signature (hard rock, plateau
headwaters), not whitewater. (2) Flood size alone gets AUC 0.92 against all reaches of 2 km2 or
more, so plain AUC flatters every model; the **size-matched AUC** (each test reach ranked only
against background reaches in the same flood-size decile) is the number to watch: v2 lifts it from
0.55 to 0.70. (3) Inferred coordinates help slightly (v2 > v2-published on both test views): more,
noisier labels beat fewer, cleaner ones here. (4) All metrics are lower bounds (unknown runs sit in
the background); precision cannot be measured without labelled not-runnable reaches.

**Viewer.** Model picker (URL hash `#<region>/<model>`), per-model score files
`<region>_m_<model>.txt` (base64: uint16 national percentile x 100, then uint8 flags: bits 0-1
trained on / test river, bit 2 on a guide section) so adding a model needs no region rebuild:
`river-finder models viewer` writes them for every card and re-renders the page. Top runs carry
Training river / Test river / Prediction and In guides pills that follow the selected model; the
Models tab shows every model on each test set, per-river and per-region tables and coefficients.
The packed reaches' label status, grade and held-out flag (`sta`, `gr`, `lho`) come from the same placed
sections, research included (`viewer_nz.national_labels`, rf-7km; before, known_runs.toml only), so the 3D
separation colours research runs by their grade.

Workflow for a new model: `models train --name v3 --split guides-v1 [--variant sourced]`, then
`models evaluate`, then `models viewer`, then publish the new `*_m_v3.txt` files and the page.

## Waterfalls and access (2026-11-18)

`river-finder external fetch` reads OpenStreetMap waterfalls, rapids, roads and tracks for NZ from
the Overture Maps GeoParquet release on S3 (no key; ~30 s). `river-finder external attach` (~9 min)
gives every reach (1.1 M national + the three 5 m areas):

- `d_road_m`: distance to the nearest public road (OSM motorway to service; capped at 20 km);
- `d_track_m`: nearest 4WD track or road closed to motor vehicles (`access=private`/`no`);
- `d_foot_m`: nearest path, footway, bridleway or cycleway (most DOC tracks);
- `falls_n`, `falls_max_h_m`: waterfalls within 100 m of the reach line, tallest mapped height.

National values go to `data/nz/external.pkl` (`national_table` row order), viewer files to
`<region>_x.txt` and `<region>_falls.json`. In the viewer a run shows road distance at the put-in
and take-out (and the closest point along it), 4WD and foot-track distance, and its mapped waterfalls;
the map has a waterfall/rapids layer.

**Knickpoint check** (`data/nz/waterfall_check.json`): 1,634 reaches hold a mapped waterfall. How
well each terrain feature separates them from other reaches (AUC; size-matched = within catchment-area
deciles):

| feature | AUC | size-matched | falls ≥ 5 m | median with / without falls |
|---|---|---|---|---|
| steepest 50 m | 0.86 | 0.89 | 0.92 | 380 / 32 m/km |
| smoothed gradient | 0.82 | 0.86 | 0.87 | 140 / 25 m/km |
| knick ratio | 0.72 | 0.71 | 0.73 | 1.7 / 0.86 |

So the 8 m DEM's steepest-50 m window does find real falls; the k_sn jump is a weaker signal.
OSM's waterfall coverage is patchy (Topo50 import plus volunteers), so "no mapped waterfall" is
not "no waterfall".

Not used in the models yet: access is not runnability, and a waterfall feature would need care
(falls are both the attraction and the portage). Candidates for v3 (rf-gz6): `falls_max_h_m` as a
"likely unrunnable drop" flag.

## ML evaluation: style-matched, catchment-grouped CV (2026-11-18)

`river-finder ml table|explore|cv|page` (~10 min after the national scan) builds the ML page
`data/viewer_nz/ml.html`, which holds the data views, all metrics and the full documentation. Code:
`ml.py` (features, folds, models, metrics), `reachtype.py` (reach types), `mlpage.py` (page).

**Decisions** (from the brainstorm):

- Labels stay positive-unlabelled. Every AUC is a lower bound. There is no two-stage
  runnable-then-grade model, because there are no reliable negatives.
- No guide attributes as features: tags, portages, deaths, popularity, commercial rafting, access
  type, gauge, flow notes, length and the guide gradient are all excluded.
- Access never enters a ranking. Road and track distances stay information for the person reading
  the map.
- Headline metric: **style-matched AUC**. Each reach gets a Montgomery & Buffington (1997) reach
  type, from spec `mb1997-ops-1.0.0` (`docs/MB1997_reach_classification_spec.pdf`), Track B with
  slope and confinement only. The type maps to a paddling style, and known runs are compared only
  with background reaches of the same style. Confinement = valley width / (4.0·MAF^0.5).
- CV: 5 folds × 5 repeats. Folds are whole sea-outlet catchments, balanced on positive rivers.
  Leave-one-region-out (LORO) is reported as well. The domain is all reaches with an area of at
  least 2 km² (594,736). Positives: 14,130 reaches on 195 rivers.

**Styles.** Background and known runs use the same classifier:

| | Pool-riffle | Read-and-run | Bedrock | Pool-drop | Boulder garden | Too small |
|---|---|---|---|---|---|---|
| Background (all reaches) | 45% | 27% | 18% | 4% | 4% | 2% |
| Known-run reaches | 50% | 11% | 35% | 4% | 0.5% | 0.2% |

39% of reaches are transitional (top two types within 0.15), which is expected with two inputs.

**Results** (OOF mean over repeats; the fold-to-fold sd is about 0.03 and the repeat sd about
0.002):

| model | AUC | style-matched | size-matched | style × size | recall@10% | LORO style |
|---|---|---|---|---|---|---|
| flow | 0.905 | 0.896 | 0.540 | 0.679 | 0.73 | 0.885 |
| logit13 | 0.926 | 0.915 | 0.712 | 0.754 | 0.78 | 0.899 |
| logit_plus | 0.932 | 0.920 | 0.740 | 0.770 | 0.80 | 0.899 |
| gbm13 | 0.933 | 0.924 | 0.782 | 0.792 | 0.80 | 0.909 |
| gbm_terrain | 0.938 | 0.929 | 0.789 | 0.804 | 0.81 | 0.908 |
| gbm_osm | 0.938 | 0.929 | 0.790 | 0.804 | 0.81 | 0.910 |
| gbm_xy | 0.936 | 0.930 | 0.800 | 0.812 | 0.81 | 0.907 |

What this says:

- **Style matching alone does not remove size.** Styles come from slope and confinement, so
  within a style the bigger rivers still win: flood size alone gets 0.896 style-matched but 0.540
  size-matched. Style × size is the metric that separates the models. The page plots it next to
  the headline.
- **Terrain shape beyond size is worth about +0.11 to +0.12 style × size AUC**: flow-only scores
  0.68, the logistic models 0.75–0.77 and LightGBM 0.79–0.80. The non-linear model gains about
  0.03 over the logistic one. Geology adds about +0.01 (gbm13 → gbm_terrain), within fold noise.
- **The OSM waterfall and rapids features add nothing.** Their split gain is about 0 and their
  univariate AUC is 0.50. Mapped falls touch only 959 domain reaches, so the "a documented river
  has mapped falls" bias does not show up. The karst bed flag also has zero gain. Karst share
  upstream has a little (0.03).
- **Coordinates** (`gbm_xy`) give the best CV numbers (style × size 0.812), but with the largest
  fold sd (0.043). Under LORO they drop below gbm_terrain and gbm_osm (0.907 vs 0.908–0.910), and
  CV for Gisborne falls to 0.70. They learn where guide authors paddled, so do not use them for
  ranking.
- **LORO ≈ CV on the same rows** for every model (within 0.01), so the catchment-grouped folds are
  not leaking much. West Coast (0.84–0.86) and Manawatu are the hardest held-out regions.
- **By style:** LightGBM gets 0.94 in pool-riffle and 0.93 in bedrock, but 0.73–0.76 in the steep
  styles (boulder garden, too small). Those styles have only 69 and 33 known-run reaches.
- **By grade:** 4+ sections score highest (0.94 style-matched), ≤3 and 3–4 score 0.92–0.93.
- **Gain importance** without coordinates: MAF 0.36–0.39, then Melton ruggedness, head, plateau
  share and resistant rock upstream (0.08–0.13 each). With coordinates, x and y take 0.22 between
  them.
- The existing v1/v2 models, re-scored on the rebuilt table, reproduce their earlier AUC to the
  fourth decimal. v2 gets 0.937 style-matched and 0.733 size-matched on the guide rivers
  (`models/evaluations.json`).

## v3: LightGBM in the registry, and the first top-runs audit (2026-11-18)

`models/v3.json` is the `gbm_terrain` recipe frozen as a registry card, the first of kind `gbm`. The
LightGBM model is in `models/v3.lgb.txt` (1.4 MB), checked against `booster_sha256`. Like every
card it is frozen: `save_gbm` refuses to replace it with a different model.

- **Inputs:** the 13 v2 features plus valley width, confinement, karst bed, karst share upstream, rock
  age, and GNS rock class and group. No coordinates, no OSM and no access.
- **Training:** the same split and labels as v2 (`guides-v1` training rivers, every section, held-out
  rivers left out), with the CV recipe: reaches of 2 km² or more, 30% of background, weighted back
  up, fixed seed, deterministic LightGBM.
- **Train it:** `river-finder models train --kind gbm --recipe gbm_terrain --name v3 ...` (3 s).
- **Scoring:** `models.logits` runs on any reach frame. `ml.enrich` adds geology, karst and reach type
  to frames that lack them, such as the 5 m reference areas. National reaches are scored from
  `ml_table`, because karst share needs the whole-NZ network. The viewer files are
  `<region>_m_v3.txt`, as for v2.
- **Registry:** v3 is now the default model: the newest card trained on every section. The registry
  payload also carries the CV summary (`cv`) and v3's gain importance.

Held-out guide rivers (`guides-v1`, 49 rivers; `models/evaluations.json`, which now also has a
style × size column):

| model | AUC | size-matched | style-matched | style × size | recall@10% |
|---|---|---|---|---|---|
| baseline-flow | 0.917 | 0.548 | 0.905 | 0.711 | 0.81 |
| v2 | 0.931 | 0.695 | 0.921 | 0.768 | 0.81 |
| v2-published | 0.925 | 0.670 | 0.915 | 0.746 | 0.79 |
| **v3** | **0.952** | **0.840** | **0.948** | **0.859** | **0.85** |

With published coordinates only, v3 scores 0.973 AUC and 0.904 style × size. v3 also scores 0.977
AUC on the Mangakarengorengo reference test, against 0.889 for v2.

This split holds out whole rivers but not whole catchments, so it flatters a flexible model. The
catchment-grouped CV is the more honest comparison: `gbm_terrain` gets 0.804 style × size there,
against 0.754 for `logit13` (the v2 recipe).

**Audit** (`river-finder ml audit`, `models/audits/v3_top_runs.csv`, notes in
`docs/audits/v3-top-runs-2026-11-18.md`):

- The candidates are the top 1% of unlisted reaches (the national top 1% is 97% guide reaches),
  chained into runs. Each run carries an out-of-fold percentile as a stability check.
- 43 of the top 60 rivers are other stretches of guide rivers, and 11 are flat big-river reaches.
- Two are DEM flats (0 m/km): the Roaring Lion and the lower Arawhata.
- Three are hydro-affected, including the Waikaremoana outlet.
- The plausible new leads are mostly remote South Westland and Fiordland rivers: Mahitahi, Olivine,
  Mueller, Clarke, Hidden Falls Creek, Moeraki, Mikonui and Esk.

Follow-ups are in beads: a gradient or style floor for discovery ranking, a hydro mask, a section
mapping fix, and a human check of the leads.

## Open questions

See the beads issue labelled `question`. Answers get recorded with `bd remember`.
