# Data sources

All checked in November 2026. NIWA and GNS Science merged into **Earth Sciences New Zealand**
on 1 July 2025, so some NIWA/GNS URLs may move. Licences are mostly CC-BY-4.0; check each one
before redistributing derived products.

## 0. National layers in use (Nov 2026)

`river-finder nz fetch` writes these to `data/nz/` (EPSG:2193):

| Layer | Service |
|---|---|
| REC2 reaches + names | NIWA `REC2_Layers/FeatureServer/0`, `NZ_River_Names_REC2_V1` |
| Floods (rain-driven) | NIWA `NZ_Flood_Statistics_Henderson_Collins_V2_REC1_Layer_WFL1` (MAF, Q10, Q100) and `..._HIRDSV3_REC1_Layer_WFL3` (rational-method floods from HIRDS v3 rainfall intensities) |
| Geology | GNS `NZL_GNS_250K_Geology_2023/.../FeatureServer/55` |
| Regional councils 2022 (generalised) | NIWA mirror `Regional_Council_Boundaries__gen_/FeatureServer/2` |
| Large catchments | NIWA `NZ_Large_Catchments/FeatureServer/0` (90 basins) |
| River-guide region names | riverguide.co.nz GraphQL `guides{region}`: West Coast, Tasman, Canterbury, Waikato, Otago, Bay of Plenty, Hawke's Bay, Southland, Wellington, Wanganui, Taranaki, Manawatu, Marlborough, Northland, Gisborne |

## 1. Terrain (elevation)

LINZ publishes all national and regional elevation data as Cloud-Optimised GeoTIFFs (COGs) in a
public S3 bucket. The bucket needs no account or keys, and every collection has a STAC catalogue.
This will be our main terrain source.

- Bucket: `s3://nz-elevation` (ap-southeast-2). Root catalogue:
  <https://nz-elevation.s3-ap-southeast-2.amazonaws.com/catalog.json>
- Docs: <https://github.com/linz/elevation> · <https://registry.opendata.aws/nz-elevation/>
- CRS: NZTM2000 (EPSG:2193). Vertical datum is NZVD2016 for LiDAR.

Collections we have confirmed in the bucket (paths are relative to the bucket root):

| Use | Collection | Notes |
|---|---|---|
| Regional scan (all NZ) | `new-zealand/new-zealand-contour/dem_8m/2193/` | 8 m DEM interpolated from 20 m contours (Geographx). Covers the whole country (449 tiles). Fine for catchment area. Too coarse for true reach gradients and small waterfalls. |
| National 1 m mosaic | `new-zealand/new-zealand/dem_1m/2193/` | Best-available LiDAR mosaic. It has gaps where no LiDAR exists. |
| Kaimai, east side | `bay-of-plenty/bay-of-plenty_2019-2022/dem_1m/2193/` | Also 2018-2019 and 2024 captures. |
| Kaimai, west side | `waikato/waikato_2021/dem_1m/2193/`, `waikato/waikato_2024/dem_1m/2193/` | The range crosses the Waikato/BoP boundary, so we need both regions. |
| Northland | `northland/northland_2018-2020/dem_1m/2193/`, `northland/northland_2024/dem_1m/2193/` | Covers Waima/Waipoua/Tutamoe. DOI 10.5069/G9BR8QDQ (also on OpenTopography). |
| Hillshades | `.../dem-hillshade*_1m`, `.../dem-hillshade_8m` | Ready-made basemaps for the viewer. |

The `dsm_1m` collections include trees and buildings. Use them only for things like canopy height,
never for flow routing.

