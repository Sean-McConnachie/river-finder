/* Full-screen river details (RF.detail renderers for "section", "run" and "reach"): summary, a stats
   table, the long profile (elevation and height-to-area against distance), a flow-rate placeholder,
   put-in / take-out coordinates, hazards and links to sources. Renderers load their region through
   RF.data, so they work from any page. Owner: map agent. */
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

  /** Reaches in downstream order: keep a joined path, otherwise sort by elevation (high to low). */
  function ordered(S, members) {
    const joined = members.every((m, i) => i === 0 || S.d[members[i - 1]] === m);
    return joined ? members.slice() : members.slice().sort((a, b) => S.zt[b] - S.zt[a]);
  }
  /** A reach's named river (or ±20 reaches of main stem when unnamed), downstream order. */
  function stretch(S, seg) {
    const nm = S.r[seg], same = (i) => (nm >= 0 ? S.r[i] === nm : true);
    const up = []; let cur = seg, n = 0;
    while (S.u[cur] >= 0 && same(S.u[cur]) && S.d[S.u[cur]] === cur && n++ < (nm >= 0 ? 4000 : 20)) { cur = S.u[cur]; up.push(cur); }
    const down = []; cur = seg; n = 0;
    while (S.d[cur] >= 0 && same(S.d[cur]) && n++ < (nm >= 0 ? 4000 : 20)) { cur = S.d[cur]; down.push(cur); }
    return [...up.reverse(), seg, ...down];
  }

  function stats(S, ms) {
    const L = ms.reduce((t, m) => t + S.len[m], 0), first = ms[0], last = ms[ms.length - 1];
    const zTop = S.zt[first], zBot = S.zb[last], drop = zTop - zBot;
    let falls = 0, fallsH = 0;
    if (S.fallsN) ms.forEach((m) => { falls += S.fallsN[m]; fallsH = Math.max(fallsH, S.fallsH[m]); });
    return { L, zTop, zBot, drop, grad: L > 0 ? drop / L * 1000 : NaN, s50: Math.max(...ms.map((m) => S.s50[m])),
      mel: median(ms.map((m) => S.mel[m])), maf: Math.max(...ms.map((m) => S.maf[m])), aTop: S.a[first], aBot: Math.max(...ms.map((m) => S.a[m])),
      road: S.dRoad ? [S.dRoad[first], S.dRoad[last]] : null, foot: S.dFoot ? Math.min(...ms.map((m) => S.dFoot[m])) : null, falls, fallsH, first, last };
  }

  function statsTable(st, score) {
    const rows = [
      ["Length", `${(st.L / 1000).toFixed(1)} km`, ""],
      ["Average gradient", `${fmt.grad(st.grad)} m/km`, `${fmt.int(st.drop)} m drop over the length`],
      ["Max gradient", `${fmt.grad(st.s50)} m/km`, "Steepest 50 m of channel"],
      ["Height-to-area", isFinite(st.mel) ? st.mel.toFixed(3) : "–", "Melton ratio: catchment relief ÷ √area (median)"],
      ["Flow (mean annual flood)", `${fmt.flow(st.maf)} m³/s`, "NIWA statistic at the bottom, not live flow"],
      ["Catchment area", `${fmt.km2(st.aTop)} → ${fmt.km2(st.aBot)} km²`, "Top → bottom"],
      ["Elevation", `${fmt.int(st.zTop)} → ${fmt.int(st.zBot)} m`, "NZVD2016"],
    ];
    if (score) rows.push(["Model score", `${score.pct.toFixed(score.pct >= 99 ? 1 : 0)} percentile`, `${score.model}: nationally, higher is more like the known runs`]);
    if (st.road) rows.push(["Road access", `${fmt.dist(st.road[0])} · ${fmt.dist(st.road[1])}`, "From the top · from the bottom (OpenStreetMap)"]);
    if (st.foot !== null && st.foot !== undefined) rows.push(["Nearest foot track", fmt.dist(st.foot), "Along the section"]);
    if (st.falls !== undefined && st.road) rows.push(["Mapped waterfalls", st.falls ? `${st.falls}${st.fallsH ? `, tallest ${st.fallsH} m` : ""}` : "None mapped", "OpenStreetMap; coverage is patchy"]);
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
      ext(`https://basemaps.linz.govt.nz/@${lat.toFixed(5)},${lon.toFixed(5)},z14`, "LINZ Basemaps"),
      ext(`https://www.openstreetmap.org/?mlat=${lat.toFixed(5)}&mlon=${lon.toFixed(5)}#map=14/${lat.toFixed(5)}/${lon.toFixed(5)}`, "OpenStreetMap"),
      ext(`https://www.google.com/maps/search/?api=1&query=${lat.toFixed(5)},${lon.toFixed(5)}`, "Google Maps")]]);
    groups.push(["Videos and web", [
      ext(`https://www.youtube.com/results?search_query=${q(`${name} ${sec?.section || ""} kayak`.trim())}`, "Search videos"),
      ext(`https://www.google.com/search?q=${q(`${name} whitewater kayak New Zealand`)}`, "Search the web")]]);
    return el("div", { class: "det-links" }, ...groups.map(([h, items]) => el("div", { class: "det-linkgroup" }, el("h4", { text: h }), el("div", { class: "det-linklist" }, ...items))));
  }

  const section = (title, ...kids) => el("section", { class: "det-sec" }, el("h3", { text: title }), ...kids);

  async function riverDetail(f, ctx) {
    const R = await RF.data.region(f.region), S = R.segments;
    let ms, sec = null, highlight;
    if (f.kind === "section") { sec = f.sec || R.sections.find((x) => x.id === f.id); ms = ordered(S, sec.m); }
    else if (f.kind === "run") ms = f.members.slice();
    else { ms = stretch(S, f.seg); highlight = f.seg; }
    const st = stats(S, ms);
    let score = null;
    if (RF.state.model) {
      const sc = await RF.data.scores(R.id, RF.state.model);
      if (sc.pct?.length) score = { pct: Math.max(...ms.map((m) => sc.pct[m])),
        model: sc.fallback ? "Pooled regression (this build has no per-model scores)" : RF.models.get(RF.state.model)?.title || RF.state.model };
    }
    const onSection = f.kind !== "section" ? R.sections.find((x) => ms.some((m) => x.m.includes(m))) : null;
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
    if (sec?.popularity) pills.append(el("span", { class: "rf-pill", text: sec.popularity }));
    if (sec?.time) pills.append(el("span", { class: "rf-pill", text: sec.time }));
    const desc = sec?.desc ? [el("p", { class: "det-desc", text: sec.desc }), el("p", { class: "rf-note", text: SRC_NOTE[sec.src] || "" })]
      : [el("p", { class: "det-desc", text: f.kind === "section" ? "No description recorded." : onSection ? "Part of this stretch is a section in the guides (see above); open it from the map for its description." : "Not in the guides." })];
    const gen = el("p", { class: "rf-note", text: `${(st.L / 1000).toFixed(1)} km at an average ${fmt.grad(st.grad)} m/km, dropping ${fmt.int(st.drop)} m from ${fmt.int(st.zTop)} m to ${fmt.int(st.zBot)} m. Catchment ${fmt.km2(st.aTop)} → ${fmt.km2(st.aBot)} km²; mean annual flood ${fmt.flow(st.maf)} m³/s at the bottom.${f.kind === "reach" ? ` The profile covers ${f.name === "Unnamed stream" ? "the main stem around the reach" : `the ${f.name} channel`}.` : ""}` });

    // coordinates
    const topLL = S.c[st.first][0], botLL = S.c[st.last][S.c[st.last].length - 1];
    const coords = [];
    if (sec?.putIn) coords.push(coordBlock("Put-in", sec.putIn[0], sec.putIn[1], sec.putInDesc, sec.coords === "inferred" ? "Worked out from directions, not published: check before relying on it." : null));
    else coords.push(coordBlock(f.kind === "section" ? "Top of the mapped section" : "Top", topLL[1], topLL[0], null, null));
    if (sec?.takeOut) coords.push(coordBlock("Take-out", sec.takeOut[0], sec.takeOut[1], sec.takeOutDesc, sec.coords === "inferred" ? "Worked out from directions, not published: check before relying on it." : null));
    else coords.push(coordBlock(f.kind === "section" ? "Bottom of the mapped section" : "Bottom", botLL[1], botLL[0], null, null));
    const midC = S.c[ms[ms.length >> 1]][0];

    const body = [
      el("div", { class: "det-summary" }, pills, ...desc, gen),
      section("Key numbers", statsTable(st, score)),
      section("Long profile", ...profile(S, ms, highlight)),
      section("Flow rate", RF.chart.placeholder("Flow rate over time",
        `To do: live and historical flow need the river gauges (planned). For now the best guide is the NIWA mean annual flood at the bottom: ${fmt.flow(st.maf)} m³/s.`)),
      section("Put-in and take-out", el("div", { class: "det-coords" }, ...coords)),
    ];
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
