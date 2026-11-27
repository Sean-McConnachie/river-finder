/* Reach scores: every modelled reach coloured by its score under the ranking in use (RF.runs.rankOf: the current
   model's national percentile from the per-model score files the predicted runs use, by default; or the weighted
   score, the similarity to known runs or the user's feature weights: RF.runs.scores), red (low) through amber to
   green (high) from the score tokens. Off by default; "From" hides the lower reaches and stretches the ramp over the rest.
   Drawn from the modelled-channel features (M.netData, source src-net: one per reach, property i = reach index) with
   the scores handed to MapLibre as one literal array read by ["at", ["get", "i"], …]: a model or ranking switch only
   repaints, and no feature is built or re-sent per reach. Below zoom 8 a source of the larger rivers (from 30 km², as
   the rain layer) stands in, because the tiler drops reaches shorter than its tolerance at region scale.
   Owner: map agent. */
(() => {
  "use strict";
  const RF = window.RF, M = RF.mapkit, { el, fmt } = RF.util;
  const EMPTY = { type: "FeatureCollection", features: [] };
  const CUTS = [0, 50, 70, 90];
  const LO_MAXZOOM = 8, W_LO = 0.422, W_MID = 0.258;   // w = log10(km²) / 3.5: 30 km² below zoom 8, 8 km² below zoom 9
  const LINES = ["score-line-lo", "score-line"];
  M.prefs.score = Object.assign({ min: 0 }, M.prefs.score || {});
  if (!CUTS.includes(M.prefs.score.min)) M.prefs.score.min = 0;
  const cut = () => M.prefs.score.min;

  // cur: {R, mid, p (run params), rank, fallback, src (the score array it came from), q: per-reach score 0-100
  // (integers), shown: reaches at or above the cut}
  let cur = null;
  let token = 0, want = null;  // the (region, model, params) being loaded
  let loInto = null;           // the M.netData written into the zoomed-out source
  let painted = { q: null, sig: "" };

  const fresh = () => !!cur && cur.R === M.R && cur.mid === RF.state.model && cur.p === RF.state.runParams;
  const short = (id) => (RF.models.get(id)?.title || id || "").split(":")[0];
  const rank = () => (fresh() ? cur.rank : RF.runs.rankOf(RF.state.runParams, !!RF.state.model));
  /** What the colours show: "Model v4" / "Pooled regression" / "Weighted score" / ... */
  function source() {
    const r = rank();
    if (r !== "model") return { pareto: "Weighted score", wsum: "Weighted score", sig: "Similarity to known runs", comp: "Your feature weights" }[r];
    return !cur || cur.fallback || !cur.mid ? "Pooled regression" : `Model ${short(cur.mid)}`;
  }
  const KIND = { model: "national percentile", pareto: "0 to 100", wsum: "0 to 100", sig: "percentile", comp: "percentile" };
  const ABOUT = {
    model: "National percentile among reaches of 2 km² or more: 100 is most like the known runs.",
    pareto: "The run finder's weighted mean of the three part scores (flood flow, gradient, height-to-area), × 100. Pareto layers rank whole runs, not reaches.",
    wsum: "The run finder's weighted mean of the three part scores (flood flow, gradient, height-to-area), × 100.",
    sig: "How close each reach's terrain is to the average reach of the known runs, as a percentile.",
    comp: "Your feature weights (Models page) on the standardised features, as a percentile among reaches of 2 km² or more.",
  };
  const colors = () => ["lo", "mid", "hi"].map((k) => M.colorOf("score", k));

  // ---------- layer tree, legend ----------
  M.defineLayer({ id: "score", parent: "rivers", after: "pred", label: "Every reach by its score", defaultOn: false,
    note: () => (M.isVisible("score") && !fresh() ? "Loading the scores…" : `${source()}: ${KIND[rank()]}, red (low) to green (high)`),
    colors: [{ key: "lo", token: "--score-lo", label: "low score" }, { key: "mid", token: "--score-mid", label: "middle" }, { key: "hi", token: "--score-hi", label: "high score" }],
    count: () => (fresh() ? cur.shown : null), legendBlock: legend, mini: true, controls, apply: sync });

  function controls() {
    return el("div", { class: "map-score-ctl" }, el("span", { class: "map-score-k", id: "map-score-from", text: "From" }),
      el("div", { class: "map-seg", role: "radiogroup", "aria-labelledby": "map-score-from" },
        ...CUTS.map((v) => el("button", { type: "button", role: "radio", "aria-checked": String(v === cut()), "aria-label": v ? `Score ${v} and up` : "All reaches",
          dataset: { v: String(v) }, onclick: () => setCut(v) }, v ? `${v}+` : "All"))));
  }
  function setCut(v) {
    M.prefs.score.min = v; M.savePrefs();
    document.querySelectorAll(".map-score-ctl .map-seg button").forEach((b) => b.setAttribute("aria-checked", String(+b.dataset.v === v)));
    if (fresh()) cur.shown = countFrom(cur.q, v);
    sync(); M.renderTree(); M.renderLegend();
  }

  /** The colour ramp with its stops: the cut, the middle of the rest, 100. */
  function legend() {
    const c = colors(), lo = cut(), title = el("div", { class: "map-legend-title",
      text: `Every reach: ${source().replace(/^(Weighted|Similarity|Your)/, (w) => w.toLowerCase())}${rank() === "model" ? " score" : ""}${lo ? `, ${lo} and up` : ""}` });
    return el("div", { class: "map-legend-block map-score-legend" }, title,
      el("span", { class: "map-score-bar", role: "img", "aria-label": `Colour ramp from ${lo} (red) through ${(lo + 100) / 2} (amber) to 100 (green)`,
        style: `--lo:${c[0]};--mid:${c[1]};--hi:${c[2]}` }),
      el("span", { class: "map-score-ticks num", "aria-hidden": "true" }, el("span", { text: String(lo) }), el("span", { text: String((lo + 100) / 2) }), el("span", { text: "100" })),
      el("p", { class: "map-rain-note map-score-note", text: ABOUT[rank()] }));
  }

  // ---------- on the map ----------
  // "zoom" must be the input of a top-level interpolate, so the data terms go into each stop (w: 0..1 by catchment area).
  const width = ["interpolate", ["linear"], ["zoom"], 7, ["+", 0.6, ["*", 1, ["get", "w"]]], 10, ["+", 0.8, ["*", 1.4, ["get", "w"]]],
    13, ["+", 1.2, ["*", 2.2, ["get", "w"]]], 16, ["+", 2, ["*", 3, ["get", "w"]]]];
  M.layerInits.push(() => {
    const map = M.map;
    map.addSource("src-score-lo", { type: "geojson", data: EMPTY, tolerance: 0.15, maxzoom: LO_MAXZOOM });
    // over the LINZ lines and modelled channels, under known rivers and predicted runs (they keep their own colours)
    const def = (id, lo) => Object.assign({ id, type: "line", source: lo ? "src-score-lo" : "src-net",
      layout: { visibility: "none", "line-cap": "round", "line-join": "round", "line-sort-key": ["get", "w"] },
      paint: { "line-color": "rgba(0,0,0,0)", "line-width": width, "line-opacity": 0.95 } },
    lo ? { maxzoom: LO_MAXZOOM } : { minzoom: LO_MAXZOOM, filter: ["any", [">=", ["zoom"], 9], [">=", ["get", "w"], W_MID]] });
    M.addLayer(def("score-line-lo", true), "network");
    M.addLayer(def("score-line", false), "network");
    // a tap on a coloured reach selects it, before the LINZ line under it (reaches below the cut are transparent: not a target)
    M.clickable({ layers: LINES, priority: 46, feature: (f, e, hits) => {
      const h = fresh() && (hits || [f]).find((x) => (cur.q[x.properties.i] ?? -1) >= cut());
      return h ? M.reachFeature(h.properties.i) : null;
    } });
  });

  const countFrom = (q, lo) => { let n = 0; for (let i = 0; i < q.length; i++) if (q[i] >= lo) n++; return n; };

  /** Each reach's score for the region on the map, the current model and the run-finder settings (with them the
   *  ranking): fetched once per region and model, recomputed when the settings change. */
  async function load() {
    const R = M.R, mid = RF.state.model, p = RF.state.runParams, key = { R, mid, p };
    if (!R?.segments || (want && want.R === R && want.mid === mid && want.p === p)) return;
    want = key;
    const my = ++token;
    let sc = null;
    try { sc = mid ? await RF.data.scores(R.id, mid) : null; } catch (e) { sc = null; }
    if (my !== token || M.R !== R || mid !== RF.state.model || p !== RF.state.runParams) return;
    want = null;
    const model = sc?.pct?.length === R.segments.g.length ? sc : null, rk = RF.runs.rankOf(p, !!mid);
    const src = RF.runs.scores(R, { params: p, model, rankBy: rk });
    // the same scores as before (a settings change that leaves the model's percentiles as they were): keep the array,
    // so nothing is repainted
    const q = cur && cur.R === R && cur.src === src ? cur.q : Array.from(src, (v) => Math.max(0, Math.min(100, Math.round(v))));
    cur = { R, mid, p, rank: rk, fallback: !model || !!model.fallback, src, q, shown: countFrom(q, cut()) };
    sync(); M.renderTree(); M.renderLegend();
  }

  /** The zoomed-out source: the larger modelled channels (the same features, so the same reach index i). */
  function writeLo() {
    const fc = M.netData; loInto = fc;
    M.map.getSource("src-score-lo")?.setData(fc ? { type: "FeatureCollection", features: fc.features.filter((f) => f.properties.w >= W_LO) } : EMPTY);
  }

  /** Colour each reach by its score: one expression, the scores as a literal array by reach index. Repaints only
   *  when the scores, the cut or the colours changed (applyLayers calls this for every layer toggle). */
  function paint() {
    const c = colors(), lo = cut(), sig = `${lo}|${c.join()}`;
    if (painted.q === cur.q && painted.sig === sig) return;
    const ramp = ["interpolate", ["linear"], ["var", "v"], lo, c[0], (lo + 100) / 2, c[1], 100, c[2]];
    const color = ["let", "v", ["at", ["get", "i"], ["literal", cur.q]], lo > 0 ? ["case", ["<", ["var", "v"], lo], "rgba(0,0,0,0)", ramp] : ramp];
    LINES.forEach((id) => M.map.setPaintProperty(id, "line-color", color));
    painted = { q: cur.q, sig };
  }

  /** Show the layer when it is on and the scores of this region, model and settings are in (asks for them otherwise). */
  function sync() {
    if (!M.ready || !M.map.getLayer("score-line")) return;
    const on = M.isVisible("score");
    if (on && M.R && !fresh()) load();
    const show = on && fresh() && !!M.netData;
    if (show && loInto !== M.netData) writeLo();
    LINES.forEach((id) => M.vis(id, show));
    if (show) paint();
    M.updateNote("score");
  }

  M.onRegion(() => { cur = null; want = null; loInto = null; painted = { q: null, sig: "" }; M.map?.getSource("src-score-lo")?.setData(EMPTY); });
  const follow = () => { if (M.ready && M.R && M.isVisible("score")) { sync(); M.renderLegend(); } };
  RF.on("model", follow);
  RF.on("runs", follow);

  // a reach card says its score while the layer is on
  const reachCard = M.card.reach;
  M.card.reach = (f) => {
    const d = reachCard(f);
    if (M.isVisible("score") && fresh() && f.region === M.R.id && cur.q[f.seg] != null) {
      d.lines.push(el("span", { class: "num", text: `${source()}${rank() === "model" ? " score" : ""} ${fmt.int(cur.q[f.seg])} (${KIND[rank()]})` }));
    }
    return d;
  };

  M.score = { sync, values: () => (fresh() ? cur.q : null) };
})();
