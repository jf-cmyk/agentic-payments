"""HTML for the protected operator usage dashboard.

The page reads ``/internal/observability/usage`` and renders KPIs, activity
over time, avenues, tickers, users, paid calls, health, and the assessment
with its improvement plan. Charts are plain SVG so the page needs no external
scripts, and every string that came from traffic (user agents, search terms,
tickers) is inserted with ``textContent``.
"""

from __future__ import annotations

import json


_PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Blocksize Usage</title>
  <style>
    :root {
      color-scheme: light;
      --page: #f9f9f7;
      --surface: #fcfcfb;
      --ink: #0b0b0b;
      --ink-2: #52514e;
      --muted: #898781;
      --grid: #e1e0d9;
      --axis: #c3c2b7;
      --ring: rgba(11, 11, 11, 0.10);
      --good-text: #006300;
      --bad-text: #b42323;
      --ch-x402_http: #2a78d6;
      --ch-direct_mcp: #eb6834;
      --ch-claude: #1baf7a;
      --ch-openai: #eda100;
      --ch-cursor: #e87ba4;
      --ch-pay_sh: #008300;
      --ch-smithery: #4a3aa7;
      --ch-glama: #e34948;
      --ch-monitor: #898781;
      --ch-other: #c3c2b7;
      --series: #2a78d6;
      --status-good: #0ca30c;
      --status-warning: #fab219;
      --status-serious: #ec835a;
      --status-critical: #d03b3b;
      --status-neutral: #86b6ef;
    }
    @media (prefers-color-scheme: dark) {
      :root:not([data-theme="light"]) {
        color-scheme: dark;
        --page: #0d0d0d;
        --surface: #1a1a19;
        --ink: #ffffff;
        --ink-2: #c3c2b7;
        --muted: #898781;
        --grid: #2c2c2a;
        --axis: #383835;
        --ring: rgba(255, 255, 255, 0.10);
        --good-text: #0ca30c;
        --bad-text: #e66767;
        --ch-x402_http: #3987e5;
        --ch-direct_mcp: #d95926;
        --ch-claude: #199e70;
        --ch-openai: #c98500;
        --ch-cursor: #d55181;
        --ch-pay_sh: #008300;
        --ch-smithery: #9085e9;
        --ch-glama: #e66767;
        --ch-monitor: #6b6a65;
        --ch-other: #454541;
        --series: #3987e5;
        --status-neutral: #256abf;
      }
    }
    :root[data-theme="dark"] {
      color-scheme: dark;
      --page: #0d0d0d;
      --surface: #1a1a19;
      --ink: #ffffff;
      --ink-2: #c3c2b7;
      --muted: #898781;
      --grid: #2c2c2a;
      --axis: #383835;
      --ring: rgba(255, 255, 255, 0.10);
      --good-text: #0ca30c;
      --bad-text: #e66767;
      --ch-x402_http: #3987e5;
      --ch-direct_mcp: #d95926;
      --ch-claude: #199e70;
      --ch-openai: #c98500;
      --ch-cursor: #d55181;
      --ch-pay_sh: #008300;
      --ch-smithery: #9085e9;
      --ch-glama: #e66767;
      --ch-monitor: #6b6a65;
      --ch-other: #454541;
      --series: #3987e5;
      --status-neutral: #256abf;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--page);
      color: var(--ink);
      font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif;
    }
    main { max-width: 1280px; margin: 0 auto; padding: 20px 16px 64px; }
    header.top {
      display: flex; flex-wrap: wrap; gap: 12px 24px;
      align-items: center; justify-content: space-between; margin-bottom: 16px;
    }
    h1 { font-size: 22px; margin: 0; }
    h2 { font-size: 16px; margin: 0 0 4px; }
    .sub { color: var(--ink-2); margin: 0 0 14px; font-size: 13px; }
    .muted { color: var(--muted); }
    .toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
    .seg { display: inline-flex; border: 1px solid var(--ring); border-radius: 8px; overflow: hidden; }
    .seg button {
      border: 0; background: var(--surface); color: var(--ink-2); padding: 7px 12px;
      font: inherit; cursor: pointer;
    }
    .seg button[aria-pressed="true"] { background: var(--ink); color: var(--page); font-weight: 600; }
    .toolbar a, button.link {
      color: var(--ink-2); text-decoration: none; padding: 7px 10px; border-radius: 8px;
      border: 1px solid var(--ring); background: var(--surface); font: inherit; cursor: pointer;
    }
    .status-line { font-size: 12px; color: var(--muted); }
    .card {
      background: var(--surface); border: 1px solid var(--ring); border-radius: 12px;
      padding: 18px; margin-top: 16px;
    }
    .grid-2 { display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); }
    .grid-2 > .card { margin-top: 0; }
    .row-gap { margin-top: 16px; }
    .kpis { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(max(150px, calc((100% - 36px) / 4)), 1fr)); }
    .kpi { background: var(--surface); border: 1px solid var(--ring); border-radius: 12px; padding: 14px; }
    .kpi .label { color: var(--ink-2); font-size: 12px; }
    .kpi .value { font-size: 26px; font-weight: 650; margin: 2px 0; }
    .kpi .delta { font-size: 12px; font-variant-numeric: tabular-nums; }
    .kpi .note { font-size: 12px; color: var(--muted); margin-top: 2px; }
    .up-good, .down-good { color: var(--good-text); }
    .up-bad, .down-bad { color: var(--bad-text); }
    .legend { display: flex; flex-wrap: wrap; gap: 6px 14px; margin: 6px 0 10px; font-size: 12px; color: var(--ink-2); }
    .legend span { display: inline-flex; align-items: center; gap: 6px; }
    .key { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
    svg text { fill: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
    svg .grid { stroke: var(--grid); stroke-width: 1; }
    svg .axis { stroke: var(--axis); stroke-width: 1; }
    svg .hit { fill: transparent; cursor: default; }
    svg .hit:hover, svg .hit:focus { fill: var(--ring); outline: none; }
    .chart { width: 100%; height: auto; display: block; }
    #tip {
      position: fixed; pointer-events: none; z-index: 10; display: none;
      background: var(--surface); color: var(--ink); border: 1px solid var(--ring);
      border-radius: 8px; padding: 8px 10px; font-size: 12px; box-shadow: 0 4px 16px rgba(0,0,0,.12);
      min-width: 140px; max-width: 280px;
    }
    #tip .t { color: var(--ink-2); margin-bottom: 4px; }
    #tip .r { display: flex; align-items: center; gap: 6px; justify-content: space-between; }
    #tip .r b { font-variant-numeric: tabular-nums; }
    #tip .stroke { width: 12px; height: 2px; display: inline-block; margin-right: 4px; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--grid); vertical-align: middle; }
    th { color: var(--ink-2); font-weight: 600; font-size: 12px; }
    td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
    .table-wrap { overflow-x: auto; }
    .bar-cell { width: 40%; min-width: 120px; }
    .hbar { display: flex; height: 12px; gap: 2px; }
    .hbar i { display: block; height: 100%; border-radius: 0 4px 4px 0; min-width: 0; }
    .hbar i + i { border-radius: 0 4px 4px 0; }
    .hbar i:not(:last-child) { border-radius: 0; }
    .chip {
      display: inline-flex; align-items: center; gap: 5px; font-size: 11px; font-weight: 700;
      padding: 2px 8px; border-radius: 999px; border: 1px solid var(--ring); color: var(--ink);
    }
    .chip .dot { width: 8px; height: 8px; border-radius: 50%; }
    .issues { display: grid; gap: 10px; }
    .issue { border: 1px solid var(--ring); border-radius: 10px; padding: 12px 14px; }
    .issue h3 { font-size: 14px; margin: 6px 0 4px; }
    .issue p { margin: 4px 0; color: var(--ink-2); }
    .issue p b { color: var(--ink); }
    .plan { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }
    .plan ol { margin: 0; padding-left: 18px; }
    .plan li { margin: 0 0 10px; }
    .plan li b { display: block; }
    .plan li span { color: var(--ink-2); font-size: 13px; }
    .stats { display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); margin-bottom: 12px; }
    .stat { border: 1px solid var(--ring); border-radius: 10px; padding: 10px 12px; }
    .stat .v { font-size: 20px; font-weight: 650; }
    .stat .l { font-size: 12px; color: var(--ink-2); }
    .flag { font-size: 11px; padding: 1px 6px; border-radius: 4px; border: 1px solid var(--ring); color: var(--ink-2); margin-right: 4px; }
    .ua { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; word-break: break-all; }
    .loading { opacity: .5; transition: opacity .2s; }
    .empty { color: var(--muted); padding: 12px 0; }
    details summary { cursor: pointer; color: var(--ink-2); }
    dl.defs { display: grid; grid-template-columns: max-content 1fr; gap: 6px 14px; font-size: 13px; }
    dl.defs dt { font-weight: 600; }
    dl.defs dd { margin: 0; color: var(--ink-2); }
    @media (max-width: 640px) { dl.defs { grid-template-columns: 1fr; } .bar-cell { min-width: 80px; } }
  </style>
