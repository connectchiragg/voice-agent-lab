// The VALR pages' behaviour (every page): countdowns, the (legacy) interview drawer, the owner's actions (POST with the
// page's CSRF token), the prep checklists, the trend chart and a quiet refresh.
const csrf = document.querySelector('meta[name="csrf-token"]')?.content ?? "";
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

// Relative times next to the IST times the server wrote.
function until(ms) {
  const minutes = Math.round(Math.abs(ms) / 60000);
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours} h ${String(minutes % 60).padStart(2, "0")} min`;
  return `${Math.round(hours / 24)} days`;
}
function tick() {
  const now = Date.now();
  document.querySelectorAll("[data-countdown]").forEach(node => {
    const ms = Date.parse(node.dataset.countdown) - now;
    node.textContent = ms > 30000 ? `in ${until(ms)}` : ms > -60000 ? "now" : `${until(ms)} ago`;
  });
}
tick();
setInterval(tick, 15000);

// Toasts.
const toast = document.querySelector(".toast");
let toastTimer = null;
function say(text, ok = true) {
  toast.textContent = text;
  toast.classList.toggle("bad", !ok);
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, 5000);
}

// The interview drawer: a <dialog> per interview; the URL hash keeps it linkable.
function openDrawer(ref, push = true) {
  const dialog = document.getElementById(`iv-${ref}`);
  if (!dialog || dialog.open) return;
  document.querySelectorAll("dialog.drawer[open]").forEach(d => d.close());
  dialog.showModal();
  loadRecording(dialog);
  if (push) history.pushState({ref}, "", `#iv-${ref}`);
  setTimeout(() => dialog.querySelector(".close")?.focus(), 50);
}
document.addEventListener("click", event => {
  const opener = event.target.closest("[data-open]");
  if (opener) {
    event.preventDefault();
    openDrawer(opener.dataset.open);
    return;
  }
  if (event.target.closest("[data-close]")) event.target.closest("dialog").close();
  if (event.target.matches("dialog.drawer")) event.target.close();  // A click on the backdrop.
});
document.querySelectorAll("dialog.drawer").forEach(dialog => {
  dialog.addEventListener("close", () => {
    if (location.hash === `#${dialog.id}`) history.pushState(null, "", location.pathname + location.search);
  });
});
function fromHash() {
  const match = location.hash.match(/^#iv-(slot-[\w-]+)$/);
  document.querySelectorAll("dialog.drawer[open]").forEach(d => { if (!match || d.id !== `iv-${match[1]}`) d.close(); });
  if (match) openDrawer(match[1], false);
}
addEventListener("popstate", fromHash);
fromHash();

// Actions.
async function post(path, body = {}) {
  const response = await fetch(path, {
    method: "POST", credentials: "same-origin",
    headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf},
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Something went wrong (HTTP ${response.status}).`);
  return data;
}
const BUSY = {sync: "Syncing…", refresh: "Starting…", dials: "Saving…", callnow: "Calling…", tgcode: "Getting a code…",
              tglink: "Getting a link…", tgtest: "Sending…", ntsync: "Checking…", ntdisconnect: "Disconnecting…",
              reach: "Saving…"};

// Telegram: one tap. The code comes with a t.me deep link (Telegram sends "/start <code>"
// when he presses Start); a QR of the same link for scanning with the phone. Then wait for
// the link to land and reload.
// qrcode-generator 1.4.4, vendored in static/ (no third-party script at run time).
const QR_SRC = "/static/qrcode.min.js";
function loadQr() {
  if (window.qrcode) return Promise.resolve(window.qrcode);
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = QR_SRC;
    script.onload = () => resolve(window.qrcode);
    script.onerror = reject;
    document.head.append(script);
  });
}
async function showTelegramLink(result) {
  const panel = document.querySelector("[data-tg-panel]");
  if (!panel) {
    if (result.link) window.open(result.link, "_blank", "noopener");
    return;
  }
  const open = panel.querySelector("[data-tg-open]");
  const help = panel.querySelector("[data-tg-help]");
  panel.hidden = false;
  if (result.link) {
    open.href = result.link;
    open.hidden = false;
    help.textContent = `On this phone: tap Open Telegram, then Start. On a computer: scan the code with your phone. Valid ${result.expires_minutes} minutes.`;
    try {
      const qr = (await loadQr())(0, "M");
      qr.addData(result.link);
      qr.make();
      panel.querySelector("[data-tg-qr]").innerHTML = qr.createSvgTag({cellSize: 4, margin: 0, scalable: true});
    } catch {}
  } else {
    open.hidden = true;
    help.textContent = `Send ${result.command} to the VALR bot within ${result.expires_minutes} minutes.`;
  }
  const started = Date.now();
  const wait = setInterval(async () => {
    if (Date.now() - started > result.expires_minutes * 60000) { clearInterval(wait); return; }
    try {
      const state = await (await fetch("/valr/api/state", {credentials: "same-origin", cache: "no-store"})).json();
      if (state.connections?.telegram?.linked) { clearInterval(wait); location.reload(); }
    } catch {}
  }, 4000);
}
document.addEventListener("click", async event => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  if (action === "reload") {
    location.reload();
    return;
  }
  const label = button.textContent;
  button.setAttribute("aria-busy", "true");
  button.disabled = true;
  button.textContent = BUSY[action];
  try {
    let result;
    if (action === "sync") {
      result = await post("/valr/api/sync");
    } else if (action === "callnow") {
      result = await post("/valr/api/call-now");
    } else if (action === "refresh") {
      result = await post(`/valr/api/slots/${encodeURIComponent(button.dataset.ref)}/refresh`);
    } else if (action === "tglink") {
      result = await post("/valr/api/telegram/code");
      await showTelegramLink(result);
    } else if (action === "tgtest") {
      result = await post("/valr/api/telegram/test");
    } else if (action === "tgcode") {
      result = await post("/valr/api/telegram/code");
      const code = button.parentElement.querySelector("[data-tg-code]");
      if (code) {
        code.textContent = `${result.command} (valid ${result.expires_minutes} min)`;
        code.hidden = false;
      }
    } else if (action === "ntsync") {
      result = await post(`/valr/api/notetakers/${encodeURIComponent(button.dataset.provider)}/sync`);
    } else if (action === "ntdisconnect") {
      result = await post(`/valr/api/notetakers/${encodeURIComponent(button.dataset.provider)}/disconnect`);
      setTimeout(() => location.reload(), 800);
    } else if (action === "reach") {
      result = await post("/valr/api/reach", {mode: button.dataset.mode});
      setTimeout(() => location.reload(), 600);
    } else if (action === "dials") {
      result = await post("/valr/api/dials", {paused: button.dataset.paused === "true"});
      setTimeout(() => { rememberOpen(); location.reload(); }, 600);
    }
    say(result?.message ?? "Done.");
    scheduleCheck(4000);
  } catch (error) {
    say(error.message, false);
  } finally {
    button.removeAttribute("aria-busy");
    button.disabled = false;
    button.textContent = label;
  }
});

// Notetakers: the connect forms post the key to the server and forget it at once.
function connectForm(name) {
  return document.querySelector(`form[data-connect="${CSS.escape(name)}"]`);
}
function openConnect(name) {
  const form = connectForm(name);
  if (!form) return;
  document.querySelectorAll("dialog[open]").forEach(d => d.close());
  form.closest("details")?.setAttribute("open", "");
  form.hidden = false;
  document.querySelectorAll(`[data-connect-open="${CSS.escape(name)}"][aria-controls]`).forEach(b => b.setAttribute("aria-expanded", "true"));
  form.closest(".nt-card")?.scrollIntoView({behavior: reduceMotion ? "auto" : "smooth", block: "center"});
  setTimeout(() => form.querySelector("input")?.focus(), reduceMotion ? 0 : 350);
}
document.addEventListener("click", event => {
  const opener = event.target.closest("[data-connect-open]");
  if (opener) {
    event.preventDefault();
    openConnect(opener.dataset.connectOpen);
    return;
  }
  const cancel = event.target.closest("[data-connect-cancel]");
  if (cancel) {
    const form = connectForm(cancel.dataset.connectCancel);
    if (form) { form.reset(); form.hidden = true; }
  }
});
document.addEventListener("submit", async event => {
  const form = event.target;
  if (form.matches("form[data-connect]")) {
    event.preventDefault();
    const input = form.querySelector('input[name="api_key"]');
    const key = input.value.trim();
    input.value = "";  // The key leaves the page with this request and is never shown again.
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      const result = await post(`/valr/api/notetakers/${encodeURIComponent(form.dataset.connect)}/connect`, {api_key: key});
      say(result.message);
      setTimeout(() => location.reload(), 900);
    } catch (error) {
      say(error.message, false);
    } finally {
      button.disabled = false;
    }
  } else if (form.matches("form[data-import]")) {
    event.preventDefault();
    const file = form.querySelector('input[type="file"]').files[0];
    if (!file) { say("Choose the export file first.", false); return; }
    if (file.size > 3_000_000) { say("That export is too large (3 MB at most).", false); return; }
    try {
      const result = await post(`/valr/api/notetakers/${encodeURIComponent(form.dataset.import)}/import`,
        {filename: file.name, export: await file.text()});
      say(result.message);
      form.reset();
    } catch (error) {
      say(error.message, false);
    }
  }
});
if (location.hash.startsWith("#nt-")) openConnect(location.hash.slice(4));

// Past interviews: the recording, its player and key moments live in static/valr-player.js
// (its own module). The drawer asks it to fill the drawer's recording section.
function loadRecording(dialog) {
  const section = dialog.querySelector("[data-recording-ref]");
  if (!section || section.dataset.loaded) return;
  section.dataset.loaded = "1";
  document.dispatchEvent(new CustomEvent("valr:recording", {detail: {
    body: section.querySelector("[data-recording-body]"), query: `ref=${encodeURIComponent(section.dataset.recordingRef)}`}}));
}

// Prep checklists: each tick is saved on the server (so Home can show it done).
document.querySelectorAll("input[data-prep-ref]").forEach(box => {
  box.addEventListener("change", async () => {
    const item = box.nextElementSibling?.textContent?.trim() ?? "";
    try {
      await post("/valr/api/prep", {ref: box.dataset.prepRef, item, done: box.checked});
    } catch (error) {
      box.checked = !box.checked;
      say(error.message, false);
    }
  });
});

// Quiet refresh: poll the JSON state; reload only when something shown changed.
function rememberOpen() {
  const open = document.querySelector("dialog.drawer[open]");
  try { sessionStorage.setItem("valr-open", open ? open.id : ""); } catch {}
}
try {
  const open = sessionStorage.getItem("valr-open");
  if (open && !location.hash) openDrawer(open.replace(/^iv-/, ""), false);
  sessionStorage.removeItem("valr-open");
} catch {}
function signature(view) {
  return JSON.stringify([
    view.dials_paused?.enabled, view.next_call?.at, view.jobs?.[0]?.status, view.connections?.telegram?.linked,
    (view.blocks ?? []).map(b => [b.id, b.state, b.slots.map(s => [s.ref, s.status, s.updated_at])]),
  ]);
}
let baseline = null;
let checkTimer = null;
const updated = document.querySelector("[data-updated]");
async function check() {
  if (document.hidden) return;
  try {
    const response = await fetch("/valr/api/state", {credentials: "same-origin", cache: "no-store"});
    if (!response.ok) return;
    const now = signature(await response.json());
    if (baseline !== null && now !== baseline) {
      rememberOpen();
      location.reload();
      return;
    }
    baseline = now;
    if (updated) {
      updated.textContent = new Date().toLocaleTimeString("en-US", {hour: "numeric", minute: "2-digit", timeZone: "Asia/Kolkata"});
    }
  } catch {}
}
function scheduleCheck(delay) {
  clearTimeout(checkTimer);
  checkTimer = setTimeout(check, delay);
}
check();
setInterval(check, 60000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) check(); });

// The trend chart: single interviews as dots, the 3-interview average as the line,
// for the overall score, a dimension, or one skill within it (performance.json series).
const SVG = "http://www.w3.org/2000/svg";
function el(name, attrs = {}, parent) {
  const node = document.createElementNS(SVG, name);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  parent?.append(node);
  return node;
}
const source = document.querySelector("#perf-data");
const data = source ? JSON.parse(source.textContent) : null;
const BADGES = {improving: "improving", flat: "steady", declining: "slipping", insufficient_data: "too early to tell"};
let chosen = "overall";
function labelOf(key) {
  if (key === "overall") return "Overall";
  for (const d of data.dimensions) {
    if (d.key === key) return d.label;
    const sub = d.subs.find(x => x.key === key);
    if (sub) return `${d.label} · ${sub.label}`;
  }
  return key;
}
function drawChart() {
  const holder = document.querySelector("#perf-chart");
  if (!holder || !data) return;
  const s = data.series[chosen];
  const status = document.querySelector(".chart-status");
  if (!s || !s.points.length) {
    holder.replaceChildren();
    if (status) status.textContent = "No scores for this yet.";
    return;
  }
  const all = Object.values(data.series).flatMap(x => x.points.map(p => Date.parse(`${p.x}T00:00:00Z`)));
  const t0 = Math.min(...all);
  const t1 = Math.max(Math.max(...all), t0 + 86400000);
  const width = Math.max(holder.clientWidth, 280);
  const height = width < 560 ? 230 : 280;
  const pad = {l: 30, r: 14, t: 14, b: 30};
  const x = day => pad.l + ((Date.parse(`${day}T00:00:00Z`) - t0) / (t1 - t0)) * (width - pad.l - pad.r);
  const xm = ms => pad.l + ((ms - t0) / (t1 - t0)) * (width - pad.l - pad.r);
  const y = v => pad.t + ((10 - v) / 9) * (height - pad.t - pad.b);
  holder.replaceChildren();
  const svg = el("svg", {viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true"}, holder);
  const grid = el("g", {class: "grid axis"}, svg);
  for (const v of [1, 4, 7, 10]) {
    el("line", {x1: pad.l, x2: width - pad.r, y1: y(v), y2: y(v)}, grid);
    el("text", {x: pad.l - 10, y: y(v) + 3, "text-anchor": "end"}, grid).textContent = v;
  }
  const ticks = width < 560 ? 3 : 6;
  for (let i = 0; i < ticks; i++) {
    const ms = t0 + ((t1 - t0) * i) / (ticks - 1);
    const label = new Date(ms).toLocaleDateString("en-GB", {day: "numeric", month: "short", timeZone: "UTC"}).toUpperCase();
    el("text", {x: xm(ms), y: height - 8, "text-anchor": i === 0 ? "start" : i === ticks - 1 ? "end" : "middle"}, grid).textContent = label;
  }
  // Context: the overall average when a dimension is shown; the dimension when a skill is.
  const parent = chosen === "overall" ? null : chosen.includes(".") ? chosen.split(".")[0] : "overall";
  const context = parent && data.series[parent];
  if (context && context.points.length > 1) {
    el("path", {class: "ma faint", d: `M${context.points.map(p => `${x(p.x).toFixed(1)},${y(p.ma3).toFixed(1)}`).join("L")}`}, svg);
  }
  const points = s.points.map(p => [x(p.x), y(p.ma3)]);
  if (points.length > 1) {
    el("path", {class: "ma-area", d: `M${points.map(p => p.join(",")).join("L")}L${points.at(-1)[0]},${y(1)}L${points[0][0]},${y(1)}Z`}, svg);
    const line = el("path", {class: "ma", d: `M${points.map(p => p.join(",")).join("L")}`}, svg);
    if (!reduceMotion) {
      const length = line.getTotalLength();
      line.animate([{strokeDasharray: `${length}`, strokeDashoffset: length}, {strokeDasharray: `${length}`, strokeDashoffset: 0}],
        {duration: 800, easing: "ease-out"});
    }
  }
  let tip = holder.parentElement.querySelector(".chart-tip");
  if (!tip) {
    tip = document.createElement("div");
    tip.className = "chart-tip";
    holder.parentElement.append(tip);
  }
  tip.hidden = true;
  s.points.forEach(p => {
    el("circle", {class: "raw", cx: x(p.x), cy: y(p.y), r: 4}, svg);
    el("circle", {class: "ma-dot", cx: x(p.x), cy: y(p.ma3), r: 3.5}, svg);
    const hit = el("circle", {cx: x(p.x), cy: y(p.y), r: 14, fill: "transparent"}, svg);
    hit.addEventListener("pointerenter", () => {
      tip.textContent = `${p.company || "Interview"}${p.round ? ` · ${p.round}` : ""} · ${p.x} · ${p.y.toFixed(1)} (avg ${p.ma3.toFixed(1)})`;
      tip.hidden = false;
      const scale = holder.getBoundingClientRect().width / width;
      tip.style.left = `${Math.max(0, Math.min(x(p.x) * scale - 90, holder.clientWidth - 280))}px`;
      tip.style.top = `${y(p.y) * scale - 44}px`;
    });
    hit.addEventListener("pointerleave", () => { tip.hidden = true; });
  });
  if (status) {
    const change = s.change === null ? "" : `, ${s.change > 0 ? "+" : ""}${s.change.toFixed(1)} across the period`;
    status.textContent = `${labelOf(chosen)}: ${BADGES[s.badge] ?? s.badge}${change} · ${s.n} interview${s.n === 1 ? "" : "s"}`;
  }
}
const subPicker = document.querySelector(".picker.subs");
function showSubs(dim) {
  if (!subPicker) return;
  const d = data.dimensions.find(x => x.key === dim);
  subPicker.replaceChildren();
  subPicker.hidden = !d || !d.subs.length;
  if (!d) return;
  for (const sub of [{key: d.key, label: `All of ${d.label.toLowerCase()}`}, ...d.subs]) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "pick sub";
    button.dataset.dim = sub.key;
    button.setAttribute("aria-pressed", String(sub.key === chosen));
    button.textContent = sub.label;
    subPicker.append(button);
  }
}
document.addEventListener("click", event => {
  const button = event.target.closest(".pick");
  if (!button || !data) return;
  chosen = button.dataset.dim;
  if (!button.classList.contains("sub")) {
    document.querySelectorAll(".picker:not(.subs) .pick").forEach(b => b.setAttribute("aria-pressed", String(b === button)));
    showSubs(chosen);
  } else {
    subPicker.querySelectorAll(".pick").forEach(b => b.setAttribute("aria-pressed", String(b === button)));
  }
  drawChart();
});
drawChart();
// The chart lives under "The numbers behind this" (folded): draw it when it opens.
document.querySelector("details.numbers")?.addEventListener("toggle", event => { if (event.target.open) drawChart(); });
let resizeTimer = null;
addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(drawChart, 200);
});
