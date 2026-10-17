// The Home Screen app: static/valr-pwa.js's pure parts and static/sw.js's push and click handlers.
import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";
import {deviceRow, devicesText, endpointKey, isIos, isStandalone, keyBytes, sameKey, viewFor} from "../static/valr-pwa.js";

const IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1";

test("iPhone and iPad (which says Mac) are iOS; a Mac is not", () => {
  assert.equal(isIos(IPHONE), true);
  assert.equal(isIos("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", "MacIntel", 5), true);
  assert.equal(isIos("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", "MacIntel", 0), false);
});

test("standalone comes from the display-mode media query or navigator.standalone", () => {
  assert.equal(isStandalone({matchMedia: () => ({matches: true}), navigator: {}}), true);
  assert.equal(isStandalone({matchMedia: () => ({matches: false}), navigator: {standalone: true}}), true);
  assert.equal(isStandalone({matchMedia: () => ({matches: false}), navigator: {}}), false);
});

test("the card shows install steps in iPhone Safari, then enable, then on", () => {
  const base = {ios: true, standalone: false, supported: false, configured: true, permission: "default", subscribed: false};
  assert.equal(viewFor(base), "install");
  assert.equal(viewFor({...base, standalone: true}), "update");  // iOS before 16.4: no PushManager.
  assert.equal(viewFor({...base, standalone: true, supported: true}), "enable");
  assert.equal(viewFor({...base, standalone: true, supported: true, permission: "denied"}), "denied");
  assert.equal(viewFor({...base, standalone: true, supported: true, permission: "granted", subscribed: true}), "on");
  assert.equal(viewFor({...base, standalone: true, supported: true, configured: false}), "server");
  assert.equal(viewFor({...base, ios: false}), "unsupported");
  assert.equal(viewFor({...base, ios: false, supported: true}), "enable");  // A desktop browser can subscribe too.
});

test("VAPID key decoding and comparison", () => {
  const bytes = keyBytes("BPk-_w");
  assert.deepEqual([...bytes], [4, 249, 62, 255]);
  assert.equal(sameKey(bytes.buffer, keyBytes("BPk-_w")), true);
  assert.equal(sameKey(bytes.buffer, keyBytes("BPk-AA")), false);
  assert.equal(sameKey(null, bytes), false);
  assert.equal(devicesText(0), "Phone notifications are off: alerts only reach the bell.");
  assert.equal(devicesText(1), "1 device subscribed.");
  assert.equal(devicesText(2), "2 devices subscribed.");
});

function worker() {
  const handlers = {}, shown = [], badges = [], opened = [], focused = [];
  const windows = [];
  const self = {
    addEventListener: (type, fn) => { handlers[type] = fn; },
    location: {origin: "https://valr.example.test"},
    navigator: {setAppBadge: n => { badges.push(n); return Promise.resolve(); },
                clearAppBadge: () => { badges.push(0); return Promise.resolve(); }},
    registration: {showNotification: (title, options) => { shown.push({title, options}); return Promise.resolve(); }},
    clients: {matchAll: async () => windows, openWindow: async url => { opened.push(url); }, claim: async () => {}},
    skipWaiting: async () => {},
  };
  const context = vm.createContext({self, caches: {}, fetch: async () => {}, Response, URL, Promise, JSON, Number, String,
                                    Boolean, Date});
  vm.runInContext(readFileSync(new URL("../static/sw.js", import.meta.url), "utf8"), context);
  return {handlers, shown, badges, opened, focused, windows, context};
}

test("a push shows the notification with its tag and sets the badge", async () => {
  const sw = worker();
  let done;
  sw.handlers.push({data: {text: () => JSON.stringify({title: "Action needed · Acme", body: "Acme sent you an offer.",
                                                      url: "/valr/leads?lead=l1", tag: "valr-abc", badge: 3, urgent: true})},
                    waitUntil: p => { done = p; }});
  await done;
  assert.equal(sw.shown[0].title, "Action needed · Acme");
  assert.equal(sw.shown[0].options.tag, "valr-abc");
  assert.equal(sw.shown[0].options.renotify, true);
  assert.equal(sw.shown[0].options.data.url, "/valr/leads?lead=l1");
  assert.deepEqual(sw.badges, [3]);
  const evil = sw.context.parsePush(JSON.stringify({url: "//evil.example/x"}));
  assert.equal(evil.options.data.url, "/valr");
  assert.equal(sw.context.parsePush("not json").options.body, "not json");
});

test("a click focuses an open VALR window, or opens one", async () => {
  const sw = worker();
  let done;
  const notification = {data: {url: "/valr/leads?lead=l1"}, close: () => {}};
  sw.handlers.notificationclick({notification, waitUntil: p => { done = p; }});
  await done;
  assert.deepEqual(sw.opened, ["https://valr.example.test/valr/leads?lead=l1"]);
  const navigated = [];
  sw.windows.push({url: "https://valr.example.test/valr", focus: async () => {}, navigate: async url => { navigated.push(url); }});
  sw.handlers.notificationclick({notification, waitUntil: p => { done = p; }});
  await done;
  assert.deepEqual(navigated, ["https://valr.example.test/valr/leads?lead=l1"]);
  assert.equal(sw.opened.length, 1);
});

test("the device list marks this device and says when each was last notified", async () => {
  const now = Date.parse("2026-10-17T12:00:00Z");
  const device = {id: 3, label: "iPhone", key: "abc", added_at: "2026-10-05T09:00:00Z", last_ok_at: "2026-10-17T07:00:00Z", failures: 0};
  assert.deepEqual(deviceRow(device, "abc", now), {mine: true, title: "iPhone (this one)", sub: "Added 12 days ago · last notified today"});
  assert.equal(deviceRow({...device, last_ok_at: null, failures: 2}, "zzz", now).sub, "Added 12 days ago · not notified yet · 2 failed");
  const key = await endpointKey("https://web.push.apple.com/fictional");
  assert.match(key, /^[0-9a-f]{16}$/);  // The same hash as app/webpush.endpoint_key.
  assert.equal(await endpointKey(""), "");
});
