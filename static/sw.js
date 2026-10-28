// VALR's service worker, served as /sw.js (scope /) by app/webpush_web.py.
//
// - push: shows the notification app/webpush.py sent ({title, body, url, tag, badge, urgent}).
//   The same `tag` replaces an earlier notification from the same thread instead of stacking.
//   `badge` (the unread count) goes on the Home Screen icon where the browser supports it.
// - notificationclick: focuses an open VALR window (and sends it to the item), or opens one.
// - Offline: a page load that fails shows /static/offline.html.
// - Speed: page loads use navigation preload (the request starts while this worker wakes up,
//   which on iOS can take a while); versioned assets (/static/<name>?v=<hash>, and the pinned
//   three.js for the globe) come from the cache, so a page needs no network for its scripts and
//   styles. Pages themselves are never cached: no private data ever lands in the cache, only
//   these public files.

const OFFLINE = "/static/offline.html";
const CACHE = "valr-offline-v1";
const ASSETS = "valr-assets-v1";
const THREE = "https://cdn.jsdelivr.net/npm/three@";
const ICON = "/static/icons/icon-192.png";
const BADGE = "/static/icons/badge-96.png";

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll([OFFLINE, ICON].map(url => new Request(url, {cache: "reload"}))))
    .then(() => self.skipWaiting()));
});

self.addEventListener("activate", event => {
  event.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(key => key !== CACHE && key !== ASSETS).map(key => caches.delete(key))))
    .then(() => self.registration.navigationPreload?.enable().catch(() => {}))
    .then(() => self.clients.claim()));
});

// A file that never changes at this URL: /static/<name>?v=<hash> (app/valr_ui/shell.asset) or a
// pinned three.js build. Anything else goes to the network as before.
function isVersioned(url, origin) {
  if (url.startsWith(THREE)) return true;
  const parsed = new URL(url);
  return parsed.origin === origin && parsed.pathname.startsWith("/static/") && /^[0-9a-f]{6,64}$/.test(parsed.searchParams.get("v") || "");
}

async function fromCache(request) {
  const cache = await caches.open(ASSETS);
  const hit = await cache.match(request);
  if (hit) return hit;
  const response = await fetch(request);
  if (response.ok) {
    const copy = response.clone();
    const path = new URL(request.url).pathname;
    // One copy per file: drop the versions an earlier deploy left behind.
    cache.keys().then(keys => Promise.all(keys.filter(key => new URL(key.url).pathname === path && key.url !== request.url)
      .map(key => cache.delete(key)))).then(() => cache.put(request, copy)).catch(() => {});
  }
  return response;
}

async function page(event) {
  try {
    const preloaded = await event.preloadResponse;
    if (preloaded) return preloaded;
    return await fetch(event.request);
  } catch {
    return (await caches.match(OFFLINE)) || new Response("You're offline.", {headers: {"Content-Type": "text/plain"}});
  }
}

self.addEventListener("fetch", event => {
  const {request} = event;
  if (request.mode === "navigate") { event.respondWith(page(event)); return; }
  if (request.method === "GET" && isVersioned(request.url, self.location.origin)) event.respondWith(fromCache(request));
  // The rest (the API, unversioned files): no respondWith, the browser fetches it as usual.
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
  const work = [self.registration.showNotification(title, options),
                self.clients.matchAll({type: "window", includeUncontrolled: true})  // Open pages refresh the bell now.
                  .then(windows => windows.forEach(client => client.postMessage({type: "valr-push"}))).catch(() => {})];
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