Other routes: the LINZ Data Service (<https://data.linz.govt.nz>), for example BoP 2019-2022 DEM
layer 105690. The BOPRC ImageServer also serves the BoP DEM.

## 2. Rainfall / runoff

**In use (Nov 2026):** NIWA Henderson & Collins (2018) flood statistics per REC1 reach, via the public FeatureServer `NZ_Flood_Statistics_Henderson_Collins_V2_REC1_Layer_WFL1` (mean annual, 10- and 100-year floods). The MfE rainfall grid below is blocked for scripts (bot protection on data.govt.nz; the MfE portal needs an API key).


| Dataset | Resolution | Use |
|---|---|---|
| MfE / NIWA *Average annual rainfall 1972–2016* (ASCII grid) — [data.govt.nz](https://catalogue.data.govt.nz/dataset/average-annual-rainfall-19722016/resource/071be53e-b7dc-4dd8-9447-db692c47cc79) | ~500 m | Catchment-mean rainfall gives a mean-flow proxy. **Start here.** |
| NIWA national climate maps 1981–2010 — [niwa.co.nz](https://niwa.co.nz/node/105437) | 500 m | Alternative normals, offered as GIS layers. |
| HOTRUNZ monthly rainfall 1910–2019 — [ESSD paper](https://essd.copernicus.org/articles/14/2817/2022/essd-14-2817-2022.html) | 1 km monthly | Seasonality: when a creek is likely to be "in". |
| NIWA CliFlo / regional council gauges (BOPRC Environmental Data Portal, Northland Regional Council hydrology) | point | Calibrate rainfall → flow; real-time flow for trip planning later. |
| Virtual Climate Station Network (VCSN), Earth Sciences NZ | ~5 km daily | Event rainfall (later phase; may need a licence). |

## 3. Geology

| Dataset | Notes |
|---|---|
| **In use:** GNS 250K geology 2023, `NZL_GNS_250K_Geology_2023/NZL_GNS_250K_geology_FeatureService_NZ/FeatureServer/55` (geological units; fields rockgroup, rockclass, mainrock). Older: GNS 1:250 000 Geological Map of NZ (QMAP seamless) — [FeatureServer](https://gis.gns.cri.nz/server/rest/services/NZL_GNS_250K_Geology_2014/NZL_GNS_250K_geology_FeatureService/FeatureServer) · [data.govt.nz](https://catalogue.data.govt.nz/dataset/nzl-gns-1-250k-geology-2nd-edition1) | Lithology polygons with rock type, age and group. Used to build "same geology as the reference regions" masks. Can be queried from the ArcGIS REST service in EPSG:2193. |
| Free digital QMAP sheets — [GNS](https://www.gns.cri.nz/data-and-resources/digital-qmap-geological-maps-at-1250000/) | Georeferenced JPEGs. Good for checking by eye. |

## 4. Existing river network (validation + "already known" mask)

| Dataset | Notes |
|---|---|
| NIWA REC2 River Environment Classification — [niwa.co.nz](https://niwa.co.nz/freshwater/river-environment-classification-2) · [FeatureServer](https://services3.arcgis.com/fp1tibNcN9mbExhG/ArcGIS/rest/services/REC2_Layers/FeatureServer) | Reach polylines with upstream catchment area, Strahler order, and modelled mean/low flows. **The main validation target** for our flow-accumulation output. |
| NZ Local River Names (REC2) — [data.govt.nz](https://catalogue.data.govt.nz/dataset/nz-local-river-names-rec2-layer) | Names for the REC2 reaches. |
| LINZ Topo50 river centrelines, river polygons, **waterfall points**, contours (data.linz.govt.nz) | Mapped waterfalls work well as a check for knickpoint detection. |
| LINZ Topo50 road centrelines / NZ Roads (Addressing) | Access cost: distance from road to put-in and take-out. |
| **In use:** OpenStreetMap via [Overture Maps](https://docs.overturemaps.org/) GeoParquet on public S3 (`s3://overturemaps-us-west-2/release/2026-10-21.0/`, anonymous, bbox row-group pruning; `river-finder external fetch`) | Stands in for Topo50 until we have an LDS key. Waterfalls (`base/water`, class `waterfall`: 2,095 in NZ, 997 with `height`, most from the LINZ Topo50 import), 252 rapids, and 760k road/track/path segments (`transportation/segment`). ODbL: credit "© OpenStreetMap contributors". Geofabrik and Overpass are not reachable from the cloud container. |
| DOC Public Conservation Land | Access and land-tenure flags. |

## 5. Known kayaking runs (the "already paddled" set)

| Source | Notes |
|---|---|
| Whitewater NZ / NZRCA online River Guide, now at **riverguide.co.nz** (a React app; per-guide map markers come from a GraphQL API at api.riverguide.co.nz). Scripted API access was blocked in this session; markers have to be read in a browser or supplied by hand. — [whitewater.nz](https://whitewater.nz/nzrca-river-guide-now-online/) | Seeded from Graham Charles, *New Zealand Whitewater* (4th ed.). Our main "known" list. Check the terms of use before any scraping. A hand-digitised list of put-in/take-out points may be simpler. |
| Graham Charles, *New Zealand Whitewater* (guidebook) | Print reference. |
| NZ Canoeing Association 1981 North Island river survey (NIWA Water & Soil misc. pubs [14](https://webstatic.niwa.co.nz/library/w&smp14.pdf), [15](https://webstatic.niwa.co.nz/library/w&smp15.pdf)) | Historical list. Covers the Wairoa and Mangakahia. |
| Local knowledge and club sites (for example [north island rivers](https://sites.google.com/a/verner.co.nz/north-island-rivers)) | Fill gaps. |

## 6. Imagery (for checking candidates by eye)

- LINZ aerial imagery bucket `s3://nz-imagery` (same COG + STAC layout) — rural ~0.3 m.

### LINZ aerial photos on the map (bead rf-aq6.29)

| What | Source | Key |
|---|---|---|
| Survey table: one STAC collection per survey, `linz:geospatial_category`, `gsd`, capture interval, `capture-area.geojson` footprint | `https://nz-imagery.s3-ap-southeast-2.amazonaws.com/catalog.json` (public S3, CC BY 4.0) | none |
| Which surveys the LINZ Basemaps `aerial` tile set draws, bottom to top, with their zoom range | `linz/basemaps-config` on GitHub, `config/tileset/aerial.json` | none |
| The photos on the map | LINZ Basemaps tiles `https://basemaps.linz.govt.nz/v1/tiles/aerial/WebMercatorQuad/{z}/{x}/{y}.webp?api=KEY` | Basemaps API key |

`river-finder web data --only aerial` (`aerial.py`, `webdata.write_aerial`) writes `<region>_aerial.json`: per viewer
reach the survey that Basemaps draws on top at zoom 16 over most of the reach (sample points every 25 m), with its GSD
and capture dates (NZ dates), and the share of the reach under any photo. Only RGB rural, urban and (where Basemaps
draws them) ancillary storm and flood flights are used, never satellite, near-infrared or historic scans. If the
Basemaps layer list cannot be fetched, the finest rural or urban survey, then the newest, is used instead. Caches in
`data/nz/aerial/` (`surveys.json`, `basemaps_aerial.json`, `capture/<slug>.geojson`): the first run downloads about
115 MB of STAC JSON (313 collections) and 14 MB of footprints, about a minute; after that the step runs offline in
about 30 s for the whole country. `--refresh` fetches the two tables again (new surveys appear every month or two).

The site's Aerial base map is the LINZ Basemaps `aerial` layer at every zoom: rural photos from zoom 13, urban ones
from 14, and LINZ's own 10 m satellite below that and where it has no photos, so it covers the whole country. The tiles
need a LINZ Basemaps API key (not the LDS key) built into the standalone site; Basemaps keys are public by design (they
end up in the page). Without a key the Aerial option is left out; the survey still shows
on river cards, from the data file.

Coverage in November 2026 (124 Basemaps aerial layers matched to STAC; 2 Canterbury 2021 layers have no STAC
collection and are left out):

| Region | Reaches covered | Main GSD (share of km) | Capture years (share of km) |
|---|---:|---|---|
| Northland | 100 % | 0.3 m 98 % | 2023-2025 |
| Auckland | 100 % | 0.075 m 97 % (urban survey of the whole region) | 2024-2025 |
| Waikato | 100 % | 0.3 m 80 %, 0.1 m 8 % | 2021-2026 (most 2023-2024) |
| Bay of Plenty | 100 % | 0.2 m 88 %, 0.1 m 12 % | 2024-2026 |
| Gisborne | 100 % | 0.1 m 69 %, 0.2 m 29 % | 2024-2026 |
| Hawke's Bay | 100 % | 0.2 m 94 % | 2025-2026 |
| Taranaki | 100 % | 0.25 m 91 % | 2021-2022 |
| Whanganui | 100 % | 0.3 m 92 % | 2021-2022 87 % |
| Manawatū | 100 % | 0.3 m 50 %, 0.2 m 38 % | 2021-2024 |
| Wellington | 100 % | 0.2 m 89 % | 2025-2026 |
| Tasman | 100 % | 0.25 m 74 %, 0.3 m 19 % | 2020-2025 |
| Marlborough | 100 % | 0.25 m 96 % | 2021-2025 |
| West Coast | 88 % | 0.2 m 59 %, 0.3 m 33 % | 2024-2026 60 %, 2015-2017 26 % |
| Canterbury | 100 % | 0.2 m 34 %, 0.3 m 28 %, 0.25 m | 2016-2025 |
| Otago | 99 % | 0.2 m 65 %, 0.3 m 31 % | 2017-2025 |
| Southland | 100 % | 0.25 m 54 %, 0.75 m 38 % (Fiordland: the 2005-2011 survey) | 2023-2024 54 %, 2005-2011 38 % |
| Kaimai, Karangahake, Tutamoe (5 m) | 100 % | 0.2-0.3 m | 2023-2026 |

Nationally 99 % of 612 000 reaches (143 500 km); 85 % of the length is at 0.2-0.3 m, 9 % finer (urban surveys), 5 % at
0.4-1 m; 69 % was flown in 2024-2026, 4 % (Southland) in 2005-2011. The gaps are on the West Coast (12 % of its reaches:
the 2024-2026 survey is still being flown) and a little of Otago.
