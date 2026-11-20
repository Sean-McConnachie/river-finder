/* RF.rain: live rain per reach from the hourly job (bead rf-t26.9; file format and reference decoder in
   src/river_finder/rainfall/live.py). Owner: core.
   Files under RF.config.rainBase (the standalone site only; the artifact host blocks other hosts):
   index.json (runs, windows, attribution, the region list) and <region>.bin.gz (raw gzip bytes, unpacked here
   with DecompressionStream). Loaded on demand, refreshed about every 15 min while the page is visible. Any
   failure, a stale index or a reach-count mismatch ends in a quiet error state ({code, message}), never a throw
   into the page. Events: RF.emit("rain", {region}) when a region's state changes. */
(() => {
  "use strict";
  const RF = window.RF;
  const BASE = RF.config.target === "site" ? String(RF.config.rainBase || "") : "";
  const WINDOWS = [1, 3, 6, 12, 24];
  const REFRESH_MS = 15 * 60e3, RETRY_MS = 60e3, STALE_HOURS = 12, TIMEOUT_MS = 20e3, KEEP = 3;
  const MAGIC = [0x52, 0x46, 0x52, 0x31]; // "RFR1"
  const UNAVAILABLE = "Live rain unavailable";
  const MSG = {
    host: "Live rain needs the standalone site: this host blocks outside data.",
    off: "Live rain is not part of this build.",
    browser: `${UNAVAILABLE}: this browser cannot unpack the files.`,
    reference: "Live rain isn't available for the 5 m reference areas.",
    missing: `${UNAVAILABLE} for this region.`,
    mismatch: `${UNAVAILABLE}: the rain files are for another build of the river network.`,
    network: `${UNAVAILABLE}: the rain files did not load.`,
    format: `${UNAVAILABLE}: the rain files have an unknown format.`,
  };
  const PERMANENT = new Set(["host", "off", "browser", "reference"]);
  const canGunzip = typeof DecompressionStream === "function" && typeof Response === "function" && typeof Blob === "function";
  const failure = (code, message, extra) => Object.assign(new Error(message || MSG[code] || UNAVAILABLE), { code }, extra);

  // ---------- times ----------
  /** "2026-11-20 18:00Z" (an hour, as in the files) or ISO -> Date (null if unreadable). */
  const parseTime = (s) => { if (!s) return null; const d = new Date(String(s).trim().replace(" ", "T")); return isNaN(d) ? null : d; };
  const NZ = (() => {
    try {
      const o = { timeZone: "Pacific/Auckland" };
      return { time: new Intl.DateTimeFormat("en-NZ", { ...o, hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZoneName: "short" }),
        day: new Intl.DateTimeFormat("en-NZ", { ...o, weekday: "short" }), date: new Intl.DateTimeFormat("en-CA", o) };
    } catch (e) { return null; }
  })();
  /** NZ local time of an hour: "07:00 NZDT", with the weekday when it is not today in NZ ("Sat 07:00 NZDT"). */
  function nzTime(s, opts = {}) {
    const d = s instanceof Date ? s : parseTime(s);
    if (!d) return "–";
    if (!NZ) return `${d.toISOString().slice(11, 16)} UTC`;
    const t = NZ.time.format(d), same = NZ.date.format(d) === NZ.date.format(new Date());
    return (same && !opts.day) || opts.day === false ? t : `${NZ.day.format(d)} ${t}`;
  }
  /** "just now", "12 min ago", "3 h ago". */
  function age(d) {
    d = d instanceof Date ? d : parseTime(d);
    if (!d) return "–";
    const min = Math.max(0, (Date.now() - d.getTime()) / 60e3);
    if (min < 2) return "just now";
    if (min < 90) return `${Math.round(min)} min ago`;
    const h = min / 60;
    return h < 48 ? `${h < 10 ? h.toFixed(1).replace(/\.0$/, "") : Math.round(h)} h ago` : `${Math.round(h / 24)} days ago`;
  }
  const hoursSince = (d) => (d ? (Date.now() - d.getTime()) / 3600e3 : Infinity);

  // ---------- decoding (the contract in rainfall/live.py) ----------
  async function gunzip(bytes) {
    const ds = new DecompressionStream("gzip");
    return new Uint8Array(await new Response(new Blob([bytes]).stream().pipeThrough(ds)).arrayBuffer());
  }
  /** Decode a region file (gzip or plain bytes) -> {head, cols}: per field k, a typed array of n values (u16null:
   *  Float32Array with NaN for "no value"; u16log: Float32Array; u8: Uint8Array) or, for a constant field, its
   *  value (a number, or null). */
  async function decode(input) {
    let b = input instanceof Uint8Array ? input : new Uint8Array(input);
    if (b[0] === 0x1f && b[1] === 0x8b) {
      if (!canGunzip) throw failure("browser");
      b = await gunzip(b);
    }
    if (b.length < 12 || MAGIC.some((m, i) => b[i] !== m)) throw failure("format");
    if (b.byteOffset % 8) b = b.slice(); // typed views need an aligned buffer
    const v = new DataView(b.buffer, b.byteOffset, b.byteLength), H = v.getUint32(4, true), D = v.getUint32(8, true);
    const head = JSON.parse(new TextDecoder().decode(b.subarray(12, 12 + H))), n = head.n | 0, cols = {};
    for (const f of head.fields || []) {
      if ("const" in f) { cols[f.k] = f.const; continue; }
      const off = b.byteOffset + D + f.off;
      if (f.kind === "u8") { cols[f.k] = new Uint8Array(b.buffer, off, n); continue; }
      const q = new Uint16Array(b.buffer, off, n), out = new Float32Array(n);
      if (f.kind === "u16log") for (let i = 0; i < n; i++) out[i] = 10 ** (q[i] * f.scale + f.add);
      else if (f.kind === "u16null") for (let i = 0; i < n; i++) out[i] = q[i] === 0 ? NaN : (q[i] - 1) * f.scale + f.add;
      else throw failure("format", `${MSG.format} (field kind ${f.kind})`);
      cols[f.k] = out;
    }
    return { head, cols };
  }

  // ---------- fetching ----------
  async function get(url, json) {
    const ctl = typeof AbortController === "function" ? new AbortController() : null;
    const timer = ctl ? setTimeout(() => ctl.abort(), TIMEOUT_MS) : 0;
    try {
      // revalidate (the files are cached 5 min); cheap 304 when unchanged
      const res = await fetch(url, { cache: "no-cache", credentials: "omit", signal: ctl?.signal });
      if (!res.ok) throw failure("network", `${MSG.network} (HTTP ${res.status})`);
      return json ? await res.json() : new Uint8Array(await res.arrayBuffer());
    } catch (e) {
      throw e.code ? e : failure("network");
    } finally { clearTimeout(timer); }
  }

  let index = null, indexAt = 0, indexP = null, indexErr = null, indexErrAt = 0;
  /** The current index.json (cached; refetched once older than 15 min). Rejects with {code, message}. */
  function loadIndex(force = false) {
    if (!BASE) return Promise.reject(failure(RF.config.target === "site" ? "off" : "host"));
    if (indexP) return indexP;
    if (index && !force && Date.now() - indexAt < REFRESH_MS) return Promise.resolve(index);
    if (!index && indexErr && !force && Date.now() - indexErrAt < RETRY_MS) return Promise.reject(indexErr);
    indexP = get(`${BASE}index.json`, true).then((ix) => {
      if (!ix || ix.schema !== 1 || !Array.isArray(ix.regions)) throw failure("format");
      index = ix; indexAt = Date.now(); indexErr = null;
      return ix;
    }, (e) => {
      indexErr = e; indexErrAt = Date.now();
      if (index) { indexAt = Date.now() - REFRESH_MS + 5 * 60e3; return index; } // keep the last good one; retry in 5 min
      throw e;
    }).finally(() => { indexP = null; });
    return indexP;
  }

  // ---------- regions ----------
  const regions = new Map(); // id -> {status: "loading" | "ok" | "error", data, error, at, promise, generated}
  const isReference = (id) => /reference/i.test(RF.data.entry(id)?.group || "") || !!index?.omitted?.[id];
  const siteCount = (id) => RF.data.loaded(id)?.segments?.g?.length ?? RF.data.entry(id)?.segments ?? null;

  function stale(data) {
    const h = hoursSince(data.generated);
    return h > STALE_HOURS ? failure("stale", `${UNAVAILABLE}: the last update was ${age(data.generated)}.`) : null;
  }

  function setState(id, st) {
    regions.delete(id); regions.set(id, st); // most recent last
    for (const k of regions.keys()) { if (regions.size <= KEEP) break; if (k !== id && k !== RF.state.region) regions.delete(k); }
    RF.emit("rain", { region: id });
  }

  async function fetchRegion(id) {
    if (!BASE) throw failure(RF.config.target === "site" ? "off" : "host");
    if (/reference/i.test(RF.data.entry(id)?.group || "")) throw failure("reference");
    const ix = await loadIndex();
    if (ix.omitted?.[id]) throw failure("reference");
    const ixTime = parseTime(ix.generated);
    if (hoursSince(ixTime) > STALE_HOURS) throw failure("stale", `${UNAVAILABLE}: the last update was ${age(ixTime)}.`);
    const entry = ix.regions.find((r) => r.id === id);
    if (!entry) throw failure("missing");
    const n = siteCount(id);
    if (n != null && entry.n !== n) throw failure("mismatch");
    const prev = regions.get(id)?.data;
    if (prev && prev.sha === entry.sha256) return prev; // unchanged since the last load
    const { head, cols } = await decode(await get(`${BASE}${encodeURIComponent(entry.file)}`, false));
    if (head.schema !== 1 || head.region !== id) throw failure("format");
    if (head.n !== entry.n || (n != null && head.n !== n)) throw failure("mismatch");
    const data = { id, n: head.n, head, cols, index: ix, entry, sha: entry.sha256, generated: parseTime(head.generated || ix.generated) };
    const s = stale(data); if (s) throw s;
    return data;
  }

  function load(id) {
    const cur = regions.get(id);
    if (cur?.promise) return cur.promise;
    const p = fetchRegion(id).then((data) => { setState(id, { status: "ok", data, at: Date.now() }); return data; }, (error) => {
      setState(id, { status: "error", error, at: Date.now(), data: null });
      throw error;
    });
    p.catch(() => {}); // callers may ignore it; the state carries the error
    regions.set(id, Object.assign({}, cur || {}, { status: cur?.data ? "ok" : "loading", promise: p }));
    if (!cur?.data) RF.emit("rain", { region: id });
    startRefresh();
    return p;
  }

  /** Rain for a region: resolves to its data, rejects with {code, message} (cached; reloads failures after a minute). */
  function region(id) {
    const cur = regions.get(id);
    if (cur?.promise) return cur.promise;
    if (cur?.status === "ok") {
      const s = stale(cur.data);
      if (!s) return Promise.resolve(cur.data);
      setState(id, { status: "error", error: s, at: Date.now(), data: null });
      return Promise.reject(s);
    }
    if (cur?.status === "error" && (PERMANENT.has(cur.error.code) || Date.now() - cur.at < RETRY_MS)) return Promise.reject(cur.error);
    return load(id);
  }

  /** Synchronous snapshot for rendering: {status: "idle" | "loading" | "ok" | "error", data, code, message}. Starts no fetch. */
  function state(id) {
    if (!BASE) { const e = failure(RF.config.target === "site" ? "off" : "host"); return { status: "error", code: e.code, message: e.message, data: null }; }
    if (id && isReference(id)) return { status: "error", code: "reference", message: MSG.reference, data: null };
    const cur = id ? regions.get(id) : null;
    if (!cur) return { status: "idle", data: null };
    if (cur.status === "ok") {
      const s = stale(cur.data);
      return s ? { status: "error", code: s.code, message: s.message, data: null } : { status: "ok", data: cur.data };
    }
    if (cur.status === "error") return { status: "error", code: cur.error.code, message: cur.error.message, data: null };
    return { status: "loading", data: null };
  }

  // ---------- refresh while visible ----------
  let timer = null;
  function startRefresh() {
    if (timer || !BASE) return;
    timer = setInterval(check, 60e3);
    document.addEventListener("visibilitychange", check);
  }
  async function check() {
    if (document.visibilityState === "hidden") return;
    const ids = [...regions.keys()].filter((id) => !PERMANENT.has(regions.get(id)?.error?.code));
    if (!ids.length) return;
    if (Date.now() - indexAt >= REFRESH_MS && !indexP) {
      const before = index?.generated;
      try { await loadIndex(true); } catch (e) { /* the state below says how old the data are */ }
      const changed = index && index.generated !== before;
      ids.forEach((id) => {
        const cur = regions.get(id);
        if (!cur || cur.promise) return;
        if (id !== RF.state.region) { if (changed || cur.status === "error") regions.delete(id); return; } // reloads when next asked for
        if (changed || cur.status === "error") load(id);
        else if (cur.status === "ok" && stale(cur.data)) setState(id, { status: "error", error: stale(cur.data), at: Date.now(), data: null });
        else RF.emit("rain", { region: id }); // ages moved on
      });
    } else ids.forEach((id) => RF.emit("rain", { region: id, tick: true }));
  }

  // ---------- per reach ----------
  const val = (c, i) => (c == null ? null : typeof c === "number" ? c : c[i]);
  const num = (v) => (v == null || !isFinite(v) ? null : v);
  /** Bit-mask products -> their tags (the header's products list for "obs" | "fc"). */
  const tags = (data, p, bits) => (data.head.products?.[p] || []).filter((t, k) => bits & (1 << k));
  /** One reach of a region's data: {reach, area (km²), flags, past: {x: w}, next: {x: w}} with
   *  w = {mm, m3, miss, products, scale} (mm and m3 null: no value). */
  function at(data, reach) {
    if (!data || reach == null || reach < 0 || reach >= data.n) return null;
    const c = data.cols, area = num(val(c.area_km2, reach)), out = { reach, area, flags: val(c.flags, reach) | 0, past: {}, next: {} };
    for (const [kind, p] of [["past", "obs"], ["next", "fc"]]) {
      for (const x of WINDOWS) {
        const mm = num(val(c[`${p}_mm_${x}h`], reach));
        out[kind][x] = { mm, m3: mm == null || area == null ? null : mm * area * 1000, miss: val(c[`${p}_miss_${x}h`], reach) | 0,
          products: tags(data, p, val(c[`${p}_prod_${x}h`], reach) | 0), scale: val(c[`${p}_scale_${x}h`], reach) | 0 };
      }
    }
    return out;
  }
  /** Times of window x of "past" (observed, ending at the window end) or "next" (forecast, from its start): {start, end} Dates. */
  function span(data, kind, x) {
    const h = data?.head;
    if (kind === "past") { const end = parseTime(h?.observed?.window_end); return end ? { start: new Date(end - x * 3600e3), end } : null; }
    const start = parseTime(h?.forecast?.start);
    return start ? { start, end: new Date(+start + x * 3600e3) } : null;
  }

  const fmt = {
    /** mm: "0", "0.4", "12.3", "140". */
    mm: (v) => (v == null || !isFinite(v) ? "–" : v === 0 ? "0" : v < 100 ? v.toFixed(1) : v.toFixed(0)),
    /** m³: "850", "3.4k", "120k", "4.1M", "120M". */
    m3: (v) => {
      if (v == null || !isFinite(v)) return "–";
      const s = (x, d) => x.toFixed(d).replace(/\.0$/, "");
      return v < 1e3 ? String(Math.round(v)) : v < 1e6 ? `${s(v / 1e3, v < 1e4 ? 1 : 0)}k` : v < 1e9 ? `${s(v / 1e6, v < 1e7 ? 1 : 0)}M` : `${s(v / 1e9, 1)}G`;
    },
  };

  RF.rain = {
    /** Live rain is configured for this page (standalone site with a rain URL and a browser that can unpack it). */
    enabled: !!BASE && canGunzip,
    base: BASE,
    windows: WINDOWS.slice(),
    staleHours: STALE_HOURS,
    unavailable: UNAVAILABLE,
    index: () => loadIndex(),
    region, state, at, span, decode,
    time: nzTime, age, parseTime, fmt,
  };
  if (BASE && !canGunzip) RF.rain.state = () => ({ status: "error", code: "browser", message: MSG.browser, data: null });
})();
