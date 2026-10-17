// "Call my phone" / "Talk here": static/valr-call.js's remembered default, live state and help,
// and static/webrtc-call.js's error wording and caption timing (no browser needed).
import test from "node:test";
import assert from "node:assert/strict";
import {DEFAULT_KEY, clock, defaultChoice, liveState, micHelp, readDefault, rememberDefault} from "../static/valr-call.js";
import {CaptionScheduler, describeError, supported} from "../static/webrtc-call.js";

function storage(initial = {}) {
  const box = {...initial};
  return {getItem: key => (key in box ? box[key] : null), setItem: (key, value) => { box[key] = String(value); }, box};
}

test("with nothing remembered, the default is the phone when there is one, else Talk here", () => {
  assert.equal(defaultChoice(null, true), "phone");
  assert.equal(defaultChoice(null, false), "talk");
  assert.equal(defaultChoice("nonsense", true), "phone");
});

test("the last choice is remembered and comes back after a reload", () => {
  const store = storage();
  assert.equal(rememberDefault(store, "talk"), true);
  assert.equal(store.box[DEFAULT_KEY], "talk");
  assert.equal(readDefault(store, true), "talk");  // A fresh page reads it back.
  rememberDefault(store, "phone");
  assert.equal(readDefault(store, true), "phone");
  assert.equal(rememberDefault(store, "fax"), false);
  assert.equal(readDefault(store, true), "phone");
});

test("a remembered phone choice falls back to Talk here once the phone is gone", () => {
  assert.equal(readDefault(storage({[DEFAULT_KEY]: "phone"}), false), "talk");
});

test("blocked storage (private mode) never breaks the button", () => {
  const blocked = {getItem() { throw new Error("SecurityError"); }, setItem() { throw new Error("QuotaExceeded"); }};
  assert.equal(readDefault(blocked, true), "phone");
  assert.equal(rememberDefault(blocked, "talk"), false);
  assert.equal(readDefault(undefined, false), "talk");
});

test("the panel's state follows the audio and the turn", () => {
  const now = 10000;
  assert.equal(liveState({phase: "listening", now}), "listening");
  assert.equal(liveState({phase: "listening", now, lastPartialAt: now - 500}), "hearing");
  assert.equal(liveState({phase: "thinking", now, lastPartialAt: now - 500}), "thinking");
  assert.equal(liveState({phase: "thinking", now, speakingUntil: now + 800}), "speaking");
  assert.equal(liveState({phase: "listening", now, voiceActiveAt: now - 100}), "speaking");
});

test("a denied microphone gets steps for this device", () => {
  const iphone = {userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)", maxTouchPoints: 5};
  assert.match(micHelp(iphone).join(" "), /Settings app.*Safari.*Microphone/);
  assert.match(micHelp({userAgent: "Mozilla/5.0 (Windows NT 10.0) Chrome/130", maxTouchPoints: 0}).join(" "), /lock/);
  assert.equal(describeError({name: "NotAllowedError"}).kind, "mic-denied");
  assert.equal(describeError({name: "NotFoundError"}).kind, "no-mic");
  assert.equal(describeError(new Error("Too many calls right now.")).text, "Too many calls right now.");
  assert.equal(supported({}), false);
  assert.equal(clock(125.7), "2:05");
});

test("captions reveal word by word in time with the audio, and stop on an interrupt", () => {
  const timers = [];
  const fake = {setTimeout: (fn, ms) => { timers.push({fn, ms}); return timers.length; }, clearTimeout: id => { timers[id - 1].fn = null; }};
  const words = [];
  const captions = new CaptionScheduler((line, word) => words.push(word), fake);
  captions.schedule({text: "Ready for the tip?", starts_in_s: 0.2, seconds: 1.0}, null);
  assert.deepEqual(timers.map(t => t.ms), [200, 450, 700, 950]);
  const first = timers[0].fn;
  timers[0].fn = null;  // A timer fires once.
  first();
  captions.cancel();
  timers.forEach(t => t.fn?.());
  assert.deepEqual(words, ["Ready"]);
});
