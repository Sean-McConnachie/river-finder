/* Rain (live rain, RF.rain in core/rain.js): the modelled reaches coloured by upstream rain, the mean depth over
   each reach's whole catchment, in the past or next 1, 3, 6, 12 or 24 h (ECMWF model); a rain line in river
   cards and a Rain section in river Details (every window, the times, the caveats and the attribution).
   Drawn from the modelled-channel features (M.netData, source src-net: one per reach, in viewer order, the order
   of the rain files) with the rain added as integer tenths of a mm (o<x> past, f<x> next, -1 no value), so
   switching the window only repaints. Off by default; the artifact host cannot fetch it (a note says so).
   Used by 40-select.js (M.rain.lines) and 60-detail.js (M.rain.section). Owner: map agent. */
(() => {
  "use strict";
  const RF = window.RF, M = RF.mapkit, Rn = RF.rain, { el } = RF.util;
  const WINDOWS = Rn.windows, KINDS = ["run", "section", "reach"], EMPTY = { type: "FeatureCollection", features: [] };
  const SITE_URL = "https://sean-mcconnachie.github.io/river-finder/";
  // Classes by lower bound, mm: trace (0.1, faint), then 0.5, 2, 5, 10, 25, 50, 100+. Values are in tenths.
  const LABELS = ["0.1", "0.5", "2", "5", "10", "25", "50", "100+"];
  const TENTHS = [1, 5, 20, 50, 100, 250, 500, 1000];
  // Sequential green-blue (like ColorBrewer GnBu), in equal OKLCH lightness steps: light to dark on the light theme,
  // dim to bright on the dark one; blue, not violet, so it is not read as the grades of the known rivers. Each line
  // has a casing (white / near-black), so it reads on terrain, satellite and the catchment maps.
  const RAMP = {
    light: ["#c9efd9", "#8ddabf", "#4ec3bd", "#00a4bd", "#067fbb", "#1a5ea7", "#1f4184", "#16255c"],
    dark: ["#264a41", "#2c675b", "#27847f", "#299dac", "#4db2db", "#86c6f6", "#b4d8ff", "#dfedfe"],
  };
  const ramp = () => RAMP[M.isDark() ? "dark" : "light"];
  const classOf = (mm) => { if (mm == null || !isFinite(mm)) return -1; const t = Math.round(mm * 10); let k = -1; TENTHS.forEach((b, i) => { if (t >= b) k = i; }); return k; };

  M.prefs.rain = Object.assign({ mode: "past", x: 24 }, M.prefs.rain || {});
  if (!WINDOWS.includes(M.prefs.rain.x)) M.prefs.rain.x = 24;
  if (M.prefs.rain.mode !== "next") M.prefs.rain.mode = "past";
  const P = () => M.prefs.rain;
  const key = () => `${P().mode === "next" ? "f" : "o"}${P().x}`;
  let drawn = null, drawnInto = null; // the RF.rain data written into M.netData, and that M.netData

  // ---------- helpers ----------
  /** The reach whose rain describes a selection: the reach itself, or the bottom of a run or section (of its
   *  largest piece when it is mapped in several). */
  function outlet(f, R) {
    const S = R?.segments; if (!S) return null;
    if (f.kind === "reach") return f.seg >= 0 && f.seg < S.d.length ? { reach: f.seg, pieces: 1 } : null;
    const ms = (f.members || f.sec?.m || []).filter((m) => m >= 0 && m < S.d.length);
    if (!ms.length) return null;
    const parts = RF.runs.pieces(S, ms);
    return { reach: parts[0].bottom, pieces: parts.length };
  }
  /** National regions whose box holds the middle of a reference area (they carry its rain). */
  function nationalFor(R) {
    const b = R?.bounds; if (!b) return [];
    const la = (b[0][0] + b[1][0]) / 2, lo = (b[0][1] + b[1][1]) / 2;
    return RF.data.regions().filter((r) => !/reference/i.test(r.group || "") && r.bounds[0][0] <= la && la <= r.bounds[1][0] && r.bounds[0][1] <= lo && lo <= r.bounds[1][1]);
  }
  const refHint = (R) => { const n = nationalFor(R).map((r) => r.title); return n.length ? ` ${n.join(" and ")} ${n.length > 1 ? "have" : "has"} rain for this area.` : ""; };
  /** Buttons that open the national regions holding a reference area, at the same view (from Details too). */
  const refButtons = (R) => nationalFor(R).map((r) => el("button", { type: "button", class: "rf-btn map-rain-open", onclick: () => {
    if (RF.detail.isOpen()) RF.detail.close();
    if (M.ready) M.openRegion(r.id, { fit: false }); else RF.setRegion(r.id).catch(() => {});
    if (RF.state.page !== "map") RF.go("map");
  } }, `Open ${r.title}`));
  const span = (data, kind = P().mode) => Rn.span(data, kind, P().x);
  /** "Past 24 h to 07:00 NZDT" / "Next 6 h from 10:00 NZDT". */
  function label(data, kind = P().mode) {
    const t = span(data, kind), x = P().x;
    return kind === "past" ? `Past ${x} h${t ? ` to ${Rn.time(t.end)}` : ""}` : `Next ${x} h${t ? ` from ${Rn.time(t.start)}` : ""}`;
  }
  const ageSpan = (data) => el("span", { dataset: { rainAge: data.head.generated || "" }, text: Rn.age(data.generated) });
  const stateHere = () => (M.R ? Rn.state(M.R.id) : { status: "idle" });
  function request(id) { const s = Rn.state(id); if (s.status === "idle" || s.status === "error") Rn.region(id).catch(() => {}); }

  // ---------- layer tree, legend ----------
  if (Rn.enabled) {
    M.defineGroup({ id: "rain", label: "Rain", defaultOn: false, note: groupNote, controls });
    M.defineLayer({ id: "rain-net", parent: "rain", label: "Upstream rain on the rivers", note: "Mean depth over each river's catchment", defaultOn: true,
      swatch: "none", rowKey: () => scale(false), legendBlock: legend, mini: true, apply: sync });
  } else if (RF.config.target === "artifact") {
    M.layerNotes.push("Live rain (past and next 24 h, per river) is on the standalone site only: this host blocks outside data.");
  }

  function groupNote() {
    if (!M.isOn("rain")) return "Off · ECMWF model, past or next 1 to 24 h";
    const st = stateHere();
    if (st.status === "ok") return `${label(st.data)} · updated ${Rn.age(st.data.generated)}`;
    if (st.status === "error") return st.code === "reference" ? `${st.message}${refHint(M.R)}` : st.message;
    return "Loading live rain…";
  }

  function controls() {
    const seg = (group, aria, items, cur) => el("div", { class: "map-seg", role: "radiogroup", "aria-label": aria, dataset: { group } },
      ...items.map(([v, text, a]) => el("button", { type: "button", role: "radio", "aria-checked": String(v === cur), "aria-label": a || null, dataset: { v: String(v) },
        onclick: () => set(group === "mode" ? { mode: v } : { x: v }) }, text)));
    return el("div", { class: "map-rain-ctl" },
      seg("mode", "Rain: past or next hours", [["past", "Past"], ["next", "Next"]], P().mode),
      el("span", { class: "map-rain-ctl-x" }, seg("x", "Hours", WINDOWS.map((x) => [x, String(x), `${x} hours`]), P().x), el("span", { class: "map-rain-unit", text: "h" })));
  }
  function set(patch) {
    Object.assign(M.prefs.rain, patch); M.savePrefs();
    document.querySelectorAll(".map-rain-ctl .map-seg").forEach((g) => g.querySelectorAll("button").forEach((b) => {
      b.setAttribute("aria-checked", String(b.dataset.v === String(g.dataset.group === "mode" ? P().mode : P().x)));
    }));
    paint(); M.updateNote("rain"); M.renderLegend(); M.refreshCard();
  }

  /** The colour steps with their lower bounds (labels: false for the small key in the tree row). */
  function scale(labels = true) {
    const r = ramp();
    return el("span", { class: `map-rain-scale${labels ? "" : " map-rain-scale--key"}`, role: labels ? "img" : null, "aria-hidden": labels ? null : "true",
      "aria-label": labels ? `Colour steps from ${LABELS.join(", ")} mm` : null },
    ...r.map((c, k) => el("span", { class: "map-rain-step" }, el("span", { class: `map-rain-sw${k === 0 ? " map-rain-sw--trace" : ""}`, style: `--c:${c}` }),
      labels ? el("span", { class: "map-rain-lab num", text: LABELS[k] }) : null)));
  }

  function legend() {
    const st = stateHere(), title = (t) => el("div", { class: "map-legend-title", text: t });
    if (st.status === "ok") {
      return el("div", { class: "map-legend-block map-rain-legend" }, title(`Rain, ${label(st.data).replace(/^P|^N/, (c) => c.toLowerCase())}, mm over the catchment`), scale(),
        el("p", { class: "map-rain-note" }, "ECMWF model, not gauged · updated ", ageSpan(st.data)));
    }
    const msg = st.status === "error" ? st.message : "Loading live rain…";
    return el("div", { class: "map-legend-block map-rain-legend" }, title("Rain"), el("p", { class: "map-rain-note", text: msg }),
      st.code === "reference" ? el("div", { class: "rf-row" }, ...refButtons(M.R)) : null);
  }

  // ---------- on the map ----------
  // "zoom" must be the input of a top-level interpolate, so the data terms go into each stop (w: 0..1 by catchment area).
  // The core is narrower than the lines of known rivers and predicted runs, which it is drawn over.
  const width = (add) => ["interpolate", ["linear"], ["zoom"], 7, ["+", 0.7 + add * 0.7, ["*", 1.1, ["get", "w"]]], 10, ["+", 0.9 + add, ["*", 1.5, ["get", "w"]]],
    13, ["+", 1.3 + add, ["*", 2.2, ["get", "w"]]], 16, ["+", 2 + add, ["*", 3.2, ["get", "w"]]]];
  // Zoomed out the larger rivers only (w = log10(km²) / 3.5). Below zoom 8 they come from a source of their own
  // (rivers from 30 km², simplified less): the tiler drops lines shorter than its tolerance, and at region scale that
  // is most reaches of src-net. From zoom 8 the modelled channels themselves: from 8 km² below zoom 9, then all.
  const LO_MAXZOOM = 8, W_LO = 0.422, W_MID = 0.258;
  const LAYERS = ["rain-casing-lo", "rain-line-lo", "rain-casing", "rain-line"], LINES = ["rain-line-lo", "rain-line"];
  M.layerInits.push(() => {
    if (!Rn.enabled) return;
    const map = M.map;
    map.addSource("src-rain-lo", { type: "geojson", data: EMPTY, tolerance: 0.15, maxzoom: LO_MAXZOOM });
    // casing over the modelled channels (under their arrows) but under known rivers and predicted runs; the coloured
    // core over those too (under the arrows of known rivers), so a run shows its own colour round the rain
    const netFlow = map.getLayer("net-flow") ? "net-flow" : undefined, knownFlow = map.getLayer("known-flow") ? "known-flow" : undefined;
    const def = (id, core, lo) => Object.assign({ id, type: "line", source: lo ? "src-rain-lo" : "src-net",
      layout: { visibility: "none", "line-cap": "round", "line-join": "round", "line-sort-key": ["get", "w"] },
      paint: core ? { "line-color": "rgba(0,0,0,0)", "line-width": width(0) } : { "line-color": M.casing(), "line-width": width(2.2), "line-opacity": 0 } },
    lo ? { maxzoom: LO_MAXZOOM } : { minzoom: LO_MAXZOOM, filter: ["any", [">=", ["zoom"], 9], [">=", ["get", "w"], W_MID]] });
    M.addLayer(def("rain-casing-lo", false, true), "network", netFlow);
    M.addLayer(def("rain-casing", false, false), "network", netFlow);
    M.addLayer(def("rain-line-lo", true, true), "rivers", knownFlow);
    M.addLayer(def("rain-line", true, false), "rivers", knownFlow);
    // a tap on a coloured reach selects it (dry reaches are drawn transparent: not a target)
    M.clickable({ layers: LINES, priority: 44, feature: (f, e, hits) => {
      const k = key(), h = (hits || [f]).find((x) => (x.properties[k] ?? -1) >= TENTHS[0]);
      return h ? M.reachFeature(h.properties.i) : null;
    } });
    M.onBasemap(paint);
  });

  function paint() {
    const map = M.map; if (!map?.getLayer("rain-line")) return;
    const v = ["coalesce", ["get", key()], -1], r = ramp(), sat = M.isImagery(M.prefs.basemap);
    LINES.forEach((id) => {
      map.setPaintProperty(id, "line-color", ["step", v, "rgba(0,0,0,0)", ...TENTHS.flatMap((t, k) => [t, r[k]])]);
      map.setPaintProperty(id, "line-opacity", ["step", v, 0, TENTHS[0], 0.6, TENTHS[1], 1]);
    });
    ["rain-casing-lo", "rain-casing"].forEach((id) => {
      map.setPaintProperty(id, "line-color", M.casing());
      map.setPaintProperty(id, "line-opacity", ["step", v, 0, TENTHS[1], sat ? 0.95 : 0.8]);
    });
  }

  /** Write a region's rain into the modelled-channel features (one per reach, same order; the zoomed-out source shares
   *  the larger ones) and redraw them. */
  function write(data) {
    const fc = M.netData;
    if (!fc || !M.R || data.id !== M.R.id || fc.features.length !== data.n) return false;
    const c = data.cols, cols = WINDOWS.flatMap((x) => [[`o${x}`, c[`obs_mm_${x}h`]], [`f${x}`, c[`fc_mm_${x}h`]]]);
    for (let i = 0; i < data.n; i++) {
      const p = fc.features[i].properties;
      for (const [k, col] of cols) {
        const v = col == null ? NaN : typeof col === "number" ? col : col[i];
        p[k] = isFinite(v) ? Math.round(v * 10) : -1;
      }
    }
    M.map.getSource("src-net")?.setData(fc);
    M.map.getSource("src-rain-lo")?.setData({ type: "FeatureCollection", features: fc.features.filter((f) => f.properties.w >= W_LO) });
    drawn = data; drawnInto = fc;
    return true;
  }

  /** Show the layer when it is on and this region's rain is loaded (asks for it otherwise). */
  function sync() {
    if (!M.ready || !M.map.getLayer("rain-line")) return;
    const on = M.isVisible("rain-net");
    if (on && M.R) request(M.R.id);
    const st = stateHere();
    if (on && st.status === "ok" && (drawn !== st.data || drawnInto !== M.netData)) write(st.data);
    const show = on && st.status === "ok" && drawn === st.data && drawnInto === M.netData;
    LAYERS.forEach((id) => M.vis(id, show));
    if (show) paint();
  }

  RF.on("rain", ({ region, tick }) => {
    if (!M.R || region !== M.R.id) return;
    if (tick) { // ages moved on
      document.querySelectorAll("[data-rain-age]").forEach((e) => { if (e.dataset.rainAge) e.textContent = Rn.age(e.dataset.rainAge); });
      M.updateNote("rain");
      return;
    }
    sync(); M.updateNote("rain"); M.renderLegend(); M.refreshCard();
  });
  M.onRegion(() => { drawn = null; drawnInto = null; M.map?.getSource("src-rain-lo")?.setData(EMPTY); });

  // ---------- river card ----------
  const dot = (mm) => { const k = classOf(mm); return el("span", { class: `map-rain-dot${k < 0 ? " map-rain-dot--dry" : k === 0 ? " map-rain-sw--trace" : ""}`, style: k >= 0 ? `--c:${ramp()[k]}` : null, "aria-hidden": "true" }); };
  /** Card lines for a river selection: past and next x h at its outlet, mm and m³, the times and the source. */
  function lines(f) {
    if (!Rn.enabled || !f || !KINDS.includes(f.kind) || !M.R || (f.region && f.region !== M.R.id)) return [];
    const st = Rn.state(M.R.id);
    if (st.status === "error") return M.isVisible("rain") ? [el("span", { class: "rf-note", text: st.code === "reference" ? `${st.message}${refHint(M.R)}` : st.message })] : [];
    if (st.status !== "ok") { request(M.R.id); return [el("span", { class: "rf-note", text: "Live rain: loading…" })]; }
    const o = outlet(f, M.R), a = o && Rn.at(st.data, o.reach);
    if (!a) return [];
    const x = P().x, part = (kind, w) => el("span", { class: "map-rain-part" }, dot(w.mm),
      el("span", { class: "num", text: `${kind} ${x} h ${Rn.fmt.mm(w.mm)} mm · ${Rn.fmt.m3(w.m3)} m³` }));
    const tp = span(st.data, "past"), tn = span(st.data, "next");
    const zone = (t) => t.replace(/ NZ[DS]T$/, "");
    return [el("span", { class: "map-rain-card", title: "Upstream rain at the bottom of the selection: mean depth over its catchment, and the volume that makes" },
      el("span", { class: "map-rain-k", text: "Rain" }), el("span", { class: "map-rain-parts" }, part("past", a.past[x]), part("next", a.next[x]))),
    el("span", { class: "rf-note map-rain-when" }, `ECMWF model${tp ? ` · past to ${Rn.time(tp.end)}` : ""}${tn ? ` · next from ${tp ? zone(Rn.time(tn.start)) : Rn.time(tn.start)}` : ""} · updated `, ageSpan(st.data))];
  }

  // ---------- river Details ----------
  const ext = (href, text) => el("a", { href, target: "_blank", rel: "noopener" }, text);
  /** The Rain section of a river's Details (filled in when the rain has loaded), or null with no live rain. */
  function section(f, R) {
    const sec = (...kids) => el("section", { class: "det-sec det-rain" }, el("h3", { text: "Rain" }), ...kids);
    if (RF.config.target === "artifact") {
      return sec(el("p", { class: "rf-note" }, "Live rain (past and next 1 to 24 h, from the ECMWF model) is on the ", ext(SITE_URL, "standalone site"), ": this host blocks outside data."));
    }
    if (!Rn.enabled) return null;
    const box = el("div", { class: "det-rain-box" }, el("p", { class: "rf-muted", text: "Loading live rain…" }));
    Rn.region(R.id).then((data) => { try { fill(box, f, R, data); } catch (e) { console.error(e); box.replaceChildren(el("p", { class: "rf-note", text: Rn.unavailable })); } },
      (e) => box.replaceChildren(el("p", { class: "rf-note", text: e.code === "reference" ? `${e.message}${refHint(R)}` : e.message || Rn.unavailable }),
        e.code === "reference" ? el("div", { class: "rf-row" }, ...refButtons(R)) : null));
    return sec(box);
  }

  function fill(box, f, R, data) {
    const o = outlet(f, R), a = o && Rn.at(data, o.reach);
    if (!a) { box.replaceChildren(el("p", { class: "rf-note", text: "No modelled reaches here, so no rain total." })); return; }
    const h = data.head, tp = span(data, "past"), tn = span(data, "next"), x = P().x;
    const where = f.kind === "reach" ? "at this reach" : o.pieces > 1 ? "at the bottom of its largest piece" : `at the bottom of the ${f.kind === "run" ? "run" : "section"}`;
    const intro = el("p", { text: `Upstream rain ${where}: the mean depth over its ${M.fmtArea(a.area)} catchment, and the volume that makes. From the ECMWF weather model.` });
    const th = (t, cls, extra = {}) => el("th", Object.assign({ class: cls || null, text: t }, extra));
    const table = el("div", { class: "rf-tablewrap" }, el("table", { class: "rf-table det-rain-table" },
      el("thead", {},
        el("tr", {}, th("", null), th(`Past${tp ? `, to ${Rn.time(tp.end)}` : ""}`, "det-rain-grp", { colspan: 2, scope: "colgroup" }), th(`Next${tn ? `, from ${Rn.time(tn.start)}` : ""}`, "det-rain-grp", { colspan: 2, scope: "colgroup" })),
        el("tr", {}, th("Hours", null, { scope: "col" }), th("mm", "n", { scope: "col" }), th("m³", "n", { scope: "col" }), th("mm", "n", { scope: "col" }), th("m³", "n", { scope: "col" }))),
      el("tbody", {}, ...WINDOWS.map((w) => el("tr", { class: w === x ? "det-rain-cur" : null, "aria-current": w === x ? "true" : null },
        el("th", { scope: "row", text: `${w} h` }),
        el("td", { class: "n", text: Rn.fmt.mm(a.past[w].mm) }), el("td", { class: "n", text: Rn.fmt.m3(a.past[w].m3) }),
        el("td", { class: "n", text: Rn.fmt.mm(a.next[w].mm) }), el("td", { class: "n", text: Rn.fmt.m3(a.next[w].m3) }))))));
    const behind = h.observed?.hours_behind_now, nowT = Rn.parseTime(h.now);
    const run = h.forecast?.run, runAge = h.forecast?.run_age_hours;
    const times = el("p", { class: "rf-note" },
      tp ? `Past: the hours up to ${Rn.time(tp.end)}${behind ? `, ${behind} h before the latest whole hour (${Rn.time(nowT)})` : ""}. ` : "",
      tn ? `Next: the hours from ${Rn.time(tn.start)}${run ? `, from the ECMWF run of ${Rn.time(run)}${runAge != null ? ` (${runAge} h old)` : ""}` : ""}. ` : "",
      `Updated ${Rn.time(data.generated)} (`, ageSpan(data), ").");
    const raw = WINDOWS.every((w) => a.past[w].scale <= 1 && a.next[w].scale <= 1);
    const missing = Math.max(...WINDOWS.map((w) => Math.max(a.past[w].miss, a.next[w].miss)));
    const caveats = [
      `Model data, not gauged: the past hours are the first hours of recent ECMWF forecast runs, standing in for measured rain${raw ? ", with no rain-gauge correction yet" : ""}.`,
      tp ? `The past windows end at ${Rn.time(tp.end)}${behind ? `, ${behind} h before now: the newest hours are in neither window yet` : ""}.` : "",
      "Depths are averaged over the whole upstream catchment; m³ = depth × catchment area. Rain takes time to reach the river, longer in big catchments.",
      a.flags & 16 ? "Part of this catchment is cut off at the edge of a processing tile, so the totals here miss some of its rain." : "",
      missing ? `Up to ${missing} h had no data upstream and count as 0 mm.` : "",
    ].filter(Boolean);
    const ix = data.index || {}, lic = ix.licences || [], notes = ix.products || {};
    const prods = (kind) => [...new Set(WINDOWS.flatMap((w) => a[kind][w].products))].map((t) => notes[t] || t).join("; ") || "none";
    const source = el("p", { class: "rf-note", text: `Source of the past hours: ${prods("past")}. Of the next hours: ${prods("next")}.` });
    const attr = el("p", { class: "rf-note det-rain-attr" }, h.attribution || ix.attribution || "Rain: ECMWF IFS open data, CC BY 4.0.",
      ...(lic.length ? [" Sources: ", ...lic.flatMap((l, k) => [k ? ", " : "", ext(l.url, l.name.replace(/ \(.*\)$/, ""))]), "."] : []));
    box.replaceChildren(intro, table, times, source, el("ul", { class: "det-rain-caveats" }, ...caveats.map((t) => el("li", { text: t }))), attr);
  }

  M.rain = { lines, section, outlet, ramp, classOf, TENTHS, LABELS, sync };
})();
