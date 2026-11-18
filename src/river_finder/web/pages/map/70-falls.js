/* Waterfalls and rapids (OpenStreetMap, most heights from the LINZ Topo50 import) with their detail
   view: height and its source, coordinates to copy, OSM and photo links, the reach they sit on.
   Owner: map agent. */
(() => {
  "use strict";
  const RF = window.RF, M = RF.mapkit, { el, fmt, icon } = RF.util;
  const EMPTY = { type: "FeatureCollection", features: [] };
  const ext = (href, text) => el("a", { href, target: "_blank", rel: "noopener", class: "det-link" }, el("span", { text }), el("span", { html: icon("external"), "aria-hidden": "true" }));
  const count = (k) => (M.R?.falls || []).filter((f) => f[5] === k).length;

  M.defineGroup({ id: "features", label: "Waterfalls and rapids", link: "https://www.openstreetmap.org/copyright" });
  M.defineLayer({ id: "falls", parent: "features", label: "Waterfalls", note: "OpenStreetMap / LINZ Topo50", defaultOn: true, swatch: "dot",
    colors: [{ key: "c", token: "--falls" }], legend: "Waterfall (larger = taller)", count: () => count("w"), apply: applyFalls });
  M.defineLayer({ id: "rapids", parent: "features", label: "Rapids", note: "Ring marker", defaultOn: true, swatch: "dot",
    colors: [{ key: "c", token: "--falls" }], legend: "Rapid (ring)", count: () => count("r"), apply: applyFalls });

  M.layerInits.push(() => {
    const map = M.map;
    map.addSource("src-falls", { type: "geojson", data: EMPTY });
    M.addLayer({ id: "falls-dot", type: "circle", source: "src-falls", filter: ["==", ["get", "k"], "w"],
      paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 8, 3, 13, ["+", 5, ["min", 5, ["/", ["sqrt", ["get", "h"]], 2]]]],
        "circle-color": "#0e7c86", "circle-stroke-color": "#ffffff", "circle-stroke-width": 1.5 } }, "points");
    M.addLayer({ id: "rapids-dot", type: "circle", source: "src-falls", filter: ["==", ["get", "k"], "r"],
      paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 8, 3, 13, 6], "circle-color": "rgba(0,0,0,0)",
        "circle-stroke-color": "#0e7c86", "circle-stroke-width": 2.5 } }, "points");
    M.addLayer({ id: "falls-label", type: "symbol", source: "src-falls", minzoom: 12.5, filter: ["!=", ["get", "t"], ""],
      layout: { "text-field": ["get", "t"], "text-font": M.FONT_LABEL, "text-size": 11.5, "text-anchor": "left", "text-offset": [0.9, 0], "text-optional": true },
      paint: { "text-color": "#0b5d64", "text-halo-color": "#ffffff", "text-halo-width": 1.5 } }, "labels");
    const theme = () => {
      if (!map.getLayer("falls-label")) return;
      map.setPaintProperty("falls-label", "text-color", M.isDark() ? "#7fd6dd" : "#0b5d64");
      map.setPaintProperty("falls-label", "text-halo-color", M.isDark() ? "#111513" : "#ffffff");
      map.setPaintProperty("falls-dot", "circle-stroke-color", M.isDark() ? "#111513" : "#ffffff");
    };
    M.onTheme(theme); theme();
    M.clickable({ layers: ["falls-dot", "rapids-dot"], priority: 95, feature: (f) => fallFeature(f.properties.i) });
  });

  M.onRegion((R) => {
    const src = M.map.getSource("src-falls"); if (!src) return;
    src.setData({ type: "FeatureCollection", features: (R.falls || []).map((p, i) => ({
      type: "Feature", properties: { i, k: p[5], h: p[3] || 0, t: [p[2], p[3] ? `${p[3]} m` : ""].filter(Boolean).join(" · ") },
      geometry: { type: "Point", coordinates: [p[0], p[1]] } })) });
  });

  function applyFalls() {
    if (!M.ready || !M.map.getLayer("falls-dot")) return;
    const w = M.isVisible("falls"), r = M.isVisible("rapids");
    M.vis("falls-dot", w); M.vis("rapids-dot", r); M.vis("falls-label", w || r);
    M.map.setFilter("falls-label", ["all", ["!=", ["get", "t"], ""], ["in", ["get", "k"], ["literal", [w ? "w" : "-", r ? "r" : "-"]]]]);
    M.map.setPaintProperty("falls-dot", "circle-color", M.colorOf("falls"));
    M.map.setPaintProperty("rapids-dot", "circle-stroke-color", M.colorOf("rapids"));
  }

  function fallFeature(i, R = M.R) {
    const p = R?.falls?.[i]; if (!p) return null;
    const rapids = p[5] === "r";
    return { kind: rapids ? "rapids" : "waterfall", region: R.id, id: p[6] ? `osm:${p[6]}` : `${R.id}:falls:${i}`, i,
      name: p[2] || (rapids ? "Unnamed rapid" : "Unnamed waterfall"), named: !!p[2], lon: p[0], lat: p[1], height: p[3], seg: p[4],
      osm: p[6] || "", linz: !!p[7], grade: p[8] || "", photo: p[9] || {} };
  }
  M.fallFeature = fallFeature;

  M.card.waterfall = M.card.rapids = (f) => ({
    eyebrow: f.kind === "rapids" ? "Rapid" : "Waterfall", title: f.name,
    lines: [f.height ? el("span", { class: "num", text: `${f.height} m high${f.linz ? " (LINZ Topo50)" : ""}` }) : (f.kind === "waterfall" ? el("span", { text: "Height not mapped" }) : null),
      RF.util.copyable(fmt.coord(f.lat, f.lon))],
  });

  const osmUrl = (id) => {
    const m = /^([nwr])(\d+)$/.exec(id || ""); if (!m) return null;
    return `https://www.openstreetmap.org/${{ n: "node", w: "way", r: "relation" }[m[1]]}/${m[2]}`;
  };
  function photoLinks(f) {
    const out = [], ph = f.photo || {};
    if (ph.c) out.push(ext(`https://commons.wikimedia.org/wiki/${encodeURIComponent(ph.c.replace(/ /g, "_")).replace(/%3A/g, ":")}`, "Wikimedia Commons"));
    if (ph.i && /^https?:\/\//.test(ph.i)) out.push(ext(ph.i, "Photo (OSM image tag)"));
    if (ph.p) { const [lang, ...t] = ph.p.split(":"); out.push(ext(`https://${t.length ? lang : "en"}.wikipedia.org/wiki/${encodeURIComponent((t.length ? t.join(":") : lang).replace(/ /g, "_"))}`, "Wikipedia")); }
    if (ph.w) out.push(ext(`https://www.wikidata.org/wiki/${encodeURIComponent(ph.w)}`, "Wikidata"));
    out.push(ext(`https://commons.wikimedia.org/wiki/Special:Nearby#/coord/${f.lat.toFixed(5)},${f.lon.toFixed(5)}`, "Photos near here (Commons)"));
    out.push(ext(`https://www.google.com/search?tbm=isch&q=${encodeURIComponent(`${f.named ? f.name : "waterfall"} New Zealand`)}`, "Image search"));
    return out;
  }

  async function fallDetail(f, ctx) {
    const R = await RF.data.region(f.region), S = R.segments;
    const [E, N] = RF.util.toNZTM(f.lat, f.lon);
    const reach = f.seg >= 0 && S ? { kind: "reach", region: R.id, id: `${R.id}:reach:${f.seg}`, seg: f.seg, name: S.r[f.seg] >= 0 ? S.rivers[S.r[f.seg]] : "Unnamed stream" } : null;
    ctx.actions.append(el("button", { type: "button", class: "rf-btn det-show", "aria-label": "Show on map", onclick: () => { ctx.close(); RF.go("map", { select: f }); } },
      el("span", { html: icon("pin"), "aria-hidden": "true" }), el("span", { class: "det-show-label", text: "Show on map" })));
    const rows = [
      ["Type", f.kind === "rapids" ? `Rapid${f.grade ? `, grade ${f.grade}` : ""}` : "Waterfall"],
      ["Height", f.height ? `${f.height} m` : "Not mapped"],
      ["Height source", f.height ? (f.linz ? "LINZ Topo50, imported into OpenStreetMap" : "OpenStreetMap contributors") : "–"],
    ];
    if (reach) rows.push(["On", `${reach.name}: ${fmt.grad(S.g[f.seg])} m/km, ${fmt.km2(S.a[f.seg])} km², flood ${fmt.flow(S.maf[f.seg])} m³/s`]);
    const body = [
      el("div", { class: "rf-tablewrap" }, el("table", { class: "rf-table det-stats" }, el("tbody", {}, ...rows.map(([k, v]) => el("tr", {}, el("th", { scope: "row", text: k }), el("td", { text: v })))))),
      el("section", { class: "det-sec" }, el("h3", { text: "Location" }), el("div", { class: "det-coord" },
        el("div", { class: "rf-row" }, el("span", { class: "rf-muted det-k", text: "Lat, lon" }), RF.util.copyable(fmt.coord(f.lat, f.lon))),
        el("div", { class: "rf-row" }, el("span", { class: "rf-muted det-k", text: "NZTM" }), RF.util.copyable(`${Math.round(E)} E, ${Math.round(N)} N`)))),
      el("section", { class: "det-sec" }, el("h3", { text: "Sources and photos" }), el("div", { class: "det-linklist" },
        ...(osmUrl(f.osm) ? [ext(osmUrl(f.osm), "OpenStreetMap feature")] : []), ...photoLinks(f),
        ext(`https://basemaps.linz.govt.nz/@${f.lat.toFixed(5)},${f.lon.toFixed(5)},z16`, "LINZ Basemaps")))];
    if (reach) body.push(el("div", { class: "rf-row" }, el("button", { type: "button", class: "rf-btn", onclick: () => RF.detail.open(reach) }, `Open the river (${reach.name})`)));
    body.push(el("p", { class: "rf-note det-foot", text: "OpenStreetMap's waterfall coverage is patchy: no mapped waterfall does not mean no waterfall. © OpenStreetMap contributors (ODbL), via Overture Maps." }));
    return { eyebrow: f.kind === "rapids" ? "Rapid" : "Waterfall", title: f.name, subtitle: R.title, body };
  }
  RF.detail.register("waterfall", fallDetail);
  RF.detail.register("rapids", fallDetail);
})();
