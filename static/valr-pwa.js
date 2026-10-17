// The Home Screen app: registers /sw.js on every page and runs Settings → "Phone notifications"
// (app/valr_ui/pwa.py renders every state; this shows the one that fits the device).
//
// iOS rules this follows: web push works only in the app added to the Home Screen (iOS 16.4+),
// and the permission prompt must come straight from a tap, so the public key is fetched on
// load and Notification.requestPermission() is the first thing the click handler does.

export function isIos(ua = "", platform = "", touchPoints = 0) {
  return /iPhone|iPad|iPod/.test(ua) || (platform === "MacIntel" && touchPoints > 1);  // iPadOS says "Mac".
}

export function isStandalone(win) {
  try {
    return Boolean(win.matchMedia?.("(display-mode: standalone)").matches || win.navigator?.standalone === true);
  } catch { return false; }
}

// Which part of the card to show.
export function viewFor({ios, standalone, supported, configured, permission, subscribed}) {
  if (ios && !standalone) return "install";
  if (!supported) return ios ? "update" : "unsupported";
  if (!configured) return "server";
  if (permission === "denied") return "denied";
  if (subscribed && permission === "granted") return "on";
  return "enable";
}

export function deviceLabel(ua = "") {
  if (/iPhone/.test(ua)) return "iPhone";
  if (/iPad/.test(ua)) return "iPad";
  if (/Android/.test(ua)) return "phone";
  return "browser";
}

export function devicesText(count) {
  const n = Number(count) || 0;
  return n ? `${n} device${n === 1 ? "" : "s"} subscribed.` : "Phone notifications are off: alerts only reach the bell.";
}

// A device row's label: "iPhone (this one)", added date; the same rule as app/webpush.device_view.
export function deviceRow(device, thisKey = "", now = Date.now()) {
  const mine = Boolean(thisKey) && device.key === thisKey;
  const when = iso => {
    if (!iso) return "";
    const days = Math.floor((now - Date.parse(iso)) / 86400000);
    return days <= 0 ? "today" : days === 1 ? "yesterday" : `${days} days ago`;
  };
  const seen = device.last_ok_at ? `last notified ${when(device.last_ok_at)}` : "not notified yet";
  const trouble = device.failures ? ` · ${device.failures} failed` : "";
  return {mine, title: `${device.label}${mine ? " (this one)" : ""}`, sub: `Added ${when(device.added_at)} · ${seen}${trouble}`};
}

export async function endpointKey(endpoint) {
  if (!endpoint || !globalThis.crypto?.subtle) return "";
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(endpoint));
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, "0")).join("").slice(0, 16);
}

export function sameKey(a, b) {
  if (!a || !b) return false;
  const x = new Uint8Array(a), y = new Uint8Array(b);
  return x.length === y.length && x.every((v, i) => v === y[i]);
}

export function keyBytes(base64url) {
  const padded = (base64url + "===".slice((base64url.length + 3) % 4)).replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(padded), c => c.charCodeAt(0));
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

async function register() {
  if (!("serviceWorker" in navigator)) return null;
  try { return await navigator.serviceWorker.register("/sw.js", {scope: "/"}); } catch { return null; }
}

