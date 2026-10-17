// The past-interview player and key moments (static/valr-player.js), with a tiny fake DOM.
import assert from "node:assert/strict";
import test from "node:test";
import {createPlayer, fillMoments, mediaView, momentItems, nextDelay, playableUrl, POLL_LIMIT_MS,
        renderMoments, renderRecording, renderTranscript, filterTranscript} from "../static/valr-player.js";

class El {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.attrs = {}; this.children = []; this.listeners = {};
    this.hidden = false; this._text = ""; this.currentTime = 0; this.dataset = {};
  }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k.startsWith("data-")) this.dataset[k.slice(5).replace(/-(\w)/g, (_, c) => c.toUpperCase())] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  hasAttribute(k) { return k in this.attrs; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; this._text = ""; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(""); }
  addEventListener(type, fn) { (this.listeners[type] ??= []).push(fn); }
  fire(type) { for (const fn of this.listeners[type] ?? []) fn({type}); }
  all() { return [this, ...this.children.flatMap(c => c.all())]; }
  matches(sel) {
    if (sel.startsWith("[")) return this.hasAttribute(sel.slice(1, -1));
    const [tag, cls] = sel.split(".");
    return (!tag || this.tagName === tag.toUpperCase()) && (!cls || (this.attrs.class ?? "").split(" ").includes(cls));
  }
  querySelector(sel) { return this.all().slice(1).find(e => e.matches(sel)) ?? null; }
  querySelectorAll(sel) { return this.all().slice(1).filter(e => e.matches(sel)); }
}
const doc = {createElement: tag => new El(tag)};
const MOMENTS = [
  {label: "Tell me about a time you pushed back?", seconds: 1490, clock: "24:50", url: "https://fathom.video/calls/1?timestamp=1490"},
  {label: "Walk me through the ingest design?", seconds: 95, clock: "1:35", url: "https://fathom.video/calls/1?timestamp=95"},
  {label: "", seconds: 5}, {label: "Bad link", seconds: 10, url: "javascript:alert(1)"},
];
const json = (body, ok = true, status = 200) => async () => ({ok, status, json: async () => body});

test("moments are cleaned, ordered, and never carry a non-https link", () => {
  const items = momentItems(MOMENTS);
  assert.deepEqual(items.map(m => m.seconds), [10, 95, 1490]);
  assert.equal(items[0].url, "");
  assert.equal(momentItems(null).length, 0);
});

test("with a player, a moment's time seeks it; without one it links to Fathom", () => {
  const sought = [];
  const list = renderMoments(doc, MOMENTS, s => sought.push(s));
  const buttons = list.all().filter(e => e.tagName === "BUTTON");
  assert.equal(buttons.length, 3);
  buttons[1].fire("click");
  assert.deepEqual(sought, [95]);
  assert.equal(list.all().filter(e => e.tagName === "A").length, 0);
  const links = renderMoments(doc, MOMENTS).all().filter(e => e.tagName === "A");
  assert.deepEqual(links.map(a => a.attrs.href), ["https://fathom.video/calls/1?timestamp=95", "https://fathom.video/calls/1?timestamp=1490"]);
});

test("the page's key moments render from /valr/api/recording", async () => {
  const box = new El("div"); box.setAttribute("data-moments-id", "fathom:4242");
  const body = new El("div"); body.setAttribute("data-recording-body", ""); body.textContent = "Looking for key moments…";
  box.append(body);
  const asked = [];
  const fetchImpl = async (url) => { asked.push(url); return {ok: true, status: 200, json: async () => (
    {ok: true, found: true, provider: "fathom", label: "Fathom", title: "x", start: null, share_url: "https://fathom.video/calls/4242",
     embed_url: "", moments: MOMENTS, note: ""})}; };
  const seeks = [];
  const players = {"fathom:4242": {seek: s => seeks.push(s)}};
  assert.equal(await fillMoments(doc, box, fetchImpl, players, null), "moments");
  assert.equal(asked[0], "/valr/api/recording?id=fathom%3A4242");
  assert.ok(body.textContent.includes("Key moments") && body.textContent.includes("Walk me through the ingest design?"));
  assert.ok(!body.textContent.includes("Looking for"));
  body.all().find(e => e.tagName === "BUTTON" && e.attrs["data-seconds"] === "1490").fire("click");
  assert.deepEqual(seeks, [1490]);
});

