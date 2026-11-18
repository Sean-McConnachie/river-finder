# River Finder web app

Four pages in one app: **Map** (`#map`), **Top rivers** (`#top`), **Models** (`#models`), **Info** (`#info`).
Plain CSS and JavaScript, no bundler: `river-finder web build` (`src/river_finder/webapp.py`) inlines
everything into one page and writes it twice:

- `data/web/index.html`: standalone site (GitHub Pages, local serving). LINZ Basemaps tiles work here.
- `data/web/artifact.html`: fragment for a claude.ai artifact. That host adds the `<html>/<head>`
  skeleton and **blocks every external request** except scripts from cdnjs/jsdelivr/unpkg and Google
  Fonts, so no tile servers, no `fetch` to other hosts, no iframes. Region data files are published
  next to the page and fetched with relative URLs.

## Ownership

Several agents work here at once. Edit only the folder you own; ask the core owner (open a beads
issue under epic `rf-aq6`) for any change to `core/`, `shell.html` or `webapp.py`.

| Path | Owner | Beads |
|---|---|---|
| `core/`, `shell.html`, `vendor/`, `../webapp.py` | core (map agent) | rf-aq6.1 |
| `pages/map/` | map agent | rf-aq6.7 to rf-aq6.14 |
| `pages/info/` | map agent | rf-aq6.6 |
| `pages/models/` | Models agent | rf-aq6.4, brief `docs/ui/models.md` |
| `pages/top/` | Top-rivers agent | rf-aq6.5, brief `docs/ui/top-rivers.md` |

Build order: `core/tokens.css`, `core/base.css`, `vendor/*.css`, `pages/<id>/*.css`; then
`core/rf.js`, `core/data.js`, `core/runs.js`, `core/detail.js`, `pages/<id>/*.js` (name order),
`core/start.js`. A page may have several files; they load in name order.

## Build and preview

```bash
uv run river-finder run kaimai karangahake tutamoe   # reference regions, ~15 min (once)
uv run river-finder nz viewer                        # region data files -> data/viewer_nz/ (refs only if no national scan)
uv run river-finder web build                        # -> data/web/index.html + artifact.html (+ data files)
uv run river-finder web serve                        # http://localhost:8000/
```

`web build` takes a second, so rebuild after each edit. With no data the app still builds and every
page shows its empty state. Run `uv run pytest -q tests/test_webapp.py` before pushing.

## Writing a page

```js
(() => {
  "use strict";
  const RF = window.RF, { el } = RF.util;
  RF.page("top", {
    title: "Top rivers",
    mount(root) { /* once, on first visit: build DOM inside root */ },
    show(params) { /* every visit; params from RF.go("top", params) */ },
    hide() { /* leaving the page */ },
  });
})();
```

- Wrap everything in an IIFE; expose nothing globally except through `RF`.
- Prefix classes with the page id (`top-…`, `models-…`); shared components are `rf-…` (see `core/base.css`).
- Colours only from tokens (`core/tokens.css`: `--fg`, `--muted`, `--accent`, `--grade-2`…`--grade-6`,
  `--score-lo/mid/hi`, …). Light and dark both work through the tokens.
- Must work at phone width (~390 px): no horizontal page scroll, wide tables inside `.rf-tablewrap`,
  touch targets ≥ 44 px on touch screens. On phones the nav is a bottom tab bar.
- Reading pages use `<div class="rf-doc">` (centred column with safe side gutters).
- Links to sources open in a new tab: `el("a", {href, target: "_blank", rel: "noopener"}, …)`.
- `localStorage` only through `RF.store` (it may be unavailable), and only for per-browser preferences.

## The `RF` contract

Stable unless noted. Anything not listed here is internal.

### State and events

| | |
|---|---|
| `RF.state.region` | current region id (or null) |
| `RF.state.model` | current model id (from `RF.models.list()`) |
| `RF.state.runParams` | run-finder parameters in use (see `RF.runs.defaults()`) |
| `RF.state.selection` | last selected feature reference (or null) |
| `RF.setRegion(id)` → `Promise<Region>` | load and make current; emits `region` |
| `RF.setModel(id)` | emits `model` |
| `RF.setRunParams(params \| null)` | merge over defaults (null resets); emits `runs` |
| `RF.select(feature)` | emits `select` |
| `RF.on(evt, fn)` → `unsubscribe` | events: `ready`, `page`, `region`, `model`, `runs`, `select`, `detail-close` |
| `RF.go(pageId, params)` | open a page; e.g. `RF.go("map", {select: feature})` zooms the map to it and highlights it |

### Data: `RF.data`

