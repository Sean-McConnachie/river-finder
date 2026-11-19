/* Flow direction: small arrows pointing downstream along the river lines from zoom 13, on known sections,
   predicted runs, LINZ rivers and modelled channels. Each arrow layer follows its line's visibility, filter
   and colour, and sits just above it, so a line drawn over another also covers its arrows. Arrows give way
   to labels and are not tap targets (taps reach the line under them).
   Direction: modelled reaches run upstream to downstream (S.c), and so do the lines of known sections and
   predicted runs built from them (RF.runs.chains / path). LINZ lines are digitised either way, so each is
   oriented once per region (lineDirections) and gets no arrows when its direction is unclear.
   Used by 50-rivers.js: M.flow.{sync, orientLinz}. Owner: map agent. */
(() => {
  "use strict";
  const RF = window.RF, M = RF.mapkit, { el } = RF.util;
  const MINZOOM = 13, IMAGE = "rf-flow";
  // [arrow layer, the line it follows, its slot, the layer it is drawn under (null: top of the slot)]
  const LAYERS = [["linz-flow", "linz-line", "network", "net-line"], ["net-flow", "net-line", "network", null],
    ["pred-flow", "pred-line", "rivers", "known-casing"], ["known-flow", "known-line", "rivers", null]];
  // the arrowhead (a notched dart pointing +x, along the line), logical px from the icon centre
  const DART = [[4.5, 0], [-3, 4], [-1.2, 0], [-3, -4]];
  const KEY = '<svg viewBox="0 0 22 12" aria-hidden="true"><path d="M1 6h20" stroke="var(--river-linz)" stroke-width="2" stroke-linecap="round"/>' +
    '<path d="M15 6 7.5 2 9.3 6 7.5 10Z" fill="currentColor"/></svg>';

  M.defineLayer({ id: "flow", parent: "rivers", label: "Flow direction", note: "Arrows downstream, from zoom 13", defaultOn: true, swatch: "none",
    legend: "Flow direction, downstream (from zoom 13)", legendKey: () => el("span", { class: "map-key map-key--flow", html: KEY }), apply: sync });

  /** The arrowhead as a signed distance field (MapLibre SDF: edge at 0.75, 1/8 per px), so its colour and halo can be set. */
  function arrowhead() {
    const PR = 2, N = 16, S = N * PR, data = new Uint8Array(S * S * 4), P = DART;
    const seg = (x, y, [ax, ay], [bx, by]) => {
      const vx = bx - ax, vy = by - ay, t = Math.max(0, Math.min(1, ((x - ax) * vx + (y - ay) * vy) / (vx * vx + vy * vy)));
      return Math.hypot(x - ax - t * vx, y - ay - t * vy);
    };
    for (let r = 0; r < S; r++) for (let q = 0; q < S; q++) {
      const x = (q + 0.5) / PR - N / 2, y = (r + 0.5) / PR - N / 2;
      let d = Infinity, inside = false;
      P.forEach((a, k) => {
        const b = P[(k + 1) % P.length];
        d = Math.min(d, seg(x, y, a, b));
        if ((a[1] > y) !== (b[1] > y) && x < a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1])) inside = !inside;
      });
      data[4 * (r * S + q) + 3] = Math.max(0, Math.min(255, Math.round(255 * (0.75 - (inside ? -d : d) / 8))));
    }
    return [{ width: S, height: S, data }, { sdf: true, pixelRatio: PR }];
  }

  // "zoom" must be the input of a top-level interpolate, so the data terms go into each stop.
  const zoomed = (lo, hi) => ["interpolate", ["linear"], ["zoom"], MINZOOM, lo, 16, hi];
  const SIZE = {
    "known-flow": zoomed(0.9, 1.3),
    "pred-flow": zoomed(["+", 0.75, ["*", 0.2, ["get", "t"]]], ["+", 1, ["*", 0.3, ["get", "t"]]]),
    "linz-flow": zoomed(["case", ["get", "named"], 0.9, 0.8], ["case", ["get", "named"], 1.2, 1.05]),
    "net-flow": zoomed(["+", 0.8, ["*", 0.15, ["get", "w"]]], ["+", 1.05, ["*", 0.2, ["get", "w"]]]),
  };

  M.layerInits.push(() => {
    const map = M.map;
    if (!map.hasImage(IMAGE)) map.addImage(IMAGE, ...arrowhead());
    LAYERS.forEach(([id, line, slot, before]) => {
      const src = map.getLayer(line)?.source; if (!src) return;
      M.addLayer({ id, type: "symbol", source: src, minzoom: MINZOOM, filter: id === "linz-flow" ? ["==", ["get", "dir"], 1] : ["all"],
        layout: { visibility: "none", "symbol-placement": "line", "symbol-spacing": zoomed(130, 190), "icon-image": IMAGE, "icon-size": SIZE[id],
          "icon-rotation-alignment": "map", "icon-keep-upright": false, "icon-padding": 1 },
        paint: { "icon-color": "#ffffff", "icon-halo-width": 1, "icon-opacity": zoomed(0.85, 1) } }, slot, before);
    });
  });

  /** Arrows on wherever their line is shown, in its filter and colour (lines: 50-rivers.js). */
  function sync() {
    const map = M.map;
    if (!M.ready || !map.getLayer("known-flow")) return;
    const on = M.isVisible("flow"), casing = M.casing();
    LAYERS.forEach(([id, line]) => {
      if (!map.getLayer(id)) return;
      const show = on && map.getLayoutProperty(line, "visibility") !== "none";
      M.vis(id, show);
      if (!show) return;
      if (id === "known-flow" || id === "pred-flow") {
        // inside the line, in the casing colour
        map.setFilter(id, map.getFilter(line) || ["all"]);
        map.setPaintProperty(id, "icon-color", casing);
      } else {
        // over a thin line: its colour, with a thin casing-coloured halo so it reads on any base map
        map.setPaintProperty(id, "icon-color", map.getPaintProperty(line, "line-color"));
        map.setPaintProperty(id, "icon-halo-color", casing);
      }
    });
  }

  // ---------- LINZ lines: oriented downstream once per region ----------
  let oriented = null; // the LINZ data (M.linz) already oriented
  M.onAreaGrid(() => orientLinz());
  /** Turn each LINZ line to run downstream (in place) and mark it dir 1, or dir 0 when unclear. Runs once both
   *  the lines and the area grid of the region are loaded (called by 50-rivers.js and on the area grid). */
  function orientLinz() {
    const R = M.R, L = M.linz, F = M.linzFeats, G = M.areaGrid(), src = M.map?.getSource("src-linz");
    if (!R || !L || !F || !G || G.R !== R || !src || oriented === L) return;
    oriented = L;
    const dirs = lineDirections(R, G.area, F.map((f) => f.geometry.coordinates), F.map((f) => L.types?.[f.properties.t] === "canal_cl"));
    let reversed = 0, unknown = 0;
    F.forEach((f, i) => {
      if (dirs[i] < 0) { f.geometry.coordinates.reverse(); reversed++; }
      if (!dirs[i]) unknown++;
      f.properties.dir = dirs[i] ? 1 : 0;
    });
    M.flow.stats = { region: R.id, lines: F.length, oriented: F.length - unknown, reversed, unknown };
    src.setData({ type: "FeatureCollection", features: F });
  }

  /**
   * Direction of each line: 1 runs downstream as digitised, -1 runs upstream (reverse it), 0 unclear.
   * Signals, from samples every 3/4 cell along the line on the heat-map grid:
   * - reaches: the modelled reaches (upstream to downstream) through the sampled cells point along the line,
   *   or against it, at 80 % of at least 4 samples that cover at least a quarter of the line (so a stream
   *   running beside a larger river near their confluence is not taken for it);
   * - elevation: the reaches at the line's two ends (elevation interpolated along each reach) differ by 3 m;
   * - area: the catchment-area code (the largest in the 3x3 cells around each sample, so a line a cell off
   *   the modelled channel still reads it; sea and nodata left out) is larger in the line's last third than in
   *   its first (by at least 2 codes, ~18 %), or smaller, and the rank trend over the whole line (Kendall)
   *   agrees, so neither a stretch off the channel nor an end touching a larger channel decides it; and the
   *   code at the line's downstream end must not be below that at its upstream end.
   * The first of reaches, elevation and area that gives a direction decides it, and no other may disagree,
   * except that the area (a coarser grid, cut off at the region edge) does not count against reaches that
   * cover half the line or more. A line that strays from the modelled drainage (reaches both ways, or
   * signals that disagree) gets no arrows, and so do canals (skip).
   */
  function lineDirections(R, A, lines, skip) {
    const W = R.width, H = R.height, x0 = R.merc[0], y1 = R.merc[3], pw = (R.merc[2] - x0) / W, ph = (y1 - R.merc[1]) / H;
    const E = 6378137, rad = Math.PI / 180, NS = 32, step = 0.75 * pw;
    const merc = (p) => [E * p[0] * rad, E * Math.log(Math.tan(Math.PI / 4 + p[1] * rad / 2))];
    const cell = (x, y) => {
      const q = Math.floor((x - x0) / pw), r = Math.floor((y1 - y) / ph);
      return q < 0 || r < 0 || q >= W || r >= H ? -1 : r * W + q;
    };
    const near = (i, fn) => {
      const q = i % W, r = (i - q) / W;
      for (let dr = -1; dr <= 1; dr++) for (let dq = -1; dq <= 1; dq++) {
        const rr = r + dr, qq = q + dq;
        if (rr >= 0 && qq >= 0 && rr < H && qq < W) fn(rr * W + qq);
      }
    };
    // the reaches drawn into the grid: direction in NS sectors (0 = none) and elevation per cell
    const sector = new Uint8Array(W * H), elev = new Map(), S = R.segments;
    (S?.c || []).forEach((c, i) => {
      const P = c.map(merc), L = P.slice(1).map((b, k) => Math.hypot(b[0] - P[k][0], b[1] - P[k][1])), total = L.reduce((t, x) => t + x, 0) || 1;
      for (let k = 0, done = 0; k < L.length; done += L[k], k++) {
        const [ax, ay] = P[k], [bx, by] = P[k + 1], n = Math.max(1, Math.ceil(L[k] / (0.5 * pw)));
        const s = 1 + ((Math.round(Math.atan2(by - ay, bx - ax) / (2 * Math.PI) * NS) % NS) + NS) % NS;
        for (let t = 0; t <= n; t++) {
          const j = cell(ax + (bx - ax) * t / n, ay + (by - ay) * t / n); if (j < 0) continue;
          sector[j] = s;
          elev.set(j, S.zt[i] + (S.zb[i] - S.zt[i]) * (done + L[k] * t / n) / total);
        }
      }
    });
    const elevAt = ([x, y]) => {
      const i = cell(x, y); if (i < 0) return null;
      let sum = 0, n = 0; near(i, (j) => { const z = elev.get(j); if (z !== undefined) { sum += z; n++; } });
      return n ? sum / n : null;
    };
    return lines.map((line, li) => {
      if (skip[li] || line.length < 2) return 0;
      const m = line.map(merc), codes = [];
      let along = 0, against = 0, seen = 0;
      const sample = (x, y, ang) => {
        const i = cell(x, y); if (i < 0) return;
        seen++;
        let a = 0; near(i, (j) => { if (A[j] > a) a = A[j]; });
        if (a >= 2) codes.push(a);
        let s = sector[i]; if (!s) near(i, (j) => { if (!s && sector[j]) s = sector[j]; });
        if (!s || ang === null) return;
        const cs = Math.cos(ang - (s - 1) * 2 * Math.PI / NS);
        if (cs > 0.5) along++; else if (cs < -0.5) against++;
      };
      for (let j = 0; j < m.length - 1; j++) {
        const [ax, ay] = m[j], [bx, by] = m[j + 1], n = Math.max(1, Math.ceil(Math.hypot(bx - ax, by - ay) / step)), ang = Math.atan2(by - ay, bx - ax);
        for (let k = 0; k < n; k++) sample(ax + (bx - ax) * k / n, ay + (by - ay) * k / n, ang);
      }
      sample(...m[m.length - 1], null);
      const votes = along + against, cover = votes / Math.max(seen, 1);
      const byReach = votes < 4 || cover < 0.25 ? 0 : along >= 0.8 * votes ? 1 : against >= 0.8 * votes ? -1 : null;
      if (byReach === null) return 0; // follows reaches both ways
      const z0 = elevAt(m[0]), z1 = elevAt(m[m.length - 1]), dz = z0 === null || z1 === null ? 0 : z0 - z1;
      const byElev = dz >= 3 ? 1 : dz <= -3 ? -1 : 0;
      let byArea = 0, ends = 0;
      const n = codes.length;
      if (n >= 2) {
        ends = codes[n - 1] - codes[0];
        const k = Math.max(1, Math.round(n / 3));
        let first = 0, last = 0;
        for (let j = 0; j < k; j++) { first = Math.max(first, codes[j]); last = Math.max(last, codes[n - 1 - j]); }
        // Kendall's S on at most 48 evenly spaced samples
        const T = Math.min(n, 48), v = Array.from({ length: T }, (_, j) => codes[Math.round(j * (n - 1) / (T - 1))]);
        let trend = 0;
        for (let i = 0; i < T; i++) for (let j = i + 1; j < T; j++) trend += Math.sign(v[j] - v[i]);
        byArea = last - first >= 2 && trend > 0 ? 1 : first - last >= 2 && trend < 0 ? -1 : 0;
      }
      const dir = byReach || byElev || byArea, areaCounts = !(byReach && cover >= 0.5);
      if (byElev === -dir || (areaCounts && (byArea === -dir || ends * dir < 0))) return 0;
      return dir;
    });
  }

  M.flow = { sync, orientLinz, lineDirections, stats: null };
})();
