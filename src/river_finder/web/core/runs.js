/* RF.runs: the run finder, shared by Map (predicted runs), Top rivers (ranking) and Models (tuning).
   Mirrors river_finder.scoring (desirability, assemble_runs, pareto_layers) and the legacy viewer.
   Owner: core. Pure functions: nothing here touches the DOM. */
(() => {
  "use strict";
  const RF = window.RF;
  const LEG = () => RF.models.legacy || {};
  const HA_DEFAULTS = () => LEG().ha_defaults || { melton: [0.03, 0.1], height_to_width: [0.1, 1.5] };
  const trap = (x, a, b, c, d) => Math.max(0, Math.min((x - a) / Math.max(b - a, 1e-9), (d - x) / Math.max(d - c, 1e-9), 1));

  /** Default parameters (from model.json run_params where present). */
  function defaults() {
    const RP = LEG().run_params || {}, HAD = HA_DEFAULTS();
    const fb = RP.flow_band || [2, 8, 300, 2000], gb = RP.gradient_band || [5, 20, 80, 150];
    const haHi = {}; Object.keys(HAD).forEach((k) => { haHi[k] = HAD[k][1]; });
    return {
      q: [fb[1], fb[2]],              // full credit for mean annual flood between these (m³/s)
      g: [gb[1], gb[2]],              // full credit for gradient between these (m/km)
      haMeasure: RP.ha_measure || "melton", haHi,
      w: (RP.weights || [1, 1, 1]).slice(),   // weights: flow, gradient, height-to-area
      threshold: RP.threshold ?? 0.7, gate: RP.gate ?? true, flowScaled: RP.flow_scaled ?? true,
      minLen: RP.min_length_km ?? 1, maxGap: RP.max_gap_segments ?? 1,
      featureWeights: Object.assign({}, LEG().default_weights || {}),
    };
  }

  function percentiles(values, refMask) {
    const ref = []; for (let i = 0; i < values.length; i++) if (refMask[i]) ref.push(values[i]);
    ref.sort((a, b) => a - b);
    return values.map((v) => {
      let lo = 0, hi = ref.length; while (lo < hi) { const m = (lo + hi) >> 1; if (ref[m] <= v) lo = m + 1; else hi = m; }
      return 100 * lo / Math.max(ref.length, 1);
    });
  }

  /** Per-reach desirability: {effG, dQ, dS, dH, ws, ha} (typed arrays, reach order). */
  function segmentScores(S, p) {
    const n = S.g.length, effG = new Float64Array(n), dQ = new Float64Array(n), dS = new Float64Array(n), dH = new Float64Array(n), ws = new Float64Array(n);
    const HAD = HA_DEFAULTS(), [qb, qc] = p.q, [gb, gc] = p.g, m = p.haMeasure, ha = m === "melton" ? S.mel : S.hw;
    const lo = HAD[m][0], hi = Math.max(p.haHi[m], lo * 1.01), w = p.w, wsum = Math.max(w[0] + w[1] + w[2], 1e-9);
    for (let i = 0; i < n; i++) {
      const maf = Math.max(S.maf[i], 1e-3);
      effG[i] = p.flowScaled ? S.g[i] * Math.min(3, Math.max(1, Math.sqrt(maf / 20))) : S.g[i];
      dQ[i] = trap(Math.log10(maf), Math.log10(qb / 4), Math.log10(qb), Math.log10(qc), Math.log10(qc * 6.67));
      dS[i] = trap(effG[i], gb / 4, gb, gc, gc * 1.875);
      dH[i] = Math.min(1, Math.max(0, (ha[i] - lo) / (hi - lo)));
      ws[i] = (w[0] * dQ[i] + w[1] * dS[i] + w[2] * dH[i]) / wsum;
    }
    return { effG, dQ, dS, dH, ws, ha };
  }

  /** Per-reach ranking score (0-100) for rankBy: "model" | "wsum" | "pareto" | "sig" | "comp". */
  function rankScores(S, D, rankBy, p, model) {
    if (rankBy === "model") return model?.pct?.length ? model.pct : S.reg;
    if (rankBy === "sig") return S.sig;
    if (rankBy === "comp") {
      const w = S.features.map((f) => p.featureWeights[f] ?? 0);
      return percentiles(S.z.map((row) => row.reduce((acc, v, k) => acc + v * w[k], 0)), S.a.map((a) => a >= 2));
    }
    return Array.from(D.ws, (v) => 100 * v);
  }

  function paretoLayers(pts) {
    const n = pts.length, layer = new Int32Array(n); let rem = [...Array(n).keys()], k = 0;
    while (rem.length) {
      k++;
      const keep = [], next = [];
      for (const i of rem) {
        let dom = false;
        for (const j of rem) {
          if (j === i) continue;
          const a = pts[j], b = pts[i];
          if (a[0] >= b[0] && a[1] >= b[1] && a[2] >= b[2] && (a[0] > b[0] || a[1] > b[1] || a[2] > b[2])) { dom = true; break; }
        }
        (dom ? next : keep).push(i);
      }
      keep.forEach((i) => { layer[i] = k; });
      rem = next;
    }
    return layer;
  }

  // 0 = no label, 1 = the model trained on it, 2 = held out (its test set).
  const segRole = (S, role, m) => role ? role[m] & 3 : S.lab[m] === 1 ? 1 : S.ho[m] === 1 ? 2 : 0;

  /**
   * Find runs in a loaded region.
   * opts: {params (default RF.state.runParams), rankBy ("model" default when a model is given, else "pareto"),
   *        model: {pct, role} from RF.data.scores()}
   * Returns runs sorted best first. Each run is a feature reference ({kind: "run", region, id, members, ...}).
   */
  function find(R, opts = {}) {
    const S = R.segments; if (!S) return [];
    const p = opts.params || RF.state.runParams || defaults();
    const model = opts.model || null, role = model?.role || null;
    const rankBy = opts.rankBy || (model ? "model" : "pareto");
    const D = segmentScores(S, p), scores = rankScores(S, D, rankBy, p, model);
    const n = S.g.length, ok = new Uint8Array(n), runId = new Int32Array(n).fill(-1), runs = [];
    for (let i = 0; i < n; i++) ok[i] = D.ws[i] >= p.threshold && (!p.gate || (D.dQ[i] > 0 && D.dS[i] > 0 && D.dH[i] > 0)) ? 1 : 0;
    const listed = new Set(S.listed);
    for (let s = 0; s < n; s++) {
      if (!ok[s] || runId[s] >= 0 || (S.u[s] >= 0 && ok[S.u[s]])) continue;
      const members = []; let gap = [], cur = s;
      while (cur >= 0 && runId[cur] < 0) {
        if (ok[cur]) { members.push(...gap, cur); gap = []; }
        else { gap.push(cur); if (gap.length > p.maxGap) break; }
        cur = S.d[cur];
      }
      const len = members.reduce((t, m) => t + S.len[m], 0) / 1000;
      if (len < p.minLen) continue;
      const rid = runs.length; members.forEach((m) => { runId[m] = rid; });
      const counts = {}; let score = 0, s50 = 0, eg = 0, wsc = 0, rl = 0, guide = false;
      const has = [];
      members.forEach((m) => {
        if (S.r[m] >= 0) counts[S.r[m]] = (counts[S.r[m]] || 0) + 1;
        score = Math.max(score, scores[m]); s50 = Math.max(s50, S.s50[m]);
        const rv = segRole(S, role, m); if (rv === 1) rl = 1; else if (rv === 2 && rl !== 1) rl = 2;
        guide = guide || (role ? (role[m] & 4) > 0 : false);
        eg += D.effG[m] * S.len[m]; wsc += D.ws[m] * S.len[m]; has.push(D.ha[m]);
      });
      has.sort((a, b) => a - b);
      const top = Object.entries(counts).sort((a, b) => b[1] - a[1])[0];
      const river = top ? S.rivers[+top[0]] : "";
      const first = members[0], last = members[members.length - 1], drop = S.zt[first] - S.zb[last];
      let acc = null, falls = 0, fallsH = 0;
      if (S.dRoad) {
        const near = (A) => members.reduce((t, m) => Math.min(t, A[m]), Infinity);
        acc = { putIn: S.dRoad[first], takeOut: S.dRoad[last], road: near(S.dRoad), track: near(S.dTrack), foot: near(S.dFoot) };
        members.forEach((m) => { falls += S.fallsN[m]; fallsH = Math.max(fallsH, S.fallsH[m]); });
      }
      const isListed = listed.has(river) || guide;
      runs.push({
        kind: "run", region: R.id, id: `${R.id}:run:${first}`, rid, members, name: river || "Unnamed creek", river,
        len, drop, grad: drop / (len * 1000) * 1000, effG: eg / (len * 1000), s50,
        aTop: S.a[first], aBot: S.a[last], maf: S.maf[last], ha: has[has.length >> 1], ws: wsc / (len * 1000),
        score, role: rl, guide: isListed, acc, falls, fallsH,
        status: isListed ? "listed" : rl === 2 ? "heldout" : "candidate",
      });
    }
    const layers = paretoLayers(runs.map((r) => [Math.log10(Math.max(r.maf, 1e-3)), r.effG, r.ha]));
    runs.forEach((r, i) => { r.layer = layers[i]; if (rankBy === "pareto" || rankBy === "wsum") r.score = 100 * r.ws; });
    return rankBy === "pareto" ? runs.sort((a, b) => a.layer - b.layer || b.ws - a.ws) : runs.sort((a, b) => b.score - a.score);
  }

  /** One smoothed line for a run: [[lon, lat], ...] (reach pieces joined, two Chaikin passes). */
  function path(S, members) {
    const pts = [];
    members.forEach((m) => S.c[m].forEach((pt, k) => {
      const q = pts[pts.length - 1];
      if (k === 0 && q && q[0] === pt[0] && q[1] === pt[1]) return;
      pts.push(pt);
    }));
    if (pts.length < 3) return pts;
    let sm = pts;
    for (let pass = 0; pass < 2; pass++) {
      const nx = [sm[0]];
      for (let i = 0; i < sm.length - 1; i++) {
        const a = sm[i], b = sm[i + 1];
        nx.push([0.75 * a[0] + 0.25 * b[0], 0.75 * a[1] + 0.25 * b[1]], [0.25 * a[0] + 0.75 * b[0], 0.25 * a[1] + 0.75 * b[1]]);
      }
      nx.push(sm[sm.length - 1]);
      sm = nx;
    }
    return sm;
  }

  /** Every reach upstream of (and including) a reach: Uint8Array mask in reach order. */
  function upstream(S, seg) {
    const n = S.d.length, ups = new Array(n);
    for (let i = 0; i < n; i++) { const d = S.d[i]; if (d >= 0) (ups[d] ||= []).push(i); }
    const mask = new Uint8Array(n), stack = [seg];
    while (stack.length) { const s = stack.pop(); if (mask[s]) continue; mask[s] = 1; (ups[s] || []).forEach((u) => stack.push(u)); }
    return mask;
  }

  RF.runs = { defaults, find, path, upstream, segmentScores, percentiles, paretoLayers };
})();
