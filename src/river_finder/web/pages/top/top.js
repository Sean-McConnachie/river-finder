/* Top rivers page (#top): the ranked runs for the current model, in one region or across every region,
   with the filters and sorts a paddler uses. The bridge between the map browser and the model interface.
   Owned by the Top-rivers agent: see docs/ui/top-rivers.md and beads issue rf-aq6.5.
   Only edit files under src/river_finder/web/pages/top/. */
(() => {
  "use strict";
  const RF = window.RF, { el, fmt, icon } = RF.util;
  const PAGE = 20;       // rows shown first, then added each time the end of the list comes into view
  const ALL = "*";       // region picker value: rank across every region in RF.data.regions()
  const PARALLEL = 3;    // regions fetched at a time for "All regions"
  const STORE = "top:filters";
  const ROMAN = { 2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI" };
  const STATUS = [["candidate", "New leads"], ["listed", "In guides"], ["heldout", "Test rivers"]];
  const STATUS_KEYS = STATUS.map(([k]) => k);
  const PILL = { candidate: ["rf-pill--ok", "New lead"], listed: ["", "In guides"], heldout: ["rf-pill--warn", "Test river"] };
  const SORTS = [["score", "Score (best first)"], ["grad", "Gradient (steepest)"], ["maf", "Flood flow (largest)"], ["len", "Length (longest)"]];
  const SORT_KEY = { grad: (r) => r.grad, maf: (r) => r.maf, len: (r) => r.len };
  const OPT = { grade: [2, 3, 4, 5, 6], grad: [10, 20, 30, 40, 50, 60, 80, 100], len: [1, 2, 3, 5, 10, 20],
    maf: [2, 5, 10, 20, 50, 100, 200, 500], road: [100, 250, 500, 1000, 2000, 5000] };
  const FALLS = [["any", "Any"], ["none", "None mapped"], ["some", "At least one"]];
  const DEFAULTS = () => ({ scope: null, sort: "score", status: STATUS_KEYS.slice(), gMin: null, gMax: null,
    lenMin: null, lenMax: null, mafMin: null, mafMax: null, gradMin: null, road: null, falls: "any" });

  let root = null, list = null, regionSel = null, newOnly = null, toggle = null, badge = null, panel = null, resetBtn = null;
  let progress = null, summary = null, modelLine = null;
  let F = DEFAULTS(), scope = null;
  let pool = [];          // items of the regions loaded so far: scope order, then the run finder's order
  let hasAcc = false, hasGrade = false, fallback = false, loading = false, stale = false, errors = [];
  let loadToken = 0, listToken = 0, io = null, rewind = false, frame = 0, view = null;
  const cache = new Map();          // region id → {model, params, items, fallback}
  const secIndex = new WeakMap();   // region → Map(reach → known sections on it)
  const ctl = {};                   // filter key → its control, for syncing after a reset

  // ---------- preferences (per browser) ----------
  function loadPrefs() {
    const d = DEFAULTS();
    try {
      const s = RF.store.get(STORE);
      if (!s || typeof s !== "object") return d;
      const pick = (v, opts) => (opts.includes(v) ? v : null);
      const status = Array.isArray(s.status) ? STATUS_KEYS.filter((k) => s.status.includes(k)) : [];
      return { scope: s.scope === ALL ? ALL : null, sort: SORTS.some(([k]) => k === s.sort) ? s.sort : d.sort,
        status: status.length ? status : d.status, gMin: pick(s.gMin, OPT.grade), gMax: pick(s.gMax, OPT.grade),
        lenMin: pick(s.lenMin, OPT.len), lenMax: pick(s.lenMax, OPT.len), mafMin: pick(s.mafMin, OPT.maf), mafMax: pick(s.mafMax, OPT.maf),
        gradMin: pick(s.gradMin, OPT.grad), road: pick(s.road, OPT.road), falls: FALLS.some(([k]) => k === s.falls) ? s.falls : d.falls };
    } catch (e) { return d; }
  }
  function savePrefs() { try { RF.store.set(STORE, F); } catch (e) { /* storage unavailable: filters last this visit only */ } }

  /** Active filters (sort and region are not filters). */
  function activeCount() {
    return [F.status.length !== STATUS_KEYS.length, F.gMin != null || F.gMax != null, F.lenMin != null || F.lenMax != null,
      F.mafMin != null || F.mafMax != null, F.gradMin != null, F.road != null, F.falls !== "any"].filter(Boolean).length;
  }

  // ---------- known sections: grade and guide link of a listed run ----------
  const gradeBin = (sec) => { const g = sec.gnum ?? sec.gmin ?? sec.gmax; return g == null ? 0 : Math.max(2, Math.min(6, Math.floor(g + 1e-6))); };
  /** The guide page among a section's links (not the API record), http(s) only. */
  function guideLink(sec) {
    const ok = (sec.links || []).filter((l) => l && /^https?:\/\//i.test(l.u || ""));
    return ok.find((l) => !/\/\/api\.|\.json(\?|$)/i.test(l.u)) || ok[0] || null;
  }
  function sectionsByReach(R) {
    let m = secIndex.get(R);
    if (!m) {
      m = new Map();
      (R.sections || []).forEach((s) => (s.m || []).forEach((reach) => { const a = m.get(reach); if (a) a.push(s); else m.set(reach, [s]); }));
      secIndex.set(R, m);
    }
    return m;
  }
  /** Rows of one region: the run plus what the list shows about it. The section a run is matched to is the
   *  graded one it overlaps most (else the one it overlaps most); the guide link is the first one with a link. */
  function itemsFor(R, runs) {
    const idx = sectionsByReach(R), title = R.title || R.id;
    return runs.map((r, k) => {
      const hits = new Map();
      r.members.forEach((m) => (idx.get(m) || []).forEach((s) => hits.set(s, (hits.get(s) || 0) + 1)));
      const secs = [...hits].sort((a, b) => (gradeBin(b[0]) ? 1 : 0) - (gradeBin(a[0]) ? 1 : 0) || b[1] - a[1]).map(([s]) => s);
      const sec = secs[0] || null;
      return { r, k, region: title, sec, bin: sec ? gradeBin(sec) : 0, link: secs.map(guideLink).find(Boolean) || null };
    });
  }

  // ---------- loading: one region, or all of them a few at a time ----------
  const yieldMain = () => (globalThis.scheduler?.yield ? globalThis.scheduler.yield() : new Promise((r) => setTimeout(r, 0)));

  /** The cached rows of a region, if they are for the current model and run-finder settings. */
  const fresh = (id) => { const c = cache.get(id); return c && c.model === RF.state.model && c.params === RF.state.runParams ? c : null; };

  async function regionItems(id) {
    const model = RF.state.model, params = RF.state.runParams, hit = fresh(id);
    if (hit) return hit;
    const R = await RF.data.region(id);
    const sc = model ? await RF.data.scores(id, model) : null;
    await yieldMain();   // the region was just decoded: let the page paint before the run finder runs
    const out = { model, params, items: itemsFor(R, RF.runs.find(R, { model: sc })), fallback: !!sc?.fallback };
    if (model === RF.state.model && params === RF.state.runParams) cache.set(id, out);
    return out;
  }

  async function load() {
    const my = ++loadToken, regions = RF.data.regions(), ids = scope === ALL ? regions.map((r) => r.id) : [scope];
    const title = (id) => regions.find((r) => r.id === id)?.title || id;
    errors = [];
    if (ids.every(fresh)) {   // all in memory (a model or region switched back): no loading state, no flash
      const parts = ids.map(fresh);
      pool = parts.flatMap((p) => p.items); fallback = parts.some((p) => p.fallback); loading = false;
      setProgress(ids.length, ids.length); update();
      return;
    }
    const parts = new Array(ids.length);
    let next = 0, done = 0;
    pool = []; fallback = false; loading = true;
    setProgress(0, ids.length);
    update();
    const worker = async () => {
      while (next < ids.length) {
        const i = next++;
        try { parts[i] = await regionItems(ids[i]); } catch (e) { if (my === loadToken) errors.push([title(ids[i]), e.message]); }
        if (my !== loadToken) return;
        done++;
        pool = parts.flatMap((p) => p?.items || []);
        fallback = parts.some((p) => p?.fallback);
        setProgress(done, ids.length);
        schedule();
        await yieldMain();
        if (my !== loadToken) return;
      }
    };
    await Promise.all(Array.from({ length: Math.min(PARALLEL, ids.length) }, worker));
    if (my !== loadToken) return;
    loading = false;
    setProgress(done, ids.length);
    update({ keep: true });
  }

  /** Reload now, or on the next visit when the page is hidden (model or run-finder changes elsewhere). */
  function reload() { if (root.hidden) stale = true; else load(); }

  function setProgress(done, total) {
    const lines = [];
    if (scope === ALL && (loading || errors.length)) {
      lines.push(el("span", { text: loading ? `Loaded ${done} of ${total} regions. The ranking updates as each one arrives.` : `Ranked across ${total - errors.length} of ${total} regions.` }));
      if (loading) lines.push(el("progress", { max: total, value: done, "aria-hidden": "true" }));
    }
    if (errors.length) lines.push(el("span", { class: "top-err", text: `Could not load ${errors.map(([t, m]) => `${t} (${m})`).join("; ")}.` }));
    progress.replaceChildren(...lines);
    progress.hidden = !lines.length;
  }

  /** Re-rank once per frame while regions arrive (keeps the rows already shown and the scroll position). */
  function schedule() { if (!frame) frame = requestAnimationFrame(() => { frame = 0; update({ keep: true }); }); }

  // ---------- filtering and sorting ----------
  function passes(it) {
    const r = it.r;
    if (!F.status.includes(r.status)) return false;
    if ((F.gMin != null || F.gMax != null) && !(it.bin && it.bin >= (F.gMin ?? 2) && it.bin <= (F.gMax ?? 6))) return false;
    if (F.lenMin != null && !(r.len >= F.lenMin)) return false;
    if (F.lenMax != null && !(r.len <= F.lenMax)) return false;
    if (F.mafMin != null && !(r.maf >= F.mafMin)) return false;
    if (F.mafMax != null && !(r.maf <= F.mafMax)) return false;
    if (F.gradMin != null && !(r.grad >= F.gradMin)) return false;
    if (F.road != null && !(r.acc && r.acc.putIn <= F.road)) return false;
    if (F.falls === "none" && !(r.acc && !r.falls)) return false;
    if (F.falls === "some" && !(r.falls > 0)) return false;
    return true;
  }
  function ranked() {
    const items = pool.filter(passes), key = SORT_KEY[F.sort];
    // One region keeps the run finder's own order for "score"; across regions the scores are national percentiles.
    if (key) items.sort((a, b) => key(b.r) - key(a.r) || b.r.score - a.r.score);
    else if (scope === ALL) items.sort((a, b) => b.r.score - a.r.score);
    return items;
  }

  function update({ keep = false } = {}) {
    if (frame) { cancelAnimationFrame(frame); frame = 0; }
    hasAcc = pool.some((it) => it.r.acc);
    hasGrade = pool.some((it) => it.bin);
    syncPanel();
    setModelLine();
    const items = ranked(), n = activeCount();
    const parts = [];
    if (n && pool.length) parts.push(`${fmt.int(items.length)} of ${fmt.int(pool.length)} runs match ${n === 1 ? "the filter" : `${n} filters`}${loading ? " so far" : ""}.`);
    if (F.sort !== "score") parts.push(`Sorted by ${SORTS.find(([k]) => k === F.sort)[1].replace(/ \(.*/, "").toLowerCase()}.`);
    summary.textContent = parts.join(" ");
    summary.hidden = !parts.length;
    draw(items, keep);
  }

  // ---------- the list (paged: PAGE rows, then PAGE more as the end comes into view) ----------
  // Columns: header shown on desktop, header spoken (data-label), cell class. Below 720 px top.css turns
  // each row into a list item; the headers stay in the accessibility tree (visually hidden).
  const COLS = [["#", "Rank", "n top-rank"], ["River", "River", "top-name"], ["km", "km", "n top-km"], ["m/km", "m/km", "n top-grad"],
    ["Flood m³/s", "Flood m³/s", "n top-flood"], ["Road", "Put-in to road", "n top-road"], ["Score", "Score", "n top-score"], ["", "Actions", "top-act"]];
  const TITLES = { "top-road": "Distance from the put-in to the nearest road (OpenStreetMap)", "top-score": "The model's percentile: 100 is most like the known runs",
    "top-flood": "Mean annual flood at the bottom (NIWA), not live flow" };
  /** One cell of column k (explicit role: some browsers drop table semantics once the display changes). */
  const cell = (k, ...kids) => el("td", { class: COLS[k][2], role: "cell", "data-label": COLS[k][1] }, ...kids);
  const head = () => el("thead", { role: "rowgroup" }, el("tr", { role: "row" }, ...COLS.map(([h, label, c]) =>
    el("th", { class: c.startsWith("n") ? `n ${c.slice(2)}` : c, role: "columnheader", scope: "col", title: TITLES[c.replace(/^n /, "")] || null },
      h === label ? h : [h ? el("span", { "aria-hidden": "true", text: h }) : null, el("span", { class: "top-vh", text: label })]))));
  /** "Waima River" and "Waima" are the same river: guides often drop River / Stream. */
  const bare = (s) => String(s || "").toLowerCase().replace(/\b(river|stream|creek)\b/g, "").replace(/\s+/g, " ").trim();
  const sameRiver = (a, b) => { const x = bare(a), y = bare(b); return !!x && !!y && (x.startsWith(y) || y.startsWith(x)); };
  const band = (s) => (s >= 90 ? "hi" : s >= 70 ? "mid" : "lo");
  const road = (m) => (m < 50 ? "< 50 m" : fmt.dist(m));
  const dist = (m) => (m < 1000 ? `${m} m` : `${m / 1000} km`);

  function row(it, i, multi) {
    const r = it.r, [pc, pt] = PILL[r.status] || PILL.candidate;
    const grade = it.sec && (it.sec.grade || it.bin) ? el("span", { class: "rf-pill top-grade", title: `${it.sec.river}${it.sec.section ? `: ${it.sec.section}` : ""}` },
      it.bin ? el("span", { class: "top-gdot", style: `background:var(--grade-${it.bin})`, "aria-hidden": "true" }) : null,
      `Grade ${it.sec.grade || ROMAN[it.bin]}${sameRiver(r.name, it.sec.river) ? "" : ` on ${it.sec.river}`}`) : null;
    const falls = r.falls > 0 ? el("span", { class: "rf-pill top-falls", title: "Mapped waterfalls (OpenStreetMap; coverage is patchy)" },
      `${r.falls} waterfall${r.falls === 1 ? "" : "s"}${r.fallsH ? `, ${r.fallsH} m` : ""}`) : null;
    const name = r.name;
    return el("tr", { role: "row", "data-id": r.id },
      cell(0, String(i + 1)),
      cell(1, el("strong", { text: name }), " ", el("span", { class: `rf-pill ${pc}`, text: pt }), grade ? " " : null, grade, falls ? " " : null, falls,
        multi ? el("span", { class: "top-sub", text: it.region }) : null),
      cell(2, r.len.toFixed(1)), cell(3, fmt.grad(r.grad)), cell(4, fmt.flow(r.maf)), cell(5, r.acc ? road(r.acc.putIn) : "–"),
      cell(6, el("span", { class: `top-dot top-dot--${band(r.score)}`, "aria-hidden": "true" }), r.score.toFixed(0)),
      cell(7, el("div", { class: "rf-row top-acts" },
        el("button", { type: "button", class: "rf-btn", "data-act": "map", "aria-label": `Map: ${name}`, onclick: () => RF.go("map", { select: r }) }, "Map"),
        el("button", { type: "button", class: "rf-btn", "data-act": "detail", "aria-label": `Details: ${name}`, onclick: (e) => RF.detail.open(r, { opener: e.currentTarget }) }, "Details"),
        it.link ? el("a", { class: "rf-btn rf-btn--quiet top-guide", href: it.link.u, target: "_blank", rel: "noopener", "data-act": "guide", title: it.link.t,
          "aria-label": `Guide: ${it.link.t} (opens in a new tab)` }, "Guide", el("span", { html: icon("external"), "aria-hidden": "true" })) : null)));
  }

  /** Stop watching the end of the list (before each re-render, so an old observer never fires). */
  function unwatch() { io?.disconnect(); io = null; }

  /** Which row control has focus, so a re-render can put it back. */
  function focusedKey() {
    const a = document.activeElement;
    if (!a || !list.contains(a) || !a.dataset.act) return null;
    return { id: a.closest("tr")?.dataset.id || null, act: a.dataset.act };
  }

  function emptyState() {
    if (loading && !pool.length) return el("p", { class: "rf-muted", text: scope === ALL ? "Loading the regions…" : "Loading the region…" });
    if (!pool.length && errors.length) return el("div", { class: "rf-empty", text: "Nothing to rank: the data did not load (see above)." });
    if (!pool.length) return el("div", { class: "rf-empty", text: `No runs pass the current run-finder settings in ${scope === ALL ? "these regions" : "this region"}.` });
    return el("div", { class: "rf-empty" }, el("span", { text: "No runs match these filters." }),
      el("button", { type: "button", class: "rf-btn", onclick: resetFilters }, "Reset filters"));
  }

  /** Show items; keep = new data while regions arrive (same rows shown, same scroll), else back to the first PAGE. */
  function draw(items, keep) {
    const my = ++listToken, was = keep && view ? view.shown : 0, focus = focusedKey(), total = items.length;
    unwatch();
    if (!keep) {
      // A new list starts at its top, so the end is not already in view from the old scroll position.
      if (root.hidden) rewind = true;
      else {
        const top = list.getBoundingClientRect().top - root.getBoundingClientRect().top + root.scrollTop - 8;
        if (root.scrollTop > top) root.scrollTop = Math.max(0, top);
      }
    }
    if (!total) { view = null; list.replaceChildren(emptyState()); return; }
    const multi = scope === ALL, tb = el("tbody", { role: "rowgroup" });
    const count = el("p", { class: "rf-note", "aria-live": "polite" });
    const more = el("button", { type: "button", class: "rf-btn", "data-act": "more", onclick: () => {
      const first = next(); if (first) { first.tabIndex = -1; first.focus(); }   // keyboard and screen readers continue at the new rows
    }, onblur: () => watch() });
    const end = el("div", { class: "rf-row top-end" }, count, more);   // sentinel: the end of the list
    const v = (view = { shown: 0 });
    /** (Re)start watching the end: observe() reports at once, so an end still in view loads again. */
    const watch = () => { if (io && my === listToken) { io.unobserve(end); io.observe(end); } };
    /** Append the next n rows (ranks continue); returns the first new row. */
    function next(n = PAGE) {
      if (my !== listToken || v.shown >= total) return null;
      const from = v.shown, frag = document.createDocumentFragment();
      v.shown = Math.min(from + n, total);
      for (let i = from; i < v.shown; i++) frag.append(row(items[i], i, multi));
      tb.append(frag);
      const left = total - v.shown;
      count.textContent = left ? `Showing ${fmt.int(v.shown)} of ${fmt.int(total)} runs` : `Showing all ${fmt.int(total)} run${total === 1 ? "" : "s"}`;
      more.textContent = `Show ${Math.min(PAGE, left)} more`;
      more.hidden = !left;
      if (left) watch(); else unwatch();
      return tb.children[from];
    }
    next(Math.max(PAGE, was));
    list.replaceChildren(el("div", { class: "rf-tablewrap" },
      el("table", { class: `rf-table top-table${hasAcc ? "" : " top-noroad"}`, role: "table" }, head(), tb)), end);
    if (focus) {
      const sel = focus.act === "more" ? '[data-act="more"]' : `tr[data-id="${CSS.escape(focus.id || "")}"] [data-act="${focus.act}"]`;
      const t = list.querySelector(sel); if (t && !t.hidden) t.focus({ preventScroll: true });
    }
    if (v.shown < total && "IntersectionObserver" in window) {
      // Not while "Show more" has focus: a keyboard user who tabs to it loads with it, and moves on to the new rows.
      io = new IntersectionObserver((entries) => {
        if (my === listToken && document.activeElement !== more && entries.some((e) => e.isIntersecting)) next();
      }, { root, rootMargin: "0px 0px 200px 0px" });
      io.observe(end);
    }
  }

  // ---------- controls ----------
  function set(key, value) { F[key] = value; savePrefs(); update(); }
  function setStatus(keys) { F.status = STATUS_KEYS.filter((k) => keys.includes(k)); savePrefs(); syncControls(); update(); }
  function resetFilters() {
    F = Object.assign(DEFAULTS(), { scope: F.scope, sort: F.sort });
    savePrefs(); syncControls(); update();
  }
  function setScope(v) { scope = v; F.scope = v === ALL ? ALL : null; savePrefs(); load(); }

  /** A select over values (null = any). */
  function select(key, label, values, text, any = "Any", id = `top-f-${key}`) {
    const s = el("select", { class: "rf-select", id, "aria-label": label, onchange: () => set(key, s.value === "" ? null : +s.value) },
      el("option", { value: "", text: any }), ...values.map((v) => el("option", { value: String(v), text: text(v) })));
    ctl[key] = s;
    return s;
  }
  const field = (cls, label, forId, ...kids) => el("div", { class: `top-field ${cls}` }, el("label", { class: "top-flabel", for: forId, text: label }), ...kids);
  function range(cls, label, k0, k1, values, text) {
    const id = `top-f-${k0}-l`;
    return el("div", { class: `top-field top-field--range ${cls}`, role: "group", "aria-labelledby": id },
      el("span", { class: "top-flabel", id, text: label }),
      el("div", { class: "top-range" }, select(k0, `${label}: from`, values, text), el("span", { class: "rf-muted", "aria-hidden": "true", text: "to" }),
        select(k1, `${label}: to`, values, text)));
  }

  function syncControls() {
    Object.entries(ctl).forEach(([k, c]) => {
      if (k === "status") c.forEach((box) => { box.checked = F.status.includes(box.value); });
      else c.value = F[k] == null ? "" : String(F[k]);
    });
    newOnly.checked = F.status.length === 1 && F.status[0] === "candidate";
  }

  /** Field visibility (no data, no field, unless it is set), the active count and Reset. */
  function syncPanel() {
    const n = activeCount();
    panel.querySelector(".top-f-grade").hidden = !(hasGrade || F.gMin != null || F.gMax != null);
    panel.querySelector(".top-f-road").hidden = !(hasAcc || F.road != null);
    panel.querySelector(".top-f-falls").hidden = !(hasAcc || F.falls !== "any");
    badge.textContent = String(n);
    badge.hidden = !n;
    toggle.setAttribute("aria-label", n ? `Filters, ${n} active` : "Filters");
    resetBtn.disabled = !n;
  }

  function setModelLine() {
    const m = RF.models.get(RF.state.model);
    const name = !RF.state.model ? "The run finder (this build has no models)"
      : fallback ? `${m?.title || RF.state.model}, with the pooled regression where this build has no per-model scores` : m?.title || RF.state.model;
    modelLine.replaceChildren(
      el("p", {}, el("span", { class: "rf-muted", text: "Ranked by " }), el("strong", { text: name }), el("br"),
        el("span", { class: "rf-note", text: RF.state.model ? "Score is the model's percentile: 100 is most like the known runs." : "Score is the run finder's desirability, 0 to 100." })),
      el("a", { class: "rf-btn", href: "#models", onclick: (e) => { e.preventDefault(); RF.go("models"); } },
        el("span", { html: icon("model"), "aria-hidden": "true" }), "Why rivers rank where they do"));
  }

  function controls(regions) {
    regionSel = el("select", { class: "rf-select", id: "top-region", onchange: () => setScope(regionSel.value) });
    if (regions.length > 1) regionSel.append(el("option", { value: ALL, text: `All regions (${regions.length})` }));
    const groups = [...new Set(regions.map((r) => r.group))], opt = (r) => el("option", { value: r.id, text: r.title });
    if (groups.length > 1) groups.forEach((g) => regionSel.append(el("optgroup", { label: g }, ...regions.filter((r) => r.group === g).map(opt))));
    else regions.forEach((r) => regionSel.append(opt(r)));
    regionSel.value = scope;

    newOnly = el("input", { type: "checkbox", id: "top-newonly", onchange: () => setStatus(newOnly.checked ? ["candidate"] : STATUS_KEYS) });
    badge = el("span", { class: "top-badge", "aria-hidden": "true" });
    toggle = el("button", { type: "button", class: "rf-btn top-ftoggle", "aria-expanded": "false", "aria-controls": "top-panel", onclick: () => {
      const open = !wrap.classList.contains("is-open");
      wrap.classList.toggle("is-open", open); toggle.setAttribute("aria-expanded", String(open));
    } }, "Filters", badge);

    const sort = el("select", { class: "rf-select", id: "top-f-sort", onchange: () => { F.sort = sort.value; savePrefs(); update(); } },
      ...SORTS.map(([k, t]) => el("option", { value: k, text: t })));
    ctl.sort = sort;
    const boxes = STATUS.map(([k]) => el("input", { type: "checkbox", value: k,
      onchange: () => setStatus(boxes.filter((x) => x.checked).map((x) => x.value)) }));
    ctl.status = boxes;
    const falls = el("select", { class: "rf-select", id: "top-f-falls", onchange: () => set("falls", falls.value) },
      ...FALLS.map(([k, t]) => el("option", { value: k, text: t })));
    ctl.falls = falls;
    resetBtn = el("button", { type: "button", class: "rf-btn top-reset", onclick: resetFilters }, "Reset filters");
    const roman = (v) => ROMAN[v];
    panel = el("div", { class: "top-panel", id: "top-panel" },
      field("top-field--wide", "Sort by", "top-f-sort", sort),
      el("div", { class: "top-field top-field--status", role: "group", "aria-labelledby": "top-f-status-l" },
        el("span", { class: "top-flabel", id: "top-f-status-l", text: "Status" }), el("div", { class: "top-chips" }, ...boxes.map((b, i) => el("label", { class: "top-chip" }, b, STATUS[i][1])))),
      range("top-f-grade", "Grade (known rivers)", "gMin", "gMax", OPT.grade, roman),
      range("top-f-len", "Length (km)", "lenMin", "lenMax", OPT.len, String),
      range("top-f-maf", "Flood flow (m³/s)", "mafMin", "mafMax", OPT.maf, String),
      field("top-f-grad", "Gradient (m/km)", "top-f-gradMin", select("gradMin", "Gradient (m/km)", OPT.grad, (v) => `${v} or more`)),
      field("top-f-road", "Put-in to road", "top-f-road", select("road", "Put-in to road", OPT.road, (v) => `Up to ${dist(v)}`, "Any distance")),
      field("top-f-falls", "Mapped waterfalls", "top-f-falls", falls),
      el("div", { class: "top-field top-field--reset" }, resetBtn));
    const wrap = el("div", { class: "top-controls" },
      el("div", { class: "top-bar" },
        el("div", { class: "top-region" }, el("label", { for: "top-region", text: "Region" }), regionSel),
        el("label", { class: "top-check", for: "top-newonly" }, newOnly, "New leads only"),
        toggle),
      panel);
    syncControls();
    return wrap;
  }

  function render() {
    const regions = RF.data.regions();
    F = loadPrefs();
    const header = el("header", { class: "rf-stack" },
      el("span", { class: "rf-eyebrow", text: "Ranking" }),
      el("h1", { text: "Top rivers" }),
      el("p", { class: "rf-note", text: "The best-scoring runs for the current model, in one region or across all of them. Each one opens on the map or in full detail." }),
      (modelLine = el("div", { class: "top-model" })),
      el("p", { class: "top-safety", role: "note" }, el("span", { html: icon("info"), "aria-hidden": "true" }),
        el("span", {}, el("strong", { text: "Before you go: " }), "new leads come from terrain data only. Check access, land ownership, hazards, flows and local knowledge.")));
    if (!regions.length) {
      setModelLine();
      root.replaceChildren(el("div", { class: "rf-doc" }, header,
        el("div", { class: "rf-empty", text: "No regions in this build. Run the pipeline, then river-finder web build." })));
      return false;
    }
    const cur = RF.state.region || RF.data.defaultRegion();
    scope = F.scope === ALL && regions.length > 1 ? ALL : regions.some((r) => r.id === cur) ? cur : regions[0].id;
    progress = el("div", { class: "top-progress", role: "status", hidden: true });
    summary = el("p", { class: "rf-note top-summary", hidden: true });
    list = el("div", { class: "rf-stack" });
    root.replaceChildren(el("div", { class: "rf-doc" }, header,
      el("section", { class: "rf-panel rf-stack top-card", "aria-label": "Ranked runs" }, controls(regions), progress, summary, list)));
    return true;
  }

  RF.page("top", {
    title: "Top rivers",
    mount(el_) {
      root = el_;
      if (render()) load();
      RF.on("model", () => { if (list) reload(); else setModelLine(); });
      RF.on("runs", () => { cache.clear(); if (list) reload(); });
    },
    show() {
      if (stale) { stale = false; load(); }
      else if (rewind) { rewind = false; root.scrollTop = 0; }
    },
    hide() {},
  });
})();
