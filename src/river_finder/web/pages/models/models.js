/* Models page (#models): the ML model interface.
   Owned by the Models agent: see docs/ui/models.md and beads issue rf-aq6.4 (rf-qiw: ML page link and
   style-matched / style x size AUC). Still to come: run-finder tuning and the separation views.
   Only edit files under src/river_finder/web/pages/models/. */
(() => {
  "use strict";
  const RF = window.RF, { el, esc } = RF.util;
  let root = null;
  let mlPage = null;   // true / false once checked: is ml.html published next to this page?
  const view = { split: null, coords: "any" };

  const F2 = (v) => (v == null || !isFinite(v) ? "–" : (+v).toFixed(2));
  const F3 = (v) => (v == null || !isFinite(v) ? "–" : (+v).toFixed(3));
  const PCT = (v) => (v == null || !isFinite(v) ? "–" : `${Math.round(100 * v)}%`);
  const KIND = { logistic: "Logistic", gbm: "LightGBM", baseline: "Baseline" };
  // the CV summary rides in the build payload next to the cards (models.viewer_models -> "cv")
  const CV = () => (RF.payload.registry || {}).cv || null;

  function checkMlPage() {
    if (mlPage !== null || !window.fetch) return;
    fetch("ml.html", { method: "HEAD" }).then((r) => { mlPage = r.ok; }).catch(() => { mlPage = false; })
      .finally(() => { if (root) render(); });
  }

  function mlLink(text) {
    return mlPage ? el("a", { href: "ml.html", class: "models-ml" }, text) : el("span", { class: "models-ml" }, text);
  }

  function th(label, num, title) { return el("th", { class: num ? "n" : null, title: title || null, scope: "col", text: label }); }

  function table(head, rows) {
    return el("div", { class: "rf-tablewrap" }, el("table", { class: "rf-table" }, el("thead", {}, el("tr", {}, ...head)), el("tbody", {}, ...rows)));
  }

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

  function evalSection() {
    const sp = splits();
    if (!evalRows().length || !sp.length) return el("p", { class: "rf-note", text: "No evaluations in this build (run `river-finder models evaluate`)." });
    if (!view.split || !sp.some((s) => s.name === view.split)) view.split = (sp.find((s) => s.name !== "reference") || sp[0]).name;
    const pick = (id, label, opts, value, onchange) => el("label", { class: "rf-stack models-ctl", for: id },
      el("span", { class: "rf-eyebrow", text: label }),
      el("select", { id, class: "rf-select", onchange }, ...opts.map(([v, t]) => el("option", { value: v, selected: v === value, text: t }))));
    const controls = el("div", { class: "rf-row models-ctls" },
      pick("models-split", "Test set", sp.map((s) => [s.name, s.name === "reference" ? "Reference: the Mangakarengorengo only" : `Guide research: ${s.test.length} held-out rivers (${s.name})`]),
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
    return el("div", { class: "rf-stack" }, controls,
      table([th("Model"), th("Test rivers", true), th("AUC", true), th("Size-matched", true), th("Style-matched", true),
        th("Style × size", true), th("Top 10%", true, "Share of test reaches in the model's top 10%"), th("Median pct", true)], rows),
      el("p", { class: "rf-note", html: `Each model ranks every reach of 2&nbsp;km² or more that is not on a known run (the background, which still holds unknown runs, so these are lower bounds). Test reaches should rank high. <b>AUC</b>: chance a test reach outranks a random background reach. <b>Size-matched</b>: only against background reaches of similar flood size. <b>Style-matched</b>: only against the same paddling style (Montgomery &amp; Buffington reach type from slope and confinement), so preferring steep creeks earns nothing. <b>Style × size</b>: same style and same flood quintile, so neither "steep" nor "big" earns anything. (−n): test rivers left out because that model trained on them.${s.strata ? ` Split: ${esc(s.strata)}, seed ${esc(s.seed ?? "fixed")}.` : ""}` }),
      el("p", { class: "rf-note", text: "This split holds out whole rivers, not whole catchments, so a test river can share its catchment with training rivers. That flatters flexible models; the cross-validation below holds out whole catchments and is the more honest comparison." }));
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

  function render() {
    const p = RF.state.runParams || {};
    root.replaceChildren(el("div", { class: "rf-doc" },
      el("header", { class: "rf-stack" },
        el("span", { class: "rf-eyebrow", text: "Model interface" }),
        el("h1", { text: "Models" }),
        el("p", { class: "rf-note", text: "Which model is in use, what it was trained on, how it does on rivers and catchments it never saw, and the run-finder settings that the map and Top rivers use." }),
        mlPage ? el("p", { class: "rf-row" }, el("a", { href: "ml.html", class: "rf-btn models-mlbtn", html: `${RF.util.icon("model")}Open the ML page` }),
          el("span", { class: "rf-note", text: "Cross-validated metrics for every model, data by region and paddling style, feature separation and full documentation." })) : null,
        el("span", { class: "rf-status", text: "In progress: run-finder tuning and separation views are still to come" })),
      el("section", { class: "rf-panel rf-stack", "aria-labelledby": "models-reg-h" },
        el("h2", { id: "models-reg-h", text: "Models in this build" }),
        el("p", { class: "rf-note", text: "Choosing a model here changes the predicted runs on the map and the ranking in Top rivers (RF.setModel)." }),
        modelsTable()),
      el("section", { class: "rf-panel rf-stack", "aria-labelledby": "models-ev-h" },
        el("h2", { id: "models-ev-h", text: "On held-out guide rivers" }),
        evalSection()),
      el("section", { class: "rf-panel rf-stack", "aria-labelledby": "models-cv-h" },
        el("h2", { id: "models-cv-h", text: "Cross-validated model recipes" }),
        cvSection()),
      el("section", { class: "rf-panel rf-stack" },
        el("h2", { text: "Run-finder settings in use" }),
        el("dl", { class: "rf-kv" },
          el("dt", { text: "Flood flow, full credit" }), el("dd", { class: "num", text: p.q ? `${p.q[0]}–${p.q[1]} m³/s` : "–" }),
          el("dt", { text: "Gradient, full credit" }), el("dd", { class: "num", text: p.g ? `${p.g[0]}–${p.g[1]} m/km` : "–" }),
          el("dt", { text: "Height-to-area measure" }), el("dd", { text: p.haMeasure || "–" }),
          el("dt", { text: "Weights (flow, gradient, H/A)" }), el("dd", { class: "num", text: p.w ? p.w.join(", ") : "–" }),
          el("dt", { text: "Threshold" }), el("dd", { class: "num", text: p.threshold ?? "–" }),
          el("dt", { text: "Minimum run length" }), el("dd", { class: "num", text: p.minLen != null ? `${p.minLen} km` : "–" })),
        el("div", { class: "rf-row" }, el("button", { type: "button", class: "rf-btn", onclick: () => RF.setRunParams(null) }, "Reset to defaults")))));
  }

  RF.page("models", {
    title: "Models",
    mount(el_) { root = el_; render(); checkMlPage(); RF.on("model", render); RF.on("runs", render); },
    show() {},
    hide() {},
  });
})();
