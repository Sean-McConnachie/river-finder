# Brief: Top rivers page (`#top`)

**Beads:** `rf-aq6.5` (epic `rf-aq6`). Claim it with `bd update rf-aq6.5 --claim`.
**You own:** `src/river_finder/web/pages/top/` only. Ask for anything in `core/` through a beads
issue under `rf-aq6` (or a note on `rf-aq6.5`); the core owner is building the Map page in parallel.
**Branch:** commit to your session branch and merge to `main` when a step works (the user asked for
merges to `main` as work lands). Pull `main` first; other agents push there too.

## What the page is for

The ranked list of the best rivers, and the bridge between the map browser and the model interface:

- rank runs (consecutive high-scoring reaches) for the current model (`RF.state.model`) and run-finder
  settings (`RF.state.runParams`), per region or across regions;
- make it obvious which are new leads (`status: "candidate"`, not in the guides), which are known
  (`listed`), and which are held-out test rivers (`heldout`);
- every row opens on the map (`RF.go("map", {select: run})`) and in full detail
  (`RF.detail.open(run)`); the river detail view is built by the Map page (rf-aq6.11);
- link to the Models page (`RF.go("models")`) for why a river ranks where it does, and follow model
  changes live (`RF.on("model", ...)`, `RF.on("runs", ...)`);
- useful filters and sorts: grade band (once known sections land), length, gradient, flow, access
  (distance to road: `run.acc`), mapped waterfalls (`run.falls`), not-in-guides only.
- show the ranking in use (rf-aq6.33): `RF.state.runParams.rankBy`, set on the Models page (model, Pareto layer,
  weighted score, similarity to known runs, your feature weights). The line above the list names it and says what
  the score is, with "Change the ranking" (`RF.go("models", {section: "models-rf"})`); the score column's title
  follows it; runs on the Pareto frontier (`run.layer === 1`) carry a "Pareto frontier" pill; ranked by Pareto
  layer, the layer shows under the score and "All regions" orders by layer, then score.

## What you can use

Read `src/river_finder/web/README.md` (the `RF` contract). Most relevant:

- `RF.data.regions()`, `RF.data.region(id)`, `RF.data.scores(regionId, modelId)`
- `RF.runs.find(region, {model})` → runs best first (fields in the README: `name`, `len`, `grad`,
  `s50`, `maf`, `aBot`, `score`, `status`, `acc`, `falls`, ...)
- `RF.go`, `RF.detail.open`, `RF.select`, events `model`, `runs`, `region`
- The placeholder in `pages/top/top.js` already ranks the top 10 for a region with Map and Details
  buttons: start from it or replace it.

Loading a national region fetches several MB; load regions on demand, not all at start.

## Constraints

- Works as a standalone site and as a claude.ai artifact (no external requests except cdnjs scripts
  and Google Fonts; data files are relative).
- Phone first: ~390 px wide, no horizontal page scroll, touch targets ≥ 44 px. On phones a card list
  usually reads better than a wide table.
- Professional, efficient, organised, simple, intuitive. Colours from `core/tokens.css` only
  (grades `--grade-2`…`--grade-6`, scores `--score-lo/mid/hi`).
- Link to sources (river guide entries, gauges) where possible; candidates are leads from terrain
  data only, so keep the safety note visible (access, land ownership, hazards, local knowledge).

## Build and check

```bash
uv sync
uv run river-finder run kaimai karangahake tutamoe   # once, ~15 min: reference-region data
uv run river-finder nz viewer                        # region files -> data/viewer_nz/
uv run river-finder web build && uv run river-finder web serve
uv run pytest -q tests/test_webapp.py && uv run ruff check src tests
```
