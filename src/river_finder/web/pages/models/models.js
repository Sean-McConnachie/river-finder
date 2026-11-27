/* Models page (#models): the ML model interface.
   Owned by the Models agent: see docs/ui/models.md and beads issue rf-aq6.4 (rf-qiw: ML page link and
   style-matched / style x size AUC).
   Sections: the models in this build (choosing one calls RF.setModel, which the map and Top rivers follow), the
   chosen model's card and what it uses (coefficients or gain importance), how the models do on held-out guide
   rivers and in cross-validation, the run-finder settings (RF.setRunParams, with their effect on one region)
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
    return out;
  }

  function summarize(R, runs) {
    const by = { candidate: 0, listed: 0, heldout: 0 }, on = new Set();
    let km = 0;
    runs.forEach((r) => { by[r.status] = (by[r.status] || 0) + 1; km += r.len; r.members.forEach((m) => on.add(m)); });
    const secs = R.sections || [];
    return { n: runs.length, km, by, secN: secs.length, secHit: secs.filter((s) => (s.m || []).some((m) => on.has(m))).length };
  }

  function buildTuner() {
    let cand = clone(RF.state.runParams || RF.runs.defaults()), token = 0;
    const base = new Map();   // `${region}|${model}` → summary with the default settings
    const ctl = {}, outs = {};
    const status = el("p", { class: "rf-note models-inuse", role: "status" });
    const box = el("div", { class: "rf-stack models-preview", "aria-live": "polite" });
    const regionPick = el("div", { class: "models-ctl rf-stack" });
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

    const form = el("div", { class: "rf-stack models-form" },
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
      const ch = changes(RF.state.runParams || cand);
      status.textContent = ch.length ? `In use on the map and in Top rivers: changed from the defaults (${ch.join(", ")}).` : "In use on the map and in Top rivers: the default settings.";
    }

    async function preview() {
      const my = ++token, id = view.region;
      if (!id) { box.replaceChildren(el("p", { class: "rf-note", text: "No regions in this build." })); return; }
      if (!box.firstChild || box.dataset.region !== id) box.replaceChildren(el("p", { class: "rf-note", text: "Finding runs…" }));
      let R, model;
      try {
        R = await RF.data.region(id);
        model = RF.state.model ? await RF.data.scores(id, RF.state.model) : null;
      } catch (e) { if (my === token) box.replaceChildren(el("p", { class: "rf-note", text: e.message || "Could not load the region." })); return; }
      if (my !== token) return;
      await yieldMain();
      if (my !== token) return;
      if (!R.segments) { box.replaceChildren(el("p", { class: "rf-note", text: "This region has no modelled reaches." })); return; }
      const key = `${id}|${RF.state.model}`, p = clone(cand);
      if (!base.has(key)) base.set(key, summarize(R, RF.runs.find(R, { params: RF.runs.defaults(), model })));
      const runs = RF.runs.find(R, { params: p, model }), now = summarize(R, runs), d = base.get(key);
      if (my !== token) return;
      const row = (label, a, b, f = (v) => fmt.int(v)) => el("tr", {}, el("th", { scope: "row", text: label }), el("td", { class: "n", text: f(a) }), el("td", { class: "n rf-muted", text: f(b) }));
      const top = runs.slice(0, 5).map((r) => el("li", { class: "models-run" },
        el("div", { class: "models-run-main" }, el("strong", { text: r.name }),
          el("span", { class: `rf-pill${r.status === "candidate" ? " rf-pill--ok" : r.status === "heldout" ? " rf-pill--warn" : ""}`, text: r.status === "candidate" ? "New lead" : r.status === "heldout" ? "Test river" : "In guides" }),
          el("span", { class: "num rf-muted", text: `${r.len.toFixed(1)} km · ${fmt.grad(r.grad)} m/km · flood ${fmt.flow(r.maf)} m³/s · score ${r.score.toFixed(0)}` })),
        el("button", { type: "button", class: "rf-btn", onclick: () => RF.go("map", { select: r }) }, "Show on map")));
      box.dataset.region = id;
      box.replaceChildren(
        table([th(""), th("These settings", true), th("Defaults", true)], [
          row("Runs", now.n, d.n), row("New leads", now.by.candidate || 0, d.by.candidate || 0), row("In the guides", now.by.listed || 0, d.by.listed || 0),
          row("Test rivers", now.by.heldout || 0, d.by.heldout || 0), row("Total length, km", now.km, d.km, (v) => v.toFixed(0)),
          row("Guide sections touched", now.secHit, d.secHit, (v) => (now.secN ? `${v} of ${now.secN}` : "–"))], "models-cmp"),
        el("p", { class: "rf-note", text: `With ${model && !model.fallback ? shortOf(RF.state.model) : "the pooled regression"} ranking the runs. "Guide sections touched": known sections in this region that at least one run overlaps, a rough check that the settings still find the runs people paddle.` }),
        runs.length ? el("h3", { text: "Best five runs with these settings" }) : null,
        runs.length ? el("ol", { class: "models-runs" }, ...top) : el("p", { class: "rf-note", text: "No runs pass these settings here." }));
    }

    const node = el("section", { class: "rf-panel rf-stack", id: "models-rf", "aria-labelledby": "models-rf-h" },
      el("h2", { id: "models-rf-h", text: "Run finder" }),
      el("p", { class: "rf-note", text: "How the map and Top rivers turn reaches into runs before the model ranks them. Each reach gets three part scores from 0 to 1 (flood flow, gradient, height-to-area); a reach passes when their weighted mean reaches the threshold, and passing reaches in a row make a run. Changes apply when you let go of a slider or leave a box." }),
      status,
      el("div", { class: "rf-row" }, el("button", { type: "button", class: "rf-btn", onclick: () => RF.setRunParams(null) }, "Reset to defaults")),
      form,
      el("h3", { text: "Effect in one region" }),
      regionPick, box);

    return {
      node,
      /** The settings in use changed (here or elsewhere): controls from RF.state.runParams, then the preview. */
      sync(quiet) { cand = clone(RF.state.runParams || RF.runs.defaults()); sync(); if (!quiet) preview(); },
      model() { preview(); },
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
    show() { sep?.show(); },
    hide() { sep?.hide(); },
  });
})();
