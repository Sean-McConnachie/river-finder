/* Models page (#models): the ML model interface.
   Owned by the Models agent: see docs/ui/models.md and beads issue rf-aq6.4 (rf-qiw: ML page link and
   style-matched / style x size AUC).
   Sections: the models in this build (choosing one calls RF.setModel, which the map and Top rivers follow), the
   chosen model's card and what it uses (coefficients or gain importance), how the models do on held-out guide
   rivers and in cross-validation, the run-finder settings and the ranking of runs (RF.setRunParams: model, Pareto
   layer, weighted score, similarity to known runs or the user's feature weights; their effect on one region and the
   runs there by flood flow and gradient, the Pareto frontier ringed)
   and how well single measures separate known runs from the other reaches (AUC per measure and a 3D scatter;
   Plotly's 3D bundle is loaded from cdnjs only when that view is opened).
   Only edit files under src/river_finder/web/pages/models/. */
(() => {
  "use strict";
  const RF = window.RF, { el, esc, fmt } = RF.util;
  const REPO = "https://github.com/Sean-McConnachie/river-finder";
  const PLOTLY = "https://cdnjs.cloudflare.com/ajax/libs/plotly.js/2.35.0/plotly-gl3d.min.js";
  let root = null, tuner = null, sep = null;
  let mlPage = null;   // true / false once checked: is ml.html published next to this page?
  const view = { split: null, coords: "any", region: null, regionPicked: false };

  const F2 = (v) => (v == null || !isFinite(v) ? "–" : (+v).toFixed(2));
  const F3 = (v) => (v == null || !isFinite(v) ? "–" : (+v).toFixed(3));
  const PCT = (v) => (v == null || !isFinite(v) ? "–" : `${Math.round(100 * v)}%`);
  const N0 = (v) => (v == null || !isFinite(v) ? "–" : String(Math.round(v)));
  const KIND = { logistic: "Logistic", gbm: "LightGBM", baseline: "Baseline" };
  const KIND_LONG = { logistic: "Logistic regression on standardised terrain features", gbm: "Gradient-boosted trees (LightGBM)",
    baseline: "A fixed rule, not trained" };
  // the CV summary rides in the build payload next to the cards (models.viewer_models -> "cv")
  const CV = () => (RF.payload.registry || {}).cv || null;
  const desc = (f) => (RF.models.descriptions || {})[f] || f;
  const ext = (href, text) => el("a", { href, target: "_blank", rel: "noopener" }, text);
  const river = (key) => { const [r, g] = String(key).split(" | "); return g ? `${r} (${g})` : r; };
  const titleOf = (id) => RF.models.get(id)?.title || id;
  const shortOf = (id) => titleOf(id).split(":")[0];

  function checkMlPage() {
    if (mlPage !== null || !window.fetch) return;
    fetch("ml.html", { method: "HEAD" }).then((r) => { mlPage = r.ok; }).catch(() => { mlPage = false; })
      .finally(() => { if (root) render(); });
  }

  function mlLink(text) {
    return mlPage ? el("a", { href: "ml.html", class: "models-ml" }, text) : el("span", { class: "models-ml" }, text);
  }

  function th(label, num, title) { return el("th", { class: num ? "n" : null, title: title || null, scope: "col", text: label }); }

  function table(head, rows, cls) {
    return el("div", { class: "rf-tablewrap" }, el("table", { class: `rf-table${cls ? ` ${cls}` : ""}` }, el("thead", {}, el("tr", {}, ...head)), el("tbody", {}, ...rows)));
  }

  function panel(id, title, ...kids) {
    return el("section", { class: "rf-panel rf-stack", id, "aria-labelledby": `${id}-h` }, el("h2", { id: `${id}-h`, text: title }), ...kids);
  }

  const kv = (pairs) => el("dl", { class: "rf-kv models-kv" }, ...pairs.filter(Boolean).flatMap(([k, v]) => [el("dt", { text: k }), el("dd", {}, v)]));

  function evalRows() {
    const ev = RF.models.evaluations;
    return ev ? ev.evaluations || [] : [];
  }
  function splits() {
    const ev = RF.models.evaluations;
    return ev ? ev.splits || [] : [];
  }
  function evalOf(mid) {
    return evalRows().find((e) => e.model === mid && e.split === view.split && e.coords === view.coords) || null;
  }
  function pickSplit() {
    const sp = splits();
    if (sp.length && (!view.split || !sp.some((s) => s.name === view.split))) view.split = (sp.find((s) => s.name !== "reference") || sp[0]).name;
  }

  // ---------- the models in this build: choose one ----------
  function modelsTable() {
    const models = RF.models.list();
    if (!models.length) return el("div", { class: "rf-empty" }, el("strong", { text: "No model registry in this build." }),
      el("span", { text: "Run `river-finder models viewer` (see docs/PLAN.md) to include models/*.json." }));
    const rows = models.slice().reverse().map((m) => {
      const cur = m.id === RF.state.model, e = evalOf(m.id) || {};
      return el("tr", { class: cur ? "models-cur" : null },
        el("td", {}, el("label", { class: "rf-row models-pick" },
          el("input", { type: "radio", name: "models-current", value: m.id, checked: cur, onchange: () => RF.setModel(m.id) }),
          el("span", {}, el("strong", { text: m.title || m.id }), el("span", { class: "rf-muted models-id", text: ` ${m.id}` })))),
        el("td", { text: KIND[m.kind] || m.kind || "" }),
        el("td", { class: "n", text: (m.train_rivers || []).length }),
        el("td", { class: "n", text: (m.test_rivers || []).length }),
        el("td", { class: "n", text: F2(e.auc_style_size_matched) }),
        el("td", { class: "n", text: (m.created || "").slice(0, 10) }));
    });
    return table([th("Model"), th("Kind"), th("Train rivers", true), th("Test rivers", true),
      th("Style × size AUC", true, "On the held-out test rivers of the test set chosen below"), th("Created", true)], rows);
  }

  // ---------- the chosen model: its card and what it uses ----------
  /** Horizontal bars: rows = [{label, title, value text, frac (0..1, or -1..1 when signed)}]. */
  function bars(rows, signed) {
    return el("ul", { class: `models-bars${signed ? " models-bars--signed" : ""}` }, ...rows.map((r) => {
      const w = Math.min(Math.abs(r.frac), 1) * (signed ? 50 : 100);
      const left = signed ? (r.frac < 0 ? 50 - w : 50) : 0;
      return el("li", { class: "models-bar", title: r.title || null },
        el("span", { class: "models-bar-label", text: r.label }),
        el("span", { class: "models-bar-track", "aria-hidden": "true" },
          el("span", { class: `models-bar-fill${r.frac < 0 ? " models-bar-fill--neg" : ""}`, style: `left:${left.toFixed(1)}%;width:${w.toFixed(1)}%` })),
        el("span", { class: "num models-bar-val", text: r.text }));
    }));
  }

  function compareLinear(cur) {
    const lin = RF.models.list().filter((m) => m.kind === "logistic" && Object.keys(m.coef || {}).length).reverse();
    if (lin.length < 2) return null;
    const feats = [...new Set(lin.flatMap((m) => Object.keys(m.coef)))];
    const rows = feats.map((f) => el("tr", {}, el("td", { text: desc(f) }),
      ...lin.map((m) => el("td", { class: `n${m.id === cur ? " models-curcell" : ""}`, text: F2(m.coef[f]) }))));
    return el("details", { class: "models-more" }, el("summary", { text: "Compare the coefficients of the linear models" }),
      table([th("Feature (standardised)"), ...lin.map((m) => th(m.id, true, m.title))], rows));
  }

  function weightsBlock(m) {
    if (m.kind === "baseline") return el("p", { class: "rf-note", text: "No weights: it ranks every reach by its mean annual flood alone. Any model should beat it; the gap is what the terrain adds." });
    const imp = Object.entries(m.importance || {}).filter(([, v]) => isFinite(v) && v > 0).sort((a, b) => b[1] - a[1]);
    if (imp.length) {
      const top = imp[0][1];
      return el("div", { class: "rf-stack" }, el("h3", { text: "What it uses: gain importance" }),
        bars(imp.map(([f, v]) => ({ label: desc(f), title: f, text: `${(100 * v).toFixed(1)}%`, frac: v / top }))),
        el("p", { class: "rf-note", text: `Share of the trees' split gain per input: how much the model leans on it, not in which direction. Gradient-boosted trees have no single weight per input.${imp[0][0] === "maf" ? " Flood size leads, which is why the size-matched results below matter." : ""}` }));
    }
    const coef = Object.entries(m.coef || {}).filter(([, v]) => isFinite(v)).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]));
    if (!coef.length) return null;
    const mx = Math.max(...coef.map(([, v]) => Math.abs(v)), 1e-9);
    return el("div", { class: "rf-stack" }, el("h3", { text: "What it uses: coefficients" }),
      bars(coef.map(([f, v]) => ({ label: desc(f), title: f, text: v.toFixed(2), frac: v / mx })), true),
      el("p", { class: "rf-note", text: "Weights on standardised features (one standard deviation each): blue raises the score, amber lowers it. The features move together (gradient, steepness and stream power), so read the sign of a single weight with care and compare models side by side." }),
      compareLinear(m.id));
  }

  function cardPanel() {
    const m = RF.models.get(RF.state.model);
    if (!m) return null;
    const sp = splits().find((s) => s.name === m.split) || null, tr = m.train_rivers || [], te = m.test_rivers || [];
    const splitText = !sp ? m.split || "–" : sp.name === "reference" ? `${sp.name}: ${sp.strata}`
      : `${sp.name}: ${sp.test_frac ? `${Math.round(100 * sp.test_frac)}% of the guide rivers held out` : "rivers held out"}${sp.strata ? `, stratified by ${sp.strata}` : ""}${sp.seed != null ? `, seed ${sp.seed}` : ""}`;
    const list = (label, items) => (items.length ? el("details", { class: "models-more" }, el("summary", { text: `${label} (${items.length})` }),
      el("p", { class: "rf-note models-rivers", text: items.map(river).join(" · ") })) : null);
    const files = [ext(`${REPO}/blob/main/models/${encodeURIComponent(m.id)}.json`, `models/${m.id}.json`)];
    if (sp) files.push(" · ", ext(`${REPO}/tree/main/models/splits`, "models/splits/"));
    return el("section", { class: "rf-panel rf-stack", id: "models-card", "aria-labelledby": "models-card-h" },
      el("div", { class: "rf-stack models-cardhead" }, el("span", { class: "rf-eyebrow", text: "Model in use on the map and in Top rivers" }), el("h2", { id: "models-card-h", text: m.title || m.id })),
      m.description ? el("p", { class: "models-desc", text: m.description }) : null,
      kv([
        ["Kind", KIND_LONG[m.kind] || m.kind || "–"],
        ["Trained on", m.kind === "baseline" ? "Nothing" : `${tr.length} rivers${m.positives ? `: ${fmt.int(m.positives)} reaches on known runs among ${fmt.int(m.training_reaches)}` : ""}`],
        ["Held out", te.length ? `${te.length} rivers it never saw` : "None"],
        ["Split", splitText],
        m.recipe ? ["Recipe", el("span", { class: "num", text: m.recipe })] : null,
        m.checkpoint_check ? ["Check", `Reproduces the first national run within ${m.checkpoint_check.max_abs_pct_diff} percentile points`] : null,
        ["Created", el("span", { class: "num", text: (m.created || "–").slice(0, 10) })],
        ["Files", el("span", {}, ...files)],
      ]),
      tr.length || te.length ? el("div", { class: "rf-stack models-lists" }, list("Training rivers", tr), list("Test rivers", te)) : null,
      weightsBlock(m));
  }

  // ---------- held-out guide rivers and cross-validation ----------
  function keyFinding() {
    const base = evalOf("baseline-flow"), cur = evalOf(RF.state.model), v1 = evalOf("v1");
    if (!base || base.auc == null) return null;
    const curTxt = cur && cur.model !== "baseline-flow" && cur.auc_style_size_matched != null ? `, ${shortOf(cur.model)} ${F2(cur.auc_style_size_matched)}` : "";
    const v1Txt = v1 && v1.auc != null && v1.auc < 0.6 ? ` v1, trained on six reference rivers, is at chance here (AUC ${F2(v1.auc)}).` : "";
    return el("p", { class: "models-key" }, el("strong", { text: "Key finding: " }),
      `flood size alone (the baseline) already scores AUC ${F2(base.auc)}, because guide runs are on bigger rivers than most creeks. So compare models on the style × size column, where size and steepness earn nothing: baseline ${F2(base.auc_style_size_matched)}${curTxt}.${v1Txt}`);
  }

  function riverTable() {
    const e = evalOf(RF.state.model);
    if (!e?.rivers?.length) return null;
    const rows = e.rivers.slice().sort((a, b) => (b.median_pct_style_size_matched ?? -1) - (a.median_pct_style_size_matched ?? -1)).map((r) => el("tr", {},
      el("td", { text: r.river }), el("td", { text: r.region || "" }), el("td", { text: r.grade || "" }), el("td", { class: "n", text: r.reaches }),
      el("td", { class: "n", text: N0(r.median_pct) }), el("td", { class: "n", text: N0(r.median_pct_style_size_matched) }),
      el("td", { class: "n", text: PCT(r.share_top10) })));
    return el("details", { class: "models-more" }, el("summary", { text: `Each test river for ${shortOf(e.model)} (${e.rivers.length})` }),
      table([th("River"), th("Region"), th("Grade"), th("Reaches", true), th("Median pct", true), th("Style × size pct", true), th("In top 10%", true)], rows),
      el("p", { class: "rf-note", text: "Percentiles among the background reaches (100 = top); style × size ranks each reach only against reaches of the same paddling style and flood size." }));
  }

  function evalSection() {
    const sp = splits();
    if (!evalRows().length || !sp.length) return el("p", { class: "rf-note", text: "No evaluations in this build (run `river-finder models evaluate`)." });
    pickSplit();
    const pick = (id, label, opts, value, onchange) => el("label", { class: "rf-stack models-ctl", for: id },
      el("span", { class: "rf-eyebrow", text: label }),
      el("select", { id, class: "rf-select", onchange }, ...opts.map(([v, t]) => el("option", { value: v, selected: v === value, text: t }))));
    const controls = el("div", { class: "rf-row models-ctls" },
      pick("models-split", "Test set", sp.map((s) => [s.name, s.name === "reference" ? "Reference: the Mangakarengorengo only" : `Guide research: ${Array.isArray(s.test) ? s.test.length : s.test} held-out rivers (${s.name})`]),
        view.split, (e) => { view.split = e.target.value; render(); }),
      pick("models-coords", "Test sections", [["any", "All coordinates, inferred included"], ["sourced", "Published coordinates only"]],
        view.coords, (e) => { view.coords = e.target.value; render(); }));
    const rows = RF.models.list().slice().reverse().map((m) => {
      const e = evalOf(m.id) || {};
      const cut = e.excluded_trained && e.excluded_trained.length ? ` (−${e.excluded_trained.length})` : "";
      return el("tr", { class: m.id === RF.state.model ? "models-cur" : null },
        el("td", {}, el("strong", { text: m.title || m.id })),
        el("td", { class: "n", text: e.test_rivers != null ? `${e.test_rivers}${cut}` : "–" }),
        el("td", { class: "n", text: F2(e.auc) }), el("td", { class: "n", text: F2(e.auc_size_matched) }),
        el("td", { class: "n", text: F2(e.auc_style_matched) }), el("td", { class: "n", text: F2(e.auc_style_size_matched) }),
        el("td", { class: "n", text: PCT(e.recall_top10) }), el("td", { class: "n", text: e.median_pct != null ? Math.round(e.median_pct) : "–" }));
    });
    const s = sp.find((x) => x.name === view.split) || {};
    return el("div", { class: "rf-stack" }, controls, keyFinding(),
      table([th("Model"), th("Test rivers", true), th("AUC", true), th("Size-matched", true), th("Style-matched", true),
        th("Style × size", true), th("Top 10%", true, "Share of test reaches in the model's top 10%"), th("Median pct", true)], rows),
      el("p", { class: "rf-note", html: `Each model ranks every reach of 2&nbsp;km² or more that is not on a known run (the background, which still holds unknown runs, so these are lower bounds). Test reaches should rank high. <b>AUC</b>: chance a test reach outranks a random background reach. <b>Size-matched</b>: only against background reaches of similar flood size. <b>Style-matched</b>: only against the same paddling style (Montgomery &amp; Buffington reach type from slope and confinement), so preferring steep creeks earns nothing. <b>Style × size</b>: same style and same flood quintile, so neither "steep" nor "big" earns anything. (−n): test rivers left out because that model trained on them.${s.strata ? ` Split: ${esc(s.strata)}, seed ${esc(s.seed ?? "fixed")}.` : ""}` }),
      el("p", { class: "rf-note", text: "This split holds out whole rivers, not whole catchments, so a test river can share its catchment with training rivers. That flatters flexible models; the cross-validation below holds out whole catchments and is the more honest comparison." }),
      riverTable());
  }

  function cvSection() {
    const cv = CV();
    if (!cv) return el("p", { class: "rf-note", text: "No cross-validation results in this build (run `river-finder ml cv`)." });
    const ms = cv.models.slice().sort((a, b) => (b.cv.auc_style_size?.mean ?? 0) - (a.cv.auc_style_size?.mean ?? 0));
    const ms_ = (m, k) => { const v = m.cv[k]; return v ? `${F3(v.mean)}` : "–"; };
    const tip = (m, k) => { const v = m.cv[k]; return v ? `± ${F3(v.sd)} over repeats, ± ${F3(v.fold_sd)} over single folds` : null; };
    const rows = ms.map((m) => el("tr", {},
      el("td", {}, el("strong", { text: m.title }), el("span", { class: "rf-muted models-id", text: ` ${m.id}` })),
      el("td", { text: KIND[m.kind] || m.kind }), el("td", { class: "n", text: m.inputs }),
      el("td", { class: "n", title: tip(m, "auc_style_size"), text: ms_(m, "auc_style_size") }),
      el("td", { class: "n", title: tip(m, "auc_style"), text: ms_(m, "auc_style") }),
      el("td", { class: "n", title: tip(m, "auc_size"), text: ms_(m, "auc_size") }),
      el("td", { class: "n", title: tip(m, "auc"), text: ms_(m, "auc") }),
      el("td", { class: "n", text: m.cv.recall_top10 ? PCT(m.cv.recall_top10.mean) : "–" }),
      el("td", { class: "n", text: F3(m.loro_auc_style) })));
    return el("div", { class: "rf-stack" },
      table([th("Model"), th("Kind"), th("Inputs", true), th("Style × size", true), th("Style-matched", true), th("Size-matched", true),
        th("AUC", true), th("Top 10%", true), th("Held-out region", true, "Style-matched AUC with a whole guide region held out (mean over regions)")], rows),
      el("p", { class: "rf-note", html: `Out-of-fold means over ${esc(cv.repeats)} repeats of a ${esc(cv.folds)}-fold split that holds out whole sea-outlet catchments; every reach of ${esc(cv.domain_km2)}&nbsp;km² or more gets a score from a model that never saw its catchment. Hover a number for its spread. These are the model recipes, not the frozen cards above: <span class="num">v3</span> is the <span class="num">gbm_terrain</span> recipe trained once on the v2 training rivers. Style matching alone does not control river size (flood size alone scores about 0.90 style-matched), so style × size is the column that separates the models.` }),
      el("p", { class: "rf-note" }, mlPage ? "Every number, chart, data source and transformation is on the " : "Every number, chart and transformation is documented on the ML page (", mlLink(mlPage ? "ML page" : "build it with `river-finder ml page` before `web build`"), mlPage ? ", with the reach-type spec (PDF)." : ")."));
  }

  // ---------- region shared by the run-finder preview and the separation view ----------
  function regionSelect(id, onchange) {
    const regs = RF.data.regions(), groups = [...new Set(regs.map((r) => r.group))];
    const opt = (r) => el("option", { value: r.id, selected: r.id === view.region, text: r.title });
    return el("select", { id, class: "rf-select models-region", onchange: (e) => onchange(e.target.value) },
      ...(groups.length > 1 ? groups.map((g) => el("optgroup", { label: g }, ...regs.filter((r) => r.group === g).map(opt))) : regs.map(opt)));
  }
  function setRegion(id, picked) {
    if (!id || id === view.region) return;
    const old = view.region;
    view.region = id;
    if (picked) view.regionPicked = true;
    tuner?.region(); sep?.region();
    // a region only these previews opened is not kept: the map and Details load it again on demand
    if (old && old !== RF.state.region) RF.data.release(old);
  }
  const yieldMain = () => new Promise((r) => setTimeout(r, 0));

  // ---------- run-finder settings: RF.setRunParams, with their effect on one region ----------
  const clone = (p) => JSON.parse(JSON.stringify(p));
  const HA_LABEL = { melton: "Height-to-area: relief ÷ √area (Melton ratio)", height_to_width: "Height-to-width: gorge depth ÷ valley-floor width" };
  const haDefaults = () => RF.models.legacy?.ha_defaults || { melton: [0.03, 0.1], height_to_width: [0.1, 1.5] };

  /** What differs from the defaults, in words. */
  function changes(p) {
    const d = RF.runs.defaults(), out = [], ne = (a, b) => JSON.stringify(a) !== JSON.stringify(b);
    if (ne(p.q, d.q)) out.push("flood band");
    if (ne(p.g, d.g)) out.push("gradient band");
    if (p.haMeasure !== d.haMeasure) out.push("height-to-area measure");
    if (p.haHi?.[p.haMeasure] !== d.haHi?.[p.haMeasure]) out.push("height-to-area full credit");
    if (ne(p.w, d.w)) out.push("weights");
    if (p.threshold !== d.threshold) out.push("threshold");
    if (p.gate !== d.gate) out.push("gate");
    if (p.flowScaled !== d.flowScaled) out.push("big-river gradient");
    if (p.minLen !== d.minLen) out.push("minimum length");
    if (p.maxGap !== d.maxGap) out.push("gaps");
    if (rankOf(p) !== rankOf(d)) out.push("ranking");
    if (ne(p.featureWeights, d.featureWeights)) out.push("feature weights");
    return out;
  }

  // ---------- ranking: how runs are ranked and coloured (params.rankBy, RF.runs.rankings) ----------
  const rankOf = (p) => RF.runs.rankOf(p, !!RF.state.model);
  /** "Model v4" for the model option (the pooled regression when the build has no models), else the ranking's label. */
  const rankName = (id) => (id === "model" ? (RF.state.model ? `Model ${shortOf(RF.state.model)}` : "Pooled regression")
    : RF.runs.rankings.find((r) => r.id === id)?.label || id);
  /** The same, inside a sentence ("ranked by similarity to known runs"; Pareto keeps its capital). */
  const rankWords = (id) => rankName(id).replace(/^(Weighted|Similarity|Your)/, (w) => w.toLowerCase());
  const SCORE_NAME = { model: "score", pareto: "weighted score", wsum: "weighted score", sig: "similarity", comp: "your weights" };
  const STATUS_PILL = { candidate: ["rf-pill--ok", "New lead"], heldout: ["rf-pill--warn", "Test river"], listed: ["", "In guides"] };
  const FRONT_TIP = "No other run in this region has more flood flow, a steeper gradient and a higher height-to-area at once";

  /** One run in a list: name, status, Pareto frontier, its numbers (text), Show on map. */
  function runItem(r, text) {
    const [cls, label] = STATUS_PILL[r.status] || STATUS_PILL.candidate;
    return el("li", { class: "models-run" },
      el("div", { class: "models-run-main" }, el("strong", { text: r.name }), el("span", { class: `rf-pill ${cls}`, text: label }),
        r.layer === 1 ? el("span", { class: "rf-pill rf-pill--accent", title: FRONT_TIP, text: "Pareto frontier" }) : null,
        el("span", { class: "num rf-muted", text })),
      el("button", { type: "button", class: "rf-btn", "aria-label": `Show on map: ${r.name}`, onclick: () => RF.go("map", { select: r }) }, "Show on map"));
  }

  function summarize(R, runs) {
    const by = { candidate: 0, listed: 0, heldout: 0 }, on = new Set();
    let km = 0;
    runs.forEach((r) => { by[r.status] = (by[r.status] || 0) + 1; km += r.len; r.members.forEach((m) => on.add(m)); });
    const secs = R.sections || [];
    return { n: runs.length, km, by, secN: secs.length, secHit: secs.filter((s) => (s.m || []).some((m) => on.has(m))).length,
      front: runs.filter((r) => r.layer === 1).length };
  }

  // ---------- runs: flow vs gradient, the Pareto view of the run finder (SVG) ----------
  /** Shade of a mark from its height-to-area rank t (0..1): the accent mixed into the surface, pale low to dark high. */
  const shade = (t) => `color-mix(in oklab, var(--accent) ${Math.round(18 + 82 * t)}%, var(--surface))`;
  /** SVG of a mark at (x, y): circle (new lead), square (in the guides) or diamond (test river). */
  function mark(r, x, y, rad, attrs) {
    const X = x.toFixed(1), Y = y.toFixed(1);
    if (r.status === "listed") { const s = rad * 0.9; return `<rect x="${(x - s).toFixed(1)}" y="${(y - s).toFixed(1)}" width="${(2 * s).toFixed(1)}" height="${(2 * s).toFixed(1)}" rx="1.5" ${attrs}/>`; }
    if (r.status === "heldout") { const s = rad * 1.3; return `<path d="M${X} ${(y - s).toFixed(1)}L${(x + s).toFixed(1)} ${Y}L${X} ${(y + s).toFixed(1)}L${(x - s).toFixed(1)} ${Y}Z" ${attrs}/>`; }
    return `<circle cx="${X}" cy="${Y}" r="${rad}" ${attrs}/>`;
  }
  const svgIcon = (inner) => el("span", { class: "models-scat-ico", "aria-hidden": "true", html: `<svg viewBox="0 0 16 16" width="16" height="16">${inner}</svg>` });

  function buildScatter() {
    const st = { R: null, runs: [], p: null, pts: [], W: 0, sel: null };
    const host = el("div", { class: "models-scat-host" });
    const tip = el("div", { class: "rf-chart-tip models-scat-tip", hidden: true });
    const pick = el("div", { class: "models-scat-pick", "aria-live": "polite" });
    const front = el("div");
    const ring = 'fill="none" stroke="var(--fg)" stroke-width="1.75"';
    const legend = el("div", { class: "models-scat-legend" },
      el("span", {}, svgIcon(`<circle cx="8" cy="8" r="4" style="fill:${shade(0.6)}" stroke="var(--surface)"/>`), "New lead"),
      el("span", {}, svgIcon(`<rect x="4.4" y="4.4" width="7.2" height="7.2" rx="1.5" style="fill:${shade(0.6)}" stroke="var(--surface)"/>`), "In the guides"),
      el("span", {}, svgIcon(`<path d="M8 2.8L13.2 8L8 13.2L2.8 8Z" style="fill:${shade(0.6)}" stroke="var(--surface)"/>`), "Test river"),
      el("span", {}, svgIcon(`<circle cx="8" cy="8" r="5" style="fill:${shade(0.6)}" ${ring.replace('fill="none" ', "")}/>`), "Pareto frontier (ringed)"),
      el("span", {}, el("span", { class: "models-scat-ramp", "aria-hidden": "true" }), "Height-to-area, low to high"),
      el("span", {}, svgIcon('<rect x="2" y="3" width="12" height="10" rx="1" class="models-scat-band"/>'), "Full-credit band"));
    const note = el("p", { class: "rf-note" });
    const node = el("div", { class: "rf-stack models-scat" },
      el("h3", { text: "Runs: flow vs gradient" }), note, el("div", { class: "models-scat-wrap" }, host, tip), legend, pick, front);
    let lastW = 0;
    if ("ResizeObserver" in window) {
      new ResizeObserver(RF.util.debounce(() => { const w = host.clientWidth; if (w && Math.abs(w - lastW) > 8 && st.R) draw(); }, 120)).observe(host);
    }

    const haName = () => (st.p?.haMeasure === "height_to_width" ? "height-to-width" : "height-to-area");
    const numbers = (r) => `flood ${fmt.flow(r.maf)} m³/s · ${fmt.grad(r.grad)} m/km${st.p?.flowScaled && r.effG > r.grad * 1.05 ? ` (counted as ${fmt.grad(r.effG)})` : ""} · ${haName()} ${r.ha.toFixed(3)} · ${r.len.toFixed(1)} km`;

    function draw() {
      const { runs, p, R } = st;
      lastW = host.clientWidth;
      tip.hidden = true;
      if (!R) { host.replaceChildren(); pick.replaceChildren(); front.replaceChildren(); note.textContent = ""; return; }
      note.textContent = `Each mark is a run in ${R.title}: across, its mean annual flood (log scale); up, its gradient${p.flowScaled ? ", with big rivers counted steeper as set above" : ""}; the shade is its ${haName()}. Ringed marks are on the Pareto frontier: no other run here has more flood flow, a steeper gradient and a higher ${haName()} at once. The frontier is outlined on the map and marked in the lists. Tap or click a mark for its run; arrow keys step through them.`;
      if (!runs.length) { host.replaceChildren(el("p", { class: "rf-note", text: "No runs pass these settings here." })); pick.replaceChildren(); front.replaceChildren(); return; }
      const W = (st.W = Math.max(280, Math.min(760, lastW || 600))), H = W < 480 ? 300 : 360, m = { l: 42, r: 12, t: 10, b: 40 };
      const qs = runs.map((r) => Math.max(r.maf, 1e-3));
      const x0 = Math.floor(Math.log10(Math.min(...qs, p.q[0]))), x1 = Math.max(x0 + 1, Math.ceil(Math.log10(Math.max(...qs, p.q[1]))));
      const gmax = Math.max(...runs.map((r) => r.effG), p.g[1], 1), yt = RF.chart.ticks(0, gmax, H < 320 ? 4 : 5), step = yt[1] - yt[0] || gmax;
      const y1 = Math.ceil(gmax / step - 1e-9) * step;
      const X = (q) => m.l + (Math.log10(Math.max(q, 10 ** x0)) - x0) / (x1 - x0) * (W - m.l - m.r);
      const Y = (g) => H - m.b - Math.max(0, Math.min(g, y1)) / y1 * (H - m.t - m.b);
      // shade: rank of height-to-area among these runs (pale low, dark high)
      const t = new Float64Array(runs.length);
      runs.map((r, i) => i).sort((a, b) => runs[a].ha - runs[b].ha).forEach((i, k) => { t[i] = runs.length > 1 ? k / (runs.length - 1) : 1; });
      st.pts = runs.map((r) => [X(r.maf), Y(r.effG)]);
      const s = [];
      s.push(`<rect class="models-scat-band" x="${X(p.q[0]).toFixed(1)}" y="${Y(p.g[1]).toFixed(1)}" width="${Math.max(0, X(p.q[1]) - X(p.q[0])).toFixed(1)}" height="${Math.max(0, Y(p.g[0]) - Y(p.g[1])).toFixed(1)}"/>`);
      for (let k = x0; k <= x1; k++) {
        const x = X(10 ** k).toFixed(1), lab = k >= 0 ? fmt.int(10 ** k) : String(+(10 ** k).toPrecision(1));
        // the last label ends at the plot's edge, so it is not cut off
        s.push(`<line class="rf-chart-grid" x1="${x}" x2="${x}" y1="${m.t}" y2="${H - m.b}"/><text class="rf-chart-xlab" x="${x}" y="${H - m.b + 14}" text-anchor="${k === x1 ? "end" : k === x0 ? "start" : "middle"}">${lab}</text>`);
      }
      for (let v = 0; v <= y1 + 1e-9; v += step) {
        const y = Y(v).toFixed(1);
        s.push(`<line class="rf-chart-grid" x1="${m.l}" x2="${W - m.r}" y1="${y}" y2="${y}"/><text class="rf-chart-ylab" x="${m.l - 5}" y="${(+y + 3.5).toFixed(1)}" text-anchor="end">${+v.toPrecision(6)}</text>`);
      }
      s.push(`<line class="rf-chart-axis" x1="${m.l}" x2="${W - m.r}" y1="${H - m.b}" y2="${H - m.b}"/>`);
      s.push(`<text class="rf-chart-xlab" x="${(m.l + W - m.r) / 2}" y="${H - 6}" text-anchor="middle">Mean annual flood, m³/s</text>`);
      s.push(`<text class="rf-chart-xlab" transform="translate(11 ${(m.t + H - m.b) / 2}) rotate(-90)" text-anchor="middle">Gradient, m/km</text>`);
      // the frontier last, so its rings are on top
      const order = runs.map((r, i) => i).sort((a, b) => (runs[a].layer === 1) - (runs[b].layer === 1));
      order.forEach((i) => {
        const r = runs[i], [x, y] = st.pts[i], f = r.layer === 1;
        s.push(mark(r, x, y, f ? 5.5 : 4, `style="fill:${shade(t[i])}" stroke="${f ? "var(--fg)" : "var(--surface)"}" stroke-width="${f ? 1.75 : 1}"`));
      });
      s.push('<circle class="models-scat-hov" r="9" cx="-20" cy="-20"/><circle class="models-scat-sel" r="10" cx="-20" cy="-20"/>');
      const nf = runs.filter((r) => r.layer === 1).length;
      host.innerHTML = `<svg class="rf-chart-svg models-scat-svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" tabindex="0" role="img" aria-label="${esc(`${runs.length} runs in ${R.title} by mean annual flood and gradient; ${nf} on the Pareto frontier. Arrow keys step through them.`)}">${s.join("")}</svg>`;
      const i = runs.findIndex((r) => r.id === st.sel);
      select(i >= 0 ? i : -1, true);
      const fr = runs.filter((r) => r.layer === 1).sort((a, b) => b.score - a.score);
      front.replaceChildren(el("details", { class: "models-more" }, el("summary", { text: `Runs on the Pareto frontier (${fr.length})` }),
        el("ol", { class: "models-runs" }, ...fr.map((r) => runItem(r, numbers(r))))));
    }

    /** The run nearest a pointer event, within tol px of the drawing (-1: none). */
    function nearest(ev, tol) {
      const svg = host.querySelector("svg"); if (!svg || !st.pts.length) return -1;
      const b = svg.getBoundingClientRect(), k = st.W / b.width, x = (ev.clientX - b.left) * k, y = (ev.clientY - b.top) * k;
      let best = (tol * k) ** 2, bi = -1;
      st.pts.forEach(([px, py], i) => { const d = (px - x) ** 2 + (py - y) ** 2; if (d < best) { best = d; bi = i; } });
      return bi;
    }
    const place = (cls, i) => {
      const c = host.querySelector(cls); if (!c) return;
      const [x, y] = i >= 0 ? st.pts[i] : [-20, -20];
      c.setAttribute("cx", x.toFixed(1)); c.setAttribute("cy", y.toFixed(1));
    };
    /** Select run i (-1: none): ring it and show it under the plot. */
    function select(i, quiet) {
      const r = i >= 0 ? st.runs[i] : null;
      st.sel = r ? r.id : null;
      place(".models-scat-sel", i);
      if (!r) { pick.replaceChildren(el("p", { class: "rf-note", text: "Tap a mark for its run." })); return; }
      const sname = SCORE_NAME[rankOf(st.p)] || "score";
      const [cls, label] = STATUS_PILL[r.status] || STATUS_PILL.candidate;
      pick.replaceChildren(el("div", { class: "models-scat-card" },
        el("div", { class: "models-run-main" }, el("strong", { text: r.name }), el("span", { class: `rf-pill ${cls}`, text: label }),
          el("span", { class: `rf-pill${r.layer === 1 ? " rf-pill--accent" : ""}`, title: r.layer === 1 ? FRONT_TIP : "Pareto layer: 1 is the frontier, 2 the frontier once those are left out, and so on", text: r.layer === 1 ? "Pareto frontier" : `Pareto layer ${r.layer}` })),
        el("span", { class: "num rf-muted", text: `${numbers(r)} · ${sname} ${r.score.toFixed(0)}` }),
        el("div", { class: "rf-row" },
          el("button", { type: "button", class: "rf-btn rf-btn--primary", onclick: () => RF.go("map", { select: r }) }, "Show on map"),
          el("button", { type: "button", class: "rf-btn", onclick: (e) => RF.detail.open(r, { opener: e.currentTarget }) }, "Details"))));
      if (!quiet) tip.hidden = true;
    }

    host.addEventListener("pointermove", (ev) => {
      if (ev.pointerType !== "mouse") return;
      const i = nearest(ev, 12);
      place(".models-scat-hov", i);
      host.style.cursor = i >= 0 ? "pointer" : "";
      if (i < 0) { tip.hidden = true; return; }
      const r = st.runs[i];
      tip.replaceChildren(el("strong", { text: r.name }), el("br"), el("span", { text: r.layer === 1 ? "Pareto frontier" : `Pareto layer ${r.layer}` }), el("br"),
        el("span", { text: `${fmt.flow(r.maf)} m³/s · ${fmt.grad(r.effG)} m/km · ${r.ha.toFixed(3)}` }));
      tip.hidden = false;
      const wb = host.parentElement.getBoundingClientRect(), left = ev.clientX - wb.left + 14;
      tip.style.left = `${Math.max(0, Math.min(left, wb.width - tip.offsetWidth - 4))}px`;
      tip.style.top = `${Math.max(0, ev.clientY - wb.top - tip.offsetHeight - 10)}px`;
    });
    host.addEventListener("pointerleave", () => { tip.hidden = true; place(".models-scat-hov", -1); });
    host.addEventListener("click", (ev) => {
      const i = nearest(ev, window.matchMedia("(pointer: coarse)").matches ? 22 : 14);
      if (i >= 0) select(i);
    });
    host.addEventListener("keydown", (ev) => {
      if (!st.runs.length) return;
      const byX = st.runs.map((r, i) => i).sort((a, b) => st.pts[a][0] - st.pts[b][0] || st.pts[b][1] - st.pts[a][1]);
      const at = byX.indexOf(st.runs.findIndex((r) => r.id === st.sel));
      if (ev.key === "ArrowRight" || ev.key === "ArrowDown") select(byX[Math.min(byX.length - 1, at + 1)]);
      else if (ev.key === "ArrowLeft" || ev.key === "ArrowUp") select(byX[Math.max(0, at < 0 ? 0 : at - 1)]);
      else if (ev.key === "Enter" && at >= 0) RF.go("map", { select: st.runs[byX[at]] });
      else if (ev.key === "Escape") select(-1);
      else return;
      ev.preventDefault();
    });

    return {
      node,
      /** Draw the runs of region R found with params p (R null: clear). */
      update(R, runs = [], p = null) {
        if (R !== st.R) st.sel = null;
        Object.assign(st, { R, runs, p });
        draw();
      },
    };
  }

  function buildTuner() {
    let cand = clone(RF.state.runParams || RF.runs.defaults()), token = 0;
    const base = new Map();   // `${region}|${model}` → summary with the default settings
    const ctl = {}, outs = {};
    const status = el("p", { class: "rf-note models-inuse", role: "status" });
    const box = el("div", { class: "rf-stack models-preview", "aria-live": "polite" });
    const regionPick = el("div", { class: "models-ctl rf-stack" });
    const scat = buildScatter();
    const later = RF.util.debounce(() => preview(), 250);

    const apply = () => {
      const q = cand.q.map(Number), g = cand.g.map(Number);
      if (!(q[0] > 0 && q[1] > q[0]) || !(g[0] > 0 && g[1] > g[0])) { status.textContent = "The lower end of a band must be above zero and below the upper end."; return; }
      if (JSON.stringify(cand) !== JSON.stringify(RF.state.runParams)) RF.setRunParams(clone(cand));
    };
    const changed = () => { later(); };
    const field = (id, label, help, ...input) => el("div", { class: "models-field" },
      el("label", { for: id, class: "models-flabel" }, el("span", { class: "models-fname", text: label }), help ? el("span", { class: "rf-note", text: help }) : null),
      el("div", { class: "models-finput" }, ...input));
    const number = (id, get, set, o) => {
      const inp = el("input", { type: "number", id, class: "rf-input models-num", min: o.min, max: o.max, step: o.step, inputmode: "decimal",
        oninput: (e) => { const v = parseFloat(e.target.value); if (isFinite(v)) { set(Math.min(o.max, Math.max(o.min, v))); changed(); } },
        onchange: (e) => { e.target.value = get(); apply(); } });
      ctl[id] = { el: inp, get };
      return inp;
    };
    const range = (id, get, set, o) => {
      const out = el("output", { for: id, class: "num models-out" });
      const inp = el("input", { type: "range", id, class: "models-range", min: o.min, max: o.max, step: o.step,
        oninput: (e) => { set(+e.target.value); out.textContent = o.fmt(get()); changed(); }, onchange: () => apply() });
      ctl[id] = { el: inp, get }; outs[id] = () => { out.textContent = o.fmt(get()); };
      return [inp, out];
    };
    const check = (id, label, get, set) => {
      const inp = el("input", { type: "checkbox", id, onchange: (e) => { set(e.target.checked); apply(); } });
      ctl[id] = { el: inp, get, check: true };
      return el("label", { class: "models-check", for: id }, inp, el("span", { text: label }));
    };
    const W = ["flood", "gradient", "height-to-area"], w2 = (v) => v.toFixed(2);
    const haSel = el("select", { id: "models-rf-ham", class: "rf-select", onchange: (e) => { cand.haMeasure = e.target.value; sync(true); apply(); } },
      ...Object.keys(haDefaults()).map((k) => el("option", { value: k, text: HA_LABEL[k] || k })));
    ctl["models-rf-ham"] = { el: haSel, get: () => cand.haMeasure };
    const haHelp = el("span", { class: "rf-note" });

    // how runs are ranked and coloured: one choice, applied at once
    const ranks = RF.runs.rankings.map((r) => {
      const input = el("input", { type: "radio", name: "models-rank", value: r.id, onchange: () => { cand.rankBy = r.id; sync(true); apply(); } });
      const name = el("strong");
      return { id: r.id, input, name, node: el("label", { class: "models-rank" }, input,
        el("span", { class: "models-rank-text" }, name, el("span", { class: "rf-note", text: r.note }))) };
    });
    const rankSet = el("fieldset", { class: "models-set models-rankset" }, el("legend", { text: "Rank and colour runs by" }),
      el("div", { class: "models-ranks" }, ...ranks.map((r) => r.node)));

    // your feature weights (ranking "comp"): a slider for every feature
    const feats = RF.runs.features(), hints = {};
    /** The weights to start from: the current model's coefficients when it is a linear model, else the pooled regression's. */
    const startFrom = () => {
      const m = RF.models.get(RF.state.model);
      if (m?.kind === "logistic" && Object.keys(m.coef || {}).length) return { coef: m.coef, name: shortOf(m.id) };
      const c = RF.models.legacy?.coef;
      return c && Object.keys(c).length ? { coef: c, name: "the pooled regression" } : null;
    };
    const signed = (v) => `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(2)}`;
    const startBtn = el("button", { type: "button", class: "rf-btn", onclick: () => {
      const s = startFrom(); if (!s) return;
      feats.forEach((f) => { cand.featureWeights[f.id] = Math.max(-2, Math.min(2, Math.round((s.coef[f.id] ?? 0) * 4) / 4)); });
      sync(true); apply();
    } });
    const wSet = el("fieldset", { class: "models-set models-wset" }, el("legend", { text: "Your feature weights" }),
      el("p", { class: "rf-note models-wide", text: "Each feature is standardised: 1 is one standard deviation across the reaches. A reach's score is the weighted sum of its features, as a percentile among reaches of 2 km² or more, and a run takes its best reach's. Positive weights raise the score, negative ones lower it. Changes apply when you let go of a slider." }),
      el("div", { class: "rf-row models-wide" },
        el("button", { type: "button", class: "rf-btn", onclick: () => { cand.featureWeights = RF.runs.defaults().featureWeights; sync(true); apply(); } }, "Reset the weights"),
        startBtn),
      ...(feats.length ? feats.map((f) => {
        const id = `models-rf-fw-${f.id}`;
        hints[f.id] = el("span", { class: "rf-note" });
        return el("div", { class: "models-field" },
          el("label", { for: id, class: "models-flabel" }, el("span", { class: "models-fname", text: f.label }), hints[f.id]),
          el("div", { class: "models-finput" }, ...range(id, () => cand.featureWeights[f.id] ?? 0, (v) => { cand.featureWeights[f.id] = v; },
            { min: -2, max: 2, step: 0.25, fmt: signed })));
      }) : [el("p", { class: "rf-note models-wide", text: "This build has no feature list (model.json), so there is nothing to weight." })]));
    /** The ranking controls from cand: which one is checked, the model's name, the weights shown only for "comp". */
    function syncRank() {
      const cur = rankOf(cand);
      ranks.forEach((r) => { r.input.checked = r.id === cur; r.name.textContent = rankName(r.id); });
      wSet.hidden = cur !== "comp";
      const s = startFrom();
      startBtn.hidden = !s;
      if (s) startBtn.textContent = `Start from ${s.name}'s weights`;
      feats.forEach((f) => { hints[f.id].textContent = s && s.coef[f.id] != null ? `${s.name[0].toUpperCase()}${s.name.slice(1)}: ${signed(s.coef[f.id])}` : ""; });
    }

    const form = el("div", { class: "rf-stack models-form" },
      rankSet, wSet,
      el("fieldset", { class: "models-set" }, el("legend", { text: "Bands: full credit inside, none far outside" }),
        field("models-rf-q0", "Flood flow (mean annual flood), m³/s", "Full credit between these. None below a quarter of the lower end or above 6.7 × the upper.",
          number("models-rf-q0", () => cand.q[0], (v) => { cand.q[0] = v; }, { min: 0.1, max: 5000, step: 0.5 }), el("span", { class: "rf-muted", text: "to" }),
          number("models-rf-q1", () => cand.q[1], (v) => { cand.q[1] = v; }, { min: 0.5, max: 20000, step: 10 })),
        field("models-rf-g0", "Gradient, m/km", "Full credit between these. None below a quarter of the lower end or above 1.9 × the upper.",
          number("models-rf-g0", () => cand.g[0], (v) => { cand.g[0] = v; }, { min: 0.5, max: 300, step: 1 }), el("span", { class: "rf-muted", text: "to" }),
          number("models-rf-g1", () => cand.g[1], (v) => { cand.g[1] = v; }, { min: 1, max: 600, step: 5 })),
        field("models-rf-ham", "Height-to-area measure", "How deep the valley is for the size of the river.", haSel),
        el("div", { class: "models-field" },
          el("label", { for: "models-rf-hah", class: "models-flabel" }, el("span", { class: "models-fname", text: "Height-to-area: full credit from" }), haHelp),
          el("div", { class: "models-finput" }, number("models-rf-hah", () => cand.haHi[cand.haMeasure], (v) => { cand.haHi[cand.haMeasure] = v; }, { min: 0.001, max: 50, step: 0.01 })))),
      el("fieldset", { class: "models-set" }, el("legend", { text: "Score and threshold" }),
        ...W.map((w, k) => field(`models-rf-w${k}`, `Weight: ${w}`, null, ...range(`models-rf-w${k}`, () => cand.w[k], (v) => { cand.w[k] = v; }, { min: 0, max: 3, step: 0.25, fmt: w2 }))),
        field("models-rf-thr", "Threshold", "A reach passes when the weighted mean of its three part scores (0 to 1) reaches this.",
          ...range("models-rf-thr", () => cand.threshold, (v) => { cand.threshold = v; }, { min: 0.3, max: 1, step: 0.05, fmt: w2 })),
        el("div", { class: "models-field models-checks" },
          check("models-rf-gate", "All three part scores must be above zero (gate)", () => cand.gate, (v) => { cand.gate = v; }),
          check("models-rf-fs", "Count big rivers as steeper: gradient × √(flood ÷ 20 m³/s), from 1× to 3×", () => cand.flowScaled, (v) => { cand.flowScaled = v; }))),
      el("fieldset", { class: "models-set" }, el("legend", { text: "Joining reaches into runs" }),
        field("models-rf-len", "Minimum run length, km", "Shorter stretches of passing reaches are left out.",
          number("models-rf-len", () => cand.minLen, (v) => { cand.minLen = v; }, { min: 0, max: 50, step: 0.25 })),
        field("models-rf-gap", "Gaps allowed inside a run, reaches", "Reaches below the threshold (about 250 m each) that a run may cross.",
          number("models-rf-gap", () => cand.maxGap, (v) => { cand.maxGap = Math.round(v); }, { min: 0, max: 5, step: 1 }))));

    /** Controls from cand (after a reset elsewhere, or a new height-to-area measure). keepFocus: leave the focused one alone. */
    function sync(keepFocus) {
      Object.entries(ctl).forEach(([id, c]) => {
        if (keepFocus && document.activeElement === c.el && c.el.type !== "select-one") return;
        if (c.check) c.el.checked = !!c.get(); else c.el.value = c.get();
      });
      Object.values(outs).forEach((f) => f());
      const [lo] = haDefaults()[cand.haMeasure] || [0];
      haHelp.textContent = `None at or below ${lo.toFixed(3)}; the score rises in a straight line up to this.`;
      syncRank();
      const inUse = RF.state.runParams || cand, ch = changes(inUse);
      status.textContent = `In use on the map and in Top rivers: runs ranked by ${rankWords(rankOf(inUse))}; ${ch.length ? `changed from the defaults (${ch.join(", ")})` : "the default settings"}.`;
    }

    async function preview() {
      const my = ++token, id = view.region;
      if (!id) { box.replaceChildren(el("p", { class: "rf-note", text: "No regions in this build." })); scat.update(null); return; }
      if (!box.firstChild || box.dataset.region !== id) { box.replaceChildren(el("p", { class: "rf-note", text: "Finding runs…" })); scat.update(null); }
      let R, model;
      try {
        R = await RF.data.region(id);
        model = RF.state.model ? await RF.data.scores(id, RF.state.model) : null;
      } catch (e) { if (my === token) box.replaceChildren(el("p", { class: "rf-note", text: e.message || "Could not load the region." })); return; }
      if (my !== token) return;
      await yieldMain();
      if (my !== token) return;
      if (!R.segments) { box.replaceChildren(el("p", { class: "rf-note", text: "This region has no modelled reaches." })); scat.update(null); return; }
      const key = `${id}|${RF.state.model}`, p = clone(cand);
      if (!base.has(key)) base.set(key, summarize(R, RF.runs.find(R, { params: RF.runs.defaults(), model })));
      const runs = RF.runs.find(R, { params: p, model }), now = summarize(R, runs), d = base.get(key);
      if (my !== token) return;
      const row = (label, a, b, f = (v) => fmt.int(v)) => el("tr", {}, el("th", { scope: "row", text: label }), el("td", { class: "n", text: f(a) }), el("td", { class: "n rf-muted", text: f(b) }));
      const rk = rankOf(p), sname = SCORE_NAME[rk] || "score";
      const by = rk === "model" ? (model && !model.fallback ? shortOf(RF.state.model) : "the pooled regression") : rankWords(rk);
      const top = runs.slice(0, 5).map((r) => runItem(r, `${r.len.toFixed(1)} km · ${fmt.grad(r.grad)} m/km · flood ${fmt.flow(r.maf)} m³/s · ${sname} ${r.score.toFixed(0)}${rk === "pareto" && r.layer > 1 ? ` · layer ${r.layer}` : ""}`));
      box.dataset.region = id;
      box.replaceChildren(
        table([th(""), th("These settings", true), th("Defaults", true)], [
          row("Runs", now.n, d.n), row("New leads", now.by.candidate || 0, d.by.candidate || 0), row("In the guides", now.by.listed || 0, d.by.listed || 0),
          row("Test rivers", now.by.heldout || 0, d.by.heldout || 0), row("Total length, km", now.km, d.km, (v) => v.toFixed(0)),
          row("Guide sections touched", now.secHit, d.secHit, (v) => (now.secN ? `${v} of ${now.secN}` : "–")),
          row("On the Pareto frontier", now.front, d.front)], "models-cmp"),
        el("p", { class: "rf-note", text: `Ranked by ${by}. "Guide sections touched": known sections in this region that at least one run overlaps, a rough check that the settings still find the runs people paddle.` }),
        runs.length ? el("h3", { text: "Best five runs with these settings" }) : null,
        runs.length ? el("ol", { class: "models-runs" }, ...top) : el("p", { class: "rf-note", text: "No runs pass these settings here." }));
      scat.update(R, runs, p);
    }

    const node = el("section", { class: "rf-panel rf-stack", id: "models-rf", "aria-labelledby": "models-rf-h" },
      el("h2", { id: "models-rf-h", text: "Run finder" }),
      el("p", { class: "rf-note", text: "How the map and Top rivers turn reaches into runs and rank them. Each reach gets three part scores from 0 to 1 (flood flow, gradient, height-to-area); a reach passes when their weighted mean reaches the threshold, and passing reaches in a row make a run. The ranking orders and colours the runs: on the map, in Top rivers and here. Changes apply when you let go of a slider or leave a box." }),
      status,
      el("div", { class: "rf-row" }, el("button", { type: "button", class: "rf-btn", onclick: () => RF.setRunParams(null) }, "Reset to defaults")),
      form,
      el("h3", { text: "Effect in one region" }),
      regionPick, box, scat.node);

    return {
      node,
      /** The settings in use changed (here or elsewhere): controls from RF.state.runParams, then the preview. */
      sync(quiet) { cand = clone(RF.state.runParams || RF.runs.defaults()); sync(); if (!quiet) preview(); },
      model() { sync(true); preview(); },
      region() {
        regionPick.replaceChildren(el("label", { for: "models-rf-region", class: "rf-eyebrow", text: "Region" }), regionSelect("models-rf-region", (id) => setRegion(id, true)));
        preview();
      },
    };
  }

  // ---------- separation: how well single measures tell known runs from the other reaches ----------
  const AXES = {
    score: { label: "Model score, percentile", get: (S, i, X) => X.pct[i] },
    maf: { label: "Mean annual flood, m³/s", log: true, get: (S, i) => S.maf[i] },
    a: { label: "Catchment area, km²", log: true, get: (S, i) => S.a[i] },
    g: { label: "Gradient, m/km", get: (S, i) => S.g[i] },
    s50: { label: "Steepest 50 m, m/km", get: (S, i) => S.s50[i] },
    mel: { label: "Height-to-area (Melton ratio)", log: true, get: (S, i) => S.mel[i] },
    hw: { label: "Gorge depth ÷ valley-floor width", log: true, get: (S, i) => S.hw[i] },
    gorge: { label: "Gorge depth, m", get: (S, i) => S.gorge[i] },
    head: { label: "Catchment height above the channel, m", get: (S, i) => S.head[i] },
    ksn: { label: "Channel steepness k_sn", log: true, get: (S, i) => S.ksn[i] },
    pow: { label: "Stream power at the mean annual flood, kW/m", log: true, get: (S, i) => S.pow[i] },
  };
  const SAMPLE = 6000;   // most other reaches drawn in 3D (all of them count in the AUC)

  /** Chance that a random pos value is above a random neg value (ties count half). */
  function auc(pos, neg) {
    if (!pos.length || !neg.length) return NaN;
    const all = new Float64Array(pos.length + neg.length), lab = new Uint8Array(all.length);
    pos.forEach((v, k) => { all[k] = v; lab[k] = 1; });
    neg.forEach((v, k) => { all[pos.length + k] = v; });
    const idx = Array.from(all.keys()).sort((a, b) => all[a] - all[b]);
    let sum = 0;
    for (let i = 0; i < idx.length;) {
      let j = i; while (j < idx.length && all[idx[j]] === all[idx[i]]) j++;
      const avg = (i + j + 1) / 2; for (let k = i; k < j; k++) if (lab[idx[k]]) sum += avg;
      i = j;
    }
    return (sum - pos.length * (pos.length + 1) / 2) / (pos.length * neg.length);
  }
  const median = (a) => { const b = Float64Array.from(a).sort(); return b.length ? b[b.length >> 1] : NaN; };
  const fmtv = (v) => (!isFinite(v) ? "–" : Math.abs(v) >= 100 ? v.toFixed(0) : Math.abs(v) >= 10 ? v.toFixed(1) : Math.abs(v) >= 1 ? v.toFixed(2) : v.toPrecision(2));

  let plotlyP = null;
  function loadPlotly() {
    if (window.Plotly) return Promise.resolve(window.Plotly);
    if (!plotlyP) plotlyP = new Promise((res, rej) => {
      const s = document.createElement("script");
      s.src = PLOTLY; s.async = true; s.crossOrigin = "anonymous";
      s.onload = () => (window.Plotly ? res(window.Plotly) : rej(new Error("no Plotly")));
      s.onerror = () => { plotlyP = null; s.remove(); rej(new Error("load failed")); };
      document.head.append(s);
    });
    return plotlyP;
  }

  function buildSep() {
    const st = { x: "maf", y: "g", z: "mel", open: false, data: null };
    let token = 0, drawn = false;
    const regionPick = el("div", { class: "models-ctl rf-stack" });
    const aucBox = el("div", { class: "rf-stack", "aria-live": "polite" });
    const plot = el("div", { class: "models-plot", role: "img", "aria-label": "3D scatter of reaches: known runs and the other reaches" });
    const plotNote = el("p", { class: "rf-note" });
    const axisPick = (k) => el("label", { class: "rf-stack models-ctl", for: `models-sep-${k}` }, el("span", { class: "rf-eyebrow", text: `${k.toUpperCase()} axis` }),
      el("select", { id: `models-sep-${k}`, class: "rf-select", onchange: (e) => { st[k] = e.target.value; draw(); } },
        ...Object.entries(AXES).map(([v, a]) => el("option", { value: v, selected: v === st[k], text: a.label }))));
    const openBtn = el("button", { type: "button", class: "rf-btn rf-btn--primary", onclick: () => { st.open = true; openBtn.hidden = true; plotBox.hidden = false; draw(); } }, "Show the 3D view");
    const plotBox = el("div", { class: "rf-stack", hidden: true }, el("div", { class: "rf-row models-ctls" }, axisPick("x"), axisPick("y"), axisPick("z")), plot, plotNote);

    /** Reaches of 2 km² or more in the region: known = on a guide section; role from the model's scores. */
    async function data() {
      const id = view.region, mid = RF.state.model;
      if (st.data && st.data.id === id && st.data.mid === mid) return st.data;
      const R = await RF.data.region(id), S = R.segments;
      if (!S) return { id, mid, R, S: null };
      const sc = mid ? await RF.data.scores(id, mid) : { pct: S.reg, role: null };
      const sec = new Map();
      (R.sections || []).forEach((s) => (s.m || []).forEach((m) => { if (!sec.has(m)) sec.set(m, s); }));
      const known = [], other = [];
      for (let i = 0; i < S.g.length; i++) if (S.a[i] >= 2) (sec.has(i) ? known : other).push(i);
      st.data = { id, mid, R, S, X: { pct: sc.pct && sc.pct.length ? sc.pct : S.reg }, role: sc.role, fallback: !!sc.fallback, sec, known, other };
      return st.data;
    }

    async function update() {
      const my = ++token;
      if (!view.region) { aucBox.replaceChildren(el("p", { class: "rf-note", text: "No regions in this build." })); return; }
      aucBox.replaceChildren(el("p", { class: "rf-note", text: "Measuring…" }));
      let D;
      try { D = await data(); } catch (e) { if (my === token) aucBox.replaceChildren(el("p", { class: "rf-note", text: e.message || "Could not load the region." })); return; }
      if (my !== token) return;
      await yieldMain();
      if (my !== token) return;
      if (!D.S) { aucBox.replaceChildren(el("p", { class: "rf-note", text: "This region has no modelled reaches." })); return; }
      if (!D.known.length) { aucBox.replaceChildren(el("p", { class: "rf-note", text: "No guide sections are placed in this region, so there is nothing to compare." })); if (st.open) draw(); return; }
      const rows = Object.entries(AXES).map(([k, a]) => {
        const pos = D.known.map((i) => a.get(D.S, i, D.X)).filter(isFinite), neg = D.other.map((i) => a.get(D.S, i, D.X)).filter(isFinite);
        const A = auc(pos, neg);
        return { k, a, A, mp: median(pos), mn: median(neg) };
      });
      const label = (r) => (r.k === "score" ? `${D.fallback || !D.mid ? "Pooled regression" : shortOf(D.mid)} score, percentile` : r.a.label);
      aucBox.replaceChildren(
        el("p", { class: "rf-note", text: `${fmt.int(D.known.length)} reaches on known runs and ${fmt.int(D.other.length)} other reaches of 2 km² or more in ${D.R.title}.` }),
        table([th("Measure"), th("AUC", true, "Chance that a reach on a known run sits higher than another reach"), th("Separation"), th("Known runs, median", true), th("Others, median", true)],
          rows.map((r) => el("tr", { class: r.k === "score" ? "models-cur" : null }, el("td", { text: label(r) }), el("td", { class: "n", text: F2(r.A) }),
            el("td", { class: "models-sepcell" }, bars([{ label: "", text: "", frac: isFinite(r.A) ? (r.A - 0.5) * 2 : 0 }], true)),
            el("td", { class: "n", text: fmtv(r.mp) }), el("td", { class: "n", text: fmtv(r.mn) }))), "models-sep"),
        el("p", { class: "rf-note", text: "AUC: the chance that a random reach on a known run sits higher on that measure than a random other reach. 0.5 is no separation; far from 0.5 either way separates well (below 0.5: known runs sit lower). Known runs here include the rivers the model trained on, so its score flatters it; the held-out results above are the fair test. The other reaches still hold runs nobody has listed." }));
      if (st.open) draw();
    }

    async function draw() {
      if (!st.open) return;
      const my = token;
      let P;
      plotNote.textContent = "";
      if (!window.Plotly) plot.replaceChildren(el("p", { class: "rf-note models-plotmsg", text: "Loading the 3D plotting library (Plotly, 1.6 MB)…" }));
      try { P = await loadPlotly(); } catch (e) {
        plot.replaceChildren(el("div", { class: "rf-stack models-plotmsg" }, el("p", { class: "rf-note", text: "Could not load the 3D plotting library from cdnjs. Check the connection and try again; the table above does not need it." }),
          el("div", {}, el("button", { type: "button", class: "rf-btn", onclick: () => draw() }, "Try again"))));
        return;
      }
      let D;
      try { D = await data(); } catch (e) { return; }
      if (my !== token || !st.open || !D.S) return;
      const { S, X, role } = D, ax = [st.x, st.y, st.z].map((k) => AXES[k]);
      const val = (a, i) => { const v = a.get(S, i, X); return a.log ? Math.max(v, 1e-4) : v; };
      const name = (i) => (S.r[i] >= 0 ? S.rivers[S.r[i]] : "Unnamed stream");
      const step = Math.max(1, Math.ceil(D.other.length / SAMPLE)), others = D.other.filter((_, k) => k % step === 0);
      const test = D.known.filter((i) => role && (role[i] & 3) === 2), known = D.known.filter((i) => !(role && (role[i] & 3) === 2));
      const hover = (i) => `<b>${esc(name(i))}</b>${D.sec.has(i) ? `<br>${esc(D.sec.get(i).section || "Guide section")}` : ""}<br>${ax.map((a) => `${esc(a.label)}: ${fmtv(a.get(S, i, X))}`).join("<br>")}`;
      const css = RF.util.css, surf = css("--surface"), fg = css("--fg"), muted = css("--muted"), line = css("--line");
      const trace = (ids, nameTxt, color, size, opacity, symbol) => ({ type: "scatter3d", mode: "markers", name: `${nameTxt} (${fmt.int(ids.length)})`,
        x: ids.map((i) => val(ax[0], i)), y: ids.map((i) => val(ax[1], i)), z: ids.map((i) => val(ax[2], i)),
        text: ids.map(hover), hovertemplate: "%{text}<extra></extra>", customdata: ids,
        marker: { size, color, opacity, symbol, line: { width: 0 } } });
      const traces = [trace(others, step > 1 ? "Other reaches, a sample" : "Other reaches", muted, 2, 0.35, "circle"),
        trace(known, "On a known run", css("--accent"), 4, 0.95, "circle")];
      if (test.length) traces.push(trace(test, `Test river for ${shortOf(D.mid)}`, css("--warn"), 5, 1, "diamond"));
      const axis = (a) => ({ title: { text: a.label, font: { size: 11, color: fg } }, type: a.log ? "log" : "linear", gridcolor: line, zerolinecolor: line,
        linecolor: line, tickfont: { size: 10, color: muted }, backgroundcolor: surf, showbackground: true });
      const phone = window.innerWidth < 600;
      const layout = { paper_bgcolor: surf, plot_bgcolor: surf, font: { family: css("--font-ui"), color: fg }, height: phone ? 380 : 520,
        margin: { l: 0, r: 0, t: 8, b: 0 }, legend: { orientation: "h", x: 0, y: 1.04, font: { size: 11, color: fg } }, uirevision: `${D.id}|${st.x}|${st.y}|${st.z}`,
        scene: { xaxis: axis(ax[0]), yaxis: axis(ax[1]), zaxis: axis(ax[2]), camera: { eye: phone ? { x: 1.8, y: -1.8, z: 1 } : { x: 1.35, y: -1.35, z: 0.75 } } } };
      if (!drawn) plot.replaceChildren();
      await P.react(plot, traces, layout, { displaylogo: false, responsive: true, modeBarButtonsToRemove: ["toImage"] });
      if (!drawn) {
        plot.on("plotly_click", (ev) => {
          const i = ev.points?.[0]?.customdata, Dn = st.data; if (i == null || !Dn?.S) return;
          const s = Dn.sec.get(i), Rn = Dn.R;
          RF.go("map", { select: s ? { kind: "section", region: Rn.id, id: s.id, name: s.river, section: s.section, grade: s.grade, src: s.src, members: s.m, sec: s }
            : { kind: "reach", region: Rn.id, id: `${Rn.id}:reach:${i}`, seg: i, name: Dn.S.r[i] >= 0 ? Dn.S.rivers[Dn.S.r[i]] : "Unnamed stream" } });
        });
      }
      drawn = true;
      plotNote.textContent = `Drag to turn, pinch or scroll to zoom; tap a point to open it on the map.${step > 1 ? ` Every ${step}th other reach is drawn (${fmt.int(others.length)} of ${fmt.int(D.other.length)}); the table counts them all.` : ""}`;
    }

    const node = el("section", { class: "rf-panel rf-stack", id: "models-sep", "aria-labelledby": "models-sep-h" },
      el("h2", { id: "models-sep-h", text: "Separation: known runs and the other reaches" }),
      el("p", { class: "rf-note", text: "How well single measures, and the model's score, tell reaches on guide sections apart from the other reaches of 2 km² or more in one region. The 3D view shows three measures at once." }),
      regionPick, aucBox, el("div", { class: "rf-row" }, openBtn), plotBox);

    return {
      node,
      model() { st.data = null; update(); },
      region() {
        regionPick.replaceChildren(el("label", { for: "models-sep-region", class: "rf-eyebrow", text: "Region" }), regionSelect("models-sep-region", (id) => setRegion(id, true)));
        st.data = null; update();
      },
      hide() { if (drawn && window.Plotly) { window.Plotly.purge(plot); drawn = false; } },
      show() { if (st.open && !drawn) draw(); },
    };
  }

  // ---------- page ----------
  function jump(id, text) {
    return el("button", { type: "button", class: "rf-btn rf-btn--quiet models-jump", onclick: () => document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" }) }, text);
  }

  function render() {
    root.replaceChildren(el("div", { class: "rf-doc" },
      el("header", { class: "rf-stack" },
        el("span", { class: "rf-eyebrow", text: "Model interface" }),
        el("h1", { text: "Models" }),
        el("p", { class: "rf-note", text: "Which model is in use, what it was trained on, how it does on rivers and catchments it never saw, the run-finder settings that the map and Top rivers use, and how well the measures separate known runs." }),
        mlPage ? el("p", { class: "rf-row" }, el("a", { href: "ml.html", class: "rf-btn models-mlbtn", html: `${RF.util.icon("model")}Open the ML page` }),
          el("span", { class: "rf-note", text: "Cross-validated metrics for every model, data by region and paddling style, feature separation and full documentation." })) : null,
        el("nav", { class: "rf-row models-jumps", "aria-label": "On this page" }, jump("models-reg", "Choose a model"), jump("models-ev", "Held-out results"),
          jump("models-cv", "Cross-validation"), jump("models-rf", "Run finder"), jump("models-sep", "Separation"))),
      panel("models-reg", "Models in this build",
        el("p", { class: "rf-note", text: "Choosing a model here changes the predicted runs on the map and the ranking in Top rivers." }),
        modelsTable()),
      cardPanel(),
      panel("models-ev", "On held-out guide rivers", evalSection()),
      panel("models-cv", "Cross-validated model recipes", cvSection()),
      tuner.node,
      sep.node));
  }

  RF.page("models", {
    title: "Models",
    mount(el_) {
      root = el_;
      view.region = RF.state.region || RF.data.defaultRegion();
      tuner = buildTuner(); sep = buildSep();
      render();
      tuner.sync(true); tuner.region(); sep.region();
      checkMlPage();
      RF.on("model", () => {
        const focused = document.activeElement?.name === "models-current";
        render();
        if (focused) root.querySelector("input[name=models-current]:checked")?.focus();
        tuner.model(); sep.model();
      });
      RF.on("runs", () => tuner.sync());
      // follow the map's region until a region is picked here
      RF.on("region", ({ id }) => { if (!view.regionPicked) setRegion(id, false); });
    },
    /** params.section: the id of a section to scroll to (e.g. "models-rf", the run finder and its ranking). */
    show(params) {
      sep?.show();
      if (params?.section) requestAnimationFrame(() => document.getElementById(params.section)?.scrollIntoView({ block: "start" }));
    },
    hide() { sep?.hide(); },
  });
})();