test("a failing or empty moments call says so instead of staying on 'Looking for…'", async () => {
  const make = () => { const box = new El("div"); box.setAttribute("data-moments-id", "fathom:1");
    const body = new El("div"); body.setAttribute("data-recording-body", ""); box.append(body); return [box, body]; };
  let [box, body] = make();
  assert.equal(await fillMoments(doc, box, json({detail: "Postgres is unavailable."}, false, 503), {}, null), "error");
  assert.match(body.textContent, /Key moments couldn't be loaded: Postgres is unavailable/);
  [box, body] = make();
  assert.equal(await fillMoments(doc, box, async () => { throw new Error("offline"); }, {}, null), "error");
  [box, body] = make();
  assert.equal(await fillMoments(doc, box, json({ok: true, found: true, moments: [], note: "The transcript could not be loaded just now."}), {}, null), "empty");
  assert.match(body.textContent, /transcript could not be loaded/);
});

test("media answers map to play, wait or stop", () => {
  assert.equal(mediaView({status: "completed", url: "https://x.example/v.mp4"}).kind, "play");
  assert.match(mediaView({status: "processing"}).text, /Preparing the video… \(first play can take a minute or two\)/);
  assert.equal(mediaView({status: "busy", message: "Fathom is busy, try again in a minute."}).kind, "wait");
  assert.equal(mediaView({status: "unavailable", message: "Fathom says no."}).text, "Fathom says no.");
  assert.equal(mediaView(undefined).kind, "stop");
});

test("polling backs off from 5 s to at most 20 s", () => {
  assert.deepEqual([0, 1, 2, 3, 4, 9].map(nextDelay), [5000, 7500, 11250, 16875, 20000, 20000]);
});

test("only https URLs, or the page's own origin, are played", () => {
  assert.equal(playableUrl("https://streamkit.storage.googleapis.com/v.mp4?sig=1"), "https://streamkit.storage.googleapis.com/v.mp4?sig=1");
  assert.equal(playableUrl("javascript:alert(1)", "http://127.0.0.1:8090"), "");
  assert.equal(playableUrl("http://evil.example/v.mp4", "http://127.0.0.1:8090"), "");
  assert.equal(playableUrl("/preview-media.mp4", "http://127.0.0.1:8090"), "http://127.0.0.1:8090/preview-media.mp4");
});

function playerBox() {
  const box = new El("div"); box.setAttribute("data-player", "fathom:4242");
  const video = new El("video"); video.hidden = true;
  const status = new El("div"); status.setAttribute("data-player-status", "");
  const span = new El("span"); span.setAttribute("class", "muted small"); status.append(span);
  box.append(video, status);
  return {box, video, status, span};
}
function fakeEnv(answers) {
  let t = 0; const waits = [], asked = [];
  return {waits, asked, origin: "http://127.0.0.1:8090", now: () => t,
          wait: async ms => { waits.push(ms); t += ms; },
          fetchMedia: async (id, refresh) => { asked.push([id, refresh]); const a = answers.shift(); if (a instanceof Error) throw a; return a; }};
}

test("the player says 'Preparing…', polls, then streams the signed URL", async () => {
  const {box, video, status, span} = playerBox();
  const url = "https://streamkit.storage.googleapis.com/b/rec.mp4?X-Goog-Signature=fake";
  const env = fakeEnv([{status: "processing", message: "Preparing the video… (first play can take a minute or two)"},
                       {status: "processing"}, {status: "completed", url, content_type: "video/mp4"}]);
  const player = createPlayer(box, env);
  player.seek(95);  // A moment clicked before the video is ready: applied once it loads.
  assert.equal(await player.load(), "playing");
  assert.deepEqual(env.waits, [5000, 7500]);
  assert.equal(video.getAttribute("src"), url);
  assert.equal(video.hidden, false); assert.equal(status.hidden, true);
  video.fire("loadedmetadata");
  assert.equal(video.currentTime, 95);
  player.seek(610);
  assert.equal(video.currentTime, 610);
  assert.ok(span.textContent.startsWith("Preparing the video"));
});

test("the player gives up politely after about 5 minutes of processing", async () => {
  const {box, span} = playerBox();
  const env = fakeEnv(Array.from({length: 40}, () => ({status: "processing"})));
  assert.equal(await createPlayer(box, env).load(), "gave_up");
  assert.ok(env.waits.reduce((a, b) => a + b, 0) >= POLL_LIMIT_MS);
  assert.match(span.textContent, /still preparing/);
});

test("an expired URL mid-session: one fresh URL, resumed at the same second", async () => {
  const {box, video, span} = playerBox();
  const first = "https://streamkit.storage.googleapis.com/b/rec.mp4?sig=old";
  const second = "https://streamkit.storage.googleapis.com/b/rec.mp4?sig=new";
  const env = fakeEnv([{status: "completed", url: first}, {status: "completed", url: second}]);
  const player = createPlayer(box, env);
  await player.load();
  video.currentTime = 1234;
  video.fire("error");
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(env.asked, [["fathom:4242", false], ["fathom:4242", true]]);
  assert.equal(video.getAttribute("src"), second);
  video.fire("loadedmetadata");
  assert.equal(video.currentTime, 1234);
  video.fire("error");  // Only once.
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(env.asked.length, 2);
  assert.match(span.textContent, /stopped loading/);
});

test("an unavailable recording shows Fathom's reason and never a video", async () => {
  const {box, video, span} = playerBox();
  const env = fakeEnv([{status: "unavailable", message: "Fathom says this recording isn't downloadable for your account."}]);
  assert.equal(await createPlayer(box, env).load(), "stopped");
  assert.equal(video.getAttribute("src"), null);
  assert.match(span.textContent, /isn't downloadable for your account/);
});

test("the drawer's full recording gets a player whose moments seek it", () => {
  const body = new El("div");
  const made = [];
  const player = renderRecording(doc, body, {found: true, provider: "fathom", label: "Fathom", id: "fathom:7", player: true,
    title: "Orbit", share_url: "https://fathom.video/calls/7", moments: MOMENTS},
    {}, box => { const p = {seek: s => made.push(s)}; return p; });
  assert.ok(player);
  assert.ok(body.all().some(e => e.attrs["data-player"] === "fathom:7"));
  assert.ok(body.textContent.includes("Open in Fathom"));
  body.all().find(e => e.tagName === "BUTTON").fire("click");
  assert.deepEqual(made, [10]);
});

test("the transcript renders turns whose times seek the player, and the find box filters them", () => {
  const body = new El("div");
  const sought = [];
  const list = renderTranscript(doc, body, {turns: [
    {who: "Ana", seconds: 95, clock: "1:35", text: "How would you shard it?"},
    {who: "You", seconds: 110, clock: "1:50", text: "By tenant, then by time."},
  ]}, s => sought.push(s));
  const buttons = list.all().filter(e => e.tagName === "BUTTON");
  assert.equal(buttons.length, 2);
  buttons[1].fire("click");
  assert.deepEqual(sought, [110]);
  assert.match(body.textContent, /You: By tenant/);
  assert.equal(filterTranscript(list, "shard"), 1);
  assert.equal(filterTranscript(list, ""), 2);
  const empty = new El("div");
  assert.equal(renderTranscript(doc, empty, {turns: [], message: "Couldn't read this recording's transcript from Fireflies."}), null);
  assert.match(empty.textContent, /from Fireflies/);
});

test("an audio-only recording plays in an audio element", async () => {
  const box = new El("div"); box.setAttribute("data-player", "fireflies:ff1");
  const video = new El("video"); video.replaceWith = node => { box.children = box.children.map(c => c === video ? node : c); };
  box.ownerDocument = doc;
  box.append(video);
  const player = createPlayer(box, {origin: "https://valr.example", now: () => 0, wait: async () => {},
    fetchMedia: async () => ({status: "completed", url: "https://cdn.fireflies.ai/a.mp3", content_type: "audio/mpeg"})});
  assert.equal(await player.load(), "playing");
  const audio = box.children[0];
  assert.equal(audio.tagName, "AUDIO");
  assert.equal(audio.getAttribute("src"), "https://cdn.fireflies.ai/a.mp3");
});
