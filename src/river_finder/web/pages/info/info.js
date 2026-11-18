/* Info page (#info): project overview and state. Owner: core (beads rf-aq6.6). */
(() => {
  "use strict";
  const RF = window.RF, { el } = RF.util;
  RF.page("info", {
    title: "Info",
    mount(root) {
      root.append(el("div", { class: "rf-doc" },
        el("header", { class: "rf-stack" }, el("span", { class: "rf-eyebrow", text: "About" }), el("h1", { text: "River Finder" }),
          el("p", { class: "rf-note", text: "Finds unexplored whitewater in New Zealand from LINZ terrain, NIWA flood statistics, geology and the river guides. The full overview lands here next." }))));
    },
  });
})();
