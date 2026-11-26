/* Base maps: none, terrain (own LINZ-DEM hillshade), NZ Topo50 (LINZ Basemaps; standalone site only,
   needs a Basemaps key), satellite (Sentinel-2 cloudless 2025 by EOX, CC BY-NC-SA 4.0: live tiles on the
   standalone site, a pre-rendered image per region on the artifact host, which blocks tile servers),
   aerial (the LINZ Basemaps aerial layer at every zoom: LINZ photos, with LINZ's own satellite where it has no
   photos; standalone site with a key, like Topo50; the survey under each river in 47-aerial.js), catchment area (linear) and catchment area (log), plus region
   outlines. Owner: map agent. */
(() => {
  "use strict";
  const RF = window.RF, M = RF.mapkit;
  const SITE = RF.config.target !== "artifact";
  const KEY = RF.config.basemapsKey || "";
  const linz = (set) => `https://basemaps.linz.govt.nz/v1/tiles/${set}/WebMercatorQuad/{z}/{x}/{y}.webp${KEY ? `?api=${encodeURIComponent(KEY)}` : ""}`;
  const LINZ_ATTR = '<a href="https://basemaps.linz.govt.nz/" target="_blank" rel="noopener">LINZ Basemaps</a> CC BY 4.0';
  const LINZ_AERIAL_ATTR = 'Aerial photos <a href="https://basemaps.linz.govt.nz/" target="_blank" rel="noopener">LINZ Basemaps</a> CC BY 4.0';
  // EOxCloudless (Sentinel-2 cloudless) 2025: the newest cloud-free mosaic, CC BY-NC-SA 4.0
  const S2_TILES = "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2025_3857/default/g/{z}/{y}/{x}.jpg";
  M.S2_ATTR = 'Satellite <a href="https://cloudless.eox.at" target="_blank" rel="noopener">EOxCloudless 2025</a> by EOX (modified Copernicus Sentinel data 2025) <a href="https://creativecommons.org/licenses/by-nc-sa/4.0/" target="_blank" rel="noopener">CC BY-NC-SA 4.0</a>';

  M.BASEMAPS = [
    { id: "none", label: "None", note: "Plain background" },
    { id: "terrain", label: "Terrain", note: "Shaded relief from the LINZ elevation model" },
    { id: "topo", label: "Topographic", note: "NZ Topo50 maps (LINZ)", site: true, key: true },
    { id: "satellite", label: "Satellite", note: "Sentinel-2 cloudless 2025 (EOX), 10 m pixels" },
    { id: "linz-aerial", label: "Aerial", site: true, key: true,
      note: "LINZ aerial photos of all of New Zealand (mostly 0.075 to 0.3 m pixels)" },
    { id: "catch", label: "Catchment area", note: "Upstream area at every point, linear colour scale" },
    { id: "catch-log", label: "Catchment area (log)", note: "Upstream area, logarithmic colour scale" },
  ];
  // Topographic and aerial need a LINZ Basemaps key built into the site (web build --basemaps-key); without
  // one LINZ answers 400 for most tiles, so those options are left out.
  M.basemapAvailable = (b) => (!b.site || SITE) && (!b.key || !!KEY);
  /** Left out of the picker: topographic and aerial on a site built without a key (nothing to enable them). */
  M.basemapHidden = (b) => SITE && !!b.key && !KEY;
  /** Photo base maps (dark imagery: overlays take a stronger tint). */
  M.isImagery = (id) => id === "satellite" || id === "linz-aerial";
  M.AERIAL_MIN_ZOOM = 13; // the zoom from which river lines fade on the aerial photos (50-rivers.js)
  /** Two significant figures, no trailing zeros: 0.05, 0.5, 3.2, 45, 1200. */
  M.nice = (v) => v <= 0 ? "0" : String(+v.toPrecision(2));

  // Sequential blue, small to large catchment. Every land point is coloured, so the ramp spans the full
  // range: ridges pale, channels dark in light mode; in dark mode the largest rivers are brightest.
  const RAMP_LIGHT = ["#f4f8fd", "#dceafb", "#bcd6f6", "#94bdf0", "#6aa1e8", "#3f84dc", "#2a6cc0", "#1d5299", "#123a70", "#0a2550"];
  const RAMP_DARK = ["#101b28", "#152b44", "#1b3d61", "#225181", "#2c67a3", "#3f82c6", "#62a0e2", "#93c0f0", "#c5dcf8", "#eef5fd"];
  M.ramp = () => (M.isDark() ? RAMP_DARK : RAMP_LIGHT).map(M.hexRgb);
  M.rampAt = (t) => {
    const R = M.ramp(), x = Math.min(Math.max(t, 0), 1) * (R.length - 1), i = Math.min(Math.floor(x), R.length - 2), f = x - i;
    return [0, 1, 2].map((k) => Math.round(R[i][k] + (R[i + 1][k] - R[i][k]) * f));
  };

  // Catchment settings (per browser): smallest area shown, opacity, linear range, fade truncated.
  // minKm2 2e-5 = every land cell (the area code starts below one 5 m or 8 m cell)
  M.catchDefaults = { minKm2: 2e-5, opacity: 0.85, linMax: 100, fadeTrunc: true };
  /** Area for people: m² under 0.01 km², else km². */
  M.fmtArea = (km2) => !isFinite(km2) ? "–" : km2 < 0.01 ? `${Math.round(km2 * 1e6).toLocaleString("en-NZ")} m²` : `${RF.util.fmt.km2(km2)} km²`;
  M.catchSettings = () => Object.assign({}, M.catchDefaults, M.prefs.catch || {});

  function initBaseSources() {
    const map = M.map;
    if (SITE) {
      if (KEY) map.addSource("bm-topo", { type: "raster", tiles: [linz("topo-raster")], tileSize: 256, maxzoom: 17, attribution: LINZ_ATTR });
      map.addSource("bm-sat", { type: "raster", tiles: [S2_TILES], tileSize: 256, maxzoom: 14 });
      // The LINZ aerial layer at every zoom: full coverage (LINZ fills gaps in its photos with its own satellite imagery)
      if (KEY) map.addSource("bm-aerial", { type: "raster", tiles: [linz("aerial")], tileSize: 256, maxzoom: 20, attribution: LINZ_AERIAL_ATTR });
      if (KEY) M.addLayer({ id: "bm-topo", type: "raster", source: "bm-topo", layout: { visibility: "none" }, paint: { "raster-fade-duration": 150 } }, "base");
      M.addLayer({ id: "bm-sat", type: "raster", source: "bm-sat", layout: { visibility: "none" }, paint: { "raster-fade-duration": 150 } }, "base");
      if (KEY) M.addLayer({ id: "bm-aerial", type: "raster", source: "bm-aerial", layout: { visibility: "none" }, paint: { "raster-fade-duration": 150 } }, "base");
    }
    let warned = false;
    map.on("error", (e) => {
      if (e?.sourceId && String(e.sourceId).startsWith("bm-")) {
        if (!warned) { warned = true; RF.util.toast(e.sourceId === "bm-sat" ? "Satellite tiles did not load. Check the connection." : "LINZ Basemaps tiles did not load. Check the connection or the API key."); }
        return;
      }
      console.warn("map:", e?.error?.message || e);
    });
    // region outlines (every region in the index)
    const feats = RF.data.regions().map((r) => {
      const [[la0, lo0], [la1, lo1]] = r.bounds;
      return { type: "Feature", properties: { id: r.id, title: r.title }, geometry: { type: "Polygon", coordinates: [[[lo0, la0], [lo1, la0], [lo1, la1], [lo0, la1], [lo0, la0]]] } };
    });
    map.addSource("regions", { type: "geojson", data: { type: "FeatureCollection", features: feats } });
    M.addLayer({ id: "regions-fill", type: "fill", source: "regions", paint: { "fill-color": "#000", "fill-opacity": 0 } }, "regions");
    M.addLayer({ id: "regions-line", type: "line", source: "regions", paint: { "line-color": M.token("--muted"), "line-width": 1, "line-dasharray": [3, 2], "line-opacity": 0.7 } }, "regions");
    M.addLayer({ id: "regions-label", type: "symbol", source: "regions", maxzoom: 9,
      layout: { "text-field": ["get", "title"], "text-font": M.FONT_LABEL, "text-size": 12, "text-allow-overlap": false },
      paint: { "text-color": M.token("--muted"), "text-halo-color": M.token("--surface"), "text-halo-width": 1.5 } }, "labels");
    M.onTheme(() => {
      if (!M.map.getLayer("regions-line")) return;
      M.map.setPaintProperty("regions-line", "line-color", M.token("--muted"));
      M.map.setPaintProperty("regions-label", "text-color", M.token("--muted"));
      M.map.setPaintProperty("regions-label", "text-halo-color", M.token("--surface"));
      M.map.setPaintProperty("bg", "background-color", M.token("--surface-2"));
      paintHillshade(); redrawCatch();
    });
  }
  M.onRegion((R) => {
    if (M.map.getLayer("regions-line")) M.map.setFilter("regions-line", ["!=", ["get", "id"], R.id]);
  });

  // ---------- per-region terrain and catchment ----------
  let C = null; // {R, area, trunc, disp, dispTrunc, canvas, ctx, maxLog}

  // Hillshade drawn on a canvas so the nodata margin is transparent and the tint follows the theme
  // (and lightens under the catchment colours).
  const HS = { light: ["#8c928d", "#f7f8f5"], dark: ["#0d110f", "#545d58"], lightCatch: ["#c3c8c3", "#fafbf9"], darkCatch: ["#0a0d0b", "#323a35"] };
  function paintHillshade() {
    if (!C || !C.hs) return;
    const dark = M.isDark(), catchOn = M.prefs.basemap.startsWith("catch");
    const [lo, hi] = HS[(dark ? "dark" : "light") + (catchOn ? "Catch" : "")].map(M.hexRgb);
    const L = new Uint32Array(256);
    for (let v = 1; v < 256; v++) {
      const t = v / 255, c = [0, 1, 2].map((k) => Math.round(lo[k] + (hi[k] - lo[k]) * t));
      L[v] = (255 << 24) | (c[2] << 16) | (c[1] << 8) | c[0];
    }
    const img = C.hsCtx.createImageData(C.R.width, C.R.height), px = new Uint32Array(img.data.buffer), h = C.hs;
    for (let i = 0; i < px.length; i++) px[i] = L[h[i]];
    C.hsCtx.putImageData(img, 0, 0);
    const src = M.map.getSource("terrain");
    if (src && src.play) { src.play(); src.pause(); }
    M.map.triggerRepaint();
  }

  async function loadRegionImages(R) {
    const coords = M.corners(R);
    C = null;
    const [hs, area, trunc] = await Promise.all([RF.util.decodeGray(R.hillshade, R.width, R.height),
      RF.util.decodeGray(R.area, R.width, R.height), RF.util.decodeGray(R.trunc, R.width, R.height)]);
    if (M.R !== R) return;
    const W = R.width, H = R.height;
    let maxCode = 0; for (let i = 0; i < area.length; i++) if (area[i] > maxCode) maxCode = area[i];
    // Display copy with channels widened by size (1 px; 3 px above 10 km²; 5 px above 100 km²), so
    // rivers stay visible zoomed out. Point readouts use the unwidened grid.
    const disp = area.slice(), dispTrunc = trunc.slice();
    const code = (km2) => Math.round((Math.log10(km2) - R.code.log_min) / R.code.step) + 2;
    const c10 = code(10), c100 = code(100);
    for (let i = 0; i < area.length; i++) {
      const c = area[i]; if (c < c10) continue;
      const rad = c >= c100 ? 2 : 1, r0 = (i / W) | 0, q0 = i - r0 * W;
      for (let dr = -rad; dr <= rad; dr++) for (let dq = -rad; dq <= rad; dq++) {
        const r = r0 + dr, q = q0 + dq; if (r < 0 || q < 0 || r >= H || q >= W) continue;
        const j = r * W + q; if (area[j] !== 0 && c > disp[j]) { disp[j] = c; dispTrunc[j] = trunc[i]; }
      }
    }
    const canvas = document.createElement("canvas"); canvas.width = W; canvas.height = H;
    const hsCanvas = document.createElement("canvas"); hsCanvas.width = W; hsCanvas.height = H;
    C = { R, hs, area, trunc, disp, dispTrunc, canvas, ctx: canvas.getContext("2d"), hsCanvas, hsCtx: hsCanvas.getContext("2d"),
          maxLog: R.code.log_min + (maxCode - 2) * R.code.step };
    M.setSource("terrain", { type: "canvas", canvas: hsCanvas, coordinates: coords, animate: false });
    M.addLayer({ id: "terrain", type: "raster", source: "terrain", layout: { visibility: "none" }, paint: { "raster-fade-duration": 0 } }, "terrain");
    paintHillshade();
    drawCatch();
    M.setSource("catch", { type: "canvas", canvas, coordinates: coords, animate: false });
    M.addLayer({ id: "catch", type: "raster", source: "catch", layout: { visibility: "none" }, paint: { "raster-fade-duration": 0, "raster-opacity": M.catchSettings().opacity } }, "overlay");
    applyBasemap();
    M.drawCatchLegend?.();
    areaHooks.forEach((fn) => { try { fn(R); } catch (e) { console.error(e); } });
  }
  const areaHooks = [];
  /** Run fn(R) once a region's area grid is decoded (M.areaGrid() is then that region's). */
  M.onAreaGrid = (fn) => areaHooks.push(fn);
  M.onRegion((R) => loadRegionImages(R).catch((e) => { console.error(e); RF.util.toast("Could not draw the terrain for this region."); }));
  // Artifact host: no tile servers, so the satellite base map is the region's pre-rendered image (webdata.write_satellite).
  M.onRegion((R) => {
    if (SITE) return;
    M.setSource("sat-img", { type: "image", url: `${R.id}_sat.jpg`, coordinates: M.corners(R) });
    M.addLayer({ id: "sat-img", type: "raster", source: "sat-img", layout: { visibility: M.prefs.basemap === "satellite" ? "visible" : "none" }, paint: { "raster-fade-duration": 0 } }, "base");
  });

  /** Colour lookup per area code (256 entries, RGBA packed little-endian). */
  function lut(R, s, mode, dark) {
    const out = new Uint32Array(256), minLog = Math.max(Math.log10(Math.max(s.minKm2, 1e-9)), R.code.log_min), maxLog = Math.max(C.maxLog, minLog + 0.5);
    const a = Math.round(255);
    for (let c = 2; c < 256; c++) {
      const lg = R.code.log_min + (c - 2) * R.code.step;
      if (lg < minLog) continue;
      const t = mode === "catch-log" ? (lg - minLog) / (maxLog - minLog) : Math.min(10 ** lg / s.linMax, 1);
      const [r, g, b] = M.rampAt(t);
      out[c] = (a << 24) | (b << 16) | (g << 8) | r;
    }
    return out;
  }

  function drawCatch() {
    if (!C) return;
    const s = M.catchSettings(), mode = M.prefs.basemap === "catch" ? "catch" : "catch-log";
    const L = lut(C.R, s, mode, M.isDark()), W = C.R.width, H = C.R.height;
    const img = C.ctx.createImageData(W, H), px = new Uint32Array(img.data.buffer), d = C.disp, tr = C.dispTrunc;
    const fade = s.fadeTrunc;
    for (let i = 0; i < px.length; i++) {
      const v = L[d[i]];
      if (!v) continue;
      px[i] = fade && tr[i] ? (v & 0x00ffffff) | (90 << 24) : v;
    }
    C.ctx.putImageData(img, 0, 0);
  }
  function redrawCatch() {
    if (!C) return;
    drawCatch();
    const src = M.map.getSource("catch");
    if (src && src.play) { src.play(); src.pause(); }
    M.map.triggerRepaint();
    M.drawCatchLegend?.();
  }
  M.redrawCatch = RF.util.debounce(redrawCatch, 60);
  M.setCatch = (patch) => {
    M.prefs.catch = Object.assign({}, M.prefs.catch, patch); M.savePrefs();
    if ("opacity" in patch && M.map.getLayer("catch")) M.map.setPaintProperty("catch", "raster-opacity", patch.opacity);
    if (Object.keys(patch).some((k) => k !== "opacity")) M.redrawCatch();
  };

  /** The region's decoded grids once drawn: {R, area (8-bit log code), trunc (0/255)} on the heat-map grid, or null. */
  M.areaGrid = () => (C ? { R: C.R, area: C.area, trunc: C.trunc } : null);
  /** Index of the heat-map grid pixel (row * width + column) under a map point, or -1 outside the region. */
  M.pixelOf = (R, lngLat) => {
    const p = window.maplibregl.MercatorCoordinate.fromLngLat(lngLat);
    const E = 20037508.342789244, x = p.x * 2 * E - E, y = E - p.y * 2 * E;
    const pw = (R.merc[2] - R.merc[0]) / R.width, ph = (R.merc[3] - R.merc[1]) / R.height;
    const q = Math.floor((x - R.merc[0]) / pw), r = Math.floor((R.merc[3] - y) / ph);
    return q < 0 || r < 0 || q >= R.width || r >= R.height ? -1 : r * R.width + q;
  };

  /** Catchment area at a map point: {km2, trunc, channel: {km2, lon, lat} | null}, or null off land.
   *  Every land point has an area (down to one cell); a much larger channel within ~3 px is also given,
   *  because a finger rarely lands exactly on a 1 px stream. */
  M.areaAt = (lngLat) => {
    if (!C) return null;
    const R = C.R, E = 20037508.342789244, i0 = M.pixelOf(R, lngLat);
    if (i0 < 0) return null;
    const pw = (R.merc[2] - R.merc[0]) / R.width, ph = (R.merc[3] - R.merc[1]) / R.height;
    const q = i0 % R.width, r = (i0 - q) / R.width, c0 = C.area[i0];
    if (c0 < 1) return null;
    const km2 = (c) => 10 ** (R.code.log_min + (c - 2) * R.code.step);
    const here = c0 >= 2 ? km2(c0) : 0;
    let best = c0, bi = i0;
    for (let dr = -3; dr <= 3; dr++) for (let dq = -3; dq <= 3; dq++) {
      const rr = r + dr, qq = q + dq; if (rr < 0 || qq < 0 || rr >= R.height || qq >= R.width) continue;
      const i = rr * R.width + qq; if (C.area[i] > best) { best = C.area[i]; bi = i; }
    }
    let channel = null;
    if (bi !== i0 && best >= 2 && km2(best) >= 0.5 && km2(best) >= 20 * Math.max(here, 1e-6)) {
      const bq = bi % R.width, br = (bi - bq) / R.width;
      const mx = R.merc[0] + (bq + 0.5) * pw, my = R.merc[3] - (br + 0.5) * ph;
      const ll = new window.maplibregl.MercatorCoordinate((mx + E) / (2 * E), (E - my) / (2 * E)).toLngLat();
      channel = { km2: km2(best), lon: ll.lng, lat: ll.lat, trunc: C.trunc[bi] > 0 };
    }
    return { km2: here, trunc: C.trunc[i0] > 0, channel, pixelM: Math.round(pw * Math.cos(lngLat.lat * Math.PI / 180)) };
  };

  const basemapHooks = [];
  /** Run fn(id) whenever the base map in use changes (e.g. to adapt an overlay's paint). */
  M.onBasemap = (fn) => basemapHooks.push(fn);
  /** Show the chosen base map. */
  function applyBasemap() {
    const id = M.prefs.basemap, map = M.map; if (!map) return;
    const b = M.BASEMAPS.find((x) => x.id === id);
    const use = b && M.basemapAvailable(b) ? id : "terrain";
    M.vis("bm-topo", use === "topo");
    M.vis("bm-sat", use === "satellite");
    M.vis("bm-aerial", use === "linz-aerial");
    M.vis("sat-img", use === "satellite");
    M.vis("terrain", use === "terrain" || use.startsWith("catch"));
    M.vis("catch", use.startsWith("catch"));
    paintHillshade();
    document.querySelector(".map-root")?.setAttribute("data-basemap", use);
    basemapHooks.forEach((fn) => { try { fn(use); } catch (e) { console.error(e); } });
  }
  M.setBasemap = (id) => {
    const prevCatch = M.prefs.basemap.startsWith("catch");
    M.prefs.basemap = id; M.savePrefs();
    applyBasemap();
    if (id.startsWith("catch") && (prevCatch || true)) redrawCatch();
    M.renderLegend?.();
    M.renderBasemapControls?.();
  };
  M.initBasemaps = () => { initBaseSources(); applyBasemap(); };

  /** Legend content for the active base map (gradient bar for the catchment maps). */
  M.basemapLegend = () => {
    const id = M.prefs.basemap, { el } = RF.util;
    if (!id.startsWith("catch") || !C) return null;
    const s = M.catchSettings(), log = id === "catch-log", minLog = Math.max(Math.log10(s.minKm2), C.R.code.log_min), maxLog = Math.max(C.maxLog, minLog + 0.5);
    const cv = el("canvas", { width: 240, height: 10, class: "map-ramp", "aria-hidden": "true" });
    const ctx = cv.getContext("2d");
    for (let x = 0; x < 240; x++) {
      const t = x / 239, [r, g, b] = M.rampAt(t);
      ctx.fillStyle = `rgb(${r},${g},${b})`; ctx.fillRect(x, 0, 1, 10);
    }
    const ticks = el("div", { class: "map-ticks" });
    if (log) {
      const decades = []; for (let e = Math.ceil(minLog); e <= Math.floor(maxLog); e++) decades.push(e);
      const stride = Math.ceil(decades.length / 5);
      decades.filter((e, k) => k % stride === 0).forEach((e) => {
        const pos = (e - minLog) / (maxLog - minLog) * 100;
        const v = 10 ** e;
        ticks.append(el("span", { style: `left:${pos}%`, text: v < 0.01 ? `${Math.round(v * 1e6)} m²` : String(+v.toPrecision(1)) }));
      });
    } else {
      [0, 0.25, 0.5, 0.75, 1].forEach((t) => ticks.append(el("span", { style: `left:${t * 100}%`, text: t === 1 ? `${M.nice(s.linMax)}+` : M.nice(s.linMax * t) })));
    }
    return el("div", { class: "map-legend-block" }, el("div", { class: "map-legend-title", text: `Catchment area${log ? ", log scale (km² unless marked m²)" : ", km²"}` }), cv, ticks,
      el("p", { class: "rf-note", text: `${s.minKm2 > 3e-5 ? `Areas under ${M.fmtArea(s.minKm2)} are not coloured. ` : "Every land point is coloured. "}Tap the map for the area at any point.${s.fadeTrunc ? " Faded: catchments cut off by the region edge." : ""}` }));
  };
})();
