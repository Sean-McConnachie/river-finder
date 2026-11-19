/* Top rivers page (#top): connects the map browser and the model interface.
   Owned by the Top-rivers agent: see docs/ui/top-rivers.md and beads issue rf-aq6.5. Ranking, paging
   (20 at a time, more on scroll) and the phone layout work; the rest of the brief is still open.
   Only edit files under src/river_finder/web/pages/top/. */
(() => {
  "use strict";
  const RF = window.RF, { el, fmt } = RF.util;
  const PAGE = 20;   // rows shown first, then added each time the end of the list comes into view
  let root = null, list = null, regionSel = null, token = 0, io = null, rewind = false;

  // Columns: header shown on desktop, header spoken (data-label), cell class. Below 720 px top.css turns
  // each row into a two-line list item; the headers stay in the accessibility tree (visually hidden).
  const COLS = [["#", "Rank", "n top-rank"], ["River", "River", "top-name"], ["km", "km", "n top-km"], ["m/km", "m/km", "n top-grad"],
    ["Flood m³/s", "Flood m³/s", "n top-flood"], ["Score", "Score", "n top-score"], ["", "Actions", "top-act"]];
  /** One cell of column k (explicit role: some browsers drop table semantics once the display changes). */
  const cell = (k, ...kids) => el("td", { class: COLS[k][2], role: "cell", "data-label": COLS[k][1] }, ...kids);
  const head = () => el("thead", { role: "rowgroup" }, el("tr", { role: "row" }, ...COLS.map(([h, label, c]) =>
    el("th", { class: c.startsWith("n") ? "n" : null, role: "columnheader", scope: "col" },
      h === label ? h : [h ? el("span", { "aria-hidden": "true", text: h }) : null, el("span", { class: "top-vh", text: label })]))));

  /** Stop watching the end of the list (before each re-render, so an old observer never fires). */
  function unwatch() { io?.disconnect(); io = null; }

  function row(r, i) {
    return el("tr", { role: "row" },
      cell(0, String(i + 1)),
      cell(1, el("strong", { text: r.name }), " ",
        el("span", { class: `rf-pill ${r.status === "listed" ? "" : r.status === "heldout" ? "rf-pill--warn" : "rf-pill--ok"}`, text: r.status === "listed" ? "In guides" : r.status === "heldout" ? "Test river" : "Not in guides" })),
      cell(2, r.len.toFixed(1)), cell(3, fmt.grad(r.grad)), cell(4, fmt.flow(r.maf)), cell(5, r.score.toFixed(0)),
      cell(6, el("div", { class: "rf-row", style: "flex-wrap:nowrap" },
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
      const tb = el("tbody", { role: "rowgroup" });
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
      list.replaceChildren(el("div", { class: "rf-tablewrap" }, el("table", { class: "rf-table top-table", role: "table" }, head(), tb)), end);
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
        el("p", { class: "rf-note", text: "The best-scoring rivers for the current model, region by region. Each one opens on the map or in full detail." })),
      regions.length ? el("section", { class: "rf-panel rf-stack" },
        el("div", { class: "rf-row" }, el("label", { for: "top-region", text: "Region" }), regionSel),
        list) : el("div", { class: "rf-empty", text: "No regions in this build. Run the pipeline, then river-finder web build." })));
    if (regions.length) refresh();
  }

  RF.page("top", {
    title: "Top rivers",
    mount(el_) { root = el_; render(); RF.on("model", refresh); RF.on("runs", refresh); },
    show() { if (rewind) { rewind = false; root.scrollTop = 0; } },
    hide() {},
  });
})();
