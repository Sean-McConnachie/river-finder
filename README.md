# river-finder

Finds unexplored whitewater in New Zealand from terrain, rainfall and geology.

The approach: learn the "signature" of regions that already hold great creeks (Kaimai Range,
Tutamoe/Waima plateau), then search for unpaddled reaches that match it.

- Plan: [`docs/PLAN.md`](docs/PLAN.md)
- Data sources: [`docs/data-sources.md`](docs/data-sources.md)
- Flow-routing algorithms: [`docs/flow-routing-algorithms.md`](docs/flow-routing-algorithms.md)
- Reference regions: [`regions/`](regions/)

## Quick start

```bash
uv sync
uv run river-finder run kaimai tutamoe   # ~3 min: fetch, route, validate, export, viewer
open data/viewer/viewer.html             # browser heat map
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
