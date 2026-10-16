// The VALR app shell: light/dark, old /valr#... links, menus and sheets, the leads globe
// (valr-leads' static/leads-globe.js with the raw /valr/api/leads JSON; static/valr-globe-lite.js
// as the fallback) with its lead peek on Home, the Leads filters and corrections, "Check email
// now", "Fix company" and "Hear the briefing". static/valr.js does the rest.

const root = document.documentElement;
const THEME = "valr-theme";
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

// Theme: system -> the other one -> back to system. Kept in this browser only.
document.querySelector("[data-theme-toggle]")?.addEventListener("click", () => {
  const system = matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  const now = root.dataset.theme || system;
  const next = now === "light" ? "dark" : "light";
  if (next === system) {
    delete root.dataset.theme;
    try { localStorage.removeItem(THEME); } catch {}
  } else {
    root.dataset.theme = next;
    try { localStorage.setItem(THEME, next); } catch {}
  }
});

// Links from before the redesign: /valr#iv-slot-... -> /valr/interviews/slot-..., the
// scorecard anchors in leads and Telegram post-call reports (/valr#past-<provider>-<id>) ->
// /valr/interviews/past/<provider>-<id>, and the old sections -> their pages.
const OLD_SECTIONS = {"#performance": "/valr/progress", "#upcoming": "/valr/interviews", "#setup": "/valr/settings",
                      "#notetakers": "/valr/settings#notetakers", "#system": "/valr/settings#advanced"};
