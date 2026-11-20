/* Map panel: region picker, base maps, the layer tree (indented, GIS style, per-layer colours) and
   the legend. A side panel on desktop, a bottom sheet on phones. Owner: map agent.

   Layer files register nodes with M.defineGroup / M.defineLayer:
     M.defineLayer({id, label, parent, colors: [{key, token | hex, label}], defaultOn, swatch: "line" | "dot" | "area",
                    apply(on, colorOf), count(), note (text, or a function → text), link, legend,
                    legendKey() → Node (legend key of a layer without colours), rowKey() → Node (key in its tree row),
                    legendBlock() → Node | null (a whole legend block instead of a row; mini: true also puts it in the
                    mini legend), controls() → Node (shown under the row while the node is on)})
   M.layerNotes: short notes under the tree (e.g. a layer this host cannot show).
   A layer shows when it and every group above it are on (as in QGIS). */
(() => {
  "use strict";
  const RF = window.RF, M = RF.mapkit, { el, icon, fmt } = RF.util;
  M.nodes = new Map();
  M.order = [];
  M.layerNotes = [];
  const noteOf = (n) => (typeof n.note === "function" ? n.note() : n.note);

  M.defineGroup = (g) => { const n = Object.assign({ type: "group", defaultOn: true, colors: [] }, g); M.nodes.set(n.id, n); M.order.push(n.id); return n; };
  M.defineLayer = (l) => { const n = Object.assign({ type: "layer", defaultOn: false, colors: [], swatch: "line" }, l); M.nodes.set(n.id, n); M.order.push(n.id); return n; };
  M.isOn = (id) => { const n = M.nodes.get(id); if (!n) return false; const p = M.prefs.layers[id]; return p === undefined ? n.defaultOn : p; };
  M.isVisible = (id) => { let n = M.nodes.get(id); while (n) { if (!M.isOn(n.id)) return false; n = n.parent ? M.nodes.get(n.parent) : null; } return true; };
  /** Resolved colour of a layer's colour slot (user choice, else the token for the current theme). */
  M.colorOf = (id, key = "c") => {
    const n = M.nodes.get(id), user = M.prefs.colors[`${id}.${key}`];
    if (user) return user;
    const c = n?.colors.find((x) => x.key === key) || n?.colors[0];
    if (!c) return "#888888";
    return c.token ? M.token(c.token) : c.hex;
  };
  const children = (id) => M.order.map((k) => M.nodes.get(k)).filter((n) => (n.parent || null) === id);
  const descendants = (id) => children(id).flatMap((c) => [c, ...descendants(c.id)]);

  /** Re-apply every layer's visibility and colours (after toggles, colour edits, theme changes). */
  M.applyLayers = () => {
    if (!M.ready) return;
    M.order.forEach((id) => {
      const n = M.nodes.get(id);
      if (n.type !== "layer" || !n.apply) return;
      try { n.apply(M.isVisible(id), (key) => M.colorOf(id, key)); } catch (e) { console.error(`layer ${id}`, e); }
    });
    M.renderLegend();
  };
  M.onTheme(() => { M.applyLayers(); M.renderTree(); });

  // ---------- DOM ----------
  let panel, body, treeEl, baseEl, legendEl, regionSel, tabs = {};

  function regionPicker() {
    const groups = {};
    RF.data.regions().forEach((r) => { (groups[r.group] ||= []).push(r); });
    regionSel = el("select", { class: "rf-select map-region", id: "map-region", "aria-label": "Region" },
      ...Object.entries(groups).map(([g, rs]) => el("optgroup", { label: g }, ...rs.map((r) => el("option", { value: r.id, text: r.title })))));
    regionSel.addEventListener("change", () => M.openRegion(regionSel.value, { fit: true }));
    return regionSel;
  }
  M.syncRegionPicker = () => { if (regionSel && M.R) regionSel.value = M.R.id; const chip = document.querySelector(".map-chip-label"); if (chip && M.R) chip.textContent = M.R.title; };

  function slider({ id, label, min, max, step, value, fmtv, oninput }) {
    const out = el("output", { class: "num", for: id, text: fmtv(value) });
    const input = el("input", { type: "range", id, min, max, step, value });
    input.addEventListener("input", () => { out.textContent = fmtv(+input.value); oninput(+input.value); });
    return el("div", { class: "map-slider" }, el("div", { class: "map-slider-row" }, el("label", { for: id, text: label }), out), input);
  }

  M.renderBasemapControls = () => {
    if (!baseEl) return;
    const cur = M.prefs.basemap;
    const list = el("div", { class: "map-basemaps", role: "radiogroup", "aria-label": "Base map" });
    M.BASEMAPS.filter((b) => !M.basemapHidden(b)).forEach((b) => {
      const ok = M.basemapAvailable(b);
      const btn = el("button", { type: "button", role: "radio", class: "map-basemap", "aria-checked": String(cur === b.id), disabled: !ok,
        title: ok ? b.note : `${b.note}: available on the standalone site (this host blocks map tiles)`, dataset: { id: b.id } },
      el("span", { class: `map-basemap-thumb map-thumb-${b.id}`, "aria-hidden": "true" }), el("span", { text: b.label }));
      btn.addEventListener("click", () => M.setBasemap(b.id));
      list.append(btn);
    });
    const parts = [list];
    if (!M.basemapAvailable({ site: true })) parts.push(el("p", { class: "rf-note", text: "Topographic maps need the standalone site: this host blocks map tile servers. Satellite here is a pre-rendered image of the region." }));
    if (cur.startsWith("catch")) {
      const s = M.catchSettings();
      // smallest coloured area: 2e-5 km² (every cell) to 100 km², log slider
      const LO = Math.log10(2e-5), SPAN = 2 - LO;
      const logToS = (v) => Math.round((Math.log10(v) - LO) / SPAN * 1000), sToLog = (x) => 10 ** (LO + x / 1000 * SPAN);
      parts.push(el("div", { class: "map-catch-ctl" },
        slider({ id: "map-catch-min", label: "Colour areas from", min: 0, max: 1000, step: 1, value: logToS(s.minKm2), fmtv: (x) => (x <= 2 ? "All land" : M.fmtArea(+sToLog(x).toPrecision(2))), oninput: (x) => M.setCatch({ minKm2: x <= 2 ? 2e-5 : sToLog(x) }) }),
        cur === "catch" ? slider({ id: "map-catch-max", label: "Colour scale up to", min: 0, max: 1000, step: 1, value: Math.round((Math.log10(s.linMax)) / 4 * 1000), fmtv: (x) => `${M.nice(10 ** (x / 1000 * 4))} km²`, oninput: (x) => M.setCatch({ linMax: Math.max(10 ** (x / 1000 * 4), 0.1) }) }) : null,
        slider({ id: "map-catch-op", label: "Opacity", min: 0, max: 100, step: 1, value: Math.round(s.opacity * 100), fmtv: (x) => `${x}%`, oninput: (x) => M.setCatch({ opacity: x / 100 }) }),
        el("label", { class: "map-check" }, el("input", { type: "checkbox", checked: s.fadeTrunc, onchange: (e) => M.setCatch({ fadeTrunc: e.target.checked }) }), "Fade catchments cut off by the region edge")));
    }
    baseEl.replaceChildren(...parts.filter(Boolean));
  };

  function swatchButton(n, c) {
    const color = M.colorOf(n.id, c.key);
    const input = el("input", { type: "color", value: /^#[0-9a-f]{6}$/i.test(color) ? color : "#888888", "aria-label": `Colour for ${n.label}${c.label ? `, ${c.label}` : ""}` });
    input.addEventListener("input", () => { M.prefs.colors[`${n.id}.${c.key}`] = input.value; M.savePrefs(); M.applyLayers(); wrap.style.setProperty("--sw", input.value); });
    const wrap = el("label", { class: `map-swatch map-swatch--${n.swatch}`, title: `Change colour${c.label ? ` (${c.label})` : ""}`, style: `--sw:${color}` }, input);
    return wrap;
  }

  function nodeRow(n, depth) {
    const on = M.isOn(n.id), kids = children(n.id);
    const cb = el("input", { type: "checkbox", id: `lyr-${n.id}`, checked: on });
    if (n.type === "group" && kids.length) {
      const all = descendants(n.id).filter((d) => d.type === "layer"), onCount = all.filter((d) => M.isOn(d.id)).length;
      cb.indeterminate = on && onCount > 0 && onCount < all.length;
    }
    cb.addEventListener("change", () => { M.prefs.layers[n.id] = cb.checked; M.savePrefs(); M.applyLayers(); M.renderTree(); });
    const count = n.count ? n.count() : null, note = noteOf(n);
    const row = el("div", { class: `map-node map-node--${n.type}`, style: `--depth:${depth}`, "data-off": String(!M.isVisible(n.id)) },
      cb, el("label", { for: `lyr-${n.id}`, class: "map-node-label" }, el("span", { text: n.label }),
        note ? el("span", { class: "map-node-note", dataset: { nodeNote: n.id }, text: note }) : null),
      count !== null && count !== undefined ? el("span", { class: "map-node-count num", text: fmt.int(count) }) : null,
      el("span", { class: "map-node-swatches" }, ...n.colors.map((c) => swatchButton(n, c)), n.rowKey ? n.rowKey() : null),
      n.link ? el("a", { class: "map-node-link", href: n.link, target: "_blank", rel: "noopener", "aria-label": `Source for ${n.label}`, html: icon("external") }) : null);
    const ctl = n.controls && M.isVisible(n.id) ? n.controls() : null;
    return [row, ...(ctl ? [el("div", { class: "map-node-ctl", style: `--depth:${depth}` }, ctl)] : []), ...kids.flatMap((k) => nodeRow(k, depth + 1))];
  }

  M.renderTree = () => {
    if (!treeEl) return;
    const roots = children(null);
    treeEl.replaceChildren(...roots.flatMap((n) => nodeRow(n, 0)), ...M.layerNotes.map((t) => el("p", { class: "rf-note map-tree-note", text: t })));
  };
  /** Update a node's note in place (keeps focus in the tree). */
  M.updateNote = (id) => { const n = M.nodes.get(id), e = treeEl?.querySelector(`[data-node-note="${id}"]`); if (n && e) e.textContent = noteOf(n) || ""; };

  M.renderLegend = () => {
    if (!legendEl) return;
    const blocks = [];
    const bl = M.basemapLegend?.(); if (bl) blocks.push(bl);
    const minis = [];
    M.order.map((id) => M.nodes.get(id)).filter((n) => n.type === "layer" && M.isVisible(n.id) && (n.colors.length || n.legendKey || n.legendBlock)).forEach((n) => {
      if (n.legendBlock) {
        const b = n.legendBlock(); if (b) blocks.push(b);
        const m = n.mini && n.legendBlock(); if (m) minis.push(m);
        return;
      }
      blocks.push(el("div", { class: "map-legend-row" },
        el("span", { class: "map-node-swatches" }, ...(n.legendKey ? [n.legendKey()]
          : n.colors.map((c) => el("span", { class: `map-key map-key--${n.swatch}`, style: `--sw:${M.colorOf(n.id, c.key)}`, title: c.label || "" })))),
        el("span", { text: n.legend || n.label })));
    });
    const b = M.BASEMAPS.find((x) => x.id === M.prefs.basemap);
    blocks.push(el("p", { class: "rf-note", text: `Base map: ${b ? b.label : "–"}. ${b?.note || ""}` }));
    legendEl.replaceChildren(...blocks);
    const mini = document.querySelector(".map-minilegend");
    if (mini) { const x = M.basemapLegend?.(), all = [...(x ? [x] : []), ...minis]; mini.replaceChildren(...all); mini.hidden = !all.length; }
  };
  M.drawCatchLegend = () => M.renderLegend();

  function modelPicker() {
    const models = RF.models.list();
    if (!models.length) return null;
    const sel = el("select", { class: "rf-select", id: "map-model" },
      ...models.slice().reverse().map((m) => el("option", { value: m.id, text: m.title || m.id, selected: m.id === RF.state.model })));
    sel.addEventListener("change", () => RF.setModel(sel.value));
    RF.on("model", ({ id }) => { sel.value = id; });
    return el("div", { class: "map-model" }, el("label", { for: "map-model", text: "Model for predicted runs" }), sel,
      el("button", { type: "button", class: "rf-btn rf-btn--quiet map-model-more", onclick: () => RF.go("models") }, "About the models"));
  }

  function setTab(name) {
    Object.entries(tabs).forEach(([k, t]) => { t.btn.setAttribute("aria-selected", String(k === name)); t.pane.hidden = k !== name; });
    M.prefs.tab = name; M.savePrefs();
  }

  M.openPanel = (open) => {
    if (!panel) return;
    if (open === undefined) open = panel.hasAttribute("data-collapsed");
    panel.toggleAttribute("data-collapsed", !open);
    document.querySelector(".map-layers-btn")?.setAttribute("aria-expanded", String(open));
    if (!M.isPhone()) { M.prefs.panelOpen = open; M.savePrefs(); }
    if (open && M.isPhone()) panel.querySelector(".map-panel-close")?.focus({ preventScroll: true, focusVisible: false });
  };

  /** Build the panel inside the map root. */
  M.buildPanel = (root) => {
    treeEl = el("div", { class: "map-tree", role: "group", "aria-label": "Layers" });
    baseEl = el("div", { class: "map-base" });
    legendEl = el("div", { class: "map-legend" });
    const layersPane = el("div", { class: "map-pane", role: "tabpanel", id: "map-pane-layers" },
      el("section", { class: "map-sec" }, el("h3", { class: "rf-eyebrow", text: "Base map" }), baseEl),
      el("section", { class: "map-sec" }, el("h3", { class: "rf-eyebrow", text: "Layers" }), treeEl, modelPicker(),
        el("button", { type: "button", class: "rf-btn rf-btn--quiet map-reset", onclick: () => { M.prefs.colors = {}; M.savePrefs(); M.applyLayers(); M.renderTree(); RF.util.toast("Colours reset"); } }, "Reset colours")));
    const legendPane = el("div", { class: "map-pane", role: "tabpanel", id: "map-pane-legend", hidden: true }, legendEl);
    const tabBar = el("div", { class: "map-tabs", role: "tablist" });
    [["layers", "Layers", layersPane], ["legend", "Legend", legendPane]].forEach(([k, label, pane]) => {
      const btn = el("button", { type: "button", role: "tab", "aria-controls": pane.id, "aria-selected": "false" }, label);
      btn.addEventListener("click", () => setTab(k));
      tabs[k] = { btn, pane }; tabBar.append(btn);
    });
    body = el("div", { class: "map-panel-body" }, layersPane, legendPane);
    panel = el("aside", { class: "map-panel", "aria-label": "Map layers and legend" },
      el("div", { class: "map-panel-grip", "aria-hidden": "true" }),
      el("div", { class: "map-panel-head" }, regionPicker(),
        el("button", { type: "button", class: "rf-btn rf-btn--quiet rf-iconbtn map-panel-close", "aria-label": "Hide the panel", html: icon("close"), onclick: () => M.openPanel(false) })),
      tabBar, body);
    root.append(panel);
    setTab(M.prefs.tab === "legend" ? "legend" : "layers");
    if (M.isPhone() || M.prefs.panelOpen === false) panel.setAttribute("data-collapsed", "");
    M.renderBasemapControls(); M.renderTree(); M.renderLegend();
  };
})();
