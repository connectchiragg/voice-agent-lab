// Settings and Profile (app/valr_ui/settings.py, app/valr_ui/profile.py): the confirm sheet,
// the phone form, the Settings actions, the profile form and the photo cropper. Every POST
// carries the page's CSRF token. Pure parts are exported for tests/valr-settings.test.mjs.
//
// Confirm: any element with [data-confirm] (a title), [data-confirm-body] and [data-confirm-ok]
// asks first in a small sheet; [data-confirm-type="DELETE"] also asks to type that word. The
// click is caught in the capture phase, so the element's own handler (here, static/valr.js or
// static/valr-pwa.js) runs only after a yes.

export const PHOTO_SIZE = 512;
export const PHOTO_MAX = 2 * 1024 * 1024;

// The phone field, checked as the server does it (app/owner.normalize_phone), so most mistakes
// show before a round trip. Returns "" when it looks fine, else what to fix.
export function phoneProblem(raw) {
  let text = String(raw || "").replace(/[\s().-]/g, "");
  if (!text) return "Enter your mobile number.";
  if (text.startsWith("00")) text = `+${text.slice(2)}`;
  if (!text.startsWith("+")) {
    const national = text.length === 11 && text.startsWith("0") ? text.slice(1) : text;
    return /^[6-9]\d{9}$/.test(national) ? "" : "Add the country code, like +91 98765 43210.";
  }
  if (!/^\+[1-9]\d{7,14}$/.test(text)) return "That doesn't look like a phone number: + and the country code, then 8 to 15 digits.";
  if (text.startsWith("+91")) {
    let national = text.slice(3);
    if (national.length === 11 && national.startsWith("0")) national = national.slice(1);
    if (!/^\d{10}$/.test(national)) return "An Indian mobile number has 10 digits after +91.";
    if (!/^[6-9]/.test(national)) return "Use your mobile number: VALR calls you on the go.";
  }
  return "";
}

// The square crop: the source rectangle for a zoom (1 = the short side fills the square) and an
// offset in -1..1 along each axis (0 = centred).
export function cropBox(width, height, zoom = 1, dx = 0, dy = 0) {
  const side = Math.min(width, height) / Math.max(zoom, 1);
  const clamp = v => Math.max(-1, Math.min(1, v));
  const x = (width - side) / 2 * (1 + clamp(dx));
  const y = (height - side) / 2 * (1 + clamp(dy));
  return {x, y, side};
}

export function initials(name) {
  return String(name || "").trim().split(/\s+/).slice(0, 2).map(w => w[0] || "").join("").toUpperCase() || "?";
}

const csrf = () => document.querySelector('meta[name="csrf-token"]')?.content ?? "";

async function api(url, body) {
  const response = await fetch(url, body === undefined ? {credentials: "same-origin"} : {
    method: "POST", credentials: "same-origin",
    headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf()}, body: JSON.stringify(body)});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(typeof data.detail === "string" ? data.detail : `Something went wrong (HTTP ${response.status}).`);
    error.fields = data.errors || {};
    throw error;
  }
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

function later(text) {
  try { sessionStorage.setItem("valr-toast", text); } catch {}
}

async function busy(button, words, work) {
  const label = button.innerHTML;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  if (words) button.textContent = words;
  try { return await work(); } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
    button.innerHTML = label;
  }
}

// The confirm sheet.

function confirmSheet() {
  let sheet = document.getElementById("confirm-sheet");
  if (sheet) return sheet;
  sheet = document.createElement("dialog");
  sheet.className = "sheet confirm-sheet";
  sheet.id = "confirm-sheet";
  sheet.setAttribute("aria-labelledby", "confirm-title");
  sheet.innerHTML = `<form method="dialog" class="sheet-body confirm-body">
    <h2 class="sheet-title" id="confirm-title"></h2><p class="confirm-text"></p>
    <label class="confirm-type" hidden><span class="field-label"></span><input class="input" autocomplete="off" spellcheck="false"></label>
    <div class="confirm-actions"><button class="btn" value="cancel" type="submit">Cancel</button>
    <button class="btn primary" value="ok" type="submit" data-confirm-yes></button></div></form>`;
  document.body.append(sheet);
  return sheet;
}

export function ask({title, body = "", ok = "OK", type = "", danger = false}) {
  const sheet = confirmSheet();
  sheet.querySelector(".sheet-title").textContent = title;
  sheet.querySelector(".confirm-text").textContent = body;
  const yes = sheet.querySelector("[data-confirm-yes]");
  yes.textContent = ok;
  yes.classList.toggle("danger", danger);
  const typed = sheet.querySelector(".confirm-type");
  const input = typed.querySelector("input");
  typed.hidden = !type;
  input.value = "";
  typed.querySelector(".field-label").textContent = type ? `Type ${type} to confirm` : "";
  yes.disabled = Boolean(type);
  input.oninput = () => { yes.disabled = input.value.trim() !== type; };
  return new Promise(resolve => {
    sheet.addEventListener("close", () => resolve(sheet.returnValue === "ok"), {once: true});
    sheet.returnValue = "";
    sheet.showModal();
    (type ? input : sheet.querySelector('button[value="cancel"]')).focus();
  });
}