if (location.pathname === "/valr" || location.pathname === "/valr/") {
  const hash = location.hash;
  const slot = hash.match(/^#iv-(slot-[\w-]+)$/);
  const past = hash.match(/^#past-([A-Za-z0-9-]+)$/);
  if (slot) location.replace(`/valr/interviews/${slot[1]}`);
  else if (past) location.replace(`/valr/interviews/past/${past[1]}`);
  else if (OLD_SECTIONS[hash]) location.replace(OLD_SECTIONS[hash]);
}
if (location.hash === "#advanced") document.getElementById("advanced")?.setAttribute("open", "");

// Menus and the bell (<details>): one open at a time; Escape or a click outside closes them.
const popovers = () => document.querySelectorAll("details[data-menu][open], details[data-popover][open]");
document.addEventListener("click", event => {
  popovers().forEach(box => { if (!box.contains(event.target)) box.open = false; });
  const item = event.target.closest(".menu-item");
  if (item && !item.matches("[data-sheet]")) setTimeout(() => { const box = item.closest("details"); if (box) box.open = false; }, 0);
});
document.addEventListener("toggle", event => {
  const box = event.target;
  if (box.open && box.matches?.("details[data-menu], details[data-popover]")) {
    popovers().forEach(other => { if (other !== box && !other.contains(box)) other.open = false; });
  }
}, true);
document.addEventListener("keydown", event => {
  if (event.key !== "Escape") return;
  popovers().forEach(box => { box.open = false; box.querySelector("summary")?.focus(); });
});

// Sheets: [data-sheet="<dialog id>"] opens it (and [data-focus] focuses a field inside).
document.addEventListener("click", event => {
  const opener = event.target.closest("[data-sheet]");
  if (opener) {
    const dialog = document.getElementById(opener.dataset.sheet);
    if (!dialog) return;
    opener.closest("details")?.removeAttribute("open");
    dialog.showModal();
    const target = opener.dataset.focus && dialog.querySelector(opener.dataset.focus);
    setTimeout(() => (target || dialog.querySelector("input, select, button:not([data-sheet-close])"))?.focus(), 30);
    return;
  }
  if (event.target.closest("[data-sheet-close]")) event.target.closest("dialog")?.close();
  else if (event.target.matches("dialog.sheet")) event.target.close();  // The backdrop.
});

// Filters that apply themselves (the form still works without JavaScript).
document.addEventListener("change", event => {
  if (event.target.matches("[data-autosubmit]")) event.target.form?.submit();
});

// Hear the briefing: the written script, read aloud by the browser's own voice.
document.addEventListener("click", event => {
  const button = event.target.closest("[data-hear]");
  if (!button) return;
  const label = button.querySelector("span");
  if (!("speechSynthesis" in window)) { toast("This browser can't read aloud. Use Call me instead."); return; }
  if (speechSynthesis.speaking) { speechSynthesis.cancel(); if (label) label.textContent = "Hear the briefing"; return; }
  const text = document.getElementById(button.dataset.hear)?.content?.textContent?.trim();
  if (!text) return;
  const say = new SpeechSynthesisUtterance(text);
  say.lang = "en-IN";
  say.onend = () => { if (label) label.textContent = "Hear the briefing"; };
  speechSynthesis.speak(say);
  if (label) label.textContent = "Stop reading";
});

// The globe: the real 3D one (async mount, raw API JSON, node = {type, id, lead}), else the
// lite one (sync mount, normalised leads, onSelect(lead)).
const globe = document.querySelector("[data-globe]");
const source = document.querySelector("#leads-data");
const rawSource = document.querySelector("#leads-raw");
const BAND = {hot: "Hot", warm: "Warm", cool: "Cool", cold: "Cold", closed: "Closed"};
function esc(text) {
  const node = document.createElement("span");
  node.textContent = text ?? "";
  return node.innerHTML;
}
function json(el) {
  try { return el ? JSON.parse(el.textContent) : null; } catch { return null; }
}
function peek(lead) {
  const box = document.querySelector("[data-lead-peek]");
  if (!box || !lead) return;
  const band = BAND[lead.band] || "";
  box.innerHTML = `<button class="icon-btn peek-close" type="button" aria-label="Close">×</button>
    <span class="peek-meta"><span class="band-sw b-${esc(lead.band)}" aria-hidden="true"></span>${esc(band)} · ${esc(lead.stage_label || lead.stage)}</span>
    <span class="peek-title">${esc(lead.company)}</span>
    ${lead.role ? `<span class="peek-sub">${esc(lead.role)}</span>` : ""}
    <a class="btn sm on-stage" href="/valr/leads?lead=${encodeURIComponent(lead.id)}#lead">Open lead</a>`;
  box.hidden = false;
  box.querySelector(".peek-close").addEventListener("click", () => { box.hidden = true; });
}
async function mountGlobe() {
  if (!globe || !source) return;
  const data = json(source);
  const raw = json(rawSource);
  if (!data) return;
  const byId = new Map((data.leads || []).map(l => [l.id, l]));
  function choose(id) { if (id) peek(byId.get(id)); }
  // Closed leads are drawn only while "Show closed" is ticked.
  const toggle = document.querySelector("[data-globe-closed]");
  const shown = () => ({leads: (raw?.leads || []).filter(l => l.status !== "closed" || toggle?.checked)});
  for (const lead of (raw?.leads || []).filter(l => l.status === "closed")) byId.set(lead.id, {...lead, role: ""});
  if (raw?.leads?.length && /leads-globe\.js/.test(globe.dataset.src || "")) {
    try {
      const module = await import(globe.dataset.src);
      const view = await module.mount(globe, shown(), {
        onSelect(node) { if (node?.type === "lead") choose(node.id); },
        maxNodes: 150, labels: 150, reducedMotion,
      });
      if (view?.supported !== false) {
        toggle?.addEventListener("change", () => view.update(shown()));
        // Pointing at a "Your move" row turns the globe to that lead.
        document.querySelectorAll(".move-main").forEach(row => {
          const id = new URL(row.href).searchParams.get("lead");
          row.addEventListener("mouseenter", () => view.hover?.(id));
          row.addEventListener("focus", () => view.hover?.(id));
          row.addEventListener("mouseleave", () => view.hover?.(null));
        });
        globe.dataset.globeKind = "3d";
        return;
      }
      view?.destroy?.();
    } catch (error) {
      console.warn("3D globe unavailable, using the light one:", error?.message || error);
      globe.querySelectorAll("canvas, .globe-labels, .globe-tip").forEach(node => node.remove());
    }
  }
  try {
    const module = await import(globe.dataset.fallback || "/static/valr-globe-lite.js");
    module.mount(globe, data, {onSelect(lead) { choose(lead?.id); }});
    globe.dataset.globeKind = "lite";
  } catch {}
}
mountGlobe();

// Toasts and posts (with the page's CSRF token).
const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content ?? "";
function toast(text) {
  const box = document.querySelector(".toast");
  if (!box) return;
  box.textContent = text;
  box.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { box.hidden = true; }, 4000);
}
async function postJson(path, body) {
  const response = await fetch(path, {method: "POST", credentials: "same-origin",
    headers: {"Content-Type": "application/json", "X-CSRF-Token": csrfToken}, body: JSON.stringify(body)});
  const reply = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof reply.detail === "string" ? reply.detail : `Not saved (HTTP ${response.status}).`);
  return reply;
}

