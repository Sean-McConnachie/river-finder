/* River layers: known rivers (guide sections, by grade and by source), predicted runs (current model,
   red low to green high), the river network (grey/white) and river names along the lines.
   Owner: map agent. */
(() => {
  "use strict";
  const RF = window.RF, M = RF.mapkit, { el, fmt } = RF.util;
  const EMPTY = { type: "FeatureCollection", features: [] };
  const GRADES = [2, 3, 4, 5, 6];
  const SOURCES = [
    ["riverguide", "NZ RiverGuide", "https://www.riverguide.co.nz/"],
    ["packrafting", "packraftingtrips.nz", "https://www.packraftingtrips.nz/"],
    ["other", "Other guides and reports", null],
    ["local", "Local knowledge", null],
  ];
  const ROMAN = { 2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI" };
  // Colour by the section's stated grade (gnum), not its hardest rapid.
  M.gradeBin = (sec) => { const g = sec.gnum ?? sec.gmin ?? sec.gmax; return g == null ? 0 : Math.max(2, Math.min(6, Math.floor(g + 1e-6))); };
  M.gradeColor = (bin) => bin ? M.colorOf(`known-g${bin}`) : M.colorOf("known-g0");
  let runs = [], runToken = 0, scored = null; // scored: {R, model} the predicted runs were found with

  // ---------- layer tree ----------
  M.defineGroup({ id: "rivers", label: "Rivers" });
  M.defineGroup({ id: "known", parent: "rivers", label: "Known rivers", note: "Sections in the guides" });
  M.defineGroup({ id: "known-grade", parent: "known", label: "By grade" });
  GRADES.forEach((g) => M.defineLayer({ id: `known-g${g}`, parent: "known-grade", label: `Grade ${ROMAN[g]}`, defaultOn: true,
    colors: [{ key: "c", token: `--grade-${g}` }], legend: `Known river, grade ${ROMAN[g]}`,
    count: () => (M.R?.sections || []).filter((s) => M.gradeBin(s) === g).length, apply: applyKnown }));
  M.defineLayer({ id: "known-g0", parent: "known-grade", label: "Grade not recorded", defaultOn: true,
    colors: [{ key: "c", token: "--muted" }], legend: "Known river, grade not recorded",
    count: () => (M.R?.sections || []).filter((s) => !M.gradeBin(s)).length, apply: applyKnown });
  M.defineGroup({ id: "known-src", parent: "known", label: "By source" });
  SOURCES.forEach(([k, label, link]) => M.defineLayer({ id: `known-src-${k}`, parent: "known-src", label, link, defaultOn: true, swatch: "none",
    count: () => (M.R?.sections || []).filter((s) => s.src === k).length, apply: applyKnown }));

  M.defineLayer({ id: "pred", parent: "rivers", label: "Predicted runs", note: "Current model, red (low) to green (high)", defaultOn: true,
    colors: [{ key: "lo", token: "--score-lo", label: "low score" }, { key: "mid", token: "--score-mid", label: "middle" }, { key: "hi", token: "--score-hi", label: "high score" }],
    legend: "Predicted run: low to high score", count: () => runs.filter((r) => r.status !== "listed").length, apply: applyPred });
  M.defineLayer({ id: "pred-listed", parent: "pred", label: "Include runs already in the guides", defaultOn: false, swatch: "none",
    count: () => runs.filter((r) => r.status === "listed").length, apply: applyPred });

  M.defineLayer({ id: "linz", parent: "rivers", label: "LINZ rivers", note: "NZ Topo50 river lines with LINZ names", defaultOn: true,
    link: "https://data.linz.govt.nz/layer/103632-nz-river-name-lines-pilot/",
    colors: [{ key: "c", token: "--river-linz" }], legend: "LINZ river (NZ River Name Lines)", count: () => M.linz?.lines.length ?? null, apply: applyLinz });
  M.defineLayer({ id: "net", parent: "rivers", label: "Modelled channels", note: "From the LINZ elevation model (shown when LINZ lines are missing)", defaultOn: false,
    colors: [{ key: "c", token: "--muted" }], legend: "Modelled channel (LINZ DEM)", count: () => M.R?.segments?.g.length ?? null, apply: applyNet });
  M.defineLayer({ id: "names", parent: "rivers", label: "River names", note: "From zoom 11", defaultOn: true, swatch: "none", apply: applyNames });

  // ---------- map layers ----------
  // "zoom" must be the input of a top-level interpolate, so extra width goes into each stop.
  const width = (base, add = 0) => ["interpolate", ["linear"], ["zoom"], 7, base * 0.45 + add, 10, base * 0.8 + add, 13, base * 1.3 + add, 16, base * 2.2 + add];
  const byScore = ["+", 0.6, ["*", 0.6, ["get", "t"]]];
  const predWidth = (add = 0) => ["interpolate", ["linear"], ["zoom"], 8, ["+", add, ["*", 2, byScore]], 13, ["+", add, ["*", 4, byScore]], 16, ["+", add, ["*", 7, byScore]]];
  const casing = () => (M.isDark() ? "#0b0f0d" : "#ffffff");

  M.layerInits = M.layerInits || [];
  M.layerInits.push(() => {
    const map = M.map;
    ["net", "linz", "known", "pred", "names"].forEach((id) => map.addSource(`src-${id}`, { type: "geojson", data: EMPTY, tolerance: 0.4 }));
    M.addLayer({ id: "linz-line", type: "line", source: "src-linz", layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": "#888", "line-opacity": 0.95,
        "line-width": ["interpolate", ["linear"], ["zoom"], 8, ["case", ["get", "named"], 1.1, 0.5], 12, ["case", ["get", "named"], 2, 1], 16, ["case", ["get", "named"], 4, 2]] } }, "network");
    M.addLayer({ id: "net-line", type: "line", source: "src-net", layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": "#888", "line-width": ["interpolate", ["linear"], ["zoom"], 8, ["interpolate", ["linear"], ["get", "w"], 0, 0.4, 1, 1.6], 14, ["interpolate", ["linear"], ["get", "w"], 0, 1, 1, 4]], "line-opacity": 0.9 } }, "network");
    M.addLayer({ id: "pred-casing", type: "line", source: "src-pred", layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": casing(), "line-width": predWidth(2), "line-opacity": 0.85 } }, "rivers");
    M.addLayer({ id: "pred-line", type: "line", source: "src-pred", layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": "#888", "line-width": predWidth() } }, "rivers");
    M.addLayer({ id: "known-casing", type: "line", source: "src-known", layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": casing(), "line-width": width(4, 2.5), "line-opacity": 0.9 } }, "rivers");
    M.addLayer({ id: "known-line", type: "line", source: "src-known", layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": "#888", "line-width": width(4) } }, "rivers");
    M.addLayer({ id: "names-label", type: "symbol", source: "src-names", minzoom: 11,
      layout: { "symbol-placement": "line", "text-field": ["get", "n"], "text-font": M.FONT_WATER, "text-size": ["interpolate", ["linear"], ["zoom"], 11, 11, 15, 14],
        "symbol-spacing": 350, "text-max-angle": 50, "text-padding": 4, "text-letter-spacing": 0.03, "text-offset": [0, -0.8] },
      paint: { "text-color": "#1f5fa8", "text-halo-color": "#fff", "text-halo-width": 1.6 } }, "labels");
    M.onTheme(() => {
      ["pred-casing", "known-casing"].forEach((l) => map.getLayer(l) && map.setPaintProperty(l, "line-color", casing()));
      if (map.getLayer("names-label")) {
        map.setPaintProperty("names-label", "text-color", M.isDark() ? "#9cc4f2" : "#1f5fa8");
        map.setPaintProperty("names-label", "text-halo-color", M.isDark() ? "#111513" : "#ffffff");
      }
    });
    if (M.isDark()) { map.setPaintProperty("names-label", "text-color", "#9cc4f2"); map.setPaintProperty("names-label", "text-halo-color", "#111513"); }

    M.clickable({ layers: ["known-line", "known-casing"], priority: 90, feature: (f) => sectionFeature(f.properties.i) });
    M.clickable({ layers: ["pred-line", "pred-casing"], priority: 80, feature: (f) => runs[f.properties.i] || null });
    M.clickable({ layers: ["net-line"], priority: 40, feature: (f) => reachFeature(f.properties.i) });
    M.clickable({ layers: ["linz-line"], priority: 45, feature: (f, e) => linzFeature(f, e) });
  });

  // ---------- data per region ----------
  const multi = (S, members) => ({ type: "MultiLineString", coordinates: RF.runs.chains(S, members) });

  function buildKnown(R) {
    const S = R.segments; if (!S) return EMPTY;
    return { type: "FeatureCollection", features: (R.sections || []).map((sec, i) => ({
      type: "Feature", properties: { i, g: M.gradeBin(sec), src: sec.src }, geometry: multi(S, sec.m) })) };
  }

  function buildNet(R) {
    const S = R.segments; if (!S) return EMPTY;
    // width 0..1 from catchment area on a log scale, 1 km² to about 3000 km²
    return { type: "FeatureCollection", features: S.c.map((c, i) => ({
      type: "Feature", properties: { i, w: Math.min(1, Math.max(0, Math.log10(Math.max(S.a[i], 1)) / 3.5)) },
      geometry: { type: "LineString", coordinates: c } })) };
  }

  // Moving average (half-window k): the D8 staircase bends too sharply for labels to follow it.
  function smooth(c, k) {
    if (c.length < 2 * k + 2) return c;
    const out = [c[0]];
    for (let i = 1; i < c.length - 1; i++) {
      const a = Math.max(0, i - k), b = Math.min(c.length - 1, i + k);
      let x = 0, y = 0; for (let j = a; j <= b; j++) { x += c[j][0]; y += c[j][1]; }
      if (i % 2 === 0) out.push([x / (b - a + 1), y / (b - a + 1)]);
    }
    out.push(c[c.length - 1]);
    return out;
  }

  // Named rivers as long lines: chain reaches downstream while the name stays the same, so labels fit.
  function buildNames(R) {
    const S = R.segments; if (!S) return EMPTY;
    const n = S.c.length, name = (i) => (S.r[i] >= 0 ? S.rivers[S.r[i]] : ""), feats = [];
    for (let i = 0; i < n; i++) {
      const nm = name(i); if (!nm) continue;
      const up = S.u[i];
      if (up >= 0 && name(up) === nm && S.d[up] === i) continue; // not the top of a chain
      const coords = []; let cur = i, guard = 0;
      while (cur >= 0 && name(cur) === nm && guard++ < 5000) {
        S.c[cur].forEach((p, k) => { if (k === 0 && coords.length) return; coords.push(p); });
        const d = S.d[cur];
        if (d < 0 || S.u[d] !== cur) break;
        cur = d;
      }
      if (coords.length > 1) feats.push({ type: "Feature", properties: { n: nm }, geometry: { type: "LineString", coordinates: smooth(coords, 6) } });
    }
    return { type: "FeatureCollection", features: feats };
  }

  async function buildPred(R) {
    const my = ++runToken, S = R.segments; if (!S) return;
    const model = RF.state.model ? await RF.data.scores(R.id, RF.state.model) : null;
    if (my !== runToken || M.R !== R) return;
    runs = RF.runs.find(R, { model });
    scored = { R, model };
    const sc = runs.map((r) => r.score), lo = Math.min(...sc), hi = Math.max(...sc);
    M.predRange = runs.length ? [lo, hi] : null;
    M.map.getSource("src-pred")?.setData({ type: "FeatureCollection", features: runs.map((r, i) => ({
      type: "Feature", properties: { i, t: hi > lo ? (r.score - lo) / (hi - lo) : 1, listed: r.status === "listed" ? 1 : 0 },
      geometry: { type: "LineString", coordinates: RF.runs.path(S, r.members) } })) });
    applyPred(); M.renderTree(); M.refreshCard("section");
  }
  M.runs = () => runs;

  // LINZ river lines (river_finder.linz): <id>_linz.json = {names, types, lines: [[type, name | -1, section id, flat lon/lat]]}
  M.linz = null;
  async function loadLinz(R) {
    let L = null;
    try { const r = await fetch(`${R.id}_linz.json`); if (r.ok) L = await r.json(); } catch (e) { L = null; }
    if (M.R !== R) return;
    M.linz = L;
    const feats = (L?.lines || []).map(([t, n, sid, flat], i) => {
      const c = []; for (let j = 0; j < flat.length; j += 2) c.push([flat[j], flat[j + 1]]);
      return { type: "Feature", properties: { i, t, n: n >= 0 ? L.names[n] : "", named: n >= 0, sid }, geometry: { type: "LineString", coordinates: c } };
    });
    M.linzFeats = feats;
    M.map.getSource("src-linz").setData({ type: "FeatureCollection", features: feats });
    // labels: LINZ names (their lines are smooth Topo50 centrelines); REC2 chains when there is no LINZ file
    M.map.getSource("src-names").setData(L ? { type: "FeatureCollection", features: feats.filter((f) => f.properties.named).map((f) => ({ type: "Feature", properties: { n: f.properties.n }, geometry: f.geometry })) } : buildNames(R));
    applyLinz(); applyNet(M.isVisible("net"), (k) => M.colorOf("net", k)); M.renderTree();
  }

  M.onRegion((R) => {
    M.linzFeats = null;
    if (!M.map.getSource("src-known")) return;
    M.map.getSource("src-known").setData(buildKnown(R));
    M.map.getSource("src-net").setData(buildNet(R));
    M.linz = null; M.map.getSource("src-linz").setData(EMPTY); M.map.getSource("src-names").setData(EMPTY);
    loadLinz(R);
    runs = []; scored = null; M.map.getSource("src-pred").setData(EMPTY);
    buildPred(R);
  });
  RF.on("model", () => { if (M.R && M.ready) buildPred(M.R); });
  RF.on("runs", () => { if (M.R && M.ready) buildPred(M.R); });

  // ---------- styling (visibility, colours, filters) ----------
  function applyKnown() {
    if (!M.ready || !M.map.getLayer("known-line")) return;
    const bins = [0, ...GRADES].filter((g) => M.isVisible(`known-g${g}`));
    const srcs = SOURCES.map(([k]) => k).filter((k) => M.isVisible(`known-src-${k}`));
    const filter = ["all", ["in", ["get", "g"], ["literal", bins]], ["in", ["get", "src"], ["literal", srcs]]];
    const color = ["match", ["get", "g"], ...GRADES.flatMap((g) => [g, M.gradeColor(g)]), M.gradeColor(0)];
    const on = M.isVisible("known") && bins.length > 0 && srcs.length > 0;
    ["known-line", "known-casing"].forEach((l) => { M.map.setFilter(l, filter); M.vis(l, on); });
    M.map.setPaintProperty("known-line", "line-color", color);
  }
  function applyPred() {
    if (!M.ready || !M.map.getLayer("pred-line")) return;
    const on = M.isVisible("pred"), listed = M.isVisible("pred-listed");
    const f = listed ? ["all"] : ["==", ["get", "listed"], 0];
    ["pred-line", "pred-casing"].forEach((l) => { M.map.setFilter(l, f); M.vis(l, on); });
    M.map.setPaintProperty("pred-line", "line-color", ["interpolate", ["linear"], ["get", "t"], 0, M.colorOf("pred", "lo"), 0.5, M.colorOf("pred", "mid"), 1, M.colorOf("pred", "hi")]);
  }
  function applyNet(on, colorOf) {
    if (!M.ready || !M.map.getLayer("net-line")) return;
    // stand in for the LINZ lines where a region has none (no LDS key at build time)
    M.vis("net-line", on || (M.isVisible("linz") && !M.linz));
    M.map.setPaintProperty("net-line", "line-color", on ? colorOf("c") : M.colorOf("linz"));
  }
  function applyLinz() {
    if (!M.ready || !M.map.getLayer("linz-line")) return;
    M.vis("linz-line", M.isVisible("linz"));
    M.map.setPaintProperty("linz-line", "line-color", M.colorOf("linz"));
  }
  function applyNames(on) { if (M.ready) M.vis("names-label", on); }

  // ---------- feature references, cards ----------
  function sectionFeature(i) {
    const sec = M.R?.sections?.[i]; if (!sec) return null;
    return { kind: "section", region: M.R.id, id: sec.id, name: sec.river, section: sec.section, grade: sec.grade, src: sec.src, members: sec.m, sec };
  }
  /** The modelled reach nearest a point (within maxM metres), or -1. */
  function nearestReach(S, lon, lat, maxM = 300) {
    const k = Math.cos(lat * Math.PI / 180), lim = (maxM / 111200) ** 2;
    let best = lim, bi = -1;
    for (let i = 0; i < S.c.length; i++) {
      const c = S.c[i];
      for (let j = 0; j < c.length; j++) {
        const dx = (c[j][0] - lon) * k, dy = c[j][1] - lat, d = dx * dx + dy * dy;
        if (d < best) { best = d; bi = i; }
      }
    }
    return bi;
  }
  M.nearestReach = nearestReach;
  const LINZ_TYPE = { river_cl: "river", canal_cl: "canal" };
  function linzFeature(f, e) {
    const p = f.properties, S = M.R?.segments, type = LINZ_TYPE[M.linz?.types?.[p.t]] || "river";
    // the whole LINZ line (rendered features are clipped to map tiles)
    f = { properties: p, geometry: M.linzFeats?.[p.i]?.geometry || f.geometry };
    const linz = { name: p.n || "", sid: p.sid, type };
    const seg = S ? nearestReach(S, e.lngLat.lng, e.lngLat.lat) : -1;
    const name = p.n || (seg >= 0 && S.r[seg] >= 0 ? S.rivers[S.r[seg]] : `Unnamed ${type}`);
    if (seg >= 0) return { kind: "reach", region: M.R.id, id: `${M.R.id}:reach:${seg}`, seg, name, linz, geometry: f.geometry };
    const a = M.areaAt(e.lngLat);
    return { kind: "linz-river", region: M.R.id, id: `linz:${p.sid}`, name, linz, lat: e.lngLat.lat, lon: e.lngLat.lng, geometry: f.geometry,
      km2: a ? Math.max(a.km2, a.channel?.km2 || 0) : null };
  }

  function reachFeature(i) {
    const S = M.R?.segments; if (!S) return null;
    return { kind: "reach", region: M.R.id, id: `${M.R.id}:reach:${i}`, seg: i, name: S.r[i] >= 0 ? S.rivers[S.r[i]] : "Unnamed stream" };
  }
  M.sectionFeature = sectionFeature; M.reachFeature = reachFeature;

  const SRC_LABEL = Object.fromEntries(SOURCES.map(([k, l]) => [k, l]));
  // The numbers a predicted run shows, from the section's reaches and the model the runs were found with
  // (no score until those scores have loaded). A section placed by river name can be separate pieces.
  M.card.section = (f) => {
    const s = f.sec, ready = scored?.R === M.R, st = RF.runs.measure(M.R, s.m, { model: ready ? scored.model : null });
    return {
      eyebrow: `Known river · ${SRC_LABEL[s.src] || s.srcName}`, title: s.section ? `${s.river}: ${s.section}` : s.river,
      lines: [el("span", { class: "rf-row" },
        el("span", { class: "rf-pill", style: `background:${M.gradeColor(M.gradeBin(s))};color:#fff;border-color:transparent`, text: s.grade ? `Grade ${s.grade}` : "Grade not recorded" })),
      st ? M.runLine(ready ? st : Object.assign(st, { score: null }), { mapped: true }) : el("span", { class: "rf-note", text: "No modelled reaches here." }),
      st?.pieces > 1 ? el("span", { class: "rf-note", text: `Mapped as ${st.pieces} separate pieces: length is their total, flow and area are for the largest.` }) : null],
    };
  };
  M.card.reach = (f) => {
    const S = M.R.segments, i = f.seg;
    return { eyebrow: f.linz ? `LINZ ${f.linz.type} · numbers from the nearest modelled reach` : "Modelled channel · one reach", title: f.name,
      lines: [el("span", { class: "num", text: `${fmt.grad(S.g[i])} m/km · flood ${fmt.flow(S.maf[i])} m³/s · ${fmt.km2(S.a[i])} km² · ${Math.round(S.zt[i])} m` })] };
  };
})();

