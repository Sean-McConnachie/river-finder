/* Models page (#models): the ML model interface.
   PLACEHOLDER. Owned by the Models agent: see docs/ui/models.md and beads issue rf-aq6.4.
   Everything below is a starting point that shows the RF contract working; replace it freely.
   Only edit files under src/river_finder/web/pages/models/. */
(() => {
  "use strict";
  const RF = window.RF, { el, esc } = RF.util;
  let root = null;

  function modelsTable() {
    const models = RF.models.list();
    if (!models.length) return el("div", { class: "rf-empty" }, el("strong", { text: "No model registry in this build." }),
      el("span", { text: "Run `river-finder models viewer` (see docs/PLAN.md) to include models/*.json." }));
    const tb = el("tbody");
    models.slice().reverse().forEach((m) => {
      const cur = m.id === RF.state.model;
      tb.append(el("tr", {},
        el("td", {}, el("label", { class: "rf-row" },
          el("input", { type: "radio", name: "models-current", value: m.id, checked: cur, onchange: () => RF.setModel(m.id) }),
          el("strong", { text: m.title || m.id }))),
        el("td", { class: "num", text: m.id }), el("td", { text: m.kind || "" }),
        el("td", { class: "n", text: (m.train_rivers || []).length }), el("td", { class: "n", text: (m.test_rivers || []).length }),
        el("td", { class: "num", text: (m.created || "").slice(0, 10) })));
    });
    return el("div", { class: "rf-tablewrap" }, el("table", { class: "rf-table" },
      el("thead", {}, el("tr", {}, ...["Model", "Id", "Kind", "Train rivers", "Test rivers", "Created"].map((h, i) => el("th", { class: i === 3 || i === 4 ? "n" : null, text: h })))),
      tb));
  }

  function render() {
    const p = RF.state.runParams || {};
    const ev = RF.models.evaluations;
    root.replaceChildren(el("div", { class: "rf-doc" },
      el("header", { class: "rf-stack" },
        el("span", { class: "rf-eyebrow", text: "Model interface" }),
        el("h1", { text: "Models" }),
        el("p", { class: "rf-note", text: "Compare and tune the runnability models: which model is in use, what it was trained on, how it does on rivers it never saw, and the run-finder settings that the map and Top rivers use." }),
        el("span", { class: "rf-status", text: "Placeholder: this page is being built" })),
      el("section", { class: "rf-panel rf-stack", "aria-labelledby": "models-reg-h" },
        el("h2", { id: "models-reg-h", text: "Models in this build" }),
        el("p", { class: "rf-note", text: "Choosing a model here changes the predicted runs on the map and the ranking in Top rivers (RF.setModel)." }),
        modelsTable()),
      el("div", { class: "rf-grid" },
        el("section", { class: "rf-panel rf-stack" },
          el("h2", { text: "Run-finder settings in use" }),
          el("dl", { class: "rf-kv" },
            el("dt", { text: "Flood flow, full credit" }), el("dd", { class: "num", text: p.q ? `${p.q[0]}–${p.q[1]} m³/s` : "–" }),
            el("dt", { text: "Gradient, full credit" }), el("dd", { class: "num", text: p.g ? `${p.g[0]}–${p.g[1]} m/km` : "–" }),
            el("dt", { text: "Height-to-area measure" }), el("dd", { text: p.haMeasure || "–" }),
            el("dt", { text: "Weights (flow, gradient, H/A)" }), el("dd", { class: "num", text: p.w ? p.w.join(", ") : "–" }),
            el("dt", { text: "Threshold" }), el("dd", { class: "num", text: p.threshold ?? "–" }),
            el("dt", { text: "Minimum run length" }), el("dd", { class: "num", text: p.minLen != null ? `${p.minLen} km` : "–" })),
          el("div", { class: "rf-row" }, el("button", { type: "button", class: "rf-btn", onclick: () => RF.setRunParams(null) }, "Reset to defaults"))),
        el("section", { class: "rf-panel rf-stack" },
          el("h2", { text: "Evaluations" }),
          el("p", { class: "rf-note", html: ev ? `${esc((ev.evaluations || []).length)} evaluation rows over ${esc((ev.splits || []).length)} split(s) are in <span class="num">RF.models.evaluations</span>.` : "No evaluations in this build." }))),
      el("section", { class: "rf-panel rf-stack" },
        el("h2", { text: "For the agent building this page" }),
        el("p", { class: "rf-note", html: "Brief: <span class=\"num\">docs/ui/models.md</span>. Contract: <span class=\"num\">src/river_finder/web/README.md</span>. Edit only <span class=\"num\">src/river_finder/web/pages/models/</span>. The legacy viewer's run-finder sliders, 3D separation and Models views live in <span class=\"num\">src/river_finder/assets/viewer_template.html</span> for reference." }))));
  }

  RF.page("models", {
    title: "Models",
    mount(el_) { root = el_; render(); RF.on("model", render); RF.on("runs", render); },
    show() {},
    hide() {},
  });
})();
