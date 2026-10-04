import assert from "node:assert/strict";
import test from "node:test";
import {StreamAudioPlayer} from "../static/audio-player.js";

function wav({channels = 1, sampleRate = 24000, streamingSize = null, extended = false} = {}) {
  const samples = [-32768, 32767, 0, 16384];
  const fmtSize = extended ? 18 : 16;
  const dataStart = 28 + fmtSize;
  const bytes = new Uint8Array(dataStart + samples.length * 2);
  const view = new DataView(bytes.buffer);
  const label = (offset, text) => bytes.set(new TextEncoder().encode(text), offset);
  label(0, "RIFF");
  view.setUint32(4, streamingSize ?? bytes.length - 8, true);
  label(8, "WAVE");
  label(12, "fmt ");
  view.setUint32(16, fmtSize, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, channels, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * channels * 2, true);
  view.setUint16(32, channels * 2, true);
  view.setUint16(34, 16, true);
  label(dataStart - 8, "data");
  view.setUint32(dataStart - 4, streamingSize ?? samples.length * 2, true);
  samples.forEach((value, index) => view.setInt16(dataStart + index * 2, value, true));
  return bytes;
}

class FakeAudioContext {
  state = "running";
  currentTime = 0;
  destination = {};
  buffers = [];
  sources = [];
  async resume() {}
  async close() { this.state = "closed"; }
  createBuffer(channels, frames, sampleRate) {
    const data = Array.from({length: channels}, () => new Float32Array(frames));
    const buffer = {
      channels, frames, sampleRate, data, duration: frames / sampleRate,
      copyToChannel: (samples, channel) => data[channel].set(samples),
    };
    this.buffers.push(buffer);
    return buffer;
  }
  createBufferSource() {
    const source = {
      stopped: false, connect() {}, disconnect() {},
      start(time) { this.startTime = time; },
      stop() { this.stopped = true; },
    };
    this.sources.push(source);
    return source;
  }
}

globalThis.window = {AudioContext: FakeAudioContext};

async function playerWithAudio() {
  const changes = [];
  const player = new StreamAudioPlayer(active => changes.push(active));
  await player.unlock();
  player.start();
  return {player, changes};
}

for (const channels of [1, 2]) {
  for (const chunkSize of [1, 3, 47, 4096]) {
    for (const streamingSize of [null, 0, 0xffffffff]) {
      test(`decode fragmented WAV: channels=${channels}, chunk=${chunkSize}, size=${streamingSize}`, async () => {
        const {player} = await playerWithAudio();
        const bytes = wav({channels, streamingSize});
        for (let offset = 0; offset < bytes.length; offset += chunkSize) {
          player.enqueue(bytes.slice(offset, offset + chunkSize).buffer);
        }
        const decoded = Array.from({length: channels}, (_, channel) =>
          player.context.buffers.flatMap(buffer => [...buffer.data[channel]]));
        assert.deepEqual(decoded, channels === 1
          ? [[-1, 32767 / 32768, 0, 0.5]] : [[-1, 0], [32767 / 32768, 0.5]]);
        assert.ok(player.context.buffers.every(buffer => buffer.sampleRate === 24000));
        player.finish();
      });
    }
  }
}

test("Groq-style extended header plays before the final chunk, then waits for audio to drain", async () => {
  const {player, changes} = await playerWithAudio();
  const bytes = wav({extended: true, streamingSize: 0xffffffff});
  assert.equal(player.enqueue(bytes.slice(0, 45).buffer), false);
  assert.equal(player.enqueue(bytes.slice(45, 48).buffer), true);
  assert.equal(player.context.sources.length, 1); // Final provider bytes have not arrived.
  assert.equal(changes.at(-1), true);
  player.enqueue(bytes.slice(48).buffer);
  assert.equal(player.context.sources[1].startTime,
    player.context.sources[0].startTime + player.context.buffers[0].duration);
  player.finish();
  assert.equal(changes.at(-1), true);
  for (const source of [...player.sources]) source.onended();
  assert.equal(changes.at(-1), false);
});

test("each reply uses its own WAV metadata rather than hardcoded rate or channels", async () => {
  const {player} = await playerWithAudio();
  player.enqueue(wav().buffer);
  const oldSource = player.context.sources[0];
  player.start();
  assert.equal(oldSource.stopped, true);
  player.enqueue(wav({channels: 2, sampleRate: 48000}).buffer);
  assert.equal(player.context.buffers.at(-1).channels, 2);
  assert.equal(player.context.buffers.at(-1).sampleRate, 48000);
});

test("invalid header and header-only streams fail visibly; reset allows a later reply", async () => {
  const {player} = await playerWithAudio();
  assert.throws(() => player.enqueue(new TextEncoder().encode("not-a-wav").buffer), /Not a WAV/);
  player.start();
  player.enqueue(wav().slice(0, 44).buffer);
  assert.throws(() => player.finish(), /no playable audio/);
  player.start();
  assert.equal(player.enqueue(wav().buffer), true);
  player.finish();
  assert.throws(() => player.enqueue(wav().buffer), /outside a stream/);
});

test("disconnect stops scheduled sources and frees the decoder", async () => {
  const {player, changes} = await playerWithAudio();
  player.enqueue(wav().buffer);
  const sources = [...player.sources];
  player.close();
  assert.ok(sources.every(source => source.stopped));
  assert.equal(player.sources.size, 0);
  assert.equal(player.decoder, null);
  assert.equal(changes.at(-1), false);
  assert.equal(player.context.state, "closed");
});

test("playback requires a user-unlocked audio context", () => {
  const player = new StreamAudioPlayer(() => {});
  assert.throws(() => player.start(), /blocked/);
});
