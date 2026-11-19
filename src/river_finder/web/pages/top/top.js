/* Top rivers page (#top): connects the map browser and the model interface.
   PLACEHOLDER. Owned by the Top-rivers agent: see docs/ui/top-rivers.md and beads issue rf-aq6.5.
   Everything below is a starting point that shows the RF contract working; replace it freely.
   Only edit files under src/river_finder/web/pages/top/. */
(() => {
  "use strict";
  const RF = window.RF, { el, fmt } = RF.util;
  const PAGE = 20;   // rows shown first, then added each time the end of the list comes into view
  let root = null, list = null, regionSel = null, token = 0, io = null, rewind = false;

  /** Stop watching the end of the list (before each re-render, so an old observer never fires). */
  function unwatch() { io?.disconnect(); io = null; }

  function row(r, i) {
    return el("tr", {},
      el("td", { class: "n", text: i + 1 }),
      el("td", {}, el("strong", { text: r.name }), " ",
        el("span", { class: `rf-pill ${r.status === "listed" ? "" : r.status === "heldout" ? "rf-pill--warn" : "rf-pill--ok"}`, text: r.status === "listed" ? "In guides" : r.status === "heldout" ? "Test river" : "Not in guides" })),
      el("td", { class: "n", text: r.len.toFixed(1) }), el("td", { class: "n", text: fmt.grad(r.grad) }),
      el("td", { class: "n", text: fmt.flow(r.maf) }), el("td", { class: "n", text: r.score.toFixed(0) }),
      el("td", {}, el("div", { class: "rf-row", style: "flex-wrap:nowrap" },
        el("button", { type: "button", class: "rf-btn", onclick: () => RF.go("map", { select: r }) }, "Map"),
        el("button", { type: "button", class: "rf-btn", onclick: (e) => RF.detail.open(r, { opener: e.currentTarget }) }, "Details"))));
  }

  async function refresh() {
    const id = regionSel?.value; if (!id) return;
    const my = ++token;
    unwatch();
    list.replaceChildren(el("p", { class: "rf-muted", text: "Loading the region…" }));
    try {
      const R = await RF.data.region(id);
      const model = RF.state.model ? await RF.data.scores(id, RF.state.model) : null;
      if (my !== token) return;
      const runs = RF.runs.find(R, { model }), total = runs.length;
      if (!total) { list.replaceChildren(el("div", { class: "rf-empty", text: "No runs pass the current run-finder settings in this region." })); return; }
      const tb = el("tbody");
      const count = el("p", { class: "rf-note", "aria-live": "polite" });
      const more = el("button", { type: "button", class: "rf-btn", onclick: () => {
        const first = next(); if (first) { first.tabIndex = -1; first.focus(); }   // keyboard and screen readers continue at the new rows
      }, onblur: () => watch() });
      const end = el("div", { class: "rf-row top-end" }, count, more);   // sentinel: the end of the list
      let shown = 0;
      /** (Re)start watching the end: observe() reports at once, so an end still in view loads again. */
      const watch = () => { if (io && my === token) { io.unobserve(end); io.observe(end); } };
      /** Append the next PAGE rows (ranks continue); returns the first new row. */
      function next() {
        if (my !== token || shown >= total) return null;
        const from = shown; shown = Math.min(shown + PAGE, total);
        runs.slice(from, shown).forEach((r, k) => tb.append(row(r, from + k)));
        const left = total - shown;
        count.textContent = left ? `Showing ${fmt.int(shown)} of ${fmt.int(total)} runs` : `Showing all ${fmt.int(total)} run${total === 1 ? "" : "s"}`;
        more.textContent = `Show ${Math.min(PAGE, left)} more`;
        more.hidden = !left;
        if (left) watch(); else unwatch();
        return tb.children[from];
      }
      next();
      list.replaceChildren(el("div", { class: "rf-tablewrap" }, el("table", { class: "rf-table top-table" },
        el("thead", {}, el("tr", {}, ...[["#", "n"], ["River"], ["km", "n"], ["m/km", "n"], ["Flood m³/s", "n"], ["Score", "n"], [""]].map(([h, c]) => el("th", { class: c || null, text: h })))),
        tb)), end);
      // A new list starts at the top, so the end is not already in view from the old scroll position.
      if (root.hidden) rewind = true; else root.scrollTop = 0;
      if (shown < total && "IntersectionObserver" in window) {
        // Not while "Show more" has focus: a keyboard user who tabs to it loads with it, and moves on to the new rows.
        io = new IntersectionObserver((entries) => {
          if (my === token && document.activeElement !== more && entries.some((e) => e.isIntersecting)) next();
        }, { root, rootMargin: "0px 0px 200px 0px" });
        io.observe(end);
      }
    } catch (e) { list.replaceChildren(el("p", { class: "rf-note", text: e.message })); }
  }

  function render() {
    const regions = RF.data.regions();
    regionSel = el("select", { class: "rf-select", id: "top-region", onchange: refresh },
      ...regions.map((r) => el("option", { value: r.id, text: r.title, selected: r.id === (RF.state.region || RF.data.defaultRegion()) })));
    list = el("div", { class: "rf-stack" });
    root.replaceChildren(el("div", { class: "rf-doc" },
      el("header", { class: "rf-stack" },
        el("span", { class: "rf-eyebrow", text: "Ranking" }),
        el("h1", { text: "Top rivers" }),
        el("p", { class: "rf-note", text: "The best-scoring rivers for the current model, region by region. Each one opens on the map or in full detail." }),
        el("span", { class: "rf-status", text: "Placeholder: this page is being built" })),
      regions.length ? el("section", { class: "rf-panel rf-stack" },
        el("div", { class: "rf-row" }, el("label", { for: "top-region", text: "Region" }), regionSel),
        list) : el("div", { class: "rf-empty", text: "No regions in this build. Run the pipeline, then river-finder web build." }),
      el("section", { class: "rf-panel rf-stack" },
        el("h2", { text: "For the agent building this page" }),
        el("p", { class: "rf-note", html: "Brief: <span class=\"num\">docs/ui/top-rivers.md</span>. Contract: <span class=\"num\">src/river_finder/web/README.md</span>. Edit only <span class=\"num\">src/river_finder/web/pages/top/</span>." }))));
    if (regions.length) refresh();
  }

  RF.page("top", {
    title: "Top rivers",
    mount(el_) { root = el_; render(); RF.on("model", refresh); RF.on("runs", refresh); },
    show() { if (rewind) { rewind = false; root.scrollTop = 0; } },
    hide() {},
  });
})();
