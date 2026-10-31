// Settings › API keys (app/valr_ui/keys.py, app/user_keys_web.py): a workspace's own LLM and
// Deepgram keys. Loads the saved summaries, then Save (checked by the server first), Test and
// Remove. The key field is cleared after a save and the key is never shown again. Every POST
// carries the page's CSRF token. Pure parts are exported for tests/valr-keys.test.mjs.

const STATUS = {ok: "Working", rejected: "Rejected", unchecked: "Not checked yet"};

// "OpenAI · gpt-6-luna · key ending …1234", or what to do when there is none.
export function describe(row) {
  if (!row) return "No key yet.";
  const parts = [row.label || row.provider];
  if (row.model) parts.push(row.model);
  if (row.last4) parts.push(`key ending …${row.last4}`);
  return parts.join(" · ");
}

export function statusLabel(row) {
  return row ? (STATUS[row.status] || row.status) : "";
}

const csrf = () => document.querySelector('meta[name="csrf-token"]')?.content ?? "";

async function api(url, body) {
  const response = await fetch(url, body === undefined ? {credentials: "same-origin"} : {
    method: "POST", credentials: "same-origin",
    headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf()}, body: JSON.stringify(body)});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Something went wrong (HTTP ${response.status}).`);
  return data;
}

function toast(text, ok = true) {
  const box = document.querySelector(".toast");
  if (!box) return;
  box.textContent = text;
  box.classList.toggle("bad", !ok);
  box.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { box.hidden = true; }, 5000);
}

function fill(form, row) {
  const status = form.querySelector("[data-keys-status]");
  status.textContent = row ? `${describe(row)} — ${statusLabel(row)}` : describe(row);
  status.classList.toggle("bad", row?.status === "rejected");
  form.querySelector("[data-keys-test]").hidden = !row;
  form.querySelector("[data-keys-remove]").hidden = !row;
  if (row) form.querySelector("[data-keys-provider]").value = row.provider;
  const model = form.querySelector("[data-keys-model]");
  if (model && row) model.value = row.custom_model ? row.model : "";
  chosen(form);
}

// The chosen provider's console link and default model (the model field's placeholder).
function chosen(form) {
  const option = form.querySelector("[data-keys-provider]").selectedOptions[0];
  const link = form.querySelector("[data-keys-console]");
  if (option && link) {
    link.href = option.dataset.console;
    link.textContent = new URL(option.dataset.console).host;
  }
  const model = form.querySelector("[data-keys-model]");
  if (model && option) model.placeholder = option.dataset.defaultModel ? `Default: ${option.dataset.defaultModel}` : "";
}

function showError(form, text) {
  const error = form.querySelector("[data-keys-error]");
  error.textContent = text;
  error.hidden = !text;
}

function render(data) {
  for (const form of document.querySelectorAll("[data-keys-form]")) fill(form, data.keys?.[form.dataset.purpose] || null);
}

async function busy(button, words, work) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = words;
  try { return await work(); } finally { button.disabled = false; button.textContent = label; }
}

function start() {
  const forms = document.querySelectorAll("[data-keys-form]");
  if (!forms.length) return;
  api("/valr/api/keys").then(render).catch(error => forms.forEach(form => {
    form.querySelector("[data-keys-status]").textContent = error.message;
  }));
  for (const form of forms) {
    const purpose = form.dataset.purpose;
    form.querySelector("[data-keys-provider]").addEventListener("change", () => chosen(form));
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const key = form.elements.key.value.trim();
      if (!key) { showError(form, "Paste your API key."); form.elements.key.focus(); return; }
      showError(form, "");
      const body = {purpose, provider: form.elements.provider.value, key};
      if (form.elements.model) body.model = form.elements.model.value.trim();
      try {
        const data = await busy(form.querySelector('button[type="submit"]'), "Checking…", () => api("/valr/api/keys", body));
        form.elements.key.value = "";
        render(data);
        toast(data.message);
      } catch (error) { showError(form, error.message); }
    });
    form.querySelector("[data-keys-test]").addEventListener("click", async event => {
      try {
        const data = await busy(event.currentTarget, "Testing…", () => api("/valr/api/keys/test", {purpose}));
        render(data);
        toast(data.message, data.status !== "rejected");
      } catch (error) { toast(error.message, false); }
    });
    form.querySelector("[data-keys-remove]").addEventListener("click", async event => {
      try {
        const data = await busy(event.currentTarget, "Removing…", () => api("/valr/api/keys/remove", {purpose}));
        render(data);
        toast(data.message);
      } catch (error) { toast(error.message, false); }
    });
  }
}

if (typeof document !== "undefined" && typeof window !== "undefined") start();
