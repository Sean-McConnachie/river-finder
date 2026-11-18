/* RF.chart: small SVG charts in the app's tokens. Owner: core.
   RF.chart.tracks({x, xLabel, tracks: [{label, unit, y, color, area, fmt, markers}], ...}) draws one
   graph with stacked tracks that share the x axis and one crosshair (each track has its own y scale:
   several measures along a river without a misleading dual axis). Redraws on resize; touch friendly. */
(() => {
  "use strict";
  const RF = window.RF, { el } = RF.util;
  const NS = "http://www.w3.org/2000/svg";
  const s = (tag, attrs = {}, ...kids) => {
    const e = document.createElementNS(NS, tag);
    Object.entries(attrs).forEach(([k, v]) => { if (v !== null && v !== undefined) e.setAttribute(k, v); });
    kids.forEach((k) => k !== null && k !== undefined && e.append(k.nodeType ? k : document.createTextNode(String(k))));
    return e;
  };

  /** About n round tick values covering [lo, hi]. */
  function ticks(lo, hi, n = 4) {
    if (!(hi > lo)) return [lo];
    const raw = (hi - lo) / n, mag = 10 ** Math.floor(Math.log10(raw)), f = raw / mag;
    const step = (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * mag, out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(+v.toPrecision(12));
    return out;
  }
  const nearest = (xs, v) => { let lo = 0, hi = xs.length - 1; while (hi - lo > 1) { const m = (lo + hi) >> 1; if (xs[m] < v) lo = m; else hi = m; } return Math.abs(xs[lo] - v) <= Math.abs(xs[hi] - v) ? lo : hi; };

  function tracks(opts) {
    const { x, tracks: T, xLabel = "", xFmt = (v) => v.toFixed(1), label = "Chart" } = opts;
    const wrap = el("figure", { class: "rf-chart", role: "img", "aria-label": label });
    const tip = el("div", { class: "rf-chart-tip", hidden: true });
    const host = el("div", { class: "rf-chart-host" });
    wrap.append(host, tip);
    const M = { l: 52, r: 14, t: 22, gap: 30, b: 34 };

    function draw() {
      const W = Math.max(280, host.clientWidth || 600);
      const heights = T.map((t) => t.height || 120);
      const H = M.t + heights.reduce((a, b) => a + b, 0) + M.gap * (T.length - 1) + M.b;
      const x0 = x[0], x1 = x[x.length - 1] > x0 ? x[x.length - 1] : x0 + 1;
      const sx = (v) => M.l + (v - x0) / (x1 - x0) * (W - M.l - M.r);
      const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, class: "rf-chart-svg" });
      let top = M.t;
      const frames = [];
      T.forEach((t, k) => {
        const h = heights[k], ys = t.y.filter((v) => v !== null && isFinite(v));
        let lo = Math.min(...ys), hi = Math.max(...ys);
        if (t.zero) lo = Math.min(0, lo);
        if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
        const pad = (hi - lo) * 0.08; lo -= t.zero && lo === 0 ? 0 : pad; hi += pad;
        const tk = ticks(lo, hi, h > 100 ? 4 : 3);
        const sy = (v) => top + h - (v - lo) / (hi - lo) * h;
        svg.append(s("text", { x: M.l, y: top - 8, class: "rf-chart-title" }, `${t.label}${t.unit ? ` (${t.unit})` : ""}`));
        tk.forEach((v) => {
          if (v < lo || v > hi) return;
          svg.append(s("line", { x1: M.l, x2: W - M.r, y1: sy(v), y2: sy(v), class: "rf-chart-grid" }));
          svg.append(s("text", { x: M.l - 6, y: sy(v) + 4, class: "rf-chart-ylab", "text-anchor": "end" }, (t.fmt || ((q) => String(q)))(v)));
        });
        const pts = x.map((xv, i) => (t.y[i] === null || !isFinite(t.y[i]) ? null : [sx(xv), sy(t.y[i])])).filter(Boolean);
        if (t.area && pts.length > 1) {
          svg.append(s("path", { d: `M${pts[0][0]},${top + h}L${pts.map((p) => p.join(",")).join("L")}L${pts[pts.length - 1][0]},${top + h}Z`, fill: t.color, "fill-opacity": 0.12, stroke: "none" }));
        }
        svg.append(s("path", { d: `M${pts.map((p) => p.join(",")).join("L")}`, fill: "none", stroke: t.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
        (t.markers || []).forEach((mk) => {
          const px = sx(mk.x), i = nearest(x, mk.x), py = sy(t.y[i]);
          svg.append(s("path", { d: `M${px},${py - 9}l-5,-9h10z`, class: "rf-chart-marker" }, s("title", {}, mk.label)));
        });
        svg.append(s("line", { x1: M.l, x2: W - M.r, y1: top + h, y2: top + h, class: "rf-chart-axis" }));
        frames.push({ top, h, sy, t });
        top += h + M.gap;
      });
      const base = top - M.gap;
      ticks(x0, x1, Math.max(3, Math.floor((W - M.l) / 90))).forEach((v) => {
        if (v < x0 || v > x1) return;
        svg.append(s("text", { x: sx(v), y: base + 16, class: "rf-chart-xlab", "text-anchor": "middle" }, xFmt(v)));
      });
      if (xLabel) svg.append(s("text", { x: W - M.r, y: base + 30, class: "rf-chart-xlab", "text-anchor": "end" }, xLabel));
      // crosshair
      const cross = s("line", { y1: M.t - 4, y2: base, class: "rf-chart-cross", visibility: "hidden" });
      const dots = frames.map((f) => s("circle", { r: 4, fill: f.t.color, stroke: "var(--surface)", "stroke-width": 2, visibility: "hidden" }));
      const hit = s("rect", { x: M.l, y: 0, width: W - M.l - M.r, height: H, fill: "transparent", class: "rf-chart-hit" });
      svg.append(cross, ...dots, hit);
      const move = (ev) => {
        const r = svg.getBoundingClientRect(), px = (ev.clientX - r.left) * (W / r.width);
        const xv = x0 + (px - M.l) / (W - M.l - M.r) * (x1 - x0), i = nearest(x, xv), cx = sx(x[i]);
        cross.setAttribute("x1", cx); cross.setAttribute("x2", cx); cross.setAttribute("visibility", "visible");
        const lines = [`${xLabel ? `${xFmt(x[i])} ${xLabel.replace(/^.*\((.*)\)$/, "$1")}` : xFmt(x[i])}`];
        frames.forEach((f, k) => {
          const v = f.t.y[i];
          if (v === null || !isFinite(v)) { dots[k].setAttribute("visibility", "hidden"); return; }
          dots[k].setAttribute("cx", cx); dots[k].setAttribute("cy", f.sy(v)); dots[k].setAttribute("visibility", "visible");
          lines.push(`${f.t.label}: ${(f.t.tipFmt || f.t.fmt || String)(v)}${f.t.unit ? ` ${f.t.unit}` : ""}`);
        });
        tip.replaceChildren(...lines.map((l, k) => el("div", { class: k ? "" : "rf-chart-tip-x", text: l })));
        tip.hidden = false;
        const left = (cx / W) * r.width;
        tip.style.left = `${Math.min(Math.max(left + 12, 0), r.width - tip.offsetWidth - 4)}px`;
        if (left + 12 + tip.offsetWidth > r.width) tip.style.left = `${Math.max(left - tip.offsetWidth - 12, 0)}px`;
      };
      const leave = () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); dots.forEach((d) => d.setAttribute("visibility", "hidden")); };
      hit.addEventListener("pointermove", move);
      hit.addEventListener("pointerdown", move);
      hit.addEventListener("pointerleave", leave);
      host.replaceChildren(svg);
    }
    const ro = new ResizeObserver(RF.util.debounce(draw, 60));
    ro.observe(host);
    requestAnimationFrame(draw);
    return wrap;
  }

  /** An empty chart frame with a message (placeholders such as the flow-rate chart). */
  function placeholder(title, message) {
    return el("figure", { class: "rf-chart rf-chart--empty" },
      el("div", { class: "rf-chart-empty-title", text: title }), el("p", { class: "rf-note", text: message }));
  }

  RF.chart = { tracks, placeholder, ticks };
})();
