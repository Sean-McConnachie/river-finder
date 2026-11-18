/* Roads, 4WD / private roads and walking tracks (OpenStreetMap via Overture Maps), with names along
   the lines from zoom 13. The file loads only when one of these layers is on. Owner: map agent. */
(() => {
  "use strict";
  const RF = window.RF, M = RF.mapkit, { el, fmt, icon } = RF.util;
  const EMPTY = { type: "FeatureCollection", features: [] };
  const KIND = ["road", "track", "foot"];
  const LABEL = { road: "Road", track: "4WD or private road", foot: "Walking track" };
  const cache = new Map();
  let shown = null, counts = { road: null, track: null, foot: null };
  const ext = (href, text) => el("a", { href, target: "_blank", rel: "noopener", class: "det-link" }, el("span", { text }), el("span", { html: icon("external"), "aria-hidden": "true" }));

  M.defineGroup({ id: "access", label: "Roads and tracks", link: "https://www.openstreetmap.org/copyright" });
  M.defineLayer({ id: "acc-road", parent: "access", label: "Roads", note: "Major roads drawn wider", defaultOn: true,
    colors: [{ key: "c", token: "--road" }], legend: "Road", count: () => counts.road, apply: applyAccess });
  M.defineLayer({ id: "acc-track", parent: "access", label: "4WD and private roads", note: "Dashed", defaultOn: true,
    colors: [{ key: "c", token: "--road" }], legend: "4WD or private road (dashed)", count: () => counts.track, apply: applyAccess });
  M.defineLayer({ id: "acc-foot", parent: "access", label: "Walking tracks", note: "Dotted; most DOC tracks", defaultOn: true,
    colors: [{ key: "c", token: "--track" }], legend: "Walking track (dotted)", count: () => counts.foot, apply: applyAccess });
  M.defineLayer({ id: "acc-names", parent: "access", label: "Road and track names", note: "From zoom 13", defaultOn: true, swatch: "none", apply: applyAccess });

  M.layerInits.push(() => {
    const map = M.map;
    map.addSource("src-access", { type: "geojson", data: EMPTY, tolerance: 0.5 });
    const w = (base) => ["interpolate", ["linear"], ["zoom"], 9, ["*", base, 0.4], 13, base, 16, ["*", base, 2.2]];
    M.addLayer({ id: "acc-road-line", type: "line", source: "src-access", minzoom: 8, filter: ["==", ["get", "k"], 0],
      layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": "#6b5a4a", "line-width": w(["case", ["==", ["get", "m"], 1], 2.4, 1.3]), "line-opacity": 0.85 } }, "access");
    M.addLayer({ id: "acc-track-line", type: "line", source: "src-access", minzoom: 9, filter: ["==", ["get", "k"], 1],
      layout: { "line-join": "round" }, paint: { "line-color": "#6b5a4a", "line-width": w(1.3), "line-dasharray": [3, 2], "line-opacity": 0.85 } }, "access");
    M.addLayer({ id: "acc-foot-line", type: "line", source: "src-access", minzoom: 9, filter: ["==", ["get", "k"], 2],
      layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": "#a0642c", "line-width": w(1.4), "line-dasharray": [0.6, 1.8], "line-opacity": 0.95 } }, "access");
    M.addLayer({ id: "acc-label", type: "symbol", source: "src-access", minzoom: 13, filter: ["!=", ["get", "n"], ""],
      layout: { "symbol-placement": "line", "text-field": ["get", "n"], "text-font": M.FONT_LABEL, "text-size": ["interpolate", ["linear"], ["zoom"], 13, 10.5, 16, 13],
        "symbol-spacing": 300, "text-max-angle": 40, "text-padding": 2 },
      paint: { "text-color": "#4a3f35", "text-halo-color": "#ffffff", "text-halo-width": 1.6 } }, "labels");
    const theme = () => {
      if (!map.getLayer("acc-label")) return;
      map.setPaintProperty("acc-label", "text-color", M.isDark() ? "#e0d6ca" : "#4a3f35");
      map.setPaintProperty("acc-label", "text-halo-color", M.isDark() ? "#111513" : "#ffffff");
    };
    M.onTheme(theme); theme();
    M.clickable({ layers: ["acc-road-line", "acc-track-line", "acc-foot-line"], priority: 30, feature: (f, e) => accessFeature(f, e) });
  });

  async function load(R) {
    if (!cache.has(R.id)) {
      cache.set(R.id, fetch(`${R.id}_access.json`).then((r) => (r.ok ? r.json() : null)).catch(() => null));
    }
    return cache.get(R.id);
  }

  async function show(R) {
    const anyOn = ["acc-road", "acc-track", "acc-foot"].some((id) => M.isVisible(id));
    if (!anyOn || shown === R.id) return;
    const A = await load(R);
    if (M.R !== R) return;
    shown = R.id;
    if (!A) { M.map.getSource("src-access").setData(EMPTY); counts = { road: 0, track: 0, foot: 0 }; M.renderTree(); return; }
    const feats = [];
    counts = { road: 0, track: 0, foot: 0 };
    A.lines.forEach(([k, n, m, o, flat], i) => {
      const c = []; for (let j = 0; j < flat.length; j += 2) c.push([flat[j], flat[j + 1]]);
      if (c.length < 2) return;
      counts[KIND[k]]++;
      feats.push({ type: "Feature", properties: { i, k, m, n: n >= 0 ? A.names[n] : "", o: o >= 0 ? A.osm[o] : "" }, geometry: { type: "LineString", coordinates: c } });
    });
    M.map.getSource("src-access").setData({ type: "FeatureCollection", features: feats });
    M.renderTree();
  }

  M.onRegion((R) => { shown = null; counts = { road: null, track: null, foot: null }; M.map.getSource("src-access")?.setData(EMPTY); show(R); });

  function applyAccess() {
    if (!M.ready || !M.map.getLayer("acc-road-line")) return;
    [["acc-road", "acc-road-line"], ["acc-track", "acc-track-line"], ["acc-foot", "acc-foot-line"]].forEach(([id, layer]) => {
      M.vis(layer, M.isVisible(id)); M.map.setPaintProperty(layer, "line-color", M.colorOf(id));
    });
    const kinds = [["acc-road", 0], ["acc-track", 1], ["acc-foot", 2]].filter(([id]) => M.isVisible(id)).map(([, k]) => k);
    M.vis("acc-label", M.isVisible("acc-names") && kinds.length > 0);
    M.map.setFilter("acc-label", ["all", ["!=", ["get", "n"], ""], ["in", ["get", "k"], ["literal", kinds]]]);
    if (M.R) show(M.R);
  }

  function accessFeature(f, e) {
    const p = f.properties, kind = KIND[p.k];
    return { kind, region: M.R.id, id: `${M.R.id}:${kind}:${p.i}`, name: p.n || `Unnamed ${LABEL[kind].toLowerCase()}`, named: !!p.n,
      osm: p.o || "", lat: e.lngLat.lat, lon: e.lngLat.lng, geometry: f.geometry };
  }
  M.geometry.road = M.geometry.track = M.geometry.foot = (f) => f.geometry || null;
  M.card.road = M.card.track = M.card.foot = (f) => ({ eyebrow: LABEL[f.kind], title: f.name, lines: [RF.util.copyable(fmt.coord(f.lat, f.lon))] });

  const osmUrl = (id) => { const m = /^([nwr])(\d+)$/.exec(id || ""); return m ? `https://www.openstreetmap.org/${{ n: "node", w: "way", r: "relation" }[m[1]]}/${m[2]}` : null; };
  function accessDetail(f, ctx) {
    const [E, N] = RF.util.toNZTM(f.lat, f.lon);
    ctx.actions.append(el("button", { type: "button", class: "rf-btn det-show", "aria-label": "Show on map", onclick: () => { ctx.close(); RF.go("map", { select: f }); } },
      el("span", { html: icon("pin"), "aria-hidden": "true" }), el("span", { class: "det-show-label", text: "Show on map" })));
    const links = [];
    if (osmUrl(f.osm)) links.push(ext(osmUrl(f.osm), "OpenStreetMap feature"));
    if (f.kind === "foot" && f.named) links.push(ext(`https://www.google.com/search?q=${encodeURIComponent(`${f.name} DOC track`)}`, "Track information (search, DOC)"));
    links.push(ext(`https://basemaps.linz.govt.nz/@${f.lat.toFixed(5)},${f.lon.toFixed(5)},z16`, "LINZ Basemaps"));
    links.push(ext(`https://www.google.com/maps/search/?api=1&query=${f.lat.toFixed(5)},${f.lon.toFixed(5)}`, "Google Maps"));
    const note = f.kind === "track" ? "Closed to the public or 4WD only in OpenStreetMap: check access and permission before using it." :
      f.kind === "foot" ? "Walking track in OpenStreetMap. Check DOC for closures and conditions." : "Public road in OpenStreetMap.";
    return {
      eyebrow: LABEL[f.kind], title: f.name, subtitle: RF.data.entry(f.region)?.title || "",
      body: [
        el("p", { class: "det-desc", text: note }),
        el("section", { class: "det-sec" }, el("h3", { text: "Where you tapped" }), el("div", { class: "det-coord" },
          el("div", { class: "rf-row" }, el("span", { class: "rf-muted det-k", text: "Lat, lon" }), RF.util.copyable(fmt.coord(f.lat, f.lon))),
          el("div", { class: "rf-row" }, el("span", { class: "rf-muted det-k", text: "NZTM" }), RF.util.copyable(`${Math.round(E)} E, ${Math.round(N)} N`)))),
        el("section", { class: "det-sec" }, el("h3", { text: "Sources" }), el("div", { class: "det-linklist" }, ...links)),
        el("p", { class: "rf-note det-foot", text: "© OpenStreetMap contributors (ODbL), via Overture Maps." }),
      ],
    };
  }
  ["road", "track", "foot"].forEach((k) => RF.detail.register(k, accessDetail));
})();