| | |
|---|---|
| `RF.data.regions()` | `[{id, title, group, bounds: [[lat0, lon0], [lat1, lon1]], segments}]` |
| `RF.data.defaultRegion()` | last-used region id, else the first |
| `RF.data.region(id)` → `Promise<Region>` | fetch once, then cached |
| `RF.data.loaded(id)` | the region if already loaded, else null |
| `RF.data.scores(regionId, modelId)` → `Promise<{pct, role}>` | per-reach percentile (0-100) for a model; `role` bits 0-1: 1 trained on, 2 test river; bit 2: on a guide section |

`Region` has `id, title, description, bounds, segments` (below), `falls`
(`[lon, lat, name, height_m | null, reach | -1, kind ("r" = rapids)]`), `runs` (reference markers),
`rec2` (NIWA REC2 main rivers), `stats`, and image fields used by the map.

`Region.segments` (`S`) is columnar: one entry per reach (about 250 m of channel), index = reach id.

| column | meaning |
|---|---|
| `c[i]` | line `[[lon, lat], …]`, upstream to downstream |
| `d[i]`, `u[i]` | downstream / main upstream reach index (-1 none) |
| `len` | length, m |
| `g` | gradient, m/km (smoothed) |
| `s50` | steepest 50 m, m/km |
| `a` | catchment area, km² |
| `maf` | mean annual flood, m³/s (NIWA) |
| `zt`, `zb` | elevation at top / bottom, m (NZVD2016) |
| `mel` | height-to-area (Melton ratio: relief / √area) |
| `hw` | gorge depth / valley-floor width |
| `gorge`, `head`, `ksn`, `pow`, `hard` | gorge depth m, head m, steepness index, stream power kW/m, hard bed (0/1) |
| `r` | index into `S.rivers` (REC2 name), -1 unnamed |
| `reg`, `sig` | packed scores (pooled regression percentile, similarity to known runs) |
| `sta`, `gr`, `lab`, `ho`, `lho` | label status, grade, training label, held out (legacy v1 labels) |
| `z[i]` | standardised features, names in `S.features` |
| `dRoad`, `dTrack`, `dFoot` | distance to road / 4WD or private road / foot track, m (when external data exist) |
| `fallsN`, `fallsH` | mapped waterfalls on the reach, tallest height m |

`S.rivers` (names), `S.listed` (rivers named in the guides), `S.sections` (known sections).

### Runs: `RF.runs`

| | |
|---|---|
| `RF.runs.defaults()` | `{q: [lo, hi] m³/s, g: [lo, hi] m/km, haMeasure, haHi, w: [flow, gradient, H/A], threshold, gate, flowScaled, minLen, maxGap, featureWeights}` |
| `RF.runs.find(region, {params?, model?, rankBy?})` | runs, best first. `model` = result of `RF.data.scores`; `rankBy`: `"model"` (default with a model), `"pareto"`, `"wsum"`, `"sig"`, `"comp"` |
| `RF.runs.path(S, members)` | smoothed `[[lon, lat], …]` for drawing |
| `RF.runs.upstream(S, reach)` | `Uint8Array` mask of every reach upstream of `reach` |

### Models: `RF.models`

`list()`, `get(id)`, `default`, `evaluations` (`models/evaluations.json`), `descriptions` (features),
`legacy` (pooled regression and run-finder defaults from `model.json`, or null).

### Detail view: `RF.detail`

`RF.detail.open(feature, {opener})` shows the full-screen detail over the current page; closing it
(×, Esc, Back) leaves the page and map exactly as they were. `RF.detail.register(kind, render)` is
used by the map for each feature kind; `render(feature, {actions, close})` returns
`{title, subtitle, eyebrow, body}`.

### Feature references

Anything selectable is a plain object with `kind`, `region`, `id` and `name`:

| kind | extra fields |
|---|---|
| `run` | `members` (reach indices, upstream first), `river`, `len` km, `drop` m, `grad` m/km, `s50`, `aTop`, `aBot` km², `maf` m³/s, `ha`, `score` 0-100, `status` (`listed` / `heldout` / `candidate`), `layer` (Pareto), `acc`, `falls`, `fallsH` |
| `reach` | `seg` (reach index) |
| `section` | known guide section (coming with rf-aq6.10): `members`, `section`, `grade`, `source`, `links` |
| `waterfall` | `lon`, `lat`, `height`, `seg` |
| `gauge-rain`, `gauge-flow`, `road`, `track` | coming (rf-aq6.13, rf-aq6.14) |

### Utilities: `RF.util`

`el(tag, props, ...children)`, `esc`, `icon(name)`, `fmt.{km2, flow, grad, km, dist, coord, int}`,
`copy(text)`, `copyable(text)` (value with a Copy button), `toast(msg)`, `css(token)`, `debounce`,
`toNZTM(lat, lon)`, `decodeGray(src, w, h)`, `b64bytes(text)`. `RF.dialog({title, body, actions})`,
`RF.store.get/set/remove`, `RF.config` (`target`: `"site"` or `"artifact"`, `built`, `commit`).
