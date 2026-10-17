// The VALR pages' behaviour (every page): countdowns, the (legacy) interview drawer, the owner's actions (POST with the
// page's CSRF token), the prep checklists, the focus ticks and a quiet refresh.
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
const BUSY = {sync: "Syncing…", refresh: "Starting…", dials: "Saving…", callnow: "Calling…", ntsync: "Syncing…",
              ntdisconnect: "Disconnecting…", reach: "Saving…"};

document.addEventListener("click", async event => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  if (action === "reload") {
    location.reload();
    return;
  }
  const label = button.innerHTML;  // Icons included.
  button.setAttribute("aria-busy", "true");
  button.disabled = true;
  const words = button.querySelector(".split-label, span:not(.sr-only)") || button;
  if (BUSY[action]) words.textContent = BUSY[action];
  try {
    let result;
    if (action === "sync") {
      result = await post("/valr/api/sync");
    } else if (action === "callnow") {
      result = await post("/valr/api/call-now");
    } else if (action === "refresh") {
      result = await post(`/valr/api/slots/${encodeURIComponent(button.dataset.ref)}/refresh`);
    } else if (action === "ntsync") {
      result = await post(`/valr/api/notetakers/${encodeURIComponent(button.dataset.provider)}/sync`);
    } else if (action === "ntdisconnect") {
      result = await post(`/valr/api/notetakers/${encodeURIComponent(button.dataset.provider)}/disconnect`);
      setTimeout(() => location.reload(), 800);
    } else if (action === "reach") {
      result = await post("/valr/api/reach", {mode: button.dataset.mode});
      setTimeout(() => location.reload(), 600);
    } else if (action === "dials") {
      const days = Number(button.dataset.days) || 0;  // Pause for 1 or 7 days; none: until resumed.
      result = await post("/valr/api/dials", {paused: button.dataset.paused === "true", ...(days ? {days} : {})});
      setTimeout(() => { rememberOpen(); location.reload(); }, 600);
    }
    say(result?.message ?? "Done.");
    scheduleCheck(4000);
  } catch (error) {
    say(error.message, false);
  } finally {
    button.removeAttribute("aria-busy");
    button.disabled = false;
    button.innerHTML = label;
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
    view.dials_paused?.enabled, view.next_call?.at, view.jobs?.[0]?.status, view.connections?.push?.devices?.length,
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

// This week's focus (Progress): ticks are remembered on this device.
for (const box of document.querySelectorAll("input[data-focus]")) {
  const key = `valr-focus:${box.dataset.focus}`;
  try { box.checked = localStorage.getItem(key) === "1"; } catch {}
  box.addEventListener("change", () => {
    try { box.checked ? localStorage.setItem(key, "1") : localStorage.removeItem(key); } catch {}
  });
}
