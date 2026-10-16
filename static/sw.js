// VALR's service worker, served as /sw.js (scope /) by app/webpush_web.py.
//
// - push: shows the notification app/webpush.py sent ({title, body, url, tag, badge, urgent}).
//   The same `tag` replaces an earlier notification from the same thread instead of stacking.
//   `badge` (the unread count) goes on the Home Screen icon where the browser supports it.
// - notificationclick: focuses an open VALR window (and sends it to the item), or opens one.
// - Offline: a page load that fails shows /static/offline.html. Only that page and its icon are
//   cached: no private data ever lands in the cache.

const OFFLINE = "/static/offline.html";
const CACHE = "valr-offline-v1";
const ICON = "/static/icons/icon-192.png";
const BADGE = "/static/icons/badge-96.png";

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll([OFFLINE, ICON].map(url => new Request(url, {cache: "reload"}))))
    .then(() => self.skipWaiting()));
});

self.addEventListener("activate", event => {
  event.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", event => {
  if (event.request.mode !== "navigate") return;  // Only page loads; the browser handles the rest.
  event.respondWith(fetch(event.request).catch(async () =>
    (await caches.match(OFFLINE)) || new Response("You're offline.", {headers: {"Content-Type": "text/plain"}})));
});

function parsePush(text) {
  let data = {};
  try { data = JSON.parse(text || "{}") || {}; } catch { data = {body: String(text || "")}; }
  const url = typeof data.url === "string" && data.url.startsWith("/") && !data.url.startsWith("//") ? data.url : "/valr";
  return {
    title: String(data.title || "VALR"),
    options: {body: String(data.body || ""), tag: String(data.tag || "valr"), renotify: Boolean(data.urgent),
              icon: ICON, badge: BADGE, data: {url}, timestamp: Date.now()},
    badge: Number.isInteger(data.badge) ? data.badge : null,
  };
}

self.addEventListener("push", event => {
  const {title, options, badge} = parsePush(event.data ? event.data.text() : "");
  const work = [self.registration.showNotification(title, options)];
  if (badge !== null && "setAppBadge" in self.navigator) {
    work.push((badge ? self.navigator.setAppBadge(badge) : self.navigator.clearAppBadge()).catch(() => {}));
  }
  event.waitUntil(Promise.all(work));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const target = new URL(event.notification.data?.url || "/valr", self.location.origin).href;
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({type: "window", includeUncontrolled: true});
    const open = windows.find(client => new URL(client.url).origin === self.location.origin);
    if (open) {
      await open.focus();
      if ("navigate" in open) return open.navigate(target).catch(() => open);
      return open;
    }
    return self.clients.openWindow(target);
  })());
});
