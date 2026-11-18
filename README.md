# river-finder

Finds unexplored whitewater in New Zealand from terrain, rainfall and geology.

The approach: learn the "signature" of regions that already hold great creeks (Kaimai Range,
Karangahake Gorge, Tutamoe/Waima plateau), then score every reach on flow, gradient and
height-to-area, and find runs that are not in the guides.

- Plan: [`docs/PLAN.md`](docs/PLAN.md)
- Data sources: [`docs/data-sources.md`](docs/data-sources.md)
- Flow-routing algorithms: [`docs/flow-routing-algorithms.md`](docs/flow-routing-algorithms.md)
- Reference regions: [`regions/`](regions/)

## Quick start

```bash
uv sync
uv run river-finder run kaimai karangahake tutamoe   # ~20 min: fetch, route, validate, reaches, score, export, viewer
open data/viewer/viewer.html             # browser heat map
```

Whole country (8 m national scan, about 4 hours, 12 GB of memory):

```bash
uv run river-finder nz fetch     # national rivers, floods, geology, boundaries -> data/nz/
uv run river-finder nz tiles     # 115 tiles of 64 km: route, reach segments
uv run river-finder nz score     # pooled model; runs and candidates per river-guide region
uv run river-finder nz export    # data/nz/gis/: GeoPackage (runs, reaches, regions) + VRT rasters for QGIS
uv run river-finder nz viewer    # data/viewer_nz/index.html (serve the folder over HTTP; ~30 min)
uv run river-finder models import|split|train|evaluate|viewer   # versioned models (models/*.json), see PLAN
uv run river-finder external fetch|attach   # OSM waterfalls, rapids, roads, tracks per reach (Overture Maps)
```

Each region's GIS export is in `data/<region>/gis/`. Drag the `.tif` files and the `.gpkg` into
QGIS; `area_km2.qml` styles the heat map automatically.

## Task tracking

Work is tracked with [beads](https://github.com/gastownhall/beads) (`bd`):

```bash
npm install -g @beads/bd   # once per machine
bd ready                   # what can be worked on now
bd list --all              # everything
bd memories                # persistent project knowledge
```

`.beads/issues.jsonl` is committed as a readable snapshot of the issue database.
