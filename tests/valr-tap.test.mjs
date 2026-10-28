// Tap feedback (static/valr-tap.js) and the service worker's asset cache (static/sw.js).
import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";
import {isPageNavigation, sectionHref} from "../static/valr-tap.js";

const here = new URL("https://valr.example.test/valr/leads?tab=all");
const link = (href, attrs = {}) => ({getAttribute: name => (name === "href" ? href : null), target: attrs.target || "",
                                     hasAttribute: name => name in attrs});
const click = (extra = {}) => ({defaultPrevented: false, button: 0, metaKey: false, ctrlKey: false, shiftKey: false, altKey: false, ...extra});

test("a plain tap on a link to another page of the site is a navigation", () => {
  assert.equal(isPageNavigation(click(), link("/valr/progress"), here), true);
  assert.equal(isPageNavigation(click(), link("/valr/leads?tab=live"), here), true);  // Another filter.
  assert.equal(isPageNavigation(click(), link("https://valr.example.test/valr"), here), true);
});

test("anchors, new tabs, other sites, handled clicks and modifier keys are not", () => {
  assert.equal(isPageNavigation(click(), link("#lead"), here), false);
  assert.equal(isPageNavigation(click(), link("/valr/leads?tab=all#lead"), here), false);
  assert.equal(isPageNavigation(click(), link("https://mail.google.com/x"), here), false);
  assert.equal(isPageNavigation(click(), link("/valr", {target: "_blank"}), here), false);
  assert.equal(isPageNavigation(click(), link("/export.json", {download: ""}), here), false);
  assert.equal(isPageNavigation(click({defaultPrevented: true}), link("/valr"), here), false);
  assert.equal(isPageNavigation(click({metaKey: true}), link("/valr"), here), false);
  assert.equal(isPageNavigation(click(), link("mailto:a@b.example"), here), false);
  assert.equal(isPageNavigation(click(), null, here), false);
});

test("the tab for a path: the longest matching prefix, Home only for /valr", () => {
  const tabs = ["/valr", "/valr/interviews", "/valr/leads", "/valr/progress", "/valr/settings"];
  assert.equal(sectionHref("/valr", tabs), "/valr");
  assert.equal(sectionHref("/valr/interviews/past/fathom-1", tabs), "/valr/interviews");
  assert.equal(sectionHref("/valr/leads", tabs), "/valr/leads");
  assert.equal(sectionHref("/valr/profile", tabs), "");  // Not a tab: nothing changes.
  assert.equal(sectionHref("/valr/leadsx", tabs), "");
});

function worker({cached = new Map(), network = url => new Response(`net ${url}`)} = {}) {
  const handlers = {}, fetched = [];
  const cache = {
    match: async request => cached.get(request.url ?? request),
    put: async (request, response) => { cached.set(request.url ?? request, response); },
    keys: async () => [...cached.keys()].map(url => ({url})),
    delete: async request => cached.delete(request.url ?? request),
  };
  const self = {addEventListener: (type, fn) => { handlers[type] = fn; }, location: {origin: "https://valr.example.test"},
                registration: {}, clients: {}, navigator: {}};
  const context = vm.createContext({self, caches: {open: async () => cache, match: async () => undefined}, Response, URL,
                                    fetch: async request => { fetched.push(request.url ?? request); return network(request.url ?? request); },
                                    Promise, JSON, Number, String, Boolean, Date});
  vm.runInContext(readFileSync(new URL("../static/sw.js", import.meta.url), "utf8"), context);
  const dispatch = (request, preloadResponse = Promise.resolve(undefined)) => {
    let answer = null;
    handlers.fetch({request, preloadResponse, respondWith: p => { answer = p; }});
    return answer;
  };
  return {context, dispatch, fetched, cached};
}

test("versioned static files and pinned three.js are the only cacheable URLs", () => {
  const {context} = worker();
  const origin = "https://valr.example.test";
  assert.equal(context.isVersioned(`${origin}/static/valr.js?v=3b4517af43`, origin), true);
  assert.equal(context.isVersioned("https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.min.js", origin), true);
  assert.equal(context.isVersioned(`${origin}/static/valr.js`, origin), false);
  assert.equal(context.isVersioned(`${origin}/static/valr.js?v=<script>`, origin), false);
  assert.equal(context.isVersioned(`${origin}/valr/api/notifications?v=abcdef`, origin), false);
  assert.equal(context.isVersioned(`https://other.example/static/a.js?v=abcdef`, origin), false);
});

test("a versioned file comes from the cache after the first load; the API never does", async () => {
  const sw = worker();
  const url = "https://valr.example.test/static/valr.js?v=abcdef1234";
  assert.equal(await (await sw.dispatch({url, method: "GET", mode: "cors"})).text(), `net ${url}`);
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(await (await sw.dispatch({url, method: "GET", mode: "cors"})).text(), `net ${url}`);
  assert.equal(sw.fetched.length, 1);  // The second came from the cache.
  assert.equal(sw.dispatch({url: "https://valr.example.test/valr/api/notifications?unread=1", method: "GET", mode: "cors"}), null);
});

test("a new deploy's version replaces the old copy", async () => {
  const sw = worker();
  const old = "https://valr.example.test/static/valr.js?v=aaaaaa1111";
  const fresh = "https://valr.example.test/static/valr.js?v=bbbbbb2222";
  await sw.dispatch({url: old, method: "GET", mode: "cors"});
  await new Promise(resolve => setTimeout(resolve, 0));
  await sw.dispatch({url: fresh, method: "GET", mode: "cors"});
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual([...sw.cached.keys()], [fresh]);
});

test("a page load uses the preloaded response, and the offline page when the network fails", async () => {
  const sw = worker({network: () => { throw new TypeError("offline"); }});
  const nav = {url: "https://valr.example.test/valr", mode: "navigate", method: "GET"};
  assert.equal(await (await sw.dispatch(nav, Promise.resolve(new Response("preloaded")))).text(), "preloaded");
  assert.equal(sw.fetched.length, 0);
  assert.equal(await (await sw.dispatch(nav)).text(), "You're offline.");
  assert.equal(sw.fetched.length, 1);
});
