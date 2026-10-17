// Interviews → Processes: static/valr-processes.js's playback logic (no browser needed).
import test from "node:test";
import assert from "node:assert/strict";
import {DAY_MS, REPLAY_S, advance, barEnd, countUpTo, describe, fromPosition, nextEventTime, prepare,
        recentEvents, snapshot, statusAt, toPosition} from "../static/valr-processes.js";

const T0 = Date.parse("2026-08-01T00:00:00Z");
const day = n => new Date(T0 + n * DAY_MS).toISOString();
const DATA = {
  range: {start: day(0), end: day(60)},
  processes: [
    {id: "a", company: "Aster", status: "offer", start: day(0), end: day(30), last_activity: day(30), fizzle_at: null, milestones: []},
    {id: "b", company: "Birch", status: "rejected", start: day(10), end: day(20), last_activity: day(20), fizzle_at: null, milestones: []},
    {id: "c", company: "Cobalt", status: "fizzled", start: day(5), end: day(8), last_activity: day(8), fizzle_at: day(29), milestones: []},
    {id: "d", company: "Dune", status: "active", start: day(40), end: null, last_activity: day(55), fizzle_at: null, milestones: []},
  ],
  events: [
    {at: day(10), process: "b", company: "Birch", kind: "email", label: "Email", title: "Hello"},
    {at: day(0), process: "a", company: "Aster", kind: "email", label: "Email", title: "Applied"},
    {at: day(20), process: "b", company: "Birch", kind: "rejection", label: "Rejection", title: "Update"},
    {at: day(30), process: "a", company: "Aster", kind: "offer", label: "Offer", title: "Offer"},
    {at: day(29), process: "c", company: "Cobalt", kind: "fizzled", label: "Fizzled out", title: ""},
    {at: day(40), process: "d", company: "Dune", kind: "email", label: "Email", title: "Hi"},
  ],
};

test("events are put in the order they happened", () => {
  const p = prepare(DATA);
  assert.deepEqual(p.events.map(e => e.company), ["Aster", "Birch", "Birch", "Cobalt", "Aster", "Dune"]);
  assert.equal(countUpTo(p.events, Date.parse(day(20))), 3);  // Events on the date itself count.
  assert.equal(countUpTo(p.events, Date.parse(day(-1))), 0);
});

test("a company lights up when its process starts and ends with its outcome", () => {
  const p = prepare(DATA);
  const [a, b, c, d] = p.processes;
  assert.equal(statusAt(b, Date.parse(day(5))), "future");
  assert.equal(statusAt(b, Date.parse(day(15))), "live");
  assert.equal(statusAt(b, Date.parse(day(20))), "rejected");
  assert.equal(statusAt(c, Date.parse(day(20))), "live");  // Quiet, but not yet fizzled out.
  assert.equal(statusAt(c, Date.parse(day(29))), "fizzled");
  assert.equal(statusAt(d, Date.parse(day(60))), "live");  // Active: never ends.
  assert.equal(barEnd(c, Date.parse(day(25))), Date.parse(day(8)));  // The bar stops at the last word.
  assert.equal(barEnd(a, Date.parse(day(12))), Date.parse(day(12)));
  assert.equal(barEnd(a, Date.parse(day(50))), Date.parse(day(30)));
});

test("dragging to a date shows the pipeline at that moment", () => {
  const p = prepare(DATA);
  assert.deepEqual(snapshot(p, Date.parse(day(15))), {live: 3, offer: 0, rejected: 0, withdrew: 0, fizzled: 0, future: 1});
  assert.deepEqual(snapshot(p, Date.parse(day(45))), {live: 1, offer: 1, rejected: 1, withdrew: 0, fizzled: 1, future: 0});
  assert.equal(describe(snapshot(p, Date.parse(day(45)))), "1 live · 1 offer · 1 rejected · 1 fizzled out");
  assert.deepEqual(snapshot(p, Date.parse(day(45)), "b"), {live: 0, offer: 0, rejected: 1, withdrew: 0, fizzled: 0, future: 0});
  assert.deepEqual(recentEvents(p, Date.parse(day(25)), 2).map(e => e.kind), ["rejection", "email"]);
  assert.deepEqual(recentEvents(p, Date.parse(day(60)), 5, "a").map(e => e.kind), ["offer", "email"]);
});

test("the scrubber maps both ways over the whole range", () => {
  const p = prepare(DATA);
  assert.equal(toPosition(p, p.start), 0);
  assert.equal(toPosition(p, p.end), 1000);
  assert.equal(fromPosition(p, 500), Date.parse(day(30)));
  assert.equal(fromPosition(p, 5000), p.end);
});

test("playback covers the range in REPLAY_S seconds at 1x, twice as fast at 2x, and stops at the end", () => {
  const p = prepare(DATA);
  const oneSecond = advance(p, p.start, 1000, 1) - p.start;
  assert.equal(Math.round(oneSecond), Math.round((p.end - p.start) / REPLAY_S));
  assert.equal(Math.round(advance(p, p.start, 1000, 2) - p.start), Math.round(2 * oneSecond));
  assert.equal(Math.round(advance(p, p.start, 1000, 4) - p.start), Math.round(4 * oneSecond));
  assert.equal(advance(p, p.end - 1, 5000, 4), p.end);
});

test("reduced motion steps from one event to the next, optionally one company's", () => {
  const p = prepare(DATA);
  assert.equal(nextEventTime(p, Date.parse(day(0)) + 1), Date.parse(day(10)));
  assert.equal(nextEventTime(p, Date.parse(day(0)) + 1, "a"), Date.parse(day(30)));
  assert.equal(nextEventTime(p, Date.parse(day(41))), p.end);
});

test("an empty or broken payload prepares to nothing instead of throwing", () => {
  const p = prepare({});
  assert.deepEqual(p.processes, []);
  assert.deepEqual(p.events, []);
});