function startConfirm() {
  document.addEventListener("click", async event => {
    const target = event.target.closest("[data-confirm]");
    if (!target || target.dataset.confirmed === "1") return;
    event.preventDefault();
    event.stopPropagation();
    target.closest("details[open]")?.removeAttribute("open");
    const yes = await ask({title: target.dataset.confirm, body: target.dataset.confirmBody || "",
                           ok: target.dataset.confirmOk || "OK", type: target.dataset.confirmType || "",
                           danger: target.classList.contains("danger") || Boolean(target.dataset.confirmType)});
    if (!yes) return;
    target.dataset.confirmed = "1";
    try { target.click(); } finally { delete target.dataset.confirmed; }
  }, true);
}

// Settings actions.

const ACTIONS = {
  "google-disconnect": async () => {
    const data = await api("/valr/api/google/disconnect", {});
    later(data.message);
    location.reload();
  },
  "google-sync": async () => toast((await api("/valr/api/google/sync", {})).message),
  "phone-remove": async () => {
    const data = await api("/valr/api/profile/phone/remove", {});
    later(data.message);
    location.reload();
  },
  "sample-digest": async () => toast((await api("/valr/api/notifications/test", {kind: "digest"})).message),
  "logout-all": async () => { location.href = (await api("/logout/all", {})).next || "/login"; },
  "data-delete": async () => { location.href = (await api("/valr/api/data/delete", {confirm: "DELETE"})).next || "/login"; },
};

function startActions() {
  document.addEventListener("click", async event => {
    const button = event.target.closest("[data-settings-action]");
    if (!button) return;
    const run = ACTIONS[button.dataset.settingsAction];
    if (!run) return;
    try { await busy(button, "Working…", run); } catch (error) { toast(error.message, false); }
  });
  document.addEventListener("click", async event => {
    const button = event.target.closest("[data-copy]");
    if (!button) return;
    try { await navigator.clipboard.writeText(button.dataset.copy); toast("Copied."); }
    catch { toast("Select the number and copy it.", false); }
  });
}

// The phone form (Settings › Calls & reminders, and the set-up step).

function startPhone() {
  const form = document.querySelector("[data-phone-form]");
  if (!form) return;
  const input = form.querySelector("input[name=phone]");
  const error = form.querySelector("[data-phone-error]");
  const show = text => { error.textContent = text; error.hidden = !text; input.setAttribute("aria-invalid", text ? "true" : "false"); };
  const open = () => {
    form.hidden = false;
    show("");
    form.closest(".set-row")?.scrollIntoView({behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "center"});
    setTimeout(() => input.focus(), 250);
  };
  document.querySelectorAll("[data-phone-edit]").forEach(button => button.addEventListener("click", open));
  form.querySelector("[data-phone-cancel]")?.addEventListener("click", () => { form.hidden = true; input.value = ""; show(""); });
  input.addEventListener("input", () => { if (!error.hidden) show(phoneProblem(input.value)); });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const problem = phoneProblem(input.value);
    if (problem) { show(problem); input.focus(); return; }
    const button = form.querySelector('button[type="submit"]');
    try {
      const data = await busy(button, "Saving…", () => api("/valr/api/profile/phone", {phone: input.value}));
      input.value = "";
      later(data.message);
      location.reload();
    } catch (failure) { show(failure.message); input.focus(); }
  });
  if (location.hash === "#phone-edit") open();
}

// The profile page.

function setErrors(form, fields) {
  form.querySelectorAll("[data-error-for]").forEach(node => {
    const text = fields[node.dataset.errorFor] || "";
    node.textContent = text;
    node.hidden = !text;
    form.querySelector(`[name="${node.dataset.errorFor}"]`)?.setAttribute("aria-invalid", text ? "true" : "false");
  });
}

function startProfile() {
  const form = document.querySelector("[data-profile-form]");
  if (!form) return;
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const body = {};
    for (const name of ["display_name", "recording_name", "name_pronunciation", "timezone"]) {
      const field = form.elements[name];
      if (field) body[name] = field.value;
    }
    const button = form.querySelector('button[type="submit"]');
    setErrors(form, {});
    try {
      const data = await busy(button, "Saving…", () => api("/valr/api/profile", body));
      toast(data.message);
      document.querySelectorAll("[data-avatar].initials").forEach(node => { node.textContent = initials(data.display_name); });
    } catch (error) {
      setErrors(form, error.fields || {});
      toast(error.message, false);
    }
  });
  document.querySelector("[data-photo-remove]")?.addEventListener("click", async event => {
    try {
      const data = await busy(event.currentTarget, "Removing…", () => api("/valr/api/profile/photo/remove", {}));
      later(data.message);
      location.reload();
    } catch (error) { toast(error.message, false); }
  });
  startCropper();
}

