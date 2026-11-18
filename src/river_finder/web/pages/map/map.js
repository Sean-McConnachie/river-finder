/* Map page (#map): the interactive map browser. Owner: core/map agent (beads rf-aq6.7 onwards).
   Temporary stand-in until the MapLibre map lands. */
(() => {
  "use strict";
  const RF = window.RF, { el } = RF.util;
  let root = null, note = null;

  RF.page("map", {
    title: "Map",
    mount(el_) {
      root = el_;
      root.dataset.scroll = "auto";
      note = el("p", { class: "rf-note" });
      root.append(el("div", { class: "rf-doc" },
        el("header", { class: "rf-stack" }, el("span", { class: "rf-eyebrow", text: "Map browser" }), el("h1", { text: "Map" }),
          el("p", { class: "rf-note", text: "The interactive map is being rebuilt on MapLibre (base maps, layers, river details). It lands here next." })),
        el("section", { class: "rf-panel rf-stack" }, el("h2", { text: "Regions in this build" }),
          el("ul", {}, ...RF.data.regions().map((r) => el("li", { text: `${r.title} (${r.group})` }))), note)));
    },
    show(params) {
      if (params?.select) note.textContent = `Requested on the map: ${params.select.name || params.select.kind} (${params.select.region}).`;
    },
  });
})();
