// Provider sends raw mono Float32 PCM; no WAV container needs decoding.
export class StreamAudioPlayer {
  constructor(onActiveChange) {
    this.onActiveChange = onActiveChange;
    this.context = null;
    this.sources = new Set();
    this.sampleRate = null;
    this.pendingBytes = new Uint8Array();
    this.hasAudio = false;
    this.nextStartTime = 0;
    this.streamEnded = true;
  }

  async unlock() {
    // Start call is a user gesture: unlock before the provider responds.
    const AudioContext = window.AudioContext || window.webkitAudioContext;
    this.context ??= new AudioContext();
    await this.context.resume();
    if (this.context.state !== "running") throw new Error("Audio playback is blocked.");
  }

  start({sample_rate, encoding}) {
    if (!this.context || this.context.state !== "running") throw new Error("Audio playback is blocked.");
    this.reset();
    if (encoding !== "pcm_f32le" || !Number.isInteger(sample_rate) || sample_rate < 8000 || sample_rate > 96000) {
      throw new Error("Unsupported speech format.");
    }
    this.sampleRate = sample_rate;
    this.streamEnded = false;
    this.nextStartTime = this.context.currentTime + 0.08;
  }

  enqueue(bytes) {
    if (this.streamEnded) throw new Error("Audio arrived outside a stream.");
    const combined = new Uint8Array(this.pendingBytes.length + bytes.byteLength);
    combined.set(this.pendingBytes);
    combined.set(new Uint8Array(bytes), this.pendingBytes.length);
    const sampleCount = Math.floor(combined.length / 4);
    this.pendingBytes = combined.slice(sampleCount * 4);
    if (!sampleCount) return false;
    const view = new DataView(combined.buffer);
    const samples = new Float32Array(sampleCount);
    for (let index = 0; index < sampleCount; index++) {
      const sample = view.getFloat32(index * 4, true);
      if (!Number.isFinite(sample)) throw new Error("Invalid PCM sample.");
      samples[index] = sample;
    }
    const buffer = this.context.createBuffer(1, sampleCount, this.sampleRate);
    buffer.copyToChannel(samples, 0);
    this.hasAudio = true;
    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.context.destination);
    this.sources.add(source);
    this.onActiveChange(true);
    source.onended = () => {
      source.disconnect();
      this.sources.delete(source);
      this.finishIfDrained();
    };
    // Schedule in order; a late packet starts after a small gap rather than in the past.
    const start = Math.max(this.nextStartTime, this.context.currentTime + 0.02);
    if (start - this.context.currentTime > 3) {
      this.sources.delete(source);
      source.disconnect();
      throw new Error("Playback cannot keep up with live audio.");
    }
    source.start(start);
    this.nextStartTime = start + buffer.duration;
    return true;
  }

  finish() {
    if (!this.hasAudio) throw new Error("Speech stream contained no playable audio.");
    if (this.pendingBytes.length) throw new Error("Incomplete PCM sample.");
    this.streamEnded = true;
    this.finishIfDrained();
  }

  finishIfDrained() {
    if (this.sources.size === 0) {
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
    this.sampleRate = null;
    this.pendingBytes = new Uint8Array();
    this.hasAudio = false;
    this.streamEnded = true;
    this.onActiveChange(false);
  }

  close() {
    this.reset();
    if (this.context) void this.context.close();
  }
}
