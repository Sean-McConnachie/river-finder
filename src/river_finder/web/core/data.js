/* RF.data and RF.models: the regions index, region loading (JSON + packed reaches + optional
   waterfalls/access), per-model scores, hydro-affected reaches and the model registry. Owner: core.
   Files are the ones `river-finder nz viewer` / `models viewer` / `external attach` write
   (see viewer_nz.py, models.py, external.py); this reads them unchanged. */
(() => {
  "use strict";
  const RF = window.RF, P = RF.payload;
  const INDEX = (P.regions || []).map((r) => Object.assign({ group: "Regions" }, r));
  const cache = new Map(), done = new Map();

  // Packed reaches: one little-endian buffer + header (viewer_nz.pack_segments).
  function decodeSegments(buf, H, info) {
    const n = H.n, S = Object.assign({}, info);
    H.fields.forEach((f) => {
      const out = new Array(n);
      if (f.kind === "i32") { const a = new Int32Array(buf, f.off, n); for (let i = 0; i < n; i++) out[i] = a[i]; }
      else if (f.kind === "u8") { const a = new Uint8Array(buf, f.off, n); for (let i = 0; i < n; i++) out[i] = a[i]; }
      else {
        const a = new Uint16Array(buf, f.off, n);
        for (let i = 0; i < n; i++) {
          const q = a[i];
          out[i] = f.kind === "u16log" ? 10 ** (q * f.scale + f.add)
            : f.kind === "u16null" ? (q === 0 ? null : (q - 1) * f.scale + f.add) : q * f.scale + f.add;
        }
      }
      S[f.k] = out;
    });
    const nf = H.z.nf, z = new Int8Array(buf, H.z.off, n * nf);
    S.z = new Array(n);
    for (let i = 0; i < n; i++) { const row = new Array(nf); for (let k = 0; k < nf; k++) row[k] = z[i * nf + k] * H.z.scale; S.z[i] = row; }
    const C = H.coords, cnt = new Uint16Array(buf, C.count, n), first = new Int32Array(buf, C.first, 2 * n),
      del = new Int16Array(buf, C.delta, 2 * C.ndelta), sc = C.scale;
    S.c = new Array(n);
    for (let i = 0, p = 0; i < n; i++) {
      let x = first[2 * i], y = first[2 * i + 1];
      const pts = [[x * sc, y * sc]];
      for (let k = 1; k < cnt[i]; k++, p++) { x += del[2 * p]; y += del[2 * p + 1]; pts.push([x * sc, y * sc]); }
      S.c[i] = pts;
    }
    return S;
  }

  async function getText(url, what) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`Could not load ${what} (HTTP ${res.status}).`);
    return res.text();
  }

  // Waterfalls, rapids and access (river_finder.external): <id>_x.txt holds per reach three uint16
  // distances in 10 m (road, 4WD/private road, foot track), then uint8 waterfall count and uint8
  // tallest height (m). <id>_falls.json = [lon, lat, name, height m | null, reach index | -1, kind ("r" = rapids)].
  async function loadExternal(R) {
    const S = R.segments; if (!S) return;
    try {
      const [xr, fr] = await Promise.all([fetch(`${R.id}_x.txt`), fetch(`${R.id}_falls.json`)]);
      if (xr.ok) {
        const b = RF.util.b64bytes(await xr.text()), buf = b.buffer, n = S.g.length;
        const d = (k) => Array.from(new Uint16Array(buf, 2 * n * k, n), (v) => v * 10);
        S.dRoad = d(0); S.dTrack = d(1); S.dFoot = d(2);
        S.fallsN = Array.from(new Uint8Array(buf, 6 * n, n)); S.fallsH = Array.from(new Uint8Array(buf, 7 * n, n));
      }
      R.falls = fr.ok ? await fr.json() : [];
    } catch (e) { R.falls = R.falls || []; /* optional layer */ }
  }

  // Known sections placed on this region's reaches (river_finder.webdata): <id>_sections.json.
  async function loadSections(R) {
    R.sections = [];
    try { const res = await fetch(`${R.id}_sections.json`); if (res.ok) R.sections = await res.json(); } catch (e) { /* optional */ }
  }

  async function load(entry) {
    const R = Object.assign({}, entry);
    if (R.src) {
      Object.assign(R, JSON.parse(await getText(R.src, R.title)));
      if (R.segB64) R.segments = decodeSegments(RF.util.b64bytes(await getText(R.segB64, `the ${R.title} reaches`)).buffer, R.segHead, R.segInfo);
    }
    R.falls = R.falls || [];
    await Promise.all([loadExternal(R), loadSections(R)]);
    R.loaded = true;
    return R;
  }

  RF.data = {
    /** Index of every region: [{id, title, group, bounds: [[lat0, lon0], [lat1, lon1]], segments}]. */
    regions: () => INDEX.slice(),
    /** Region id to open when nothing else says which: last used, else the first. */
    defaultRegion: () => { const r = RF.store.get("region"); return INDEX.some((e) => e.id === r) ? r : INDEX[0]?.id ?? null; },
    entry: (id) => INDEX.find((r) => r.id === id) || null,
    /** Load a region once (cached). Resolves to R: the index entry plus
     *  segments (S, columnar: see README), falls, runs (reference markers), rec2, stats, image fields. */
    region(id) {
      const entry = RF.data.entry(id);
      if (!entry) return Promise.reject(new Error(`Unknown region "${id}".`));
      if (!cache.has(id)) {
        const p = load(entry);
        p.then((R) => done.set(id, R), () => cache.delete(id));
        cache.set(id, p);
      }
      return cache.get(id);
    },
    /** The already-loaded region, or null (no fetch). */
    loaded: (id) => done.get(id) || null,
    /** Forget a loaded region so its memory can be freed (a later region(id) loads it again). The region open
     *  on the map (RF.state.region) is kept. For pages that scan many regions and keep only summaries. */
    release(id) {
      if (!id || id === RF.state.region) return false;
      const had = cache.delete(id); done.delete(id);
      return had;
    },
    /** Per-model reach scores: {pct: number[] (percentile 0-100), role: number[] | null}.
     *  role bits 0-1: 1 = the model trained on this reach's river, 2 = test river; bit 2: on a guide section.
     *  Falls back to the scores packed with the reaches when a model file is missing. */
    async scores(regionId, modelId) {
      const R = await RF.data.region(regionId), S = R.segments;
      if (!S) return { pct: [], role: null };
      R.mscore = R.mscore || {};
      if (!R.mscore[modelId]) {
        try {
          if (!modelId) throw new Error("no model");
          const b = RF.util.b64bytes(await getText(`${R.id}_m_${modelId}.txt`, "model scores")), n = S.g.length;
          R.mscore[modelId] = { pct: Array.from(new Uint16Array(b.buffer, 0, n), (v) => v / 100), role: Array.from(new Uint8Array(b.buffer, 2 * n, n)) };
        } catch (e) { R.mscore[modelId] = { pct: S.reg.slice(), role: null, fallback: true }; }
      }
      return R.mscore[modelId];
    },
    /** National gauges (river_finder.webdata): {flow: [[site, name, lat, lon, council, operator, funder,
     *  area km², years, mean annual flood, Q10, Q100]], rain: [[agent, name, lat, lon, owner]], sources}. */
    gauges() {
      if (!gaugesP) gaugesP = fetch("gauges.json").then((r) => (r.ok ? r.json() : { flow: [], rain: [] })).catch(() => ({ flow: [], rain: [] }));
      return gaugesP;
    },
    /** Hydro-affected reaches of a region (river_finder.webdata.write_hydro): <id>_hydro.json, optional, fetched once.
     *  Resolves to the file ({classes, exclude, notes, schemes, r: [[reach, class, scheme, share %]]}) plus
     *  at: Map(reach → [class name, scheme record, share %]); null when the region has no file. Never rejects. */
    hydro(id) {
      if (!hydroP.has(id)) {
        hydroP.set(id, fetch(`${id}_hydro.json`).then((res) => (res.ok ? res.json() : null)).then((H) => {
          if (!H || !Array.isArray(H.r)) return null;
          H.at = new Map(H.r.map(([m, c, s, share]) => [m, [H.classes[c], H.schemes[s], share ?? 100]]));
          return H;
        }).catch(() => null));
      }
      return hydroP.get(id);
    },
    /** The hydro class over most of the flagged length of a set of reaches (S = Region.segments, H from hydro()):
     *  {cls, scheme, frac (flagged share of the length, 0-1), note, out (class in H.exclude: not a new lead)}; null
     *  when none is flagged. Pages pick their own threshold on frac (Top rivers and the details pill: 0.5). */
    hydroOf(H, S, members) {
      if (!H?.at?.size || !S) return null;
      const by = new Map(); let all = 0, hit = 0;
      (members || []).forEach((m) => {
        const L = S.len[m] || 0; all += L;
        const h = H.at.get(m); if (!h) return;
        hit += L; const key = `${h[0]}|${h[1]?.id}`, cur = by.get(key);
        if (cur) cur.len += L; else by.set(key, { cls: h[0], scheme: h[1], len: L });
      });
      if (!hit || !all) return null;
      const top = [...by.values()].sort((a, b) => b.len - a.len)[0];
      return { cls: top.cls, scheme: top.scheme, frac: hit / all, note: H.notes?.[top.cls] || "", out: (H.exclude || []).includes(top.cls) };
    },
    decodeSegments,
  };
  let gaugesP = null;
  const hydroP = new Map();   // region id → Promise<hydro file | null>

  // ---------- model registry (models/*.json cards, models/evaluations.json) ----------
  const REG = P.registry || null, CARDS = REG ? REG.models : [];
  RF.models = {
    /** Model cards: [{id, title, description, created, kind, coef, intercept, variant, split, train_rivers, test_rivers, ...}]. */
    list: () => CARDS.slice(),
    get: (id) => CARDS.find((m) => m.id === id) || null,
    default: REG ? REG.default : null,
    /** {splits, evaluations} from models/evaluations.json, or null. */
    evaluations: REG ? REG.evaluations : null,
    /** Feature descriptions keyed by feature name. */
    descriptions: REG ? REG.descriptions : {},
    /** Cross-validated model zoo (models.cv_summary of models/cv/results.json): {created, folds, repeats,
     *  domain_km2, spec_version, models: [{id, kind, title, description, inputs, cv: {auc, auc_style, auc_size,
     *  auc_style_size, recall_top10, recall_top5, median_pct: {mean, sd, fold_sd}}, loro_auc_style}]}, or null. */
    cv: REG ? REG.cv || null : null,
    /** Pooled regression and run-finder defaults from model.json (coef, holdout, lorocv, run_params, ...), or null. */
    legacy: P.model || null,
  };
})();