function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
}

function encode(canvas) {
  return new Promise(resolve => canvas.toBlob(blob => {
    if (blob && blob.type === "image/webp") return resolve(blob);
    canvas.toBlob(resolve, "image/jpeg", 0.9);  // Safari can't write WebP: JPEG, still without EXIF.
  }, "image/webp", 0.9));
}

function startCropper() {
  const file = document.querySelector("[data-photo-file]");
  const sheet = document.querySelector("[data-crop-sheet]");
  if (!file || !sheet) return;
  const view = sheet.querySelector("canvas");
  const zoom = sheet.querySelector("[data-crop-zoom]");
  const status = sheet.querySelector("[data-crop-status]");
  let image = null, dx = 0, dy = 0, drag = null;
  const draw = () => {
    if (!image) return;
    const box = cropBox(image.naturalWidth, image.naturalHeight, Number(zoom.value), dx, dy);
    const ctx = view.getContext("2d");
    ctx.clearRect(0, 0, view.width, view.height);
    ctx.drawImage(image, box.x, box.y, box.side, box.side, 0, 0, view.width, view.height);
  };
  document.querySelector("[data-photo-pick]")?.addEventListener("click", () => file.click());
  file.addEventListener("change", () => {
    const chosen = file.files?.[0];
    file.value = "";
    if (!chosen) return;
    if (!/^image\/(jpeg|png|webp|heic|heif)$/.test(chosen.type)) { toast("Pick a JPEG, PNG or WebP photo.", false); return; }
    const url = URL.createObjectURL(chosen);
    image = new Image();
    image.onload = () => { dx = dy = 0; zoom.value = "1"; status.textContent = ""; draw(); sheet.showModal(); };
    image.onerror = () => { toast("That photo couldn't be opened. Try a JPEG or PNG.", false); URL.revokeObjectURL(url); };
    image.src = url;
  });
  zoom.addEventListener("input", draw);
  view.addEventListener("pointerdown", event => { drag = {x: event.clientX, y: event.clientY, dx, dy}; view.setPointerCapture(event.pointerId); });
  view.addEventListener("pointermove", event => {
    if (!drag) return;
    const scale = 2 / view.clientWidth;
    dx = Math.max(-1, Math.min(1, drag.dx - (event.clientX - drag.x) * scale));
    dy = Math.max(-1, Math.min(1, drag.dy - (event.clientY - drag.y) * scale));
    draw();
  });
  view.addEventListener("pointerup", () => { drag = null; });
  view.addEventListener("keydown", event => {
    const step = {ArrowLeft: [-0.1, 0], ArrowRight: [0.1, 0], ArrowUp: [0, -0.1], ArrowDown: [0, 0.1]}[event.key];
    if (!step) return;
    event.preventDefault();
    dx = Math.max(-1, Math.min(1, dx + step[0]));
    dy = Math.max(-1, Math.min(1, dy + step[1]));
    draw();
  });
  sheet.querySelector("[data-crop-cancel]")?.addEventListener("click", () => sheet.close());
  sheet.querySelector("[data-crop-save]")?.addEventListener("click", async event => {
    if (!image) return;
    const out = document.createElement("canvas");
    out.width = out.height = PHOTO_SIZE;
    const box = cropBox(image.naturalWidth, image.naturalHeight, Number(zoom.value), dx, dy);
    out.getContext("2d").drawImage(image, box.x, box.y, box.side, box.side, 0, 0, PHOTO_SIZE, PHOTO_SIZE);
    try {
      await busy(event.currentTarget, "Saving…", async () => {
        const blob = await encode(out);
        if (!blob || blob.size > PHOTO_MAX) throw new Error("That photo is too large: 2 MB at most.");
        const data = await api("/valr/api/profile/photo", {image: await blobToBase64(blob)});
        later(data.message);
      });
      sheet.close();
      location.reload();
    } catch (error) { status.textContent = error.message; }
  });
}

function startToast() {
  let text = "";
  try { text = sessionStorage.getItem("valr-toast") || ""; sessionStorage.removeItem("valr-toast"); } catch {}
  text = text || document.querySelector("[data-toast-on-load]")?.dataset.toastOnLoad || "";
  if (text) setTimeout(() => toast(text), 300);
}

if (typeof document !== "undefined" && typeof window !== "undefined") {
  startConfirm();
  startActions();
  startPhone();
  startProfile();
  startToast();
}