</head>
<body>
<main id="app">
  <header class="top">
    <div>
      <h1>Blocksize usage</h1>
      <div class="status-line" id="status-line">Loading…</div>
    </div>
    <div class="toolbar">
      <div class="seg" role="group" aria-label="Time window" id="window-seg">
        <button type="button" data-days="7">7 days</button>
        <button type="button" data-days="30">30 days</button>
        <button type="button" data-days="90">90 days</button>
      </div>
      <button type="button" class="link" id="refresh">Refresh</button>
      <a href="__DEEP_DIVE_PATH__">Deep dive</a>
      <a href="__LOGOUT_PATH__">Log out</a>
    </div>
  </header>

  <section class="kpis" id="kpis" aria-label="Key metrics"></section>
  <p class="sub row-gap" id="retention-note" hidden></p>

  <section class="card" aria-labelledby="h-assess">
    <h2 id="h-assess">Assessment</h2>
    <p class="sub">What the numbers in this window say, ranked by severity, each with the fix.</p>
    <div class="issues" id="issues"></div>
  </section>

  <section class="card" aria-labelledby="h-activity">
    <h2 id="h-activity">Activity over time</h2>
    <p class="sub" id="activity-sub">Calls per day, stacked by avenue. Monitors are shown in gray so real demand stays readable.</p>
    <div class="legend" id="activity-legend"></div>
    <div id="activity-chart"></div>
  </section>

  <div class="grid-2 row-gap">
    <section class="card" aria-labelledby="h-dau">
      <h2 id="h-dau">Unique clients per day</h2>
      <p class="sub">Distinct client hashes on the API and MCP, monitors excluded.</p>
      <div id="users-chart"></div>
    </section>
    <section class="card" aria-labelledby="h-paidday">
      <h2 id="h-paidday">Paid calls per day</h2>
      <p class="sub">Delivered live-data calls backed by a settled x402 payment.</p>
      <div id="paid-chart"></div>
    </section>
  </div>

  <section class="card" aria-labelledby="h-avenues">
    <h2 id="h-avenues">Calls by avenue</h2>
    <p class="sub" id="avenue-sub">Which door each call came through.</p>
    <div class="table-wrap"><table id="avenue-table"></table></div>
  </section>

  <section class="card" aria-labelledby="h-tickers">
    <h2 id="h-tickers">Tickers called</h2>
    <p class="sub" id="ticker-sub">Live-data requests per ticker, excluding monitors, with monitor volume in gray.</p>
    <div class="legend">
      <span><i class="key" style="background:var(--series)"></i>Calls excl. monitors</span>
      <span><i class="key" style="background:var(--ch-monitor)"></i>Monitor calls</span>
    </div>
    <div class="table-wrap"><table id="ticker-table"></table></div>
  </section>

  <div class="grid-2 row-gap">
    <section class="card" aria-labelledby="h-users">
      <h2 id="h-users">Users</h2>
      <p class="sub">Reach (client hashes) next to the people and wallets we can actually verify.</p>
      <div class="stats" id="user-stats"></div>
      <h3 class="sub" style="margin:8px 0 6px">Traffic concentration: busiest clients</h3>
      <div class="table-wrap"><table id="client-table"></table></div>
    </section>
    <section class="card" aria-labelledby="h-search">
      <h2 id="h-search">What agents look for</h2>
      <p class="sub">Search terms and MCP tools used in this window.</p>
      <div class="grid-2">
        <div><div class="table-wrap"><table id="search-table"></table></div></div>
        <div><div class="table-wrap"><table id="tool-table"></table></div></div>
      </div>
    </section>
  </div>

  <section class="card" aria-labelledby="h-paid">
    <h2 id="h-paid">Paid calls</h2>
    <p class="sub" id="paid-sub">Settled x402 payments, newest first.</p>
    <div class="stats" id="paid-stats"></div>
    <div class="grid-2" style="margin-bottom:12px">
      <div><div class="table-wrap"><table id="paid-failures"></table></div></div>
      <div><div class="table-wrap"><table id="paid-network"></table></div></div>
    </div>
    <div class="table-wrap"><table id="paid-table"></table></div>
    <div class="toolbar row-gap" id="paid-more"></div>
  </section>

  <section class="card" aria-labelledby="h-health">
    <h2 id="h-health">Request health</h2>
    <p class="sub">Outcome of non-monitor HTTP requests on the API and MCP.</p>
    <div class="legend" id="health-legend"></div>
    <div id="health-bar"></div>
    <div class="table-wrap row-gap"><table id="error-table"></table></div>
  </section>

  <section class="card" aria-labelledby="h-plan">
    <h2 id="h-plan">Improvement plan</h2>
    <p class="sub">Changes to the setup itself, in order. The assessment above covers what the traffic shows.</p>
    <div class="plan" id="plan"></div>
  </section>

  <section class="card" aria-labelledby="h-ua">
    <h2 id="h-ua">Top user agents</h2>
    <p class="sub">Raw client strings and the avenue they were attributed to. Use this to spot unclassified bots.</p>
    <div class="table-wrap"><table id="ua-table"></table></div>
  </section>

  <section class="card">
    <details>
      <summary>How these numbers are defined</summary>
      <dl class="defs" id="defs" style="margin-top:12px"></dl>
    </details>
  </section>
