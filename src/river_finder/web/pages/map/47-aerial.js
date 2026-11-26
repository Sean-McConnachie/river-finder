/* LINZ aerial survey per river (bead rf-aq6.29).
   Coverage: <id>_aerial.json next to the page (river_finder.webdata.write_aerial; format in web/README.md): per
   viewer reach the survey LINZ Basemaps draws on top at zoom 16, with its pixel size (GSD) and capture dates. River
   cards and the Details say which survey covers a river ("Aerial photo: 0.3 m, 2021-2024, Waikato Rural Aerial
   Photos") on every host, because that comes from data. The photos themselves are the Aerial base map
   (20-basemaps.js; standalone site built with a Basemaps key).
   Owner: map agent. Used by 40-select.js (card), 60-detail.js (Details) and 90-page.js:
   M.aerial.{file, of, describe, available, lines}. */
(() => {
  "use strict";
  const RF = window.RF, M = RF.mapkit, { el } = RF.util;
  const BASE = "linz-aerial";
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
  /** The Aerial base map works here (standalone site built with a Basemaps key). */
  const available = () => { const b = M.BASEMAPS?.find((x) => x.id === BASE); return !!b && M.basemapAvailable(b); };

  const reachesOf = (f) => (f.kind === "run" || f.kind === "section" ? f.members || f.sec?.m || []
    : f.kind === "reach" ? [f.seg] : (f.kind === "waterfall" || f.kind === "rapids") && f.seg >= 0 ? [f.seg] : null);
  const WHERE = { run: "run", section: "section", reach: "reach", waterfall: "reach", rapids: "reach" };

  // ---------- the aerial file of the region on the map ----------
  let cur = null; // {R, A}, once loaded
  M.onRegion((R) => {
    cur = null;
    file(R.id).then((A) => { if (M.R !== R) return; cur = { R, A }; M.refreshCard?.(); });
  });

  function summary(f) {
    if (!f || !cur || cur.R !== M.R || !cur.A || (f.region && f.region !== cur.R.id)) return null;
    const ms = reachesOf(f);
    return ms && ms.length ? of(cur.A, cur.R.segments, ms) : null;
  }

  // ---------- the middle of a line (for the LINZ Basemaps link) ----------
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
  // ---------- card ----------
  function basemapsLink(f) {
    const g = M.geometryOf(f), t = g && lineOf(g), p = g?.type === "Point" ? g.coordinates : t ? pointAt(t, t.L / 2) : null;
    if (!p) return null;
    return el("a", { class: "map-aerial-link", href: `https://basemaps.linz.govt.nz/@${p[1].toFixed(5)},${p[0].toFixed(5)},z16`, target: "_blank", rel: "noopener",
      title: "LINZ aerial photos in LINZ Basemaps (opens in a new tab)" }, "See it in LINZ Basemaps");
  }
  /** Card lines: which aerial survey covers the river. */
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
    return out;
  }
  M.aerial = { file, of, describe, available, lines, PHOTO };
})();
