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

**North Island scale.** The North Island at 8 m is about 1.8 billion cells, too many for one
in-memory grid. Plan: split by coastal-outlet basins, which drain independently, or use tiled
Priority-Flood (Barnes 2016). Beads issue under phase 6.

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

**Not done yet:** road/access distance (OSM and LINZ hosts unreachable), the NZRCA guide as the
"known" layer (needs the guide or your list), North Island scaling.

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

## Open questions

See the beads issue labelled `question`. Answers get recorded with `bd remember`.
