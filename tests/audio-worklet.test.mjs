import {readFileSync} from "node:fs";
import vm from "node:vm";
import assert from "node:assert/strict";

for (const rate of [16000, 44100, 48000]) {
  const packets = [];
  let Processor;
  const sandbox = {
    sampleRate: rate, ArrayBuffer, DataView,
    AudioWorkletProcessor: class { constructor() {this.port = {postMessage: p => packets.push(p)};} },
    registerProcessor: (_, cls) => Processor = cls,
  };
  vm.runInNewContext(readFileSync("static/microphone-worklet.js", "utf8"), sandbox);
  const capture = new Processor();
  for (let offset = 0; offset < rate; offset += 128) {
    capture.process([[new Float32Array(Math.min(128, rate - offset)).fill(0.25)]]);
  }
  assert.equal(packets.length, 10);
  assert.equal(packets[0].byteLength, 6400);
  assert.equal(new DataView(packets[0]).getFloat32(0, true), 0.25);
}
console.log("Microphone packet sizing/resampling checks passed");
