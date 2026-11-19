/* Catchment of the selection: "Show catchment" on the card shades everything that drains into the bottom of
   the selected run, known section (every piece of one mapped in several), reach, LINZ river or point.
   Data: R.catch, the region's local-catchment labels on the heat-map grid (an RGB PNG, a data URI or a file
   next to the page; value = 1 + index of the reach a pixel first drains into, 0 = none; built by
   viewer._catchment_labels and viewer_nz.region_files). The labels are decoded once per region; a catchment
   is every pixel whose reach lies upstream of an outlet, traced along pixel edges into polygons (corners
   softened) and drawn under the river lines. Owner: map agent.
   Used by 40-select.js: M.catchment.{offers, button, lines, shown, clear}. */
(() => {
  "use strict";
  const RF = window.RF, M = RF.mapkit, { el, icon } = RF.util;
  const E = 20037508.342789244, EMPTY = { type: "FeatureCollection", features: [] };
  const TOKEN = "--map-catch";
  const cache = new Map(); // region id -> Promise<labels>, the last two regions
  let active = null;       // {id, R, ready, km2, cut, viaReach}
  let token = 0;           // bumped by every show/clear, so a slow decode never draws a stale catchment

  // ---------- labels: decoded once per region ----------
  /** {R, lab: Uint32Array (1 + reach per pixel), box: Uint16Array (col0, row0, col1, row1 per reach),
   *  start/kids: upstream reaches of each reach (CSR)} */
  function decode(R) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => {
        const W = R.width, H = R.height, S = R.segments, n = S.d.length;
        const cv = document.createElement("canvas"); cv.width = W; cv.height = H;
        const ctx = cv.getContext("2d", { willReadFrequently: true }); ctx.drawImage(img, 0, 0);
        const px = ctx.getImageData(0, 0, W, H).data, lab = new Uint32Array(W * H), box = new Uint16Array(4 * n);
        for (let k = 0; k < n; k++) { box[4 * k] = 65535; box[4 * k + 1] = 65535; }
        for (let r = 0, i = 0; r < H; r++) {
          for (let q = 0; q < W; q++, i++) {
            const v = (px[4 * i] << 16) | (px[4 * i + 1] << 8) | px[4 * i + 2];
            if (!v || v > n) continue;
            lab[i] = v;
            const b = 4 * (v - 1);
            if (q < box[b]) box[b] = q;
            if (r < box[b + 1]) box[b + 1] = r;
            if (q > box[b + 2]) box[b + 2] = q;
            if (r > box[b + 3]) box[b + 3] = r;
          }
        }
        const start = new Int32Array(n + 1), kids = new Int32Array(n);
        for (let i = 0; i < n; i++) if (S.d[i] >= 0) start[S.d[i] + 1]++;
        for (let i = 0; i < n; i++) start[i + 1] += start[i];
        const fill = start.slice(0, n);
        for (let i = 0; i < n; i++) if (S.d[i] >= 0) kids[fill[S.d[i]]++] = i;
        resolve({ R, lab, box, start, kids });
      };
      img.onerror = () => reject(new Error("Could not load the catchments for this region."));
      img.src = R.catch;
    });
  }
  function labels(R) {
    let p = cache.get(R.id);
    if (!p) { p = decode(R); p.catch(() => cache.delete(R.id)); }
    cache.delete(R.id); cache.set(R.id, p); // most recent last
    while (cache.size > 2) cache.delete(cache.keys().next().value);
    return p;
  }

  // ---------- which reaches drain out where ----------
  /** The outlet reaches of a feature: the bottom of each connected piece of a run or section, the reach
   *  itself, or for a LINZ line or a point the modelled reach its ground first drains into. */
  function outlets(f, L) {
    const R = L.R, S = R.segments, n = S.d.length;
    if (f.kind === "run" || f.kind === "section") {
      const ms = (f.members || []).filter((m) => m >= 0 && m < n);
      return ms.length ? RF.runs.pieces(S, ms).map((p) => p.bottom) : [];
    }
    if (f.kind === "reach") return f.seg >= 0 && f.seg < n ? [f.seg] : [];
    const at = onLine(f), i = M.pixelOf(R, { lng: at[0], lat: at[1] }), v = i >= 0 ? L.lab[i] : 0;
    if (v) return [v - 1];
    const seg = M.nearestReach(S, at[0], at[1], 1000);
    return seg >= 0 ? [seg] : [];
  }
  /** Where a LINZ line was tapped, snapped onto the line (the tap may land a few metres off it). */
  function onLine(f) {
    const g = f.kind === "linz-river" ? f.geometry : null;
    const pts = !g ? [] : g.type === "LineString" ? g.coordinates : g.type === "MultiLineString" ? g.coordinates.flat() : [];
    let best = [f.lon, f.lat], bd = Infinity;
    const k = Math.cos(f.lat * Math.PI / 180);
    pts.forEach((p) => { const d = ((p[0] - f.lon) * k) ** 2 + (p[1] - f.lat) ** 2; if (d < bd) { bd = d; best = p; } });
    return best;
  }

  /** Every reach upstream of the outlets: own[reach] = 1 + index into top (the outermost outlet holding it). */
  function upstreamOf(L, seeds) {
    const S = L.R.segments, own = new Uint16Array(S.d.length), top = [], inner = new Set();
    [...new Set(seeds)].sort((a, b) => S.a[b] - S.a[a]).forEach((s) => {
      if (own[s]) return; // inside a larger outlet's catchment
      top.push(s);
      const k = top.length, stack = [s];
      while (stack.length) {
        const x = stack.pop();
        if (own[x] === k) continue;
        if (own[x]) inner.add(own[x]); // an outlet of equal area upstream of this one
        own[x] = k;
        for (let j = L.start[x]; j < L.start[x + 1]; j++) stack.push(L.kids[j]);
      }
    });
    return { own, top, outer: top.filter((s, i) => !inner.has(i + 1)) };
  }

  // ---------- shading: mask, holes filled, traced along pixel edges ----------
  function shade(L, seeds) {
    const R = L.R, S = R.segments, W = R.width, { own, top, outer } = upstreamOf(L, seeds);
    let c0 = 65535, r0 = 65535, c1 = -1, r1 = -1;
    for (let k = 0; k < own.length; k++) {
      if (!own[k]) continue;
      const b = 4 * k; if (L.box[b] > L.box[b + 2]) continue; // a reach with no pixel on this grid
      c0 = Math.min(c0, L.box[b]); r0 = Math.min(r0, L.box[b + 1]); c1 = Math.max(c1, L.box[b + 2]); r1 = Math.max(r1, L.box[b + 3]);
    }
    const km2 = outer.reduce((t, s) => t + S.a[s], 0);
    if (c1 < 0) return { km2, cut: false, n: 0, geojson: EMPTY, bounds: null };
    // Cut off by the region edge: the outlet channel is flagged truncated. Only pixels whose area is close
    // to the outlet's count, so a truncated neighbour stream along the divide does not.
    const G = M.areaGrid(), grid = G && G.R === R ? G : null, code = (km2) => Math.round((Math.log10(km2) - R.code.log_min) / R.code.step) + 2;
    const lo = top.map((s) => code(S.a[s] / 2)), hi = top.map((s) => code(S.a[s] * 1.5));
    // padded mask: 1 = in the catchment, 0 = not (a ring of 0 all round)
    const w2 = c1 - c0 + 3, h2 = r1 - r0 + 3, m = new Uint8Array(w2 * h2);
    let n = 0, cut = false;
    for (let r = r0; r <= r1; r++) {
      for (let q = c0, i = r * W + c0, j = (r - r0 + 1) * w2 + 1; q <= c1; q++, i++, j++) {
        const v = L.lab[i]; if (!v) continue;
        const k = own[v - 1]; if (!k) continue;
        m[j] = 1; n++;
        if (grid && !cut && grid.trunc[i] && grid.area[i] >= lo[k - 1] && grid.area[i] <= hi[k - 1]) cut = true;
      }
    }
    outside(m, w2, h2);
    const pw = (R.merc[2] - R.merc[0]) / W, ph = (R.merc[3] - R.merc[1]) / R.height;
    const lon = (x) => (R.merc[0] + (c0 + x - 1) * pw) / E * 180;
    const lat = (y) => Math.atan(Math.sinh((R.merc[3] - (r0 + y - 1) * ph) / E * Math.PI)) * 180 / Math.PI;
    const polys = trace(m, w2, h2).map((ring) => {
      const s = soften(ring), out = [];
      for (let k = 0; k < s.length; k += 2) out.push([lon(s[k]), lat(s[k + 1])]);
      out.push(out[0]);
      return [out];
    });
    return { km2, cut, n, bounds: [[lon(1), lat(h2 - 1)], [lon(w2 - 1), lat(1)]],
      geojson: { type: "FeatureCollection", features: [{ type: "Feature", properties: {}, geometry: { type: "MultiPolygon", coordinates: polys } }] } };
  }

  /** Mark the 0-pixels 4-connected to the border as 2 (outside); 0-pixels left over are holes, shaded too. */
  function outside(m, w2, h2) {
    const stack = [0];
    while (stack.length) {
      const i = stack.pop(); if (m[i] !== 0) continue;
      const y = (i / w2) | 0, row = y * w2;
      let x0 = i - row, x1 = x0;
      while (x0 > 0 && m[row + x0 - 1] === 0) x0--;
      while (x1 < w2 - 1 && m[row + x1 + 1] === 0) x1++;
      m.fill(2, row + x0, row + x1 + 1);
      for (const ny of [y - 1, y + 1]) {
        if (ny < 0 || ny >= h2) continue;
        for (let x = x0, run = false, nrow = ny * w2; x <= x1; x++) {
          if (m[nrow + x] === 0) { if (!run) stack.push(nrow + x); run = true; } else run = false;
        }
      }
    }
  }

  // Pixel sides, clockwise on screen (inside on the right): 0 top (heads east), 1 right (south), 2 bottom
  // (west), 3 left (north). D: heading, O: the outside neighbour, V: start vertex.
  const DX = [1, 0, -1, 0], DY = [0, 1, 0, -1], OX = [0, 1, 0, -1], OY = [-1, 0, 1, 0], VX = [0, 1, 1, 0], VY = [0, 0, 1, 1];
  /** Closed rings along the edges between inside (m != 2) and outside pixels, corners only, as flat
   *  [x, y, ...] pixel-vertex coordinates. With the holes filled, every ring is an outer boundary. */
  function trace(m, w2, h2) {
    const seen = new Uint8Array(w2 * h2), rings = [];
    for (let y = 1; y < h2 - 1; y++) {
      for (let x = 1; x < w2 - 1; x++) {
        const p = y * w2 + x; if (m[p] === 2) continue;
        for (let s = 0; s < 4; s++) {
          if (seen[p] & (1 << s) || m[p + OY[s] * w2 + OX[s]] !== 2) continue;
          const ring = [];
          let cx = x, cy = y, cs = s, prev = -1;
          while (!(seen[cy * w2 + cx] & (1 << cs))) {
            seen[cy * w2 + cx] |= 1 << cs;
            if (cs !== prev) ring.push(cx + VX[cs], cy + VY[cs]);
            prev = cs;
            const fx = cx + DX[cs], fy = cy + DY[cs];
            if (m[fy * w2 + fx] === 2) cs = (cs + 1) & 3; // turn right round this pixel
            else if (m[(fy + OY[cs]) * w2 + fx + OX[cs]] === 2) { cx = fx; cy = fy; } // straight on
            else { cx = fx + OX[cs]; cy = fy + OY[cs]; cs = (cs + 3) & 3; } // turn left
          }
          rings.push(ring);
        }
      }
    }
    return rings;
  }
  /** Two passes of corner cutting (Chaikin), at most half a pixel from each corner: the staircase of the
   *  grid reads as a natural outline, and long straight edges keep their corners. */
  function soften(a) {
    for (let pass = 0; pass < 2; pass++) {
      const n = a.length / 2, out = [];
      for (let i = 0; i < n; i++) {
        const j = (i + 1) % n, x0 = a[2 * i], y0 = a[2 * i + 1], dx = a[2 * j] - x0, dy = a[2 * j + 1] - y0;
        const t = Math.min(0.25, 0.5 / (Math.hypot(dx, dy) || 1));
        out.push(x0 + dx * t, y0 + dy * t, x0 + dx * (1 - t), y0 + dy * (1 - t));
      }
      a = out;
    }
    return a;
  }

  // ---------- on the map ----------
  const casing = () => (M.isDark() ? "#0b0f0d" : "#ffffff");
  function paint() {
    const map = M.map; if (!map?.getLayer("catchsel-fill")) return;
    const sat = document.querySelector(".map-root")?.dataset.basemap === "satellite"; // dark imagery takes a stronger tint
    map.setPaintProperty("catchsel-fill", "fill-color", M.token(`${TOKEN}-fill`));
    map.setPaintProperty("catchsel-fill", "fill-opacity", (M.isDark() ? 0.3 : 0.26) + (sat ? 0.1 : 0));
    map.setPaintProperty("catchsel-casing", "line-color", casing());
    map.setPaintProperty("catchsel-line", "line-color", M.token(TOKEN));
  }
  M.layerInits = M.layerInits || [];
  M.layerInits.push(() => {
    M.map.addSource("catchsel", { type: "geojson", data: EMPTY, tolerance: 0.3 });
    M.addLayer({ id: "catchsel-fill", type: "fill", source: "catchsel", paint: { "fill-color": "#888", "fill-opacity": 0.24 } }, "catchment");
    M.addLayer({ id: "catchsel-casing", type: "line", source: "catchsel", layout: { "line-join": "round" },
      paint: { "line-color": "#fff", "line-opacity": 0.8, "line-width": ["interpolate", ["linear"], ["zoom"], 8, 3.5, 14, 5] } }, "catchment");
    M.addLayer({ id: "catchsel-line", type: "line", source: "catchsel", layout: { "line-join": "round" },
      paint: { "line-color": "#888", "line-width": ["interpolate", ["linear"], ["zoom"], 8, 1.5, 14, 2.5] } }, "catchment");
    paint();
    M.onTheme(paint);
    M.onBasemap(paint);
  });

  /** Pan or zoom out (never in) so the whole catchment shows clear of the panel and the card. */
  function reveal(b) {
    const map = M.map, pad = M.padding(), card = document.querySelector(".map-card:not([hidden])"), box = map.getContainer().getBoundingClientRect();
    if (card) pad.bottom = Math.max(pad.bottom, Math.round(box.bottom - card.getBoundingClientRect().top) + 16);
    const { clientWidth: w, clientHeight: h } = map.getContainer();
    const tl = map.unproject([pad.left, pad.top]), br = map.unproject([w - pad.right, h - pad.bottom]);
    if (b[0][0] >= tl.lng && b[1][0] <= br.lng && b[0][1] >= br.lat && b[1][1] <= tl.lat) return;
    map.fitBounds(b, { padding: pad, maxZoom: map.getZoom(), duration: 600 });
  }

  // ---------- state ----------
  const refresh = () => M.refreshCard(); // keeps keyboard focus on the toggle
  async function show(f) {
    const R = M.R, my = ++token;
    active = { id: f.id, R, ready: false };
    refresh();
    let L = null;
    try { L = await labels(R); } catch (e) {
      if (my === token) { active = null; refresh(); RF.util.toast(e.message); }
      return;
    }
    if (my !== token || M.R !== R) return;
    const seeds = outlets(f, L);
    if (!seeds.length) { active = null; refresh(); RF.util.toast("No modelled catchment here."); return; }
    const res = shade(L, seeds);
    M.map.getSource("catchsel")?.setData(res.geojson);
    active = { id: f.id, R, ready: true, km2: res.km2, cut: res.cut, viaReach: f.kind === "linz-river" || f.kind === "point" };
    refresh();
    if (res.bounds) reveal(res.bounds);
  }
  function clear() {
    token++;
    if (!active) return;
    active = null;
    M.map?.getSource("catchsel")?.setData(EMPTY);
  }
  const shown = (f) => !!(active && f && active.id === f.id && active.R === M.R);
  let minArea = null; // smallest modelled reach in the region (km²)
  M.onRegion((R) => {
    clear();
    minArea = null;
    const a = R.segments?.a; if (a) { minArea = Infinity; for (let i = 0; i < a.length; i++) if (a[i] < minArea) minArea = a[i]; }
  });

  M.catchment = {
    /** Whether the card offers "Show catchment": a river with modelled reaches, in a region with labels,
     *  or a point on a modelled channel. */
    offers(f) {
      const R = M.R;
      if (!f || !R?.catch || !R.segments || (f.region && f.region !== R.id)) return false;
      if (f.kind === "run" || f.kind === "section") return (f.members || []).length > 0;
      if (f.kind === "reach") return f.seg >= 0;
      if (f.kind === "linz-river") return isFinite(f.lat) && isFinite(f.lon);
      if (f.kind === "point") return minArea != null && f.km2 >= minArea * 0.9;
      return false;
    },
    shown,
    /** The toggle for the card: "Show catchment" / "Hide catchment". */
    button(f) {
      const on = shown(f);
      return el("button", { type: "button", class: "rf-btn map-card-catch", "aria-pressed": on ? "true" : "false",
        "aria-busy": on && !active.ready ? "true" : null, title: on ? "Hide the shaded catchment" : "Shade everything that drains into this river",
        html: icon("catchment"), onclick: () => { if (shown(f)) { clear(); refresh(); } else show(f); } }, on ? "Hide catchment" : "Show catchment");
    },
    /** Card lines while shown: the area shaded, and whether the region edge cuts it off. */
    lines(f) {
      if (!shown(f)) return [];
      if (!active.ready) return [el("span", { class: "map-catch-line" }, el("span", { class: "map-catch-key", "aria-hidden": "true" }), "Shading the catchment…")];
      const cut = active.cut && !(f.kind === "point" && f.trunc); // a point's card already says so
      return [
        el("span", { class: "rf-row map-catch-line" }, el("span", { class: "map-catch-key", "aria-hidden": "true" }),
          el("span", { class: "num", text: `Catchment shaded: ${M.fmtArea(active.km2)}` }),
          cut ? el("span", { class: "rf-pill rf-pill--warn", text: "Cut off by the region edge" }) : null),
        active.viaReach ? el("span", { class: "rf-note", text: "Down to the end of the modelled reach it drains into." }) : null,
      ].filter(Boolean);
    },
    clear,
  };
})();
