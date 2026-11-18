/* Selection on the map: tap anything drawn to highlight it and get a short card; "Details" opens the
   full-screen detail view (RF.detail), which closes back to the same map view.
   Layer files register:
     M.clickable({layers: [layerIds], priority, feature(mapFeature) → feature reference})
     M.card[kind] = (feature) → {eyebrow, title, lines: [Node|string]}
     M.geometry[kind] = (feature) → GeoJSON geometry (for highlight and zoom)
   Owner: map agent. */
(() => {
  "use strict";
  const RF = window.RF, M = RF.mapkit, { el, icon, fmt } = RF.util;
  const clickables = [];
  M.card = {};
  M.geometry = {};
  M.clickable = (c) => { clickables.push(Object.assign({ priority: 50 }, c)); clickables.sort((a, b) => b.priority - a.priority); };

  // ---------- geometry of feature references ----------
  // Lines follow each connected downstream chain of reaches (never joined out of order), smoothed.
  M.geometry.run = (f) => { const S = M.R?.segments; return S ? { type: "MultiLineString", coordinates: RF.runs.chains(S, f.members) } : null; };
  M.geometry.section = M.geometry.run;
  M.geometry.reach = (f) => { if (f.geometry) return f.geometry; const S = M.R?.segments; return S ? { type: "LineString", coordinates: RF.runs.smooth(S.c[f.seg]) } : null; };
  M.geometry["linz-river"] = (f) => f.geometry || null;
  M.card["linz-river"] = (f) => ({ eyebrow: `LINZ ${f.linz.type}`, title: f.name,
    lines: [f.km2 ? el("span", { class: "num", text: `Catchment area here: ${M.fmtArea(f.km2)}` }) : null,
      el("span", { class: "rf-note", text: "Smaller than the modelled reaches (from 1 km²), so no gradient or flow here." }),
      RF.util.copyable(fmt.coord(f.lat, f.lon))], noDetails: true });
  M.geometryOf = (f) => {
    if (M.geometry[f.kind]) return M.geometry[f.kind](f);
    if (f.lon !== undefined && f.lat !== undefined) return { type: "Point", coordinates: [f.lon, f.lat] };
    if (f.geometry) return f.geometry;
    return null;
  };
  const coordsOf = (g) => !g ? [] : g.type === "Point" ? [g.coordinates] : g.type === "LineString" ? g.coordinates
    : g.type === "MultiLineString" ? g.coordinates.flat() : [];

  M.initSelection = () => {
    const map = M.map;
    map.addSource("sel", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    M.addLayer({ id: "sel-casing", type: "line", source: "sel", filter: ["in", ["geometry-type"], ["literal", ["LineString", "MultiLineString"]]],
      layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": "#ffffff", "line-width": ["interpolate", ["linear"], ["zoom"], 8, 7, 14, 12], "line-opacity": 0.9 } }, "select");
    M.addLayer({ id: "sel-line", type: "line", source: "sel", filter: ["in", ["geometry-type"], ["literal", ["LineString", "MultiLineString"]]],
      layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": M.token("--accent"), "line-width": ["interpolate", ["linear"], ["zoom"], 8, 3, 14, 6] } }, "select");
    M.addLayer({ id: "sel-point", type: "circle", source: "sel", filter: ["in", ["geometry-type"], ["literal", ["Point", "MultiPoint"]]],
      paint: { "circle-radius": 9, "circle-color": "rgba(0,0,0,0)", "circle-stroke-color": M.token("--accent"), "circle-stroke-width": 3 } }, "select");
    M.onTheme(() => { if (map.getLayer("sel-line")) { map.setPaintProperty("sel-line", "line-color", M.token("--accent")); map.setPaintProperty("sel-point", "circle-stroke-color", M.token("--accent")); } });

    map.on("click", (e) => onClick(e));
    map.on("mousemove", RF.util.debounce((e) => {
      if (!M.ready) return;
      const hit = query(e.point, 4).length > 0;
      map.getCanvas().style.cursor = hit ? "pointer" : "";
    }, 30));
  };

  function query(point, tol) {
    const map = M.map, box = [[point.x - tol, point.y - tol], [point.x + tol, point.y + tol]];
    for (const c of clickables) {
      const layers = c.layers.filter((l) => map.getLayer(l) && map.getLayoutProperty(l, "visibility") !== "none");
      if (!layers.length) continue;
      const hits = map.queryRenderedFeatures(box, { layers });
      if (hits.length) return [c, hits];
    }
    return [];
  }

  async function onClick(e) {
    const tol = window.matchMedia("(pointer: coarse)").matches ? 14 : 6;
    const [c, hits] = query(e.point, tol);
    if (c) {
      const f = await c.feature(hits[0], e);
      if (f) { M.select(f, { fit: false }); return; }
    }
    // nothing drawn here: another region's outline when zoomed out, else the catchment area at this land point
    const reg = M.map.queryRenderedFeatures(e.point, { layers: ["regions-fill"] }).find((r) => r.properties.id !== M.R?.id);
    if (reg && M.map.getZoom() < 10) { M.openRegion(reg.properties.id, { fit: true }); return; }
    const a = M.areaAt(e.lngLat);
    if (a) { M.select(Object.assign({ kind: "point", region: M.R?.id, id: `pt:${e.lngLat.lng},${e.lngLat.lat}`, name: "This point", lon: e.lngLat.lng, lat: e.lngLat.lat }, a), { fit: false }); return; }
    M.clearSelection();
  }

  // ---------- highlight + card ----------
  let cardEl = null;
  M.buildCard = (root) => { cardEl = el("div", { class: "map-card", hidden: true, role: "region", "aria-label": "Selected feature" }); root.append(cardEl); };

  M.card.point = (f) => ({
    eyebrow: "Catchment area at this point", title: M.fmtArea(f.km2),
    lines: [
      f.trunc ? el("span", { class: "rf-pill rf-pill--warn", text: "Cut off by the region edge: the true area is larger" }) : null,
      f.channel ? el("span", { class: "rf-row" }, el("span", { text: `Nearest channel: ${M.fmtArea(f.channel.km2)}` }),
        el("button", { type: "button", class: "rf-btn", onclick: () => { M.map.easeTo({ center: [f.channel.lon, f.channel.lat], duration: 300 }); M.select(Object.assign({}, f, { lon: f.channel.lon, lat: f.channel.lat, km2: f.channel.km2, trunc: f.channel.trunc, channel: null }), { fit: false }); } }, "Go to it")) : null,
      RF.util.copyable(fmt.coord(f.lat, f.lon), "Copy"),
      el("span", { class: "rf-note", text: `Area draining through this point (map cell about ${f.pixelM} m).` })],
    noDetails: true,
  });
  M.card.run = (f) => ({
    eyebrow: f.status === "listed" ? "Known river · in guides" : f.status === "heldout" ? "Predicted run · test river" : "Predicted run · not in guides",
    title: f.name,
    lines: [el("span", { class: "num", text: `${f.len.toFixed(1)} km · ${fmt.grad(f.grad)} m/km · flood ${fmt.flow(f.maf)} m³/s · ${fmt.km2(f.aBot)} km² · score ${f.score.toFixed(0)}` })],
  });

  function renderCard(f) {
    const def = (M.card[f.kind] || ((x) => ({ eyebrow: x.kind, title: x.name || x.kind, lines: [] })))(f);
    const close = el("button", { type: "button", class: "rf-btn rf-btn--quiet rf-iconbtn map-card-close", "aria-label": "Clear selection", html: icon("close"), onclick: () => M.clearSelection() });
    const actions = el("div", { class: "rf-row map-card-actions" });
    if (!def.noDetails) actions.append(el("button", { type: "button", class: "rf-btn rf-btn--primary", onclick: (e) => RF.detail.open(f, { opener: e.currentTarget }) }, "Details"));
    if (M.geometryOf(f)) actions.append(el("button", { type: "button", class: "rf-btn", onclick: () => M.zoomTo(f) }, "Zoom to"));
    cardEl.replaceChildren(el("div", { class: "map-card-head" },
      el("div", { class: "map-card-titles" }, el("span", { class: "rf-eyebrow", text: def.eyebrow || "" }), el("strong", { class: "map-card-title", text: def.title || "" })), close),
    ...(def.lines || []).filter(Boolean).map((l) => el("div", { class: "map-card-line" }, l)), actions);
    cardEl.hidden = false;
  }

  M.zoomTo = (f) => {
    const cs = coordsOf(M.geometryOf(f)); if (!cs.length) return;
    const pad = M.padding(); if (M.isPhone()) pad.bottom = Math.max(pad.bottom, (cardEl?.offsetHeight || 0) + 24);
    if (cs.length === 1) M.map.easeTo({ center: cs[0], zoom: Math.max(M.map.getZoom(), 14), padding: pad, duration: 600 });
    else M.map.fitBounds(M.bboxOf(cs), { padding: pad, maxZoom: 15, duration: 600 });
  };

  /** Highlight a feature, show its card, and optionally zoom to it. */
  M.select = (f, opts = {}) => {
    const g = M.geometryOf(f);
    M.map.getSource("sel")?.setData({ type: "FeatureCollection", features: g ? [{ type: "Feature", properties: {}, geometry: g }] : [] });
    renderCard(f);
    RF.select(f);
    if (opts.fit) M.zoomTo(f);
  };
  M.clearSelection = () => {
    M.map?.getSource("sel")?.setData({ type: "FeatureCollection", features: [] });
    if (cardEl) cardEl.hidden = true;
    RF.select(null);
  };
  M.onRegion(() => M.clearSelection());
})();
