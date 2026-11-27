# river-finder

Finds unexplored whitewater in New Zealand from terrain, rainfall and geology.

The approach: learn the "signature" of regions that already hold great creeks (Kaimai Range,
Karangahake Gorge, Tutamoe/Waima plateau), then score every reach on flow, gradient and
height-to-area, and find runs that are not in the guides.

- Plan: [`docs/PLAN.md`](docs/PLAN.md)
- Data sources: [`docs/data-sources.md`](docs/data-sources.md)
- Flow-routing algorithms: [`docs/flow-routing-algorithms.md`](docs/flow-routing-algorithms.md)
- Reach types for style-matched evaluation: [`docs/MB1997_reach_classification_spec.pdf`](docs/MB1997_reach_classification_spec.pdf)
- Reference regions: [`regions/`](regions/)

## Quick start

```bash
uv sync
uv run river-finder run kaimai karangahake tutamoe   # ~20 min: fetch, route, validate, reaches, export, score
uv run river-finder nz viewer            # region data files -> data/viewer_nz/ (the reference areas only, without a national scan)
uv run river-finder web build            # the web app (Map, Top rivers, Models, Info) -> data/web/
uv run river-finder web serve            # http://localhost:8000/
```

The web app ([`src/river_finder/web/`](src/river_finder/web/README.md)) is the main interface, published at
https://sean-mcconnachie.github.io/river-finder/. It replaced the single-file Leaflet viewers, which were removed
on 2026-11-27 (rf-aq6.18, see `docs/PLAN.md`).

Whole country (8 m national scan, about 4 hours, 12 GB of memory):

```bash
uv run river-finder nz fetch     # national rivers, floods, geology, boundaries -> data/nz/
uv run river-finder nz tiles     # 115 tiles of 64 km: route, reach segments
uv run river-finder nz score     # pooled model; runs and candidates per river-guide region
uv run river-finder nz export    # data/nz/gis/: GeoPackage (runs, reaches, regions) + VRT rasters for QGIS
uv run river-finder nz viewer    # data/viewer_nz/: region files for the web app (~30 min)
uv run river-finder models import|split|train|evaluate|viewer   # versioned models (models/*.json; viewer: per-model score files), see PLAN
uv run river-finder external fetch|attach   # OSM waterfalls, rapids, roads, tracks per reach (Overture Maps)
uv run river-finder ml table|explore|cv|page|audit   # ML evaluation (grouped CV + LORO), the ML page, top-runs audit
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
