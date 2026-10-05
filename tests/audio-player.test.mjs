import assert from "node:assert/strict";
import test from "node:test";
import {StreamAudioPlayer} from "../static/audio-player.js";

function pcm(samples = [-1, 0.5, 0, 1]) {
  const bytes = new Uint8Array(samples.length * 4);
  const view = new DataView(bytes.buffer);
  samples.forEach((sample, index) => view.setFloat32(index * 4, sample, true));
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


async function playerWithAudio(sampleRate = 24000) {
  const changes = [];
  const player = new StreamAudioPlayer(active => changes.push(active));
  await player.unlock();
  player.start({sample_rate: sampleRate, encoding: "pcm_f32le"});
  return {player, changes};
}

for (const chunkSize of [1, 3, 4, 9, 4096]) {
  test(`raw PCM survives fragmented samples: chunk=${chunkSize}`, async () => {
    const {player} = await playerWithAudio();
    const bytes = pcm();
    for (let offset = 0; offset < bytes.length; offset += chunkSize) {
      player.enqueue(bytes.slice(offset, offset + chunkSize).buffer);
    }
    assert.deepEqual(player.context.buffers.flatMap(buffer => [...buffer.data[0]]), [-1, 0.5, 0, 1]);
    assert.ok(player.context.buffers.every(buffer => buffer.sampleRate === 24000));
    player.finish();
  });
}

test("playback begins before stream completion and waits for scheduled audio to drain", async () => {
  const {player, changes} = await playerWithAudio();
  player.enqueue(pcm([0.5]).buffer);
  player.enqueue(pcm([0.25]).buffer);
  assert.equal(player.context.sources[1].startTime,
    player.context.sources[0].startTime + player.context.buffers[0].duration);
  player.finish();
  assert.equal(changes.at(-1), true);
  for (const source of [...player.sources]) source.onended();
  assert.equal(changes.at(-1), false);
});

test("format is supplied explicitly for each reply", async () => {
  const {player} = await playerWithAudio();
  player.enqueue(pcm().buffer);
  const source = player.context.sources[0];
  player.start({sample_rate: 48000, encoding: "pcm_f32le"});
  assert.equal(source.stopped, true);
  player.enqueue(pcm().buffer);
  assert.equal(player.context.buffers.at(-1).sampleRate, 48000);
  assert.throws(() => player.start({sample_rate: 24000, encoding: "wav"}), /Unsupported/);
});

test("incomplete samples, empty streams and non-finite samples fail visibly", async () => {
  const {player} = await playerWithAudio();
  assert.throws(() => player.finish(), /no playable audio/);
  player.enqueue(pcm().buffer);
  player.enqueue(new Uint8Array([1]).buffer);
  assert.throws(() => player.finish(), /Incomplete PCM/);
  player.start({sample_rate: 24000, encoding: "pcm_f32le"});
  assert.throws(() => player.enqueue(pcm([NaN]).buffer), /Invalid PCM/);
});

test("disconnect stops playback and clears pending bytes", async () => {
  const {player, changes} = await playerWithAudio();
  player.enqueue(pcm().buffer);
  player.enqueue(new Uint8Array([1]).buffer);
  const sources = [...player.sources];
  player.close();
  assert.ok(sources.every(source => source.stopped));
  assert.equal(player.sources.size, 0);
  assert.equal(player.pendingBytes.length, 0);
  assert.equal(changes.at(-1), false);
  assert.equal(player.context.state, "closed");
});

test("playback needs a user-unlocked context and an active stream", async () => {
  const player = new StreamAudioPlayer(() => {});
  assert.throws(() => player.start({sample_rate: 24000, encoding: "pcm_f32le"}), /blocked/);
  await player.unlock();
  assert.throws(() => player.enqueue(pcm().buffer), /outside a stream/);
});
