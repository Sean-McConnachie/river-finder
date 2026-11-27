# River Finder web app

Four pages in one app: **Map** (`#map`), **Top rivers** (`#top`), **Models** (`#models`), **Info** (`#info`).
Plain CSS and JavaScript, no bundler: `river-finder web build` (`src/river_finder/webapp.py`) inlines
everything into one page and writes it twice:

- `data/web/index.html`: standalone site (GitHub Pages, local serving). LINZ Basemaps tiles and live rain work here.
- `data/web/artifact.html`: fragment for a claude.ai artifact. That host adds the `<html>/<head>`
  skeleton and **blocks every external request** except scripts from cdnjs/jsdelivr/unpkg and Google
  Fonts, so no tile servers, no `fetch` to other hosts (so no live rain: the map says so), no iframes. Region
  data files are published next to the page and fetched with relative URLs.

## Ownership

Several agents work here at once. Edit only the folder you own; ask the core owner (open a beads
issue under epic `rf-aq6`) for any change to `core/`, `shell.html` or `webapp.py`.

| Path | Owner | Beads |
|---|---|---|
| `core/`, `shell.html`, `vendor/`, `icons/`, `../webapp.py` | core (map agent) | rf-aq6.1 |
| `pages/map/` | map agent | rf-aq6.7 to rf-aq6.14 |
| `pages/info/` | map agent | rf-aq6.6 |
| `pages/models/` | Models agent | rf-aq6.4, brief `docs/ui/models.md` |
| `pages/top/` | Top-rivers agent | rf-aq6.5, brief `docs/ui/top-rivers.md` |

Build order: `core/tokens.css`, `core/base.css`, `vendor/*.css`, `pages/<id>/*.css`; then
`core/rf.js`, `core/data.js`, `core/rain.js`, `core/runs.js`, `core/detail.js`, `core/chart.js`, `pages/<id>/*.js` (name order),
`core/start.js`. A page may have several files; they load in name order.

## Build and preview

```bash
uv run river-finder run kaimai karangahake tutamoe   # reference regions, ~15 min (once)
uv run river-finder nz viewer                        # region data files -> data/viewer_nz/ (refs only if no national scan)
uv run river-finder external all                     # waterfalls, roads, tracks (~1 min for the refs)
LINZ_LDS_KEY=... uv run river-finder web data        # known sections, roads/tracks, gauges, LINZ rivers (key: LDS, never committed)
uv run river-finder web data --only linz             # only <region>_linz.json, from the cached LINZ lines (no key, no network)
uv run river-finder web data --only aerial           # only <region>_aerial.json, LINZ aerial survey per reach (no key; ~1 min)
uv run river-finder web build                        # -> data/web/index.html + artifact.html (+ data files)
uv run river-finder web serve                        # http://localhost:8000/
```

Live rain is fetched by the page itself, every 15 min, from the `rain` branch the hourly job publishes
(`docs/live-rain.md`; nothing to build). `web build --rain-base URL` (or `$RIVER_FINDER_RAIN_BASE`) points the
site at another copy, e.g. the output of `river-finder rain publish --out DIR` served with CORS from another
port; `--rain-base ""` leaves live rain out.

`web build` takes a second, so rebuild after each edit.

LINZ rivers (`<region>_linz.json`, `linz.py`): the map draws NZ Topo50 river lines with their LINZ names,
but not unnamed headwater lines (Strahler order 1) that drain less than 2 km², where the modelled
channels also stop. That keeps the largest region near 7 MB (2.5 MB gzipped) instead of 47 MB.
Coordinates are integer steps of `q` degrees from the previous point (`50-rivers.js` decodes them).

Publishing:

- claude.ai artifact (private to the owner until shared): https://claude.ai/artifact/EhgWyCEjHYFHAY5235CHcu.
  Publish `data/web/artifact.html` with every other file in `data/web/` (not `index.html`) as
  supporting files at the same relative paths.
