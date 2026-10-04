import {decoder} from "./vendor/decode-wav/decode-wav.js";

// WAV parsing belongs to the library; this class only schedules decoded samples.
export class StreamAudioPlayer {
  constructor(onActiveChange) {
    this.onActiveChange = onActiveChange;
    this.context = null;
    this.sources = new Set();
    this.decoder = null;
    this.hasAudio = false;
    this.nextStartTime = 0;
    this.streamEnded = true;
  }

  async unlock() {
    // Record is a user gesture, so unlock playback before the provider responds.
    const AudioContext = window.AudioContext || window.webkitAudioContext;
    this.context ??= new AudioContext();
    await this.context.resume();
    if (this.context.state !== "running") throw new Error("Audio playback is blocked.");
  }

  start() {
    if (!this.context || this.context.state !== "running") throw new Error("Audio playback is blocked.");
    this.reset();
    this.decoder = decoder();
    this.streamEnded = false;
    this.nextStartTime = this.context.currentTime + 0.08;
    this.onActiveChange(true);
  }

  enqueue(bytes) {
    if (!this.decoder || this.streamEnded) throw new Error("Audio arrived outside a stream.");
    const {channelData, sampleRate} = this.decoder.decode(new Uint8Array(bytes));
    // Header-only chunks and partial samples need more input, not a playback error.
    if (!channelData.length || !channelData[0].length) return false;
    const buffer = this.context.createBuffer(channelData.length, channelData[0].length, sampleRate);
    for (let channel = 0; channel < channelData.length; channel++) {
      buffer.copyToChannel(channelData[channel], channel);
    }
    this.hasAudio = true;
    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.context.destination);
    this.sources.add(source);
    source.onended = () => {
      source.disconnect();
      this.sources.delete(source);
      this.finishIfDrained();
    };
    // Schedule in order; a late packet starts after a small gap rather than in the past.
    const start = Math.max(this.nextStartTime, this.context.currentTime + 0.02);
    source.start(start);
    this.nextStartTime = start + buffer.duration;
    return true;
  }

  finish() {
    if (!this.hasAudio) throw new Error("Speech stream contained no playable audio.");
    this.decoder?.free();
    this.decoder = null;
    this.streamEnded = true;
    this.finishIfDrained();
  }

  finishIfDrained() {
    if (this.streamEnded && this.sources.size === 0) {
      this.onActiveChange(false);
    }
  }

  reset() {
    for (const source of this.sources) {
      source.onended = null;
      source.stop();
      source.disconnect();
    }
    this.sources.clear();
    this.decoder?.free();
    this.decoder = null;
    this.hasAudio = false;
    this.streamEnded = true;
    this.onActiveChange(false);
  }

  close() {
    this.reset();
    if (this.context) void this.context.close();
  }
}
