# river-finder — plan

## Goal

Find steep, runnable whitewater in New Zealand that is not in any guidebook. Method:

1. Describe two **reference regions** that already hold good, known creeks.
2. Learn what their terrain, geology and rainfall have in common.
3. Search elsewhere for places that share that signature.
4. Rank candidate reaches with simple, intuitive parameters (gradient, flow, length, access).

## Reference regions

| Region | Known runs | Approx. search box (WGS84) | Working hypothesis |
|---|---|---|---|
| **Kaimai Range** (BoP/Waikato) | Wairoa, Tuakopai, Mangakarengorengo(?) | lon 175.80–176.10, lat −37.95 to −37.60 | Miocene–Pliocene andesite/dacite (Coromandel Gp, Kaimai Subgroup) under Pleistocene welded ignimbrite sheets (e.g. Waiteariki, Mamaku). A high plateau tilts toward the Bay of Plenty. Streams cut deep gorges and falls through the welded sheets. |
| **Tutamoe / Waima plateau** (Northland, inland of Omapere, west of Kaikohe) | Waima, Waipoua(?), Mangakahia | lon 173.35–173.90, lat −35.80 to −35.35 | Miocene basalt lava plateau (Waipoua/Tutamoe basalts) over weaker sedimentary/allochthon rocks. High rainfall on the plateau. Rivers drop off the plateau edges in every direction. |

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
```

## Tech stack

- **Python 3.12+**, managed with `uv`.
- `rasterio`, `numpy`, `numba` (the core algorithms), `pystac-client`/plain STAC JSON for tile
  discovery, `geopandas`/`shapely` (vectors), `pyproj`.
- Validation: `richdem` and/or `pysheds` as reference implementations; `pytest`.
- Output: Cloud-Optimised GeoTIFF rasters plus a static **Leaflet/MapLibre HTML viewer** with
  hillshade, catchment-area heat map, REC2 rivers and a click-for-catchment tool. The GeoTIFFs
  also open directly in QGIS.
- CLI: `river-finder fetch|route|heatmap|validate|score --region kaimai`.
- Region definitions live in `regions/*.toml` (bbox, DEM collections, known runs).

## Resolution strategy

| Stage | DEM | Kaimai box ≈ 27×39 km |
|---|---|---|
| Regional catchment heat map | 8 m contour DEM (or LiDAR resampled to 5–10 m) | ~17 M cells (seconds) |
| Reach gradients, knickpoints | 1–2 m LiDAR, clipped to a buffer around candidate streams | tiled |

Rule: compute catchment area at coarse resolution and gradient at fine resolution.
Accumulation is robust to coarse resolution. Waterfalls are not.

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

## Intuitive parameters (draft, for phase 7)

| Parameter | Default | Meaning |
|---|---|---|
| `min_catchment_km2` | 5 | Enough water to float a boat after rain. |
| `min_mean_flow_m3s` | 0.5 | From area × rainfall × runoff coefficient. |
| `gradient_band_m_per_km` | 20–80 | Roughly grade 3–5 creeking. Above ~100 is mostly waterfalls/portages. |
| `min_reach_length_km` | 1 | A run worth the walk-in. |
| `max_road_distance_km` | 3 | Access. |
| `lithology` | from reference | Must match the reference geology class. |

## Validation definition of done (Phase 3/4)

- The catchment-area heat map visually tracks the mapped rivers in both reference regions.
- At REC2 reach outlets inside the boxes, our catchment area is within ±10 % of REC2's for at
  least 90 % of reaches with catchment > 5 km².
- Named reference rivers are detected as continuous high-accumulation lines.

## Open questions

See the beads issue labelled `question`. Answers get recorded with `bd remember`.
