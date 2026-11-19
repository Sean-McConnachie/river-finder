/* Map page internals shared by the map files (loaded in name order): RF.mapkit (M).
   Owner: map agent. Other pages use RF.go("map", {select}) and RF.map.* (see 90-page.js). */
(() => {
  "use strict";
  const RF = window.RF;
  const M = (RF.mapkit = {});
  M.NZ_BOUNDS = [[166.3, -47.4], [178.7, -34.3]];
  M.map = null;           // the maplibregl.Map, once mounted
  M.R = null;             // the region on the map
  M.ready = false;
  M.isPhone = () => window.matchMedia("(max-width: 720px)").matches;

  // ---------- layer slots: groups are drawn in this order (bottom to top) ----------
  // Each group is closed by an invisible anchor layer; addLayer(def, group) inserts before it, or before
  // the layer `before` when that is on the map (to place a layer between two of its group).
  // "catchment" (the shaded catchment of the selection) sits under every line, so rivers stay visible on it.
  M.SLOTS = ["base", "terrain", "overlay", "regions", "catchment", "network", "access", "rivers", "points", "select", "labels"];
  M.initSlots = () => M.SLOTS.forEach((s) => M.map.addLayer({ id: `slot-${s}`, type: "background", layout: { visibility: "none" } }));
  M.addLayer = (def, group, before) => {
    if (M.map.getLayer(def.id)) M.map.removeLayer(def.id);
    M.map.addLayer(def, before && M.map.getLayer(before) ? before : `slot-${group}`);
  };
  M.setSource = (id, spec) => {
    const src = M.map.getSource(id);
    if (src && spec.type === "geojson") { src.setData(spec.data); return src; }
    if (src) { M.layersUsing(id).forEach((l) => M.map.removeLayer(l)); M.map.removeSource(id); }
    M.map.addSource(id, spec);
    return M.map.getSource(id);
  };
  M.layersUsing = (src) => (M.map.getStyle()?.layers || []).filter((l) => l.source === src).map((l) => l.id);
  M.vis = (id, on) => { if (M.map.getLayer(id)) M.map.setLayoutProperty(id, "visibility", on ? "visible" : "none"); };

  // ---------- colours ----------
  M.token = (name) => RF.util.css(name) || "#888";
  M.isDark = () => {
    const t = document.documentElement.dataset.theme;
    return t ? t === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
  };
  M.hexRgb = (h) => { const s = h.replace("#", ""); const v = s.length === 3 ? s.split("").map((c) => c + c).join("") : s; return [0, 2, 4].map((i) => parseInt(v.slice(i, i + 2), 16)); };
  const themeHandlers = [];
  M.onTheme = (fn) => themeHandlers.push(fn);
  const fireTheme = () => themeHandlers.forEach((fn) => { try { fn(); } catch (e) { console.error(e); } });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", fireTheme);
  new MutationObserver(fireTheme).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

  // ---------- preferences (per browser) ----------
  const PREF_KEY = "map-prefs";
  M.prefs = Object.assign({ basemap: "terrain", layers: {}, colors: {}, catch: {} }, RF.store.get(PREF_KEY, {}) || {});
  if (M.prefs.basemap === "aerial") M.prefs.basemap = "satellite"; // renamed when LINZ aerial gave way to Sentinel-2
  M.savePrefs = RF.util.debounce(() => RF.store.set(PREF_KEY, M.prefs), 300);

  // ---------- label glyphs, served from glyphs/<Font_Name>__<range>.txt (base64) ----------
  M.FONT_WATER = ["Noto Sans Italic"];
  M.FONT_LABEL = ["Noto Sans Regular"];
  let glyphsReady = false;
  M.initGlyphs = () => {
    if (glyphsReady || !window.maplibregl) return;
    glyphsReady = true;
    window.maplibregl.addProtocol("rfglyph", async (params) => {
      const m = /^rfglyph:\/\/(.+)\/(\d+-\d+)$/.exec(params.url);
      if (!m) return { data: new ArrayBuffer(0) };
      const font = decodeURIComponent(m[1]).split(",")[0].trim().replace(/ /g, "_");
      try {
        const res = await fetch(`glyphs/${font}__${m[2]}.txt`);
        if (!res.ok) return { data: new ArrayBuffer(0) };
        return { data: RF.util.b64bytes(await res.text()).buffer };
      } catch (e) { return { data: new ArrayBuffer(0) }; }
    });
  };

  // ---------- region geometry helpers ----------
  /** Image corners for a region grid: TL, TR, BR, BL as [lon, lat]. */
  M.corners = (R) => { const [[la0, lo0], [la1, lo1]] = R.bounds; return [[lo0, la1], [lo1, la1], [lo1, la0], [lo0, la0]]; };
  M.lngLatBounds = (R) => { const [[la0, lo0], [la1, lo1]] = R.bounds; return [[lo0, la0], [lo1, la1]]; };
  M.fitRegion = (R, opts = {}) => M.map.fitBounds(M.lngLatBounds(R), Object.assign({ padding: M.padding(), duration: 600 }, opts));
  /** Padding that keeps fitted features clear of the side panel (desktop) or the card. */
  M.padding = () => {
    if (M.isPhone()) return { top: 60, bottom: 40, left: 24, right: 24 };
    const panel = document.querySelector(".map-panel:not([data-collapsed])");
    return { top: 40, bottom: 40, left: (panel ? panel.offsetWidth + 24 : 0) + 24, right: 72 };
  };
  M.bboxOf = (coords) => {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    coords.forEach(([x, y]) => { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); });
    return [[x0, y0], [x1, y1]];
  };

  // ---------- hooks: later files register what to do when a region is drawn ----------
  M.regionHooks = [];
  M.onRegion = (fn) => M.regionHooks.push(fn);
})();
