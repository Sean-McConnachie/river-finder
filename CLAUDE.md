# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:46cd31e7 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/core-concepts/sync-concepts.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   bd dolt push
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->


## Beads in an ephemeral cloud container

The Dolt database (`.beads/embeddeddolt/`) is NOT committed. `.beads/issues.jsonl` is the
committed snapshot of issues (auto-exported by the hook). `.beads/memories.jsonl` holds
the `bd remember` memories. In a fresh clone:

```bash
npm install -g @beads/bd
bd init --prefix rf --non-interactive   # skip if .beads/embeddeddolt exists
bd import .beads/issues.jsonl && bd import .beads/memories.jsonl
```

After `bd remember`: `bd export --include-memories | grep '"_type":"memory"' > .beads/memories.jsonl`.

## Project overview

river-finder finds unexplored whitewater in NZ. It uses terrain (LINZ DEMs), rainfall and
geology, starting from three reference regions (Kaimai Range, Karangahake Gorge, Tutamoe/Waima plateau).

- Plan: `docs/PLAN.md` · data: `docs/data-sources.md` · algorithms: `docs/flow-routing-algorithms.md`
- Region definitions: `regions/*.toml` (bbox in WGS84, processing in EPSG:2193 NZTM)
- Known runnability labels (status, grade, extent): `labels/known_runs.toml` (`labels.py`)

## Build & Test

```bash
uv sync                                   # install (Python 3.12+)
uv run pytest -q                          # unit tests (synthetic DEMs + pysheds cross-check)
uv run ruff check src tests               # lint
uv run river-finder run kaimai karangahake tutamoe  # fetch (8 m + 5 m LiDAR, REC2, geology, floods), route,
                                          # validate, reaches, export, pooled score, viewer (~15 min)
uv run river-finder score kaimai karangahake tutamoe  # re-score only (regression is pooled across the regions given)
uv run river-finder nz fetch|tiles|score|viewer   # national 8 m scan in 64 km tiles (data/nz/, ~4 h)
uv run river-finder external fetch|attach   # OSM waterfalls/roads/tracks per reach via Overture S3 (~10 min)
uv run river-finder hydro check|attach      # hydro-diverted/regulated/reservoir reaches from config/hydro.toml -> data/nz/hydro.pkl
                                          # (~30 s; ml audit and web data --only hydro use it; docs/hydro.md)
uv run river-finder lidar fetch|coverage   # 1 m LiDAR coverage from the nz-elevation STAC: survey footprints, per-reach and
                                          # catchment share, survey + year, gaps, summary -> data/analysis/lidar/ (~7 min; docs/lidar-coverage.md)
uv run river-finder lidar bench stream|speed|route|compare|tiling|seam|lakes|report  # 1 m pipeline benchmark on tile
                                          # x05y07 (lidarpipe.py pieces, lidarbench.py) -> data/analysis/lidar-bench/ (docs/lidar-pipeline.md)
uv run river-finder nz1m plan|tile <qid...>|check  # 1 m rebuild per 32 km tile (rf-lxa.6): 1 m -> 2 m, sea/lakes, fallback, route,
                                          # reaches, 2 m + 1 m extraction -> data/nz1m/tiles/<qid>/ (~6 min, < 8 GB, ~50 MB per tile;
                                          # docs/nz1m-tile-job.md). 378 land tiles, ~26 h national: never run all without the user's go-ahead
uv run river-finder nz1m lakes survey <qid|x0,y0,x1,y1...>|build; nz1m merge [qid...] [--out DIR]  # rf-lxa.5: national lake
                                          # table (one level/outlet per lake, re-run list) and merge (stitch, lakes as nodes, network areas)
                                          # -> data/nz1m/lakes.gpkg, network.parquet, reaches.parquet, merge.json (~5 min; docs/nz1m-merge.md)
uv run river-finder xsec1m tiles|check|study|select|spacing|storage|figure  # 1 m LiDAR cross-sections (rf-lxa.4): sections every
                                          # 25 m re-centred on the 1 m channel, channel-shape features, keep list, rf-lxa.3 storage
                                          # spec -> data/analysis/lidar-xsec/ (14 tiles ~25 min, < 6 GB; docs/cross-sections-1m.md)
uv run river-finder web data|build|serve|pages  # web app (Map, Top rivers, Models, Info) -> data/web/
uv run river-finder nz xsec [tiles]; xsec <region>  # 8 m DEM cross-sections per reach (widths at 10/20 m stages, walls,
                                          # constriction) -> data/nz/xsec.pkl (~65 min, < 2 GB, resumable; docs/cross-sections.md)
uv run river-finder ml table|explore|cv|page|audit  # ML: features, grouped repeated 5-fold OOF + LORO, ML page (~10 min);
                                          # audit: top unlisted runs of a model -> models/audits/
uv run river-finder models train --kind gbm --recipe gbm_terrain --name v3  # LightGBM registry card (+ v3.lgb.txt)
uv run river-finder rain weights --grid gsmap|ecmwf|all; rain status  # rainfall: 0.1° cell -> local-catchment weights -> data/nz/rain/ (~4 min; config/rainfall.toml)
uv run river-finder rain ecmwf [--run latest|YYYYMMDDHH] [--obs] [--fc] [--reader openmeteo-s3|ecmwf-s3]  # ECMWF IFS hourly rain:
                                          # observed proxy to now (each hour from the newest run, leads 1-18) + next 24 h -> data/nz/rain/obs|fc_ecmwf.npz + .json (~1 min, ~35 MB)
uv run river-finder rain gauges [--days N] [--council wcrc ecan tdc] [--sites-only] [--discover]  # rain gauges
                                          # (Hilltop: WCRC, TDC; ECan Open Data Portal): hourly totals, quality states, ECMWF cell and
                                          # region per active gauge -> data/nz/rain/gauges/gauges_hourly.npz + .json (docs/rain-gauges.md)
uv run river-finder rain scale [--days 30] [--now YYYYMMDDHH] [--force] [--no-fetch]  # gauge scale factors per region: ECMWF
                                          # at the gauge cells (incremental store), copies of one station counted once, ratio of sums
                                          # with guard rails -> data/nz/rain/scaling/factors_ecmwf.json + .history.jsonl; re-scales the
                                          # buffers ([scaling] apply; publish = false until rf-t26.14; docs/rainfall-scaling.md)
uv run river-finder rain totals [--windows 1 2 ..] [--raw]  # upstream mm + m3 per reach and window (past/next x h) down the
                                          # network, missing-hour/product/scale flags -> data/nz/rain/totals.npz + .json (~10 s; rainfall/upstream.py)
uv run river-finder rain inputs --out DIR; rain publish --out DIR  # live rain (docs/live-rain.md): the hourly job's inputs (~35 MB,
                                          # branch rain-inputs; refresh after a national rebuild) and per-region rain files + index.json
                                          # (branch rain, force-pushed hourly by .github/workflows/rain.yml; format in rainfall/live.py)
```