function startCard(registering) {
  const card = document.querySelector("[data-pwa-card]");
  if (!card) return;
  const ua = navigator.userAgent;
  const ios = isIos(ua, navigator.platform, navigator.maxTouchPoints);
  const standalone = isStandalone(window);
  const supported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  const devices = card.querySelector("[data-pwa-devices]");
  const list = card.querySelector("[data-pwa-device-list]");
  const label = card.querySelector("[data-pwa-on-label]");
  if (label) label.textContent = `On for this ${deviceLabel(ua)}`;
  let server = {configured: false, public_key: "", devices: 0, device_list: []};

  const show = view => {
    card.querySelectorAll("[data-pwa-view]").forEach(node => { node.hidden = node.dataset.pwaView !== view; });
    card.dataset.pwaState = view;
  };
  const current = async () => {
    const registration = await registering;
    return registration?.pushManager ? registration.pushManager.getSubscription() : null;
  };
  const renderDevices = async subscription => {
    devices.textContent = devicesText(server.devices);
    if (!list) return;
    const mine = await endpointKey(subscription?.endpoint).catch(() => "");
    list.replaceChildren(...(server.device_list || []).map(device => {
      const row = deviceRow(device, mine);
      const item = document.createElement("li");
      item.className = "device";
      const text = document.createElement("div");
      const title = document.createElement("p");
      title.className = "device-title";
      title.textContent = row.title;
      const sub = document.createElement("p");
      sub.className = "field-help";
      sub.textContent = row.sub;
      text.append(title, sub);
      const actions = document.createElement("div");
      actions.className = "device-actions";
      const test = document.createElement("button");
      test.className = "btn sm";
      test.type = "button";
      test.textContent = "Send a test";
      test.addEventListener("click", async () => {
        try { const data = await api("/valr/api/push/test", {id: device.id}); server = data; toast(data.message); }
        catch (error) { toast(error.message); }
        refresh();
      });
      const remove = document.createElement("button");
      remove.className = "btn sm ghost";
      remove.type = "button";
      remove.textContent = "Remove";
      remove.setAttribute("aria-label", `Remove ${row.title}`);
      // Asked first by static/valr-settings.js's confirm sheet ([data-confirm]).
      remove.dataset.confirm = `Remove ${row.title}?`;
      remove.dataset.confirmBody = "It gets no more VALR notifications until they're turned on again on that device.";
      remove.dataset.confirmOk = "Remove";
      remove.addEventListener("click", async () => {
        try {
          server = await api("/valr/api/push/remove", {id: device.id});
          if (row.mine) await (await current())?.unsubscribe();
          toast(server.message || "Removed.");
        } catch (error) { toast(error.message); }
        refresh();
      });
      actions.append(test, remove);
      item.append(text, actions);
      return item;
    }));
  };
  const refresh = async () => {
    const subscription = supported ? await current().catch(() => null) : null;
    await renderDevices(subscription);
    show(viewFor({ios, standalone, supported, configured: server.configured,
                  permission: supported ? Notification.permission : "default", subscribed: Boolean(subscription)}));
  };

  api("/valr/api/push").then(data => {
    server = data;
  }).then(async () => {
    // Already subscribed here: tell the server again (it forgets a device the push service dropped).
    const subscription = supported && Notification.permission === "granted" ? await current() : null;
    if (subscription && server.configured) server = await api("/valr/api/push/subscribe", subscription.toJSON());
  }).catch(() => {}).finally(refresh);

  card.querySelector("[data-pwa-enable]")?.addEventListener("click", async () => {
    // First, still inside the tap: iOS only shows the prompt for a user gesture.
    const permission = await Notification.requestPermission();
    if (permission !== "granted") { refresh(); return; }
    try {
      const registration = (await registering) || await navigator.serviceWorker.ready;
      const key = keyBytes(server.public_key);
      let existing = await registration.pushManager.getSubscription();
      if (existing && !sameKey(existing.options?.applicationServerKey, key)) {
        await existing.unsubscribe();  // Made with older server keys: it would never deliver.
        existing = null;
      }
      const subscription = existing || await registration.pushManager.subscribe(
        {userVisibleOnly: true, applicationServerKey: key});
      server = await api("/valr/api/push/subscribe", subscription.toJSON());
      toast("Notifications are on for this device.");
    } catch (error) { toast(`Couldn't turn on notifications: ${error.message}`); }
    refresh();
  });

  card.querySelector("[data-pwa-test]")?.addEventListener("click", async () => {
    try { const data = await api("/valr/api/push/test", {}); server = data; toast(data.message); }
    catch (error) { toast(error.message); }
    refresh();
  });

  card.querySelector("[data-pwa-off]")?.addEventListener("click", async () => {
    try {
      const subscription = await current();
      if (subscription) {
        server = await api("/valr/api/push/unsubscribe", {endpoint: subscription.endpoint});
        await subscription.unsubscribe();
      }
      toast("Notifications are off for this device.");
    } catch (error) { toast(error.message); }
    refresh();
  });

}

if (typeof document !== "undefined" && typeof window !== "undefined") {
  const registering = register();
  startCard(registering);
  // The installed app's icon shows the unread count; the bell's count is the same number.
  if ("setAppBadge" in navigator && isStandalone(window)) {
    api("/valr/api/notifications?unread=1").then(data => {
      const n = Number(data.unread) || 0;
      return n ? navigator.setAppBadge(n) : navigator.clearAppBadge();
    }).catch(() => {});
  }
}