- Standalone site (GitHub Pages): https://sean-mcconnachie.github.io/river-finder/ (public repository,
  Settings → Pages → Deploy from a branch → `gh-pages` / root). Deploy from a machine with the data:
  `uv run river-finder models viewer` (per-model scores), `web build`, then `web pages --push`. That
  stages `data/web_pages/` (`index.html`, data files, glyphs, the logo icons from `icons/`, `.nojekyll`; not the
  artifact files or `<region>_sat.jpg`) as a single orphan commit and force-pushes it to `gh-pages`, which only ever
  holds the latest build. The satellite base map is Sentinel-2 cloudless 2025 (EOX, CC BY-NC-SA 4.0):
  live tiles on the site, `<region>_sat.jpg` (from `web data`) on the artifact. The topographic and
  the Aerial base maps need a LINZ Basemaps API key at build time (`--basemaps-key` or
  `$LINZ_BASEMAPS_KEY`; Basemaps keys are public by design and end up in the page). Without one both
  options are left out, because LINZ answers HTTP 400 for tiles outside its CDN cache. With no data the
  app still builds and every page shows its empty state. Run `uv run pytest -q tests/test_webapp.py` before pushing.

LINZ aerial photos (`<region>_aerial.json`, `aerial.py`, bead rf-aq6.29): the **Aerial** base map, next to
Satellite, draws the LINZ Basemaps `aerial` layer at every zoom (full coverage: LINZ fills gaps in its photos with its
own satellite imagery). River cards and the Details name the survey under the river ("Aerial photo: 0.2 m, 2024-2025,
Bay of Plenty West Rural Aerial Photos") on every host, from the data file. The artifact gets no photos: its host blocks tile servers, and pre-rendered
strips for the top rivers would be about 0.5 MB per km at 0.5 m pixels (hundreds of MB for the top runs of 19 regions),
so it shows the survey line and a "See it in LINZ Basemaps" link instead.

File format (`webdata.write_aerial`; ``aerial.region_record``):

```json
{"v": 1, "rank": "basemaps", "zoom": 16, "minZoom": 13, "step": 25.0, "attribution": "...", "licence": "CC BY 4.0",
 "surveys": [..., {"id": "bay-of-plenty_2024_0.2m", "title": "Bay of Plenty West 0.2m Rural Aerial Photos (2024-2025)",
                   "name": "Bay of Plenty West Rural Aerial Photos", "gsd": 0.2, "years": "2024-2025",
                   "start": "2024-01-26", "end": "2025-01-14", "cat": "rural", "km": 730.2}, ...],
 "s": [5, 2, 4, 4, -1, ...],
 "part": [[831, 80], [911, 60], ...],
 "stats": {"reaches": 6730, "covered": 6730, "km": 1532.6, "kmCovered": 1532.6}}
```

`s[i]`: index into `surveys` of the survey drawn on top (at `zoom`) over most of viewer reach `i` (same order as
`Region.segments`), -1 = no aerial photo over the reach. `part`: `[reach, percent]` for reaches only partly under any
photo (the rest are 100 % when `s >= 0`, 0 % when -1). `rank`: `"basemaps"` (the survey Basemaps draws on top) or `"gsd"`
(finest, then newest: the Basemaps layer list could not be read). `gsd` in metres, dates are NZ capture dates,
`cat` rural | urban | ancillary (storm and flood flights Basemaps draws), `km` the length of reaches it is the main
survey of (times their covered share). A file whose `s` length differs from the
region's reach count is from another build and is ignored. Coverage numbers per region: `docs/data-sources.md`.

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
| `RF.on(evt, fn)` → `unsubscribe` | events: `ready`, `page`, `region`, `model`, `runs`, `select`, `detail-close`, `rain` |
| `RF.go(pageId, params)` | open a page; e.g. `RF.go("map", {select: feature})` zooms the map to it and highlights it; `{select, aerial: true}` also switches to the Aerial base map (site with a Basemaps key) |

### Data: `RF.data`

| | |
|---|---|
| `RF.data.regions()` | `[{id, title, group, bounds: [[lat0, lon0], [lat1, lon1]], segments}]` |
| `RF.data.defaultRegion()` | last-used region id, else the first |
| `RF.data.region(id)` → `Promise<Region>` | fetch once, then cached |
| `RF.data.loaded(id)` | the region if already loaded, else null |
| `RF.data.release(id)` | forget a loaded region so its memory can be freed (not the one open on the map); `region(id)` loads it again |
| `RF.data.gauges()` → `Promise<{flow, rain, sources}>` | national river-flow gauges `[site, name, lat, lon, council, operator, funder, area, years, maf, q10, q100]` and rain gauges `[agent, name, lat, lon, owner]` |
| `RF.data.scores(regionId, modelId)` → `Promise<{pct, role}>` | per-reach percentile (0-100) for a model; `role` bits 0-1: 1 trained on, 2 test river; bit 2: on a guide section |
| `RF.data.hydro(id)` → `Promise<Hydro \| null>` | hydro-affected reaches of a region (`<id>_hydro.json`, below), fetched once; null when the region has no file; never rejects |
| `RF.data.hydroOf(H, S, members)` | `{cls, scheme, frac, note, out}`: the hydro class over most of the flagged length of a set of reaches, `frac` the flagged share of their length (0-1), `out` the class is in `H.exclude`; null when none is flagged. Top rivers and the details pill use it from `frac >= 0.5` |

Catchment area: `Region.area` is an 8-bit log code (see `areacode.py`, decoded with `Region.code`):
every land cell has an area, from 2e-5 km² (one cell) to 30 000 km².

`Region` has `id, title, description, bounds, segments` (below), `sections` (known whitewater
sections placed on reaches: `{id, river, section, grade, gnum, gmin, gmax, src, srcName, desc, putIn, takeOut,
putInDesc, takeOutDesc, coords, gauge, flow, len, time, hazards, deaths, popularity, status, links: [{t, u}], how,
dropped, gap, m: [reach]}`, see `webdata.py`), `falls`
(`[lon, lat, name, height_m | null, reach | -1, kind ("r" = rapids)]`), `runs` (reference markers),
`rec2` (NIWA REC2 main rivers), `stats`, and image fields used by the map.

Sections placed by river name keep one path down the river (`labels.one_path`):

- `dropped`: null when nothing was left out, else `{pieces, branches, km}`: how many other pieces (same-name
  stretches that do not join this one, e.g. a stream with the same name in another valley) and other branches
  (the other fork, where the branches share the name) were left out, and their length. The map shows such a
  section as "Approximate: placed by river name, 1 of N pieces kept, X km left out".
- `gap`: reach indices (often empty) of the flatter or smaller reaches between the section's pieces along that
  path. They are not part of the section (`m`), so they are not in its numbers, but they join its pieces into
  one line: the map draws them dashed, counts pieces across them and runs the profile along the whole line.

Hydro-affected reaches: `<region>_hydro.json` (`webdata.hydro_record`, from `hydro.py` and `config/hydro.toml`;
`docs/hydro.md`), optional, flagged reaches only:

```json
{"classes": ["", "diverted", "regulated", "reservoir"], "exclude": ["diverted", "reservoir"],
 "notes": {"diverted": "Hydro-diverted: most of the natural flow is taken out upstream, ...", ...},
 "schemes": [{"id": "kaimai", "name": "Kaimai hydro scheme", "operator": "Manawa Energy (formerly Trustpower)", "note": "...",
              "src": ["https://www.manawaenergy.co.nz/kaimai-power-scheme", ...]}, ...],
 "r": [[742, 1, 2, 95], [743, 1, 2, 95], ...]}
```

`r`: `[reach, class index, scheme index, share %]` sorted by reach (viewer order, as `Region.segments`); share is
the share of the natural flow behind the class (100 for reservoir reaches). `exclude`: the classes the lists leave
out of new leads (Top rivers files those runs under "Hydro-affected"). `RF.data.hydro` adds
`at: Map(reach → [class name, scheme record, share %])`.

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

On the 1 m LiDAR rebuild (`docs/nz1m-finish.md`) the packed reaches carry seven more columns
(`viewer_nz.PACK_1M`). They are decoded like the others (the header lists them), so pages that do not read them
are not affected; on the 8 m scan and the 5 m reference areas they are absent (`undefined`). Proposed for the
core owner (rf-aq6) to expose; nothing in `core/` reads them yet.

| column | meaning |
|---|---|
| `s100` | steepest 100 m, m/km (1 m long profile) |
| `stp` | bed steps of 1 m or more on the reach (1 m profile) |
| `smax` | tallest bed step, m |
| `f1m` | steps of 5 m or more (waterfall candidates) |
| `lake` | 1 = on a lake |
| `dsrc` | DEM under the reach: 0 LiDAR, 1 mixed, 2 8 m fallback, 3 lake fill |
| `ly` | end year of the LiDAR survey over the reach, null none |

`S.rivers` (names), `S.listed` (rivers named in the guides), `S.sections` (known sections).

### Live rain: `RF.rain`

Upstream rain per reach from the hourly job (`core/rain.js`; file format and reference decoder in
`src/river_finder/rainfall/live.py`, the job in `docs/live-rain.md`). The files live under `RF.config.rainBase`
(standalone site only): `index.json` and one `<region>.bin.gz` per national region, loaded on demand and
refreshed about every 15 min while the page is visible. Nothing throws into the page: a failed fetch, an index
over 12 h old, a reach count that differs from the site's (rain inputs from another national build) or a 5 m
reference area (not published) all end in an error state with a message for people.

| | |
|---|---|
| `RF.rain.enabled` | live rain is configured for this page (site build with a rain URL, a browser with `DecompressionStream`) |
| `RF.rain.region(id)` → `Promise<RainData>` | load once, then cached; rejects with `{code, message}`: `host` (artifact), `off`, `browser`, `reference`, `missing`, `mismatch`, `stale`, `network`, `format` |
| `RF.rain.state(id)` | synchronous snapshot for rendering, starts no fetch: `{status: "idle" \| "loading" \| "ok" \| "error", data, code, message}` |
| `RF.rain.at(data, reach)` | one reach: `{reach, area (km²), flags, past: {x: w}, next: {x: w}}`, `w = {mm, m3, miss, products, scale}`; `mm`/`m3` null = no value |
| `RF.rain.span(data, "past" \| "next", x)` | `{start, end}` Dates of window `x` (past windows end at `head.observed.window_end`, normally `now`, `hours_behind_now` earlier when the newest hours are not published yet; next ones start at `head.forecast.start`) |
| `RF.rain.windows` | `[1, 3, 6, 12, 24]` (hours) |
| `RF.rain.time(t, {day})`, `RF.rain.age(t)` | `"07:00 NZDT"` (weekday added when not today in NZ), `"12 min ago"` |
| `RF.rain.fmt.{mm, m3}` | `"12.3"`; `"850"`, `"3.4k"`, `"4.1M"` |
| `RF.rain.decode(bytes)` → `Promise<{head, cols}>` | the decoder (gzip or plain); `cols[k]`: typed array per reach, or the constant |

`RainData`: `{id, n, head (the file's header: generated, now, observed, forecast, products, attribution, fields),
cols, index (index.json: runs, windows, products, flags, attribution, licences), sha, generated (Date)}`. Element
`i` is the region's reach `i` (viewer order, as `Region.segments`). `m3 = mm × area_km2 × 1000`. Flags: 16 = part
of the catchment is cut off at a tile edge (the total reads low). The past windows are ECMWF short-lead hours
standing in for observed rain: model data, not gauged; say so wherever rain is shown, with the window times.
`RF.on("rain", ({region, tick}) => …)`: a region's state changed (loaded, failed, refreshed); `tick: true` comes
once a minute only so that shown ages can move on.

### Runs: `RF.runs`

| | |
|---|---|
| `RF.runs.defaults()` | `{q: [lo, hi] m³/s, g: [lo, hi] m/km, haMeasure, haHi, w: [flow, gradient, H/A], threshold, gate, flowScaled, minLen, maxGap, featureWeights}` |
| `RF.runs.find(region, {params?, model?, rankBy?})` | runs, best first. `model` = result of `RF.data.scores`; `rankBy`: `"model"` (default with a model), `"pareto"`, `"wsum"`, `"sig"`, `"comp"` |
| `RF.runs.measure(region, members, {params?, model?, rankBy?})` | the numbers `find` gives a run (`len`, `grad`, `s50`, `aTop`, `aBot`, `maf`, `ha`, `score`, `acc`, `falls`, …) for any set of reaches, e.g. a known section; `pieces` counts its separate pieces (totals for length and drop, so the gradient is length-weighted; area and flow at the largest piece), null if none |
| `RF.runs.pieces(S, members)` | connected pieces of a set of reaches, largest catchment first: `[{members, top, bottom, stem, len}]` |
| `RF.runs.path(S, members)` | smoothed `[[lon, lat], …]` for drawing |
| `RF.runs.upstream(S, reach)` | `Uint8Array` mask of every reach upstream of `reach` |

### Models: `RF.models`

`list()`, `get(id)`, `default`, `evaluations` (`models/evaluations.json`), `descriptions` (features),
`cv` (the cross-validated model zoo from `models/cv/results.json`: per recipe the mean, repeat sd and
fold sd of each AUC and recall, plus the leave-one-region-out style-matched AUC; or null),
`legacy` (pooled regression and run-finder defaults from `model.json`, or null).

Cards belong to a national build (`data`: `"8m"` or `"1m"`). The registry's `models` holds only the cards of the
build the site was made from: they have score files. From the 1 m switch on, v1-v4 are history: the registry also
has `archived` (their summaries, same fields) and `archived_evaluations` (their 8 m results), and `data` (`"1m"`).
Archived cards have no `<region>_m_<id>.txt`; a page must not offer them as map scores.

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
| `section` | known guide section: `members` (reaches), `section`, `grade`, `src`, `sec` (the full record from `Region.sections`) |
| `waterfall` | `lon`, `lat`, `height`, `seg` |
| `gauge-flow`, `gauge-rain` | `lat`, `lon`, `g` (the row from `RF.data.gauges()`) |
| `rapids` | as `waterfall`, plus `grade` |
| `road`, `track`, `foot` | `lat`, `lon` (where tapped), `osm` (way id), `geometry` |

### Charts: `RF.chart`

`RF.chart.tracks({x, xLabel, xFmt, label, tracks: [{label, unit, y, color, area, height, fmt, tipFmt, markers: [{x, label}]}]})`
draws one graph of stacked tracks sharing the x axis and one crosshair (each track its own y scale;
no dual axes). `RF.chart.placeholder(title, message)`, `RF.chart.ticks(lo, hi, n)`. River detail views
(kinds `section`, `run`, `reach`) are registered by the map page and work from any page.
Shared libraries go in `LIBS` (`webapp.py`). A large library that one view needs may instead be loaded by that
page from cdnjs (exact version) when the view opens: the Models page loads Plotly gl3d 2.35.0 (1.6 MB) only for its
3D separation view and says so if it cannot load.

### Utilities: `RF.util`

`el(tag, props, ...children)`, `esc`, `icon(name)`, `fmt.{km2, flow, grad, km, dist, coord, int}`,
`copy(text)`, `copyable(text)` (value with a Copy button), `toast(msg)`, `css(token)`, `debounce`,
`toNZTM(lat, lon)`, `decodeGray(src, w, h)`, `b64bytes(text)`. `RF.dialog({title, body, actions})`,
`RF.store.get/set/remove`, `RF.config` (`target`: `"site"` or `"artifact"`, `built`, `commit`, `basemapsKey` and
`rainBase`: site only, empty on the artifact).
