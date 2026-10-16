// /valr/leads: filters, the lead panel, the list, and the globe (static/leads-globe.js).
import { mount, tempColor } from "/static/leads-globe.js";

const STAGE = { sourced: "Sourced", applied: "Applied", screening: "Screening", interviewing: "Interviewing",
  offer: "Offer", "closed-won": "Won", "closed-lost": "Lost", ghosted: "Ghosted" };
const KIND = { email: "Email", calendar: "Interview", recording: "Recording", evaluation: "Scored" };
const ROLE = { recruiter: "Recruiter", hiring_manager: "Hiring manager", interviewer: "Interviewer", coordinator: "Coordinator", other: "Contact" };
const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const when = (iso) => iso ? new Date(iso).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "numeric", minute: "2-digit", timeZone: zone }) : "–";
const ago = (iso) => {
  if (!iso) return "";
  const days = Math.round((Date.now() - new Date(iso)) / 864e5);
  return days < 0 ? `in ${-days} d` : days === 0 ? "today" : `${days} d ago`;
};
const rgb = (t) => `rgb(${tempColor(t).join(",")})`;

let data = JSON.parse($("#leads-data").textContent);
const filters = { status: "active", stage: "", tmin: 0 };
let globe = null;

function filtered() {
  const leads = data.leads.filter((l) => (filters.status === "all" || l.status === filters.status)
    && (!filters.stage || l.stage === filters.stage) && l.temperature >= filters.tmin);
  const ids = new Set(leads.map((l) => l.id));
  const contacts = data.contacts.map((c) => ({ ...c, leads: c.leads.filter((id) => ids.has(id)) })).filter((c) => c.leads.length);
  const cids = new Set(contacts.map((c) => c.id));
  const links = data.links.filter((k) => ids.has(k.source) && (ids.has(k.target) || cids.has(k.target)));
  return { ...data, leads, contacts, links };
}

function part(name, p) {
  const share = p.max ? Math.max(0, Math.min(1, p.points / p.max)) : 0;
  return `<li><span class="tp-name">${esc(name)}</span><span class="tp-bar"><i style="width:${(share * 100).toFixed(0)}%"></i></span>`
    + `<span class="tp-val num">${p.points}<span class="muted">/${p.max}</span></span><span class="tp-why muted small">${esc(p.why)}</span></li>`;
}

function panel(lead) {
  const el = $("#lead-panel");
  if (!lead) { el.hidden = true; el.innerHTML = ""; return; }
  const p = lead.parts || {};
  const parts = ["recency", "stage", "momentum", "quality"].filter((k) => p[k]).map((k) => part(k[0].toUpperCase() + k.slice(1), p[k])).join("");
  const cap = p.cap ? `<p class="muted small">Capped at ${p.cap} while ${esc(STAGE[lead.stage] || lead.stage)}.</p>` : "";
  const contacts = lead.contacts.map((c) => `<li><span>${esc(c.name || c.email)}</span><span class="mono small muted">${esc(ROLE[c.role] || c.role)}</span>`
    + (c.email ? `<a class="small email" href="mailto:${esc(c.email)}">${esc(c.email)}</a>` : "") + "</li>").join("") || '<li class="muted small">No people yet.</li>';
  const timeline = lead.timeline.map((t) => {
    const links = [t.url ? `<a class="src" href="${esc(t.url)}" target="_blank" rel="noopener noreferrer">${t.kind === "email" ? "Gmail" : "Recording"} ↗</a>` : "",
      t.detail_url ? `<a class="src" href="${esc(t.detail_url)}">Interview detail</a>` : ""].filter(Boolean).join(" ");
    const cue = t.cue !== "none" ? `<span class="chip tone-${t.cue === "positive" ? "ok" : "bad"}">${t.cue}</span>` : "";
    return `<li class="tl tl-${esc(t.kind)}"><p class="mono label">${esc(KIND[t.kind] || t.kind)}${t.direction === "out" ? " · you" : ""} · ${esc(when(t.at))}</p>`
      + `<p class="tl-title">${esc(t.title)} ${cue}</p>${t.summary ? `<p class="small muted">${esc(t.summary)}</p>` : ""}${links ? `<p class="small">${links}</p>` : ""}</li>`;
  }).join("");
  const next = lead.next_step ? `<p class="next"><span class="mono label">Next step</span> ${esc(lead.next_step)}${lead.next_step_at ? ` · ${esc(when(lead.next_step_at))}` : ""}</p>` : "";
  el.innerHTML = `
    <button class="panel-close" type="button" aria-label="Close">×</button>
    <p class="mono label">${esc(STAGE[lead.stage] || lead.stage)}${lead.status === "expired" ? " · expired" : ""} · last touch ${esc(ago(lead.last_touch_at))}</p>
    <h2 class="panel-title">${esc(lead.company)}</h2>
    <p class="muted">${esc(lead.role || "Role unknown")}${lead.domain ? ` · ${esc(lead.domain)}` : ""}</p>
    <div class="temp-big"><span class="num" style="color:${rgb(lead.temperature)}">${lead.temperature}</span><span class="muted small">/100 temperature</span></div>
    ${next}
    <h3 class="sub-title">Why this temperature</h3><ul class="temp-parts">${parts}</ul>${cap}
    <h3 class="sub-title">People</h3><ul class="people">${contacts}</ul>
    <h3 class="sub-title">Timeline</h3><ol class="timeline">${timeline}</ol>
    ${corrections(lead)}`;
  el.hidden = false;
  bindCorrections(el, lead);
  $(".panel-close", el).addEventListener("click", () => { panel(null); globe && globe.select(null); });
}

