/* LINZ aerial photos for the selection (bead rf-aq6.29).
   Coverage: <id>_aerial.json next to the page (river_finder.webdata.write_aerial; format in web/README.md): per
   viewer reach the survey LINZ Basemaps draws on top at zoom 16, with its pixel size (GSD) and capture dates. River
   cards and the Details say which survey covers a river ("Aerial photo: 0.3 m, 2021-2024, Waikato Rural Aerial
   Photos") on every host, because that comes from data. "View aerial" (standalone site built with a Basemaps key:
   the Aerial (LINZ) base map of 20-basemaps.js) turns the photos on and zooms to the selection; a river longer than
   a screen at photo zoom gets Upstream / Downstream steps along its main line, one screen at a time.
   Owner: map agent. Used by 40-select.js (card), 60-detail.js (Details) and 90-page.js (RF.go("map", {select, aerial})):
   M.aerial.{file, of, describe, available, offers, button, lines, view, hide}. */
(() => {
  "use strict";
  const RF = window.RF, M = RF.mapkit, { el } = RF.util;
  const BASE = "linz-aerial", TOUR_ZOOM = 16, FIT_MIN = 15, FIT_MAX = 17;
  const PHOTO = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    + '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m3 16 5-5 4 4 2.5-2.5L21 19"/><circle cx="15.5" cy="8.5" r="1.5"/></svg>';
  const files = new Map(); // region id -> Promise<A | null>

  /** <id>_aerial.json, decoded: {surveys, s (survey index per reach, -1 none), pct (Uint8Array: % of each reach under
   *  any photo)}; null when the file is missing or unreadable. */
  function file(id) {
    if (!files.has(id)) {
      files.set(id, fetch(`${id}_aerial.json`).then((res) => (res.ok ? res.json() : null)).then((A) => {
        if (!A || !Array.isArray(A.s) || !Array.isArray(A.surveys)) return null;
        const pct = new Uint8Array(A.s.length);
        for (let i = 0; i < A.s.length; i++) pct[i] = A.s[i] >= 0 ? 100 : 0;
        (A.part || []).forEach(([i, p]) => { if (i >= 0 && i < pct.length) pct[i] = p; });
        A.pct = pct;
        return A;
      }).catch(() => null));
    }
    return files.get(id);
  }

  /** Over reaches ms (length-weighted): {sv: the survey under most of them (null: none), share: part of the length under
   *  any photo, main: part under sv, others: how many other surveys}; null without data for this build's reaches. */
  function of(A, S, ms) {
    if (!A || !S || A.s.length !== S.d.length || !ms?.length) return null;
    let all = 0, cov = 0;
    const by = new Map();
    ms.forEach((m) => {
      const L = S.len[m] || 0, c = L * A.pct[m] / 100, s = A.s[m];
      all += L; cov += c;
      if (s >= 0) by.set(s, (by.get(s) || 0) + c);
    });
    if (!all) return null;
    let best = -1, bl = 0;
    by.forEach((l, s) => { if (l > bl) { bl = l; best = s; } });
    return { sv: best >= 0 ? A.surveys[best] || null : null, share: cov / all, main: bl / all, others: Math.max(by.size - 1, 0) };
  }
  const fmtGsd = (g) => `${+(+g).toPrecision(3)} m`;
  /** {value: "0.3 m, 2021-2024", name: "Waikato Rural Aerial Photos", title: the LINZ survey title} of a summary with a survey. */
  const describe = (a) => ({ value: `${fmtGsd(a.sv.gsd)}, ${a.sv.years}`, name: a.sv.name, title: a.sv.title });
  /** The Aerial (LINZ) base map works here (standalone site built with a Basemaps key). */
  const available = () => { const b = M.BASEMAPS?.find((x) => x.id === BASE); return !!b && M.basemapAvailable(b); };

  const reachesOf = (f) => (f.kind === "run" || f.kind === "section" ? f.members || f.sec?.m || []
    : f.kind === "reach" ? [f.seg] : (f.kind === "waterfall" || f.kind === "rapids") && f.seg >= 0 ? [f.seg] : null);
  const WHERE = { run: "run", section: "section", reach: "reach", waterfall: "reach", rapids: "reach" };

  // ---------- state of the open map ----------
  let cur = null;      // {R, A}: the aerial file of the region on the map, once loaded
  let tour = null;     // {id, line, cum (m along the line), L (m), at (m)}: the selection "View aerial" follows
  let prevBase = null; // base map to go back to with "Hide aerial"
  const fid = (f) => (f ? `${f.region || ""}|${f.kind}|${f.id}` : "");
  const viewing = (f) => !!(tour && f && tour.id === fid(f) && M.prefs.basemap === BASE);

  M.onRegion((R) => {
    cur = null; tour = null;
    file(R.id).then((A) => { if (M.R !== R) return; cur = { R, A }; M.refreshCard?.(); });
  });
  RF.on("select", ({ feature }) => { if (tour && fid(feature) !== tour.id) tour = null; });

  function summary(f) {
    if (!f || !cur || cur.R !== M.R || !cur.A || (f.region && f.region !== cur.R.id)) return null;
    const ms = reachesOf(f);
    return ms && ms.length ? of(cur.A, cur.R.segments, ms) : null;
  }

  // ---------- following a line ----------
  function cumDist(c) {
    const out = [0];
    for (let i = 1; i < c.length; i++) {
      const k = Math.cos(c[i][1] * Math.PI / 180), dx = (c[i][0] - c[i - 1][0]) * 111320 * k, dy = (c[i][1] - c[i - 1][1]) * 110574;
      out.push(out[i - 1] + Math.hypot(dx, dy));
    }
    return out;
  }
  /** The longest line of a geometry (a section in pieces: its longest piece), downstream order. */
  function lineOf(g) {
    const ls = g.type === "LineString" ? [g.coordinates] : g.type === "MultiLineString" ? g.coordinates : [];
    let best = null;
    ls.filter((c) => c.length > 1).forEach((c) => { const cum = cumDist(c), L = cum[cum.length - 1]; if (!best || L > best.L) best = { line: c, cum, L }; });
    if (best) best.pieces = ls.length;
    return best;
  }
  function pointAt(t, d) {
    const { line, cum } = t;
    let lo = 0, hi = cum.length - 1;
    while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (cum[mid] <= d) lo = mid; else hi = mid; }
    const f = cum[hi] > cum[lo] ? (d - cum[lo]) / (cum[hi] - cum[lo]) : 0, a = line[lo], b = line[hi];
    return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f];
  }
  function along(t, ll) {
    const k = Math.cos(ll.lat * Math.PI / 180);
    let best = Infinity, bi = 0;
    t.line.forEach(([x, y], i) => { const d = ((x - ll.lng) * k) ** 2 + (y - ll.lat) ** 2; if (d < best) { best = d; bi = i; } });
    return t.cum[bi];
  }
  /** Metres per screen pixel at a zoom (MapLibre: 512 px tiles). */
  const mpp = (zoom, lat) => 40075016.686 * Math.cos(lat * Math.PI / 180) / (512 * 2 ** zoom);
  /** Map padding clear of the panel and of the card (on desktop too: the photos under it are what people look at). */
  function padding() {
    const pad = M.padding(), card = document.querySelector(".map-card:not([hidden])");
    if (card) pad.bottom = Math.max(pad.bottom, card.offsetHeight + 24);
    return pad;
  }
  /** One step along the line: 80 % of the smaller side of the free map area at that zoom. */
  function stepM(zoom, lat) {
    const c = M.map.getContainer(), pad = padding();
    const w = Math.max(c.clientWidth - pad.left - pad.right, 120), h = Math.max(c.clientHeight - pad.top - pad.bottom, 120);
    return 0.8 * Math.min(w, h) * mpp(zoom, lat);
  }
  function goTo(d, zoom) {
    tour.at = Math.min(Math.max(d, 0), tour.L);
    M.map.easeTo({ center: pointAt(tour, tour.at), zoom, padding: padding(), duration: 700 });
    M.refreshCard?.();
  }
  function step(dir) {
    if (!tour?.L || !M.map) return;
    const zoom = Math.max(M.map.getZoom(), M.AERIAL_MIN_ZOOM + 1), lat = tour.line[0][1], s = stepM(zoom, lat);
    const here = along(tour, M.map.getCenter()), from = Math.abs(here - tour.at) > s / 2 ? here : tour.at; // panned away: go on from there
    goTo(from + dir * s, zoom);
  }

  /** Turn the aerial photos on and zoom to f (a long river: its top, then step along it). */
  function view(f) {
    if (!available() || !M.map) return;
    if (M.prefs.basemap !== BASE) { prevBase = M.prefs.basemap; M.setBasemap(BASE); }
    const g = M.geometryOf(f);
    tour = { id: fid(f), L: 0, at: 0 };
    if (!g) { M.refreshCard?.(); return; }
    const pad = padding();
    if (g.type === "Point") {
      M.map.easeTo({ center: g.coordinates, zoom: Math.max(M.map.getZoom(), FIT_MAX), padding: pad, duration: 700 });
      M.refreshCard?.(); return;
    }
    const t = lineOf(g);
    if (!t) { M.refreshCard?.(); return; }
    const box = M.bboxOf(g.type === "LineString" ? g.coordinates : g.coordinates.flat());
    const cam = M.map.cameraForBounds(box, { padding: pad, maxZoom: FIT_MAX });
    if (cam && cam.zoom >= FIT_MIN) {
      M.map.fitBounds(box, { padding: pad, maxZoom: FIT_MAX, duration: 700 });
      M.refreshCard?.(); return;
    }
    Object.assign(tour, t);
    goTo(Math.min(t.L, stepM(TOUR_ZOOM, t.line[0][1]) / 2), TOUR_ZOOM);
  }
  /** Back to the base map in use before "View aerial". */
  function hide() {
    tour = null;
    const back = prevBase && prevBase !== BASE ? prevBase : "satellite";
    prevBase = null;
    M.setBasemap(back);
    M.refreshCard?.();
  }

  // ---------- card ----------
  function basemapsLink(f) {
    const g = M.geometryOf(f), t = g && lineOf(g), p = g?.type === "Point" ? g.coordinates : t ? pointAt(t, t.L / 2) : null;
    if (!p) return null;
    return el("a", { class: "map-aerial-link", href: `https://basemaps.linz.govt.nz/@${p[1].toFixed(5)},${p[0].toFixed(5)},z16`, target: "_blank", rel: "noopener",
      title: "LINZ aerial photos in LINZ Basemaps (opens in a new tab)" }, "See it in LINZ Basemaps");
  }
  function stepper() {
    const km = (m) => (m / 1000).toFixed(m < 9950 ? 1 : 0);
    return el("div", { class: "map-aerial-steps", role: "group", "aria-label": "Follow the river on the aerial photos" },
      el("button", { type: "button", class: "rf-btn", disabled: tour.at <= 1, title: "One screen upstream", onclick: () => step(-1) }, "‹ Upstream"),
      el("span", { class: "map-aerial-pos" }, el("span", { class: "num", text: `${km(tour.at)} of ${km(tour.L)} km` }),
        tour.pieces > 1 ? el("span", { class: "rf-muted", text: " (longest piece)" }) : null),
      el("button", { type: "button", class: "rf-btn", disabled: tour.at >= tour.L - 1, title: "One screen downstream", onclick: () => step(1) }, "Downstream ›"));
  }
  /** Card lines: which aerial survey covers the river, and the steps along it while it is being followed. */
  function lines(f) {
    const a = summary(f), out = [];
    if (a) {
      const where = WHERE[f.kind] || "river";
      if (!a.sv) {
        out.push(el("span", { class: "map-aerial-line" }, el("span", { class: "map-aerial-key", html: PHOTO }),
          el("span", { text: `No LINZ aerial photos over this ${where}: satellite only.` })));
      } else {
        const d = describe(a), part = a.share < 0.95 ? ` (${Math.round(100 * a.share)}% of the ${where})` : "";
        const more = a.others ? `; ${a.others} other survey${a.others > 1 ? "s" : ""} on parts` : "";
        out.push(el("span", { class: "map-aerial-line", title: d.title }, el("span", { class: "map-aerial-key", html: PHOTO }),
          el("span", {}, "Aerial photo: ", el("span", { class: "num", text: d.value }), `, ${d.name}${part}${more}. `,
            available() ? null : basemapsLink(f))));
      }
    }
    if (viewing(f) && tour.L) out.push(stepper());
    return out;
  }
  /** Whether the card offers "View aerial": the photos work here, the selection has a place, and the data does not say
   *  that no photo covers it. */
  const offers = (f) => available() && !!M.geometryOf(f) && summary(f)?.sv !== null;
  /** The toggle for the card: "View aerial" / "Hide aerial". */
  function button(f) {
    const on = viewing(f);
    return el("button", { type: "button", class: "rf-btn map-card-aerial", "aria-pressed": on ? "true" : "false",
      title: on ? "Back to the base map you had" : "LINZ aerial photos, zoomed in on this", html: PHOTO, onclick: () => (on ? hide() : view(f)) },
    el("span", { class: "map-aerial-long", text: on ? "Hide aerial" : "View aerial" }), el("span", { class: "map-aerial-short", text: on ? "Hide" : "Aerial" }));
  }

  M.aerial = { file, of, describe, available, offers, button, lines, view, hide, PHOTO };
})();
