/* Full-screen river details (RF.detail renderers for "section", "run" and "reach"): summary, a stats
   table (with the LINZ aerial survey over the river, 47-aerial.js), the long profile (elevation and
   height-to-area against distance), a flow-rate placeholder, put-in / take-out coordinates, hazards and
   links to sources. Renderers load their region through RF.data, so they work from any page. Owner: map agent. */
(() => {
  "use strict";
  const RF = window.RF, M = RF.mapkit, { el, fmt, icon } = RF.util;
  const SRC_NOTE = {
    riverguide: "From the NZ RiverGuide (Whitewater NZ; WWNZ and Graham Charles, NZ Whitewater 4th ed.), CC BY-SA 3.0 NZ.",
    packrafting: "Summary in our own words from packraftingtrips.nz; follow the link for the original.",
    other: "Summary in our own words; follow the links for the originals.",
    local: "Local knowledge recorded in labels/known_runs.toml.",
  };
  const ext = (href, text) => el("a", { href, target: "_blank", rel: "noopener", class: "det-link" }, el("span", { text }), el("span", { html: icon("external"), "aria-hidden": "true" }));
  const median = (a) => { const b = a.filter((v) => isFinite(v)).sort((p, q) => p - q); return b.length ? b[b.length >> 1] : NaN; };

  /** A reach's named river (or ±20 reaches of main stem when unnamed), downstream order. */
  function stretch(S, seg) {
    const nm = S.r[seg], same = (i) => (nm >= 0 ? S.r[i] === nm : true);
    const up = []; let cur = seg, n = 0;
    while (S.u[cur] >= 0 && same(S.u[cur]) && S.d[S.u[cur]] === cur && n++ < (nm >= 0 ? 4000 : 20)) { cur = S.u[cur]; up.push(cur); }
    const down = []; cur = seg; n = 0;
    while (S.d[cur] >= 0 && same(S.d[cur]) && n++ < (nm >= 0 ? 4000 : 20)) { cur = S.d[cur]; down.push(cur); }
    return [...up.reverse(), seg, ...down];
  }

  /**
   * The numbers each predicted run gets (RF.runs.measure: same parameters and model), plus the elevation range,
   * the median Melton ratio and the reaches the profile follows. A known section placed by river name can be
   * several separate pieces (n > 1): then the profile follows the main stem of the longest piece. Null: no reaches.
   */
  function stats(R, ms, model) {
    const S = R.segments, st = RF.runs.measure(R, ms, { model });
    if (!st) return null;
    const parts = RF.runs.pieces(S, ms), longest = parts.reduce((b, q) => (q.len > b.len ? q : b)), stem = longest.stem;
    return Object.assign(st, {
      n: st.pieces, simple: parts.length === 1 && stem.length === longest.members.length, stem, wholePiece: stem.length === longest.members.length,
      stemKm: stem.reduce((t, m) => t + S.len[m], 0) / 1000, zTop: S.zt[st.top], zBot: S.zb[st.bottom],
      zMax: Math.max(...ms.map((m) => S.zt[m])), zMin: Math.min(...ms.map((m) => S.zb[m])), mel: median(ms.map((m) => S.mel[m])) });
  }

  function statsTable(st, score, air) {
    // not one joined stretch: no single top → bottom, so say what each figure covers
    const many = st.n > 1, over = many ? `${st.n} separate pieces` : "the main stem and its side branches";
    const rows = [
      ["Length", `${st.len.toFixed(1)} km`, st.simple ? "" : `Total of ${over}`],
      ["Average gradient", `${fmt.grad(st.grad)} m/km`, st.simple ? `${fmt.int(st.drop)} m drop over the length` : `Length-weighted over ${over}`],
      ["Max gradient", `${fmt.grad(st.s50)} m/km`, "Steepest 50 m of channel"],
      ["Height-to-area", isFinite(st.mel) ? st.mel.toFixed(3) : "–", "Melton ratio: catchment relief ÷ √area (median)"],
      ["Flow (mean annual flood)", `${fmt.flow(st.maf)} m³/s`, `NIWA statistic at the bottom${many ? " of the largest piece" : ""}, not live flow`],
      many ? ["Catchment area", `${fmt.km2(st.aBot)} km²`, "At the bottom of the largest piece (the pieces have no common top)"]
        : ["Catchment area", `${fmt.km2(st.aTop)} → ${fmt.km2(st.aBot)} km²`, "Top → bottom"],
      many ? ["Elevation", `${fmt.int(st.zMin)} to ${fmt.int(st.zMax)} m`, "Lowest to highest point of the pieces (NZVD2016)"]
        : ["Elevation", `${fmt.int(st.zTop)} → ${fmt.int(st.zBot)} m`, "NZVD2016"],
    ];
    if (score) rows.push(["Model score", `${score.pct.toFixed(score.pct >= 99 ? 1 : 0)} percentile`, `${score.model}: nationally, higher is more like the known runs`]);
    if (st.acc) {
      rows.push(many ? ["Road access", fmt.dist(st.acc.road), "Nearest road to any of the pieces (OpenStreetMap)"]
        : ["Road access", `${fmt.dist(st.acc.putIn)} · ${fmt.dist(st.acc.takeOut)}`, "From the top · from the bottom (OpenStreetMap)"]);
      rows.push(["Nearest foot track", fmt.dist(st.acc.foot), "Along the section"]);
      rows.push(["Mapped waterfalls", st.falls ? `${st.falls}${st.fallsH ? `, tallest ${st.fallsH} m` : ""}` : "None mapped", "OpenStreetMap; coverage is patchy"]);
    }
    if (air) {
      if (!air.sv) rows.push(["Aerial photos", "None", "No LINZ aerial photos over this stretch: satellite only"]);
      else {
        const d = M.aerial.describe(air), pct = Math.round(100 * air.share);
        rows.push(["Aerial photos", d.value, `${d.name} (LINZ, pixel size and capture years)${pct < 95 ? `, over ${pct}% of the length` : ""}`
          + `${air.others ? `; ${air.others} other survey${air.others > 1 ? "s" : ""} on parts` : ""}`]);
      }
    }
    return el("div", { class: "rf-tablewrap" }, el("table", { class: "rf-table det-stats" },
      el("thead", {}, el("tr", {}, el("th", { text: "Measure" }), el("th", { class: "n", text: "Value" }), el("th", { class: "det-note", text: "Note" }))),
      el("tbody", {}, ...rows.map(([k, v, n]) => el("tr", {}, el("th", { scope: "row" }, k, n ? el("span", { class: "det-note-inline", text: n }) : null),
        el("td", { class: "n", text: v }), el("td", { class: "rf-muted det-note", text: n }))))));
  }

  function profile(S, ms, highlight) {
    const xs = [], zs = [], ha = [], rows = [];
    let cum = 0;
    ms.forEach((m, k) => {
      if (k === 0) { xs.push(0); zs.push(S.zt[m]); ha.push(S.mel[m]); }
      cum += S.len[m] / 1000;
      xs.push(cum); zs.push(S.zb[m]); ha.push(S.mel[m]);
      rows.push([cum, S.zb[m], S.g[m], S.mel[m], S.a[m]]);
    });
    const markers = [];
    if (S.fallsN) { let c = 0; ms.forEach((m) => { const mid = c + S.len[m] / 2000; if (S.fallsN[m]) markers.push({ x: mid, label: `Mapped waterfall${S.fallsH[m] ? `, ${S.fallsH[m]} m` : ""}` }); c += S.len[m] / 1000; }); }
    if (highlight !== undefined) { let c = 0; ms.forEach((m) => { if (m === highlight) markers.push({ x: c + S.len[m] / 2000, label: "The reach you selected" }); c += S.len[m] / 1000; }); }
    const chart = RF.chart.tracks({
      label: "Long profile: elevation and height-to-area against distance downstream",
      x: xs, xLabel: "Distance downstream (km)", xFmt: (v) => (v >= 10 ? v.toFixed(0) : v.toFixed(1)),
      tracks: [
        { label: "Elevation", unit: "m", y: zs, color: M.token("--accent"), area: true, height: 150, fmt: (v) => String(Math.round(v)), markers },
        { label: "Height-to-area (Melton)", unit: "", y: ha, color: M.token("--falls"), height: 90, fmt: (v) => v.toFixed(2), tipFmt: (v) => v.toFixed(3) },
      ],
    });
    const table = el("details", { class: "det-data" }, el("summary", { text: "Profile data (table)" }),
      el("div", { class: "rf-tablewrap" }, el("table", { class: "rf-table" },
        el("thead", {}, el("tr", {}, ...["km", "Elevation m", "Gradient m/km", "Height-to-area", "Area km²"].map((h) => el("th", { class: "n", text: h })))),
        el("tbody", {}, ...rows.map((r) => el("tr", {}, el("td", { class: "n", text: r[0].toFixed(2) }), el("td", { class: "n", text: Math.round(r[1]) }),
          el("td", { class: "n", text: fmt.grad(r[2]) }), el("td", { class: "n", text: r[3].toFixed(3) }), el("td", { class: "n", text: fmt.km2(r[4]) })))))));
    return [chart, table];
  }

  function coordBlock(label, lat, lon, desc, note) {
    const [E, N] = RF.util.toNZTM(lat, lon);
    return el("div", { class: "det-coord" }, el("h4", { text: label }), desc ? el("p", { text: desc }) : null,
      el("div", { class: "rf-row" }, el("span", { class: "rf-muted det-k", text: "Lat, lon" }), RF.util.copyable(fmt.coord(lat, lon))),
      el("div", { class: "rf-row" }, el("span", { class: "rf-muted det-k", text: "NZTM" }), RF.util.copyable(`${Math.round(E)} E, ${Math.round(N)} N`)),
      note ? el("p", { class: "rf-note", text: note }) : null);
  }

  function linkGroups(name, mid, sec, near, linz) {
    const [lon, lat] = mid, q = encodeURIComponent;
    const groups = [];
    if (sec?.links?.length) groups.push(["Guides and sources", sec.links.map((l) => ext(l.u, l.t))]);
    if (linz) groups.push(["River line and name", [ext("https://data.linz.govt.nz/layer/103632-nz-river-name-lines-pilot/", "LINZ NZ River Name Lines (CC BY 4.0)")]]);
    const gauges = [];
    if (sec?.gauge) gauges.push(el("span", { class: "det-gauge" }, `Gauge named in the guide: ${sec.gauge}. `, ext(`https://www.google.com/search?q=${q(`${sec.gauge} river flow gauge NZ`)}`, "Look it up")));
    const gb = ({ feature, km }) => el("button", { type: "button", class: "det-gaugebtn", onclick: () => RF.detail.open(feature) },
      el("span", { text: feature.name }), el("span", { class: "num rf-muted", text: `${km.toFixed(1)} km` }));
    if (near?.flow.length) gauges.push(el("div", { class: "det-gaugelist" }, el("span", { class: "rf-muted det-gk", text: "River gauges" }), ...near.flow.map(gb)));
    if (near?.rain.length) gauges.push(el("div", { class: "det-gaugelist" }, el("span", { class: "rf-muted det-gk", text: "Rain gauges" }), ...near.rain.map(gb)));
    if (!near?.flow.length && !near?.rain.length) gauges.push(el("span", { class: "rf-note", text: "No gauges within 25 km." }));
    groups.push(["Gauges", gauges]);
    groups.push(["Maps", [
      ext(`https://basemaps.linz.govt.nz/@${lat.toFixed(5)},${lon.toFixed(5)},z16`, "LINZ Basemaps (aerial photos)"),
      ext(`https://www.openstreetmap.org/?mlat=${lat.toFixed(5)}&mlon=${lon.toFixed(5)}#map=14/${lat.toFixed(5)}/${lon.toFixed(5)}`, "OpenStreetMap"),
      ext(`https://www.google.com/maps/search/?api=1&query=${lat.toFixed(5)},${lon.toFixed(5)}`, "Google Maps")]]);
    groups.push(["Videos and web", [
      ext(`https://www.youtube.com/results?search_query=${q(`${name} ${sec?.section || ""} kayak`.trim())}`, "Search videos"),
      ext(`https://www.google.com/search?q=${q(`${name} whitewater kayak New Zealand`)}`, "Search the web")]]);
    return el("div", { class: "det-links" }, ...groups.map(([h, items]) => el("div", { class: "det-linkgroup" }, el("h4", { text: h }), el("div", { class: "det-linklist" }, ...items))));
  }

  const section = (title, ...kids) => el("section", { class: "det-sec" }, el("h3", { text: title }), ...kids);

  // Hydro-affected reaches (river_finder.webdata.write_hydro): <id>_hydro.json, optional.
  const hydroFiles = new Map();
  function hydroFile(id) {
    if (!hydroFiles.has(id)) {
      hydroFiles.set(id, fetch(`${id}_hydro.json`).then((res) => (res.ok ? res.json() : null)).then((H) => {
        if (H) H.at = new Map(H.r.map(([m, c, sc]) => [m, [H.classes[c], H.schemes[sc]]]));
        return H;
      }).catch(() => null));
    }
    return hydroFiles.get(id);
  }
  const HYDRO_PILL = { diverted: "Hydro-diverted", regulated: "Regulated flow", reservoir: "Hydro lake or dam" };
  /** {cls, scheme, frac, note}: the hydro class over most of the flagged length of these reaches; null if none. */
  function hydroOf(H, S, ms) {
    if (!H || !H.at?.size) return null;
    const by = new Map(); let all = 0, hit = 0;
    ms.forEach((m) => {
      all += S.len[m]; const h = H.at.get(m); if (!h) return;
      hit += S.len[m]; const k = `${h[0]}|${h[1].id}`, cur = by.get(k);
      if (cur) cur.len += S.len[m]; else by.set(k, { cls: h[0], scheme: h[1], len: S.len[m] });
    });
    if (!hit) return null;
    const top = [...by.values()].sort((a, b) => b.len - a.len)[0];
    return { cls: top.cls, scheme: top.scheme, frac: hit / all, note: H.notes[top.cls] || "" };
  }
  function hydroNote(hy) {
    const part = hy.frac < 0.95 ? `${Math.round(100 * hy.frac)}% of this stretch: ` : "";
    const src = (hy.scheme.src || []).filter((u) => /^https?:\/\//i.test(u))[0];
    return el("p", { class: "rf-note det-hydro" }, `${part}${hy.note} ${hy.scheme.name}${hy.scheme.operator ? ` (${hy.scheme.operator})` : ""}. `,
      src ? ext(src, "Scheme source") : null);
  }

  async function riverDetail(f, ctx) {
    const R = await RF.data.region(f.region), S = R.segments;
    let ms, sec = null, highlight;
    if (f.kind === "section") { sec = f.sec || R.sections.find((x) => x.id === f.id); ms = sec.m.slice(); }
    else if (f.kind === "run") ms = f.members.slice();
    else { ms = stretch(S, f.seg); highlight = f.seg; }
    // scored like the predicted runs on the map: the current model's best reach
    const sc = RF.state.model ? await RF.data.scores(R.id, RF.state.model) : null;
    const st = stats(R, ms, sc?.pct?.length ? sc : null);
    if (!st) return { title: f.name, subtitle: R.title, body: el("p", { class: "rf-muted", text: "No modelled reaches here." }) };
    const many = st.n > 1;
    const score = sc?.pct?.length ? { pct: st.score,
      model: sc.fallback ? "Pooled regression (this build has no per-model scores)" : RF.models.get(RF.state.model)?.title || RF.state.model } : null;
    const onSection = f.kind !== "section" ? R.sections.find((x) => ms.some((m) => x.m.includes(m))) : null;
    const hy = hydroOf(await hydroFile(R.id), S, f.kind === "reach" ? [f.seg] : ms);
    const air = M.aerial ? M.aerial.of(await M.aerial.file(R.id), S, ms) : null; // 47-aerial.js; null without data
    const name = f.kind === "section" ? sec.river : f.name;

    // header
    const showBtn = el("button", { type: "button", class: "rf-btn det-show", "aria-label": "Show on map", onclick: () => { ctx.close(); RF.go("map", { select: f }); } },
      el("span", { html: icon("pin"), "aria-hidden": "true" }), el("span", { class: "det-show-label", text: "Show on map" }));
    ctx.actions.append(showBtn);
    const eyebrow = f.kind === "section" ? `Known river · ${sec.srcName === "riverguide" ? "NZ RiverGuide" : sec.srcName}`
      : f.kind === "run" ? (f.status === "listed" ? "Predicted run · in the guides" : f.status === "heldout" ? "Predicted run · test river" : "Predicted run · not in the guides")
        : f.linz ? `LINZ ${f.linz.type} · numbers from the modelled channel` : "Modelled channel";
    const title = f.kind === "section" && sec.section ? `${sec.river}: ${sec.section}` : name;

    // summary
    const pills = el("div", { class: "rf-row" });
    if (sec) pills.append(el("span", { class: "rf-pill det-grade", style: `--g:${M.gradeColor(M.gradeBin(sec))}`, text: sec.grade ? `Grade ${sec.grade}` : "Grade not recorded" }));
    if (f.kind === "run" && !(onSection && f.status === "candidate")) pills.append(el("span", { class: `rf-pill ${f.status === "candidate" ? "rf-pill--ok" : f.status === "heldout" ? "rf-pill--warn" : ""}`, text: f.status === "candidate" ? "Not in the guides" : f.status === "heldout" ? "Test river (held out)" : "In the guides" }));
    if (onSection) pills.append(el("span", { class: "rf-pill rf-pill--accent", text: `In the guides: overlaps ${onSection.river}${onSection.section ? ` (${onSection.section})` : ""}` }));
    if (hy && hy.frac >= 0.5) pills.append(el("span", { class: `rf-pill${hy.cls === "regulated" ? "" : " rf-pill--warn"}`, text: HYDRO_PILL[hy.cls] || hy.cls }));
    if (sec?.popularity) pills.append(el("span", { class: "rf-pill", text: sec.popularity }));
    if (sec?.time) pills.append(el("span", { class: "rf-pill", text: sec.time }));
    const desc = sec?.desc ? [el("p", { class: "det-desc", text: sec.desc }), el("p", { class: "rf-note", text: SRC_NOTE[sec.src] || "" })]
      : [el("p", { class: "det-desc", text: f.kind === "section" ? "No description recorded." : onSection ? "Part of this stretch is a section in the guides (see above); open it from the map for its description."
        : f.kind === "run" && f.status === "listed" ? "The river is in the guides, but no guide section covers this stretch." : "Not in the guides." })];
    const gen = el("p", { class: "rf-note", text: many
      ? `${st.len.toFixed(1)} km mapped in ${st.n} separate pieces, at an average ${fmt.grad(st.grad)} m/km. The largest drains ${fmt.km2(st.aBot)} km², with a mean annual flood of ${fmt.flow(st.maf)} m³/s at its bottom.`
      : `${st.len.toFixed(1)} km${st.simple ? "" : " with side branches"} at an average ${fmt.grad(st.grad)} m/km, ${st.simple ? `dropping ${fmt.int(st.drop)} m` : "the main stem dropping"} from ${fmt.int(st.zTop)} m to ${fmt.int(st.zBot)} m. Catchment ${fmt.km2(st.aTop)} → ${fmt.km2(st.aBot)} km²; mean annual flood ${fmt.flow(st.maf)} m³/s at the bottom.${f.kind === "reach" ? ` The profile covers ${f.name === "Unnamed stream" ? "the main stem around the reach" : `the ${f.name} channel`}.` : ""}` });
    const apart = many ? el("p", { class: "rf-note", text: "The pieces are not joined, so the section has no single drop, top or bottom. The long profile shows the longest piece." }) : null;
    const stemNote = st.simple ? null : el("p", { class: "rf-note",
      text: `The profile shows ${st.stemKm.toFixed(1)} of the ${st.len.toFixed(1)} km mapped: ${many ? (st.wholePiece ? "the longest piece" : "the main stem of the longest piece") : "the main stem"}.` });

    // coordinates: published put-in and take-out, else the ends of the profiled stretch
    const top = st.stem[0], bot = st.stem[st.stem.length - 1], topLL = S.c[top][0], botLL = S.c[bot][S.c[bot].length - 1];
    const ends = f.kind !== "section" ? ["Top", "Bottom"] : many ? ["Top of the longest piece", "Bottom of the longest piece"] : ["Top of the mapped section", "Bottom of the mapped section"];
    const coords = [];
    if (sec?.putIn) coords.push(coordBlock("Put-in", sec.putIn[0], sec.putIn[1], sec.putInDesc, sec.coords === "inferred" ? "Worked out from directions, not published: check before relying on it." : null));
    else coords.push(coordBlock(ends[0], topLL[1], topLL[0], null, null));
    if (sec?.takeOut) coords.push(coordBlock("Take-out", sec.takeOut[0], sec.takeOut[1], sec.takeOutDesc, sec.coords === "inferred" ? "Worked out from directions, not published: check before relying on it." : null));
    else coords.push(coordBlock(ends[1], botLL[1], botLL[0], null, null));
    const midC = S.c[st.stem[st.stem.length >> 1]][0];

    const body = [
      el("div", { class: "det-summary" }, pills, ...desc, gen, apart, hy ? hydroNote(hy) : null),
      section("Key numbers", statsTable(st, score, air)),
      section("Long profile", ...profile(S, st.stem, highlight), stemNote),
      M.rain?.section(f, R), // 57-rain.js: live rain at the outlet (null without live rain)
      section("Flow rate", RF.chart.placeholder("Flow rate over time",
        `To do: live and historical flow need the river gauges (planned). For now the best guide is the NIWA mean annual flood at the bottom${many ? " of the largest piece" : ""}: ${fmt.flow(st.maf)} m³/s.`)),
      section("Put-in and take-out", el("div", { class: "det-coords" }, ...coords)),
    ].filter(Boolean);
    if (sec?.hazards?.length || sec?.deaths) {
      body.push(section("Hazards", el("div", { class: "rf-row" }, ...(sec.hazards || []).map((h) => el("span", { class: "rf-pill rf-pill--warn", text: h }))),
        sec.deaths ? el("p", { class: "rf-note", text: `Fatalities recorded on or near this section: ${sec.deaths}. See the sources.` }) : null,
        sec.flow ? el("p", { class: "rf-note", text: `Flow notes: ${sec.flow}` }) : null));
    }
    const near = M.nearGauges ? await M.nearGauges(midC[1], midC[0]).catch(() => null) : null;
    body.push(section("Links", linkGroups(name, midC, sec, near, f.linz)));
    body.push(el("p", { class: "rf-note det-foot", text: [
      sec ? `Placed on the river network by: ${sec.how}.` : "",
      f.kind === "run" ? "A run is a stretch of consecutive reaches that pass the run-finder settings (see Models)." : "",
      "Leads from terrain data only: check access, land ownership, hazards and local knowledge before going anywhere."].filter(Boolean).join(" ") }));
    return { eyebrow, title, subtitle: R.title, body };
  }

  ["section", "run", "reach"].forEach((k) => RF.detail.register(k, riverDetail));
  M.riverDetail = riverDetail;
})();
