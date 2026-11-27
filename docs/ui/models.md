# Brief: Models page (`#models`), the ML model interface

**Beads:** `rf-aq6.4` (epic `rf-aq6`). Claim it with `bd update rf-aq6.4 --claim`.
**You own:** `src/river_finder/web/pages/models/` only. Ask for anything in `core/` through a beads
issue under `rf-aq6` (or a note on `rf-aq6.4`); the core owner is building the Map page in parallel.
**Branch:** commit to your session branch and merge to `main` when a step works (the user asked for
merges to `main` as work lands). Pull `main` first; other agents push there too.

## What the page is for

The interface to the runnability models. A user should be able to:

- see which models exist (`models/*.json` cards), what each was trained on and how it was split;
- see how each does on rivers it never saw (`models/evaluations.json`: AUC, size-matched AUC, ...);
- choose the model the rest of the app uses (`RF.setModel(id)`): the Map's predicted runs and the
  Top rivers ranking follow it;
- tune the run finder (flow band, gradient band, height-to-area measure, weights, threshold, gate,
  minimum length) with `RF.setRunParams({...})`, and see the effect;
- inspect feature weights and how well features separate runnable from unknown reaches (the legacy
  viewer's "3D separation" view and Models view did this: `src/river_finder/assets/viewer_template.html`,
  functions `drawPerf`, `drawSeparation`, `buildWeights`).

Key finding to surface honestly (from `.beads/memories.jsonl`, `model-registry`): v1 is at chance on
held-out guide rivers (AUC 0.53); flood size alone gets 0.92, so the size-matched AUC matters
(v2 0.69, baseline 0.55).

Done so far (rf-qiw, 2026-11-18):

- an "Open the ML page" link (shown when `ml.html` is in the build, which `river-finder ml page`
  writes into the data folder);
- a held-out evaluation table with test-set and test-section selectors, including the style-matched
  and style × size AUC;
- the cross-validated model recipes, from `RF.payload.registry.cv` (built by
  `models.viewer_models` -> `cv_summary`).

v3 (LightGBM, kind `gbm`, no coefficients but `importance`) is now the default model.

Done (rf-aq6.4, 2026-11-27):

- the chosen model's card: description, kind, training rivers and reaches, held-out rivers, split (share,
  strata, seed), recipe, links to `models/<id>.json` and `models/splits/`; river lists in `<details>`;
- what it uses: gain importance bars (`gbm`) or signed coefficient bars (`logistic`), plus a side-by-side
  table of the linear models' coefficients;
- a key-finding line computed from the evaluations (baseline AUC vs style × size; v1 at chance), and a
  per-test-river table for the chosen model;
- run-finder settings with `RF.setRunParams` (bands, height-to-area measure and full-credit level, weights,
  threshold, gate, big-river gradient, minimum length, gaps; applied on release / leaving a box; Reset), and
  their effect on one region next to the defaults (runs, new leads, length, guide sections touched, best five
  with "Show on map");
- separation: AUC and medians of known-run reaches against the other reaches (≥ 2 km²) for the model score
  and ten measures in one region, and a 3D scatter (Plotly gl3d 2.35.0 loaded from cdnjs only when opened;
  tap a point to open it on the map).

Done (rf-aq6.33, 2026-11-27, rebuilt from the legacy viewer):

- "Rank and colour runs by" at the top of the run finder: the model (default), Pareto layer, weighted score,
  similarity to known runs, or your feature weights (`params.rankBy` through `RF.setRunParams`; the Map's
  predicted runs and "Every reach by its score" layer and Top rivers follow it);
- "Your feature weights" (shown for that ranking): a slider (-2 to +2) for every feature of `RF.runs.features()`,
  the pooled regression's (or the chosen linear model's) coefficient beside it, "Reset the weights" and
  "Start from ...'s weights";
- "Runs: flow vs gradient": the runs of the preview region in a small SVG scatter (mean annual flood, log;
  gradient as the run finder counts it; shade = height-to-area; shape = new lead, in the guides, test river), the
  Pareto frontier ringed (outlined on the map, a pill in the lists); hover for a tip, tap or click a mark (or arrow
  keys) for the run with Show on map and Details; the frontier runs listed under it;
- `RF.go("models", {section: "models-rf"})` scrolls to the run finder (Top rivers' "Change the ranking").

Not done: archived cards (`registry.archived` on the 1 m site) are not listed (correctly not offered as map
scores); run-finder settings and the ranking are not kept between visits.

## What you can use

Read `src/river_finder/web/README.md` (the `RF` contract). Most relevant:

- `RF.models.list() / get(id) / default / evaluations / descriptions / legacy`
- `RF.state.model`, `RF.setModel(id)`, event `model`
- `RF.state.runParams`, `RF.runs.defaults()`, `RF.setRunParams(p)`, event `runs`
- `RF.data.regions()`, `RF.data.region(id)`, `RF.data.scores(regionId, modelId)`
- `RF.runs.find(region, {model, params, rankBy})`, `RF.runs.segmentScores(S, params)`
- `RF.go("map", {select: run})` and `RF.detail.open(run)` to show a run
- Charts: no chart library is loaded. Small SVG charts are fine; if you need a library, load it from
  cdnjs with an exact version (artifact CSP) and ask the core owner to add it to `LIBS` in `webapp.py`, or load it
  from the page only when its view opens (agreed for Plotly gl3d, 2026-11-27).

The placeholder in `pages/models/models.js` already lists the registry, switches models and shows the
run-finder settings: start from it or replace it.

## Constraints

- Works as a standalone site and as a claude.ai artifact (no external requests except cdnjs scripts
  and Google Fonts; data files are relative).
- Phone first: ~390 px wide, no horizontal page scroll, touch targets ≥ 44 px.
- Professional, efficient, organised, simple, intuitive. Colours from `core/tokens.css` only.
- Link to sources (model cards, data, papers) where possible.

## Build and check

```bash
uv sync
uv run river-finder run kaimai karangahake tutamoe   # once, ~15 min: reference-region data
uv run river-finder nz viewer                        # region files -> data/viewer_nz/
uv run river-finder models viewer                    # per-model score files (needs the national table; optional)
uv run river-finder web build && uv run river-finder web serve
uv run pytest -q tests/test_webapp.py && uv run ruff check src tests
```
