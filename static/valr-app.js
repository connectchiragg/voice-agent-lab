// The VALR app shell: light/dark toggle, old #iv-<ref> links, the leads globe (data-src:
// the leads branch's static/leads-globe.js when deployed, else valr-globe-lite.js), the lead peek on
// Home, instant lead search, and closing the attention popover. static/valr.js does the rest.

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

// Links from before the redesign: /valr#iv-slot-... -> /valr/interviews/slot-...
const legacy = location.pathname === "/valr" && location.hash.match(/^#iv-(slot-[\w-]+)$/);
if (legacy) location.replace(`/valr/interviews/${legacy[1]}`);

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

// The globe.
const globe = document.querySelector("[data-globe]");
const source = document.querySelector("#leads-data");
function esc(text) {
  const node = document.createElement("span");
  node.textContent = text ?? "";
  return node.innerHTML;
}
function peek(lead) {
  const box = document.querySelector("[data-lead-peek]");
  if (!box) return;
  const stage = lead.stage.charAt(0).toUpperCase() + lead.stage.slice(1);
  box.innerHTML = `<button class="peek-close" type="button" aria-label="Close">✕</button>
    <span class="label"><span class="sw st-${esc(lead.stage)}" aria-hidden="true"></span> ${esc(stage)}</span>
    <span class="peek-title">${esc(lead.company)}</span>
    <span class="muted small">${esc(lead.role)}</span>
    ${lead.next_step ? `<span class="small">Next: ${esc(lead.next_step)}</span>` : ""}
    <a class="hero-link mono" href="/valr/leads?lead=${encodeURIComponent(lead.id)}#lead">Open lead →</a>`;
  box.hidden = false;
  box.querySelector(".peek-close").addEventListener("click", () => { box.hidden = true; });
}
async function mountGlobe() {
  if (!globe || !source) return;
  let data;
  try { data = JSON.parse(source.textContent); } catch { return; }
  let module;
  try {
    module = await import(globe.dataset.src || "/static/valr-globe-lite.js");
  } catch {
    module = await import("/static/valr-globe-lite.js");
  }
  const onLeads = globe.dataset.mode === "leads";
  const selected = new URLSearchParams(location.search).get("lead");
  const view = module.mount(globe, data, {
    onSelect(lead) {
      if (onLeads) {
        const params = new URLSearchParams(location.search);
        params.set("lead", lead.id);
        location.assign(`/valr/leads?${params}#lead`);
      } else {
        peek(lead);
      }
    },
  });
  if (selected) view?.select?.(selected);
}
mountGlobe();

// Leads: filter the rows as you type (the form still works without JavaScript).
const search = document.querySelector("[data-lead-search]");
search?.addEventListener("input", () => {
  const q = search.value.trim().toLowerCase();
  document.querySelectorAll("[data-lead-row]").forEach(row => {
    row.hidden = Boolean(q) && !row.textContent.toLowerCase().includes(q);
  });
});
