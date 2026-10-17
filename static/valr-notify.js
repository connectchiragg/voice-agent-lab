// The notification bell, the Telegram alert switches and the phone-recordings card.
//
// Bell (app/valr_ui/shell.bell): the count is unread notifications (new emails, call recordings
// and calendar changes only), asked for every 60 s (/valr/api/notifications?unread=1). Opening the panel loads the list, grouped
// by day (Today, Yesterday, Earlier), several items of one company in a day folded into one
// row. Opening does not mark anything read: clicking an item does (then it opens its page);
// "Mark all read" marks everything. Built with DOM calls only; pure parts are exported.
// The same rule holds for the server-rendered items (the panel's first list,
// app/valr_ui/shell.note_item): a red dot stays until that item is clicked. Every
// [data-notif-count] on the page (the bell's badge) shows one number.

export const POLL_MS = 60000;

export function groupByDay(items) {
  const days = [];
  for (const item of Array.isArray(items) ? items : []) {
    let day = days.find(d => d.day === item.day);
    if (!day) days.push(day = {day: item.day, rows: []});
    const key = item.company || `#${item.id}`;
    let row = day.rows.find(r => r.key === key);
    if (!row) day.rows.push(row = {key, company: item.company, items: []});
    row.items.push(item);
  }
  return days;
}

export function badgeCount(unread, actions) {
  const n = (Number(unread) || 0) + (Number(actions) || 0);
  return n > 99 ? "99+" : n ? String(n) : "";
}

export function safeHref(url, origin = "") {
  try {
    const parsed = new URL(url, origin || undefined);
    if (origin && parsed.origin === new URL(origin).origin) return parsed.pathname + parsed.search + parsed.hash;
    return parsed.protocol === "https:" ? parsed.href : "";
  } catch { return ""; }
}

// Where a message link opens: LinkedIn threads say so; everything else is Gmail.
export function mailSource(url) {
  try {
    const host = new URL(url).hostname.toLowerCase();
    return host === "linkedin.com" || host.endsWith(".linkedin.com") ? "LinkedIn" : "Gmail";
  } catch { return "Gmail"; }
}

function el(doc, tag, attrs = {}, text = "") {
  const node = doc.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text) node.textContent = text;
  return node;
}

