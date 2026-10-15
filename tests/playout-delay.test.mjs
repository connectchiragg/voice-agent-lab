import assert from "node:assert/strict";
import test from "node:test";
import {playoutDelay} from "../static/playout-delay.js";

function stats(jitter, emitted, playout, samples, rtt = 0.04) {
  return new Map([
    ["T", {type: "transport", selectedCandidatePairId: "P"}],
    ["P", {type: "candidate-pair", nominated: true, currentRoundTripTime: rtt}],
    ["Q", {type: "candidate-pair", nominated: true, currentRoundTripTime: 9}],  // Not selected.
    ["I", {type: "inbound-rtp", kind: "audio", jitterBufferDelay: jitter, jitterBufferEmittedCount: emitted}],
    ["M", {type: "media-playout", totalPlayoutDelay: playout, totalSamplesCount: samples}],
  ]);
}

test("delay is half the selected round trip plus recent jitter-buffer and output delays", () => {
  const first = playoutDelay(stats(48, 960, 9.6, 960), {});
  assert.ok(Math.abs(first.delay - (0.02 + 0.05 + 0.01)) < 1e-9);
  // Averages cover only the samples since the last report: 80 ms of jitter buffer now.
  const second = playoutDelay(stats(48 + 76.8, 1920, 9.6 + 9.6, 1920), first.next);
  assert.ok(Math.abs(second.delay - (0.02 + 0.08 + 0.01)) < 1e-9);
});

test("no audio yet: only the network share counts", () => {
  const {delay} = playoutDelay(new Map([["P", {type: "candidate-pair", nominated: true,
    currentRoundTripTime: 0.1}]]), {});
  assert.equal(delay, 0.05);
});
