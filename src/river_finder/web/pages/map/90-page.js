/* Map page (#map): mounts MapLibre, the panel, the card and the floating controls; opens regions.
   Public: RF.go("map", {select: feature, aerial?}) (aerial: true also switches to the Aerial base map, where the
   site has it) and RF.map.{instance, select, zoomTo, region}. Owner: map agent. */
(() => {
  "use strict";
  const RF = window.RF, M = RF.mapkit, { el, icon } = RF.util;
  let root = null, statusEl = null, pending = null, opening = null;

  function status(msg) { if (!statusEl) return; statusEl.textContent = msg || ""; statusEl.hidden = !msg; }

  /** Load a region and draw it. fit: zoom to it. Resolves to the region. */
  M.openRegion = async (id, opts = {}) => {
    if (M.R && M.R.id === id && !opts.force) { if (opts.fit) M.fitRegion(M.R); return M.R; }
    const entry = RF.data.entry(id); if (!entry) return null;
    status(`Loading ${entry.title}…`);
    const my = (opening = id);
    try {
      const R = await RF.setRegion(id);
      if (opening !== my) return R;
      M.R = R;
      M.syncRegionPicker();
      M.regionHooks.forEach((fn) => { try { fn(R); } catch (e) { console.error(e); } });
      M.applyLayers();
      M.renderTree();
      if (opts.fit) M.fitRegion(R);
      return R;
    } catch (e) {
      console.error(e); RF.util.toast(e.message || "Could not load the region."); return null;
    } finally { if (opening === my) status(""); }
  };

  async function handleParams(params) {
    if (!params?.select || !M.ready) return;
    const f = params.select;
    if (f.region && (!M.R || M.R.id !== f.region)) await M.openRegion(f.region, { fit: false });
    if (params.aerial && M.aerial?.available()) M.setBasemap("linz-aerial");
    // The map may have been created while its page was hidden (0 x 0): size it before fitting, or fitBounds
    // computes the camera for an empty canvas and the view lands far from the feature.
    M.map.resize();
    M.select(f, { fit: true });
  }

  function mountMap(container) {
    if (!window.maplibregl) {
      container.replaceChildren(el("div", { class: "rf-empty map-error" }, el("strong", { text: "The map library did not load." }),
        el("span", { text: "Check the connection (MapLibre GL loads from cdnjs.cloudflare.com) and reload." })));
      return;
    }
    M.initGlyphs();
    const map = (M.map = new window.maplibregl.Map({
      container,
      style: { version: 8, glyphs: "rfglyph://{fontstack}/{range}", sources: {},
        layers: [{ id: "bg", type: "background", paint: { "background-color": M.token("--surface-2") } }] },
      bounds: M.NZ_BOUNDS, fitBoundsOptions: { padding: 20 },
      minZoom: 4, maxZoom: 17.5, dragRotate: false, pitchWithRotate: false, touchPitch: false,
      attributionControl: false, fadeDuration: 150,
    }));
    map.touchZoomRotate.disableRotation();
    map.keyboard.disableRotation();
    map.addControl(new window.maplibregl.NavigationControl({ showCompass: false }), "top-right");
    map.addControl(new window.maplibregl.ScaleControl({ unit: "metric", maxWidth: 110 }), "bottom-right");
    map.addControl(new window.maplibregl.AttributionControl({ compact: true, customAttribution: [
      'Terrain <a href="https://registry.opendata.aws/nz-elevation/" target="_blank" rel="noopener">LINZ</a> CC BY 4.0',
      'Rivers <a href="https://data.linz.govt.nz/layer/103632-nz-river-name-lines-pilot/" target="_blank" rel="noopener">LINZ</a>, <a href="https://niwa.co.nz/" target="_blank" rel="noopener">NIWA</a>',
      '<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">© OpenStreetMap</a>',
      'Guides <a href="https://www.riverguide.co.nz/" target="_blank" rel="noopener">NZ RiverGuide</a> CC BY-SA 3.0 NZ',
      M.S2_ATTR,
      RF.rain?.enabled ? 'Rain <a href="https://www.ecmwf.int/en/forecasts/datasets/open-data" target="_blank" rel="noopener">ECMWF</a> via <a href="https://open-meteo.com/" target="_blank" rel="noopener">Open-Meteo</a> CC BY 4.0' : null,
    ].filter(Boolean) }), "bottom-right");
    map.on("load", () => {
      container.querySelector(".maplibregl-ctrl-attrib")?.classList.remove("maplibregl-compact-show");
      M.initSlots();
      M.initBasemaps();
      M.initSelection();
      M.layerInits?.forEach((fn) => { try { fn(); } catch (e) { console.error(e); } });
      M.ready = true;
      M.applyLayers();
      const first = RF.state.region || RF.data.defaultRegion();
      const go = first ? M.openRegion(first, { fit: !pending?.select }) : Promise.resolve();
      go.then(() => { if (pending) { const p = pending; pending = null; handleParams(p); } });
    });
  }

  RF.page("map", {
    title: "Map",
    mount(pageEl) {
      root = el("div", { class: "map-root", "data-basemap": M.prefs.basemap });
      const canvas = el("div", { class: "map-canvas", id: "map-canvas" });
      statusEl = el("div", { class: "map-status", role: "status", hidden: true });
      const chip = el("button", { type: "button", class: "map-chip", "aria-label": "Region and layers", onclick: () => M.openPanel(true) },
        el("span", { html: icon("layers") }), el("span", { class: "map-chip-label", text: RF.data.entry(RF.data.defaultRegion())?.title || "Layers" }));
      const layersBtn = el("button", { type: "button", class: "rf-btn map-layers-btn", "aria-expanded": "true", onclick: () => M.openPanel() },
        el("span", { html: icon("layers") }), "Layers");
      const fitBtn = el("button", { type: "button", class: "rf-btn rf-iconbtn map-fit-btn", "aria-label": "Zoom to the region", title: "Zoom to the region",
        html: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg>',
        onclick: () => M.R && M.fitRegion(M.R) });
      root.append(canvas, chip, layersBtn, fitBtn, el("div", { class: "map-minilegend", hidden: true }), statusEl);
      pageEl.replaceChildren(root);
      M.buildPanel(root);
      M.buildCard(root);
      if (!RF.data.regions().length) {
        root.append(el("div", { class: "map-empty rf-empty" }, el("strong", { text: "No regions in this build." }),
          el("span", { text: "Run the pipeline and river-finder web build (see Info)." })));
      }
      mountMap(canvas);
    },
    show(params) {
      if (M.map) requestAnimationFrame(() => M.map.resize());
      if (params?.select) { if (M.ready) handleParams(params); else pending = params; }
    },
    hide() {},
  });

  RF.map = {
    get instance() { return M.map; },
    get region() { return M.R; },
    select: (f) => RF.go("map", { select: f }),
    zoomTo: (f) => M.zoomTo(f),
  };
})();
