/* River Finder core: the window.RF namespace, events, shared state, page registry and router.
   Owner: core. The contract other pages may rely on is documented in src/river_finder/web/README.md;
   anything not listed there is internal and may change. */
(() => {
  "use strict";
  const DATA = JSON.parse(document.getElementById("rf-data").textContent);

  // Navigation order and icons. Page modules register themselves with RF.page(id, def).
  const NAV = [
    { id: "map", title: "Map", icon: "map" },
    { id: "top", title: "Top rivers", icon: "list" },
    { id: "models", title: "Models", icon: "model" },
    { id: "info", title: "Info", icon: "info" },
  ];
  const ICONS = {
    map: '<path d="M9 4 3 6.5v13L9 17l6 2.5 6-2.5v-13L15 6.5 9 4Z"/><path d="M9 4v13M15 6.5v13"/>',
    list: '<path d="M8 6h13M8 12h13M8 18h13"/><path d="M3.5 6h.01M3.5 12h.01M3.5 18h.01" stroke-width="3"/>',
    model: '<path d="M4 19V9M10 19V5M16 19v-7M22 19H2"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.01" />',
    close: '<path d="M6 6l12 12M18 6 6 18"/>',
    back: '<path d="M15 5l-7 7 7 7"/>',
    copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/>',
    external: '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
    layers: '<path d="m12 3 9 5-9 5-9-5 9-5Z"/><path d="m3 13 9 5 9-5"/>',
    pin: '<path d="M12 21s-7-6.5-7-12a7 7 0 0 1 14 0c0 5.5-7 12-7 12Z"/><circle cx="12" cy="9" r="2.5"/>',
    river: '<path d="M3 6c3 0 3 3 6 3s3-3 6-3 3 3 6 3M3 12c3 0 3 3 6 3s3-3 6-3 3 3 6 3"/>',
    // a catchment: the basin outline and its streams joining at the outlet
    catchment: '<path d="M12 21 5 15.5 3.5 9 7.5 4l4.5 1.5L16.5 4l4 5-1.5 6.5Z"/><path d="M12 21v-8.5M12 12.5 8.5 9M12 15.5l3.5-4"/>',
    logo: '<path d="M4 20c2.5-5 6-6.5 8-8.5S14.5 6 13 3"/><path d="M11 20c2.5-3 6-4 7.5-6S20 9 19 6" opacity=".55"/>',
  };
  const icon = (name, cls = "") =>
    `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ""}</svg>`;

  const pages = {}, handlers = {};
  const RF = (window.RF = {
    version: 1,
    /** Build-time settings: {target: "site" | "artifact", built, commit, basemapsKey, ...}. */
    config: DATA.app || {},
    /** Raw build payload (regions index, model registry). Prefer RF.data / RF.models. */
    payload: DATA,
    nav: NAV,
    /** Shared state. Read freely; change only through the setters (they emit events). */
    state: { page: null, region: null, model: null, selection: null, runParams: null },

    // ---------- events ----------
    /** Subscribe; returns an unsubscribe function. Events: ready, page, region, model, runs, select. */
    on(evt, fn) { (handlers[evt] ||= new Set()).add(fn); return () => handlers[evt].delete(fn); },
    off(evt, fn) { handlers[evt]?.delete(fn); },
    emit(evt, payload) {
      (handlers[evt] || []).forEach((fn) => { try { fn(payload); } catch (e) { console.error(`RF "${evt}" handler failed`, e); } });
    },

    // ---------- pages ----------
    /** Register a page: def = {title, mount(el), show(params), hide()}. mount runs once, on first show. */
    page(id, def) { pages[id] = Object.assign({ id, mounted: false }, def); },
    /** Open a page (and pass params to its show()). E.g. RF.go("map", {select: feature}). */
    go(id, params = {}, opts = {}) {
      const def = pages[id] || pages.map;
      if (!def) return;
      const prev = RF.state.page && pages[RF.state.page];
      if (prev && prev !== def) { try { prev.hide?.(); } catch (e) { console.error(e); } }
      document.querySelectorAll(".rf-page").forEach((el) => { el.hidden = el.dataset.page !== def.id; });
      const el = document.getElementById(`page-${def.id}`);
      if (!def.mounted) { def.mounted = true; try { def.mount?.(el); } catch (e) { console.error(`mount ${def.id}`, e); } }
      RF.state.page = def.id;
      document.querySelectorAll(".rf-nav a").forEach((a) => {
        if (a.dataset.page === def.id) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
      });
      const title = def.title || id;
      document.title = def.id === "map" ? "River Finder" : `${title} · River Finder`;
      if (!opts.fromHistory) {
        const h = `#${def.id}`;
        try { if (location.hash !== h) (opts.replace ? history.replaceState : history.pushState).call(history, { rfPage: def.id }, "", h); } catch (e) { /* sandboxed */ }
      }
      try { def.show?.(params); } catch (e) { console.error(`show ${def.id}`, e); }
      RF.emit("page", { id: def.id, params });
    },
    current() { return RF.state.page; },
    pages,

    // ---------- shared state setters ----------
    /** Load a region (see RF.data.region) and make it current. Resolves to the region object. */
    async setRegion(id) {
      const R = await RF.data.region(id);
      if (RF.state.region !== R.id) {
        RF.state.region = R.id;
        RF.store.set("region", R.id);
        RF.emit("region", { id: R.id, region: R });
      }
      return R;
    },
    /** Make a model current (id from RF.models.list()). */
    setModel(id) {
      if (!RF.models.get(id) || RF.state.model === id) return;
      RF.state.model = id;
      RF.store.set("model", id);
      RF.emit("model", { id });
    },
    /** Replace the run-finder parameters (see RF.runs.defaults()); null restores the defaults. */
    setRunParams(params) {
      RF.state.runParams = params ? Object.assign(RF.runs.defaults(), params) : RF.runs.defaults();
      RF.emit("runs", { params: RF.state.runParams });
    },
    /** Select a feature (see README: feature references). Pages listen for "select". */
    select(feature) { RF.state.selection = feature || null; RF.emit("select", { feature: RF.state.selection }); },

    // ---------- per-browser preferences ----------
    store: {
      get(key, fallback = null) {
        try { const v = localStorage.getItem(`rf:${key}`); return v === null ? fallback : JSON.parse(v); } catch (e) { return fallback; }
      },
      set(key, value) { try { localStorage.setItem(`rf:${key}`, JSON.stringify(value)); } catch (e) { /* private mode */ } },
      remove(key) { try { localStorage.removeItem(`rf:${key}`); } catch (e) { /* ignore */ } },
    },

    // ---------- small utilities ----------
    util: {
      icon,
      $: (sel, root = document) => root.querySelector(sel),
      $$: (sel, root = document) => Array.from(root.querySelectorAll(sel)),
      /** el("div", {class: "x", onclick: fn}, child, "text") */
      el(tag, props = {}, ...children) {
        const e = document.createElement(tag);
        for (const [k, v] of Object.entries(props || {})) {
          if (v === null || v === undefined || v === false) continue;
          if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
          else if (k === "html") e.innerHTML = v;
          else if (k === "text") e.textContent = v;
          else if (k === "dataset") Object.assign(e.dataset, v);
          else e.setAttribute(k, v === true ? "" : v);
        }
        children.flat().forEach((c) => { if (c !== null && c !== undefined && c !== false) e.append(c.nodeType ? c : String(c)); });
        return e;
      },
      esc: (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]),
      fmt: {
        km2: (v) => v == null || !isFinite(v) ? "–" : v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v >= 1 ? v.toFixed(2) : v.toFixed(3),
        flow: (v) => v == null || !isFinite(v) ? "–" : v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2),
        grad: (v) => v == null || !isFinite(v) ? "–" : v.toFixed(0),
        km: (m) => m == null || !isFinite(m) ? "–" : m >= 10000 ? `${(m / 1000).toFixed(0)} km` : m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m)} m`,
        dist: (m) => m >= 20000 ? "over 20 km" : m < 50 ? "on it" : m < 1000 ? `${Math.round(m / 10) * 10} m` : `${(m / 1000).toFixed(1)} km`,
        coord: (lat, lon) => `${lat.toFixed(5)}, ${lon.toFixed(5)}`,
        int: (v) => v == null ? "–" : Math.round(v).toLocaleString("en-NZ"),
      },
      /** Copy text; resolves true on success. Must run inside a click handler. */
      async copy(text) {
        try { await navigator.clipboard.writeText(text); RF.util.toast("Copied"); return true; } catch (e) {
          const ta = document.createElement("textarea"); ta.value = text; ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.opacity = "0";
          document.body.append(ta); ta.select();
          let ok = false; try { ok = document.execCommand("copy"); } catch (e2) { ok = false; }
          ta.remove(); RF.util.toast(ok ? "Copied" : "Select the text to copy it"); return ok;
        }
      },
      /** A value with a Copy button (coordinates, ids). */
      copyable(text, label = "Copy") {
        const { el } = RF.util;
        return el("span", { class: "rf-copy" }, el("code", { text }),
          el("button", { type: "button", class: "rf-btn", "aria-label": `${label}: ${text}`, onclick: () => RF.util.copy(text) }, label));
      },
      toast(msg, ms = 1600) {
        let t = document.querySelector(".rf-toast");
        if (!t) { t = document.createElement("div"); t.className = "rf-toast"; t.setAttribute("role", "status"); document.body.append(t); }
        t.textContent = msg; t.hidden = false; clearTimeout(t._h); t._h = setTimeout(() => { t.hidden = true; }, ms);
      },
      css: (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim(),
      debounce(fn, ms = 150) { let h; return (...a) => { clearTimeout(h); h = setTimeout(() => fn(...a), ms); }; },
      /** NZTM2000 (EPSG:2193) [E, N] from WGS84 lat/lon (transverse Mercator on GRS80, Snyder 1987). */
      toNZTM(lat, lon) {
        const a = 6378137, f = 1 / 298.257222101, k0 = 0.9996, lon0 = 173 * Math.PI / 180;
        const e2 = f * (2 - f), ep2 = e2 / (1 - e2), phi = lat * Math.PI / 180, lam = lon * Math.PI / 180;
        const N = a / Math.sqrt(1 - e2 * Math.sin(phi) ** 2), T = Math.tan(phi) ** 2;
        const C = ep2 * Math.cos(phi) ** 2, A = (lam - lon0) * Math.cos(phi);
        const M = a * ((1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256) * phi
          - (3 * e2 / 8 + 3 * e2 ** 2 / 32 + 45 * e2 ** 3 / 1024) * Math.sin(2 * phi)
          + (15 * e2 ** 2 / 256 + 45 * e2 ** 3 / 1024) * Math.sin(4 * phi) - (35 * e2 ** 3 / 3072) * Math.sin(6 * phi));
        const E = 1600000 + k0 * N * (A + (1 - T + C) * A ** 3 / 6 + (5 - 18 * T + T * T + 72 * C - 58 * ep2) * A ** 5 / 120);
        const Nn = 10000000 + k0 * (M + N * Math.tan(phi) * (A * A / 2 + (5 - T + 9 * C + 4 * C * C) * A ** 4 / 24
          + (61 - 58 * T + T * T + 600 * C - 330 * ep2) * A ** 6 / 720));
        return [E, Nn];
      },
      /** Decode a greyscale image (URL or data URI) into a Uint8Array of its red channel. */
      decodeGray(src, w, h) {
        return new Promise((resolve, reject) => {
          const img = new Image();
          img.onload = () => {
            const c = document.createElement("canvas"); c.width = w; c.height = h;
            const ctx = c.getContext("2d", { willReadFrequently: true }); ctx.drawImage(img, 0, 0);
            const px = ctx.getImageData(0, 0, w, h).data, out = new Uint8Array(w * h);
            for (let i = 0; i < out.length; i++) out[i] = px[i * 4];
            resolve(out);
          };
          img.onerror = () => reject(new Error(`Could not decode ${String(src).slice(0, 60)}`));
          img.src = src;
        });
      },
      b64bytes(txt) {
        const s = atob(txt.trim()), b = new Uint8Array(s.length);
        for (let i = 0; i < s.length; i++) b[i] = s.charCodeAt(i);
        return b;
      },
    },

    /** Small modal dialog: RF.dialog({title, body: Node, actions: [{label, primary, onclick}]}) → close(). */
    dialog({ title, body, actions = [], label }) {
      const { el } = RF.util;
      const opener = document.activeElement;
      const box = el("div", { class: "rf-dialog-box", role: "dialog", "aria-modal": "true", "aria-label": label || title });
      if (title) box.append(el("h2", { text: title }));
      if (body) box.append(body);
      const close = () => { wrap.remove(); document.removeEventListener("keydown", onKey); opener?.focus?.(); };
      const row = el("div", { class: "rf-row", style: "justify-content:flex-end" });
      actions.forEach((a) => row.append(el("button", { type: "button", class: `rf-btn${a.primary ? " rf-btn--primary" : ""}`, onclick: () => { a.onclick?.(); if (a.close !== false) close(); } }, a.label)));
      if (actions.length) box.append(row);
      const wrap = el("div", { class: "rf-dialog", onclick: (e) => { if (e.target === wrap) close(); } }, box);
      const onKey = (e) => { if (e.key === "Escape") close(); };
      document.addEventListener("keydown", onKey);
      document.body.append(wrap);
      (box.querySelector(".rf-btn--primary") || box.querySelector("button, a"))?.focus();
      return close;
    },
  });

  // ---------- app frame and routing ----------
  function buildNav() {
    const nav = document.getElementById("rf-nav");
    NAV.forEach((p) => {
      const a = RF.util.el("a", { href: `#${p.id}`, dataset: { page: p.id }, html: `${icon(p.icon)}<span>${p.title}</span>` });
      a.addEventListener("click", (e) => { e.preventDefault(); RF.go(p.id); });
      nav.append(a);
    });
    document.getElementById("rf-brand").innerHTML = `${icon("logo")}<span>River Finder</span>`;
  }
  const routeFromHash = () => { const id = location.hash.slice(1); return NAV.some((p) => p.id === id) ? id : "map"; };

  /** Called once by core/start.js after every page module has registered. */
  RF._start = () => {
    buildNav();
    window.addEventListener("popstate", () => {
      if (RF.detail?.isOpen()) { RF.detail.close({ fromHistory: true }); return; }
      const id = routeFromHash(); if (id !== RF.state.page) RF.go(id, {}, { fromHistory: true });
    });
    window.addEventListener("hashchange", () => { const id = routeFromHash(); if (id !== RF.state.page) RF.go(id, {}, { fromHistory: true }); });
    const m = RF.store.get("model");
    RF.state.model = RF.models.get(m) ? m : RF.models.default;
    RF.state.runParams = RF.runs.defaults();
    RF.go(routeFromHash(), {}, { replace: true });
    RF.emit("ready", {});
  };
})();