export function ago(iso, now = Date.now()) {
  const minutes = Math.max(0, Math.round((now - Date.parse(iso)) / 60000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  return hours < 24 ? `${hours} h ago` : `${Math.round(hours / 24)} d ago`;
}

function itemNode(doc, item, onOpen, origin) {
  const li = el(doc, "li", {class: `notif-item${item.read ? "" : " unread"} notif-${item.importance} kind-${item.kind}`,
                            "data-notif-id": String(item.id)});
  const link = el(doc, "a", {class: "notif-main", href: safeHref(item.link, origin) || "/valr/leads"});
  const title = el(doc, "span", {class: "notif-title"}, item.title);
  if (!item.read) title.append(el(doc, "span", {class: "sr-only"}, " (unread)"));
  link.append(el(doc, "span", {class: "notif-dot", "aria-hidden": "true"}), title,
              el(doc, "span", {class: "notif-meta"}, [item.company, item.label, ago(item.created_at)].filter(Boolean).join(" · ")));
  if (item.body) link.append(el(doc, "span", {class: "notif-body"}, item.body));
  link.addEventListener("click", event => { event.preventDefault(); onOpen(item, link.getAttribute("href")); });
  li.append(link);
  const gmail = safeHref(item.gmail_url);
  if (gmail) {
    const a = el(doc, "a", {class: "notif-gmail", href: gmail, target: "_blank", rel: "noopener noreferrer"}, `Open in ${mailSource(gmail)}`);
    a.addEventListener("click", () => onOpen(item, null));
    li.append(a);
  }
  return li;
}

export function renderList(doc, box, items, onOpen, origin = "") {
  box.replaceChildren();
  const days = groupByDay(items);
  if (!days.length) { box.append(el(doc, "p", {class: "notif-empty"}, "You're all caught up.")); return; }
  for (const day of days) {
    box.append(el(doc, "p", {class: "notif-group"}, day.day));
    const list = el(doc, "ul", {class: "notif-items"});
    for (const row of day.rows) {
      if (row.items.length < 2) { list.append(itemNode(doc, row.items[0], onOpen, origin)); continue; }
      const li = el(doc, "li", {class: "notif-fold"});
      const fold = el(doc, "details");
      const unread = row.items.some(i => !i.read);
      fold.append(el(doc, "summary", {class: unread ? "unread" : ""}, `${row.company} · ${row.items.length} updates`));
      const inner = el(doc, "ul", {class: "notif-items"});
      row.items.forEach(i => inner.append(itemNode(doc, i, onOpen, origin)));
      fold.append(inner);
      li.append(fold);
      list.append(li);
    }
    box.append(list);
  }
}

const csrf = () => document.querySelector('meta[name="csrf-token"]')?.content ?? "";

async function api(url, body) {
  const response = await fetch(url, body === undefined ? {credentials: "same-origin"} : {
    method: "POST", credentials: "same-origin",
    headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf()}, body: JSON.stringify(body)});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  return data;
}

function toast(text) {
  const box = document.querySelector(".toast");
  if (!box) return;
  box.textContent = text;
  box.hidden = false;
  setTimeout(() => { box.hidden = true; }, 4000);
}

function startBell() {
  const bell = document.querySelector("[data-notif]");
  if (!bell) return;
  const box = bell.querySelector("[data-notif-list]");
  const show = unread => {
    const text = badgeCount(unread, 0);
    document.querySelectorAll("[data-notif-count]").forEach(node => { node.textContent = text; });
    bell.classList.toggle("has-unread", Boolean(text));
  };
  const cleared = id => document.querySelectorAll(`[data-notif-id="${CSS.escape(String(id))}"]`)
    .forEach(node => { node.classList.remove("unread"); node.querySelector(".sr-only")?.remove(); });
  const open = async (item, href) => {
    if (!item.read) {
      try { show((await api("/valr/api/notifications/read", {ids: [item.id]})).unread); } catch { /* still open it */ }
      item.read = true;
      cleared(item.id);
    }
    if (href) location.href = href;
  };
  // Server-rendered items (the panel before it loads): the same click rule.
  document.addEventListener("click", event => {
    const link = event.target.closest("[data-notif-item]");
    if (!link || link.closest("[data-notif-list]")?.dataset.loaded) return;
    const item = {id: Number(link.dataset.notifItem), read: !link.closest(".notif-item")?.classList.contains("unread")};
    if (link.hasAttribute("data-notif-stay")) { open(item, null); return; }
    event.preventDefault();
    open(item, link.getAttribute("href"));
  });
  document.addEventListener("click", event => {
    if (event.target.closest("[data-notif-close]")) { bell.open = false; bell.querySelector("summary")?.focus(); }
  });
  const load = async () => {
    try {
      const data = await api("/valr/api/notifications");
      show(data.unread);
      renderList(document, box, data.items, open, location.origin);
      box.dataset.loaded = "1";
    } catch (error) { box.replaceChildren(el(document, "p", {class: "muted small"}, `Couldn't load: ${error.message}`)); }
  };
  const poll = async () => {
    try { show((await api("/valr/api/notifications?unread=1")).unread); } catch { /* next time */ }
  };
  bell.addEventListener("toggle", () => { if (bell.open) load(); });
  bell.querySelector("[data-notif-all]")?.addEventListener("click", async () => {
    try { show((await api("/valr/api/notifications/read", {all: true})).unread); load(); } catch (error) { toast(error.message); }
  });
  poll();
  setInterval(() => { if (!document.hidden) poll(); }, POLL_MS);
}

function startSwitches() {
  const boxes = [...document.querySelectorAll("[data-notif-switch]")];
  if (!boxes.length) return;
  api("/valr/api/notifications").then(data => {
    for (const box of boxes) { box.checked = Boolean(data.switches?.[box.dataset.notifSwitch]); box.disabled = false; }
  }).catch(() => {});
  for (const box of boxes) {
    box.addEventListener("change", async () => {
      try {
        await api("/valr/api/notifications/settings", {[box.dataset.notifSwitch]: box.checked});
        toast("Saved.");
      } catch (error) { box.checked = !box.checked; toast(error.message); }
    });
  }
}

function startPhone() {
  const status = document.querySelector("[data-phone-status]");
  if (!status) return;
  const url = document.querySelector("[data-phone-url]");
  if (url) url.textContent = `${location.origin}/hooks/recording-upload`;
  const refresh = () => api("/valr/api/phone/status").then(data => {
    if (!data.token) status.textContent = "Not set up.";
    else if (!data.last_upload_at) status.textContent = "Ready. No recordings uploaded yet.";
    else {
      const when = new Date(data.last_upload_at).toLocaleString([], {dateStyle: "medium", timeStyle: "short"});
      const state = {done: "added to Past interviews", failed: "couldn't be transcribed",
                     uploaded: "waiting to be transcribed", processing: "being transcribed"}[data.last_status] || "";
      status.textContent = `Last upload: ${when}${state ? ` (${state})` : ""}.`;
    }
  }).catch(() => { status.textContent = "Status unavailable."; });
  refresh();
  const panel = document.querySelector("[data-phone-token]");
  const value = document.querySelector("[data-phone-token-value]");
  document.querySelectorAll("[data-phone-action]").forEach(button => button.addEventListener("click", async () => {
    const action = button.dataset.phoneAction;
    if (action === "rotate" && !confirm("Rotate the upload token? VALR for Mac and the iPhone Shortcut stop sending until you paste the new one into both.")) return;
    try {
      const data = await api("/valr/api/phone/token", {action});
      value.textContent = data.token;
      panel.hidden = false;
      refresh();
    } catch (error) { toast(error.message); }
  }));
  document.querySelector("[data-phone-copy]")?.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(value.textContent); toast("Copied."); } catch { toast("Select it and copy."); }
  });
}

if (typeof document !== "undefined" && typeof window !== "undefined") {
  startBell();
  startSwitches();
  startPhone();
}
