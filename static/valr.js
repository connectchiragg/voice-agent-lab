// The VALR cockpit's behaviour: countdowns, the performance chart, the owner's
// actions (POST with the page's CSRF token) and a quiet refresh when something changes.
const csrf = document.querySelector('meta[name="csrf-token"]')?.content ?? "";
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

// Countdowns.
function until(ms) {
  if (ms <= 0) return "now";
  const minutes = Math.round(ms / 60000);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours} h ${String(minutes % 60).padStart(2, "0")} min`;
  return `${Math.floor(hours / 24)} days`;
}
function tick() {
  const now = Date.now();
  document.querySelectorAll("[data-countdown]").forEach(node => {
    const ms = Date.parse(node.dataset.countdown) - now;
    if (node.hasAttribute("data-short")) {
      node.textContent = ms > 0 ? `in ${until(ms)}` : "passed";
    } else {
      node.textContent = ms > 0 ? `in ${until(ms)}` : "calling now";
    }
  });
}
tick();
setInterval(tick, 15000);

// Actions.
async function post(path, body = {}) {
  const response = await fetch(path, {
    method: "POST", credentials: "same-origin",
    headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf},
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `HTTP ${response.status}`);
  return data;
}
function report(near, text, ok) {
  const box = near.closest(".hero-actions, .slot-actions")?.querySelector(".action-result");
  if (!box) return;
  box.textContent = text;
  box.className = `action-result small tone-${ok ? "ok" : "bad"}`;
}
document.addEventListener("click", async event => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  button.disabled = true;
  try {
    const result = action === "sync" ? await post("/valr/api/sync")
      : await post(`/valr/api/slots/${encodeURIComponent(button.dataset.ref)}/refresh`);
    report(button, result.message ?? "Done.", true);
    scheduleCheck(4000);
  } catch (error) {
    report(button, error.message, false);
  } finally {
    setTimeout(() => { button.disabled = false; }, 3000);
  }
});
const dials = document.querySelector("#dials-toggle");
dials?.addEventListener("change", async () => {
  const paused = !dials.checked;
  dials.disabled = true;
  try {
    const result = await post("/valr/api/dials", {paused});
    document.querySelector("[data-dials-label]").textContent = paused ? "paused" : "on";
    report(dials, result.message, true);
    scheduleCheck(500);
  } catch (error) {
    dials.checked = !dials.checked;
    report(dials, error.message, false);
  } finally {
    dials.disabled = false;
  }
});

// Prep checklist ticks: this browser only.
const CHECKS = "valr-checklist";
function loadChecks() {
  try { return JSON.parse(localStorage.getItem(CHECKS) ?? "{}"); } catch { return {}; }
}
const checks = loadChecks();
document.querySelectorAll("[data-check]").forEach(box => {
  const key = box.nextElementSibling?.textContent ?? box.dataset.check;
  box.checked = Boolean(checks[key]);
  box.addEventListener("change", () => {
    checks[key] = box.checked;
    try { localStorage.setItem(CHECKS, JSON.stringify(checks)); } catch {}
  });
});

// Keep open briefings open across a refresh.
const OPEN = "valr-open";
try {
  const open = new Set(JSON.parse(sessionStorage.getItem(OPEN) ?? "[]"));
  document.querySelectorAll(".slot[data-ref]").forEach(slot => {
    if (open.has(slot.dataset.ref)) slot.querySelector("details").open = true;
  });
} catch {}
function rememberOpen() {
  const refs = [...document.querySelectorAll(".slot[data-ref] details[open]")]
    .map(details => details.closest(".slot").dataset.ref);
  try { sessionStorage.setItem(OPEN, JSON.stringify(refs)); } catch {}
}

// Quiet refresh: poll the JSON state; reload only when what is shown changed.
function signature(view) {
  return JSON.stringify([
    view.dials_paused?.enabled, view.next_call?.at, view.jobs?.length && view.jobs[0].status,
    (view.blocks ?? []).map(b => [b.id, b.state, b.slots.map(s => [s.ref, s.status, s.updated_at])]),
  ]);
}
let baseline = null;
let checkTimer = null;
async function check() {
  if (document.hidden) return;
  try {
    const response = await fetch("/valr/api/state", {credentials: "same-origin", cache: "no-store"});
    if (!response.ok) return;
    const now = signature(await response.json());
    if (baseline !== null && now !== baseline) {
      rememberOpen();
      location.reload();
    }
    baseline = now;
  } catch {}
}
function scheduleCheck(delay) {
  clearTimeout(checkTimer);
  checkTimer = setTimeout(check, delay);
}
check();
setInterval(check, 60000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) check(); });

// The performance chart: hand-rolled SVG, one line per dimension (mean score per day).
const SVG = "http://www.w3.org/2000/svg";
function el(name, attrs = {}, parent) {
  const node = document.createElementNS(SVG, name);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  parent?.append(node);
  return node;
}
function drawChart() {
  const holder = document.querySelector("#perf-chart");
  const source = document.querySelector("#perf-data");
  if (!holder || !source) return;
  const {rows, dimensions} = JSON.parse(source.textContent);
  const dims = Object.keys(dimensions);
  if (!rows.length || !dims.length) return;
  const styles = getComputedStyle(document.documentElement);
  const colors = dims.map((_, i) => styles.getPropertyValue(`--c${(i % 7) + 1}`).trim());
  const days = [...new Set(rows.map(r => r.date))].sort();
  const t = day => Date.parse(`${day}T00:00:00Z`);
  const t0 = t(days[0]);
  const t1 = Math.max(t(days.at(-1)), t0 + 86400000);
  const width = Math.max(holder.clientWidth, 300);
  const height = width < 520 ? 240 : 280;
  const pad = {l: 28, r: 12, t: 12, b: 28};
  const x = ms => pad.l + ((ms - t0) / (t1 - t0)) * (width - pad.l - pad.r);
  const y = score => pad.t + ((5 - score) / 4) * (height - pad.t - pad.b);
  holder.replaceChildren();
  const svg = el("svg", {viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true"}, holder);
  const grid = el("g", {class: "grid axis"}, svg);
  for (let s = 1; s <= 5; s++) {
    el("line", {x1: pad.l, x2: width - pad.r, y1: y(s), y2: y(s)}, grid);
    el("text", {x: pad.l - 10, y: y(s) + 4, "text-anchor": "end"}, grid).textContent = s;
  }
  const ticks = Math.min(days.length, width < 520 ? 3 : 6);
  for (let i = 0; i < ticks; i++) {
    const ms = t0 + ((t1 - t0) * i) / Math.max(ticks - 1, 1);
    const label = new Date(ms).toLocaleDateString(undefined, {day: "numeric", month: "short", timeZone: "UTC"});
    el("text", {x: x(ms), y: height - 8, "text-anchor": i === 0 ? "start" : i === ticks - 1 ? "end" : "middle"}, grid)
      .textContent = label;
  }
  let tip = holder.parentElement.querySelector(".chart-tip");
  if (!tip) {
    tip = document.createElement("div");
    tip.className = "chart-tip";
    holder.parentElement.append(tip);
  }
  tip.hidden = true;
  const groups = {};
  dims.forEach((dim, i) => {
    const byDay = new Map();
    rows.filter(r => r.dimension === dim).forEach(r => {
      const list = byDay.get(r.date) ?? [];
      list.push(r);
      byDay.set(r.date, list);
    });
    const points = [...byDay.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([day, list]) => ({
      day, score: list.reduce((sum, r) => sum + r.score, 0) / list.length,
      who: list.map(r => r.company).filter(Boolean).join(", "),
    }));
    const group = groups[dim] = el("g", {class: "series", style: `color:${colors[i]}`}, svg);
    const d = points.map((p, k) => `${k ? "L" : "M"}${x(t(p.day)).toFixed(1)},${y(p.score).toFixed(1)}`).join("");
    const path = el("path", {d, stroke: "currentColor"}, group);
    if (!reduceMotion) {
      const length = path.getTotalLength();
      path.style.strokeDasharray = `${length}`;
      path.style.strokeDashoffset = `${length}`;
      path.animate([{strokeDashoffset: length}, {strokeDashoffset: 0}], {duration: 900, easing: "ease-out", fill: "forwards"});
    }
    points.forEach(p => {
      const dot = el("circle", {cx: x(t(p.day)), cy: y(p.score), r: 4, fill: "currentColor"}, group);
      dot.addEventListener("pointerenter", () => {
        tip.textContent = `${dimensions[dim].label}: ${p.score.toFixed(1)} · ${p.day}${p.who ? ` · ${p.who}` : ""}`;
        tip.hidden = false;
        const box = holder.getBoundingClientRect();
        const scale = box.width / width;
        tip.style.left = `${Math.min(x(t(p.day)) * scale, box.width - 180)}px`;
        tip.style.top = `${y(p.score) * scale - 36}px`;
      });
      dot.addEventListener("pointerleave", () => { tip.hidden = true; });
    });
  });
  const legend = document.querySelector("#perf-legend");
  legend.replaceChildren();
  let focused = null;
  dims.forEach((dim, i) => {
    const button = document.createElement("button");
    button.type = "button";
    button.setAttribute("aria-pressed", "false");
    button.style.color = colors[i];
    button.innerHTML = "<i></i>";
    button.querySelector("i").style.background = colors[i];
    button.append(document.createTextNode(dimensions[dim].label));
    button.title = "Highlight this dimension";
    button.addEventListener("click", () => {
      focused = focused === dim ? null : dim;
      holder.classList.toggle("focus", focused !== null);
      Object.entries(groups).forEach(([name, group]) => group.classList.toggle("on", name === focused));
      legend.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b === button && focused !== null)));
    });
    legend.append(button);
  });
}
drawChart();
let resizeTimer = null;
addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(drawChart, 200);
});