function corrections(lead) {
  const stages = data.stages.map((s) => `<option value="${esc(s)}"${lead.stage_manual && lead.stage === s ? " selected" : ""}>${esc(STAGE[s] || s)}</option>`).join("");
  const others = data.leads.filter((l) => l.id !== lead.id).sort((a, b) => a.company.localeCompare(b.company))
    .map((l) => `<option value="${esc(l.id)}">${esc(l.company)}</option>`).join("");
  return `<details class="fix"><summary class="mono label">Correct this lead</summary>
    <div class="fix-row"><select data-fix-stage aria-label="Stage"><option value=""${lead.stage_manual ? "" : " selected"}>Automatic stage</option>${stages}</select>
      <button class="btn small" type="button" data-fix="stage">Set stage</button></div>
    ${others ? `<div class="fix-row"><select data-fix-into aria-label="Merge into">${others}</select>
      <button class="btn small" type="button" data-fix="merge">Merge into</button></div>` : ""}
    <div class="fix-row"><button class="btn small danger" type="button" data-fix="dismiss">Dismiss lead</button>
      <span class="muted small">Hidden for good, with its email threads.</span></div></details>`;
}

function bindCorrections(el, lead) {
  for (const button of el.querySelectorAll("[data-fix]")) {
    button.addEventListener("click", async () => {
      const action = button.dataset.fix;
      const body = { action };
      if (action === "stage") body.stage = $("[data-fix-stage]", el).value;
      if (action === "merge") body.into = $("[data-fix-into]", el).value;
      if (action === "dismiss" && !confirm(`Dismiss ${lead.company}? It stays hidden, with its email threads.`)) return;
      button.setAttribute("aria-busy", "true");
      try {
        const response = await fetch(`/valr/api/leads/${encodeURIComponent(lead.id)}/override`, { method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": $("meta[name=csrf-token]").content }, body: JSON.stringify(body) });
        const reply = await response.json().catch(() => ({}));
        toast(reply.message || reply.detail || (response.ok ? "Saved." : "Not saved."));
        if (response.ok) {
          await refresh();
          const next = data.leads.find((l) => l.id === (action === "merge" ? body.into : lead.id));
          panel(action === "dismiss" ? null : next || null);
          if (globe) globe.select(next && action !== "dismiss" ? next.id : null);
        }
      } finally { button.removeAttribute("aria-busy"); }
    });
  }
}

function contactPanel(contact) {
  const el = $("#lead-panel");
  const leads = data.leads.filter((l) => contact.leads.includes(l.id));
  el.innerHTML = `<button class="panel-close" type="button" aria-label="Close">×</button>
    <p class="mono label">${esc(ROLE[contact.role] || "Contact")}</p><h2 class="panel-title">${esc(contact.name)}</h2>
    <h3 class="sub-title">Leads</h3><ul class="leads-list">${leads.map(row).join("")}</ul>`;
  el.hidden = false;
  $(".panel-close", el).addEventListener("click", () => { panel(null); globe && globe.select(null); });
  bindRows(el);
}

function row(lead) {
  return `<li class="lead-row" data-status="${esc(lead.status)}"><button type="button" class="lead-btn" data-lead="${esc(lead.id)}">`
    + `<span class="lt-dot" style="background:${rgb(lead.temperature)}"></span><span class="lr-main"><span class="lr-company">${esc(lead.company)}</span>`
    + `<span class="lr-role muted small">${esc(lead.role || "Role unknown")}</span></span><span class="lr-stage mono">${esc(STAGE[lead.stage] || lead.stage)}</span>`
    + `<span class="lr-temp num">${lead.temperature}</span></button></li>`;
}

function bindRows(root) {
  for (const button of root.querySelectorAll("[data-lead]")) {
    button.addEventListener("click", () => {
      const lead = data.leads.find((l) => l.id === button.dataset.lead);
      panel(lead);
      if (globe) { globe.select(lead.id); globe.focus(lead.id); }
      $("#lead-panel").scrollIntoView({ block: "nearest", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    });
  }
}

function render() {
  const view = filtered();
  const list = $("#leads-list");
  list.innerHTML = view.leads.map(row).join("") || '<li class="muted small lead-empty">No leads match these filters.</li>';
  bindRows(list);
  if (globe) globe.update(view);
}

function toast(text) {
  const el = $(".toast");
  el.textContent = text; el.hidden = false;
  clearTimeout(toast.t); toast.t = setTimeout(() => { el.hidden = true; }, 4000);
}

function wire() {
  const form = $(".leads-filters");
  for (const button of form.querySelectorAll("[data-status]")) {
    button.addEventListener("click", () => {
      filters.status = button.dataset.status;
      form.querySelectorAll("[data-status]").forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
      render();
    });
  }
  form.stage.addEventListener("change", () => { filters.stage = form.stage.value; render(); });
  form.tmin.addEventListener("input", () => { filters.tmin = Number(form.tmin.value); form.tval.value = form.tmin.value; render(); });
  $("[data-action=leads-sync]").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.setAttribute("aria-busy", "true");
    try {
      const response = await fetch("/valr/api/leads/sync", { method: "POST", headers: { "Content-Type": "application/json",
        "X-CSRF-Token": $("meta[name=csrf-token]").content }, body: "{}" });
      const body = await response.json().catch(() => ({}));
      toast(body.message || body.detail || (response.ok ? "Started." : "Could not start the sync."));
      if (response.ok) setTimeout(refresh, 20000);
    } finally { button.removeAttribute("aria-busy"); }
  });
}

async function refresh() {
  try {
    const response = await fetch("/valr/api/leads", { headers: { Accept: "application/json" } });
    if (response.ok) { data = await response.json(); render(); }
  } catch { /* Keep what is shown. */ }
}

async function start() {
  wire();
  render();
  const el = $("#leads-globe");
  if (matchMedia("(max-width: 420px)").matches && !location.search.includes("globe=1")) {
    el.classList.add("globe-collapsed");  // Phones: the list first; the globe on demand.
    const show = document.createElement("button");
    show.className = "btn small globe-show"; show.type = "button"; show.textContent = "Show the globe";
    el.append(show);
    await new Promise((resolve) => show.addEventListener("click", () => { show.remove(); el.classList.remove("globe-collapsed"); resolve(); }, { once: true }));
  }
  try {
    globe = await mount(el, filtered(), {
      onSelect(node) {
        if (!node) panel(null);
        else if (node.type === "lead") panel(node.lead);
        else contactPanel(node.contact);
      },
    });
    if (!globe.supported) el.insertAdjacentHTML("beforeend", '<p class="muted small globe-note">This browser has no WebGL: the list below has every lead.</p>');
    else if (globe.hidden) el.insertAdjacentHTML("beforeend", `<p class="muted small globe-note">Showing the hottest leads; ${globe.hidden} more in the list.</p>`);
  } catch (error) {
    el.classList.add("globe-unsupported");
    el.insertAdjacentHTML("beforeend", '<p class="muted small globe-note">The globe could not load; the list below has every lead.</p>');
  }
  setInterval(() => { if (!document.hidden) refresh(); }, 120000);
}

start();