Web app: `src/river_finder/web/` (contract and ownership in `web/README.md`; page briefs in
`docs/ui/`). Edit only the page folder you own; `core/` changes go through the core owner.

National tiles peak at ~12 GB: never run another heavy job (reference viewer build, 5 m
rasters) while `nz tiles` runs, or the OOM killer takes the tile run. It resumes where it
stopped.

Outputs go to `data/<region>/` (gitignored): `area_km2.tif`, `validation_summary.json`, `gis/`
(COG + .qml + GeoPackage). The browser viewer is `data/viewer/viewer.html`.
Code: `src/river_finder/`. `routing.py` holds the graph algorithms (Priority-Flood, D8, flat
resolution, Kahn accumulation), `reaches.py` the channel network and reach metrics, `scoring.py`
the run finder, signature, composite and regression, and `context.py` the geology and flood
fetchers. `ml.py` holds the CV evaluation (catchment folds, model zoo, metrics), `reachtype.py`
the Montgomery & Buffington reach types used for style-matched AUC (spec: `docs/MB1997_reach_classification_spec.pdf`),
`mlpage.py` the ML page (`data/viewer_nz/ml.html`) built from `models/cv/*.json`. The 1 m LiDAR rebuild: `lidarpipe.py` (reader, lean router,
lakes, seams, profiles), `xsec1m.py` (1 m sections) and `lidarspec.py` (the one-pass field list, stores and reach ids;
`docs/lidar-extraction-spec.md`). An 80 M-cell (5 m) region routes in about 100 s on this 4-core, 15 GB machine.

## Conventions & Patterns

- All rasters are processed in EPSG:2193. Elevations use NZVD2016.
- Route flow on DEMs only, never DSMs.
- Downloaded data goes in `data/` (gitignored). Never commit rasters.
