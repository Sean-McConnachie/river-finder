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

## Open questions

See the beads issue labelled `question`. Answers get recorded with `bd remember`.