// Leads: the owner's corrections (company, stage, merge, dismiss) and "Check email now".
document.addEventListener("click", async event => {
  const fix = event.target.closest("[data-fix]");
  const sync = event.target.closest("[data-leads-sync]");
  if (!fix && !sync) return;
  const button = fix || sync;
  button.setAttribute("aria-busy", "true");
  button.disabled = true;
  try {
    if (sync) {
      toast((await postJson("/valr/api/leads/sync", {})).message || "Checking your email.");
      return;
    }
    const box = fix.closest("[data-lead-fix]");
    const id = box.dataset.leadFix;
    const body = {action: fix.dataset.fix};
    if (body.action === "stage") body.stage = box.querySelector("[data-fix-stage]").value;
    if (body.action === "merge") body.into = box.querySelector("[data-fix-into]").value;
    if (body.action === "rename") body.company = box.querySelector("[data-fix-company]").value.trim();
    if (body.action === "dismiss" && !confirm(`Dismiss ${box.dataset.company}? It stays hidden, with its email threads.`)) return;
    const reply = await postJson(`/valr/api/leads/${encodeURIComponent(id)}/override`, body);
    toast(reply.message || "Saved.");
    const params = new URLSearchParams(location.search);
    if (body.action === "dismiss") params.delete("lead");
    if (body.action === "merge") params.set("lead", body.into);
    setTimeout(() => location.assign(`/valr/leads?${params}${params.get("lead") ? "#lead" : ""}`), 700);
  } catch (error) {
    toast(error.message);
  } finally {
    button.removeAttribute("aria-busy");
    button.disabled = false;
  }
});

// Merge: filter the company list as you type.
document.querySelector("[data-merge-search]")?.addEventListener("input", event => {
  const q = event.target.value.trim().toLowerCase();
  const select = event.target.closest("[data-lead-fix]")?.querySelector("[data-fix-into]");
  if (!select) return;
  let first = null;
  for (const option of select.options) {
    option.hidden = Boolean(q) && !option.textContent.toLowerCase().includes(q);
    if (!option.hidden && !first) first = option;
  }
  if (first) select.value = first.value;
});

// Leads: filter the rows as you type (the form still works without JavaScript).
const search = document.querySelector("[data-lead-search]");
search?.addEventListener("input", () => {
  const q = search.value.trim().toLowerCase();
  document.querySelectorAll("[data-lead-row]").forEach(row => {
    row.hidden = Boolean(q) && !row.textContent.toLowerCase().includes(q);
  });
});

// "Fix company" on an interview (the owner's name for it always wins over the resolver's).
document.addEventListener("click", async event => {
  const save = event.target.closest("[data-company-save]");
  if (!save) return;
  const box = save.closest("[data-company-fix]");
  save.disabled = true;
  try {
    const reply = await postJson("/valr/api/interviews/company",
      {id: box.dataset.companyFix, company: box.querySelector("[data-company-input]").value.trim()});
    toast(reply.message || "Saved.");
    setTimeout(() => location.reload(), 700);
  } catch (error) {
    toast(error.message);
  } finally {
    save.disabled = false;
  }
});