</main>
<div id="tip" role="tooltip"></div>
<script>
(() => {
  const USAGE_PATH = __USAGE_PATH__;
  const SVGNS = "http://www.w3.org/2000/svg";
  const state = { days: 30, data: null };
  try { const saved = Number(localStorage.getItem("usage-days")); if ([7, 30, 90].includes(saved)) state.days = saved; } catch (e) {}

  const $ = (id) => document.getElementById(id);
  const fmtInt = (n) => (n == null ? "–" : Math.round(n).toLocaleString("en-US"));
  const fmtPct = (n, d = 1) => (n == null ? "–" : (n * 100).toFixed(d) + "%");
  const fmtUsd = (n) => (n == null ? "–" : "$" + Number(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 3 }));
  const fmtCompact = (n) => (n == null ? "–" : Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(n));
  const chColor = (id) => `var(--ch-${id})`;

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null) continue;
      if (k === "class") node.className = v;
      else if (k === "style") node.setAttribute("style", v);
      else node.setAttribute(k, v);
    }
    for (const child of children.flat()) {
      if (child == null) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }
  function s(tag, attrs = {}) {
    const node = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    return node;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); return node; }

  // ---- tooltip -----------------------------------------------------------
  const tip = $("tip");
  function showTip(evt, title, rows) {
    clear(tip);
    tip.append(el("div", { class: "t" }, title));
    for (const r of rows) {
      const left = el("span", {}, r.color ? el("i", { class: "stroke", style: `background:${r.color}` }) : null, r.label);
      tip.append(el("div", { class: "r" }, left, el("b", {}, r.value)));
    }
    tip.style.display = "block";
    const rect = evt.target.getBoundingClientRect();
    const x = evt.clientX ?? rect.left + rect.width / 2;
    const y = evt.clientY ?? rect.top;
    const w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.min(window.innerWidth - w - 8, Math.max(8, x + 14)) + "px";
    tip.style.top = Math.max(8, y - h - 10) + "px";
  }
  function hideTip() { tip.style.display = "none"; }
  function bindTip(node, title, rows) {
    node.setAttribute("tabindex", "0");
    node.addEventListener("pointermove", (e) => showTip(e, title, rows()));
    node.addEventListener("focus", (e) => showTip(e, title, rows()));
    node.addEventListener("pointerleave", hideTip);
    node.addEventListener("blur", hideTip);
  }

  // ---- chart helpers -----------------------------------------------------
  function niceMax(v) {
    if (v <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
    return 10 * p;
  }
  // Rectangle with 4px rounded top corners: the data end of a vertical bar.
  function topRounded(x, y, w, h, r = 4) {
    r = Math.min(r, w / 2, h);
    return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
  }
  function shortDate(iso) {
    const d = new Date(iso + "T00:00:00Z");
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
  }
  function frame(width, height, pad, maxV) {
    const svg = s("svg", { viewBox: `0 0 ${width} ${height}`, class: "chart", role: "img" });
    const plotH = height - pad.t - pad.b;
    for (let i = 0; i <= 4; i++) {
      const v = (maxV / 4) * i;
      const y = pad.t + plotH - (v / maxV) * plotH;
      svg.append(s("line", { x1: pad.l, x2: width - pad.r, y1: y, y2: y, class: i === 0 ? "axis" : "grid" }));
      const t = s("text", { x: pad.l - 6, y: y + 4, "text-anchor": "end" });
      t.textContent = fmtCompact(v);
      svg.append(t);
    }
    return svg;
  }
  function xLabels(svg, days, pad, width, height, step) {
    const plotW = width - pad.l - pad.r;
    const every = Math.max(1, Math.ceil(days.length / 8));
    days.forEach((d, i) => {
      const last = i === days.length - 1;
      // Skip a regular tick that would collide with the final date label.
      if ((i % every !== 0 && !last) || (!last && days.length - 1 - i < every / 2)) return;
      const t = s("text", { x: pad.l + step * i + step / 2, y: height - 6, "text-anchor": "middle" });
      t.textContent = shortDate(d.date);
      svg.append(t);
    });
  }

  function stackedDaily(container, days, channels) {
    clear(container);
    const width = 1200, height = 300, pad = { t: 10, r: 8, b: 24, l: 48 };
    const maxV = niceMax(Math.max(1, ...days.map((d) => d.calls)));
    const svg = frame(width, height, pad, maxV);
    svg.setAttribute("aria-label", "Calls per day stacked by avenue");
    const plotW = width - pad.l - pad.r, plotH = height - pad.t - pad.b;
    const step = plotW / Math.max(1, days.length);
    const bw = Math.max(2, step - 2);
    days.forEach((d, i) => {
      const x = pad.l + step * i + (step - bw) / 2;
      let y = pad.t + plotH;
      const segs = channels.filter((c) => d.by_channel[c.id]);
      segs.forEach((c, j) => {
        const h = (d.by_channel[c.id] / maxV) * plotH;
        const gap = j < segs.length - 1 && h > 3 ? 2 : 0;
        y -= h;
        const shape = j === segs.length - 1
          ? s("path", { d: topRounded(x, y, bw, Math.max(0, h - gap)), fill: chColor(c.id) })
          : s("rect", { x, y: y + gap, width: bw, height: Math.max(0, h - gap), fill: chColor(c.id) });
        svg.append(shape);
      });
      const hit = s("rect", { x: pad.l + step * i, y: pad.t, width: step, height: plotH, class: "hit" });
      bindTip(hit, shortDate(d.date), () => [
        { label: "All calls", value: fmtInt(d.calls) },
        ...channels.filter((c) => d.by_channel[c.id]).reverse()
          .map((c) => ({ label: c.label, value: fmtInt(d.by_channel[c.id]), color: chColor(c.id) })),
      ]);
      svg.append(hit);
    });
    xLabels(svg, days, pad, width, height, step);
    container.append(svg);
  }

  function singleBars(container, days, key, label, fmt = fmtInt) {
    clear(container);
    const width = 600, height = 220, pad = { t: 10, r: 8, b: 24, l: 40 };
    const maxV = niceMax(Math.max(1, ...days.map((d) => d[key] || 0)));
    const svg = frame(width, height, pad, maxV);
    svg.setAttribute("aria-label", label);
    const plotW = width - pad.l - pad.r, plotH = height - pad.t - pad.b;
    const step = plotW / Math.max(1, days.length);
    const bw = Math.max(2, Math.min(18, step - 2));
    days.forEach((d, i) => {
      const v = d[key] || 0;
      const h = (v / maxV) * plotH;
      const x = pad.l + step * i + (step - bw) / 2;
      if (h > 0) svg.append(s("path", { d: topRounded(x, pad.t + plotH - h, bw, h), fill: "var(--series)" }));
      const hit = s("rect", { x: pad.l + step * i, y: pad.t, width: step, height: plotH, class: "hit" });
      bindTip(hit, shortDate(d.date), () => [{ label, value: fmt(v), color: "var(--series)" }]);
      svg.append(hit);
    });
    xLabels(svg, days, pad, width, height, step);
    container.append(svg);
  }

  function lineChart(container, days, key, label) {
    clear(container);
    const width = 600, height = 220, pad = { t: 12, r: 12, b: 24, l: 40 };
    const maxV = niceMax(Math.max(1, ...days.map((d) => d[key] || 0)));
    const svg = frame(width, height, pad, maxV);
    svg.setAttribute("aria-label", label);
    const plotW = width - pad.l - pad.r, plotH = height - pad.t - pad.b;
    const step = plotW / Math.max(1, days.length);
    const pts = days.map((d, i) => [pad.l + step * i + step / 2, pad.t + plotH - ((d[key] || 0) / maxV) * plotH]);
    svg.append(s("path", { d: pts.map((p, i) => (i ? "L" : "M") + p[0] + "," + p[1]).join(""), fill: "none", stroke: "var(--series)", "stroke-width": 2, "stroke-linejoin": "round" }));
    const cross = s("line", { y1: pad.t, y2: pad.t + plotH, class: "axis", visibility: "hidden" });
    const dot = s("circle", { r: 4, fill: "var(--series)", stroke: "var(--surface)", "stroke-width": 2, visibility: "hidden" });
    svg.append(cross, dot);
    const last = pts[pts.length - 1];
    if (last) {
      svg.append(s("circle", { cx: last[0], cy: last[1], r: 4, fill: "var(--series)", stroke: "var(--surface)", "stroke-width": 2 }));
      const t = s("text", { x: last[0] - 6, y: last[1] - 8, "text-anchor": "end" });
      t.textContent = fmtInt(days[days.length - 1][key]);
      svg.append(t);
    }
    days.forEach((d, i) => {
      const hit = s("rect", { x: pad.l + step * i, y: pad.t, width: step, height: plotH, class: "hit" });
      const show = () => {
        cross.setAttribute("x1", pts[i][0]); cross.setAttribute("x2", pts[i][0]); cross.setAttribute("visibility", "visible");
        dot.setAttribute("cx", pts[i][0]); dot.setAttribute("cy", pts[i][1]); dot.setAttribute("visibility", "visible");
      };
      hit.addEventListener("pointerenter", show);
      hit.addEventListener("focus", show);
      hit.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); });
      bindTip(hit, shortDate(d.date), () => [{ label, value: fmtInt(d[key]), color: "var(--series)" }]);
      svg.append(hit);
    });
    xLabels(svg, days, pad, width, height, step);
    container.append(svg);
  }

  // A horizontal bar inside a table cell; segments get a 2px gap.
  function hbar(segments, maxV, title) {
    const wrap = el("div", { class: "hbar" });
    for (const seg of segments) {
      if (!seg.value) continue;
      const i = el("i", { style: `width:${(seg.value / maxV) * 100}%;background:${seg.color}` });
      wrap.append(i);
    }
    bindTip(wrap, title, () => segments.map((seg) => ({ label: seg.label, value: fmtInt(seg.value), color: seg.color })));
    return wrap;
  }

  function table(node, headers, rows, emptyText = "Nothing in this window.") {
    clear(node);
    const thead = el("thead", {}, el("tr", {}, headers.map((h) => el("th", { class: h.num ? "num" : null }, h.label))));
    const tbody = el("tbody");
    if (!rows.length) tbody.append(el("tr", {}, el("td", { colspan: headers.length, class: "empty" }, emptyText)));
    for (const row of rows) {
      tbody.append(el("tr", {}, row.map((cell, i) => el("td", { class: headers[i].num ? "num" : headers[i].cls || null }, cell))));
    }
    node.append(thead, tbody);
  }

  // ---- sections ----------------------------------------------------------
  function renderKpis(data) {
    const node = clear($("kpis"));
    const days = data.window_days;
    for (const k of data.kpis.filter((k) => k.id !== "verified_identities")) {
      const value = k.unit === "rate" ? fmtPct(k.value) : k.unit === "usdc" ? fmtUsd(k.value) : fmtInt(k.value);
      let delta = el("div", { class: "delta muted" }, `no prior ${days}d data`);
      const basis = k.comparable_value ?? k.value;
      if (k.unit === "rate" && basis != null && k.previous != null) {
        // Rates change in percentage points; a relative change of a rate misleads.
        const pts = (basis - k.previous) * 100;
        const up = pts >= 0;
        const good = (up && k.good_direction === "up") || (!up && k.good_direction === "down");
        delta = el("div", { class: `delta ${up ? "up" : "down"}-${good ? "good" : "bad"}` },
          `${up ? "▲" : "▼"} ${Math.abs(pts).toFixed(1)} pts vs prior ${days}d`);
      } else if (k.delta != null) {
        const up = k.delta >= 0;
        const good = (up && k.good_direction === "up") || (!up && k.good_direction === "down");
        delta = el("div", { class: `delta ${up ? "up" : "down"}-${good ? "good" : "bad"}` },
          `${up ? "▲" : "▼"} ${fmtPct(Math.abs(k.delta), 0)} vs prior ${days}d`);
      } else if (k.previous != null && k.previous === 0 && k.value) {
        delta = el("div", { class: "delta muted" }, `new: 0 in prior ${days}d`);
      }
      node.append(el("div", { class: "kpi" },
        el("div", { class: "label" }, k.label),
        el("div", { class: "value" }, value),
        delta,
        k.note ? el("div", { class: "note" }, k.note) : null));
    }
  }

  const SEVERITY = {
    P0: { color: "var(--status-critical)", text: "P0 · fix now" },
    P1: { color: "var(--status-serious)", text: "P1 · this month" },
    P2: { color: "var(--status-warning)", text: "P2 · watch" },
  };
  function renderIssues(data) {
    const node = clear($("issues"));
    if (!data.assessment.length) {
      node.append(el("div", { class: "issue" },
        el("span", { class: "chip" }, el("i", { class: "dot", style: "background:var(--status-good)" }), "OK"),
        el("h3", {}, "No issues crossed a threshold in this window.")));
      return;
    }
    for (const issue of data.assessment) {
      const sev = SEVERITY[issue.severity];
      node.append(el("article", { class: "issue" },
        el("span", { class: "chip" }, el("i", { class: "dot", style: `background:${sev.color}` }), sev.text),
        " ", el("span", { class: "muted", style: "font-size:12px" }, issue.area),
        el("h3", {}, issue.title),
        el("p", {}, el("b", {}, "Evidence: "), issue.evidence),
        el("p", {}, el("b", {}, "Fix: "), issue.fix)));
    }
  }

  function renderRetention(data) {
    const r = data.retention || {};
    const node = $("retention-note");
    const parts = [];
    if (r.comparison_excludes_claude) parts.push(`Changes vs the prior ${data.window_days} days leave out Claude connector traffic, because it is deleted after ${r.claude_days} days.`);
    if (r.window_exceeds_claude_retention) parts.push(`Claude connector data older than ${r.claude_days} days is gone, so this window undercounts it.`);
    node.textContent = parts.join(" ");
    node.hidden = !parts.length;
    $("activity-sub").textContent = "Calls per day, stacked by avenue. Monitors are shown in gray so real demand stays readable."
      + (r.window_exceeds_claude_retention ? ` Claude connector calls appear only for the last ${r.claude_days} days.` : "");
  }

  function renderActivity(data) {
    const channels = data.channel_order;
    const present = channels.filter((c) => data.activity.some((d) => d.by_channel[c.id]));
    const legend = clear($("activity-legend"));
    for (const c of present) legend.append(el("span", {}, el("i", { class: "key", style: `background:${chColor(c.id)}` }), c.label));
    stackedDaily($("activity-chart"), data.activity, channels);
    lineChart($("users-chart"), data.activity, "unique_clients", "Unique clients");
    singleBars($("paid-chart"), data.activity, "paid_calls", "Paid calls");
  }

  function renderAvenues(data) {
    const rows = [...data.channels].sort((a, b) => b.calls - a.calls);
    const maxV = Math.max(1, ...rows.map((r) => r.calls));
    const nonMonitor = rows.filter((r) => r.id !== "monitor").reduce((a, r) => a + r.calls, 0);
    $("avenue-sub").textContent = `Which door each call came through. ${fmtInt(nonMonitor)} non-monitor calls in the last ${data.window_days} days.`;
    table($("avenue-table"), [
      { label: "Avenue" }, { label: "Calls", cls: "bar-cell" }, { label: "Calls", num: true },
      { label: "Share", num: true }, { label: "Clients", num: true }, { label: "Paid calls", num: true }, { label: "Revenue", num: true },
    ], rows.map((r) => [
      el("span", { title: r.retention_note || null }, el("i", { class: "key", style: `background:${chColor(r.id)};margin-right:8px` }), r.label,
        r.retention_note ? el("span", { class: "muted", style: "font-size:12px" }, ` · connector: last ${data.retention.claude_days} days`) : null),
      hbar([{ label: r.label, value: r.calls, color: chColor(r.id) }], maxV, r.label),
      fmtInt(r.calls), fmtPct(r.share), fmtInt(r.users), fmtInt(r.paid_calls), fmtUsd(r.revenue_usdc),
    ]));
  }

  function renderTickers(data) {
    const t = data.tickers;
    const labels = Object.fromEntries(data.channel_order.map((c) => [c.id, c.label]));
    $("ticker-sub").textContent = `${fmtInt(t.distinct_tickers)} distinct tickers; the top three take ${fmtPct(t.top3_share)} of non-monitor live-data requests.`;
    const rows = t.rows.slice(0, 20);
    const maxV = Math.max(1, ...rows.map((r) => r.calls + r.monitor_calls));
    table($("ticker-table"), [
      { label: "Ticker" }, { label: "Requests", cls: "bar-cell" }, { label: "Calls", num: true },
      { label: "Monitors", num: true }, { label: "Paid", num: true }, { label: "Free credit", num: true }, { label: "Top avenue" },
    ], rows.map((r) => {
      const top = Object.entries(r.by_channel)[0];
      return [
        el("b", {}, r.ticker),
        hbar([
          { label: "Calls excl. monitors", value: r.calls, color: "var(--series)" },
          { label: "Monitor calls", value: r.monitor_calls, color: "var(--ch-monitor)" },
        ], maxV, r.ticker),
        fmtInt(r.calls), fmtInt(r.monitor_calls), fmtInt(r.paid_calls), fmtInt(r.credit_calls),
        top ? `${labels[top[0]] || top[0]} (${fmtPct(top[1] / Math.max(1, r.calls), 0)})` : "–",
      ];
    }));
  }

  function stat(value, label) { return el("div", { class: "stat" }, el("div", { class: "v" }, value), el("div", { class: "l" }, label)); }

  function renderUsers(data) {
    const u = data.users;
    const node = clear($("user-stats"));
    node.append(
      stat(fmtInt(u.unique_clients), "Unique clients"),
      stat(fmtInt(u.new_clients), "New vs prior window"),
      stat(fmtInt(u.returning_clients), `Active on 2+ days (${fmtPct(u.returning_rate, 0)})`),
      stat(fmtInt(u.verified_identities), "Verified identities"),
      stat(fmtInt(u.paying_wallets), "Paying wallets"),
    );
    const maxV = Math.max(1, ...u.top_clients.map((c) => c.requests));
    table($("client-table"), [
      { label: "Client" }, { label: "Requests", cls: "bar-cell" }, { label: "Share", num: true }, { label: "Days active", num: true },
    ], u.top_clients.map((c) => [
      c.client, hbar([{ label: "Requests", value: c.requests, color: "var(--series)" }], maxV, c.client),
      fmtPct(c.share), fmtInt(c.active_days),
    ]));
  }

  function renderSearch(data) {
    table($("search-table"), [{ label: "Search term" }, { label: "Count", num: true }],
      data.search_terms.map((r) => [r.term, fmtInt(r.count)]));
    table($("tool-table"), [{ label: "MCP tool" }, { label: "Calls", num: true }],
      data.mcp_tools.map((r) => [r.tool, fmtInt(r.count)]));
  }

  function renderPaid(data) {
    const p = data.paid;
    const labels = Object.fromEntries(data.channel_order.map((c) => [c.id, c.label]));
    const burstShare = p.settled_payments ? p.burst_settlements / p.settled_payments : null;
    $("paid-sub").textContent = p.internal_wallets_configured
      ? `Settled x402 payments, newest first. ${fmtInt(p.external_settled_payments)} of ${fmtInt(p.settled_payments)} came from wallets not marked internal.`
      : "Settled x402 payments, newest first. No payer wallets are marked internal, so every payment counts as a customer.";
    clear($("paid-stats")).append(
      stat(fmtInt(p.x402_paid_calls), "Paid calls (x402)"),
      stat(fmtUsd(p.revenue_usdc), "Revenue"),
      stat(fmtInt(p.paying_wallets), "Paying wallets"),
      stat(`${fmtInt(p.settled_attempts)}/${fmtInt(p.proof_attempts)}`, "Proofs settled"),
      stat(fmtPct(burstShare, 0), "Arrived in bursts"),
      stat(fmtInt(p.credit_calls), "Free-credit calls"),
    );
    table($("paid-failures"), [{ label: "Payment failure reason" }, { label: "Count", num: true }],
      Object.entries(p.proof_failures).map(([k, v]) => [k, fmtInt(v)]), "No failed proofs.");
    table($("paid-network"), [{ label: "Network" }, { label: "Payments", num: true }],
      Object.entries(p.by_network).map(([k, v]) => [k, fmtInt(v)]), "No payments.");
    const PAID_PREVIEW = 20;
    const paidRows = state.showAllPaid ? p.rows : p.rows.slice(0, PAID_PREVIEW);
    table($("paid-table"), [
      { label: "Time (UTC)" }, { label: "Ticker" }, { label: "Endpoint" }, { label: "Network" },
      { label: "Amount", num: true }, { label: "Avenue" }, { label: "Payer" }, { label: "Flags" },
    ], paidRows.map((r) => [
      r.timestamp.slice(0, 16).replace("T", " "), el("b", {}, r.ticker), r.endpoint || "–", r.network,
      fmtUsd(r.amount_usdc), labels[r.channel] || r.channel, r.payer,
      el("span", {}, r.burst ? el("span", { class: "flag" }, "burst") : null, r.internal ? el("span", { class: "flag" }, "internal") : null),
    ]), "No paid calls in this window.");
    const more = clear($("paid-more"));
    if (p.rows.length > PAID_PREVIEW) {
      const button = el("button", { type: "button", class: "link" },
        state.showAllPaid ? "Show fewer" : `Show all ${p.rows.length} payments`);
      button.addEventListener("click", () => { state.showAllPaid = !state.showAllPaid; renderPaid(state.data); });
      more.append(button);
    }
  }

  const HEALTH = [
    { id: "ok", label: "Delivered", color: "var(--status-good)" },
    { id: "payment_required", label: "402 payment required", color: "var(--status-neutral)" },
    { id: "client_error", label: "4xx client error", color: "var(--status-warning)" },
    { id: "rate_limited", label: "429 rate limited", color: "var(--status-serious)" },
    { id: "server_error", label: "5xx server error", color: "var(--status-critical)" },
  ];
  function renderHealth(data) {
    const h = data.health;
    const total = Math.max(1, h.http_calls);
    const legend = clear($("health-legend"));
    for (const b of HEALTH) legend.append(el("span", {}, el("i", { class: "key", style: `background:${b.color}` }),
      `${b.label}: ${fmtInt(h.status[b.id])} (${fmtPct(h.status[b.id] / total)})`));
    const bar = el("div", { class: "hbar", style: "height:16px" });
    for (const b of HEALTH) {
      if (!h.status[b.id]) continue;
      bar.append(el("i", { style: `width:${(h.status[b.id] / total) * 100}%;background:${b.color}` }));
    }
    bindTip(bar, "Request outcomes", () => HEALTH.map((b) => ({ label: b.label, value: fmtInt(h.status[b.id]), color: b.color })));
    clear($("health-bar")).append(bar);
    table($("error-table"), [{ label: "Failing endpoint" }, { label: "Status", num: true }, { label: "Requests", num: true }],
      h.top_errors.map((r) => [el("span", { class: "ua" }, r.endpoint), String(r.status_code), fmtInt(r.count)]), "No errors.");
  }

  function renderPlan(data) {
    const node = clear($("plan"));
    for (const horizon of ["Now", "Next", "Later"]) {
      const items = data.improvement_plan.filter((i) => i.horizon === horizon);
      node.append(el("div", {}, el("h3", { style: "font-size:14px;margin:0 0 8px" }, horizon),
        el("ol", {}, items.map((i) => el("li", {}, el("b", {}, i.title), el("span", {}, i.detail))))));
    }
  }

  function renderUserAgents(data) {
    const labels = Object.fromEntries(data.channel_order.map((c) => [c.id, c.label]));
    table($("ua-table"), [{ label: "User agent" }, { label: "Avenue" }, { label: "Calls", num: true }],
      data.user_agents.map((r) => [el("span", { class: "ua" }, r.user_agent), labels[r.channel] || r.channel, fmtInt(r.calls)]));
  }

  function renderDefs(data) {
    const node = clear($("defs"));
    for (const [k, v] of Object.entries(data.definitions)) node.append(el("dt", {}, k.replace(/_/g, " ")), el("dd", {}, v));
  }

  function render(data) {
    const generated = new Date(data.generated_at);
    $("status-line").textContent = `Last ${data.window_days} days · updated ${generated.toLocaleString()} · test traffic excluded`;
    renderKpis(data); renderRetention(data); renderIssues(data); renderActivity(data); renderAvenues(data); renderTickers(data);
    renderUsers(data); renderSearch(data); renderPaid(data); renderHealth(data); renderPlan(data);
    renderUserAgents(data); renderDefs(data);
  }

  async function load() {
    document.querySelectorAll("#window-seg button").forEach((b) => b.setAttribute("aria-pressed", String(Number(b.dataset.days) === state.days)));
    $("app").classList.add("loading");
    try {
      const url = `${USAGE_PATH}?days=${encodeURIComponent(state.days)}`;
      const res = await fetch(url, { credentials: "same-origin", cache: "no-store" });
      if (res.status === 401) { window.location.reload(); return; }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      state.data = await res.json();
      render(state.data);
    } catch (err) {
      $("status-line").textContent = `Could not load usage data (${err.message}). Showing the last successful load, if any.`;
    } finally {
      $("app").classList.remove("loading");
    }
  }

  document.querySelectorAll("#window-seg button").forEach((b) => b.addEventListener("click", () => {
    state.days = Number(b.dataset.days);
    try { localStorage.setItem("usage-days", String(state.days)); } catch (e) {}
    load();
  }));
  $("refresh").addEventListener("click", load);
  load();
})();
</script>
</body>
</html>
"""


def usage_dashboard_html(*, usage_path: str, deep_dive_path: str, logout_path: str) -> str:
    return (
        _PAGE.replace("__USAGE_PATH__", json.dumps(usage_path))
        .replace("__DEEP_DIVE_PATH__", deep_dive_path)
        .replace("__LOGOUT_PATH__", logout_path)
    )
