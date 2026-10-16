// The VALR app shell: light/dark toggle, old /valr#... links, the leads globe (valr-leads'
// static/leads-globe.js with the raw /valr/api/leads JSON; static/valr-globe-lite.js as the
// fallback), the lead peek on Home, instant lead search, the lead corrections and "Check Gmail
// now", and closing the attention popover. static/valr.js does the rest.

// Theme: system -> light -> dark -> system. Kept in this browser only.
const root = document.documentElement;
const THEME = "valr-theme";
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
const OLD_SECTIONS = {"#performance": "/valr/performance", "#upcoming": "/valr/interviews", "#setup": "/valr/settings",
                      "#notetakers": "/valr/settings#notetakers", "#system": "/valr/system"};
if (location.pathname === "/valr" || location.pathname === "/valr/") {
  const hash = location.hash;
  const slot = hash.match(/^#iv-(slot-[\w-]+)$/);
  const past = hash.match(/^#past-([A-Za-z0-9-]+)$/);
  if (slot) location.replace(`/valr/interviews/${slot[1]}`);
  else if (past) location.replace(`/valr/interviews/past/${past[1]}`);
  else if (OLD_SECTIONS[hash]) location.replace(OLD_SECTIONS[hash]);
}

// The attention popover: close on Escape or a click outside.
const popover = document.querySelector("details[data-popover]");
document.addEventListener("click", event => {
  if (popover?.open && !popover.contains(event.target)) popover.open = false;
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && popover?.open) {
    popover.open = false;
    popover.querySelector("summary")?.focus();
  }
});

// The globe: the real 3D one (async mount, raw API JSON, node = {type, id, lead|contact}),
// else the lite one (sync mount, normalised leads, onSelect(lead)).
const globe = document.querySelector("[data-globe]");
const source = document.querySelector("#leads-data");
const rawSource = document.querySelector("#leads-raw");
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
  const temp = Number.isFinite(lead.temperature) ? ` · ${lead.temperature}/100` : "";
  box.innerHTML = `<button class="peek-close" type="button" aria-label="Close">✕</button>
    <span class="label"><span class="sw st-${esc(lead.stage)}" aria-hidden="true"></span> ${esc(lead.stage_label || lead.stage)}${esc(temp)}</span>
    <span class="peek-title">${esc(lead.company)}</span>
    <span class="muted small">${esc(lead.role)}</span>
    ${lead.next_step ? `<span class="small">Next: ${esc(lead.next_step)}</span>` : ""}
    <a class="hero-link mono" href="/valr/leads?lead=${encodeURIComponent(lead.id)}#lead">Open lead →</a>`;
  box.hidden = false;
  box.querySelector(".peek-close").addEventListener("click", () => { box.hidden = true; });
}
async function mountGlobe() {
  if (!globe || !source) return;
  const data = json(source);
  const raw = json(rawSource);
  if (!data) return;
  const byId = new Map((data.leads || []).map(l => [l.id, l]));
  const onLeads = globe.dataset.mode === "leads";
  const selected = new URLSearchParams(location.search).get("lead");
  function choose(id) {
    if (!id) return;
    if (onLeads) {
      const params = new URLSearchParams(location.search);
      params.set("lead", id);
      location.assign(`/valr/leads?${params}#lead`);
    } else {
      peek(byId.get(id));
    }
  }
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const phone = matchMedia("(max-width: 640px)").matches;
  if (raw?.leads?.length && /leads-globe\.js/.test(globe.dataset.src || "")) {
    try {
      const module = await import(globe.dataset.src);
      const view = await module.mount(globe, raw, {
        onSelect(node) {
          if (node?.type === "lead") choose(node.id);
          else if (node?.type === "contact" && node.contact?.leads?.length) choose(node.contact.leads[0]);
        },
        maxNodes: phone ? 40 : 120,
        // Home's hero copy sits over the globe: no labels there on a phone (a tap shows the lead).
        labels: phone ? (onLeads ? 6 : 0) : (onLeads ? 24 : 12), reducedMotion: reduced,
      });
      if (view?.supported !== false) {
        if (selected) { view.select?.(selected); view.focus?.(selected); }
        globe.dataset.globeKind = "3d";
        return;
      }
      view?.destroy?.();
    } catch (error) {
      console.warn("3D globe unavailable, using the light one:", error?.message || error);
      globe.querySelectorAll("canvas, .globe-labels").forEach(node => node.remove());
    }
  }
  try {
    const module = await import(globe.dataset.fallback || "/static/valr-globe-lite.js");
    const view = module.mount(globe, data, {onSelect(lead) { choose(lead?.id); }});
    globe.dataset.globeKind = "lite";
    if (selected) view?.select?.(selected);
  } catch {}
}
mountGlobe();

// Leads: the owner's corrections (stage, merge, dismiss) and "Check Gmail now".
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
document.addEventListener("click", async event => {
  const fix = event.target.closest("[data-fix]");
  const sync = event.target.closest("[data-leads-sync]");
  if (!fix && !sync) return;
  const button = fix || sync;
  button.setAttribute("aria-busy", "true");
  button.disabled = true;
  try {
    if (sync) {
      toast((await postJson("/valr/api/leads/sync", {})).message || "Gmail sync started.");
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

// Leads: filter the rows as you type (the form still works without JavaScript).
const search = document.querySelector("[data-lead-search]");
search?.addEventListener("input", () => {
  const q = search.value.trim().toLowerCase();
  document.querySelectorAll("[data-lead-row]").forEach(row => {
    row.hidden = Boolean(q) && !row.textContent.toLowerCase().includes(q);
  });
});

// Interviews: "Set company" (the owner's name for it always wins over the resolver's).
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
