/* RF.detail: the full-screen detail view used for anything selectable (rivers, gauges, waterfalls,
   roads). It sits over the current page without touching it, so closing it (×, Esc, browser back)
   leaves the map view and every scroll position exactly as they were. Owner: core.
   Renderers are registered per feature kind (the Map page registers river, waterfall, gauge, ...). */
(() => {
  "use strict";
  const RF = window.RF, { el, icon, esc } = RF.util;
  const renderers = {};
  let current = null; // {wrap, opener, pushed, feature}

  function onKey(e) { if (e.key === "Escape" && current) { e.preventDefault(); close(); } }

  function fallback(f) {
    const dl = el("dl", { class: "rf-kv" });
    Object.entries(f).forEach(([k, v]) => {
      if (v === null || v === undefined || typeof v === "object") return;
      dl.append(el("dt", { text: k }), el("dd", { text: String(v) }));
    });
    return { title: f.name || f.kind || "Details", eyebrow: f.kind, body: dl };
  }

  /** Open the detail view for a feature reference. Returns a promise that resolves when rendered. */
  async function open(feature, opts = {}) {
    if (!feature) return;
    if (current) remove();
    const opener = opts.opener || document.activeElement;
    const titles = el("div", { class: "rf-detail-titles" },
      el("span", { class: "rf-eyebrow", "data-role": "eyebrow" }), el("h2", { "data-role": "title", id: "rf-detail-title", text: feature.name || "Loading…" }),
      el("span", { class: "rf-muted", "data-role": "subtitle", style: "font-size:var(--fs-sm)" }));
    const closeBtn = el("button", { type: "button", class: "rf-btn rf-btn--quiet rf-iconbtn", "aria-label": "Close details", html: icon("close") });
    closeBtn.addEventListener("click", () => close());
    const actions = el("div", { class: "rf-row", "data-role": "actions" });
    const head = el("header", { class: "rf-detail-head" }, titles, actions, closeBtn);
    const doc = el("div", { class: "rf-doc" }, el("p", { class: "rf-muted", text: "Loading…" }));
    const body = el("div", { class: "rf-detail-body" }, doc);
    const wrap = el("div", { class: "rf-detail", role: "dialog", "aria-modal": "true", "aria-labelledby": "rf-detail-title" }, head, body);
    document.body.append(wrap);
    let pushed = false;
    if (!opts.noHistory) { try { history.pushState({ rfDetail: true }, "", location.hash || "#"); pushed = true; } catch (e) { pushed = false; } }
    current = { wrap, opener, pushed, feature };
    document.addEventListener("keydown", onKey);
    closeBtn.focus({ preventScroll: true });
    RF.select(feature);
    try {
      const render = renderers[feature.kind] || fallback;
      const out = (await render(feature, { actions, close })) || {};
      if (!current || current.wrap !== wrap) return; // closed while rendering
      head.querySelector('[data-role="eyebrow"]').textContent = out.eyebrow || "";
      head.querySelector('[data-role="title"]').textContent = out.title || feature.name || "Details";
      head.querySelector('[data-role="subtitle"]').textContent = out.subtitle || "";
      doc.replaceChildren(...[].concat(out.body || []));
    } catch (e) {
      console.error(e);
      doc.replaceChildren(el("p", { html: `Could not show these details: ${esc(e.message)}` }));
    }
  }

  function remove() {
    if (!current) return;
    const { wrap, opener } = current;
    wrap.remove(); document.removeEventListener("keydown", onKey);
    current = null;
    try { opener?.focus?.({ preventScroll: true }); } catch (e) { /* gone */ }
  }

  /** Close the detail view (pops the history entry it pushed, so Back and × behave the same). */
  function close(opts = {}) {
    if (!current) return;
    if (current.pushed && !opts.fromHistory) { current.pushed = false; try { history.back(); } catch (e) { /* ignore */ } }
    remove();
    RF.emit("detail-close", {});
  }

  RF.detail = {
    /** render(feature, {actions, close}) → {title, subtitle, eyebrow, body: Node | Node[]} (may be async). */
    register(kind, render) { renderers[kind] = render; },
    open, close,
    isOpen: () => !!current,
    current: () => current?.feature || null,
  };
})();
