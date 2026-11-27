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
      rankBy: null,                    // null: the model when one is chosen, else "pareto" (see rankings)
    };
  }

  /** How runs can be ranked and coloured (params.rankBy); the order the settings offer them. */
  const RANKINGS = [
    { id: "model", label: "Model", note: "The chosen model's score: its national percentile, 100 is most like the known runs" },
    { id: "pareto", label: "Pareto layer", note: "The frontier first: runs that no other run beats on flood flow, gradient and height-to-area at once; then the next layer, and so on. Within a layer, the weighted score" },
    { id: "wsum", label: "Weighted score", note: "The weighted mean of the three part scores (flood flow, gradient, height-to-area), 0 to 100" },
    { id: "sig", label: "Similarity to known runs", note: "How close a reach's terrain is to the average reach of the known runs, as a percentile" },
    { id: "comp", label: "Your feature weights", note: "Your weights on the standardised terrain features, as a percentile among reaches of 2 km² or more" },
  ];
  /** The ranking in use: params.rankBy, else the model when there is one, else the Pareto layers. */
  const rankOf = (p, hasModel) => (RANKINGS.some((r) => r.id === p?.rankBy) ? p.rankBy : hasModel ? "model" : "pareto");

  /** The features the feature weights apply to, in order: [{id, label, weight (default)}]. */
  function features() {
    const dw = LEG().default_weights || {}, desc = Object.assign({}, LEG().descriptions || {}, RF.models?.descriptions || {});
    return Object.keys(dw).map((id) => ({ id, label: desc[id] || id, weight: dw[id] }));
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

  /** What scoring a set of reaches needs for one region, parameter set and model (shared by find and measure). */
  function context(R, opts) {
    const S = R.segments, p = opts.params || RF.state.runParams || defaults();
    const model = opts.model || null, rankBy = opts.rankBy || rankOf(p, !!model);
    const D = segmentScores(S, p);
    return { R, S, p, model, role: model?.role || null, rankBy, D, scores: rankScores(S, D, rankBy, p, model),
      listed: new Set(S.listed),
      // reaches on a known guide section (R.sections): a run touching one counts as in the guides
      onGuide: new Set((R.sections || []).flatMap((x) => x.m)) };
  }

  /**
   * Connected pieces of a set of reaches (joined by downstream steps inside the set), largest catchment first.
   * Each: {members, top, bottom, stem, len (m)}: bottom = the outlet, stem = main stem top to bottom (at a
   * confluence it follows the larger catchment). A run is one piece whose stem is the whole run.
   */
  function pieces(S, members) {
    const set = new Set(members), up = new Map();
    set.forEach((m) => { const d = S.d[m]; if (set.has(d)) { if (!up.has(d)) up.set(d, []); up.get(d).push(m); } });
    return [...set].filter((m) => !set.has(S.d[m])).map((bottom) => {
      const all = [], stack = [bottom];
      while (stack.length) { const s = stack.pop(); all.push(s); (up.get(s) || []).forEach((u) => stack.push(u)); }
      const stem = [bottom];
      for (let cur = bottom, us; (us = up.get(cur));) { cur = us.reduce((b, u) => (S.a[u] > S.a[b] ? u : b)); stem.push(cur); }
      stem.reverse();
      return { members: all, top: stem[0], bottom, stem, len: all.reduce((t, m) => t + S.len[m], 0) };
    }).sort((a, b) => S.a[b.bottom] - S.a[a.bottom]);
  }

  /**
   * The numbers of a run for any set of reaches. Separate pieces count together: length and drop are totals
   * (so the gradient is the length-weighted mean), max gradient the steepest reach, score the best reach;
   * catchment area, flood flow, top and bottom belong to the piece with the largest catchment.
   */
  function summarize(C, members) {
    const { S, D, scores, role } = C, P = pieces(S, members), first = P[0].top, last = P[0].bottom;
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
    const topName = Object.entries(counts).sort((a, b) => b[1] - a[1])[0];
    const river = topName ? S.rivers[+topName[0]] : "";
    const len = members.reduce((t, m) => t + S.len[m], 0) / 1000;
    // each piece's main stem drops from its top to its bottom; side branches add their own reaches' drop
    let drop = 0;
    P.forEach((q) => { const st = new Set(q.stem); drop += S.zt[q.top] - S.zb[q.bottom]; q.members.forEach((m) => { if (!st.has(m)) drop += S.zt[m] - S.zb[m]; }); });
    let acc = null, falls = 0, fallsH = 0;
    if (S.dRoad) {
      const near = (A) => members.reduce((t, m) => Math.min(t, A[m]), Infinity);
      acc = { putIn: S.dRoad[first], takeOut: S.dRoad[last], road: near(S.dRoad), track: near(S.dTrack), foot: near(S.dFoot) };
      members.forEach((m) => { falls += S.fallsN[m]; fallsH = Math.max(fallsH, S.fallsH[m]); });
    }
    const isListed = C.listed.has(river) || guide || members.some((m) => C.onGuide.has(m)), ws = wsc / (len * 1000);
    return {
      river, len, drop, grad: drop / (len * 1000) * 1000, effG: eg / (len * 1000), s50,
      aTop: S.a[first], aBot: S.a[last], maf: S.maf[last], ha: has[has.length >> 1], ws,
      score: C.rankBy === "pareto" || C.rankBy === "wsum" ? 100 * ws : score, role: rl, guide: isListed, acc, falls, fallsH,
      status: isListed ? "listed" : rl === 2 ? "heldout" : "candidate", pieces: P.length, top: first, bottom: last,
    };
  }

  /**
   * Find runs in a loaded region.
   * opts: {params (default RF.state.runParams), rankBy (default params.rankBy, else "model" when a model is given,
   *        else "pareto"), model: {pct, role} from RF.data.scores()}
   * Returns runs sorted best first. Each run is a feature reference ({kind: "run", region, id, members, ...}).
   */
  function find(R, opts = {}) {
    const S = R.segments; if (!S) return [];
    const C = context(R, opts), { p, D, rankBy } = C;
    const n = S.g.length, ok = new Uint8Array(n), runId = new Int32Array(n).fill(-1), runs = [];
    for (let i = 0; i < n; i++) ok[i] = D.ws[i] >= p.threshold && (!p.gate || (D.dQ[i] > 0 && D.dS[i] > 0 && D.dH[i] > 0)) ? 1 : 0;
    for (let s = 0; s < n; s++) {
      if (!ok[s] || runId[s] >= 0 || (S.u[s] >= 0 && ok[S.u[s]])) continue;
      const members = []; let gap = [], cur = s;
      while (cur >= 0 && runId[cur] < 0) {
        if (ok[cur]) { members.push(...gap, cur); gap = []; }
        else { gap.push(cur); if (gap.length > p.maxGap) break; }
        cur = S.d[cur];
      }
      if (members.reduce((t, m) => t + S.len[m], 0) / 1000 < p.minLen) continue;
      const rid = runs.length; members.forEach((m) => { runId[m] = rid; });
      const r = summarize(C, members);
      runs.push(Object.assign({ kind: "run", region: R.id, id: `${R.id}:run:${members[0]}`, rid, members, name: r.river || "Unnamed creek" }, r));
    }
    const layers = paretoLayers(runs.map((r) => [Math.log10(Math.max(r.maf, 1e-3)), r.effG, r.ha]));
    runs.forEach((r, i) => { r.layer = layers[i]; });
    return rankBy === "pareto" ? runs.sort((a, b) => a.layer - b.layer || b.ws - a.ws) : runs.sort((a, b) => b.score - a.score);
  }

  let memo = null;
  /**
   * The numbers find() gives a run (len, grad, s50, aTop, aBot, maf, ha, score, acc, falls, ...) for any set of
   * reaches, such as a known section, scored with the same parameters and model. A section placed by river
   * name can be several separate pieces: `pieces` says how many (see summarize). Null when no reaches.
   * opts as find().
   */
  function measure(R, members, opts = {}) {
    const S = R?.segments, ms = S ? [...new Set(members || [])].filter((m) => m >= 0 && m < S.g.length) : [];
    if (!ms.length) return null;
    return summarize(memoized(R, opts), ms);
  }
  function memoized(R, opts) {
    const p = opts.params || RF.state.runParams || defaults(), model = opts.model || null, rankBy = opts.rankBy || rankOf(p, !!model);
    if (!memo || memo.R !== R || memo.p !== p || memo.model !== model || memo.rankBy !== rankBy) memo = context(R, { params: p, model, rankBy });
    return memo;
  }

  /**
   * Every reach's score (0-100, reach order) under a ranking, as runs take their best reach's: the model's
   * percentiles ("model"), 100 × the weighted score ("wsum"; also "pareto", which ranks whole runs), the similarity
   * percentile ("sig") or the percentile of the feature-weighted sum ("comp"). opts as find(); null without reaches.
   */
  function scores(R, opts = {}) { return R?.segments ? memoized(R, opts).scores : null; }

  /** Two Chaikin passes: softens the 5-8 m grid staircase; keeps both end points. */
  function smooth(pts) {
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
  const join = (S, ms) => {
    const pts = [];
    ms.forEach((m) => S.c[m].forEach((pt, k) => {
      const q = pts[pts.length - 1];
      if (k === 0 && q && q[0] === pt[0] && q[1] === pt[1]) return;
      pts.push(pt);
    }));
    return pts;
  };

  /** One smoothed line for reaches already in downstream order (a run): [[lon, lat], ...]. */
  function path(S, members) { return smooth(join(S, members)); }

  /**
   * Smoothed lines for any set of reaches (in any order, with branches): one line per connected
   * downstream chain, so reaches are never joined out of order. Returns [[[lon, lat], ...], ...].
   */
  function chains(S, members) {
    const set = new Set(members), fed = new Set(), seen = new Set(), out = [];
    members.forEach((m) => { if (set.has(S.d[m])) fed.add(S.d[m]); });
    const walk = (start) => {
      const chain = []; let cur = start;
      while (cur >= 0 && set.has(cur) && !seen.has(cur)) { seen.add(cur); chain.push(cur); cur = S.d[cur]; }
      if (!chain.length) return;
      const pts = join(S, chain);
      if (cur >= 0 && seen.has(cur)) pts.push(S.c[cur][0]); // close the joint at a confluence already drawn
      out.push(smooth(pts));
    };
    members.filter((m) => !fed.has(m)).forEach(walk);
    members.forEach((m) => { if (!seen.has(m)) walk(m); });
    return out;
  }

  /** Every reach upstream of (and including) a reach: Uint8Array mask in reach order. */
  function upstream(S, seg) {
    const n = S.d.length, ups = new Array(n);
    for (let i = 0; i < n; i++) { const d = S.d[i]; if (d >= 0) (ups[d] ||= []).push(i); }
    const mask = new Uint8Array(n), stack = [seg];
    while (stack.length) { const s = stack.pop(); if (mask[s]) continue; mask[s] = 1; (ups[s] || []).forEach((u) => stack.push(u)); }
    return mask;
  }

  RF.runs = { defaults, find, measure, scores, pieces, path, chains, smooth, upstream, segmentScores, percentiles, paretoLayers,
    rankings: RANKINGS, rankOf, features };
})();
