/* Rain and river gauges (national): NIWA flood-statistics flow sites and NIWA CliFlo rain stations.
   Detail: operator, record length, flood statistics, coordinates to copy, links to the source and the
   operator's data. Live readings are a later step. Owner: map agent. */
(() => {
  "use strict";
  const RF = window.RF, M = RF.mapkit, { el, fmt, icon } = RF.util;
  const EMPTY = { type: "FeatureCollection", features: [] };
  const ext = (href, text) => el("a", { href, target: "_blank", rel: "noopener", class: "det-link" }, el("span", { text }), el("span", { html: icon("external"), "aria-hidden": "true" }));
  // Operators of the flow gauges: display names and where their data are published (when known).
  const OPERATORS = {
    NIWA: ["NIWA (now Earth Sciences New Zealand)", "https://hydrowebportal.niwa.co.nz/"],
    ECan: ["Environment Canterbury", "https://www.ecan.govt.nz/data/riverflow/"],
    WRC: ["Waikato Regional Council", "https://www.waikatoregion.govt.nz/"], HRC: ["Horizons Regional Council", "https://www.horizons.govt.nz/"],
    HBRC: ["Hawke's Bay Regional Council", "https://www.hbrc.govt.nz/"], GWRC: ["Greater Wellington Regional Council", "https://www.gw.govt.nz/"],
    AC: ["Auckland Council", "https://www.aucklandcouncil.govt.nz/"], ORC: ["Otago Regional Council", "https://www.orc.govt.nz/"],
    ES: ["Environment Southland", "https://www.es.govt.nz/"], NRC: ["Northland Regional Council", "https://www.nrc.govt.nz/"],
    BOPRC: ["Bay of Plenty Regional Council", "https://www.boprc.govt.nz/"], TRC: ["Taranaki Regional Council", "https://www.trc.govt.nz/"],
    TDC: ["Tasman District Council", "https://www.tasman.govt.nz/"], GDC: ["Gisborne District Council", "https://www.gdc.govt.nz/"],
    MDC: ["Marlborough District Council", "https://www.marlborough.govt.nz/"], WCRC: ["West Coast Regional Council", "https://www.wcrc.govt.nz/"],
    Genesis: ["Genesis Energy", null], Watercare: ["Watercare", null],
  };
  let G = null;

  M.defineGroup({ id: "gauges", label: "Gauges", link: "https://niwa.co.nz/" });
  M.defineLayer({ id: "g-flow", parent: "gauges", label: "River gauges", note: "Flow sites (NIWA flood statistics)", defaultOn: true, swatch: "dot",
    colors: [{ key: "c", token: "--gauge-flow" }], legend: "River-flow gauge", count: () => G?.flow.length ?? null, apply: applyGauges });
  M.defineLayer({ id: "g-rain", parent: "gauges", label: "Rain gauges", note: "NIWA CliFlo stations", defaultOn: false, swatch: "dot",
    colors: [{ key: "c", token: "--gauge-rain" }], legend: "Rain gauge (square)", count: () => G?.rain.length ?? null, apply: applyGauges });

  M.layerInits.push(() => {
    const map = M.map;
    map.addSource("src-gauges", { type: "geojson", data: EMPTY });
    const r = ["interpolate", ["linear"], ["zoom"], 6, 2.5, 10, 4.5, 14, 6.5];
    M.addLayer({ id: "g-flow-ring", type: "circle", source: "src-gauges", filter: ["==", ["get", "k"], "f"],
      paint: { "circle-radius": r, "circle-color": "#ffffff", "circle-stroke-color": "#0f8a8a", "circle-stroke-width": 3 } }, "points");
    M.addLayer({ id: "g-rain-dot", type: "symbol", source: "src-gauges", filter: ["==", ["get", "k"], "r"],
      layout: { "icon-image": "rf-square", "icon-size": ["interpolate", ["linear"], ["zoom"], 6, 0.5, 12, 0.9], "icon-allow-overlap": true } ,
      paint: { "icon-color": "#3b6fb6", "icon-halo-color": "#ffffff", "icon-halo-width": 1.5 } }, "points");
    M.addLayer({ id: "g-label", type: "symbol", source: "src-gauges", minzoom: 11,
      layout: { "text-field": ["get", "n"], "text-font": M.FONT_LABEL, "text-size": 11, "text-anchor": "top", "text-offset": [0, 0.9], "text-optional": true, "text-max-width": 10 },
      paint: { "text-color": "#24524f", "text-halo-color": "#ffffff", "text-halo-width": 1.5 } }, "labels");
    // a square marker as an SDF icon, so its colour can follow the layer colour
    const size = 20, c = document.createElement("canvas"); c.width = c.height = size;
    const ctx = c.getContext("2d"); ctx.fillStyle = "#000"; ctx.fillRect(4, 4, size - 8, size - 8);
    map.addImage("rf-square", ctx.getImageData(0, 0, size, size), { sdf: true });
    const theme = () => {
      if (!map.getLayer("g-label")) return;
      map.setPaintProperty("g-label", "text-color", M.isDark() ? "#9ad7d3" : "#24524f");
      map.setPaintProperty("g-label", "text-halo-color", M.isDark() ? "#111513" : "#ffffff");
      map.setPaintProperty("g-flow-ring", "circle-color", M.isDark() ? "#111513" : "#ffffff");
      map.setPaintProperty("g-rain-dot", "icon-halo-color", M.isDark() ? "#111513" : "#ffffff");
    };
    M.onTheme(theme); theme();
    M.clickable({ layers: ["g-flow-ring", "g-rain-dot"], priority: 96, feature: (f) => gaugeFeature(f.properties.k, f.properties.i) });
    RF.data.gauges().then((g) => {
      G = g;
      map.getSource("src-gauges").setData({ type: "FeatureCollection", features: [
        ...g.flow.map((x, i) => ({ type: "Feature", properties: { k: "f", i, n: x[1] }, geometry: { type: "Point", coordinates: [x[3], x[2]] } })),
        ...g.rain.map((x, i) => ({ type: "Feature", properties: { k: "r", i, n: titleCase(x[1]) }, geometry: { type: "Point", coordinates: [x[3], x[2]] } })),
      ] });
      applyGauges(); M.renderTree();
    });
  });

  const titleCase = (s) => String(s).toLowerCase().replace(/\b([a-z])/g, (m) => m.toUpperCase()).replace(/\b(Aws|Ews|Cws)\b/g, (m) => m.toUpperCase());

  function applyGauges() {
    if (!M.ready || !M.map.getLayer("g-flow-ring")) return;
    const f = M.isVisible("g-flow"), r = M.isVisible("g-rain");
    M.vis("g-flow-ring", f); M.vis("g-rain-dot", r); M.vis("g-label", f || r);
    M.map.setFilter("g-label", ["in", ["get", "k"], ["literal", [f ? "f" : "-", r ? "r" : "-"]]]);
    M.map.setPaintProperty("g-flow-ring", "circle-stroke-color", M.colorOf("g-flow"));
    M.map.setPaintProperty("g-rain-dot", "icon-color", M.colorOf("g-rain"));
  }

  function gaugeFeature(k, i) {
    const g = (k === "f" ? G?.flow : G?.rain)?.[i]; if (!g) return null;
    return k === "f"
      ? { kind: "gauge-flow", region: M.R?.id || null, id: `gauge-flow:${g[0]}`, name: g[1], lat: g[2], lon: g[3], g }
      : { kind: "gauge-rain", region: M.R?.id || null, id: `gauge-rain:${g[0]}`, name: titleCase(g[1]), lat: g[2], lon: g[3], g };
  }
  M.gaugeFeature = gaugeFeature;

  /** Gauges near a point, closest first: [{feature, km}] (used by the river detail view). */
  M.nearGauges = async (lat, lon, maxKm = 25, n = 3) => {
    const g = await RF.data.gauges(), k = Math.cos(lat * Math.PI / 180);
    const d = (a, b) => Math.hypot((a - lat) * 111.2, (b - lon) * 111.2 * k);
    const pick = (rows, kind) => rows.map((x, i) => ({ i, km: d(x[2], x[3]) })).filter((x) => x.km <= maxKm).sort((a, b) => a.km - b.km).slice(0, n)
      .map(({ i, km }) => { G = G || g; return { feature: gaugeFeature(kind, i), km }; });
    G = G || g;
    return { flow: pick(g.flow, "f"), rain: pick(g.rain, "r") };
  };

  M.card["gauge-flow"] = (f) => ({ eyebrow: "River gauge", title: f.name,
    lines: [el("span", { class: "num", text: `Site ${f.g[0]} · ${OPERATORS[f.g[5]]?.[0] || f.g[5]} · ${f.g[8]} years` }), RF.util.copyable(fmt.coord(f.lat, f.lon))] });
  M.card["gauge-rain"] = (f) => ({ eyebrow: "Rain gauge", title: f.name,
    lines: [el("span", { class: "num", text: `CliFlo agent ${f.g[0]} · ${f.g[4]}` }), RF.util.copyable(fmt.coord(f.lat, f.lon))] });

  function coordSec(f) {
    const [E, N] = RF.util.toNZTM(f.lat, f.lon);
    return el("section", { class: "det-sec" }, el("h3", { text: "Location" }), el("div", { class: "det-coord" },
      el("div", { class: "rf-row" }, el("span", { class: "rf-muted det-k", text: "Lat, lon" }), RF.util.copyable(fmt.coord(f.lat, f.lon))),
      el("div", { class: "rf-row" }, el("span", { class: "rf-muted det-k", text: "NZTM" }), RF.util.copyable(`${Math.round(E)} E, ${Math.round(N)} N`))));
  }
  const kv = (rows) => el("div", { class: "rf-tablewrap" }, el("table", { class: "rf-table det-stats" },
    el("tbody", {}, ...rows.map(([k, v, n]) => el("tr", {}, el("th", { scope: "row", text: k }), el("td", { class: "n", text: v }), n !== undefined ? el("td", { class: "rf-muted det-note", text: n }) : null)))));
  const showBtn = (f, ctx) => el("button", { type: "button", class: "rf-btn det-show", "aria-label": "Show on map", onclick: () => { ctx.close(); RF.go("map", { select: f }); } },
    el("span", { html: icon("pin"), "aria-hidden": "true" }), el("span", { class: "det-show-label", text: "Show on map" }));
  const live = () => RF.chart.placeholder("Live readings", "To do: live and historical readings will be integrated later. Follow the source links for current data.");

  RF.detail.register("gauge-flow", async (f, ctx) => {
    const g = f.g, src = (await RF.data.gauges()).sources?.flow, op = OPERATORS[g[5]];
    ctx.actions.append(showBtn(f, ctx));
    const links = [];
    if (op?.[1]) links.push(ext(op[1], `${op[0]}: data`));
    links.push(ext(`https://www.google.com/search?q=${encodeURIComponent(`${g[1]} river flow`)}`, "Search for current flow"));
    if (src) links.push(ext(`${src}/query?where=Siteno%3D${g[0]}&outFields=*&f=html`, "Source record (NIWA flood statistics)"));
    return {
      eyebrow: "River gauge", title: g[1], subtitle: `Site ${g[0]}`,
      body: [
        kv([["Operator", op?.[0] || g[5] || "–", ""], ["Funder", g[6] || "–", ""], ["Council area", g[4] || "–", ""],
          ["Catchment area", `${fmt.km2(g[7])} km²`, "Upstream of the gauge"], ["Years of record", String(g[8] ?? "–"), "Used for the flood statistics"],
          ["Mean annual flood", `${fmt.flow(g[9])} m³/s`, "From the gauge record (Henderson & Collins 2018)"],
          ["10-year flood", `${fmt.flow(g[10])} m³/s`, ""], ["100-year flood", `${fmt.flow(g[11])} m³/s`, ""]]),
        el("section", { class: "det-sec" }, el("h3", { text: "Flow" }), live()),
        coordSec(f),
        el("section", { class: "det-sec" }, el("h3", { text: "Sources" }), el("div", { class: "det-linklist" }, ...links)),
        el("p", { class: "rf-note det-foot", text: "Site list and flood statistics: NIWA (now Earth Sciences New Zealand), Henderson and Collins (2018), CC BY 4.0." }),
      ],
    };
  });
  RF.detail.register("gauge-rain", async (f, ctx) => {
    const g = f.g, src = (await RF.data.gauges()).sources?.rain;
    ctx.actions.append(showBtn(f, ctx));
    const links = [ext("https://cliflo.niwa.co.nz/", "NIWA CliFlo (climate database)"),
      ext(`https://www.google.com/search?q=${encodeURIComponent(`${f.name} rainfall`)}`, "Search for current rainfall")];
    if (src) links.push(ext(`${src}/query?where=AGENT_NO%3D${g[0]}&outFields=*&f=html`, "Source record (NIWA CliFlo stations)"));
    return {
      eyebrow: "Rain gauge", title: f.name, subtitle: `CliFlo agent ${g[0]}`,
      body: [
        kv([["Station", f.name, ""], ["CliFlo agent number", String(g[0]), "Use it to find the station in CliFlo"], ["Owner", g[4] || "–", ""]]),
        el("section", { class: "det-sec" }, el("h3", { text: "Rainfall" }), live()),
        coordSec(f),
        el("section", { class: "det-sec" }, el("h3", { text: "Sources" }), el("div", { class: "det-linklist" }, ...links)),
        el("p", { class: "rf-note det-foot", text: "CliFlo rain stations: NIWA (now Earth Sciences New Zealand). Regional council rain gauges are not included yet." }),
      ],
    };
  });
})();
